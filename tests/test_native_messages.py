import json
from concurrent.futures import ThreadPoolExecutor
import pytest

from relay import native_agent, native_messages
from compute_plane.native_journal import custody_lock
from test_native_agent import setup, Client, deployment


@pytest.mark.parametrize('protocol', ['openai-chat', 'bedrock-converse'])
def test_correction_is_delivered_in_same_session_and_not_replayed(setup, protocol):
    config, directory, root = setup
    def incoming():
        if client.generations == 1:
            native_messages.enqueue(directory, 'Only explain now; do not edit.', message_id='one', project=root)
    client = Client(protocol, [('read', {'repository': 'repo', 'path': 'a.py'}),
                              ('tare_deliver', {'summary': 'Explained without edits.'})], incoming)
    result = native_agent.run_session(config, deployment(protocol), directory, 'Read a.py.',
                                     client=client, local_counter=client, accept_messages=True)
    assert result['status'] == 'PROPOSED'
    generated = [body for path, body in client.calls if path.endswith('/chat/completions') or path.endswith('/converse')]
    assert 'Only explain now' in json.dumps(generated[-1])
    assert native_messages.inspect(directory)['items'][0]['status'] == 'IN_CONTEXT'
    assert (root/'a.py').read_text() == 'value = 1\n'
    replay = native_agent.run_session(None, None, directory, resume=True, client=client, local_counter=client)
    assert replay['model_calls'] == result['model_calls'] == 2
    assert client.generations == 2


def saved(directory, root, enabled=True):
    directory.mkdir()
    (directory/'native-agent.json').write_text(json.dumps({'messages_enabled': enabled,
        'session_id': 's', 'config': {'repositories': {'repo': str(root)}}}))


def test_queue_concurrent_writers_retry_and_session_custody(tmp_path):
    directory=tmp_path/'session';saved(directory, tmp_path)
    retry = []
    with custody_lock(directory):
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending=[pool.submit(native_messages.enqueue, directory, 'm'+str(i), message_id=str(i)) for i in range(2)]
            for i, future in enumerate(pending):
                try:
                    future.result()
                except ValueError as exc:
                    assert str(exc) == 'native_custody_has_active_writer'
                    # A competing writer fails explicitly and can retry the same ID.
                    retry.append(i)
        for i in retry:
            native_messages.enqueue(directory, 'm'+str(i), message_id=str(i))
    native_messages.enqueue(directory, 'm0', message_id='0')
    assert len(native_messages.inspect(directory)['items']) == 2
    with pytest.raises(ValueError, match='different text'):
        native_messages.enqueue(directory, 'changed', message_id='0')
    with pytest.raises(ValueError, match='another project'):
        native_messages.enqueue(directory, 'wrong root', project=tmp_path/'elsewhere')


def test_old_session_refuses_queue_without_starting_execution(tmp_path):
    directory=tmp_path/'session';saved(directory, tmp_path, False)
    with pytest.raises(ValueError, match='does not accept'):
        native_messages.enqueue(directory, 'hello')
    assert not (directory/'messages').exists()


def test_cli_records_and_shows_pending_message(tmp_path, capsys):
    from tare_ops import main
    directory=tmp_path/'session';saved(directory, tmp_path)
    assert main(['message','--session',str(directory),'--text','café','--message-id','m']) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'SAVED'
    assert main(['message','--session',str(directory),'--show']) == 0
    assert json.loads(capsys.readouterr().out)['items'][0]['status'] == 'QUEUED'


@pytest.mark.parametrize('refresh', [False, True])
def test_late_message_waits_for_resume_and_precedes_explicit_follow_up(setup, refresh):
    config, directory, root = setup
    client = Client(steps=[('read', {'repository': 'repo', 'path': 'a.py'}),
                           ('tare_deliver', {'summary': 'First.'}),
                           ('tare_deliver', {'summary': 'Second.'})])
    native_agent.run_session(config, deployment(), directory, 'Read a.py.',
                             client=client, local_counter=client, accept_messages=True)
    native_messages.enqueue(directory, 'A late correction.', message_id='late')
    assert native_messages.inspect(directory)['items'][0]['status'] == 'QUEUED'
    if refresh:
        (root/'a.py').write_text('value = 3\n')
    with pytest.raises(ValueError, match='Queued messages precede'):
        native_agent.run_session(None, None, directory, resume=True, refresh=refresh,
                                 follow_up='Overtake.', message_id='new', client=client, local_counter=client)
    assert client.generations == 2
    result = native_agent.run_session(None, None, directory, resume=True, refresh=refresh,
                                      client=client, local_counter=client)
    assert result['status'] == 'PROPOSED' and client.generations == 3
    assert native_messages.inspect(directory)['items'][0]['status'] == 'IN_CONTEXT'


