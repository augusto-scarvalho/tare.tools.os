"""Provider constraints, actual publisher lineage, data-use admission and native reasoning pins."""
import json
import argparse
from pathlib import Path

import pytest

from relay import environment, model_choices, native_catalog, native_generation, native_routes, openrouter, work_audit
from relay.execution_scope import Scope, activated

FRONTIERS = ('openrouter-kimi-k3-1-pro', 'openrouter-muse-wonder')
ALL = (*FRONTIERS, *openrouter.RESEARCH_ROUTES)


@pytest.mark.parametrize('name', ALL)
def test_model_role_boundary_and_exact_requested_route(name, monkeypatch):
    monkeypatch.setattr(environment, 'read', lambda: {'model_data_use': {'meta_contributor': True}})
    value = native_catalog.select(name)
    profile = native_generation.deployment_profile(value, value['native_profile']['model'])
    params = json.loads(profile.parameters_json)
    provider = params['provider']
    assert len(provider['only']) == 1 and provider['only'] == provider['order']
    assert provider['require_parameters'] and provider['allow_fallbacks'] is False
    assert params['transforms'] == []
    assert provider['data_collection'] == ('allow' if profile.model == openrouter.CONTRIBUTOR else 'deny')
    roles = next(r['roles'] for r in native_routes.configured()['deployments'] if r['id'] == name)
    assert set(roles) == ({'planner', 'plan-auditor', 'auditor'} if name in FRONTIERS else {'implementer', 'explorer', 'researcher'})
    assert work_audit.identity(value, 'implementer').vendor != 'OpenRouter'
    assert openrouter.details(value)['precision_provenance'] == 'unknown'


def test_independent_manufacturers_through_one_gateway_and_same_family_rejection(monkeypatch):
    monkeypatch.setattr(native_routes, 'credential_available', lambda name: True)
    author = native_catalog.select('openrouter-kimi-k3-1-pro')
    candidates, _ = native_routes.ranked('auditor', author)
    assert 'openrouter-muse-wonder' in {c['id'] for c in candidates}
    assert work_audit.identity(author, 'implementer').vendor == 'Moonshot AI'
    author = native_catalog.select('openrouter-muse-spark-1-3-contributor')
    candidates, _ = native_routes.ranked('auditor', author)
    assert 'openrouter-muse-wonder' not in {c['id'] for c in candidates}
    assert 'openrouter-kimi-k3-1-pro' in {c['id'] for c in candidates}


def test_contributor_needs_its_own_choice_even_for_custom_profile_and_direct_generation(monkeypatch):
    value = native_catalog.select('openrouter-muse-spark-1-3-contributor')
    value.pop('native_openrouter')
    monkeypatch.setattr(environment, 'read', lambda: {})
    assert not environment.allows(value)[0]
    with pytest.raises(ValueError, match='Meta may use'):
        native_generation.inference_client(value, native_generation.deployment_profile(value, value['model']), credential=lambda: 'fixture')
    # Listing a connection never sends inference or requires data-use acceptance.
    native_generation.inference_client(value, native_generation.deployment_profile(value, value['model']), credential=lambda: 'fixture', metadata_only=True)
    assert environment.allows(value, {'model_data_use': {'meta_contributor': True}})[0]
    normal = native_catalog.select('openrouter-muse-wonder')
    assert environment.allows(normal, {})[0]
    assert json.loads(normal['native_profile']['parameters_json'])['provider']['data_collection'] == 'deny'


@pytest.mark.parametrize('name', ('openrouter-mimo-v2-6-pro', 'openrouter-ling-3-1-flash', 'openrouter-qwen3-8-omni-flash'))
def test_native_reasoning_has_an_exact_default_pin_without_a_made_up_effort(name, monkeypatch):
    monkeypatch.setattr(environment, 'read', lambda: {})
    rows = model_choices.projection(role='researcher', provider='openrouter')
    row = next(r for r in rows if r['deployment'] == name)
    assert row['supported_reasoning'] == ['default'] and row['allowed_reasoning'] == ['default']
    chosen = model_choices.select_exact('openrouter/'+name+'@default', 'researcher')
    params = json.loads(chosen['native_profile']['parameters_json'])
    assert params['reasoning'] == {'enabled': True}
    with pytest.raises(ValueError):
        model_choices.select_exact('openrouter/'+name+'@high', 'researcher')


