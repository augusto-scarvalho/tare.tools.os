import copy
import json
from decimal import Decimal
from datetime import date

import pytest
from compute_plane.native_usage import normalize_usage, UsageContract
from relay import provider_prices as prices, delivery_costs


@pytest.fixture(autouse=True)
def quote_study_date(monkeypatch):
    # These arithmetic cases qualify the recorded September price study. A
    # wall-clock rollover must not expire the fixture or renew production facts.
    class StudyDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 9, 23)
    monkeypatch.setattr(delivery_costs, 'date', StudyDate)


def deployment(endpoint='cli://codex', model='gpt-6-luna', protocol='vendor-cli'):
    return {'id': 'test', 'native_profile': {'endpoint': endpoint, 'model': model, 'protocol': protocol},
            'native_cli': {'version': '0.154.0' if endpoint == 'cli://codex' else '1.2.7'}}


def entry(raw, contract='codex-cli-inclusive'):
    return {'attempts': [{'started_at': 10, 'ended_at': 1810}], 'usage': {'calls': [
        {'execution': 'possibly_executed', 'normalized': normalize_usage(raw, UsageContract(contract, 'fixture')),
         'cost': {'complete': False}}]}}


def value(d, e, quote=None):
    q = quote or prices.quote(d)
    result = delivery_costs.apply(e, d, {'profiles': {'test': q}})
    return result['usage']['calls'][0]['cost']


def test_codex_all_models_long_context_and_fast_pricing():
    for model in ('gpt-6-astra', 'gpt-6-sol', 'gpt-6-luna', 'gpt-5.6-sol', 'gpt-5.6-luna', 'gpt-5.6-terra', 'gpt-5.5'):
        q = prices.quote(deployment(model=model))
        assert q['mode'] == 'tokens' and q['rates']['cache_write_tokens'] == q['rates']['uncached_input_tokens']
        assert ('long_rates' in q) == (model != 'gpt-6-astra')
    d = deployment(); e = entry({'input_tokens': 300000, 'cached_input_tokens': 100000, 'output_tokens': 1000})
    assert Decimal(value(d, e)['known_amount']) == Decimal('.04275')
    d['native_profile']['parameters_json'] = '{"service_tier":"priority"}'
    assert Decimal(value(d, e)['known_amount']) == Decimal('.106875')
    raw = {'input_tokens': 100, 'cached_input_tokens': 40, 'cache_write_input_tokens': 60, 'output_tokens': 0}
    assert Decimal(value(deployment(), entry(raw))['known_amount']) == Decimal('.0000064')


def test_google_normalized_cache_is_priced_and_receipt_unchanged():
    d = deployment('cli://agy', 'gemini-3.8-flash-high')
    e = entry({'input_tokens': 1000, 'cache_read_tokens': 2000, 'output_tokens': 100,
               'thinking_tokens': 80}, 'agy-cli-exclusive')
    before = copy.deepcopy(e)
    assert Decimal(value(d, e)['known_amount']) == Decimal('.001275')
    assert e == before


@pytest.mark.parametrize('day,expired', [(23,False), (24,True)])
def test_recorded_quote_expiry_boundary_is_enforced_independent_of_wall_clock(monkeypatch, day, expired):
    class StudyDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 10, day)
    monkeypatch.setattr(delivery_costs, 'date', StudyDate)
    d = deployment()
    q = prices.quote(d)
    q['valid_until'] = '2026-10-23'
    e = entry({'input_tokens':100, 'cached_input_tokens':0, 'output_tokens':10})
    if expired:
        with pytest.raises(ValueError, match='expired'):
            value(d,e,q)
    else:
        assert value(d,e,q)['complete']


def test_local_and_subscription_allocation_count_active_session_time_once():
    d = deployment('http://local', 'local', 'openai-chat'); d['native_counter_protocol'] = 'slop-fleet-v1'
    e = entry({}); e['usage']['calls'].append(copy.deepcopy(e['usage']['calls'][0]))
    q = prices.quote(d)
    assert not value(d, e, q)['complete']
    q.update(usd_per_hour='2', source='Monthly cost / measured active hours')
    result = delivery_costs.apply(e, d, {'profiles': {'test': q}})
    assert [c['cost']['known_amount'] for c in result['usage']['calls']] == ['1', '0']
    q['profile_sha256'] = 'changed'
    with pytest.raises(ValueError, match='profile changed'):
        value(d, e, q)


