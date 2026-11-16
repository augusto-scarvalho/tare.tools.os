"""The planner of a plain conversation comes from the role table by class, never from a list of names in the code;
a list the owner saved still decides (owner, 2026-11-15)."""
from relay import environment, native_catalog, native_routes, planning_tools, turn_engine
from relay.model_signals import klass
from test_cross_vendor_auditors import pool  # noqa: F401  (fixture)
from test_planning_tools import execution, planner, wrap  # noqa: F401

SUBSCRIPTION = 'opencode-sub-gemini-4-argon'  # a middling route on a subscription, on no list the code ever held


def test_a_route_switched_on_outside_the_old_list_plans(pool):
    names = planning_tools.snapshot()['route_order']
    assert SUBSCRIPTION in names and list(planning_tools.snapshot()['routes']) == names
    assert names == turn_engine.planner_routes()  # one source for the snapshot, /delegation and the serve flow


def test_a_planner_list_the_owner_saved_still_decides(pool):
    turn_engine.save_policy({'planner': [['openai-api'], ['claude-api-opus-5-5']]})
    snapshot = planning_tools.snapshot()
    assert snapshot['route_order'] == ['openai-api', 'claude-api-opus-5-5'] == turn_engine.planner_routes()
    assert SUBSCRIPTION not in snapshot['routes']


def test_clearing_the_saved_planner_list_goes_back_to_automatic(pool):
    turn_engine.save_policy({'planner': [['openai-api']]})
    turn_engine.save_policy({'planner': [[]], 'implementer': [['local-default']]})  # the Order tab saves an empty box
    assert 'planner' not in turn_engine.policy() and SUBSCRIPTION in planning_tools.snapshot()['routes']


def test_a_route_switched_off_for_the_planner_never_plans(pool):
    environment.save({'routing_profile': {'off': {'planner': [SUBSCRIPTION, 'claude/fable@api']}}})
    names = planning_tools.snapshot()['route_order']
    assert SUBSCRIPTION not in names and 'claude-api-fable-5-1' not in names and 'openai-api-gpt-6-astra' in names


def test_the_planner_prefers_a_frontier_route_per_token_to_a_middling_subscription_one(pool):
    names = planning_tools.snapshot()['route_order']
    frontier = [n for n in names if (klass(native_catalog.select(n)) or {}).get('class') == 'frontier']
    assert 'openai-api-gpt-6-astra' in frontier and 'claude-api-fable-5-1' not in frontier  # 5.1: an older version
    assert names[:len(frontier)] == frontier  # every route that reaches frontier precedes any that falls short
    assert names.index('openai-api-gpt-6-astra') < names.index(SUBSCRIPTION)
    assert names.index('openai-api-gpt-6-astra') < names.index('claude-api-fable-5-1')  # the newer line first
    _, selection = native_routes.ranked('planner')
    short = {row['deployment']: row['classes_short'] for row in selection['candidates'] if row['eligible']}
    assert short['openai-api-gpt-6-astra'] == 0 and short[SUBSCRIPTION] == 1
    assert next(row for row in selection['candidates'] if row['deployment'] == SUBSCRIPTION)['cost_tier'] == 'flat'


def test_a_resumed_plan_keeps_its_saved_routes_and_their_order(planner):
    parent, state, candidate = planner
    state['planning'] = {'routes': {'worker': candidate, 'spare': candidate}, 'route_order': ['worker', 'spare']}
    saved = {'routes': dict(state['planning']['routes']), 'route_order': ['worker', 'spare']}
    tool = wrap(parent, state, execution())
    assert tool.planning == saved and tool.route_names() == ['worker', 'spare']
    assert planning_tools.snapshot()['route_order'] != tool.route_names()  # a new session would plan elsewhere