def test_reported_provider_is_not_confused_with_requested_quantization_or_old_response():
    class Http:
        def post(self, *args):
            return {'provider': 'Fixture Host', 'choices': []}
    client = openrouter.Client(Http())
    rows = []
    record = openrouter.observer(native_catalog.select('openrouter-kimi-k3-1-pro'), client, rows.append)
    client.post(); record({'usage': None})
    assert rows[-1]['openrouter']['served_provider'] == 'Fixture Host'
    assert rows[-1]['openrouter']['served_quantization'] == 'UNKNOWN'
    assert rows[-1]['openrouter']['requested_quantizations'] == ['mxfp4']
    client.inner.post = lambda *args: {}
    client.post(); record({'usage': None})
    assert rows[-1]['openrouter']['served_provider'] is None


def test_low_precision_reference_is_admitted_and_unknown_is_kept_unknown():
    kimi = native_catalog.select('openrouter-kimi-k3-1-pro')
    assert json.loads(kimi['native_profile']['parameters_json'])['provider']['quantizations'] == ['mxfp4']
    ling = native_catalog.select('openrouter-ling-3-1-flash')
    assert ling['native_profile']['context_window'] == 1000000
    assert json.loads(ling['native_profile']['parameters_json'])['provider']['only'] == ['novita']
    assert 'quantizations' not in json.loads(ling['native_profile']['parameters_json'])['provider']


def test_public_role_arguments_accept_native_reasoning_and_enforce_the_pin(monkeypatch):
    from relay import execution_scope, turn_engine
    parser = argparse.ArgumentParser()
    execution_scope.add_arguments(parser)
    name = 'openrouter-mimo-v2-6-pro'
    scope = execution_scope.from_arguments(parser.parse_args(['--researcher', 'openrouter/'+name+'@default']))
    monkeypatch.setattr(turn_engine, 'available', lambda chosen: (execution_scope.constrained(native_catalog.select(chosen), 'researcher'), None))
    with execution_scope.activated(scope):
        value = execution_scope.pinned('researcher')
        execution_scope.require(value, 'researcher')
        assert json.loads(value['native_profile']['parameters_json'])['reasoning'] == {'enabled': True}


