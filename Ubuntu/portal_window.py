#!/usr/bin/env python3
"""In-app WebKitGTK campus portal with opt-in HTTPS form auto-login.

Receives credentials only via stdin, never command-line arguments or disk.
Only uses automation on the exact configured HTTPS origin; leaves CAPTCHA,
2FA, SSO and unfamiliar forms for interactive completion.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import threading
import sys
from urllib.parse import urlsplit
from instance import SingleInstance


def login_origin_matches(configured: str, current: str, *, allow_http=False) -> bool:
    try:
        expected, actual = urlsplit(configured), urlsplit(current)
        schemes = ('https', 'http') if allow_http else ('https',)
        default_port = 443 if expected.scheme == 'https' else 80
        return (expected.scheme == actual.scheme and expected.scheme in schemes and
                expected.hostname == actual.hostname and
                (expected.port or default_port) == (actual.port or default_port) and
                bool(expected.hostname))
    except ValueError:
        return False


def prefill_script(username: str, password: str, auto_submit: bool = False, *, allow_http=False) -> str:
    """Try safe, same-origin login form filling, including delayed SPA forms.

    Returns pending when dynamic inputs have not rendered yet. Does not
    auto-submit if a CAPTCHA/MFA/SSO challenge is present. The caller retries
    a bounded number of times and then shows the page for manual login.
    """
    script = r'''(function(){
      const user = %s, password = %s, automatic = %s;
      if (!user || !password) return 'manual';
      const text = document.body ? document.body.innerText.slice(0, 8000) : '';
      const hasChallenge = !!document.querySelector(`
        [name*=captcha i], [id*=captcha i], [class*=captcha i],
        [name*=otp i], [name*=totp i], [autocomplete=one-time-code],
        iframe[src*=captcha i], iframe[src*=recaptcha i]`) ||
        /验证码|人机验证|二次验证|短信验证|两步验证|安全校验|扫码登录/.test(text);
      if (/网络安全审计系统/.test(text) && /初始用户\s*[:：]\s*admin/i.test(text)) return 'wrong-portal';
      const inputs = [...document.querySelectorAll('input')]
        .filter(el => !el.disabled && el.type !== 'hidden' && el.offsetParent !== null);
      const passField = inputs.find(el => el.type === 'password');
      const userField = inputs.find(el => /user|account|login|student|学号|账号|用户名/i.test(
        (el.name || '') + ' ' + (el.id || '') + ' ' + (el.placeholder || '')))
        || inputs.find(el => ['text', 'email', 'tel'].includes(el.type));
      if (!userField || !passField) return hasChallenge ? 'manual' : 'pending';
      function setValue(element, value) {
        const native = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
        native.call(element, value);
        element.dispatchEvent(new Event('input', {bubbles: true}));
        element.dispatchEvent(new Event('change', {bubbles: true}));
      }
      setValue(userField, user);
      setValue(passField, password);
      if (hasChallenge || !automatic) return 'manual';
      const form = passField.form || userField.form;
      if (form && !form.contains(userField)) return 'manual';
      if (form) {
        const action = new URL(form.getAttribute('action') || location.href, location.href);
        if (action.protocol !== 'https:' || action.origin !== location.origin) return 'manual';
      }
      // A one-time DOM marker prevents duplicate submissions on retries.
      const root = form || passField.closest('main') || document.body;
      if (root.dataset.wiflowSubmitted === '1') return 'waiting';
      if (form) {
        root.dataset.wiflowSubmitted = '1';
        setTimeout(() => { if (form.requestSubmit) form.requestSubmit(); else form.submit(); }, 350);
        return 'submitted';
      }
      // Some campus portals use Vue/React instead of a native form.
      // Click only a visible, enabled button with an explicit login caption;
      // never choose arbitrary submit-like elements or links.
      const buttons = [...document.querySelectorAll('button, input[type=submit]')].filter(el =>
        !el.disabled && el.offsetParent !== null);
      const submit = buttons.find(el => /^(登[录陆]|立即登录|上网认证|连接网络|log\s?in|sign\s?in)$/i.test(
        (el.innerText || el.value || '').replace(/\s+/g, '')));
      if (!submit) return 'manual';
      root.dataset.wiflowSubmitted = '1';
      setTimeout(() => submit.click(), 350);
      return 'submitted';
    })();''' % (json.dumps(username, ensure_ascii=True), json.dumps(password, ensure_ascii=True),
                'true' if auto_submit else 'false')

    return script.replace("action.protocol !== 'https:'", 'action.protocol !== location.protocol') if allow_http else script


def start_browser(config: dict) -> None:
    import gi
    gi.require_version('Gtk', '3.0')
    gi.require_version('WebKit2', '4.1')
    from gi.repository import Gtk, GLib, WebKit2, Pango

    url = config.get('url', '')
    parts = urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
        raise ValueError('学校认证网址必须是有效的 HTTP/HTTPS 地址')
    username, password = config.get('username', ''), config.get('password', '')
    allow_http = bool(config.get('allow_http', False))
    automatic = bool(config.get('automatic', False) and (parts.scheme == 'https' or allow_http) and username and password)
    settings_dir = Path.home() / '.local' / 'share' / 'wiflow' / 'webview'
    cache_dir = Path.home() / '.cache' / 'wiflow' / 'webview'
    settings_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    storage = WebKit2.WebsiteDataManager(base_data_directory=str(settings_dir),
                                         base_cache_directory=str(cache_dir))
    context = WebKit2.WebContext.new_with_website_data_manager(storage)
    windows = []

    def restore():
        if windows:
            windows[0].show_all()
            windows[0].deiconify()
            windows[0].present()

    instance = None if config.get('smoke_test') else SingleInstance('wiflow-portal', restore)
    if instance is not None and not instance.primary:
        return

    def poll_instance():
        if instance:
            instance.poll()
        return True
    GLib.timeout_add(200, poll_instance)

    def create_window(target=url, subview=None):
        win = Gtk.Window(title='WiFlow · 上网认证')
        win.set_default_size(1050, 740)
        win.set_icon_name('network-wireless')
        windows.append(win)
        def closed(_win):
            if _win in windows:
                windows.remove(_win)
            if not windows and Gtk.main_level() > 0:
                Gtk.main_quit()
        win.connect('destroy', closed)
        shell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        win.add(shell)
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        bar.set_border_width(10)
        back, forward, reload_btn = (Gtk.Button(label=x) for x in ('← 后退', '前进 →', '刷新'))
        address = Gtk.Label(label=target)
        address.set_ellipsize(Pango.EllipsizeMode.END)
        address.set_xalign(0)
        address.set_selectable(True)
        for widget in (back, forward, reload_btn):
            bar.pack_start(widget, False, False, 0)
        bar.pack_start(address, True, True, 0)
        shell.pack_start(bar, False, False, 0)
        view = subview if subview is not None else WebKit2.WebView.new_with_context(context)
        shell.pack_start(view, True, True, 0)
        back.connect('clicked', lambda *_: view.go_back())
        forward.connect('clicked', lambda *_: view.go_forward())
        reload_btn.connect('clicked', lambda *_: view.reload())

        # Dynamic university portals often create inputs well after the initial
        # HTML load event. Retry for a limited time instead of declaring failure
        # immediately and silently ignoring the login form.
        login_state = {'url': '', 'count': 0, 'busy': False, 'timer': None, 'done': False}

        def stop_retry():
            timer = login_state['timer']
            if timer is not None:
                GLib.source_remove(timer)
                login_state['timer'] = None

        def script_finished(web_view, result, _data=None):
            login_state['busy'] = False
            try:
                response = web_view.run_javascript_finish(result)
                result_value = response.get_js_value().to_string()
            except Exception:
                result_value = 'manual'
            if result_value in ('submitted', 'waiting'):
                login_state['done'] = True
                stop_retry()
            elif result_value == 'wrong-portal':
                login_state['done'] = True
                stop_retry()
                address.set_text('当前地址是设备管理后台，请修改校园网认证地址')
                restore()
            elif result_value == 'manual':
                login_state['done'] = True
                stop_retry()
                if automatic:
                    restore()
            elif login_state['count'] >= 12:
                login_state['done'] = True
                stop_retry()
                if automatic:
                    restore()

        def try_fill():
            if login_state['done'] or view.get_uri() != login_state['url']:
                login_state['timer'] = None
                return False
            if login_state['busy']:
                return True
            if login_state['count'] >= 12:
                login_state['timer'] = None
                if automatic:
                    restore()
                return False
            login_state['count'] += 1
            login_state['busy'] = True
            view.run_javascript(prefill_script(username, password, automatic, allow_http=allow_http), None, script_finished, None)
            return True

        def loaded(_view, phase):
            current = view.get_uri() or ''
            address.set_text(current)
            back.set_sensitive(view.can_go_back())
            forward.set_sensitive(view.can_go_forward())
            if phase == WebKit2.LoadEvent.STARTED:
                stop_retry()
                login_state.update(url=current, count=0, busy=False, done=False)
            if phase == WebKit2.LoadEvent.FINISHED:
                stop_retry()
                login_state.update(url=current, count=0, busy=False, done=False)
                if login_origin_matches(url, current, allow_http=allow_http):
                    try_fill()
                    if not login_state['done']:
                        login_state['timer'] = GLib.timeout_add(1000, try_fill)
                elif automatic and current:
                    # Redirect to a different authentication origin: never
                    # transmit the saved credentials to a new host.
                    restore()


        def new_tab(_view, _nav):
            # Campus SSO may request a popup. Reuse the same app window instead
            # of creating multiple top-level windows (including when minimized).
            nonlocal view
            popup = WebKit2.WebView.new_with_context(context)
            stop_retry()
            shell.remove(view)
            shell.pack_start(popup, True, True, 0)
            view = popup
            popup.connect('load-changed', loaded)
            popup.connect('create', new_tab)
            popup.show()
            login_state.update(url='', count=0, busy=False, done=False, timer=None)
            win.present()
            return popup

        view.connect('load-changed', loaded)
        view.connect('create', new_tab)
        win.show_all()
        if automatic:
            win.hide()
        if subview is None:
            view.load_uri(target)
        return win

    if config.get('smoke_test'):
        window = create_window()
        window.destroy()
        return

    try:
        create_window()
        if automatic:
            attempts = {'count': 0, 'checking': False}
            def monitor():
                if not windows:
                    return False
                if attempts['checking']:
                    return True
                attempts['checking'] = True
                attempts['count'] += 1
                def check_in_worker():
                    full = False
                    try:
                        from network_core import connectivity_state, run_nmcli, probe_online
                        full = connectivity_state(run_nmcli) == 'full' and probe_online()
                    except (OSError, subprocess.TimeoutExpired):
                        pass
                    def done():
                        attempts['checking'] = False
                        if full:
                            for window in list(windows):
                                window.destroy()
                        elif attempts['count'] >= 5:
                            restore()
                        return False
                    GLib.idle_add(done)
                threading.Thread(target=check_in_worker, daemon=True).start()
                return attempts['count'] < 5
            GLib.timeout_add_seconds(6, monitor)
        Gtk.main()
    finally:
        if instance:
            instance.close()


def main() -> None:
    if '--smoke-test' in sys.argv:
        start_browser({'url': 'https://example.edu', 'smoke_test': True})
        return
    payload = sys.stdin.buffer.readline(65536)
    if not payload:
        raise RuntimeError('缺少认证信息')
    start_browser(json.loads(payload.decode('utf-8')))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        import traceback
        log_dir = Path.home() / '.cache' / 'wiflow'
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / 'portal.log').open('a', encoding='utf-8') as log:
            traceback.print_exc(file=log)
        raise
