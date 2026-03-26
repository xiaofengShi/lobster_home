# 🦞 Lobster Terminal

把旧手机/任意浏览器变成 AI 语音终端，对话接入 [OpenClaw](https://openclaw.ai) Agent。

![demo](https://img.shields.io/badge/platform-iOS%20%7C%20Android%20%7C%20Desktop-blue)
![python](https://img.shields.io/badge/python-3.9%2B-green)

## 功能

- 🎤 **语音对话**：按住按钮说话 → Whisper 转文字 → OpenClaw Agent 回复 → TTS 播放
- ⌨️ **文字输入**：打字输入，按回车发送
- 📱 **移动端优化**：支持 iOS Safari / Android Chrome，可添加到主屏幕

## 快速开始

### 1. 安装依赖

```bash
pip install edge-tts
```

> Python 标准库即可运行，无需额外框架。

### 2. 配置环境变量

复制并填写配置：

```bash
cp .env.example .env.local
```

编辑 `.env.local`：

```env
# OpenAI API Key（用于 Whisper 语音识别）
OPENAI_API_KEY=sk-xxxxxxxxxxxxxxxx

# OpenClaw Gateway 地址（默认本机）
OPENCLAW_HOST=127.0.0.1
OPENCLAW_PORT=18789

# OpenClaw 访问 Token（在 openclaw config 里查看）
OPENCLAW_TOKEN=your-openclaw-token

# 目标 Agent 名称
LOBSTER_AGENT=your-agent-name

# 服务端口
LOBSTER_WEB_PORT=8091
```

### 3. 启动服务

```bash
python3 server.py
```

### 4. 访问

**局域网**（同一 WiFi 下）：

```
http://你的Mac IP:8091
```

> ⚠️ 局域网 HTTP 访问时麦克风不可用（浏览器安全限制）。需要 HTTPS 才能使用麦克风，见下文。

**公网 HTTPS（推荐）**：

见 [配置 HTTPS 访问](#配置-https-访问) 一节。

---

## 配置 HTTPS 访问

### 方案 A：Cloudflare Tunnel（推荐，免费，URL 固定）

> 需要有自己的域名并将 DNS 托管到 Cloudflare。

```bash
# 安装 cloudflared
brew install cloudflare/cloudflare/cloudflared

# 登录 Cloudflare（会弹出浏览器授权）
cloudflared tunnel login

# 创建隧道
cloudflared tunnel create lobster-home

# 绑定子域名（替换为你的域名）
cloudflared tunnel route dns lobster-home alan.yourdomain.com

# 写配置文件 ~/.cloudflared/config.yml
cat > ~/.cloudflared/config.yml << EOF
tunnel: <你的 Tunnel ID>
credentials-file: /Users/<你>/.cloudflared/<Tunnel ID>.json

ingress:
  - hostname: alan.yourdomain.com
    service: http://localhost:8091
  - service: http_status:404
EOF

# 启动隧道
cloudflared tunnel run lobster-home

# 设为开机自启（macOS）
cloudflared service install
```

访问 `https://alan.yourdomain.com` 即可，麦克风全平台可用。

---

### 方案 B：自签证书（局域网 HTTPS）

```bash
# 生成证书（替换为你的 Mac IP）
openssl req -x509 -newkey rsa:2048 \
  -keyout /tmp/lobster.key -out /tmp/lobster.crt \
  -days 365 -nodes -subj "/CN=lobster-terminal" \
  -addext "subjectAltName=IP:192.168.x.x,IP:127.0.0.1"

# 用 HTTPS 启动
python3 start_https.py
```

浏览器会提示「证书不安全」→ 点「继续访问」即可。

---

## 技术架构

```
手机/电脑浏览器
     ↓ HTTP/HTTPS
  server.py（Python）
     ↓ OpenAI API（Whisper STT）
     ↓ Edge TTS（文字转语音）
     ↓ HTTP
  OpenClaw Gateway
     ↓
  你的 Agent
```

| 组件 | 技术 |
|------|------|
| 前端 | 原生 HTML/CSS/JS，无框架 |
| 后端 | Python 内置 http.server |
| STT  | OpenAI Whisper API |
| TTS  | Edge TTS (zh-CN-YunxiaNeural) |
| Agent | OpenClaw Gateway |

---

## 添加到手机主屏幕

1. Safari 打开页面
2. 点击底部分享按钮 →「添加到主屏幕」
3. 像 App 一样打开，全屏体验

---

## 常见问题

**麦克风提示 `undefined is not an object (evaluating 'navigator.mediaDevices.getUserMedia')`**

原因：HTTP 协议下浏览器不开放麦克风 API。解决：使用 HTTPS 访问（见上文配置方案）。

**页面打不开**

检查 `server.py` 是否在运行，以及防火墙是否放行 8091 端口。

**语音识别不准**

确认 `OPENAI_API_KEY` 已正确配置。
