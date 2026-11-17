"""Use first the quota and the API credit that lapse: the renewing declared credit, the one signal, the order it gives."""
import copy
import time
from datetime import datetime, timezone

import pytest

from relay import execution_scope, native_catalog, native_routes, provider_quota, provider_spend, turn_engine

DAY = 86400


def ts(year, month, day, hour=0):
    return datetime(year, month, day, hour, tzinfo=timezone.utc).timestamp()


NOW = time.time()  # the ranking reads the real clock
API_KEY = {'id': 'api', 'native_credential_env': 'ANTHROPIC_API_KEY',
           'native_profile': {'endpoint': 'https://api.anthropic.com', 'model': 'fable-5-2', 'protocol': 'anthropic-messages'}}


def cli(name='claude', model='fable-5-2'):
    return {'id': name + '-' + model, 'native_profile': {'endpoint': 'cli://' + name, 'model': model, 'protocol': 'vendor-cli'}}


CODEX = {'id': 'codex-direct', 'native_profile': {'endpoint': 'https://chatgpt.com', 'model': 'gpt-6',
                                                  'protocol': 'openai-codex-responses'}}


@pytest.fixture(autouse=True)
def storage(tmp_path, monkeypatch):
    global NOW  # each test's own start: in a whole-suite run the import is minutes (or over an hour) before the test,
    NOW = time.time()  # and a reading stamped then is stale to the 60 s guard and to the one-hour limit
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path))
    return tmp_path


def bucket(window='week', remaining=0.23, resets_in=DAY, scope=None, id=None):
    return {'id': id or window, 'label': window + ', all models', 'window': window, 'scope': scope,
            'remaining': remaining, 'resets_at': NOW + resets_in}


def reading(account='claude', *buckets):
    return {'account': account, 'name': account, 'kind': 'subscription', 'status': 'OBSERVED', 'observed_at': NOW,
            'buckets': list(buckets)}


def save(*accounts):
    from relay.relay_io import write_json_atomic
    path = provider_quota.cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, {'schema': provider_quota.SCHEMA, 'observed_at': NOW, 'accounts': list(accounts)})


def spend(usd, at):
    assert provider_spend.record(API_KEY, [{'call_id': str(at), 'cost': {'currency': 'USD', 'known_amount': usd}}], at) == 1


@pytest.mark.parametrize('day,now,expected', [
    (14, ts(2026, 11, 10), (ts(2026, 10, 14), ts(2026, 11, 14))),   # before the day: the cycle began last month
    (14, ts(2026, 11, 14), (ts(2026, 11, 14), ts(2026, 12, 14))),   # on the day it renews
    (14, ts(2026, 11, 20), (ts(2026, 11, 14), ts(2026, 12, 14))),   # after the day
    (31, ts(2026, 11, 15), (ts(2026, 10, 31), ts(2026, 11, 30))),   # 31 in a 30-day month is its last day
    (31, ts(2026, 2, 10), (ts(2026, 1, 31), ts(2026, 2, 28))),      # and in February
    (31, ts(2028, 2, 29, 5), (ts(2028, 2, 29), ts(2028, 3, 31))),   # a leap year
    (14, ts(2026, 12, 20), (ts(2026, 12, 14), ts(2027, 1, 14))),    # December into January
    (14, ts(2027, 1, 3), (ts(2026, 12, 14), ts(2027, 1, 14))),      # and back
])
def test_cycle_is_utc_with_the_day_clamped_to_the_month(day, now, expected):
    assert provider_spend.cycle(day, now) == expected
    assert expected[0] <= now < expected[1]


def test_renewing_balance_is_the_stated_amount_then_the_grant_with_nothing_carried():
    provider_spend.declare('anthropic-api', 80, now=ts(2026, 11, 20), renews=(14, 100))
    spend(5, ts(2026, 11, 25))
    before = provider_spend.remaining('anthropic-api', ts(2026, 12, 1))
    assert (before['remaining'], before['total'], before['grant'], before['renews_at']) == (75, 100, 100, ts(2026, 12, 14))
    assert before['cycle_start'] == ts(2026, 11, 14)
    spend(10, ts(2026, 12, 15))
    after = provider_spend.remaining('anthropic-api', ts(2026, 12, 20))
    assert after['remaining'] == 90 and after['renews_at'] == ts(2027, 1, 14)  # 100 less 10: neither the 80 nor the 5 carry
    assert provider_spend.remaining('anthropic-api', ts(2027, 1, 14))['remaining'] == 100


