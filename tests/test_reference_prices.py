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
from test_work_run import NativeFixtureClient, _git, native_fixture_arguments, os_root  # noqa: F401

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
    fetched = []
    # Two hours after the last reading: past the hourly attempt limit, so only the daily one holds this back.
    assert prices.refresh_reading(NOW + 7200, fetch=lambda: fetched.append(1) or models_dev()) == reading
    assert fetched == []  # at most once a day
    later = NOW + 2*86400

    def failing():
        raise OSError('offline')
    reshaped = models_dev(); reshaped['google'] = {'models': []}
    unpriced = models_dev()
    for entry in unpriced['openai']['models'].values():
        entry['cost'] = {'input': 'free'}
    calls = []
    for i, fetch in enumerate((failing, lambda: {}, lambda: [], lambda: reshaped, lambda: unpriced)):
        # Two hours apart: a failed attempt holds the next one back for an hour.
        assert prices.refresh_reading(later + i*7200, fetch=lambda f=fetch: calls.append(1) or f()) == reading
        assert prices.reading()['read_at'] == NOW
    assert len(calls) == 5
    # A new reading keeps the day a price was first read while it holds, and moves it when the price changes.
    changed = models_dev()
    changed['anthropic']['models']['claude-fable-5-2']['cost']['input'] = 7
    fresh = prices.refresh_reading(later + 5*7200, fetch=lambda: changed)
    assert fresh['read_at'] == later + 5*7200 and fresh['models']['anthropic/claude-haiku-5-5']['since'] == '2026-11-14'
    assert fresh['models'][FABLE]['since'] == '2026-11-16'


FABLE = 'anthropic/claude-fable-5-2'


def fable_cost(data):
    return data['anthropic']['models']['claude-fable-5-2']['cost']


@pytest.mark.parametrize('value', ['1e-324', '0', '0.000999'])
def test_a_zero_or_tiny_price_leaves_the_model_out_and_refused_under_a_cap(keys, value):
    data = models_dev()
    fable_cost(data).update(input='TINY', output='TINY', cache_read='TINY', cache_write='TINY')
    data = json.loads(json.dumps(data).replace('"TINY"', value))  # 1e-324 as JSON text, which reads back as 0.0
    reading = prices.refresh_reading(NOW, fetch=lambda: data)
    assert FABLE not in reading['models'] and 'anthropic/claude-haiku-5-5' in reading['models']
    assert rates(route('claude-fable-5-2')) is None
    with pytest.raises(ValueError, match='spend_limit_requires_matching_tariff_and_usage_contract'):
        settings({**route('claude-fable-5-2'), 'native_spend_policy': POLICY})


def test_a_price_at_the_floor_is_kept_a_cache_price_under_it_is_unknown_and_a_tier_under_it_drops_the_model(keys):
    data = models_dev()
    fable_cost(data).update(input=0.001, output=0.001, cache_read=0, cache_write=0.0009)
    data['anthropic']['models']['claude-haiku-5-5']['cost']['tiers'][0]['output'] = 0
    reading = prices.refresh_reading(NOW, fetch=lambda: data)
    assert rates(route('claude-fable-5-2')) == {'uncached_input_tokens': '0.001', 'output_tokens': '0.001',
                                                 'cache_read_tokens': None, 'cache_write_tokens': None}
    assert 'anthropic/claude-haiku-5-5' not in reading['models']


@pytest.mark.parametrize('field,value', [('tiers', {}), ('tiers', False), ('tiers', 0), ('tiers', ''),
                                         ('tiers', 'context'), ('context_over_200k', []),
                                         ('context_over_200k', 'x'), ('context_over_200k', 0)])
def test_a_malformed_tier_leaves_the_model_out_never_the_base_price(keys, field, value):
    data = models_dev()
    data['anthropic']['models']['claude-haiku-5-5']['cost'][field] = value
    prices.refresh_reading(NOW, fetch=lambda: data)
    assert rates(route('claude-haiku-5-5')) is None  # the 131072-token window reaches its 100k tier


def test_an_absent_or_null_tier_is_no_tier(keys):
    data = models_dev()
    data['anthropic']['models']['claude-haiku-5-5']['cost']['tiers'] = None
    prices.refresh_reading(NOW, fetch=lambda: data)
    assert rates(route('claude-haiku-5-5'))['uncached_input_tokens'] == '0.1'


