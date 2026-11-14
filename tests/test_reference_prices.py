"""models.dev reference prices: the daily reading, matching a route to its entry, and every price consumer."""
import json
import time
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest

from relay import provider_prices as prices
from relay.native_catalog import select
from relay.native_spend import settings
from test_native_agent import Client, deployment as agent_deployment, setup as agent_setup, steps  # noqa: F401

# An excerpt of the real file (MIT, attribution inside): whole entries for a few models of five providers.
FIXTURE = Path(__file__).resolve().parent/'fixtures'/'models_dev_api_20261114.json'
NOW = 1794628000.0  # 2026-11-14 UTC
POLICY = {'max_amount': '1', 'currency': 'USD'}


def models_dev():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


@pytest.fixture
def keys(monkeypatch):
    """An API key is set on this machine (never a real one: the reading only asks whether one is)."""
    monkeypatch.setattr('relay.provider_quota.secret', lambda name: 'key' if name == 'GEMINI_API_KEY' else None)


@pytest.fixture
def reading(keys):
    return prices.refresh_reading(NOW, fetch=models_dev)


def route(model, endpoint='https://api.anthropic.com/v1', protocol='anthropic-messages', window=131072, **parameters):
    return {'id': 'fixture', 'native_profile': {'protocol': protocol, 'model': model, 'endpoint': endpoint,
            'context_window': window, 'output_tokens': 1024, 'parameters_json': json.dumps(parameters)},
            'native_usage_contract': {'layout': 'anthropic-exclusive', 'source': 'fixture'}}


def rates(deployment):
    _, tariff = settings(deployment)
    return tariff and json.loads(tariff.rates_json)


def test_reading_keeps_priced_models_of_the_route_providers_with_the_day(reading):
    assert reading['read_at'] == NOW and prices.reading()['models'] == reading['models']
    assert set(reading['models']) == {'anthropic/claude-haiku-5-5', 'anthropic/claude-fable-5-2', 'openai/gpt-6.1-terra',
        'google/gemini-4-carbon', 'google/gemini-4-hydrogen', 'amazon-bedrock/us.anthropic.claude-fable-5-2',
        'amazon-bedrock/global.anthropic.claude-fable-5-2', 'amazon-bedrock/zai.glm-5'}  # no OpenRouter, no _source
    assert {m['since'] for m in reading['models'].values()} == {'2026-11-14'}


def test_first_party_api_route_takes_its_models_reference_price_labelled_everywhere(reading):
    from relay import native_profiles, route_facts
    from relay.environment import cost_line
    deployment = select('claude-api-fable-5-2')
    _, tariff = settings(deployment)
    assert json.loads(tariff.rates_json) == {'uncached_input_tokens': '8', 'output_tokens': '40',
                                             'cache_read_tokens': '0.2', 'cache_write_tokens': '10'}
    assert prices.is_reference({'source': tariff.source}) and 'not a confirmed tariff' in tariff.source
    assert tariff.source.endswith('anthropic/claude-fable-5-2, https://models.dev/api.json')
    assert tariff.effective_at == '2026-11-14'
    quote = prices.quote(deployment)
    assert quote['mode'] == 'tokens' and quote['basis'] == 'reference' and quote['source'] == tariff.source
    assert 'not a confirmed tariff' in quote['scope'] and 'only as exact as it' in quote['scope']
    assert native_profiles.describe('claude-api-fable-5-2')['cost_accounting'] == 'reference_price'
    facts = route_facts.collect(deployment)['price']
    assert facts['status'] == 'REFERENCE_PRICE' and 'read 2026-11-14' in facts['scope']
    assert cost_line(deployment).endswith('(at a models.dev reference price read 2026-11-14, not a confirmed tariff)')


def test_own_tariff_comes_first(reading):
    from relay import route_facts
    own = {'currency': 'USD', 'source': 'operator contract', 'effective_at': '2026-11-01', 'rates_json': json.dumps(
        {'uncached_input_tokens': '1', 'cache_read_tokens': '1', 'cache_write_tokens': '1', 'output_tokens': '1'})}
    deployment = {**route('claude-fable-5-2'), 'native_usage_tariff': own}
    assert settings(deployment)[1].source == 'operator contract'
    assert prices.quote(deployment)['source'] == 'operator contract'
    assert route_facts.collect({**select('claude-api-fable-5-2'), 'native_usage_tariff': own})['price']['status'] \
        == 'CONFIGURED_TARIFF'


