#!/usr/bin/env python3
"""Anthropic API 中继服务

在可以访问 api.anthropic.com 的机器上运行，接收 HTTP 请求并转发到 Anthropic API。
通过 SSH 反向隧道暴露到不能直接访问 Anthropic 的服务器。

用法:
    python3 relay.py [--port 18888]

配合 SSH 反向隧道:
    ssh -R 18888:127.0.0.1:18888 deploy@server

服务器端 Gateway 配置:
    ANTHROPIC_API_RELAY=http://127.0.0.1:18888
"""

import http.server
import json
import ssl
import sys
import urllib.request
import urllib.error
from urllib.parse import urlparse

ANTHROPIC_API = 'https://api.anthropic.com'
DEFAULT_PORT = 18888


class RelayHandler(http.server.BaseHTTPRequestHandler):
    """将 HTTP 请求转发到 Anthropic API (HTTPS)"""

    def do_POST(self):
        target_url = ANTHROPIC_API + self.path
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length) if content_length > 0 else None

        # 透传所有请求头
        headers = {}
        for key in ('Content-Type', 'Authorization', 'anthropic-version', 'anthropic-beta',
                     'x-api-key'):
            val = self.headers.get(key)
            if val:
                headers[key] = val

        try:
            req = urllib.request.Request(target_url, data=body, headers=headers, method='POST')
            with urllib.request.urlopen(req, timeout=300) as resp:
                resp_body = resp.read()
                self.send_response(resp.status)
                self.send_header('Content-Type', resp.headers.get('Content-Type', 'application/json'))
                self.send_header('Content-Length', str(len(resp_body)))
                self.end_headers()
                self.wfile.write(resp_body)
        except urllib.error.HTTPError as e:
            err_body = e.read()
            self.send_response(e.code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(err_body)))
            self.end_headers()
            self.wfile.write(err_body)
        except Exception as e:
            err = json.dumps({'error': str(e)}).encode()
            self.send_response(502)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(err)))
            self.end_headers()
            self.wfile.write(err)

    def do_GET(self):
        """健康检查"""
        if self.path == '/health':
            body = json.dumps({'ok': True, 'relay': 'anthropic-api'}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        sys.stderr.write(f'[relay] {self.client_address[0]} - {format % args}\n')


def main():
    port = DEFAULT_PORT
    if len(sys.argv) > 1:
        for i, arg in enumerate(sys.argv[1:]):
            if arg == '--port' and i + 2 <= len(sys.argv[1:]):
                port = int(sys.argv[i + 2])

    server = http.server.HTTPServer(('127.0.0.1', port), RelayHandler)
    print(f'[relay] Anthropic API relay listening on 127.0.0.1:{port}')
    print(f'[relay] Forwarding to {ANTHROPIC_API}')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n[relay] Shutting down')
        server.shutdown()


if __name__ == '__main__':
    main()
