"""Short clock/protocol tests for optional Work limits, without real inference."""
from dataclasses import replace
import hashlib
import json
import math
from threading import Event

import pytest

from relay import native_work_host, relay_mesh
from test_work_run import os_root, durable_fixture
from test_native_agent import Client, deployment


def test_stage_limits_inherit_profile_and_explicit_flags_override_unlimited():
    profile = {'native_execution_limits': {'max_model_calls': 7, 'read_seconds': 4}}
    assert native_work_host.stage_limits(profile, 'plan')['max_model_calls'] == 7
    resolved = native_work_host.stage_limits(profile, 'implement', unlimited=True,
                                            limits={'max_model_calls': 5, 'read_seconds': 8})
    assert resolved['max_model_calls'] == 5 and resolved['read_seconds'] == 8
    assert resolved['max_seconds'] is None and resolved['request_seconds'] is None
    assert resolved['max_stagnant_turns'] is None and resolved['max_model_retries'] == 0


@pytest.mark.parametrize('unlimited', [False, True])
def test_plan_audit_can_exceed_previous_call_ceiling_or_keep_it(tmp_path, monkeypatch, unlimited):
    from compute_plane import native_models
    # This fixture isolates call ceilings. Real descriptor/source projection is
    # qualified by test_work_plan_context; the files below are scripted provider inputs.
    monkeypatch.setattr(native_work_host, 'plan_context', lambda *args, repository_root: 'PLAN_CONTEXT.json')
    train = tmp_path/'train'; train.mkdir()
    repo = tmp_path/'repo'; repo.mkdir()
    names = ['PACKET.md', 'EXECUTION_DESCRIPTOR.json'] + ['dependency-'+str(i)+'.txt' for i in range(12)]
    for name in names:
        (train/name).write_text('bounded reference '+name)
    refs = [{'repository': 'train', 'path': name, 'sha256': hashlib.sha256((train/name).read_bytes()).hexdigest()}
            for name in names[:2]]
    review = {'summary': 'Read the plan.', 'sources': refs, 'remaining': [], 'verdict': 'ACCEPT', 'findings': []}
    client = Client(steps=[*[('read', {'repository': 'train', 'path': n}) for n in names], ('tare_deliver', review)])
    options = []
    class Http:
        def __new__(cls, *a, **kw):
            options.append(kw)
            return client
    monkeypatch.setattr(native_models, 'NativeHttpClient', Http)
    result = native_work_host.plan_audit(deployment(), train, 'repo', repo, tmp_path/'audit', unlimited=unlimited)
    assert result['verdict'] == ('ACCEPT' if unlimited else 'INCONCLUSIVE')
    assert client.generations == (15 if unlimited else 12)
    state = json.loads((tmp_path/'audit/native-agent.json').read_text(encoding='utf-8'))
    assert state['config']['context_policy'].get('max_compactions', 1) == (None if unlimited else 1)
    assert state['deadline_utc'] is None if unlimited else state['deadline_utc'] is not None
    assert all(row['read_timeout_seconds'] == (None if unlimited else 240) for row in options)


def test_unlimited_attempt_persists_null_deadline_and_resumes_without_new_calls(os_root, tmp_path):
    from relay.native_attempt import run_native_attempt
    args = durable_fixture(os_root, tmp_path)
    args['control'] = replace(args['control'], deadline_monotonic=math.inf)
    assert run_native_attempt(**args)['status'] == 'PROPOSED'
    manifest = args['custody']/'attempt.json'
    assert json.loads(manifest.read_text())['deadline_utc'] is None
    assert 'Infinity' not in manifest.read_text()
    assert run_native_attempt(resume=True, **args)['status'] == 'PROPOSED'
    assert len(args['client'].generations) == 5


def test_lease_renews_beyond_original_expiry_and_never_reacquires_after_revocation(tmp_path, os_root):
    from relay.cluster_coordinator import CoordinatorDB
    from relay.runtime_authority import CoordinatorAuthorityGate
    from tare_tools_agent_runtime import CancellationToken
    clock = [0.0]
    db = CoordinatorDB(tmp_path/'leases.db', clock=lambda: clock[0])
    gate = CoordinatorAuthorityGate(db)
    binding = gate.acquire('work', 'operator', ttl_s=.3)
    renewed = Event()
    original = gate.renew
    def renew(*args, **kwargs):
        result = original(*args, **kwargs)
        renewed.set()
        return result
    gate.renew = renew
    cancel = CancellationToken()
    with gate.maintain(binding, cancel, ttl_s=.3):
        for instant in [.2, .4, .6]:
            clock[0] = instant
            renewed.clear()
            assert renewed.wait(2)
            assert gate.is_current(binding)
        renewed.clear()
        assert gate.revoke(binding, reason='test operator cancellation')
        assert renewed.wait(2)
        assert not gate.is_current(binding)
    # The heartbeat did not silently acquire a successor fence.
    successor = gate.acquire('work', 'next', ttl_s=.3)
    assert successor.fence == binding.fence+1


