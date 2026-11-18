"""A demand may name its types of work (`kinds`), and the conversation's planner and a plan's reviewer are then judged
by the weakest of them: only whether a route falls short of the role's class moves it. Independence, another vendor
first, a pin and the owner's own lists are as before. Every model, board and number here is invented."""
import json
from types import SimpleNamespace

import pytest

from relay import conversation_tasks as tasks, native_catalog, native_routes, planning_tools, turn_engine
from relay import execution_scope as scope
from relay import model_signals as signals
from test_auditor_kind import FABLE, LINES, not_read
from test_conversation_tasks import effect, host, intake  # noqa: F401  (host is a fixture)
from test_cross_vendor_auditors import pool  # noqa: F401  (a fixture)
from test_planning_tools import execution
from test_role_pin_dispatch import pin_catalog  # noqa: F401  (a fixture)
from test_role_pins import parse
from test_task_types import KIND, ONE, TWO, board

TYPES = [{**KIND, 'id': 'coding', 'sources': [ONE]}, {**KIND, 'id': 'math', 'sources': [TWO]}]
BOTH = ['coding', 'math']
ASTRA, SOL, OPUS = 'openai-api-gpt-6-astra', 'openai-api', 'claude-api-opus-5-5'


def saved(monkeypatch, coding=(), math=()):
    """The machine's saved boards: for each type one board ranking the eight lines in that order, best first (the first
    two frontier for the type, the next four middling, the last two cheap); none for a type with no order. Returns the
    list of reads."""
    reads = []
    kept = {signals.source_key(src): board(src, anchors=[(*LINES[n], .9 - .1*i) for i, n in enumerate(order)])
            for src, order in ((ONE, coding), (TWO, math)) if order}
    monkeypatch.setattr(signals, 'TYPES', TYPES)
    monkeypatch.setattr(signals, 'boards', lambda: reads.append(1) or kept)
    return reads


def planners(kinds=None):
    """([route ids in order], {route id: its candidate row}) of the automatic choice of the planner."""
    chosen, receipt = native_routes.ranked('planner', work_type=kinds)
    return [d['id'] for d in chosen], {c['deployment']: c for c in receipt['candidates']}


def plan_reviewers(planner, kinds=None):
    """The same for the automatic choice of the reviewer of a plan by `planner`."""
    chosen, receipt = native_routes.reviewers(native_catalog.select(planner), 'plan-auditor', work_type=kinds)
    return [d['id'] for d in chosen], {c['deployment']: c for c in receipt['candidates']}


def planning_tool(planning, root='.'):
    tools = SimpleNamespace(names=('read',), tool_specs_json='{}', session_id='parent', binding=execution().binding)
    return planning_tools.PlanningTools(tools, root, {'planning': planning})


def test_the_demand_records_its_types_for_a_new_or_revised_demand_and_keeps_them_on_continue_or_correct(host):
    spec = tasks.SPEC['demand']['parameters']['properties']['kinds']
    assert spec['items']['enum'] == [t['id'] for t in signals.TYPES] and spec['maxItems'] == 6
    assert spec['description'] == (
        'The types of work this demand involves, when it is clearly one or more of: '
        + ', '.join(t['id'] + (f" ({t['hint']})" if t.get('hint') else '') for t in signals.TYPES)
        + '. tare then picks a planner that is good at every one of them. '
        'Omit it when the demand is none of these.')
    assert 'professional (legal, consulting, finance)' in spec['description']
    root = host[0].root
    assert intake(host, kinds=['cad', '3d'])['task']['kinds'] == ['cad', '3d']
    for relation in ('continue', 'correct'):
        intake(host, relation=relation)
        assert tasks.current(tasks.load(root))['kinds'] == ['cad', '3d']
    intake(host, relation='revise')  # omitted: the recorded ones, as the objective
    assert tasks.current(tasks.load(root))['kinds'] == ['cad', '3d']


@pytest.mark.parametrize('kinds', [['knitting'], ['cad', 'cad'], [t['id'] for t in signals.TYPES][:7], 'cad'])
def test_an_unknown_or_repeated_type_or_more_than_six_are_refused_and_record_nothing(host, kinds):
    tools, control, *_ = host
    result = tools.execute(effect('demand', {'intent': 'deliver', 'relation': 'new', 'objective': 'Change value to two.',
                                             'criteria': ['value equals two'], 'kinds': kinds}, key='typed'), control)
    assert result.status == 'refused' and json.loads(result.output_json)['reason'].startswith('Give kinds as up to 6')
    assert tasks.load(tools.root)['tasks'] == []
    assert 'kinds' not in intake(host)['task']  # a demand that names none records none, as before this field


