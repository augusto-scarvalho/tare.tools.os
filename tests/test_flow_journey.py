"""Cross-stage console journeys over owner state and real Git/Work fixtures."""
import asyncio
import copy
from dataclasses import asdict
import json

from textual.widgets import Input, Select, TextArea
from relay import console_demand, demand_conduction, demand_outcome, relay_mesh, work_sequence, research_flow
from relay.console_backend import Selection
from relay.native_console import WorkConsole
from test_demand_outcome import saved
from test_mutation_recovery import LocalBackend
from test_sequence_membership import add_work, supersede
from test_sequence_revision import stop_after_first
from test_work_sequence import sequence, create
from test_work_resume import host
from test_work_run import os_root
from test_research_flow import study
from test_native_agent import setup


def test_tui_replaces_remaining_work_then_resumes_without_reexecuting_completed_work(sequence, monkeypatch):
    value = saved(sequence, monkeypatch); create(sequence)
    first = copy.deepcopy(stop_after_first(sequence, monkeypatch)['items'][0])
    add_work('work-two-replacement'); supersede('work-two', 'work-two-replacement')
    # Graph replacement has already been reviewed/applied by its existing owner.
    value = console_demand.save(value['id'], value['revision'], sequence['repo'], relay_mesh.RELAY_ROOT,
        'work_replacement_applied', value['snapshot'], {'replacements': {'work-two': 'work-two-replacement'},
                                                      'work_ids': ['work-two-replacement']})
    async def scenario():
        app = WorkConsole(sequence['repo'], backend=LocalBackend())
        async with app.run_test(size=(140, 52)) as pilot:
            await pilot.pause(); await app.list_demands().wait()
            app.query_one('#saved-demand', Select).value = value['id']
            app.open_demand(); await app.workers.wait_for_complete()
            app.query_one('#mode', Select).value = 'sequence'
            app.query_one('#sequence', Input).value = str(sequence['dir'])
            app.query_one('#sequence-revision-reason', Input).value = 'Use the reviewed successor.'
            app.query_one('#sequence-revision-works', Input).value = 'work-one, work-two-replacement'
            await app.review_sequence_revision().wait()
            assert app.reviewed_sequence_revision
            app.confirm_sequence_revision(); await app.workers.wait_for_complete()
            assert app.last_result['status'] == 'READY', app.last_result
            assert len(sequence['calls']) == 5
            assert app.last_result['items'][0] == first
            # Resume is an explicit execution; the saved first Work must remain untouched.
            result = await asyncio.to_thread(work_sequence.execute, sequence['dir'])
            assert result['status'] == 'COMPLETED', result
            assert len(sequence['calls']) == 10 and result['items'][0] == first
            assert [r['work_id'] for r in result['items']] == ['work-one', 'work-two-replacement']
            await app.continue_demand().wait()
            assert app.query_one('#outcome-options').display
            app.query_one('#outcome-summary', TextArea).load_text('Both files verified; second Work used its reviewed successor.')
            await app.review_demand_outcome().wait(); await app.confirm_demand_outcome().wait()
            assert demand_outcome.latest(app.demand_state)['disposition'] == 'FULFILLED'
            assert demand_conduction.next_step(app.demand_state)['action'] == 'DEMAND_COMPLETE'
            assert len(sequence['calls']) == 10
    asyncio.run(scenario())


def test_research_continue_reviews_findings_then_restores_plan_attachment(study, monkeypatch):
    directory, root, calls, fetched, collect = study
    monkeypatch.setattr(relay_mesh, 'RELAY_ROOT', directory.parent/'control')
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(directory.parent/'journey-state'))
    assert research_flow.execute(directory, fetch=collect)['status'] == 'COMPLETED'
    async def scenario():
        app = WorkConsole(root, backend=LocalBackend(), guided=True)
        async with app.run_test(size=(140, 52)) as pilot:
            await pilot.pause()
            app.query_one('#mode', Select).value = 'research'
            app.query_one('#research', Input).value = str(directory)
            app.checkpoint('execution_requested', {'invocation_id': 'a'*32})
            app.checkpoint('execution_observed', {'status': 'COMPLETED'})
            before = len(calls)
            app.follow_demand(); await app.workers.wait_for_complete()
            assert app.reviewed_findings
            assert app.demand_state['events'][-1]['detail']['action'] == 'REVIEW_FINDINGS'
            await app.use_findings().wait(); await app.workers.wait_for_complete()
            assert app.selection().mode == 'plan' and app.selection().findings
            assert app.demand_state['events'][-1]['detail']['action'] == 'START_PLAN'
            assert len(calls) == before
            identity = app.demand_id
        app = WorkConsole(root, backend=LocalBackend())
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(); await app.list_demands().wait()
            app.query_one('#saved-demand', Select).value = identity
            app.open_demand(); await app.workers.wait_for_complete()
            assert app.selection().mode == 'plan' and app.selection().findings_sha256
            assert demand_conduction.next_step(app.demand_state)['action'] == 'START_PLAN'
            assert len(calls) == before
    asyncio.run(scenario())
