"""A check the project already failed before the change is said apart from one the change broke: in the unit's
result, in the coordinator's next step, and without running a red suite a second time to learn which check failed.
In a conversation the user is asked before the implementer starts: continue, continue without it, or stop."""
import asyncio
import hashlib
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from relay import conversation_permissions as permissions, delegation, native_agent, relay_mesh, turn_engine, work_units
from relay.conversation_tasks import delivery_view
from test_implement_unit import repository
from test_native_agent import Client, deployment, setup  # noqa: F401 (fixture)


def results(codes):
    """A check port whose commands exit with the code their name maps to; it counts the runs."""
    runs = []

    def check(argv, **kwargs):
        runs.append(argv[0])
        return subprocess.CompletedProcess(argv, codes[argv[0]], '', '')
    return check, runs


def test_each_check_keeps_its_own_state(tmp_path):
    check, _ = results({'a': 0, 'b': 1, 'c': 0})
    token = work_units.CHECK_PORT.set(check)
    try:
        ran = work_units.run_checks(tmp_path, {name: [name] for name in 'abc'})
    finally:
        work_units.CHECK_PORT.reset(token)
    state, output = ran  # still the pair every caller unpacks
    assert state == 'red' and '$ b (exit 1)' in output
    assert ran.states == {'a': 'green', 'b': 'red', 'c': 'green'}


def test_only_a_check_red_on_both_sides_already_failed():
    before = work_units.Checked('red', '', {'old': 'red', 'broken': 'green', 'fixed': 'red'})
    after = work_units.Checked('red', '', {'old': 'red', 'broken': 'red', 'fixed': 'green'})
    assert work_units.already_red(before, after) == ['old']
    assert work_units.already_red(('red', ''), after) == []  # a result with no per-check states claims nothing


def test_the_coordinator_is_not_sent_to_repair_a_failure_the_project_already_had():
    plain = delivery_view({'verdict': 'NOT_VERIFIED', 'reason': 'the checks fail after the change'})
    assert 'repair only the failed part' in plain['next'] and 'before the change' not in plain['next']
    told = delivery_view({'verdict': 'NOT_VERIFIED', 'reason': 'r', 'already_red': ['godot-import']})
    assert told['already_red'] == ['godot-import']
    assert 'godot-import' in told['next'] and 'do not send it back' in told['next']
    assert 'a test it adds, a bug it fixes' in told['next']  # a red baseline the plan is about is still repaired


def test_a_unit_says_which_failing_check_already_failed_and_runs_the_red_suite_once(setup, tmp_path, monkeypatch):
    config, custody, root = setup
    repository(root)
    config['verifiers']['broken'] = {'repository': 'repo', 'argv': [sys.executable, '-B', '-c', 'raise SystemExit(3)']}
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'storage'))
    monkeypatch.delenv('TARE_DELEGATION_POLICY')
    delegation.save_experiments(implement_child=True)
    monkeypatch.setattr(turn_engine, 'policy', lambda: {'implementer': [['child-route']]})
    monkeypatch.setattr(turn_engine, 'available', lambda name: (deployment(), None))
    child = Client(steps=[('edit', {'repository': 'repo', 'path': 'a.py', 'search': 'value = 1', 'replace': 'value = 2',
                                    'sha256': hashlib.sha256(b'value = 1\n').hexdigest()}),
                          ('tare_deliver', {'summary': 'Set the value to 2.'})])
    monkeypatch.setattr(native_agent, 'inference_client', lambda *a, **kw: child)
    from compute_plane import native_models
    monkeypatch.setattr(native_models, 'NativeHttpClient', lambda *a, **kw: child)
    ran = []
    real = work_units.run_checks
    monkeypatch.setattr(work_units, 'run_checks', lambda tree, commands: ran.append(sorted(commands)) or real(tree, commands))
    unit = {'title': 'Set the value to 2', 'target_paths': ['a.py'], 'criteria': ['a.py holds 2'],
            'verifiers': ['unit', 'broken']}
    parent = Client(steps=[('implement', unit), ('tare_deliver', {'summary': 'Done.'})])
    native_agent.run_session(config, deployment(), custody, 'Change the value.', client=parent, local_counter=parent,
                             limits={'max_model_calls': 16})
    assert (root/'a.py').read_bytes() == b'value = 1\n'  # not verified: nothing applied
    receipt = json.loads(next((custody/'units').glob('*/receipt.json')).read_text(encoding='utf-8'))
    result = receipt['result']
    assert result['verdict'] == 'NOT_VERIFIED' and result['already_red'] == ['broken']
    assert result['reason'] == 'the checks that fail after the change already failed before it: broken'
    assert ran == [['broken', 'unit']]*2  # before and after: the red baseline was not run again check by check


