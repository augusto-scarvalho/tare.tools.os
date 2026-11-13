"""Checks run in the Windows sandbox by default, whatever the project's mode (owner, 2026-11-13). The launcher is
replaced as in test_command_protection; the real sandbox is proven by the probe harness, not here."""
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest
from compute_plane.action_control import ActionRefused
from compute_plane.native_journal import NativeJournal
from relay import command_protection as protection, execution_scope, work_units
from test_command_protection import host  # noqa: F401  (the MXC launcher replaced, nothing else)

PASS = [sys.executable, '-B', '-c', "print('verified')"]
FAIL = [sys.executable, '-B', '-c', "print('assertion failed'); raise SystemExit(2)"]
NETWORK = 'ISOLATION_NETWORK_DENIED: every failing test of this check failed because the sandbox denied a connection.'
UNIT = {'repository': 'project', 'title': 'Run the named checks', 'criteria': [], 'target_paths': ['a.py']}


def unprotected(host, monkeypatch, available=True):
    """The fixture's project without command protection, on a host with (or without) the sandbox."""
    monkeypatch.setattr(protection, 'sandbox_available', lambda: available)
    del host.config['command_protection']
    protection.sys.base_prefix, protection.sys.prefix, protection.sys.executable = sys.base_prefix, sys.prefix, sys.executable
    return host


def decisions(custody):
    return [r.event() for r in NativeJournal(custody/'host-checks'/'command-reviews.jsonl').snapshot().records]


def test_a_project_without_protection_runs_its_checks_in_the_sandbox_with_no_review(host, monkeypatch):
    unprotected(host, monkeypatch)
    (host.root/'.env').write_text('FAKE_SECRET_FIXTURE')
    calls, bound = [], []
    factory, isolation = protection.factory, protection.windows_isolation_factory
    monkeypatch.setattr(protection, 'factory', lambda *a, **kw: calls.append(kw) or factory(*a, **kw))
    monkeypatch.setattr(protection, 'windows_isolation_factory', lambda **kw: bound.append(kw) or isolation(**kw))
    run = protection.conversation_verifier(host.control, host.custody, host.config, {}, host.root, UNIT,
                                           {'check': PASS, 'failing': FAIL})
    done = run(PASS)
    assert (done.returncode, done.execution_limitation) == (0, None) and 'verified' in done.stdout
    assert run(FAIL).returncode == 2
    assert calls[0]['checks_only'] is True and calls[0]['host_checks'] is True
    policy = bound[0]['policy']
    assert bound[0]['backend'] == 'mxc' and str(host.root/'.env') in policy.denied
    assert str((host.root/'a.py').resolve()) in policy.read_write and str(host.root) not in policy.read_write
    assert bound[0]['environment']['TARE_SANDBOX_SOCKETPAIR'] == '1'  # the socketpair shim, as protected checks
    # No Jev, reviewer or Codex call, and no review budget: only the host's decision is journaled.
    assert host.native == [] and host.activities == []
    events = decisions(host.custody)
    assert not any(e['kind'] == 'command_review_reserved' for e in events)
    assert {e['reason'] for e in events if e['kind'] == 'command_review_decision'} == {protection.SANDBOXED_CHECK}
    receipts = [e['protection'] for e in events if e['kind'] == 'command_protected_effect']
    assert len(receipts) == 2 and all(r['review'] == 'none' and r['reserved_calls'] == 0
                                      and r['review_basis'] == 'sandbox_by_default' for r in receipts)
    with pytest.raises(ValueError, match='not declared'):
        run([sys.executable, '-c', 'print("unregistered")'])


def test_the_checks_only_gate_admits_registered_argv_and_nothing_else(host, monkeypatch):
    del host.config['command_protection']
    declared = [str(host.executable), '-c', 'print(1)']
    config = {**host.config, 'verifiers': {'check': {'repository': 'project', 'argv': declared}}}
    port = protection.factory(config, {}, host.custody, 'Run the checks.', 'invocation', checks_only=True)(
        session_id='session', binding=host.binding, control=host.control, config=config, custody=host.custody,
        scratch=host.custody/'scratch', capabilities=host.capabilities)
    result = port.execute(host.facts(args=declared), host.effect)
    assert result['command_protection']['review_status'] == 'not_required'
    assert result['command_protection']['review_basis'] == 'sandbox_by_default' and host.native == []
    with pytest.raises(ActionRefused):
        port.execute(host.facts('2', args=[str(host.executable), '-c', 'print(2)']), host.effect)
    assert host.effects == ['effect']
    with pytest.raises(ValueError, match='keeps its command review'):
        protection.factory({**config, 'command_protection': {'review': 'codex'}}, {}, host.custody, '', 'i',
                           checks_only=True)


