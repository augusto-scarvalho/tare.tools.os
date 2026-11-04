"""Operational falsifiers from the independent demand/TUI review."""
import asyncio
from dataclasses import asdict

import pytest
from textual.widgets import Button, Checkbox, Select, TextArea
from relay import console_demand, demand_conduction, demand_outcome, console_recovery, relay_mesh
from relay.console_backend import Selection
from relay.native_console import WorkConsole
from test_mutation_recovery import LocalBackend
from test_demand_outcome import saved, settle
from test_work_sequence import sequence
from test_work_resume import host
from test_work_run import os_root


@pytest.mark.parametrize('top_level',[False,True])
def test_successful_stage_with_reported_obligations_requires_explicit_resolution(tmp_path,monkeypatch,top_level):
    choice=asdict(Selection(str(tmp_path),mode='research',research='study'))
    value={'id':'demand','revision':1,'root':str(tmp_path),'control_root':str(relay_mesh.RELAY_ROOT),
        'snapshot':{'selection':choice},'events':[
            {'kind':'execution_requested','selection':choice},
            {'kind':'execution_observed','selection':choice,'detail':{'status':'COMPLETED'}}]}
    observation={'state':'RECOVERED','reason':'Observed report','receipts':[],
        'result':{'status':'COMPLETED','report':{'summary':'Premise reviewed','remaining':['Verify corrected implementation.']}}}
    if top_level:
        observation['result']['remaining']=observation['result'].pop('report')['remaining']
    monkeypatch.setattr(console_recovery,'observe',lambda value: observation)
    with pytest.raises(ValueError,match='reported obligations'):
        demand_outcome.preview(value,'All done')
    view=demand_outcome.preview(value,'All done',resolutions={'1':'Corrected implementation was verified in the successor.'})
    assert not view['evidence']['blockers']
    assert view['evidence']['stages'][0]['reported_remaining']==['Verify corrected implementation.']


def test_plan_obligations_exclude_standard_preparation_hint(tmp_path,monkeypatch):
    choice=asdict(Selection(str(tmp_path),mode='plan',plan='proposal'))
    value={'id':'demand','root':str(tmp_path),'control_root':str(relay_mesh.RELAY_ROOT),
        'snapshot':{'selection':choice},'events':[{'kind':'execution_requested','selection':choice}]}
    result={'status':'REVIEWED','remaining':['Prepare Works after applying the plan.'],
        'proposal':{'plan':{'remaining':['Check corrected premise.']}},
        'review':{'review':{'remaining':['Confirm integration.']}}}
    monkeypatch.setattr(console_recovery,'observe',lambda value: {
        'state':'RECOVERED','reason':'Review saved','receipts':[],'result':result})
    stage=demand_outcome.inspect(value)['stages'][0]
    assert stage['reported_remaining']==['Check corrected premise.','Confirm integration.']
    assert stage['can_resolve']


def test_recovered_apply_uses_original_request_not_current_screen(tmp_path):
    a = asdict(Selection(str(tmp_path), mode='plan', plan='A'))
    b = {**a, 'plan': 'B'}
    value = {'root': str(tmp_path), 'events': [
        {'kind': 'execution_requested', 'selection': a},
        {'kind': 'execution_requested', 'selection': b},
        {'kind': 'graph_apply_requested', 'selection': a},
        {'kind': 'mutation_reconciled', 'selection': b,
         'detail': {'operation': 'graph_apply_requested', 'request_event': 2, 'result': {'work_ids': ['one']}}}]}
    ids, applied, plans = demand_conduction.linkage(value)
    assert ids == ['one'] and applied == {'A'} and plans[-1] == 'B'


def test_new_plan_attempt_at_same_owner_requires_fresh_apply(tmp_path):
    choice = asdict(Selection(str(tmp_path), mode='plan', plan='D'))
    value = {'root': str(tmp_path), 'events': [
        {'kind': 'execution_requested', 'selection': choice},
        {'kind': 'graph_applied', 'selection': choice, 'detail': {'work_ids': ['one']}},
        {'kind': 'execution_requested', 'selection': choice}]}
    assert demand_conduction.linkage(value) == (['one'], set(), ['D', 'D'])


