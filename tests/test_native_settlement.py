"""Real Git/coordinator/capabilities; scripted model, existing settlement policy."""
import json
from dataclasses import asdict, replace
import os
import subprocess
import sys
from pathlib import Path

import pytest

from test_work_run import os_root, native_fixture_arguments, evidence, _git
from relay import relay_mesh, work_run
from relay.native_settlement import NativeWorkRunner


def runner_for(tmp_path, node):
    from relay.agent_runtime_host import CoordinatorAuthorityGate
    from relay.cluster_coordinator import CoordinatorDB
    from relay.landing_policy import attest_identity
    args = native_fixture_arguments(tmp_path, node)
    args.pop('journal')
    gate = CoordinatorAuthorityGate(CoordinatorDB(tmp_path / 'coordinator.db'))
    binding = gate.acquire(node['id'], 'native-test', ttl_s=120)
    args['control'] = replace(args['control'], binding=binding)
    roster = relay_mesh.RELAY_DIR / 'round_table_roster.json'
    value = json.loads(roster.read_bytes())
    value['models']['qwen-3.8-27b']['runtime']['native'] = args['profile'].model
    roster.write_text(json.dumps(value), encoding='utf-8')
    identity = attest_identity(roster_path=roster, agent_id='native-test', role='implementer',
        model_id='qwen-3.8-27b', runtime_key='native', runtime_model=args['profile'].model)
    return NativeWorkRunner(authority_gate=gate, identity=identity, settings=args)


def test_native_proposal_settles_exact_candidate_and_keeps_native_identity(os_root, tmp_path):
    _, repo = os_root
    node = work_run.load_node(relay_mesh.WORK_GRAPH_FILE, 'work-one')
    runner = runner_for(tmp_path, node)
    result = work_run.run_work('work-one', repository_root=repo, native_runner=runner,
        plan_auditor=lambda *a: {'verdict':'ACCEPT','findings':[]}, evidence_factory=evidence)
    assert result['status'] == 'SETTLED', result
    assert result['execution'] == 'native' and result['deployment'] is None
    train = work_run.train_dir_for('work-one')
    accepted = json.loads((train/'NATIVE_ACCEPTANCE.json').read_bytes())
    assert accepted['candidate_commit'] == result['candidate_commit'] == _git('rev-parse','HEAD',cwd=repo)
    assert accepted['claim']['status'] == 'PROPOSED'
    assert json.loads((train/'IMPLEMENTER_IDENTITY.json').read_bytes())['runtime_key'] == 'native'
    assert (repo/'src/value.txt').read_text() == 'strict\n'
    assert not runner.authority_gate.is_current(runner.settings['control'].binding)
    assert len(runner.settings['client'].generations) == 5
    from scripts.round_table.report_round_table_telemetry import collect
    report = collect([work_run.receipt_path(result['run_id']), runner.settings['custody']])
    attempt, = report['work_runs']
    assert attempt['settlement_confirmed'] and attempt['inference_link'] == 'recorded'
    assert attempt['inference_usage']['calls'] == 5
    assert attempt['inference_usage']['unknown_cost_calls'] == 5
    assert attempt['inference_usage']['estimated_cost_usd'] is None
    assert {row['work_run_id'] for row in report['rows']} == {result['run_id']}
    assert not report['warnings']