def test_balance_without_renews_is_what_it_was():
    provider_spend.declare('anthropic-api', 80, now=ts(2026, 11, 20))
    spend(5, ts(2026, 11, 25))
    result = provider_spend.remaining('anthropic-api', ts(2027, 3, 1))
    assert result == {'currency': 'USD', 'total': 80.0, 'declared_at': ts(2026, 11, 20), 'remaining': 75.0}


def test_an_invalid_renews_is_dropped_and_the_entry_reads_as_a_plain_balance(storage):
    import json
    (storage/'serve').mkdir()
    (storage/'serve'/'provider-balance.json').write_text(json.dumps({
        'anthropic-api': {'usd': 5, 'at': 1, 'renews': {'day': 32, 'usd': 100}},
        'openai-api': {'usd': 6, 'at': 1, 'renews': {'day': 3, 'usd': 9}}}))
    assert provider_spend.declared() == {'anthropic-api': {'usd': 5, 'at': 1}, 'openai-api': {'usd': 6, 'at': 1,
                                                                                            'renews': {'day': 3, 'usd': 9}}}
    with pytest.raises(ValueError):
        provider_spend.declare('anthropic-api', 5, renews=(0, 100))


def test_week_with_a_day_left_is_in_surplus():
    save(reading('claude', bucket('week', 0.23, DAY)))
    signal = provider_quota.surplus(cli(), NOW)
    assert signal['lead'] == pytest.approx(0.23 - 1/7) and round(signal['lead'], 3) == 0.087
    assert signal['allowance'] == 'claude week, all models' and signal['resets_at'] == NOW + DAY


def test_every_window_must_be_in_surplus_and_a_zero_one_ends_it():
    save(reading('claude', bucket('week', 0.9, 3*DAY), bucket('5 h', 0.1, 4*3600)))  # the 5 h window is behind its pace
    assert provider_quota.surplus(cli(), NOW) is None
    save(reading('claude', bucket('week', 0.9, 3*DAY), bucket('5 h', 0.9, 4*3600)))
    assert provider_quota.surplus(cli(), NOW)['allowance'].endswith('5 h, all models')  # the smaller lead
    save(reading('claude', bucket('week', 0.9, 3*DAY), bucket('5 h', 0.0, 4*3600)))
    assert provider_quota.surplus(cli(), NOW) is None


def test_a_window_already_reset_or_of_another_model_or_unplaced_is_ignored():
    save(reading('claude', bucket('week', 0.9, 3*DAY), bucket('5 h', 0.0, -60)))  # its reading says nothing of the new window
    assert provider_quota.surplus(cli(), NOW)['lead'] > 0
    save(reading('claude', bucket('week', 0.9, 3*DAY), bucket('week', 0.0, 3*DAY, scope='opus', id='week:opus')))
    assert provider_quota.surplus(cli(model='fable-5-2'), NOW)['lead'] > 0  # Opus's window does not limit Fable
    assert provider_quota.surplus(cli(model='opus-5'), NOW) is None
    save(reading('claude', bucket('session', 0.9, 3*DAY)))
    assert provider_quota.surplus(cli(), NOW) is None  # a window tare cannot place


def test_renewing_credit_half_spent_with_a_quarter_of_the_cycle_left():
    provider_spend.declare('anthropic-api', 100, now=ts(2026, 11, 14), renews=(14, 100))
    spend(50, ts(2026, 11, 20))
    signal = provider_quota.surplus(API_KEY, ts(2026, 12, 6, 12))
    assert (signal['remaining'], signal['time_left'], signal['lead']) == (0.5, 0.25, 0.25)
    assert provider_quota.surplus(API_KEY, ts(2026, 11, 25)) is None  # 63% of the cycle left: behind the pace
    spend(50, ts(2026, 12, 1))
    assert provider_quota.surplus(API_KEY, ts(2026, 12, 6, 12)) is None  # spent: nothing to use first


