"""Offline recovery and policy fixtures; never use the operator installation."""
import json
import multiprocessing

import pytest

from relay import native_product as product

A, B, C = 'a' * 64, 'b' * 64, 'c' * 64


def selected(home, current=B, previous=A):
    product.write_json(home/'active.json', {'current': current, 'previous': previous})


@pytest.fixture
def recovery(tmp_path, monkeypatch):
    selected(tmp_path)
    monkeypatch.setattr(product, 'status', lambda home: product.state(home))
    monkeypatch.setattr(product, 'check_release', lambda home, identifier: {})
    return tmp_path


def test_B_fails_A_recovers_and_receipt_and_barrier_are_atomic(recovery, monkeypatch):
    home = recovery
    seen = []
    def move(home, state, *args):
        seen.append(state['current'])
        return 'failed' if state['current'] == B else 'restarted'
    monkeypatch.setattr(product, '_move_serve', move)
    assert product.restart_serve(home, wait_seconds=0) == 'rolled back: restarted'
    value = product.state(home)
    assert seen == [B, A]
    assert (value['current'], value['previous']) == (A, B)
    assert value['last_transition']['operation'] == 'auto_rollback'
    assert value['auto_update'] == {'paused': False, 'after': B}


def test_A_also_fails_and_replay_never_oscillates(recovery, monkeypatch):
    home = recovery
    attempted = []
    def fail(home, state, *args):
        attempted.append(state['current'])
        return 'failed'
    monkeypatch.setattr(product, '_move_serve', fail)
    with pytest.raises(ValueError, match='Recovery failed'):
        product.auto_update(home, wait_seconds=0)
    before = (home/'active.json').read_bytes()
    with pytest.raises(ValueError, match='Recovery failed'):
        product.auto_update(home, wait_seconds=0)
    assert attempted == [B, A, A]
    assert (home/'active.json').read_bytes() == before
    assert product.state(home)['current'] == A


def test_failed_previous_qualification_preserves_pointer(recovery, monkeypatch):
    monkeypatch.setattr(product, '_move_serve', lambda *args: 'failed')
    def reject(*args):
        raise ValueError('fixture qualification rejected')
    monkeypatch.setattr(product, 'check_release', reject)
    before = (recovery/'active.json').read_bytes()
    with pytest.raises(ValueError, match='qualification rejected'):
        product.restart_serve(recovery, wait_seconds=0)
    assert (recovery/'active.json').read_bytes() == before


def test_late_failure_cannot_rollback_a_different_pointer(recovery, monkeypatch):
    def late(home, state, *args):
        selected(home, C, B)
        return 'failed'
    monkeypatch.setattr(product, '_move_serve', late)
    with pytest.raises(ValueError, match='stale transition refused'):
        product.restart_serve(recovery, wait_seconds=0)
    assert product.state(recovery) == {'current': C, 'previous': B}


def test_manual_rollback_pauses_and_resume_is_byte_idempotent(recovery, monkeypatch):
    home = recovery
    product.rollback(home)
    receipt = product.state(home)['last_transition']
    assert product.state(home)['auto_update']['paused'] is True
    monkeypatch.setattr(product, '_restart_locked', lambda *a, **k: 'current')
    monkeypatch.setattr(product, 'latest_published', lambda *a, **k: pytest.fail('paused lookup'))
    assert product.auto_update(home)['serve'] == 'paused'
    assert product.main(['--home', str(home), 'auto-update', '--resume']) == 0
    before = (home/'active.json').read_bytes()
    product.auto_update(home, resume=True)
    assert (home/'active.json').read_bytes() == before
    assert product.state(home)['last_transition'] == receipt
    assert product.state(home)['auto_update'] == {'paused': False, 'after': None}


def test_rejected_B_excludes_all_older_candidates_but_allows_C(monkeypatch):
    rows = [{'tagName': 'product-' + value[:16], 'publishedAt': str(i), 'createdAt': str(i)}
            for i, value in enumerate((A, B, C))]
    class Reply:
        stdout = json.dumps(rows)
    monkeypatch.setattr(product.subprocess, 'run', lambda *a, **k: Reply())
    asked = []
    monkeypatch.setattr(product, 'ci_green', lambda repo, tag: asked.append(tag) or True)
    assert product.latest_published('fixture', A, after=B) == 'product-' + C[:16]
    assert asked == ['product-' + C[:16]]
    asked.clear()
    assert product.latest_published('fixture', C, after=B) is None
    assert product.latest_published('fixture', A, after='d' * 64) is None
    assert asked == []


def _lock_contender(home, queue):
    try:
        with product.exclusive(home):
            queue.put('entered')
    except OSError:
        queue.put('locked')