def test_route_change_is_local_new_selection_only_and_cannot_change_data_policy(tmp_path, capsys, monkeypatch):
    from relay import native_profiles
    monkeypatch.setattr('compute_plane.model_facts.read_json_observation', lambda *a, **k: {
        'status': 'OBSERVED', 'observed_at': 1, 'value': {'data': {'endpoints': [
            {'tag': 'relace/fp4', 'quantization': 'fp4', 'supported_parameters': ['tools', 'tool_choice', 'reasoning_effort'],
             'context_length': 262144, 'max_completion_tokens': 8192}]}}})
    name = 'openrouter-deepseek-v4-1-flash-1017'
    before = native_catalog.select(name)
    saved = tmp_path/'saved.json'
    saved.write_text(json.dumps(before), encoding='utf-8')
    assert native_profiles.main(['route', name, '--provider', 'relace/fp4', '--quantization', 'fp4']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['inference_calls'] == 0 and result['requested_endpoints'] == ['relace/fp4']
    new = native_catalog.select(name)
    assert json.loads(new['native_profile']['parameters_json'])['provider']['quantizations'] == ['fp4']
    assert new['native_profile']['context_window'] == 262144
    assert new['native_profile']['output_tokens'] == 8192
    assert json.loads(new['native_profile']['parameters_json'])['provider']['data_collection'] == 'deny'
    assert native_generation.load_deployment(saved) == before
    assert native_profiles.main(['route', name, '--reset']) == 0
    assert native_catalog.select(name) == before


def test_selected_endpoint_price_does_not_use_the_cheapest_other_quantization(monkeypatch):
    from relay import route_facts
    def read(address, **kwargs):
        assert address.endswith('/deepseek/deepseek-v4.1-flash-1017/endpoints')
        return {'status': 'OBSERVED', 'source': address, 'observed_at': 1, 'expires_at': 3601,
                'value': {'data': {'endpoints': [
                    {'tag': 'relace/fp4', 'quantization': 'fp4', 'pricing': {'prompt': '0.000000035', 'completion': '0.0000005'}},
                    {'tag': 'morph/fp8', 'quantization': 'fp8', 'pricing': {'prompt': '0.00000005', 'completion': '0.000000192',
                                                                                  'input_cache_read': '0.000000008'}}]}}}
    monkeypatch.setattr(route_facts, 'read_json_observation', read)
    value = native_catalog.select('openrouter-deepseek-v4-1-flash-1017')
    price = route_facts.collect(value, live=True, evidence={})['price']
    assert price['endpoint'] == 'morph/fp8' and price['quantization'] == 'fp8'
    assert float(price['per_million_tokens']['uncached_input_tokens']) == .05
    assert float(price['per_million_tokens']['cache_read_tokens']) == .008


def test_contributor_setup_choice_is_independent_of_the_api_key(capsys):
    assert environment.main(['--meta-contributor', 'on', '--json']) == 0
    capsys.readouterr()
    value = native_catalog.select('openrouter-muse-spark-1-3-contributor')
    assert environment.allows(value)[0]
    assert environment.main(['--meta-contributor', 'off', '--json']) == 0
    assert not environment.allows(value)[0]


@pytest.mark.parametrize('name', ALL)
def test_shipped_routes_match_public_endpoint_limits_and_required_tools(name, monkeypatch):
    fixture = json.loads((Path(__file__).parent/'fixtures/openrouter_endpoints_20261101.json').read_text())
    endpoint = fixture['endpoints'][name]
    value = native_catalog.select(name)
    profile = value['native_profile']
    params = json.loads(profile['parameters_json'])
    required = ('tools', 'tool_choice', 'reasoning_effort' if params['reasoning'].get('effort') else 'reasoning')
    monkeypatch.setattr('compute_plane.model_facts.read_json_observation', lambda *a, **k: {
        'status': 'OBSERVED', 'value': {'data': {'endpoints': [endpoint]}}, 'observed_at': fixture['observed_at']})
    inspected = openrouter.inspect_route(profile['model'], {'providers': params['provider']['only'],
        'quantizations': params['provider'].get('quantizations', [])}, required)
    assert profile['context_window'] <= inspected['context_window']
    assert profile['output_tokens'] <= inspected['output_tokens']
    # Advertising tools alone must not admit a route which refuses tool_choice.
    endpoint['supported_parameters'].remove('tool_choice')
    with pytest.raises(ValueError, match='No endpoint matches'):
        openrouter.inspect_route(profile['model'], {'providers': params['provider']['only']}, required)


def test_provider_specific_cache_contracts():
    for name in ('openrouter-muse-wonder', 'openrouter-muse-spark-1-3-contributor'):
        value = native_catalog.select(name)
        params = json.loads(value['native_profile']['parameters_json'])
        assert params['prompt_cache_key'] == 'tare-'+name+'-v1'
    grok = native_catalog.select('openrouter-grok-4-7')
    assert grok['native_profile']['protocol'] == 'openai-responses'
    assert grok['native_usage_contract']['layout'] == 'openrouter-responses-inclusive'
    assert 'prompt_cache_key' not in json.loads(grok['native_profile']['parameters_json'])
    assert model_choices.describe(grok['id'], grok)['route']['label'] == 'OpenRouter API'
