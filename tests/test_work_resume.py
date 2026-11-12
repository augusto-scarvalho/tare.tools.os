"""Public settlement recovery: real Git/coordinator, scripted provider, simulated admission inputs."""
import json
from pathlib import Path

import pytest

from relay import relay_mesh, work_run
from test_work_run import os_root, NativeFixtureClient, evidence, _git


@pytest.fixture
def host(os_root, monkeypatch):
    from compute_plane import native_models
    from relay import native_work_host
    from test_native_agent import deployment
    root, repo = os_root
    profile = deployment('bedrock-converse')
    profile['model'] = 'qwen-3.8-27b'
    roster = relay_mesh.RELAY_DIR/'round_table_roster.json'
    value = json.loads(roster.read_bytes())
    value['models']['qwen-3.8-27b']['runtime']['native'] = 'fixture'
    roster.write_text(json.dumps(value))
    profile_path = root/'profile.json'; profile_path.write_text(json.dumps(profile))
    client = NativeFixtureClient()
    class HttpFixture:
        def __new__(cls, *a, **kw): return client
    monkeypatch.setattr(native_models, 'NativeHttpClient', HttpFixture)
    plans = []
    def plan(*a, **kw):
        plans.append(1)
        return {'verdict': 'ACCEPT', 'findings': []}
    monkeypatch.setattr(native_work_host, 'plan_audit', plan)
    def inputs(tid):
        if not (relay_mesh.TRAINS_DIR/tid/'LANDING_EVIDENCE.json').exists():
            evidence(tid)
    monkeypatch.setattr(relay_mesh, 'materialize_admission_evidence', inputs)
    return root, repo, profile_path, client, plans


def run(host, *extra):
    import tare_ops
    return tare_ops.main(['run', 'work-one', '--repo-root', str(host[1]),
                          '--native-deployment', str(host[2]), '--unlimited', *extra])


def interrupt_after_git(patch, *, after_graph=False):
    if after_graph:
        original = relay_mesh.transition_train
        def stop(train_id, new_status, *args, **kwargs):
            if new_status == 'LANDED':
                raise SystemExit('graph saved; terminal transition interrupted')
            return original(train_id, new_status, *args, **kwargs)
        patch.setattr(relay_mesh, 'transition_train', stop)
    else:
        original = relay_mesh.relay_io.write_text_atomic
        def stop(path, *args, **kwargs):
            if Path(path) == relay_mesh.WORK_GRAPH_FILE:
                raise SystemExit('Git landed; graph write interrupted')
            return original(path, *args, **kwargs)
        patch.setattr(relay_mesh.relay_io, 'write_text_atomic', stop)


@pytest.mark.parametrize('cut', ['claim_ack', 'evidence', 'evidence_written', 'ready', 'landed',
                               'git_landed', 'graph_written'])
