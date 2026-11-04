import asyncio
import inspect
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from relay import native_console, turn_engine
from relay.console_backend import Selection


@pytest.mark.parametrize('cancel', [False, True])
def test_preparation_failure_or_cancel_never_launches_and_releases_ui(tmp_path, monkeypatch, cancel):
    selection = Selection(root=str(tmp_path), mode='session', session=str(tmp_path/'reserved'))
    messages, controls, launches, checkpoints = [], [], [], []
    async def launch(*args):
        launches.append(args)
        raise AssertionError('must not launch')
    async def refresh(*args, **kwargs):
        pass
    output = SimpleNamespace(finish_stream=lambda: None)
    app = SimpleNamespace(process=None, cancel_requested=False, assistant_session=selection.session,
        last_result=None, close_after_run=False, backend=SimpleNamespace(launch=launch),
        say=messages.append, set_status=messages.append, controls=controls.append,
        query_one=lambda *args: output, finish_activity=lambda: None,
        refresh_messages=refresh, refresh_session=refresh, selection=lambda: selection,
        checkpoint=lambda kind, detail, **kwargs: checkpoints.append((kind, detail)))
    def snapshot(root, *, cancelled):
        if cancel:
            app.cancel_requested = True
            assert cancelled()
            return None, 'snapshot cancelled'
        raise OSError('preparation fixture failure')
    monkeypatch.setattr(native_console, 'session_has_terminal', lambda selection: True)
    monkeypatch.setattr(turn_engine, 'snapshot', snapshot)
    asyncio.run(inspect.unwrap(native_console.WorkConsole.run_command)(app, [], selection))
    assert not launches and app.process is None
    assert controls[-1] is False
    assert app.assistant_session is None and selection.session == ''
    assert any(('before launch' if cancel else 'preparation fixture failure') in text for text in messages)
    assert not (tmp_path/'reserved').exists()
    assert checkpoints[-1][0] == 'execution_observed'
    assert checkpoints[-1][1]['status'] == ('CANCELLED' if cancel else 'BLOCKED')
    assert app.last_result['model_calls'] == 0
    assert app.last_result['execution'] == 'agent_not_started'


def test_snapshot_deadline_is_shared_across_git_commands(tmp_path, monkeypatch):
    clock = [0.0]
    seen = []
    monkeypatch.setattr(turn_engine.time, 'monotonic', lambda: clock[0])
    def shadow(root, *args, deadline, cancelled):
        seen.append((args[0], deadline))
        clock[0] += 2
        return b''
    monkeypatch.setattr(turn_engine, 'shadow', shadow)
    assert turn_engine.snapshot(tmp_path) == (None, 'snapshot deadline exceeded')
    assert [name for name, _ in seen] == ['ls-files', 'add', 'write-tree']
    assert {deadline for _, deadline in seen} == {5.0}