def test_delegate_plan_carries_the_recorded_types_to_the_planners_choice(host):
    tools, control, *_ = host
    tool = planning_tools.PlanningTools(SimpleNamespace(names=(), tool_specs_json='{}', session_id='parent',
        binding=control.binding), tools.root, {**tools.state, 'planning': {'routes': {}, 'route_order': []}})
    asked = []

    def choose(args, kinds=None):
        asked.append(kinds)
        raise ValueError('stop before any model call')
    tool.choose = choose
    for arguments in ({}, {'kinds': ['cad', '3d']}):
        intake(host, relation='revise' if asked else 'new', **arguments)
        with pytest.raises(ValueError, match='stop'):
            tool.run(SimpleNamespace(effect_id='plan-'+str(len(asked))), {'instruction': 'Plan it.'}, control)
    assert asked == [None, ['cad', '3d']]  # a demand with no types asks exactly as before


def test_a_planner_weak_at_one_of_the_types_goes_after_one_good_at_both(pool, monkeypatch):
    # Astra is frontier in general and for coding but middling for math; Sol, middling in general, is frontier for
    # both; Fable, which no board places, keeps its general class (frontier) for both.
    reads = saved(monkeypatch, coding=('sol', 'astra', 'terra', 'opus', 'argon', 'haiku', 'flash', 'carbon'),
                  math=('sol', 'terra', 'opus', 'astra', 'argon', 'haiku', 'flash', 'carbon'))
    today, before = planners()
    assert today[:2] == [ASTRA, FABLE] and reads == []
    typed, rows = planners(BOTH)
    assert reads == [1]  # once for the whole ranking
    assert typed.index(SOL) < typed.index(ASTRA) and typed.index(FABLE) < typed.index(ASTRA)
    assert set(typed) == set(today)  # the same planners, in another order
    astra, sol = rows[ASTRA], rows[SOL]
    assert (astra['kind'], astra['class_source'], astra['classes_short']) == (BOTH, ['Board one.csv', 'Board two.csv'], 1)
    assert (sol['classes_short'], before[SOL]['classes_short'], rows[FABLE]['class_source']) == (0, 1, ['general', 'general'])
    assert 'kind' not in before[ASTRA]  # with no types the row is today's
    # The session's planner follows that order when planning starts, and only then.
    monkeypatch.setattr(turn_engine, 'available', lambda name, **kw: (native_catalog.select(name), None))
    tool = planning_tool(planning_tools.snapshot())
    assert tool.route_names()[0] == ASTRA and tool.choose({})[0] == ASTRA
    assert tool.choose({}, BOTH)[0] == typed[0] != ASTRA
    assert tool.route_names()[0] == ASTRA  # the saved order and the tool's schema are untouched


def test_a_plan_reviewer_is_judged_by_its_weakest_type_and_one_good_at_both_keeps_its_place(pool, monkeypatch):
    # For Fable's plan: Sol is frontier for both types (better than a reviewer needs), middling in general; nobody falls
    # short. Sol is not put after Gemini for being above the class: that stays a matter of the general class.
    saved(monkeypatch, coding=('sol', 'astra', 'terra', 'opus', 'argon', 'haiku', 'flash', 'carbon'),
          math=('sol', 'terra', 'opus', 'astra', 'argon', 'haiku', 'flash', 'carbon'))
    today, _ = plan_reviewers(FABLE)
    typed, rows = plan_reviewers(FABLE, BOTH)
    assert typed == today and typed.index(SOL) < typed.index('gemini-api') and rows[SOL]['classes_short'] == 0
    # Argon, middling in general and for coding, is cheap for math: it now goes after the reviewers that reach middling
    # for both.
    saved(monkeypatch, coding=('sol', 'terra', 'opus', 'astra', 'argon', 'haiku', 'flash', 'carbon'),
          math=('sol', 'terra', 'opus', 'astra', 'haiku', 'flash', 'argon', 'carbon'))
    today, _ = plan_reviewers(ASTRA)
    typed, rows = plan_reviewers(ASTRA, BOTH)
    assert today[0] == 'opencode-sub-gemini-4-argon' and typed[0] == OPUS
    # Kimi and Muse Wonder have no class (basic): two classes below Astra in general (frontier), one below it for these
    # types (middling), so only the typed plan has them (model_signals.may_review, owner 2026-11-18).
    extra = {'openrouter-kimi-k3-1-pro', 'openrouter-muse-wonder'}
    assert typed.index(FABLE) < typed.index('opencode-sub-gemini-4-argon') and set(typed) == set(today) | extra


