"""tare auto's tiers, for delegated implementers and for a conversation's own model (the coordinator), are nested floors
of intelligence over the role table (the cheapest sufficient route first), never a list of route names in the code; a
list the owner saved still decides (owner, 2026-11-16)."""
import json

import pytest

from relay import cli_discovery, environment, native_catalog, native_routes, turn_engine
from relay import model_signals as signals
from relay.model_signals import ORDER, klass
from test_class_anchors import other, twice
from test_cross_vendor_auditors import install, pool  # noqa: F401  (fixture)
from test_derived_role_rows import SOL, discovered

# The hand-written tiers this change removed: none of these routes may be lost.
OLD = ['local-default', 'bedrock-sonnet', 'codex-native-gpt-6-luna', 'codex-cli-gpt-6-luna', 'claude-sonnet-5-5-cli',
       'openrouter-deepseek-v4-1-flash-1017', 'bedrock-opus', 'codex-native-gpt-6-1-sol', 'codex-cli-gpt-6-1-sol',
       'claude-opus-5-5-cli', 'openrouter-glm-5-3', 'codex-native', 'codex-cli', 'claude-fable-5-1-cli',
       'agy-gemini-4-argon-cli']
CODEX_DIRECT = {'codex-native': 'gpt-6-astra', 'codex-native-gpt-6-1-sol': 'gpt-6.1-sol',
                'codex-native-gpt-6-luna': 'gpt-6-luna', 'codex-native-gpt-5-5': 'gpt-5.5',
                'codex-native-gpt-5-6-luna': 'gpt-5.6-luna', 'codex-native-gpt-5-6-sol': 'gpt-5.6-sol',
                'codex-native-gpt-5-6-terra': 'gpt-5.6-terra'}


@pytest.fixture
def everything(pool):
    """Every route the role table names, set up here: the vendor CLIs (this file as their executable), Codex Direct,
    and a key for every API (pool)."""
    templates = json.loads(cli_discovery.TEMPLATES.read_text(encoding='utf-8'))
    pool.mkdir(exist_ok=True)
    for name, template in templates.items():
        (pool/(name+'.json')).write_text(json.dumps(cli_discovery.deployment(template, __file__)), encoding='utf-8')
    for name, model in CODEX_DIRECT.items():
        install(pool, name, 'cli://codex', model)
    return pool


def class_of(name):
    """The route's class; None for a route with no class."""
    return (klass(native_catalog.select(name)) or {}).get('class')


def reaches(name, floor):
    return bool(class_of(name)) and ORDER.index(class_of(name)) >= floor


def ranked_names(role):
    chosen, selection = native_routes.ranked(role)
    names = {c['selected_deployment']: c['catalog'] for c in selection['candidates'] if c.get('eligible')}
    return [names[d['id']] for d in chosen]


def cost_of(name):
    row = next(r for r in native_routes.configured()['deployments'] if r.get('native_catalog', r['id']) == name)
    return native_routes.COST[row['cost_tier']]


def test_the_tiers_are_nested_floors_holding_what_reaches_them_in_the_role_tables_order(everything):
    for role in ('implementer', 'coordinator'):
        tiers = turn_engine.implementer_tiers(role)
        ranked = ranked_names(role)
        assert len(tiers) == len(ORDER)
        # Tier 0 holds every route, the fleet's default model leading; the others hold those whose class reaches.
        assert tiers[0][0] == 'local-qwen' and tiers[0][1:] == [n for n in ranked if n != 'local-qwen']
        for floor in (1, 2):
            assert tiers[floor] == [n for n in ranked if reaches(n, floor)]
        assert set(tiers[2]) < set(tiers[1]) < set(tiers[0])
    assert {'codex-native-gpt-6-1-sol', 'codex-cli-gpt-6-1-sol', 'claude-opus-5-5-cli', 'agy-gemini-4-argon-cli'} <= \
        set(tiers[1])
    assert {'codex-native', 'codex-cli', 'claude-fable-5-1-cli'} <= set(tiers[2])
    assert 'claude-sonnet-5-5-cli' in tiers[1] and class_of('claude-sonnet-5-5-cli') == 'middling'
    assert 'agy-cli' in tiers[0] and 'agy-cli' not in tiers[1]  # cheap: reaches only the lowest floor