def test_queue_cannot_misacknowledge_an_existing_follow_up_id(setup):
    config, directory, _ = setup
    client = Client(steps=[('tare_deliver', {'summary': 'First.'}),
                           ('tare_deliver', {'summary': 'Second.'})])
    native_agent.run_session(config, deployment(), directory, 'Explain.',
                             client=client, local_counter=client, accept_messages=True)
    native_agent.run_session(None, None, directory, resume=True, follow_up='Original follow-up.',
                             message_id='existing', client=client, local_counter=client)
    with pytest.raises(ValueError, match='different text'):
        native_messages.enqueue(directory, 'Different message.', message_id='existing')
    assert native_messages.inspect(directory)['items'] == []


@pytest.mark.parametrize('reason,cleanup', [('HTTP_CANCELLED',False), ('HTTP_CLEANUP_UNCONFIRMED',True)])
def test_explicit_model_resume_requires_confirmed_local_transport_cleanup(setup, reason, cleanup):
    from compute_plane.native_models import NativeTransportInterrupted, NativeHttpClient
    config, directory, root = setup
    class Interrupted(Client, NativeHttpClient):
        def post_observed(self, path, body, control):
            if path.endswith('/chat/completions'):
                self.generations += 1
                raise NativeTransportInterrupted(reason, {'started':True, 'cleanup_verified':cleanup,
                    'remote_stop':'unknown', 'http_attempts_upper_bound':1})
            return self.post(path, body, control), {'started':True, 'cleanup_verified':True,
                'remote_stop':'unknown', 'http_attempts_upper_bound':1}
    client = Interrupted()
    first = native_agent.run_session(config, deployment(), directory, 'Read a.py.', client=client, local_counter=client)
    assert first['status'] == 'RECONCILIATION_REQUIRED'
    assert json.loads((directory/'usage.json').read_text())['calls'][0]['observation']['error'] == reason
    again = native_agent.run_session(None, None, directory, resume=True, resume_interrupted_model=True,
                                    client=client, local_counter=client)
    assert again['status'] == 'RECONCILIATION_REQUIRED' and client.generations == 1
    assert (root/'a.py').read_text() == 'value = 1\n'


def test_explicit_resume_does_not_auto_recover_a_later_model_failure(setup):
    from compute_plane.native_models import NativeTransportInterrupted, NativeHttpClient
    from tare_tools_agent_runtime import CancellationToken
    from test_native_agent import response
    config, directory, root = setup
    cancel = CancellationToken()
    class Interrupted(Client, NativeHttpClient):
        def post_observed(self, path, body, control):
            if path.endswith('/chat/completions'):
                self.generations += 1
                if self.generations == 1:
                    cancel.cancel()
                    return response('openai-chat','read',{'repository':'repo','path':'a.py'}), {
                        'started':True, 'cleanup_verified':True, 'remote_stop':'unknown', 'http_attempts_upper_bound':1}
                raise NativeTransportInterrupted('HTTP_CANCELLED', {'started':True,
                    'cleanup_verified':True, 'remote_stop':'unknown', 'http_attempts_upper_bound':1})
            return self.post(path, body, control), {'started':True, 'cleanup_verified':True,
                'remote_stop':'unknown', 'http_attempts_upper_bound':1}
    client = Interrupted()
    first = native_agent.run_session(config, deployment(), directory, 'Read a.py.',
                                    cancellation=cancel, client=client, local_counter=client)
    assert first['status'] == 'CANCELLED' and first['pending_id'] is None
    resumed = native_agent.run_session(None, None, directory, resume=True, resume_interrupted_model=True,
                                      client=client, local_counter=client)
    assert resumed['status'] == 'RECONCILIATION_REQUIRED' and client.generations == 2


@pytest.mark.parametrize('proof', ['dead', 'legacy', 'alive', 'missing', 'codex', 'claude', 'agy',
                                   'legacy_codex', 'legacy_claude', 'legacy_agy'])