BROKEN = [sys.executable, '-B', '-c', 'raise SystemExit(3)']


def asked_unit(setup, tmp_path, monkeypatch, answer, *, test_paths=(), verdicts=('ACCEPT',)):
    """A conversation unit whose check `broken` fails before any change; `answer` is the user's to the question."""
    config, custody, root = setup
    repository(root)
    config.update(conversation_flow=True)
    config['verifiers']['broken'] = {'repository': 'repo', 'argv': BROKEN}
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'storage'))
    monkeypatch.setattr(turn_engine, 'project_python', lambda path: sys.executable)
    monkeypatch.setattr(work_units, 'routes', lambda *a, **kw: iter([('worker', deployment())]))
    workers, asked, audits, ran = [], [], [], []

    def worker(child, deployment, path, instruction, **kwargs):
        workers.append((child, instruction))
        (Path(child['repositories']['repo'])/'a.py').write_text('value = 2\n')
        return {'status': 'PROPOSED', 'model_calls': 1}
    monkeypatch.setattr(native_agent, 'run_session', worker)

    def check(argv, **kwargs):  # broken always fails, a planned test until it is written; unit always passes
        planned = next((t for t in test_paths if t in argv), None)
        code = 1 if argv == BROKEN else int(not (Path(kwargs['cwd'])/planned).exists()) if planned else 0
        return subprocess.CompletedProcess(argv, code, '', '')
    real = work_units.run_checks
    monkeypatch.setattr(work_units, 'run_checks', lambda tree, commands: ran.append(sorted(commands)) or real(tree, commands))

    def question(checks, output, aside):
        asked.append((sorted(checks), output, aside))
        return answer

    def review(tree, deployment, unit, changed, checks):
        audits.append(checks)
        return {'verdict': verdicts[len(audits)-1], 'sources': [], 'reference_sources': [], 'summary': 'Reviewed.',
                'findings': [{'claim': 'Tighten it.'}]}

    def run(candidate=None):
        return work_units.run_unit(custody, {'config': config, 'conversation_flow': True, 'deployment': deployment(),
            'limits': {'max_model_calls': 30}, 'deadline_utc': None},
            {'title': 'Set two', 'target_paths': ['a.py', *test_paths], 'criteria': ['value equals two'],
             'verifiers': ['unit', 'broken'], 'test_paths': list(test_paths)}, lambda: False, review=review,
            red_baseline=question, candidate=candidate)
    token = work_units.CHECK_PORT.set(check)
    try:
        result = run()
        for _ in verdicts[1:]:  # a correction round on the candidate the review sent back
            result = run({**result['candidate'], 'review_only': False})
    finally:
        work_units.CHECK_PORT.reset(token)
    receipt = json.loads(Path(result['receipt']).read_text(encoding='utf-8'))
    return result, receipt, workers, asked, audits, ran


@pytest.mark.parametrize('answer', ['deny', None])
def test_stop_starts_no_implementer_and_names_the_checks_that_already_failed(setup, tmp_path, monkeypatch, answer):
    result, receipt, workers, asked, _, _ = asked_unit(setup, tmp_path, monkeypatch, answer)
    assert asked == [(['broken'], receipt['rungs'][0]['before'], True)] and workers == []
    assert result['verdict'] == 'INCONCLUSIVE' and result['already_red'] == ['broken']
    assert 'already failed before any change: broken' in result['reason'] and '$ broken (exit 1)' in result['checks_before']
    assert receipt['rungs'][0]['model_calls'] == 0 and len(receipt['rungs']) == 1  # no other route was tried
    assert receipt['rungs'][0]['red_before'] == {'checks': ['broken'], 'answer': answer or 'none'}
    view = delivery_view(result)
    assert view['already_red'] == ['broken'] and 'No implementer started' in view['next']
    assert 'Do not implement again unchanged' in view['next']


