"""Cancellation stops continuation, without pretending a saved mutation rolled back."""
import asyncio
import threading

import pytest
from textual.widgets import Input, Select

from relay import planning_findings
from relay.native_console import WorkConsole
from test_mutation_recovery import LocalBackend
from test_native_plan import proposal  # noqa: F401
from test_planning_findings import reports  # noqa: F401


@pytest.mark.parametrize('start_plan', [False, True])
@pytest.mark.parametrize('stop', ['cancel', 'quit', 'none'])
def test_findings_save_retains_receipt_but_stops_next_stage(reports, monkeypatch, start_plan, stop):
    entered, release = threading.Event(), threading.Event()
    original = planning_findings.save

    def save(*args):
        entered.set()
        assert release.wait(10), 'test did not release findings save'
        return original(*args)

    monkeypatch.setattr(planning_findings, 'save', save)

    async def scenario():
        app = WorkConsole(reports[1], backend=LocalBackend())
        calls = []
        monkeypatch.setattr(app, 'execute', lambda resume: calls.append(('execute', resume)))
        monkeypatch.setattr(app, 'follow_demand', lambda: calls.append(('follow',)))
        async with app.run_test(size=(145, 58)) as pilot:
            await pilot.pause()
            app.query_one('#mode', Select).value = 'parallel'
            app.query_one('#batch', Input).value = str(reports[0])
            await app.preview_findings().wait()
            app.cancel_requested = True  # An explicit new operation clears old cancellation.
            worker = app.use_findings(start_plan=start_plan)
            try:
                assert await asyncio.to_thread(entered.wait, 10)
                assert app.busy and not app.cancel_requested
                if stop == 'cancel':
                    app.action_cancel()
                elif stop == 'quit':
                    app.action_quit()
                assert app.busy and not calls  # Completion is still unconfirmed.
            finally:
                release.set()
            await worker.wait()
            assert not app.busy
            selected = app.selection()
            assert selected.mode == 'plan'
            planning_findings.load(selected.findings, selected.findings_sha256, reports[1])
            assert any(e['kind'] == 'findings_attached' for e in app.demand_state['events'])
            assert calls == ([] if stop != 'none' else [('execute', False)] if start_plan else [('follow',)])
    asyncio.run(scenario())


@pytest.mark.parametrize('boundary', ['reconcile', 'next_step'])
def test_cancelled_guidance_does_not_navigate_and_explicit_retry_works(tmp_path, monkeypatch, boundary):
    from relay import demand_conduction
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))
    root = tmp_path/'project'
    root.mkdir()
    entered, release = threading.Event(), threading.Event()
    calls = []

    def next_step(state):
        calls.append('next_step')
        if boundary == 'next_step' and not release.is_set():
            entered.set()
            assert release.wait(10)
        return {'action': 'SUGGEST_EXPLORATION', 'prompt': 'Inspect the project', 'reason': 'Explore first'}

    monkeypatch.setattr(demand_conduction, 'next_step', next_step)

    async def scenario():
        app = WorkConsole(root, backend=LocalBackend())

        class Reconciliation:
            async def wait(self):
                if boundary == 'reconcile' and not release.is_set():
                    entered.set()
                    assert await asyncio.to_thread(release.wait, 10)

        monkeypatch.setattr(app, 'reconcile_demand', Reconciliation)
        async with app.run_test(size=(140, 52)) as pilot:
            await pilot.pause()
            app.checkpoint('draft_saved')
            before = app.selection()
            worker = app.continue_demand()
            try:
                assert await asyncio.to_thread(entered.wait, 10)
                app.action_cancel()
                # A later explicit action may clear the boolean; it must not revive this worker.
                app.cancel_requested = False
            finally:
                release.set()
            await worker.wait()
            assert app.selection() == before
            assert not any(e['kind'] == 'next_step_opened' for e in app.demand_state['events'])
            if boundary == 'reconcile':
                assert calls == []
            app.cancel_requested = True
            await app.continue_demand().wait()
            assert app.selection().mode == 'session'
            assert app.selection().prompt == 'Inspect the project'
            assert app.demand_state['events'][-1]['kind'] == 'next_step_opened'
    asyncio.run(scenario())