def test_pre_custody_refusal_remains_visible_without_permanently_blocking_closure(sequence, monkeypatch):
    value = saved(sequence, monkeypatch); settle(sequence)
    choice = {**value['snapshot']['selection'], 'mode': 'sequence', 'sequence': str(sequence['dir'].parent/'refused')}
    value['events'] += [{'kind': 'execution_requested', 'selection': choice, 'detail': {'invocation_id': 'a'*32}},
                        {'kind': 'execution_observed', 'selection': choice, 'detail': {'status': 'REFUSED'}}]
    report = demand_outcome.preview(value, 'Both Works delivered; the rejected grouping caused no execution.')
    assert report['evidence']['stages'][0]['state'] == 'REFUSED_BEFORE_CUSTODY'
    assert not report['evidence']['blockers']


def test_addressed_failure_requires_explicit_reason_and_unknown_cannot_be_dismissed(sequence, monkeypatch):
    value = saved(sequence, monkeypatch); settle(sequence)
    choice = {**value['snapshot']['selection'], 'mode': 'research', 'research': 'old-study'}
    value['events'] += [{'kind': 'execution_requested', 'selection': choice, 'detail': {'invocation_id': 'b'*32}},
                        {'kind': 'execution_observed', 'selection': choice, 'detail': {'status': 'FAILED'}}]
    observation = {'state': 'RECOVERED', 'reason': 'Completed failed research attempt.',
                   'result': {'status': 'FAILED'}, 'receipts': [{'path': 'fixture-receipt', 'sha256': 'a'*64}]}
    monkeypatch.setattr(console_recovery, 'observe', lambda value: observation)
    with pytest.raises(ValueError): demand_outcome.preview(value, 'Request delivered')
    resolved = {'2': 'The question was answered and verified by the completed Works.'}
    report = demand_outcome.preview(value, 'Request delivered', resolutions=resolved)
    assert report['evidence']['stages'][0]['resolution'] == resolved['2']
    observation.update(state='UNKNOWN', result=None)
    with pytest.raises(ValueError, match='reconciliation'):
        demand_outcome.preview(value, 'Request delivered', resolutions=resolved)


def test_stage_navigation_keeps_current_demand_drafts_and_execution_settings(tmp_path, monkeypatch):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path.parent/'stage-state'))
    async def scenario():
        app = WorkConsole(tmp_path, backend=LocalBackend())
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(); await app.workers.wait_for_complete()
            app.checkpoint('draft_saved')
            app.query_one('#prompt', TextArea).load_text('New follow-up')
            app.query_one('#outcome-summary', TextArea).load_text('Current assessment')
            app.query_one('#outcome-remaining', TextArea).load_text('Current obligations')
            app.query_one('#outcome-resolutions', TextArea).load_text('2: Earlier question resolved')
            app.query_one('#guide-next', Checkbox).value = True
            app.query_one('#unlimited', Checkbox).value = False
            app.query_one('#demand-stage', Select).value = '0'
            app.open_demand_stage()
            selected = app.selection()
            assert selected.prompt == 'New follow-up' and selected.outcome_summary == 'Current assessment'
            assert selected.outcome_remaining == 'Current obligations' and selected.outcome_resolutions.startswith('2:')
            assert selected.guide_next and not selected.unlimited
            reopened = console_demand.read(app.demand_id, tmp_path, relay_mesh.RELAY_ROOT)
            assert reopened['snapshot']['selection'] == asdict(selected)
    asyncio.run(scenario())


def test_failed_dispatch_never_follows_a_previous_success(tmp_path, monkeypatch):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path.parent/'failed-dispatch-state'))
    class FailedBackend(LocalBackend):
        launches = 0
        async def launch(self, args, root):
            self.launches += 1
            raise OSError('launch failed')
    async def scenario():
        backend = FailedBackend(); app = WorkConsole(tmp_path, backend=backend, guided=True)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(); await app.workers.wait_for_complete()
            followed = []
            monkeypatch.setattr(app, 'follow_demand', lambda: followed.append(True))
            app.last_result = {'status': 'PROPOSED'}
            app.query_one('#prompt', TextArea).load_text('A new invocation')
            app.execute(False); await app.workers.wait_for_complete()
            assert backend.launches == 1 and not followed
            assert app.demand_state['events'][-1]['kind'] == 'execution_unobserved'
    asyncio.run(scenario())


