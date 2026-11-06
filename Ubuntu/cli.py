#!/usr/bin/env python3
"""Desktop dispatcher and display-independent WiFlow terminal commands."""
from __future__ import annotations
import argparse
import getpass
import json
import os
import signal
from pathlib import Path
import shutil
import subprocess
import sys

from credentials import connect_wifi, get_secret, put_secret
from portal_client import supports_direct_portal, authenticate_portal, probe_interface_access
from network_core import (connectivity_state, current_networks, default_route_device,
                          matching_campus_link, probe_online, run_nmcli, valid_portal)
from web_main import Store, portal_secret_key, wifi_secret_key, APP_VERSION


class CLIError(Exception):
    def __init__(self, message, exit_code=2):
        super().__init__(message)
        self.exit_code = exit_code


def parser():
    p = argparse.ArgumentParser(prog='wiflow', description='WiFlow 桌面与终端校园网客户端')
    p.add_argument('--version', action='version', version='WiFlow ' + APP_VERSION)
    commands = p.add_subparsers(dest='command')
    commands.add_parser('gui', help='打开桌面界面')
    commands.add_parser('diagnose', help='运行环境诊断')
    status = commands.add_parser('status', help='查看连接与系统上网状态')
    status.add_argument('--json', action='store_true', help='输出 JSON')
    config = commands.add_parser('configure', help='保存配置，与桌面版共用')
    config.add_argument('--mode', choices=('wifi', 'wired'), required=True)
    config.add_argument('--ssid', help='Wi-Fi 名称')
    config.add_argument('--portal-url', help='学校认证地址')
    config.add_argument('--username', help='校园网账号')
    config.add_argument('--ethernet-name', help='有线接口或 NetworkManager 配置名称')
    config.add_argument('--set-password', action='store_true', help='隐藏输入并保存校园网密码')
    config.add_argument('--set-wifi-password', action='store_true', help='隐藏输入并保存 Wi-Fi 密码')
    connect = commands.add_parser('connect', help='连接保存的 Wi-Fi 或有线配置')
    connect.add_argument('--mode', choices=('wifi', 'wired'))
    connect.add_argument('--password-stdin', action='store_true', help='从标准输入读取一行 Wi-Fi 密码')
    login = commands.add_parser('login', help='在终端完成网页认证，无需图形桌面')
    login.add_argument('--mode', choices=('wifi', 'wired'))
    login.add_argument('--password-stdin', action='store_true', help='从标准输入读取一行校园网密码')
    login.add_argument('--timeout', type=int, default=45, metavar='SECONDS')
    login.add_argument('--force', action='store_true', help='当前已可上网时仍发起登录')
    login.add_argument('--allow-http', action='store_true', help='允许向配置的 HTTP 门户提交密码（明文传输）')
    login.add_argument('--trust-cert', type=Path, help='仅信任指定 PEM 服务器证书，用于学校自签名证书')
    return p


def read_password(key: str, *, from_stdin=False, prompt='密码：') -> str:
    if from_stdin:
        value = sys.stdin.readline(8194)
        if not value or len(value) > 8193:
            raise CLIError('标准输入中缺少有效密码')
        return value.rstrip('\r\n')
    value = get_secret(key)
    if value:
        return value
    if not sys.stdin.isatty():
        raise CLIError('未找到可用密钥环密码；请在交互终端输入，或使用 --password-stdin')
    return getpass.getpass(prompt)


def require_link(mode, profile, *, runner=None):
    runner = runner or run_nmcli
    links = current_networks(runner)
    link = links.get(mode)
    if not link:
        raise CLIError('所选网络尚未连接；请先运行 wiflow connect --mode ' + mode, 3)
    if not matching_campus_link(mode, profile, link):
        raise CLIError('当前网络与保存的校园网配置不匹配', 3)
    if not supports_direct_portal(profile) and default_route_device() != link['device']:
        raise CLIError('所选网络不是当前默认上网接口；请先确认路由后再认证', 3)
    return link


def status_command(args):
    links = current_networks(run_nmcli)
    data = {'connections': links, 'default_device': default_route_device(),
            'system_connectivity': connectivity_state(run_nmcli)}
    if args.json:
        print(json.dumps(data, ensure_ascii=False))
    else:
        for mode, label in (('wifi', 'Wi-Fi'), ('wired', '有线')):
            link = links.get(mode)
            print(label + '：' + (('已连接 ' + (link.get('ssid') or link['profile']) + ' [' + link['device'] + ']') if link else '未连接'))
        print('默认上网接口：' + (data['default_device'] or '未知'))
        print('系统连通性：' + data['system_connectivity'] + '（不代表每个接口均已通过校园网认证）')
    return 0


def configure_command(args, store):
    profile = store.profiles[args.mode].copy()
    fields = {'ssid': args.ssid, 'portalUrl': args.portal_url,
              'username': args.username, 'ethernetName': args.ethernet_name}
    profile.update({key: value for key, value in fields.items() if value is not None})
    profile['connectionMode'] = args.mode
    if args.portal_url is not None:
        profile['portalEnabled'] = bool(args.portal_url)
    mode = store.set_profile(profile)
    profile = store.profiles[mode]
    secrets = []
    if args.set_password:
        if not profile['portalUrl'] or not profile['username']:
            raise CLIError('请先设置认证网址和账号')
        secrets.append((portal_secret_key(profile), getpass.getpass('校园网密码（隐藏输入）：')))
    if args.set_wifi_password:
        if mode != 'wifi' or not profile['ssid']:
            raise CLIError('保存 Wi-Fi 密码需要 Wi-Fi 模式和 SSID')
        secrets.append((wifi_secret_key(profile), getpass.getpass('Wi-Fi 密码（隐藏输入）：')))
    for key, value in secrets:
        if not put_secret(key, value):
            raise CLIError('密码保存失败；请确认系统密钥环已解锁。SSH 登录可直接使用隐藏输入，无需保存密码')
    store.save()
    print('已保存 ' + mode + ' 配置；桌面版重新打开后读取。')
    return 0