def test_a_model_the_reading_lacks_is_unknown_and_refused_under_a_cap_as_before(reading):
    from relay.challenger_variants import price
    missing = route('claude-fable-5-3')
    assert rates(missing) is None and prices.quote(missing)['mode'] == 'unknown'
    with pytest.raises(ValueError, match='spend_limit_requires_matching_tariff_and_usage_contract'):
        settings({**missing, 'native_spend_policy': POLICY})
    with pytest.raises(ValueError, match='unavailable or conditional'):
        price({**missing, 'native_profile': {**missing['native_profile']}})


@pytest.mark.parametrize('model,endpoint,protocol', [
    ('claude-fable-5-1', 'https://api.anthropic.com/v1', 'anthropic-messages'),  # an older version of a listed line
    ('claude-opus-4.6', 'https://api.anthropic.com/v1', 'anthropic-messages'),  # listed only by OpenRouter
    ('claude-fable-5-2', 'https://api.openai.com/v1', 'openai-responses'),  # another provider's model
    ('anthropic.claude-fable-5-2', 'https://bedrock-runtime.us-east-1.amazonaws.com', 'bedrock-converse'),
    ('us.anthropic.claude-haiku-5-5', 'https://bedrock-runtime.us-east-1.amazonaws.com', 'bedrock-converse'),
    ('claude-fable-5-2', 'https://openrouter.ai/api/v1', 'openai-chat'),  # OpenRouter keeps its own price source
    ('claude-fable-5-2', 'https://opencode.ai/zen/v1', 'anthropic-messages'),
    ('claude-fable-5-2', 'http://127.0.0.1:8080', 'openai-chat'),
    ('claude-fable-5-2', 'cli://claude', 'vendor-cli')])
def test_no_price_is_inherited_from_another_version_provider_or_route(reading, model, endpoint, protocol):
    assert prices.reference(route(model, endpoint, protocol)['native_profile']) is None


def test_a_context_tier_is_never_priced_below_the_window_that_reaches_it(reading):
    # Haiku 5.5 costs 5x above 100k tokens; tare's window (131072) reaches that, so every call is priced there.
    assert rates(route('claude-haiku-5-5')) == {'uncached_input_tokens': '0.5', 'output_tokens': '2.5',
                                                 'cache_read_tokens': '0.05', 'cache_write_tokens': '0.625'}
    assert rates(route('claude-haiku-5-5', window=65536))['uncached_input_tokens'] == '0.1'
    terra = dict(endpoint='https://api.openai.com/v1', protocol='openai-responses')
    assert rates(route('gpt-6.1-terra', **terra))['uncached_input_tokens'] == '1'  # 131072 < 272000
    assert rates(route('gpt-6.1-terra', window=300000, **terra)) == {'uncached_input_tokens': '2', 'output_tokens': '7.5',
        'cache_read_tokens': '0.1', 'cache_write_tokens': '2.5'}
    # A tier of a kind tare does not know applies always; one it cannot read leaves the model out.
    data = models_dev()
    entry = data['anthropic']['models']['claude-fable-5-2']['cost']
    entry['tiers'] = [{'input': 9, 'output': 45, 'tier': {'type': 'requests', 'size': 5}}]
    data['anthropic']['models']['claude-haiku-5-5']['cost']['tiers'][0].pop('output')
    parsed = prices.parse_reading(data)
    assert parsed['anthropic/claude-fable-5-2']['tiers'] == [{'above': 0, 'rates': {
        'uncached_input_tokens': '9', 'cache_read_tokens': None, 'cache_write_tokens': None, 'output_tokens': '45'}}]
    assert 'anthropic/claude-haiku-5-5' not in parsed
    saved = {'models': {k: {**v, 'since': 'd'} for k, v in parsed.items()}}
    merged = json.loads(prices.reference(route('claude-fable-5-2')['native_profile'], saved)['rates_json'])
    assert merged == {'uncached_input_tokens': '9', 'output_tokens': '45', 'cache_read_tokens': None,
                      'cache_write_tokens': None}  # a tier without a cache price leaves cache unknown, not cheaper


def test_bedrock_takes_the_price_of_the_exact_regional_id(reading):
    bedrock = dict(endpoint='https://bedrock-runtime.us-east-1.amazonaws.com', protocol='bedrock-converse',
                   window=1000000)
    assert rates(route('us.anthropic.claude-fable-5-2', **bedrock))['uncached_input_tokens'] == '8.8'
    assert rates(route('global.anthropic.claude-fable-5-2', **bedrock))['output_tokens'] == '40'
    assert rates(route('zai.glm-5', **bedrock)) == {'uncached_input_tokens': '1', 'output_tokens': '3.2',
                                                    'cache_read_tokens': None, 'cache_write_tokens': None}
    assert rates(route('us.anthropic.claude-fable-5-2', serviceTier={'type': 'flex'}, **bedrock)) is None


