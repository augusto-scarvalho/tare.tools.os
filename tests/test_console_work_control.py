import asyncio
from dataclasses import replace

import pytest
from textual.widgets import Button, Input

from relay.console_backend import Selection
from relay.native_console import WorkConsole
from relay import console_demand, console_mutation
from test_native_console import FakeBackend


class ControlBackend(FakeBackend):
    def __init__(self, status='PAUSED'):
        super().__init__()
        self.status = status
        self.writes = []
        self.inspections = []

    def work_control(self, selected):
        self.inspections.append(selected)
        return {'status': self.status, 'token': 'current-token', 'record': {'receiver':'fixture-custody'}}

    def control_work(self, selected, operation, token, note):
        assert token == 'current-token'
        self.writes.append((selected, operation, token, note))
        self.status = {'pause': 'PAUSE_REQUESTED', 'resume': 'RESUME_REQUESTED',
                       'intervene': 'PAUSED'}[operation]
        return self.work_control(selected)


def test_busy_pause_uses_active_selection_and_deduplicates_clicks(tmp_path):
    async def scenario():
        backend = ControlBackend('RUNNING')
        app = WorkConsole(tmp_path, backend=backend, initial_mode='work')
        async with app.run_test(size=(132, 44)) as pilot:
            await app.workers.wait_for_complete()
            selected = Selection(str(tmp_path), mode='work', work='active-work')
            app.selection = lambda: replace(selected, work='edited-work')
            app.active = selected
            app.last_result = {'status': 'RUNNING', 'sentinel': True}
            checkpoints = []
            app.checkpoint = lambda *a, **k: checkpoints.append((a, k))
            async def confirm(kind, detail, action, *args, **kwargs):
                assert kwargs['selection'] == selected
                await asyncio.sleep(0)
                return action(*args)
            app.confirmed_mutation = confirm
            app.controls(True)
            await pilot.pause()
            button = app.query_one('#pause-work', Button)
            assert not button.disabled
            assert app.screen.region.contains_region(button.region)
            assert app.query_one('#work-controls').max_scroll_x == 0
            app.request_work_control('pause')
            app.request_work_control('pause')
            await app.workers.wait_for_complete()
            assert len(backend.writes) == 1 and backend.writes[0][0] == selected
            assert app.busy and app.query_one('#resume', Button).disabled
            assert app.last_result == {'status': 'RUNNING', 'sentinel': True}
            assert 'Finishing the current round' in str(app.query_one('#work-control-status').render())
            app.controls(False)
    asyncio.run(scenario())


@pytest.mark.parametrize('status,expected_writes', [('PAUSED', 1), ('RESUME_REQUESTED', 0), ('RUNNING', 0)])
def test_explicit_resume_cas_before_execution(tmp_path, status, expected_writes):
    async def scenario():
        backend = ControlBackend(status)
        app = WorkConsole(tmp_path, backend=backend, initial_mode='work')
        async with app.run_test(size=(132, 44)):
            await app.workers.wait_for_complete()
            selected = Selection(str(tmp_path), mode='work', work='saved-work')
            app.selection = lambda: selected
            events = []
            app.checkpoint = lambda *a, **k: None
            async def confirm(kind, detail, action, *args, **kwargs):
                assert kind == 'work_control_requested' and detail['token'] == 'current-token'
                events.append('write')
                return action(*args)
            app.confirmed_mutation = confirm
            app.execute = lambda resume: events.append(('execute', resume))
            app.request_work_control('inspect')
            await app.workers.wait_for_complete()
            assert not events and not backend.launches
            app.resume_selected()
            app.resume_selected()
            await app.workers.wait_for_complete()
            assert len(backend.writes) == expected_writes
            assert events == (['write'] if expected_writes else []) + [('execute', True)]
    asyncio.run(scenario())


