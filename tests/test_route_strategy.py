"""Exercise the real role policy and host-profile resolution, with no vendor calls."""
import copy
import json
from types import SimpleNamespace

import pytest

from compute_plane.choice_assessment import digest
from relay import adaptive_routes, native_catalog, native_routes, route_facts, route_strategy
from test_current_route_profiles import installed
from test_model_choices import profile


@pytest.fixture
def joint(installed, monkeypatch):
    codex = profile('cli://codex', 'gpt-6-astra', 'high')
    codex['id'] = 'codex-cli'
    (installed/'codex-cli.json').write_text(json.dumps(codex))
    original = route_facts.collect
    def collect(value, **kwargs):
        facts = original(value, **kwargs)
        # Explicitly authored fixture evidence, never a real qualification claim.
        facts['quality'] = {'status': 'SUPPLIED_EVIDENCE', 'records': [{
            'kind': 'local-evaluation', 'metric': 'fixture-score', 'value': 90,
            'scope': 'Synthetic fixture only.'}]}
        return facts
    monkeypatch.setattr(route_facts, 'collect', collect)
    policy = {'schema': 'tare.tools/adaptive-route-policy/3', 'mode': 'shadow',
              'strategies': [
                  {'implementer': {'deployment': 'claude-cli', 'effort': 'low'},
                   'auditor': {'deployment': 'codex-cli', 'effort': 'high'}},
                  {'implementer': {'deployment': 'claude-sonnet-5-5-cli', 'effort': 'medium'},
                   'auditor': {'deployment': 'claude-fable-5-1-cli', 'effort': 'high'}}]}
    path = installed/'policy.json'
    monkeypatch.setenv('TARE_ADAPTIVE_ROUTE_POLICY', str(path))
    calls = []
    def assess(command, request, **kwargs):
        calls.append(copy.deepcopy(request))
        return {'status': 'OBSERVED', 'inference_attempts': 1,
                'answer': {'choice': 'strategy_2', 'usage': {'input_tokens': 120}}}
    monkeypatch.setattr(adaptive_routes, 'run_assessment', assess)
    cancel = SimpleNamespace(is_cancelled=lambda: False)
    task = {'title': 'Fix bounded input validation', 'exit_criteria': ['Reject malformed input']}
    def run(automatic=True, audit=None):
        path.write_text(json.dumps(policy))
        return adaptive_routes.observe_work(task, native_catalog.select('claude-cli'),
            {'selection': {'automatic': True}} if audit is None else audit, installed, cancel,
            automatic_implementation=automatic)
    def saved():
        return json.loads((installed/'route-assessment'/'route-strategy.json').read_bytes())
    return SimpleNamespace(policy=policy, calls=calls, run=run, saved=saved,
                           root=installed, collect=collect, cancel=cancel, task=task)


def test_pair_is_atomic_effort_specific_shadow_and_replayable(joint, monkeypatch):
    result = joint.run()
    saved = joint.saved()
    assert result['status'] == 'SUGGESTED' and result['applied'] is False
    assert saved['primary']['routes']['implementer']['model'] == 'claude-sonnet-5.5'
    assert saved['primary']['routes']['implementer']['effort'] == 'medium'
    assert saved['primary']['routes']['auditor']['model'] == 'fable-5.1'
    assert saved['qualification'] == 'UNQUALIFIED' and saved['authority'] == 'NONE'
    assert saved['fallback'] == 'KEEP_BASELINE' and 'REVISE' in saved['stop_on']
    request = joint.calls[0]
    assert 'claude-sonnet-5.5@medium + fable-5.1@high' in request['options']['strategy_2']
    assert all(c['delivery'] == {'status': 'UNKNOWN'} for c in json.loads(request['state'])['strategies'])
    artifact = json.loads((joint.root/'route-assessment'/saved['primary']['facts_path']).read_bytes())
    assert digest(artifact) == saved['primary']['facts_sha256']
    monkeypatch.setattr(route_facts, 'collect', lambda *a, **kw: pytest.fail('replay probe'))
    assert joint.run() == result and len(joint.calls) == 1
    assert len(json.dumps(adaptive_routes.summary(result, joint.root))) < 2200
    joint.policy['strategies'][0]['implementer']['effort'] = 'high'
    assert joint.run()['status'] == 'FAILED'  # Changed custody binding never reuses a decision.


