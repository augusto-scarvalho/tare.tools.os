import asyncio
from dataclasses import asdict
import json

import pytest
from textual.widgets import Select, TextArea
from compute_plane.native_journal import custody_lock, save_json
from relay import console_demand, console_recovery, native_agent, relay_mesh
from relay.console_backend import Selection
from relay.native_console import WorkConsole
from test_native_agent import Client, deployment, setup
from test_console_demand import Backend

IDENTITY = 'a'*32


def request(root, custody, identity=IDENTITY):
    selected = asdict(Selection(str(root), session=str(custody), role='explorer'))
    return {'root': str(root), 'events': [{'kind': 'execution_requested', 'selection': selected,
        'detail': {'invocation_id': identity}}], 'snapshot': {'selection': selected, 'entry_history': [],
        'entry_questions': [], 'entry_proposal': None, 'entry_selection': None, 'last_result': None}}


def finish(config, custody, protocol='openai-chat', identity=IDENTITY):
    client = Client(protocol, [('tare_deliver', {'summary': 'Observed result'})])
    result = native_agent.run_session(config, deployment(protocol), custody, 'Inspect the project',
        client=client, invocation_id=identity)
    return client, result


@pytest.mark.parametrize('protocol', ['openai-chat', 'bedrock-converse'])
def test_lost_ui_checkpoint_recovers_exact_owner_outcome_without_replay(setup, protocol):
    config, custody, root = setup
    client, result = finish(config, custody, protocol)
    value = request(root, custody)
    recovered = console_recovery.observe(value)
    assert recovered['state'] == 'RECOVERED' and recovered['result'] == result
    assert len(recovered['receipts']) == 2
    before = client.generations
    with pytest.raises(ValueError, match='invocation_already_recorded'):
        native_agent.run_session(None, None, custody, resume=True, client=client, invocation_id=IDENTITY)
    assert client.generations == before


@pytest.mark.parametrize('failure', ['old_outcome', 'no_end', 'other_project', 'corrupt', 'legacy', 'raised_after_outcome'])
def test_ambiguous_receipts_do_not_become_completion(setup, failure):
    config, custody, root = setup
    finish(config, custody)
    value = request(root, custody)
    state = native_agent.read_json(custody/'native-agent.json', 1024*1024)
    if failure == 'old_outcome':
        state['invocations'].append({**state['invocations'][0], 'id': 'b'*32})
        value['events'][0]['detail']['invocation_id'] = 'b'*32
    elif failure == 'no_end':
        state['invocations'][0].pop('ended_at')
    elif failure == 'other_project':
        state['config']['repositories'] = {'wrong': str(root.parent)}
    elif failure == 'corrupt':
        (custody/'outcome.json').write_text('{', encoding='utf-8')
    elif failure == 'legacy':
        value['events'][0]['detail'].clear()
    else:
        state['invocations'][0]['error_type'] = 'OSError'
    save_json(custody/'native-agent.json', state)
    assert console_recovery.observe(value)['state'] == 'UNKNOWN'


def test_active_writer_and_missing_custody_are_not_dead_process_proofs(setup):
    config, custody, root = setup
    value = request(root, custody)
    assert console_recovery.observe(value)['state'] == 'UNKNOWN'
    assert not custody.exists()  # Observer creates no session.
    finish(config, custody)
    with custody_lock(custody):
        assert console_recovery.observe(value)['state'] == 'BUSY'
    assert console_recovery.observe(value)['state'] == 'RECOVERED'


def test_reopen_recovers_result_once_keeps_draft_and_does_not_dispatch(setup, monkeypatch):
    config, custody, root = setup
    finish(config, custody)
    control = root.parent/'control'; control.mkdir()
    monkeypatch.setattr(relay_mesh, 'RELAY_ROOT', control)
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(root.parent/'state'))
    value = request(root, custody)
    saved = console_demand.save(None, 0, root, control, 'execution_requested', value['snapshot'], value['events'][0]['detail'])
    snapshot = saved['snapshot']; snapshot['selection']['prompt'] = 'Unsent follow-up'
    console_demand.save(saved['id'], 1, root, control, 'draft_saved', snapshot)
    async def scenario():
        backend = Backend(); app = WorkConsole(root, backend=backend)
        async with app.run_test(size=(140, 52)) as pilot:
            await pilot.pause(); await app.list_demands().wait()
            app.query_one('#saved-demand', Select).value = saved['id']
            app.open_demand()
            await app.workers.wait_for_complete()
            assert app.last_result['invocation_id'] == IDENTITY
            assert app.demand_state['events'][-1]['kind'] == 'session_reconciled'
            assert app.query_one('#prompt', TextArea).text == 'Unsent follow-up'
            assert app.reviewed_plan is None and app.reviewed_preparation is None
            revision = app.demand_revision
            await app.reconcile_demand().wait()
            assert revision == app.demand_revision
            assert not backend.calls and app.process is None
    asyncio.run(scenario())


def test_new_request_not_hidden_by_an_old_recovery_or_unobserved_event(setup):
    _, custody, root = setup
    value = request(root, custody)
    value['events'] += [{'kind': 'session_reconciled', 'detail': {'request_event': 0}},
                        {'kind': 'execution_requested', 'selection': value['snapshot']['selection'],
                         'detail': {'invocation_id': 'b'*32}}, {'kind': 'execution_unobserved'}]
    assert console_recovery.pending(value)[0] == 2
    value['events'].append({'kind': 'execution_observed'})
    assert console_recovery.pending(value) is None
