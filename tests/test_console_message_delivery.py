"""Falsification tests for visible delivery state for every queued message in TUI console."""
import asyncio
import json
from pathlib import Path
from threading import Event
import pytest
from textual.widgets import Input, Select, TextArea, Static

from compute_plane.native_journal import NativeJournal
from relay import console_demand, native_messages, relay_mesh
from relay.console_backend import Backend, Selection
from relay.console_output import Transcript, MessageDelivery, SelectableMessage
from relay.native_console import WorkConsole


def rendered_text(widget):
    return str(getattr(widget, 'content', widget.render()))


def make_session(directory, root, session_id='s'):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'native-agent.json').write_text(json.dumps({
        'schema': 'tare.tools/native-agent/2',
        'messages_enabled': True,
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


def init_journal(directory, session_id='s'):
    journal = NativeJournal(directory / 'journal.jsonl')
    journal.append({
        'kind': 'session_started', 'schema': 'tare-native-session/1',
        'session_id': session_id, 'instruction': 'Do work.',
        'capabilities': ['read', 'edit'], 'budget': {},
        'binding': {'work_id': 'w1', 'binding_id': 'b1', 'owner_id': 'o1', 'fence': 1},
    })
    return journal


@pytest.mark.parametrize('size', [(80, 24), (130, 44)])
def test_multiple_messages_through_three_states(tmp_path, monkeypatch, size):
    """Multiple messages progress through Saved, Received, Response ready.
    Later message remains Saved while earlier is done; existing row updated in place."""
    project = tmp_path / 'project'; project.mkdir()
    session = tmp_path / 'session'
    make_session(session, project, session_id='s')
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path / 'state'))
    monkeypatch.setattr(relay_mesh, 'RELAY_ROOT', tmp_path / 'control')

    # Enqueue two messages: both start SAVED
    native_messages.enqueue(session, 'First steering task.', message_id='m1', project=project)
    native_messages.enqueue(session, 'Second steering task.', message_id='m2', project=project)

    async def scenario():
        backend = Backend()
        app = WorkConsole(project, backend=backend, initial_mode='assistant', storage_preferred=tmp_path / 'state')
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            app.assistant_session = str(session)
            await app.refresh_messages()
            await pilot.pause()

            output = app.query_one('#output', Transcript)
            assert 'm1' in output.message_rows
            assert 'm2' in output.message_rows

            row1 = output.message_rows['m1']
            row2 = output.message_rows['m2']

            # Phase 1: Both SAVED
            assert row1.delivery_status == 'SAVED'
            assert 'Saved' in row1.title
            assert row2.delivery_status == 'SAVED'
            assert 'Saved' in row2.title

            status_text = rendered_text(app.query_one('#message-status', Static))
            assert '2 saved' in status_text

            # Phase 2: m1 RECEIVED into context; m2 remains SAVED
            journal = init_journal(session, session_id='s')
            journal.append({
                'kind': 'user_message', 'message_id': 'm1',
                'text': 'First steering task.', 'after_sequence': 1,
            })
            await app.refresh_messages()
            await pilot.pause()

            assert output.message_rows['m1'] is row1
            assert output.message_rows['m2'] is row2
            assert row1.delivery_status == 'RECEIVED'
            assert 'Received' in row1.title
            assert row2.delivery_status == 'SAVED'
            assert 'Saved' in row2.title

            status_text = rendered_text(app.query_one('#message-status', Static))
            assert '1 saved' in status_text and '1 received' in status_text

            # Phase 3: Completed response for m1 -> RESPONSE_READY; m2 still SAVED
            journal.append({'kind': 'model_requested', 'call_id': 's/model/1'})
            journal.append({
                'kind': 'model_returned', 'call_id': 's/model/1',
                'reply': {
                    'text': 'Completed m1.', 'complete': True,
                    'proposes_delivery': True, 'tool_calls': (), 'metadata_json': '{}',
                },
            })
            journal.append({
                'kind': 'terminal', 'status': 'proposed', 'reason': 'DELIVERY_PROPOSED',
                'output': 'Completed m1.',
            })
            await app.refresh_messages()
            await pilot.pause()

            assert output.message_rows['m1'] is row1
            assert output.message_rows['m2'] is row2
            assert row1.delivery_status == 'RESPONSE_READY'
            assert 'Response ready' in row1.title
            assert row2.delivery_status == 'SAVED'
            assert 'Saved' in row2.title

            status_text = rendered_text(app.query_one('#message-status', Static))
            assert '1 saved' in status_text and '1 response ready' in status_text

            # Phase 4: m2 RECEIVED into context
            journal.append({
                'kind': 'user_message', 'message_id': 'm2',
                'text': 'Second steering task.', 'after_sequence': 5,
            })
            await app.refresh_messages()
            await pilot.pause()

            assert row1.delivery_status == 'RESPONSE_READY'
            assert row2.delivery_status == 'RECEIVED'
            assert 'Received' in row2.title

            # Phase 5: Completed response for m2 -> RESPONSE_READY
            journal.append({'kind': 'model_requested', 'call_id': 's/model/2'})
            journal.append({
                'kind': 'model_returned', 'call_id': 's/model/2',
                'reply': {
                    'text': 'Completed m2.', 'complete': True,
                    'proposes_delivery': True, 'tool_calls': (), 'metadata_json': '{}',
                },
            })
            journal.append({
                'kind': 'terminal', 'status': 'proposed', 'reason': 'DELIVERY_PROPOSED',
                'output': 'Completed m2.',
            })
            await app.refresh_messages()
            await pilot.pause()

            assert output.message_rows['m1'] is row1
            assert output.message_rows['m2'] is row2
            assert row1.delivery_status == 'RESPONSE_READY'
            assert row2.delivery_status == 'RESPONSE_READY'
            assert 'Response ready' in row2.title

            status_text = rendered_text(app.query_one('#message-status', Static))
            assert '2 response ready' in status_text

            # Verify row details contain sequence evidence and do not claim semantic answer
            detail_text = rendered_text(row1.detail_widget)
            assert 'm1' in detail_text
            assert 's/model/1' in detail_text
            assert 'Response ready' in detail_text
            assert 's/model/1' not in row1.title

    asyncio.run(scenario())


def test_row_identity_and_dedup_on_repeated_snapshots(tmp_path, monkeypatch):
    """Repeated snapshots update existing row in place without appending new rows."""
    project = tmp_path / 'project'; project.mkdir()
    session = tmp_path / 'session'
    make_session(session, project)
    native_messages.enqueue(session, 'Task message.', message_id='m1', project=project)
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path / 'state'))
    monkeypatch.setattr(relay_mesh, 'RELAY_ROOT', tmp_path / 'control')

    async def scenario():
        backend = Backend()
        app = WorkConsole(project, backend=backend, initial_mode='assistant', storage_preferred=tmp_path / 'state')
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            app.assistant_session = str(session)
            await app.refresh_messages()
            await pilot.pause()

            output = app.query_one('#output', Transcript)
            row_initial = output.message_rows['m1']
            initial_count = len(output.items)
            rows_found = list(output.query(MessageDelivery))
            assert len(rows_found) == 1

            # Poll/refresh 5 times
            for _ in range(5):
                await app.refresh_messages()
                await pilot.pause(0.02)

            assert len(output.items) == initial_count
            assert list(output.query(MessageDelivery)) == [row_initial]
            assert output.message_rows['m1'] is row_initial

    asyncio.run(scenario())


