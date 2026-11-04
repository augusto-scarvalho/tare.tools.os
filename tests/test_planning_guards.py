"""No paid repeats, silent effort changes or fake accounting during recovery."""
from copy import deepcopy
from dataclasses import asdict
import json

import pytest

from compute_plane.native_journal import NativeJournal
from relay import native_catalog
from test_planning_tools import planner, execution, wrap, request, stub_child  # noqa: F401
from test_native_agent import Client, setup, deployment  # noqa: F401
from relay import native_agent


@pytest.mark.parametrize('changed', [False, True])
def test_recovery_checks_pending_arguments_before_reusing_a_completed_plan(planner, monkeypatch, changed):
    parent, state, candidate = planner
    calls = []
    stub_child(monkeypatch, candidate, calls)
    control = execution(); tool = wrap(parent, state, control)
    original = request()
    tool.execute(original, control)
    pending = request({'instruction': 'Changed mission.'}) if changed else original
    journal = NativeJournal(parent/'journal.jsonl')
    journal.append({'kind':'effect_requested','effect_id':pending.effect_id,'tool_call':asdict(pending.tool_call)})
    recovered, result = tool.recover_effect(journal)
    assert result.status == ('refused' if changed else 'succeeded')
    assert recovered.effect_id == pending.effect_id and len(calls) == 1


def test_recovery_without_a_dispatch_receipt_never_starts_a_planner(planner):
    parent, state, candidate = planner
    control = execution(); tool = wrap(parent, state, control)
    pending = request()
    journal = NativeJournal(parent/'journal.jsonl')
    journal.append({'kind':'effect_requested','effect_id':pending.effect_id,'tool_call':asdict(pending.tool_call)})
    recovered, result = tool.recover_effect(journal)
    assert result.status == 'refused' and 'no automatic repeat' in result.output_json
    assert not (parent/'planning').exists()


@pytest.mark.parametrize('name,field', [('openai-api','reasoning'), ('gemini-api-3-8-flash','reasoning_effort'),
                                        ('claude-api', 'output_config')])
def test_api_effort_uses_the_same_frozen_mapping_as_the_picker(planner, monkeypatch, name, field):
    parent, state, candidate = planner
    candidate.clear(); candidate.update(native_catalog.bundled(name))
    before = deepcopy(candidate)
    state['planning']['other_providers'] = True
    calls = []
    stub_child(monkeypatch, candidate, calls)
    control = execution()
    result = wrap(parent, state, control).execute(request({'instruction':'Plan.', 'effort':'high'}), control)
    assert result.status == 'succeeded', result
    params = json.loads(calls[0][1]['native_profile']['parameters_json'])
    assert (params[field]['effort'] if field in {'reasoning', 'output_config'} else params[field]) == 'high'
    assert candidate == before


def test_unsupported_api_effort_is_refused_before_spending(planner, monkeypatch):
    parent, state, candidate = planner
    candidate.clear(); candidate.update(native_catalog.bundled('claude-api'))
    candidate['native_profile']['endpoint'] = 'https://unqualified.example/v1'
    candidate.pop('native_reasoning_levels', None)
    state['planning']['other_providers'] = True
    calls = []
    stub_child(monkeypatch, candidate, calls)
    control = execution()
    result = wrap(parent, state, control).execute(request({'instruction':'Plan.', 'effort':'xhigh'}), control)
    assert result.status == 'refused' and calls == []
    assert 'no registered xhigh' in result.output_json and not (parent/'planning').exists()


def test_partial_reply_exposes_provider_stop_without_repeating_the_request(setup):
    config, custody, source = setup
    class Partial(Client):
        def post(self, path, body, control):
            if path.endswith('/chat/completions'):
                self.generations += 1
                return {'model':'fixture', 'choices':[{'finish_reason':'other',
                    'message':{'role':'assistant','content':None}}]}
            return super().post(path, body, control)
    client = Partial()
    result = native_agent.run_session(config, deployment(), custody, 'Plan only.', client=client,
        local_counter=client, limits={'max_model_calls':2, 'max_seconds':30})
    assert result['reason'] == 'PARTIAL_REPLY' and result['provider_stop_reason'] == 'other'
    assert client.generations == 1
