"""A unit runs at the effort its model was judged at on its type's board, and a plan's task may state its own effort.
Every model, board row and score here is invented."""
import copy
import hashlib
import json
from pathlib import Path

import sys

import pytest

from relay import execution_scope, model_signals as signals, native_artifacts, native_catalog, turn_engine, work_units
from relay.execution_team import reasoning
from relay.model_choices import EFFORTS, supported_level
from relay.native_artifacts import GraphTask
from test_conversation_tasks import host, intake  # noqa: F401  (host is a fixture)
from test_expiring_allowance import API_KEY, CHEAP, MUSE, OPENROUTER, cli
from test_task_types import KIND, ONE
from test_unit_kind import PLAN, TASK

TYPE = {**KIND, 'id': 'coding', 'sources': [ONE]}
FABLE = 'api'  # Fable by the Anthropic API; 'cheap' is the local model, 'openrouter-deepseek' a reseller's


def full(deployment):
    """The fixtures of test_expiring_allowance with the profile fields `with_effort` validates."""
    value = copy.deepcopy(deployment)
    value['native_profile'].update(context_window=100000, output_tokens=4096, source='fixture')
    if value['native_profile']['protocol'] == 'vendor-cli':
        value.update(native_cli={'executable': str(Path.cwd().anchor + 'claude'), 'version': '1'},
                     native_counter_protocol='utf8-envelope-estimate-v1')
    elif 'native_credential_env' in value:
        value['native_reasoning_levels'] = list(EFFORTS)  # what the picker lists for a route that declares its levels
    return value


@pytest.fixture
def ran(monkeypatch):
    """What `routes` yields as {route: effort it runs at}, for routes whose own efforts (`own`) and the type's board
    rows (`board`: model -> (class, effort or None)) the test gives. The real availability gate and `with_effort`."""
    monkeypatch.setattr(signals, 'TYPES', [TYPE])
    found = {d['id']: full(d) for d in (CHEAP, OPENROUTER, API_KEY, MUSE)}
    monkeypatch.setattr(native_catalog, 'select', lambda name: copy.deepcopy(found[name]))
    monkeypatch.setattr(turn_engine, 'policy', lambda: {'implementer': [['cheap'], ['openrouter-deepseek'], [FABLE]]})
    monkeypatch.setattr(turn_engine, 'local_gpu_busy', lambda deployment: None)
    monkeypatch.setattr(turn_engine, 'avoid_local_gpu', lambda: False)
    for key in ('OPENROUTER_API_KEY', 'ANTHROPIC_API_KEY'):
        monkeypatch.setenv(key, 'fixture')
    model = lambda d: d['native_profile']['model']

    def run(own=None, board=None, **arguments):
        for name, level in (own or {}).items():
            found[name] = turn_engine.with_effort(found[name], level)
        monkeypatch.setattr(signals, 'klass', lambda d: {'class': 'middling'})
        monkeypatch.setattr(signals, 'boards', lambda: {'k': 1} if board else {})

        def placed(deployment, kind, kept):
            if kept and model(deployment) in (board or {}):
                cls, effort = board[model(deployment)]
                return {'class': cls, 'general': False, 'source': 'Board one.csv', 'score': .9, 'effort': effort}
            return {'class': 'middling', 'general': True}
        monkeypatch.setattr(signals, 'type_class', placed)
        with execution_scope.activated(execution_scope.Scope()):
            return {name: reasoning(d) for name, d in work_units.routes(full(MUSE), **arguments)}
    run.found = found
    return run


def test_a_route_judged_at_max_by_a_board_is_raised_to_high_and_to_xhigh_when_critical(ran):
    """Boards read models at max: too dear to run every unit of a type at. An ordinary unit is raised to high, a
    critical one to xhigh; a route already at or above that keeps its own."""
    board = {'fable-5-2': ('frontier', 'max'), 'deepseek/v4': ('frontier', 'max')}
    today = ran(work_type='coding')  # no board: every route as it is
    assert today == {'cheap': None, 'openrouter-deepseek': None, FABLE: None}
    assert ran(board=board, work_type='coding') == {'cheap': None, 'openrouter-deepseek': 'high', FABLE: 'high'}
    assert ran({FABLE: 'low'}, board, work_type='coding')[FABLE] == 'high'
    assert ran({FABLE: 'low'}, board, work_type='coding', critical=True)[FABLE] == 'xhigh'
    assert ran({FABLE: 'max'}, board, work_type='coding')[FABLE] == 'max'  # never lowered
    # the row says high, the route is at xhigh: never lowered
    assert ran({FABLE: 'xhigh'}, {'fable-5-2': ('frontier', 'high')}, work_type='coding')[FABLE] == 'xhigh'