def test_reopen_recovery_with_no_send_or_inference(tmp_path, monkeypatch):
    """Restoring rows from authoritative observations on reopen does not require
    last_sent_message in memory, makes no model calls, and avoids duplicate operator content."""
    project = tmp_path / 'project'; project.mkdir()
    session = tmp_path / 'session'
    make_session(session, project)
    native_messages.enqueue(session, 'Queued steering item.', message_id='m1', project=project)
    journal = init_journal(session)
    journal.append({
        'kind': 'user_message', 'message_id': 'm1',
        'text': 'Queued steering item.', 'after_sequence': 1,
    })
    journal.append({'kind': 'model_requested', 'call_id': 's/model/1'})
    journal.append({
        'kind': 'model_returned', 'call_id': 's/model/1',
        'reply': {'text': 'Task output.', 'complete': True, 'proposes_delivery': True, 'tool_calls': (), 'metadata_json': '{}'},
    })
    journal.append({'kind': 'terminal', 'status': 'proposed', 'reason': 'DELIVERY_PROPOSED', 'output': 'Task output.'})

    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path / 'state'))
    monkeypatch.setattr(relay_mesh, 'RELAY_ROOT', tmp_path / 'control')

    # Save a demand checkpoint with history already having the 'You: ' text
    demand_rec = console_demand.save(
        None, 0, str(project), tmp_path / 'control', 'draft_saved',
        {
            'selection': {'root': str(project), 'mode': 'assistant', 'session': str(session)},
            'interface_mode': 'assistant',
            'assistant_session': str(session),
            'assistant_history': ['You: Initial question', 'tare: Initial answer', 'You: Queued steering item.'],
            'entry_history': [], 'entry_questions': [], 'entry_proposal': None,
            'entry_selection': None, 'last_result': None, 'message_draft': '', 'order_draft': {},
        },
    )
    demand_id = demand_rec['id']

    class CountingBackend(Backend):
        def __init__(self):
            super().__init__()
            self.model_calls = 0
            self.sent_messages = 0

        async def launch(self, args, root):
            self.model_calls += 1
            return await super().launch(args, root)

        def send_message(self, selection, text, message_id):
            self.sent_messages += 1
            return super().send_message(selection, text, message_id)

    async def scenario():
        backend = CountingBackend()
        app = WorkConsole(project, backend=backend, initial_mode='assistant', storage_preferred=tmp_path / 'state')
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            app.query_one('#saved-demand', Select).set_options([(demand_id, demand_id)])
            app.query_one('#saved-demand', Select).value = demand_id
            app.open_demand()
            await app.workers.wait_for_complete()
            await pilot.pause()

            # Delivery row is restored from observations
            output = app.query_one('#output', Transcript)
            assert 'm1' in output.message_rows
            row = output.message_rows['m1']
            assert row.delivery_status == 'RESPONSE_READY'
            assert 'Response ready' in row.title

            # No send or inference was triggered
            assert backend.model_calls == 0
            assert backend.sent_messages == 0
            assert app.last_sent_message is None

            # No duplicate operator content: 'You: Queued steering item.' appears once
            you_lines = [item for item in output.items
                         if isinstance(item, SelectableMessage) and 'Queued steering item.' in rendered_text(item)]
            assert len(you_lines) == 1

    asyncio.run(scenario())


