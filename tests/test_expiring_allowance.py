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


def test_a_route_in_surplus_goes_before_a_cheaper_tier_and_the_larger_lead_first(routes):
    routes(CHEAP, 'free'); routes(cli(), 'metered'); routes(CODEX, 'metered')
    assert order()[0] == ['cheap', 'claude-fable-5-2', 'codex-direct']  # no reading: cost then table order
    save(reading('claude', bucket('week', 0.23, DAY)))
    names, selection = order()
    assert names == ['claude-fable-5-2', 'cheap', 'codex-direct']
    assert selection['expiring_first'] is True and selection['reason'] == 'AUTO_COST_AND_CONFIGURED_ORDER'
    seen = {c['deployment']: c for c in selection['candidates']}
    assert seen['claude-fable-5-2']['expiring']['allowance'] == 'claude week, all models' and 'expiring' not in seen['cheap']
    save(reading('claude', bucket('week', 0.23, DAY)), reading('codex', bucket('week', 0.9, 3*DAY, id='codex:week')))
    assert order()[0] == ['codex-direct', 'claude-fable-5-2', 'cheap']  # lead 0.47 before 0.09


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
    assert '· used first: 23% left, 14% of the window left' in week
    assert 'used first' not in next(line for line in lines if line.strip().startswith('5 h'))
    assert next(line for line in lines if 'Claude API' in line).endswith(
        'US$ 50.00 left of US$ 100.00 a cycle, renews Mon 14 Dec · used first: 50% left, 25% of the cycle left')