def test_public_resume_recovers_same_candidate_without_model_or_verifier_reexecution(host, monkeypatch, capsys, cut):
    import tare_ops
    from relay.runtime_authority import CoordinatorAuthorityGate
    from relay.native_settlement import NativeWorkRunner
    root, repo, _, client, plans = host
    graph = json.loads(relay_mesh.WORK_GRAPH_FILE.read_bytes())
    graph['nodes'][0]['metrics']['task_class'] = 'bounded-edit'
    relay_mesh.WORK_GRAPH_FILE.write_text(json.dumps(graph), encoding='utf-8')
    with monkeypatch.context() as fault:
        if cut == 'claim_ack':
            original = CoordinatorAuthorityGate.submit_terminal_claim
            def die(self, claim):
                assert original(self, claim)
                raise SystemExit('lost acceptance acknowledgement')
            fault.setattr(CoordinatorAuthorityGate, 'submit_terminal_claim', die)
        elif cut in {'evidence', 'evidence_written'}:
            original = relay_mesh.materialize_admission_evidence
            def die(tid):
                if cut == 'evidence_written': original(tid)
                raise SystemExit('host interrupted')
            fault.setattr(relay_mesh, 'materialize_admission_evidence', die)
        elif cut in {'git_landed', 'graph_written'}:
            interrupt_after_git(fault, after_graph=cut == 'graph_written')
        else:
            def die(*a, **kw): raise SystemExit('host interrupted')
            fault.setattr(relay_mesh if cut == 'ready' else work_run,
                          'land_train' if cut == 'ready' else 'refresh_checkout', die)
        with pytest.raises(SystemExit): run(host)
    capsys.readouterr()
    train = work_run.train_dir_for('work-one')
    origin = json.loads((train/'WORK_RUN.json').read_bytes())
    first = work_run.status('work-one')['last_run']
    assert first['task_class'] == origin['node']['metrics']['task_class'] == 'bounded-edit'
    assert json.loads(first['runtime_identity']) == {
        'deployment_id': 'fixture', 'requested_model': 'fixture', 'protocol': 'bedrock-converse'}
    assert first['recorded_audit_outcome'].startswith('plan_verdict=ACCEPT')
    custody = Path(origin['implementation_custody'])
    before = {n: (custody/n).read_bytes() for n in ('attempt.json', 'journal.jsonl')}
    candidate = json.loads((train/'CANDIDATE_CHANGE.json').read_bytes())['candidate_commit']
    verifier = (train/'NATIVE_VERIFIER_PROCESS.json').read_bytes()
    graph_before = relay_mesh.WORK_GRAPH_FILE.read_bytes()
    # Resume must not consult a deployment, acquire a new model binding or rerun generation/verifiers.
    def forbidden(*a, **kw): pytest.fail('recovery attempted new execution')
    from relay import native_catalog
    monkeypatch.setattr(native_catalog, 'select', forbidden)
    monkeypatch.setattr(CoordinatorAuthorityGate, 'acquire', forbidden)
    monkeypatch.setattr(NativeWorkRunner, 'run', forbidden)
    if cut in {'git_landed', 'graph_written'}:
        assert _git('rev-parse', 'HEAD', cwd=repo) == candidate
        # Git has the candidate; the Backlog does not say so yet (git_landed) or says so only once saved.
        assert (json.loads(graph_before)['nodes'][0]['work_status'] == 'done') == (cut == 'graph_written')
        assert json.loads((train/'STATE.json').read_bytes())['status'] == 'READY_FOR_LANDING'
        monkeypatch.setattr(relay_mesh, 'materialize_admission_evidence', forbidden)
        monkeypatch.setattr(relay_mesh, 'prepare_admission_verdict', forbidden)
        from relay import work_audit
        monkeypatch.setattr(work_audit, 'run', forbidden)
    graph_settled = None
    for attempt in range(2):
        assert tare_ops.main(['run', 'work-one', '--resume']) == 0, capsys.readouterr().out
        result = json.loads(capsys.readouterr().out)
        assert result['status'] == 'SETTLED'
        assert result['candidate_commit'] == candidate == _git('rev-parse', 'HEAD', cwd=repo)
        assert result['resumed_from'] == origin['run_id']
        assert result['task_class'] == 'bounded-edit'
        assert result['runtime_identity'] == first['runtime_identity']
        assert result['effective_limits']['implementation']['max_seconds'] is None
        assert result['recovery']['new_model_calls'] == result['recovery']['new_verifier_calls'] == 0
        if not attempt and cut in {'git_landed', 'graph_written'}:
            assert result['recovery']['backlog_reconciled'] is True
            assert result['recovery']['git_already_landed'] is True
        current_graph = relay_mesh.WORK_GRAPH_FILE.read_bytes()
        if cut == 'graph_written':
            assert current_graph == graph_before
        if graph_settled is not None:
            assert current_graph == graph_settled
        graph_settled = current_graph
    assert len(client.generations) == 5 and len(plans) == 1
    node = json.loads(relay_mesh.WORK_GRAPH_FILE.read_bytes())['nodes'][0]
    assert node['work_status'] == 'done' and node['completion']['status'] == 'DONE'
    assert len([n for n in node['notes'] if n.startswith('[RELAY_LANDED]')]) == 1  # completed once, never twice
    assert (repo/'src/value.txt').read_text() == 'strict\n'
    assert before == {n: (custody/n).read_bytes() for n in before}
    assert (work_run.train_dir_for('work-one')/'NATIVE_VERIFIER_PROCESS.json').read_bytes() == verifier