def test_a_stated_credit_that_lapses_on_a_day_is_used_first_while_ahead_of_the_pace_and_gone_after_it(monkeypatch):
    """A cloud's promotional credit (owner, 2026-11-16: go first to the credit that lapses soon): no renewal, an end."""
    bedrock = {'id': 'bedrock-x', 'native_credential_env': 'AWS_BEARER_TOKEN_BEDROCK', 'native_profile': {'model': 'x'}}
    start, end = ts(2026, 11, 1), ts(2027, 3, 1)  # 120 days
    provider_spend.declare('bedrock', 300, now=start, expires=end)
    assert provider_quota.surplus(bedrock, start + DAY) is None  # nothing lost yet: at the pace
    signal = provider_quota.surplus(bedrock, start + 60*DAY)
    assert (signal['remaining'], signal['time_left'], signal['resets_at'], signal['allowance']) == (1.0, 0.5, end, 'bedrock credit')
    assert provider_spend.record(bedrock, [{'call_id': 'c', 'cost': {'currency': 'USD', 'known_amount': 200}}], start + 30*DAY) == 1
    assert provider_quota.surplus(bedrock, start + 60*DAY) is None  # a third left with half the time: behind the pace
    credit = provider_spend.remaining('bedrock', start + 60*DAY)
    assert (credit['remaining'], credit['total'], credit.get('stated'), 'console' in credit) == (100, 300, True, False)
    line = provider_quota.render({'observed_at': start, 'accounts': [{'account': 'bedrock', 'name': 'Bedrock', 'kind': 'credit',
        'status': 'DECLARED', 'observed_at': start, 'credit': credit}]}, now=start + 60*DAY).splitlines()[1]
    assert line.startswith('  Bedrock: US$ 100.00 left of US$ 300.00, lapses Sun 28 Feb 2027, stated ') and 'surplus' not in line
    assert provider_spend.remaining('bedrock', end) is None and provider_quota.surplus(bedrock, end + 1) is None  # lapsed
    for wrong in ({'expires': start - 1}, {'expires': end, 'renews': (14, 100)}, {'expires': end, 'usd': 0}):
        with pytest.raises(ValueError):
            provider_spend.declare('bedrock', wrong.pop('usd', 300), now=start, **wrong)
    monkeypatch.setattr(provider_quota, 'refresh', lambda: {'observed_at': NOW, 'accounts': []})
    assert provider_quota.main(['--set', 'bedrock', '50', '--expires', '2999-12-31']) == 0
    for wrong in (['--renews', '14'], ['--expires', 'tomorrow']):
        with pytest.raises(SystemExit):
            provider_quota.main(['--set', 'bedrock', '50', '--expires', '2999-12-31', *wrong])
    window = provider_spend.declared()['bedrock']['window']
    assert window['end'] == ts(3000, 1, 1) and window['stated'] is True  # the whole stated day, UTC


def test_credit_without_renewal_openrouter_and_local_have_no_surplus():
    provider_spend.declare('anthropic-api', 100, now=ts(2026, 11, 14))
    assert provider_quota.surplus(API_KEY, ts(2026, 12, 6)) is None
    save(reading('openrouter'))
    assert provider_quota.surplus({'id': 'or', 'native_credential_env': 'OPENROUTER_API_KEY'}, NOW) is None
    assert provider_quota.surplus({'id': 'local', 'native_profile': {'endpoint': 'http://127.0.0.1:1', 'model': 'q'}}, NOW) is None


@pytest.fixture
def routes(monkeypatch):
    """Route rows {id: (deployment, cost tier)} that admission lets through, so only the ranking is under test."""
    rows = {}

    def put(deployment, cost_tier):
        rows[deployment['id']] = (deployment, cost_tier)
    monkeypatch.setattr(native_routes, 'configured', lambda: {'schema_version': 1, 'deployments': [
        {'id': name, 'cost_tier': tier, 'kind': 'primary'} for name, (_, tier) in rows.items()]})
    monkeypatch.setattr(native_routes, 'admission', lambda row, role, author=None, credentials=None: (
        copy.deepcopy(rows[row['id']][0]), None, None))
    put.rows = rows
    return put


def order(role='implementer'):
    deployments, selection = native_routes.ranked(role)
    return [d['id'] for d in deployments], selection


CHEAP = {'id': 'cheap', 'native_profile': {'endpoint': 'http://127.0.0.1:1', 'model': 'q', 'protocol': 'openai-chat'}}


