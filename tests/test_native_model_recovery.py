"""Recorded transport evidence drives both explicit continuation and its explanation."""
import hashlib
import json

import pytest

from compute_plane.native_journal import NativeJournal
from compute_plane.native_models import NativeHttpClient, NativeTransportInterrupted
from relay import native_agent
from relay.native_model_recovery import inspect
from tare_tools_agent_runtime import NativeBudget
from test_native_agent import Client, deployment, setup, steps


@pytest.mark.parametrize('not_started', [True, False])
def test_explicit_cli_preflight_resume_keeps_original_failure(setup, not_started):
    from tare_tools_agent_runtime import ModelFailure
    config, directory, root = setup
    class Preflight(Client):
        def post_observed(self, path, body, control):
            return self.post(path, body, control), {'started': True, 'cleanup_verified': True}

        def post(self, path, body, control):
            if path.endswith('/chat/completions') and self.generations == 0:
                self.generations += 1
                failure = ModelFailure('CLI_VERSION_CHANGED', not_started=not_started)
                failure.transport_observation = {'cleanup_verified': True, 'started': not not_started,
                                                 'remote_stop': 'not_dispatched' if not_started else 'unknown'}
                raise failure
            return super().post(path, body, control)
    client = Preflight(steps=steps())
    first = native_agent.run_session(config, deployment(), directory, 'Change and check a.py.',
                                     client=client, local_counter=client)
    assert first['status'] == 'BLOCKED'
    assert bool(first.get('model_recovery', {}).get('available')) is not_started
    before = (directory/'journal.jsonl').read_bytes()
    ordinary = native_agent.run_session(None, None, directory, resume=True, client=client, local_counter=client)
    assert ordinary['status'] == 'BLOCKED' and client.generations == 1
    result = native_agent.run_session(None, None, directory, resume=True, resume_interrupted_model=True,
                                     client=client, local_counter=client)
    assert result['status'] == ('PROPOSED' if not_started else 'BLOCKED')
    assert (directory/'journal.jsonl').read_bytes().startswith(before)
    assert (root/'a.py').read_text() == ('value = 2\n' if not_started else 'value = 1\n')


@pytest.mark.parametrize('limit,cleanup', [(8, True), (3, True), (8, False)])
def test_interrupted_model_recovery_preserves_edit_calls_and_unknown_usage(setup, limit, cleanup):
    config, directory, root = setup

    class Interrupted(Client, NativeHttpClient):
        def post_observed(self, path, body, control):
            if path.endswith('/chat/completions') and self.generations == 2:
                self.generations += 1
                raise NativeTransportInterrupted('HTTP_INCOMPLETE_STREAM', {
                    'started': True, 'cleanup_verified': cleanup, 'remote_stop': 'unknown',
                    'http_attempts_upper_bound': 1, 'stream_events': 0, 'worker_error_type': 'URLError'})
            return self.post(path, body, control), {'started': True, 'cleanup_verified': True,
                'remote_stop': 'unknown', 'http_attempts_upper_bound': 1}

    client = Interrupted(steps=steps())
    first = native_agent.run_session(config, deployment(), directory, 'Change and check a.py.',
        client=client, local_counter=client, budget=NativeBudget(max_model_calls=limit))
    assert first['reason'] == 'UNFINISHED_INVOCATION'
    assert first['model_calls'] == 3 and (root/'a.py').read_bytes() == b'value = 2\n'
    recovery = first['model_recovery']
    assert recovery['transport_eligible'] is cleanup
    assert recovery['available'] is (cleanup and limit > 3)
    assert recovery['reason'] == ('LOCAL_CLEANUP_UNCONFIRMED' if not cleanup else
                                  'MODEL_CALL_LIMIT' if limit == 3 else 'EXPLICIT_RESUME_AVAILABLE')
    before = hashlib.sha256((directory/'journal.jsonl').read_bytes()).hexdigest()
    # Ordinary Resume is not permission to discard an interrupted response.
    ordinary = native_agent.run_session(None, None, directory, resume=True, client=client, local_counter=client)
    assert ordinary['reason'] == 'UNFINISHED_INVOCATION' and client.generations == 3
    assert hashlib.sha256((directory/'journal.jsonl').read_bytes()).hexdigest() == before
    result = native_agent.run_session(None, None, directory, resume=True, resume_interrupted_model=True,
                                     client=client, local_counter=client)
    events = [r.event() for r in NativeJournal(directory/'journal.jsonl').snapshot().records]
    abandoned = [e for e in events if e['kind'] == 'model_abandoned']
    assert len(abandoned) == int(cleanup and limit > 3)
    if cleanup and limit > 3:
        assert result['status'] == 'PROPOSED' and result['model_calls'] == 5
        assert abandoned[0]['call_id'] == first['pending_id']
        assert abandoned[0]['usage'] is None and abandoned[0]['remote_stop'] == 'unknown'
        assert len([e for e in events if e['kind'] == 'effect_requested' and e['tool_call']['name'] == 'edit']) == 1
        usage = json.loads((directory/'usage.json').read_text())
        assert usage['calls'][2]['normalized']['tokens']['total_tokens'] is None
    else:
        assert result['reason'] == 'UNFINISHED_INVOCATION' and client.generations == 3
    assert (root/'a.py').read_bytes() == b'value = 2\n'


