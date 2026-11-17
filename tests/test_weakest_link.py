"""A plan of several types of work still gets one implementer: the one that is good enough at every task, not only at
the hardest. Every model, board and score here is invented."""
import copy

import pytest

from relay import execution_scope, model_signals as signals, native_catalog, turn_engine, work_units
from relay.execution_team import reasoning
from test_conversation_tasks import host, intake  # noqa: F401  (host is a fixture)
from test_expiring_allowance import API_KEY, CHEAP, MUSE, OPENROUTER
from test_task_types import KIND, ONE
from test_unit_effort import full
from test_unit_kind import PLAN

TYPES = [{**KIND, 'id': i, 'sources': [ONE]} for i in ('coding', 'math')]
CODING, MATH = {'kind': 'coding'}, {'kind': 'math'}
# route order today: 'cheap' (model q), 'openrouter-deepseek' (deepseek/v4), 'api' (fable-5-2)
Q, DEEP, FABLE = 'q', 'deepseek/v4', 'fable-5-2'
TODAY = ['cheap', 'openrouter-deepseek', 'api']


@pytest.fixture
def world(monkeypatch):
    """`world(general, board)` gives a model its general class and, in `board` ({(model, type id): (class, score, effort)}),
    its class on a type's board. Returns a function: the routes `routes` yields, as {name: effort it runs at}."""
    monkeypatch.setattr(signals, 'TYPES', TYPES)
    found = {d['id']: full(d) for d in (CHEAP, OPENROUTER, API_KEY, MUSE)}
    monkeypatch.setattr(native_catalog, 'select', lambda name: copy.deepcopy(found[name]))
    monkeypatch.setattr(turn_engine, 'policy', lambda: {'implementer': [['cheap'], ['openrouter-deepseek'], ['api']]})
    monkeypatch.setattr(turn_engine, 'local_gpu_busy', lambda deployment: None)
    monkeypatch.setattr(turn_engine, 'avoid_local_gpu', lambda: False)
    for key in ('OPENROUTER_API_KEY', 'ANTHROPIC_API_KEY'):
        monkeypatch.setenv(key, 'fixture')
    model = lambda d: d['native_profile']['model']

    def make(general, board=None):
        board = board or {}
        monkeypatch.setattr(signals, 'klass', lambda d: {'class': general[model(d)]})
        monkeypatch.setattr(signals, 'boards', lambda: {'k': 1})

        def placed(deployment, kind, kept):
            if kept and (model(deployment), kind['id']) in board:
                cls, score, effort = board[model(deployment), kind['id']]
                return {'class': cls, 'general': False, 'source': ONE['name'], 'score': score, 'effort': effort}
            return {'class': general[model(deployment)], 'general': True}
        monkeypatch.setattr(signals, 'type_class', placed)

        def run(**arguments):
            with execution_scope.activated(execution_scope.Scope()):
                return {name: reasoning(d) for name, d in work_units.routes(full(MUSE), **arguments)}
        return run
    return make


GENERAL = {Q: 'cheap', DEEP: 'middling', FABLE: 'middling'}
NEED_BOTH = [{'needs': 'middling', **CODING}, {'needs': 'middling', **MATH}]


def test_a_route_that_reaches_every_part_goes_before_the_better_at_one_but_weak_at_the_other(world):
    # deepseek is frontier at coding and cheap at math; fable is middling at both; today's hardest-task rule sees coding only
    run = world(GENERAL, {(DEEP, 'coding'): ('frontier', .9, None), (DEEP, 'math'): ('cheap', .1, None),
                          (FABLE, 'coding'): ('middling', .5, None), (FABLE, 'math'): ('middling', .5, None)})
    assert list(run(needs='middling', work_type='coding')) == ['openrouter-deepseek', 'api', 'cheap']  # today
    assert list(run(needs='middling', work_type='coding', parts=NEED_BOTH)) == ['api', 'cheap', 'openrouter-deepseek']
    # the task it is weak at states no need: nothing to fall short of
    assert list(run(needs='middling', work_type='coding', parts=[NEED_BOTH[0], MATH])) == ['openrouter-deepseek', 'api', 'cheap']