def test_delayed_old_success_or_error_after_a_b_a(tmp_path, monkeypatch):
    """An observation or send started in A, delayed until after switching A -> B -> A,
    is rejected before UI mutation because generation changed."""
    project = tmp_path / 'project'; project.mkdir()
    session_a = tmp_path / 'session_a'; make_session(session_a, project, session_id='sa')
    session_b = tmp_path / 'session_b'; make_session(session_b, project, session_id='sb')
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path / 'state'))
    monkeypatch.setattr(relay_mesh, 'RELAY_ROOT', tmp_path / 'control')

    obs_started = Event()
    obs_release = Event()

    class StallingBackend(Backend):
        def __init__(self):
            super().__init__()
            self.first_call = True

        def message_observations(self, selection):
            if self.first_call and selection.session == str(session_a):
                self.first_call = False
                obs_started.set()
                assert obs_release.wait(10)
                return [{
                    'message_id': 'stale_m',
                    'text': 'Stale message from old visit',
                    'status': 'QUEUED',
                    'delivery': {
                        'status': 'SAVED',
                        'receipt_sequence': None,
                        'call_id': None,
                        'request_sequence': None,
                        'response_sequence': None,
                        'terminal_sequence': None,
                    },
                }]
            return []

    async def scenario():
        backend = StallingBackend()
        app = WorkConsole(project, backend=backend, initial_mode='assistant', storage_preferred=tmp_path / 'state')
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            # Visit A
            app.assistant_session = str(session_a)
            gen_a1 = app.navigation_generation
            # Trigger refresh which will stall in thread
            refresh_task = asyncio.create_task(app.refresh_messages())
            await asyncio.to_thread(obs_started.wait, 5)

            # Switch to B (generation advances)
            app.new_demand()
            app.assistant_session = str(session_b)
            gen_b = app.navigation_generation
            assert gen_b > gen_a1

            # Switch back to A (generation advances again: A -> B -> A)
            app.new_demand()
            app.assistant_session = str(session_a)
            gen_a2 = app.navigation_generation
            assert gen_a2 > gen_b

            # Now release the stalled observation from the first visit to A
            obs_release.set()
            await refresh_task
            await pilot.pause()

            # The stale observation from generation gen_a1 must have been rejected!
            output = app.query_one('#output', Transcript)
            assert 'stale_m' not in output.message_rows

    try:
        asyncio.run(scenario())
    finally:
        obs_release.set()


