"""A real native researcher loop over fixture HTTP, without project or live web access."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from relay import native_catalog, native_generation, research_api, web_research
from relay.research_cli import REPORT
from tare_tools_agent_runtime import BindingRef, CancellationToken, ExecutionControl

ADDRESS = 'https://example.org/reference'
REPORT_VALUE = {'status': 'COMPLETED', 'summary': 'The documented value is five.', 'remaining': [],
    'sources': [{'url': ADDRESS, 'title': 'Reference'}],
    'findings': [{'url': ADDRESS, 'claim': 'The value is five.', 'anchor': 'value is five', 'kind': 'observed'}]}


@pytest.fixture
def api(monkeypatch):
    dep = native_catalog.select('openrouter-mimo-v2-6-pro')
    bodies = []
    class Http:
        endpoint = dep['native_profile']['endpoint']
        def post(self, path, body, control):
            bodies.append(deepcopy(body))
            message = {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'report-'+str(len(bodies)), 'type': 'function',
                'function': {'name': 'tare_deliver', 'arguments': json.dumps(REPORT_VALUE)}}]}
            finish = 'tool_calls'
            if len(bodies) == 1:
                message = {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'read-1', 'type': 'function',
                    'function': {'name': 'read_url', 'arguments': json.dumps({'url': ADDRESS})}}]}
                finish = 'tool_calls'
            return {'model': dep['model'], 'provider': 'Fixture Host',
                'choices': [{'message': message, 'finish_reason': finish}],
                'usage': {'prompt_tokens': 100, 'completion_tokens': 10,
                          'prompt_tokens_details': {'cached_tokens': 0 if len(bodies) == 1 else 80}}}
    monkeypatch.setattr(native_generation, 'inference_client', lambda *a, **k: Http())
    monkeypatch.setattr(native_generation, 'vault_secret', lambda _: 'fixture-secret')
    monkeypatch.setattr(web_research, 'read_page', lambda address, *a, **k: {
        'url': address, 'status': 'OK', 'text': 'The value is five.', 'fetched_at': 1, 'sha256': 'fixture'})
    control = ExecutionControl(BindingRef('parent', 'binding', 'owner', 1, 'opaque'), CancellationToken(), 100000, monotonic=lambda: 0)
    return dep, bodies, control


def test_api_research_uses_only_web_tools_then_reuses_native_history_and_cache_identity(api, tmp_path):
    dep, bodies, control = api
    first = research_api.research(dep, 'Find the value.', root=tmp_path, control=control, max_calls=3)
    assert first['status'] == 'COMPLETED', first
    assert first['report'] == REPORT_VALUE and first['calls_used'] == 2
    second = research_api.research(dep, 'What was the value?', root=tmp_path, control=control, max_calls=3)
    assert second['status'] == 'COMPLETED' and second['session_reused'], second
    assert second['calls_used'] == 1
    for body in bodies:
        assert {t['function']['name'] for t in body['tools']} == {'read_url', 'search_web', 'tare_deliver'}
        assert body['reasoning'] == {'enabled': True}
        assert body['session_id'] == bodies[0]['session_id']
    assert bodies[0]['messages'][0:2] == bodies[-1]['messages'][0:2]
    assert any(m['role'] == 'tool' for m in bodies[-1]['messages'])
    assert second['usage']['reported_calls'][0]['prompt_tokens_details']['cached_tokens'] == 80


def test_api_research_respects_parent_call_cap_without_spawning_a_second_model(api, tmp_path):
    dep, bodies, control = api
    result = research_api.research(dep, 'Find the value.', root=tmp_path, control=control, max_calls=1)
    assert result['status'] == 'FAILED' and result['error'] == 'RESEARCH_MODEL_CALL_LIMIT', result
    assert len(bodies) == result['calls_used'] == 1


def test_api_research_requires_observed_web_use_even_if_report_is_well_formed(api, monkeypatch, tmp_path):
    dep, bodies, control = api
    bodies.extend([{}])  # The fixture now answers directly, without any web tool.
    result = research_api.research(dep, 'Find the value.', root=tmp_path, control=control, max_calls=3)
    assert result['status'] == 'FAILED' and result['error'] == 'ValueError', result


def test_interrupted_api_research_preserves_history_and_does_not_replay_the_old_call(api, monkeypatch, tmp_path):
    dep, bodies, control = api
    original = native_generation.inference_client
    class Interrupted:
        endpoint = dep['native_profile']['endpoint']
        def post(self, *args):
            raise KeyboardInterrupt('fixture owner stopped')
    monkeypatch.setattr(native_generation, 'inference_client', lambda *a, **k: Interrupted())
    with pytest.raises(KeyboardInterrupt):
        research_api.research(dep, 'The interrupted question.', root=tmp_path, control=control, max_calls=3)
    monkeypatch.setattr(native_generation, 'inference_client', original)
    result = research_api.research(dep, 'A new question after interruption.', root=tmp_path, control=control, max_calls=3)
    assert result['status'] == 'COMPLETED' and result['session_reused'], result
    journal = next((tmp_path/'research-connections').glob('*/journal.jsonl'))
    events = [json.loads(line) for line in journal.read_text().splitlines()]
    # Custody records retain the unknown call; recovery never claims that it cost zero.
    assert 'model_abandoned' in journal.read_text()
    assert len(bodies) == result['calls_used'] == 2
