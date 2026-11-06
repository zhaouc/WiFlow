from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
from system_network import connect_wired,network_choices,launch_client,open_network_settings


class SystemNetworkTests(unittest.TestCase):
    def test_pppoe_uses_native_saved_uuid(self):
        runner=Mock(return_value='Campus:campus-id:pppoe\nVPN:vpn-id:vpn\n')
        connect_wired({'wiredAuth':'pppoe','broadbandName':'Campus'},runner)
        runner.assert_any_call('connection','up','uuid','campus-id')

    def test_pppoe_does_not_dial_other_connection_types(self):
        runner=Mock(return_value='Campus:vpn-id:vpn\n')
        with self.assertRaises(RuntimeError):connect_wired({'wiredAuth':'pppoe','broadbandName':'Campus'},runner)
        self.assertEqual(runner.call_count,1)

    def test_8021x_reuses_system_profile(self):
        runner=Mock(return_value='Campus wired:wired-id:802-3-ethernet\n')
        connect_wired({'wiredAuth':'dot1x','ethernetName':'Campus wired'},runner)
        runner.assert_any_call('connection','up','uuid','wired-id')

    def test_interface_can_be_selected_without_profile_name(self):
        def result(*args):
            if args==('-t','-f','NAME,UUID,TYPE','connection','show'):return 'Other:other-id:ethernet\n'
            if args==('-t','-f','DEVICE,TYPE','device','status'):return 'eno1:ethernet\n'
            return ''
        runner=Mock(side_effect=result)
        connect_wired({'wiredAuth':'web','ethernetName':'eno1'},runner)
        runner.assert_any_call('device','connect','eno1')
        self.assertFalse(any(call.args[:2]==('connection','up') for call in runner.call_args_list))

    def test_choices_keep_broadband_and_ethernet_separate(self):
        runner=Mock(side_effect=['eno1:ethernet\nwlan0:wifi\n','School:pppoe-id:pppoe\nVPN:vpn-id:vpn\n'])
        self.assertEqual(network_choices(runner),(['eno1'],['School']))

    def test_windows_executable_is_not_launched_as_linux_client(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'client.exe';path.write_text('fixture');path.chmod(0o700)
            with patch('system_network.subprocess.Popen') as start:
                with self.assertRaises(RuntimeError):launch_client(str(path))
                start.assert_not_called()

    def test_system_settings_use_native_ui(self):
        with patch('system_network.shutil.which',return_value='/usr/bin/gnome-control-center'),patch('system_network.subprocess.Popen') as start:
            open_network_settings('networkConnections')
        self.assertEqual(start.call_args.args[0],['gnome-control-center','network'])

    def test_profile_preserves_system_authentication_fields(self):
        from web_main import normalize_profile
        p=normalize_profile({'wiredAuth':'pppoe','broadbandName':'School','ethernetName':'eno1'},'wired')
        self.assertEqual((p['wiredAuth'],p['broadbandName'],p['ethernetName']),('pppoe','School','eno1'))


if __name__=='__main__':unittest.main()


class BroadbandLinkTests(unittest.TestCase):
    def test_active_pppoe_interface_is_selected_over_ethernet_carrier(self):
        from network_core import current_networks
        runner=Mock(return_value='eno1:ethernet:connected:Carrier\nppp0:ppp:connected:Campus PPPoE\n')
        self.assertEqual(current_networks(runner)['wired']['device'],'ppp0')
