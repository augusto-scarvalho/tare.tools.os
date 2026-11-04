"""Falsification tests for correlating queued messages with completed responses."""
import json
from pathlib import Path
import pytest

from relay import native_agent, native_messages
from relay.console_backend import Backend, Selection
from compute_plane.native_journal import NativeJournal
from test_native_agent import setup, Client, deployment


def saved_session(directory, root, enabled=True, session_id='s'):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'native-agent.json').write_text(json.dumps({
        'schema': 'tare.tools/native-agent/2',
        'messages_enabled': enabled,
        'session_id': session_id,
        'config': {
            'schema': 'tare.tools/native-agent-config/1',
            'role': 'implementer',
            'repositories': {'repo': str(root)},
            'writable': {'repo': ['a.py']},
        },
        'deployment': {
            'runner': 'native-http',
            'native_profile': {'model': 'fixture'},
        },
        'instruction': 'Do work.',
        'budget': {'max_model_calls': 16, 'max_stagnant_turns': 3, 'max_model_retries': 0},
        'timeout': None,
        'invocations': [],
        'authority': 'NONE',
    }), encoding='utf-8')


def test_saved_receipt_before_generation_and_completed_response_after_receipt(setup):
    """Parent falsifier: Saved only; receipt before generation; completed response after receipt."""
    config, directory, root = setup
    saved_session(directory, root, enabled=True, session_id='s')
    backend = Backend()
    selection = Selection(root=str(root), session=str(directory))

    # Phase 1: Saved only
    native_messages.enqueue(directory, 'First steering message.', message_id='m1', project=root)
    view = native_messages.inspect(directory, root)
    assert len(view['items']) == 1
    item = view['items'][0]
    assert item['message_id'] == 'm1'
    assert item['text'] == 'First steering message.'
    assert item['status'] == 'QUEUED'
    assert item['sequence'] is None
    assert item['delivery'] == {
        'status': 'SAVED',
        'receipt_sequence': None,
        'call_id': None,
        'request_sequence': None,
        'response_sequence': None,
        'terminal_sequence': None,
    }

    obs = backend.message_observations(selection)
    assert len(obs) == 1
    assert obs[0]['delivery']['status'] == 'SAVED'
    assert backend.message_status(selection, 'm1') == 'QUEUED'

    # Phase 2: Receipt before generation
    journal = NativeJournal(directory / 'journal.jsonl')
    journal.append({
        'kind': 'session_started', 'schema': 'tare-native-session/1',
        'session_id': 's', 'instruction': 'Do work.',
        'capabilities': ['read', 'edit'], 'budget': {},
        'binding': {'work_id': 'w1', 'binding_id': 'b1', 'owner_id': 'o1', 'fence': 1},
    })
    journal.append({
        'kind': 'user_message', 'message_id': 'm1',
        'text': 'First steering message.', 'after_sequence': 1,
    })

    view = native_messages.inspect(directory, root)
    item = view['items'][0]
    assert item['status'] == 'IN_CONTEXT'
    assert item['sequence'] == 2
    assert item['delivery'] == {
        'status': 'RECEIVED',
        'receipt_sequence': 2,
        'call_id': None,
        'request_sequence': None,
        'response_sequence': None,
        'terminal_sequence': None,
    }
    assert backend.message_status(selection, 'm1') == 'IN_CONTEXT'

    # Phase 3: Completed response after receipt
    journal.append({'kind': 'model_requested', 'call_id': 's/model/1'})
    journal.append({
        'kind': 'model_returned', 'call_id': 's/model/1',
        'reply': {
            'text': 'Completed task response.', 'complete': True,
            'proposes_delivery': True, 'tool_calls': (), 'metadata_json': '{}',
        },
    })
    journal.append({
        'kind': 'terminal', 'status': 'proposed', 'reason': 'DELIVERY_PROPOSED',
        'output': 'Completed task response.',
    })

    view = native_messages.inspect(directory, root)
    item = view['items'][0]
    assert item['status'] == 'IN_CONTEXT'
    assert item['sequence'] == 2
    assert item['delivery'] == {
        'status': 'RESPONSE_READY',
        'receipt_sequence': 2,
        'call_id': 's/model/1',
        'request_sequence': 3,
        'response_sequence': 4,
        'terminal_sequence': 5,
    }
    assert backend.message_status(selection, 'm1') == 'IN_CONTEXT'


