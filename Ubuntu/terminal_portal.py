#!/usr/bin/env python3
"""Bounded portal login in a private virtual display; JSON secrets via stdin."""
from __future__ import annotations
import json
import ssl
import sys
import threading
from urllib.parse import urlsplit
from network_core import current_networks, default_route_device, matching_campus_link, probe_online, run_nmcli, valid_portal
from portal_window import login_origin_matches, prefill_script


def same_login_origin(configured, current, allow_http=False):
    if login_origin_matches(configured, current):
        return True
    if not allow_http:
        return False
    try:
        expected, actual = urlsplit(configured), urlsplit(current)
        return (expected.scheme == actual.scheme == 'http' and bool(expected.hostname)
                and expected.hostname == actual.hostname and (expected.port or 80) == (actual.port or 80))
    except ValueError:
        return False


def run_login(config, *, online_probe=probe_online, route_check=None):
    url = config.get('url', '')
    if not url or not valid_portal(url):
        return 2, {'message': '认证地址无效', 'submitted': False}
    if not config.get('username') or not config.get('password'):
        return 2, {'message': '缺少认证账号或密码', 'submitted': False}
    allow_http = bool(config.get('allow_http'))
    if urlsplit(url).scheme == 'http' and not allow_http:
        return 2, {'message': 'HTTP 认证需要明确启用 --allow-http', 'submitted': False}
    if route_check is None:
        def route_check():
            mode, profile = config['mode'], config['profile']
            link = current_networks(run_nmcli).get(mode)
            return (matching_campus_link(mode, profile, link)
                    and default_route_device() == link['device'])
    import gi
    gi.require_version('Gtk', '3.0')
    gi.require_version('WebKit2', '4.1')
    from gi.repository import Gtk, GLib, WebKit2
    if not Gtk.init_check()[0]:
        return 2, {'message': '无法初始化虚拟显示器', 'submitted': False}
    context = WebKit2.WebContext.new_ephemeral()
    view = WebKit2.WebView.new_with_context(context)
    window = Gtk.Window()
    window.add(view)
    window.set_default_size(1000, 720)
    window.show_all()
    state = {'done': False, 'submitted': False, 'attempts': 0, 'filling': False,
             'probing': False, 'tls_retried': False, 'generation': 0}
    outcome = [5, {'message': '未确认认证成功', 'submitted': False}]

    def finish(code, message):
        if state['done']:
            return False
        state['done'] = True
        outcome[:] = [code, {'message': message, 'submitted': state['submitted']}]
        if Gtk.main_level():
            Gtk.main_quit()
        return False

    def route_is_valid():
        try:
            return bool(route_check())
        except (RuntimeError, OSError):
            return False

    def filled(web_view, result, generation):
        state['filling'] = False
        if state['done'] or generation != state['generation']:
            return
        try:
            value = web_view.run_javascript_finish(result).get_js_value().to_string()
        except Exception:
            finish(4, '无法处理认证表单，请使用桌面版完成认证')
            return
        if value in ('submitted', 'waiting'):
            state['submitted'] = True
        elif value == 'wrong-portal':
            finish(2, '当前地址是设备管理后台，请修改校园网认证地址')
        elif value == 'manual':
            finish(4, '门户需要验证码、交互或不支持的表单，请使用桌面版完成认证')

    def fill():
        if state['done'] or state['submitted']:
            return False
        if state['filling']:
            return True
        current = view.get_uri() or ''
        if not same_login_origin(url, current, allow_http):
            return True
        if not route_is_valid():
            return finish(3, '网络或默认路由已变化，已停止认证')
        state['attempts'] += 1
        if state['attempts'] > 12:
            return finish(4, '未找到可自动填写的登录表单，请使用桌面版完成认证')
        script = prefill_script(config['username'], config['password'], True, allow_http=allow_http)
        if allow_http and urlsplit(url).scheme == 'http':
            script = script.replace("action.protocol !== 'https:'", 'action.protocol !== location.protocol')
        state['filling'] = True
        view.run_javascript(script, None, filled, state['generation'])
        return True

    def loaded(_view, phase):
        if state['done']:
            return
        if phase == WebKit2.LoadEvent.STARTED:
            state['generation'] += 1
        if phase == WebKit2.LoadEvent.FINISHED and not state['submitted']:
            current = view.get_uri() or ''
            if current in ('', 'about:blank'):
                return
            if not same_login_origin(url, current, allow_http):
                finish(4, '门户跳转到其他认证地址，请在桌面版确认登录流程')
            else:
                fill()

    def tls_failed(_view, failing_uri, certificate, _errors):
        trusted = config.get('trusted_certificate', '')
        try:
            matches = bool(trusted and ssl.PEM_cert_to_DER_cert(trusted) ==
                           ssl.PEM_cert_to_DER_cert(certificate.get_property('certificate-pem')))
        except (ValueError, TypeError):
            matches = False
        if (matches and not state['tls_retried'] and
                same_login_origin(url, failing_uri, allow_http)):
            state['tls_retried'] = True
            context.allow_tls_certificate_for_host(certificate, urlsplit(url).hostname)
            GLib.idle_add(lambda: (view.load_uri(url), False)[1])
        else:
            finish(6, '学校 HTTPS 证书不受信任；请安装学校证书，或通过 --trust-cert 指定已核实的服务器 PEM 证书')
        return True

    def load_failed(_view, _phase, _uri, _error):
        if not state['done'] and not state['submitted']:
            finish(5, '无法加载学校认证页面，请检查地址、网络和证书')
        return True

    def monitor():
        if state['done']:
            return False
        if state['probing']:
            return True
        state['probing'] = True
        def worker():
            valid, online = False, False
            try:
                valid = bool(route_check())
                online = valid and bool(online_probe())
            except Exception:
                pass
            def deliver():
                state['probing'] = False
                if not valid:
                    finish(3, '网络或默认路由已变化，已停止认证')
                elif online:
                    finish(0, '认证后检测到互联网已连通。' if state['submitted'] else '当前网络已可访问互联网。')
                return False
            GLib.idle_add(deliver)
        threading.Thread(target=worker, daemon=True).start()
        return True

    def popup(*_):
        finish(4, '门户需要弹出窗口或 SSO 交互，请使用桌面版完成认证')
        return None

    view.connect('load-changed', loaded)
    view.connect('load-failed-with-tls-errors', tls_failed)
    view.connect('load-failed', load_failed)
    view.connect('create', popup)
    if not route_is_valid():
        state['done'] = True
        window.destroy()
        return 3, {'message': '所选网络或默认路由不匹配，已停止认证', 'submitted': False}
    timers = [GLib.timeout_add_seconds(max(5, min(300, int(config.get('timeout', 45)))),
                                      lambda: finish(5, '认证超时，尚未确认互联网连通；请检查账号或使用桌面版')),
              GLib.timeout_add(1000, fill), GLib.timeout_add(2000, monitor)]
    try:
        view.load_uri(url)
        Gtk.main()
    finally:
        state['done'] = True
        for timer in timers:
            source = GLib.MainContext.default().find_source_by_id(timer)
            if source is not None:
                source.destroy()
        window.destroy()
    return tuple(outcome)


def main():
    try:
        raw = sys.stdin.read(262145)
        if len(raw) > 262144:
            raise ValueError('payload too large')
        config = json.loads(raw)
        code, result = run_login(config)
    except Exception:
        code, result = 2, {'message': '终端认证初始化失败，请检查 WebKitGTK、Xvfb 和配置', 'submitted': False}
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return code


if __name__ == '__main__':
    sys.exit(main())