def test_one_in_flight_read_under_timer_calls(tmp_path, monkeypatch):
    """Timer and manual calls coalesce into a single in-flight read; no parallel reads."""
    project = tmp_path / 'project'; project.mkdir()
    session = tmp_path / 'session'; make_session(session, project)
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path / 'state'))
    monkeypatch.setattr(relay_mesh, 'RELAY_ROOT', tmp_path / 'control')

    in_flight = 0
    max_in_flight = 0
    total_reads = 0
    read_started = Event()
    read_release = Event()

    class ThrottledBackend(Backend):
        def message_observations(self, selection):
            nonlocal in_flight, max_in_flight, total_reads
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            total_reads += 1
            read_started.set()
            assert read_release.wait(10)
            in_flight -= 1
            return []

    async def scenario():
        backend = ThrottledBackend()
        app = WorkConsole(project, backend=backend, initial_mode='assistant', storage_preferred=tmp_path / 'state')
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            app.assistant_session = str(session)

            # Launch first read
            first_task = asyncio.create_task(app.refresh_messages())
            await asyncio.to_thread(read_started.wait, 5)

            # Fire multiple timer/manual calls while first read is still in flight
            app.poll_message_observations()
            coalesce_task1 = asyncio.create_task(app.refresh_messages())
            coalesce_task2 = asyncio.create_task(app.refresh_messages())

            assert in_flight == 1
            assert max_in_flight == 1

            # Release the in-flight read
            read_release.set()
            await asyncio.gather(first_task, coalesce_task1, coalesce_task2)
            await app.workers.wait_for_complete()
            await pilot.pause()

            assert max_in_flight == 1
            assert in_flight == 0
            # Coalesced: total reads should be at most 2, never 4
            assert total_reads <= 2

    try:
        asyncio.run(scenario())
    finally:
        read_release.set()