def test_changed_selection_refuses_before_control_write(tmp_path):
    async def scenario():
        backend = ControlBackend()
        app = WorkConsole(tmp_path, backend=backend, initial_mode='work')
        async with app.run_test(size=(132, 44)):
            await app.workers.wait_for_complete()
            selected = Selection(str(tmp_path), mode='work', work='old')
            app.selection = lambda: selected
            app.execute = lambda *a: pytest.fail('Stale selection executed')
            app.resume_selected()
            app.selection = lambda: replace(selected, work='new')
            await app.workers.wait_for_complete()
            assert not backend.writes
            assert 'Selection changed' in str(app.query_one('#status').render())
    asyncio.run(scenario())


def test_declined_reconsider_enablement_and_stale_selection(tmp_path):
    async def scenario():
        app = WorkConsole(tmp_path, backend=ControlBackend(), initial_mode='work')
        async with app.run_test(size=(132, 44)):
            await app.workers.wait_for_complete()
            selected = Selection(str(tmp_path), mode='work', work='saved')
            app.selection = lambda: selected
            view = {'status': 'HUMAN_DECLINED', 'request_sha256': 'old-digest'}
            app.reviewed_decision = (app.decision_selection(selected), view)
            app.update_mode()
            assert not app.query_one('#reconsider-decision', Button).disabled
            assert app.query_one('#approve-decision', Button).disabled
            assert app.query_one('#decline-decision', Button).disabled
            app.selection = lambda: replace(selected, graph_path='different.json')
            app.answer_decision('reconsider')
            assert app.reviewed_decision is None and not app.busy
            assert not app.backend.writes and not app.backend.launches
    asyncio.run(scenario())


def test_note_byte_bound_and_saved_copy(tmp_path):
    async def scenario():
        backend = ControlBackend()
        app = WorkConsole(tmp_path, backend=backend, initial_mode='work')
        async with app.run_test(size=(132, 44)):
            await app.workers.wait_for_complete()
            selected = Selection(str(tmp_path), mode='work', work='saved')
            app.selection = lambda: selected
            app.reviewed_work_control = (selected, backend.work_control(selected))
            app.checkpoint = lambda *a, **k: None
            async def confirm(kind, detail, action, *args, **kwargs):
                return action(*args)
            app.confirmed_mutation = confirm
            app.query_one('#work-note', Input).value = 'é' * 4097
            app.request_work_control('intervene')
            assert not backend.writes and not app.busy
            app.query_one('#work-note', Input).value = 'Review the pending check.'
            app.request_work_control('intervene')
            await app.workers.wait_for_complete()
            assert len(backend.writes) == 1
            assert 'Note saved for the next agent round.' in str(app.query_one('#status').render())
    asyncio.run(scenario())


def test_pause_and_decline_guidance_and_completion_mapping():
    assert console_mutation.OPERATIONS['work_control_requested'] == 'work_control_observed'
    paused = {'events': [{'kind': 'execution_observed', 'detail': {
        'status': 'HUMAN_DECLINED', 'operational_control': {'status': 'PAUSED'}}}]}
    assert 'Work paused. You can close this TUI.' in console_demand.guidance(paused)
    declined = {'events': [{'kind': 'decision_observed', 'detail': {'status': 'HUMAN_DECLINED'}}]}
    assert 'reconsider' in console_demand.guidance(declined)


def test_reopen_observes_without_execution_or_mutation(tmp_path, monkeypatch):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path / 'state'))
    project = tmp_path / 'project'; project.mkdir()
    async def scenario():
        backend = ControlBackend()
        app = WorkConsole(project, backend=backend, initial_mode='work')
        async with app.run_test(size=(132, 44)):
            await app.workers.wait_for_complete()
            app.query_one('#work-id', Input).value = 'saved-work'
            app.checkpoint('draft_saved')
            identity = app.demand_id
            from textual.widgets import Select
            saved = app.query_one('#saved-demand', Select)
            saved.set_options([('Saved Work', identity)])
            saved.value = identity
            app.open_demand()
            await app.workers.wait_for_complete()
            assert app.selection().work == 'saved-work'
            assert not backend.launches and not backend.writes
    asyncio.run(scenario())


