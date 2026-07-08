#!/usr/bin/env python3
"""在 3000 端口做反向代理，注入搜索引擎钩子 + /control 面板"""
import http.server
import urllib.request
import urllib.error
import os
import sys

NEXT_PORT = 3002
LISTEN_PORT = 3000
PANEL_PATH = "/DATA/AppData/search_engine/panel.html"

# 预加载面板 HTML
panel_html = ""
try:
    with open(PANEL_PATH, "r", encoding="utf-8") as f:
        panel_html = f.read()
    print(f"[proxy] Panel loaded ({len(panel_html)} bytes)")
except Exception as e:
    print(f"[proxy] Panel load failed: {e}")

# 注入到 Next.js 页面的钩子脚本
INJECT_SCRIPT = """
<script>
(function(){
  if(window.__se_injected)return;window.__se_injected=1;
  var style=document.createElement('style');
  style.textContent='#se-btn{position:fixed;bottom:20px;right:20px;z-index:9999;background:#6d28d9;color:#fff;border:none;padding:10px 18px;border-radius:20px;cursor:pointer;font-size:14px;box-shadow:0 4px 20px rgba(109,40,217,0.4);transition:all .2s}#se-btn:hover{transform:scale(1.05)}#se-overlay{display:none;position:fixed;top:0;left:0;width:100%;height:100%;z-index:10000;background:rgba(0,0,0,0.85)}#se-overlay.show{display:flex;justify-content:center;align-items:center}#se-frame{width:95%;height:90%;border:none;border-radius:12px;background:#0a0a0a}';
  document.head.appendChild(style);
  var div=document.createElement('div');
  div.innerHTML='<button id="se-btn" onclick="document.getElementById(\'se-overlay\').classList.add(\'show\');document.getElementById(\'se-frame\').src=\'http://'+location.hostname+':3001/\'">⚡ 搜索控制台</button><div id="se-overlay" onclick="if(event.target===this)this.classList.remove(\'show\')"><iframe id="se-frame" src=""></iframe></div>';
  document.body.appendChild(div);
  console.log('[SearchEngine] Control panel injected - click bottom-right button');
})();
</script>
"""

class ProxyHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        # /control → 服务面板
        if self.path == "/control" or self.path.startswith("/control?"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(panel_html.encode("utf-8"))
            return

        # 代理到 Next.js
        target_url = f"http://127.0.0.1:{NEXT_PORT}{self.path}"
        try:
            req = urllib.request.Request(target_url)
            resp = urllib.request.urlopen(req, timeout=30)
            content = resp.read()
            ctype = resp.headers.get("Content-Type", "")

            self.send_response(resp.status)
            for k, v in resp.headers.items():
                if k.lower() != "transfer-encoding":
                    self.send_header(k, v)
            self.end_headers()

            # 注入脚本到 HTML
            if "text/html" in ctype and b"</body>" in content:
                content = content.replace(b"</body>", INJECT_SCRIPT.encode() + b"</body>")
            self.wfile.write(content)
        except Exception as e:
            self.send_response(502)
            self.end_headers()
            self.wfile.write(f"Proxy Error: {e}".encode())

    def do_POST(self):
        target_url = f"http://127.0.0.1:{NEXT_PORT}{self.path}"
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        try:
            req = urllib.request.Request(target_url, data=body, method="POST")
            for k, v in self.headers.items():
                if k.lower() not in ("host", "content-length"):
                    req.add_header(k, v)
            resp = urllib.request.urlopen(req, timeout=30)
            content = resp.read()
            self.send_response(resp.status)
            for k, v in resp.headers.items():
                if k.lower() != "transfer-encoding":
                    self.send_header(k, v)
            self.end_headers()
            # Also inject into any POST responses that are HTML
            ctype = resp.headers.get("Content-Type", "")
            if "text/html" in ctype and b"</body>" in content:
                content = content.replace(b"</body>", INJECT_SCRIPT.encode() + b"</body>")
            self.wfile.write(content)
        except Exception as e:
            self.send_response(502)
            self.end_headers()
            self.wfile.write(f"Proxy Error: {e}".encode())

    def log_message(self, format, *args):
        pass  # 静默日志

if __name__ == "__main__":
    server = http.server.HTTPServer(("0.0.0.0", LISTEN_PORT), ProxyHandler)
    print(f"[proxy] Listening on :{LISTEN_PORT} → Next.js on :{NEXT_PORT}")
    server.serve_forever()
