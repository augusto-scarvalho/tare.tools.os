import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from threading import Event

import pytest
from compute_plane.native_journal import save_json
from tare_tools_agent_runtime import CancellationToken
from textual.widgets import Select
from relay import console_demand, console_recovery, flow_invocation, relay_mesh
from relay import parallel_explore, research_flow, work_sequence
from relay.console_backend import Selection
from relay.native_console import WorkConsole
from test_native_agent import setup
from test_parallel_explore import exploration
from test_research_flow import study
from test_work_sequence import sequence, create
from test_work_resume import host
from test_work_run import os_root
from test_console_demand import Backend

ID = 'a'*32


def demand(root, mode, directory, identity=ID):
    field = flow_invocation.FIELDS[mode]
    selected = asdict(Selection(str(root), mode=mode, **{field: str(directory)}))
    return {'root': str(root), 'control_root': str(relay_mesh.RELAY_ROOT),
        'events': [{'kind': 'execution_requested', 'selection': selected, 'detail': {'invocation_id': identity}}],
        'snapshot': {'selection': selected, 'entry_history': [], 'entry_questions': [],
                     'entry_proposal': None, 'entry_selection': None, 'last_result': None}}


def test_newer_observed_result_is_not_replaced_by_an_older_pending_request(envelope):
    root, directory = envelope
    value = demand(root, 'parallel', directory)
    value['events'].extend([{'kind': 'execution_requested', 'selection': value['snapshot']['selection'],
        'detail': {'invocation_id': 'b'*32}}, {'kind': 'execution_observed', 'detail': {'status': 'COMPLETED'}}])
    assert console_recovery.pending(value) is None
    value['events'].append({'kind': 'draft_saved'})
    assert console_recovery.observe(value) is None


def test_batch_receipt_and_reopening_do_not_repeat_completed_questions(exploration):
    directory, root, calls, _ = exploration
    result = parallel_explore.execute(directory, invocation_id=ID)
    assert result['status'] == 'COMPLETED' and len(calls) == 2
    journals = {p: p.read_bytes() for p in directory.glob('*/journal.jsonl')}
    parallel_explore.execute(directory, invocation_id='b'*32)
    observation = console_recovery.observe(demand(root, 'parallel', directory))
    assert observation['state'] == 'RECOVERED' and observation['result'] == result
    assert len(calls) == 2 and all(p.read_bytes() == raw for p, raw in journals.items())
    with pytest.raises(FileExistsError): parallel_explore.execute(directory, invocation_id=ID)


def test_research_retains_historical_result_and_does_not_refetch(study):
    directory, root, calls, fetched, fetch = study
    result = research_flow.execute(directory, fetch=fetch, invocation_id=ID)
    assert result['status'] == 'COMPLETED'
    page = research_flow.pages(directory)[0]
    (directory/'sources'/'corpus'/(page['sha256']+'.md')).write_bytes(b'changed source')
    newer = research_flow.execute(directory, fetch=fetch, invocation_id='b'*32)
    assert newer['status'] == 'NEEDS_REVIEW'
    observed = console_recovery.observe(demand(root, 'research', directory))
    assert observed['result'] == result  # Historical receipt, not current source validation.
    assert len(calls) == len(fetched) == 1


def test_sequence_recovery_retains_two_real_settlements_without_reexecution(sequence):
    create(sequence)
    directory, root = sequence['dir'], sequence['repo']
    result = work_sequence.execute(directory, invocation_id=ID)
    assert result['status'] == 'COMPLETED' and result['completed'] == 2
    count = len(sequence['calls']); graph = relay_mesh.WORK_GRAPH_FILE.read_bytes()
    work_sequence.execute(directory, invocation_id='b'*32)
    observed = console_recovery.observe(demand(root, 'sequence', directory))
    assert observed['result'] == result and len(sequence['calls']) == count
    assert relay_mesh.WORK_GRAPH_FILE.read_bytes() == graph


