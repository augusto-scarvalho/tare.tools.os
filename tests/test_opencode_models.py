"""OpenCode profiles use the existing native loop, picker and identities; no provider is called."""
from copy import deepcopy
import json
from pathlib import Path
import time

import pytest

from relay import model_choices, native_agent, native_catalog, opencode
from relay.landing_policy import model_id_for_runtime
from relay.work_audit import identity
from test_native_agent import Client, setup, steps
from test_model_choices import catalog

ROSTER = Path(__file__).parents[1]/'relay/round_table_roster.json'
FAMILIES = {'opencode-sub-glm-5-3-flash': ('/chat/completions', lambda p: p['reasoning_effort']),
            'opencode-sub-gpt-6-luna': ('/responses', lambda p: p['reasoning']['effort']),
            'opencode-sub-claude-haiku-5-5': ('/messages', lambda p: p['output_config']['effort'])}
SUBSCRIPTION = ['claude-haiku-5-5', 'deepseek-v4-1-flash', 'glm-5-3-flash', 'gpt-6-luna', 'grok-4-7', 'kimi-k3',
                'mimo-v2-6-flash']  # the operator's list; Gemini 4 Argon waits for a Gemini API codec


def api_profile(name='opencode-sub-glm-5-3-flash'):
    """The same model as a pay-per-token API profile (none ships until the account has a balance)."""
    value = native_catalog.select(name)
    value['id'] = name.replace('opencode-sub-', 'opencode-api-')
    value['native_opencode'] = {**value['native_opencode'], 'plan': 'api'}
    value['native_profile']['endpoint'] = value['native_profile']['endpoint'].replace('/inference/go/', '/inference/')
    return value


def test_only_the_operators_models_have_profiles():
    assert sorted(n for n in native_catalog.LOCAL if n.startswith('opencode-')) == ['opencode-sub-' + n for n in SUBSCRIPTION]


@pytest.mark.parametrize('name', FAMILIES)
def test_picker_writes_each_family_reasoning_key_and_keeps_the_gateway(name, monkeypatch):
    base = native_catalog.select(name)
    original = deepcopy(base)
    item, = catalog(monkeypatch, {name: base})
    assert item['group'] == item['routes'][0]['label'] == 'OpenCode Subscription'
    levels = item['routes'][0]['efforts']
    assert 'high' in levels and levels == base['native_reasoning_levels']
    for effort in levels:
        value = model_choices.realization([item], {'key': item['key'], 'connection': name, 'reasoning': effort})
        assert FAMILIES[name][1](json.loads(value['native_profile']['parameters_json'])) == effort
        profile = value['native_profile']
        assert profile['session_header'] == 'x-opencode-session' and profile['endpoint'].startswith('https://opencode.ai/inference/go/')
        assert value['native_credential_env'] == 'OPENCODE_API_KEY'
        assert value['native_usage_contract']['layout'] == 'opencode-exclusive'
    assert base == original


def test_a_model_without_listed_levels_offers_only_its_default(monkeypatch):
    name = 'opencode-sub-mimo-v2-6-flash'
    base = native_catalog.select(name)
    item, = catalog(monkeypatch, {name: base})
    assert item['routes'][0]['efforts'] == []
    value = model_choices.realization([item], {'key': item['key'], 'connection': name, 'reasoning': 'default'})
    assert json.loads(value['native_profile']['parameters_json']) == {}


@pytest.mark.parametrize('wire,counterpart', [('glm-5.3-flash', 'z-ai/glm-5.3-flash'), ('kimi-k3', 'moonshotai/kimi-k3.1-pro'),
                                              ('gpt-6-luna', 'gpt-6-luna'), ('grok-4.7', 'x-ai/grok-4.7')])
def test_author_is_the_family_not_the_gateway(wire, counterpart):
    value = native_catalog.select('opencode-sub-' + wire.replace('.', '-'))
    assert model_id_for_runtime(ROSTER, 'native', wire) == value['model']
    roster = json.loads(ROSTER.read_text(encoding='utf-8'))['models']
    other = next(m for m in roster.values() if (m.get('runtime') or {}).get('native') == counterpart)
    author = identity(value, 'implementer')
    assert (author.vendor, author.bias_domain) == (other['vendor'], other['bias_domain'])


def test_last_call_names_the_delivery_tool_only_where_opencode_lists_it():
    assert opencode.names_delivery_tool(native_catalog.select('opencode-sub-gpt-6-luna'))
    glm = native_catalog.select('opencode-sub-glm-5-3-flash')
    assert not opencode.names_delivery_tool(glm)  # tool_choice: auto only
    glm['native_opencode']['tool_choice'] = []
    assert not opencode.names_delivery_tool(glm)  # not listed: unknown
    assert opencode.names_delivery_tool(native_catalog.select('openrouter-glm-5-3'))  # other gateways keep it


