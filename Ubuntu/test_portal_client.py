"""Portal protocol and automatic-login regressions; no real credentials/network."""
from contextlib import nullcontext
import json
from unittest.mock import Mock,patch
import unittest
from urllib.parse import parse_qs
import portal_client
import web_main
from test_network_regressions import DirectThread

PAGE=b'<div id="form1"><input id="username"><input id="password"><button onclick="DoLoginSubmit()">login</button><script>var endpoint="ProbeSocket.php";var method="userlogin";</script></div>'
PROFILE={'connectionMode':'wifi','ssid':'Campus','portalUrl':'http://school.example:12345/','username':'fixture-user','portalEnabled':True,'autoSubmit':True}


def response(body,url=PROFILE['portalUrl'],status=200):
    item=Mock(status=status)
    item.read.return_value=body
    item.geturl.return_value=url
    return nullcontext(item)


class PortalProtocolTests(unittest.TestCase):
    def opener(self,result):
        item=Mock()
        item.open.side_effect=[response(PAGE),response(json.dumps(result).encode())]
        return item

    def test_login_uses_expected_post_and_server_confirmation(self):
        opener=self.opener({'code':0,'msg':'ok'})
        result=portal_client.authenticate_portal(PROFILE,'fixture-password','wlan0',opener=opener)
        self.assertTrue(result.authenticated)
        request=opener.open.call_args_list[1].args[0]
        fields=parse_qs(request.data.decode())
        self.assertEqual(fields['usrname'],['fixture-user'])
        self.assertEqual(fields['usrpwd'],['fixture-password'])
        self.assertEqual(fields['method'],['userlogin'])
        self.assertEqual(request.full_url,'http://school.example:12345/ProbeSocket.php')
        self.assertNotIn('fixture-password',request.full_url+result.message)

    def test_unknown_page_never_receives_password(self):
        opener=Mock();opener.open.return_value=response(b'<html>Another login page</html>')
        result=portal_client.authenticate_portal(PROFILE,'fixture-password','wlan0',opener=opener)
        self.assertFalse(result.supported)
        self.assertEqual(opener.open.call_count,1)

    def test_cross_origin_entry_never_receives_password(self):
        opener=Mock();opener.open.return_value=response(PAGE,'http://other.example/')
        result=portal_client.authenticate_portal(PROFILE,'fixture-password','wlan0',opener=opener)
        self.assertFalse(result.authenticated)
        self.assertEqual(opener.open.call_count,1)

    def test_refusal_is_not_success_and_does_not_echo_secrets(self):
        result=portal_client.authenticate_portal(PROFILE,'fixture-password','wlan0',opener=self.opener({'code':5,'msg':'fixture-user fixture-password invalid'}))
        self.assertFalse(result.authenticated)
        self.assertNotIn('fixture-password',result.message)
        self.assertNotIn('fixture-user',result.message)

    def test_invalid_server_json_is_not_success(self):
        opener=Mock();opener.open.side_effect=[response(PAGE),response(b'not json')]
        self.assertFalse(portal_client.authenticate_portal(PROFILE,'fixture-password','wlan0',opener=opener).authenticated)

    def test_probe_rejects_redirect_to_school_portal(self):
        opener=Mock();opener.open.side_effect=[response(b'baidu',PROFILE['portalUrl']),response(b'neverssl',PROFILE['portalUrl'])]
        self.assertFalse(portal_client.probe_interface_access('wlan0',opener=opener))

    def test_probe_rejects_intercepted_html_at_original_url(self):
        opener=Mock();opener.open.side_effect=[response(b'Login required','http://www.baidu.com/'),response(b'Login required','http://neverssl.com/')]
        self.assertFalse(portal_client.probe_interface_access('wlan0',opener=opener))

    def test_probe_accepts_ordinary_http_content(self):
        opener=Mock();opener.open.return_value=response(b'<title>baidu</title>','http://www.baidu.com/')
        self.assertTrue(portal_client.probe_interface_access('wlan0',opener=opener))

    def test_interface_bind_failure_never_falls_back_to_unbound_socket(self):
        channel=Mock();channel.setsockopt.side_effect=PermissionError('not permitted')
        with patch.object(portal_client.socket,'getaddrinfo',return_value=[(2,1,6,'',('10.0.0.1',80))]),patch.object(portal_client.socket,'socket',return_value=channel):
            with self.assertRaises(PermissionError):portal_client._connect_on_interface('wlan0',('school.example',80))
        channel.connect.assert_not_called()
        channel.close.assert_called_once()


