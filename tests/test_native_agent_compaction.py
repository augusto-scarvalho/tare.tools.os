"""Host budgets admit repeated compaction without resetting session limits."""
import json

import pytest

from compute_plane.native_models import NativeModelAdapter
from relay import native_agent
from tare_tools_agent_runtime import ModelReply
from test_native_agent import Client, deployment, setup, steps


@pytest.fixture
def growing_context(monkeypatch):
    class GrowingAdapter(NativeModelAdapter):
        def count(self, request, control):
            value = super().count(request, control)
            turns = sum(e['kind'] == 'model_returned' for e in request.context.events())
            return value | {'input_tokens': 7000 if request.purpose == 'model' and turns >= 2 else 80}

        def generate(self, request, control):
            if request.purpose == 'summary':
                return ModelReply(text='Continue the recorded edit and named verifier; source: a.py.')
            return super().generate(request, control)

    monkeypatch.setattr('compute_plane.native_models.NativeModelAdapter', GrowingAdapter)


@pytest.mark.parametrize('calls,cap,reason,summaries', [
    (6, None, None, 2),
    (5, None, None, 1),  # The final call fits the hard window; another summary would strand delivery.
    (6, 1, 'COMPACTION_BUDGET', 1),
])
def test_edit_verify_delivery_survives_two_compactions_with_original_limits(
        setup, growing_context, calls, cap, reason, summaries):
    config, custody, root = setup
    config['capabilities'] = ['read', 'edit', 'verify']
    if cap is not None:
        config['context_policy']['max_compactions'] = cap
    client = Client(steps=steps())
    result = native_agent.run_session(config, deployment(), custody, 'Change and verify a.py',
        client=client, local_counter=client, limits={'max_model_calls': calls})
    assert (root/'a.py').read_text() == 'value = 2\n'
    assert result['status'] == ('PROPOSED' if reason is None else 'BLOCKED')
    if reason is not None:
        assert result['reason'] == reason
    assert result['model_calls'] == (calls if reason is None else 4)
    events = [json.loads(row)['event'] for row in (custody/'journal.jsonl').read_text().splitlines()]
    assert sum(e['kind'] == 'context_activated' for e in events) == summaries
    assert any(e['kind'] == 'effect_returned' and json.loads(e['result']['output_json']).get('returncode') == 0
               for e in events)
    state = json.loads((custody/'native-agent.json').read_text())
    original_deadline = state['deadline_utc']
    assert state['context_compaction_budget'] == calls - 1
    resumed = native_agent.run_session(None, None, custody, resume=True, client=client, local_counter=client)
    assert resumed['model_calls'] == result['model_calls']
    assert resumed['status'] == result['status']
    assert json.loads((custody/'native-agent.json').read_text())['deadline_utc'] == original_deadline


def test_legacy_session_without_host_checkpoint_flags_keeps_its_policy(setup, monkeypatch):
    config, custody, _ = setup
    config['capabilities'] = ['read', 'edit', 'verify']
    from tare_tools_agent_runtime import NativeContext as real_context

    def legacy_context(policy, **kwargs):
        from dataclasses import replace
        from compute_plane.native_checkpoint import operational_checkpoint
        policy = replace(policy, max_compactions=1)
        kwargs['checkpoint'] = operational_checkpoint
        return real_context(policy, **kwargs)

    # Materialize the old header/checkpoint format, then restore with today's host.
    monkeypatch.setattr('tare_tools_agent_runtime.NativeContext', legacy_context)
    client = Client(steps=steps())
    first = native_agent.run_session(config, deployment(), custody, 'Change and verify a.py',
                                    client=client, local_counter=client)
    assert first['status'] == 'PROPOSED'
    path = custody/'native-agent.json'
    state = json.loads(path.read_text())
    state.pop('context_compaction_budget')
    state.pop('working_source_checkpoint')
    path.write_text(json.dumps(state), encoding='utf-8')
    monkeypatch.setattr('tare_tools_agent_runtime.NativeContext', real_context)
    resumed = native_agent.run_session(None, None, custody, resume=True, client=client, local_counter=client)
    assert resumed['status'] == 'PROPOSED' and resumed['model_calls'] == first['model_calls']
    assert client.generations == 4
