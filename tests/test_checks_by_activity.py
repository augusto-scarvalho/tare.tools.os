"""A check or command is judged by its activity, not by a fixed clock (owner, 2026-11-14). Real Python children with
the stall window and the unattended ceiling patched to seconds; the real sandbox is proven by the probe harness."""
import asyncio
import json
import re
import subprocess
import sys
import threading
import time

import pytest
from relay import command_protection as protection, conversation_permissions as permissions, work_units
from test_checks_in_sandbox import UNIT, unprotected
from test_command_protection import host  # noqa: F401  (the MXC launcher replaced, nothing else)

BUSY = [sys.executable, '-B', '-c', 'import time\nend = time.monotonic() + 7\nwhile time.monotonic() < end: pass']
SILENT = [sys.executable, '-B', '-c', 'import time; time.sleep(40)']
STALLED = 'it showed no activity (no output, no CPU time) for '


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr(work_units, 'STALL_SECONDS', 2)
    monkeypatch.setattr(work_units, 'UNATTENDED_SECONDS', 4)


class Asker:
    """A TUI's stall question: records each question and gives the scripted answers, the last one from then on."""

    def __init__(self, *answers):
        self.answers, self.asked = list(answers), []

    def __call__(self, name, argv, sample, outside=False):
        self.asked.append({'at': time.monotonic(), 'name': name, 'argv': argv, 'outside': outside, **sample})
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


def checks(commands, ask=None):
    """run_checks with no port (unsandboxed, managed_check) and `ask` as the conversation's stall question."""
    token = work_units.STALL_ASK.set(ask)
    began = time.monotonic()
    try:
        state, receipt = work_units.run_checks('.', commands)
    finally:
        work_units.STALL_ASK.reset(token)
    return state, receipt, time.monotonic() - began


def test_a_busy_check_that_prints_nothing_runs_past_the_stall_window_with_no_question(quick):
    ask = Asker('deny')
    state, receipt, seconds = checks({'busy': BUSY}, ask)
    assert state == 'green' and ask.asked == [] and seconds > 6  # three stall windows; no ceiling with someone to ask


def test_a_silent_check_is_asked_about_once_per_window_and_stopping_it_leaves_it_unjudged(quick):
    ask = Asker('once', 'deny')
    state, receipt, seconds = checks({'silent': SILENT}, ask)
    assert state == 'inconclusive' and 'silent: Check stopped: ' + STALLED in receipt and seconds < 15
    first, second = ask.asked
    assert second['at'] - first['at'] >= 2 - .05  # "keep waiting": another full window of silence first
    assert first['idle_ms'] >= 2000 and first['argv'] == SILENT and first['outside'] is False  # not sandboxed


def test_keep_waiting_lets_a_slow_silent_check_finish_green(quick):
    ask = Asker('once')
    state, receipt, _ = checks({'slow': [sys.executable, '-B', '-c', 'import time; time.sleep(5)']}, ask)
    assert state == 'green' and len(ask.asked) >= 1


def test_with_nobody_to_ask_a_silent_check_stops_at_the_window_and_an_active_one_at_the_ceiling(quick):
    state, receipt, seconds = checks({'silent': SILENT})
    assert state == 'inconclusive' and 'silent: Check stopped: ' + STALLED in receipt and seconds < 8
    state, receipt, seconds = checks({'busy': BUSY})
    assert state == 'inconclusive' and 3.5 < seconds < 7
    assert 'busy: Check stopped: it was still running (not stalled) when its time limit stopped it after ' in receipt


def test_unlimited_time_removes_the_ceiling_and_keeps_the_stall_stop(quick, monkeypatch):
    from dataclasses import replace
    from relay import execution_scope
    scope = replace(execution_scope.current(), unlimited=('time',))
    monkeypatch.setattr(execution_scope, 'current', lambda: scope)
    assert checks({'busy': BUSY})[0] == 'green'
    state, receipt, _ = checks({'silent': SILENT})
    assert state == 'inconclusive' and STALLED in receipt


