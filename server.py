#!/usr/bin/env python3
"""
MLB Algo Proxy Server
- Local: python server.py (serves on port 8080 + static files)
- Cloud: Deploy to Render.com (proxy only, no static files needed)
"""
import http.server, urllib.request, json, os

PORT = int(os.environ.get('PORT', 8080))
IS_CLOUD = os.environ.get('RENDER', '') != ''  # Render sets this env var

PROXY_RULES = {
    '/fg/':      'https://www.fangraphs.com/api/scores/',
    '/mlb-api/': 'https://statsapi.mlb.com/api/v1/',
}

FG_HEADERS = {
    'User-Agent':      'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept':          'application/json, text/plain, */*',
    'Referer':         'https://www.fangraphs.com/scores',
    'Origin':          'https://www.fangraphs.com',
    'Accept-Language': 'en-US,en;q=0.9',
    'Cookie':          'wordpress_logged_in_0cae6f5cb929d209043cb97f8c2eee44=Epemstein%7C1804794039%7CVbo4EUNM0l4lEQ01rf6OjCjddQIldJExT4vDBBVIe9u%7C8ad64507ab61ff6e0297bbb71ea01690d90cfbe1e54b846e8dad03661d4eb64f',
}

MLB_HEADERS = {
    'User-Agent': 'Mozilla/5.0',
    'Accept':     'application/json',
}

class Handler(http.server.BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        # Health check for Render
        if self.path == '/health':
            self.send_response(200)
            self._cors()
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'ok')
            return

        # Proxy routes
        for prefix, upstream_base in PROXY_RULES.items():
            if self.path.startswith(prefix):
                tail = self.path[len(prefix):]
                upstream = upstream_base + tail
                hdrs = FG_HEADERS if 'fangraphs' in upstream_base else MLB_HEADERS
                self._proxy(upstream, hdrs)
                return

        # Static files (local only)
        if not IS_CLOUD:
            http.server.SimpleHTTPRequestHandler(self.request, self.client_address, self.server).do_GET()
            return

        self.send_error(404)

    def _proxy(self, url, headers):
        try:
            req = urllib.request.Request(url, headers=headers)
            resp = urllib.request.urlopen(req, timeout=15)
            data = resp.read()
            self.send_response(200)
            self._cors()
            self.send_header('Content-Type', resp.headers.get('Content-Type', 'application/json'))
            self.send_header('Cache-Control', 'max-age=60')
            self.end_headers()
            self.wfile.write(data)
            print(f'  ✓ {url[:80]}')
        except Exception as e:
            print(f'  ✗ {url[:80]} — {e}')
            self.send_response(502)
            self._cors()
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': str(e)}).encode())

    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', '*')

    def log_message(self, fmt, *args):
        print(f'[{self.address_string()}] {fmt % args}')

if __name__ == '__main__':
    if not IS_CLOUD:
        os.chdir(os.path.dirname(os.path.abspath(__file__)))
    print(f'\n  MLB Algo {"Cloud Proxy" if IS_CLOUD else "Dashboard"}')
    print(f'  Port: {PORT}\n')
    http.server.HTTPServer(('0.0.0.0', PORT), Handler).serve_forever()
