"""Explicit pilot window, plus serialization of new-system build processes."""
import contextlib
import fcntl
import json
import os
import signal
import subprocess
import time
from pathlib import Path

from .core import SafeError, atomic_json


def bitrise_processes():
    result = subprocess.run(['/bin/ps', '-axo', 'pid=,comm='], capture_output=True, text=True, check=True)
    matches = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 1)
        executable = Path(parts[1]).name if len(parts) == 2 else ''
        if any(s in line.lower() for s in ('bitrise-den-agent', 'bitrise-agent')) or executable == 'bitrise':
            matches.append(line.strip())
    return matches


def check_window(root):
    try:
        window = json.loads((Path(root) / 'data' / 'pilot-window.json').read_text())
        if window['expires_at'] <= time.time():
            raise ValueError()
    except (OSError, ValueError, KeyError):
        raise SafeError('No active pilot window. Drain and temporarily stop Bitrise, then run ./bento open-window.') from None
    if bitrise_processes():
        raise SafeError('Bitrise is running. Drain and stop its agent for the pilot window; its installation is preserved.')
    return window


def open_window(root, minutes):
    if not 1 <= minutes <= 480:
        raise SafeError('Choose a pilot window between 1 and 480 minutes.')
    if bitrise_processes():
        raise SafeError('Bitrise is still running. Drain it and stop its agent before opening a window.')
    atomic_json(Path(root) / 'data' / 'pilot-window.json', {'expires_at': time.time() + minutes * 60})


@contextlib.contextmanager
def host_lock(root):
    path = Path(root) / 'data' / 'host.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as handle:
        os.chmod(path, 0o600)
        print('Waiting for the Mac build slot…', flush=True)
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(1)
        try:
            check_window(root)
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