def test_lost_final_receipt_remains_unknown_even_when_owner_saved_cancellation(study, monkeypatch):
    directory, root, calls, fetched, fetch = study
    real_save = flow_invocation.save_json
    def lose(path, value):
        if value.get('state') == 'FINISHED': raise OSError('lost completion write')
        real_save(path, value)
    token = CancellationToken(); token.cancel()
    with monkeypatch.context() as m:
        m.setattr(flow_invocation, 'save_json', lose)
        with pytest.raises(OSError): research_flow.execute(directory, cancellation=token, fetch=fetch, invocation_id=ID)
    assert research_flow.read(directory)['status'] == 'CANCELLED'
    assert console_recovery.observe(demand(root, 'research', directory))['state'] == 'UNKNOWN'
    with pytest.raises(FileExistsError): research_flow.execute(directory, invocation_id=ID)
    assert not calls and not fetched


@pytest.fixture
def envelope(tmp_path, monkeypatch):
    root = tmp_path/'project'; root.mkdir()
    control = tmp_path/'control'; control.mkdir()
    monkeypatch.setattr(relay_mesh, 'RELAY_ROOT', control)
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'storage'))
    directory = tmp_path/'flow'; directory.mkdir()
    save_json(directory/'state.json', {'directory': str(directory), 'root': str(root),
        'config': {'repository_root': str(root)}})
    return root, directory


def result(root, directory, mode):
    return {'root': str(root), flow_invocation.RESULT_FIELDS[mode]: str(directory), 'status': 'COMPLETED',
        'completed': 0, 'total': 0, 'workers': 1, 'items': [], 'reason': None,
        'question': 'Fixture', 'report': None, 'sources': [], 'session': str(directory/'analyst')}


@pytest.mark.parametrize('fault', ['identity', 'mode', 'root', 'control', 'end', 'result_owner', 'running', 'corrupt'])
def test_wrong_or_incomplete_envelopes_never_become_completion(envelope, fault):
    root, directory = envelope
    flow_invocation.record(directory, ID, 'parallel', 'state.json', lambda: result(root, directory, 'parallel'))
    file = flow_invocation.receipt_path(directory, ID)
    from relay.native_agent import read_json
    value = read_json(file)
    if fault == 'identity': value['invocation_id'] = 'b'*32
    elif fault == 'mode': value['mode'] = 'sequence'
    elif fault == 'root': value['repository_root'] = str(directory)
    elif fault == 'control': value['control_root'] = str(directory)
    elif fault == 'end': value.pop('finished_at')
    elif fault == 'result_owner': value['result']['batch'] = str(root)
    elif fault == 'running': value['result']['status'] = 'RUNNING'
    save_json(file, value)
    if fault == 'corrupt': file.write_text('{', encoding='utf-8')
    assert console_recovery.observe(demand(root, 'parallel', directory))['state'] == 'UNKNOWN'


def test_running_invocation_is_busy_and_observer_creates_no_missing_custody(envelope):
    root, directory = envelope
    value = demand(root, 'parallel', directory)
    assert console_recovery.observe(value)['state'] == 'UNKNOWN'
    assert not (directory/'invocation-results').exists()
    entered, release = Event(), Event()
    def action():
        entered.set(); assert release.wait(10)
        return result(root, directory, 'parallel')
    with ThreadPoolExecutor(1) as pool:
        task = pool.submit(flow_invocation.record, directory, ID, 'parallel', 'state.json', action)
        try:
            assert entered.wait(10)
            assert console_recovery.observe(value)['state'] == 'BUSY'
        finally: release.set()
        task.result()
    assert console_recovery.observe(value)['state'] == 'RECOVERED'


@pytest.mark.parametrize('mode', ['parallel', 'research', 'sequence'])
def test_tui_reopens_compound_result_without_dispatch_or_approval(envelope, mode):
    root, directory = envelope
    expected = flow_invocation.record(directory, ID, mode, 'state.json', lambda: result(root, directory, mode))
    value = demand(root, mode, directory)
    saved = console_demand.save(None, 0, root, relay_mesh.RELAY_ROOT, 'execution_requested',
        value['snapshot'], value['events'][0]['detail'])
    async def scenario():
        backend = Backend(); app = WorkConsole(root, backend=backend)
        async with app.run_test(size=(140, 52)) as pilot:
            await pilot.pause(); await app.list_demands().wait()
            app.query_one('#saved-demand', Select).value = saved['id']; app.open_demand()
            await app.workers.wait_for_complete()
            assert app.last_result == expected
            assert not backend.calls and app.process is None and app.reviewed_plan is None
            revision = app.demand_revision; await app.reconcile_demand().wait()
            assert app.demand_revision == revision
    asyncio.run(scenario())
