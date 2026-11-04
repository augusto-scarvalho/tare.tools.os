"""Completed local refusals resume read-only planning, keeping history and ceilings."""
from copy import deepcopy
import hashlib
import json

import pytest
from compute_plane.native_journal import NativeJournal, save_json
from relay import native_agent, planning_tools, conversation_tasks
from tare_tools_agent_runtime import NativeBudget
from test_native_agent import Client, deployment, setup  # noqa: F401
from test_planning_tools import planner, wrap, execution, request, PLAN  # noqa: F401


def test_explicit_leaf_continuation_preserves_history_after_completed_refusal(setup):
    config, custody, source = setup
    config.update(role='planner', writable={}, verifiers={}, capabilities=['read', 'list', 'search'], typed_output=True)
    invalid = dict(PLAN, sources=[{'repository': 'repo', 'path': 'a.py',
        'sha256': hashlib.sha256((source/'a.py').read_bytes()).hexdigest()}])
    client = Client(steps=[('tare_deliver', invalid), ('tare_deliver', invalid)])
    first = native_agent.run_session(config, deployment(role='planner'), custody, 'Plan the saved bounded change.',
        delegated_leaf=True, client=client, local_counter=client, budget=NativeBudget(max_model_calls=None))
    assert first['failure_code'] == 'INVALID_DELIVERY'
    before = json.loads((custody/'native-agent.json').read_text())
    retry = Client(steps=[('read', {'repository': 'repo', 'path': 'a.py'}), ('tare_deliver', invalid)])
    result = native_agent.run_session(None, None, custody, resume=True,
        follow_up='Continue.', message_id='explicit-human-continuation', client=retry, local_counter=retry)
    after = json.loads((custody/'native-agent.json').read_text())
    assert result['status'] == 'PROPOSED'
    assert before['session_id'] == after['session_id']
    assert before['budget'] == after['budget'] and before['deployment'] == after['deployment']
    messages = retry.calls[-1][1]['messages']
    assert 'Plan the saved bounded change.' in json.dumps(messages)
    assert 'failed local delivery validation' in json.dumps(messages)


@pytest.mark.parametrize('damage', [None, 'no_human', 'different_model', 'unproven', 'writable', 'already_retried'])
def test_bridge_only_reuses_a_proven_same_demand_same_model_refusal(planner, damage):
    root, state, candidate = planner
    task = {'id': 'demand', 'planning_revision': 0, 'intent_input': 'human'}
    effect = 'first-dispatch'; folder = root/'planning'/hashlib.sha256(effect.encode()).hexdigest()[:24]
    session = folder/'session'; session.mkdir(parents=True)
    journal = NativeJournal(root/'journal.jsonl')
    journal.append({'kind': 'effect_requested', 'effect_id': effect, 'tool_call': {'name': 'delegate_plan'}})
    if damage != 'no_human': journal.append({'kind': 'user_message', 'message_id': 'human', 'text': 'Continue.'})
    saved = {'session_id': 'saved', 'deployment': candidate,
        'config': {'role': 'planner', 'repositories': state['config']['repositories'],
            'writable': {'repo': ['a.py']} if damage == 'writable' else {}, 'verifiers': {}, 'capabilities': ['read']}}
    save_json(session/'native-agent.json', saved)
    for e in [{'kind': 'model_requested', 'call_id': 'saved/model/1'},
              {'kind': 'model_failed', 'call_id': 'saved/model/1', 'code': 'INVALID_DELIVERY'}]:
        NativeJournal(session/'journal.jsonl').append(e)
    NativeJournal(session/'observations.jsonl').append({'purpose': 'model', 'call_id': 'saved/model/1',
        'status': 'interrupted' if damage == 'unproven' else 'invalid_delivery', 'error': 'INVALID_DELIVERY',
        'response_sha256': 'a'*64, 'delivery_validation': {'issues': [{'code': 'native_artifact_ignored_target'}]}})
    row = {'status': 'FINISHED', 'route': 'worker', 'demand_id': 'demand', 'result': {'failure_code': 'INVALID_DELIVERY'}}
    save_json(folder/'receipt.json', row)
    cohort = [row] + ([{'continuation_of': 'earlier'}] if damage == 'already_retried' else [])
    selected = dict(candidate, id='changed') if damage == 'different_model' else deepcopy(candidate)
    result = wrap(root, state, execution()).delivery_continuation(task, cohort, 'worker', selected)
    assert result == ((folder/'receipt.json', session) if damage is None else None)


def test_new_phase_default_does_not_shrink_saved_unbounded_planner(planner, monkeypatch):
    root, state, candidate = planner
    state.update(conversation_flow=True, limits={'max_model_calls': None})
    task = {'id': 'demand', 'planning_revision': 0, 'intent': 'deliver'}
    conversation_tasks.save(root, {'schema': 'tare.tools/conversation-tasks/1', 'current': 'demand', 'tasks': [task]})
    session = root/'planning/old/session'; session.mkdir(parents=True)
    save_json(session/'native-agent.json', {'budget': {'max_model_calls': None}})
    old = session.parent/'receipt.json'
    save_json(old, {'status': 'FINISHED', 'calls_reserved': None, 'calls_used': 4})
    monkeypatch.setattr(planning_tools.PlanningTools, 'choose', lambda self, args: ('worker', candidate))
    monkeypatch.setattr(planning_tools.PlanningTools, 'delivery_continuation', lambda *args: (old, session))
    calls = []
    monkeypatch.setattr(native_agent, 'run_session', lambda *args, **kw: calls.append((args, kw)))
    monkeypatch.setattr(planning_tools, 'record', lambda path: {'calls_used': 4, 'result': {'status': 'PLANNED'}})
    result = wrap(root, state, execution()).execute(request(), execution())
    assert json.loads(result.output_json)['status'] == 'PLANNED'
    assert calls[0][0] == (None, None, session)
    assert calls[0][1]['resume'] is True and 'limits' not in calls[0][1]
    new = next(p for p in (root/'planning').glob('*/receipt.json') if p != old)
    row = json.loads(new.read_text())
    assert row['calls_used'] == row['calls_reserved'] == 0
    assert row['continuation_of'] == str(old)