def test_a_failed_attempt_holds_the_next_for_an_hour_and_the_first_reading_never_waits(keys):
    calls = []

    def failing():
        calls.append('failed')
        raise OSError('offline')
    assert prices.refresh_reading(NOW, fetch=failing) is None and calls == ['failed']  # the first attempt: at once
    assert prices.refresh_reading(NOW + 600, fetch=failing) is None and calls == ['failed']  # a start 10 min later
    assert prices.refresh_reading(NOW + 3601, fetch=models_dev)['read_at'] == NOW + 3601


def test_setup_no_use_api_with_refresh_reads_nothing(keys, monkeypatch):
    import urllib.request
    from relay import environment
    calls = []

    def urlopen(*args, **kwargs):
        calls.append(args)
        raise OSError('no network in tests')
    monkeypatch.setattr(urllib.request, 'urlopen', urlopen)
    assert environment.main(['--no-use', 'api', '--refresh']) == 0
    assert calls == [] and prices.reading() is None


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
    row = json.loads((provider_spend.folder()/'provider-spend.jsonl').read_text(encoding='utf-8').splitlines()[-1])
    assert row['reference'] is True and row['price_source'] == tariff['source'] and row['price_read'] == '2026-11-14'
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
    assert result['spend']['reference_prices_read'] == ['2026-11-14']  # the returned spend report says so too
    assert result['cost']['reference_priced_calls'] == 4
    usage = json.loads((custody/'usage.json').read_text(encoding='utf-8'))
    assert all(prices.is_reference(c['cost']['tariff']) and c['cost']['complete'] for c in usage['calls'])
    from compute_plane.native_journal import NativeJournal
    spend = [r.event() for r in NativeJournal(custody/'spend.jsonl').snapshot().records]
    assert spend[0]['tariff']['source'].startswith('models.dev reference price (not a confirmed tariff) for google/')
    assert all(Decimal(e['amount']) > 0 for e in spend if e.get('kind') == 'spend_reserved')
    credit = provider_spend.remaining('gemini-api')
    assert credit['remaining'] == pytest.approx(1 - 0.001364) and credit['at_reference_prices'] == pytest.approx(0.001364)
    assert 'valued at models.dev reference prices read 2026-11-14' in usage_rows(native_profiles.usage(custody))[0]['body']


def test_a_work_reserves_against_the_reference_and_its_outcome_says_so(keys, os_root, tmp_path):
    """The Work host: a Bedrock route with no tariff of its own, under a cap, through implement_native."""
    from compute_plane.native_models import ModelProfile
    from relay import relay_mesh, work_run
    from relay.native_work import implement_native
    prices.refresh_reading(NOW, fetch=models_dev)
    root, repo = os_root
    args = native_fixture_arguments(tmp_path, work_run.load_node(relay_mesh.WORK_GRAPH_FILE, 'work-one'))
    endpoint = 'https://bedrock-runtime.us-east-1.amazonaws.com'
    args['profile'] = ModelProfile('bedrock-converse', 'us.anthropic.claude-fable-5-2', 32768, 1024, 'fixture', endpoint)
    args['client'].endpoint = endpoint
    args['spend_configuration'] = {'native_spend_policy': {'max_amount': '1', 'currency': 'USD'},
                                   'native_usage_contract': {'layout': 'converse-exclusive', 'source': 'fixture'}}
    seen = {}

    def implement(worktree, descriptor):
        seen['worktree'] = worktree
        seen['outcome'] = implement_native(worktree=worktree, descriptor=descriptor, **args)
        return seen['outcome']
    try:
        work_run.run_work('work-one', repository_root=repo, plan_auditor=lambda *a: {'verdict': 'ACCEPT', 'findings': []},
                          implementer=implement, evidence_factory=lambda *a: pytest.fail('proposal is not admission'))
        outcome = seen['outcome']
        assert outcome['status'] == 'PROPOSED', outcome
        # Each of 5 calls: 500 uncached input x $8.80 + 20 output x $44 per million tokens (the US regional price).
        assert Decimal(outcome['spend']['known_amount']) == Decimal('0.0264') and outcome['spend']['uncertain_calls'] == 0
        assert outcome['spend']['reference_sources'] == outcome['cost']['reference_sources']
        assert Decimal(outcome['cost']['known_by_currency']['USD']) == Decimal('0.0264')
        assert outcome['cost']['reference_priced_calls'] == 5
        assert outcome['cost']['reference_prices_read'] == ['2026-11-14']
        assert outcome['cost']['reference_sources'] == ['models.dev reference price (not a confirmed tariff) for '
                                                        'amazon-bedrock/us.anthropic.claude-fable-5-2, https://models.dev/api.json']
    finally:
        if seen.get('worktree') and seen['worktree'].exists():
            _git('worktree', 'remove', '--force', str(seen['worktree']), cwd=repo)


