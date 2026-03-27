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
    '/fg/':       'https://www.fangraphs.com/api/scores/',
    '/fg-proj/':  'https://www.fangraphs.com/api/projections',
    '/mlb-api/':  'https://statsapi.mlb.com/api/v1/',
}

FG_HEADERS = {
    'User-Agent':      'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept':          'application/json, text/plain, */*',
    'Referer':         'https://www.fangraphs.com/projections',
    'Origin':          'https://www.fangraphs.com',
    'Accept-Language': 'en-US,en;q=0.9',
    'Cookie':          '_au_1d=AU1D-0100-001748981788-GG2G0RYJ-D9S4; cf_clearance=BzW0fAcwvff_.OM4Mq.ekVttgaMSPqBDzCe1mjGB_Hw-1753973712-1.2.1.1-iDX3AyWCi8T2kVRXB6NGosfOwY9q1bQt50B98TiWZak5EgMbOeDSysFTGTsIsj33_FbuGcktUHqVJ9uVMV3KUfywnL9IV4QijJcSIpgSOW2EPfzvcr7AV42TX_ovytqfA36EpT6G2rC02RqN86WDR1dwUDZ4YyvMlEnpL9MBr4CejkS52xkwFy0iVZ_eJ1FgC9dJC8351rbtjcj_cE4iNPkJ5mMs_IoIGH_FGpiqzw0; fg_uuid=6d8415ad-fd39-42e2-80e2-4824c15b1a0e; mm-user-id=C5Dtbtkw3rahQuFF; __qca=P1-db630c2d-d885-402f-aaec-0ba188abfc9e; _cc_id=40ebdcf2badbcc744c17d835c1dcfa5f; _ga=GA1.1.373109119.1767974795; 33acrossIdTp=dlSdPqXS7WEtljhXUkKinGv91LsR2phydPzNpWgSD5I%3D; cto_bundle=nLLol19RQjQ3YzVKbiUyRnBMQ1o5aTJTazdaUVljZk42YjVmOVQ2d3Q3aW5ybTNKRDZiWCUyQjFhTzdPRDgxQ3V1NlI2WHY4JTJCMGt2aW40Vm1IUnlhYyUyQmhMNjQxclNGQTBxeFhKT2NCN0VwTmZsdmhrTVU2WE1GMm9GVnFEaHQzem4zZUlEZWlCNTZHVE4lMkZTOHc2QzdCYjVOZk1ZM0VRJTNEJTNE; cto_bidid=mlQVd19KemRvcTloMjdEcDltSlZtdXVjT3FYRXdoQU5IdGZCZFklMkZtUktORnYyS2J4VGhHWTB6TTR2N1E3MFNLakhQdERuWVg2JTJCSHpORVlzN1hoVU11RkJNVlhualBqeXVMcnBQdEFBaVpvcEV2NzAlM0Q; cto_dna_bundle=8QDtdl9RQjQ3YzVKbiUyRnBMQ1o5aTJTazdaUVVxZHUyRG55WERCaXZna0JwTHdHZm1XWUkzaFFwOGhLMFRMNzdUa1poSVp2MVNWQXp5dTAlMkJxNEpjc2dOaW9WWEElM0QlM0Q; _ga_FVWZ0RM4DH=GS2.1.s1768419318$o2$g1$t1768419330$j48$l0$h0; wordpress_logged_in_0cae6f5cb929d209043cb97f8c2eee44=Epemstein%7C1799976258%7CTwGN3KpQ8kEMWJkUaRXAyfqIo3ctQwOam4womAV6ogv%7C8b8d9d6c562305d72eaffe3574855d0a70e5cc180fde0b4a7f0e73616c86308d; fg_is_member=true; _sharedID=491613f0-bf22-4dfa-b48a-3f4d566734d0; _sharedID_cst=zix7LPQsHA%3D%3D; _pubcid=f52cd2fd-ef08-4d4c-bfa8-992e9a1deb8d; theme-fg_seasonal-banner__feb2026=RANDOM; _ga_R079V0VW20=GS2.1.s1774385453$o2$g1$t1774386511$j60$l0$h0; _ga_G0RN1B30CD=GS2.1.s1774624661$o23$g1$t1774624663$j58$l0$h0; FCCDCF=%5Bnull%2Cnull%2Cnull%2Cnull%2Cnull%2Cnull%2C%5B%5B32%2C%22%5B%5C%22f7aa1c23-61c8-4817-b66f-ccceaebcb5e2%5C%22%2C%5B1767973289%2C156000000%5D%5D%22%5D%5D%5D; __gads=ID=4ca96541c6a551c7:T=1748981788:RT=1774624664:S=ALNI_MacNXBHYzINsDGrfp25z3KkSED8Hg; __gpi=UID=000010d26e75068e:T=1748981788:RT=1774624664:S=ALNI_Ma0lzL9EFcUh8vZkl7mpySvobNplg; __eoi=ID=bd65f482c6a50d38:T=1767973291:RT=1774624664:S=AA-AfjY2eaWQKKvot7aNV3j89Bvq; FCNEC=%5B%5B%22AKsRol9w5XXKfa8oUHvVbFRIEwqqan_Om7Qqhs9SJz_-1UcwO2NKB_PZjaEwt_DNiAw7xbZQS8wDN7Hb-70Uo_lm5FtrdKrNOVeLceiyZa98PYpPjwDVCShyVr8fEpDi8mJZw6qtm7k5S81iA_7SSl9CYgDB6mIwOQ%3D%3D%22%5D%5D',
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
                # For fg-proj, the base is the full endpoint; tail is just the query string
                if prefix == '/fg-proj/':
                    upstream = upstream_base + ('?' + tail.split('?',1)[1] if '?' in tail else '')
                else:
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