def test_late_send_and_edited_draft_preservation(tmp_path, monkeypatch):
    """Late send completion does not clear newly typed drafts, and does not leak
    into another conversation after switching."""
    project = tmp_path / 'project'; project.mkdir()
    session_a = tmp_path / 'session_a'; make_session(session_a, project, session_id='sa')
    session_b = tmp_path / 'session_b'; make_session(session_b, project, session_id='sb')
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path / 'state'))
    monkeypatch.setattr(relay_mesh, 'RELAY_ROOT', tmp_path / 'control')

    send_started = Event()
    send_release = Event()

    class DelayedSendBackend(Backend):
        def send_message(self, selection, text, message_id):
            send_started.set()
            assert send_release.wait(10)
            return {'status': 'SAVED'}

        def message_observations(self, selection):
            return []

    async def scenario():
        backend = DelayedSendBackend()
        app = WorkConsole(project, backend=backend, initial_mode='assistant', storage_preferred=tmp_path / 'state')
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            app.assistant_session = str(session_a)
            prompt = app.query_one('#prompt', TextArea)

            # Test 1: Draft edited while send is in flight
            prompt.load_text('Draft 1 to be sent')
            app.send_operator_message()
            await asyncio.to_thread(send_started.wait, 5)

            # User edits prompt to a newer draft while send is in flight
            prompt.load_text('Newer draft typed while sending')
            send_release.set()

            for _ in range(30):
                await pilot.pause(0.05)
                if not app.sending_message:
                    break

            assert prompt.text == 'Newer draft typed while sending'

            # Test 2: Conversation switched while send is in flight
            send_started.clear()
            send_release.clear()
            prompt.load_text('Message for Conversation A')
            app.send_operator_message()
            await asyncio.to_thread(send_started.wait, 5)

            # Switch conversation to B
            app.new_demand()
            app.assistant_session = str(session_b)
            prompt.load_text('Draft for Conversation B')

            # Release send for A
            send_release.set()
            for _ in range(30):
                await pilot.pause(0.05)
                if not app.sending_message:
                    break

            # Prompt in B is preserved
            assert prompt.text == 'Draft for Conversation B'
            # Content from A must not leak into B's transcript or history
            output = app.query_one('#output', Transcript)
            for item in output.items:
                if isinstance(item, SelectableMessage):
                    assert 'Message for Conversation A' not in rendered_text(item)
            assert not any('Message for Conversation A' in str(h) for h in app.assistant_history)

    try:
        asyncio.run(scenario())
    finally:
        send_release.set()


def test_observation_failure_preserving_proof(tmp_path, monkeypatch):
    """Observation error does not downgrade or remove already proven delivery state,
    and does not append repeated duplicate error rows."""
    project = tmp_path / 'project'; project.mkdir()
    session = tmp_path / 'session'; make_session(session, project)
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path / 'state'))
    monkeypatch.setattr(relay_mesh, 'RELAY_ROOT', tmp_path / 'control')

    fail_observations = False

    class FlakyBackend(Backend):
        def message_observations(self, selection):
            if fail_observations:
                raise OSError('Simulated disk read error on journal')
            return [{
                'message_id': 'm1',
                'text': 'Proven message',
                'status': 'IN_CONTEXT',
                'delivery': {
                    'status': 'RESPONSE_READY',
                    'receipt_sequence': 2,
                    'call_id': 's/model/1',
                    'request_sequence': 3,
                    'response_sequence': 4,
                    'terminal_sequence': 5,
                },
            }]

    async def scenario():
        nonlocal fail_observations
        backend = FlakyBackend()
        app = WorkConsole(project, backend=backend, initial_mode='session', storage_preferred=tmp_path / 'state')
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            app.query_one('#session', Select).set_options([(str(session), str(session))])
            app.query_one('#session', Select).value = str(session)
            await app.refresh_messages()
            await pilot.pause()
            await app.workers.wait_for_complete()
            await app.refresh_messages()

            output = app.query_one('#output', Transcript)
            assert 'm1' in output.message_rows
            row = output.message_rows['m1']
            assert row.delivery_status == 'RESPONSE_READY'
            initial_item_count = len(output.items)

            # Now induce observation failure
            fail_observations = True
            for _ in range(4):
                await app.refresh_messages()
                await pilot.pause(0.02)

            # Proven state preserved; no repeated error rows appended to transcript
            assert row.delivery_status == 'RESPONSE_READY'
            assert 'Response ready' in row.title
            assert 'observation unavailable' in row.title
            assert len(output.items) == initial_item_count

            detail_text = rendered_text(row.detail_widget)
            assert 'Observation note' in detail_text

            # Session status shows observation unavailable
            status_text = rendered_text(app.query_one('#message-status', Static)).lower()
            assert 'observation unavailable' in status_text

    asyncio.run(scenario())