def test_a_row_with_no_effort_a_general_judgement_and_a_unit_with_no_type_leave_the_route_as_it_is(ran):
    assert ran({FABLE: 'low'}, {'fable-5-2': ('frontier', None)}, work_type='coding')[FABLE] == 'low'
    assert ran({FABLE: 'low'}, {'other': ('frontier', 'max')}, work_type='coding')[FABLE] == 'low'  # judged generally
    assert ran({FABLE: 'low'}, {'fable-5-2': ('frontier', 'max')})[FABLE] == 'low'  # no type
    assert ran({FABLE: 'low'}, {'fable-5-2': ('frontier', 'max')}, needs='frontier', critical=True)[FABLE] == 'low'


def test_a_level_the_route_does_not_list_is_clamped_to_the_highest_it_lists_below_it(ran):
    top_high = ['low', 'medium', 'high']
    ran.found[FABLE]['native_reasoning_levels'] = top_high  # a model that tops at high, asked for max by the board
    assert ran({FABLE: 'low'}, {'fable-5-2': ('frontier', 'max')}, work_type='coding')[FABLE] == 'high'
    assert ran(unit_effort='xhigh')[FABLE] == 'high'  # the plan's too
    # a local profile that declares low and medium: the board's max is medium, higher than its own low
    ran.found['cheap']['native_reasoning_levels'] = ['low', 'medium']
    assert ran({'cheap': 'low'}, {'q': ('frontier', 'max')}, work_type='coding')['cheap'] == 'medium'
    # clamped to what it already runs at it is not raised; a route the board does not place stays; every route is kept
    assert ran({FABLE: 'high'}, {'fable-5-2': ('frontier', 'max')}, work_type='coding') == {
        'cheap': 'low', 'openrouter-deepseek': None, FABLE: 'high'}


def test_a_route_that_lists_no_level_or_none_at_or_below_the_wanted_one_is_left_at_its_own(ran):
    ran.found[FABLE]['native_reasoning_levels'] = []  # lists nothing: no level is known to be accepted
    assert ran({FABLE: 'low'}, {'fable-5-2': ('frontier', 'max')}, work_type='coding')[FABLE] == 'low'
    assert ran(unit_effort='high')[FABLE] == 'low'
    ran.found['openrouter-deepseek']['native_reasoning_levels'] = ['medium', 'high']  # lowest level medium
    assert ran({'openrouter-deepseek': 'high'}, unit_effort='low')['openrouter-deepseek'] == 'high'  # nothing at or below low
    assert ran(unit_effort='medium')['openrouter-deepseek'] == 'medium'
    assert list(ran(unit_effort='low')) == ['cheap', 'openrouter-deepseek', FABLE]  # the unit still gets every route