def real_binding(monkeypatch):
    """The real windows_isolation binding, with a launcher that starts a plain process (no MXC in tests)."""
    from relay import windows_isolation

    class Launcher:
        job_limits = (16, 256 * 1024**2, 512 * 1024**2)  # MXC's own, before job_limits() sizes them

        def __init__(self, policy, cwd, environment):
            pass

        def __call__(self, argv, **options):
            return subprocess.Popen(argv, **options)
    monkeypatch.setattr(windows_isolation, 'WindowsMxcProfile', Launcher)
    monkeypatch.setattr(protection, 'windows_isolation_factory', windows_isolation.windows_isolation_factory)


def sandboxed(host, commands, ask, **options):
    token = work_units.STALL_ASK.set(ask)  # before the binding: it decides what the binding watches
    try:
        run = protection.conversation_verifier(host.control, host.custody, host.config, {}, host.root, UNIT, commands,
                                               **options)
        port = work_units.CHECK_PORT.set(run)
        try:
            return work_units.run_checks(host.root, commands)
        finally:
            work_units.CHECK_PORT.reset(port)
            getattr(run, 'close', lambda: None)()
    finally:
        work_units.STALL_ASK.reset(token)


def test_stop_and_run_outside_reruns_a_sandboxed_check_outside_with_the_stall_as_its_reason(host, monkeypatch, quick):
    unprotected(host, monkeypatch)
    real_binding(monkeypatch)
    outside_runs = []
    monkeypatch.setattr(work_units, 'managed_check', lambda argv, **kw: outside_runs.append(argv) or
                        subprocess.CompletedProcess(argv, 0, 'passed outside\n', ''))
    ask = Asker('outside')
    state, receipt = sandboxed(host, {'tests': SILENT}, ask,
                               outside=lambda *a: pytest.fail('The stall answer decides, not the sandbox question.'))
    assert state == 'green' and outside_runs == [SILENT] and 'passed outside' in receipt
    assert ask.asked[0]['name'] == 'tests' and ask.asked[0]['outside'] is True
    assert ("tests: run outside the sandbox by the user's decision, because in the sandbox " + STALLED) in receipt


def test_a_sandboxed_check_that_cannot_run_outside_is_not_offered_it_and_a_stop_leaves_it_unjudged(host, monkeypatch,
                                                                                                  quick):
    unprotected(host, monkeypatch)
    real_binding(monkeypatch)
    ask = Asker('deny')
    state, receipt = sandboxed(host, {'tests': SILENT}, ask)  # no `outside`: nothing may run outside
    assert state == 'inconclusive' and 'tests: Check stopped: ' + STALLED in receipt
    assert [a['outside'] for a in ask.asked] == [False]


def test_a_sandboxed_check_with_nobody_to_ask_is_stopped_at_the_window(host, monkeypatch, quick):
    unprotected(host, monkeypatch)
    real_binding(monkeypatch)
    began = time.monotonic()
    state, receipt = sandboxed(host, {'tests': SILENT}, None)
    assert state == 'inconclusive' and 'tests: Check stopped: ' + STALLED in receipt and time.monotonic()-began < 10


@pytest.mark.parametrize('case', ['asked', 'nobody', 'unlimited'])
def test_the_implementers_command_is_bound_by_activity_not_by_30_seconds(host, monkeypatch, case):
    from dataclasses import replace
    from relay import execution_scope
    bound, isolation = [], protection.windows_isolation_factory
    monkeypatch.setattr(protection, 'windows_isolation_factory', lambda **kw: bound.append(kw) or isolation(**kw))
    if case == 'unlimited':
        scope = replace(execution_scope.current(), unlimited=('time',))
        monkeypatch.setattr(execution_scope, 'current', lambda: scope)
    token = work_units.STALL_ASK.set(Asker('once') if case == 'asked' else None)
    try:
        host.build()
    finally:
        work_units.STALL_ASK.reset(token)
    options = bound[0]
    ceiling = None if case != 'nobody' else work_units.UNATTENDED_SECONDS
    assert options['unbounded_command'] is options['unbounded_verification'] is (ceiling is None)
    assert options['verification_seconds'] == ceiling and options['seconds'] == (ceiling or 30)  # 30: search only
    assert options['idle_seconds'] == (None if case == 'asked' else work_units.STALL_SECONDS)
    assert isinstance(options['on_progress'], work_units.StallWatch) is (case == 'asked')


