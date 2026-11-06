"""CLI behavior, credential handling, routing and shared configuration tests."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import cli
from terminal_portal import same_login_origin
from web_main import Store, normalize_profile, portal_secret_key


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'settings.json')
        self.store.set_profile({'connectionMode': 'wifi', 'ssid': 'Campus',
                                'portalUrl': 'https://school.example/login', 'username': 'student'})
        self.login = cli.parser().parse_args(['login', '--mode', 'wifi'])

    def test_wired_interface_name_survives_shared_normalization(self):
        profile = normalize_profile({'ethernetName': 'eno1'}, 'wired')
        self.assertEqual(profile['ethernetName'], 'eno1')
        self.assertFalse(profile['autoSubmit'])

    def test_cli_config_round_trips_into_desktop_store(self):
        args = cli.parser().parse_args(['configure', '--mode', 'wired', '--ethernet-name', 'eno1', '--portal-url', 'https://school.example', '--username', 'user'])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli.configure_command(args, self.store), 0)
        restored = Store(self.store.path)
        self.assertEqual(restored.profiles['wired']['ethernetName'], 'eno1')
        self.assertEqual(restored.profiles['wifi']['ssid'], 'Campus')
        self.assertNotIn('password', self.store.path.read_text())

    def test_saved_secret_uses_normalized_profile_key(self):
        args = cli.parser().parse_args(['configure', '--mode', 'wifi', '--portal-url', ' https://school.example/new ', '--username', ' student ', '--set-password'])
        with patch.object(cli.getpass, 'getpass', return_value='fixture-password'), patch.object(cli, 'put_secret', return_value=True) as save_secret, redirect_stdout(io.StringIO()):
            cli.configure_command(args, self.store)
        save_secret.assert_called_once_with(portal_secret_key(self.store.profiles['wifi']), 'fixture-password')

    def test_password_stdin_is_not_echoed(self):
        stream = io.StringIO()
        with patch.object(cli.sys, 'stdin', io.StringIO('private-password\n')), redirect_stdout(stream):
            self.assertEqual(cli.read_password('unused', from_stdin=True), 'private-password')
        self.assertNotIn('private-password', stream.getvalue())

    def test_noninteractive_missing_secret_fails_clearly(self):
        stream = io.StringIO()
        with patch.object(cli, 'get_secret', return_value=''), patch.object(cli.sys, 'stdin', stream):
            with self.assertRaisesRegex(cli.CLIError, '--password-stdin'):
                cli.read_password('missing')

    def test_wrong_route_stops_before_reading_password(self):
        with patch.object(cli, 'require_link', side_effect=cli.CLIError('wrong route')), patch.object(cli, 'read_password') as password:
            with self.assertRaisesRegex(cli.CLIError, 'wrong route'):
                cli.login_command(self.login, self.store)
            password.assert_not_called()

    def test_require_link_rejects_other_ssid(self):
        with patch.object(cli, 'current_networks', return_value={'wifi': {'mode': 'wifi', 'ssid': 'Other', 'device': 'wlan0'}}):
            with self.assertRaisesRegex(cli.CLIError, '不匹配'):
                cli.require_link('wifi', self.store.profiles['wifi'])

    def test_require_link_rejects_single_link_on_other_route(self):
        with patch.object(cli, 'current_networks', return_value={'wifi': {'mode': 'wifi', 'ssid': 'Campus', 'device': 'wlan0'}}), patch.object(cli, 'default_route_device', return_value='tun0'):
            with self.assertRaisesRegex(cli.CLIError, '默认上网接口'):
                cli.require_link('wifi', self.store.profiles['wifi'])

    def test_already_online_does_not_submit_or_request_password(self):
        with patch.object(cli, 'require_link'), patch.object(cli, 'probe_online', return_value=True), patch.object(cli, 'read_password') as password, patch.object(cli, 'run_terminal_backend') as backend, redirect_stdout(io.StringIO()):
            self.assertEqual(cli.login_command(self.login, self.store), 0)
            password.assert_not_called()
            backend.assert_not_called()

    def test_http_login_requires_explicit_option(self):
        self.store.profiles['wifi']['portalUrl'] = 'http://school.example'
        with self.assertRaisesRegex(cli.CLIError, '--allow-http'):
            cli.login_command(self.login, self.store)

    def test_untrusted_and_different_origins_never_match(self):
        self.assertFalse(same_login_origin('http://school.example', 'http://school.example'))
        self.assertTrue(same_login_origin('http://school.example', 'http://school.example:80', True))
        self.assertFalse(same_login_origin('http://school.example', 'http://evil.example', True))
        self.assertFalse(same_login_origin('https://school.example', 'http://school.example', True))

    def test_backend_password_uses_stdin_and_not_command_arguments(self):
        process = Mock(returncode=0)
        process.communicate.return_value = (json.dumps({'message': 'online'}), '')
        with patch.object(cli.subprocess, 'Popen', return_value=process) as launch, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.run_terminal_backend({'password': 'private-password'}, 5), 0)
        self.assertNotIn('private-password', repr(launch.call_args))
        self.assertEqual(json.loads(process.communicate.call_args.args[0])['password'], 'private-password')
        self.assertNotIn('private-password', output.getvalue())

    def test_backend_timeout_terminates_process_group(self):
        process = Mock(pid=4567)
        process.communicate.side_effect = [cli.subprocess.TimeoutExpired('xvfb', 25), ('', '')]
        with patch.object(cli.subprocess, 'Popen', return_value=process), patch.object(cli.os, 'killpg') as kill:
            with self.assertRaises(cli.CLIError): cli.run_terminal_backend({'password': 'secret'}, 5)
        kill.assert_called_once_with(4567, cli.signal.SIGTERM)

    def test_login_passes_only_selected_profile_to_backend(self):
        with patch.object(cli, 'require_link'), patch.object(cli, 'probe_online', return_value=False), patch.object(cli, 'read_password', return_value='private-password'), patch.object(cli.shutil, 'which', return_value='/usr/bin/xvfb-run'), patch.object(cli, 'run_terminal_backend', return_value=0) as backend, redirect_stdout(io.StringIO()):
            self.assertEqual(cli.login_command(self.login, self.store), 0)
        payload = backend.call_args.args[0]
        self.assertEqual(payload['mode'], 'wifi')
        self.assertEqual(payload['password'], 'private-password')
        self.assertEqual(payload['profile']['ssid'], 'Campus')

    def test_status_json_has_no_password(self):
        args = cli.parser().parse_args(['status', '--json'])
        with patch.object(cli, 'current_networks', return_value={}), patch.object(cli, 'default_route_device', return_value=''), patch.object(cli, 'connectivity_state', return_value='unknown'), redirect_stdout(io.StringIO()) as stream:
            self.assertEqual(cli.status_command(args), 0)
        data = json.loads(stream.getvalue())
        self.assertEqual(data['connections'], {})
        self.assertNotIn('password', stream.getvalue())

    def test_terminal_no_arguments_shows_help_without_gui(self):
        with redirect_stdout(io.StringIO()) as stream:
            self.assertEqual(cli.main(['--terminal']), 0)
        self.assertIn('login', stream.getvalue())


if __name__ == '__main__': unittest.main()
