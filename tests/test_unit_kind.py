"""A plan's task names its type of work (`kind`), and its implementer is chosen by the class for that type where a board
places the model, else by the general class. Every model and number here is invented."""
import json

import pytest

from relay import model_signals as signals
from relay import native_artifacts
from relay.native_artifacts import GraphTask
from test_expiring_allowance import CHEAP, LADDER, cli, unit  # noqa: F401  (unit is a fixture)
from test_conversation_tasks import host, intake  # noqa: F401  (host is a fixture)
from test_task_types import KIND, ONE, board, route

TASK = dict(key='a', title='t', summary='s', cluster='c', priority='P1', criteria=['x'], target_paths=['a.py'], depends_on=[])
PLAN = dict(summary='s', sources=[], remaining=[], steps=['do'], criteria=['x'])
# Sol and Terra lead this board of eight lines, Carbon and Astra (frontier in general) trail it. Invented.
BOARD = [('gpt-6.1-sol_max', 'OpenAI', .90), ('gpt-6.1-terra_high', 'OpenAI', .80), ('claude-opus-5-5_high', 'Anthropic', .70),
         ('gemini-4-argon_high', 'Google DeepMind', .60), ('gemini-3.8-flash_high', 'Google DeepMind', .50),
         ('claude-haiku-5-5_high', 'Anthropic', .40), ('gpt-6-astra_max', 'OpenAI', .30),
         ('gemini-4-carbon_high', 'Google DeepMind', .20)]
TYPE = {**KIND, 'id': 'coding', 'sources': [ONE]}
KEPT = {signals.source_key(ONE): board(ONE, anchors=BOARD)}
SOL, ASTRA, KIMI = route('gpt-6.1-sol'), route('gpt-6-astra'), route('moonshotai/kimi-k3.1-pro')


@pytest.fixture
def types(monkeypatch):
    monkeypatch.setattr(signals, 'TYPES', [TYPE])


def test_the_task_may_name_a_type_from_the_catalogue_and_an_unknown_one_is_refused():
    ids = [t['id'] for t in signals.TYPES]
    assert GraphTask(**TASK).kind is None and GraphTask(**TASK, kind='coding').kind == 'coding'
    assert all(GraphTask(**TASK, kind=i).kind == i for i in ids)
    with pytest.raises(ValueError):
        GraphTask(**TASK, kind='astrology')
    text = GraphTask.model_json_schema()['properties']['kind']['description']
    assert text.startswith('The type of work this task is, when it is clearly one of: coding, web, math, science, '
                           'professional (legal, consulting, finance), security,')
    assert text.endswith('Leave it out when the task is none of these or mixes several.') and ids[-1] in text


def test_a_plan_saved_before_the_field_keeps_its_digest():
    old = dict(PLAN, tasks=[TASK])
    assert native_artifacts.validate('planner', old)['tasks'][0].keys() == {*TASK, 'verifiers', 'test_paths'}
    with_kind = native_artifacts.validate('planner', dict(PLAN, tasks=[dict(TASK, kind='math')]))
    assert with_kind['tasks'][0]['kind'] == 'math' and 'needs' not in with_kind['tasks'][0]
    assert 'kind' not in json.dumps(native_artifacts.validate('planner', old))


def test_a_board_that_places_the_model_judges_it_by_the_type_else_by_the_general_class(types):
    # in general Sol is middling and Astra frontier; on this board Sol leads and Astra trails
    assert signals.judged(SOL)['class'] == 'middling' and signals.judged(ASTRA)['class'] == 'frontier'
    sol, astra = signals.judged(SOL, 'coding', KEPT), signals.judged(ASTRA, 'coding', KEPT)
    assert sol == {'class': 'frontier', 'source': 'Board one.csv'} and astra['class'] == 'cheap'
    assert not signals.reaches(SOL, 'frontier') and signals.reaches(SOL, 'frontier', 'coding', KEPT)  # now reaches it
    assert signals.reaches(ASTRA, 'frontier') and not signals.reaches(ASTRA, 'middling', 'coding', KEPT)  # no longer
    assert signals.short(ASTRA, 'frontier', 'coding', KEPT) == 2
    # no board places it, no saved boards, no type, an id the catalogue does not have: the general class
    assert signals.judged(KIMI, 'coding', KEPT) == signals.judged(KIMI) == {'class': 'cheap', 'source': 'general'}
    assert signals.judged(SOL, 'coding', {}) == {'class': 'middling', 'source': 'general'}
    assert signals.judged(SOL, None, KEPT) == signals.judged(SOL, 'astrology', KEPT) == signals.judged(SOL)


