"""Regressions reproduced from Ubuntu NetworkManager failures."""
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import Mock, patch
import credentials
import network_core
import web_main


class DirectThread:
    def __init__(self, target, **kwargs): self.target = target
    def start(self): self.target()


class NetworkRegressions(unittest.TestCase):
    def test_refresh_retains_connected_wifi_when_recheck_denied(self):
        app = web_main.WebApplication.__new__(web_main.WebApplication)
        app.busy = False
        app.connection = None
        app.store = Mock(mode='wifi')
        app.set_status = Mock()
        app.dispatch = lambda callback, *args: callback(*args)
        link = {'mode': 'wifi', 'ssid': 'Campus', 'device': 'wlan0'}
        def runner(*args):
            if args == ('networking', 'connectivity', 'check'):
                raise RuntimeError('Not authorized to recheck connectivity')
            if args == ('networking', 'connectivity'): return 'full'
            raise AssertionError(args)
        with patch.object(web_main, 'current_networks', return_value={'wifi':link}), patch.object(web_main, 'run_nmcli', runner), patch.object(web_main.threading, 'Thread', DirectThread):
            app.refresh()
        self.assertEqual(app.connection, link)
        self.assertTrue(app.internet_online)
        self.assertFalse(app.busy)

    def test_connectivity_timeout_is_unknown(self):
        def runner(*args): raise subprocess.TimeoutExpired('nmcli', 20)
        self.assertEqual(network_core.connectivity_state(runner), 'unknown')

    def test_saved_nonpersistent_psk_supplied_privately_and_cleaned(self):
        paths, calls = [], []
        def runner(*args):
            calls.append(args)
            if args[:3] == ('-t', '-f', 'NAME,UUID,TYPE'): return 'Campus:uuid-campus:wifi'
            if args[:2] == ('-g', '802-11-wireless.ssid'): return 'Campus'
            if args[:2] == ('-g', '802-11-wireless-security.key-mgmt'): return 'wpa-psk'
            if args[:2] == ('-g', '802-11-wireless-security.psk-flags'): return '2'
            if args[:2] == ('connection', 'up'):
                self.assertIn('passwd-file', args)
                path = Path(args[-1])
                paths.append(path)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(path.read_text(), '802-11-wireless-security.psk:private-password\n')
        credentials.connect_wifi('Campus', 'private-password', runner)
        self.assertEqual(len(paths), 1)
        self.assertFalse(paths[0].exists())
        self.assertFalse(any('private-password' in arg for call in calls for arg in call))
        self.assertFalse(any(call[:2] == ('connection', 'modify') for call in calls))

    def test_secret_file_removed_on_activation_failure(self):
        paths = []
        def runner(*args):
            if args[:3] == ('-t', '-f', 'NAME,UUID,TYPE'): return 'Campus:uuid-campus:wifi'
            if args[:2] == ('-g', '802-11-wireless.ssid'): return 'Campus'
            if args[:2] == ('-g', '802-11-wireless-security.key-mgmt'): return 'sae'
            if args[:2] == ('-g', '802-11-wireless-security.psk-flags'): return '2'
            if args[:2] == ('connection', 'up'):
                self.assertIn('passwd-file', args)
                paths.append(Path(args[-1]))
                raise RuntimeError('activation failed')
        with self.assertRaisesRegex(RuntimeError, 'activation failed'):
            credentials.connect_wifi('Campus', 'private-password', runner)
        self.assertEqual(len(paths), 1)
        self.assertFalse(paths[0].exists())

    def test_saved_system_secret_is_not_overridden(self):
        calls = []
        def runner(*args):
            calls.append(args)
            if args[:3] == ('-t', '-f', 'NAME,UUID,TYPE'): return 'Campus:uuid-campus:wifi'
            if args[:2] == ('-g', '802-11-wireless.ssid'): return 'Campus'
            if args[:2] == ('-g', '802-11-wireless-security.key-mgmt'): return 'wpa-psk'
            if args[:2] == ('-g', '802-11-wireless-security.psk-flags'): return '0'
        credentials.connect_wifi('Campus', 'private-password', runner)
        self.assertIn(('connection', 'up', 'uuid', 'uuid-campus'), calls)
        self.assertFalse(any('passwd-file' in call for call in calls))


