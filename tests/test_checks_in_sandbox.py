"""Checks run in the Windows sandbox by default, whatever the project's mode (owner, 2026-11-13). The launcher is
replaced as in test_command_protection; the real sandbox is proven by the probe harness, not here."""
import asyncio  # noqa: F401  Winsock starts here, before the host fixture points SystemRoot at a stand-in runtime
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


@pytest.mark.parametrize('linked', [False, True])
def test_node_keeps_its_paths_in_the_sandbox_unless_node_modules_holds_links(host, tmp_path, monkeypatch, linked):
    unprotected(host, monkeypatch)
    (host.root/'node_modules'/'dep').mkdir(parents=True)
    monkeypatch.setattr(protection.sandbox_secrets, 'linked', lambda folder: linked and folder.endswith('node_modules'))
    rustup = tmp_path/'rustup'; rustup.mkdir()
    monkeypatch.setenv('RUSTUP_HOME', str(rustup))
    bound, isolation = [], protection.windows_isolation_factory
    monkeypatch.setattr(protection, 'windows_isolation_factory', lambda **kw: bound.append(kw) or isolation(**kw))
    protection.conversation_verifier(host.control, host.custody, host.config, {}, host.root, UNIT, {'check': PASS})
    environment = bound[0]['environment']
    # Node's realpath reads the drive root, which the sandbox denies; linked packages need it (pnpm, npm link).
    assert environment.get('NODE_OPTIONS') == (None if linked else '--preserve-symlinks --preserve-symlinks-main')
    # rustup's toolchains, which the profile moved to scratch would hide; cargo's own home stays in scratch.
    assert environment['RUSTUP_HOME'] == str(rustup.resolve()) and str(rustup.resolve()) in bound[0]['policy'].read_only
    assert 'CARGO_HOME' not in environment


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


# Lines the real sandbox produced (2026-11-13, tools_probe.py): unittest, node --test, cargo test.
UNITTEST_NETWORK = '''test_echo_server (test_server.T.test_echo_server) ... ERROR

======================================================================
ERROR: test_echo_server (test_server.T.test_echo_server)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "C:\\tare-tp\\wt\\0123abcd\\ut_loopback\\test_server.py", line 7, in test_echo_server
    socket.create_connection(("127.0.0.1", server.getsockname()[1]), timeout=5).close()
PermissionError: [WinError 10013] An attempt was made to access a socket in a way forbidden by its access permissions

----------------------------------------------------------------------
Ran 1 test in 0.004s

FAILED (errors=1)
'''
UNITTEST_SECRET = "    return io.open(self, mode, buffering, encoding, errors, newline)\n" \
                  "PermissionError: [Errno 13] Permission denied: '.env'\n\nFAILED (errors=1)\n"
UNITTEST_DNS = 'socket.gaierror: [Errno 11001] getaddrinfo failed\n\nFAILED (errors=1)\n'
UNITTEST_FAIL = "    self.assertEqual(1 + 1, 3)\nAssertionError: 2 != 3\n\nFAILED (failures=1)\n"
NODE_ROOT = ("node:internal/modules/run_main:107\nError: EPERM: operation not permitted, lstat 'C:\\'\n"
             "    at Object.realpathSync (node:fs:2777:25) {\n  errno: -4048,\n  code: 'EPERM',\n  syscall: 'lstat',\n"
             "  path: 'C:\\\\'\n}\n\u2139 fail 1\n")
NODE_SECRET = ("\u2716 failing tests:\nError: EPERM: operation not permitted, open "
               "'C:\\Users\\augus\\AppData\\Local\\Temp\\tare-tp\\storage\\wt\\0123abcd\\.env'\n  code: 'EPERM',\n")
NODE_CONNECT = 'connect error EACCES connect EACCES 127.0.0.1:57376\n'
NODE_FAIL = ("\u2716 failing tests:\nAssertionError [ERR_ASSERTION]: Expected values to be strictly equal:\n\n2 !== 3\n"
             "  code: 'ERR_ASSERTION',\n")