def test_a_mode_with_its_own_price_is_unknown(reading):
    terra = dict(endpoint='https://api.openai.com/v1', protocol='openai-responses')
    assert rates(route('gpt-6.1-terra', service_tier='priority', **terra)) is None
    assert rates(route('claude-fable-5-2', speed='fast')) is None
    # models.dev gives "pro" no price of its own: the same per-token price, with more tokens.
    assert rates(route('gpt-6.1-terra', reasoning={'mode': 'pro'}, service_tier='auto', **terra))['output_tokens'] == '5'


def test_a_failed_empty_reshaped_or_partial_reading_keeps_the_last_good_one(reading):
    def unreachable():
        raise AssertionError('read twice in a day')
    assert prices.refresh_reading(NOW + 3600, fetch=unreachable) == reading  # at most once a day
    later = NOW + 2*86400

    def failing():
        raise OSError('offline')
    reshaped = models_dev(); reshaped['google'] = {'models': []}
    unpriced = models_dev()
    for entry in unpriced['openai']['models'].values():
        entry['cost'] = {'input': 'free'}
    for fetch in (failing, lambda: {}, lambda: [], lambda: reshaped, lambda: unpriced):
        assert prices.refresh_reading(later, fetch=fetch) == reading
        assert prices.reading()['read_at'] == NOW
    # A new reading keeps the day a price was first read while it holds, and moves it when the price changes.
    changed = models_dev()
    changed['anthropic']['models']['claude-fable-5-2']['cost']['input'] = 7
    fresh = prices.refresh_reading(later, fetch=lambda: changed)
    assert fresh['read_at'] == later and fresh['models']['anthropic/claude-haiku-5-5']['since'] == '2026-11-14'
    assert fresh['models']['anthropic/claude-fable-5-2']['since'] == '2026-11-16'


def test_the_reading_respects_the_machine_setup(monkeypatch, tmp_path):
    from relay import environment
    calls = []

    def fetch():
        calls.append(1)
        return models_dev()
    monkeypatch.setattr('relay.provider_quota.secret', lambda name: None)
    assert prices.refresh_reading(NOW, fetch=fetch) is None  # no API key of a route that would use it
    monkeypatch.setattr('relay.provider_quota.secret', lambda name: 'key')
    for setup in ({'use': False}, {'items': {k: False for k in prices.REFERENCE_KEYS}}):
        environment.store().parent.mkdir(parents=True, exist_ok=True)
        environment.store().write_text(json.dumps({'schema': environment.SCHEMA, 'api_providers': setup}))
        assert prices.refresh_reading(NOW, fetch=fetch) is None
    assert calls == [] and prices.reading() is None


def test_without_a_reading_every_consumer_is_as_before(keys):
    from relay import native_profiles, route_facts
    from relay.challenger_variants import price
    from relay.environment import cost_line
    deployment = select('claude-api-fable-5-2')
    assert prices.reading() is None and settings(deployment) == (None, None)
    assert prices.quote(deployment)['mode'] == 'unknown'
    with pytest.raises(ValueError, match='spend_limit_requires_matching_tariff_and_usage_contract'):
        settings({**deployment, 'native_spend_policy': POLICY})
    with pytest.raises(ValueError, match='unavailable or conditional'):
        price(deepcopy(deployment))
    bedrock = prices.quote(select('bedrock-opus'))
    assert bedrock['valid_until'] == '2026-10-23' and 'basis' not in bedrock
    assert native_profiles.describe('claude-api-fable-5-2')['cost_accounting'] == 'unknown'
    assert route_facts.collect(deployment)['price'] == {'status': 'UNKNOWN'}
    assert 'unknown price' in cost_line(deployment)


def test_subscription_cli_tables_are_untouched_by_a_reading(reading):
    codex = {'id': 'x', 'native_profile': {'endpoint': 'cli://codex', 'protocol': 'vendor-cli', 'model': 'gpt-6-luna'}}
    quote = prices.quote(codex)
    assert quote['valid_until'] == '2026-10-23' and 'basis' not in quote