def test_snapshot_cancelled_before_git(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('cancelled snapshot ran Git')
    monkeypatch.setattr(turn_engine, 'shadow', forbidden)
    assert turn_engine.snapshot(tmp_path, cancelled=lambda: True) == (None, 'snapshot cancelled')


def test_failed_observation_keeps_ui_busy_until_the_child_is_reaped(tmp_path, monkeypatch):
    selection = Selection(root=str(tmp_path), mode='session', session=str(tmp_path/'reserved'))
    controls, cancelled = [], []
    class Process:
        returncode = None
        async def wait(self):
            assert cancelled and False not in controls
            self.returncode = 1
    process = Process()
    async def launch(*args):
        return process
    async def failed_observation(*args):
        raise OSError('lost child observation')
    async def refresh(*args, **kwargs):
        assert process.returncode == 1
    output = SimpleNamespace(finish_stream=lambda: None, live=None, tool_rows={})
    app = SimpleNamespace(process=None, cancel_requested=False, assistant_session=selection.session,
        last_result=None, close_after_run=False,
        backend=SimpleNamespace(launch=launch, cancel=cancelled.append),
        say=lambda *a: None, set_status=lambda *a: None, controls=controls.append,
        query_one=lambda *args: output, finish_activity=lambda: None,
        refresh_messages=refresh, refresh_session=refresh, selection=lambda: selection,
        checkpoint=lambda *args, **kwargs: None, assistant_activity=lambda *a: None)
    monkeypatch.setattr(native_console, 'session_has_terminal', lambda selected: False)
    monkeypatch.setattr(native_console, 'observe_process', failed_observation)
    asyncio.run(inspect.unwrap(native_console.WorkConsole.run_command)(app, [], selection))
    assert controls and controls[-1] is False and process.returncode == 1


@pytest.mark.parametrize('cancel', [False, True])
def test_shadow_subprocess_is_reaped_on_deadline_or_cancel(tmp_path, monkeypatch, cancel):
    processes = []
    popen = subprocess.Popen
    def record(*args, **kwargs):
        process = popen(*args, **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(turn_engine.subprocess, 'Popen', record)
    started = time.monotonic()
    cancelled = (lambda: time.monotonic()-started > .1) if cancel else None
    with pytest.raises(ValueError, match='snapshot cancelled' if cancel else 'snapshot deadline exceeded'):
        turn_engine._shadow_run([sys.executable, '-c', 'import time; time.sleep(30)'],
                               tmp_path, started + (.8 if cancel else .1), cancelled)
    assert len(processes) == 1 and processes[0].poll() is not None


def test_shadow_deadline_closes_descendants_that_keep_output_open(tmp_path):
    """Killing only the launcher used to hang forever draining its child's pipe."""
    pid_file = tmp_path/'descendant.pid'
    child = 'import os,time; from pathlib import Path; Path('+repr(str(pid_file))+').write_text(str(os.getpid())); time.sleep(30)'
    parent = 'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",'+repr(child)+']); time.sleep(30)'
    handle = None
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]

    started = time.monotonic()
    def observe_descendant():
        nonlocal handle
        if os.name == 'nt' and handle is None and time.monotonic() < started+2 and pid_file.exists():
            text = pid_file.read_text()
            if text:
                pid = int(text)
                handle = kernel.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE
                assert handle, f'OpenProcess({pid}) failed: {ctypes.get_last_error()}'
                state = kernel.WaitForSingleObject(handle, 0)
                assert state == 258, f'Descendant {pid} was not alive before cleanup: {state}'
                assert time.monotonic() < started+2, 'Handle must be acquired before the deadline'
        return False

    try:
        with pytest.raises(ValueError, match='snapshot deadline exceeded'):
            turn_engine._shadow_run([sys.executable, '-c', parent], tmp_path, started+2,
                                   observe_descendant if os.name == 'nt' else None)
        if os.name == 'nt':
            assert handle is not None, 'The live descendant must be captured before cleanup'
            state = kernel.WaitForSingleObject(handle, 0)
            assert state == 0, f'Descendant handle not signaled immediately after cleanup: {state}'
        assert pid_file.exists(), 'The descendant must actually have started'
        assert time.monotonic()-started < 7
        pid = int(pid_file.read_text())
        if os.name != 'nt':
            proc_stat = Path('/proc')/str(pid)/'stat'
            if proc_stat.exists():
                assert proc_stat.read_text().rsplit(')', 1)[1].split()[0] == 'Z'
        assert time.monotonic()-started < 3
    finally:
        if handle:
            kernel.CloseHandle(handle)


def test_snapshot_uses_private_index_and_removes_its_lock(tmp_path, monkeypatch):
    indexes = []
    def shadow(root, *args, deadline, cancelled):
        index = Path(turn_engine._SNAPSHOT_INDEX.get())
        indexes.append(index)
        index.with_suffix('.lock').touch()
        raise ValueError('snapshot cancelled')
    monkeypatch.setattr(turn_engine, 'shadow', shadow)
    for _ in range(2):
        assert turn_engine.snapshot(tmp_path) == (None, 'snapshot cancelled')
    assert indexes[0] != indexes[1]
    assert all(not index.parent.exists() for index in indexes)