def test_contributor_models_wait_for_the_data_use_choice():
    from relay.openrouter import allowed
    contributor = native_catalog.select('opencode-sub-glm-5-3-flash')
    contributor['native_opencode']['data_use'] = 'meta-contributor'
    assert not allowed(contributor, {})[0]
    assert allowed(contributor, {'model_data_use': {'meta_contributor': True}})[0]
    assert allowed(native_catalog.select('opencode-sub-glm-5-3-flash'), {})[0]


def test_client_names_tare_and_charges_are_provider_reported():
    from relay.native_generation import deployment_profile, inference_client
    from relay.native_profiles import describe
    from relay.provider_prices import quote
    value = native_catalog.select('opencode-sub-glm-5-3-flash')
    client = inference_client(value, deployment_profile(value, value['native_profile']['model']), credential=lambda: 'k')
    assert client.request_headers()['User-Agent'].startswith('tare/')
    haiku = native_catalog.select('opencode-sub-claude-haiku-5-5')
    messages = inference_client(haiku, deployment_profile(haiku, 'claude-haiku-5-5'), credential=lambda: 'k')
    assert messages.request_headers()['anthropic-version'] == '2023-06-01'
    assert describe('opencode-sub-glm-5-3-flash', value)['cost_accounting'] == 'provider_reported'
    assert quote(value)['source'] == 'opencode.usage.cost'


def test_chat_profile_runs_the_native_loop_with_the_opencode_wire_id(setup):
    config, custody, root = setup
    config['capabilities'] = ['read', 'edit', 'verify']
    deployment = native_catalog.select('opencode-sub-glm-5-3-flash')

    class OpenCodeFixture(Client):
        endpoint = deployment['native_profile']['endpoint']

        def post(self, path, body, control):
            assert path == '/chat/completions' and body['model'] == 'glm-5.3-flash'
            assert body['reasoning_effort'] == 'high' and body['tool_choice'] == 'auto'
            response = super().post(path, body, control)
            response['model'] = 'glm-5.3-flash'
            response['usage'] = {'prompt_tokens': 80, 'completion_tokens': 5, 'cost': 0,
                                 'opencode': {'billing_source': 'go', 'allowance': 0.0001,
                                              'tokens': {'input': 80, 'output': 5, 'reasoning': 0, 'cache_read': 0,
                                                         'cache_write_5m': 0, 'cache_write_1h': 0}}}
            return response

    result = native_agent.run_session(config, deployment, custody, 'Change and verify a.py',
                                      client=OpenCodeFixture(steps=steps()))
    assert result['status'] == 'PROPOSED' and (root/'a.py').read_text() == 'value = 2\n'


def test_the_subscription_is_a_plan_and_the_api_is_paid_per_token():
    from relay import execution_scope
    from relay.challenger_variants import accounting
    from relay.inference_allowlist import identities
    from relay.turn_engine import usage_keys
    from relay.work_units import bill
    sub, api = native_catalog.select('opencode-sub-glm-5-3-flash'), api_profile()
    assert execution_scope.kind(sub) == 'subscription' and execution_scope.kind(api) == 'api'
    assert execution_scope.Scope(only='subscription').allows(sub, 'implementer')[0]
    assert not execution_scope.Scope(only='subscription').allows(api, 'implementer')[0]
    assert not execution_scope.Scope(only='api').allows(sub, 'implementer')[0]
    assert execution_scope.Scope(only='api').allows(api, 'implementer')[0]
    assert accounting(sub) == 'quota' and bill(sub) == ('quota', 'opencode') and accounting(api) == 'usd'
    assert usage_keys(sub) == ('opencode-sub', 'opencode-sub:glm-5.3-flash') and usage_keys(api) == ()
    assert identities(sub['id'], sub)[0] == 'opencode-sub' and identities(api['id'], api)[0] == 'opencode-api'
    assert model_choices.describe(api['id'], api)['route']['label'] == 'OpenCode API'
    listed = model_choices.projection(provider='opencode-sub', role='implementer', include_unconfigured=True)
    assert {row['via'] for row in listed} == {'api'} and len(listed) == len(SUBSCRIPTION)


def test_every_subscription_model_is_a_pinnable_reserve_until_qualified():
    from relay.inference_allowlist import exact, matches
    from relay.native_routes import configured
    rows = [r for r in configured()['deployments'] if r['id'].startswith('opencode-')]
    assert sorted(r['id'] for r in rows) == ['opencode-sub-' + n for n in SUBSCRIPTION]
    assert {(tuple(r['roles']), r['kind'], r['cost_tier'], r['quota_domain'], r['qualified']) for r in rows} == {
        (('implementer', 'explorer'), 'reserve', 'flat', 'opencode-sub', False)}
    pin = exact('opencode-sub/opencode-sub-glm-5-3-flash@high')
    assert matches(pin, 'opencode-sub-glm-5-3-flash', native_catalog.select('opencode-sub-glm-5-3-flash'))