def test_continue_without_it_sets_aside_only_the_red_check_and_applies_on_the_others(setup, tmp_path, monkeypatch):
    result, receipt, workers, asked, audits, ran = asked_unit(setup, tmp_path, monkeypatch, 'aside')
    (child, instruction), = workers
    assert set(child['verifiers']) == {'unit'} and 'broken' not in instruction  # the implementer is not given it
    assert ran[:2] == [['broken', 'unit'], ['unit']]  # before, then after the change without it
    assert result['verdict'] == 'VERIFIED' and result['checks_set_aside'] == ['broken']
    assert result['reason'] == ('green before and after (the change keeps them green); not judged by broken, which '
                                "already failed before the change (the user's decision)")
    assert receipt['rungs'][0]['checks_set_aside'] == ['broken'] and audits[0]['checks_set_aside']['checks'] == ['broken']
    # The answer holds to the end: the session-suite gate runs the other checks only, and they pass.
    assert ran[2:] == [['unit']] and result['application'] == 'APPLIED'
    assert result['suite'] == "the session's checks pass with the change"
    assert delivery_view(result)['checks_set_aside'] == ['broken']


def test_the_choice_travels_with_the_change_into_its_correction(setup, tmp_path, monkeypatch):
    result, receipt, workers, asked, audits, ran = asked_unit(setup, tmp_path, monkeypatch, 'aside',
                                                              verdicts=('REVISE', 'ACCEPT'))
    assert len(asked) == 1 and len(workers) == 2  # the correction is not asked again: its tree already holds a change
    assert set(workers[1][0]['verifiers']) == {'unit'} and audits[1]['checks_set_aside']['checks'] == ['broken']
    assert receipt['input_candidate']['checks_set_aside'] == ['broken']  # the choice is in the candidate's record
    assert result['verdict'] == 'VERIFIED' and result['checks_set_aside'] == ['broken']
    assert result['application'] == 'APPLIED'


def test_continue_keeps_todays_behaviour(setup, tmp_path, monkeypatch):
    result, _, workers, asked, _, _ = asked_unit(setup, tmp_path, monkeypatch, 'once')
    assert len(asked) == 1 and set(workers[0][0]['verifiers']) == {'unit', 'broken'}
    assert result['verdict'] == 'NOT_VERIFIED' and result['already_red'] == ['broken']


def test_a_test_the_plan_writes_is_red_by_construction_and_never_asked_about(setup, tmp_path, monkeypatch):
    result, _, workers, asked, _, ran = asked_unit(setup, tmp_path, monkeypatch, 'aside', test_paths=('tests/test_new.py',))
    assert '$ pytest-tests:tests/test_new.py (exit 1)' in result['checks_before']  # red too, by construction
    # The plan's test makes the unit's checks more than the session's: its red checks are still not run twice.
    assert ran[:2] == [['broken', 'pytest-tests:tests/test_new.py', 'unit'], ['pytest-tests:tests/test_new.py', 'unit']]
    assert [a[0] for a in asked] == [['broken']]  # only the check the plan does not write
    assert 'pytest-tests:tests/test_new.py' in workers[0][0]['verifiers']
    assert result['checks_set_aside'] == ['broken']