def test_http_owner_death_explicit_resume_preserves_history_and_effects(setup, monkeypatch, proof):
    import os
    import psutil
    from compute_plane.native_journal import NativeJournal
    from compute_plane.native_models import NativeHttpClient, NativeTransportInterrupted
    from test_native_agent import steps
    config, directory, root = setup
    class Interrupted(Client, NativeHttpClient):
        def post_observed(self, path, body, control):
            if path.endswith('/chat/completions') and self.generations == 2:
                self.generations += 1
                state = json.loads((directory/'native-agent.json').read_text())
                assert state['invocations'][-1]['owner_pid'] == os.getpid()
                assert state['invocations'][-1]['owner_host']
                transport = {'started': True, 'cleanup_verified': False,
                             'remote_stop': 'unknown', 'http_attempts_upper_bound': 1, 'transport': 'isolated_stdlib_http', 'transport': 'isolated_stdlib_http'}
                if proof.removeprefix('legacy_') in ('codex', 'claude', 'agy'):
                    transport.update(vendor=proof.removeprefix('legacy_'), http_attempts_upper_bound=0)
                raise NativeTransportInterrupted('HTTP_CLEANUP_UNCONFIRMED', transport)
            return self.post(path, body, control), {'started': True, 'cleanup_verified': True,
                'remote_stop': 'unknown', 'http_attempts_upper_bound': 1, 'transport': 'isolated_stdlib_http', 'transport': 'isolated_stdlib_http'}
    client = Interrupted(steps=steps())
    first = native_agent.run_session(config, deployment(), directory, 'Change and check a.py.',
                                     client=client, local_counter=client)
    assert first['model_recovery']['reason'] == 'LOCAL_CLEANUP_UNCONFIRMED'
    state = json.loads((directory/'native-agent.json').read_text())
    if proof == 'missing' or proof.startswith('legacy'):
        state['invocations'][0].pop('owner_pid')
        assert state['invocations'][0]['ended_at'] > 0
        if proof == 'missing':
            state['invocations'][0].pop('ended_at')
    else:
        state['invocations'][0]['owner_pid'] = 12345
    (directory/'native-agent.json').write_text(json.dumps(state))
    def process(pid):
        if pid == 12345 and proof != 'alive':
            raise psutil.NoSuchProcess(pid)
        return object()
    monkeypatch.setattr(psutil, 'Process', process)
    before = (directory/'journal.jsonl').read_bytes()
    ordinary = native_agent.run_session(None, None, directory, resume=True, client=client, local_counter=client)
    assert ordinary['reason'] == 'UNFINISHED_INVOCATION' and client.generations == 3
    assert (directory/'journal.jsonl').read_bytes() == before
    result = native_agent.run_session(None, None, directory, resume=True, resume_interrupted_model=True,
                                     client=client, local_counter=client)
    assert (directory/'journal.jsonl').read_bytes().startswith(before)
    events = [r.event() for r in NativeJournal(directory/'journal.jsonl').snapshot().records]
    abandoned = [e for e in events if e['kind'] == 'model_abandoned']
    assert len(abandoned) == int(proof in ('dead', 'legacy'))
    assert sum(e['kind'] == 'effect_requested' and e['tool_call']['name'] == 'edit' for e in events) == 1
    assert (root/'a.py').read_bytes() == b'value = 2\n'
    if proof in ('dead', 'legacy'):
        assert result['status'] == 'PROPOSED'
        assert abandoned[0]['call_id'] == first['pending_id']
        assert abandoned[0]['usage'] is None and abandoned[0]['remote_stop'] == 'unknown'
        usage = json.loads((directory/'usage.json').read_text())
        assert usage['calls'][2]['normalized']['tokens']['total_tokens'] is None
    else:
        assert result['reason'] == 'UNFINISHED_INVOCATION' and client.generations == 3


def _hold_recovery_custody(directory, ready, release):
    """Separate writer, with bounded synchronization and no synthetic lock files."""
    with custody_lock(directory):
        ready.set()
        if not release.wait(20):
            raise TimeoutError('Recovery test did not release custody')