def test_a_project_venv_on_another_python_reads_its_base_installation(host, tmp_path, monkeypatch):
    base, env = tmp_path/'Python311', tmp_path/'project-env'
    base.mkdir(); (env/'Scripts').mkdir(parents=True)
    (env/'pyvenv.cfg').write_text(f'home = {base}\nversion = 3.11.9\n')
    python = env/'Scripts'/'python.exe'; python.write_bytes(b'fixture; not executed')
    del host.config['command_protection']
    host.config['verifiers'] = {'tests': {'repository': 'project', 'argv': [str(python), '-m', 'pytest']}}
    bound, isolation = [], protection.windows_isolation_factory
    monkeypatch.setattr(protection, 'windows_isolation_factory', lambda **kw: bound.append(kw) or isolation(**kw))
    protection.factory(host.config, {}, host.custody, 'Run the checks.', 'invocation', checks_only=True)(
        session_id='session', binding=host.binding, control=host.control, config=host.config, custody=host.custody,
        scratch=host.custody/'scratch', capabilities=host.capabilities).close()
    read = bound[0]['policy'].read_only
    assert str(env) in read and str(base.resolve()) in read and str(base) not in bound[0]['policy'].read_write


@pytest.mark.parametrize('allowed', [True, False])
def test_a_check_the_sandbox_blocked_runs_outside_only_when_the_user_says_so(host, monkeypatch, allowed):
    unprotected(host, monkeypatch)
    monkeypatch.setattr(protection, 'verification_limitation', lambda value: NETWORK)
    outside_runs, asked = [], []
    monkeypatch.setattr(work_units, 'managed_check', lambda argv, **kw: outside_runs.append(argv) or
                        subprocess.CompletedProcess(argv, 0, 'passed outside\n', ''))
    def outside(name, argv, limitation):
        asked.append((name, argv, limitation))
        return (True, 'for this turn, as the user chose') if allowed else (False, 'the user chose not to run it '
                                                                             'outside the sandbox')
    run = protection.conversation_verifier(host.control, host.custody, host.config, {}, host.root, UNIT,
                                           {'tests': PASS}, outside=outside)
    token = work_units.CHECK_PORT.set(run)
    try:
        state, receipt = work_units.run_checks(host.root, {'tests': PASS})
    finally:
        work_units.CHECK_PORT.reset(token)
    assert asked == [('tests', PASS, NETWORK)]
    if allowed:  # that check, rerun outside, and the receipt says by whose decision and why
        assert state == 'green' and outside_runs == [PASS] and 'passed outside' in receipt
        assert ("tests: run outside the sandbox by the user's decision (for this turn, as the user chose), because "
                'the sandbox blocked it: ISOLATION_NETWORK_DENIED') in receipt
    else:
        assert state == 'inconclusive' and outside_runs == [] and 'verified' in receipt  # its sandboxed output stays
        assert 'Not run outside the sandbox: the user chose not to run it outside the sandbox.' in receipt
        assert work_units.blocked_by_sandbox(receipt)


def test_an_ordinary_failing_test_is_red_and_asks_nothing(host, monkeypatch):
    unprotected(host, monkeypatch)
    run = protection.conversation_verifier(host.control, host.custody, host.config, {}, host.root, UNIT,
        {'failing': FAIL}, outside=lambda *a: pytest.fail('An ordinary failure is not a question.'))
    token = work_units.CHECK_PORT.set(run)
    try:
        assert work_units.run_checks(host.root, {'failing': FAIL})[0] == 'red'
    finally:
        work_units.CHECK_PORT.reset(token)