def faked(monkeypatch, boards):
    """Fable is middling in general and cheap for the type; the reseller's model is cheap in general and frontier for it;
    the others no board places. Returns how many times the boards were read."""
    reads = []
    monkeypatch.setattr(signals, 'klass', lambda d: {'class': 'middling'} if 'fable' in d['native_profile']['model'] else None)
    monkeypatch.setattr(signals, 'boards', lambda: reads.append(1) or boards)

    def placed(deployment, kind, kept):
        model = deployment['native_profile']['model']
        if kept and 'fable' in model:
            return {'class': 'cheap', 'general': False, 'source': 'Board'}
        if kept and 'deepseek' in model:
            return {'class': 'frontier', 'general': False, 'source': 'Board'}
        return {'class': (signals.klass(deployment) or {}).get('class'), 'general': True}
    monkeypatch.setattr(signals, 'type_class', placed)
    return reads


def test_the_routes_of_a_unit_follow_the_class_for_its_type(unit, monkeypatch, types):
    reads = faked(monkeypatch, KEPT)
    today = ['claude-fable-5-2', 'api', 'cheap', 'openrouter-deepseek']
    assert unit(needs='middling') == today
    reads.clear()
    # a route that now reaches the need (DeepSeek) and a route that no longer does (Fable)
    assert unit(needs='middling', work_type='coding') == ['openrouter-deepseek', 'cheap', 'claude-fable-5-2', 'api']
    assert reads == [1]  # the boards are read once for the whole ranking, not once per route
    assert unit(needs='frontier', work_type='coding') == ['openrouter-deepseek', 'cheap', 'claude-fable-5-2', 'api']
    assert unit(needs='frontier') == today  # no type: today's order


def test_with_no_saved_boards_or_no_type_the_order_is_todays(unit, monkeypatch, types):
    faked(monkeypatch, {})
    assert unit(needs='middling', work_type='coding') == unit(needs='middling') == [
        'claude-fable-5-2', 'api', 'cheap', 'openrouter-deepseek']
    assert unit(work_type='coding') == unit() == LADDER  # no need: the type alone ranks nothing


def test_a_chosen_route_and_a_pin_are_never_reordered(unit, monkeypatch, types):
    from relay import execution_scope
    faked(monkeypatch, KEPT)
    assert unit(chosen='cheap', needs='frontier', work_type='coding') == ['cheap']
    monkeypatch.setattr(execution_scope.Scope, 'pin', lambda self, role: 'cheap')
    monkeypatch.setattr(execution_scope, 'pin_selection', lambda *a: {'native_model_choice': {'connection': 'cheap'}})
    monkeypatch.setattr(signals, 'boards', lambda: pytest.fail('a pin is not ranked'))
    assert unit(needs='frontier', work_type='coding') == ['cheap']


def test_the_outcome_row_carries_the_type_and_the_class_it_was_judged_by(types):
    from relay.work_units import class_row
    assert class_row({'needs': 'frontier'}, SOL) == {'needs': 'frontier', 'route_class': 'middling'}  # as today
    assert class_row({'kind': 'coding'}, SOL) == {}  # a type with no need ranks nothing, so records nothing
    # the boards of this machine are the saved ones: none here, so the general class, said as such
    assert class_row({'needs': 'frontier', 'kind': 'coding'}, SOL) == {
        'needs': 'frontier', 'route_class': 'middling', 'kind': 'coding', 'class_source': 'general'}


def test_the_row_names_the_board_that_placed_the_model(monkeypatch, types):
    from relay.work_units import class_row
    monkeypatch.setattr(signals, 'boards', lambda: KEPT)
    assert class_row({'needs': 'frontier', 'kind': 'coding'}, SOL) == {
        'needs': 'frontier', 'route_class': 'frontier', 'kind': 'coding', 'class_source': 'Board one.csv'}


def test_the_delivery_says_the_type_beside_the_need():
    from relay.conversation_tasks import delivery_view
    view = delivery_view({'verdict': 'INCONCLUSIVE', 'reason': 'r', 'needs': 'frontier', 'route_class': 'cheap',
                          'kind': 'coding', 'class_source': 'Board one.csv'})
    assert (view['kind'], view['class_source']) == ('coding', 'Board one.csv')
    assert view['class_note'] == ('The plan says this unit needs a frontier model; the route that ran is a cheap one '
                                  'for coding work (Board one.csv).')
    assert delivery_view({'verdict': 'INCONCLUSIVE', 'reason': 'r', 'needs': 'frontier', 'route_class': 'cheap'}
                         )['class_note'].endswith('the route that ran is a cheap one.')


def test_the_unit_takes_the_type_of_the_task_that_needs_the_most(host):
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
    both = unit_of({'needs': 'middling', 'kind': 'coding'}, {'needs': 'frontier', 'kind': 'math'})
    assert (both['needs'], both['kind']) == ('frontier', 'math')
    assert 'kind' not in unit_of({'needs': 'frontier'}, {'needs': 'cheap', 'kind': 'math'})  # the hardest has none
    assert 'kind' not in unit_of({'kind': 'math'})  # no need: no ranking, so no type kept
