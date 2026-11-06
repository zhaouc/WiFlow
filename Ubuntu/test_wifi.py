import json
import subprocess
import sys
import os
from pathlib import Path
import tempfile
import unittest
from network_core import AUTO_CHECK_INTERVAL_MS, load_settings, needs_reconnect, parse_networks, save_settings, split_nmcli, valid_portal, current_network

class UbuntuTests(unittest.TestCase):
    def test_colon_escaped_ssid(self):
        self.assertEqual(split_nmcli(r'*:Campus\:WiFi:90:WPA2'), ['*','Campus:WiFi','90','WPA2'])

    def test_network_sort_and_merge(self):
        items = parse_networks(' :WiFi-A:20:WPA2\n*:WiFi-A:70:WPA2\n :Guest:66:--\n')
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]['ssid'], 'WiFi-A')
        self.assertTrue(items[0]['connected'])

    def test_safe_config(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'settings.json'
            save_settings({'ssid':'Campus','portal_url':'https://portal.example.edu','username':'u','password':'must-not-be-written','auto_connect':False}, p)
            self.assertNotIn('password', p.read_text())
            self.assertEqual(load_settings(p)['ssid'], 'Campus')
            self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)

    def test_auto_check_every_five_minutes(self):
        self.assertEqual(AUTO_CHECK_INTERVAL_MS, 300000)

    def test_avoid_reconnecting_active_wifi(self):
        self.assertFalse(needs_reconnect('wifi', 'Campus', 'wifi:Campus\n'))
        self.assertFalse(needs_reconnect('wifi', 'Campus', '802-11-wireless:OtherWiFi\n'))
        self.assertTrue(needs_reconnect('wifi', 'Campus', 'ethernet:Wired\n'))
        self.assertFalse(needs_reconnect('wifi', '', ''))

    def test_avoid_reconnecting_active_ethernet(self):
        self.assertFalse(needs_reconnect('wired', '', '802-3-ethernet:LAN\n'))
        self.assertFalse(needs_reconnect('wired', '', 'ethernet:LAN\n'))
        self.assertTrue(needs_reconnect('wired', '', 'wifi:Campus\n'))

    def test_html_ui_is_independent_of_tkinter(self):
        import web_main, network_core
        self.assertNotIn('import tkinter', (Path(__file__).parent / 'web_main.py').read_text())
        self.assertNotIn('import tkinter', (Path(__file__).parent / 'network_core.py').read_text())
        self.assertTrue(web_main.HTML.is_file())
        self.assertIn('<title>WiFlow</title>', web_main.HTML.read_text(encoding='utf-8'))

    def test_webkit_bridge_channel_matches_html(self):
        """Prevent a legacy listener from silently breaking page startup."""
        import web_main
        html = web_main.HTML.read_text(encoding='utf-8')
        self.assertEqual(web_main.BRIDGE_NAME, 'wiflowBridge')
        self.assertIn('messageHandlers.' + web_main.BRIDGE_NAME + '.postMessage(', html)
        self.assertIn("post('ready');", html)
        source = (Path(__file__).parent / 'web_main.py').read_text(encoding='utf-8')
        self.assertIn("'script-message-received::' + BRIDGE_NAME", source)
        self.assertNotIn('script-message-received::wifiAssistant', source)

    def test_smoke_ready_exits_without_network_operations(self):
        from unittest.mock import Mock
        import web_main
        app = web_main.WebApplication.__new__(web_main.WebApplication)
        app.ready = False
        app.smoke_test = True
        app.GLib = type('FakeGLib', (), {'idle_add': staticmethod(lambda cb: cb())})()
        app.stop_smoke_test = Mock()
        app.send_state = Mock()
        app.start_load_secrets = Mock()
        app.handle_command({'command': 'ready'})
        self.assertTrue(app.ready)
        app.stop_smoke_test.assert_called_once_with()
        app.send_state.assert_not_called()
        app.start_load_secrets.assert_not_called()

    def test_connected_wifi_is_detected_without_scan_in_use_marker(self):
        """The radio is connected even when a nearby-AP scan has no '*' entry."""
        commands = []
        def nmcli(*args):
            commands.append(args)
            if args == ('-t', '-f', 'DEVICE,TYPE,STATE,CONNECTION', 'device', 'status'):
                return 'wlp0s20f3:wifi:connected:Campus profile\nlo:loopback:connected (externally):lo\n'
            if args == ('-t', '-f', 'UUID,TYPE,DEVICE', 'connection', 'show', '--active'):
                return 'edaa-123:wifi:wlp0s20f3\n'
            if args == ('-g', '802-11-wireless.ssid', 'connection', 'show', 'uuid', 'edaa-123'):
                return 'Campus\n'
            raise AssertionError(args)
        actual = current_network(nmcli)
        self.assertEqual(actual['ssid'], 'Campus')
        self.assertEqual(actual['mode'], 'wifi')
        self.assertFalse(any('list' in command for command in commands))

    def test_connection_name_is_not_assumed_to_equal_ssid(self):
        def nmcli(*args):
            if 'device' in args and 'status' in args:
                return 'wlp3s0:wifi:connected:Student\\: Wi-Fi profile\n'
            if args[:3] == ('-t', '-f', 'UUID,TYPE,DEVICE'):
                return 'school-uuid:802-11-wireless:wlp3s0\n'
            if args[:2] == ('-g', '802-11-wireless.ssid'):
                return 'ACTUAL_CAMPUS_SSID\n'
            raise AssertionError(args)
        self.assertEqual(current_network(nmcli)['ssid'], 'ACTUAL_CAMPUS_SSID')

    def test_failed_ssid_lookup_still_reports_connected(self):
        def nmcli(*args):
            if args[:3] == ('-t', '-f', 'DEVICE,TYPE,STATE,CONNECTION'):
                return 'wlan0:wifi:connected (externally):MyProfile\n'
            raise RuntimeError('profile is not accessible')
        connected = current_network(nmcli)
        self.assertEqual(connected['mode'], 'wifi')
        self.assertEqual(connected['profile'], 'MyProfile')
        self.assertEqual(connected['ssid'], '')

    def test_wifi_not_connected_and_wired_connected(self):
        def wired_nmcli(*args):
            return 'wlp2s0:wifi:disconnected:--\nenp0s25:ethernet:connected:Wired connection 1\n'
        self.assertEqual(current_network(wired_nmcli)['mode'], 'wired')
        def offline_nmcli(*args):
            return 'wlan0:wifi:disconnected:--\n'
        self.assertIsNone(current_network(offline_nmcli))

    def test_store_prefers_real_ssid(self):
        import web_main
        profile = {'connectionMode': 'wifi', 'ssid': 'Campus', 'portalUrl': '',
                   'username': '', 'portalEnabled': True, 'autoSubmit': False}
        self.assertEqual(web_main.normalize_profile(profile, 'wifi')['ssid'], 'Campus')

    def test_url_validation(self):
        self.assertTrue(valid_portal('https://example.edu/login'))
        self.assertFalse(valid_portal('file:///etc/passwd'))
        self.assertFalse(valid_portal('https://user:password@example.edu/login'))

if __name__ == '__main__':
    unittest.main()