CARGO = ("error: rustup could not choose a version of cargo to run, because one wasn't specified explicitly, and no "
         "default is configured.\n")
# node_probe.py (2026-11-13): cargo test with the toolchain readable, and the line Node prints for an option it rejects
# in NODE_OPTIONS (as a Node older than 12 does for the preserve-symlinks flags).
RUST_NETWORK = ("thread 'echo_server' (6272) panicked at src\\lib.rs:4:59:\ncalled `Result::unwrap()` on an `Err` value: "
                'Os { code: 10013, kind: PermissionDenied, message: "An attempt was made to access a socket in a way '
                'forbidden by its access permissions." }\n')
RUST_SECRET = ("thread 'settings' (6360) panicked at src\\lib.rs:3:48:\ncalled `Result::unwrap()` on an `Err` value: "
               'Os { code: 5, kind: PermissionDenied, message: "Access is denied." }\n')
RUST_WRITE = ('error: failed to write C:\\tare-np\\rust-ro\\rs_ok\\Cargo.lock\n\nCaused by:\n  failed to open: '
              'C:\\tare-np\\rust-ro\\rs_ok\\Cargo.lock\n\nCaused by:\n  Access is denied. (os error 5)\n')
CARGO_DEPENDENCY = ('warning: spurious network error (3 tries remaining): [6] Could not resolve hostname (Could not '
                    'resolve host: index.crates.io)\nerror: failed to get `cfg-if` as a dependency of package\n')
RUST_FAIL = ("thread 'regression' (18772) panicked at src\\lib.rs:3:5:\nassertion `left == right` failed\n  left: 2\n"
             " right: 3\nerror: test failed, to rerun pass `--lib`\n")
NODE_OPTION = 'C:\\Program Files\\nodejs\\node.exe: --bogus-flag is not allowed in NODE_OPTIONS\n'


def test_a_denied_file_outside_every_scope_is_the_sandboxs_and_one_inside_is_the_tests():
    # default_probe.py's outside_config check: pytest's E line, with the repr's doubled backslashes.
    line = ("E       PermissionError: [Errno 13] Permission denied: "
            "'C:\\\\Users\\\\augus\\\\AppData\\\\Local\\\\Temp\\\\tare-dp\\\\outside\\\\config.ini'")
    value = {'returncode': 1, 'output': 'FAILED\n'+line+'\n', 'process': {'confinement': {'backend': 'windows-mxc'}}}
    scopes = ('C:\\Windows', 'C:\\Users\\augus\\AppData\\Local\\Temp\\tare-dp\\storage\\wt\\0123abcd')
    what, shown = protection.sandbox_blocked(value, scopes)
    assert what.startswith('a file outside the folders checks may read (C:\\Users\\augus') and shown == line
    inside = line.replace('outside', 'storage\\\\wt\\\\0123abcd')
    assert protection.sandbox_blocked({**value, 'output': inside}, scopes) is None  # the test's own file: its failure
    assert protection.sandbox_blocked(value) is None  # no scopes known: no claim


@pytest.mark.parametrize('output,what', [
    (UNITTEST_NETWORK, 'a network connection'), (UNITTEST_SECRET, 'a secret file (.env)'),
    (UNITTEST_DNS, 'a network name lookup'), (NODE_ROOT, "the drive's root folder, which Node reads to load any script"),
    (NODE_SECRET, 'a secret file (.env)'), (NODE_CONNECT, 'a network connection'),
    (CARGO, 'the Rust toolchain in your user profile'), (UNITTEST_FAIL, None), (NODE_FAIL, None),
    (RUST_NETWORK, 'a network connection'), (RUST_SECRET, 'a file it may not read or write'),
    (RUST_WRITE, 'a file it may not read or write'), (CARGO_DEPENDENCY, 'a network name lookup'),
    (NODE_OPTION, "the drive's root folder, which Node reads to load any script"), (RUST_FAIL, None)])
