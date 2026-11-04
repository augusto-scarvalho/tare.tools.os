"""Importing the coordinator or Work entrypoints must not open the coordinator DB.

The process-default ``cluster_coordinator.DB`` initializes lazily, exactly once
under concurrency, and retries after a failed initialization. Explicit
``CoordinatorDB(path)`` construction is unchanged.
"""
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from relay import cluster_coordinator as cc  # noqa: E402

PROBE = textwrap.dedent('''
    import importlib, sqlite3, sys
    calls = []
    real = sqlite3.connect
    def guard(*a, **k):
        calls.append(repr(a[:1]))
        return real(":memory:")
    sqlite3.connect = guard
    sys.path.insert(0, sys.argv[1])
    for name in sys.argv[2:]:
        importlib.import_module(name)
    print("CONNECTS=%d %s" % (len(calls), calls))
''')


@pytest.mark.parametrize('modules', [
    ['relay.cluster_coordinator'],
    ['relay.work_run', 'relay.work_resume', 'relay.native_work_host', 'relay.cluster_coordinator'],
])
def test_clean_process_import_opens_no_sqlite_connection(modules):
    proc = subprocess.run([sys.executable, '-c', PROBE, str(REPO_ROOT), *modules],
                          cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stderr[-4000:]
    assert 'CONNECTS=0 ' in proc.stdout, proc.stdout


def test_first_default_use_initializes_once_under_concurrency(tmp_path, monkeypatch):
    target = tmp_path / 'history' / 'coordinator.db'
    monkeypatch.setattr(cc, 'DB_FILE', target)
    created = []
    real_cls = cc.CoordinatorDB

    class Counting(real_cls):
        def __init__(self, *a, **kw):
            created.append(a)
            super().__init__(*a, **kw)

    monkeypatch.setattr(cc, 'CoordinatorDB', Counting)
    lazy = cc._LazyCoordinatorDB(cc._open_default_db)
    assert not target.exists()
    barrier = threading.Barrier(8)
    results, errors = [], []

    def use():
        try:
            barrier.wait(timeout=10)
            results.append(lazy.get_live_nodes())
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=use) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert errors == []
    assert len(results) == 8
    assert created == [(target,)]
    assert lazy._resolve().db_path == str(target)
    assert target.exists()


def test_failed_initialization_is_retried(tmp_path):
    attempts = []

    def factory():
        attempts.append(1)
        if len(attempts) == 1:
            raise sqlite3_error()
        return cc.CoordinatorDB(tmp_path / 'retry.db')

    lazy = cc._LazyCoordinatorDB(factory)
    with pytest.raises(OSError):
        lazy.get_live_nodes()
    assert lazy._instance is None
    assert lazy.get_live_nodes() == []
    assert len(attempts) == 2
    lazy.get_live_nodes()
    assert len(attempts) == 2


def sqlite3_error():
    return OSError('transient coordinator DB failure')


def test_explicit_path_is_eager_and_independent_of_default(tmp_path, monkeypatch):
    default = tmp_path / 'default.db'
    monkeypatch.setattr(cc, 'DB_FILE', default)
    explicit = tmp_path / 'explicit.db'
    db = cc.CoordinatorDB(explicit)
    assert explicit.exists()
    assert db.get_live_nodes() == []
    assert not default.exists()
