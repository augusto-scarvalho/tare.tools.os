"""A plan's task may be `critical`, and a critical unit's implementer is the best route for its type of work, whatever it
costs. Every model, board and score here is invented."""
import json

import pytest

from relay import model_signals as signals
from relay import native_artifacts
from relay.native_artifacts import GraphTask
from test_expiring_allowance import LADDER, unit  # noqa: F401  (unit is a fixture)
from test_conversation_tasks import host, intake  # noqa: F401  (host is a fixture)
from test_task_types import KIND, ONE
from test_unit_kind import PLAN, TASK

TYPE = {**KIND, 'id': 'coding', 'sources': [ONE]}
BOARD = ONE['name']
# route order today: cheap ('q'), openrouter-deepseek ('deepseek/v4'), claude-fable-5-2 and api (both 'fable-5-2')
CHEAP, DEEP, FABLE = 'q', 'deepseek/v4', 'fable-5-2'
FABLE_FIRST = ['claude-fable-5-2', 'api', 'openrouter-deepseek', 'cheap']


@pytest.fixture
def ranked(monkeypatch):
    """Give each model a general class and, where `board` says, a class and a score on the type's board."""
    monkeypatch.setattr(signals, 'TYPES', [TYPE])

    def make(general, board=None):
        model = lambda d: d['native_profile']['model']
        monkeypatch.setattr(signals, 'klass', lambda d: {'class': general[model(d)]} if model(d) in general else None)
        monkeypatch.setattr(signals, 'boards', lambda: {'k': 1} if board else {})

        def placed(deployment, kind, kept):
            if kept and model(deployment) in board:
                cls, score = board[model(deployment)]
                return {'class': cls, 'general': False, 'source': BOARD, 'score': score}
            return {'class': general.get(model(deployment)), 'general': True}
        monkeypatch.setattr(signals, 'type_class', placed)
    return make


def test_the_field_validates_and_a_plan_saved_before_it_keeps_its_digest():
    assert GraphTask(**TASK).critical is None and GraphTask(**TASK, critical=True).critical is True
    with pytest.raises(ValueError):
        GraphTask(**TASK, critical='sometimes')
    assert GraphTask.model_json_schema()['properties']['critical']['description'] == (
        'true when a wrong result here would be costly, or hard to notice or undo: security-sensitive code, money, a '
        'data migration or deletion, a public interface others depend on, a result nobody will check. tare then gives '
        'the task to the best model for its type, whatever it costs. Leave it out for ordinary work: tare then uses '
        'the cheapest model that is good enough. '
        'For a task you mark critical, add to its criteria one worked example for each sentence of the request it '
        'implements, written as "Example: given <the concrete stored data or the command typed>, <command> gives '
        '<the exact expected result>". Choose the case a plausible wrong reading of that sentence would get wrong, '
        'and take the expected result from the sentence of the request, not from your design.')
    old = dict(PLAN, tasks=[TASK])
    assert 'critical' not in json.dumps(native_artifacts.validate('planner', old))
    assert native_artifacts.validate('planner', dict(PLAN, tasks=[dict(TASK, critical=True)]))['tasks'][0]['critical'] is True


def test_a_critical_unit_takes_the_best_for_its_type_before_a_cheaper_middling_one_that_reaches_the_need(unit, ranked):
    ranked({CHEAP: 'cheap', DEEP: 'frontier', FABLE: 'frontier'}, {DEEP: ('middling', .6), FABLE: ('frontier', .9)})
    assert unit(needs='middling', work_type='coding') == ['openrouter-deepseek', 'claude-fable-5-2', 'api', 'cheap']  # today
    assert unit(needs='middling', work_type='coding', critical=True) == FABLE_FIRST
    # the need does not reorder it (it is still recorded elsewhere)
    assert unit(needs='cheap', work_type='coding', critical=True) == unit(needs='frontier', work_type='coding', critical=True) \
        == unit(work_type='coding', critical=True) == FABLE_FIRST


def test_in_one_class_placed_by_one_board_the_higher_score_goes_first_even_when_it_costs_more(unit, ranked):
    ranked({CHEAP: 'cheap', DEEP: 'frontier', FABLE: 'frontier'}, {DEEP: ('frontier', .7), FABLE: ('frontier', .9)})
    assert unit(needs='frontier', work_type='coding') == ['openrouter-deepseek', 'claude-fable-5-2', 'api', 'cheap']
    assert unit(needs='frontier', work_type='coding', critical=True) == FABLE_FIRST
    assert unit(needs='frontier', critical=True) == LADDER[1:2] + ['claude-fable-5-2', 'api', 'cheap']  # no type: no score


