"""OpenCode profiles use the existing native loop, picker and identities; no provider is called."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from relay import model_choices, native_agent, native_catalog, opencode
from relay.landing_policy import model_id_for_runtime
from relay.work_audit import identity
from test_native_agent import Client, setup, steps
from test_model_choices import catalog

ROSTER = Path(__file__).parents[1]/'relay/round_table_roster.json'
FAMILIES = {'opencode-go-glm-5-3': ('/chat/completions', lambda p: p['reasoning_effort']),
            'opencode-go-gpt-6-luna': ('/responses', lambda p: p['reasoning']['effort']),
            'opencode-go-claude-haiku-5-5': ('/messages', lambda p: p['output_config']['effort'])}


@pytest.mark.parametrize('name', FAMILIES)
def test_picker_writes_each_family_reasoning_key_and_keeps_the_gateway(name, monkeypatch):
    base = native_catalog.select(name)
    original = deepcopy(base)
    item, = catalog(monkeypatch, {name: base})
    assert item['group'] == 'OpenCode' and item['routes'][0]['label'] == 'OpenCode Go'
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
    name = 'opencode-go-kimi-k2-7-code'
    base = native_catalog.select(name)
    item, = catalog(monkeypatch, {name: base})
    assert item['routes'][0]['efforts'] == []
    value = model_choices.realization([item], {'key': item['key'], 'connection': name, 'reasoning': 'default'})
    assert json.loads(value['native_profile']['parameters_json']) == {}
    assert model_choices.projection(provider='opencode', role='implementer')[0]['provider'] == 'opencode'


@pytest.mark.parametrize('wire,counterpart', [('glm-5.3', 'z-ai/glm-5.3'), ('kimi-k3.1-pro', 'moonshotai/kimi-k3.1-pro'),
                                              ('gpt-6-luna', 'gpt-6-luna'), ('grok-4.7', 'x-ai/grok-4.7')])
def test_author_is_the_family_not_the_gateway(wire, counterpart):
    name = 'opencode-go-' + wire.replace('.', '-')
    value = native_catalog.select(name)
    assert model_id_for_runtime(ROSTER, 'native', wire) == value['model']
    roster = json.loads(ROSTER.read_text(encoding='utf-8'))['models']
    other = next(m for m in roster.values() if (m.get('runtime') or {}).get('native') == counterpart)
    author = identity(value, 'implementer')
    assert (author.vendor, author.bias_domain) == (other['vendor'], other['bias_domain'])


def test_last_call_names_the_delivery_tool_only_where_opencode_lists_it():
    assert opencode.names_delivery_tool(native_catalog.select('opencode-go-gpt-6-luna'))
    assert not opencode.names_delivery_tool(native_catalog.select('opencode-go-glm-5-3'))  # tool_choice: auto only
    assert not opencode.names_delivery_tool(native_catalog.select('opencode-go-hy3'))  # not listed: unknown
    assert opencode.names_delivery_tool(native_catalog.select('openrouter-glm-5-3'))  # other gateways keep it


def test_contributor_models_wait_for_the_data_use_choice():
    from relay.openrouter import allowed
    contributor = native_catalog.select('opencode-go-muse-spark-1-3-contributor')
    assert not allowed(contributor, {})[0]
    assert allowed(contributor, {'model_data_use': {'meta_contributor': True}})[0]
    assert allowed(native_catalog.select('opencode-go-glm-5-3'), {})[0]


def test_client_names_tare_and_charges_are_provider_reported():
    from relay.native_generation import deployment_profile, inference_client
    from relay.native_profiles import describe
    from relay.provider_prices import quote
    value = native_catalog.select('opencode-go-glm-5-3')
    client = inference_client(value, deployment_profile(value, value['native_profile']['model']), credential=lambda: 'k')
    assert client.request_headers()['User-Agent'].startswith('tare/')
    assert describe('opencode-go-glm-5-3', value)['cost_accounting'] == 'provider_reported'
    assert quote(value)['source'] == 'opencode.usage.cost'


def test_chat_profile_runs_the_native_loop_with_the_opencode_wire_id(setup):
    config, custody, root = setup
    config['capabilities'] = ['read', 'edit', 'verify']
    deployment = native_catalog.select('opencode-go-glm-5-3')

    class OpenCodeFixture(Client):
        endpoint = deployment['native_profile']['endpoint']

        def post(self, path, body, control):
            assert path == '/chat/completions' and body['model'] == 'glm-5.3'
            assert body['reasoning_effort'] == 'high' and body['tool_choice'] == 'auto'
            response = super().post(path, body, control)
            response['model'] = 'glm-5.3'
            response['usage'] = {'prompt_tokens': 80, 'completion_tokens': 5, 'cost': 0,
                                 'opencode': {'billing_source': 'go', 'allowance': 0.0001,
                                              'tokens': {'input': 80, 'output': 5, 'reasoning': 0, 'cache_read': 0,
                                                         'cache_write_5m': 0, 'cache_write_1h': 0}}}
            return response

    result = native_agent.run_session(config, deployment, custody, 'Change and verify a.py',
                                      client=OpenCodeFixture(steps=steps()))
    assert result['status'] == 'PROPOSED' and (root/'a.py').read_text() == 'value = 2\n'


def test_go_is_a_subscription_on_its_own_plan_account():
    from relay import execution_scope
    from relay.challenger_variants import accounting
    from relay.turn_engine import usage_keys
    from relay.work_units import bill
    go = native_catalog.select('opencode-go-glm-5-3')
    assert execution_scope.kind(go) == 'subscription'
    assert execution_scope.Scope(only='subscription').allows(go, 'implementer')[0]
    assert not execution_scope.Scope(only='api').allows(go, 'implementer')[0]
    assert accounting(go) == 'quota' and bill(go) == ('quota', 'opencode')
    assert usage_keys(go) == ('opencode-go', 'opencode-go:glm-5.3')  # a model's own limit leaves its siblings usable
    listed = model_choices.projection(provider='opencode', role='implementer', include_unconfigured=True)
    assert {row['via'] for row in listed} == {'api'}
