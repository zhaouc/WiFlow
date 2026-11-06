"""Real WebKitGTK login integration against a temporary local HTTPS portal.

Run: xvfb-run -a /usr/bin/python3 verify_terminal_login.py
Uses synthetic credentials and never contacts the configured school portal.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import ssl
import subprocess
import tempfile
import threading
from urllib.parse import parse_qs
from terminal_portal import run_login


def main():
    with tempfile.TemporaryDirectory(prefix='wiflow-test-portal-') as folder:
        cert, key = Path(folder) / 'cert.pem', Path(folder) / 'key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                        '-keyout', str(key), '-out', str(cert), '-days', '1',
                        '-subj', '/CN=127.0.0.1', '-addext', 'subjectAltName=IP:127.0.0.1,DNS:localhost'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        state = {'posts': 0, 'online': False, 'accept': True}
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                if self.path == '/redirect':
                    self.send_response(302)
                    self.send_header('Location', 'https://localhost:' + str(self.server.server_port) + '/login')
                    self.end_headers()
                    return
                challenge = '<input name="captcha">' if self.path == '/captcha' else ''
                action = 'https://elsewhere.example/session' if self.path == '/external-action' else '/session'
                if self.path == '/spaced-login':
                    page = '''<!doctype html><html><body><div id="form1"><input name="username" id="username"><input type="password" name="password" id="password"><button onclick="fetch('/session',{method:'POST',body:new URLSearchParams({username:document.getElementById('username').value,password:document.getElementById('password').value})})">登 录</button><button>注销</button></div></body></html>'''
                else:
                    page = ('<!doctype html><html><body><form method="post" action="' + action + '">' +
                        '<input name="username"><input type="password" name="password">' + challenge +
                        '<button type="submit">Login</button></form></body></html>')
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.end_headers()
                self.wfile.write(page.encode())
            def do_POST(self):
                fields = parse_qs(self.rfile.read(int(self.headers.get('Content-Length', 0))).decode())
                state['posts'] += 1
                if fields == {'username': ['fixture-student'], 'password': ['fixture-password']} and state['accept']:
                    state['online'] = True
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.end_headers()
                self.wfile.write(b'<html><body>Response received</body></html>')
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = 'https://127.0.0.1:' + str(server.server_port)
        defaults = {'url': base + '/login', 'username': 'fixture-student',
                    'password': 'fixture-password', 'timeout': 7,
                    'trusted_certificate': cert.read_text()}
        def run(path='/login', **changes):
            state.update(posts=0, online=False, accept=True)
            config = {**defaults, 'url': base + path, **changes}
            return run_login(config, online_probe=lambda: state['online'], route_check=lambda: True)
        try:
            code, result = run()
            assert code == 0 and state['posts'] == 1 and result['submitted'], (code, result, state)
            print('PASS: trusted HTTPS form, real password submission, confirmed success, one submission', flush=True)
            code, result = run('/captcha')
            assert code == 4 and state['posts'] == 0, (code, result, state)
            print('PASS: CAPTCHA requires interaction and is not submitted', flush=True)
            code, result = run('/external-action')
            assert code == 4 and state['posts'] == 0, (code, result, state)
            print('PASS: cross-origin form action is not submitted', flush=True)
            code, result = run('/redirect')
            assert code in (4, 6) and state['posts'] == 0, (code, result, state)
            print('PASS: cross-origin redirect receives no credentials', flush=True)
            code, result = run(trusted_certificate='')
            assert code == 6 and state['posts'] == 0, (code, result, state)
            print('PASS: untrusted TLS certificate receives no credentials', flush=True)
            state.update(posts=0, online=False, accept=False)
            code, result = run_login({**defaults, 'timeout': 5}, online_probe=lambda: False, route_check=lambda: True)
            assert code == 5 and state['posts'] == 1, (code, result, state)
            print('PASS: unsuccessful login times out without repeated submissions or false success', flush=True)
            state.update(posts=0, online=False)
            code, result = run_login(defaults, online_probe=lambda: False, route_check=lambda: False)
            assert code == 3 and state['posts'] == 0, (code, result, state)
            print('PASS: route mismatch stops before submitting credentials', flush=True)
            plain = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            threading.Thread(target=plain.serve_forever, daemon=True).start()
            state.update(posts=0, online=False, accept=True)
            try:
                http_config = {**defaults, 'url': 'http://127.0.0.1:' + str(plain.server_port) + '/login', 'allow_http': True}
                code, result = run_login(http_config, online_probe=lambda: state['online'], route_check=lambda: True)
                assert code == 0 and state['posts'] == 1, (code, result, state)
                print('PASS: explicitly enabled same-origin HTTP login completes', flush=True)
            finally:
                plain.shutdown()
                plain.server_close()
            spaced = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            threading.Thread(target=spaced.serve_forever, daemon=True).start()
            state.update(posts=0, online=False, accept=True)
            try:
                config = {**defaults, 'url': 'http://127.0.0.1:' + str(spaced.server_port) + '/spaced-login', 'allow_http': True}
                code, result = run_login(config, online_probe=lambda: state['online'], route_check=lambda: True)
                assert code == 0 and state['posts'] == 1, (code, result, state)
                print('PASS: real campus-style HTTP page with spaced login caption and JavaScript handler', flush=True)
            finally:
                spaced.shutdown()
                spaced.server_close()
            print('All 9 terminal portal integration checks passed.', flush=True)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == '__main__': main()