def test_a_red_check_names_what_the_sandbox_blocked_during_it(output, what):
    value = {'returncode': 1, 'output': output, 'process': {'confinement': {'backend': 'windows-mxc'}}}
    found = protection.sandbox_blocked(value)
    assert (found and found[0]) == what
    if found:
        assert len(found[1]) <= 200 and '\n' not in found[1]
    assert protection.verification_limitation(value) is None  # the strict rule stays pytest-only
    for other in ({'returncode': 0}, {'process': {'confinement': {'backend': 'codex'}}}):
        assert protection.sandbox_blocked({**value, **other}) is None  # passing, or not this sandbox


# The errors the Linux sandbox (bubblewrap) gives (measured 2026-11-13), as Python, Node, Rust and bwrap print them.
@pytest.mark.parametrize('output,what', [
    ('OSError: [Errno 101] Network is unreachable', 'a network connection'),
    ('socket.gaierror: [Errno -3] Temporary failure in name resolution', 'a network name lookup'),
    (CARGO_DEPENDENCY, 'a network name lookup'),
    ('ConnectionRefusedError: [Errno 111] Connection refused', 'a connection to a service on this machine'),
    ('Error: connect ECONNREFUSED 127.0.0.1:5432', 'a connection to a service on this machine'),
    ("OSError: [Errno 30] Read-only file system: '/home/u/project/Cargo.lock'", 'a file it may not write'),
    ("PermissionError: [Errno 13] Permission denied: '/home/u/project/.env'", 'a secret file (.env)'),
    ("Error: EACCES: permission denied, open '/home/u/project/.env'", 'a secret file (.env)'),
    ('Os { code: 13, kind: PermissionDenied, message: "Permission denied" }', 'a file it may not read or write'),
    (CARGO, 'the Rust toolchain in your user profile'),
    ('bwrap: Creating new namespace failed: Operation not permitted', 'the sandbox itself, which could not start'),
    (UNITTEST_FAIL, None), (NODE_FAIL, None), (RUST_FAIL, None),
    (UNITTEST_NETWORK, None), (NODE_ROOT, None)])  # Windows' errors are not this sandbox's
def test_a_red_check_names_what_the_linux_sandbox_blocked_during_it(output, what):
    value = {'returncode': 1, 'output': output, 'process': {'confinement': {'backend': 'linux-bwrap'}}}
    found = protection.sandbox_blocked(value)
    assert (found and found[0]) == what
    assert protection.sandbox_blocked({**value, 'returncode': 0}) is None


def test_a_file_the_linux_sandbox_hides_is_the_sandboxs_and_a_missing_one_is_the_tests(monkeypatch):
    # Inside bubblewrap a path outside every scope does not exist: reading one is ENOENT, not a denial.
    value = {'returncode': 1, 'process': {'confinement': {'backend': 'linux-bwrap'}}}
    scopes = ('/usr', '/home/u/project')
    here = {'/home/u/notes/private.txt', '/home/u/project/data.txt'}  # what exists on this host
    monkeypatch.setattr(protection, 'os', SimpleNamespace(path=SimpleNamespace(exists=here.__contains__)))
    python = "FileNotFoundError: [Errno 2] No such file or directory: '/home/u/notes/private.txt'"
    for line in (python, "Error: ENOENT: no such file or directory, open '/home/u/notes/private.txt'",
                 "PermissionError: [Errno 13] Permission denied: '/home/u/notes/private.txt'"):
        what, shown = protection.sandbox_blocked({**value, 'output': line}, scopes)
        assert what == 'a file outside the folders checks may read (/home/u/notes/private.txt)' and shown == line
    for line in (python.replace('notes/private', 'project/data'),  # inside a scope: the test's own missing file
                 python.replace('private', 'gone')):  # missing here too: not the sandbox's
        assert protection.sandbox_blocked({**value, 'output': line}, scopes) is None
    assert protection.sandbox_blocked({**value, 'output': python}) is None  # no scopes known: no claim