@pytest.mark.parametrize('fault', ['opus-auditor', 'astra-implementer', 'effort', 'duplicate', 'gpu', 'quota', 'quality', 'evidence'])
def test_invalid_pair_is_removed_before_inference(joint, monkeypatch, fault):
    first = joint.policy['strategies'][0]
    if fault == 'opus-auditor': first['auditor']['deployment'] = 'claude-cli'
    elif fault == 'astra-implementer': first['implementer']['deployment'] = 'codex-cli'
    elif fault == 'effort': first['implementer']['effort'] = 'invented'
    elif fault == 'duplicate': joint.policy['strategies'][1] = copy.deepcopy(first)
    elif fault == 'evidence': first['evidence'] = {'pair_sha256': 'stale'}
    else:
        def collect(value, **kwargs):
            facts = joint.collect(value, **kwargs)
            if value['model'] == 'claude-opus-5.5':
                if fault == 'gpu': facts['resource'] = {'status': 'BUSY', 'immediate': False, 'reason': 'GPU_LEASE_HELD'}
                elif fault == 'quota': facts['quota'] = {'status': 'EXHAUSTED'}
                else: facts['quality'] = {'status': 'UNKNOWN', 'records': []}
            return facts
        monkeypatch.setattr(route_facts, 'collect', collect)
    joint.run()
    result = joint.saved()
    assert len(result['exclusions']) == 1
    excluded = result['exclusions'][0]['key']
    assert excluded not in joint.calls[0]['options']
    assert len(json.loads(joint.calls[0]['state'])['strategies']) == 1


def test_independence_uses_proposed_pair_not_baseline(joint, monkeypatch):
    # Deliberately make the eligibility provider permissive; the post-effort
    # identity guard must still reject reviewing the proposed author's family.
    original = native_routes.ranked
    def ranked(role, producer=None, **kwargs):
        if role == 'auditor':
            return [native_catalog.select('claude-cli')], {'candidates': []}
        return original(role, producer, **kwargs)
    monkeypatch.setattr(native_routes, 'ranked', ranked)
    joint.policy['strategies'] = [joint.policy['strategies'][0]]
    joint.policy['strategies'][0]['auditor']['deployment'] = 'claude-cli'
    joint.run()
    assert joint.saved()['exclusions'][0]['reason'] == 'AUDITOR_NOT_INDEPENDENT'
    assert not joint.calls


@pytest.mark.parametrize('fault', ['explicit-implementation', 'explicit-audit', 'cancel', 'failure', 'abstain', 'invalid', 'oversize'])
def test_no_pair_proposal_on_incomplete_or_explicit_selection(joint, monkeypatch, fault):
    if fault == 'explicit-implementation': joint.run(automatic=False)
    elif fault == 'explicit-audit': joint.run(audit={})
    elif fault == 'cancel':
        joint.cancel.is_cancelled = lambda: True
        assert joint.run()['status'] == 'SKIPPED'
        assert not joint.calls
        return
    elif fault == 'oversize':
        joint.task['title'] = 'x' * 9000
        joint.run()
        assert not joint.calls
    else:
        def assess(*args, **kwargs):
            if fault == 'failure': return {'status': 'FAILED', 'reason': 'ASSESSMENT_TIMEOUT'}
            return {'status': 'OBSERVED', 'answer': {'choice': 'insufficient' if fault == 'abstain' else 'fake'}}
        monkeypatch.setattr(adaptive_routes, 'run_assessment', assess)
        joint.run()
    assert joint.saved()['primary'] is None
    assert joint.saved()['fallback'] == 'KEEP_BASELINE'


