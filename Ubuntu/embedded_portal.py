"""A separate WebKit context embedded visually in the main application window."""
from __future__ import annotations
from portal_window import login_origin_matches,prefill_script
from network_core import valid_portal


class EmbeddedPortal:
    def __init__(self,on_status,on_address):
        import gi
        gi.require_version('Gtk','3.0')
        gi.require_version('WebKit2','4.1')
        from gi.repository import Gtk,GLib,WebKit2
        self.GLib,self.WebKit2=GLib,WebKit2
        self.on_status,self.on_address=on_status,on_address
        self.widget=Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.context=WebKit2.WebContext.new_ephemeral()
        self.context.connect('download-started',lambda _context,item:item.cancel())
        self.view=None
        self.timer=None
        self.config={}
        self.attempts=0
        self.generation=0
        self._replace_view(WebKit2.WebView.new_with_context(self.context))

    def _replace_view(self,view):
        self.stop_retry()
        if self.view is not None:
            self.widget.remove(self.view)
            self.view.destroy()
        self.view=view
        self.widget.pack_start(view,True,True,0)
        view.connect('load-changed',self.loaded)
        view.connect('load-failed',self.load_failed)
        view.connect('load-failed-with-tls-errors',self.tls_failed)
        view.connect('create',self.popup)
        view.connect('script-dialog',self.dialog)
        view.connect('permission-request',lambda _view,request:(request.deny(),True)[1])
        view.connect('decide-policy',self.policy)
        view.show()

    def start(self,config):
        self.stop_retry()
        self.config=config.copy()
        self.generation+=1
        self.attempts=0
        self.on_status('正在加载学校认证页面…')
        self.view.load_uri(config['url'])

    def loaded(self,view,phase):
        if view is not self.view:return
        current=view.get_uri() or ''
        self.on_address(current)
        if phase==self.WebKit2.LoadEvent.STARTED:
            self.stop_retry()
            self.generation+=1
            self.attempts=0
        if phase==self.WebKit2.LoadEvent.FINISHED:
            if login_origin_matches(self.config.get('url',''),current,allow_http=self.config.get('allow_http',False)):
                self.fill()
                self.timer=self.GLib.timeout_add(800,self.fill)
            elif current and current!='about:blank':
                self.on_status('学校页面已跳转；请确认地址后手动完成认证')

    def fill(self):
        if not self.view or self.attempts>=12:
            self.timer=None
            return False
        if not login_origin_matches(self.config.get('url',''),self.view.get_uri() or '',allow_http=self.config.get('allow_http',False)):
            return False
        self.attempts+=1
        script=prefill_script(self.config.get('username',''),self.config.get('password',''),self.config.get('automatic',False),allow_http=self.config.get('allow_http',False))
        self.view.run_javascript(script,None,self.filled,self.generation)
        return True

    def filled(self,view,result,generation):
        if view is not self.view or generation!=self.generation:return
        try:value=view.run_javascript_finish(result).get_js_value().to_string()
        except Exception:return
        if value in ('manual','submitted','waiting','wrong-portal'):
            self.stop_retry()
            messages={'manual':'学校页面已打开，请完成登录或验证码',
                      'submitted':'已自动填写并提交认证',
                      'waiting':'已自动填写并提交认证',
                      'wrong-portal':'当前地址是设备管理后台，请修改校园网认证地址'}
            self.on_status(messages[value])

    def policy(self,_view,decision,kind):
        if kind==self.WebKit2.PolicyDecisionType.NAVIGATION_ACTION:
            uri=decision.get_navigation_action().get_request().get_uri()
            if uri!='about:blank' and not valid_portal(uri):
                decision.ignore()
                self.on_status('学校认证页面仅支持 HTTP/HTTPS 地址')
                return True
        return False

    def popup(self,_view,_action):
        self.on_status('学校页面请求新窗口，请在系统浏览器中手动处理')
        return None

    def dialog(self,_view,dialog):
        message=dialog.get_message() or ''
        for key in ('password','username'):
            secret=self.config.get(key,'')
            if secret:message=message.replace(secret,'[已隐藏]')
        self.on_status('学校返回：'+message[:180])
        return False

    def load_failed(self,*_):
        self.stop_retry()
        self.on_status('学校认证页面加载失败，请检查地址和网络')
        return False

    def tls_failed(self,*_):
        self.stop_retry()
        self.on_status('学校 HTTPS 证书不受信任，请核实认证地址和学校证书')
        return False

    def stop_retry(self):
        if self.timer is not None:
            source=self.GLib.MainContext.default().find_source_by_id(self.timer)
            if source is not None:source.destroy()
            self.timer=None

    def stop(self):
        self.stop_retry()
        self.generation+=1
        self.config={}
        if self.view:self.view.load_uri('about:blank')
