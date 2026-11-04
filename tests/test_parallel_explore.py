import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
from threading import Barrier

import pytest
from compute_plane.native_journal import custody_lock
from tare_tools_agent_runtime import CancellationToken
from relay import native_agent, parallel_explore as batch
from test_native_agent import setup, Client, deployment


@pytest.fixture
def exploration(setup, monkeypatch):
    _, custody, root = setup
    original = native_agent.run_session
    calls = []
    barrier = Barrier(2)
    payload = {'summary': 'Observed value = 1', 'remaining': [], 'sources': [
        {'repository': 'project', 'path': 'a.py',
         'sha256': hashlib.sha256((root/'a.py').read_bytes()).hexdigest()}]}
    def run(config, selected, session, instruction, **kwargs):
        client = Client(steps=[('read', {'repository': 'project', 'path': 'a.py'}),
                               ('tare_deliver', payload)])
        def overlap():
            if client.generations == 1:
                barrier.wait(timeout=15)
        client.after_model = overlap
        calls.append(client)
        return original(config, selected, session, instruction, client=client, local_counter=client, **kwargs)
    monkeypatch.setattr(native_agent, 'run_session', run)
    batch.create(custody, root, ['Inspect the value', 'Inspect the assignment'], deployment(), workers=2, unlimited=True)
    return custody, root, calls, barrier


def test_native_sessions_overlap_and_resume_does_not_repeat(exploration):
    directory, root, calls, _ = exploration
    result = batch.execute(directory)
    assert result['status'] == 'COMPLETED', result
    assert len(calls) == 2 and all(c.generations == 2 for c in calls)
    assert (root/'a.py').read_bytes() == b'value = 1\n'
    for row in result['items']:
        assert row['report']['sources'][0]['path'] == 'a.py'
        state = json.loads((Path(row['session'])/'native-agent.json').read_bytes())
        assert state['limits']['max_model_retries'] == 0
        assert all(v is None for k, v in state['limits'].items() if k != 'max_model_retries')
        assert state['config']['capabilities'] == ['read', 'list', 'search']
    assert batch.execute(directory)['status'] == 'COMPLETED'
    assert len(calls) == 2


def test_public_tui_limits_reach_sessions_without_control_graph(setup, monkeypatch, capsys):
    from relay import native_installation, relay_mesh
    from relay.console_backend import Selection, command
    _, custody, root = setup
    control = root.parent/'empty-control'
    profile = root.parent/'profile.json'
    profile.write_text(json.dumps(deployment()))
    selected = Selection(str(root), mode='parallel', prompt='Inspect a.py', profile=str(profile),
                         unlimited=False, max_model_calls='2', max_seconds='90')
    monkeypatch.setattr(batch, 'batch_root', lambda: custody.parent/'batches')
    args = command(selected)
    assert args[args.index('--max-model-calls')+1] == '2'
    payload = {'summary': 'Read a.py.', 'remaining': [], 'sources': [
        {'repository': 'project', 'path': 'a.py', 'sha256': hashlib.sha256((root/'a.py').read_bytes()).hexdigest()}]}
    client = Client(steps=[('read', {'repository': 'project', 'path': 'a.py'}), ('tare_deliver', payload)])
    monkeypatch.setattr(native_agent, 'inference_client', lambda *a, **k: client)
    monkeypatch.setattr('compute_plane.native_models.NativeHttpClient', lambda *a, **k: client)
    original = relay_mesh.RELAY_ROOT
    try:
        assert native_installation.main(['--os-root', str(control), *args]) == 0
        result = json.loads(capsys.readouterr().out)
        assert result['status'] == 'COMPLETED' and client.generations == 2
        assert not (control/'work-graph.json').exists()
        state = json.loads((Path(selected.batch)/'1/native-agent.json').read_bytes())
        assert state['limits']['max_model_calls'] == 2 and state['limits']['max_seconds'] == 90
        assert batch.read(selected.batch)['schema'] == 'tare.tools/parallel-exploration/2'
        journal = (Path(selected.batch)/'1/journal.jsonl').read_bytes()
        assert batch.main(['--batch', selected.batch, '--resume', '--max-model-calls', '3']) == 2
        assert 'frozen' in capsys.readouterr().out
        assert batch.main(['--batch', selected.batch, '--resume', '--format', 'json']) == 0
        assert client.generations == 2
        assert (Path(selected.batch)/'1/journal.jsonl').read_bytes() == journal
        saved = Path(selected.batch)/'batch.json'
        value = json.loads(saved.read_bytes()); value.pop('limits')
        saved.write_text(json.dumps(value))
        with pytest.raises(ValueError, match='limits are missing'):
            batch.read(selected.batch)
    finally:
        relay_mesh.rebind_root(original)