def test_a_reviewer_of_the_planners_model_or_bias_domain_is_never_picked_whatever_its_class(pool, monkeypatch):
    # Sol (Astra's bias domain) and Astra itself are frontier for both types; every other reviewer falls short on one.
    saved(monkeypatch, coding=('sol', 'astra', 'terra', 'opus', 'haiku', 'flash', 'argon', 'carbon'),
          math=('astra', 'sol', 'terra', 'argon', 'flash', 'haiku', 'opus', 'carbon'))
    typed, rows = plan_reviewers(ASTRA, BOTH)
    assert typed
    for own in (ASTRA, SOL):
        assert own not in typed and rows[own]['reason'] == 'AUDITOR_NOT_INDEPENDENT'


def test_another_vendor_still_comes_first_when_the_same_vendor_is_the_one_good_at_both(pool, monkeypatch):
    # Opus (Fable's vendor) is frontier for both types; every reviewer of another vendor is cheap for one of them.
    saved(monkeypatch, coding=('opus', 'terra', 'sol', 'astra', 'haiku', 'flash', 'argon', 'carbon'),
          math=('opus', 'terra', 'argon', 'haiku', 'flash', 'carbon', 'sol', 'astra'))
    typed, rows = plan_reviewers(FABLE, BOTH)
    assert typed[-1] == OPUS and rows[OPUS]['reason'] == 'SAME_VENDOR_NOT_INDEPENDENT' and rows[OPUS]['classes_short'] == 0
    assert all(rows[n]['classes_short'] == 1 for n in typed[:-1])
    monkeypatch.setattr(turn_engine, 'cross_vendor_first', lambda: False)  # --any_vendor_auditor
    assert plan_reviewers(FABLE, BOTH)[0][0] == OPUS


def test_the_owners_lists_are_never_reordered_and_read_no_board(pool, monkeypatch):
    monkeypatch.setattr(turn_engine, 'available', lambda name, **kw: (native_catalog.select(name), None))
    turn_engine.save_policy({'planner': [[SOL], [OPUS]], 'auditor': [[SOL], ['gemini-api']]})
    today = planning_tool(planning_tools.snapshot()).choose({})[0]
    reviewers_today = plan_reviewers(FABLE)[0]
    monkeypatch.setattr(signals, 'boards', not_read)
    assert planning_tool(planning_tools.snapshot()).choose({}, BOTH)[0] == today == SOL
    assert turn_engine.planner_routes(BOTH) == [SOL, OPUS]
    assert plan_reviewers(FABLE, BOTH)[0] == reviewers_today == [SOL, 'gemini-api']


def test_a_planner_pin_and_an_auditor_pin_are_untouched_and_read_no_board(pin_catalog, monkeypatch):
    monkeypatch.setattr(signals, 'boards', not_read)
    with scope.activated(parse(['--planner', 'anthropic/test-cli@max'])):
        name, value = planning_tool({'routes': {}, 'route_order': []}).choose({}, BOTH)
        assert name == 'test-cli' and scope.pin_matches('anthropic/test-cli@max', value)
        assert native_routes.ranked('planner', work_type=BOTH)[1]['reason'] == 'EXACT_TEMPORARY_PIN'
    # A pinned reviewer is checked for the types (model_signals.may_review): with no saved board, by the general classes.
    monkeypatch.setattr(signals, 'boards', lambda: {})
    with scope.activated(parse(['--auditor', 'anthropic/test-cli@max'])):
        chosen, receipt = native_routes.reviewers(pin_catalog['producer-cli'], 'plan-auditor', work_type=BOTH)
        assert receipt['reason'] == 'EXACT_TEMPORARY_PIN' and scope.pin_matches('anthropic/test-cli@max', chosen[0])


@pytest.mark.parametrize('planner', [FABLE, OPUS, SOL, ASTRA, 'gemini-api'])
def test_with_no_saved_boards_both_selections_are_todays(pool, monkeypatch, planner):
    saved(monkeypatch)
    today, before = planners()
    typed, rows = planners(BOTH)
    assert typed == today and all(rows[n]['classes_short'] == before[n]['classes_short'] for n in today)
    today, before = plan_reviewers(planner)
    typed, rows = plan_reviewers(planner, BOTH)
    assert typed == today and all(rows[n]['classes_short'] == before[n]['classes_short']
                                  and rows[n]['class_source'] == ['general', 'general'] for n in today)
