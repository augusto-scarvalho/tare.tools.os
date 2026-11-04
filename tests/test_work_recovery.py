"""Recovery before acceptance: real tools/Git/coordinator; deterministic model and clock."""
import json
from pathlib import Path

import pytest
from test_work_resume import host, run
from test_work_run import os_root
from relay import relay_mesh, work_run
from relay import native_work_credentials
REAL_VAULT = {name: getattr(native_work_credentials, name) for name in ('read', 'write', 'discard')}


@pytest.mark.parametrize('cut', ['read', 'edit', 'edit_receipt', 'verify', 'pin', 'claim', 'workspace', 'plan', 'before_plan'])
def test_resume_before_acceptance_preserves_session_effects_and_limits(host, monkeypatch, capsys, cut):
    from compute_plane.native_journal import NativeJournal
    from relay import native_work_host, strict_workspace
    from relay.runtime_authority import CoordinatorAuthorityGate
    import tare_ops
    with monkeypatch.context() as fault:
        if cut in {'read', 'edit', 'edit_receipt', 'verify'}:
            append = NativeJournal.append
            def stop(journal, event):
                if cut == 'edit_receipt' and event['kind'] == 'effect_returned' and len(host[3].generations) == 3:
                    raise SystemExit('process interrupted before recording completed edit')
                result = append(journal, event)
                wanted = {'read': 1, 'edit': 3, 'edit_receipt': 3, 'verify': 4}[cut]
                if event['kind'] == 'effect_returned' and len(host[3].generations) == wanted:
                    raise SystemExit('process interrupted after recorded effect')
                return result
            fault.setattr(NativeJournal, 'append', stop)
        elif cut in {'pin', 'claim', 'workspace', 'before_plan'}:
            if cut == 'pin':
                from compute_plane import candidate_git
                owner, name = candidate_git, 'pin_candidate_commit'
            elif cut == 'claim': owner, name = CoordinatorAuthorityGate, 'submit_terminal_claim'
            elif cut == 'workspace': owner, name = strict_workspace, '_write_create_only'
            else: owner, name = native_work_host, 'plan_audit'
            original = getattr(owner, name)
            def stop(*args, **kw):
                if cut == 'pin': original(*args, **kw)
                if cut == 'workspace' and not args[0].name.startswith('WORKSPACE_READY.'):
                    return original(*args, **kw)
                raise SystemExit('interrupted')
            fault.setattr(owner, name, stop)
        if cut == 'plan':
            assert run(host, '--dry-run') == 0
        else:
            with pytest.raises(SystemExit): run(host)
    capsys.readouterr()
    train = work_run.train_dir_for('work-one')
    origin = json.loads((train/'WORK_RUN.json').read_bytes())
    custody = Path(origin['implementation_custody'])
    previous = json.loads((custody/'attempt.json').read_bytes()) if (custody/'attempt.json').exists() else None
    before_calls = len(host[3].generations)
    assert tare_ops.main(['run', 'work-one', '--resume']) == 0, capsys.readouterr().out
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'SETTLED'
    assert result['resumed_from'] == origin['run_id']
    assert result['resumed_execution']['model_calls_total'] == 5
    assert len(host[3].generations) == 5
    assert (host[1]/'src/value.txt').read_text() == 'strict\n'
    state = json.loads((custody/'attempt.json').read_bytes())
    if previous:
        assert previous['identity'] == state['identity']
        assert previous['deadline_utc'] == state['deadline_utc']
    from compute_plane.native_journal import custody_lock
    with custody_lock(custody):
        effects = [r.event() for r in NativeJournal(custody/'journal.jsonl').snapshot().records
                   if r.event()['kind'] == 'effect_returned']
    assert len(effects) == 4  # read + failing verifier + one edit + passing verifier
    assert result['effective_limits']['implementation']['max_seconds'] is None


