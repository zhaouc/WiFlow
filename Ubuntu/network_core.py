#!/usr/bin/env python3
"""WiFlow network utilities; deliberately independent of tkinter and GTK."""
from __future__ import annotations
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from urllib.parse import urlparse

APP_NAME = "WiFlow"
AUTO_CHECK_INTERVAL_MS = 5 * 60 * 1000
XDG_CONFIG = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config'))
CONFIG_PATH = XDG_CONFIG / 'wiflow' / 'settings.json'
LEGACY_CONFIG_PATH = XDG_CONFIG / 'wifi-assistant' / 'settings.json'
AUTOSTART = Path.home() / '.config' / 'autostart' / 'wiflow.desktop'
def run_nmcli(*args: str) -> str:
    """Never invoke a shell: SSIDs are passed as literal arguments."""
    if not shutil.which("nmcli"):
        raise RuntimeError("需要安装 NetworkManager（nmcli）")
    # NetworkManager state strings are localized; parse a stable English state.
    result = subprocess.run(["nmcli", *args], capture_output=True, text=True,
                            env={**os.environ, "LC_ALL": "C"},
                            timeout=90 if "up" in args or "connect" in args else 20, check=False)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or "NetworkManager 操作失败").strip())
    return result.stdout



def connectivity_state(runner=None) -> str:
    """Recheck when permitted; fall back to NetworkManager's cached state."""
    runner = runner or run_nmcli
    for args in (('networking', 'connectivity', 'check'),
                 ('networking', 'connectivity')):
        try:
            state = runner(*args).strip().lower()
            if state in ('full', 'portal', 'limited', 'none', 'unknown'):
                return state
        except (RuntimeError, OSError, subprocess.TimeoutExpired):
            continue
    return 'unknown'


def split_nmcli(line: str) -> list[str]:
    """nmcli -t uses backslash to escape colons/backslashes in SSIDs."""
    pieces, value, escaping = [], [], False
    for character in line:
        if escaping:
            value.append(character)
            escaping = False
        elif character == "\\":
            escaping = True
        elif character == ":":
            pieces.append("".join(value))
            value = []
        else:
            value.append(character)
    if escaping:
        value.append("\\")
    pieces.append("".join(value))
    return pieces


def parse_networks(text: str) -> list[dict]:
    seen = {}
    for line in text.splitlines():
        parts = split_nmcli(line)
        if len(parts) < 4:
            continue
        connected, ssid, signal, security = parts[:4]
        if not ssid.strip():
            continue
        try:
            strength = max(0, min(100, int(signal)))
        except ValueError:
            strength = 0
        network = {"ssid": ssid, "signal": strength, "secured": bool(security.strip() and security.strip() != "--"), "connected": connected.strip() == "*"}
        if ssid not in seen or strength > seen[ssid]["signal"]:
            seen[ssid] = network
    return sorted(seen.values(), key=lambda n: (not n["connected"], -n["signal"], n["ssid"].casefold()))


def probe_online() -> bool:
    """Only used when NetworkManager cannot determine captive portal state.

    Checks Ubuntu's standard connectivity URL, never sends credentials.
    """
    from urllib.request import Request, urlopen
    try:
        with urlopen(Request('http://connectivity-check.ubuntu.com/',
                             headers={'User-Agent': 'WiFlow/1.0.0'}), timeout=5) as response:
            final = urlparse(response.geturl())
            if (final.hostname != 'connectivity-check.ubuntu.com' or
                    final.scheme not in ('http', 'https') or final.path not in ('', '/')):
                return False
            if response.status == 204:
                return True
            return response.status == 200 and 'NetworkManager is online' in response.read(256).decode('utf-8', 'replace')
    except (OSError, ValueError):
        return False


def needs_reconnect(mode: str, ssid: str, active_connections: str) -> bool:
    """Only reconnect if NetworkManager has no active connection of the chosen type.

    Do not displace a different active Wi-Fi network automatically; the Windows
    edition follows the same non-disruptive policy.
    """
    active_types = set()
    for line in active_connections.splitlines():
        fields = split_nmcli(line)
        if len(fields) >= 2:
            active_types.add(fields[0].strip().lower())
    if mode == "wifi":
        return bool(ssid) and not bool(active_types & {"wifi", "802-11-wireless"})
    return not bool(active_types & {"ethernet", "802-3-ethernet"})



def current_networks(runner) -> dict[str, dict]:
    """Return independently active Wi-Fi and Ethernet interfaces.

    A device can be connected under an arbitrary NetworkManager profile name;
    the saved Wi-Fi SSID must be resolved from the active connection UUID.
    """
    devices = runner('-t', '-f', 'DEVICE,TYPE,STATE,CONNECTION', 'device', 'status')
    links = {}
    for line in devices.splitlines():
        fields = split_nmcli(line)
        if len(fields) < 4:
            continue
        device, kind, state, profile = [f.strip() for f in fields[:4]]
        if not state.lower().startswith('connected'):
            continue
        mode = ('wifi' if kind.lower() in ('wifi', '802-11-wireless') else
                'wired' if kind.lower() in ('ethernet', '802-3-ethernet', 'ppp', 'pppoe') else '')
        if not mode or (mode in links and kind.lower() not in ('ppp','pppoe')):
            continue
        ssid = ''
        if mode == 'wifi':
            try:
                active = runner('-t', '-f', 'UUID,TYPE,DEVICE', 'connection', 'show', '--active')
                for item in active.splitlines():
                    parts = split_nmcli(item)
                    if (len(parts) >= 3 and parts[2] == device and
                            parts[1].lower() in ('wifi', '802-11-wireless') and parts[0]):
                        value = runner('-g', '802-11-wireless.ssid', 'connection',
                                       'show', 'uuid', parts[0]).strip()
                        ssid = value if value not in ('', '--') else ''
                        break
            except (RuntimeError, OSError, subprocess.TimeoutExpired):
                pass
        links[mode] = {'mode': mode, 'device': device, 'profile': profile, 'ssid': ssid}
    return links


