"""Parallel proposals, serial CAS incorporation, and conservative resource fallback."""
import json
from pathlib import Path
from threading import Barrier

import pytest
from relay import relay_mesh, work_parallel, work_run, work_sequence
from test_work_sequence import sequence as base_sequence, save_graph
from test_work_resume import host
from test_work_run import os_root

pytestmark = pytest.mark.timeout(120)  # Two complete native Work lifecycles and four Git worktrees.


@pytest.fixture
def sequence(base_sequence, monkeypatch):
    from compute_plane import native_models
    original = native_models.NativeHttpClient
    def client(*args, **kwargs):
        value = original(*args, **kwargs)
        post = value.post
        def typed(*args, **kwargs):
            result = post(*args, **kwargs)
            body = args[1]
            typed_delivery = any('sources' in item.get('toolSpec', {}).get('inputSchema', {}).get('json', {}).get('properties', {})
                for item in body.get('toolConfig', {}).get('tools', []))
            for item in result.get('output', {}).get('message', {}).get('content', []):
                tool = item.get('toolUse', {})
                if tool.get('name') == 'tare_deliver' and typed_delivery:
                    tool['input'].update(sources=[], remaining=[])
            return result
        value.post = typed
        return value
    monkeypatch.setattr(native_models, 'NativeHttpClient', client)
    return base_sequence


def independent(case):
    data = json.loads(relay_mesh.WORK_GRAPH_FILE.read_bytes())
    data['edges'] = []
    save_graph(data)
    return data


def create(case, **options):
    return work_sequence.create(case['dir'], ['work-one', 'work-two'], case['repo'],
        case['profile'], unlimited=True, workers=2, isolated_effects=True, **options)


def test_two_native_proposals_in_parallel_land_serially_and_resume_without_calls(sequence, monkeypatch):
    from relay import work_prepared
    independent(sequence)
    create(sequence)
    barrier = Barrier(2, timeout=20)
    original = work_prepared.prepare
    def concurrent(*args, **kwargs):
        barrier.wait()
        return original(*args, **kwargs)
    monkeypatch.setattr(work_prepared, 'prepare', concurrent)
    result = work_sequence.execute(sequence['dir'])
    assert result['status'] == 'COMPLETED', json.dumps(result, default=str)  # a string is never truncated
    assert result['completed'] == 2 and len(sequence['calls']) == 10
    assert all(row['preparation']['status'] == 'PREPARED' for row in result['items'])
    assert len({work_prepared.inspect(row['prepared'])['base_commit'] for row in result['items']}) == 1
    receipts = [json.loads(Path(row['attempts'][0]['receipt']).read_bytes()) for row in result['items']]
    assert all(row['execution'] == 'prepared' for row in receipts)
    assert receipts[0]['candidate_commit'] != receipts[1]['candidate_commit']
    assert (sequence['repo']/'src/value.txt').read_text() == 'strict\n'
    assert (sequence['repo']/'src/second.txt').read_text() == 'strict\n'
    calls = len(sequence['calls'])
    assert work_sequence.execute(sequence['dir']) == result
    assert len(sequence['calls']) == calls


def test_reviewed_context_is_frozen_per_prepared_session_and_rechecked_at_new_work(sequence):
    from test_work_context_reuse_integration import reviewed
    from relay import work_prepared
    independent(sequence)
    selected = reviewed(sequence['repo'], sequence['repo'].parent/'explorations',
                        ['src/value.txt', 'src/second.txt'])
    create(sequence, findings=selected['path'], findings_sha256=selected['sha256'])
    result = work_sequence.execute(sequence['dir'])
    assert result['status'] == 'COMPLETED', result
    before = {}
    for row in result['items']:
        prepared = work_prepared.inspect(row['prepared'])
        assert prepared['schema'] == work_prepared.SHARED_SCHEMA
        session = Path(prepared['session'])
        saved = json.loads((session/'native-agent.json').read_bytes())
        assert len(saved['config']['shared_context']['reports']) == 2
        before[str(session/'journal.jsonl')] = (session/'journal.jsonl').read_bytes()
    receipts = [json.loads(Path(row['attempts'][-1]['receipt']).read_bytes()) for row in result['items']]
    assert [len(r['shared_context']['reports']) for r in receipts] == [2, 1]
    assert all(r['execution'] == 'prepared' and r['prepared_implementation']['sha256'] for r in receipts)
    calls = len(sequence['calls'])
    assert work_sequence.execute(sequence['dir'])['status'] == 'COMPLETED'
    assert len(sequence['calls']) == calls == 10
    assert before == {name: Path(name).read_bytes() for name in before}


