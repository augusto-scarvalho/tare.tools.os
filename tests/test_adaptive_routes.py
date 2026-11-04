import json
from types import SimpleNamespace

import pytest

from relay import adaptive_routes, native_catalog, native_routes, work_run
from test_native_routes import candidates
from test_work_run import os_root
from test_work_resume import host, run
from test_work_audit import reviewers


@pytest.fixture
def policy(tmp_path, monkeypatch):
    path = tmp_path / 'policy.json'
    path.write_text(json.dumps({'schema': 'tare.tools/adaptive-route-policy/1', 'mode': 'shadow',
                               'command': ['unused'], 'preferences': {}}))
    monkeypatch.setenv('TARE_ADAPTIVE_ROUTE_POLICY', str(path))
    return path


def test_disabled_and_explicit_selection_cost_nothing(tmp_path, monkeypatch, policy):
    monkeypatch.setattr(adaptive_routes, 'run_assessment', lambda *a, **k: pytest.fail('unexpected inference'))
    cancel = SimpleNamespace(is_cancelled=lambda: False)
    assert adaptive_routes.observe_work({}, {}, {}, tmp_path, cancel) is None
    monkeypatch.delenv('TARE_ADAPTIVE_ROUTE_POLICY')
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    assert adaptive_routes.observe_work({}, {}, {}, tmp_path, cancel, automatic_implementation=True) is None


def test_replay_stale_input_and_cancellation(tmp_path, monkeypatch, policy):
    calls = []
    monkeypatch.setattr(adaptive_routes, 'run_assessment', lambda *a, **k: calls.append(a) or {'status': 'FAILED', 'reason': 'ASSESSMENT_TIMEOUT'})
    cancel = SimpleNamespace(is_cancelled=lambda: False)
    def observe(node):
        return adaptive_routes.observe_work(node, {}, {}, tmp_path, cancel, automatic_implementation=True)
    first = observe({'title': 'Change'})
    assert first == observe({'title': 'Change'}) and len(calls) == 1
    assert observe({'title': 'Different'})['reason'] == 'ADAPTIVE_OBSERVATION_UNAVAILABLE'
    saved = tmp_path / 'route-assessment' / 'observation.json'
    receipt = json.loads(saved.read_bytes()); receipt['status'] = 'UNKNOWN'; saved.write_text(json.dumps(receipt))
    assert observe({'title': 'Change'})['status'] == 'UNKNOWN' and len(calls) == 1
    other = tmp_path / 'cancelled'; other.mkdir()
    cancel.is_cancelled = lambda: True
    result = adaptive_routes.observe_work({}, {}, {}, other, cancel, automatic_implementation=True)
    assert result['reason'] == 'CANCELLED' and len(calls) == 1