class AutomaticLoginTests(unittest.TestCase):
    def application(self):
        app=web_main.WebApplication.__new__(web_main.WebApplication)
        app.busy=False
        app.store=Mock();app.store.automatic=True;app.store.mode='wired'
        app.store.profiles={'wifi':PROFILE.copy(),'wired':{**PROFILE,'connectionMode':'wired','ssid':'','autoSubmit':False}}
        app.passwords={web_main.portal_secret_key(PROFILE):'fixture-password'}
        app.authenticated_links={}
        app.auto_events={'wifi':'','wired':''};app.auto_checked={'wifi':'','wired':''}
        app.send_auth_state=Mock();app.set_status=Mock();app.open_portal=Mock();app.connect_network=Mock()
        app.dispatch=lambda callback,*args:callback(*args)
        return app

    def run_case(self,app,*,manual=False,active='/active/1',success=True,route='wlan0'):
        link={'mode':'wifi','ssid':'Campus','device':'wlan0','profile':'Campus profile'}
        with patch.object(web_main.threading,'Thread',DirectThread),patch.object(web_main,'current_networks',return_value={'wifi':link}),patch.object(web_main,'default_route_device',return_value=route),patch.object(web_main,'run_nmcli',return_value=active),patch.object(web_main,'probe_online',return_value=True),patch.object(web_main,'connectivity_state',return_value='full'),patch.object(web_main,'probe_interface_access',return_value=True),patch.object(web_main,'authenticate_portal',return_value=portal_client.PortalResult(True,success,'accepted' if success else 'rejected')) as authenticate:
            app.auto_one_shot(mode_filter='wifi' if manual else None)
        return authenticate

    def key(self,active='/active/1'):
        return ('wlan0','Campus','Campus profile',active,PROFILE['portalUrl'],PROFILE['username'])

    def test_explicit_check_authenticates_even_when_probe_is_full(self):
        app=self.application();app.authenticated_links['wifi']=self.key()
        authenticate=self.run_case(app,manual=True)
        authenticate.assert_called_once_with(PROFILE,'fixture-password','wlan0')
        self.assertIn('学校已确认',app.auto_events['wifi'])
        app.open_portal.assert_not_called()

    def test_new_connection_authenticates_without_trusting_whitelisted_probe(self):
        app=self.application();authenticate=self.run_case(app)
        authenticate.assert_called_once()
        self.assertEqual(app.authenticated_links['wifi'],self.key())

    def test_confirmed_accessible_link_is_not_repeated(self):
        app=self.application();app.authenticated_links['wifi']=self.key()
        self.run_case(app).assert_not_called()

    def test_reconnection_with_same_ssid_is_authenticated(self):
        app=self.application();app.authenticated_links['wifi']=self.key()
        self.run_case(app,active='/active/2').assert_called_once()
        self.assertEqual(app.authenticated_links['wifi'],self.key('/active/2'))

    def test_failed_login_is_not_cached_or_cooled_down(self):
        app=self.application()
        self.run_case(app,success=False).assert_called_once()
        self.assertNotIn('wifi',app.authenticated_links)
        self.run_case(app,success=False).assert_called_once()

    def test_wifi_is_authenticated_on_its_interface_with_another_default_route(self):
        app=self.application();authenticate=self.run_case(app,route='eno1')
        authenticate.assert_called_once_with(PROFILE,'fixture-password','wlan0')