def test_message_queued_during_generation_receives_second_answer_not_first(setup):
    """Parent review correction 5: Queue during generation; append after terminal.
    First answer leaves it SAVED, then RECEIVED after append, then RESPONSE_READY after next call."""
    config, directory, root = setup
    saved_session(directory, root, enabled=True, session_id='s')
    journal = NativeJournal(directory / 'journal.jsonl')
    journal.append({
        'kind': 'session_started', 'schema': 'tare-native-session/1',
        'session_id': 's', 'instruction': 'Do work.',
        'capabilities': ['read', 'edit'], 'budget': {},
        'binding': {'work_id': 'w1', 'binding_id': 'b1', 'owner_id': 'o1', 'fence': 1},
    })

    # Model request 1 is in-flight
    journal.append({'kind': 'model_requested', 'call_id': 's/model/1'})

    # Message is queued during generation (durable queue entry, not yet in journal)
    native_messages.enqueue(directory, 'Queued mid-flight.', message_id='m1', project=root)

    # First model call finishes and proposes delivery
    journal.append({
        'kind': 'model_returned', 'call_id': 's/model/1',
        'reply': {
            'text': 'First answer.', 'complete': True,
            'proposes_delivery': True, 'tool_calls': (), 'metadata_json': '{}',
        },
    })
    journal.append({
        'kind': 'terminal', 'status': 'proposed', 'reason': 'DELIVERY_PROPOSED',
        'output': 'First answer.',
    })

    # First answer leaves m1 SAVED (not credited with first answer)
    item = native_messages.inspect(directory, root)['items'][0]
    assert item['status'] == 'QUEUED'
    assert item['delivery']['status'] == 'SAVED'
    assert item['delivery']['call_id'] is None

    # Next continuation boundary: user_message is appended after prior terminal
    journal.append({
        'kind': 'user_message', 'message_id': 'm1',
        'text': 'Queued mid-flight.', 'after_sequence': 4,
    })

    # After append, it is RECEIVED
    item = native_messages.inspect(directory, root)['items'][0]
    assert item['status'] == 'IN_CONTEXT'
    assert item['delivery']['status'] == 'RECEIVED'
    assert item['delivery']['receipt_sequence'] == 5
    assert item['delivery']['call_id'] is None

    # Next generation round for m1
    journal.append({'kind': 'model_requested', 'call_id': 's/model/2'})
    journal.append({
        'kind': 'model_returned', 'call_id': 's/model/2',
        'reply': {
            'text': 'Second answer.', 'complete': True,
            'proposes_delivery': True, 'tool_calls': (), 'metadata_json': '{}',
        },
    })
    journal.append({
        'kind': 'terminal', 'status': 'proposed', 'reason': 'DELIVERY_PROPOSED',
        'output': 'Second answer.',
    })

    # RESPONSE_READY after next call
    item = native_messages.inspect(directory, root)['items'][0]
    assert item['delivery'] == {
        'status': 'RESPONSE_READY',
        'receipt_sequence': 5,
        'call_id': 's/model/2',
        'request_sequence': 6,
        'response_sequence': 7,
        'terminal_sequence': 8,
    }


