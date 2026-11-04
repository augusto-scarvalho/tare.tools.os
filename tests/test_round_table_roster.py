import copy

import pytest

from relay import round_table_roster


def test_roster_validation_and_projection():
    roster = round_table_roster.load_roster()
    entries = round_table_roster._all_entries(roster)
    assert all(not e['runner'].startswith('aider') for e in entries)
    assert [e['model'] for e in round_table_roster.eligible_mediators(roster)] == ['qwen-3.8-27b','fable-tc']
    assert roster['policy']['work_author_conflict'] == 'explicit_case_bias_domain_exclusion'
    assert roster['policy']['external_model_attestation']['runtime_mismatch'] == 'fail_closed'
    projection = round_table_roster.render_markdown(roster)
    assert 'Qwen 3.8 27B -> Fable TC' in projection
    assert 'Amazon Bedrock' not in projection
    fallbacks = {e['id']:e for e in roster['chairs']['fallbacks']}
    assert round_table_roster.bias_domain(roster, fallbacks['chair-qwen-local']) == round_table_roster.bias_domain(roster, fallbacks['chair-fable-tc-local'])
    duplicate = copy.deepcopy(roster)
    duplicate['chairs']['fixed'][2].update(model='gpt-5.6-terra',runner='codex',effort='medium')
    with pytest.raises(ValueError, match='duplicate bias domains'):
        round_table_roster.validate_roster(duplicate)


def test_roster_rejects_pseudo_effort_and_effort_ignored_by_runner():
    roster = round_table_roster.load_roster()

    pseudo = copy.deepcopy(roster)
    pseudo["mediators"][2]["effort"] = "approved_high_fidelity_template"
    with pytest.raises(ValueError, match="unsupported effort"):
        round_table_roster.validate_roster(pseudo)

    ignored = copy.deepcopy(roster)
    next(entry for entry in ignored["chairs"]["fallbacks"] if entry["id"] == "chair-kimi-cli")["effort"] = "high"
    with pytest.raises(ValueError, match="unsupported effort"):
        round_table_roster.validate_roster(ignored)


def test_bedrock_catalog_and_native_role_deployments_preserve_access_policy():
    from relay.native_catalog import select
    roster = round_table_roster.load_roster()
    assert roster['models']['z-ai/glm-5.3']['bias_domain'] == roster['models']['glm-5']['bias_domain']
    assert roster['models']['deepseek/deepseek-v4.1-flash']['bias_domain'] == roster['models']['deepseek-v3.2']['bias_domain']
    catalog = {item['id']:item for item in round_table_roster.bedrock_catalog(roster)}
    assert {key for key,row in catalog.items() if row['access']=='denied'} == {'fable-5','gpt-5.6-sol','grok-4.6'}
    assert len([row for row in catalog.values() if row['access']=='verified']) == 12
    assert select('bedrock-opus')['native_profile']['model'] == 'us.anthropic.claude-opus-4-6-v1'
    roles = round_table_roster.load_role_deployments(roster=roster)
    assert all(not e['runner'].startswith('aider') for e in roles['deployments'])
    assert set(roles['deployments'][0]['roles']) == {'implementer', 'explorer'}
    reserve = next(e for e in roles['deployments'] if e['id']=='bedrock-opus-explorer')
    assert reserve['kind']=='reserve' and reserve['roles']==['implementer','explorer']
    assert reserve['native_catalog']=='bedrock-opus'
    bad = copy.deepcopy(roles);bad['deployments'][0]['roles']=['chair']
    with pytest.raises(ValueError,match='must list roles'):
        round_table_roster.validate_role_deployments(bad,roster)
    denied = copy.deepcopy(roster);denied['models']['claude-opus-4.6']['bedrock']['access']='denied'
    with pytest.raises(ValueError,match='not verified'):
        round_table_roster.validate_role_deployments(roles,denied)
    chair = copy.deepcopy(roster);chair['chairs']['fallbacks'].append(dict(reserve,priority=99))
    with pytest.raises(ValueError,match='not a Round Table transport'):
        round_table_roster.validate_roster(chair)


def test_shipped_local_endpoints_resolve_to_this_machines_gateway(monkeypatch):
    # The catalogs named one operator's node by address; each machine now resolves the placeholder, as bundled
    # profiles do, and a machine without a gateway setting gets localhost.
    from relay import resource_telemetry
    monkeypatch.setenv('TARE_LOCAL_GATEWAY', 'http://gpu-node:8080')
    monkeypatch.delenv('RELAY_COORDINATOR_URL', raising=False)
    roster, roles = round_table_roster.load_roster(), round_table_roster.load_role_deployments()
    endpoints = [e['native_profile']['endpoint'] for e in [*round_table_roster._all_entries(roster), *roles['deployments']]
                 if 'native_profile' in e and e['native_profile']['endpoint'].startswith('http://')]
    assert endpoints and set(endpoints) == {'http://gpu-node:8080/v1'}
    assert resource_telemetry.coordinator_candidates() == ['http://127.0.0.1:8765', 'http://gpu-node:8765']