def connect_command(args, store):
    mode = args.mode or store.mode
    profile = store.profiles[mode]
    links = current_networks(run_nmcli)
    if matching_campus_link(mode, profile, links.get(mode)):
        print('所选网络已连接。')
        return 0
    if mode == 'wifi':
        if not profile['ssid']:
            raise CLIError('请先使用 configure 设置 Wi-Fi 名称')
        password = (read_password(wifi_secret_key(profile), from_stdin=True)
                    if args.password_stdin else get_secret(wifi_secret_key(profile)))
        try:
            connect_wifi(profile['ssid'], password, run_nmcli)
        except (RuntimeError, ValueError):
            if password or not sys.stdin.isatty():
                raise
            password = getpass.getpass('Wi-Fi 密码（开放网络留空）：')
            connect_wifi(profile['ssid'], password, run_nmcli)
    else:
        name = profile['ethernetName']
        if not name:
            raise CLIError('请先通过 configure --mode wired --ethernet-name 指定有线配置或接口')
        devices = {line.split(':', 1)[0] for line in run_nmcli('-t', '-f', 'DEVICE', 'device', 'status').splitlines()}
        if name in devices:
            run_nmcli('device', 'connect', name)
        else:
            run_nmcli('connection', 'up', 'id', name)
    print('网络连接成功；使用 wiflow login --mode ' + mode + ' 进行上网认证。')
    return 0


def login_command(args, store):
    if not 5 <= args.timeout <= 300:
        raise CLIError('--timeout 范围为 5 到 300 秒')
    mode = args.mode or store.mode
    profile = store.profiles[mode]
    url = profile['portalUrl']
    if not url or not valid_portal(url) or not profile['portalEnabled']:
        raise CLIError('请先配置并启用有效的校园网认证地址')
    if url.lower().startswith('http://') and not args.allow_http:
        raise CLIError('该门户使用 HTTP，密码将明文传输；确认学校地址后可使用 --allow-http')
    link = require_link(mode, profile)
    if not supports_direct_portal(profile) and not args.force and probe_online():
        print('当前默认网络已可访问互联网，无需重复认证。')
        return 0
    if not profile['username']:
        raise CLIError('请先通过 configure 设置校园网账号')
    password = read_password(portal_secret_key(profile), from_stdin=args.password_stdin,
                             prompt='校园网密码（隐藏输入）：')
    if not password:
        raise CLIError('校园网密码不能为空')
    if supports_direct_portal(profile):
        result = authenticate_portal(profile, password, link['device'])
        if result.supported:
            print(result.message)
            if not result.authenticated:return 5
            if probe_interface_access(link['device']):
                print('普通网页访问检查通过。')
                return 0
            print('学校已确认登录，但普通网页访问检查尚未通过。')
            return 5
        if default_route_device() != link['device']:
            raise CLIError('该门户需要网页认证，请先确认默认上网接口', 3)
    if not shutil.which('xvfb-run'):

        raise CLIError('终端认证需要 xvfb 和 xauth；请安装完整的 WiFlow 软件包')
    cert = ''
    if args.trust_cert:
        import ssl
        cert = args.trust_cert.read_text()
        ssl.PEM_cert_to_DER_cert(cert)
    config = {'url': url, 'username': profile['username'], 'password': password,
              'mode': mode, 'profile': profile, 'timeout': args.timeout,
              'allow_http': args.allow_http, 'trusted_certificate': cert}
    print('正在进行终端认证…', flush=True)
    return run_terminal_backend(config, args.timeout)


def run_terminal_backend(config, timeout):
    helper = Path(__file__).with_name('terminal_portal.py')
    proc = subprocess.Popen(['xvfb-run', '-a', '/usr/bin/python3', str(helper)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        output, _errors = proc.communicate(json.dumps(config), timeout=timeout + 20)
    except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
        raise CLIError('认证已停止，未确认上网成功', 130 if isinstance(exc, KeyboardInterrupt) else 5)
    try:
        result = json.loads(output)
        message = result['message']
        if not isinstance(message, str): raise ValueError('invalid result')
    except (ValueError, TypeError, KeyError):
        raise CLIError('认证进程未正常返回，退出码 ' + str(proc.returncode))
    print(message)
    return proc.returncode


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    terminal_only = bool(argv and argv[0] == '--terminal')
    if terminal_only:
        argv.pop(0)
    if (not argv and not terminal_only) or (argv and argv[0] in ('gui', '--background', '--diagnose', '--smoke-test')):
        from web_main import launch
        sys.argv = [sys.argv[0], *(argv[1:] if argv and argv[0] == 'gui' else argv)]
        return launch()
    p = parser()
    args = p.parse_args(argv)
    if args.command is None:
        p.print_help()
        return 0
    try:
        if args.command == 'status': return status_command(args)
        if args.command == 'diagnose':
            from network_core import print_diagnostics
            print_diagnostics()
            return 0
        store = Store()
        return {'configure': configure_command, 'connect': connect_command,
                'login': login_command}[args.command](args, store)
    except (CLIError, OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        print('错误：' + str(exc), file=sys.stderr)
        return getattr(exc, 'exit_code', 2)
    except (EOFError, KeyboardInterrupt):
        print('已取消。', file=sys.stderr)
        return 130


if __name__ == '__main__':
    sys.exit(main())
