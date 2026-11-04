"""Work attribution follows newly durable native intents, including resumed sessions."""
from dataclasses import replace
import json

import pytest

from compute_plane.native_journal import NativeJournal
from relay import native_agent, work_inference
from tare_tools_agent_runtime import CancellationToken, ConversationLog, NativeBudget
from test_native_agent import Client, run, setup, steps
from test_work_run import durable_fixture, os_root


def receipt(tmp_path, run_id):
    return {'run_id': run_id, 'work_id': 'work-one'}, tmp_path / (run_id + '.json')


def ids(value):
    return {(call['session_id'], call['call_id']) for call in value['inference_calls']}


def test_native_session_records_only_new_calls_in_each_work_attempt(setup, tmp_path):
    config, custody, _ = setup
    first, first_path = receipt(tmp_path, 'first')
    second, second_path = receipt(tmp_path, 'second')
    stopped = CancellationToken()
    with work_inference.scope():
        work_inference.bind(first, first_path)
        initial = run(config, custody, Client(steps=[steps()[0]], after_model=stopped.cancel),
                      cancellation=stopped, budget=NativeBudget(max_model_calls=2))
    assert initial['status'] == 'CANCELLED'
    first_usage = json.loads((custody / 'usage.json').read_text())
    expected_first = {(initial['session_id'], call['call_id']) for call in first_usage['calls']}
    assert ids(first) == expected_first and len(expected_first) == 1
    assert json.loads(first_path.read_text())['inference_calls'] == first['inference_calls']

    with work_inference.scope():
        work_inference.bind(second, second_path)
        client = Client(steps=[('tare_deliver', {'summary': 'Observed source.'})])
        result = native_agent.run_session(None, None, custody, resume=True, client=client, local_counter=client)
    assert result['status'] == 'PROPOSED' and result['model_calls'] == 2
    usage = json.loads((custody / 'usage.json').read_text())
    all_calls = {(result['session_id'], call['call_id']) for call in usage['calls']}
    assert ids(second) == all_calls - expected_first
    assert len(ids(second)) == 1 and not (ids(first) & ids(second))
    assert all(call['stage'] == 'implementer' for call in first['inference_calls'] + second['inference_calls'])
    assert first['inference_observation_failures'] == second['inference_observation_failures'] == 0

    third, third_path = receipt(tmp_path, 'third')
    with work_inference.scope():
        work_inference.bind(third, third_path)
        replay = Client()
        result = native_agent.run_session(None, None, custody, resume=True, client=replay, local_counter=replay)
    assert result['status'] == 'PROPOSED' and not replay.calls
    assert ids(third) == set()  # Reopening cumulative usage cannot charge an old call again.


def test_attribution_sink_failure_preserves_successful_native_operation(setup, tmp_path, monkeypatch):
    from compute_plane import native_journal
    config, custody, _ = setup
    owner, owner_path = receipt(tmp_path, 'owner')
    save = native_journal.save_json
    def fail_optional(path, value, **kwargs):
        if path == owner_path:
            raise OSError('optional attribution disk failure')
        return save(path, value, **kwargs)
    monkeypatch.setattr(native_journal, 'save_json', fail_optional)
    with work_inference.scope():
        work_inference.bind(owner, owner_path)
        result = run(config, custody, Client(steps=[('tare_deliver', {'summary': 'Ready.'})]))
    assert result['status'] == 'PROPOSED' and result['model_calls'] == 1
    assert len(owner['inference_calls']) == 1
    assert owner['inference_observation_failures'] == 1
    assert json.loads((custody / 'usage.json').read_text())['calls']


