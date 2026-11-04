"""A saved delivery remains observable after the execution deadline expires."""
import json

import pytest

from relay import native_agent
from tare_tools_agent_runtime import NativeBudget
from test_native_agent import Client, deployment, setup


def test_expired_completed_session_rebuilds_report_without_provider_or_journal_changes(setup, monkeypatch):
    config, custody, _ = setup
    config['verifiers'] = {}
    client = Client(steps=[('tare_deliver', {'summary': 'Previously completed delivery.'})])
    first = native_agent.run_session(config, deployment(), custody, 'Report completion',
                                     budget=NativeBudget(max_model_calls=1), timeout=60,
                                     client=client, local_counter=client)
    assert first['status'] == 'PROPOSED'
    saved = json.loads((custody/'native-agent.json').read_text())
    journal = (custody/'journal.jsonl').read_bytes()
    provider_calls = list(client.calls)
    monkeypatch.setattr(native_agent.time, 'time', lambda: saved['deadline_utc'] + 1)
    # Recover missing derived reports from the durable terminal, as after a host crash.
    (custody/'outcome.json').unlink()
    (custody/'usage.json').unlink()
    replay = native_agent.run_session(None, None, custody, resume=True, client=client, local_counter=client)
    assert (replay['status'], replay['reason'], replay['output']) == (
        first['status'], first['reason'], first['output'])
    assert replay['model_calls'] == first['model_calls'] == 1
    assert replay['tokens'] == first['tokens']
    assert client.calls == provider_calls
    assert (custody/'journal.jsonl').read_bytes() == journal
    assert json.loads((custody/'native-agent.json').read_text())['deadline_utc'] == saved['deadline_utc']
    assert json.loads((custody/'outcome.json').read_text())['output'] == first['output']
    with pytest.raises(ValueError, match='execution budget exhausted'):
        native_agent.run_session(None, None, custody, resume=True, client=client, local_counter=client,
                                 follow_up='Do another task', message_id='late-follow-up')
    assert client.calls == provider_calls
    assert (custody/'journal.jsonl').read_bytes() == journal
