"""Per-user desktop single instance with wake/restore via local Unix socket."""
from __future__ import annotations

import os
import fcntl
import time
from pathlib import Path
import queue
import socket
import threading


class SingleInstance:
    def __init__(self, name: str, on_wake):
        self.on_wake = on_wake
        self.requests: queue.SimpleQueue[str] = queue.SimpleQueue()
        runtime = Path(os.environ.get('XDG_RUNTIME_DIR') or '/nonexistent')
        directory = runtime if runtime.is_dir() and os.access(runtime, os.W_OK) else (Path.home() / '.cache' / 'wiflow')
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / (name + '.sock')
        self.sock = None
        self.lock_fd = os.open(str(self.path) + '.lock', os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # The other process owns the lock even when its socket has not yet
            # been created. Never unlink another live instance's socket.
            os.close(self.lock_fd)
            self.lock_fd = None
            for _ in range(15):
                if signal_existing(name):
                    break
                time.sleep(0.1)
            return
        try:
            self._open()
        except Exception:
            self.close()
            raise

    def _open(self):
        # flock serializes owners. An existing socket now belongs to a dead
        # process, never another running instance, so it can be removed safely.
        self.path.unlink(missing_ok=True)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(str(self.path))
            os.chmod(self.path, 0o600)
            sock.listen(3)
            sock.settimeout(0.5)
            self.sock = sock
            threading.Thread(target=self._serve, daemon=True).start()
        except Exception:
            sock.close()
            raise

    @property
    def primary(self) -> bool:
        return self.sock is not None

    def _serve(self):
        while self.sock:
            try:
                connection, _ = self.sock.accept()
                with connection:
                    if connection.recv(64).startswith(b'WAKE'):
                        self.requests.put('WAKE')
            except socket.timeout:
                continue
            except OSError:
                break

    def poll(self):
        while not self.requests.empty():
            self.requests.get_nowait()
            self.on_wake()

    def close(self):
        if self.sock is not None:
            self.sock.close()
            self.sock = None
            self.path.unlink(missing_ok=True)
        if self.lock_fd is not None:
            fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
            os.close(self.lock_fd)
            self.lock_fd = None


def signal_existing(name: str) -> bool:
    runtime = Path(os.environ.get('XDG_RUNTIME_DIR') or '/nonexistent')
    directory = runtime if runtime.is_dir() and os.access(runtime, os.W_OK) else (Path.home() / '.cache' / 'wiflow')
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(1)
            client.connect(str(directory / (name + '.sock')))
            client.sendall(b'WAKE\n')
        return True
    except OSError:
        return False