def test_the_question_is_asked_once_per_turn_and_failing_checks_and_never_headless(tmp_path):
    from tare_tools_agent_runtime import BindingRef, CancellationToken, ExecutionControl
    root, project, directory = tmp_path/'session', tmp_path/'project', tmp_path/'console'
    root.mkdir(); project.mkdir()
    control = ExecutionControl(BindingRef('work', 'binding', 'operator', 1, 'opaque'), CancellationToken(),
                               time.monotonic()+60)
    assert permissions.red_baseline(root, {'session_id': 's'}, None, 'e', project, control, None, 'turn-1') is None

    def ask(turn, checks, choice=None, aside=True):
        seen, errors = [], []

        def user():  # the TUI: answers the one request it sees
            while permissions.pending(root) is None:
                time.sleep(.01)
            request = permissions.pending(root)
            seen.append(request)
            for wrong in ('project', 'change', 'outside', *(() if aside else ('aside',))):
                try:
                    permissions.answer(root, request['id'], wrong, project=project, directory=directory,
                                       control_root=relay_mesh.RELAY_ROOT)
                except ValueError as error:
                    errors.append(str(error))
            permissions.answer(root, request['id'], choice, project=project, directory=directory,
                               control_root=relay_mesh.RELAY_ROOT)
        thread = threading.Thread(target=user, daemon=True)
        if choice:
            thread.start()
        got = permissions.red_baseline(root, {'session_id': 's'}, None, 'e', project, control, directory, turn)(
            {n: ['run', n] for n in checks}, '$ broken (exit 3)', aside)
        if choice:
            thread.join(5)
        return got, seen, errors

    got, seen, errors = ask('turn-1', ['broken'], 'aside')
    assert got == 'aside' and seen[0]['kind'] == 'baseline' and seen[0]['checks'] == {'broken': ['run', 'broken']}
    assert seen[0]['baseline'] == {'aside': True, 'output': '$ broken (exit 3)'} and 'broken' in seen[0]['objective']
    assert len(errors) == 3 and all('Nothing was' in e for e in errors)  # no saved "always" on this question
    assert ask('turn-1', ['broken'])[:2] == ('aside', [])  # the same turn and checks: no second question
    got, _, errors = ask('turn-1', ['broken', 'lint'], 'deny', aside=False)  # other failing checks ask again
    assert got == 'deny' and len(errors) == 4  # 'aside' too, where no check would remain
    assert ask('turn-2', ['broken'], 'once')[0] == 'once'  # a new turn asks again
    assert not directory.exists()  # nothing saved for the project


def test_continue_without_it_answers_only_the_question_that_offers_it(tmp_path):
    root = tmp_path/'session'; (root/'execution-permission').mkdir(parents=True)
    request = {'schema': permissions.SCHEMA, 'kind': 'sandbox', 'session': 's', 'effect': 'e', 'task': '', 'plan': '',
               'project': str(tmp_path), 'paths': [], 'checks': {'tests': BROKEN}, 'tests': [], 'objective': 'q',
               'status': 'WAITING', 'sandbox': {'code': 'ISOLATION_NETWORK_DENIED', 'check': 'tests'}}
    request['id'] = permissions.identity(request)
    (root/'execution-permission'/'request.json').write_text(json.dumps(request))
    with pytest.raises(ValueError, match='Nothing was authorized'):
        permissions.answer(root, request['id'], 'aside', project=tmp_path, directory=tmp_path/'console',
                           control_root=relay_mesh.RELAY_ROOT)


@pytest.mark.parametrize('aside', [True, False])
def test_the_dialog_starts_on_stop_and_escape_stops(tmp_path, aside):
    from textual.app import App
    from relay.execution_permission_panel import ExecutionPermissionPanel
    request = {'kind': 'baseline', 'objective': 'Before any change, the project already fails this check: broken.',
               'project': str(tmp_path), 'checks': {'broken': BROKEN}, 'baseline': {'aside': aside, 'output': 'x'}}

    async def scenario():
        app, answers = App(), []
        async with app.run_test(size=(120, 40)) as pilot:
            for key in ('enter', 'escape'):
                app.push_screen(ExecutionPermissionPanel(request), answers.append)
                await pilot.pause()
                assert bool(app.screen.query('#permission-aside')) is aside
                await pilot.press(key); await pilot.pause()
        assert answers == ['deny', 'deny']
    asyncio.run(scenario())