def test_partial_failure_keeps_other_result(exploration, monkeypatch):
    directory, _, calls, _ = exploration
    original = native_agent.run_session
    def fail(config, selected, session, *args, **kwargs):
        if session.name == '1':
            raise OSError('endpoint unavailable')
        # Remove the concurrency barrier; its failed peer never reaches the model.
        return original(config, selected, session, *args, **kwargs)
    exploration[3].wait = lambda **kwargs: None
    monkeypatch.setattr(native_agent, 'run_session', fail)
    first = batch.execute(directory)
    assert first['status'] == 'PAUSED' and first['completed'] == 1
    receipt = (directory/'2'/'outcome.json').read_bytes()
    monkeypatch.setattr(native_agent, 'run_session', original)
    assert batch.execute(directory)['status'] == 'COMPLETED'
    assert (directory/'2'/'outcome.json').read_bytes() == receipt and len(calls) == 2


def test_changed_completed_source_requires_review(exploration):
    directory, root, calls, _ = exploration
    assert batch.execute(directory)['status'] == 'COMPLETED'
    (root/'a.py').write_text('value = 2\n')
    assert batch.inspect(directory)['status'] == 'NEEDS_REVIEW'
    assert batch.execute(directory)['status'] == 'NEEDS_REVIEW' and len(calls) == 2


def test_cancel_stops_queue_and_preserves_completed(exploration, monkeypatch):
    directory, _, calls, barrier = exploration
    state = batch.read(directory); state['workers'] = 1
    from compute_plane.native_journal import save_json
    save_json(directory/'batch.json', state)
    barrier.wait = lambda **kwargs: None
    cancel = CancellationToken()
    original = native_agent.run_session
    def run(*args, **kwargs):
        result = original(*args, **kwargs)
        cancel.cancel()
        return result
    monkeypatch.setattr(native_agent, 'run_session', run)
    result = batch.execute(directory, cancellation=cancel)
    assert result['status'] == 'CANCELLED' and result['completed'] == 1
    assert result['items'][1]['status'] == 'PENDING' and len(calls) == 1
    monkeypatch.setattr(native_agent, 'run_session', original)
    assert batch.execute(directory)['status'] == 'COMPLETED' and len(calls) == 2


def test_lost_result_recovers_native_journal_without_new_generation(exploration, monkeypatch):
    directory, _, calls, barrier = exploration
    barrier.wait = lambda **kwargs: None
    original = native_agent.run_session
    def lost(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError('lost acknowledgement')
    monkeypatch.setattr(native_agent, 'run_session', lost)
    assert batch.execute(directory)['status'] == 'PAUSED'
    assert sum(c.generations for c in calls) == 4
    monkeypatch.setattr(native_agent, 'run_session', original)
    assert batch.execute(directory)['status'] == 'COMPLETED'
    assert sum(c.generations for c in calls) == 4


def test_exclusive_batch_and_writer_locks_retained(exploration, setup):
    directory, root, _, _ = exploration
    with custody_lock(directory), pytest.raises((OSError, RuntimeError, ValueError)):
        batch.execute(directory)
    with ExitStack() as outer:
        native_agent.workspace_locks(outer, {'repo': str(root)})
        with ExitStack() as inner, pytest.raises((OSError, RuntimeError, ValueError)):
            native_agent.workspace_locks(inner, {'repo': str(root)})


def test_cli_validation_and_tui_selection(exploration, monkeypatch, capsys):
    from relay.console_backend import Selection, command, Backend
    directory, root, _, _ = exploration
    assert batch.main(['--batch', str(directory), '--resume', '--workers', '2']) == 2
    capsys.readouterr()
    selected = Selection(root=str(root), mode='parallel', workers='2', prompt='Question one\nQuestion two')
    monkeypatch.setattr(batch, 'batch_root', lambda: directory.parent/'batches')
    argv = command(selected)
    assert argv.count('--question') == 2 and '--unlimited' in argv and selected.batch
    selected.batch = str(directory)
    assert command(selected, True) == ['parallel-explore', '--batch', str(directory), '--resume', '--format', 'json']
    selected.root = str(root.parent)
    with pytest.raises(ValueError, match='another project'):
        Backend().batch_view(selected)
    for workers in (0, -1, True):
        with pytest.raises(ValueError):
            batch.create(directory.parent/'invalid', root, ['q'], deployment(), workers=workers)


def test_console_parallel_fields(exploration):
    from relay.native_console import WorkConsole
    from textual.widgets import Select, Input, TextArea
    class Backend:
        def catalog(self, root):
            return {'models': [{'deployment': 'local-qwen', 'model': 'fixture'}], 'sessions': [], 'works': [], 'errors': []}
    async def check():
        app = WorkConsole(exploration[1], backend=Backend())
        async with app.run_test(size=(120, 70)) as pilot:
            app.query_one('#mode', Select).value = 'parallel'
            await pilot.pause()
            app.query_one('#workers', Input).value = '2'
            app.query_one('#prompt', TextArea).text = 'one\ntwo'
            assert app.query_one('#parallel-options').display
            assert not app.query_one('#prompt', TextArea).disabled
            assert app.selection().workers == '2'
    asyncio.run(check())