def test_managed_work_schedules_challenger_and_keeps_real_work_receipts(os_root, tmp_path, monkeypatch):
    import copy
    import psutil
    from relay import challenger
    from relay.challenger_workspace import tree
    from relay.native_agent import read_json
    from compute_plane.native_journal import save_json
    from test_native_agent import deployment
    _, repo = os_root
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'experimental-state'))
    monkeypatch.setattr('relay.native_catalog.select', lambda *a, **kw: deployment(endpoint='http://127.0.0.1:8081'))
    monkeypatch.setattr(challenger, 'spawn', lambda job: (os.getpid(), psutil.Process().create_time()))
    settings = copy.deepcopy(challenger.DEFAULT)
    settings.update(enabled=True, models=['fixture'], local_percent=100)
    challenger.configure(repo, settings)
    node = work_run.load_node(relay_mesh.WORK_GRAPH_FILE, 'work-one')
    runner = runner_for(tmp_path, node)
    custody = runner.settings['custody']
    baseline = deployment('bedrock-converse')
    baseline['native_profile'] = asdict(runner.settings['profile'])
    save_json(custody/'host.json', {'session_id': runner.settings['session_id'], 'deployment': baseline})
    result = work_run.run_work('work-one', repository_root=repo, native_runner=runner,
        plan_auditor=lambda *a: {'verdict':'ACCEPT','findings':[]}, evidence_factory=evidence)
    assert result['status'] == 'SETTLED'
    marker = read_json(custody/'challenger.json')
    assert marker['status'] == 'RUNNING', marker
    job = Path(marker['job'])
    assert (tree(job, 'candidate')/'src/value.txt').read_text() != (repo/'src/value.txt').read_text()
    assert (tree(job, 'reference')/'src/value.txt').read_text() == 'strict\n'
    state = challenger.source_state(custody)
    assert 'work-one' in state['instruction'] and len(state['invocations']) == 1
    assert not (custody/'native-agent.json').exists()
    report = challenger.inspect(repo)
    assert report['runs'][0]['managed_usage']['generation_intents'] == 5


def prepared(os_root, tmp_path):
    from test_candidate_change import _write_ready
    from relay.train_execution import parse_execution_descriptor, canonical_json_bytes
    from compute_plane.workspace import provision_linked_worktree
    _, repo = os_root
    node = work_run.load_node(relay_mesh.WORK_GRAPH_FILE, 'work-one')
    runner = runner_for(tmp_path, node)
    descriptor = parse_execution_descriptor(canonical_json_bytes(work_run.derive_descriptor(
        node, train_id='RUN-work-one', repository_root=repo)))
    train = tmp_path/'train'; train.mkdir()
    lease = provision_linked_worktree(repo, tmp_path/'candidate-worktree',
        base_commit=descriptor.base_commit, worktree_id='native-candidate-test')
    _write_ready(train, descriptor, lease)
    return runner, dict(train_dir=train, descriptor=descriptor, lease=lease, node=node)


@pytest.mark.parametrize('cleanup,limit', [(True, 6), (False, 6), (True, 4)])
def test_work_resume_recovers_only_clean_model_interruption(os_root, tmp_path, cleanup, limit):
    from compute_plane.native_journal import NativeJournal
    from compute_plane.native_models import NativeHttpClient, NativeTransportInterrupted
    from test_work_run import NativeFixtureClient
    runner, args = prepared(os_root, tmp_path)

    class Interrupted(NativeFixtureClient, NativeHttpClient):
        interrupted = False

        def post_observed(self, path, body, control):
            if not path.endswith('/count-tokens') and len(self.generations) == 3 and not self.interrupted:
                self.interrupted = True
                raise NativeTransportInterrupted('HTTP_INCOMPLETE_STREAM', {
                    'started': True, 'cleanup_verified': cleanup, 'remote_stop': 'unknown',
                    'http_attempts_upper_bound': 1})
            return self.post(path, body, control), {'started': True, 'cleanup_verified': True,
                'remote_stop': 'unknown', 'http_attempts_upper_bound': 1}

    client = Interrupted()
    custody = runner.settings['custody']
    runner.settings.update(client=client, observe=NativeJournal(custody/'observations.jsonl').append,
        budget=replace(runner.settings['budget'], max_model_calls=limit))
    first = runner.run(**args)
    assert first['outcome']['reason'] == 'UNFINISHED_INVOCATION'
    assert (args['lease'].root/'src/value.txt').read_text() == 'strict\n'
    deadline = json.loads((custody/'attempt.json').read_bytes())['deadline_utc']
    result = runner.run(**args, resume=True)
    events = [r.event() for r in NativeJournal(custody/'journal.jsonl').snapshot().records]
    recovered = cleanup and limit > 4
    assert len([e for e in events if e['kind'] == 'model_abandoned']) == int(recovered)
    assert len([e for e in events if e['kind'] == 'effect_requested' and e['tool_call']['name'] == 'edit']) == 1
    assert json.loads((custody/'attempt.json').read_bytes())['deadline_utc'] == deadline
    assert result['outcome']['model_calls'] == (6 if recovered else 4)
    assert result['status'] == ('PROPOSED' if recovered else 'RECONCILIATION_REQUIRED')


