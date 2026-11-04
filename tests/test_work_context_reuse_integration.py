"""Real public composition with scripted inference; no provider savings claimed."""
import copy
import hashlib
import json
from pathlib import Path

import pytest
from compute_plane.native_journal import save_json
from relay import native_agent, parallel_explore, planning_findings, work_context_reuse as reuse
from test_native_codex import deployment as codex_deployment
from test_native_plan import proposal  # noqa: F401
from test_planning_findings import reports  # noqa: F401
from test_work_context_reuse import selected, prepare, key  # noqa: F401
from test_work_prepared import fixture, git  # noqa: F401
from test_work_resume import host  # noqa: F401
from test_work_run import os_root  # noqa: F401
from test_work_sequence import sequence, create  # noqa: F401


def reviewed(root, directory, paths):
    from test_native_agent import deployment
    parallel_explore.create(directory, root, ['Inspect ' + p for p in paths], deployment())
    state = parallel_explore.read(directory)
    for index, path in enumerate(paths, 1):
        source = {'repository': 'project', 'path': path,
                  'sha256': hashlib.sha256((root/path).read_bytes()).hexdigest()}
        artifact = {'schema': 'tare.tools/native-artifact/1', 'role': 'explorer',
            'kind': 'exploration', 'authority': 'NONE', 'session_id': 'explorer-' + str(index),
            'call_id': 'call', 'profile_digest': str(index)*64,
            'payload': {'summary': 'Inspect ' + path + ' before editing.',
                        'remaining': ['Run the Work verifier.'], 'sources': [source]}}
        (directory/str(index)).mkdir()
        save_json(directory/str(index)/'artifact.json', artifact)
        state['items'][index-1]['status'] = 'PROPOSED'
    state['status'] = 'COMPLETED'
    save_json(directory/'batch.json', state)
    view = planning_findings.preview('parallel', directory)
    return planning_findings.save(view, view['sha256'])


def test_responses_prefix_and_affinity_survive_new_tasks_and_exact_resume(selected, tmp_path, monkeypatch):
    from compute_plane.native_codex_auth import CODEX_ENDPOINT
    context = prepare(selected)
    monkeypatch.setattr(native_agent, 'SESSION_ROOT', tmp_path/'locks')
    config = {'schema': 'tare.tools/native-agent-config/1', 'role': 'implementer',
              'repositories': {'project': str(selected[1])}, 'writable': {}, 'verifiers': {},
              'capabilities': ['read'], 'specgraph_edit_context': False, 'shared_context': context}
    calls = []
    class Client:
        endpoint = CODEX_ENDPOINT
        def post_stream_observed(self, path, body, control, **kwargs):
            calls.append(copy.deepcopy(body))
            return {'id': 'resp', 'model': 'fixture', 'status': 'completed', 'output': [
                {'id': 'fc', 'type': 'function_call', 'status': 'completed', 'call_id': 'call',
                 'name': 'tare_deliver', 'arguments': json.dumps({'summary': 'Advisory only.'})}]}, {
                    'http_attempts_upper_bound': 0, 'transport': 'fixture',
                    'cleanup_verified': True, 'remote_stop': 'not_applicable'}
    client, deployment = Client(), codex_deployment(tmp_path)
    sessions = [tmp_path/'first', tmp_path/'second']
    for custody, instruction in zip(sessions, ['Inspect the default.', 'Inspect the override.']):
        result = native_agent.run_session(config, deployment, custody, instruction, client=client)
        assert result['status'] == 'PROPOSED', result
    saved = [json.loads((p/'native-agent.json').read_bytes()) for p in sessions]
    assert saved[0]['session_id'] != saved[1]['session_id']
    assert calls[0]['prompt_cache_key'] == calls[1]['prompt_cache_key'] == key(saved[0]['deployment'])
    assert calls[0]['tools'] == calls[1]['tools'] and calls[0]['instructions'] == calls[1]['instructions']
    for call in calls:
        assert call['input'][0]['content'].startswith(context['prefix'] + '\n\nCurrent task:\n')
        assert call['input'][0]['content'].count(context['prefix']) == 1
    # Own later edits must not rebuild the first session or erase its journal.
    (selected[1]/'finding.txt').write_bytes(b'Changed after the first delivery.\n')
    before = (sessions[0]/'journal.jsonl').read_bytes()
    result = native_agent.run_session(None, deployment, sessions[0], resume=True, client=client)
    assert result['status'] == 'PROPOSED' and len(calls) == 2
    assert (sessions[0]/'journal.jsonl').read_bytes() == before
    resumed = json.loads((sessions[0]/'native-agent.json').read_bytes())
    for field in ('session_id', 'config', 'deployment', 'instruction', 'budget', 'limits'):
        assert resumed[field] == saved[0][field]