def test_two_received_messages_before_one_request_both_complete(setup):
    """Parent review correction 2 & 6: Several user messages at a boundary before generation
    both get the first later complete answer."""
    config, directory, root = setup
    saved_session(directory, root, enabled=True, session_id='s')
    native_messages.enqueue(directory, 'First instruction.', message_id='m1', project=root)
    native_messages.enqueue(directory, 'Second instruction.', message_id='m2', project=root)

    journal = NativeJournal(directory / 'journal.jsonl')
    journal.append({
        'kind': 'session_started', 'schema': 'tare-native-session/1',
        'session_id': 's', 'instruction': 'Do work.',
        'capabilities': ['read', 'edit'], 'budget': {},
        'binding': {'work_id': 'w1', 'binding_id': 'b1', 'owner_id': 'o1', 'fence': 1},
    })
    # Both messages received at continuation boundary before next generation
    journal.append({'kind': 'user_message', 'message_id': 'm1', 'text': 'First instruction.', 'after_sequence': 1})
    journal.append({'kind': 'user_message', 'message_id': 'm2', 'text': 'Second instruction.', 'after_sequence': 2})

    # One model request and completion answers both
    journal.append({'kind': 'model_requested', 'call_id': 's/model/1'})
    journal.append({
        'kind': 'model_returned', 'call_id': 's/model/1',
        'reply': {'text': 'Joint delivery answer.', 'complete': True, 'proposes_delivery': True, 'tool_calls': (), 'metadata_json': '{}'},
    })
    journal.append({'kind': 'terminal', 'status': 'proposed', 'reason': 'DELIVERY_PROPOSED', 'output': 'Joint delivery answer.'})

    items = native_messages.inspect(directory, root)['items']
    assert len(items) == 2

    assert items[0]['message_id'] == 'm1'
    assert items[0]['delivery'] == {
        'status': 'RESPONSE_READY',
        'receipt_sequence': 2,
        'call_id': 's/model/1',
        'request_sequence': 4,
        'response_sequence': 5,
        'terminal_sequence': 6,
    }

    assert items[1]['message_id'] == 'm2'
    assert items[1]['delivery'] == {
        'status': 'RESPONSE_READY',
        'receipt_sequence': 3,
        'call_id': 's/model/1',
        'request_sequence': 4,
        'response_sequence': 5,
        'terminal_sequence': 6,
    }


def test_foreign_call_identity_refused(setup):
    """Parent review correction 3 & 6: Reject foreign session call IDs."""
    config, directory, root = setup
    saved_session(directory, root, enabled=True, session_id='s')
    native_messages.enqueue(directory, 'Task text.', message_id='m1', project=root)

    journal = NativeJournal(directory / 'journal.jsonl')
    journal.append({
        'kind': 'session_started', 'schema': 'tare-native-session/1',
        'session_id': 's', 'instruction': 'Do work.',
        'capabilities': ['read', 'edit'], 'budget': {},
        'binding': {'work_id': 'w1', 'binding_id': 'b1', 'owner_id': 'o1', 'fence': 1},
    })
    journal.append({'kind': 'user_message', 'message_id': 'm1', 'text': 'Task text.', 'after_sequence': 1})

    # Foreign session call identity
    journal.append({'kind': 'model_requested', 'call_id': 'foreign-session/model/1'})
    journal.append({
        'kind': 'model_returned', 'call_id': 'foreign-session/model/1',
        'reply': {'text': 'Done.', 'complete': True, 'proposes_delivery': True, 'tool_calls': (), 'metadata_json': '{}'},
    })
    journal.append({'kind': 'terminal', 'status': 'proposed', 'reason': 'DELIVERY_PROPOSED', 'output': 'Done.'})

    item = native_messages.inspect(directory, root)['items'][0]
    assert item['delivery']['status'] == 'RECEIVED'
    assert item['delivery']['call_id'] is None


def test_crossing_intervening_requests_reset_candidate(setup):
    """Parent review correction 3 & 6: Crossing/intervening requests reset candidate."""
    config, directory, root = setup
    saved_session(directory, root, enabled=True, session_id='s')
    native_messages.enqueue(directory, 'Task text.', message_id='m1', project=root)

    journal = NativeJournal(directory / 'journal.jsonl')
    journal.append({
        'kind': 'session_started', 'schema': 'tare-native-session/1',
        'session_id': 's', 'instruction': 'Do work.',
        'capabilities': ['read', 'edit'], 'budget': {},
        'binding': {'work_id': 'w1', 'binding_id': 'b1', 'owner_id': 'o1', 'fence': 1},
    })
    journal.append({'kind': 'user_message', 'message_id': 'm1', 'text': 'Task text.', 'after_sequence': 1})

    # Request 1 started
    journal.append({'kind': 'model_requested', 'call_id': 's/model/1'})
    # Intervening request 2 before request 1 returned (crossing/intervening request)
    journal.append({'kind': 'model_requested', 'call_id': 's/model/2'})
    # Return for request 1 arrives after request 2 started
    journal.append({
        'kind': 'model_returned', 'call_id': 's/model/1',
        'reply': {'text': 'Done.', 'complete': True, 'proposes_delivery': True, 'tool_calls': (), 'metadata_json': '{}'},
    })
    journal.append({'kind': 'terminal', 'status': 'proposed', 'reason': 'DELIVERY_PROPOSED', 'output': 'Done.'})

    item = native_messages.inspect(directory, root)['items'][0]
    assert item['delivery']['status'] == 'RECEIVED'
    assert item['delivery']['call_id'] is None