@pytest.mark.parametrize('fault', ['candidate', 'journal', 'identity', 'target', 'lease', 'criteria', 'origin', 'descriptor_graph'])
def test_public_resume_refuses_changed_evidence_without_landing(host, monkeypatch, capsys, fault):
    import tare_ops
    from relay.runtime_authority import CoordinatorAuthorityGate
    def stop(*a, **kw): raise SystemExit('interrupt')
    with monkeypatch.context() as cut:
        if fault == 'no_claim':
            cut.setattr(CoordinatorAuthorityGate, 'submit_terminal_claim', stop)
        else:
            cut.setattr(relay_mesh, 'materialize_admission_evidence', stop)
        with pytest.raises(SystemExit): run(host)
    capsys.readouterr()
    train = work_run.train_dir_for('work-one')
    if fault in {'candidate', 'identity'}:
        path = train/('CANDIDATE_CHANGE.json' if fault == 'candidate' else 'IMPLEMENTER_IDENTITY.json')
        value = json.loads(path.read_bytes())
        value['worktree_id' if fault == 'candidate' else 'agent_id'] = 'changed'
        from relay.train_execution import canonical_json_bytes
        path.write_bytes(canonical_json_bytes(value))
    elif fault == 'journal':
        origin = json.loads((train/'WORK_RUN.json').read_bytes())
        with (Path(origin['implementation_custody'])/'journal.jsonl').open('ab') as stream: stream.write(b'\n')
    elif fault == 'target':
        (host[1]/'new.txt').write_text('another change')
        _git('add', '.', cwd=host[1]); _git('commit', '-qm', 'another commit', cwd=host[1])
    elif fault == 'lease':
        assert relay_mesh.acquire_lease('RUN-work-one', 'another-worker', 'auditor')
        capsys.readouterr()
    elif fault == 'criteria':
        graph = json.loads(relay_mesh.WORK_GRAPH_FILE.read_bytes())
        graph['nodes'][0]['exit_criteria'] = ['new requirement']
        relay_mesh.WORK_GRAPH_FILE.write_text(json.dumps(graph))
    elif fault == 'origin':  # the Work binding saved at the start no longer names this descriptor
        path = train/'WORK_RUN.json'
        value = json.loads(path.read_bytes()); value['descriptor_sha256'] = 'f'*64
        path.write_text(json.dumps(value))
    elif fault == 'descriptor_graph':
        from relay.train_execution import canonical_json_bytes
        path = train/'EXECUTION_DESCRIPTOR.json'
        value = json.loads(path.read_bytes()); value['work_graph'] = 'other-graph.json'
        path.write_bytes(canonical_json_bytes(value))
    expected = _git('rev-parse', 'HEAD', cwd=host[1])
    assert tare_ops.main(['run', 'work-one', '--resume']) == 1
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'FAILED' and result['error']
    named = {'origin': 'WORK_RESUME_IDENTITY_CHANGED', 'descriptor_graph': 'WORK_RESUME_GRAPH_CHANGED',
             'lease': 'WORK_RESUME_LEASE_CHANGED'}
    assert named.get(fault, '') in result['error']
    assert expected == _git('rev-parse', 'HEAD', cwd=host[1])
    assert len(host[3].generations) == 5