def test_reconsider_only_reopens_and_requires_fresh_review(tmp_path, monkeypatch):
    monkeypatch.setattr('relay.human_wait.render', lambda view: view['status'])
    async def scenario():
        backend = ControlBackend()
        app = WorkConsole(tmp_path, backend=backend, initial_mode='work')
        async with app.run_test(size=(132, 44)):
            await app.workers.wait_for_complete()
            selected = Selection(str(tmp_path), mode='work', work='saved')
            app.selection = lambda: selected
            app.reviewed_decision = (app.decision_selection(selected),
                {'status': 'HUMAN_DECLINED', 'request_sha256': 'old'})
            answers = []
            def answer(selection, digest, decision, note):
                answers.append((selection, digest, decision))
                return {'status': 'WAITING_FOR_HUMAN', 'request_sha256': 'new'}
            backend.answer_decision = answer
            app.checkpoint = lambda *a, **k: None
            async def confirm(kind, detail, action, *args, **kwargs):
                assert kind == 'decision_requested'
                return action(*args)
            app.confirmed_mutation = confirm
            app.answer_decision('reconsider')
            app.answer_decision('reconsider')
            await app.workers.wait_for_complete()
            assert answers == [(selected, 'old', 'reconsider')]
            assert app.reviewed_decision is None
            assert app.query_one('#approve-decision', Button).disabled
            assert 'Decision reopened. Review and answer again.' in str(app.query_one('#status').render())
            assert not backend.launches
    asyncio.run(scenario())


def test_confirmed_mutation_keeps_serialization_after_cancellation(tmp_path, monkeypatch):
    import threading
    from relay.demand_console import DemandConsole
    entered, release = threading.Event(), threading.Event()
    events = []
    def record(value, action):
        return action()
    monkeypatch.setattr(console_mutation, 'record', record)
    class Harness:
        def checkpoint(self, kind, detail, **kwargs):
            events.append(('request', detail['mutation_id']))
            return {}
    def first():
        entered.set()
        if not release.wait(5):
            raise AssertionError('Timed out waiting for test release')
        events.append('first-finished')
        return {'status': 'PAUSED'}
    async def scenario():
        owner = Harness()
        task = asyncio.create_task(DemandConsole.confirmed_mutation(owner,
            'work_control_requested', {}, first))
        try:
            for _ in range(200):
                if entered.is_set():
                    break
                await asyncio.sleep(.01)
            assert entered.is_set()
            task.cancel()
            second = asyncio.create_task(DemandConsole.confirmed_mutation(owner,
                'work_control_requested', {}, lambda: {'status': 'PAUSED'}))
            await asyncio.sleep(.01)
            assert len(events) == 1
            task.cancel()
            await asyncio.sleep(.01)
            assert len(events) == 1
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            await second
            assert events[1] == 'first-finished'
            assert events[0][1] != events[2][1]
        finally:
            release.set()
    asyncio.run(scenario())


def test_explicit_resume_leaves_lost_landing_ack_to_existing_work_owner(tmp_path):
    async def scenario():
        backend = ControlBackend()
        def moved(selection):
            raise ValueError('WORK_CONTROL_TARGET_MOVED')
        backend.work_control = moved
        app = WorkConsole(tmp_path, backend=backend, initial_mode='work')
        async with app.run_test(size=(132, 44)):
            await app.workers.wait_for_complete()
            selected = Selection(str(tmp_path), mode='work', work='saved')
            app.selection = lambda:selected
            executions = []
            app.execute = lambda resume:executions.append(resume)
            app.resume_selected()
            await app.workers.wait_for_complete()
            assert executions == [True] and backend.writes == []
    asyncio.run(scenario())


