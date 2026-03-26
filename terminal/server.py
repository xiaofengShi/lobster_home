#!/usr/bin/env python3
"""
🦞 Lobster Terminal — Web Backend v2
Routes voice/text to LobsterHive agent via OpenClaw Gateway WebSocket.

Architecture:
  iPhone Safari ─→ this server ─→ OpenClaw Gateway WS ─→ lobsterhive agent
                                                              ↓
                                                     蜂巢 Queen (看家/天气/设备)

Run: python3 server.py
"""

import asyncio
import json
import os
import subprocess
import tempfile
import time
import threading
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

# Load .env.local
env_file = Path(__file__).parent.parent / ".env.local"
if env_file.exists():
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                val = val.strip().strip('"').strip("'")
                os.environ.setdefault(key, val)

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENCLAW_HOST = os.environ.get("OPENCLAW_HOST", "127.0.0.1")
OPENCLAW_PORT = os.environ.get("OPENCLAW_PORT", "18789")
OPENCLAW_TOKEN = os.environ.get("OPENCLAW_TOKEN", "your-openclaw-token")
LISTEN_PORT = int(os.environ.get("LOBSTER_WEB_PORT", "8091"))
TARGET_AGENT = os.environ.get("LOBSTER_AGENT", "lobsterhive")
SESSION_KEY = f"agent:{TARGET_AGENT}:main"

ACCESS_PASSWORD = os.environ.get("ACCESS_PASSWORD", "")  # 空字符串=不需要密码
STATIC_DIR = Path(__file__).parent / "static"

# 简单的内存 session 存储
import hashlib, secrets
_valid_sessions: set = set()

def _check_auth(handler) -> bool:
    """检查请求是否已认证。无密码设置时直接通过。"""
    if not ACCESS_PASSWORD:
        return True
    cookie = handler.headers.get("Cookie", "")
    for part in cookie.split(";"):
        part = part.strip()
        if part.startswith("lobster_sess="):
            token = part[len("lobster_sess="):]
            if token in _valid_sessions:
                return True
    return False

def _json_401(handler):
    handler.send_response(401)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(b'{"error":"unauthorized"}')