def test_a_route_in_surplus_goes_before_a_cheaper_tier_and_the_sooner_lapse_first(routes):
    routes(CHEAP, 'free'); routes(cli(), 'metered'); routes(CODEX, 'metered')
    assert order()[0] == ['cheap', 'claude-fable-5-2', 'codex-direct']  # no reading: cost then table order
    save(reading('claude', bucket('week', 0.23, DAY)))
    names, selection = order()
    assert names == ['claude-fable-5-2', 'cheap', 'codex-direct']
    assert selection['expiring_first'] is True and selection['reason'] == 'AUTO_COST_AND_CONFIGURED_ORDER'
    seen = {c['deployment']: c for c in selection['candidates']}
    assert seen['claude-fable-5-2']['expiring']['allowance'] == 'claude week, all models' and 'expiring' not in seen['cheap']
    save(reading('claude', bucket('week', 0.23, DAY)), reading('codex', bucket('week', 0.9, 3*DAY, id='codex:week')))
    # Claude's week ends in a day, Codex's in three: what is lost first is used first, though Codex's lead is larger
    # (0.47 against 0.09). The owner's Claude week ended unused that way while a credit a week from lapsing was spent.
    assert order()[0] == ['claude-fable-5-2', 'codex-direct', 'cheap']
    save(reading('claude', bucket('week', 0.23, DAY)), reading('codex', bucket('week', 0.9, DAY, id='codex:week')))
    assert order()[0] == ['codex-direct', 'claude-fable-5-2', 'cheap']  # lapsing together: the larger lead first


def test_no_surplus_anywhere_leaves_the_order_and_the_selection_as_they_were(routes):
    routes(CHEAP, 'free'); routes(cli(), 'metered'); routes(CODEX, 'flat')
    plain = order()
    save(reading('claude', bucket('week', 0.1, 6*DAY)), reading('codex', bucket('week', 0.05, 6*DAY, id='codex:week')))
    assert order() == plain and 'expiring_first' not in plain[1]
    assert not any('expiring' in c for c in plain[1]['candidates'])


def test_prefer_subscription_still_decides_before_a_surplus(routes, monkeypatch):
    routes(cli(), 'metered'); routes(API_KEY, 'metered')
    monkeypatch.setattr(execution_scope, 'current', lambda: execution_scope.Scope(prefer='subscription'))
    tomorrow = datetime.fromtimestamp(time.time() + DAY, timezone.utc).day  # a credit renewing within a day, all left
    provider_spend.declare('anthropic-api', 100, renews=(tomorrow, 100))
    assert order('planner')[0] == ['claude-fable-5-2', 'api']  # the credit is in surplus; the preference comes first
    monkeypatch.setattr(execution_scope, 'current', lambda: execution_scope.Scope())
    assert order('planner')[0] == ['api', 'claude-fable-5-2']


def test_tare_auto_tries_the_route_in_surplus_first_inside_its_tier_and_never_changes_the_tier(monkeypatch):
    found = {d['id']: d for d in (cli('claude', 'a'), cli('codex', 'b'), cli('agy', 'c'))}
    monkeypatch.setattr(native_catalog, 'select', lambda name: found[name])
    monkeypatch.setattr(turn_engine, 'available', lambda name, probe=True: (found[name], None))
    tiers = [['claude-a', 'codex-b'], ['agy-c']]
    assert turn_engine.choose(tiers)[:2] == (0, 'claude-a')
    save(reading('codex', bucket('week', 0.99, DAY, id='codex:week')))
    assert turn_engine.choose(tiers)[:2] == (0, 'codex-b')  # first inside its tier
    save(reading('agy', bucket('week', 0.99, DAY)))
    assert turn_engine.choose(tiers)[:2] == (0, 'claude-a')  # a surplus in the second tier does not move the first
    assert turn_engine.choose(tiers, start=1)[:2] == (1, 'agy-c')


# A unit a conversation delegates (work_units.routes), owner 2026-11-14: any account's route; --prefer_* first, then the
# allowance that lapses, then tare's implementer ladder; --*_only, the routing profile and pins limit it.

OPENROUTER = {'id': 'openrouter-deepseek', 'native_credential_env': 'OPENROUTER_API_KEY',
              'native_profile': {'endpoint': 'https://openrouter.ai/api/v1', 'model': 'deepseek/v4', 'protocol': 'openai-chat'}}
MUSE = {'id': 'openrouter-muse', 'native_credential_env': 'OPENROUTER_API_KEY',
        'native_profile': {'endpoint': 'https://openrouter.ai/api/v1', 'model': 'meta/muse', 'protocol': 'openai-chat'}}
LADDER = ['cheap', 'openrouter-deepseek', 'claude-fable-5-2', 'api']  # local, OpenRouter credit, Claude plan, Claude API