def test_missing_tool_calls_in_reply_is_not_valid(setup):
    """Parent review correction 3 & 6: Never treat missing tool_calls as valid."""
    config, directory, root = setup
    saved_session(directory, root, enabled=True, session_id='s')
    native_messages.enqueue(directory, 'Task text.', message_id='m1', project=root)

    journal = NativeJournal(directory / 'journal.jsonl')
    journal.append({
        'kind': 'session_started', 'schema': 'tare-native-session/1',
        'session_id': 's', 'instruction': 'Do work.',
        'capabilities': ['read', 'edit'], 'budget': {},
        'binding': {'work_id': 'w1', 'binding_id': 'b1', 'owner_id': 'o1', 'fence': 1},
    })
    journal.append({'kind': 'user_message', 'message_id': 'm1', 'text': 'Task text.', 'after_sequence': 1})
    journal.append({'kind': 'model_requested', 'call_id': 's/model/1'})
    # Missing 'tool_calls' key in reply dict
    journal.append({
        'kind': 'model_returned', 'call_id': 's/model/1',
        'reply': {'text': 'Missing tool_calls key.', 'complete': True, 'proposes_delivery': True, 'metadata_json': '{}'},
    })
    journal.append({'kind': 'terminal', 'status': 'proposed', 'reason': 'DELIVERY_PROPOSED', 'output': 'Missing tool_calls key.'})

    item = native_messages.inspect(directory, root)['items'][0]
    assert item['delivery']['status'] == 'RECEIVED'
    assert item['delivery']['call_id'] is None


def test_return_consumed_before_terminal_prevents_response_ready(setup):
    """Parent review correction 3 & 6: return consumed before terminal."""
    config, directory, root = setup
    saved_session(directory, root, enabled=True, session_id='s')
    native_messages.enqueue(directory, 'Task text.', message_id='m1', project=root)

    journal = NativeJournal(directory / 'journal.jsonl')
    journal.append({
        'kind': 'session_started', 'schema': 'tare-native-session/1',
        'session_id': 's', 'instruction': 'Do work.',
        'capabilities': ['read', 'edit'], 'budget': {},
        'binding': {'work_id': 'w1', 'binding_id': 'b1', 'owner_id': 'o1', 'fence': 1},
    })
    journal.append({'kind': 'user_message', 'message_id': 'm1', 'text': 'Task text.', 'after_sequence': 1})
    journal.append({'kind': 'model_requested', 'call_id': 's/model/1'})
    journal.append({
        'kind': 'model_returned', 'call_id': 's/model/1',
        'reply': {'text': 'Consumed text.', 'complete': True, 'proposes_delivery': True, 'tool_calls': (), 'metadata_json': '{}'},
    })
    # Intervening reply_consumed before terminal
    journal.append({'kind': 'reply_consumed'})
    journal.append({'kind': 'terminal', 'status': 'proposed', 'reason': 'DELIVERY_PROPOSED', 'output': 'Consumed text.'})

    item = native_messages.inspect(directory, root)['items'][0]
    assert item['delivery']['status'] == 'RECEIVED'
    assert item['delivery']['call_id'] is None


def test_duplicate_user_message_ids_refused(setup):
    """Parent review correction 4 & 6: duplicate user message IDs must refuse."""
    config, directory, root = setup
    saved_session(directory, root, enabled=True, session_id='s')
    native_messages.enqueue(directory, 'Task text.', message_id='m1', project=root)

    journal = NativeJournal(directory / 'journal.jsonl')
    journal.append({
        'kind': 'session_started', 'schema': 'tare-native-session/1',
        'session_id': 's', 'instruction': 'Do work.',
        'capabilities': ['read', 'edit'], 'budget': {},
        'binding': {'work_id': 'w1', 'binding_id': 'b1', 'owner_id': 'o1', 'fence': 1},
    })
    journal.append({'kind': 'user_message', 'message_id': 'm1', 'text': 'Task text.', 'after_sequence': 1})
    journal.append({'kind': 'user_message', 'message_id': 'm1', 'text': 'Task text.', 'after_sequence': 2})

    with pytest.raises(ValueError, match='Duplicate user message ID in journal'):
        native_messages.inspect(directory, project=root)