@pytest.mark.parametrize('cut', ['pin', 'observation', 'claim_ack', 'acceptance'])
def test_native_resume_crash_cuts_reuse_candidate_without_more_model_calls(os_root, tmp_path, monkeypatch, cut):
    from compute_plane import candidate_git, native_journal
    runner, args = prepared(os_root, tmp_path)
    if cut == 'pin':
        original = candidate_git.pin_candidate_commit
        def fail(*a, **kw):
            original(*a, **kw)
            raise SystemExit('crash after pin')
        monkeypatch.setattr(candidate_git, 'pin_candidate_commit', fail)
    elif cut in {'observation','acceptance'}:
        original = native_journal.save_json
        filename = 'EXECUTOR_OBSERVATION.json' if cut == 'observation' else 'NATIVE_ACCEPTANCE.json'
        def fail(path, value):
            original(path, value)
            if path.name == filename:
                raise SystemExit('crash after durable write')
        monkeypatch.setattr(native_journal, 'save_json', fail)
    else:
        original = runner.authority_gate.submit_terminal_claim
        def fail(claim):
            assert original(claim)
            raise SystemExit('lost database commit acknowledgement')
        monkeypatch.setattr(runner.authority_gate, 'submit_terminal_claim', fail)
    with pytest.raises(SystemExit):
        runner.run(**args)
    candidate = _git('rev-parse','HEAD',cwd=args['lease'].root)
    monkeypatch.undo()
    result = runner.run(**args, resume=True)
    assert result['status'] == 'PROPOSED'
    assert result['settlement']['candidate_commit'] == candidate
    assert len(runner.settings['client'].generations) == 5
    again = runner.run(**args, resume=True)
    assert again['settlement'] == result['settlement']


@pytest.mark.parametrize('fault', ['revoked','cancelled','changed_bytes'])
def test_native_candidate_refuses_late_or_changed_proposal(os_root, tmp_path, monkeypatch, fault):
    from compute_plane import candidate_git
    runner, args = prepared(os_root, tmp_path)
    original = candidate_git.prepare_candidate_commit
    def interrupted(*a, **kw):
        result = original(*a, **kw)
        if fault == 'revoked':
            assert runner.authority_gate.revoke(runner.settings['control'].binding, reason='late-result')
        elif fault == 'cancelled':
            runner.settings['control'].cancellation.cancel()
        else:
            (args['lease'].root/'src/value.txt').write_text('different\n')
        return result
    monkeypatch.setattr(candidate_git, 'prepare_candidate_commit', interrupted)
    with pytest.raises((ValueError, RuntimeError), match='CONTROL_STOPPED|BYTES_CHANGED'):
        runner.run(**args)
    assert not (args['train_dir']/'CANDIDATE_CHANGE.json').exists()
    assert _git('rev-parse','HEAD',cwd=args['lease'].repository_root) == args['descriptor'].base_commit


def test_accepted_claim_does_not_authorize_a_different_candidate(os_root, tmp_path):
    runner, args = prepared(os_root, tmp_path)
    runner.run(**args)
    path = args['train_dir']/'CANDIDATE_CHANGE.json'
    value = json.loads(path.read_bytes())
    value['worktree_id'] = 'another-worktree'
    from relay.train_execution import canonical_json_bytes
    path.write_bytes(canonical_json_bytes(value))
    with pytest.raises(ValueError, match='CONTROL_STOPPED'):
        runner.run(**args, resume=True)
    assert len(runner.settings['client'].generations) == 5