def test_install_lock_excludes_another_process(tmp_path):
    context = multiprocessing.get_context('spawn')
    queue = context.Queue()
    child = context.Process(target=_lock_contender, args=(tmp_path, queue))
    try:
        with product.exclusive(tmp_path):
            child.start()
            assert queue.get(timeout=20) == 'locked'
        child.join(timeout=20)
        assert child.exitcode == 0
        with product.exclusive(tmp_path):
            pass  # lock is released, not a stale marker
    finally:
        if child.is_alive():
            child.terminate()
            child.join(timeout=5)
        queue.close()


@pytest.mark.parametrize('manual', ['install', 'rollback'])
def test_automatic_recovery_holds_same_lock_against_manual(recovery, monkeypatch, manual):
    def move(home, state, *args):
        with pytest.raises(OSError):
            if manual == 'install':
                product.install(home/'unused-bundle', home)
            else:
                product.rollback(home)
        return 'deferred: fixture'
    monkeypatch.setattr(product, '_move_serve', move)
    assert product.auto_update(recovery)['serve'] == 'deferred: fixture'


def test_failed_manual_rollback_does_not_undo_the_operator_choice(recovery, monkeypatch):
    product.rollback(recovery)
    before = (recovery/'active.json').read_bytes()
    attempted = []
    def fail(home, state, *args):
        attempted.append(state['current'])
        return 'failed'
    monkeypatch.setattr(product, '_move_serve', fail)
    with pytest.raises(ValueError, match='Recovery failed'):
        product.auto_update(recovery, wait_seconds=0)
    assert attempted == [A]
    assert (recovery/'active.json').read_bytes() == before


def test_installation_without_managed_serve_can_still_auto_update(recovery, monkeypatch):
    import subprocess
    import zipfile
    home = recovery
    selected(home, A, None)
    monkeypatch.setattr(product, 'serve_health', lambda: None)
    monkeypatch.setattr(product, '_port_open', lambda: False)
    monkeypatch.setattr(product.WindowsServeTask, 'available', lambda self: False)
    monkeypatch.setattr(product, 'latest_published', lambda *a, **kw: 'product-'+B[:16])
    def download(argv, **kw):
        assert argv[:3] == ['gh', 'release', 'download']
        with zipfile.ZipFile(home/argv[argv.index('--dir')+1]/'bundle.zip', 'w') as archive:
            archive.writestr('bundle.json', '{}')
        return subprocess.CompletedProcess(argv, 0)
    monkeypatch.setattr(product.subprocess, 'run', download)
    monkeypatch.setattr(product, '_install_locked', lambda bundle, home: selected(home, B, A))
    monkeypatch.setattr(product, 'published_models', lambda: [])
    monkeypatch.setattr(product, 'sync_vscode', lambda models: [])
    monkeypatch.setattr(product, 'auto_storage', lambda home: 'off')
    result = product.auto_update(home)
    assert result['installed'] == 'product-'+B[:16]
    assert result['serve'] == 'no serve task'
    assert product.state(home)['current'] == B


def test_selected_server_with_another_drain_lease_does_not_admit_an_update(recovery, monkeypatch):
    health = {'status':'ok', 'pid':7, 'release':B,
              'installation':product._installation(recovery), 'control_protocol':1,
              'busy':0, 'draining':True, 'stopping':False}
    monkeypatch.setattr(product, 'serve_health', lambda: health)
    monkeypatch.setattr(product, 'latest_published', lambda *a, **k: pytest.fail('drained lookup'))
    assert product.auto_update(recovery)['serve'].startswith('deferred:')