def test_scope_restores_previous_owner_and_outside_scope_has_no_attribution(tmp_path):
    journal = ConversationLog()
    assert work_inference.wrap(journal, 'outside', 'planner') is journal
    outer, outer_path = receipt(tmp_path, 'outer')
    inner, inner_path = receipt(tmp_path, 'inner')
    with work_inference.scope():
        work_inference.bind(outer, outer_path)
        with work_inference.scope():
            assert work_inference.wrap(journal, 'unbound', 'planner') is journal
            work_inference.bind(inner, inner_path)
            work_inference.wrap(journal, 'inner-session', 'auditor').append(
                {'kind': 'context_summary_requested', 'call_id': 'summary-1'})
        work_inference.wrap(journal, 'outer-session', 'planner').append(
            {'kind': 'model_requested', 'call_id': 'model-1'})
    assert work_inference.wrap(journal, 'outside', 'planner') is journal
    assert ids(inner) == {('inner-session', 'summary-1')}
    assert ids(outer) == {('outer-session', 'model-1')}
    assert inner['inference_calls'][0]['stage'] == 'auditor'


def test_failed_owner_journal_append_does_not_create_phantom_inference_claim(tmp_path):
    owner, owner_path = receipt(tmp_path, 'owner')
    class FailedJournal:
        def append(self, event):
            raise OSError('required journal failed')
    with work_inference.scope():
        work_inference.bind(owner, owner_path)
        with pytest.raises(OSError, match='required journal failed'):
            work_inference.wrap(FailedJournal(), 'session', 'planner').append(
                {'kind': 'model_requested', 'call_id': 'model-1'})
    assert not owner['inference_calls'] and not owner_path.exists()


def test_native_work_usage_is_cumulative_after_resume_without_duplicate_call_ownership(os_root, tmp_path, monkeypatch):
    from relay.native_attempt import run_native_attempt
    args = durable_fixture(os_root, tmp_path)
    # Match native_work_host: the owner persists adapter observations across invocations.
    args['observe'] = NativeJournal(args['custody'] / 'observations.jsonl').append
    append = NativeJournal.append
    first, first_path = receipt(tmp_path, 'first')
    second, second_path = receipt(tmp_path, 'second')
    def stop_after_saved_edit(journal, event):
        result = append(journal, event)
        if event['kind'] == 'effect_returned' and json.loads(event['result']['output_json']).get('effects') == 1:
            args['control'].cancellation.cancel()
        return result
    with work_inference.scope(), monkeypatch.context() as patch:
        work_inference.bind(first, first_path)
        patch.setattr(NativeJournal, 'append', stop_after_saved_edit)
        result = run_native_attempt(**args)
    assert result['status'] == 'CANCELLED'
    path = args['custody'] / 'scratch' / 'usage.json'
    before = json.loads(path.read_text())
    assert len(before['calls']) == 3 and len(ids(first)) == 3
    args['control'] = replace(args['control'], cancellation=CancellationToken())
    with work_inference.scope():
        work_inference.bind(second, second_path)
        result = run_native_attempt(resume=True, **args)
    after = json.loads(path.read_text())
    assert result['status'] == 'PROPOSED' and len(after['calls']) == 5
    assert len(ids(second)) == 2 and not (ids(first) & ids(second))
    assert ids(first) | ids(second) == {(args['session_id'], call['call_id']) for call in after['calls']}
    # Original calls must retain their observations rather than becoming missing on resume.
    old = {call['call_id']: call for call in before['calls']}
    assert all(call == old[call['call_id']] for call in after['calls'] if call['call_id'] in old)


def test_optional_native_work_usage_save_failure_does_not_block_proposal(os_root, tmp_path, monkeypatch):
    from compute_plane import native_journal
    from relay.native_attempt import run_native_attempt
    args = durable_fixture(os_root, tmp_path)
    save = native_journal.save_json
    def fail_usage(path, value, **kwargs):
        if path.name == 'usage.json':
            raise OSError('optional usage disk failure')
        return save(path, value, **kwargs)
    monkeypatch.setattr(native_journal, 'save_json', fail_usage)
    result = run_native_attempt(**args)
    assert result['status'] == 'PROPOSED'
    assert result['usage_receipt_status'] == 'UNAVAILABLE'
    assert len(args['client'].generations) == 5
    assert json.loads((args['custody'] / 'attempt.json').read_text())['attempts'][-1]['status'] == 'PROPOSED'