def test_pending_pair_observation_never_repeats_inference(joint, monkeypatch):
    def interrupt(*args, **kwargs):
        assert joint.saved()['assessment']['status'] == 'UNKNOWN'
        raise KeyboardInterrupt
    monkeypatch.setattr(adaptive_routes, 'run_assessment', interrupt)
    with pytest.raises(KeyboardInterrupt): joint.run()
    monkeypatch.setattr(adaptive_routes, 'run_assessment', lambda *a, **kw: pytest.fail('repeated'))
    assert joint.run()['status'] == 'UNKNOWN'


def test_historical_delivery_cost_includes_failed_attempts_and_binds_efforts():
    record = {'pair_sha256': 'pair', 'scope': 'implementation+audit+rework', 'currency': 'USD',
              'source': 'fixture-only', 'observed_at': '2026-09-21', 'cohort': 'synthetic-bounded',
              'accepted': 8, 'attempted': 10, 'total_cost_usd': 4, 'total_seconds': 800}
    result = route_strategy.delivery_evidence(record, 'pair')
    assert result['usd_per_accepted_delivery'] == .5
    assert result['seconds_per_accepted_delivery'] == 100
    failed = route_strategy.delivery_evidence(dict(record, accepted=0), 'pair')
    assert failed['accepted'] == 0 and failed['usd_per_accepted_delivery'] is None
    for key, bad in [('pair_sha256', 'other-effort'), ('accepted', -1), ('accepted', 11),
                     ('total_cost_usd', float('nan')), ('scope', 'implementation-only')]:
        with pytest.raises(ValueError, match='STRATEGY_EVIDENCE_INVALID'):
            route_strategy.delivery_evidence({**record, key: bad}, 'pair')


def test_real_policy_reserves_planning_and_audit_for_frontier(installed):
    policy = native_routes.configured()
    frontiers = {'gpt-6.1-sol','gpt-6-astra','claude-opus-5.5','fable-5.1',
                 'gemini-4-argon-high','gemini-4-argon', 'moonshotai/kimi-k3.1-pro', 'meta/muse-wonder'}
    for row in policy['deployments']:
        assert ('auditor' in row['roles']) == (row['model'] in frontiers)
        assert ('planner' in row['roles']) == ('auditor' in row['roles'])
    # A model may be admitted to both roles; a concrete producer cannot audit
    # itself or another model of its lineage, regardless of that admission.
    from relay import native_catalog, work_audit
    producer = native_catalog.select('claude-cli')
    author = work_audit.identity(producer,'implementer')
    selected, _ = native_routes.ranked('auditor',producer)
    assert selected
    assert all(work_audit.identity(value,'auditor').model_id != author.model_id and
               work_audit.identity(value,'auditor').bias_domain != author.bias_domain for value in selected)
    openrouter = [r for r in policy['deployments'] if r['id'].startswith('openrouter-')]
    assert len(openrouter) == 13  # Twelve current choices and the preserved legacy DeepSeek alias.
    assert {r['model'] for r in openrouter if 'auditor' in r['roles']} == {
        'moonshotai/kimi-k3.1-pro', 'meta/muse-wonder'}
    assert all('implementer' in r['roles'] for r in openrouter if r['model'] not in frontiers)


@pytest.mark.parametrize('fault', ['mode', 'count', 'nan', 'role'])
def test_invalid_policy_does_not_call_helper(joint, fault):
    if fault == 'mode': joint.policy['mode'] = 'automatic'
    elif fault == 'count': joint.policy['strategies'] *= 2
    elif fault == 'nan': joint.policy['total_timeout_seconds'] = float('nan')
    else: del joint.policy['strategies'][0]['auditor']
    assert joint.run()['status'] == 'FAILED' and not joint.calls


def test_shared_budget_bounds_call_and_cancellation_discards_answer(joint, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(route_strategy.time, 'monotonic', lambda: clock[0])
    joint.policy['total_timeout_seconds'] = 5
    def assess(command, request, **kwargs):
        assert kwargs['timeout_seconds'] == 5
        joint.cancel.is_cancelled = lambda: True
        return {'status': 'OBSERVED', 'answer': {'choice': 'strategy_1'}}
    monkeypatch.setattr(adaptive_routes, 'run_assessment', assess)
    joint.run()
    assert joint.saved()['primary'] is None and joint.saved()['reason'] == 'CANCELLED'