def test_wrong_scope_rejected(tmp_path, monkeypatch):
    """Observations or sends returning for another project root or session are rejected."""
    project_a = tmp_path / 'project_a'; project_a.mkdir()
    project_b = tmp_path / 'project_b'; project_b.mkdir()
    session_a = tmp_path / 'session_a'; make_session(session_a, project_a)
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path / 'state'))
    monkeypatch.setattr(relay_mesh, 'RELAY_ROOT', tmp_path / 'control')

    read_started = Event()
    read_release = Event()

    class WrongScopeBackend(Backend):
        def message_observations(self, selection):
            read_started.set()
            assert read_release.wait(10)
            return [{
                'message_id': 'foreign_m',
                'text': 'Foreign project message',
                'status': 'QUEUED',
                'delivery': {
                    'status': 'SAVED',
                    'receipt_sequence': None,
                    'call_id': None,
                    'request_sequence': None,
                    'response_sequence': None,
                    'terminal_sequence': None,
                },
            }]

    async def scenario():
        backend = WrongScopeBackend()
        app = WorkConsole(project_a, backend=backend, initial_mode='assistant', storage_preferred=tmp_path / 'state')
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            app.assistant_session = str(session_a)

            refresh_task = asyncio.create_task(app.refresh_messages())
            await asyncio.to_thread(read_started.wait, 5)

            # Switch workspace to project_b before read returns
            app.query_one('#root', Input).value = str(project_b)
            app.prepare_workspace()
            read_release.set()

            await refresh_task
            await pilot.pause()

            # Observation for project_a must not be rendered into project_b
            output = app.query_one('#output', Transcript)
            assert 'foreign_m' not in output.message_rows

    try:
        asyncio.run(scenario())
    finally:
        read_release.set()


def test_transcript_clear_and_row_eviction(tmp_path, monkeypatch):
    """Transcript.clear() removes message rows and tracking; delivery bound eviction (50)
    pops evicted message rows from internal bookkeeping."""
    project = tmp_path / 'project'; project.mkdir()
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path / 'state'))
    monkeypatch.setattr(relay_mesh, 'RELAY_ROOT', tmp_path / 'control')

    async def scenario():
        backend = Backend()
        app = WorkConsole(project, backend=backend, initial_mode='assistant', storage_preferred=tmp_path / 'state')
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            transcript = app.query_one('#output', Transcript)
            assert len(transcript.message_rows) == 0

            item1 = {'message_id': 'm1', 'text': 'First', 'status': 'QUEUED', 'delivery': {'status': 'SAVED'}}
            item2 = {'message_id': 'm2', 'text': 'Second', 'status': 'IN_CONTEXT', 'delivery': {'status': 'RECEIVED'}}

            row1 = transcript.message_delivery(item1)
            row2 = transcript.message_delivery(item2)
            await pilot.pause()

            assert transcript.message_rows['m1'] is row1
            assert transcript.message_rows['m2'] is row2
            assert len(transcript.message_rows) == 2

            # Clear transcript
            transcript.clear()
            assert len(transcript.message_rows) == 0
            assert len(transcript.items) == 0

            # Test eviction: mount a MessageDelivery, then exceed the 50 delivery limit
            row_evictable = transcript.message_delivery(item1)
            assert transcript.message_rows['m1'] is row_evictable

            # Mount 55 delivery messages to trigger eviction of the oldest delivery item
            for i in range(55):
                transcript.message_delivery({
                    'message_id': f'extra_{i}',
                    'text': f'Extra {i}',
                    'status': 'QUEUED',
                    'delivery': {'status': 'SAVED'},
                })
            await pilot.pause()

            # row_evictable was the oldest delivery item and should have been evicted
            assert 'm1' not in transcript.message_rows
            assert len(transcript.message_rows) <= 50

    asyncio.run(scenario())