def test_journal_first_event_session_id_and_after_sequence_validation(setup):
    """Parent review correction 4: Validate journal first event session_id and after_sequence."""
    config, directory, root = setup
    saved_session(directory, root, enabled=True, session_id='s')
    native_messages.enqueue(directory, 'Task text.', message_id='m1', project=root)

    # 1. First event session_id mismatch
    journal = NativeJournal(directory / 'journal.jsonl')
    journal.append({
        'kind': 'session_started', 'schema': 'tare-native-session/1',
        'session_id': 'wrong_session', 'instruction': 'Do work.',
        'capabilities': ['read', 'edit'], 'budget': {},
        'binding': {'work_id': 'w1', 'binding_id': 'b1', 'owner_id': 'o1', 'fence': 1},
    })
    with pytest.raises(ValueError, match='Journal session identity mismatch'):
        native_messages.inspect(directory, project=root)

    # Rewrite with correct session_id but invalid after_sequence
    (directory / 'journal.jsonl').unlink()
    journal = NativeJournal(directory / 'journal.jsonl')
    journal.append({
        'kind': 'session_started', 'schema': 'tare-native-session/1',
        'session_id': 's', 'instruction': 'Do work.',
        'capabilities': ['read', 'edit'], 'budget': {},
        'binding': {'work_id': 'w1', 'binding_id': 'b1', 'owner_id': 'o1', 'fence': 1},
    })
    # after_sequence: 999 does not match predecessor sequence 1
    journal.append({'kind': 'user_message', 'message_id': 'm1', 'text': 'Task text.', 'after_sequence': 999})
    with pytest.raises(ValueError, match='Invalid user message after_sequence'):
        native_messages.inspect(directory, project=root)


def test_failed_cancelled_partial_and_tool_only_outputs_are_not_answers(setup):
    """Parent falsifier: Failed, cancelled/interrupted, partial and tool-only outputs are not answers."""
    config, directory, root = setup
    saved_session(directory, root, enabled=True, session_id='s')
    native_messages.enqueue(directory, 'Test non-answers.', message_id='m1', project=root)

    journal = NativeJournal(directory / 'journal.jsonl')
    journal.append({
        'kind': 'session_started', 'schema': 'tare-native-session/1',
        'session_id': 's', 'instruction': 'Do work.',
        'capabilities': ['read', 'edit'], 'budget': {},
        'binding': {'work_id': 'w1', 'binding_id': 'b1', 'owner_id': 'o1', 'fence': 1},
    })
    journal.append({'kind': 'user_message', 'message_id': 'm1', 'text': 'Test non-answers.', 'after_sequence': 1})

    # Model failure
    journal.append({'kind': 'model_requested', 'call_id': 's/model/1'})
    journal.append({'kind': 'model_failed', 'call_id': 's/model/1', 'code': 'SERVICE_UNAVAILABLE', 'retryable': False})

    item = native_messages.inspect(directory, root)['items'][0]
    assert item['delivery']['status'] == 'RECEIVED'
    assert item['delivery']['call_id'] is None

    # Invocation interrupted
    journal.append({'kind': 'model_requested', 'call_id': 's/model/2'})
    journal.append({'kind': 'invocation_interrupted', 'invocation_id': 's/model/2', 'error_type': 'TimeoutError', 'usage': None})

    item = native_messages.inspect(directory, root)['items'][0]
    assert item['delivery']['status'] == 'RECEIVED'
    assert item['delivery']['call_id'] is None

    # Partial reply
    journal.append({'kind': 'model_requested', 'call_id': 's/model/3'})
    journal.append({
        'kind': 'model_returned', 'call_id': 's/model/3',
        'reply': {'text': 'Partial cut off…', 'complete': False, 'proposes_delivery': True, 'tool_calls': (), 'metadata_json': '{}'},
    })
    journal.append({'kind': 'terminal', 'status': 'blocked', 'reason': 'PARTIAL_REPLY', 'output': 'Partial cut off…'})

    item = native_messages.inspect(directory, root)['items'][0]
    assert item['delivery']['status'] == 'RECEIVED'
    assert item['delivery']['call_id'] is None

    # Tool calls only
    journal.append({'kind': 'model_requested', 'call_id': 's/model/4'})
    journal.append({
        'kind': 'model_returned', 'call_id': 's/model/4',
        'reply': {
            'text': 'Reading file first.', 'complete': True, 'proposes_delivery': False,
            'tool_calls': ({'call_id': 'c1', 'name': 'read', 'arguments_json': '{"path": "a.py"}'},),
            'metadata_json': '{}',
        },
    })
    journal.append({
        'kind': 'effect_requested', 'effect_id': 's/effect/4',
        'tool_call': {'call_id': 'c1', 'name': 'read', 'arguments_json': '{"path": "a.py"}'},
    })
    journal.append({
        'kind': 'effect_returned',
        'result': {'effect_id': 's/effect/4', 'status': 'succeeded', 'output_json': '{"content": "value = 1"}', 'progressed': True},
    })

    item = native_messages.inspect(directory, root)['items'][0]
    assert item['delivery']['status'] == 'RECEIVED'
    assert item['delivery']['call_id'] is None