def test_process_death_after_claim_commit_recovers_without_second_emitter(os_root, tmp_path):
    from relay.native_attempt import run_native_attempt
    runner, args = prepared(os_root, tmp_path)
    settings = runner.settings
    assert run_native_attempt(authority_gate=runner.authority_gate, **(settings | {
        'worktree':args['lease'].root, 'descriptor':args['descriptor'], 'node':args['node']}))['status'] == 'PROPOSED'
    config = {k:asdict(settings[k]) for k in ('profile','budget','context_policy')}
    config.update(session_id=settings['session_id'], custody=str(settings['custody']),
        specgraph_edit_context=False, binding=asdict(settings['control'].binding),
        deadline=settings['control'].deadline_monotonic, descriptor=args['descriptor'].canonical_bytes.decode(),
        lease={k:str(v) if isinstance(v,Path) else v for k,v in asdict(args['lease']).items()},
        node=args['node'], train_dir=str(args['train_dir']), os_root=str(os_root[0]),
        db=str(tmp_path/'coordinator.db'), identity=dict(runner.identity.payload))
    code = '''import os,sys,json,hashlib
from pathlib import Path
from types import SimpleNamespace
c=json.load(sys.stdin)
from relay import relay_mesh
relay_mesh.rebind_root(Path(c['os_root']));sys.modules['relay_mesh']=relay_mesh
from relay.cluster_coordinator import CoordinatorDB
from relay.agent_runtime_host import CoordinatorAuthorityGate
from relay.native_settlement import NativeWorkRunner
from relay.landing_policy import AgentIdentity
from relay.train_execution import canonical_json_bytes,parse_execution_descriptor
from compute_plane.workspace import WorkspaceLease
from compute_plane.native_models import ModelProfile
from tare_tools_agent_runtime import BindingRef,CancellationToken,ExecutionControl,NativeBudget,ContextPolicy
gate=CoordinatorAuthorityGate(CoordinatorDB(Path(c['db'])))
submit=gate.submit_terminal_claim
def die(claim):
    if not submit(claim):raise RuntimeError('claim refused')
    os._exit(79)
gate.submit_terminal_claim=die
raw=canonical_json_bytes(c['identity'])
identity=AgentIdentity(c['identity'],raw,hashlib.sha256(raw).hexdigest())
settings=dict(session_id=c['session_id'],custody=Path(c['custody']),profile=ModelProfile(**c['profile']),
 budget=NativeBudget(**c['budget']),context_policy=ContextPolicy(**c['context_policy']),
 control=ExecutionControl(BindingRef(**c['binding']),CancellationToken(),c['deadline']),
 client=None,specgraph_edit_context=False)
lease=WorkspaceLease(**{k:Path(v) if k in {'root','repository_root','git_common_dir','git_admin_dir'} else v for k,v in c['lease'].items()})
NativeWorkRunner(authority_gate=gate,identity=identity,settings=settings).run(
 train_dir=Path(c['train_dir']),descriptor=parse_execution_descriptor(c['descriptor'].encode()),lease=lease,node=c['node'],resume=True)
'''
    env = os.environ.copy()
    env['PYTHONPATH'] = os.pathsep.join(sys.path)
    child = subprocess.run([sys.executable,'-B','-c',code], input=json.dumps(config), text=True,
                           capture_output=True, env=env, timeout=60)
    assert child.returncode == 79, child.stderr
    assert not (args['train_dir']/'NATIVE_ACCEPTANCE.json').exists()
    result = runner.run(**args, resume=True)
    assert result['settlement']['status'] == 'CANDIDATE_ACCEPTED'
    assert len(settings['client'].generations) == 5
    assert not runner.authority_gate.is_current(settings['control'].binding)
