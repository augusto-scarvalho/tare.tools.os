import copy
import json
from types import SimpleNamespace

import pytest

from compute_plane.choice_assessment import digest, request_digest
from relay import adaptive_routes, native_catalog, native_routes, route_facts, route_plan
from test_model_choices import profile


@pytest.fixture
def routing(tmp_path, monkeypatch):
    profiles = {name: {**profile(), 'id': name} for name in ('one', 'two', 'three')}
    monkeypatch.setattr(native_catalog, 'select', lambda name: copy.deepcopy(profiles[name]))
    calls = []
    choices = ['candidate_2', 'candidate_1', 'candidate_3']
    def assess(command, request, **kw):
        calls.append(copy.deepcopy(request))
        chosen = choices.pop(0)
        return {'status': 'OBSERVED', 'inference_attempts': 1, 'elapsed_ms': 1,
            'answer': {'choice': chosen, 'probabilities': {k: float(k == chosen) for k in request['options']},
                       'usage': {'input_tokens': 100, 'output_tokens': 0}, 'model_identity': {'fixture': True}}}
    monkeypatch.setattr(adaptive_routes, 'run_assessment', assess)
    def ranked(*a, **kw):
        return list(copy.deepcopy(profiles).values()), {'candidates': [
            {'deployment': n, 'selected_deployment': n, 'eligible': True,
             'profile_sha256': digest(p), 'cost_tier': 'flat'} for n, p in profiles.items()]}
    monkeypatch.setattr(native_routes, 'ranked', ranked)
    base_collect = route_facts.collect
    def collect(deployment, **kwargs):
        facts = base_collect(deployment, live=False, evidence={})
        facts['quality'] = {'status': 'SUPPLIED_EVIDENCE', 'records': [{
            'kind': 'local-evaluation', 'source': 'fixture-only', 'observed_at': '2026-09-21',
            'task_class': 'bounded-edit', 'metric': 'accepted_works', 'value': 3,
            'sample_count': 4, 'scope': 'Authored fixture, not measured model qualification.'}]}
        return facts
    monkeypatch.setattr(route_facts, 'collect', collect)
    policy = {'schema': 'tare.tools/adaptive-route-policy/2', 'mode': 'shadow', 'command': ['unused'],
              'candidates': {'implementer': [{'deployment': n} for n in profiles]}}
    policy_path = tmp_path / 'policy.json'
    monkeypatch.setenv('TARE_ADAPTIVE_ROUTE_POLICY', str(policy_path))
    cancellation = SimpleNamespace(is_cancelled=lambda: False)
    task = {'title': 'Correct a spelling error', 'exit_criteria': ['Exact text matches'], 'summary': 'One string only'}
    def run(**kwargs):
        policy_path.write_text(json.dumps(policy))
        return adaptive_routes.observe_work(task, profiles['one'], kwargs.pop('audit', {}), tmp_path,
            cancellation, automatic_implementation=kwargs.pop('automatic', True), **kwargs)
    return SimpleNamespace(profiles=profiles, calls=calls, choices=choices, policy=policy,
        cancel=cancellation, run=run, root=tmp_path, collect=collect, task=task)


def saved(routing):
    return json.loads((routing.root / 'route-assessment' / 'route-plan.json').read_bytes())


def test_conditional_ranking_fallback_contract_and_replay(routing, monkeypatch):
    before = copy.deepcopy(routing.profiles)
    result = routing.run()
    plan = saved(routing)
    role = plan['roles']['implementer']
    assert role['primary']['deployment'] == 'two'
    assert [r['deployment'] for r in role['fallbacks']] == ['one', 'three']
    assert 'candidate_2' not in routing.calls[1]['options']
    assert set(routing.calls[2]['options']) == {'candidate_3', 'insufficient'}
    assert role['fallbacks'][0]['when'] == ['HTTP_429', 'HTTP_502', 'HTTP_503', 'HTTP_504']
    assert 'ADMITTED_SUCCESSOR' in role['fallbacks'][0]['requires']
    assert role['escalation'] is None
    assert plan['input_tokens'] == 300 and plan['inference_attempts'] == 3
    assert routing.profiles == before and result['applied'] is False
    assert plan['requires_live_revalidation'] and plan['qualification'] == 'UNQUALIFIED'
    for call, request in zip(role['assessments'], routing.calls):
        assert call['request_sha256'] == request_digest(request)
    state = json.loads(routing.calls[0]['state'])
    assert state['candidates'][0]['facts']['quality']['records'][0]['source_ref'] == 'quality.records[0]'
    fact_file = routing.root / 'route-assessment' / role['candidates'][0]['facts_path']
    saved_facts = json.loads(fact_file.read_bytes())
    assert saved_facts['quality']['records'][0]['source'] == 'fixture-only'
    assert role['candidates'][0]['profile_sha256'] == saved_facts['profile_sha256']
    assert state['candidates'][0]['facts']['configured_cost_tier'] == 'flat'
    monkeypatch.setattr(route_facts, 'collect', lambda *a, **kw: pytest.fail('replay must not probe'))
    assert routing.run() == result and len(routing.calls) == 3
    # The bounded status contains only a compact projection and a custody path.
    assert len(json.dumps(adaptive_routes.summary(result, routing.root))) < 2000