def test_compaction_summary_does_not_count_as_task_answer(setup):
    """Parent falsifier: Compaction summary does not count as a task answer; protected input remains."""
    config, directory, root = setup
    saved_session(directory, root, enabled=True, session_id='s')
    native_messages.enqueue(directory, 'Important protected task.', message_id='m1', project=root)

    journal = NativeJournal(directory / 'journal.jsonl')
    journal.append({
        'kind': 'session_started', 'schema': 'tare-native-session/1',
        'session_id': 's', 'instruction': 'Do work.',
        'capabilities': ['read', 'edit'], 'budget': {},
        'binding': {'work_id': 'w1', 'binding_id': 'b1', 'owner_id': 'o1', 'fence': 1},
    })
    journal.append({'kind': 'user_message', 'message_id': 'm1', 'text': 'Important protected task.', 'after_sequence': 1})

    # Summary lifecycle
    journal.append({'kind': 'context_summary_requested', 'call_id': 's/summary/1'})
    journal.append({'kind': 'context_summary_returned', 'call_id': 's/summary/1', 'summary': 'Condensed history.'})
    journal.append({'kind': 'context_activated', 'retained_sequences': [1, 2]})

    item = native_messages.inspect(directory, root)['items'][0]
    assert item['delivery']['status'] == 'RECEIVED'
    assert item['delivery']['call_id'] is None
    assert item['delivery']['receipt_sequence'] == 2


def test_bare_or_unmatched_proposed_terminal_lacks_proof_and_cannot_count(setup):
    """Parent falsifier: Bare/unmatched proposed terminal lacks proof and cannot count."""
    config, directory, root = setup
    saved_session(directory, root, enabled=True, session_id='s')
    native_messages.enqueue(directory, 'Audit proof test.', message_id='m1', project=root)

    journal = NativeJournal(directory / 'journal.jsonl')
    journal.append({
        'kind': 'session_started', 'schema': 'tare-native-session/1',
        'session_id': 's', 'instruction': 'Do work.',
        'capabilities': ['read', 'edit'], 'budget': {},
        'binding': {'work_id': 'w1', 'binding_id': 'b1', 'owner_id': 'o1', 'fence': 1},
    })
    journal.append({'kind': 'user_message', 'message_id': 'm1', 'text': 'Audit proof test.', 'after_sequence': 1})

    # Bare terminal
    journal.append({'kind': 'terminal', 'status': 'proposed', 'reason': 'DELIVERY_PROPOSED', 'output': 'Bare terminal claim.'})

    item = native_messages.inspect(directory, root)['items'][0]
    assert item['delivery']['status'] == 'RECEIVED'
    assert item['delivery']['call_id'] is None

    # Mismatched output
    journal.append({'kind': 'model_requested', 'call_id': 's/model/1'})
    journal.append({
        'kind': 'model_returned', 'call_id': 's/model/1',
        'reply': {'text': 'Real generated text.', 'complete': True, 'proposes_delivery': True, 'tool_calls': (), 'metadata_json': '{}'},
    })
    journal.append({'kind': 'terminal', 'status': 'proposed', 'reason': 'DELIVERY_PROPOSED', 'output': 'Different output.'})

    item = native_messages.inspect(directory, root)['items'][0]
    assert item['delivery']['status'] == 'RECEIVED'
    assert item['delivery']['call_id'] is None