@pytest.fixture
def protocol_task(recovery, monkeypatch):
    """Real HTTP admission/control, with only scheduling and installed wheels stubbed."""
    from http.server import ThreadingHTTPServer
    import socket
    import threading
    import time
    from types import SimpleNamespace
    from relay import native_serve

    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    url = f'http://127.0.0.1:{port}/health'
    read_health = product.serve_health
    monkeypatch.setattr(product, 'SERVE_HEALTH', url)
    def listening():
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=.02):
                return True
        except OSError:
            return False
    monkeypatch.setattr(product, '_port_open', listening)
    monkeypatch.setattr(product, 'serve_health', lambda: read_health(url) if listening() else None)
    clock = [time.monotonic()]
    def advance(seconds):
        clock[0] += seconds
        threading.Event().wait(.01)
    monkeypatch.setattr(product, 'time', SimpleNamespace(
        monotonic=lambda: clock[0], sleep=advance,
        strftime=time.strftime, gmtime=time.gmtime))
    monkeypatch.setattr(product, 'published_models', lambda: [])
    monkeypatch.setattr(product, 'sync_vscode', lambda models: [])
    monkeypatch.setattr(product, 'auto_storage', lambda home: 'off')

    class Task:
        def __init__(self):
            self.home, self.started, self.failures, self.servers = recovery, [], set(), []

        def available(self):
            return True

        def start(self):
            release = product.state(self.home)['current']
            self.started.append(release)
            if release in self.failures:
                return
            monkeypatch.setenv('TARE_MANAGED_HOME', str(self.home))
            monkeypatch.setenv('TARE_RELEASE_ID', release)
            admission = native_serve.Admission()
            admission.require_selected()
            server = ThreadingHTTPServer(('127.0.0.1', port),
                native_serve.handler({}, 'tare-local', admission=admission))
            def run():
                try:
                    server.serve_forever(poll_interval=.005)
                finally:
                    server.server_close()
            thread = threading.Thread(target=run, daemon=True)
            self.servers.append((server, thread, admission))
            thread.start()

    task = Task()
    monkeypatch.setattr(product, 'WindowsServeTask', lambda: task)
    try:
        yield task
    finally:
        for server, thread, _ in task.servers:
            if thread.is_alive():
                server.shutdown()
            thread.join(timeout=3)
            server.server_close()
            assert not thread.is_alive()


def test_http_failed_successor_recovers_and_next_run_skips_rejected(protocol_task, monkeypatch):
    task = protocol_task
    task.failures.add(B)
    assert product.restart_serve(task.home, wait_seconds=0, poll=.01) == 'rolled back: restarted'
    assert task.started == [B, A]
    assert product.serve_health()['release'] == A
    before = (task.home/'active.json').read_bytes()
    looked = []
    def latest(repo, current, *, after):
        looked.append((current, after))
        return None
    monkeypatch.setattr(product, 'latest_published', latest)
    assert product.auto_update(task.home)['serve'] == 'current'
    assert looked == [(A, B)]
    assert task.started == [B, A]
    assert (task.home/'active.json').read_bytes() == before


def test_http_busy_drain_renews_and_stops_after_existing_work_finishes(protocol_task, monkeypatch):
    task = protocol_task
    selected(task.home, A, None)
    task.start()
    incumbent = task.servers[-1][2]
    ticket = incumbent.admit()
    assert ticket is not None
    selected(task.home, B, A)
    control = product.serve_control
    observed = []
    def observe(identity, owner, action):
        reply = control(identity, owner, action)
        observed.append((owner, action, reply['busy']))
        if action == 'drain':
            assert incumbent.admit() is None
            if len(observed) == 3:
                ticket.done()
        return reply
    monkeypatch.setattr(product, 'serve_control', observe)
    assert product.restart_serve(task.home, wait_seconds=2, poll=.01) == 'restarted'
    assert task.started == [A, B]
    assert [action for _, action, _ in observed] == ['drain'] * 4 + ['stop']
    assert len({owner for owner, _, _ in observed}) == 1
    assert observed[-1][2] == 0 and incumbent.stopping
    assert product.serve_health()['release'] == B


def test_http_busy_deadline_releases_lease_without_stopping(protocol_task):
    task = protocol_task
    selected(task.home, A, None)
    task.start()
    incumbent = task.servers[-1][2]
    ticket = incumbent.admit()
    selected(task.home, B, A)
    before = (task.home/'active.json').read_bytes()
    try:
        assert 'lease released' in product.restart_serve(task.home, wait_seconds=0, poll=.01)
        assert incumbent.owner is None and not incumbent.stopping
        assert task.started == [A]
        assert (task.home/'active.json').read_bytes() == before
    finally:
        ticket.done()


def test_http_lost_stop_reply_replays_only_selected_start(protocol_task, monkeypatch):
    task = protocol_task
    selected(task.home, A, None)
    task.start()
    selected(task.home, B, A)
    control = product.serve_control
    def lost(identity, owner, action):
        result = control(identity, owner, action)
        if action == 'stop':
            raise OSError('fixture lost stop acknowledgement')
        return result
    monkeypatch.setattr(product, 'serve_control', lost)
    assert product.restart_serve(task.home, wait_seconds=0).startswith('deferred:')
    task.servers[0][1].join(timeout=3)
    assert not task.servers[0][1].is_alive()
    monkeypatch.setattr(product, 'serve_control', control)
    monkeypatch.setattr(product, 'latest_published', lambda *a, **k: pytest.fail('replay selected new Work'))
    assert product.auto_update(task.home, wait_seconds=0)['serve'] == 'restarted'
    assert task.started == [A, B]
    assert product.serve_health()['release'] == B