def test_an_agy_route_changes_level_by_changing_its_model_id_and_only_to_a_level_it_has_an_id_for():
    """AGY lists one model id per effort. tare left its routes at their own level; it now swaps the id, as the model
    picker does, so a plan's effort and a board's reach an AGY route too (owner, 2026-11-17)."""
    agy = {'id': 'agy-gemini-4-argon-cli', 'runner': 'native-cli', 'model': 'gemini-4-argon-high',
           'native_cli': {'executable': str(Path(sys.executable)), 'version': '1.2.7'},
           'native_counter_protocol': 'utf8-envelope-estimate-v1',
           'native_profile': {'endpoint': 'cli://agy', 'model': 'gemini-4-argon-high', 'protocol': 'vendor-cli',
                              'context_window': 1048576, 'output_tokens': 16384, 'source': 'test',
                              'parameters_json': '{"effort":"high"}'},
           'native_reasoning_models': {e: 'gemini-4-argon-' + e for e in ('low', 'medium', 'high')}}  # an id per level
    assert agy['native_profile']['model'] == 'gemini-4-argon-high' and sorted(agy['native_reasoning_models']) == ['high', 'low', 'medium']
    assert supported_level(agy, 'low') == 'low' and supported_level(agy, 'max') == 'high'  # the highest it has an id for
    low = turn_engine.with_effort(agy, 'low')
    assert (low['model'], low['native_profile']['model']) == ('gemini-4-argon-low', 'gemini-4-argon-low')
    assert json.loads(low['native_profile']['parameters_json'])['effort'] == 'low'
    assert agy['native_profile']['model'] == 'gemini-4-argon-high'  # the saved deployment is untouched
    assert signals.judged_effort(low, {'effort': 'max'}) == 'high'  # a board's effort, capped, now reaches it
    assert signals.judged_effort(agy, {'effort': 'max'}) is None  # already there: never lower, nothing to raise
    assert turn_engine.with_effort(low, 'high')['native_profile']['model'] == 'gemini-4-argon-high'
    # a level it has no id for leaves the route whole: never a parameter that names another level than its id
    assert turn_engine.with_effort(agy, 'xhigh') == agy
    lone = {k: v for k, v in low.items() if k != 'native_reasoning_models'}
    assert turn_engine.with_effort(lone, 'high') == lone and supported_level(lone, 'high') == 'low'


def test_a_stated_effort_applies_to_every_automatic_route_lower_ones_included_and_replaces_the_board(ran):
    board = {'fable-5-2': ('frontier', 'max')}
    assert ran({FABLE: 'high'}, board, work_type='coding', unit_effort='medium') == {
        'cheap': None, 'openrouter-deepseek': 'medium', FABLE: 'medium'}  # lower than high, not max
    assert ran(unit_effort='low')[FABLE] == 'low' and ran(unit_effort='xhigh')[FABLE] == 'xhigh'  # with no type at all


def test_a_pin_and_a_route_and_effort_the_coordinator_fixed_are_untouched(ran, monkeypatch):
    board = {'fable-5-2': ('frontier', 'max')}
    assert ran({FABLE: 'low'}, board, chosen=FABLE, work_type='coding', unit_effort='high') == {FABLE: 'low'}
    assert ran({FABLE: 'low'}, board, chosen=FABLE, effort='medium', work_type='coding', unit_effort='high') == {FABLE: 'medium'}
    monkeypatch.setattr(execution_scope.Scope, 'pin', lambda self, role: FABLE)
    monkeypatch.setattr(execution_scope, 'pin_selection', lambda *a: {'native_model_choice': {'connection': FABLE}})
    monkeypatch.setattr(signals, 'boards', lambda: pytest.fail('a pin reads no board'))
    assert [n for n, _ in work_units.routes(MUSE, work_type='coding', unit_effort='high')] == [FABLE]


def test_the_boards_are_read_once_for_the_whole_unit(ran, monkeypatch):
    reads = []
    board = {'fable-5-2': ('frontier', 'max')}
    ran(board=board, work_type='coding')
    monkeypatch.setattr(signals, 'boards', lambda: reads.append(1) or {'k': 1})
    with execution_scope.activated(execution_scope.Scope()):
        list(work_units.routes(full(MUSE), work_type='coding'))
    assert reads == [1]


def test_the_field_validates_refuses_an_unknown_level_and_a_plan_saved_before_it_keeps_its_digest():
    assert GraphTask(**TASK).effort is None and GraphTask(**TASK, effort='xhigh').effort == 'xhigh'
    with pytest.raises(ValueError):  # max is the user's to choose (a pin at @max), never the planner's
        GraphTask(**TASK, effort='max')
    with pytest.raises(ValueError):
        GraphTask(**TASK, effort='ludicrous')
    assert GraphTask.model_json_schema()['properties']['effort']['description'] == (
        'How hard the implementer should think on this task: low, medium, high or xhigh. Leave it out: for a '
        'task that names its type of work tare then raises the model to high (xhigh when the task is critical), else '
        'it runs at the route\'s own effort. State it only when the task clearly needs more thought than that, or is '
        'mechanical and needs little.')
    assert 'effort' not in json.dumps(native_artifacts.validate('planner', dict(PLAN, tasks=[TASK])))
    assert native_artifacts.validate('planner', dict(PLAN, tasks=[dict(TASK, effort='high')]))['tasks'][0]['effort'] == 'high'