@pytest.fixture
def unit(monkeypatch):
    """The real availability gate and scope over routes of four accounts; the catalog, the GPU probe and the keys are
    fixtures. Returns the names routes() yields for an OpenRouter coordinator's unit under a scope."""
    from relay import work_units
    found = {d['id']: d for d in (CHEAP, OPENROUTER, cli(), API_KEY, MUSE)}
    monkeypatch.setattr(native_catalog, 'select', lambda name: copy.deepcopy(found[name]))
    monkeypatch.setattr(turn_engine, 'policy', lambda: {'implementer': [['cheap'], ['openrouter-deepseek'], ['claude-fable-5-2', 'api']]})
    monkeypatch.setattr(turn_engine, 'local_gpu_busy', lambda deployment: None)  # the GPU is free
    monkeypatch.setattr(turn_engine, 'avoid_local_gpu', lambda: False)
    for key in ('OPENROUTER_API_KEY', 'ANTHROPIC_API_KEY'):
        monkeypatch.setenv(key, 'fixture')  # present, never used: nothing is called

    def names(scope=execution_scope.Scope(), **arguments):
        with execution_scope.activated(scope):
            return [name for name, _ in work_units.routes(copy.deepcopy(MUSE), **arguments)]
    return names


def lapsing_credit():
    tomorrow = datetime.fromtimestamp(time.time() + DAY, timezone.utc).day  # renews within a day, all of it left
    provider_spend.declare('anthropic-api', 100, renews=(tomorrow, 100))


def test_the_planner_takes_the_route_whose_allowance_lapses_first_among_those_it_may_use(monkeypatch):
    from relay import native_catalog, planning_tools
    astra = {'id': 'codex-cli', 'native_profile': {'endpoint': 'cli://codex', 'model': 'gpt-6-astra', 'protocol': 'vendor-cli'}}
    table = {'codex-cli': astra, 'claude-fable-5-2': cli()}
    monkeypatch.setattr(turn_engine, 'policy', lambda: {'planner': ['codex-cli', 'claude-fable-5-2', 'gone']})
    monkeypatch.setattr(native_catalog, 'select', lambda name: table[name])
    assert planning_tools.snapshot()['route_order'] == ['codex-cli', 'claude-fable-5-2']  # tare's order
    save(reading('claude', bucket('week', 0.3, DAY/12)), reading('codex', bucket('week', 0.06, 6*DAY, id='codex:week')))
    assert planning_tools.snapshot()['route_order'] == ['claude-fable-5-2', 'codex-cli']  # the week ending today


def test_a_unit_that_needs_a_class_goes_first_to_the_routes_that_reach_it(unit, monkeypatch):
    # Fable is a frontier line; the local model and the reseller's have no line and count as cheap.
    from relay import model_signals
    monkeypatch.setattr(model_signals, 'klass', lambda d: {'class': 'frontier'} if 'fable' in d['native_profile']['model'] else None)
    assert unit(needs='cheap') == LADDER  # every route reaches it: the order is the ladder's
    hard = unit(needs='frontier')
    assert hard == ['claude-fable-5-2', 'api', 'cheap', 'openrouter-deepseek']  # weaker ones stay, last
    assert unit(needs='middling')[0] == 'claude-fable-5-2'
    assert unit(chosen='cheap', needs='frontier') == ['cheap']  # the coordinator's own choice is not reordered
    # No route reaches the need: the nearest class goes first, not the plain order (the local model would lead).
    monkeypatch.setattr(model_signals, 'klass', lambda d: {'class': 'middling'} if 'fable' in d['native_profile']['model'] else None)
    assert unit(needs='frontier') == ['claude-fable-5-2', 'api', 'cheap', 'openrouter-deepseek']
    monkeypatch.setattr(model_signals, 'klass', lambda d: {'class': 'frontier'} if 'fable' in d['native_profile']['model'] else None)
    lapsing_credit()  # an allowance that lapses first still does not put a weaker model before the need
    save(reading('claude', bucket('week', 0.23, 6*DAY)))
    assert unit(needs='frontier')[-2:] == ['cheap', 'openrouter-deepseek']


