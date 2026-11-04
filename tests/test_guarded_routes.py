import copy
import json
import shutil
import pytest
from compute_plane.choice_assessment import digest
from relay import adaptive_routes, native_catalog, native_routes, route_admission
from test_route_strategy import joint
from test_current_route_profiles import installed


def seed(joint):
    joint.policy.update(mode='guarded', acceptance_floor=.9)
    joint.task['metrics'] = {'routing_cohort': 'fixture-cohort'}
    for index, row in enumerate(joint.policy['strategies']):
        producer = adaptive_routes.proposed_route(row['implementer'], native_routes.ranked('implementer')[0])
        reviewer = adaptive_routes.proposed_route(row['auditor'], native_routes.ranked('auditor', producer)[0])
        pair = digest({'implementer': digest(producer), 'auditor': digest(reviewer)})
        row['evidence'] = {'pair_sha256': pair, 'scope': 'implementation+audit+rework',
            'currency': 'USD', 'source': 'synthetic-fixture', 'observed_at': '2026-09-21',
            'cohort': 'fixture-cohort', 'accepted': 93, 'attempted': 100,
            'total_cost_usd': 200 if index == 0 else 100, 'total_seconds': 10000}


def apply(joint, result):
    base = native_catalog.select('claude-cli'); audit = {'selection': {'automatic': True}}
    return adaptive_routes.admitted_routes(result, joint.task, base, audit, joint.root, joint.cancel)


def test_guarded_pair_applies_exact_effort_and_independent_audit(joint):
    seed(joint)
    result = joint.run()
    assert result['status'] == 'SUGGESTED'
    producer, audit = apply(joint, result)
    assert producer['model'] == 'claude-sonnet-5.5'
    assert producer['native_model_choice']['effort'] == 'medium'
    assert audit['routes'][0]['deployment']['model'] == 'fable-5.1'
    assert audit['mode'] == 'required' and audit['routes'][0]['eligible']
    assert result['applied'] and result['authority'] == 'NONE'
    assert joint.run()['applied']  # custody replay performs no additional inference
    assert len(joint.calls) == 1


@pytest.mark.parametrize('fault', ['unknown', 'cohort', 'no-task-cohort', 'floor', 'one'])
def test_inadequate_evidence_never_spends_inference(joint, fault):
    seed(joint)
    if fault == 'unknown': del joint.policy['strategies'][0]['evidence']
    elif fault == 'cohort': joint.policy['strategies'][0]['evidence']['cohort'] = 'different'
    elif fault == 'no-task-cohort': joint.task.pop('metrics')
    elif fault == 'floor': joint.policy['acceptance_floor'] = .99
    else: joint.policy['strategies'].pop()
    result = joint.run()
    assert result['status'] == 'ABSTAINED' and not joint.calls and not result['applied']


def test_model_cannot_select_more_expensive_pair(joint, monkeypatch):
    seed(joint)
    monkeypatch.setattr(adaptive_routes, 'run_assessment', lambda *a, **kw: {
        'status': 'OBSERVED', 'answer': {'choice': 'strategy_1'}})
    assert joint.run()['status'] == 'ABSTAINED'
    assert joint.saved()['reason'] == 'SUGGESTION_FAILS_EVIDENCE_GATE'


@pytest.mark.parametrize('fault', ['policy', 'task', 'profile', 'cancel', 'evidence'])
def test_revalidation_keeps_atomic_baseline(joint, monkeypatch, fault):
    seed(joint); result = joint.run()
    if fault == 'policy':
        (joint.root/'policy.json').write_text('{}')
    elif fault == 'task': joint.task['title'] = 'changed'
    elif fault == 'cancel': joint.cancel.is_cancelled = lambda: True
    elif fault == 'profile':
        monkeypatch.setattr(native_routes, 'ranked', lambda *a, **kw: ([], {}))
    else:
        path = joint.root/'route-assessment'/'route-strategy.json'; saved = json.loads(path.read_text())
        saved['candidates'][0]['delivery']['usd_per_accepted_delivery'] = 100
        path.write_text(json.dumps(saved))
    producer, audit = apply(joint, result)
    assert producer['id'] == 'claude-cli'
    assert audit == {'selection': {'automatic': True}}
    assert not result['applied']


def test_missing_cost_cannot_be_hidden_by_removing_candidate():
    rows = [{'key': 'first', 'delivery': {'status': 'SUPPLIED_COHORT', 'cohort': 'same',
        'accepted': 94, 'attempted': 100, 'usd_per_accepted_delivery': 1,
        'seconds_per_accepted_delivery': 100}}, {'key': 'second', 'delivery': {'status': 'UNKNOWN'}}]
    with pytest.raises(ValueError, match='MISSING'):
        route_admission.eligible_winners(rows, cohort='same', acceptance_floor=.9)


def test_default_policy_and_explicit_off(tmp_path, monkeypatch):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path)); monkeypatch.delenv('TARE_ADAPTIVE_ROUTE_POLICY', raising=False)
    path = tmp_path/'assessment-policies/default.json'
    path.parent.mkdir(parents=True); path.write_text('{}')
    assert adaptive_routes.configured_policy() == str(path)
    monkeypatch.setenv('TARE_ADAPTIVE_ROUTE_POLICY', 'off')
    assert adaptive_routes.configured_policy() is None


@pytest.mark.parametrize('cohort', ['fixture-cohort', 'other-cohort'])
def test_measured_selection_needs_no_model_and_still_checks_cohort(joint, monkeypatch, cohort):
    from relay import delivery_comparison
    seed(joint)
    joint.policy['selection'] = 'measured_delivery'
    study = joint.root/'study.json'
    study.write_text(json.dumps({'candidates': [{'id': 'first'}, {'id': 'second'}]}))
    joint.policy['delivery_study'] = {'path': str(study), 'winner': 'second'}
    joint.task['metrics']['routing_cohort'] = cohort
    checks = []
    # Receipt binding is exercised by test_delivery_comparison; here use real
    # admission, live eligibility and the deterministic selection branch.
    monkeypatch.setattr(delivery_comparison, 'validate_evidence', lambda p: checks.append(p['selection']))
    result = joint.run()
    assert not joint.calls and checks == ['measured_delivery']
    if cohort == 'other-cohort':
        assert result['status'] == 'ABSTAINED'
        return
    producer, audit = apply(joint, result)
    assert result['applied'] and producer['model'] == 'claude-sonnet-5.5'
    assert len(checks) == 2
    def changed(policy):
        raise ValueError('DELIVERY_COMPARISON_CHANGED')
    monkeypatch.setattr(delivery_comparison, 'validate_evidence', changed)
    producer, audit = apply(joint, result)
    assert producer['id'] == 'claude-cli' and not result['applied']


def test_installed_control_root_uses_packaged_roster(tmp_path, monkeypatch):
    from relay import relay_mesh, work_audit
    monkeypatch.setattr(relay_mesh, 'RELAY_DIR', tmp_path)
    assert native_routes.configured()['deployments']
    profile = {'id': 'test', 'model': 'gpt-6-astra', 'native_profile': {'model': 'gpt-6-astra'}}
    assert work_audit.identity(profile, 'auditor')
    (tmp_path/'round_table_roster.json').write_text('invalid-json')
    assert native_catalog.roster_path() == tmp_path/'round_table_roster.json'
    with pytest.raises((ValueError, KeyError)):
        native_routes.configured()