def test_resume_keeps_operator_edit_and_refuses_changed_limits(host, monkeypatch, capsys):
    import tare_ops
    with monkeypatch.context() as cut:
        def stop(*a): raise SystemExit('interrupt')
        cut.setattr(relay_mesh, 'materialize_admission_evidence', stop)
        with pytest.raises(SystemExit): run(host)
    capsys.readouterr()
    (host[1]/'src/value.txt').write_text('operator WIP\n')
    assert tare_ops.main(['run', 'work-one', '--resume', '--max-seconds', '100']) == 2
    assert 'WORK_RESUME_REUSES_ORIGINAL_EXECUTION_OPTIONS' in capsys.readouterr().out
    assert tare_ops.main(['run', 'work-one', '--resume']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['landing']['checkout']['stale'] == ['src']
    assert (host[1]/'src/value.txt').read_text() == 'operator WIP\n'


@pytest.mark.parametrize('fault', ['target', 'missing_capability', 'changed_capability',
                                  'verdict', 'evidence', 'candidate', 'criteria', 'lease',
                                  'conflicting_completion'])
def test_resume_after_git_landing_refuses_changed_authority(host, monkeypatch, capsys, fault):
    import tare_ops
    with monkeypatch.context() as cut:
        interrupt_after_git(cut, after_graph=fault == 'conflicting_completion')
        with pytest.raises(SystemExit):
            run(host)
    capsys.readouterr()
    train = work_run.train_dir_for('work-one')
    candidate = json.loads((train/'CANDIDATE_CHANGE.json').read_bytes())['candidate_commit']
    assert _git('rev-parse', 'HEAD', cwd=host[1]) == candidate
    if fault == 'target':
        base = json.loads((train/'EXECUTION_DESCRIPTOR.json').read_bytes())['base_commit']
        tree = _git('rev-parse', candidate+'^{tree}', cwd=host[1])
        unrelated = _git('commit-tree', tree, '-p', base, '-m', 'divergent target', cwd=host[1])
        _git('update-ref', 'refs/heads/main', unrelated, candidate, cwd=host[1])
    elif fault == 'missing_capability':
        relay_mesh._capability_file('RUN-work-one').unlink()
    elif fault in {'changed_capability', 'verdict', 'evidence', 'candidate'}:
        path = (relay_mesh._capability_file('RUN-work-one') if fault == 'changed_capability'
                else train/ {'verdict': 'ADMISSION_VERDICT.json',
                             'evidence': 'LANDING_EVIDENCE.json',
                             'candidate': 'CANDIDATE_CHANGE.json'}[fault])
        value = json.loads(path.read_bytes())
        value['readyRevision' if fault == 'changed_capability' else 'tampered'] = -1
        path.write_text(json.dumps(value))
    elif fault == 'lease':
        assert relay_mesh.acquire_lease('RUN-work-one', 'other-worker', 'auditor')
        capsys.readouterr()
    else:
        graph = json.loads(relay_mesh.WORK_GRAPH_FILE.read_bytes())
        node = graph['nodes'][0]
        if fault == 'criteria':
            node['exit_criteria'] = ['changed criterion']
        else:
            node['completion']['evidence'] = 'completed by another train'
        relay_mesh.WORK_GRAPH_FILE.write_text(json.dumps(graph))
    before_graph = relay_mesh.WORK_GRAPH_FILE.read_bytes()
    before_target = _git('rev-parse', 'HEAD', cwd=host[1])
    assert tare_ops.main(['run', 'work-one', '--resume']) == 1
    result = json.loads(capsys.readouterr().out)
    assert result['status'] in {'FAILED', 'LANDING_REFUSED'}
    assert relay_mesh.WORK_GRAPH_FILE.read_bytes() == before_graph
    assert _git('rev-parse', 'HEAD', cwd=host[1]) == before_target
    assert json.loads((train/'STATE.json').read_bytes())['status'] == 'READY_FOR_LANDING'
    assert len(host[3].generations) == 5 and len(host[4]) == 1


def test_run_lock_refuses_concurrent_host(os_root, tmp_path):
    import hashlib
    from compute_plane.native_journal import custody_lock
    root = os_root[0].parent/'.reconciliation/work-runs/locks'/hashlib.sha256(b'work-one').hexdigest()
    root.mkdir(parents=True)
    with custody_lock(root):
        result = work_run.run_work('work-one', resume=True)
    assert result['error'] == 'ValueError:native_custody_has_active_writer'


def test_resume_in_new_process_after_acceptance_acknowledgement_loss(host, monkeypatch, capsys):
    import os
    import subprocess
    import sys
    from relay.runtime_authority import CoordinatorAuthorityGate
    original = CoordinatorAuthorityGate.submit_terminal_claim
    with monkeypatch.context() as cut:
        def stop(self, claim):
            assert original(self, claim)
            raise SystemExit('lost acknowledgement')
        cut.setattr(CoordinatorAuthorityGate, 'submit_terminal_claim', stop)
        with pytest.raises(SystemExit): run(host)
    capsys.readouterr()
    code = '''import sys
from pathlib import Path
from relay import relay_mesh,work_run
relay_mesh.rebind_root(Path(sys.argv[1]));sys.modules['relay_mesh']=relay_mesh
from test_work_run import evidence
work_run.eligibility_check=lambda *a:None
relay_mesh.materialize_admission_evidence=evidence
import tare_ops
raise SystemExit(tare_ops.main(['run','work-one','--resume']))
'''
    result = subprocess.run([sys.executable, '-X', 'utf8', '-B', '-c', code, str(host[0])],
        cwd=host[0].parent, env={**os.environ, 'PYTHONPATH': os.pathsep.join(str(p) for p in sys.path)},
        capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)['status'] == 'SETTLED'
    assert (host[1]/'src/value.txt').read_text() == 'strict\n'
    assert len(host[3].generations) == 5


@pytest.mark.parametrize('fault', ['none', 'descendant', 'missing_completion', 'foreign_completion',
                                   'missing_node', 'not_in_history', 'forged_signoff', 'duplicate_node',
                                   'missing_note', 'malformed_graph'])
def test_resume_after_confirmed_landing_returns_result_or_refuses_inconsistent_evidence(host, monkeypatch, capsys, fault):
    import tare_ops
    from relay import native_catalog, work_audit
    from relay.native_settlement import NativeWorkRunner
    from relay.train_execution import read_execution_descriptor
    assert run(host) == 0
    capsys.readouterr()
    train = work_run.train_dir_for('work-one')
    assert json.loads((train/'STATE.json').read_bytes())['status'] == 'LANDED'
    candidate = json.loads((train/'CANDIDATE_CHANGE.json').read_bytes())['candidate_commit']
    repo = host[1]
    if fault == 'descendant':
        _git('commit', '--allow-empty', '-qm', 'later work', cwd=repo)
    elif fault == 'not_in_history':
        base = read_execution_descriptor(train/'EXECUTION_DESCRIPTOR.json').base_commit
        _git('reset', '-q', '--hard', base, cwd=repo)
    elif fault != 'none':
        graph = json.loads(relay_mesh.WORK_GRAPH_FILE.read_bytes())
        node = next(n for n in graph['nodes'] if n.get('id') == 'work-one')
        if fault == 'missing_node':
            graph['nodes'] = [n for n in graph['nodes'] if n.get('id') != 'work-one']
        elif fault == 'missing_completion':
            node['completion'] = {'status': 'NOT_DONE'}
        elif fault == 'forged_signoff':
            node['completion']['evidence'] = node['completion']['evidence'].replace('SMART_POLICY', 'FORGED')
            node['notes'] = [n.replace('SMART_POLICY', 'FORGED') for n in node['notes']]
        elif fault == 'duplicate_node':
            graph['nodes'].append(dict(node))
        elif fault == 'missing_note':
            node['notes'] = []
        elif fault == 'malformed_graph':
            graph = {'nodes': 'not a node list'}
        else:
            node['completion']['evidence'] = 'Landed via Multi-Agent Relay Train RUN-other (SMART_POLICY)'
        relay_mesh.WORK_GRAPH_FILE.write_text(json.dumps(graph))
    def forbidden(*a, **kw): pytest.fail('confirmed landing was executed again')
    for owner, name in ((relay_mesh, 'land_train'), (relay_mesh, 'admit_train'),
                        (relay_mesh, 'materialize_admission_evidence'), (relay_mesh, 'prepare_admission_verdict'),
                        (work_audit, 'run'), (native_catalog, 'select'), (NativeWorkRunner, 'run')):
        monkeypatch.setattr(owner, name, forbidden)
    before_graph = relay_mesh.WORK_GRAPH_FILE.read_bytes()
    before_target = _git('rev-parse', 'HEAD', cwd=repo)
    for _ in range(2):
        code = tare_ops.main(['run', 'work-one', '--resume'])
        result = json.loads(capsys.readouterr().out)
        if fault in {'none', 'descendant'}:
            assert code == 0 and result['status'] == 'SETTLED', result
            assert result['candidate_commit'] == candidate
            assert result['landing']['already_landed'] is True
            assert result['recovery']['new_model_calls'] == result['recovery']['new_verifier_calls'] == 0
        else:
            assert code == 1 and result['status'] == 'FAILED', result
            expected = 'WORK_GRAPH_INVALID' if fault == 'malformed_graph' else 'WORK_RESUME_RECONCILIATION_REQUIRED'
            assert expected in result['error']
        assert relay_mesh.WORK_GRAPH_FILE.read_bytes() == before_graph
        assert _git('rev-parse', 'HEAD', cwd=repo) == before_target
    assert len(host[3].generations) == 5 and len(host[4]) == 1


@pytest.mark.parametrize('status', ['EXHAUSTED', 'CANCELLED'])
@pytest.mark.parametrize('case', ['normal', 'clean_model', 'unknown_cleanup',
                                  'pending_tool', 'unknown_tool', 'active',
                                  'writable', 'terminal', 'verifiers', 'wrong_role',
                                  'missing_recovery'])
def test_explicit_ended_plan_review_keeps_old_custody(tmp_path, monkeypatch, status, case):
    from types import SimpleNamespace
    from compute_plane import native_journal
    from relay import native_model_recovery
    from relay.work_recovery import new_plan_attempt

    previous = tmp_path/'plan-audit'
    previous.mkdir()
    config = {'role': 'plan-auditor', 'writable': [], 'terminal': False, 'verifiers': []}
    if case in {'writable', 'terminal', 'verifiers'}:
        config[case] = ['forbidden']
    if case == 'wrong_role':
        config['role'] = 'implementer'
    state = {'session_id': 'old', 'config': config,
             'invocations': [{'ended_at': None if case == 'active' else 123}],
             'deadline_utc': 100, 'model_calls': 3}
    outcome = {'status': status, 'reason': 'DEADLINE', 'model_calls': 3}
    events = []
    if case != 'normal':
        outcome['pending_id'] = 'old/model/3'
        events.append({'kind': 'model_requested', 'call_id': 'old/model/3'})
    if case in {'pending_tool', 'unknown_tool'}:
        events.insert(0, {'kind': 'effect_requested', 'effect_id': 'tool/1'})
    if case == 'unknown_tool':
        events.insert(1, {'kind': 'effect_returned',
                          'result': {'effect_id': 'tool/1', 'status': 'unknown'}})
    for name, value in [('native-agent.json', state), ('outcome.json', outcome)]:
        (previous/name).write_text(json.dumps(value))
    (previous/'journal.jsonl').write_text('original journal')
    (previous/'observations.jsonl').write_text('original observations')
    with native_journal.custody_lock(previous):
        pass
    before = {p.name: p.read_bytes() for p in previous.iterdir()}
    limits = {'plan': {'max_seconds': 60, 'max_model_calls': 3}}
    origin = {'plan_custody': str(previous), 'effective_limits': limits,
              'command_protection': {'enabled': True},
              'command_protection_sha256': 'frozen', 'plan_audit_attempts': ['older']}
    (tmp_path/'WORK_RUN.json').write_text(json.dumps(origin))
    original_origin = json.loads(json.dumps(origin))
    original_work = (tmp_path/'WORK_RUN.json').read_bytes()

    class Journal:
        def __init__(self, path):
            self.path = path
        def snapshot(self):
            return SimpleNamespace(records=[SimpleNamespace(event=lambda e=e: e) for e in events])
    monkeypatch.setattr(native_journal, 'NativeJournal', Journal)
    monkeypatch.setattr(native_journal, 'NativeObservationJournal', Journal)
    def inspect(journal, observations, session_id, **kwargs):
        assert session_id == 'old'
        assert kwargs['deadline_utc'] is None and kwargs['max_model_calls'] is None
        if case in {'normal', 'missing_recovery'}:
            return None
        return {'local_cleanup_verified': case != 'unknown_cleanup',
                'transport_eligible': False, 'reason': 'UNSUPPORTED_INTERRUPTION'}
    monkeypatch.setattr(native_model_recovery, 'inspect', inspect)

    if case in {'normal', 'clean_model'}:
        new_plan_attempt(tmp_path, origin)
        assert origin['plan_custody'] != str(previous)
        assert origin['plan_audit_attempts'] == ['older', str(previous)]
        assert {k: v for k, v in origin.items() if k not in {'plan_custody', 'plan_audit_attempts'}} == {
            k: v for k, v in original_origin.items() if k not in {'plan_custody', 'plan_audit_attempts'}}
        assert json.loads((tmp_path/'WORK_RUN.json').read_bytes()) == origin
    else:
        with pytest.raises(ValueError, match='PLAN_RETRY_REQUIRES_FINISHED_ATTEMPT'):
            new_plan_attempt(tmp_path, origin)
        assert origin == original_origin
        assert (tmp_path/'WORK_RUN.json').read_bytes() == original_work
    assert before == {p.name: p.read_bytes() for p in previous.iterdir()}


def test_run_refuses_a_candidate_receipt_changed_after_native_acceptance(host, monkeypatch, capsys):
    from relay.native_settlement import NativeWorkRunner
    from relay.train_execution import canonical_json_bytes
    original = NativeWorkRunner.run
    def run_then_change_receipt(self, **kw):
        outcome = original(self, **kw)
        path = kw['train_dir']/'CANDIDATE_CHANGE.json'
        value = json.loads(path.read_bytes())
        value['worktree_id'] = 'another-worktree'  # still verifies against Git, but is no longer the accepted bytes
        path.write_bytes(canonical_json_bytes(value))
        return outcome
    monkeypatch.setattr(NativeWorkRunner, 'run', run_then_change_receipt)
    base = _git('rev-parse', 'HEAD', cwd=host[1])
    assert run(host) == 1
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'FAILED' and 'NATIVE_SETTLEMENT_CANDIDATE_CHANGED' in result['error']
    assert result['candidate_commit'] is None and _git('rev-parse', 'HEAD', cwd=host[1]) == base


def test_resume_refuses_another_repository_than_the_original(host, monkeypatch, capsys, tmp_path):
    import tare_ops
    with monkeypatch.context() as cut:
        def stop(*a): raise SystemExit('interrupt')
        cut.setattr(relay_mesh, 'materialize_admission_evidence', stop)
        with pytest.raises(SystemExit): run(host)
    capsys.readouterr()
    before = _git('rev-parse', 'HEAD', cwd=host[1])
    assert tare_ops.main(['run', 'work-one', '--resume', '--repo-root', str(tmp_path/'elsewhere')]) == 1
    assert 'WORK_RESUME_REPOSITORY_CHANGED' in json.loads(capsys.readouterr().out)['error']
    assert _git('rev-parse', 'HEAD', cwd=host[1]) == before and len(host[3].generations) == 5


def test_resume_stops_before_landing_when_admission_is_refused(host, monkeypatch, capsys):
    import tare_ops
    with monkeypatch.context() as cut:
        def stop(*a): raise SystemExit('interrupt')
        cut.setattr(relay_mesh, 'materialize_admission_evidence', stop)
        with pytest.raises(SystemExit): run(host)
    capsys.readouterr()
    base = _git('rev-parse', 'HEAD', cwd=host[1])
    monkeypatch.setattr(relay_mesh, 'materialize_admission_evidence', lambda tid: evidence(tid, ('cas_ready',)))
    assert tare_ops.main(['run', 'work-one', '--resume']) == 1
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'ADMISSION_REFUSED' and 'CHECK_FAILED:cas_ready' in result['admission']['reason_codes']
    assert not result['landing'] and _git('rev-parse', 'HEAD', cwd=host[1]) == base
    assert json.loads((work_run.train_dir_for('work-one')/'STATE.json').read_bytes())['status'] == 'AUDIT_PENDING'
    assert len(host[3].generations) == 5