def test_a_route_with_no_class_is_the_last_resort_of_tier_0_and_in_no_higher_tier(everything):
    for role in ('implementer', 'coordinator'):
        tiers = turn_engine.implementer_tiers(role)
        none = [n for n in tiers[0] if class_of(n) is None and n != 'local-qwen']  # no reading here: no class
        assert {'openrouter-mimo-v2-6-pro', 'openrouter-glm-5-3'} <= set(none)
        assert 'codex-cli-gpt-5-5' not in tiers[0]  # from before the named lines: an older model, no candidate
        assert tiers[0][-len(none):] == none  # after every route that has a class, a pay-per-token one included
        assert not set(none) & set(tiers[1])
        assert [cost_of(n) for n in none] == sorted(cost_of(n) for n in none)  # in the same cost order
        # ranked() (a Work run's automatic implementer) is tier 0's order: the fleet's default model, then GPT-5.5
        # (no class) is not the first choice of a machine that has it.
        ranked = ranked_names(role)
        assert ranked == tiers[0] and ranked[0] == 'local-qwen' and class_of(ranked[1]) == 'cheap'


def test_a_subscription_of_a_higher_class_precedes_a_pay_per_token_route_of_the_floors_class(everything, monkeypatch):
    # Anchors place DeepSeek (cheap) and GLM (middling), both pay-per-token on OpenRouter.
    saved = twice(other('deepseek', 'DeepSeek V4.1 Flash', 10, permaslug='deepseek/deepseek-v4.1-flash-1017-20261017'),
                  other('z-ai', 'GLM 5.3', 55, permaslug='z-ai/glm-5.3-20261020'))
    monkeypatch.setattr(signals, 'reading', lambda: saved)
    cheap, middling = 'openrouter-deepseek-v4-1-flash-1017', 'openrouter-glm-5-3'
    assert (class_of(cheap), class_of(middling)) == ('cheap', 'middling')
    assert class_of('bedrock-glm-5-3') == 'middling'  # the same model under Bedrock's spelling (native_measured_as)
    for role in ('implementer', 'coordinator'):
        tiers = turn_engine.implementer_tiers(role)
        for subscription in ('claude-sonnet-5-5-cli', 'claude-opus-5-5-cli', 'claude-fable-5-1-cli'):
            assert tiers[0].index(subscription) < tiers[0].index(cheap) < tiers[0].index(middling)
        assert tiers[0].index(middling) < tiers[0].index('openrouter-mimo-v2-6-pro')  # a class before none, metered or not
        assert middling in tiers[1] and cheap not in tiers[1] and middling not in tiers[2]  # placed by its class
        assert tiers[1].index('claude-opus-5-5-cli') < tiers[1].index(middling) < tiers[1].index('claude-api-fable-5-1')


def test_no_route_of_the_removed_list_is_lost(everything, tmp_path, monkeypatch):
    # A Codex that lists no Luna newer than 6: its GPT-6 Luna is current, as when the list was written.
    home = tmp_path/'codex-home'
    home.mkdir()
    (home/'models_cache.json').write_text(json.dumps({'models': [
        {'slug': slug, 'visibility': 'list'} for slug in ('gpt-6-luna', 'gpt-6.1-sol', 'gpt-6-astra')]}))
    monkeypatch.setenv('CODEX_HOME', str(home))
    names = set(turn_engine.implementer_tiers()[0])
    assert set(OLD) - {'local-default'} <= names and 'local-qwen' in names  # local-qwen is the fleet's default here


def test_the_fleets_default_leads_tier_0_whether_or_not_a_row_names_it(everything, monkeypatch):
    assert turn_engine.implementer_tiers()[0][0] == 'local-qwen'
    monkeypatch.setattr(native_catalog, 'local_default', lambda: 'swift27b')  # no row of the table is this model
    tiers = turn_engine.implementer_tiers()
    assert tiers[0][0] == native_catalog.LOCAL_DEFAULT and 'local-qwen' in tiers[0][1:]
    assert native_catalog.LOCAL_DEFAULT not in tiers[1] + tiers[2]


def test_a_proven_newer_version_joins_its_floors_with_no_list_edited(everything):
    discovered(everything)  # gpt-6.2-sol on the Codex CLI: a derived row, pin only until it answers once here
    assert SOL not in turn_engine.implementer_tiers()[0]
    environment.save({'proofs': {SOL: {'ok': True}}})
    for role in ('implementer', 'coordinator'):
        tiers = turn_engine.implementer_tiers(role)
        assert SOL in tiers[0] and SOL in tiers[1]
        assert 'codex-cli-gpt-6-1-sol' in tiers[0] and 'codex-cli-gpt-6-1-sol' not in tiers[1]  # one class lower now


def test_the_coordinator_and_the_implementer_each_follow_their_own_ticks(everything):
    route = 'opencode-sub-kimi-k3'  # a route with no line: its id names it in the routing profile
    environment.save({'routing_profile': {'off': {'coordinator': [route]}}})
    assert route not in turn_engine.implementer_tiers('coordinator')[0]
    assert route in turn_engine.implementer_tiers('implementer')[0]
    environment.save({'routing_profile': {'off': {'implementer': [route]}}})  # ticked only for the coordinator
    assert route in turn_engine.implementer_tiers('coordinator')[0]
    assert route not in turn_engine.implementer_tiers('implementer')[0]
    # The matrix's Coordinator row comes from the same table rows.
    assert dict((n, now) for n, _, now in environment.auto_routes('coordinator'))['codex-cli-gpt-6-1-sol'] == 'usable'


