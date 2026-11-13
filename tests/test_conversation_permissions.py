"""Actual operator decisions, bound to the plan; no provider calls while waiting."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
import json
from threading import Event
import time

import pytest

from relay import conversation_permissions as permissions, conversation_tasks as tasks, relay_mesh, work_units
from relay.project_permissions import read_defaults, save_defaults, save_show_plan_default
from test_conversation_tasks import host, intake, planned, effect  # noqa: F401


@contextmanager
def permission_worker(control, workers=1):
    with ThreadPoolExecutor(max_workers=workers) as pool:
        try:
            yield pool
        finally:
            # A failed assertion or a slow request must not leave the worker waiting
            # for a user answer while the executor joins it indefinitely.
            control.cancellation.cancel()


def wait_request(root, questions=permissions):
    deadline = time.monotonic()+3
    while time.monotonic() < deadline:
        try:
            request = questions.pending(root)
        except PermissionError:  # Windows: the worker is replacing request.json; the TUI polls again too
            request = None
        if request: return request
        time.sleep(.01)
    raise AssertionError('No permission request appeared')


def test_selected_command_policy_reaches_workers_without_expanding_coordinator(host, tmp_path, monkeypatch):
    from copy import deepcopy
    config = host[0].state['config']; before = deepcopy(config)
    policy = {'review': 'auto', 'backend': 'mxc', 'max_reviews': 6, 'review_seconds': 60}
    monkeypatch.setattr('relay.project_permissions.read_defaults', lambda *a: (
        {'mode': 'commands', 'write_paths': ['.'], 'command_protection': policy}, 'revision'))
    worker = permissions.with_review_policy(config, tmp_path, host[2], relay_mesh.RELAY_ROOT)
    assert worker['capabilities'] == ['command'] and worker['command_protection'] == policy
    assert config == before


def test_candidate_checks_use_the_source_environment_and_owned_pytest_scratch(tmp_path):
    project, candidate = tmp_path/'source', tmp_path/'candidate'
    (project/'.venv/Scripts').mkdir(parents=True); candidate.mkdir()
    python = project/'.venv/Scripts/python.exe'; python.write_bytes(b'fixture; not executed')
    config = {'repositories': {'repo': str(project)}, 'verifiers': {}, 'conversation_flow': True}
    checks = work_units.checks(config, {'test_paths': ['tests/test_graph.py']}, 'repo', candidate)
    argv = checks['pytest-tests:tests/test_graph.py']
    assert argv[0] == str(python)
    assert argv[-2:] == ['--basetemp', str(tmp_path/'.candidate-pytest')]
    assert str(candidate) not in str(argv[0]) and 'tests/test_graph.py' in argv


@pytest.mark.parametrize('choice', ['once', 'project', 'deny'])
def test_operator_decision_resumes_the_same_plan_without_changing_coordinator_scope(host, tmp_path, choice):
    tools, control, root, seen = host
    intake(host); task = planned(host)
    tools.state.update(session_id='parent')
    tools.state['config']['writable'] = {}
    original = deepcopy(tools.state)
    directory = tmp_path/'operator-prefs'
    with permission_worker(control) as pool:
        running = pool.submit(permissions.authorize, tools.root, tools.state, task, 'implementation', control, directory)
        request = wait_request(tools.root)
        assert not running.done() and not seen and (root/'a.py').read_text() == 'value = 1\n'
        assert request['paths'] == ['a.py'] and request['plan'] == task['plan']['sha256']
        assert request['files_allowed'] is False and 'artifact' in request  # one dialog: the plan and its files
        permissions.answer(tools.root, request['id'], choice, project=root, directory=directory,
                           control_root=relay_mesh.RELAY_ROOT)
        if choice == 'deny':
            with pytest.raises(PermissionError, match='denied'): running.result(timeout=3)
        else:
            worker = running.result(timeout=3)
            assert worker['writable'] == {'repo': ['a.py']}
    assert tools.state == original and not permissions.pending(tools.root)
    assert tasks.current(tasks.load(tools.root))['plan'] == task['plan']
    # Use the same product control identity as the frontend does.
    defaults, _ = read_defaults(directory, root, relay_mesh.RELAY_ROOT)
    assert defaults['mode'] == ('edit' if choice == 'project' else 'read')


@pytest.mark.parametrize('choice',['once','deny'])
def test_host_implements_only_after_operator_decision_and_never_reasks_after_denial(host,tmp_path,monkeypatch,choice):
    tools,control,root,_=host
    intake(host);task=planned(host)
    tools.state.update(session_id='parent');tools.state['config']['writable']={}
    tools.permission_directory=tmp_path/'preferences'
    workers=[]
    def run_unit(custody,state,*args,**kwargs):
        workers.append(deepcopy(state['config']))
        return {'verdict':'VERIFIED','applied':True,'files':['a.py']}
    monkeypatch.setattr(work_units,'run_unit',run_unit)
    with permission_worker(control) as pool:
        running=pool.submit(tools.execute,effect('implement',{},'execution-1'),control)
        request=wait_request(tools.root)
        assert workers==[]
        permissions.answer(tools.root,request['id'],choice,project=root,
            directory=tools.permission_directory,control_root=relay_mesh.RELAY_ROOT)
        result=running.result(timeout=3)
    assert tools.state['config']['writable']=={} and 'edit' not in tools.names
    assert tasks.current(tasks.load(tools.root))['plan']==task['plan']
    if choice=='once':
        assert result.status=='succeeded' and workers[0]['writable']=={'repo':['a.py']}
        assert tasks.current(tasks.load(tools.root))['stage']=='DELIVERED'
    else:
        assert result.status=='refused' and workers==[]
        assert tools.execute(effect('implement',{},'execution-2'),control).status=='refused'
        assert not permissions.pending(tools.root)


def test_stale_or_other_project_answer_cannot_grant_access(host, tmp_path):
    tools, control, root, _ = host
    intake(host); task=planned(host)
    tools.state.update(session_id='parent'); tools.state['config']['writable']={}
    directory=tmp_path/'preferences'
    with permission_worker(control) as pool:
        running=pool.submit(permissions.authorize,tools.root,tools.state,task,'effect',control,directory)
        request=wait_request(tools.root)
        for key, project in [('old-request',root),(request['id'],tmp_path)]:
            with pytest.raises(ValueError,match='changed or ended'):
                permissions.answer(tools.root,key,'once',project=project,directory=directory,control_root=tmp_path)
        assert not running.done()
        control.cancellation.cancel()
        with pytest.raises(PermissionError,match='cancellation'): running.result(timeout=3)
    assert not permissions.pending(tools.root)


def test_permission_worker_cancels_waiting_request_when_test_body_fails(host, tmp_path):
    tools, control, *_ = host
    intake(host); task = planned(host)
    tools.state.update(session_id='parent'); tools.state['config']['writable'] = {}
    with pytest.raises(AssertionError, match='fixture body failed'):
        with permission_worker(control) as pool:
            running = pool.submit(permissions.authorize, tools.root, tools.state, task,
                                  'effect', control, tmp_path/'preferences')
            wait_request(tools.root)
            raise AssertionError('fixture body failed')
    with pytest.raises(PermissionError, match='cancellation'):
        running.result(timeout=3)
    assert not permissions.pending(tools.root)


@pytest.mark.parametrize('transition', ['request', 'answer'])
@pytest.mark.parametrize('choice', ['once', 'deny'])
def test_visible_permission_transition_waits_for_its_writer_to_release(host, tmp_path, monkeypatch, transition, choice):
    tools, control, root, _ = host
    intake(host); task = planned(host)
    tools.state.update(session_id='parent'); tools.state['config']['writable'] = {}
    directory = tmp_path/'preferences'
    held, release, contended = Event(), Event(), Event()
    save, lock = permissions.save_json, permissions.custody_lock

    def save_and_hold(path, value):
        save(path, value)
        if path.name == transition+'.json' and value.get('status', 'WAITING') == 'WAITING':
            held.set()
            release.wait(3)

    @contextmanager
    def observed_lock(folder):
        try:
            with lock(folder):
                yield
        except ValueError as exc:
            if str(exc) == 'native_custody_has_active_writer':
                contended.set()
            raise

    monkeypatch.setattr(permissions, 'save_json', save_and_hold)
    monkeypatch.setattr(permissions, 'custody_lock', observed_lock)
    with permission_worker(control, workers=2) as pool:
        try:
            running = pool.submit(permissions.authorize, tools.root, tools.state, task, 'effect', control, directory)
            request = wait_request(tools.root)
            answering = pool.submit(permissions.answer, tools.root, request['id'], choice,
                                   project=root, directory=directory, control_root=relay_mesh.RELAY_ROOT)
            assert held.wait(3) and contended.wait(3), 'Did not exercise concurrent permission writers'
        finally:
            release.set()
        assert answering.result(timeout=3) == choice
        if choice == 'deny':
            with pytest.raises(PermissionError, match='denied'):
                running.result(timeout=3)
        else:
            assert running.result(timeout=3)['writable'] == {'repo': ['a.py']}
    assert not permissions.pending(tools.root)


def test_decision_lock_retries_only_acquisition_and_bounds_contention(tmp_path, monkeypatch):
    clock = iter([0, 0, 3])
    attempts = []

    @contextmanager
    def busy(folder):
        attempts.append(folder)
        raise ValueError('native_custody_has_active_writer')
        yield

    monkeypatch.setattr(permissions, 'custody_lock', busy)
    monkeypatch.setattr(permissions.time, 'monotonic', lambda: next(clock))
    monkeypatch.setattr(permissions.time, 'sleep', lambda _: None)
    with pytest.raises(ValueError, match='native_custody_has_active_writer'):
        with permissions.decision_lock(tmp_path):
            pytest.fail('Busy lock must not enter the decision')
    assert attempts == [tmp_path, tmp_path]


def test_decision_effect_failure_is_never_replayed(tmp_path, monkeypatch):
    calls = []

    @contextmanager
    def available(folder):
        calls.append(folder)
        yield

    monkeypatch.setattr(permissions, 'custody_lock', available)
    with pytest.raises(ValueError, match='native_custody_has_active_writer'):
        with permissions.decision_lock(tmp_path):
            # Even the same error text from a decision body cannot trigger a replay.
            raise ValueError('native_custody_has_active_writer')
    assert calls == [tmp_path]


def go_straight_on(directory, project):
    defaults, revision = read_defaults(directory, project, relay_mesh.RELAY_ROOT)
    save_defaults(directory, project, relay_mesh.RELAY_ROOT, {**defaults, 'show_plan': False}, revision)


@pytest.mark.parametrize('mode',['edit','commands'])
def test_remembered_project_or_yolo_policy_needs_no_question_even_in_old_read_only_chat(host,tmp_path,mode):
    from relay.turn_engine import set_project_permission_mode
    tools,control,root,_=host
    intake(host); task=planned(host)
    tools.state['config']['writable']={}
    go_straight_on(tmp_path/'prefs', root)
    set_project_permission_mode(root,mode)
    worker=permissions.authorize(tools.root,tools.state,task,'effect',control,tmp_path/'prefs')
    assert worker['writable']=={'repo':['a.py']}
    assert tools.state['config']['writable']=={} and not permissions.pending(tools.root)


@pytest.mark.parametrize('mine', [None, True, False])
@pytest.mark.parametrize('project', [None, True, False])
def test_before_implementing_is_the_projects_choice_then_my_default_then_show_and_ask(host, tmp_path, mine, project):
    tools, control, root, _ = host
    intake(host); task = planned(host)
    tools.state.update(session_id='parent')
    directory = tmp_path/'prefs'
    if mine is not None:
        save_show_plan_default(directory, mine)
    if project is not None:
        defaults, revision = read_defaults(directory, root, relay_mesh.RELAY_ROOT)
        save_defaults(directory, root, relay_mesh.RELAY_ROOT, {**defaults, 'show_plan': project}, revision)
    asks = project if project is not None else mine if mine is not None else True
    with permission_worker(control) as pool:
        running = pool.submit(permissions.authorize, tools.root, tools.state, task, 'effect', control, directory)
        if asks:
            assert 'artifact' in wait_request(tools.root) and not running.done()
        else:
            assert running.result(timeout=3) == tools.state['config'] and not permissions.pending(tools.root)


def test_a_project_without_a_choice_follows_a_changed_default(host, tmp_path):
    tools, control, _, _ = host
    intake(host); task = planned(host)
    tools.state.update(session_id='parent')
    directory = tmp_path/'prefs'
    save_show_plan_default(directory, False)
    with permission_worker(control) as pool:
        running = pool.submit(permissions.authorize, tools.root, tools.state, task, 'effect', control, directory)
        assert running.result(timeout=3) == tools.state['config'] and not permissions.pending(tools.root)
        save_show_plan_default(directory, True)  # read again when the next plan is about to be implemented
        running = pool.submit(permissions.authorize, tools.root, tools.state, task, 'again', control, directory)
        assert 'artifact' in wait_request(tools.root) and not running.done()


def test_go_straight_on_keeps_todays_permission_question(host, tmp_path):
    tools, control, root, _ = host
    intake(host); task = planned(host)
    tools.state.update(session_id='parent')
    directory = tmp_path/'prefs'
    go_straight_on(directory, root)
    # Session edit scope already allows the plan's files: no question, no folder, the session's scope unchanged.
    assert permissions.authorize(tools.root, tools.state, task, 'effect', control, directory) == tools.state['config']
    assert not (tools.root/'execution-permission').exists()
    tools.state['config']['writable'] = {}
    with permission_worker(control) as pool:
        running = pool.submit(permissions.authorize, tools.root, tools.state, task, 'effect', control, directory)
        request = wait_request(tools.root)
        assert set(request) == {'schema', 'session', 'effect', 'task', 'plan', 'project', 'paths', 'checks',
                                'tests', 'objective', 'status', 'id'}  # exactly today's request
        with pytest.raises(ValueError, match='no plan'):
            permissions.answer(tools.root, request['id'], 'change', project=root, directory=directory,
                               control_root=relay_mesh.RELAY_ROOT)
        permissions.answer(tools.root, request['id'], 'once', project=root, directory=directory,
                           control_root=relay_mesh.RELAY_ROOT)
        assert running.result(timeout=3) == {**tools.state['config'], 'writable': {'repo': ['a.py']}}
    folder = tools.root/'execution-permission'
    assert (folder/'granted.json').exists() and not (folder/'approved.json').exists()


def test_plan_is_shown_even_where_edits_are_allowed_and_asked_once_per_plan(host, tmp_path):
    tools, control, root, _ = host
    intake(host); task = planned(host)
    tools.state.update(session_id='parent')
    directory = tmp_path/'prefs'
    # Nobody to ask without a frontend: today's behaviour.
    assert permissions.authorize(tools.root, tools.state, task, 'effect', control, None) == tools.state['config']
    with permission_worker(control) as pool:
        running = pool.submit(permissions.authorize, tools.root, tools.state, task, 'effect', control, directory)
        request = wait_request(tools.root)
        assert request['files_allowed'] is True and request['artifact'] == task['plan']['artifact']
        text = permissions.plan_text(tools.root, request)
        assert 'Change a.py.' in text and '1. Edit a.py.' in text and 'Files: a.py' in text
        permissions.answer(tools.root, request['id'], 'once', project=root, directory=directory,
                           control_root=relay_mesh.RELAY_ROOT)
        assert running.result(timeout=3) == tools.state['config']  # the session's own scope, not narrowed
        # A correction under the same plan does not ask again; a different plan does.
        assert permissions.authorize(tools.root, tools.state, task, 'again', control, directory) == tools.state['config']
        replanned = deepcopy(task); replanned['plan']['sha256'] = 'f'*64
        asking = pool.submit(permissions.authorize, tools.root, tools.state, replanned, 'replanned', control, directory)
        assert wait_request(tools.root)['plan'] == 'f'*64 and not asking.done()
    assert not (tools.root/'execution-permission'/'granted.json').exists()


def turn_control(state, seconds):
    """The session's control as native_agent builds it: on a TurnClock that moves the saved deadline too."""
    from tare_tools_agent_runtime import BindingRef, CancellationToken, ExecutionControl
    from relay.worker_wait import TurnClock
    clock = TurnClock(state)
    return ExecutionControl(BindingRef('parent', 'binding', 'owner', 1, 'opaque'), CancellationToken(),
                            clock()+seconds, monotonic=clock)