def test_a_merged_unit_takes_the_highest_effort_its_tasks_state(host):
    from compute_plane.native_journal import save_json
    from relay import conversation_tasks as tasks
    tools = host[0]
    intake(host)
    work = dict(summary='s', cluster='c', priority='P2', target_paths=['a.py'], criteria=['value equals two'], depends_on=[])

    def unit_of(*given):
        artifact = tools.root/'planning'/'p'/'session'/'artifact.json'
        artifact.parent.mkdir(parents=True, exist_ok=True)
        save_json(artifact, {'payload': dict(PLAN, criteria=['value equals two'], tasks=[
            dict(work, key=f'k{n}', title=f't{n}', **t) for n, t in enumerate(given)])})
        task = tasks.current(tasks.load(tools.root))
        tasks.task_plan(tools.root, tools.state['config'], task, {'artifact': str(artifact)})
        return task['plan']['unit']
    assert unit_of({'effort': 'low'}, {'effort': 'xhigh'}, {'effort': 'medium'}, {})['effort'] == 'xhigh'
    assert unit_of({'effort': 'xhigh'}, {'effort': 'high'})['effort'] == 'xhigh'
    assert 'effort' not in unit_of({'needs': 'frontier'}, {})


def test_the_shown_plan_says_the_stated_effort(tmp_path):
    from relay import conversation_permissions as permissions
    task = {'title': 'Fix', 'summary': 'S', 'target_paths': ['a.py'], 'criteria': ['c'], 'needs': 'middling', 'kind': 'math',
            'effort': 'high'}
    (tmp_path/'planning').mkdir()
    artifact = tmp_path/'planning'/'plan.json'
    artifact.write_bytes(json.dumps({'payload': {'summary': 'S', 'steps': [], 'criteria': [], 'tasks': [
        task, {**task, 'needs': None, 'kind': None, 'effort': None}]}}).encode())
    text = permissions.plan_text(tmp_path, {'artifact': str(artifact), 'plan': hashlib.sha256(artifact.read_bytes()).hexdigest()})
    assert '  Model: needs a middling model; math work; effort high' in text and text.count('  Model: ') == 1


