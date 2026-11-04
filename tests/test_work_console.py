"""Public Work selection uses the owner graph and never starts execution."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from relay import relay_mesh, work_console
from test_work_run import os_root, NativeFixtureClient, evidence


@pytest.fixture
def graph_root(tmp_path):
    root = tmp_path/'control root'
    root.mkdir()
    def node(identity, priority='P1'):
        return {'id': identity, 'title': identity+' title', 'summary': 'A useful change.',
            'kind': 'task', 'cluster': 'test', 'horizon': 'H0', 'priority': priority,
            'criticality': 'HIGH', 'work_status': 'PROPOSED', 'admission_state': 'ADMITTED',
            'epistemic_status': 'CERTAIN', 'authority_required': 'none', 'confidence': {'score': 1},
            'bounded_contexts': [], 'evidence_required': [], 'exit_criteria': ['unit'],
            'source_refs': [], 'source_details': [], 'provenance': [], 'source_claim_ids': [],
            'unlock_score': 0, 'tags': [], 'metrics': {}, 'notes': [], 'items': [],
            'canonical_system': 'local', 'canonical_id': identity, 'canonical_revision': 'r1',
            'observed_at': '2026-09-12T00:00:00Z', 'projection_run_id': 'test',
            'staleness_state': 'FRESH', 'readiness': {'operational_identity_required': True},
            'completion': {'status': 'NOT_DONE', 'dod_satisfied': False}}
    raw = {'meta': {'schema': 'work-graph-poc/0.5'}, 'sources': {},
           'nodes': [node('ready', 'P0'), node('blocked')],
           'edges': [{'from': 'ready', 'to': 'blocked', 'type': 'UNLOCKS', 'semantic': True,
                      'source_refs': [], 'source_details': [], 'confidence': {'score': 1},
                      'notes': [], 'requirements': {}}]}
    (root/'work-graph.json').write_text(json.dumps(raw), encoding='utf-8')
    return root


def cli(root, *args, module=True):
    # The public CLI must use the same paired satellites as the test host,
    # rather than a stale global editable checkout selected by its .pth files.
    import importlib.util
    roots=[str(Path(__file__).resolve().parents[1])]
    for module_name in ('compute_plane','tare_tools_agent_runtime','specgraph','graph_backlog'):
        spec=importlib.util.find_spec(module_name)
        paired=str(Path(spec.origin).resolve().parent.parent)
        if paired not in roots:roots.append(paired)
    env = {**os.environ, 'PYTHONPATH': os.pathsep.join(roots), 'TARE_OS_ROOT': str(root)}
    entry = ['-m', 'tare_ops'] if module else ['-c',
        'from relay.native_installation import main; raise SystemExit(main())']
    return subprocess.run([sys.executable, '-X', 'utf8', '-B', *entry, *args],
        cwd=root.parent, env=env, capture_output=True, text=True, encoding='utf-8')


@pytest.mark.parametrize('module', [True, False])
def test_public_next_explain_status_select_same_root_without_writing(graph_root, module):
    root = graph_root
    before = (root/'work-graph.json').read_bytes()
    selected = cli(root, 'next', '--limit', '1', module=module)
    assert selected.returncode == 0, selected.stdout+selected.stderr
    result = json.loads(selected.stdout)
    assert Path(result['graph']) == root/'work-graph.json'
    assert [n['id'] for n in result['suggestions']] == ['ready']
    explained = cli(root, 'explain', 'blocked', '--format', 'text', module=module)
    assert explained.returncode == 0, explained.stdout+explained.stderr
    assert 'BLOCKED' in explained.stdout and 'Blocked by: ready' in explained.stdout
    assert 'Exit criteria:\n  unit' in explained.stdout
    assert 'None recorded; run requires commands' in explained.stdout
    state = cli(root, 'status', 'ready', '--format', 'text', module=module)
    assert state.returncode == 0 and 'Run: none recorded' in state.stdout
    assert (root/'work-graph.json').read_bytes() == before
    assert set(root.iterdir()) == {root/'work-graph.json'}


def test_explicit_root_overrides_environment_and_unknown_work_is_actionable(graph_root, tmp_path):
    wrong = tmp_path/'wrong'; wrong.mkdir()
    (wrong/'work-graph.json').write_text('{}')
    result = cli(wrong, '--os-root', str(graph_root), 'explain', 'missing', '--format', 'text')
    assert result.returncode == 2
    assert 'WORK_NOT_FOUND:missing' in result.stdout and 'Traceback' not in result.stderr
    assert 'INVALID_WORK_GRAPH' not in result.stdout


def test_display_reports_settlement_and_stale_checkout_without_hiding_failures():
    receipt = {'work_id': 'ready', 'status': 'SETTLED', 'stage': 'landing', 'duration_ms': 2300,
               'candidate_commit': 'abc123', 'admission': {'verdict': 'ADMITTED'},
               'landing': {'landed': True, 'target_ref': 'refs/heads/main', 'checkout': {'stale': ['src/x.py']}}}
    text = work_console.render('status', {'work_id': 'ready', 'train': {'status': 'LANDED'},
        'last_run': receipt, 'receipt_errors': [{'path': 'old/run.json', 'reason': 'malformed_json'}]})
    assert 'Run: SETTLED' in text and 'Candidate: abc123' in text
    assert 'Checkout needs reconciliation: src/x.py' in text
    assert 'Receipt unavailable: old/run.json (malformed_json)' in text
    failed = work_console.render('run', {'work_id': 'ready', 'status': 'FAILED', 'stage': 'ground',
        'error': 'VERIFICATION_CHECKS_MISSING'})
    assert 'Reason: VERIFICATION_CHECKS_MISSING' in failed and 'Landed:' not in failed


def test_invalid_graph_is_not_presented_as_empty_frontier(graph_root):
    (graph_root/'work-graph.json').write_text('{}')
    result = cli(graph_root, 'next')
    assert result.returncode == 2
    assert 'INVALID_WORK_GRAPH' in json.loads(result.stdout)['error']


@pytest.mark.parametrize('count_unavailable', [False, True])
@pytest.mark.parametrize('unlimited', [False, True])
def test_public_run_and_status_present_real_native_candidate_landing(os_root, monkeypatch, capsys, count_unavailable, unlimited):
    from compute_plane import native_models
    from relay import native_work_host, work_run
    from test_native_agent import deployment
    import tare_ops

    root, repo = os_root
    profile = deployment('bedrock-converse')
    profile['model'] = 'qwen-3.8-27b'
    roster = relay_mesh.RELAY_DIR/'round_table_roster.json'
    value = json.loads(roster.read_bytes())
    value['models']['qwen-3.8-27b']['runtime']['native'] = 'fixture'
    roster.write_text(json.dumps(value))
    profile_path = root/'profile.json'; profile_path.write_text(json.dumps(profile))
    client = NativeFixtureClient()
    invoke = client.post
    observed = []
    def post(path, body, control):
        observed.append(work_run.status('work-one')['last_run']['status'])
        if count_unavailable:
            return {}  # provider cannot count; no generation or landing is permitted
        return invoke(path, body, control)
    client.post = post
    class HttpFixture:
        def __new__(cls, *a, **kw):
            return client
    monkeypatch.setattr(native_models, 'NativeHttpClient', HttpFixture)
    monkeypatch.setattr(native_work_host, 'plan_audit', lambda *a, **kw: {'verdict': 'ACCEPT', 'findings': []})
    monkeypatch.setattr(relay_mesh, 'materialize_admission_evidence', evidence)

    code = tare_ops.main(['run', 'work-one', '--repo-root', str(repo),
        '--native-deployment', str(profile_path), '--format', 'text'] + (['--unlimited'] if unlimited else []))
    output = capsys.readouterr().out
    if count_unavailable:
        assert code == 1 and 'IMPLEMENTATION_BLOCKED' in output
        assert 'CONTEXT_COUNT_UNAVAILABLE' in output
        assert (repo/'src/value.txt').read_text() == 'base\n' and not client.generations
        return
    assert code == 0, output
    assert 'Run: SETTLED' in output and 'Landed: True' in output and 'Candidate:' in output
    assert (repo/'src/value.txt').read_text() == 'strict\n'
    assert len(client.generations) == 5
    assert observed and set(observed) == {'RUNNING'}
    receipt = work_run.status('work-one')['last_run']
    assert receipt['effective_limits']['plan']['max_model_calls'] == (None if unlimited else 12)
    assert receipt['effective_limits']['implementation']['max_seconds'] == (None if unlimited else 600)
    if unlimited:
        attempt = next((root.parent/'.reconciliation/work-runs').glob('*/implement/attempt.json'))
        assert json.loads(attempt.read_text())['deadline_utc'] is None
        assert 'Infinity' not in attempt.read_text()
    assert tare_ops.main(['status', 'work-one', '--format', 'text']) == 0
    assert 'Run: SETTLED' in capsys.readouterr().out
    assert len(client.generations) == 5  # status does not restart execution