def test_the_plans_task_may_say_the_class_it_needs_and_the_unit_takes_the_hardest():
    from relay import model_signals
    from relay.native_artifacts import GraphTask
    task = dict(key='a', title='t', summary='s', cluster='c', priority='P1', criteria=['x'], target_paths=['a.py'],
                depends_on=[])
    assert GraphTask(**task).needs is None and GraphTask(**task, needs='middling').needs == 'middling'
    with pytest.raises(ValueError):
        GraphTask(**task, needs='genius')
    assert max(['cheap', 'frontier', 'middling'], key=model_signals.ORDER.index) == 'frontier'
    assert model_signals.reaches(cli(model='claude-fable-5-2'), 'frontier')  # by its line, with no reading
    assert not model_signals.reaches(cli(model='claude-haiku-5-5'), 'middling') and not model_signals.reaches(CHEAP, 'middling')
    assert model_signals.reaches(CHEAP, 'cheap')
    from relay.conversation_tasks import delivery_view
    view = delivery_view({'verdict': 'INCONCLUSIVE', 'reason': 'r', 'needs': 'frontier', 'route_class': 'cheap'})
    assert view['class_note'] == 'The plan says this unit needs a frontier model; the route that ran is a cheap one.'
    assert 'class_note' not in delivery_view({'verdict': 'VERIFIED', 'reason': 'r', 'needs': 'cheap', 'route_class': 'middling'})


def test_a_hosted_conversations_unit_takes_the_whole_ladder_cheapest_first_with_nothing_in_surplus(unit):
    assert unit() == LADDER  # the local model first while its GPU is free
    assert unit(allow_parent=True) == [*LADDER, 'openrouter-muse']  # the conversation's own model after the ladder
    assert unit(chosen='claude-fable-5-2') == ['claude-fable-5-2']  # a route on another account, as the coordinator chose


def test_a_route_in_surplus_on_another_account_goes_first_the_sooner_lapse_first(unit):
    save(reading('claude', bucket('week', 0.23, DAY)))  # lead 0.09
    assert unit() == ['claude-fable-5-2', 'cheap', 'openrouter-deepseek', 'api']
    lapsing_credit()  # lead about 0.97
    assert unit() == ['api', 'claude-fable-5-2', 'cheap', 'openrouter-deepseek']


@pytest.mark.parametrize('only,expected', [('subscription', ['claude-fable-5-2']), ('api', ['api', 'openrouter-deepseek']),
                                           ('local', ['cheap'])])
def test_an_only_flag_keeps_the_unit_on_its_kind_also_when_another_kind_is_in_surplus(unit, only, expected):
    save(reading('claude', bucket('week', 0.23, DAY)))
    lapsing_credit()
    assert unit(execution_scope.Scope(only=only)) == expected
    assert unit(execution_scope.Scope(only=only), allow_parent=True) == expected + (['openrouter-muse'] if only == 'api' else [])


def test_a_prefer_flag_puts_its_kind_first_then_the_lapsing_allowance_then_the_ladder(unit):
    lapsing_credit()
    assert unit(execution_scope.Scope(prefer='subscription')) == ['claude-fable-5-2', 'api', 'cheap', 'openrouter-deepseek']
    save(reading('claude', bucket('week', 0.23, DAY)))
    assert unit(execution_scope.Scope(prefer='api')) == ['api', 'openrouter-deepseek', 'claude-fable-5-2', 'cheap']
    assert unit(execution_scope.Scope(prefer='local')) == ['cheap']  # it keeps execution roles on local models


def test_a_route_off_for_implementer_is_never_yielded_in_surplus_or_not(unit):
    from relay import environment
    environment.save({'routing_profile': {'off': {'implementer': ['api', 'cheap']}}})  # the routes' own ids
    assert unit() == ['openrouter-deepseek', 'claude-fable-5-2']
    lapsing_credit()
    assert unit() == ['openrouter-deepseek', 'claude-fable-5-2']
    refused = []
    assert unit(chosen='api', refused=refused) == [] and refused[0].startswith('SWITCHED_OFF_FOR_ROLE: api')


def test_claudes_session_window_counts_as_its_5_hours_and_a_lead_under_one_percent_is_none():
    # Found by the cross-vendor review: Claude names its 5 h window 'session', and the week alone promoted the route.
    save(reading('claude', bucket('week', 0.23, DAY), bucket('session', 0.10, 4 * 3600)))
    assert provider_quota.surplus(cli(), NOW) is None  # 10% of the session left with 80% of it to run
    save(reading('claude', bucket('week', 0.23, DAY), bucket('session', 0.90, 4 * 3600)))
    assert provider_quota.surplus(cli(), NOW)['allowance'].endswith('week, all models')
    save(reading('claude', bucket('week', 0.149, DAY)))  # a day is 14.3% of the week: a lead of 0.6 of a point
    assert provider_quota.surplus(cli(), NOW) is None