def test_compaction_inference_has_the_same_work_owner_as_regular_calls(setup, tmp_path, monkeypatch):
    from compute_plane.native_models import NativeModelAdapter
    config, custody, _ = setup
    owner, owner_path = receipt(tmp_path, 'owner')
    count = NativeModelAdapter.count
    def force_compaction(adapter, request, control):
        value = count(adapter, request, control)
        if request.purpose == 'model' and request.call_id.endswith('/model/3'):
            value['input_tokens'] = 7000
        return value
    monkeypatch.setattr(NativeModelAdapter, 'count', force_compaction)
    # The summary incorrectly returns tools, which must still be attributed as a call.
    client = Client(steps=[steps()[0], steps()[0], ('tare_deliver', {'summary': 'Invalid compaction reply.'})])
    with work_inference.scope():
        work_inference.bind(owner, owner_path)
        result = run(config, custody, client, budget=NativeBudget(max_model_calls=6))
    assert result['status'] == 'BLOCKED' and result['reason'] == 'INVALID_SUMMARY'
    usage = json.loads((custody / 'usage.json').read_text())
    assert len(usage['calls']) == 3
    assert [call['purpose'] for call in usage['calls']].count('summary') == 1
    assert ids(owner) == {(result['session_id'], call['call_id']) for call in usage['calls']}


def test_invalid_optional_usage_contract_keeps_native_execution_result(os_root, tmp_path):
    from relay.native_attempt import run_native_attempt
    args = durable_fixture(os_root, tmp_path)
    args['spend_configuration'] = {'native_usage_contract': {'layout': 'unknown-contract', 'source': 'fixture'}}
    result = run_native_attempt(**args)
    assert result['status'] == 'PROPOSED'
    assert result['usage_receipt_status'] == 'UNAVAILABLE'
    assert result['cost']['unpriced_or_partial_calls'] is None
    assert len(args['client'].generations) == 5


def test_interrupted_native_work_keeps_unknown_usage_and_original_exception(os_root, tmp_path):
    from relay.native_attempt import run_native_attempt
    args = durable_fixture(os_root, tmp_path)
    original_post = args['client'].post
    interrupted = KeyboardInterrupt('fixture model interrupted')
    def stop_during_generation(path, body, control):
        if not path.endswith('/count-tokens'):
            raise interrupted
        return original_post(path, body, control)
    args['client'].post = stop_during_generation
    owner, owner_path = receipt(tmp_path, 'interrupted')
    with work_inference.scope():
        work_inference.bind(owner, owner_path)
        with pytest.raises(KeyboardInterrupt) as caught:
            run_native_attempt(**args)
    assert caught.value is interrupted
    usage = json.loads((args['custody'] / 'scratch' / 'usage.json').read_text())
    assert len(usage['calls']) == 1
    call = usage['calls'][0]
    assert ids(owner) == {(args['session_id'], call['call_id'])}
    assert call['observation']['status'] == 'unknown'
    assert call['execution'] == 'possibly_executed'
    assert all(value is None for value in call['normalized']['tokens'].values())
    assert call['cost']['complete'] is False and call['cost']['known_amount'] is None
    assert json.loads(owner_path.read_text())['inference_calls'] == owner['inference_calls']
    assert json.loads((args['custody'] / 'attempt.json').read_text())['attempts'][-1]['status'] == 'INTERRUPTED'


def test_attribution_limit_preserves_required_intent_and_records_optional_failure(tmp_path):
    owner, owner_path = receipt(tmp_path, 'bounded')
    journal = ConversationLog()
    with work_inference.scope():
        work_inference.bind(owner, owner_path)
        owner['inference_calls'] = [
            {'session_id': 'session', 'call_id': f'model-{i}', 'stage': 'implementer'}
            for i in range(8192)]
        record = work_inference.wrap(journal, 'session', 'implementer').append(
            {'kind': 'model_requested', 'call_id': 'model-overflow'})
    assert record.event()['call_id'] == 'model-overflow'
    assert journal.snapshot().records[-1].event()['call_id'] == 'model-overflow'
    assert len(owner['inference_calls']) == 8192
    assert owner['inference_observation_failures'] == 1
    assert not owner_path.exists()