def test_sequence_reuses_only_still_current_reports_and_retains_first_journal(sequence, monkeypatch):
    from relay import work_sequence, work_run
    repo = sequence['repo']
    saved = reviewed(repo, repo.parent/'explorations', ['src/value.txt', 'src/second.txt'])
    create(sequence, findings=saved['path'], findings_sha256=saved['sha256'])
    state = json.loads((sequence['dir']/work_sequence.FILE).read_bytes())
    assert state['schema'] == 'tare.tools/work-sequence/3'
    result = work_sequence.execute(sequence['dir'])
    assert result['status'] == 'COMPLETED', result
    receipts = [json.loads(Path(row['attempts'][-1]['receipt']).read_bytes()) for row in result['items']]
    contexts = [r['shared_context'] for r in receipts]
    assert [len(c['reports']) for c in contexts] == [2, 1]
    assert contexts[1]['omitted'] == [{'id': '1', 'reason': 'changed', 'paths': ['src/value.txt']}]
    # Real tools still read/edit/verify: findings establish no observed read or proof.
    assert len(sequence['calls']) == 10
    journals = []
    for row in result['items']:
        origin = json.loads((work_run.train_dir_for(row['work_id'])/'WORK_RUN.json').read_bytes())
        journals.append(Path(origin['implementation_custody'])/'journal.jsonl')
    before = {str(p): p.read_bytes() for p in journals}
    assert work_sequence.execute(sequence['dir'])['status'] == 'COMPLETED'
    assert len(sequence['calls']) == 10
    assert before == {name: Path(name).read_bytes() for name in before}


def test_prepared_resume_preserves_context_and_rejects_different_findings(fixture, monkeypatch):
    from relay import work_prepared
    directory, node, repo, deployment, client = fixture
    saved = reviewed(repo, repo.parent/'explorations', ['value.txt'])
    row = work_prepared.prepare(directory, node, repo, deployment,
        findings=saved['path'], findings_sha256=saved['sha256'])
    assert row['schema'] == work_prepared.SHARED_SCHEMA
    before = {str(p): p.read_bytes() for p in (directory/'session').rglob('*') if p.is_file()}
    (repo/'value.txt').write_bytes(b'Current project has advanced.\n')
    assert work_prepared.prepare(directory, node, repo, deployment,
        findings=saved['path'], findings_sha256=saved['sha256'])['sha256'] == row['sha256']
    assert client.generations == 4
    with pytest.raises(ValueError, match='PREPARED_FINDINGS_CHANGED'):
        work_prepared.prepare(directory, node, repo, deployment,
            findings=saved['path'], findings_sha256='f'*64)
    assert before == {name: Path(name).read_bytes() for name in before}


def test_prepared_responses_inspection_binds_effective_profile_and_context(fixture, monkeypatch):
    from compute_plane.native_codex_auth import CODEX_ENDPOINT
    from relay import work_prepared
    directory, node, repo, _, scripted = fixture
    selected = codex_deployment(repo.parent) | {'model': 'fixture-model'}
    saved = reviewed(repo, repo.parent/'explorations', ['value.txt'])
    class ResponsesClient:
        endpoint = CODEX_ENDPOINT
        def post_stream_observed(self, path, body, control, **kwargs):
            name, args = next(scripted.steps)
            return {'id': 'resp', 'model': 'fixture', 'status': 'completed', 'output': [
                {'id': 'fc', 'type': 'function_call', 'status': 'completed', 'call_id': 'call',
                 'name': name, 'arguments': json.dumps(args)}]}, {'http_attempts_upper_bound': 0,
                    'transport': 'fixture', 'cleanup_verified': True, 'remote_stop': 'not_applicable'}
    monkeypatch.setattr(native_agent, 'inference_client', lambda *a, **k: ResponsesClient())
    row = work_prepared.prepare(directory, node, repo, selected,
        findings=saved['path'], findings_sha256=saved['sha256'])
    state = json.loads((directory/'session/native-agent.json').read_bytes())
    assert key(state['deployment'])
    assert state['deployment'] != row['deployment'] == selected
    assert work_prepared.inspect(directory)['sha256'] == row['sha256']


@pytest.mark.parametrize('field', ['prefix', 'prefix_sha256', 'authority', 'sources'])
def test_frozen_projection_tampering_fails_before_session(selected, tmp_path, monkeypatch, field):
    context = prepare(selected)
    if field == 'sources':
        context['reports'][0]['sources'][0]['path'] = '../outside'
    else:
        context[field] = 'changed'
    config = {'schema': 'tare.tools/native-agent-config/1', 'role': 'implementer',
              'repositories': {'project': str(selected[1])}, 'writable': {}, 'shared_context': context}
    with pytest.raises(ValueError):
        native_agent.run_session(config, codex_deployment(tmp_path), tmp_path/'refused', 'Inspect')
    assert not (tmp_path/'refused/native-agent.json').exists()