def test_a_saved_implementer_list_still_decides(everything):
    saved = [['claude-sonnet-5-5-cli'], ['codex-cli']]
    turn_engine.save_policy({'implementer': saved})
    assert turn_engine.implementer_tiers() == saved == turn_engine.implementer_tiers('coordinator')
    turn_engine.save_policy({'implementer': [[]], 'planner': [['openai-api']]})  # the Order tab saves an empty box
    assert 'implementer' not in turn_engine.policy() and len(turn_engine.implementer_tiers()) == len(ORDER)


def test_a_failure_raises_the_floor_one_tier(everything):
    auto = {'native_auto_routes': ['tare-auto']}
    first = turn_engine.resolve_auto(auto)['native_auto_selection']
    assert first['tier'] == 0  # the floor of a start: whatever is available, the cheapest sufficient route
    moved = turn_engine.resolve_auto(auto, 1)['native_auto_selection']  # after tier 0: at least middling
    assert moved['tier'] == 1 and reaches(moved['route'], 1)
    moved = turn_engine.resolve_auto(auto, 2)['native_auto_selection']  # at least frontier
    assert moved['tier'] == 2 and class_of(moved['route']) == 'frontier'


def test_an_emptied_floor_keeps_its_index(everything):
    tiers = turn_engine.implementer_tiers()
    off = []
    for name in (n for n in tiers[1] if n not in tiers[2]):  # the middling routes: the frontier ones stay
        key = environment.route_key(native_catalog.select(name))
        off.append(environment.key_label(key) if key[0] else name)
    environment.save({'routing_profile': {'off': {'implementer': off}}})
    tiers = turn_engine.implementer_tiers()
    assert len(tiers) == len(ORDER) and tiers[0] and tiers[1] == tiers[2]
    assert all(class_of(n) == 'frontier' for n in tiers[1])
    moved = turn_engine.resolve_auto({'native_auto_routes': ['tare-auto']}, 1)['native_auto_selection']
    # asked from the middling floor, the route found is a frontier one: it is recorded at the floor it reaches
    assert moved['tier'] == 2 and class_of(moved['route']) == 'frontier'


def test_a_failure_moves_above_the_route_that_failed_never_to_it_again(monkeypatch):
    """Nested floors: Sonnet is in the first floor and in the middling one. Found at the first, it is recorded at the
    floor it reaches, so tier + 1 is the frontier floor and not Sonnet once more."""
    from relay import turn_engine
    tiers = [['flash', 'sonnet', 'fable', 'unmeasured'], ['sonnet', 'fable'], ['fable']]
    up = {'sonnet', 'fable', 'unmeasured'}
    monkeypatch.setattr(turn_engine, 'available', lambda name, probe=True: ({'id': name}, None) if name in up else (None, 'out'))
    monkeypatch.setattr('relay.execution_scope.refusal', lambda role, deployment, name: None)
    monkeypatch.setattr(turn_engine, 'surplus_first', lambda names: list(names))
    tier, name, _, _ = turn_engine.choose(tiers)
    assert (tier, name) == (1, 'sonnet')  # flash is out; Sonnet reaches the middling floor
    assert turn_engine.choose(tiers, tier + 1)[:2] == (2, 'fable')
    up.discard('sonnet'), up.discard('fable')
    assert turn_engine.choose(tiers)[:2] == (0, 'unmeasured') and turn_engine.choose(tiers, 1)[1] is None
    # an owner's saved list has each route in one tier: the tier found is the tier returned
    up.update({'sonnet', 'fable'})
    assert turn_engine.choose([['flash'], ['sonnet'], ['fable']])[:2] == (1, 'sonnet')


def test_the_copilot_gives_each_route_once_at_the_floor_it_reaches(monkeypatch):
    from relay import copilot_mode, turn_engine
    tiers = [['flash', 'sonnet', 'fable'], ['sonnet', 'fable'], ['fable']]
    chat = {'native_profile': {'protocol': 'openai-chat'}}
    monkeypatch.setattr(turn_engine, 'implementer_tiers', lambda role: tiers)
    monkeypatch.setattr(turn_engine, 'available', lambda name: ({'id': name, **chat}, None))
    monkeypatch.setattr('relay.execution_scope.refusal', lambda role, deployment, name: None)
    assert [(t, n) for t, n, _ in copilot_mode.routes()] == [(0, 'flash'), (1, 'sonnet'), (2, 'fable')]
    # Sonnet, found at the first floor, is recorded at 1: after it fails the next start is 2, never Sonnet again
    assert [(t, n) for t, n, _ in copilot_mode.routes(1 + 1)] == [(2, 'fable')]
