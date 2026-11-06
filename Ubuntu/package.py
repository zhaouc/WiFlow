#!/usr/bin/env python3
"""Build WiFlow Ubuntu Desktop packages for amd64 and arm64."""
from __future__ import annotations
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
VERSION = '1.0.0'
PKG = 'wiflow'
PYTHON_FILES = ('network_core.py', 'web_main.py', 'credentials.py', 'instance.py', 'portal_window.py', 'cli.py', 'terminal_portal.py', 'portal_client.py', 'embedded_portal.py', 'system_network.py')
DESKTOP = '[Desktop Entry]\nType=Application\nName=WiFlow\nExec=/usr/bin/wiflow gui\nTryExec=/usr/bin/wiflow\nIcon=wiflow\nTerminal=false\nCategories=Network;Utility;\nStartupNotify=true\n'
ICON = '<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128" viewBox="0 0 128 128"><rect width="128" height="128" rx="28" fill="#2563eb"/><path d="M25 53q39-35 78 0M41 70q23-21 46 0M55 88q9-9 18 0" fill="none" stroke="white" stroke-width="10" stroke-linecap="round"/><circle cx="64" cy="103" r="6" fill="white"/></svg>'

def build(arch: str) -> Path:
    if arch not in ('amd64', 'arm64'):
        raise ValueError('Unsupported CPU architecture: ' + arch)
    for name in (*PYTHON_FILES, 'Desktop.html'):
        if not (ROOT / name).is_file():
            raise FileNotFoundError(name)
    outfile = ROOT / 'dist' / f'{PKG}_{VERSION}_{arch}.deb'
    with tempfile.TemporaryDirectory(prefix='wiflow-build-') as temp:
        stage = Path(temp)
        stage.chmod(0o755)
        for folder in ('DEBIAN', 'usr/bin', 'usr/share/applications',
                       'usr/share/icons/hicolor/scalable/apps', 'usr/share/wiflow', 'usr/share/doc/wiflow'):
            (stage / folder).mkdir(parents=True, exist_ok=True)
        for name in (*PYTHON_FILES, 'Desktop.html'):
            shutil.copy2(ROOT / name, stage / 'usr/share/wiflow' / name)
        shutil.copy2(ROOT / 'README-CLI.md', stage / 'usr/share/doc/wiflow/README-CLI.md')
        entry = stage / 'usr/bin/wiflow'
        entry.write_text('#!/bin/sh\nexec /usr/bin/python3 /usr/share/wiflow/cli.py "$@"\n', encoding='utf-8')
        entry.chmod(0o755)
        terminal_entry = stage / 'usr/bin/wiflow-cli'
        terminal_entry.write_text('#!/bin/sh\nexec /usr/bin/python3 /usr/share/wiflow/cli.py --terminal "$@"\n', encoding='utf-8')
        terminal_entry.chmod(0o755)
        (stage / 'usr/share/applications/wiflow.desktop').write_text(DESKTOP, encoding='utf-8')
        (stage / 'usr/share/icons/hicolor/scalable/apps/wiflow.svg').write_text(ICON, encoding='utf-8')
        (stage / 'DEBIAN/control').write_text(
            f'Package: {PKG}\nVersion: {VERSION}\nSection: net\nPriority: optional\n'
            f'Architecture: {arch}\n'
            'Depends: python3, network-manager, python3-gi, gir1.2-gtk-3.0, gir1.2-webkit2-4.1, libsecret-tools, xvfb, xauth, gir1.2-ayatanaappindicator3-0.1\n'
            'Conflicts: wifi-assistant\nReplaces: wifi-assistant\nBreaks: wifi-assistant\n'
            'Maintainer: WiFlow project\n'
            'Description: WiFlow Wi-Fi and campus portal client with WebKitGTK interface\n',
            encoding='utf-8')
        outfile.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(['dpkg-deb', '--build', '--root-owner-group', str(stage), str(outfile)], check=True)
    return outfile

if __name__ == '__main__':
    selected = sys.argv[1:] or ['all']
    arches = ('amd64', 'arm64') if selected == ['all'] else selected
    for arch in arches:
        print(build(arch))