@pytest.mark.parametrize('fault', ['revoked', 'other_owner', 'expired_budget', 'unknown_effect', 'call_budget'])
def test_resume_refuses_revocation_and_unknown_effect_without_reexecution(host, monkeypatch, capsys, fault):
    from compute_plane.native_journal import NativeJournal
    from relay import native_work_credentials
    from relay.runtime_authority import CoordinatorAuthorityGate
    from relay.cluster_coordinator import CoordinatorDB
    from tare_tools_agent_runtime import BindingRef
    import tare_ops
    with monkeypatch.context() as cut:
        original = NativeJournal.append
        def stop(journal, event):
            result = original(journal, event)
            kind = 'effect_requested' if fault == 'unknown_effect' else 'effect_returned'
            if event['kind'] == kind and (fault != 'unknown_effect' or event['tool_call']['name'] == 'verify'):
                raise SystemExit('interrupt')
            return result
        cut.setattr(NativeJournal, 'append', stop)
        with pytest.raises(SystemExit): run(host, *(['--max-model-calls', '1'] if fault == 'call_budget' else []))
    capsys.readouterr()
    train = work_run.train_dir_for('work-one')
    origin = json.loads((train/'WORK_RUN.json').read_bytes()); custody = Path(origin['implementation_custody'])
    config = json.loads((custody/'host.json').read_bytes())
    gate = CoordinatorAuthorityGate(CoordinatorDB(relay_mesh.RELAY_DIR/'history/coordinator.db'))
    token = native_work_credentials.read(config['binding']['binding_id'])['token']
    binding = BindingRef(**config['binding'], token=token)
    if fault == 'revoked': assert gate.revoke(binding, reason='operator')
    elif fault == 'other_owner': assert gate.acquire('work-one', 'another-owner')
    elif fault == 'expired_budget':
        # Exercise the original attempt deadline, without waiting on wall time.
        path = custody/'attempt.json'; state = json.loads(path.read_bytes()); state['deadline_utc'] = 0
        path.write_text(json.dumps(state))
    before = len(host[3].generations)
    assert tare_ops.main(['run', 'work-one', '--resume']) == 1
    result = json.loads(capsys.readouterr().out)
    assert len(host[3].generations) == before
    assert (host[1]/'src/value.txt').read_text() == 'base\n'
    if fault == 'unknown_effect': assert result['status'] == 'IMPLEMENTATION_RECONCILIATION_REQUIRED'
    elif fault in {'expired_budget', 'call_budget'}: assert result['status'] == 'IMPLEMENTATION_EXHAUSTED'
    else: assert 'AUTHORITY_REFUSED' in result['error']


def test_rotated_token_stops_old_control_and_lost_ack_retry_is_idempotent(os_root, tmp_path):
    from relay.cluster_coordinator import CoordinatorDB
    from relay.runtime_authority import CoordinatorAuthorityGate
    gate = CoordinatorAuthorityGate(CoordinatorDB(tmp_path/'db.sqlite'))
    old = gate.acquire('work', 'owner')
    assert gate.pause(old)
    new = gate.recover(old, successor_token='next-token')
    assert new.binding_id == old.binding_id and new.fence == old.fence
    assert not gate.is_current(old) and gate.is_current(new)
    assert not gate.renew(old) and not gate.revoke(old, reason='stale token')
    assert gate.recover(old, successor_token='next-token') == new
    assert gate.revoke(new, reason='operator')
    assert gate.recover(old, successor_token='next-token') is None


def test_recover_after_lease_expired_during_process_death(os_root, tmp_path):
    from relay.cluster_coordinator import CoordinatorDB
    from relay.runtime_authority import CoordinatorAuthorityGate
    clock = [0]
    gate = CoordinatorAuthorityGate(CoordinatorDB(tmp_path/'db.sqlite', clock=lambda: clock[0]))
    old = gate.acquire('work', 'owner', ttl_s=1)
    clock[0] = 500
    assert not gate.is_current(old)
    gate._db.reap_expired_agent_runtime_bindings()
    new = gate.recover(old, successor_token='next-token')
    assert gate.is_current(new) and not gate.is_current(old)
    assert old.binding_id == new.binding_id