def test_a_route_that_reaches_every_part_goes_before_one_short_on_any_part_whatever_its_cost(world):
    run = world(GENERAL, {(Q, 'coding'): ('cheap', .1, None), (Q, 'math'): ('cheap', .1, None),
                          (DEEP, 'coding'): ('middling', .5, None), (DEEP, 'math'): ('middling', .5, None),
                          (FABLE, 'coding'): ('frontier', .9, None), (FABLE, 'math'): ('cheap', .1, None)})
    parts = [{'needs': 'frontier', **CODING}, {'needs': 'middling', **MATH}]
    # needs: cheap is two short on coding, deepseek one short, fable one short on math only
    assert list(run(needs='frontier', work_type='coding', parts=parts)) == ['openrouter-deepseek', 'api', 'cheap']
    parts = [{'needs': 'middling', **CODING}, {'needs': 'middling', **MATH}]
    assert list(run(needs='middling', work_type='coding', parts=parts)) == ['openrouter-deepseek', 'cheap', 'api']  # api is short on math
    # the largest shortfall counts, not the first part's: cheap and fable are two short on math, deepseek one
    assert list(run(needs='frontier', work_type='coding', parts=[{'needs': 'cheap', **CODING}, {'needs': 'frontier', **MATH}])) == [
        'openrouter-deepseek', 'cheap', 'api']


def test_parts_that_are_one_hint_order_the_routes_exactly_as_today(world):
    run = world(GENERAL, {(DEEP, 'coding'): ('frontier', .9, None), (DEEP, 'math'): ('cheap', .1, None),
                          (FABLE, 'coding'): ('middling', .5, None), (FABLE, 'math'): ('middling', .5, None)})
    one = [{'needs': 'middling', **CODING}]
    assert run(needs='middling', work_type='coding', parts=one) == run(needs='middling', work_type='coding')
    assert run(needs='middling', work_type='coding', parts=None) == run(needs='middling', work_type='coding')
    assert list(run()) == TODAY  # no need, no ranking
    crit = [{'critical': True, **CODING}]
    assert run(work_type='coding', critical=True, parts=crit) == run(work_type='coding', critical=True)


def test_a_critical_task_is_ranked_by_its_own_type_not_the_hardest_ones(world):
    run = world(GENERAL, {(DEEP, 'coding'): ('frontier', .9, None), (DEEP, 'math'): ('middling', .5, None),
                          (FABLE, 'coding'): ('frontier', .5, None), (FABLE, 'math'): ('frontier', .8, None)})
    parts = [{'needs': 'frontier', **CODING}, {'critical': True, **MATH}]
    # the unit's type is the hardest task's (coding): deepseek leads on its score; math is the critical task's
    assert list(run(needs='frontier', work_type='coding', critical=True)) == ['openrouter-deepseek', 'api', 'cheap']
    assert list(run(needs='frontier', work_type='coding', critical=True, parts=parts)) == ['api', 'openrouter-deepseek', 'cheap']


def test_with_two_critical_tasks_the_route_whose_worst_one_is_best_goes_first(world):
    run = world(GENERAL, {(DEEP, 'coding'): ('frontier', .9, None), (DEEP, 'math'): ('middling', .9, None),
                          (FABLE, 'coding'): ('frontier', .6, None), (FABLE, 'math'): ('frontier', .6, None)})
    both = [{'critical': True, **CODING}, {'critical': True, **MATH}]
    assert list(run(work_type='coding', critical=True, parts=[both[0]])) == ['openrouter-deepseek', 'api', 'cheap']  # one task
    assert list(run(work_type='coding', critical=True, parts=both)) == ['api', 'openrouter-deepseek', 'cheap']
    # tied on the critical tasks (the same two boards), the weakest task's need orders them
    run = world(GENERAL, {(m, t): ('frontier', .5, None) for m in (DEEP, FABLE) for t in ('coding', 'math')})
    parts = [{'critical': True, **CODING}, {'needs': 'frontier', **MATH}]
    assert list(run(needs='frontier', work_type='coding', critical=True, parts=parts)) == ['openrouter-deepseek', 'api', 'cheap']


