"""Demand closure proves owner evidence, preserves partial results, and rejects stale review."""
import asyncio
import copy
from dataclasses import asdict
import json

import pytest
from textual.widgets import Button, Select, TextArea

from relay import console_demand, demand_outcome, relay_mesh, work_sequence
from relay.console_backend import Selection
from relay.native_console import WorkConsole
from test_mutation_recovery import LocalBackend
from test_work_sequence import sequence, create, save_graph
from test_work_resume import host
from test_work_run import os_root


def saved(case, monkeypatch):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(case['dir'].parent/'demand-state'))
    choice = asdict(Selection(str(case['repo']), mode='work', work='work-one',
                              prompt='Update both files and verify their contents.'))
    snapshot = {'selection': choice, 'entry_history': [], 'entry_questions': [],
                'entry_proposal': None, 'entry_selection': None, 'last_result': None,
                'message_draft': '', 'order_draft': {}}
    return console_demand.save(None, 0, case['repo'], relay_mesh.RELAY_ROOT, 'graph_applied',
                               snapshot, {'work_ids': ['work-one', 'work-two']})


def settle(case):
    create(case)
    result = work_sequence.execute(case['dir'])
    assert result['status'] == 'COMPLETED', result


def test_real_settlement_review_confirmation_and_reopening(sequence, monkeypatch):
    value = saved(sequence, monkeypatch)
    settle(sequence)
    calls = len(sequence['calls'])
    view = demand_outcome.preview(value, 'Both requested files now contain strict and passed their verifiers.')
    assert not view['evidence']['blockers']
    assert len(view['evidence']['works']) == 2
    assert all(w['receipt']['run_id'] for w in view['evidence']['works'])
    result = demand_outcome.confirm(value['id'], sequence['repo'], relay_mesh.RELAY_ROOT, view)
    assert result['revision'] == value['revision']+1
    reopened = console_demand.read(value['id'], sequence['repo'], relay_mesh.RELAY_ROOT)
    assert demand_outcome.latest(reopened) == view
    assert 'FULFILLED' in console_demand.guidance(reopened)
    assert len(sequence['calls']) == calls
    with pytest.raises(ValueError, match='changed'):
        demand_outcome.confirm(value['id'], sequence['repo'], relay_mesh.RELAY_ROOT, view)


def test_incomplete_or_manually_done_graph_cannot_claim_fulfilled(sequence, monkeypatch):
    value = saved(sequence, monkeypatch)
    with pytest.raises(ValueError, match='remaining'):
        demand_outcome.preview(value, 'Done')
    data = json.loads(relay_mesh.WORK_GRAPH_FILE.read_bytes())
    for node in data['nodes']:
        node['completion'].update(status='DONE', dod_satisfied=True)
    save_graph(data)
    with pytest.raises(ValueError, match='settled execution receipt'):
        demand_outcome.preview(value, 'Done')
    partial = demand_outcome.preview(value, 'Graph marked complete without execution evidence.',
                                    'Verify the outputs and reconcile the missing receipts.', disposition='PARTIAL')
    recorded = demand_outcome.confirm(value['id'], sequence['repo'], relay_mesh.RELAY_ROOT, partial)
    assert demand_outcome.latest(recorded)['disposition'] == 'PARTIAL'
    assert not sequence['calls']


@pytest.mark.parametrize('fault', ['graph', 'demand', 'summary', 'receipt'])
def test_confirmation_refuses_changed_evidence_or_assessment(sequence, monkeypatch, fault):
    value = saved(sequence, monkeypatch); settle(sequence)
    view = demand_outcome.preview(value, 'Delivered and verified both requested changes.')
    if fault == 'graph':
        data = json.loads(relay_mesh.WORK_GRAPH_FILE.read_bytes())
        data['nodes'][0]['title'] = 'A different obligation'; save_graph(data)
    elif fault == 'demand':
        console_demand.save(value['id'], value['revision'], sequence['repo'], relay_mesh.RELAY_ROOT,
                            'draft_saved', value['snapshot'])
    elif fault == 'summary':
        view = {**view, 'summary': 'An unreviewed claim'}
    else:
        from pathlib import Path
        p = Path(view['evidence']['works'][0]['receipt']['path'])
        data = json.loads(p.read_bytes()); data['candidate_commit'] = '0'*40
        p.write_text(json.dumps(data))
    before = console_demand.path(value['id']).read_bytes()
    with pytest.raises(ValueError):
        demand_outcome.confirm(value['id'], sequence['repo'], relay_mesh.RELAY_ROOT, view)
    assert console_demand.path(value['id']).read_bytes() == before


def test_unobserved_attempt_is_not_hidden_by_later_observed_event(sequence, monkeypatch):
    value = saved(sequence, monkeypatch); settle(sequence)
    selected = {**value['snapshot']['selection'], 'mode': 'session', 'session': str(sequence['dir'].parent/'missing')}
    value['events'] += [{'kind': 'execution_requested', 'selection': selected,
                         'detail': {'invocation_id': 'b'*32}},
                        {'kind': 'execution_observed', 'selection': selected, 'detail': {'status': 'PROPOSED'}}]
    report = demand_outcome.inspect(value)
    assert report['stages'][0]['state'] == 'UNKNOWN'
    with pytest.raises(ValueError):
        demand_outcome.preview(value, 'Everything done')


def test_tui_reviews_partial_result_preserves_draft_and_rejects_edited_confirmation(sequence, monkeypatch):
    value = saved(sequence, monkeypatch)
    async def scenario():
        app = WorkConsole(sequence['repo'], backend=LocalBackend())
        async with app.run_test(size=(140, 52)) as pilot:
            await pilot.pause(); await app.list_demands().wait()
            app.query_one('#saved-demand', Select).value = value['id']
            app.open_demand(); await app.workers.wait_for_complete()
            app.query_one('#outcome-disposition', Select).value = 'PARTIAL'
            app.query_one('#outcome-summary', TextArea).load_text('Prepared two Works.')
            app.query_one('#outcome-remaining', TextArea).load_text('Execute and verify both Works.')
            await app.review_demand_outcome().wait()
            assert app.reviewed_outcome
            assert not app.query_one('#confirm-demand-outcome', Button).disabled
            app.query_one('#outcome-summary', TextArea).load_text('Changed after review')
            await app.confirm_demand_outcome().wait()
            assert demand_outcome.latest(app.demand_state) is None
            await app.review_demand_outcome().wait(); await app.confirm_demand_outcome().wait()
            assert demand_outcome.latest(app.demand_state)['summary'] == 'Changed after review'
            assert not app.busy
        app = WorkConsole(sequence['repo'], backend=LocalBackend())
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(); await app.list_demands().wait()
            app.query_one('#saved-demand', Select).value = value['id']
            app.open_demand(); await app.workers.wait_for_complete()
            assert app.query_one('#outcome-remaining', TextArea).text == 'Execute and verify both Works.'
            assert demand_outcome.latest(app.demand_state)['disposition'] == 'PARTIAL'
            assert not sequence['calls']
    asyncio.run(scenario())