def test_lease_renewal_error_cancels_and_stops_worker():
    from relay.runtime_authority import CoordinatorAuthorityGate
    from tare_tools_agent_runtime import CancellationToken
    gate = CoordinatorAuthorityGate(None)
    event = Event()
    calls = []
    def renew(*args, **kwargs):
        calls.append(1)
        if len(calls) > 1:
            event.set()
            raise OSError('database unavailable')
        return True
    gate.renew = renew
    cancel = CancellationToken()
    with gate.maintain(None, cancel, ttl_s=.03):
        assert event.wait(2)
    assert cancel.is_cancelled() and len(calls) == 2


def test_train_lease_renewal_checks_exact_process_and_does_not_reacquire(os_root, monkeypatch):
    import os
    import time
    lease = {'agent_id': 'test', 'hostname': relay_mesh.get_hostname(),
             'pid': os.getpid(), 'process_uuid': relay_mesh.PROCESS_UUID, 'expires_ts': time.time()+1}
    seen = []
    monkeypatch.setattr(relay_mesh, 'current_lease', lambda *a: lease)
    monkeypatch.setattr(relay_mesh, 'renew_lease', lambda *a, **kw: seen.append((a, kw)) or True)
    assert native_work_host.renew_train_lease('train', 'test')
    assert seen == [(('train', 'test'), {'additional_ttl': 5400})]
    lease['pid'] += 1
    assert not native_work_host.renew_train_lease('train', 'test') and len(seen) == 1
    lease.clear()
    assert not native_work_host.renew_train_lease('train', 'test') and len(seen) == 1


def test_verification_accepts_explicit_no_timeout_and_default_stays_finite(tmp_path):
    import subprocess
    from compute_plane.managed_work import VerificationCommand, observe_work_progress
    (tmp_path/'value.txt').write_text('value')
    seen = []
    def execute(argv, **kwargs):
        seen.append(kwargs['timeout'])
        return subprocess.CompletedProcess(argv, 0, b'ok', b'')
    check = VerificationCommand('unit', ('test',), timeout_seconds=None)
    report = observe_work_progress(workspace=tmp_path, exit_criteria=['unit'], checks=[check],
                                   tracked_paths=['value.txt'], runner=execute)
    assert report.remaining_obligations == 0 and seen == [None]
    assert VerificationCommand('unit', ('test',)).timeout_seconds == 60


@pytest.mark.parametrize('stop', ['call_limit', 'sigint'])
def test_public_run_honors_explicit_call_limit_and_sigint_without_landing(os_root, monkeypatch, capsys, stop):
    import signal
    from compute_plane import native_models
    from test_work_run import NativeFixtureClient
    import tare_ops
    root, repo = os_root
    profile = deployment('bedrock-converse'); profile['model'] = 'qwen-3.8-27b'
    path = root/'profile.json'; path.write_text(json.dumps(profile))
    roster = relay_mesh.RELAY_DIR/'round_table_roster.json'
    data = json.loads(roster.read_text()); data['models']['qwen-3.8-27b']['runtime']['native'] = 'fixture'
    roster.write_text(json.dumps(data))
    client = NativeFixtureClient()
    invoke = client.post
    def post(*args, **kwargs):
        value = invoke(*args, **kwargs)
        if stop == 'sigint' and client.generations:
            signal.raise_signal(signal.SIGINT)
        return value
    client.post = post
    class Http:
        def __new__(cls, *args, **kwargs):
            assert kwargs['timeout_seconds'] is None
            return client
    monkeypatch.setattr(native_models, 'NativeHttpClient', Http)
    monkeypatch.setattr(native_work_host, 'plan_audit', lambda *a, **kw: {'verdict': 'ACCEPT', 'findings': []})
    previous = signal.getsignal(signal.SIGINT)
    code = tare_ops.main(['run', 'work-one', '--repo-root', str(repo), '--native-deployment', str(path),
                         '--unlimited', '--max-model-calls', '2', '--format', 'json'])
    result = json.loads(capsys.readouterr().out)
    assert code == 1 and result['status'] == ('IMPLEMENTATION_EXHAUSTED' if stop == 'call_limit' else 'IMPLEMENTATION_CANCELLED')
    assert result['candidate_commit'] is None and result['landing'] is None
    assert (repo/'src/value.txt').read_text() == 'base\n'
    assert result['effective_limits']['implementation']['max_model_calls'] == 2
    assert signal.getsignal(signal.SIGINT) == previous
