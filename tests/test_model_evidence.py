import copy
import json

import pytest

from graph_backlog.jsonutil import sha256_canonical
from relay import model_evidence, native_catalog, route_facts, route_plan


@pytest.mark.parametrize('name,benchmark', [('bedrock-sonnet', 79.6), ('bedrock-opus', 80.8)])
def test_builtin_benchmarks_and_cost_references_reach_helper_without_configuration(monkeypatch, name, benchmark):
    monkeypatch.delenv('TARE_ROUTE_EVIDENCE', raising=False)
    deployment = native_catalog.select(name)
    facts = route_facts.collect(deployment)
    assert facts['configured_effort'] == 'high'
    assert facts['quality']['status'] == 'SOURCED_REFERENCE'
    public, own = facts['quality']['records']
    assert public['value'] == benchmark
    assert public['applicability']['level'] == 'MODEL_REFERENCE'
    assert public['applicability']['effort'] == 'DIFFERS'  # Public max vs deployed high.
    assert own['applicability']['level'] == 'HISTORICAL_RUNTIME'
    assert own['applicability']['effort'] == own['applicability']['runtime'] == 'MATCH'
    assert not own['applicability']['current_profile_qualified']
    assert facts['price']['status'] == 'UNKNOWN'  # API reference is not a Bedrock tariff.
    candidate = {'key': 'candidate_1', 'model': deployment['model'], 'effort': 'high',
                 'facts': route_plan.compact_facts(facts)}
    request = route_plan.request_for({'title': 'Fix a UI label'}, 'implementer', [candidate])
    card = json.loads(request['state'])['candidates'][0]['facts']
    assert card['quality']['records'][0]['value'] == benchmark
    assert card['quality']['records'][1]['value']['delivery'] == 'PROPOSED'
    assert card['price_reference']['per_million_tokens']['cache_read_tokens'] is not None
    assert 'not Bedrock' in card['price_reference']['scope']
    assert public['source'].startswith('https://www-cdn.anthropic.com/')


def test_effort_change_does_not_inherit_exact_profile_results(monkeypatch):
    deployment = native_catalog.select('bedrock-sonnet')
    row = {'kind': 'local-evaluation', 'source': 'fixture', 'observed_at': '2026-09-21',
           'task_class': 'bounded-edit', 'metric': 'accepted', 'value': 1, 'scope': 'fixture'}
    evidence = {sha256_canonical(deployment): {'quality': [row]}}
    facts = route_facts.collect(deployment, evidence=evidence)
    assert facts['quality']['records'][0]['applicability']['level'] == 'EXACT_PROFILE'
    assert route_plan.compact_facts(facts)['quality']['records'][0]['metric'] == 'accepted'
    changed = copy.deepcopy(deployment)
    params = json.loads(changed['native_profile']['parameters_json'])
    params['additionalModelRequestFields']['output_config']['effort'] = 'medium'
    changed['native_profile']['parameters_json'] = json.dumps(params)
    new = route_facts.collect(changed, evidence=evidence)
    assert new['configured_effort'] == 'medium'
    assert all(r['source'] != 'fixture' for r in new['quality']['records'])
    assert all(r['applicability']['effort'] == 'DIFFERS' for r in new['quality']['records'])


@pytest.mark.parametrize('field,value', [('model', 'claude-opus-5'), ('wire', 'claude-sonnet-5')])
def test_new_versions_or_unmapped_aliases_do_not_inherit_benchmarks(field, value):
    deployment = native_catalog.select('bedrock-sonnet')
    if field == 'model': deployment['model'] = value
    else: deployment['native_profile']['model'] = value
    assert model_evidence.references(deployment, 'high')['records'] == []


def test_failed_delivery_is_not_hidden_behind_passing_code_tests():
    facts = route_facts.collect(native_catalog.select('bedrock-glm'))
    row = route_plan.compact_facts(facts)['quality']['records'][0]
    assert row['value']['checks_passed'] == 6
    assert row['value']['delivery'] == 'EXHAUSTED_WITHOUT_PROTOCOL_DELIVERY'
    assert facts['quality']['records'][0]['sample_count'] == 1
    assert facts['configured_effort'] == 'high'


def test_subscription_transport_never_receives_api_price_as_plan_cost():
    deployment = native_catalog.select('bedrock-sonnet')
    deployment['model'] = 'claude-sonnet-4-6'
    deployment['native_profile'].update(protocol='vendor-cli', model='claude-sonnet-4-6')
    result = model_evidence.references(deployment, 'high')
    assert result['records'] and not result['price_references']
    assert result['records'][1]['applicability']['runtime'] == 'NOT_ESTABLISHED'


def test_missing_catalog_does_not_change_resource_admission(monkeypatch, tmp_path):
    monkeypatch.setattr(model_evidence, 'CATALOG', tmp_path / 'absent.json')
    facts = route_facts.collect(native_catalog.select('bedrock-sonnet'))
    assert facts['quality']['status'] == 'UNKNOWN'
    assert facts['quality']['reference_error'] == 'MODEL_REFERENCE_UNAVAILABLE'
    assert route_facts.admission_reason(facts) is None


def test_configured_tariff_still_wins_over_reference_prices():
    deployment = native_catalog.select('bedrock-opus')
    rates = {'uncached_input_tokens': '5.5', 'output_tokens': '27.5',
             'cache_read_tokens': '.55', 'cache_write_tokens': '6.875'}
    deployment['native_usage_tariff'] = {'currency': 'USD', 'source': 'operator tariff fixture',
                                       'effective_at': '2026-09-21', 'rates_json': json.dumps(rates)}
    facts = route_facts.collect(deployment)
    card = route_plan.compact_facts(facts)
    assert card['price']['status'] == 'CONFIGURED_TARIFF'
    assert card['price']['per_million_tokens']['output_tokens'] == '27.5'
    assert 'price_reference' not in card


def test_historical_qwen_data_does_not_override_gpu_admission():
    facts = route_facts.collect(native_catalog.select('local-qwen'), live=False)
    assert facts['quality']['records']
    assert facts['configured_effort'] == 'low'
    assert route_facts.admission_reason(facts) == 'GPU_NOT_PROBED'