def test_a_protected_project_keeps_its_policy_and_is_never_asked(host, monkeypatch):
    monkeypatch.setattr(protection, 'sandbox_available', lambda: True)
    protection.sys.base_prefix, protection.sys.prefix, protection.sys.executable = sys.base_prefix, sys.prefix, sys.executable
    monkeypatch.setattr(protection, 'verification_limitation', lambda value: NETWORK)
    run = protection.conversation_verifier(host.control, host.custody, host.config, host.deployment, host.root, UNIT,
        {'tests': PASS}, outside=lambda *a: pytest.fail('A protected project is not asked.'))
    assert run(PASS).execution_limitation == NETWORK and len(host.native) == 1  # its review, as before


def test_a_project_whose_checks_cannot_be_bound_asks_instead_of_losing_them(host, monkeypatch):
    unprotected(host, monkeypatch)
    from relay import sandbox_secrets
    def too_many(*args, **kwargs):
        raise ValueError('The project has 40 secret files or folders, such as a/.env: more than the 32 paths')
    monkeypatch.setattr(sandbox_secrets, 'find', too_many)
    asked = []
    run = protection.conversation_verifier(host.control, host.custody, host.config, {}, host.root, UNIT,
        {'tests': PASS}, outside=lambda *a: asked.append(a) or (False, 'no one could be asked: no TUI is connected'))
    done = run(PASS)
    assert asked[0][2].startswith("ISOLATION_UNAVAILABLE: this project's checks cannot run in the sandbox: The "
                                  'project has 40 secret files')
    assert done.returncode == 1 and done.stdout == ''  # nothing ran
    assert done.execution_limitation.endswith('Not run: no one could be asked: no TUI is connected.')


@pytest.mark.parametrize('read_only', [False, True])
def test_without_a_sandbox_hosted_checks_run_as_today_and_say_they_are_not_isolated(host, monkeypatch, read_only):
    unprotected(host, monkeypatch, available=False)
    asked = []
    def outside(name, argv, limitation):
        asked.append(limitation)
        return len(asked) > 1, 'for this turn, as the user chose' if len(asked) > 1 else 'the user chose not to'
    run = protection.conversation_verifier(host.control, host.custody, host.config, {}, host.root, UNIT,
                                           {'check': PASS}, outside=outside, read_only=read_only)
    token = work_units.CHECK_PORT.set(run)
    try:
        first = work_units.run_checks(host.root, {'check': PASS})
        second = work_units.run_checks(host.root, {'check': PASS})
    finally:
        work_units.CHECK_PORT.reset(token)
    if not read_only:
        assert asked == [] and first[0] == second[0] == 'green'
        assert 'check: not isolated: no sandbox on this system' in first[1]
        return
    # A read-only project never runs repository code unisolated without the user's decision: the same question.
    assert asked[0].startswith('ISOLATION_UNAVAILABLE: this system has no sandbox and this project is read only')
    assert first[0] == 'inconclusive' and 'verified' not in first[1] and work_units.blocked_by_sandbox(first[1])
    assert second[0] == 'green' and "run by the user's decision (for this turn, as the user chose)" in second[1]


def test_the_session_suite_holds_a_change_whose_check_the_sandbox_blocked(tmp_path, monkeypatch):
    """D9b: an inconclusive session check settles nothing only when it timed out, not when the sandbox stopped it."""
    tree = tmp_path/'tree'; (tree/'tests').mkdir(parents=True)
    (tree/'tests'/'test_mod.py').write_text('import mod\n')
    config = {'verifiers': {'all': {'repository': 'repo', 'argv': ['suite']}}}
    unit = {'test_paths': ['tests/test_mod.py']}
    for code in work_units.SANDBOX_LIMITS:
        receipt = f'$ all (exit 1)\nE   ...\nall: {code}: blocked. Not run outside the sandbox: the user said no.'
        monkeypatch.setattr(work_units, 'run_checks', lambda tree, commands: ('inconclusive', receipt))
        allowed, why = work_units.suite_allows(config, unit, 'repo', tree, {'mod.py': b''})
        assert not allowed and 'could not be judged in the sandbox' in why and code in why
    monkeypatch.setattr(work_units, 'run_checks', lambda tree, commands: ('inconclusive', 'all: more than 300 s (timeout)'))
    assert work_units.suite_allows(config, unit, 'repo', tree, {'mod.py': b''})[0]  # a timeout keeps today's rule