@pytest.mark.parametrize('size', [(132,44),(80,24)])
def test_paused_note_and_resume_controls_fit_small_terminal(tmp_path, size):
    async def scenario():
        app = WorkConsole(tmp_path,backend=ControlBackend(),initial_mode='work')
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            selected=Selection(str(tmp_path),mode='work',work='saved')
            app.selection=lambda:selected
            app.reviewed_work_control=(selected,app.backend.work_control(selected))
            app.update_mode(); await pilot.pause()
            for identity in ('work-note','save-work-note','resume','pause-work'):
                widget=app.query_one('#'+identity)
                assert app.screen.region.contains_region(widget.region),identity
            assert app.query_one('#output').region.height>=8
            assert app.query_one('#work-note-row').max_scroll_x==0
            app.reviewed_work_control=(selected,{'status':'PAUSED','record':{'receiver':None}})
            app.update_mode(); await pilot.pause()
            assert not app.query_one('#work-note-row').display
            assert app.query_one('#save-work-note',Button).disabled
            app.reviewed_decision=(app.decision_selection(selected),{'status':'HUMAN_DECLINED'})
            app.update_mode(); await pilot.pause()
            assert app.screen.region.contains_region(app.query_one('#reconsider-decision').region)
            assert not app.query_one('#approve-decision').display
    asyncio.run(scenario())


def test_late_pause_reply_cannot_replace_the_finished_work_status(tmp_path):
    async def scenario():
        backend=ControlBackend('RUNNING')
        app=WorkConsole(tmp_path,backend=backend,initial_mode='work')
        async with app.run_test(size=(132,44)):
            await app.workers.wait_for_complete()
            selected=Selection(str(tmp_path),mode='work',work='saved')
            app.selection=lambda:selected; app.active=selected
            app.checkpoint=lambda *a,**k:None
            async def confirmed(kind,detail,action,*args,**kwargs):
                result=action(*args)
                app.last_result={'work_id':'saved','status':'PAUSED',
                    'operational_control':{'status':'PAUSED','token':'acknowledged',
                                           'record':{'receiver':'fixture-custody'}}}
                app.controls(False)
                return result
            app.confirmed_mutation=confirmed
            app.controls(True)
            app.request_work_control('pause')
            await app.workers.wait_for_complete()
            assert 'Work paused.' in str(app.query_one('#status').render())
            assert 'Finishing' not in str(app.query_one('#work-control-status').render())
            assert app.last_result['status']=='PAUSED'
    asyncio.run(scenario())


@pytest.mark.parametrize('status',['PAUSED','FAILED','SETTLED'])
def test_execution_receipt_replaces_the_previous_resume_control_message(tmp_path, status):
    import json
    result={'work_id':'saved','status':status,'stage':'finished','duration_ms':1}
    if status=='PAUSED':
        result['operational_control']={'status':'PAUSED','token':'host-ack',
                                       'record':{'receiver':'fixture-custody'}}
    class Finished:
        returncode=1 if status=='FAILED' else 0
        async def communicate(self):
            return json.dumps(result).encode(),b''
    class CompletingBackend(ControlBackend):
        async def launch(self,args,root):
            self.launches.append(args)
            return Finished()
    async def scenario():
        backend=CompletingBackend()
        app=WorkConsole(tmp_path,backend=backend,initial_mode='work')
        async with app.run_test(size=(132,44)) as pilot:
            await app.workers.wait_for_complete()
            selected=Selection(str(tmp_path),mode='work',work='saved')
            app.selection=lambda:selected
            app.prepare_workspace=lambda:None
            app.checkpoint=lambda *a,**k:None
            async def confirmed(kind,detail,action,*args,**kwargs):
                return action(*args)
            app.confirmed_mutation=confirmed
            app.resume_selected()
            await app.workers.wait_for_complete(); await pilot.pause()
            assert backend.launches and app.last_result['status']==status
            message=str(app.query_one('#work-control-status').render())
            assert 'Resume saved' not in message
            assert 'Work paused.' in message if status=='PAUSED' else message==''
    asyncio.run(scenario())


def test_idle_pause_never_enqueues_a_request_without_a_local_runner(tmp_path):
    async def scenario():
        backend=ControlBackend('RUNNING')
        app=WorkConsole(tmp_path,backend=backend,initial_mode='work')
        async with app.run_test(size=(132,44)):
            await app.workers.wait_for_complete()
            selected=Selection(str(tmp_path),mode='work',work='saved')
            app.selection=lambda:selected
            app.update_mode()
            assert app.query_one('#pause-work',Button).disabled
            app.request_work_control('pause')
            await app.workers.wait_for_complete()
            assert not backend.writes and not backend.launches
    asyncio.run(scenario())