def test_supported_efforts_and_quality_reserve_are_separate(routing):
    routing.policy['candidates']['implementer'] = [
        {'deployment': 'one', 'effort': 'medium'},
        {'deployment': 'two', 'effort': 'low'},
        {'deployment': 'three', 'effort': 'high', 'purpose': 'escalation'}]
    routing.run()
    role = saved(routing)['roles']['implementer']
    assert role['primary']['effort'] == 'low'
    assert role['fallbacks'][0]['effort'] == 'medium'
    assert 'candidate_3' not in routing.calls[0]['options']
    reserve = role['escalation']
    assert reserve['effort'] == 'high' and reserve['max_attempts'] == 1
    assert reserve['when'] == ['IMPLEMENTATION_CHECKS_FAILED']
    assert 'FAILURE_EVIDENCE' in reserve['requires']
    assert role['primary']['profile_sha256'] != digest(routing.profiles['two'])


@pytest.mark.parametrize('choices,expected', [(['insufficient'], 0),
    (['candidate_2', 'insufficient'], 0)])
def test_abstention_does_not_invent_unassessed_fallbacks(routing, choices, expected):
    routing.choices[:] = choices
    routing.run()
    role = saved(routing)['roles']['implementer']
    assert len(role['fallbacks']) == expected and not routing.choices
    if len(choices) == 1:
        assert role['primary'] is None and role['reason'] == 'INSUFFICIENT_EVIDENCE'


@pytest.mark.parametrize('fault', ['gpu', 'quota', 'effort', 'duplicate'])
def test_excluded_candidates_never_reach_helper(routing, monkeypatch, fault):
    routing.choices[:] = ['candidate_2', 'candidate_3']
    if fault in {'gpu', 'quota'}:
        def collect(deployment, **kwargs):
            facts = routing.collect(deployment)
            if deployment['id'] == 'one':
                if fault == 'gpu':
                    facts['resource'] = {'immediate': False, 'status': 'BUSY', 'reason': 'GPU_LEASE_HELD'}
                else:
                    facts['quota'] = {'status': 'EXHAUSTED'}
            return facts
        monkeypatch.setattr(route_facts, 'collect', collect)
    elif fault == 'effort':
        routing.policy['candidates']['implementer'][0]['effort'] = 'invented'
    else:
        routing.policy['candidates']['implementer'][2] = {'deployment': 'two'}
        routing.choices[:] = ['candidate_2', 'candidate_1']
    routing.run()
    role = saved(routing)['roles']['implementer']
    assert len(role['exclusions']) == 1
    excluded = role['exclusions'][0]['key']
    assert all(excluded not in call['options'] for call in routing.calls)