def test_refusals_wrong_project_and_text_conflicts(setup, tmp_path):
    """Parent falsifier: Wrong root/session identity, text/attachment conflict, partial journal refusal."""
    config, directory, root = setup
    saved_session(directory, root, enabled=True, session_id='s')
    native_messages.enqueue(directory, 'Valid queued text.', message_id='m1', project=root)

    # 1. Wrong project root refusal
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    with pytest.raises(ValueError, match='another project'):
        native_messages.inspect(directory, project=elsewhere)

    # 2. Session identity changed refusal in queue.json
    queue_path = directory / 'messages' / 'queue.json'
    queue_data = json.loads(queue_path.read_text(encoding='utf-8'))
    queue_data['session_id'] = 'tampered_session'
    queue_path.write_text(json.dumps(queue_data), encoding='utf-8')
    with pytest.raises(ValueError, match='identity changed'):
        native_messages.inspect(directory, project=root)

    # Restore valid queue
    queue_data['session_id'] = 's'
    queue_path.write_text(json.dumps(queue_data), encoding='utf-8')

    # 3. Text conflict between journal and queue
    journal = NativeJournal(directory / 'journal.jsonl')
    journal.append({
        'kind': 'session_started', 'schema': 'tare-native-session/1',
        'session_id': 's', 'instruction': 'Do work.',
        'capabilities': ['read', 'edit'], 'budget': {},
        'binding': {'work_id': 'w1', 'binding_id': 'b1', 'owner_id': 'o1', 'fence': 1},
    })
    journal.append({'kind': 'user_message', 'message_id': 'm1', 'text': 'Conflicting journal text.', 'after_sequence': 1})
    with pytest.raises(ValueError, match='different text or attachments'):
        native_messages.inspect(directory, project=root)

    # 4. Partial / malformed journal refusal
    (directory / 'journal.jsonl').write_bytes(b'{"incomplete": "broken json')
    with pytest.raises(Exception):
        native_messages.inspect(directory, project=root)


def test_reopen_replay_gives_same_observations_with_zero_side_effects(setup):
    """Parent falsifier: Reopen/replay gives same observations with zero extra inference/tool effects."""
    config, directory, root = setup
    saved_session(directory, root, enabled=True, session_id='s')
    native_messages.enqueue(directory, 'Deterministic observation test.', message_id='m1', project=root)

    journal = NativeJournal(directory / 'journal.jsonl')
    journal.append({
        'kind': 'session_started', 'schema': 'tare-native-session/1',
        'session_id': 's', 'instruction': 'Do work.',
        'capabilities': ['read', 'edit'], 'budget': {},
        'binding': {'work_id': 'w1', 'binding_id': 'b1', 'owner_id': 'o1', 'fence': 1},
    })
    journal.append({'kind': 'user_message', 'message_id': 'm1', 'text': 'Deterministic observation test.', 'after_sequence': 1})
    journal.append({'kind': 'model_requested', 'call_id': 's/model/1'})
    journal.append({
        'kind': 'model_returned', 'call_id': 's/model/1',
        'reply': {'text': 'Final output.', 'complete': True, 'proposes_delivery': True, 'tool_calls': (), 'metadata_json': '{}'},
    })
    journal.append({'kind': 'terminal', 'status': 'proposed', 'reason': 'DELIVERY_PROPOSED', 'output': 'Final output.'})

    journal_bytes_before = (directory / 'journal.jsonl').read_bytes()
    queue_bytes_before = (directory / 'messages' / 'queue.json').read_bytes()

    first_view = native_messages.inspect(directory, root)
    for _ in range(5):
        subsequent_view = native_messages.inspect(directory, root)
        assert subsequent_view == first_view
        assert subsequent_view['items'][0]['delivery']['status'] == 'RESPONSE_READY'
        assert subsequent_view['items'][0]['delivery']['call_id'] == 's/model/1'

    assert (directory / 'journal.jsonl').read_bytes() == journal_bytes_before
    assert (directory / 'messages' / 'queue.json').read_bytes() == queue_bytes_before


