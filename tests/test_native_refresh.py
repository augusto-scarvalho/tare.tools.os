"""Refresh a completed session without trusting old reads/tests or duplicating input."""
import hashlib
import json

import pytest

from test_native_agent import setup, Client, run, steps
from relay import native_agent, native_refresh
from tare_tools_agent_runtime import NativeBudget


@pytest.mark.parametrize('protocol', ['openai-chat', 'bedrock-converse'])
def test_refresh_requires_new_read_and_tests_and_preserves_identity_and_limits(setup, protocol):
    config, custody, root = setup
    initial_client = Client(protocol, steps())
    first = run(config, custody, initial_client)
    before = json.loads((custody/'native-agent.json').read_text())
    (root/'a.py').write_bytes(b'value = 3\n')
    client = Client(protocol, [])
    with pytest.raises(ValueError, match='native_resume_source_changed'):
        native_agent.run_session(None, None, custody, resume=True, client=client)
    assert client.calls == []
    edit = ('edit', {'repository': 'repo', 'path': 'a.py',
                    'sha256': hashlib.sha256(b'value = 3\n').hexdigest(),
                    'search': 'value = 3', 'replace': 'value = 2'})
    client = Client(protocol, [edit, ('read', {'repository': 'repo', 'path': 'a.py'}), edit,
                              ('verify', {'name': 'unit'}), ('tare_deliver', {'summary': 'Rechecked'})])
    result = native_agent.run_session(None, None, custody, resume=True, refresh=True,
        client=client, local_counter=client, follow_up='Recheck changes', message_id='refresh-1')
    assert result['status'] == 'PROPOSED' and result['session_id'] == first['session_id']
    assert result['capability_observations']['verifications']['unit']['returncode'] == 0
    assert (root/'a.py').read_text() == 'value = 2\n'
    assert result['source_changes'][0]['path'] == 'a.py'
    from relay.native_display import conversation_result
    view = conversation_result(result)
    assert 'Named checks this turn: 1 passed' in view['notice']
    assert 'Earlier checks do not verify' in view['notice']
    rows = [json.loads(line)['event'] for line in (custody/'journal.jsonl').read_text().splitlines()]
    refusals = [r['result'] for r in rows if r['kind'] == 'effect_returned' and r['result']['status'] == 'refused']
    assert any('source_changed_read_again' in r['output_json'] for r in refusals)
    assert 'Host-observed changes' in json.dumps(client.calls)
    assert 'Rerun relevant named verifiers' in json.dumps(client.calls)
    generation_path = '/chat/completions' if protocol == 'openai-chat' else '/converse'
    initial_requests = [body for path, body in initial_client.calls if path.endswith(generation_path)]
    refreshed_requests = [body for path, body in client.calls if path.endswith(generation_path)]
    assert initial_requests and refreshed_requests
    prior, refreshed = initial_requests[-1], refreshed_requests[0]
    assert refreshed['messages'][:len(prior['messages'])] == prior['messages']
    if protocol == 'openai-chat':
        assert refreshed['tools'] == prior['tools']
    else:
        assert refreshed['system'] == prior['system']
        assert refreshed['toolConfig'] == prior['toolConfig']
    after = json.loads((custody/'native-agent.json').read_text())
    assert after['budget'] == before['budget'] and after['deadline_utc'] == before['deadline_utc']
    retry = Client(protocol, [])
    repeated = native_agent.run_session(None, None, custody, resume=True, refresh=True,
        client=retry, follow_up='Recheck changes', message_id='refresh-1')
    assert repeated['model_calls'] == result['model_calls'] and retry.calls == []
    assert repeated['capability_observations']['verifications'] == {}


def test_refresh_reports_deletion_without_recreating_file_or_reusing_tests(setup):
    config, custody, root = setup
    run(config, custody, Client(steps=steps()))
    (root/'a.py').unlink()
    client = Client(steps=[('tare_deliver', {'summary': 'The file was removed.'})])
    result = native_agent.run_session(None, None, custody, resume=True, refresh=True,
                                      client=client, local_counter=client)
    assert result['source_changes'][0]['current_sha256'] is None
    assert not (root/'a.py').exists() and result['capability_observations']['verifications'] == {}
    again = Client(steps=[])
    assert native_agent.run_session(None, None, custody, resume=True, refresh=True,
                                   client=again)['status'] == 'PROPOSED'
    assert again.calls == []


def test_refresh_recovers_notification_after_interrupted_acknowledgement(setup, monkeypatch):
    config, custody, root = setup
    run(config, custody, Client(steps=steps()))
    (root/'a.py').write_text('value = 3\n')
    promote = native_refresh.accepted_sources
    def interrupt(state, journal):
        if state.get('pending_source_refresh'):
            raise OSError('interruption after durable notification')
        return promote(state, journal)
    monkeypatch.setattr(native_refresh, 'accepted_sources', interrupt)
    client = Client(steps=[])
    with pytest.raises(OSError, match='interruption after durable notification'):
        native_agent.run_session(None, None, custody, resume=True, refresh=True, client=client)
    assert client.calls == []
    state = json.loads((custody/'native-agent.json').read_text())
    assert state['pending_source_refresh'] and not state.get('accepted_sources')
    monkeypatch.setattr(native_refresh, 'accepted_sources', promote)
    resumed = Client(steps=[('read', {'repository': 'repo', 'path': 'a.py'}),
                            ('tare_deliver', {'summary': 'Read current contents.'})])
    assert native_agent.run_session(None, None, custody, resume=True,
        client=resumed, local_counter=resumed)['status'] == 'PROPOSED'
    rows = [json.loads(line)['event'] for line in (custody/'journal.jsonl').read_text().splitlines()]
    assert len([r for r in rows if r['kind'] == 'user_message']) == 1


def test_exhausted_budget_cannot_silently_acknowledge_changed_sources(setup):
    config, custody, root = setup
    run(config, custody, Client(steps=steps()), budget=NativeBudget(max_model_calls=4))
    (root/'a.py').write_text('value = 3\n')
    with pytest.raises(ValueError, match='budget exhausted'):
        native_agent.run_session(None, None, custody, resume=True, refresh=True, client=Client(steps=[]))
    state = json.loads((custody/'native-agent.json').read_text())
    assert not state.get('accepted_sources')
    with pytest.raises(ValueError, match='native_resume_source_changed'):
        native_agent.run_session(None, None, custody, resume=True, client=Client(steps=[]))