def test_a_reading_over_an_hour_old_promotes_nothing_and_is_not_marked():
    # 2026-11-14: the saved reading was two days old and still showed 23% of a week the day's work had used.
    save(reading('claude', bucket('week', 0.23, DAY)))
    assert provider_quota.surplus(cli(), NOW + 3000)['lead'] > 0
    assert provider_quota.surplus(cli(), NOW + 3700) is None
    report = {'observed_at': NOW, 'accounts': [{**reading('claude', bucket('week', 0.23, DAY)), 'name': 'Claude subscription'}]}
    assert 'in surplus' in provider_quota.render(report, NOW + 3000)
    assert 'in surplus' not in provider_quota.render(report, NOW + 3700)


def test_render_marks_what_is_used_first_and_says_the_credit_renews():
    now = ts(2026, 12, 6, 12)
    provider_spend.declare('anthropic-api', 100, now=ts(2026, 11, 14), renews=(14, 100))
    spend(50, ts(2026, 11, 20))
    credit = provider_spend.remaining('anthropic-api', now)
    report = {'observed_at': now, 'accounts': [
        {'account': 'claude', 'name': 'Claude subscription', 'kind': 'subscription', 'status': 'OBSERVED', 'observed_at': now,
         'buckets': [{**bucket('week', 0.23, DAY), 'resets_at': now + DAY}, {**bucket('5 h', 0.1, 3600), 'id': 'h',
                                                                              'label': '5 h', 'resets_at': now + 3600}]},
        {'account': 'anthropic-api', 'name': 'Claude API', 'kind': 'credit', 'status': 'DECLARED', 'observed_at': now,
         'credit': credit}]}
    lines = provider_quota.render(report, now).splitlines()
    week = next(line for line in lines if line.strip().startswith('week, all models'))
    assert '· in surplus: 23% left, 14% of the window left' in week
    assert 'in surplus' not in next(line for line in lines if line.strip().startswith('5 h'))
    assert next(line for line in lines if 'Claude API' in line).endswith(
        'US$ 50.00 left of US$ 100.00 a cycle, renews Mon 14 Dec · in surplus: 50% left, 25% of the cycle left')


def test_a_demand_in_the_tui_starts_one_quota_refresh_and_a_second_one_right_after_starts_none(tmp_path, monkeypatch):
    import asyncio
    from textual.widgets import TextArea
    from relay.native_console import WorkConsole
    from test_native_console import FakeBackend
    monkeypatch.setattr('relay.native_agent.session_root', lambda: tmp_path/'custody')
    monkeypatch.delenv('TARE_CLI_DISCOVERY', raising=False)
    calls = []

    def refresh():  # a stub: the real one asks the vendors' CLIs
        calls.append(1)
        save(reading('claude', bucket()))
    monkeypatch.setattr(provider_quota, 'refresh', refresh)

    async def demand(app, pilot, text):
        app.query_one('#prompt', TextArea).load_text(text)
        await pilot.click('#start'); await pilot.pause(0.3)  # the turn itself stays running until it is cancelled

    async def scenario(asks):
        (tmp_path/'project').mkdir(exist_ok=True)
        app = WorkConsole(tmp_path/'project', backend=FakeBackend())
        async with app.run_test(size=(130, 44)) as pilot:
            await pilot.pause()
            app.asks_providers = asks  # what the interactive entry sets
            await demand(app, pilot, 'First task')
            first = len(calls)
            await pilot.click('#cancel')
            for _ in range(30):
                if not app.busy:
                    break
                await pilot.pause(0.05)
            await demand(app, pilot, 'Second task')
            return first, len(calls)
    assert asyncio.run(scenario(False)) == (0, 0)  # tests and non-interactive entries never run the vendors' CLIs
    assert asyncio.run(scenario(True)) == (1, 1)  # one for the first demand; the reading is under a minute old for the second
    monkeypatch.setattr(provider_quota, 'refresh', lambda: calls.append(1))  # a reading that fails saves nothing:
    calls.clear(); provider_quota.cache_path().unlink()
    assert asyncio.run(scenario(True)) == (1, 1)  # the attempt itself counts for the minute