def test_existing_inspect_statuses_and_backend_message_status_compatibility(setup):
    """Parent falsifier: Existing inspect statuses and Backend.message_status remain compatible."""
    config, directory, root = setup
    saved_session(directory, root, enabled=True, session_id='s')
    backend = Backend()
    selection = Selection(root=str(root), session=str(directory))

    native_messages.enqueue(directory, 'Message one.', message_id='m1', project=root)

    # QUEUED compatibility
    assert backend.message_status(selection, 'm1') == 'QUEUED'
    view = native_messages.inspect(directory, root)
    assert view['items'][0]['status'] == 'QUEUED'
    assert view['items'][0]['delivery']['status'] == 'SAVED'

    # IN_CONTEXT compatibility
    journal = NativeJournal(directory / 'journal.jsonl')
    journal.append({
        'kind': 'session_started', 'schema': 'tare-native-session/1',
        'session_id': 's', 'instruction': 'Do work.',
        'capabilities': ['read', 'edit'], 'budget': {},
        'binding': {'work_id': 'w1', 'binding_id': 'b1', 'owner_id': 'o1', 'fence': 1},
    })
    journal.append({'kind': 'user_message', 'message_id': 'm1', 'text': 'Message one.', 'after_sequence': 1})

    assert backend.message_status(selection, 'm1') == 'IN_CONTEXT'
    view = native_messages.inspect(directory, root)
    assert view['items'][0]['status'] == 'IN_CONTEXT'
    assert view['items'][0]['delivery']['status'] == 'RECEIVED'

    # Scoped backend reads return all current session message observations
    observations = backend.message_observations(selection)
    assert len(observations) == 1
    assert observations[0]['message_id'] == 'm1'
    assert observations[0]['status'] == 'IN_CONTEXT'
    assert observations[0]['delivery']['status'] == 'RECEIVED'


def test_real_agent_session_steering_receipt_lifecycle(setup):
    """End-to-end verification using real native agent execution with mock client."""
    config, directory, root = setup
    def incoming():
        if client.generations == 1:
            native_messages.enqueue(directory, 'Only explain now; do not edit.', message_id='steer-1', project=root)

    client = Client('openai-chat', [
        ('read', {'repository': 'repo', 'path': 'a.py'}),
        ('tare_deliver', {'summary': 'Explained without edits.'}),
    ], incoming)

    result = native_agent.run_session(
        config, deployment('openai-chat'), directory, 'Read a.py.',
        client=client, local_counter=client, accept_messages=True,
    )
    assert result['status'] == 'PROPOSED'

    view = native_messages.inspect(directory, root)
    assert len(view['items']) == 1
    item = view['items'][0]
    assert item['message_id'] == 'steer-1'
    assert item['status'] == 'IN_CONTEXT'
    assert item['delivery']['status'] == 'RESPONSE_READY'
    assert item['delivery']['receipt_sequence'] is not None
    assert item['delivery']['call_id'] is not None
    assert item['delivery']['request_sequence'] is not None
    assert item['delivery']['response_sequence'] is not None
    assert item['delivery']['terminal_sequence'] is not None

    backend = Backend()
    selection = Selection(root=str(root), session=str(directory))
    assert backend.message_status(selection, 'steer-1') == 'IN_CONTEXT'


def parent_receipt_session(tmp_path):
    directory=tmp_path/'session'; directory.mkdir()
    (directory/'native-agent.json').write_text(json.dumps({'messages_enabled':True,'session_id':'s','config':{'repositories':{'repo':str(tmp_path)}}}))
    native_messages.enqueue(directory,'Correction',message_id='m')
    journal=NativeJournal(directory/'journal.jsonl')
    journal.append({'kind':'session_started','session_id':'s'})
    journal.append({'kind':'user_message','message_id':'m','text':'Correction','after_sequence':1})
    return directory,journal


@pytest.mark.parametrize('where', ['before_return','before_terminal'])
@pytest.mark.parametrize('intervening', [
    {'kind':'context_summary_requested','call_id':'s/summary/1'},
    {'kind':'context_counted','input_tokens':10},
    {'kind':'effect_requested','effect_id':'s/effect/1'},
    {'kind':'invocation_interrupted','invocation_id':'s/model/1'},
])
def test_unproven_interleaved_chain_does_not_report_response(tmp_path,where,intervening):
    directory,journal=parent_receipt_session(tmp_path)
    journal.append({'kind':'model_requested','call_id':'s/model/1'})
    if where=='before_return': journal.append(intervening)
    journal.append({'kind':'model_returned','call_id':'s/model/1','reply':{'text':'Done','tool_calls':[],'complete':True,'proposes_delivery':True}})
    if where=='before_terminal': journal.append(intervening)
    journal.append({'kind':'terminal','status':'proposed','output':'Done'})
    assert native_messages.inspect(directory)['items'][0]['delivery']['status']=='RECEIVED'