if __name__=='__main__':unittest.main()


class AutomaticQueueTests(unittest.TestCase):
    def test_manual_check_waits_for_current_operation_and_is_not_dropped(self):
        app=AutomaticLoginTests().application()
        app.busy=True
        app.GLib=Mock();app.GLib.timeout_add.return_value=7
        app.auto_one_shot(mode_filter='all')
        self.assertEqual(app.pending_auto_mode,'all')
        self.assertEqual(app.auto_retry_source,7)
        app.busy=False
        with patch.object(app,'auto_one_shot') as check:
            self.assertFalse(app.retry_auto_check())
            check.assert_called_once_with('all')

    def test_stopped_operation_cannot_overwrite_new_status(self):
        app=AutomaticLoginTests().application()
        app.auth_generation=2
        app.busy=True
        app.auto_result({'wifi':'old result'},None,False,'',{},1)
        self.assertEqual(app.auto_events['wifi'],'')
        self.assertTrue(app.busy)


class DirectCLITests(unittest.TestCase):
    def test_cli_uses_confirmed_school_api_instead_of_skipping_on_probe(self):
        import cli
        from contextlib import redirect_stdout
        import io
        args=cli.parser().parse_args(['login','--mode','wifi','--allow-http'])
        store=Mock();store.mode='wifi';store.profiles={'wifi':PROFILE.copy()}
        with patch.object(cli,'require_link',return_value={'device':'wlan0'}),patch.object(cli,'read_password',return_value='fixture-password'),patch.object(cli,'authenticate_portal',return_value=portal_client.PortalResult(True,True,'accepted')) as login,patch.object(cli,'probe_interface_access',return_value=True),patch.object(cli,'run_terminal_backend') as browser,redirect_stdout(io.StringIO()):
            self.assertEqual(cli.login_command(args,store),0)
        login.assert_called_once_with(PROFILE,'fixture-password','wlan0')
        browser.assert_not_called()

    def test_cli_does_not_report_rejected_credentials_as_online(self):
        import cli
        from contextlib import redirect_stdout
        import io
        args=cli.parser().parse_args(['login','--mode','wifi','--allow-http'])
        store=Mock();store.mode='wifi';store.profiles={'wifi':PROFILE.copy()}
        with patch.object(cli,'require_link',return_value={'device':'wlan0'}),patch.object(cli,'read_password',return_value='fixture-password'),patch.object(cli,'authenticate_portal',return_value=portal_client.PortalResult(True,False,'rejected')),patch.object(cli,'probe_online',return_value=True),redirect_stdout(io.StringIO()):
            self.assertEqual(cli.login_command(args,store),5)


class PortalChallengeTests(unittest.TestCase):
    def test_captcha_falls_back_to_interactive_page_without_submitting(self):
        opener=Mock();opener.open.return_value=response(PAGE+b'<input name="captcha">')
        result=portal_client.authenticate_portal(PROFILE,'fixture-password','wlan0',opener=opener)
        self.assertFalse(result.supported)
        self.assertEqual(opener.open.call_count,1)


class EmbeddedImmediateAuthTests(unittest.TestCase):
    def test_user_immediate_auth_submits_even_when_background_auto_auth_is_off(self):
        from embedded_portal import EmbeddedPortal
        app=web_main.WebApplication.__new__(web_main.WebApplication)
        app.store=Mock();app.store.mode='wifi'
        app.store.profiles={'wifi':{**PROFILE,'autoSubmit':False}}
        app.passwords={web_main.portal_secret_key(PROFILE):'fixture-password'}
        app.embedded_portal=None;app.overlay=Mock();app.Gtk=Mock()
        app.send=Mock();app.set_status=Mock()
        browser=Mock()
        with patch('embedded_portal.EmbeddedPortal',return_value=browser):
            app.open_portal(manual=True)
        self.assertTrue(browser.start.call_args.args[0]['automatic'])
        self.assertEqual(browser.start.call_args.args[0]['password'],'fixture-password')