@pytest.mark.parametrize('reason', ['unknown', 'overlap', 'resource', 'local', 'dependency'])
def test_unsafe_parallel_request_falls_back_before_preparing(sequence, monkeypatch, reason):
    from relay import work_prepared
    data = independent(sequence)
    if reason == 'overlap':
        data['nodes'][1]['repository_scopes'][0]['target_paths'] = ['src']
    elif reason == 'resource':
        for node in data['nodes']:
            node['metrics']['shared_resources'] = ['database:test']
    elif reason == 'dependency':
        data['edges'] = [{'from': 'work-one', 'to': 'work-two', 'type': 'UNLOCKS', 'semantic': True,
                         'source_refs': [], 'source_details': [], 'confidence': {'score': 1}, 'notes': [], 'requirements': {}}]
    save_graph(data)
    if reason == 'local':
        sequence['profile']['native_challenger_accounting'] = 'local'
    work_sequence.create(sequence['dir'], ['work-one','work-two'], sequence['repo'], sequence['profile'],
                         unlimited=True, workers=2, isolated_effects=reason != 'unknown')
    monkeypatch.setattr(work_prepared, 'prepare', lambda *a, **kw: pytest.fail('unsafe preparation'))
    result = work_sequence.execute(sequence['dir'])
    assert result['status'] == 'COMPLETED', result
    assert all('Serial:' in row['parallel_note'] for row in result['items'])


def test_same_file_conflict_preserves_proposal_and_falls_back_to_serial(sequence, monkeypatch):
    from relay import work_prepared
    independent(sequence)
    create(sequence)
    original = work_prepared.check
    # The detailed blob/mode conflict detection is tested by work_prepared; here exercise scheduler fallback.
    def check(directory, *args, **kwargs):
        state = json.loads((sequence['dir']/work_sequence.FILE).read_bytes())
        if str(directory) == state['items'][1].get('prepared'):
            return {'status': 'CONFLICT', 'files': ['src/second.txt'], 'reason': 'changed'}
        return original(directory, *args, **kwargs)
    monkeypatch.setattr(work_prepared, 'check', check)
    result = work_sequence.execute(sequence['dir'])
    assert result['status'] == 'COMPLETED', result
    assert len(sequence['calls']) == 15
    assert 'Conflict:' in result['items'][1]['parallel_note']
    assert (Path(result['items'][1]['prepared'])/'prepared.json').is_file()
    receipt = json.loads(Path(result['items'][1]['attempts'][0]['receipt']).read_bytes())
    assert receipt['preparation_sessions'] == [str(Path(result['items'][1]['prepared'])/'session')]


def test_overlap_is_directory_aware_and_case_insensitive():
    assert work_parallel.overlap(['src'], ['src/a.py'])
    assert work_parallel.overlap(['SRC/A.py'], ['src/a.py'])
    assert work_parallel.overlap(['.'], ['anything.py'])
    assert not work_parallel.overlap(['src/a.py'], ['src/ab.py'])


def test_cancel_after_preparation_then_reduce_capacity_reuses_both_proposals(sequence, monkeypatch):
    import os
    from relay import native_agent, work_prepared
    from tare_tools_agent_runtime import CancellationToken
    independent(sequence)
    create(sequence)
    # Host Git preferences must not change the strict workspace's newline policy.
    work_run.git(sequence['repo'], 'config', 'core.autocrlf', 'false')
    cancel = CancellationToken()
    original = work_parallel.prepare_ready
    run_session = native_agent.run_session
    def invalidate_stat_after_session(config, *args, **kwargs):
        result = run_session(config, *args, **kwargs)
        # Fresh index stat data can mask CRLF conversion differences. Make Git
        # inspect unchanged sibling bytes as it may do on a busy Windows runner.
        for root in config['repositories'].values():
            for name in ('src/value.txt', 'src/second.txt'):
                path = Path(root)/name
                observed = path.stat()
                os.utime(path, ns=(observed.st_atime_ns, observed.st_mtime_ns + 2_000_000_000))
        return result
    def stop_after_preparing(*args, **kwargs):
        original(*args, **kwargs)
        cancel.cancel()
    with monkeypatch.context() as patch:
        patch.setattr(work_parallel, 'prepare_ready', stop_after_preparing)
        patch.setattr(native_agent, 'run_session', invalidate_stat_after_session)
        result = work_sequence.execute(sequence['dir'], cancellation=cancel)
    assert result['status'] == 'CANCELLED' and len(sequence['calls']) == 10, result
    assert result['completed'] == 0
    work_sequence.serial(sequence['dir'])
    def forbidden(*args, **kwargs):
        pytest.fail('Completed preparations must survive capacity reduction')
    monkeypatch.setattr(work_prepared, 'prepare', forbidden)
    result = work_sequence.execute(sequence['dir'])
    assert result['status'] == 'COMPLETED', result
    assert result['workers'] == 1 and len(sequence['calls']) == 10
