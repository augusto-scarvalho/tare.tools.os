"""Windows atomic-publication reader races must remain bounded and fail closed."""
import io
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from relay import audit_cases


def failure(winerror):
    error = PermissionError(13, 'file temporarily unavailable')
    if winerror is not None:
        error.winerror = winerror
    return error


@pytest.mark.parametrize('winerror', [5, 32])
def test_transient_windows_denial_reads_the_published_bytes(monkeypatch, winerror):
    attempts, sleeps = [], []
    raw = b'{"schema":"published"}'
    def open_file(path, mode):
        attempts.append((str(path), mode))
        if len(attempts) <= 2:
            raise failure(winerror)
        return io.BytesIO(raw)
    monkeypatch.setattr(Path, 'open', open_file)
    monkeypatch.setattr(audit_cases.time, 'sleep', sleeps.append)
    assert audit_cases.bounded('case.json', len(raw)) == raw
    assert len(attempts) == 3 and sleeps == [.01, .02]


@pytest.mark.parametrize('winerror,expected_attempts', [(5, 6), (32, 6), (None, 1), (123, 1)])
@pytest.mark.parametrize('platform', ['linux', 'win32'])
def test_persistent_or_unrelated_denial_is_not_hidden(monkeypatch, winerror, expected_attempts, platform):
    monkeypatch.setattr(audit_cases, 'sys', SimpleNamespace(platform=platform))
    if platform == 'win32' and winerror is None:
        expected_attempts = 6
    attempts, sleeps = [], []
    error = failure(winerror)
    def open_file(*args):
        attempts.append(True)
        raise error
    monkeypatch.setattr(Path, 'open', open_file)
    monkeypatch.setattr(audit_cases.time, 'sleep', sleeps.append)
    with pytest.raises(PermissionError) as caught:
        audit_cases.bounded('case.json')
    assert caught.value is error and len(attempts) == expected_attempts
    assert sum(sleeps) <= .31


def test_size_and_corruption_checks_survive_retry(monkeypatch):
    attempts = []
    def open_file(*args):
        attempts.append(True)
        if len(attempts) == 1:
            raise failure(32)
        return io.BytesIO(b'{"a":1,"a":2}')
    monkeypatch.setattr(Path, 'open', open_file)
    monkeypatch.setattr(audit_cases.time, 'sleep', lambda delay: None)
    with pytest.raises(ValueError, match='artifact_over_limit'):
        audit_cases.bounded('case.json', 4)
    with pytest.raises(ValueError, match='duplicate_json_key'):
        audit_cases.decode(audit_cases.bounded('case.json'))


def test_missing_case_remains_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        audit_cases.bounded(tmp_path / 'missing.json')


@pytest.mark.parametrize('transient', [False, True])
def test_windows_crt_permission_error_has_bounded_recovery(monkeypatch, transient):
    monkeypatch.setattr(audit_cases, 'sys', SimpleNamespace(platform='win32'))
    attempts, sleeps = [], []
    def open_file(*args):
        attempts.append(True)
        if transient and len(attempts) == 2:
            return io.BytesIO(b'published')
        raise failure(None)
    monkeypatch.setattr(Path, 'open', open_file)
    monkeypatch.setattr(audit_cases.time, 'sleep', sleeps.append)
    if transient:
        assert audit_cases.bounded('case.json') == b'published'
    else:
        with pytest.raises(PermissionError):
            audit_cases.bounded('case.json')
        assert len(attempts) == 6 and sum(sleeps) <= .31


@pytest.mark.skipif(sys.platform != 'win32', reason='native Windows file sharing semantics')
def test_real_windows_open_denial_recovers_after_handle_release(tmp_path):
    import ctypes
    import threading
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    kernel.CreateFileW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    path = tmp_path / 'case.json'
    path.write_bytes(b'{"state":"published"}')
    handle = kernel.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    released = threading.Event()
    def release():
        kernel.CloseHandle(handle)
        released.set()
    timer = None
    try:
        with pytest.raises(PermissionError):
            path.open('rb')
        timer = threading.Timer(.03, release)
        timer.start()
        assert audit_cases.bounded(path) == b'{"state":"published"}'
    finally:
        if timer is not None:
            timer.join()
        if not released.is_set():
            release()
