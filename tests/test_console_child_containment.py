"""The TUI's turn and Work children end with the TUI process (Windows kill-on-close Job)."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from relay import console_backend

ROOT = Path(__file__).resolve().parents[1]
windows_only = pytest.mark.skipif(os.name != 'nt', reason='Windows Job Objects')

# The owner plays the TUI: it launches a child exactly as Backend.launch does and, when asked, contains it.
OWNER = '''
import asyncio, json, subprocess, sys
from relay.console_backend import Backend

mode, kind, out = sys.argv[1:4]
SLEEP = 'import time; time.sleep(600)'
NESTED = """
import json, subprocess, sys
from compute_plane.managed_process import WindowsJob
job = WindowsJob()
grandchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])
if not job.k.AssignProcessToJobObject(job.handle, int(grandchild._handle)):
    raise job.c.WinError(job.c.get_last_error())
open(sys.argv[1], 'w').write(json.dumps({'ok': 'OK', 'grandchild': grandchild.pid}))
import time; time.sleep(600)
"""

async def main():
    backend = Backend()
    code, extra = (SLEEP, []) if kind == 'sleep' else (NESTED, [out + '.nested'])
    process = await asyncio.create_subprocess_exec(
        sys.executable, '-c', code, *extra, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    if mode == 'contain':
        backend.contain(process)
    open(out, 'w').write(json.dumps({'child': process.pid, 'containment': getattr(backend, 'containment', None)}))
    await asyncio.sleep(600)

asyncio.run(main())
'''


def _identity(pid):
    import psutil
    try:
        return pid, psutil.Process(pid).create_time()
    except psutil.NoSuchProcess:
        return None


def _alive(identity):
    """True while the very process first seen is running (a reused pid has another creation time)."""
    import psutil
    pid, created = identity
    try:
        process = psutil.Process(pid)
        return process.create_time() == created and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _wait_file(path, owner, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.read_text():
            try:
                return json.loads(path.read_text())
            except ValueError:
                pass  # half-written
        assert owner.poll() is None, 'the owner exited early'
        time.sleep(0.05)
    raise AssertionError(f'{path.name} never appeared')


def _wait_gone(identities, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(_alive(i) for i in identities):
            return True
        time.sleep(0.1)
    return False


def _run_owner(tmp_path, mode, kind):
    """Start the owner, wait until its child is up, kill ONLY the owner; return (identities, cleanup list)."""
    script = tmp_path / 'owner.py'
    script.write_text(OWNER)
    out = tmp_path / f'{mode}-{kind}.json'
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in [str(ROOT), os.environ.get('PYTHONPATH', '')] if p))
    owner = subprocess.Popen([sys.executable, str(script), mode, kind, str(out)], env=env, cwd=str(tmp_path),
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return owner, out


def _kill_pids(identities):
    import psutil
    for identity in identities:
        if identity and _alive(identity):
            try:
                psutil.Process(identity[0]).kill()
            except psutil.NoSuchProcess:
                pass


@windows_only
def test_killing_the_owner_ends_a_contained_child(tmp_path):
    owner, out = _run_owner(tmp_path, 'contain', 'sleep')
    seen = []
    try:
        info = _wait_file(out, owner)
        assert info['containment'] == 'job'
        child = _identity(info['child']); seen.append(child)
        assert _alive(child)
        owner.kill()  # TerminateProcess on the owner only, no tree kill
        owner.wait(10)
        assert _wait_gone([child]), 'the contained child outlived its owner'
    finally:
        owner.kill()
        _kill_pids(seen)


@windows_only
def test_without_containment_the_child_outlives_the_owner(tmp_path):
    owner, out = _run_owner(tmp_path, 'plain', 'sleep')
    seen = []
    try:
        info = _wait_file(out, owner)
        child = _identity(info['child']); seen.append(child)
        owner.kill()
        owner.wait(10)
        time.sleep(3)
        assert _alive(child), 'the control child died on its own, so the test above proves nothing'
    finally:
        owner.kill()
        _kill_pids(seen)


@windows_only
def test_a_contained_child_can_still_contain_its_own_grandchild(tmp_path):
    owner, out = _run_owner(tmp_path, 'contain', 'nested')
    seen = []
    try:
        info = _wait_file(out, owner)
        child = _identity(info['child']); seen.append(child)
        nested = _wait_file(Path(str(out) + '.nested'), owner)
        assert nested['ok'] == 'OK'
        grandchild = _identity(nested['grandchild']); seen.append(grandchild)
        assert _alive(child) and _alive(grandchild)
        owner.kill()
        owner.wait(10)
        assert _wait_gone([child, grandchild]), 'the child or its grandchild outlived the owner'
    finally:
        owner.kill()
        _kill_pids(seen)


def test_launch_contains_the_child_it_creates(tmp_path, monkeypatch):
    process = SimpleNamespace()
    async def create(*args, **kwargs):
        return process
    contained = []
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', create)
    monkeypatch.setattr(console_backend.Backend, 'contain', lambda self, child: contained.append(child))
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path / 'state'))
    assert asyncio.run(console_backend.Backend().launch(['status'], str(tmp_path / 'project'))) is process
    assert contained == [process]


def test_a_containment_failure_does_not_stop_the_turn(tmp_path, monkeypatch):
    process = SimpleNamespace()  # has no asyncio transport, so assigning it fails on Windows
    async def create(*args, **kwargs):
        return process
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', create)
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path / 'state'))
    backend = console_backend.Backend()
    assert asyncio.run(backend.launch(['status'], str(tmp_path / 'project'))) is process
    if os.name == 'nt':
        assert backend.containment.startswith('unavailable: ')


@windows_only
def test_an_unimportable_kernel_is_reported_not_raised(monkeypatch):
    import compute_plane.managed_process as managed
    def broken(*args, **kwargs):
        raise OSError('no job objects here')
    monkeypatch.setattr(managed, 'WindowsJob', broken)
    backend = console_backend.Backend()
    backend.contain(SimpleNamespace())
    assert backend.containment == 'unavailable: no job objects here'