@pytest.mark.parametrize('rates_value,tiers', [('0', []), ('0.0005', []), ('8', {}), ('8', [{'above': 'x', 'rates': {}}])])
def test_a_saved_reading_passes_the_same_check_as_a_fresh_one(keys, rates_value, tiers):
    """A schema-valid file with zero, sub-floor or malformed entries (edited, damaged) prices nothing from them."""
    saved = {'schema': prices.READING, 'source': prices.MODELS_DEV, 'read_at': NOW, 'models': {FABLE: {
        'rates': {k: rates_value for k in ('uncached_input_tokens', 'cache_read_tokens', 'cache_write_tokens',
                                           'output_tokens')}, 'tiers': tiers, 'since': '2026-11-14'}}}
    prices.reading_path().parent.mkdir(parents=True, exist_ok=True)
    prices.reading_path().write_text(json.dumps(saved), encoding='utf-8')
    assert prices.reading()['models'] == {} and rates(route('claude-fable-5-2')) is None
    with pytest.raises(ValueError, match='spend_limit_requires_matching_tariff_and_usage_contract'):
        settings({**route('claude-fable-5-2'), 'native_spend_policy': POLICY})
    assert prices.reference(route('claude-fable-5-2')['native_profile'], saved) is None  # however it is handed over


@pytest.mark.parametrize('record', ['{"at": 1' + '0'*400 + '}', '{"at": "1794627000"}', '{"at": -5}',
                                    '{"at": 1794638000}', '[1]', 'not json'])
def test_an_unusable_attempt_record_is_no_record(keys, record):
    """An overflowing, textual, negative or future time (a clock moved back), or a damaged file, never blocks."""
    path = prices.reading_path().with_name('reference-prices-attempt.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(record, encoding='utf-8')
    assert prices.refresh_reading(NOW, fetch=models_dev)['read_at'] == NOW


def test_a_reading_stamped_after_now_does_not_hold_the_next_back(reading):
    later_clock = NOW - 3*3600  # the clock moved back three hours: the reading looks newer than now
    assert prices.refresh_reading(later_clock, fetch=models_dev)['read_at'] == later_clock
    for damaged in (1e12, -1, 10**400):  # after the year 3000, negative, overflowing: a damaged file, read again
        saved = {**json.loads(prices.reading_path().read_text(encoding='utf-8')), 'read_at': NOW}
        prices.reading_path().write_text(json.dumps({**saved, 'read_at': damaged}), encoding='utf-8')
        assert prices.reading() is None
        prices.reading_path().write_text(json.dumps(saved), encoding='utf-8')


def test_a_reading_that_parses_replaces_the_last_whole(reading):
    smaller = models_dev()
    del smaller['amazon-bedrock']['models']['zai.glm-5']
    glm = route('zai.glm-5', 'https://bedrock-runtime.us-east-1.amazonaws.com', 'bedrock-converse')
    prices.refresh_reading(NOW + 86400, fetch=lambda: smaller)
    assert rates(glm) is None  # missing from the new reading: unknown, refused under a cap
    prices.refresh_reading(NOW + 2*86400, fetch=models_dev)
    assert rates(glm)['output_tokens'] == '3.2'  # back once a reading lists it


def test_the_spend_report_says_its_amounts_are_reference_priced(reading):
    from compute_plane.native_usage import UsageContract, UsageTariff
    from relay.native_spend import SpendModel, SpendPolicy
    from tare_tools_agent_runtime import ConversationLog
    from test_native_spend import Adapter
    tariff = UsageTariff(**prices.reference(route('claude-fable-5-2')['native_profile']))
    report = SpendModel(Adapter(), policy=SpendPolicy('1', 'USD'), tariff=tariff, journal=ConversationLog(),
                        contract=UsageContract('anthropic-exclusive', 'fixture')).report()
    assert report['reference_sources'] == [tariff.source] and report['reference_prices_read'] == ['2026-11-14']
