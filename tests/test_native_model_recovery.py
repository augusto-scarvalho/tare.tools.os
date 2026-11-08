"""Recorded transport evidence drives both explicit continuation and its explanation."""
import hashlib
import json

import pytest
from types import SimpleNamespace

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
                    'http_attempts_upper_bound': 1, 'transport': 'isolated_stdlib_http', 'stream_events': 0, 'worker_error_type': 'URLError'})
            return self.post(path, body, control), {'started': True, 'cleanup_verified': True,
                'remote_stop': 'unknown', 'http_attempts_upper_bound': 1, 'transport': 'isolated_stdlib_http', 'transport': 'isolated_stdlib_http'}

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


@pytest.mark.parametrize('case,expected', [
    ('dead', 'EXPLICIT_RESUME_AVAILABLE'), ('alive', 'LOCAL_CLEANUP_UNCONFIRMED'),
    ('missing', 'LOCAL_CLEANUP_UNCONFIRMED'), ('foreign', 'LOCAL_CLEANUP_UNCONFIRMED'),
    ('invalid', 'LOCAL_CLEANUP_UNCONFIRMED'), ('denied', 'LOCAL_CLEANUP_UNCONFIRMED'),
    ('error', 'LOCAL_CLEANUP_UNCONFIRMED'), ('wrong_call', 'LOCAL_CLEANUP_UNCONFIRMED'),
    ('ambiguous', 'LOCAL_CLEANUP_UNCONFIRMED'), ('legacy', 'LOCAL_CLEANUP_UNCONFIRMED'),
    ('codex', 'LOCAL_CLEANUP_UNCONFIRMED'), ('claude', 'LOCAL_CLEANUP_UNCONFIRMED'),
    ('agy', 'LOCAL_CLEANUP_UNCONFIRMED'), ('mixed', 'LOCAL_CLEANUP_UNCONFIRMED'),
    ('unknown_transport', 'LOCAL_CLEANUP_UNCONFIRMED'),
    ('budget', 'MODEL_CALL_LIMIT'), ('deadline', 'DEADLINE_EXHAUSTED'),
])
def test_http_cleanup_requires_death_of_exact_local_request_owner(monkeypatch, case, expected):
    import socket
    import psutil
    from tare_tools_agent_runtime import ConversationLog
    journal, observations = ConversationLog(), ConversationLog()
    journal.append({'kind': 'model_requested', 'call_id': 's/model/1'})
    journal.append({'kind': 'invocation_interrupted', 'invocation_id': 's/model/1'})
    transport = {'cleanup_verified': False, 'http_attempts_upper_bound': 1, 'transport': 'isolated_stdlib_http', 'transport': 'isolated_stdlib_http'}
    if case in ('codex', 'claude', 'agy'):
        transport['vendor'] = case
        transport['http_attempts_upper_bound'] = 0
    if case == 'unknown_transport':
        transport = {'cleanup_verified': False}
    transports = [transport]
    if case == 'mixed':
        transports.append({'vendor': 'codex', 'cleanup_verified': False})
    observations.append({'call_id': 's/model/1', 'purpose': 'model',
                         'error': 'HTTP_CLEANUP_UNCONFIRMED', 'transport_calls': transports})
    owner = {'journal_start': 0, 'journal_end': 2, 'owner_pid': 12345,
             'owner_host': socket.gethostname()}
    later = {'journal_start': 2, 'owner_pid': 54321, 'owner_host': socket.gethostname()}
    invocations = [owner, later]
    if case == 'missing':
        owner.pop('owner_pid')
    if case == 'foreign':
        owner['owner_host'] = 'not-this-host'
    if case == 'invalid':
        owner['owner_pid'] = True
    if case == 'legacy':
        invocations = None
    if case == 'wrong_call':
        owner.pop('owner_pid')  # Only a later invocation has proof; never borrow it.
    if case == 'ambiguous':
        invocations.insert(1, dict(owner))
    queried = []
    def process(pid):
        queried.append(pid)
        if case == 'alive':
            return SimpleNamespace(create_time=lambda: 1.0)
        if case == 'denied':
            raise psutil.AccessDenied(pid)
        if case == 'error':
            raise OSError('inconclusive process query')
        raise psutil.NoSuchProcess(pid)
    monkeypatch.setattr(psutil, 'Process', process)
    monkeypatch.setattr('relay.native_model_recovery.time.time', lambda: 20)
    result = inspect(journal, observations, 's', invocations=invocations,
                     max_model_calls=1 if case == 'budget' else 8,
                     deadline_utc=10 if case == 'deadline' else None)
    assert result['reason'] == expected
    assert result['available'] is (case == 'dead')
    proven = case in ('dead', 'budget', 'deadline')
    assert result['local_cleanup_verified'] is proven
    assert result['transport_eligible'] is proven
    assert not result['owner_interrupted']
    assert result['usage'] == result['remote_stop'] == 'unknown'
    assert 54321 not in queried


