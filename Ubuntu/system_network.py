"""Ubuntu equivalents for the Windows system-network and client actions."""
from pathlib import Path
import os
import shutil
import subprocess
from network_core import split_nmcli


def saved_connections(runner):
    entries=[]
    for row in runner('-t','-f','NAME,UUID,TYPE','connection','show').splitlines():
        fields=split_nmcli(row)
        if len(fields)>=3:entries.append(tuple(fields[:3]))
    return entries


def network_choices(runner):
    adapters=[]
    for row in runner('-t','-f','DEVICE,TYPE','device','status').splitlines():
        fields=split_nmcli(row)
        if len(fields)>=2 and fields[1] in ('ethernet','802-3-ethernet'):adapters.append(fields[0])
    broadband=[name for name,_,kind in saved_connections(runner) if kind=='pppoe']
    return adapters,broadband


def connect_wired(profile,runner):
    method=profile.get('wiredAuth','web')
    if method=='client':return launch_client(profile.get('authenticationClient',''))
    entries=saved_connections(runner)
    if method=='pppoe':
        selected=profile.get('broadbandName','')
        match=next(((name,uuid) for name,uuid,kind in entries if kind=='pppoe' and name==selected),None)
        if not match:raise RuntimeError('未找到已保存的 PPPoE 连接，请先在系统网络设置中创建并保存账号密码')
        runner('connection','up','uuid',match[1])
        return 'PPPoE 连接已建立'
    selected=profile.get('ethernetName','')
    matches=[(name,uuid) for name,uuid,kind in entries if kind in ('ethernet','802-3-ethernet')]
    match=next((entry for entry in matches if entry[0]==selected),None) if selected else next(iter(matches),None)
    if match:
        runner('connection','up','uuid',match[1])
    else:
        adapters,_=network_choices(runner)
        if selected not in adapters:raise RuntimeError('未找到指定有线连接或接口，请检查系统网络设置')
        runner('device','connect',selected)
    return '系统有线认证已启动' if method=='dot1x' else '有线网络已连接'


def launch_client(filename):
    path=Path(filename)
    if not path.is_absolute() or not path.is_file() or not os.access(path,os.X_OK) or path.suffix.lower()=='.exe':
        raise RuntimeError('请选择学校提供的 Ubuntu 可执行客户端')
    target=str(path.resolve()).encode()
    for directory in Path('/proc').iterdir():
        if not directory.name.isdigit():continue
        try:
            if directory.stat().st_uid!=os.getuid():continue
            if target in directory.joinpath('cmdline').read_bytes().split(b'\0'):
                return '学校认证客户端已在运行，请在客户端中完成登录'
        except OSError:pass
    subprocess.Popen([str(path)],cwd=str(path.parent),stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
    return '学校认证客户端已启动，请在客户端中完成登录'


def open_network_settings(action):
    if not shutil.which('gnome-control-center'):
        raise RuntimeError('未找到 Ubuntu 系统设置（gnome-control-center）')
    command=['gnome-control-center','wifi' if action in ('wifiSettings','location') else 'network']
    subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