@pytest.mark.parametrize('answer,outside_exit,state', [
    ('yes', 0, 'green'), ('yes', 1, 'red'), ('no', None, 'red'), ('headless', None, 'red')])
def test_a_red_check_the_sandbox_blocked_asks_and_only_the_rerun_outside_decides(host, monkeypatch, answer,
                                                                                outside_exit, state):
    unprotected(host, monkeypatch)
    monkeypatch.setattr(protection, 'sandbox_blocked', lambda value, readable=(): (
        'a network connection', 'PermissionError: [WinError 10013] An attempt was made to access a socket'))
    reruns, asked = [], []
    monkeypatch.setattr(work_units, 'managed_check', lambda argv, **kw: reruns.append(argv) or
                        subprocess.CompletedProcess(argv, outside_exit, 'rerun outside\n', ''))
    def outside(name, argv, why):
        asked.append(why)
        return (True, 'for this turn, as the user chose') if answer == 'yes' else (False, 'the user chose not to '
                                                                                         'run it outside the sandbox')
    run = protection.conversation_verifier(host.control, host.custody, host.config, {}, host.root, UNIT,
        {'tests': FAIL}, **({} if answer == 'headless' else {'outside': outside}))
    token = work_units.CHECK_PORT.set(run)
    try:
        result, receipt = work_units.run_checks(host.root, {'tests': FAIL})
    finally:
        work_units.CHECK_PORT.reset(token)
    assert result == state and not work_units.blocked_by_sandbox(receipt)  # red, never inconclusive
    if answer != 'headless':
        assert asked == ['ISOLATION_BLOCKED_DURING: a network connection. Output: PermissionError: [WinError 10013] '
                         'An attempt was made to access a socket']
    if answer == 'yes':
        assert reruns == [FAIL] and "this run's result decides" in receipt and 'rerun outside' in receipt
    else:
        assert reruns == [] and 'assertion failed' in receipt  # the sandboxed run's own output
        assert 'the sandbox blocked a network connection during this check' in receipt
        assert 'may come from the sandbox, not from the code' in receipt
        assert ('no one could be asked' if answer == 'headless' else 'the user chose not to') in receipt


@pytest.mark.parametrize('answer,platform', [('no', 'win32'), ('yes', 'win32'), ('no', 'linux')])
def test_a_check_that_runs_out_of_time_in_the_sandbox_asks_and_stays_unjudged(host, monkeypatch, answer, platform):
    # The Kernel keeps no output of a command stopped at its time limit (node_probe.py: hang/leak.test.mjs, 301 s).
    from compute_plane.native_capabilities import NativeCapabilities
    from tare_tools_agent_runtime import EffectResult
    from relay import conversation_permissions as permissions
    unprotected(host, monkeypatch)
    monkeypatch.setattr(NativeCapabilities, 'execute', lambda self, request, control: EffectResult(request.effect_id,
        'failed', json.dumps({'error_type': 'CommandInterrupted', 'process': {'timeout': True}})))
    reruns, asked = [], []
    monkeypatch.setattr(work_units, 'managed_check', lambda argv, **kw: reruns.append(argv) or
                        subprocess.CompletedProcess(argv, 0, 'passed outside\n', ''))
    run = protection.conversation_verifier(host.control, host.custody, host.config, {}, host.root, UNIT,
        {'tests': PASS}, outside=lambda name, argv, why: asked.append(why) or (answer == 'yes', 'the user chose'))
    protection.sys.platform = platform  # after binding: the factory refuses another platform before it binds
    token = work_units.CHECK_PORT.set(run)
    try:
        state, receipt = work_units.run_checks(host.root, {'tests': PASS})
    finally:
        work_units.CHECK_PORT.reset(token)
    # Not a raised "verification was refused: tests: timed out".
    assert asked == [protection.SANDBOX_TIMEOUT_LINUX if platform == 'linux' else protection.SANDBOX_TIMEOUT]
    assert permissions.sandbox_question(asked[0])[0].startswith('this check did not finish within its time limit')
    if answer == 'yes':
        assert state == 'green' and reruns == [PASS] and 'passed outside' in receipt
    else:  # Linux: the sandbox has its own loopback, so a test's own server is not what it waits for
        assert state == 'inconclusive' and reruns == []
        assert ('services of this machine' if platform == 'linux' else 'node --test does') in receipt
        assert not work_units.blocked_by_sandbox(receipt)  # a timeout settles nothing, as before (D9b)