def current_network(runner) -> dict | None:
    """Legacy single-link display preference; authentication uses both links."""
    links = current_networks(runner)
    return links.get('wifi') or links.get('wired')


def default_route_device() -> str:
    """Which interface would the portal browser actually use?"""
    try:
        proc = subprocess.run(['ip', '-4', 'route', 'get', '1.1.1.1'],
                              capture_output=True, text=True, timeout=5, check=False)
        if proc.returncode == 0:
            fields = proc.stdout.split()
            if 'dev' in fields and fields.index('dev') + 1 < len(fields):
                return fields[fields.index('dev') + 1]
    except (OSError, subprocess.TimeoutExpired):
        pass
    return ''


def active_login_mode(links: dict, route_device: str) -> str | None:
    """Only authenticate the interface used for outbound traffic.

    With two live links, refuse to guess if the default route is unknown.
    This prevents a campus login being submitted over an unrelated network.
    """
    if len(links) == 1:
        return next(iter(links))
    for mode, link in links.items():
        if route_device and link['device'] == route_device:
            return mode
    return None


def matching_campus_link(mode: str, profile: dict, link: dict | None) -> bool:
    if not link or link.get('mode') != mode:
        return False
    if mode == 'wired':
        selected = profile.get('ethernetName', '').strip()
        return not selected or selected in (link.get('device'), link.get('profile'))
    return bool(profile.get('ssid') and link.get('ssid') == profile['ssid'])


def portal_check_needed(state: str, *, online_probe=None) -> bool:
    """NetworkManager sometimes reports `none` for captive networks.

    Never auto-submit on a connection reported as fully online. On an
    indeterminate connection, use the no-credential connectivity probe.
    """
    state = state.strip().lower()
    if state in ('portal', 'limited'):
        return True
    if state in ('none', 'unknown'):
        return not online_probe() if online_probe else True
    if state == 'full':
        # A stale/misconfigured NetworkManager connectivity result can report
        # full even when the captive portal intercepts unrelated HTTP. Check
        # with a credential-free independent probe before skipping authentication.
        return not online_probe() if online_probe else False
    return False


def load_settings(path: Path = CONFIG_PATH) -> dict:
    defaults = {"ssid": "", "portal_url": "", "username": "", "auto_connect": False, "auto_auth": False, "startup": False, "connection": "wifi"}
    try:
        source = LEGACY_CONFIG_PATH if path == CONFIG_PATH and not path.is_file() and LEGACY_CONFIG_PATH.is_file() else path
        existing = json.loads(source.read_text(encoding="utf-8"))
        if isinstance(existing, dict):
            return {**defaults, **{k: existing[k] for k in defaults if k in existing}}
    except (FileNotFoundError, ValueError, OSError):
        pass
    return defaults


def save_settings(data: dict, path: Path = CONFIG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Never persist Wi-Fi credentials or portal passwords in plaintext.
    allowed = ("ssid", "portal_url", "username", "auto_connect", "auto_auth", "startup", "connection")
    tmp = path.with_name(path.name + ".tmp")
    try:
        with os.fdopen(os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600), "w", encoding="utf-8") as f:
            json.dump({k: data[k] for k in allowed if k in data}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        tmp.unlink(missing_ok=True)



def valid_portal(url: str) -> bool:
    if not url:
        return True
    try:
        parsed = urlparse(url)
        return parsed.scheme in ("http", "https") and bool(parsed.hostname) and not parsed.username and not parsed.password
    except ValueError:
        return False



def print_diagnostics() -> None:
    """Simple support report. Never include passwords, usernames or SSIDs."""
    print('WiFlow Ubuntu 诊断（不包含账号密码）')
    print('DISPLAY:', 'available' if os.environ.get('DISPLAY') else 'missing')
    print('WAYLAND_DISPLAY:', 'available' if os.environ.get('WAYLAND_DISPLAY') else 'missing')
    print('DBUS_SESSION_BUS_ADDRESS:', 'available' if os.environ.get('DBUS_SESSION_BUS_ADDRESS') else 'missing')
    print('nmcli:', shutil.which('nmcli') or 'missing')
    print('secret-tool:', shutil.which('secret-tool') or 'missing')
    checks = [
        (['nmcli', '-g', 'STATE', 'general'], 'NetworkManager state'),
        (['nmcli', 'networking', 'connectivity'], 'NetworkManager connectivity'),
        (['/usr/bin/python3', '-c',
          "import gi;gi.require_version('Gtk','3.0');gi.require_version('WebKit2','4.1');from gi.repository import Gtk,WebKit2;print('available')"],
         'WebKitGTK Python bindings'),
    ]
    for command, title in checks:
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=8, check=False)
            detail = (result.stdout or result.stderr or '').strip().splitlines()
            print(title + ':', ('; '.join(detail[:2]))[:200] if detail else 'exit ' + str(result.returncode))
        except (OSError, subprocess.TimeoutExpired) as e:
            print(title + ':', type(e).__name__)
    for name in ('startup.log', 'portal.log'):
        path = Path.home() / '.cache' / 'wiflow' / name
        print(name + ':', 'exists' if path.exists() else 'not found')