def test_reference_totals_label_usage_rows_and_the_declared_balance(reading):
    from relay import provider_quota, provider_spend
    from relay.console_workbench import usage_rows
    from relay.execution_usage import aggregate_sessions
    tariff = prices.reference(route('claude-fable-5-2')['native_profile'])
    call = {'call_id': 'c1', 'execution': 'possibly_executed', 'cost': {
        'currency': 'USD', 'known_amount': '0.5', 'complete': True, 'tariff': tariff}}
    totals = aggregate_sessions([{'session_id': 's', 'stage': 'direct', 'source': 'x', 'usage': {
        'schema': 'tare-native-usage/1', 'session_id': 's', 'calls': [call]}}])
    assert totals['reference_priced_calls'] == 1 and totals['reference_prices_read'] == ['2026-11-14']
    body = usage_rows({**totals, 'model_calls': 1})[0]['body']
    assert '1 call(s) valued at models.dev reference prices read 2026-11-14: not confirmed tariffs' in body
    provider_spend.declare('anthropic-api', 2.0, now=time.time() - 5)
    assert provider_spend.record({'id': 'r', 'native_credential_env': 'ANTHROPIC_API_KEY'}, [call]) == 1
    credit = provider_spend.remaining('anthropic-api')
    assert credit['remaining'] == 1.5 and credit['at_reference_prices'] == 0.5
    text = provider_quota.render({'observed_at': time.time(), 'accounts': [
        {'account': 'anthropic-api', 'name': 'Claude API', 'kind': 'credit', 'status': 'DECLARED', 'credit': credit}]})
    assert 'US$ 0.50 of that use is valued at models.dev reference prices, not confirmed tariffs' in text


def test_capped_api_session_reserves_against_the_reference_and_counts_the_balance_down(keys, agent_setup, tmp_path):
    """End to end with a fixture client: a Gemini API route that origin/main refuses under a cap now reserves, runs,
    records a cost marked as a reference, and a declared balance counts down by it."""
    from relay import native_agent, native_profiles, provider_spend
    from relay.console_workbench import usage_rows
    config, custody, root = agent_setup
    dep = agent_deployment('openai-chat', 'https://generativelanguage.googleapis.com/v1beta/openai')
    dep['native_profile']['model'] = 'gemini-4-carbon'
    dep.update(native_credential_env='GEMINI_API_KEY', native_spend_policy={'max_amount': '.05', 'currency': 'USD'},
               native_usage_contract={'layout': 'gemini-chat', 'source': 'fixture'})

    class GeminiClient(Client):  # the fixture reply, as Gemini reports it for this model
        endpoint = dep['native_profile']['endpoint']

        def post(self, path, body, control):
            value = super().post(path, body, control)
            if 'usage' in value:
                value.update(model='gemini-4-carbon')
                value['usage']['completion_tokens_details']['reasoning_tokens'] = 0
            return value
    with pytest.raises(ValueError, match='spend_limit_requires_matching_tariff'):  # origin/main, and no reading
        native_agent.run_session(config, deepcopy(dep), tmp_path/'refused', 'Change and verify a.py',
                                 client=GeminiClient())
    prices.refresh_reading(NOW, fetch=models_dev)
    provider_spend.declare('gemini-api', 1.0, now=time.time() - 5)
    client = GeminiClient('openai-chat', steps())
    result = native_agent.run_session(config, dep, custody, 'Change and verify a.py', client=client,
                                      local_counter=client)
    assert result['status'] == 'PROPOSED' and client.generations == 4
    # Each call: 70 uncached x $2 + 10 cached x $0.1 + 20 out x $10 per million tokens.
    assert Decimal(result['spend']['known_amount']) == Decimal('0.001364') and result['spend']['uncertain_calls'] == 0
    usage = json.loads((custody/'usage.json').read_text(encoding='utf-8'))
    assert all(prices.is_reference(c['cost']['tariff']) and c['cost']['complete'] for c in usage['calls'])
    from compute_plane.native_journal import NativeJournal
    spend = [r.event() for r in NativeJournal(custody/'spend.jsonl').snapshot().records]
    assert spend[0]['tariff']['source'].startswith('models.dev reference price (not a confirmed tariff) for google/')
    assert all(Decimal(e['amount']) > 0 for e in spend if e.get('kind') == 'spend_reserved')
    credit = provider_spend.remaining('gemini-api')
    assert credit['remaining'] == pytest.approx(1 - 0.001364) and credit['at_reference_prices'] == pytest.approx(0.001364)
    assert 'valued at models.dev reference prices read 2026-11-14' in usage_rows(native_profiles.usage(custody))[0]['body']
