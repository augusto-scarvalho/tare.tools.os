"""OpenRouter profiles use the existing native loop and immutable model picker."""
from copy import deepcopy
import json

import pytest

from relay import model_choices, native_agent, native_catalog
from test_native_agent import Client, setup, steps
from test_model_choices import catalog


NAMES = ['openrouter-deepseek-v4-1-flash', 'openrouter-glm-5-3']


@pytest.mark.parametrize('name', NAMES)
def test_openrouter_picker_materializes_supported_reasoning_without_changing_base(name, monkeypatch, tmp_path):
    base = native_catalog.select(name)
    original = deepcopy(base)
    choices = catalog(monkeypatch, {name: base})
    item = choices[0]
    assert item['group'] == 'OpenRouter'
    assert item['routes'][0]['label'] == 'OpenRouter API'
    assert item['routes'][0]['efforts'] == ['low', 'high', 'max']
    for effort in ['low', 'high', 'max']:
        chosen = {'key': base['model'], 'connection': name, 'reasoning': effort}
        value = model_choices.realization(choices, chosen)
        params = json.loads(value['native_profile']['parameters_json'])
        assert params['reasoning']['effort'] == effort
        assert params['provider'] == json.loads(original['native_profile']['parameters_json'])['provider']
        assert params['transforms'] == []
        assert value['native_credential_env'] == 'OPENROUTER_API_KEY'
        assert value['native_profile']['endpoint'] == 'https://openrouter.ai/api/v1'
        assert value['native_profile']['model'] == original['model']
    assert base == original
    for effort in ['none', 'medium', 'xhigh']:
        with pytest.raises(ValueError):
            model_choices.materialize(choices, {'key': base['model'], 'connection': name,
                                               'reasoning': effort}, tmp_path)
    assert not (tmp_path/'model-selections').exists()


@pytest.mark.parametrize('name', [*NAMES, 'openrouter-mimo-v2-6-pro'])
def test_openrouter_native_read_edit_verify_preserves_reasoning_and_tool_history(name, setup):
    config, custody, root = setup
    config['capabilities'] = ['read', 'edit', 'verify']
    deployment = native_catalog.select(name)
    details = [{'type': 'reasoning.encrypted', 'data': 'opaque-fixture',
                'id': 'reasoning-1', 'format': 'unknown', 'index': 0}]

    class OpenRouterFixture(Client):
        endpoint = deployment['native_profile']['endpoint']

        def post(self, path, body, control):
            assert path == '/chat/completions'
            assert body['model'] == deployment['model']
            response = super().post(path, body, control)
            response['model'] = deployment['model']
            response['usage']['prompt_tokens_details']['cache_write_tokens'] = 0
            response['choices'][0]['message']['reasoning_details'] = details
            return response

    client = OpenRouterFixture(steps=steps())
    result = native_agent.run_session(config, deployment, custody, 'Change and verify a.py', client=client)
    assert result['status'] == 'PROPOSED' and result['model_calls'] == 4
    assert (root/'a.py').read_text() == 'value = 2\n'
    assert result['context_counting'] == 'estimated'
    assert client.calls[1][1]['messages'][-3]['reasoning_details'] == details
    assert client.calls[1][1]['messages'][-2]['role'] == 'tool'
    assert '15 model calls remain' in client.calls[1][1]['messages'][-1]['content']
    for (_, before), (_, after) in zip(client.calls, client.calls[1:]):
        assert before['session_id'] == after['session_id']
        assert json.dumps(before['tools']) == json.dumps(after['tools'])
        history = before['messages'][:-1]  # The final budget notice changes each call.
        assert history == after['messages'][:len(history)]
        assert before['messages'][-1] != after['messages'][-1]
    resumed = native_agent.run_session(None, None, custody, resume=True, client=client)
    assert resumed['status'] == 'PROPOSED' and len(client.calls) == 4


def test_lookalike_endpoint_is_not_presented_as_openrouter():
    value = native_catalog.select(NAMES[0])
    value['native_profile']['endpoint'] = 'https://openrouter.ai.example/api/v1'
    item = model_choices.describe('custom', value)
    assert item['group'] != 'OpenRouter' and not item['route']['efforts']


def test_mimo_uses_verified_identity_and_profile_reasoning_without_invented_efforts(monkeypatch, tmp_path):
    from relay.model_evidence import references
    from relay.landing_policy import model_id_for_runtime
    from pathlib import Path
    name = 'openrouter-mimo-v2-6-pro'
    base = native_catalog.select(name)
    choices = catalog(monkeypatch, {name: base})
    item, = choices
    assert item['label'] == 'MiMo V2.6 Pro' and item['group'] == 'OpenRouter'
    assert item['routes'][0]['efforts'] == []
    chosen = {'key': base['model'], 'connection': name, 'reasoning': 'default'}
    value = model_choices.realization(choices, chosen)
    params = json.loads(value['native_profile']['parameters_json'])
    assert params['reasoning'] == {'enabled': True}
    assert params['provider']['require_parameters'] and not params['provider']['allow_fallbacks']
    assert params['provider']['only'] == ['xiaomi/fp8']
    assert params['transforms'] == []
    assert base['native_profile']['context_window'] == 1048576
    assert base['native_profile']['output_tokens'] == 16384
    assert base['native_usage_contract']['layout'] == 'openrouter-inclusive'
    assert model_id_for_runtime(Path(__file__).parents[1]/'relay/round_table_roster.json',
                                'native', base['model']) == base['model']
    evidence = references(base, 'profile-defined')
    assert not evidence['records']  # No invented quality score or frontier designation.
    assert evidence['price_references'][0]['per_million_tokens']['cache_read_tokens'] == '0.0036'
    with pytest.raises(ValueError):
        model_choices.materialize(choices, {**chosen, 'reasoning': 'max'}, tmp_path)