def test_a_subscription_limit_parks_the_whole_plan(tmp_path, monkeypatch):
    from relay import turn_engine
    monkeypatch.setattr(turn_engine, 'usage_limits_path', lambda: tmp_path/'usage-limits.json')
    glm, kimi = native_catalog.select('opencode-sub-glm-5-3-flash'), native_catalog.select('opencode-sub-kimi-k3')
    until = turn_engine.note_usage_limit(glm, now=1000.0)  # the plan's 5-hour window, shared by its models
    assert turn_engine.usage_limited(glm, now=1001.0) == until == turn_engine.usage_limited(kimi, now=1001.0)
    assert turn_engine.usage_limited(api_profile(), now=1001.0) is None  # the balance is another bill


def test_unqualified_rows_are_pinned_never_chosen_automatically(monkeypatch):
    from relay import native_routes
    monkeypatch.setattr(native_routes, 'credential_available', lambda name: True)
    _, selection = native_routes.ranked('explorer')
    reasons = {c['deployment']: c['reason'] for c in selection['candidates'] if c['deployment'].startswith('opencode-')}
    assert len(reasons) == len(SUBSCRIPTION) and set(reasons.values()) == {'NOT_QUALIFIED_PIN_ONLY'}


def test_a_spent_plan_or_a_balance_charge_is_a_limit_on_that_model_only():
    sub, api = native_catalog.select('opencode-sub-glm-5-3-flash'), api_profile()
    spent = [{'call_id': 'c1', 'transport_calls': [{'provider_error': {'type': 'GoUsageLimitError'}, 'retry_after': 14921}]}]
    assert opencode.plan_limit(sub, {'code': 'HTTP_429', 'call_id': 'c1'}, spent, 1000.0) == {
        'kind': 'account', 'resets_at': 15921.0, 'cause': 'allowance_spent'}
    summary = {'kind': 'context_summary_failed', 'call_id': 'c1', 'reason': 'HTTP_429'}  # a summary says reason
    assert opencode.plan_limit(sub, summary, spent, 1000.0)['cause'] == 'allowance_spent'
    passing = [{'call_id': 'c1', 'transport_calls': [{'provider_error': {'type': 'rate_limit_error'}, 'retry_after': 3}]}]
    assert opencode.plan_limit(sub, {'code': 'HTTP_429', 'call_id': 'c1'}, passing, 1000.0) is None
    billed = [{'call_id': 'c1', 'billing_source': 'balance'}]
    limit = opencode.plan_limit(sub, {'code': 'BILLING_SOURCE_REFUSED', 'call_id': 'c1'}, billed, 1000.0)
    assert limit == {'kind': 'account', 'resets_at': 1000.0 + 5 * 3600, 'billing_source': 'balance', 'cause': 'billed_to_balance'}
    teams = opencode.plan_limit(sub, {'code': 'BILLING_SOURCE_REFUSED', 'call_id': 'c1'},
                                [{'call_id': 'c1', 'billing_source': 'go-teams'}], 1000.0)
    assert teams['cause'] == 'unknown_billing_source'
    from relay.native_display import provider_limit_guidance
    notice = provider_limit_guidance({'failure_code': 'PLAN_USAGE_LIMIT', 'usage_limit': {**teams, 'until': 1000.0}})
    assert "'go-teams'" in notice and 'BILLING in scripts/ops/opencode_profiles.py' in notice
    assert opencode.plan_limit(api, {'code': 'HTTP_429', 'call_id': 'c1'}, spent, 1000.0) is None  # the balance has no allowance
    assert json.loads(sub['native_profile'].get('parameters_json')) is not None and sub['native_profile']['billing_sources'] == ['free', 'go', 'go-plus']


def test_a_session_on_a_spent_model_parks_it_until_retry_after(setup, tmp_path, monkeypatch):
    from tare_tools_agent_runtime import ModelFailure
    from relay import turn_engine
    from relay.native_display import provider_limit_guidance
    monkeypatch.setattr(turn_engine, 'usage_limits_path', lambda: tmp_path/'usage-limits.json')
    config, custody, root = setup
    deployment = native_catalog.select('opencode-sub-kimi-k3')

    class Spent(Client):
        endpoint = deployment['native_profile']['endpoint']

        def post_observed(self, path, body, control, **kwargs):
            failure = ModelFailure('HTTP_429')
            failure.transport_observation = {'transport': 'fixture', 'started': True, 'response_received': True,
                                             'provider_error': {'type': 'GoUsageLimitError'}, 'retry_after': 14921}
            raise failure

    before = time.time()
    result = native_agent.run_session(config, deployment, custody, 'Change and verify a.py', client=Spent(steps=steps()))
    assert result['failure_code'] == 'PLAN_USAGE_LIMIT' and result['usage_limit']['cause'] == 'allowance_spent'
    assert result['usage_limit']['account'] == 'opencode-sub'
    until = turn_engine.usage_limited(deployment)
    assert until == result['usage_limit']['until'] and before + 14921 <= until <= time.time() + 14921
    assert turn_engine.usage_limited(native_catalog.select('opencode-sub-glm-5-3-flash')) == until  # the plan's window
    assert 'no usage left until' in provider_limit_guidance(result)