def test_actual_auto_dispatch_records_observation_without_changing_execution(host, candidates, reviewers, policy, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(adaptive_routes, 'run_assessment', lambda *a, **k: calls.append(a) or {
        'status': 'OBSERVED', 'answer': {'choice': 'reasoning'}, 'elapsed_ms': 1})
    assert run(host, '--auditor-deployment', 'auto', '--max-bytes', '65536') == 0
    result = json.loads(capsys.readouterr().out)
    assert len(calls) == 1
    assert result['adaptive_routing']['applied'] is False
    assert result['adaptive_routing']['qualification'] == 'UNQUALIFIED'
    assert reviewers[1] == ['one']
    assert result['audit_routing']['selection']['generation_calls'] == 0


def test_invalid_policy_does_not_block_work(policy, tmp_path):
    policy.write_text('{broken')
    result = adaptive_routes.observe_work({}, {}, {}, tmp_path, SimpleNamespace(is_cancelled=lambda: False), automatic_implementation=True)
    assert result['status'] == 'FAILED' and result['applied'] is False


def test_route_preferences_cannot_bypass_role_allowlist(monkeypatch):
    monkeypatch.setattr(native_catalog, 'select', lambda _: {'id': 'same-author'})
    with pytest.raises(ValueError, match='NOT_ELIGIBLE'):
        adaptive_routes.proposed_route({'deployment': 'same-author'}, [{'id': 'independent'}])


def test_supported_effort_is_a_new_proposal_without_mutating_original(monkeypatch):
    from copy import deepcopy
    from test_model_choices import profile
    original = profile()
    before = deepcopy(original)
    monkeypatch.setattr(native_catalog, 'select', lambda _: deepcopy(original))
    proposed = adaptive_routes.proposed_route({'deployment': 'claude-cli', 'effort': 'medium'}, [original])
    assert json.loads(proposed['native_profile']['parameters_json'])['effort'] == 'medium'
    assert original == before
    with pytest.raises(ValueError):
        adaptive_routes.proposed_route({'deployment': 'claude-cli', 'effort': 'invented'}, [original])


def test_structured_plan_is_visible_on_work_without_changing_auditor(host, candidates, reviewers, policy, monkeypatch, capsys):
    from copy import deepcopy
    from relay import route_facts
    base_collect = route_facts.collect
    def collect(*args, **kwargs):
        facts = base_collect(*args, **kwargs)
        facts['quality'] = {'status': 'SUPPLIED_EVIDENCE', 'records': [{
            'source': 'fixture-only', 'scope': 'Test routing integration, not model quality'}]}
        return facts
    monkeypatch.setattr(route_facts, 'collect', collect)
    policy.write_text(json.dumps({'schema': 'tare.tools/adaptive-route-policy/2', 'mode': 'shadow',
        'command': ['unused'], 'candidates': {'auditor': [{'deployment': 'one'}, {'deployment': 'two'}]}}))
    profiles = {row['id']: row for row in candidates['deployments']}
    original_select = native_catalog.select
    monkeypatch.setattr(native_catalog, 'select', lambda name=None, *args, **kw:
        deepcopy(profiles[name]) if name in profiles else original_select(name, *args, **kw))
    choices = iter(['candidate_2', 'candidate_1'])
    monkeypatch.setattr(adaptive_routes, 'run_assessment', lambda *a, **kw: {
        'status': 'OBSERVED', 'inference_attempts': 1, 'answer': {
            'choice': next(choices), 'usage': {'input_tokens': 12}}})
    assert run(host, '--auditor-deployment', 'auto', '--max-bytes', '65536') == 0
    result = json.loads(capsys.readouterr().out)
    suggestion = result['adaptive_routing']['route_plan']['roles']['auditor']
    assert suggestion['primary']['deployment'] == 'two'
    assert suggestion['fallback_count'] == 1 and not suggestion['has_escalation']
    assert reviewers[1] == ['one']
    assert result['adaptive_routing']['applied'] is False


def test_structured_auditor_proposal_cannot_admit_producer(host, candidates, reviewers, policy, monkeypatch, capsys):
    from copy import deepcopy
    policy.write_text(json.dumps({'schema': 'tare.tools/adaptive-route-policy/2', 'mode': 'shadow',
        'command': ['unused'], 'candidates': {'auditor': [{'deployment': 'author'}]}}))
    original_select = native_catalog.select
    monkeypatch.setattr(native_catalog, 'select', lambda name=None, *args, **kw:
        deepcopy(candidates['deployments'][0]) if name == 'author' else original_select(name, *args, **kw))
    monkeypatch.setattr(adaptive_routes, 'run_assessment', lambda *a, **kw: pytest.fail('ineligible author must not reach helper'))
    assert run(host, '--auditor-deployment', 'auto', '--max-bytes', '65536') == 0
    result = json.loads(capsys.readouterr().out)
    suggestion = result['adaptive_routing']['route_plan']['roles']['auditor']
    assert suggestion['status'] == 'ABSTAINED' and suggestion['primary'] is None
    assert reviewers[1] == ['one']