@pytest.mark.parametrize('missing_citation', [False, True])
@pytest.mark.parametrize('recovery_enabled', [False, True])
def test_plan_audit_resume_preserves_policy_and_original_deadline(tmp_path, monkeypatch, missing_citation, recovery_enabled):
    import hashlib
    from compute_plane import native_models
    from compute_plane.native_journal import NativeJournal
    from relay.native_work_host import plan_audit
    # Projection semantics are covered separately; this fixture isolates session recovery.
    monkeypatch.setenv('TARE_PLAN_SOURCE_RECOVERY', '1' if recovery_enabled else '0')
    monkeypatch.setattr('relay.native_work_host.plan_context', lambda *args, repository_root: 'PLAN_CONTEXT.json')
    from test_native_agent import Client, deployment
    train = tmp_path/'train'; train.mkdir()
    repo = tmp_path/'repo'; repo.mkdir()
    names = ['PACKET.md', 'EXECUTION_DESCRIPTOR.json']
    for name in names: (train/name).write_text('bounded plan')
    refs = [{'repository': 'train', 'path': name, 'sha256': hashlib.sha256((train/name).read_bytes()).hexdigest()}
            for name in names]
    review = {'summary': 'Read the plan.', 'sources': refs[1:] if missing_citation else refs,
              'remaining': [], 'verdict': 'ACCEPT', 'findings': []}
    expected_recovery = missing_citation and recovery_enabled
    continuation = [('tare_deliver', dict(review, sources=refs))] if expected_recovery else []
    client = Client(steps=[*[('read', {'repository': 'train', 'path': n}) for n in names],
                           ('tare_deliver', review), *continuation])
    class Http:
        def __new__(cls, *a, **kw): return client
    monkeypatch.setattr(native_models, 'NativeHttpClient', Http)
    custody = tmp_path/'audit'
    with monkeypatch.context() as cut:
        append = NativeJournal.append
        def stop(journal, event):
            result = append(journal, event)
            if event['kind'] == 'effect_returned': raise SystemExit('plan interrupted')
            return result
        cut.setattr(NativeJournal, 'append', stop)
        with pytest.raises(SystemExit): plan_audit(deployment(), train, 'repo', repo, custody)
    before = json.loads((custody/'native-agent.json').read_bytes())
    result = plan_audit(deployment(), train, 'repo', repo, custody, resume=True, max_compactions=1,
                        limits=before['limits'])
    after = json.loads((custody/'native-agent.json').read_bytes())
    assert client.generations == (4 if expected_recovery else 3)
    records = NativeJournal(custody/'journal.jsonl').snapshot().records
    assert sum(r.event()['kind'] == 'host_observation' for r in records) == int(expected_recovery)
    assert (custody/'source-recovery-read.jsonl').exists() == expected_recovery
    if missing_citation and not recovery_enabled:
        assert result['verdict'] == 'INCONCLUSIVE' and result['failure_code'] == 'INVALID_DELIVERY'
        assert result['findings'] == ['Required review citation missing: train:PACKET.md']
        assert Path(result['session']) == custody
    else:
        assert result['verdict'] == 'ACCEPT'
    assert before['deadline_utc'] == after['deadline_utc'] and before['session_id'] == after['session_id']


def test_keyring_rotation_survives_database_commit_before_vault_finalization(os_root, tmp_path, monkeypatch):
    from relay import native_work_credentials as credentials
    from relay.cluster_coordinator import CoordinatorDB
    from relay.runtime_authority import CoordinatorAuthorityGate
    from relay.native_attempt import binding_identity
    gate = CoordinatorAuthorityGate(CoordinatorDB(tmp_path/'db.sqlite'))
    binding = gate.acquire('work', 'owner')
    credentials.write(binding.binding_id, {'token': binding.token})
    original = credentials.write
    with monkeypatch.context() as cut:
        def fail(key, value):
            if 'pending' not in value: raise SystemExit('lost vault write')
            original(key, value)
        cut.setattr(credentials, 'write', fail)
        with pytest.raises(SystemExit): credentials.recover(gate, binding_identity(binding))
    recovered = credentials.recover(gate, binding_identity(binding))
    assert gate.is_current(recovered) and not gate.is_current(binding)