def test_timeout_is_shared_and_partial_plan_preserves_evidence(routing, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(route_plan.time, 'monotonic', lambda: clock[0])
    routing.policy['total_timeout_seconds'] = 5
    def assess(command, request, **kwargs):
        assert kwargs['timeout_seconds'] == 5
        clock[0] = 6
        return {'status': 'OBSERVED', 'inference_attempts': 1,
                'answer': {'choice': 'candidate_1', 'usage': {'input_tokens': 7}}}
    monkeypatch.setattr(adaptive_routes, 'run_assessment', assess)
    routing.run()
    role = saved(routing)['roles']['implementer']
    assert role['status'] == 'PARTIAL' and role['primary']['deployment'] == 'one'
    assert role['reason'] == 'ASSESSMENT_BUDGET_EXHAUSTED' and not role['fallbacks']


@pytest.mark.parametrize('failure', ['cancel', 'timeout', 'invalid', 'overflow'])
def test_failed_or_cancelled_assessment_is_not_a_suggestion(routing, monkeypatch, failure):
    if failure == 'overflow':
        routing.task['summary'] = 'x' * 9000
        monkeypatch.setattr(adaptive_routes, 'run_assessment', lambda *a, **kw: pytest.fail('oversize inference'))
    else:
        def assess(*args, **kwargs):
            if failure == 'cancel':
                routing.cancel.is_cancelled = lambda: True
                return {'status': 'OBSERVED', 'answer': {'choice': 'candidate_1'}}
            if failure == 'timeout':
                return {'status': 'FAILED', 'reason': 'ASSESSMENT_TIMEOUT', 'inference_attempts': 1}
            return {'status': 'OBSERVED', 'answer': {'choice': 'not-allowed'}}
        monkeypatch.setattr(adaptive_routes, 'run_assessment', assess)
    routing.run()
    role = saved(routing)['roles']['implementer']
    assert role['primary'] is None and not role['fallbacks']
    assert role['status'] == 'ABSTAINED'


def test_interruption_never_repeats_pending_inference(routing, monkeypatch):
    def interrupted(*a, **kw):
        pending = saved(routing)
        assert pending['status'] == 'UNKNOWN'
        assert pending['roles']['implementer']['assessments'][0]['status'] == 'UNKNOWN'
        raise KeyboardInterrupt
    monkeypatch.setattr(adaptive_routes, 'run_assessment', interrupted)
    with pytest.raises(KeyboardInterrupt):
        routing.run()
    monkeypatch.setattr(adaptive_routes, 'run_assessment', lambda *a, **kw: pytest.fail('repeated inference'))
    assert routing.run()['status'] == 'UNKNOWN'


@pytest.mark.parametrize('change', ['mode', 'count', 'nan', 'audit_escalation'])
def test_policy_rejects_unbounded_or_unsafe_suggestions(routing, change):
    if change == 'mode': routing.policy['mode'] = 'automatic'
    elif change == 'count': routing.policy['candidates']['implementer'] *= 2
    elif change == 'nan': routing.policy['total_timeout_seconds'] = float('nan')
    else: routing.policy['candidates'] = {'auditor': [{'deployment': 'one', 'purpose': 'escalation'}]}
    assert routing.run()['status'] == 'FAILED'
    assert not routing.calls


def test_explicit_implementation_stays_explicit_when_audit_is_auto(routing):
    routing.run(automatic=False, audit={'selection': {'automatic': True}})
    assert not routing.calls
    assert saved(routing)['roles']['implementer']['reason'] == 'EXPLICIT_ROLE_SELECTION'


def test_tariff_and_partial_quality_projection_keep_unknowns_explicit():
    facts = {'context_window': 10, 'quality': {'status': 'SUPPLIED_EVIDENCE', 'records': [{'scope': 'a'}, {'scope': 'b'}]},
             'price': {'status': 'CONFIGURED_TARIFF', 'currency': 'USD',
                       'rates_json': '{"cache_read_tokens": null, "output_tokens": "2"}'},
             'quota': {'status': 'UNKNOWN'}, 'resource': {'status': 'NOT_CHECKED'}}
    projected = route_plan.compact_facts(facts)
    assert projected['price']['per_million_tokens']['cache_read_tokens'] is None
    assert projected['quality']['omitted_records'] == 1
    assert projected['quality']['records'][0]['source_ref'] == 'quality.records[0]'
    assert projected['quota']['status'] == 'UNKNOWN'


def test_helper_cannot_promote_unknown_quality_even_if_it_refuses_to_abstain(routing, monkeypatch):
    def collect(deployment, **kwargs):
        facts = routing.collect(deployment)
        facts['quality'] = {'status': 'UNKNOWN', 'records': []}
        return facts
    monkeypatch.setattr(route_facts, 'collect', collect)
    routing.run()
    role = saved(routing)['roles']['implementer']
    assert role['primary'] is None and role['reason'] == 'CHOICE_LACKS_QUALITY_EVIDENCE'
    assert role['assessments'][0]['assessment']['answer']['choice'] == 'candidate_2'
