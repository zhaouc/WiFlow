import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from network_core import AUTO_CHECK_INTERVAL_MS, load_settings, save_settings, portal_check_needed
from portal_window import login_origin_matches, prefill_script
from credentials import put_secret, get_secret, connect_wifi, find_wifi_profile, cleanup_legacy_wifi_profiles
from instance import SingleInstance


class AuthTests(unittest.TestCase):
    def test_portal_detection_with_networkmanager_state(self):
        self.assertTrue(portal_check_needed('portal', online_probe=lambda: True))
        self.assertTrue(portal_check_needed('limited', online_probe=lambda: True))
        self.assertTrue(portal_check_needed('none', online_probe=lambda: False))
        self.assertFalse(portal_check_needed('full', online_probe=lambda: True))
        self.assertTrue(portal_check_needed('full', online_probe=lambda: False))

    def test_5_minute_interval(self):
        self.assertEqual(AUTO_CHECK_INTERVAL_MS, 300_000)

    def test_secret_never_saved_to_plaintext(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'settings.json'
            save_settings({'ssid': 'School', 'portal_url': 'https://example.org',
                           'username': 'student', 'auto_connect': True,
                           'auto_auth': True, 'portal_password': 'private!',
                           'wifi_password': 'wifi-pass'}, path)
            content = path.read_text()
            self.assertNotIn('private!', content)
            self.assertNotIn('wifi-pass', content)
            self.assertTrue(load_settings(path)['auto_auth'])

    def test_exact_https_only_for_credential_autofill(self):
        self.assertTrue(login_origin_matches('https://portal.example.edu/auth',
                                              'https://portal.example.edu/login'))
        self.assertFalse(login_origin_matches('http://portal.example.edu',
                                               'http://portal.example.edu/login'))
        self.assertFalse(login_origin_matches('https://portal.example.edu',
                                               'https://evil.example.edu'))
        self.assertFalse(login_origin_matches('https://portal.example.edu',
                                               'https://portal.example.edu.evil.com'))

    def test_https_default_port_same_origin(self):
        self.assertTrue(login_origin_matches('https://portal.example.edu/login',
                                             'https://portal.example.edu:443/redirect'))
        self.assertTrue(login_origin_matches('https://portal.example.edu:443/login',
                                             'https://portal.example.edu/redirect'))
        self.assertFalse(login_origin_matches('https://portal.example.edu/login',
                                              'https://portal.example.edu:8443/login'))

    def test_wpa3_only_uses_sae_and_never_stores_psk_in_profile(self):
        commands = []
        def fake_runner(*args):
            commands.append(args)
            if args[:3] == ('-t', '-f', 'SSID,SECURITY'):
                return 'Campus:WPA3\n'
            return ''
        connect_wifi('Campus', 'some-password', fake_runner)
        self.assertTrue(any(cmd[:2] == ('connection', 'modify') and
            ('wifi-sec.key-mgmt', 'sae') == cmd[4:6] and
            ('wifi-sec.psk-flags', '2') == cmd[6:8] for cmd in commands))

    def test_enterprise_network_not_misconfigured_as_personal(self):
        def fake_runner(*args):
            if args[:3] == ('-t', '-f', 'SSID,SECURITY'):
                return 'Campus:WPA2 802.1X\n'
            return ''
        with self.assertRaisesRegex(ValueError, '802.1X'):
            connect_wifi('Campus', 'secret-password', fake_runner)

    def test_saved_network_reuses_existing_native_profile(self):
        calls = []
        def fake_runner(*args):
            calls.append(args)
            if args[:3] == ('-t', '-f', 'NAME,UUID,TYPE'):
                return 'Personal-Campus:personal-uuid:wifi\n'
            if args[:3] == ('-g', '802-11-wireless.ssid', 'connection'):
                return 'Campus\n'
            if args[:3] == ('-g', 'connection.id', 'connection'):
                return 'Personal-Campus\n'
            return ''
        connect_wifi('Campus', 'new-password', fake_runner)
        self.assertIn(('connection','up','uuid','personal-uuid'), calls)
        self.assertFalse(any(cmd[:3] == ('connection','add','type') for cmd in calls))
        self.assertFalse(any(cmd[:3] == ('connection','modify','uuid') for cmd in calls))

    def test_generated_javascript_is_syntactically_valid_and_quoted(self):
        script = prefill_script('user"; alert(9); //', "pass'wd", True)
        self.assertIn('const user = "user\\\"; alert(9); //"', script)
        self.assertIn('const user =', script)
        self.assertIn('requestSubmit', script)
        if __import__('shutil').which('node'):
            with tempfile.TemporaryDirectory() as d:
                source = Path(d) / 'portal-check.js'
                source.write_text(script)
                result = subprocess.run(['node', '--check', str(source)], text=True, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_saved_profile_with_different_name_uses_correct_uuid(self):
        calls = []
        def fake_runner(*args):
            calls.append(args)
            if args[:3] == ('-t', '-f', 'NAME,UUID,TYPE'):
                return 'Office-WiFi:1111:wifi\nCampusProfile:2222:wifi\n'
            if args[:3] == ('-g', '802-11-wireless.ssid', 'connection'):
                return 'Office\n' if args[-1] == '1111' else 'Campus\n'
            return ''
        connect_wifi('Campus', '', fake_runner)
        self.assertIn(('connection', 'up', 'uuid', '2222'), calls)
        self.assertFalse(any(c[:3] == ('connection', 'add', 'type') for c in calls))

    def test_open_wifi_can_connect_without_preexisting_profile(self):
        calls = []
        def fake_runner(*args):
            calls.append(args)
            return ''
        connect_wifi('Guest', '', fake_runner)
        self.assertIn(('device', 'wifi', 'connect', 'Guest'), calls)

    def test_password_only_in_temporary_file_not_args(self):
        called = []
        def fake_runner(*args):
            called.append(args)
            if args[:3] == ('-t', '-f', 'NAME,UUID,TYPE'):
                return ''  # no saved profile: must create permanent original-SSID entry
            if args[:3] == ('-t', '-f', 'SSID,SECURITY'):
                return 'Campus:WPA2\n'
            if 'passwd-file' in args:
                pwd_file = Path(args[-1])
                self.assertEqual(pwd_file.stat().st_mode & 0o777, 0o600)
                self.assertIn('secret-pass', pwd_file.read_text())
            return ''
        connect_wifi('Campus', 'secret-pass', fake_runner)
        self.assertTrue(any('passwd-file' in cmd for cmd in called))
        self.assertTrue(all('secret-pass' not in cmd for cmd in called))
        self.assertFalse(Path(called[-1][-1]).exists())


    def test_password_to_previously_open_network_keeps_user_profile_untouched(self):
        calls = []
        def fake_runner(*args):
            calls.append(args)
            if args[:3] == ('-t', '-f', 'NAME,UUID,TYPE'):
                return 'Original-Open:old-uuid:wifi\n'
            if args[:3] == ('-g', '802-11-wireless.ssid', 'connection'):
                return 'Campus\n'
            if args[:3] == ('-g', '802-11-wireless-security.key-mgmt', 'connection'):
                return 'none\n'
            return ''
        connect_wifi('Campus', 'new-password', fake_runner)
        self.assertIn(('connection', 'up', 'uuid', 'old-uuid'), calls)
        self.assertFalse(any(c[:3] == ('connection', 'add', 'type') for c in calls))
        self.assertFalse(any(c[:3] == ('connection', 'modify', 'uuid') for c in calls))

    def test_find_wifi_profile_ignores_old_generated_profile(self):
        def fake_runner(*args):
            if args[:3] == ('-t', '-f', 'NAME,UUID,TYPE'):
                return 'SchoolNetwork:aaa:wifi\nWIFI助手-1234567890ABCDEF:bbb:wifi\n'
            if args[:3] == ('-g', '802-11-wireless.ssid', 'connection'):
                return 'Campus\n'
            return ''
        self.assertEqual(find_wifi_profile('Campus', fake_runner), ('uuid', 'aaa'))

    def test_cleanup_verified_legacy_profiles_even_if_active(self):
        import hashlib
        name = 'WIFI助手-' + hashlib.sha256('Campus'.encode('utf-8')).hexdigest()[:16]
        calls = []
        def fake_runner(*args):
            calls.append(args)
            if args[:3] == ('-t', '-f', 'NAME,UUID,TYPE'):
                return f'{name}:legacy-uuid:wifi\nOriginal:normal-uuid:wifi\nWIFI助手-not-a-hash:other-uuid:wifi\n'
            if args[:3] == ('-g', '802-11-wireless.ssid', 'connection'):
                return 'Campus\n'
            return ''
        removed, skipped = cleanup_legacy_wifi_profiles(fake_runner)
        self.assertEqual((removed, skipped), (1, 1))
        self.assertIn(('connection', 'delete', 'uuid', 'legacy-uuid'), calls)
        self.assertNotIn(('connection', 'delete', 'uuid', 'normal-uuid'), calls)
        self.assertNotIn(('connection', 'delete', 'uuid', 'other-uuid'), calls)

    def test_settings_do_not_embed_passwords(self):
        import web_main
        p = web_main.default_profile('wifi')
        self.assertNotIn('password', p)
        self.assertNotIn('wifiPassword', p)

    def test_portal_popup_is_single_window_and_form_action_is_same_origin(self):
        source = (Path(__file__).resolve().parent / 'portal_window.py').read_text(encoding='utf-8')
        self.assertNotIn('create_window(subview=popup)', source)
        self.assertIn('shell.remove(view)', source)
        self.assertIn("action.origin !== location.origin", prefill_script('u','p',True))

    def test_blank_password_does_not_reuse_secured_wifi(self):
        calls = []
        def fake_runner(*args):
            calls.append(args)
            if args[:3] == ('-t', '-f', 'NAME,UUID,TYPE'):
                return 'SchoolConfig:1234:wifi\n'
            if args[:3] == ('-g', '802-11-wireless.ssid', 'connection'):
                return 'Campus\n'
            if args[:3] == ('-g', '802-11-wireless-security.key-mgmt', 'connection'):
                return 'wpa-psk\n'
            return ''
        connect_wifi('Campus', '', fake_runner)
        self.assertIn(('connection','up','uuid','1234'), calls)
        self.assertFalse(any(c[:3] == ('connection','add','type') for c in calls))

    def test_blank_password_does_not_connect_to_scanned_secured_wifi(self):
        calls = []
        def fake_runner(*args):
            calls.append(args)
            if args[:3] == ('-t', '-f', 'SSID,SECURITY'):
                return 'Campus:WPA2\n'
            return ''
        with self.assertRaisesRegex(ValueError, '需要密码'):
            connect_wifi('Campus', '', fake_runner)
        self.assertFalse(any(c[:3] == ('device','wifi','connect') for c in calls))

    def test_instance_second_launch_sends_wake(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.dict(os.environ, {'XDG_RUNTIME_DIR': d}):
                primary = SingleInstance('test-assistant', lambda: seen.append('wake'))
                seen = []
                try:
                    secondary = SingleInstance('test-assistant', lambda: None)
                    self.assertFalse(secondary.primary)
                    for _ in range(20):
                        import time
                        primary.poll()
                        if seen: break
                        time.sleep(0.025)
                    self.assertEqual(seen, ['wake'])
                finally:
                    primary.close()
