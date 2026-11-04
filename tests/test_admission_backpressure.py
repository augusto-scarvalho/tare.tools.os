"""Offline admission receipts for the existing native exploration scheduler."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from threading import Barrier, Event
import time
from types import SimpleNamespace

import pytest
from compute_plane.native_journal import save_json
from tare_tools_agent_runtime import CancellationToken
from relay import native_agent, parallel_explore as batch
from test_native_agent import setup, Client, deployment
from test_parallel_explore import exploration


def snapshot(directory):
    return {str(p.relative_to(directory)): p.read_bytes()
            for p in directory.rglob('*') if p.is_file()}


def test_two_slots_queue_delay_and_public_show(setup, monkeypatch, capsys):
    _, directory, root = setup
    original = native_agent.run_session
    entered = Barrier(3)
    release = Event()
    calls = []
    clock = [10.0]
    monkeypatch.setattr(batch, 'time', SimpleNamespace(time=time.time, monotonic=lambda: clock[0]))
    payload = {'summary': 'Observed', 'remaining': [], 'sources': [
        {'repository': 'project', 'path': 'a.py',
         'sha256': hashlib.sha256((root/'a.py').read_bytes()).hexdigest()}]}

    def run(config, selected, session, instruction, **kwargs):
        client = Client(steps=[('read', {'repository': 'project', 'path': 'a.py'}),
                               ('tare_deliver', payload)])
        def overlap():
            if client.generations == 1 and session.name in {'1', '2'}:
                entered.wait(timeout=15)
                assert release.wait(timeout=15)
        client.after_model = overlap
        calls.append(session.name)
        return original(config, selected, session, instruction, client=client, local_counter=client, **kwargs)

    monkeypatch.setattr(native_agent, 'run_session', run)
    batch.create(directory, root, ['one', 'two', 'queued'], deployment(), workers=2)
    with ThreadPoolExecutor(max_workers=1) as caller:
        future = caller.submit(batch.execute, directory)
        try:
            entered.wait(timeout=15)
            assert sorted(calls) == ['1', '2']  # Both admitted; third cannot yet run.
            running = batch.inspect(directory)['admission']
            assert running['complete'] is False
            assert running['active_sessions'] == running['peak_admitted_sessions'] == 2
            assert running['pending_items'] == 1 and running['outcomes'] == {}
            clock[0] = 14.0
        finally:
            release.set()
        result = future.result(timeout=30)
    assert result['status'] == 'COMPLETED'
    m = result['admission']
    assert m['schema'] == 'tare.tools/exploration-admission/1'
    assert m['peak_admitted_sessions'] == 2
    assert m['eligible_items'] == m['dispatches'] == 3
    assert m['resume_dispatches'] == m['reused_results'] == 0
    assert m['queue_delay_ms'] == {'count': 3, 'total': 4000.0, 'max': 4000.0}
    assert m['elapsed_ms'] == 4000.0
    assert m['pending_items'] == m['active_sessions'] == 0
    assert m['outcomes'] == {'PROPOSED': 3} and m['complete']
    assert m['physical_model_attempts'] is None
    assert 'not provider serving capacity' in m['scope']
    before = (directory/'batch.json').read_bytes()
    assert batch.main(['--batch', str(directory), '--show', '--format', 'json']) == 0
    assert json.loads(capsys.readouterr().out)['admission'] == m
    assert batch.main(['--batch', str(directory), '--show', '--format', 'text']) == 0
    text = capsys.readouterr().out
    assert 'peak admitted=2' in text and 'queue delay max=4000.000 ms' in text
    assert 'Physical model attempts: unknown' in text
    assert 'no time between executions' in text
    from relay.console_backend import Backend, Selection
    tui_text = Backend().inspect(Selection(root=str(root), mode='parallel', batch=str(directory)))
    assert 'peak admitted=2' in tui_text and 'not provider serving capacity' in tui_text
    assert (directory/'batch.json').read_bytes() == before


def test_failed_peer_and_native_recovery(exploration, monkeypatch):
    directory, _, calls, barrier = exploration
    barrier.wait = lambda **kwargs: None
    original = native_agent.run_session
    def lost(config, selected, session, *args, **kwargs):
        result = original(config, selected, session, *args, **kwargs)
        if session.name == '1':
            raise OSError('lost acknowledgement')
        return result
    monkeypatch.setattr(native_agent, 'run_session', lost)
    first = batch.execute(directory)
    assert first['status'] == 'PAUSED'
    assert first['admission']['outcomes'] == {'FAILED': 1, 'PROPOSED': 1}
    assert first['admission']['dispatches'] == 2
    peer = snapshot(directory/'2')
    generations = sum(c.generations for c in calls)
    monkeypatch.setattr(native_agent, 'run_session', original)
    recovered = batch.execute(directory)
    assert recovered['status'] == 'COMPLETED'
    m = recovered['admission']
    assert m['dispatches'] == m['resume_dispatches'] == m['reused_results'] == 1
    assert m['eligible_items'] == 1 and m['outcomes'] == {'PROPOSED': 1}
    assert m['physical_model_attempts'] is None  # A resumed dispatch is not a retry receipt.
    assert sum(c.generations for c in calls) == generations
    assert snapshot(directory/'2') == peer


def test_cancel_leaves_eligible_work_pending(exploration, monkeypatch):
    directory, _, calls, barrier = exploration
    state = batch.read(directory)
    state['workers'] = 1
    save_json(directory/'batch.json', state)
    barrier.wait = lambda **kwargs: None
    token = CancellationToken()
    original = native_agent.run_session
    def stop(*args, **kwargs):
        result = original(*args, **kwargs)
        token.cancel()
        return result
    monkeypatch.setattr(native_agent, 'run_session', stop)
    result = batch.execute(directory, cancellation=token)
    assert result['status'] == 'CANCELLED'
    m = result['admission']
    assert m['eligible_items'] == 2 and m['dispatches'] == m['pending_items'] == 1
    assert m['queue_delay_ms']['count'] == 1
    assert m['cancellation_requested'] and m['complete']
    assert m['outcomes'] == {'PROPOSED': 1}
    assert result['items'][1]['status'] == 'PENDING' and len(calls) == 1


def test_completed_warm_resume_invocation_and_artifacts(exploration, capsys):
    directory, _, calls, _ = exploration
    assert batch.execute(directory)['status'] == 'COMPLETED'
    before = {name: snapshot(directory/name) for name in ('1', '2')}
    generations = sum(c.generations for c in calls)
    identity = 'a'*32
    assert batch.main(['--batch', str(directory), '--resume', '--invocation-id', identity,
                       '--format', 'json']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['invocation_id'] == identity
    m = result['admission']
    assert m['dispatches'] == m['resume_dispatches'] == m['eligible_items'] == 0
    assert m['reused_results'] == 2 and m['peak_admitted_sessions'] == 0
    assert m['queue_delay_ms'] == {'count': 0, 'total': None, 'max': None}
    assert m['outcomes'] == {} and m['complete']
    assert len(calls) == 2 and sum(c.generations for c in calls) == generations
    assert {name: snapshot(directory/name) for name in ('1', '2')} == before
    receipt = json.loads((directory/'invocation-results'/identity/'result.json').read_bytes())
    assert receipt['result'] == result
    with pytest.raises(FileExistsError):
        batch.execute(directory, invocation_id=identity)
    assert batch.main(['--batch', str(directory), '--show', '--invocation-id', identity]) == 2
    assert 'Invocation identity applies to execution' in capsys.readouterr().out


def test_legacy_measurement_unknown_and_source_integrity(exploration, capsys):
    directory, root, calls, _ = exploration
    assert batch.execute(directory)['status'] == 'COMPLETED'
    state = batch.read(directory)
    state.pop('admission')
    save_json(directory/'batch.json', state)
    before = (directory/'batch.json').read_bytes()
    assert batch.main(['--batch', str(directory), '--show', '--format', 'json']) == 0
    m = json.loads(capsys.readouterr().out)['admission']
    assert m['coverage'] == 'UNKNOWN'
    for field in ('dispatches', 'resume_dispatches', 'reused_results', 'peak_admitted_sessions',
                  'queue_delay_ms', 'pending_items', 'outcomes', 'physical_model_attempts'):
        assert m[field] is None
    assert 'unknown (missing/legacy' in batch.render(batch.inspect(directory))
    assert (directory/'batch.json').read_bytes() == before
    (root/'a.py').write_text('value = 2\n')
    assert batch.execute(directory)['status'] == 'NEEDS_REVIEW'
    assert batch.inspect(directory)['admission']['coverage'] == 'UNKNOWN'
    assert len(calls) == 2


@pytest.mark.parametrize('cancel_before_dispatch', [True, False])
def test_cancelled_dispatch_outcome_or_no_admission(exploration, monkeypatch, cancel_before_dispatch):
    directory, _, calls, _ = exploration
    token = CancellationToken()
    if cancel_before_dispatch:
        token.cancel()
    def cancelled(*args, **kwargs):
        token.cancel()
        return {'status': 'CANCELLED'}
    monkeypatch.setattr(native_agent, 'run_session', cancelled)
    result = batch.execute(directory, cancellation=token)
    m = result['admission']
    assert result['status'] == 'CANCELLED' and m['complete']
    assert m['cancellation_requested'] and m['active_sessions'] == 0
    assert m['pending_items'] + m['dispatches'] == 2
    if cancel_before_dispatch:
        assert m['dispatches'] == m['peak_admitted_sessions'] == 0
        assert m['queue_delay_ms'] == {'count': 0, 'total': None, 'max': None}
        assert m['outcomes'] == {}
    else:
        assert m['dispatches'] >= 1
        assert m['outcomes'] == {'CANCELLED': m['dispatches']}
    assert calls == []