@pytest.mark.parametrize('correction', [False, True])
@pytest.mark.parametrize('proof', ['dead', 'legacy'])
def test_real_writer_contention_prevents_recovery(setup, monkeypatch, correction, proof):
    import multiprocessing
    import psutil
    from compute_plane.native_models import NativeHttpClient, NativeTransportInterrupted
    config, directory, root = setup
    class Interrupted(Client, NativeHttpClient):
        def post_observed(self, path, body, control):
            if path.endswith('/chat/completions'):
                self.generations += 1
                raise NativeTransportInterrupted('HTTP_CLEANUP_UNCONFIRMED', {
                    'started': True, 'cleanup_verified': False, 'http_attempts_upper_bound': 1, 'transport': 'isolated_stdlib_http',
                    'remote_stop': 'unknown'})
            return self.post(path, body, control), {'cleanup_verified': True}
    first = Interrupted()
    native_agent.run_session(config, deployment(), directory, 'Read a.py.', conversation=True,
                             client=first, local_counter=first)
    state_path = directory/'native-agent.json'
    state = json.loads(state_path.read_text())
    if proof == 'legacy':
        state['invocations'][0].pop('owner_pid')
        assert state['invocations'][0]['ended_at'] > 0
    else:
        state['invocations'][0]['owner_pid'] = 12345
    state_path.write_text(json.dumps(state))
    def dead(pid):
        raise psutil.NoSuchProcess(pid)
    monkeypatch.setattr(psutil, 'Process', dead)
    before = {name: (directory/name).read_bytes()
              for name in ('journal.jsonl', 'native-agent.json')}
    fresh = Client(steps=[('tare_deliver', {'summary': 'Recovered.'})])
    context = multiprocessing.get_context('spawn')
    ready, release = context.Event(), context.Event()
    writer = context.Process(target=_hold_recovery_custody, args=(directory, ready, release))
    writer.start()
    try:
        assert ready.wait(10), 'Competing writer failed to acquire custody'
        options = ({'follow_up': 'Answer this instead.', 'message_id': 'new'} if correction
                   else {'resume_interrupted_model': True})
        with pytest.raises(ValueError, match='native_custody_has_active_writer'):
            native_agent.run_session(None, None, directory, resume=True,
                                     client=fresh, local_counter=fresh, **options)
        assert fresh.generations == 0
        assert {name: (directory/name).read_bytes() for name in before} == before
    finally:
        release.set()
        writer.join(10)
        if writer.is_alive():
            writer.terminate()
            writer.join(5)
    assert writer.exitcode == 0


def test_new_http_failure_is_not_automatically_recovered_in_same_cycle(setup, monkeypatch):
    import os
    import psutil
    from compute_plane.native_journal import NativeJournal
    from compute_plane.native_models import NativeHttpClient, NativeTransportInterrupted
    config, directory, root = setup
    class Interrupted(Client, NativeHttpClient):
        def post_observed(self, path, body, control):
            if path.endswith('/chat/completions'):
                self.generations += 1
                raise NativeTransportInterrupted('HTTP_CLEANUP_UNCONFIRMED', {
                    'started': True, 'cleanup_verified': False, 'http_attempts_upper_bound': 1, 'transport': 'isolated_stdlib_http',
                    'remote_stop': 'unknown'})
            return self.post(path, body, control), {'cleanup_verified': True}
    client = Interrupted()
    first = native_agent.run_session(config, deployment(), directory, 'Read a.py.',
                                     client=client, local_counter=client)
    state_path = directory/'native-agent.json'
    state = json.loads(state_path.read_text())
    state['invocations'][0]['owner_pid'] = 12345
    state_path.write_text(json.dumps(state))
    def process(pid):
        if pid == os.getpid():
            return object()
        raise psutil.NoSuchProcess(pid)
    monkeypatch.setattr(psutil, 'Process', process)
    before = (directory/'journal.jsonl').read_bytes()
    result = native_agent.run_session(None, None, directory, resume=True, resume_interrupted_model=True,
                                     client=client, local_counter=client)
    assert client.generations == 2
    assert result['reason'] == 'UNFINISHED_INVOCATION'
    assert result['model_recovery']['reason'] == 'LOCAL_CLEANUP_UNCONFIRMED'
    assert (directory/'journal.jsonl').read_bytes().startswith(before)
    events = [r.event() for r in NativeJournal(directory/'journal.jsonl').snapshot().records]
    abandoned = [e for e in events if e['kind'] == 'model_abandoned']
    assert len(abandoned) == 1 and abandoned[0]['call_id'] == first['pending_id']
    assert result['pending_id'] != first['pending_id']
    assert not any(e['kind'] == 'effect_requested' for e in events)