def test_new_process_resumes_edit_using_real_system_keyring(host, monkeypatch, capsys):
    import keyring
    monkeypatch.delenv('PYTHON_KEYRING_BACKEND')  # the suite's null vault; this test and its child use the OS one
    monkeypatch.setattr(keyring.core, '_keyring_backend', None)
    if keyring.get_keyring().priority <= 0:
        pytest.skip('Real system-keyring recovery requires an available OS keyring backend; port recovery is tested separately.')
    import os
    import subprocess
    import sys
    from compute_plane.native_journal import NativeJournal
    for name, fn in REAL_VAULT.items(): monkeypatch.setattr(native_work_credentials, name, fn)
    binding_id = None
    try:
        with monkeypatch.context() as cut:
            original = NativeJournal.append
            def stop(journal, event):
                result = original(journal, event)
                if event['kind'] == 'effect_returned' and len(host[3].generations) == 3:
                    raise SystemExit('restart after edit')
                return result
            cut.setattr(NativeJournal, 'append', stop)
            with pytest.raises(SystemExit): run(host)
        capsys.readouterr()
        origin = json.loads((work_run.train_dir_for('work-one')/'WORK_RUN.json').read_bytes())
        custody = Path(origin['implementation_custody'])
        config = json.loads((custody/'host.json').read_bytes())
        binding_id = config['binding']['binding_id']
        before = json.loads((custody/'attempt.json').read_bytes())
        code = '''import sys
from pathlib import Path
from relay import relay_mesh,work_run
relay_mesh.rebind_root(Path(sys.argv[1]));sys.modules['relay_mesh']=relay_mesh
from test_work_run import NativeFixtureClient,evidence
from compute_plane import native_models
client=NativeFixtureClient();client.generations=[None]*3
class Http:
    def __new__(cls,*a,**kw):return client
native_models.NativeHttpClient=Http
work_run.eligibility_check=lambda *a:None
relay_mesh.materialize_admission_evidence=evidence
import tare_ops
raise SystemExit(tare_ops.main(['run','work-one','--resume']))
'''
        child = subprocess.run([sys.executable, '-X', 'utf8', '-B', '-c', code, str(host[0])],
            cwd=host[0].parent, env={**os.environ, 'PYTHONPATH': os.pathsep.join(str(p) for p in sys.path)},
            capture_output=True, text=True, encoding='utf-8', timeout=30)
        assert child.returncode == 0, child.stdout + child.stderr
        result = json.loads(child.stdout)
        assert result['status'] == 'SETTLED' and result['resumed_execution']['model_calls_total'] == 5
        after = json.loads((custody/'attempt.json').read_bytes())
        assert after['identity'] == before['identity'] and after['deadline_utc'] == before['deadline_utc']
        assert (host[1]/'src/value.txt').read_text() == 'strict\n'
        assert len(host[3].generations) == 3
        with pytest.raises(ValueError, match='CREDENTIAL_MISSING'): REAL_VAULT['read'](binding_id)
    finally:
        if binding_id: REAL_VAULT['discard'](binding_id)


def test_resume_reports_incomplete_plan_delivery_before_implementation(host, monkeypatch, capsys):
    from relay import native_work_host
    import tare_ops
    with monkeypatch.context() as cut:
        def interrupted(*args, **kwargs):
            raise SystemExit('before review')
        cut.setattr(native_work_host, 'plan_audit', interrupted)
        with pytest.raises(SystemExit):
            run(host)
    capsys.readouterr()
    monkeypatch.setattr(native_work_host, 'plan_audit', lambda *args, **kwargs: {
        'verdict': 'INCONCLUSIVE', 'failure_code': 'INVALID_DELIVERY',
        'findings': ['Required review citation missing: train:PACKET.md']})
    assert tare_ops.main(['run', 'work-one', '--resume']) != 0
    result=json.loads(capsys.readouterr().out)
    assert result['status']=='PLAN_INVALID_DELIVERY'
    assert result['stages'][-1]['failure_code']=='INVALID_DELIVERY'
    assert len(host[3].generations)==0
    assert not list(work_run.train_dir_for('work-one').glob('WORKSPACE_*.json'))


def test_unavailable_keyring_refuses_work_before_implementation(host, monkeypatch, capsys):
    import keyring
    for name, fn in REAL_VAULT.items():
        monkeypatch.setattr(native_work_credentials, name, fn)
    def unavailable(*args, **kwargs):
        raise keyring.errors.NoKeyringError('fixture backend unavailable')
    monkeypatch.setattr(keyring, 'set_password', unavailable)
    assert run(host) != 0
    result=json.loads(capsys.readouterr().out)
    assert result['status']=='FAILED' and 'NATIVE_WORK_RECOVERY_KEYRING_UNAVAILABLE' in result['error']
    assert len(host[3].generations)==0
    assert not list(work_run.train_dir_for('work-one').glob('WORKSPACE_*.json'))