def test_the_implementer_reads_whether_its_command_stalled_or_hit_its_limit(host):
    host.config['command_protection'] = protection.prepare({'review': 'codex', 'max_reviews': 2}, host.deployment)
    mediator = host.build()
    result = mediator.execute(host.facts(), lambda: {'exit_code': None, 'cleanup_verified': True, 'timeout': True,
                                                     'wall_ms': 7200300})
    assert result['reason'] == ('The process was still running (not stalled) when its time limit stopped it after '
                                '7200 seconds.')
    result = mediator.execute(host.facts('2'), lambda: {'exit_code': None, 'cleanup_verified': True, 'timeout': False,
                                                        'stalled': True, 'idle_ms': 300000})
    assert result['reason'].startswith('The process showed no activity (no output, no CPU time) for 300 seconds')


def test_a_work_runs_check_follows_the_unattended_rule(monkeypatch, quick, tmp_path):
    from tare_tools_agent_runtime import BindingRef, CancellationToken, ExecutionControl
    from relay.native_settlement import NativeWorkRunner
    control = ExecutionControl(BindingRef('work', 'binding', 'operator', 1, 'opaque'), CancellationToken(),
                               time.monotonic() + 60)
    run = NativeWorkRunner.verification_runner(control, tmp_path)  # no sandbox: the plain process
    with pytest.raises(ValueError, match=re.escape('NATIVE_CANDIDATE_VERIFIER_INTERRUPTED: ' + STALLED)):
        run(SILENT, cwd=tmp_path, timeout=60, env=None)


def stall_request(tmp_path, outside, answer):
    from tare_tools_agent_runtime import BindingRef, CancellationToken, ExecutionControl
    from relay import relay_mesh
    root, project = tmp_path/'session', tmp_path/'project'
    root.mkdir(); project.mkdir()
    control = ExecutionControl(BindingRef('work', 'binding', 'operator', 1, 'opaque'), CancellationToken(),
                               time.monotonic()+60)
    assert permissions.stall_question(root, {'session_id': 's'}, None, 'effect', project, control, None) is None
    ask = permissions.stall_question(root, {'session_id': 's'}, None, 'effect', project, control, tmp_path/'console')
    seen, errors = [], []

    def user():  # the TUI: answers the one request it sees
        while permissions.pending(root) is None:
            time.sleep(.01)
        request = permissions.pending(root)
        seen.append(request)
        for wrong in ('project', 'change', *(() if outside else ('outside',))):
            try:
                permissions.answer(root, request['id'], wrong, project=project, directory=tmp_path/'console',
                                   control_root=relay_mesh.RELAY_ROOT)
            except ValueError as error:
                errors.append(str(error))
        permissions.answer(root, request['id'], answer, project=project, directory=tmp_path/'console',
                           control_root=relay_mesh.RELAY_ROOT)
    thread = threading.Thread(target=user, daemon=True)
    thread.start()
    choice = ask('tests', SILENT, {'elapsed_ms': 754000, 'idle_ms': 301000, 'cpu_ms': 80, 'output_bytes': 0}, outside)
    thread.join(5)
    return choice, seen[0], errors


@pytest.mark.parametrize('outside,answer', [(True, 'outside'), (False, 'once')])
def test_the_stall_question_states_facts_and_takes_only_its_own_answers(tmp_path, outside, answer):
    choice, request, errors = stall_request(tmp_path, outside, answer)
    assert choice == answer and request['kind'] == 'stall' and request['checks'] == {'tests': SILENT}
    assert request['objective'] == ('The check tests has run for 12 min 34 s and has shown no activity for the last '
                                    '5 min 1 s: in that time it wrote no output and used no CPU time. tare has not '
                                    'stopped it.')
    assert 'sandbox' not in request['objective'] and request['stall'] == {'check': 'tests', 'outside': outside}
    assert len(errors) == 2 + (not outside) and all('Nothing was' in e for e in errors)  # project, change, outside