def send_to_gateway(text: str, timeout: int = 60) -> str:
    """Send message to OpenClaw Gateway via HTTP OpenAI-compatible API."""
    import urllib.request
    import urllib.error

    url = f"http://{OPENCLAW_HOST}:{OPENCLAW_PORT}/v1/chat/completions"
    payload = json.dumps({
        "model": f"openclaw:{TARGET_AGENT}",
        "messages": [{"role": "user", "content": text}],
        "stream": False
    }).encode()

    req = urllib.request.Request(url, data=payload, headers={
        "Authorization": f"Bearer {OPENCLAW_TOKEN}",
        "Content-Type": "application/json",
        "x-openclaw-agent-id": TARGET_AGENT,
        "x-openclaw-session-key": SESSION_KEY,
    })

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
            return data["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        return f"❌ HTTP {e.code}: {e.read().decode()[:200]}"
    except Exception as e:
        return f"❌ Error: {e}"


class LobsterHandler(BaseHTTPRequestHandler):
    """Handle web requests for the lobster terminal."""

    def do_GET(self):
        path = urlparse(self.path).path

        # 登录页直接放行
        if path == "/login":
            self._serve_login_page()
            return

        # 其他页面需要认证
        if not _check_auth(self):
            if path == "/" or path.endswith(".html"):
                self.send_response(302)
                self.send_header("Location", "/login")
                self.end_headers()
            else:
                _json_401(self)
            return

        if path == "/":
            path = "/index.html"

        filepath = STATIC_DIR / path.lstrip("/")
        if filepath.exists() and filepath.is_file():
            content = filepath.read_bytes()
            ctype = {
                ".html": "text/html",
                ".js": "application/javascript",
                ".css": "text/css",
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".ico": "image/x-icon",
            }.get(filepath.suffix, "application/octet-stream")
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(content)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path

        # 登录接口不需要认证
        if path == "/api/login":
            self._handle_login()
            return

        # 其他 API 需要认证
        if not _check_auth(self):
            _json_401(self)
            return

        handlers = {
            "/api/chat": self._handle_chat,
            "/api/stt": self._handle_stt,
            "/api/tts": self._handle_tts,
            "/api/status": self._handle_status,
        }
        handler = handlers.get(path)
        if handler:
            handler()
        else:
            self.send_response(404)
            self.end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _handle_chat(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        text = body.get("text", "")

        if not text:
            self._json_response({"error": "empty text"}, 400)
            return

        try:
            reply = send_to_gateway(text)
            self._json_response({"reply": reply})
        except Exception as e:
            self._json_response({"error": str(e)}, 500)

    def _handle_stt(self):
        length = int(self.headers.get("Content-Length", 0))
        audio_data = self.rfile.read(length)

        if not audio_data:
            self._json_response({"error": "no audio data"}, 400)
            return

        try:
            from urllib.request import Request, urlopen

            boundary = "----LobsterBoundary"
            parts = []
            parts.append(f"--{boundary}\r\n".encode())
            parts.append(b'Content-Disposition: form-data; name="file"; filename="audio.webm"\r\n')
            parts.append(b"Content-Type: audio/webm\r\n\r\n")
            parts.append(audio_data)
            parts.append(f"\r\n--{boundary}\r\n".encode())
            parts.append(b'Content-Disposition: form-data; name="model"\r\n\r\nwhisper-1\r\n')
            parts.append(f"--{boundary}\r\n".encode())
            parts.append(b'Content-Disposition: form-data; name="language"\r\n\r\nzh\r\n')
            parts.append(f"--{boundary}--\r\n".encode())
            body = b"".join(parts)

            req = Request(
                "https://api.openai.com/v1/audio/transcriptions",
                data=body,
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                },
                method="POST",
            )
            with urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
                text = result.get("text", "")

            self._json_response({"text": text})
        except Exception as e:
            self._json_response({"error": str(e)}, 500)

    def _handle_tts(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        text = body.get("text", "")

        if not text:
            self._json_response({"error": "empty text"}, 400)
            return

        try:
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                tmp_mp3 = f.name

            script = f"""
import asyncio, edge_tts
async def gen():
    communicate = edge_tts.Communicate({repr(text)}, "zh-CN-YunxiaNeural")
    await communicate.save({repr(tmp_mp3)})
asyncio.run(gen())
"""
            subprocess.run(
                ["/opt/miniconda3/envs/voice-copilot/bin/python3", "-c", script],
                timeout=30,
                capture_output=True,
            )

            if os.path.exists(tmp_mp3) and os.path.getsize(tmp_mp3) > 0:
                with open(tmp_mp3, "rb") as f:
                    mp3_data = f.read()
                os.unlink(tmp_mp3)
                self.send_response(200)
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(mp3_data)
            else:
                if os.path.exists(tmp_mp3):
                    os.unlink(tmp_mp3)
                self._json_response({"error": "TTS generation failed"}, 500)
        except Exception as e:
            self._json_response({"error": str(e)}, 500)

    def _handle_status(self):
        self._json_response({
            "status": "online",
            "name": "🦞 龙虾管家 Alan",
            "agent": TARGET_AGENT,
            "session": SESSION_KEY,
            "version": "2.0.0",
            "timestamp": time.time(),
        })

    def _handle_login(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        pwd = body.get("password", "")
        if ACCESS_PASSWORD and pwd == ACCESS_PASSWORD:
            token = secrets.token_hex(32)
            _valid_sessions.add(token)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie", f"lobster_sess={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=2592000")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
        else:
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"wrong password"}')

    def _serve_login_page(self):
        html = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>🦞 龙虾管家</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;background:linear-gradient(135deg,#1a1a2e,#16213e,#0f3460);min-height:100vh;display:flex;align-items:center;justify-content:center;color:#fff}
.box{background:rgba(255,255,255,0.08);backdrop-filter:blur(20px);border:1px solid rgba(255,255,255,0.15);border-radius:20px;padding:40px 32px;width:320px;text-align:center}
.logo{font-size:48px;margin-bottom:12px}
h1{font-size:20px;margin-bottom:8px;font-weight:600}
p{color:rgba(255,255,255,0.5);font-size:14px;margin-bottom:28px}
input{width:100%;padding:14px 16px;background:rgba(255,255,255,0.1);border:1px solid rgba(255,255,255,0.2);border-radius:12px;color:#fff;font-size:16px;outline:none;margin-bottom:16px;-webkit-appearance:none}
input:focus{border-color:rgba(100,160,255,0.6)}
button{width:100%;padding:14px;background:linear-gradient(135deg,#667eea,#764ba2);border:none;border-radius:12px;color:#fff;font-size:16px;font-weight:600;cursor:pointer}
.err{color:#ff6b6b;font-size:13px;margin-top:12px;display:none}
</style>
</head>
<body>
<div class="box">
  <div class="logo">🦞</div>
  <h1>龙虾管家</h1>
  <p>请输入访问密码</p>
  <input type="password" id="pwd" placeholder="密码" autofocus>
  <button onclick="login()">进入</button>
  <div class="err" id="err">密码错误</div>
</div>
<script>
document.getElementById("pwd").addEventListener("keypress",e=>{if(e.key==="Enter")login()});
async function login(){
  const pwd=document.getElementById("pwd").value;
  const r=await fetch("/api/login",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({password:pwd})});
  if(r.ok){window.location.href="/"}
  else{document.getElementById("err").style.display="block"}
}
</script>
</body>
</html>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def _json_response(self, data, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def log_message(self, fmt, *args):
        print(f"[{time.strftime('%H:%M:%S')}] {args[0]}")


if __name__ == "__main__":
    STATIC_DIR.mkdir(exist_ok=True)
    print(f"🦞 Lobster Terminal Web Server v2")
    print(f"   Agent: {TARGET_AGENT} (session: {SESSION_KEY})")
    print(f"   Gateway: ws://{OPENCLAW_HOST}:{OPENCLAW_PORT}")
    print(f"   STT: Groq Whisper ({'✅' if GROQ_API_KEY else '❌ MISSING KEY'})")
    print(f"   TTS: Edge TTS (zh-CN-YunxiaNeural)")
    print(f"   Listen: http://0.0.0.0:{LISTEN_PORT}")
    print(f"   Static: {STATIC_DIR}")
    print()
    print(f"   📱 Open http://YOUR_MAC_IP:{LISTEN_PORT} on your iPhone")

    server = HTTPServer(("0.0.0.0", LISTEN_PORT), LobsterHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n🦞 Shutting down")
        server.shutdown()