def test_waiting_for_the_answer_spends_none_of_the_turns_time(host, tmp_path):
    from relay.worker_wait import steady
    tools, _, root, _ = host
    intake(host); task = planned(host)
    tools.state.update(session_id='parent', deadline_utc=time.time()+.5)
    control = turn_control(tools.state, .5)  # the turn's limit is shorter than the user's reading
    directory = tmp_path/'prefs'
    # Without a frontend nothing waits and no clock moves, as before.
    saved = tools.state['deadline_utc']
    assert permissions.authorize(tools.root, tools.state, task, 'headless', control, None) == tools.state['config']
    assert tools.state['deadline_utc'] == saved and 'answer_wait_seconds' not in tools.state
    with permission_worker(control) as pool:
        running = pool.submit(permissions.authorize, tools.root, tools.state, task, 'effect', control, directory)
        request = wait_request(tools.root)
        left, left_utc = control.remaining_seconds(), tools.state['deadline_utc']-time.time()
        time.sleep(1)
        assert not running.done() and not control.should_stop()
        permissions.answer(tools.root, request['id'], 'once', project=root, directory=directory,
                           control_root=relay_mesh.RELAY_ROOT)
        assert running.result(timeout=3) == tools.state['config']  # the late answer is honoured
        assert abs(control.remaining_seconds()-left) < .2
        assert abs(tools.state['deadline_utc']-time.time()-left_utc) < .3 and tools.state['answer_wait_seconds'] >= 1
        # Code that reads time.monotonic() gets the same time left.
        real = steady(control)
        assert abs(real.deadline_monotonic-time.monotonic()-control.remaining_seconds()) < .1