def test_a_route_no_board_places_comes_after_the_placed_ones_of_its_class(unit, ranked):
    # DeepSeek is cheaper and frontier in general; Fable is placed, frontier on the board
    ranked({CHEAP: 'cheap', DEEP: 'frontier', FABLE: 'frontier'}, {FABLE: ('frontier', .1)})
    assert unit(work_type='coding', critical=True) == FABLE_FIRST
    # but a class outranks being placed: a general frontier is before a placed middling
    ranked({CHEAP: 'cheap', DEEP: 'frontier', FABLE: 'middling'}, {FABLE: ('middling', .99)})
    assert unit(work_type='coding', critical=True) == ['openrouter-deepseek', 'claude-fable-5-2', 'api', 'cheap']


def test_with_no_type_the_general_class_orders_a_critical_unit(unit, ranked):
    ranked({CHEAP: 'cheap', DEEP: 'frontier', FABLE: 'middling'}, {FABLE: ('frontier', .9)})
    assert unit(critical=True) == ['openrouter-deepseek', 'claude-fable-5-2', 'api', 'cheap']
    assert unit(critical=True, work_type='coding') == FABLE_FIRST  # the board changes it only with a type


def test_equal_class_and_score_keep_todays_order(unit, ranked):
    ranked({CHEAP: 'frontier', DEEP: 'frontier', FABLE: 'frontier'}, {DEEP: ('frontier', .5), FABLE: ('frontier', .5)})
    assert unit(critical=True) == LADDER
    assert unit(work_type='coding', critical=True) == ['openrouter-deepseek', 'claude-fable-5-2', 'api', 'cheap']


def test_a_unit_that_is_not_critical_is_ordered_as_today(unit, ranked):
    ranked({CHEAP: 'cheap', DEEP: 'frontier', FABLE: 'frontier'}, {DEEP: ('middling', .6), FABLE: ('frontier', .9)})
    today = unit(needs='middling', work_type='coding')
    assert today == ['openrouter-deepseek', 'claude-fable-5-2', 'api', 'cheap']
    assert unit(needs='middling', work_type='coding', critical=False) == today
    assert unit(critical=False) == unit() == LADDER  # no need and not critical: no ranking


def test_a_chosen_route_and_a_pin_are_never_reordered(unit, ranked, monkeypatch):
    from relay import execution_scope
    ranked({CHEAP: 'cheap', DEEP: 'cheap', FABLE: 'frontier'}, {FABLE: ('frontier', .9)})
    assert unit(chosen='cheap', work_type='coding', critical=True) == ['cheap']
    monkeypatch.setattr(execution_scope.Scope, 'pin', lambda self, role: 'cheap')
    monkeypatch.setattr(execution_scope, 'pin_selection', lambda *a: {'native_model_choice': {'connection': 'cheap'}})
    monkeypatch.setattr(signals, 'boards', lambda: pytest.fail('a pin is not ranked'))
    assert unit(work_type='coding', critical=True) == ['cheap']


def test_the_outcome_row_carries_critical(ranked):
    from relay.work_units import class_row
    ranked({FABLE: 'middling'})
    assert class_row({'needs': 'frontier', 'critical': True}, {'native_profile': {'model': FABLE}}) == {
        'needs': 'frontier', 'route_class': 'middling', 'critical': True}
    assert class_row({'needs': 'frontier'}, {'native_profile': {'model': FABLE}}) == {
        'needs': 'frontier', 'route_class': 'middling'}  # as today
    assert class_row({'critical': True}, {}) == {'critical': True} and class_row({}, {}) == {}
    from relay.conversation_tasks import delivery_view
    assert delivery_view({'verdict': 'INCONCLUSIVE', 'reason': 'r', 'critical': True})['critical'] is True
    assert 'critical' not in delivery_view({'verdict': 'INCONCLUSIVE', 'reason': 'r'})


def test_a_merged_unit_is_critical_when_any_of_its_tasks_is(host):
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
    assert unit_of({'needs': 'frontier'}, {'critical': True, 'kind': 'math'})['critical'] is True
    assert unit_of({'critical': True, 'needs': 'cheap', 'kind': 'math'}, {'needs': 'frontier'})['kind'] == 'math'  # no hardest type
    assert 'critical' not in unit_of({'needs': 'frontier'}, {'critical': False}, {})
    assert 'critical' not in unit_of({'needs': 'frontier', 'kind': 'math'})
