#!/usr/bin/env python3
"""启动 HTTPS 版 Lobster Terminal，解决 Safari 麦克风限制"""
import ssl, sys, os
sys.path.insert(0, os.path.dirname(__file__))

# 先 import server 模块里的东西
import importlib.util
spec = importlib.util.spec_from_file_location("server", os.path.join(os.path.dirname(__file__), "server.py"))

from http.server import HTTPServer
import server as srv

CERT = "/tmp/lobster.crt"
KEY  = "/tmp/lobster.key"
PORT = int(os.environ.get("LOBSTER_WEB_PORT", "8091"))

httpd = HTTPServer(("0.0.0.0", PORT), srv.LobsterHandler)
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain(CERT, KEY)
httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)

print(f"🦞 HTTPS 启动: https://192.168.31.121:{PORT}")
print("📱 iPhone Safari 打开上面的地址（会提示证书不安全，点「继续」即可）")
try:
    httpd.serve_forever()
except KeyboardInterrupt:
    print("关闭")