@pytest.mark.parametrize('locked', [False, True])
@pytest.mark.parametrize('case', [
    'legacy', 'no_end', 'null_end', 'bad_end', 'bool_end', 'nan_end', 'negative_end',
    'null_pid', 'bool_pid', 'zero_pid', 'text_pid', 'alive', 'denied', 'error', 'foreign',
    'wrong_call', 'ambiguous', 'no_invocations', 'codex', 'claude', 'agy', 'mixed',
    'empty_vendor', 'unknown_transport', 'budget', 'deadline',
])
def test_legacy_fallback_requires_lock_completion_and_unambiguous_http(monkeypatch, locked, case):
    import socket
    import psutil
    from tare_tools_agent_runtime import ConversationLog
    journal, observations = ConversationLog(), ConversationLog()
    journal.append({'kind': 'model_requested', 'call_id': 's/model/1'})
    journal.append({'kind': 'invocation_interrupted', 'invocation_id': 's/model/1'})
    transport = {'cleanup_verified': False, 'http_attempts_upper_bound': 1, 'transport': 'isolated_stdlib_http', 'transport': 'isolated_stdlib_http'}
    if case in ('codex', 'claude', 'agy'):
        transport.update(vendor=case, http_attempts_upper_bound=0, transport='official_vendor_cli')
    if case == 'empty_vendor':
        transport['vendor'] = None
    if case == 'unknown_transport':
        transport['transport'] = 'unknown'
    transports = [transport]
    if case == 'mixed':
        transports.append({'vendor': 'codex', 'cleanup_verified': False})
    observations.append({'purpose': 'model', 'call_id': 's/model/1',
                         'error': 'HTTP_CLEANUP_UNCONFIRMED', 'transport_calls': transports})
    owner = {'journal_start': 0, 'journal_end': 2, 'ended_at': 10}
    invocations = [owner]
    if case == 'no_end':
        owner.pop('ended_at')
    end_values = {'null_end': None, 'bad_end': '10', 'bool_end': True,
                  'nan_end': float('nan'), 'negative_end': -1}
    if case in end_values:
        owner['ended_at'] = end_values[case]
    pid_values = {'null_pid': None, 'bool_pid': True, 'zero_pid': 0, 'text_pid': '12345'}
    if case in pid_values:
        owner.update(owner_pid=pid_values[case], owner_host=socket.gethostname())
    if case in ('alive', 'denied', 'error', 'foreign'):
        owner.update(owner_pid=12345, owner_host=socket.gethostname())
    if case == 'foreign':
        owner['owner_host'] = 'another-host'
    if case == 'wrong_call':
        owner.update(journal_start=1)
    if case == 'ambiguous':
        invocations.append(dict(owner))
    if case == 'no_invocations':
        invocations = []
    queried = []
    def process(pid):
        queried.append(pid)
        if case == 'denied':
            raise psutil.AccessDenied(pid)
        if case == 'error':
            raise OSError('inconclusive')
        return SimpleNamespace(create_time=lambda: 1.0)
    monkeypatch.setattr(psutil, 'Process', process)
    monkeypatch.setattr('relay.native_model_recovery.time.time', lambda: 20)
    result = inspect(journal, observations, 's', invocations=invocations,
                     custody_lock_held=locked, max_model_calls=1 if case == 'budget' else 8,
                     deadline_utc=10 if case == 'deadline' else None)
    proven = locked and case in ('legacy', 'budget', 'deadline')
    expected = ('MODEL_CALL_LIMIT' if case == 'budget' else
                'DEADLINE_EXHAUSTED' if case == 'deadline' else 'EXPLICIT_RESUME_AVAILABLE')
    assert result['reason'] == (expected if proven else 'LOCAL_CLEANUP_UNCONFIRMED')
    assert result['available'] is (locked and case == 'legacy')
    assert result['local_cleanup_verified'] is proven
    assert result['transport_eligible'] is proven
    assert not result['owner_interrupted']
    assert result['usage'] == result['remote_stop'] == 'unknown'
    assert queried == ([12345] if case in ('alive', 'denied', 'error') else [])

