#!/usr/bin/env python3
"""WiFlow: shared Windows HTML/CSS UI on Ubuntu AMD64 and ARM64.

The interface is rendered locally by WebKitGTK; network changes go through
NetworkManager and credentials through the user's Secret Service keyring.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import threading
import traceback

from credentials import connect_wifi, get_secret, put_secret, cleanup_legacy_wifi_profiles
from instance import SingleInstance, signal_existing
from system_network import connect_wired, network_choices, open_network_settings
from portal_client import authenticate_portal, supports_direct_portal, connection_key, probe_interface_access
from network_core import (CONFIG_PATH, LEGACY_CONFIG_PATH, AUTOSTART, current_network, current_networks,
                  active_login_mode, matching_campus_link, default_route_device, load_settings,
                  needs_reconnect, portal_check_needed, probe_online, connectivity_state,
                  run_nmcli, split_nmcli, valid_portal, print_diagnostics)

APP_VERSION = '1.0.0'
# Keep the WebKitGTK message channel identical to Desktop.html.
BRIDGE_NAME = 'wiflowBridge'
HTML = Path(__file__).resolve().parent / 'Desktop.html'
PROFILE_FIELDS = ('connectionMode', 'ssid', 'portalUrl', 'username',
                  'portalEnabled', 'autoSubmit', 'ethernetName')


def default_profile(mode: str) -> dict:
    return {'connectionMode': mode, 'ssid': '', 'portalUrl': '', 'username': '',
            'ethernetName': '', 'wiredAuth': 'web', 'broadbandName': '', 'authenticationClient': '', 'portalEnabled': True, 'autoSubmit': False}


def _as_bool(value, default=False) -> bool:
    return value if isinstance(value, bool) else default


def normalize_profile(value: dict, mode: str) -> dict:
    """Allow-list data sent by the local page; ignore Windows-only modes."""
    if not isinstance(value, dict):
        raise ValueError('网络配置格式错误')
    base = default_profile(mode)
    for key in ('ssid', 'portalUrl', 'username', 'ethernetName', 'wiredAuth', 'broadbandName', 'authenticationClient'):
        if key in value:
            if not isinstance(value[key], str):
                raise ValueError('网络配置字段必须为字符串')
            base[key] = value[key].strip()[:2048]
    if len(base['ssid'].encode('utf-8')) > 32:
        raise ValueError('Wi-Fi SSID 不能超过 32 字节')
    if not valid_portal(base['portalUrl']):
        raise ValueError('学校认证地址必须是有效的 HTTP/HTTPS URL')
    for key in ('portalEnabled', 'autoSubmit'):
        base[key] = _as_bool(value.get(key), base[key])
    if isinstance(value.get('automatic'), bool):
        base['autoSubmit'] = value['automatic']
    if base['wiredAuth'] not in ('web', 'pppoe', 'dot1x', 'client'):
        raise ValueError('请选择有效的有线认证方式')
    if mode == 'wired':
        base['ssid'] = ''
        base['ethernetName'] = base['ethernetName'][:255]
    else:
        base['ethernetName'] = ''
    return base


class Store:
    """Versioned settings, with migration from the original Ubuntu settings."""
    def __init__(self, path: Path = CONFIG_PATH):
        self.path = path
        # Import settings created by the pre-rename build only when the new
        # WiFlow settings file does not yet exist. Do not overwrite new data.
        migration_source = (LEGACY_CONFIG_PATH if path == CONFIG_PATH and
                            not path.is_file() and LEGACY_CONFIG_PATH.is_file() else path)
        old = load_settings(migration_source)
        self.automatic = bool(old['auto_connect'])
        self.startup = bool(old['startup'])
        self.mode = old['connection'] if old['connection'] in ('wifi', 'wired') else 'wifi'
        self.profiles = {'wifi': default_profile('wifi'), 'wired': default_profile('wired')}
        self.profiles[self.mode].update({
            'ssid': old['ssid'] if self.mode == 'wifi' else '',
            'portalUrl': old['portal_url'], 'username': old['username'],
            'portalEnabled': bool(old['portal_url']), 'autoSubmit': bool(old['auto_auth'])})
        try:
            raw = json.loads(migration_source.read_text(encoding='utf-8'))
            if isinstance(raw, dict) and isinstance(raw.get('profiles'), dict):
                for mode in ('wifi', 'wired'):
                    entry = raw['profiles'].get(mode)
                    if isinstance(entry, dict):
                        self.profiles[mode] = normalize_profile(entry, mode)
        except (OSError, ValueError, TypeError):
            pass
        self.automatic = any(p['autoSubmit'] for p in self.profiles.values())
        if migration_source != path:
            self.save()

    def set_profile(self, value: dict) -> str:
        mode = value.get('connectionMode')
        if mode not in ('wifi', 'wired'):
            raise ValueError('只支持 Wi-Fi 或有线网络模式')
        self.profiles[mode] = normalize_profile(value, mode)
        self.mode = mode
        self.automatic = any(p['autoSubmit'] for p in self.profiles.values())
        return mode

    def save(self) -> None:
        active = self.profiles[self.mode]
        wifi = self.profiles['wifi']
        payload = {
            'schema': 2, 'profiles': self.profiles, 'connection': self.mode,
            'auto_connect': self.automatic, 'auto_auth': active['autoSubmit'] and active['portalEnabled'],
            'startup': self.startup, 'ssid': wifi['ssid'],
            'portal_url': active['portalUrl'], 'username': active['username']}
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self.path.with_name(self.path.name + '.tmp')
        try:
            with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w', encoding='utf-8') as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
            os.chmod(self.path, 0o600)
        finally:
            tmp.unlink(missing_ok=True)


def wifi_secret_key(profile: dict) -> str:
    return 'wifi:' + profile['ssid']


def portal_secret_key(profile: dict) -> str:
    return 'portal:' + profile['portalUrl'] + '|' + profile['username']


def write_linux_autostart(enabled: bool) -> None:
    legacy_autostart = AUTOSTART.with_name('wifi-assistant.desktop')
    if not enabled:
        legacy_autostart.unlink(missing_ok=True)
        AUTOSTART.unlink(missing_ok=True)
        return
    command = '/usr/bin/wiflow' if Path('/usr/bin/wiflow').exists() else (
        shlex.quote(sys.executable) + ' ' + shlex.quote(str(Path(__file__).resolve())))
    AUTOSTART.parent.mkdir(parents=True, exist_ok=True)
    AUTOSTART.write_text('[Desktop Entry]\nType=Application\nName=WiFlow\n'
                         'Exec=' + command + ' --background\nTerminal=false\nX-GNOME-Autostart-enabled=true\n',
                         encoding='utf-8')
    os.chmod(AUTOSTART, 0o600)
    legacy_autostart.unlink(missing_ok=True)


class WebApplication:
    def __init__(self, *, smoke_test=False):
        import gi
        gi.require_version('Gtk', '3.0')
        gi.require_version('WebKit2', '4.1')
        from gi.repository import Gtk, GLib, WebKit2
        self.Gtk, self.GLib, self.WebKit2 = Gtk, GLib, WebKit2
        self.store = Store()
        if self.store.startup and not smoke_test:
            try:
                write_linux_autostart(True)  # migrate old visible-login autostart
            except OSError:
                pass
        self.busy = False
        self.ready = False
        self.ethernet_adapters = []
        self.broadband_entries = []
        self.tray = None
        self.portal_process = None
        self.embedded_portal = None
        self.portal_box = None
        self.portal_page = 'dashboard'
        self.portal_address = ''
        self.portal_status = '连接校园网后，可在此完成学校网页认证。'
        self.status = '准备就绪'
        self.connection = None
        self.links = {}
        self.internet_online = None
        self.passwords = {}  # current, per-key in-memory cache; never serialized
        self.auto_events = {'wifi': '等待网络检测', 'wired': '等待网络检测'}
        self.auto_checked = {'wifi': '', 'wired': ''}
        self.authenticated_links = {}
        self.auth_generation = 0
        self.pending_auto_mode = None
        self.auto_retry_source = None
        self.auto_log = Path.home() / '.cache/wiflow/autoauth.log'
        self.last_auto_scan = 0.0
        self.background_mode = '--background' in sys.argv[1:]
        self.monitor_running = True
        self.nm_monitor = None
        self.seen_passwords = {}  # last shown to local page; for intentional clearing
        self.smoke_test = smoke_test
        self.instance = None
        self.window = Gtk.Window(title='WiFlow')
        self.window.set_default_size(1080, 770)
        self.window.set_size_request(750, 560)
        self.window.set_icon_name('network-wireless')
        self.window.connect('destroy', self.on_destroy)
        self.window.connect('delete-event', self.on_close)
        manager = WebKit2.UserContentManager()
        # Connect before registering, as recommended by WebKitGTK.
        manager.connect('script-message-received::' + BRIDGE_NAME, self.on_message)
        if not manager.register_script_message_handler(BRIDGE_NAME):
            raise RuntimeError('无法注册 WebKitGTK 页面消息通道：' + BRIDGE_NAME)
        self.view = WebKit2.WebView.new_with_user_content_manager(manager)
        self.view.connect('decide-policy', self.restrict_navigation)
        self.overlay = Gtk.Overlay()
        self.overlay.add(self.view)
        self.window.add(self.overlay)
        self.window.show_all()
        if self.background_mode:
            self.window.hide()
        # This file contains the actual Windows UI and stays entirely local.
        if not HTML.is_file():
            raise FileNotFoundError('缺少 Ubuntu/Desktop.html；请重新安装 DEB')
        self.allowed_uri = HTML.as_uri()
        self.view.load_uri(self.allowed_uri)
        if smoke_test:
            GLib.timeout_add_seconds(15, self.stop_smoke_test)
        else:
            GLib.timeout_add(250, self.poll_instance)
            GLib.timeout_add_seconds(60, self.periodic_refresh)
            GLib.timeout_add_seconds(60, self.auto_timer)
            GLib.timeout_add_seconds(5, self.first_auto_check)
            # NetworkManager event notifications, not just a 5-minute poll.
            self.start_load_secrets()  # works even when the WebKit window is hidden
            self.start_system_choices()
            self.create_tray()
            threading.Thread(target=self.monitor_network_changes, daemon=True).start()

    def restrict_navigation(self, _view, decision, decision_type):
        if decision_type == self.WebKit2.PolicyDecisionType.NAVIGATION_ACTION:
            uri = decision.get_navigation_action().get_request().get_uri()
            if uri != self.allowed_uri and uri != 'about:blank':
                decision.ignore()
                self.set_status('已阻止主界面跳转到外部网站')
                return True
        return False

    def stop_smoke_test(self):
        if not self.ready:
            print('WebKitGTK HTML bridge did not send ready', file=sys.stderr)
            self.exit_code = 1
        else:
            print('WiFlow HTML/CSS 界面加载成功')
            self.exit_code = 0
        self.Gtk.main_quit()
        return False

    def poll_instance(self):
        if self.instance:
            self.instance.poll()
        return True

    def restore_window(self):
        self.window.show_all()
        self.window.present()

    def on_close(self, *_):
        self.window.hide()
        return True

    def create_tray(self):
        try:
            import gi
            gi.require_version('AyatanaAppIndicator3','0.1')
            from gi.repository import AyatanaAppIndicator3
            self.tray=AyatanaAppIndicator3.Indicator.new('wiflow','network-wireless',AyatanaAppIndicator3.IndicatorCategory.APPLICATION_STATUS)
            menu=self.Gtk.Menu()
            for label,callback in (('显示 WiFlow',lambda *_:self.restore_window()),
                                   ('立即检测认证',lambda *_:self.auto_one_shot(mode_filter='all')),
                                   ('退出',lambda *_:self.window.destroy())):
                item=self.Gtk.MenuItem(label=label)
                item.connect('activate',callback)
                menu.append(item)
            menu.show_all()
            self.tray.set_menu(menu)
            self.tray.set_status(AyatanaAppIndicator3.IndicatorStatus.ACTIVE)
        except (ImportError,ValueError):
            self.tray=None

    def start_system_choices(self):
        def worker():
            try:adapters,entries=network_choices(run_nmcli)
            except (OSError,RuntimeError,subprocess.TimeoutExpired):return
            def done():
                self.ethernet_adapters,self.broadband_entries=adapters,entries
                self.send_state()
            self.dispatch(done)
        threading.Thread(target=worker,daemon=True).start()

    def monitor_network_changes(self):
        """Request a fresh auto-check on changes to NetworkManager state."""
        while self.monitor_running:
            try:
                proc = subprocess.Popen(['nmcli', 'monitor'], stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True, bufsize=1)
                self.nm_monitor = proc
                assert proc.stdout is not None
                for line in proc.stdout:
                    if line.strip() and self.monitor_running:
                        self.dispatch(self.request_auto_scan)
                if proc.poll() is None:
                    proc.terminate()
                self.nm_monitor = None
            except (OSError, ValueError):
                pass
            # Fallback timer remains active even if nmcli monitor is absent.
            if self.monitor_running:
                time.sleep(12)

    def request_auto_scan(self):
        now = time.monotonic()
        if now - self.last_auto_scan < 6:
            return
        self.last_auto_scan = now
        if self.store.automatic and not self.busy:
            self.GLib.timeout_add_seconds(2, self.auto_one_shot)

    def record_auto(self, mode: str, message: str):
        previous = self.auto_events.get(mode)
        if previous != message and hasattr(self, 'auto_log'):
            try:
                self.auto_log.parent.mkdir(parents=True, exist_ok=True)
                with self.auto_log.open('a') as log:
                    log.write(json.dumps({'time': time.strftime('%Y-%m-%d %H:%M:%S'), 'mode': mode, 'message': message}, ensure_ascii=False) + '\n')
            except OSError:
                pass
        self.auto_events[mode] = message
        self.auto_checked[mode] = time.strftime('%H:%M:%S')
        self.send_auth_state()
        if hasattr(self, 'view'):self.send_state()

    def send_auth_state(self):
        profiles = self.store.profiles
        self.send({'kind':'autoSettings','wifi':profiles['wifi']['autoSubmit'],'wired':profiles['wired']['autoSubmit']})
        self.send({'kind': 'authState', 'automatic': self.store.automatic,
                   'enabled': {m: bool(profiles[m]['autoSubmit']) for m in ('wifi', 'wired')},
                   'configured': {m: bool(profiles[m]['portalUrl'] and valid_portal(profiles[m]['portalUrl']) and
                                            profiles[m]['username']) for m in ('wifi', 'wired')},
                   'message': self.auto_events, 'checked': self.auto_checked})

    def on_destroy(self, *_):
        self.monitor_running = False
        if self.nm_monitor is not None and self.nm_monitor.poll() is None:
            self.nm_monitor.terminate()
        if self.instance:
            self.instance.close()
            self.instance = None
        if self.Gtk.main_level():
            self.Gtk.main_quit()

    def dispatch(self, callback, *args):
        self.GLib.idle_add(lambda: (callback(*args), False)[1])

    def send(self, payload: dict):
        if not self.ready:
            return
        # json.dumps escapes quotes, control characters, and JS line separators.
        encoded = json.dumps(payload, ensure_ascii=True)
        self.view.run_javascript('window.campus && window.campus.receive(' + encoded + ');',
                                 None, None, None)

    def set_status(self, status: str):
        self.status = status
        self.send_state()

    def send_state(self):
        mode = self.store.mode
        p = self.store.profiles[mode]
        links = getattr(self, 'links', None)
        link = links.get(mode) if links is not None else self.connection
        if link:
            if link['mode'] == 'wifi':
                title = ('已连接 Wi-Fi：' + link['ssid']) if link['ssid'] else 'Wi-Fi 已连接'
            else:
                title = '已连接有线网络'
        else:
            title = '当前未连接网络'
        campus = bool(link and ((mode == 'wifi' and link['mode'] == 'wifi' and
                                 link['ssid'] == p['ssid'] and bool(p['ssid'])) or
                                (mode == 'wired' and link['mode'] == 'wired')))
        self.send_auth_state()
        self.send({'kind': 'state', 'state': {
            'automatic': self.store.automatic, 'busy': self.busy,
            'status': self.status, 'connectionTitle': title,
            'target': p['ssid'] if mode == 'wifi' else '有线网络',
            'wiredMode': mode == 'wired', 'wiredAuth': p.get('wiredAuth','web'),
            'wifiAuthStatus': self.authentication_status('wifi'), 'wiredAuthStatus': self.authentication_status('wired'),
            'ethernetAdapters': self.ethernet_adapters, 'broadbandEntries': self.broadband_entries,
            'webPortalEnabled': p['portalEnabled'] and (mode!='wired' or p.get('wiredAuth','web')=='web'),
            'campusConnected': campus, 'internetOnline': self.internet_online,
            'portalStatus': self.portal_status, 'portalAddress': self.portal_address,
            'permissionNeeded': False}})

    def authentication_status(self, mode):
        p=self.store.profiles[mode]
        system=mode=='wired' and p.get('wiredAuth','web')!='web'
        if not system and (not p['portalUrl'] or not p['username']):return '未配置'
        if not p['autoSubmit']:return '未启用'
        message=self.auto_events.get(mode,'')
        if '认证成功' in message or '已认证' in message:return '已认证'
        if '失败' in message or '未通过' in message:return '未认证'
        if '未接入' in message or '等待有线' in message or '等待校园' in message or '未连接' in message:return '未连接'
        if '密码' in message or '密钥环' in message:return '需要手动认证'
        if '正在' in message or '发起' in message:return '认证中'
        return '待检测'

    def check_interface_access(self):
        mode=self.store.mode
        def worker():
            try:
                link=current_networks(run_nmcli).get(mode)
                online=bool(link and probe_interface_access(link['device']))
                message='所选网络网页访问正常' if online else '所选网络未放行，可能仍需校园网认证'
            except Exception:message='上网状态检测失败'
            self.dispatch(self.portal_message,message)
        threading.Thread(target=worker,daemon=True).start()

    def send_startup(self):
        self.send({'kind': 'startup', 'enabled': self.store.startup,
                   'available': True, 'error': ''})

    def send_profile(self):
        mode = self.store.mode
        p = self.store.profiles[mode]
        # Windows UI keeps per-mode draft objects. Populate both modes so
        # changing Wi-Fi / wired mode never silently discards an old profile.
        profiles = {}
        for item_mode, item_profile in self.store.profiles.items():
            profiles[item_mode] = {
                'profile': {**item_profile, 'automatic': item_profile['autoSubmit']},
                'password': self.passwords.get(portal_secret_key(item_profile), ''),
                'wifiPassword': self.passwords.get(wifi_secret_key(item_profile), '') if item_mode == 'wifi' else '',
                'dirty': False,
            }
        data = {'kind': 'profile', **profiles[mode], 'profiles': profiles}
        self.send(data)

    def start_load_secrets(self):
        mode = self.store.mode
        p = self.store.profiles[mode].copy()
        targets = set()
        for candidate_mode, candidate in self.store.profiles.items():
            if candidate['portalUrl'] and candidate['username']:
                targets.add(portal_secret_key(candidate))
            if candidate_mode == 'wifi' and candidate['ssid']:
                targets.add(wifi_secret_key(candidate))
        def worker():
            values = {key: get_secret(key) for key in targets}
            self.dispatch(self.secret_loaded, mode, p, values)
        threading.Thread(target=worker, daemon=True).start()

    def secret_loaded(self, mode: str, profile: dict, values: dict):
        self.passwords.update(values)
        if self.store.automatic:
            self.GLib.timeout_add_seconds(1, self.auto_one_shot)
        # Do not rehydrate secrets into a different network/account draft.
        if self.store.mode == mode and self.store.profiles[mode] == profile:
            self.seen_passwords.update(values)
            self.send_profile()

    def on_message(self, _manager, message):
        try:
            raw = message.get_js_value().to_string()
            if len(raw) > 262144:
                raise ValueError('消息过大')
            data = json.loads(raw)
            if not isinstance(data, dict) or not isinstance(data.get('command'), str):
                return
            self.handle_command(data)
        except Exception as exc:
            self.set_status('界面消息处理失败：' + str(exc)[:160])

    def handle_command(self, data: dict):
        cmd = data['command']
        if cmd == 'ready':
            self.ready = True
            if self.smoke_test:
                # A successful smoke test should not need to wait for its
                # timeout or query the host's Wi-Fi/keyring in CI.
                self.GLib.idle_add(self.stop_smoke_test)
                return
            self.send_state()
            self.send_auth_state()
            self.send_startup()
            self.send_profile()
            if not self.smoke_test:
                self.refresh()
        elif cmd == 'refresh':
            self.refresh()
        elif cmd == 'connect':
            self.connect_network()
        elif cmd == 'openPortal':
            self.open_portal()
        elif cmd == 'checkInternet':
            self.check_interface_access()
        elif cmd == 'setStartup':
            previous = self.store.startup
            try:
                self.store.startup = bool(data.get('enabled'))
                write_linux_autostart(self.store.startup)
                self.store.save()
            except OSError as exc:
                self.store.startup = previous
                self.set_status('设置开机启动失败：' + str(exc))
            self.send_startup()
        elif cmd == 'setAutoMode':
            mode = data.get('mode')
            if mode not in ('wifi', 'wired'):
                return
            profile = self.store.profiles[mode]
            system = mode == 'wired' and profile.get('wiredAuth','web') != 'web'
            if data.get('enabled') and (not system and (not profile['portalUrl'] or not profile['username'] or (mode=='wifi' and not profile['ssid']))):
                self.send({'kind':'autoModeError','mode':mode,'enabled':True,'error':'请先保存该网络的认证地址、账号和 Wi-Fi 名称'})
                self.send_auth_state()
                return
            before = self.store.profiles[mode]['autoSubmit']
            self.store.profiles[mode]['autoSubmit'] = bool(data.get('enabled'))
            self.store.automatic = any(p['autoSubmit'] for p in self.store.profiles.values())
            try:
                self.store.save()
                self.record_auto(mode, '已开启，等待网络接入' if data.get('enabled') else '已关闭自动认证')
                if self.store.automatic and data.get('enabled'):
                    self.GLib.timeout_add_seconds(2, self.auto_one_shot)
            except OSError as exc:
                self.store.profiles[mode]['autoSubmit'] = before
                self.record_auto(mode, '保存失败：' + str(exc)[:90])
            self.send_auth_state()
        elif cmd == 'autoCheck':
            self.auto_one_shot(mode_filter='all')
        elif cmd == 'testAutoMode':
            mode = data.get('mode')
            if mode in ('wifi', 'wired'):
                self.auto_one_shot(mode_filter=mode)
        elif cmd in ('save', 'setAutomatic'):
            revision = data.get('revision', -1)
            try:
                previous_mode = self.store.mode
                if cmd == 'setAutomatic':
                    mode = self.store.mode
                    self.store.automatic = bool(data.get('enabled'))
                else:
                    mode = self.store.set_profile(data.get('profile', {}))
                self.store.save()
                if cmd == 'save':
                    self.update_secret(mode, data)
                if previous_mode != mode:
                    self.start_load_secrets()
                self.send_state()
                self.send_auth_state()
                if cmd == 'save':
                    if self.store.automatic:
                        self.GLib.timeout_add_seconds(2, self.auto_one_shot)
                    self.send({'kind': 'saved', 'revision': revision})
                else:
                    self.set_status('网络自动监控已开启' if self.store.automatic else '网络自动监控已关闭')
                    if self.store.automatic:
                        self.GLib.timeout_add_seconds(2, self.auto_one_shot)
            except (OSError, ValueError) as exc:
                self.send({'kind': 'saveError', 'revision': revision, 'error': str(exc)[:180]})
        elif cmd in ('wifiSettings','ethernetSettings','broadbandSettings','networkConnections','location'):
            try:open_network_settings(cmd)
            except RuntimeError as exc:self.set_status(str(exc))
        elif cmd == 'chooseAuthClient':
            dialog=self.Gtk.FileChooserDialog(title='选择学校 Ubuntu 认证客户端',transient_for=self.window,action=self.Gtk.FileChooserAction.OPEN)
            dialog.add_buttons('取消',self.Gtk.ResponseType.CANCEL,'选择',self.Gtk.ResponseType.OK)
            if dialog.run()==self.Gtk.ResponseType.OK:
                filename=dialog.get_filename()
                if filename and os.access(filename,os.X_OK):self.send({'kind':'authClient','path':filename})
                else:self.set_status('请选择可执行的 Ubuntu 客户端')
            dialog.destroy()
        elif cmd == 'showCaptcha':
            if self.embedded_portal is not None:
                self.portal_page = 'portal'
                self.send({'kind':'page','page':'portal'})
            else:self.open_portal()
        elif cmd == 'discoverPortal':
            self.open_portal()
        elif cmd == 'page':
            self.portal_page = data.get('page', 'dashboard')
            if self.portal_box is not None and self.portal_page != 'portal':
                self.portal_box.hide()
        elif cmd == 'portalBounds':
            self.update_portal_bounds(data)
        elif cmd == 'stopPortal':
            self.auth_generation += 1
            self.pending_auto_mode = None
            self.busy = False
            if self.embedded_portal is not None:self.embedded_portal.stop()
            if self.portal_box is not None:self.portal_box.hide()
            self.portal_status = '认证页面已停止'
            self.send_state()
        elif cmd == 'adoptAddress':
            if self.portal_address and valid_portal(self.portal_address):
                self.send({'kind': 'address', 'address': self.portal_address})
        elif cmd in ('draft', 'hydrated'):
            return

    def update_secret(self, mode: str, data: dict):
        p = self.store.profiles[mode]
        changes = []
        for key, value in ((wifi_secret_key(p), data.get('wifiPassword')),
                           (portal_secret_key(p), data.get('password'))):
            if not isinstance(value, str):
                continue
            # Empty values on an unseen account must never accidentally
            # erase credentials stored for another target.
            if key in self.seen_passwords and self.seen_passwords[key] == value:
                continue
            if value or (key in self.seen_passwords and self.seen_passwords[key]):
                self.seen_passwords[key] = value
                self.passwords[key] = value
                changes.append((key, value))
        if changes:
            def worker():
                failed = [key for key, value in changes if not put_secret(key, value)]
                if failed:
                    self.dispatch(self.set_status, '无法保存密码：检查系统密钥环与 libsecret-tools')
            threading.Thread(target=worker, daemon=True).start()

    def periodic_refresh(self):
        if not self.busy:
            self.refresh()
        return True

    def refresh(self):
        if self.busy:
            return
        self.busy = True
        self.set_status('正在读取当前网络状态…')
        def worker():
            try:
                links = current_networks(run_nmcli)
                link = links.get(self.store.mode) or links.get('wifi') or links.get('wired')
                state = connectivity_state(run_nmcli)
                self.dispatch(self.refresh_done, link, state, '', links)
            except Exception as exc:
                self.dispatch(self.refresh_done, None, '', str(exc))
        threading.Thread(target=worker, daemon=True).start()

    def refresh_done(self, link, state, error, links=None):
        self.busy = False
        if not error:
            self.links = links if links is not None else ({link['mode']:link} if link else {})
            self.connection = link
            self.internet_online = state == 'full'
        self.set_status('网络状态查询失败：' + error if error else '网络状态已更新')

    def connect_network(self, *, automatic=False, mode_override=None):
        if self.busy:
            return
        mode = mode_override or self.store.mode
        p = self.store.profiles[mode].copy()
        if mode == 'wifi' and not p['ssid']:
            self.send({'kind': 'page', 'page': 'settings'})
            self.set_status('请先输入校园 Wi-Fi 名称')
            return
        self.busy = True
        self.set_status('正在连接校园网络…')
        password = self.passwords.get(wifi_secret_key(p), '')
        def worker():
            try:
                links = current_networks(run_nmcli)
                if matching_campus_link(mode, p, links.get(mode)) and (mode != 'wired' or p.get('wiredAuth','web') == 'web'):
                    self.dispatch(self.connect_done, automatic, mode, '')
                    return
                if automatic:
                    active = run_nmcli('-t', '-f', 'TYPE,NAME', 'connection', 'show', '--active')
                    if not needs_reconnect(mode, p['ssid'], active):
                        self.dispatch(self.connect_done, automatic, mode, '')
                        return
                if mode == 'wifi':
                    connect_wifi(p['ssid'], password, run_nmcli)
                else:
                    connect_wired(p, run_nmcli)
                self.dispatch(self.connect_done, automatic, mode, '')
            except Exception as exc:
                self.dispatch(self.connect_done, automatic, mode, str(exc))
        threading.Thread(target=worker, daemon=True).start()

    def connect_done(self, automatic: bool, mode: str, error: str):
        self.busy = False
        if error:
            self.set_status('连接失败：' + error)
            return
        else:
            self.set_status('网络连接成功 · 可以进行上网认证')
            if self.store.automatic:
                self.GLib.timeout_add_seconds(2, self.auto_one_shot)
            elif not automatic and self.store.profiles[mode]['portalUrl']:
                self.open_portal()
        self.refresh()

    def first_auto_check(self):
        self.auto_one_shot()
        return False

    def auto_timer(self):
        self.auto_one_shot()
        return True

    def queue_auto_check(self, mode_filter=None):
        current=getattr(self,'pending_auto_mode',None)
        if mode_filter or current is None:self.pending_auto_mode=mode_filter or 'background'
        if getattr(self,'auto_retry_source',None) is None:
            self.auto_retry_source=self.GLib.timeout_add(300,self.retry_auto_check)

    def retry_auto_check(self):
        if self.busy:return True
        mode=self.pending_auto_mode
        self.pending_auto_mode=None
        self.auto_retry_source=None
        if mode is not None:self.auto_one_shot(None if mode=='background' else mode)
        return False

    def auto_one_shot(self, mode_filter=None):
        """Authenticate new links and explicit tests even if a probe is whitelisted."""
        if not self.store.automatic:
            if mode_filter in ('wifi','wired'):
                self.record_auto(mode_filter, '自动认证未开启')
            return False
        if self.busy:
            self.queue_auto_check(mode_filter)
            return False
        self.busy = True
        self.set_status('正在检查校园网并执行认证…')
        profiles = {mode: p.copy() for mode, p in self.store.profiles.items()}
        passwords = getattr(self, 'passwords', {}).copy()
        completed = getattr(self, 'authenticated_links', {}).copy()
        generation = getattr(self, 'auth_generation', 0)
        def worker():
            try:
                links = current_networks(run_nmcli)
                chosen = active_login_mode(links, default_route_device())
                tasks = (mode_filter,) if mode_filter in ('wifi','wired') else ('wired', 'wifi')
                statuses, confirmed = {}, {}
                login_mode, reconnect_wifi = None, False
                for mode in tasks:
                    p = profiles[mode]
                    link = links.get(mode)
                    if not p['autoSubmit']:
                        statuses[mode] = '自动认证未开启'
                    elif mode=='wired' and p.get('wiredAuth','web')!='web':
                        if not link or mode_filter in (mode,'all'):
                            statuses[mode]=connect_wired(p,run_nmcli)
                        else:
                            statuses[mode]='已认证' if probe_interface_access(link['device']) else '系统网络已连接，等待认证'
                    elif not p['portalEnabled']:
                        statuses[mode] = '请在校园网设置中开启网页认证'
                    elif not p['portalUrl'] or not valid_portal(p['portalUrl']) or not p['username']:
                        statuses[mode] = '请先设置学校认证网址和账号'
                    elif mode == 'wifi' and not p['ssid']:
                        statuses[mode] = '请先设置校园 Wi-Fi 名称'
                    elif not matching_campus_link(mode, p, link):
                        statuses[mode] = ('当前 Wi-Fi 不是已配置校园网' if mode == 'wifi' and link
                                          else '等待有线网接入' if mode == 'wired' else '等待校园 Wi-Fi 接入')
                        if mode == 'wifi' and not link:
                            try:
                                visible = run_nmcli('-t', '-f', 'SSID', 'device', 'wifi', 'list', '--rescan', 'no')
                                reconnect_wifi = any(split_nmcli(row)[0] == p['ssid'] for row in visible.splitlines())
                            except (RuntimeError, OSError):
                                pass
                    elif chosen != mode and not supports_direct_portal(p):
                        statuses[mode] = '当前非默认上网接口；避免通过其他网络发送认证信息'
                    else:
                        key = connection_key(p, link, run_nmcli)
                        force = mode_filter in (mode,'all') or completed.get(mode) != key
                        if supports_direct_portal(p):
                            if not force and probe_interface_access(link['device']):
                                statuses[mode] = '已认证，网页访问正常'
                                continue
                            password = passwords.get(portal_secret_key(p), '')
                            if not password:
                                statuses[mode] = '未读取到上网密码，请检查密钥环或重新保存密码'
                                continue
                            result = authenticate_portal(p, password, link['device'])
                            if result.supported:
                                statuses[mode] = result.message
                                if result.authenticated:
                                    if probe_interface_access(link['device']):
                                        confirmed[mode] = key
                                        statuses[mode] = '认证成功：学校已确认登录，网页访问正常'
                                    else:
                                        statuses[mode] = '学校返回登录成功，网页访问检测未通过，将继续检测'
                                continue
                            if chosen != mode:
                                statuses[mode] = '当前门户需网页交互，请将该接口设为默认上网接口'
                                continue
                        state = connectivity_state(run_nmcli) if not force else 'unknown'
                        should_login = force or portal_check_needed(state, online_probe=probe_online)
                        statuses[mode] = '正在发起网页认证' if should_login else '互联网已连通，无需重复认证'
                        if should_login:
                            login_mode = mode
                if reconnect_wifi and not links.get('wifi') and not login_mode:
                    statuses['wifi'] = '校园 Wi-Fi 未接入，正在尝试连接'
                self.dispatch(self.auto_result, statuses, login_mode,
                              reconnect_wifi and not bool(links.get('wifi')) and not bool(login_mode), '', confirmed, generation)
            except Exception as exc:
                self.dispatch(self.auto_result, {}, None, False, str(exc), None, generation)
        threading.Thread(target=worker, daemon=True).start()
        return False

    def auto_result(self, statuses: dict, login_mode: str | None, reconnect_wifi: bool, error: str, confirmed=None, generation=None):
        if generation is not None and generation != getattr(self,'auth_generation',0):return
        self.busy = False
        if error:
            self.set_status('自动认证检测失败：' + error)
            return
        if confirmed:
            if not hasattr(self, 'authenticated_links'):
                self.authenticated_links = {}
            self.authenticated_links.update(confirmed)
        for mode, status in statuses.items():
            self.record_auto(mode, status)
        if statuses:
            active_mode = (getattr(self, 'connection', None) or {}).get('mode')
            if active_mode not in statuses:
                active_mode = next(iter(confirmed)) if confirmed else ('wifi' if 'wifi' in statuses else next(iter(statuses)))
            self.set_status(statuses[active_mode])
        if reconnect_wifi and self.store.automatic:
            self.connect_network(automatic=True, mode_override='wifi')
        if login_mode:
            self.open_portal(manual=False, mode_override=login_mode)

    def open_portal(self, manual=True, mode_override=None):
        mode = mode_override or self.store.mode
        p = self.store.profiles[mode]
        url = p['portalUrl']
        if not p['portalEnabled'] or not valid_portal(url) or not url:
            self.send({'kind': 'page', 'page': 'settings'})
            self.set_status('请先启用网页认证并填写有效的认证地址')
            return
        if not manual:
            if not valid_portal(url) or not p['username']:
                self.record_auto(mode, '自动认证需要学校认证地址与账号')
                return
        password = self.passwords.get(portal_secret_key(p), '')
        if not manual and not password:
            self.record_auto(mode, '请先保存上网密码并解锁系统密钥环')
            return
        from embedded_portal import EmbeddedPortal
        if self.embedded_portal is None:
            self.embedded_portal = EmbeddedPortal(self.portal_message, self.portal_navigated)
            self.portal_box = self.embedded_portal.widget
            self.portal_box.set_no_show_all(True)
            self.portal_box.set_halign(self.Gtk.Align.START)
            self.portal_box.set_valign(self.Gtk.Align.START)
            self.overlay.add_overlay(self.portal_box)
            self.portal_box.hide()
        self.portal_page = 'portal'
        self.send({'kind': 'page', 'page': 'portal'})
        self.embedded_portal.start({'url': url, 'username': p['username'], 'password': password,
                                    'automatic': True,
                                    'allow_http': url.lower().startswith('http://')})
        self.set_status('学校认证页面已在主窗口中打开')

    def portal_message(self, message):
        self.portal_status = message
        self.set_status(message)

    def portal_navigated(self, address):
        self.portal_address = address
        self.send_state()

    def update_portal_bounds(self, data):
        if self.portal_box is None or self.portal_page != 'portal':
            return
        import math
        values = [float(data.get(key, 0)) for key in ('x', 'y', 'width', 'height')]
        if not all(math.isfinite(value) for value in values):return
        x, y, width, height = [int(round(value)) for value in values]
        viewport_width = self.view.get_allocated_width()
        viewport_height = self.view.get_allocated_height()
        x, y = max(0, min(x, viewport_width)), max(0, min(y, viewport_height))
        width, height = min(width, viewport_width-x), min(height, viewport_height-y)
        if width < 10 or height < 10:
            self.portal_box.hide()
            return
        self.portal_box.set_margin_start(x)
        self.portal_box.set_margin_top(y)
        self.portal_box.set_size_request(width, height)
        self.portal_box.show()


def launch() -> int:
    if '--diagnose' in sys.argv[1:]:
        print_diagnostics()
        print('HTML:', str(HTML), 'exists' if HTML.is_file() else 'missing')
        return 0
    try:
        import gi
        gi.require_version('Gtk', '3.0')
        gi.require_version('WebKit2', '4.1')
        from gi.repository import Gtk
    except (ImportError, ValueError) as exc:
        print('需要 GTK3 与 WebKit2GTK 4.1：' + str(exc), file=sys.stderr)
        return 2
    if not Gtk.init_check()[0]:
        print('需要 Ubuntu 图形桌面会话（DISPLAY/Wayland）', file=sys.stderr)
        return 2
    smoke = '--smoke-test' in sys.argv[1:]
    lock = None
    if not smoke:
        lock = SingleInstance('wiflow-main', lambda: None)
        if not lock.primary:
            return 0
    try:
        try:
            removed, skipped = cleanup_legacy_wifi_profiles(run_nmcli) if not smoke else (0, 0)
        except (RuntimeError, OSError, ValueError) as exc:
            removed, skipped = 0, 0
            print('旧版 Wi-Fi 配置清理失败：' + str(exc), file=sys.stderr)
        app = WebApplication(smoke_test=smoke)
        if removed:
            app.status = f'已清理 {removed} 个旧版 WIFI助手 网络配置；如断网请连接原始 SSID'
        if skipped:
            print(f'有 {skipped} 个旧版配置未通过验证或删除失败', file=sys.stderr)
        app.instance = lock
        if lock:
            lock.on_wake = app.restore_window
        Gtk.main()
        return getattr(app, 'exit_code', 0)
    finally:
        if lock:
            lock.close()


if __name__ == '__main__':
    try:
        sys.exit(launch())
    except Exception:
        log = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache')) / 'wiflow' / 'startup.log'
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open('a', encoding='utf-8') as stream:
            traceback.print_exc(file=stream)
        traceback.print_exc()
        sys.exit(1)
