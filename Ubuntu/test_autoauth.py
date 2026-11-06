"""Auto-auth routing and config tests, no GTK/real network required."""
from pathlib import Path
from unittest.mock import Mock, patch
import tempfile
import unittest

from network_core import current_networks, active_login_mode, matching_campus_link
import web_main


class AutoAuthTests(unittest.TestCase):
    def test_dual_active_networks_resolve_independently(self):
        def runner(*a):
            if a == ('-t','-f','DEVICE,TYPE,STATE,CONNECTION','device','status'):
                return 'wlp1s0:wifi:connected:Campus Network\nenp3s0:ethernet:connected:Wired Profile\n'
            if a == ('-t','-f','UUID,TYPE,DEVICE','connection','show','--active'):
                return 'abc:wifi:wlp1s0\nxyz:ethernet:enp3s0\n'
            if a == ('-g','802-11-wireless.ssid','connection','show','uuid','abc'):
                return 'University-WiFi\n'
            raise AssertionError(a)
        links = current_networks(runner)
        self.assertEqual(set(links), {'wifi', 'wired'})
        self.assertEqual(links['wifi']['ssid'], 'University-WiFi')
        self.assertEqual(active_login_mode(links,'enp3s0'), 'wired')
        self.assertEqual(active_login_mode(links,'wlp1s0'), 'wifi')
        self.assertIsNone(active_login_mode(links,''))

    def test_no_credentials_sent_to_other_wifi(self):
        self.assertFalse(matching_campus_link('wifi', {'ssid':'University-WiFi'},
                                               {'mode':'wifi','ssid':'CoffeeShop'}))
        self.assertTrue(matching_campus_link('wifi', {'ssid':'University-WiFi'},
                                              {'mode':'wifi','ssid':'University-WiFi'}))
        self.assertTrue(matching_campus_link('wired', {'ethernetName':'enp3s0'},
                                              {'mode':'wired','device':'enp3s0','profile':'Campus LAN'}))
        self.assertFalse(matching_campus_link('wired', {'ethernetName':'enp3s0'},
                                               {'mode':'wired','device':'enp2s0','profile':'Home LAN'}))

    def test_store_keeps_separate_switches_and_does_not_write_passwords(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / 'settings.json'
            store = web_main.Store(config)
            store.set_profile({'connectionMode':'wifi','ssid':'University-WiFi',
                               'portalUrl':'https://auth.wifi.example.edu/',
                               'username':'a','autoSubmit':True})
            store.set_profile({'connectionMode':'wired','portalUrl':'https://auth.lan.example.edu/',
                               'username':'b','ethernetName':'enp3s0','autoSubmit':False})
            store.automatic=True
            store.save()
            loaded = web_main.Store(config)
            self.assertTrue(loaded.profiles['wifi']['autoSubmit'])
            self.assertFalse(loaded.profiles['wired']['autoSubmit'])
            self.assertEqual(loaded.profiles['wired']['ethernetName'],'enp3s0')
            self.assertEqual(loaded.profiles['wifi']['ssid'],'University-WiFi')
            self.assertTrue(loaded.automatic)
            self.assertNotIn('password', config.read_text())

    def test_auto_selects_only_default_routed_link(self):
        app = web_main.WebApplication.__new__(web_main.WebApplication)
        app.busy = False
        app.store = Mock()
        app.store.automatic = True
        app.store.profiles = {
            'wifi': {'connectionMode':'wifi','ssid':'Campus','portalEnabled':True,
                     'autoSubmit':True,'portalUrl':'https://school.example.edu/login', 'username':'user'},
            'wired': {'connectionMode':'wired','ssid':'','portalEnabled':True,
                      'autoSubmit':True,'portalUrl':'https://school.example.edu/login','username':'user'}}
        app.auto_events = {'wifi':'','wired':''}
        app.auto_checked = {'wifi':'','wired':''}
        app.send_auth_state = Mock()
        app.set_status = Mock()
        app.open_portal = Mock()
        app.connect_network = Mock()
        app.dispatch = lambda callback,*a: callback(*a)
        class DirectThread:
            def __init__(self, target,daemon=True): self.target=target
            def start(self): self.target()
        links = {'wifi':{'mode':'wifi','ssid':'Campus','device':'wlan0'},
                 'wired':{'mode':'wired','ssid':'','device':'eth0'}}
        with patch.object(web_main.threading,'Thread',DirectThread), \
             patch.object(web_main,'current_networks',return_value=links), \
             patch.object(web_main,'default_route_device',return_value='eth0'), \
             patch.object(web_main,'run_nmcli',return_value='portal'), \
             patch.object(web_main,'portal_check_needed',return_value=True):
            app.auto_one_shot()
        app.open_portal.assert_called_once_with(manual=False,mode_override='wired')
        self.assertIn('非默认上网接口',app.auto_events['wifi'])
        self.assertFalse(app.busy)

    def test_page_contains_visible_controls(self):
        html = web_main.HTML.read_text(encoding='utf-8')
        for ident in ('page-autoauth','wifiAutoAuth','wiredAutoAuth',
                      'wifi-auth-status','wired-auth-status'):
            self.assertIn(f'id="{ident}"',html)
        self.assertIn("post('setAutoMode'",html)
        self.assertIn('data-action="autoCheck"',html)
        self.assertNotIn('#wired-fields{display:none!important}',html)


if __name__ == '__main__': unittest.main()