class ConnectionAndAuthenticationRegressions(unittest.TestCase):
    def application(self):
        app = web_main.WebApplication.__new__(web_main.WebApplication)
        app.busy = False
        app.store = Mock()
        app.store.mode = 'wifi'
        app.store.automatic = True
        app.store.profiles = {'wifi': {'connectionMode': 'wifi', 'ssid': 'Campus',
                              'portalUrl': 'https://school.example', 'username': 'student'}}
        app.passwords = {}
        app.set_status = Mock()
        app.record_auto = Mock()
        app.open_portal = Mock()
        app.connect_done = Mock()
        app.dispatch = lambda callback, *args: callback(*args)
        return app

    def test_repeated_auth_checks_have_no_time_cooldown(self):
        app = self.application()
        app.auto_result({}, 'wifi', False, '')
        app.auto_result({}, 'wifi', False, '')
        self.assertEqual(app.open_portal.call_count, 2)

    def test_manual_connect_does_not_reactivate_connected_target(self):
        app = self.application()
        link = {'mode': 'wifi', 'ssid': 'Campus', 'device': 'wlan0'}
        with patch.object(web_main, 'current_networks', return_value={'wifi': link}), patch.object(web_main, 'connect_wifi') as activate, patch.object(web_main.threading, 'Thread', DirectThread):
            app.connect_network(automatic=False)
        activate.assert_not_called()
        app.connect_done.assert_called_once_with(False, 'wifi', '')

    def test_disconnected_target_is_connected_with_application_secret(self):
        app = self.application()
        app.passwords = {'wifi:Campus': 'fixture-password'}
        with patch.object(web_main, 'current_networks', return_value={}), patch.object(web_main, 'connect_wifi') as activate, patch.object(web_main.threading, 'Thread', DirectThread):
            app.connect_network(automatic=False)
        activate.assert_called_once_with('Campus', 'fixture-password', web_main.run_nmcli)

    def test_connection_error_is_not_overwritten_by_refresh(self):
        app = self.application()
        del app.connect_done
        app.refresh = Mock()
        web_main.WebApplication.connect_done(app, False, 'wifi', 'password required')
        app.set_status.assert_called_once_with('连接失败：password required')
        app.refresh.assert_not_called()
        app.open_portal.assert_not_called()


class ConnectivityAndPortalRegressions(unittest.TestCase):
    def probe(self, status, body=b'', final='http://connectivity-check.ubuntu.com/'):
        from contextlib import nullcontext
        response=Mock(status=status)
        response.geturl.return_value=final
        response.read.return_value=body
        with patch('urllib.request.urlopen',return_value=nullcontext(response)):
            return network_core.probe_online()

    def test_ubuntu_204_is_online(self):
        self.assertTrue(self.probe(204))

    def test_legacy_ubuntu_200_marker_is_online(self):
        self.assertTrue(self.probe(200,b'NetworkManager is online\n'))

    def test_captive_portal_redirect_is_not_online(self):
        self.assertFalse(self.probe(204,final='http://portal.example/login'))

    def test_login_html_is_not_online(self):
        self.assertFalse(self.probe(200,b'<html>Login required</html>'))

    def test_http_origin_requires_opt_in_and_matching_port(self):
        from portal_window import login_origin_matches
        self.assertFalse(login_origin_matches('http://school.example:12345/','http://school.example:12345/login'))
        self.assertTrue(login_origin_matches('http://school.example:12345/','http://school.example:12345/login',allow_http=True))
        self.assertFalse(login_origin_matches('http://school.example:12345/','http://school.example/login',allow_http=True))


class HTTPConfigurationRegression(unittest.TestCase):
    def test_http_portal_is_reported_configured_in_desktop(self):
        app=web_main.WebApplication.__new__(web_main.WebApplication)
        app.store=Mock()
        app.store.automatic=True
        app.store.profiles={m:{'portalUrl':'http://school.example:12345/','username':'student','autoSubmit':True} for m in ('wifi','wired')}
        app.auto_events={'wifi':'','wired':''}
        app.auto_checked={'wifi':'','wired':''}
        app.send=Mock()
        app.send_auth_state()
        self.assertTrue(app.send.call_args.args[0]['configured']['wifi'])