def test_the_question_asks_once_per_turn_honours_always_and_never_runs_outside_headless(tmp_path):
    import threading
    import time
    from relay import conversation_permissions as permissions, relay_mesh
    from relay.turn_engine import checks_outside_sandbox
    from tare_tools_agent_runtime import BindingRef, CancellationToken, ExecutionControl
    root, project, directory = tmp_path/'session', tmp_path/'project', tmp_path/'console'
    root.mkdir(); project.mkdir()
    control = ExecutionControl(BindingRef('work', 'binding', 'operator', 1, 'opaque'), CancellationToken(),
                               time.monotonic()+60)
    state, argv = {'session_id': 'parent'}, [sys.executable, '-m', 'pytest']

    def decide(turn, choice=None, frontend=True):
        requests = []
        def user():  # the TUI: answers the one request it sees
            while not (root/'execution-permission'/'request.json').is_file() or \
                    permissions.pending(root) is None:
                time.sleep(.01)
            request = permissions.pending(root)
            requests.append(request)
            permissions.answer(root, request['id'], choice, project=project, directory=directory,
                               control_root=relay_mesh.RELAY_ROOT)
        thread = threading.Thread(target=user, daemon=True)
        if choice:
            thread.start()
        outcome = permissions.outside_sandbox(root, state, None, 'effect', project, control,
                                              directory if frontend else None, turn)('tests', argv, NETWORK)
        if choice:
            thread.join(5)
        return outcome, requests

    assert decide('turn-1', frontend=False) == ((False, 'no one could be asked: no TUI is connected'), [])
    (allowed, why), requests = decide('turn-1', 'deny')
    assert not allowed and why == 'the user chose not to run it outside the sandbox'
    assert requests[0]['kind'] == 'sandbox' and requests[0]['checks'] == {'tests': argv}
    assert requests[0]['objective'].startswith('The sandbox blocked a network connection on this machine')
    assert decide('turn-1', 'once')[0] == (True, 'for this turn, as the user chose')
    assert decide('turn-1')[0] == (True, 'for this turn, as the user chose')  # no second question this turn
    assert decide('turn-2', 'project')[0] == (True, 'always for this project, as the user chose')
    assert checks_outside_sandbox(project)
    assert decide('turn-3', frontend=False)[0] == (True, 'always for this project, as the user chose')


LOCAL = {'native_profile': {'endpoint': 'http://127.0.0.1:8080/v1', 'protocol': 'openai-chat', 'model': 'qwen'}}
HOSTED = {'native_profile': {'endpoint': 'https://api.example.test/v1', 'protocol': 'openai-chat', 'model': 'gpt'}}


@pytest.mark.local_implementer_rule
@pytest.mark.parametrize('deployment,role,available,refused', [
    (LOCAL, 'implementer', False, True), (LOCAL, 'implementer', True, False),
    (HOSTED, 'implementer', False, False), (LOCAL, 'auditor', False, False)])
def test_a_local_implementer_is_refused_where_no_sandbox_isolates_its_checks(tmp_path, monkeypatch, deployment, role,
                                                                             available, refused):
    monkeypatch.setattr(protection, 'sandbox_available', lambda: available)
    calls = []
    @execution_scope.session_role
    def session(config, deployment, custody, **kwargs):
        calls.append(deployment)
        return 'ran'
    if refused:
        with pytest.raises(ValueError, match='does not run a local model as implementer on this system') as error:
            session({'role': role}, deployment, tmp_path)
        assert 'hosted implementer' in str(error.value) and 'Windows 11' in str(error.value) and calls == []
    else:
        assert session({'role': role}, deployment, tmp_path) == 'ran'


def test_without_flatbuffers_or_windows_the_probe_says_no_sandbox(monkeypatch):
    # CI's Linux shards pretend to be Windows elsewhere: the probe itself must not reach ctypes off Windows.
    monkeypatch.setattr(protection, 'sys', SimpleNamespace(platform='linux'))
    assert protection.probe_sandbox() is False