def test_the_effort_raised_is_the_highest_over_the_parts(world):
    run = world(GENERAL, {(FABLE, 'coding'): ('frontier', .9, 'medium'), (FABLE, 'math'): ('frontier', .9, 'max')})
    assert run(work_type='coding')['api'] == 'medium' and run(work_type='math')['api'] == 'high'  # math's max, capped
    parts = [{'needs': 'frontier', **CODING}, {'needs': 'frontier', **MATH}]
    assert run(needs='frontier', work_type='coding', parts=parts)['api'] == 'high'
    assert run(needs='frontier', work_type='coding', critical=True, parts=parts)['api'] == 'xhigh'
    assert run(needs='frontier', work_type='coding', parts=parts, unit_effort='low')['api'] == 'low'  # a stated one replaces it
    assert run(needs='frontier', work_type='coding', parts=[parts[0], {'needs': 'cheap'}])['api'] == 'medium'  # no type: none


def test_a_pin_and_a_route_the_coordinator_chose_are_untouched(world, monkeypatch):
    run = world(GENERAL, {(FABLE, 'coding'): ('frontier', .9, 'max')})
    assert list(run(chosen='api', needs='frontier', work_type='coding', parts=NEED_BOTH)) == ['api']
    monkeypatch.setattr(execution_scope.Scope, 'pin', lambda self, role: 'api')
    monkeypatch.setattr(execution_scope, 'pin_selection', lambda *a: {'native_model_choice': {'connection': 'api'}})
    monkeypatch.setattr(signals, 'boards', lambda: pytest.fail('a pin reads no board'))
    assert [n for n, _ in work_units.routes(MUSE, work_type='coding', parts=NEED_BOTH)] == ['api']


def test_the_boards_are_read_once_for_the_whole_ranking(world, monkeypatch):
    run = world(GENERAL, {(FABLE, 'coding'): ('frontier', .9, 'max')})
    reads = []
    monkeypatch.setattr(signals, 'boards', lambda: reads.append(1) or {'k': 1})
    run(needs='frontier', work_type='coding', critical=True, parts=NEED_BOTH + [{'critical': True, **MATH}])
    assert reads == [1]


def test_the_outcome_row_names_the_weakest_part_and_the_units_types(world):
    world(GENERAL, {(DEEP, 'coding'): ('frontier', .9, None), (DEEP, 'math'): ('cheap', .1, None)})
    deployment = {'native_profile': {'model': DEEP}}
    unit = {'needs': 'middling', 'kind': 'coding', 'parts': NEED_BOTH}
    assert work_units.class_row(unit, deployment) == {
        'needs': 'middling', 'route_class': 'cheap', 'kind': 'math', 'class_source': ONE['name'], 'kinds': ['coding', 'math']}
    # as today without parts, and a plan saved before the field is a unit with none
    assert work_units.class_row({'needs': 'middling', 'kind': 'coding'}, deployment) == {
        'needs': 'middling', 'route_class': 'frontier', 'kind': 'coding', 'class_source': ONE['name']}
    from relay.conversation_tasks import delivery_view
    view = delivery_view({'verdict': 'INCONCLUSIVE', 'reason': 'r', **work_units.class_row(unit, deployment)})
    assert view['kinds'] == ['coding', 'math'] and view['kind'] == 'math'
    assert 'kinds' not in delivery_view({'verdict': 'INCONCLUSIVE', 'reason': 'r'})


def test_the_unit_keeps_each_tasks_hints_only_when_they_differ(host):
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
    hard, easy = {'needs': 'frontier', 'kind': 'coding'}, {'needs': 'middling', 'kind': 'math'}
    unit = unit_of(hard, easy, {}, dict(easy), {'critical': True, 'kind': 'math'}, {'effort': 'low'})
    assert unit['parts'] == [hard, easy, {'kind': 'math', 'critical': True}]  # only the stated keys, each once, none for {}
    assert (unit['needs'], unit['kind'], unit['critical']) == ('frontier', 'coding', True)  # the unit as it was computed
    for given in ([hard, dict(hard), {}], [hard, {}], [{}, {}], [{'effort': 'low'}, hard]):
        assert 'parts' not in unit_of(*given)
    assert unit_of(hard, {'critical': False})['needs'] == 'frontier'