def test_deadline_and_non_model_interruptions_never_offer_recovery(monkeypatch):
    from tare_tools_agent_runtime import ConversationLog
    journal, observations = ConversationLog(), ConversationLog()
    journal.append({'kind': 'model_requested', 'call_id': 's/model/1'})
    journal.append({'kind': 'context_summary_requested', 'call_id': 's/summary/1'})
    journal.append({'kind': 'invocation_interrupted', 'invocation_id': 's/model/1'})
    observations.append({'call_id': 's/model/1', 'purpose': 'model', 'error': 'HTTP_INCOMPLETE_STREAM',
                         'transport_calls': [{'cleanup_verified': True}]})
    monkeypatch.setattr('relay.native_model_recovery.time.time', lambda: 20)
    value = inspect(journal, observations, 's', max_model_calls=8, deadline_utc=10)
    assert value['transport_eligible'] and not value['available']
    assert value['reason'] == 'DEADLINE_EXHAUSTED' and value['remaining_model_calls'] == 6
    for pending in ['s/effect/1', 's/summary/1', 'other/model/1']:
        journal.append({'kind': 'invocation_interrupted', 'invocation_id': pending})
        assert inspect(journal, observations, 's', max_model_calls=8, deadline_utc=None) is None


def test_owner_exception_at_model_return_is_explicitly_resumable(setup, monkeypatch):
    from tare_tools_agent_runtime import ConversationRejected
    config, directory, root = setup
    client = Client(steps=steps())
    append = NativeJournal.append
    def reject_return_once(self, event):
        if event.get('kind') == 'model_returned':
            monkeypatch.setattr(NativeJournal, 'append', append)
            raise ConversationRejected('event_too_large', 'fixture event exceeds limit')
        return append(self, event)
    monkeypatch.setattr(NativeJournal, 'append', reject_return_once)
    with pytest.raises(ConversationRejected):
        native_agent.run_session(config, deployment(), directory, 'Change and check a.py.',
            client=client, local_counter=client)
    ordinary = native_agent.run_session(None, None, directory, resume=True, client=client, local_counter=client)
    assert ordinary['model_recovery']['available']
    assert ordinary['model_recovery']['owner_interrupted']
    assert client.generations == 1
    # A fresh client provides the discarded read again; no tool was executed.
    client = Client(steps=steps())
    result = native_agent.run_session(None, None, directory, resume=True, resume_interrupted_model=True,
                                     client=client, local_counter=client)
    assert result['status'] == 'PROPOSED'
    events = [r.event() for r in NativeJournal(directory/'journal.jsonl').snapshot().records]
    assert sum(e['kind'] == 'model_abandoned' for e in events) == 1
    assert sum(e['kind'] == 'effect_requested' and e['tool_call']['name'] == 'edit' for e in events) == 1