def test_cancel_before_findings_worker_starts_does_not_save_or_execute(reports, monkeypatch):
    def unexpected(*args):
        pytest.fail('Cancelled pending action must not save or execute')
    monkeypatch.setattr(planning_findings, 'save', unexpected)

    async def scenario():
        app = WorkConsole(reports[1], backend=LocalBackend())
        monkeypatch.setattr(app, 'execute', unexpected)
        async with app.run_test(size=(145, 58)) as pilot:
            await pilot.pause()
            app.query_one('#mode', Select).value = 'parallel'
            app.query_one('#batch', Input).value = str(reports[0])
            await app.preview_findings().wait()
            worker = app.use_findings(start_plan=True)
            app.action_cancel()  # No event-loop yield between scheduling and Cancel.
            await worker.wait()
            assert not app.selection().findings and not app.busy
    asyncio.run(scenario())


def test_quit_during_findings_assessment_waits_then_exits(tmp_path, monkeypatch):
    from relay import demand_conduction, flow_assessment
    root = tmp_path/'project'
    root.mkdir()
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))
    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr(demand_conduction, 'next_step', lambda state: {'action': 'REVIEW_FINDINGS'})

    def assess(state, step, cancellation):
        entered.set()
        assert release.wait(10)
        assert cancellation.is_cancelled()
        return {'action': 'START_PLAN', 'reason': 'Must not continue'}

    monkeypatch.setattr(flow_assessment, 'after_findings', assess)

    async def scenario():
        app = WorkConsole(root, backend=LocalBackend())
        exits = []
        monkeypatch.setattr(app, 'exit', lambda: exits.append('exit'))
        async with app.run_test(size=(140, 52)) as pilot:
            await pilot.pause()
            app.checkpoint('draft_saved')
            worker = app.continue_demand()
            try:
                assert await asyncio.to_thread(entered.wait, 10)
                app.action_quit()
                assert app.busy and not exits
            finally:
                release.set()
            await worker.wait()
            assert exits == ['exit'] and not app.busy
            assert not any(e['kind'] == 'next_step_opened' for e in app.demand_state['events'])
    asyncio.run(scenario())


@pytest.mark.parametrize('operation', ['graph', 'preparation'])
def test_cancel_during_confirmed_mutation_preserves_result_without_follow(tmp_path, monkeypatch, operation):
    root = tmp_path/'project'
    root.mkdir()
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))
    entered, release = threading.Event(), threading.Event()
    marker = root/'confirmed.txt'
    result = ({'status': 'APPLIED', 'graph_path': str(root/'graph.json'), 'work_ids': ['work-one']}
              if operation == 'graph' else
              {'status': 'PREPARED', 'readiness': {'ready': True}})

    def mutate(*args):
        entered.set()
        assert release.wait(10)
        marker.write_text('Saved')
        return result

    async def scenario():
        backend = LocalBackend()
        monkeypatch.setattr(backend, 'apply_plan' if operation == 'graph' else 'apply_preparation', mutate)
        app = WorkConsole(root, backend=backend)
        follows = []
        monkeypatch.setattr(app, 'follow_demand', lambda: follows.append(True))
        async with app.run_test(size=(140, 52)) as pilot:
            await pilot.pause()
            app.cancel_requested = True
            if operation == 'graph':
                app.query_one('#mode', Select).value = 'plan'
                app.query_one('#plan', Input).value = str(root/'plan')
                app.reviewed_plan = (str(root/'plan'), 'f'*64, str(root))
                app.apply_reviewed_plan()
            else:
                app.reviewed_preparation = (app.preparation_selection(app.selection()), {'sha256': 'f'*64})
                app.confirm_preparation()
            try:
                assert await asyncio.to_thread(entered.wait, 10)
                assert app.busy and not app.cancel_requested
                app.action_cancel()
                assert app.busy and not marker.exists()
            finally:
                release.set()
            await app.workers.wait_for_complete()
            assert marker.read_text() == 'Saved'
            assert app.last_result == result and not app.busy and not follows
            expected = 'graph_applied' if operation == 'graph' else 'work_prepared'
            assert any(e['kind'] == expected for e in app.demand_state['events'])
    asyncio.run(scenario())