def test_start_shortcut_respects_disabled_primary_action(tmp_path):
    async def scenario():
        app = WorkConsole(tmp_path, backend=LocalBackend())
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(); await app.workers.wait_for_complete()
            calls = []; app.execute = lambda resume: calls.append(resume)
            button = app.query_one('#start', Button); button.disabled = True
            app.action_start(); await pilot.pause()
            assert not calls
            button.disabled = False
            app.action_start(); await pilot.pause()
            assert calls == [False]
    asyncio.run(scenario())


@pytest.mark.parametrize('mode', ['session', 'research', 'parallel'])
def test_dismissing_only_failed_attempt_does_not_invent_a_delivery(tmp_path, monkeypatch, mode):
    choice = asdict(Selection(str(tmp_path), mode=mode))
    choice['batch' if mode == 'parallel' else mode] = str(tmp_path.parent/'failed-attempt')
    value = {'id': 'a'*32, 'revision': 2, 'root': str(tmp_path), 'control_root': str(relay_mesh.RELAY_ROOT),
             'snapshot': {'selection': choice}, 'events': [
                 {'kind': 'execution_requested', 'selection': choice, 'detail': {'invocation_id': 'b'*32}},
                 {'kind': 'execution_observed', 'selection': choice, 'detail': {'status': 'FAILED'}}]}
    monkeypatch.setattr(console_recovery, 'observe', lambda value: {
        'state': 'RECOVERED', 'reason': 'Failure observed', 'result': {'status': 'FAILED'}, 'receipts': []})
    with pytest.raises(ValueError, match='No delivered'):
        demand_outcome.preview(value, 'Done', resolutions={'1': 'Answered elsewhere'})


def test_failed_plan_can_be_explicitly_resolved_after_delivery_and_missing_evidence_reopens_review(sequence, monkeypatch):
    value = saved(sequence, monkeypatch); settle(sequence)
    choice = {**value['snapshot']['selection'], 'mode': 'plan', 'plan': 'failed-plan'}
    value['events'] += [{'kind': 'execution_requested', 'selection': choice, 'detail': {'invocation_id': 'b'*32}},
                        {'kind': 'execution_observed', 'selection': choice, 'detail': {'status': 'REVIEW_REQUIRED'}}]
    observation = {'state': 'RECOVERED', 'reason': 'Review failed', 'result': {'status': 'REVIEW_REQUIRED'}, 'receipts': []}
    monkeypatch.setattr(console_recovery, 'observe', lambda value: observation)
    view = demand_outcome.preview(value, 'Original request delivered by the two verified Works.',
        resolutions={'2': 'Abandoned this failed proposal; existing completed Works address the request.'})
    assert not view['evidence']['blockers']
    value['events'].append({'kind': 'demand_result_confirmed', 'selection': choice, 'detail': view})
    assert demand_conduction.next_step(value)['action'] == 'DEMAND_COMPLETE'
    observation.update(state='UNKNOWN', result=None)
    assert demand_conduction.next_step(value)['action'] == 'REVIEW_DEMAND_RESULT'


def test_strict_decoder_rejection_during_closure_check_offers_review(tmp_path, monkeypatch):
    from compute_plane.native_models import object_json
    choice = asdict(Selection(str(tmp_path)))
    value = {'root': str(tmp_path), 'control_root': str(relay_mesh.RELAY_ROOT), 'snapshot': {'selection': choice},
             'events': [{'kind': 'demand_result_confirmed', 'selection': choice,
                         'detail': {'disposition': 'FULFILLED', 'evidence': {}, 'resolutions': {}}}]}
    monkeypatch.setattr(demand_outcome, 'inspect', lambda *a, **kw: object_json(b'{'))
    assert demand_conduction.next_step(value)['action'] == 'REVIEW_DEMAND_RESULT'