def test_the_old_sandbox_question_does_not_take_the_stall_answer(tmp_path):
    from relay import relay_mesh
    root = tmp_path/'session'; (root/'execution-permission').mkdir(parents=True)
    request = {'schema': permissions.SCHEMA, 'kind': 'sandbox', 'session': 's', 'effect': 'e', 'task': '', 'plan': '',
               'project': str(tmp_path), 'paths': [], 'checks': {'tests': SILENT}, 'tests': [], 'objective': 'q',
               'status': 'WAITING', 'sandbox': {'code': 'ISOLATION_NETWORK_DENIED', 'check': 'tests'}}
    request['id'] = permissions.identity(request)
    (root/'execution-permission'/'request.json').write_text(json.dumps(request))
    with pytest.raises(ValueError, match='Keep waiting or stop it'):
        permissions.answer(root, request['id'], 'outside', project=tmp_path, directory=tmp_path/'console',
                           control_root=relay_mesh.RELAY_ROOT)


@pytest.mark.parametrize('outside', [True, False])
def test_the_stall_dialog_starts_on_keep_waiting_and_escape_never_stops_it(tmp_path, outside):
    from textual.app import App
    from relay.execution_permission_panel import ExecutionPermissionPanel
    request = {'kind': 'stall', 'objective': 'The check tests has run for 12 min.', 'project': str(tmp_path),
               'checks': {'tests': SILENT}, 'stall': {'check': 'tests', 'outside': outside}}

    async def scenario():
        app, answers = App(), []
        async with app.run_test(size=(120, 40)) as pilot:
            for key in ('enter', 'escape'):
                app.push_screen(ExecutionPermissionPanel(request), answers.append)
                await pilot.pause()
                assert bool(app.screen.query('#permission-outside')) is outside
                await pilot.press(key); await pilot.pause()
        assert answers == ['once', 'once']
    asyncio.run(scenario())


def test_a_resumed_session_keeps_the_ceilings_it_lifted_and_nothing_more(tmp_path, monkeypatch):
    """Why a TUI conversation's units ran with 900 s and its checks with 300 s on 2026-11-14: a resumed turn gets no
    --unlimited, and the saved lift was not restored, so the unit's child session and checks lost it."""
    from relay import execution_scope
    monkeypatch.delenv(execution_scope.ENV, raising=False)
    (tmp_path/'native-agent.json').write_text(json.dumps({'config': {'role': 'implementer'}, 'deployment': None,
        'unlimited_resources': ['calls', 'time', 'money', 'compactions', 'stagnation']}))
    seen = execution_scope.session_role(lambda *a, **kw: execution_scope.current().unlimited)
    assert seen(None, None, tmp_path, resume=True) == ('calls', 'time', 'compactions', 'stagnation')  # not money
    assert seen(None, None, tmp_path, resume=True, unlimited_resources=('calls',)) == ('calls',)  # explicit wins
    from relay.execution_resources import selected
    lifted = execution_scope.session_role(lambda *a, **kw: selected('time'))
    assert lifted(None, None, tmp_path, resume=True)  # what work_units.child_limits and run_checks read in the turn


def test_a_busy_check_silent_past_quiet_output_window_asks_with_live_tail_and_stops_honestly(monkeypatch):
    monkeypatch.setattr(work_units, 'STALL_SECONDS', 5)
    monkeypatch.setattr(work_units, 'QUIET_OUTPUT_SECONDS', 2)
    busy_after_line = [
        sys.executable, '-B', '-c',
        'import time\nprint("BEFORE_HANG_LINE", flush=True)\n'
        'end = time.monotonic() + 30\nwhile time.monotonic() < end: pass',
    ]
    ask = Asker('once', 'deny')
    state, receipt, seconds = checks({'busy': busy_after_line}, ask)
    assert state == 'inconclusive' and seconds < 15
    assert 'busy: Check stopped: it produced no new output while using CPU and was stopped after ' in receipt
    assert len(ask.asked) == 2
    first, second = ask.asked
    assert second['at'] - first['at'] >= 2 - .1
    assert first['idle_ms'] < 5000 and first['output_idle_ms'] >= 2000
    assert 'BEFORE_HANG_LINE' in first.get('output_tail', '')


