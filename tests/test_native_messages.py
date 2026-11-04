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