@pytest.mark.parametrize('case, available', [
    ('owner_pid_reused', True), ('worker_gone', True), ('worker_pid_reused', True),
    ('worker_alive', False), ('worker_without_pid', False), ('owner_running', False)])
def test_an_open_tui_resumes_once_the_requests_worker_is_gone(monkeypatch, case, available):
    """The owner lives on in the same TUI: the request is over once its recorded HTTP worker is gone, and a PID
    another process now holds is no proof of life."""
    import socket
    import psutil
    from tare_tools_agent_runtime import ConversationLog
    journal, observations = ConversationLog(), ConversationLog()
    journal.append({'kind': 'model_requested', 'call_id': 's/model/1'})
    journal.append({'kind': 'invocation_interrupted', 'invocation_id': 's/model/1'})
    transport = {'cleanup_verified': False, 'http_attempts_upper_bound': 1, 'transport': 'isolated_stdlib_http',
                 'pid': 777}
    if case == 'worker_without_pid':
        transport.pop('pid')  # the kernel's exception path records none
    observations.append({'call_id': 's/model/1', 'purpose': 'model', 'error': 'HTTP_CLEANUP_UNCONFIRMED',
                         'transport_calls': [transport]})
    owner = {'journal_start': 0, 'journal_end': 2, 'owner_pid': 12345, 'owner_host': socket.gethostname(),
             'started_at': 100.0, 'ended_at': 150.0}
    if case == 'owner_running':
        owner.pop('ended_at')
    created = {12345: 200.0 if case == 'owner_pid_reused' else 50.0,
               777: {'worker_pid_reused': 300.0, 'worker_alive': 120.0}.get(case)}
    def process(pid):
        if created.get(pid) is None:
            raise psutil.NoSuchProcess(pid)
        return SimpleNamespace(create_time=lambda: created[pid])
    monkeypatch.setattr(psutil, 'Process', process)
    result = inspect(journal, observations, 's', invocations=[owner], max_model_calls=8, deadline_utc=None)
    assert result['available'] is available
    assert result['reason'] == ('EXPLICIT_RESUME_AVAILABLE' if available else 'LOCAL_CLEANUP_UNCONFIRMED')


@pytest.mark.parametrize('transport, available', [('isolated_stdlib_http', True), ('official_vendor_cli', False)])
def test_recorded_kernel_receipt_shape_decides_the_dead_http_owner(transport, available):
    """The exact receipt a coordinator left on 07/11 when its release was removed mid-request (no owner_pid)."""
    from tare_tools_agent_runtime import ConversationLog
    journal, observations = ConversationLog(), ConversationLog()
    journal.append({'kind': 'model_requested', 'call_id': 's/model/7'})
    journal.append({'kind': 'invocation_interrupted', 'invocation_id': 's/model/7'})
    receipt = {'cleanup_verified': False, 'first_content_ms': None, 'first_output_ms': None,
               'http_attempts_upper_bound': 1, 'remote_stop': 'unknown', 'started': None, 'stream_events': 0,
               'stream_observer_failures': 0, 'stream_status': 'INCOMPLETE', 'stream_usage': None,
               'timeouts': {'connect_seconds': None, 'read_seconds': None, 'request_seconds': None},
               'transport': transport}
    if transport == 'official_vendor_cli':
        receipt.update(vendor='codex', http_attempts_upper_bound=0)
    observations.append({'purpose': 'model', 'call_id': 's/model/7', 'error': 'HTTP_CLEANUP_UNCONFIRMED',
                         'transport_calls': [receipt]})
    invocations = [{'journal_start': 0, 'journal_end': 2, 'ended_at': 1794058872.1}]
    result = inspect(journal, observations, 's', invocations=invocations, custody_lock_held=True,
                     max_model_calls=None, deadline_utc=None)
    assert result['available'] is available
    assert result['reason'] == ('EXPLICIT_RESUME_AVAILABLE' if available else 'LOCAL_CLEANUP_UNCONFIRMED')
