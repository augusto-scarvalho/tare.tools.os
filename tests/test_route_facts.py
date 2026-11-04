import copy
import time

import pytest

from relay import route_facts, native_routes, native_catalog
from test_native_routes import candidates
from test_work_run import os_root
from test_work_resume import host
from test_work_audit import reviewers


def test_cache_rates_are_separate_and_schedule_is_preserved():
    price = route_facts.advertised_price({'pricing': {'prompt': '0.00000015',
        'completion': '0.0000006', 'input_cache_read': '0.000000003',
        'overrides': [{'utc_start': 100, 'prompt': '0.0000003'}]}},
        {'source': 'catalog', 'observed_at': 100, 'expires_at': 200})
    assert price['per_million_tokens'] == {'uncached_input_tokens': '0.15000000',
        'output_tokens': '0.6000000', 'cache_read_tokens': '0.003000000', 'cache_write_tokens': None}
    assert price['schedule_overrides'][0]['prompt'] == '0.0000003'
    assert price['status'] == 'ADVERTISED'


def test_quota_missing_stale_or_session_tokens_are_not_plan_balance():
    assert route_facts.quota_fact(None)['status'] == 'UNKNOWN'
    quota = {'status': 'EXHAUSTED', 'kind': 'vendor-quota-report', 'source': 'official quota response',
             'observed_at': 100, 'expires_at': 200, 'remaining': 0, 'unit': 'requests'}
    assert route_facts.quota_fact(quota, now=150)['status'] == 'EXHAUSTED'
    assert route_facts.quota_fact(quota, now=201)['status'] == 'UNKNOWN'
    assert route_facts.quota_fact({**quota, 'kind': 'session-token-counter'}, now=150)['status'] == 'UNKNOWN'


def test_live_gpu_status_is_deduplicated_and_redacted(monkeypatch):
    calls = []
    def read(url, **kwargs):
        calls.append(url); now = time.time()
        value = {'role': 'qualified-model-gateway', 'status': 'ok', 'available_models': ['qwen38-gsq'],
                 'gpu_coordination': {'enabled': True, 'held': True, 'owner': {'kind': 'image', 'nonce': 'SECRET'}}}
        if url.endswith('/models'): value = {'data': [{'id': 'qwen38-gsq', 'qualified_for': ['coding-baseline']}]}
        return {'status': 'OBSERVED', 'source': url, 'observed_at': now, 'expires_at': now+5, 'value': value}
    monkeypatch.setattr(route_facts, 'read_json_observation', read)
    profile = native_catalog.select('local-qwen'); cache = {}
    for _ in range(2): result = route_facts.collect(profile, live=True, cache=cache, evidence={})
    assert len(calls) == 2
    assert route_facts.admission_reason(result) == 'GPU_LEASE_HELD'
    assert 'SECRET' not in str(result)
    assert result['quality']['owner_catalog']['status'] == 'OWNER_CATALOG_CLAIM'


def test_metadata_failure_does_not_invent_free_price_or_quality(monkeypatch):
    monkeypatch.setattr(route_facts, 'read_json_observation', lambda url, **kw: {'status': 'UNKNOWN'})
    profile = native_catalog.select('openrouter-glm-5-3')
    facts = route_facts.collect(profile, live=True, evidence={})
    assert facts['price']['status'] == facts['quota']['status'] == facts['quality']['status'] == 'UNKNOWN'
    assert route_facts.admission_reason(facts) is None


@pytest.mark.parametrize('reason', ['GPU_LEASE_HELD', 'GPU_STATUS_UNKNOWN', 'QUOTA_EXHAUSTED'])
def test_automatic_role_excludes_currently_unavailable_candidate(host, candidates, monkeypatch, reason):
    rows = candidates['deployments']
    for row in rows: row['roles'] = ['implementer']
    def facts(deployment, **kwargs):
        return {'quota': {'status': 'EXHAUSTED' if deployment['id'] == 'author' and reason == 'QUOTA_EXHAUSTED' else 'UNKNOWN'},
                'resource': {'immediate': False if deployment['id'] == 'author' and reason != 'QUOTA_EXHAUSTED' else None,
                             'reason': reason}}
    monkeypatch.setattr(route_facts, 'collect', facts)
    routes, receipt = native_routes.ranked('implementer', check_resources=True)
    assert [r['id'] for r in routes] == ['one', 'two']
    assert receipt['candidates'][0]['eligible'] is False and receipt['candidates'][0]['reason'] == reason
    assert receipt['generation_calls'] == 0


def test_gpu_observation_expiry_is_checked_again_at_selection():
    facts = {'quota': {'status': 'UNKNOWN'}, 'resource': {'immediate': True, 'expires_at': 101}}
    assert route_facts.admission_reason(facts, now=102) == 'GPU_STATUS_EXPIRED'