def test_bedrock_geo_price_is_not_the_global_or_another_region_price():
    d = deployment('https://bedrock-runtime.us-east-1.amazonaws.com', 'us.anthropic.claude-opus-4-6-v1', 'bedrock-converse')
    q = prices.quote(d)
    assert q['rates'] == prices.rates('5.5', '.55', '6.875', '27.5')
    d['native_profile']['endpoint'] = 'https://bedrock-runtime.eu-west-1.amazonaws.com'
    assert prices.quote(d)['mode'] == 'unknown'


def test_aws_bulk_rates_require_exact_sku_region_units_and_unambiguous_schedule():
    data = {'products': {}, 'terms': {'OnDemand': {}}}
    for sku, field, region, feature, rate in [('input', 'Input tokens', 'us-east-1', 'On-demand Inference', '.001'),
        ('output', 'Output tokens', 'us-east-1', 'On-demand Inference', '.0032'),
        ('mantle', 'Output tokens', 'us-east-1', None, '.0001'),
        ('foreign', 'Input tokens', 'eu-west-1', 'On-demand Inference', '.0001')]:
        data['products'][sku] = {'attributes': {'model': 'GLM 5', 'inferenceType': field, 'regionCode': region, 'feature': feature}}
        data['terms']['OnDemand'][sku] = {'offer': {'priceDimensions': {'rate': {
            'unit': '1K tokens', 'beginRange': '0', 'endRange': 'Inf', 'pricePerUnit': {'USD': rate}}}}}
    rates, skus = prices.bedrock_rates(data, 'zai.glm-5', 'us-east-1')
    assert rates == prices.rates('1.000', None, None, '3.2000') and skus == ['input', 'output']
    data['products']['duplicate'] = data['products']['input']
    data['terms']['OnDemand']['duplicate'] = data['terms']['OnDemand']['input']
    assert prices.bedrock_rates(data, 'zai.glm-5', 'us-east-1')[0]['uncached_input_tokens'] is None


def test_charge_precedes_catalog_estimate_and_unknown_or_mismatch_never_zero():
    d = deployment('https://openrouter.ai/api/v1', 'm', 'openai-chat')
    e = entry({})
    e['usage']['calls'][0]['cost'] = delivery_costs.cost('.123', 'openrouter.usage.cost', 'provider_reported')
    assert value(d, e)['known_amount'] == '.123'
    e['usage']['calls'][0]['observation'] = {'model_identity': 'mismatch'}
    assert not value(d, e)['complete']
    d = deployment(); q = prices.quote(d); q['valid_until'] = '2000-01-01'
    with pytest.raises(ValueError, match='expired'):
        value(d, entry({}), q)


def test_claude_estimate_available_only_in_explicit_cost_snapshot():
    d = deployment('cli://claude', 'claude-opus-5')
    e = entry({}); e['usage']['calls'][0]['provider_estimate'] = {
        'currency': 'USD', 'amount': '.123', 'source': 'claude.result.total_cost_usd'}
    assert value(d, e)['basis'] == 'client_estimate'
    assert delivery_costs.apply(e, d, None) == e


def test_live_price_fetch_is_shared_and_no_generation(monkeypatch):
    seen = []
    def fetch(url, **kwargs):
        seen.append(url)
        return {'status': 'OBSERVED', 'source': url, 'observed_at': 1790164800, 'expires_at': 1790168400,
                'value': {'data': [{'id': 'm', 'pricing': {'prompt': '.000001', 'completion': '.000002'}}]}}
    monkeypatch.setattr(prices, 'read_json_observation', fetch)
    cache = {}
    for _ in range(2):
        q = prices.quote(deployment('https://openrouter.ai/api/v1', 'm', 'openai-chat'), live=True, cache=cache)
        assert q['rates']['uncached_input_tokens'] == '1.000000'
    assert seen == [prices.OPENROUTER]


def test_snapshot_deduplicates_aliases_by_recorded_deployment(monkeypatch):
    from relay import native_catalog
    d = deployment()
    monkeypatch.setattr(native_catalog, 'select', lambda name: d)
    result = prices.snapshot(['alias-one', 'alias-two'])
    assert list(result['profiles']) == ['test']
    assert result['profiles']['test']['aliases'] == ['alias-one', 'alias-two']
