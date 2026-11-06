"""Exercise the shared HTML bridge and embedded GTK browser on a local fixture."""
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import sys
import threading
from urllib.parse import parse_qs
from web_main import WebApplication,default_profile,portal_secret_key


class Handler(BaseHTTPRequestHandler):
    submissions=[]
    def log_message(self,*_):pass
    def do_GET(self):
        page=b'<!doctype html><html><title>Fixture campus login</title><body><form method="post" action="/login"><input id="username" name="username"><input id="password" type="password" name="password"><button>Login</button></form></body></html>'
        self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers();self.wfile.write(page)

    def do_POST(self):
        payload=self.rfile.read(int(self.headers.get('Content-Length',0))).decode()
        self.submissions.append(parse_qs(payload))
        self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers()
        self.wfile.write(b'<html><title>Request received</title><body>Request received</body></html>')


class Probe(WebApplication):
    def __init__(self,url):
        super().__init__(smoke_test=True)
        self.exit_code=1
        self.store.mode='wifi';self.store.automatic=False
        self.store.profiles['wifi']={**default_profile('wifi'),'ssid':'FixtureCampus','portalUrl':url,'username':'fixture-user'}
        self.passwords={portal_secret_key(self.store.profiles['wifi']):'fixture-password'}
        self.connection={'mode':'wifi','ssid':'FixtureCampus','device':'fixture0','profile':'FixtureCampus'}
        self.links={'wifi':self.connection}
        self.test_url=url
        self.requested=False
        self.GLib.timeout_add(100,self.inspect_when_ready)

    def handle_command(self,data):
        if data['command']=='ready':
            self.ready=True
            self.send_state();self.send_profile();self.send_startup()
            self.view.run_javascript("document.querySelector('[data-goto=portal]').click()",None,None,None)
            self.GLib.timeout_add(100,self.click_login)
        else:super().handle_command(data)

    def click_login(self):
        self.view.run_javascript("document.querySelector('[data-action=openPortal]').click()",None,None,None)
        return False

    def inspect_when_ready(self):
        if self.requested:return False
        if not Handler.submissions:return True
        try:
            self.requested=True
            assert Handler.submissions==[{'username':['fixture-user'],'password':['fixture-password']}]
            assert self.portal_box.get_parent() is self.overlay
            assert self.portal_box.get_toplevel() is self.window
            assert self.portal_box.get_visible()
            assert self.portal_box.get_allocated_width()>100
            assert self.portal_page=='portal'
            self.view.run_javascript("document.querySelector('[data-page=dashboard]').click()",None,None,None)
            self.GLib.timeout_add(150,self.verify_hidden)
        except Exception as exc:self.fail(str(exc))
        return False

    def verify_hidden(self):
        if self.portal_box.get_visible():return self.fail('embedded browser did not hide when leaving page')
        print('PASS: one click automatically fills and submits once without a second Login click; embedded view and navigation verified',flush=True)
        self.exit_code=0;self.window.destroy();self.Gtk.main_quit()
        return False

    def fail(self,message):
        print('FAIL:',message,file=sys.stderr,flush=True)
        self.exit_code=1;self.window.destroy();self.Gtk.main_quit()
        return False

    def stop_smoke_test(self):return self.fail('embedded portal integration timed out')


def main():
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    try:
        app=Probe('http://127.0.0.1:'+str(server.server_port)+'/')
        app.Gtk.main()
        return app.exit_code
    finally:server.shutdown();server.server_close()


if __name__=='__main__':sys.exit(main())
