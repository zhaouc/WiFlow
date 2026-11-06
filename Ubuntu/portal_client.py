"""Interface-bound authentication for the inspected ProbeSocket campus portal."""
from __future__ import annotations
from dataclasses import dataclass
import http.client
from http.cookiejar import CookieJar
import json
import re
import socket
import ssl
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import (HTTPHandler, HTTPSHandler, HTTPRedirectHandler,
                            HTTPCookieProcessor, ProxyHandler, Request, build_opener)


@dataclass(frozen=True)
class PortalResult:
    supported: bool
    authenticated: bool
    message: str


def supports_direct_portal(profile: dict) -> bool:
    try:
        u=urlsplit(profile.get('portalUrl',''))
        return (u.scheme in ('http','https') and bool(u.hostname) and u.port==12345
                and not u.username and not u.password)
    except ValueError:
        return False


def _origin(url):
    u=urlsplit(url)
    return u.scheme,u.hostname,u.port or (443 if u.scheme=='https' else 80)


def _connect_on_interface(device, address, timeout=8, source_address=None):
    last=None
    for family,kind,protocol,_,target in socket.getaddrinfo(address[0],address[1],socket.AF_UNSPEC,socket.SOCK_STREAM):
        channel=socket.socket(family,kind,protocol)
        try:
            channel.settimeout(timeout)
            channel.setsockopt(socket.SOL_SOCKET,socket.SO_BINDTODEVICE,device.encode()+b'\0')
            if source_address:channel.bind(source_address)
            channel.connect(target)
            return channel
        except OSError as exc:
            last=exc
            channel.close()
    raise last or OSError('No address available')


def interface_opener(device: str, *, origin=None):
    if not device or '\0' in device:
        raise ValueError('无效的网络接口')
    class HTTPConnection(http.client.HTTPConnection):
        def __init__(self,*args,**kwargs):
            super().__init__(*args,**kwargs)
            self._create_connection=lambda address,timeout=8,source_address=None:_connect_on_interface(device,address,timeout,source_address)
    class HTTPSConnection(http.client.HTTPSConnection):
        def __init__(self,*args,**kwargs):
            super().__init__(*args,context=ssl.create_default_context(),**kwargs)
            self._create_connection=lambda address,timeout=8,source_address=None:_connect_on_interface(device,address,timeout,source_address)
    class HTTP(HTTPHandler):
        def http_open(self,request):return self.do_open(HTTPConnection,request)
    class HTTPS(HTTPSHandler):
        def https_open(self,request):return self.do_open(HTTPSConnection,request)
    class Redirect(HTTPRedirectHandler):
        def redirect_request(self,request,fp,code,msg,headers,newurl):
            if origin is not None and _origin(newurl)!=origin:
                raise OSError('认证页面跳转到其他来源，已停止提交')
            return super().redirect_request(request,fp,code,msg,headers,newurl)
    return build_opener(ProxyHandler({}),HTTP(),HTTPS(),HTTPCookieProcessor(CookieJar()),Redirect())


def authenticate_portal(profile: dict, password: str, device: str, *, opener=None) -> PortalResult:
    """Verify the inspected page before POSTing; never record submitted secrets."""
    if not supports_direct_portal(profile):
        return PortalResult(False,False,'该门户需使用网页认证')
    if not profile.get('username') or not password:
        return PortalResult(True,False,'缺少上网账号或密码，请检查校园网设置和密钥环')
    url=profile['portalUrl']
    opener=opener or interface_opener(device,origin=_origin(url))
    try:
        with opener.open(Request(url,headers={'User-Agent':'WiFlow/1.0.0'}),timeout=8) as response:
            page=response.read(262144).decode('utf-8','replace')
            if _origin(response.geturl())!=_origin(url):
                return PortalResult(True,False,'认证入口跳转到其他来源，请检查学校地址')
        if not all(marker in page for marker in ('DoLoginSubmit','ProbeSocket.php','username','password','userlogin')):
            return PortalResult(False,False,'认证页面与已支持的学校接口不匹配')
        if re.search(r'''(?:name|id)=["'][^"']*(?:captcha|otp)''', page, re.I):
            return PortalResult(False,False,'学校页面需要验证码或二次验证')
        payload=urlencode({'call_back_param':'call_back_param=account_type%3D1',
                           'usrname':profile['username'],'usrpwd':password,'method':'userlogin',
                           'account_type':1,'isLocation':0}).encode()
        u=urlsplit(url)
        origin=u.scheme+'://'+u.netloc
        request=Request(urljoin(origin,'/ProbeSocket.php'),data=payload,
                        headers={'Content-Type':'application/x-www-form-urlencoded; charset=UTF-8',
                                 'Origin':origin,'Referer':url,'X-Requested-With':'XMLHttpRequest',
                                 'User-Agent':'WiFlow/1.0.0'})
        with opener.open(request,timeout=12) as response:
            result=json.loads(response.read(65536))
        if not isinstance(result,dict) or 'code' not in result:
            return PortalResult(True,False,'学校接口未返回有效的认证结果')
        if result['code'] in (0,'0'):
            return PortalResult(True,True,'认证成功：学校接口已确认登录')
        if any(word in str(result.get('msg','')) for word in ('验证码','二次验证','动态密码')):
            return PortalResult(False,False,'学校需要交互认证')
        message=str(result.get('msg','认证未通过')).replace(password,'[密码]').replace(profile['username'],'[账号]')[:180]
        return PortalResult(True,False,'认证失败：'+message)
    except (OSError,ValueError,http.client.HTTPException):
        return PortalResult(True,False,'认证请求失败，请检查学校地址或当前网络')


def connection_key(profile: dict, link: dict, runner) -> tuple:
    try:active=runner('-g','GENERAL.CON-PATH','device','show',link['device']).strip()
    except Exception:active=''
    return (link['device'],link.get('ssid',''),link.get('profile',''),active,
            profile.get('portalUrl',''),profile.get('username',''))


def probe_interface_access(device: str, *, opener=None) -> bool:
    """Check ordinary HTTP content and reject captive-portal redirects."""
    opener=opener or interface_opener(device)
    for url,expected,marker in (('http://www.baidu.com/','www.baidu.com','baidu'),
                                ('http://neverssl.com/','neverssl.com','neverssl')):
        try:
            with opener.open(Request(url,headers={'User-Agent':'WiFlow/1.0.0'}),timeout=5) as response:
                host=(urlsplit(response.geturl()).hostname or '').lower()
                if response.status==200 and (host==expected or (expected=='neverssl.com' and host.endswith('.neverssl.com'))):
                    if marker in response.read(65536).decode('utf-8','replace').lower():return True
        except (OSError,ValueError,http.client.HTTPException):
            continue
    return False