def test_stall_watch_keeps_running_on_background_answer(monkeypatch):
    monkeypatch.setattr(work_units, 'STALL_SECONDS', 300)
    monkeypatch.setattr(work_units, 'QUIET_OUTPUT_SECONDS', 600)
    now = [0.0]
    asked = []

    def ask(name, argv, sample, outside):
        asked.append((now[0], name, sample))
        return 'background'

    watch = work_units.StallWatch(ask, clock=lambda: now[0])
    watch.watch('e1', 'tests', ['godot'])
    now[0] = 10.0
    assert watch('e1', {'elapsed_ms': 10_000, 'idle_ms': 0, 'cpu_ms': 9_000, 'output_bytes': 120}) is False
    now[0] = 609.0
    assert watch('e1', {'elapsed_ms': 609_000, 'idle_ms': 0, 'cpu_ms': 600_000, 'output_bytes': 120}) is False
    assert asked == []
    now[0] = 610.0
    assert watch('e1', {'elapsed_ms': 610_000, 'idle_ms': 0, 'cpu_ms': 601_000, 'output_bytes': 120}) is False
    assert len(asked) == 1 and asked[0][2]['output_idle_ms'] >= 600_000
    # After choosing 'background', another full QUIET_OUTPUT_SECONDS passes before asking again.
    now[0] = 1209.0
    assert watch('e1', {'elapsed_ms': 1_209_000, 'idle_ms': 0, 'cpu_ms': 1_200_000, 'output_bytes': 120}) is False
    assert len(asked) == 1
    now[0] = 1210.0
    assert watch('e1', {'elapsed_ms': 1_210_000, 'idle_ms': 0, 'cpu_ms': 1_201_000, 'output_bytes': 120}) is False
    assert len(asked) == 2


def test_quiet_output_stall_question_includes_tail_and_validates_background_choice(tmp_path):
    from tare_tools_agent_runtime import BindingRef, CancellationToken, ExecutionControl
    from relay import relay_mesh
    from textual.app import App
    from relay.execution_permission_panel import ExecutionPermissionPanel

    root, project = tmp_path / 'session', tmp_path / 'project'
    root.mkdir(); project.mkdir()
    control = ExecutionControl(BindingRef('work', 'binding', 'operator', 1, 'opaque'), CancellationToken(),
                               time.monotonic() + 60)
    ask = permissions.stall_question(root, {'session_id': 's'}, None, 'effect', project, control, tmp_path / 'console')
    seen = []

    def user():
        while permissions.pending(root) is None:
            time.sleep(.01)
        req = permissions.pending(root)
        seen.append(req)
        permissions.answer(root, req['id'], 'background', project=project, directory=tmp_path / 'console',
                           control_root=relay_mesh.RELAY_ROOT)

    thread = threading.Thread(target=user, daemon=True)
    thread.start()
    choice = ask('tests', BUSY, {
        'elapsed_ms': 612_000, 'idle_ms': 0, 'cpu_ms': 605_000, 'output_bytes': 2044,
        'output_idle_ms': 601_000, 'output_tail': 'round 0 ok\nround 1 ok',
    }, False)
    thread.join(5)
    assert choice == 'background' and len(seen) == 1
    req = seen[0]
    assert 'no new output for the last 10 min 1 s' in req['objective']
    assert 'while still using CPU' in req['objective']
    assert req['stall'] == {'check': 'tests', 'outside': False, 'reason': 'quiet_output',
                            'output_tail': 'round 0 ok\nround 1 ok'}

    # Non-stall requests must reject 'background'.
    sandbox_req = {'schema': permissions.SCHEMA, 'kind': 'sandbox', 'session': 's', 'effect': 'e', 'task': '',
                   'plan': '', 'project': str(project.resolve()), 'paths': [], 'checks': {'tests': SILENT},
                   'tests': [], 'objective': 'q', 'status': 'WAITING',
                   'sandbox': {'code': 'ISOLATION_NETWORK_DENIED', 'check': 'tests'}}
    sandbox_req['id'] = permissions.identity(sandbox_req)
    (root / 'execution-permission' / 'request.json').write_text(json.dumps(sandbox_req))
    with pytest.raises(ValueError, match='Keep waiting or stop it'):
        permissions.answer(root, sandbox_req['id'], 'background', project=project, directory=tmp_path / 'console',
                           control_root=relay_mesh.RELAY_ROOT)

    # Panel renders recent output and exposes the Continue in background button.
    async def scenario():
        app, answers = App(), []
        async with app.run_test(size=(120, 40)) as pilot:
            app.push_screen(ExecutionPermissionPanel(req), answers.append)
            await pilot.pause()
            assert app.screen.query('#permission-background')
            await pilot.click('#permission-background')
            await pilot.pause()
        assert answers == ['background']
    asyncio.run(scenario())
