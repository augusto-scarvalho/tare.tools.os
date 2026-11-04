"""Actual operator decisions, bound to the plan; no provider calls while waiting."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
import json
from threading import Event
import time

import pytest

from relay import conversation_permissions as permissions, conversation_tasks as tasks, relay_mesh, work_units
from relay.project_permissions import read_defaults
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


def wait_request(root):
    deadline = time.monotonic()+3
    while time.monotonic() < deadline:
        try:
            request = permissions.pending(root)
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
    argv = checks['pytest-tests']
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


@pytest.mark.parametrize('mode',['edit','commands'])
def test_remembered_project_or_yolo_policy_needs_no_question_even_in_old_read_only_chat(host,tmp_path,mode):
    from relay.turn_engine import set_project_permission_mode
    tools,control,root,_=host
    intake(host); task=planned(host)
    tools.state['config']['writable']={}
    set_project_permission_mode(root,mode)
    worker=permissions.authorize(tools.root,tools.state,task,'effect',control,tmp_path/'prefs')
    assert worker['writable']=={'repo':['a.py']}
    assert tools.state['config']['writable']=={} and not permissions.pending(tools.root)


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
