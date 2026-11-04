from copy import deepcopy
import json

import pytest

from relay.execution_team import reasoning
from relay.model_choices import describe
from relay.planner_policy import choices, effective
from test_model_choices import profile
from test_native_agent import setup  # noqa: F401


@pytest.mark.parametrize('endpoint,model', [('cli://codex', 'gpt-6.1-sol'), ('cli://claude', 'claude-opus-5-5')])
def test_omission_raises_low_to_high_without_mutating_frozen_coordinator_or_planner(endpoint, model):
    original = profile(endpoint, model, 'low'); before = deepcopy(original)
    selected = effective(original)
    assert reasoning(selected) == 'high' and original == before
    assert selected['native_profile']['model'] == original['native_profile']['model']


@pytest.mark.parametrize('level', ['low', 'medium', 'minimal', 'none'])
def test_explicit_below_floor_is_rejected_before_dispatch(level):
    with pytest.raises(ValueError, match='minimum reasoning is high'):
        effective(profile(), level)


@pytest.mark.parametrize('level', ['high', 'xhigh', 'max'])
def test_higher_configured_reasoning_keeps_exact_profile_bytes(level):
    original = profile(effort=level)
    assert effective(original) == original


def test_agy_floor_changes_both_effort_and_registered_wire_model():
    original = profile('cli://agy', 'gemini-4-argon-low', 'low')
    original['native_reasoning_models'] = {'low': 'gemini-4-argon-low', 'high': 'gemini-4-argon-high'}
    selected = effective(original)
    assert selected['native_profile']['model'] == 'gemini-4-argon-high'
    assert reasoning(selected) == 'high' and reasoning(original) == 'low'


@pytest.mark.parametrize('protocol,parameters', [
    ('bedrock-converse', {'additionalModelRequestFields': {'reasoning_config': 'high'}}),
    ('anthropic-messages', {'output_config': {'effort': 'high'}, 'thinking': {'type': 'adaptive'}})])
def test_existing_high_api_profile_remains_eligible_in_planner_picker(protocol, parameters):
    dep = {'id': 'existing-high', 'native_profile': {'protocol': protocol, 'model': 'claude-opus-5-5',
           'endpoint': 'https://example.invalid', 'parameters_json': json.dumps(parameters)}}
    item = describe(dep['id'], dep)
    values = [{k: item[k] for k in ('key', 'label', 'group')} | {'routes': [item['route']]}]
    assert choices(values)[0]['routes'][0]['default_effort'] == 'high'
    assert effective(dep) == dep


@pytest.mark.parametrize('level', ['high', 'xhigh', 'max'])
def test_shipped_claude_api_uses_top_level_effort_without_changing_cache_or_thinking_settings(level):
    from relay.native_catalog import select
    dep = select('claude-api-fable-5-1')
    original = deepcopy(dep)
    selected = effective(dep, level)
    before = json.loads(dep['native_profile']['parameters_json'])
    after = json.loads(selected['native_profile']['parameters_json'])
    assert after == {**before, 'output_config': {'effort': level}}
    assert reasoning(selected) == level and dep == original


def test_picker_removes_low_levels_and_incapable_connections_without_changing_coordinator_options():
    item = describe('codex', profile('cli://codex', 'gpt-6.1-sol', 'low'))
    values = [{k: item[k] for k in ('key', 'label', 'group')} | {'routes': [item['route']]}]
    before = deepcopy(values)
    result = choices(values)
    assert result[0]['routes'][0]['efforts'] == ['high', 'max']
    assert result[0]['routes'][0]['default_effort'] == 'high' and values == before
    values[0]['routes'][0]['efforts'] = ['low', 'medium']
    assert choices(values) == []
    values[0]['routes'][0].update(efforts=['max'], default_effort='max')
    assert choices(values)[0]['routes'][0]['default_effort'] == 'max'


def test_legacy_low_planner_resume_refuses_before_inference_and_preserves_its_exact_history(setup, monkeypatch):
    from relay import native_agent, planner_policy
    from test_native_agent import Client, deployment
    from test_native_artifacts import payload
    config, custody, root = setup
    config.update(role='planner', typed_output=True, writable={}, verifiers={})
    dep = deployment()
    params = json.loads(dep['native_profile']['parameters_json'])
    params['chat_template_kwargs']['reasoning_effort'] = 'low'
    dep['native_profile']['parameters_json'] = json.dumps(params)
    first = Client(steps=[('read', {'repository': 'repo', 'path': 'a.py'}), ('tare_deliver', payload('planner', root))])
    # Produce a faithful old-release fixture, then restore today's dispatch policy.
    with monkeypatch.context() as old:
        old.setattr(planner_policy, 'effective', lambda value, requested=None: deepcopy(value))
        outcome = native_agent.run_session(config, dep, custody, 'Plan the bounded change.', client=first, local_counter=first)
    assert outcome['status'] == 'PROPOSED' and first.generations == 2
    before = {p: p.read_bytes() for p in custody.rglob('*') if p.is_file()}
    resumed = Client(steps=[])
    with pytest.raises(ValueError, match='minimum high reasoning'):
        native_agent.run_session(None, None, custody, resume=True, client=resumed, local_counter=resumed)
    assert resumed.generations == 0
    assert before == {p: p.read_bytes() for p in custody.rglob('*') if p.is_file()}