def test_the_outcome_row_and_the_delivery_carry_the_effort_it_ran_at_and_why(monkeypatch):
    monkeypatch.setattr(signals, 'TYPES', [TYPE])
    monkeypatch.setattr(signals, 'klass', lambda d: {'class': 'middling'})
    monkeypatch.setattr(signals, 'boards', lambda: {'k': 1})
    monkeypatch.setattr(signals, 'type_class', lambda d, kind, kept: {
        'class': 'frontier', 'general': False, 'source': 'Board one.csv', 'score': .9, 'effort': 'max'})
    at = lambda level: turn_engine.with_effort(full(cli('claude', 'claude-opus-5-5')), level)
    # judged by the board at max and run at the ceiling: high, or xhigh for a critical unit
    assert work_units.class_row({'needs': 'frontier', 'kind': 'coding'}, at('high')) == {
        'needs': 'frontier', 'route_class': 'frontier', 'kind': 'coding', 'class_source': 'Board one.csv',
        'effort': 'high', 'effort_from': 'Board one.csv'}
    assert work_units.class_row({'kind': 'coding'}, at('high')) == {'kinds': ['coding'], 'effort': 'high', 'effort_from': 'Board one.csv'}
    assert work_units.class_row({'kind': 'coding', 'critical': True}, at('xhigh')) == {
        'kinds': ['coding'], 'effort': 'xhigh', 'effort_from': 'Board one.csv', 'critical': True}
    assert 'effort' not in work_units.class_row({'kind': 'coding'}, at('max'))  # its own, above the ceiling: unsaid
    assert 'effort' not in work_units.class_row({'kind': 'coding'}, at('low'))  # the route did not take it: its own, unsaid
    # the level applied is the one recorded: a route that tops at high, asked for max, ran at high
    tops_high = full(API_KEY)
    tops_high['native_reasoning_levels'] = ['low', 'medium', 'high']
    assert work_units.class_row({'kind': 'coding'}, turn_engine.with_effort(tops_high, 'high')) == {
        'kinds': ['coding'], 'effort': 'high', 'effort_from': 'Board one.csv'}
    assert work_units.class_row({'effort': 'xhigh'}, turn_engine.with_effort(tops_high, 'high')) == {
        'effort': 'high', 'effort_from': 'plan'}
    # the plan's effort wins, and is said as the plan's
    assert work_units.class_row({'kind': 'coding', 'effort': 'low'}, at('low')) == {'kinds': ['coding'], 'effort': 'low', 'effort_from': 'plan'}
    assert work_units.class_row({'effort': 'high'}, at('high')) == {'effort': 'high', 'effort_from': 'plan'}
    assert work_units.class_row({'effort': 'high', 'critical': True}, at('medium')) == {'critical': True}
    assert work_units.class_row({'critical': True}, {}) == {'critical': True} and work_units.class_row({}, {}) == {}
    from relay.conversation_tasks import delivery_view
    view = delivery_view({'verdict': 'INCONCLUSIVE', 'reason': 'r', 'effort': 'max', 'effort_from': 'plan'})
    assert (view['effort'], view['effort_from']) == ('max', 'plan')
    assert 'effort' not in delivery_view({'verdict': 'INCONCLUSIVE', 'reason': 'r'})


def test_judged_carries_the_row_effort_and_judged_effort_is_never_lower_than_the_routes_own():
    higher = signals.judged_effort
    claude = lambda level: turn_engine.with_effort(full(cli('claude', 'claude-opus-5-5')), level) if level else full(cli('claude', 'claude-opus-5-5'))
    assert higher(claude('low'), {'effort': 'max'}) == higher(claude(None), {'effort': 'max'}) == 'high'  # the ceiling
    assert higher(claude('low'), {'effort': 'max'}, critical=True) == 'xhigh' == higher(claude('high'), {'effort': 'max'}, True)
    assert higher(claude('low'), {'effort': 'medium'}, critical=True) == 'medium'  # a board below the ceiling: its own
    assert higher(claude('max'), {'effort': 'max'}) is None and higher(claude('xhigh'), {'effort': 'high'}) is None
    assert higher(claude('high'), {'effort': 'max'}) is None  # already at the ordinary ceiling
    assert higher(claude('low'), {}) is None and higher(claude('low'), {'effort': None}) is None


def test_first_pass_middling_and_cheap_units_cap_effort_at_medium_and_escalate_on_retry(ran):
    board = {'fable-5-2': ('frontier', 'max')}
    # Non-critical first-pass middling/cheap units cap board effort AND high-default routes at medium.
    assert ran({FABLE: 'high'}, board, work_type='coding', needs='middling')[FABLE] == 'medium'
    assert ran({FABLE: 'low'}, board, work_type='coding', needs='cheap')[FABLE] == 'medium'
    # Skill-elevated frontier needs still honors original_needs='middling' on first pass, then escalates on retry.
    assert ran({FABLE: 'high'}, board, work_type='coding', needs='frontier', original_needs='middling')[FABLE] == 'medium'
    assert ran({FABLE: 'high'}, board, work_type='coding', needs='frontier', original_needs='middling', retry=True)[FABLE] == 'high'
    # Native frontier, critical, and explicit plan effort keep full effort.
    assert ran({FABLE: 'high'}, board, work_type='coding', needs='frontier')[FABLE] == 'high'
    assert ran({FABLE: 'low'}, board, work_type='coding', needs='middling', critical=True)[FABLE] == 'xhigh'
    assert ran({FABLE: 'high'}, board, work_type='coding', needs='middling', unit_effort='high')[FABLE] == 'high'