def test_cancellation_still_ends_the_paused_wait(host, tmp_path):
    tools, _, _, _ = host
    intake(host); task = planned(host)
    tools.state.update(session_id='parent')
    control = turn_control(tools.state, .3)
    with permission_worker(control) as pool:
        running = pool.submit(permissions.authorize, tools.root, tools.state, task, 'effect', control, tmp_path/'prefs')
        wait_request(tools.root)
        time.sleep(.6)  # past the turn's limit: still waiting, because its time is not running
        assert not running.done()
        control.cancellation.cancel()
        with pytest.raises(PermissionError, match='cancellation'):
            running.result(timeout=3)
    assert not permissions.pending(tools.root)


def test_an_implementers_question_stops_the_delegating_turns_clock_too():
    from tare_tools_agent_runtime import BindingRef, CancellationToken, ExecutionControl
    from relay.worker_wait import TurnClock, waiting_for_user
    parent_state, child_state = {'deadline_utc': time.time()+60}, {'deadline_utc': time.time()+30}
    parent = turn_control(parent_state, 60)
    clock = TurnClock(child_state, parent.monotonic)  # as run_unit hands native_agent the coordinator's clock
    child = ExecutionControl(BindingRef('child', 'binding', 'owner', 1, 'opaque'), CancellationToken(),
                             clock()+30, monotonic=clock)
    before = (parent.remaining_seconds(), child.remaining_seconds(),
              parent_state['deadline_utc'], child_state['deadline_utc'])
    with waiting_for_user(child):
        time.sleep(.5)
    assert abs(parent.remaining_seconds()-before[0]) < .1 and abs(child.remaining_seconds()-before[1]) < .1
    assert parent_state['deadline_utc']-before[2] >= .5 and child_state['deadline_utc']-before[3] >= .5
    assert TurnClock(None, time.monotonic).parent is None  # a parent off a TurnClock keeps counting, as before