def test_the_question_for_a_red_check_shows_the_line_and_says_the_rerun_decides():
    from relay import conversation_permissions as permissions
    question, line = permissions.sandbox_question(
        'ISOLATION_BLOCKED_DURING: a secret file (.env). Output: ' + "Error: EPERM: operation not permitted, open '"
        + 'x'*300 + "'")
    assert question.startswith('This check failed, and the sandbox blocked a secret file (.env) during it')
    assert 'its result decides' in question and line.startswith('Error: EPERM') and len(line) == 200


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


def test_the_question_asks_once_per_turn_honours_always_and_never_runs_outside_headless(tmp_path, monkeypatch):
    import threading
    import time
    from relay import conversation_permissions as permissions, relay_mesh
    from relay.turn_engine import checks_outside_sandbox
    from tare_tools_agent_runtime import BindingRef, CancellationToken, ExecutionControl
    monkeypatch.setattr(permissions, 'sys', SimpleNamespace(platform='win32'))  # its wording; Linux's is tested below
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


@pytest.mark.parametrize('platform', ['win32', 'linux'])
def test_the_network_question_says_what_this_platforms_sandbox_cannot_reach(monkeypatch, platform):
    from relay import conversation_permissions as permissions
    monkeypatch.setattr(permissions, 'sys', SimpleNamespace(platform=platform))
    question = permissions.sandbox_question(NETWORK)[0]
    assert question.startswith('The sandbox blocked a network connection')
    assert ('loopback included' in question) is (platform == 'win32')
    assert ('its own loopback' in question) is (platform == 'linux')


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
        assert ('bubblewrap' in str(error.value)) is (sys.platform == 'linux')  # what Linux needs, said there only
    else:
        assert session({'role': role}, deployment, tmp_path) == 'ran'


def test_without_flatbuffers_or_windows_the_probe_says_no_sandbox(monkeypatch):
    # CI's Linux shards pretend to be Windows elsewhere: the probe itself must not reach ctypes off Windows. Linux has
    # no sandbox yet even where bubblewrap works: its binding is in place, dormant until the probe asks the Kernel.
    monkeypatch.setattr('compute_plane.linux_bwrap.require_support', lambda: None)
    monkeypatch.setattr(protection, 'sys', SimpleNamespace(platform='linux'))
    assert protection.probe_sandbox() is False


@pytest.mark.parametrize('code', ['bwrap_scope_changed', 'bwrap_denied_path_changed'])
def test_a_launch_bubblewrap_refuses_is_the_sandbox_refusing_the_check_never_a_run_outside(host, monkeypatch, code):
    # A granted folder's real path or a masked secret moved since binding: the Kernel's profile refuses to launch.
    from relay.windows_isolation import windows_isolation_factory
    unprotected(host, monkeypatch)
    class Refusing:
        def __init__(self, policy, *, cwd, environment): pass
        def __call__(self, argv, **options): raise ValueError(code)
    monkeypatch.setattr('compute_plane.linux_bwrap.LinuxBwrapProfile', Refusing)
    monkeypatch.setattr(protection, 'windows_isolation_factory', windows_isolation_factory)  # the real composition
    monkeypatch.setattr(work_units, 'managed_check', lambda *a, **kw: pytest.fail('Nothing runs outside.'))
    protection.sys.platform = 'linux'
    run = protection.conversation_verifier(host.control, host.custody, host.config, {}, host.root, UNIT,
        {'tests': PASS}, outside=lambda *a: pytest.fail('A refused launch is not a question.'))
    with pytest.raises(ValueError, match=f'^Sandboxed verification was refused: tests: {code}$'):
        run(PASS)
