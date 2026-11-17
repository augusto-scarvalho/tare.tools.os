"""An API route is left out while its account's known balance is spent (owner, 2026-11-16: the balance is a guard)."""
import time

import pytest

from relay import provider_quota, provider_spend, turn_engine

DAY = 86400
API = {'id': 'api', 'native_credential_env': 'OPENAI_API_KEY',
       'native_profile': {'endpoint': 'https://api.openai.com', 'model': 'gpt-6', 'protocol': 'openai-responses'}}
OPENROUTER = {'id': 'or', 'native_credential_env': 'OPENROUTER_API_KEY',
              'native_profile': {'endpoint': 'https://openrouter.ai/api', 'model': 'x/y', 'protocol': 'openai-chat'}}


@pytest.fixture(autouse=True)
def storage(tmp_path, monkeypatch):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path))
    return tmp_path


def spend(deployment, usd, at):
    assert provider_spend.record(deployment, [{'call_id': str(at), 'cost': {'currency': 'USD', 'known_amount': usd}}], at) == 1


def reading(remaining, at):
    from relay.relay_io import write_json_atomic
    path = provider_quota.cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, {'schema': provider_quota.SCHEMA, 'observed_at': at, 'accounts': [
        {'account': 'openrouter', 'name': 'OpenRouter', 'kind': 'credit', 'status': 'OBSERVED', 'observed_at': at,
         'credit': {'currency': 'USD', 'total': 20, 'remaining': remaining}}]})


def test_no_known_balance_leaves_nothing_out():
    now = time.time()
    assert turn_engine.usage_limited(API, now) is None  # unknown is not zero
    assert turn_engine.usage_limited(OPENROUTER, now) is None  # no reading at all


def test_a_stated_balance_counted_down_to_zero_leaves_the_route_out_until_a_new_one_is_stated():
    now = time.time()
    provider_spend.declare('openai-api', 10, now=now - DAY)
    spend(API, 9.5, now - 3600)
    assert turn_engine.usage_limited(API, now) is None  # something is left
    spend(API, 0.5, now - 60)
    assert turn_engine.usage_limited(API, now) == now + turn_engine.USAGE_LIMIT_DEFAULT_SECONDS
    assert turn_engine.usage_limited(OPENROUTER, now) is None  # another account
    line = provider_quota.render({'observed_at': now, 'accounts': [{'account': 'openai-api', 'name': 'OpenAI API',
        'kind': 'credit', 'status': 'DECLARED', 'observed_at': now, 'credit': provider_spend.remaining('openai-api', now)}]},
        now).splitlines()[1]
    assert line.startswith('  OpenAI API: none left of US$ 10.00') and line.endswith(' · its routes are left out while nothing is left')
    provider_spend.declare('openai-api', 5, now=now)
    assert turn_engine.usage_limited(API, now + 1) is None  # lifted at once


def test_a_spent_credit_that_renews_is_left_out_until_it_renews_and_a_lapsed_one_is_unknown_again():
    now = time.time()
    provider_spend.declare('openai-api', 10, now=now - DAY, expires=now + 2*DAY)
    spend(API, 10, now - 60)
    assert turn_engine.usage_limited(API, now) == pytest.approx(now + 2*DAY)
    assert turn_engine.usage_limited(API, now + 3*DAY) is None  # the credit lapsed: no known balance


def test_openrouters_own_reading_at_zero_counts_only_while_it_is_fresh():
    now = time.time()
    reading(0, now - 60)
    assert turn_engine.usage_limited(OPENROUTER, now) == now + turn_engine.USAGE_LIMIT_DEFAULT_SECONDS
    assert turn_engine.usage_limited(API, now) is None  # OpenRouter's reading says nothing of another account
    reading(0, now - provider_quota.FRESH - 60)
    assert turn_engine.usage_limited(OPENROUTER, now) is None  # an old reading: it may have been topped up since
    reading(3.2, now - 60)
    assert turn_engine.usage_limited(OPENROUTER, now) is None