def command_question(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from relay import command_permission
    root = tmp_path/'approvals'
    monkeypatch.setenv(command_permission.ROOT_ENV, str(root))
    monkeypatch.setattr(command_permission, 'LEASE', .4)  # renewed about every .2 s
    action = SimpleNamespace(action_sha256='a'*64, effect_id='effect', cwd=str(tmp_path/'project'),
                             argv=('python', 'check.py'), deadline=time.monotonic())  # its review window has passed
    return command_permission, root, action


def test_run_this_command_waits_for_the_answer_and_spends_none_of_the_turns_time(tmp_path, monkeypatch):
    questions, root, action = command_question(tmp_path, monkeypatch)
    state = {'deadline_utc': time.time()+.5}
    control = turn_control(state, .5)
    with permission_worker(control) as pool:
        asking = pool.submit(questions.approve, action, 'Run the check.', control)
        question = wait_request(root, questions)
        left = control.remaining_seconds()
        time.sleep(1)  # past the action's window, the turn's limit and two leases
        assert not asking.done() and not control.should_stop()
        renewed = wait_request(root, questions)
        assert renewed['id'] == question['id'] and renewed['expires'] > question['expires']
        questions.answer(root, question['id'], 'once')
        assert asking.result(timeout=3) is True  # the late answer is honoured
        assert abs(control.remaining_seconds()-left) < .2 and state['answer_wait_seconds'] >= 1
    assert not root.exists()


def test_an_abandoned_command_question_leaves_the_console_and_a_live_one_is_not_replayed(tmp_path, monkeypatch):
    from compute_plane.action_control import ActionRefused
    questions, root, action = command_question(tmp_path, monkeypatch)
    # What an asker that died without cleaning up leaves: a question nobody renews.
    stale = root/('b'*64); stale.mkdir(parents=True)
    permissions.save_json(stale/'request.json', {'schema': questions.SCHEMA, 'kind': 'command', 'id': 'b'*64,
        'status': 'WAITING', 'expires': time.time()+.3, 'project': str(tmp_path), 'cwd': str(tmp_path),
        'argv': ['python', 'old.py'], 'objective': 'Review this command.'})
    assert wait_request(root, questions)['id'] == 'b'*64
    time.sleep(.4)
    assert questions.pending(root) is None
    with pytest.raises(ValueError, match='expired'):
        questions.answer(root, 'b'*64, 'once')
    control = turn_control({}, .3)
    with permission_worker(control) as pool:
        asking = pool.submit(questions.approve, action, 'Run the check.', control)
        wait_request(root, questions)
        with pytest.raises(ActionRefused, match='already pending; it is not replayed'):
            questions.approve(action, 'Run the check.', control)
        time.sleep(.6)  # past the turn's limit: only cancellation (or the answer) closes it
        assert not asking.done()
        control.cancellation.cancel()
        with pytest.raises(ActionRefused, match='cancelled'):
            asking.result(timeout=3)
    assert questions.pending(root) is None


def test_the_implementer_is_funded_from_the_time_left_before_the_question(host, tmp_path, monkeypatch):
    tools, _, root, _ = host
    intake(host); task = planned(host)
    tools.state.update(session_id='parent', deadline_utc=time.time()+300)
    tools.permission_directory = tmp_path/'prefs'
    control = turn_control(tools.state, 300)
    funded = []
    monkeypatch.setattr(work_units, 'run_unit', lambda custody, state, *a, **kw: funded.append(
        state['deadline_utc']-time.time()) or {'verdict': 'VERIFIED', 'applied': True, 'files': ['a.py']})
    with permission_worker(control) as pool:
        running = pool.submit(tools.execute, effect('implement', {}, 'execution-1'), control)
        request = wait_request(tools.root)
        left = tools.state['deadline_utc']-time.time()
        time.sleep(1)
        permissions.answer(tools.root, request['id'], 'once', project=root, directory=tools.permission_directory,
                           control_root=relay_mesh.RELAY_ROOT)
        assert running.result(timeout=3).status == 'succeeded'
    assert len(funded) == 1 and abs(funded[0]-left) < .5


def test_change_the_plan_starts_nothing_tells_the_coordinator_and_asks_about_the_next_plan(host, tmp_path, monkeypatch):
    tools, control, root, _ = host
    intake(host); task = planned(host)
    tools.state.update(session_id='parent')
    tools.permission_directory = tmp_path/'prefs'
    workers = []
    monkeypatch.setattr(work_units, 'run_unit', lambda custody, state, *a, **kw: workers.append(state) or
                        {'verdict': 'VERIFIED', 'applied': True, 'files': ['a.py']})
    with permission_worker(control) as pool:
        running = pool.submit(tools.execute, effect('implement', {}, 'execution-1'), control)
        request = wait_request(tools.root)
        permissions.answer(tools.root, request['id'], 'change', project=root, directory=tools.permission_directory,
                           control_root=relay_mesh.RELAY_ROOT, changes='  Keep the old value as a comment. ')
        result = running.result(timeout=3)
        reason = json.loads(result.output_json)['reason']
        assert result.status == 'refused' and workers == []
        assert 'delegate_plan' in reason and reason.endswith('The user wrote: Keep the old value as a comment.')
        saved = tasks.current(tasks.load(tools.root))
        assert saved['replan_from'] == {'artifact': task['plan']['artifact'], 'sha256': task['plan']['sha256']}
        assert saved['planning_revision'] == task.get('planning_revision', 0)+1
        # The same plan is not offered again in this turn.
        again = tools.execute(effect('implement', {}, 'execution-2'), control)
        assert again.status == 'refused' and 'change this plan' in again.output_json and not permissions.pending(tools.root)
        # The revised plan is asked about again, then implemented.
        artifact = tools.root/'planning'/'revised'/'session'/'artifact.json'; artifact.parent.mkdir(parents=True)
        payload = json.loads(open(task['plan']['artifact'], encoding='utf-8').read())['payload']
        permissions.save_json(artifact, {'payload': {**payload, 'steps': ['Edit a.py; keep the old value as a comment.']}})
        value = tasks.load(tools.root); tasks.task_plan(tools.root, tools.state['config'], tasks.current(value),
                                                       {'artifact': str(artifact)}); tasks.save(tools.root, value)
        running = pool.submit(tools.execute, effect('implement', {}, 'execution-3'), control)
        request = wait_request(tools.root)
        assert 'keep the old value as a comment' in permissions.plan_text(tools.root, request)
        permissions.answer(tools.root, request['id'], 'once', project=root, directory=tools.permission_directory,
                           control_root=relay_mesh.RELAY_ROOT)
        assert running.result(timeout=3).status == 'succeeded' and len(workers) == 1


def test_missing_plan_reports_failed_planning_instead_of_claiming_preserved_plan(host):
    tools,control,*_=host
    intake(host); tools.state['config']['writable']={}
    result=tools.execute(effect('implement',{}),control)
    assert result.status=='refused' and 'planner must produce' in result.output_json
    assert 'preserved' not in result.output_json and not permissions.pending(tools.root)


def test_recovery_closes_only_the_interrupted_permission_request_without_granting(host):
    from dataclasses import asdict
    from compute_plane.native_journal import NativeJournal
    tools, _, root, _ = host
    intake(host); task=planned(host)
    request=effect('implement',{},'pending')
    folder=tools.root/'execution-permission';folder.mkdir()
    record={'schema':permissions.SCHEMA,'session':'parent','effect':request.effect_id,
        'task':task['id'],'plan':task['plan']['sha256'],'project':str(root.resolve()),'paths':['a.py'],
        'checks':{},'tests':[],'objective':task['objective'],'status':'WAITING'}
    record['id']=permissions.identity(record)
    permissions.save_json(folder/'request.json',record)
    permissions.close_interrupted(tools.root,'another-effect')
    assert permissions.pending(tools.root)
    journal=NativeJournal(tools.root/'journal.jsonl')
    journal.append({'kind':'session_started','session_id':'parent'})
    journal.append({'kind':'effect_requested','effect_id':request.effect_id,'tool_call':asdict(request.tool_call)})
    pending,result=tools.recover_effect(journal)
    assert pending==request and result.status=='refused'
    assert not permissions.pending(tools.root) and not (folder/'answer.json').exists()
    assert (root/'a.py').read_text()=='value = 1\n'


def test_one_approval_covers_the_tasks_corrections_but_not_a_new_plan(host, tmp_path):
    tools, control, root, seen = host
    intake(host); task = planned(host)
    tools.state.update(session_id='parent')
    tools.state['config']['writable'] = {}
    directory = tmp_path/'operator-prefs'
    with permission_worker(control) as pool:
        running = pool.submit(permissions.authorize, tools.root, tools.state, task, 'implementation', control, directory)
        request = wait_request(tools.root)
        permissions.answer(tools.root, request['id'], 'once', project=root, directory=directory,
                           control_root=relay_mesh.RELAY_ROOT)
        assert running.result(timeout=3)['writable'] == {'repo': ['a.py']}
        # The correction run of the same task and plan does not ask again.
        again = permissions.authorize(tools.root, tools.state, task, 'correction', control, directory)
        assert again['writable'] == {'repo': ['a.py']} and not permissions.pending(tools.root)
        replanned = deepcopy(task); replanned['plan']['sha256'] = 'f'*64
        asking = pool.submit(permissions.authorize, tools.root, tools.state, replanned, 'replanned', control, directory)
        assert wait_request(tools.root)['plan'] == 'f'*64  # a new plan asks again
        assert not asking.done()
