import asyncio
from dataclasses import asdict
import json
import sys

import pytest
from compute_plane.native_journal import save_json
from graph_backlog.jsonutil import compute_revision_hash
from relay import console_demand, demand_conduction, native_plan, relay_mesh, work_intake
from relay.console_backend import Selection
from relay.native_console import WorkConsole
from textual.widgets import Select, Input, TextArea
from test_native_plan import proposal as plan_fixture
from test_mutation_recovery import LocalBackend
from test_work_sequence import sequence
from test_work_resume import host
from test_work_run import os_root


def demand(root, ids, control, **kw):
    choice = asdict(Selection(str(root), mode='work', work=ids[0] if ids else '', **kw))
    return {'id': 'a'*32, 'root': str(root), 'control_root': str(control),
        'snapshot': {'selection': choice}, 'events': [
            {'kind': 'graph_applied', 'selection': choice, 'detail': {'work_ids': ids}}]}


def test_selects_only_linked_unblocked_works_and_advances_after_real_settlement(sequence, capsys):
    import tare_ops
    value = demand(sequence['repo'], ['work-two', 'work-one'], relay_mesh.RELAY_ROOT)
    for key in ['work-one', 'work-two']:
        step = demand_conduction.next_step(value)
        assert step['action'] == 'START_WORK' and step['work_id'] == key
        assert len(step['done']) == (0 if key == 'work-one' else 1)
        assert tare_ops.main(['run', key, '--repo-root', str(sequence['repo']),
            '--native-deployment', str(sequence['host'][2]), '--unlimited']) == 0
        result = json.loads(capsys.readouterr().out); assert result['status'] == 'SETTLED', result
    calls = len(sequence['calls'])
    assert calls == 10
    for _ in range(2):
        assert demand_conduction.next_step(value)['action'] == 'LINKED_WORKS_DONE'
    assert len(sequence['calls']) == calls


def test_external_blocker_is_reported_without_adopting_it(sequence):
    value = demand(sequence['repo'], ['work-two'], relay_mesh.RELAY_ROOT)
    step = demand_conduction.next_step(value)
    assert step['action'] == 'BLOCKED' and 'work_id' not in step
    assert step['blocked'][0]['prerequisites'][0]['id'] == 'work-one'
    assert not sequence['calls']


@pytest.mark.parametrize('fault', ['missing', 'wrong-root', 'wrong-control', 'unconfirmed', 'superseded'])
def test_changed_scope_and_uncertain_execution_never_select_a_new_run(sequence, fault):
    value = demand(sequence['repo'], ['work-one'], relay_mesh.RELAY_ROOT)
    if fault == 'missing': value['events'][0]['detail']['work_ids'] = ['gone']
    elif fault == 'wrong-root': value['root'] += '-different'
    elif fault == 'wrong-control': value['control_root'] += '-different'
    elif fault == 'unconfirmed': value['events'].append({'kind': 'execution_requested'})
    else:
        graph = json.loads(relay_mesh.WORK_GRAPH_FILE.read_bytes())
        graph['nodes'][0]['completion']['status'] = 'SUPERSEDED'
        graph['revision'] = compute_revision_hash(graph); save_json(relay_mesh.WORK_GRAPH_FILE, graph)
    try: result = demand_conduction.next_step(value)
    except ValueError: result = {'action': 'BLOCKED'}
    assert result['action'] in {'BLOCKED', 'CHECK_RECOVERY'} and not sequence['calls']


def test_unapplied_successor_requires_plan_review_before_existing_work(sequence):
    value = demand(sequence['repo'], ['work-one'], relay_mesh.RELAY_ROOT)
    value['events'] += [
        {'kind': 'execution_requested', 'selection': {**value['snapshot']['selection'], 'mode': 'plan', 'plan': 'successor'}},
        {'kind': 'execution_observed'}]
    assert demand_conduction.next_step(value)['action'] == 'REVIEW_PLAN'
    assert not sequence['calls']


def test_tui_continues_review_apply_prepare_and_start_without_discarding_draft(plan_fixture, monkeypatch):
    from test_source_admission import git
    directory, graph, proposal, base = plan_fixture
    root = directory.parent/'project'
    git(root, 'init', '-q');git(root, 'config','user.name','Test');git(root,'config','user.email','test@example.invalid')
    git(root,'add','a.py');git(root,'commit','-qm','initial')
    monkeypatch.setattr(relay_mesh,'WORK_GRAPH_FILE',graph)
    monkeypatch.setattr(relay_mesh,'RELAY_ROOT',graph.parent)
    monkeypatch.setenv('TARE_STORAGE_ROOT',str(graph.parent/'state'))
    async def scenario():
        app = WorkConsole(root, backend=LocalBackend())
        async with app.run_test(size=(140,52)) as pilot:
            await pilot.pause();app.query_one('#mode',Select).value='plan';app.query_one('#plan',Input).value=str(directory)
            app.query_one('#message',TextArea).load_text('Keep this note')
            app.checkpoint('draft_saved')
            await app.continue_demand().wait()
            assert app.reviewed_plan and app.demand_state['events'][-1]['detail']['action']=='REVIEW_PLAN'
            app.apply_reviewed_plan()
            while app.busy:await pilot.pause(.05)
            assert app.last_result['status']=='APPLIED'
            await app.continue_demand().wait()
            assert app.demand_state['events'][-1]['detail']['action']=='BLOCKED'
            # The external prerequisite becomes satisfied through the graph owner.
            data=json.loads(graph.read_bytes());data['nodes'][0]['completion'].update(status='DONE',dod_satisfied=True)
            data['revision']=compute_revision_hash(data);save_json(graph,data)
            await app.continue_demand().wait()
            assert app.demand_state['events'][-1]['detail']['action']=='PREPARE_WORK'
            assert app.selection().work=='plan-fixture-change'
            assert app.query_one('#intake-criteria',TextArea).text=='Value equals two'
            app.query_one('#intake-criteria',TextArea).load_text('Operator edited criterion')
            app.query_one('#intake-argv',TextArea).load_text(sys.executable+'\n-m\npytest\n-q')
            await app.continue_demand().wait()
            assert app.query_one('#intake-criteria',TextArea).text=='Operator edited criterion'
            await app.preview_preparation(intake=True).wait();app.confirm_preparation()
            while app.busy:await pilot.pause(.05)
            assert app.last_result['status']=='PREPARED'
            identity=app.demand_id
        app=WorkConsole(root,backend=LocalBackend())
        async with app.run_test(size=(140,52)) as pilot:
            await pilot.pause();await app.list_demands().wait();app.query_one('#saved-demand',Select).value=identity
            app.open_demand();await app.workers.wait_for_complete()
            await app.continue_demand().wait()
            assert app.demand_state['events'][-1]['detail']['action']=='START_WORK'
            assert app.query_one('#message',TextArea).text=='Keep this note'
            assert app.reviewed_preparation is None and app.process is None
    asyncio.run(scenario())


def test_existing_human_wait_is_opened_without_starting_new_work(sequence, capsys):
    from test_human_wait import pause
    host = sequence['host']
    _, wait = pause(host, capsys)
    value = demand(host[1], ['work-one'], relay_mesh.RELAY_ROOT)
    before = len(host[3].generations)
    result = demand_conduction.next_step(value)
    assert result['action'] == 'HUMAN_DECISION'
    assert len(host[3].generations) == before


def test_changed_selection_during_next_step_discards_result(tmp_path, monkeypatch):
    import threading
    root=tmp_path/'project';root.mkdir();control=tmp_path/'control';control.mkdir()
    monkeypatch.setattr(relay_mesh,'RELAY_ROOT',control)
    monkeypatch.setenv('TARE_STORAGE_ROOT',str(tmp_path/'state'))
    started,release=threading.Event(),threading.Event()
    def delayed(value):
        started.set();assert release.wait(10)
        return {'action':'START_WORK','work_id':'old-work','reason':'Old result'}
    monkeypatch.setattr(demand_conduction,'next_step',delayed)
    async def scenario():
        app=WorkConsole(root,backend=LocalBackend())
        async with app.run_test(size=(140,52)) as pilot:
            await pilot.pause();app.checkpoint('draft_saved');worker=app.continue_demand()
            assert await asyncio.to_thread(started.wait,10)
            app.query_one('#work-id',Input).value='new-selection';release.set();await worker.wait()
            assert app.selection().work=='new-selection'
            assert not any(e['kind']=='next_step_opened' for e in app.demand_state['events'])
    try:asyncio.run(scenario())
    finally:release.set()


def test_review_linked_sequence_orders_prepared_works_without_dispatch(sequence):
    from relay import work_sequence
    value = demand(sequence['repo'], ['work-two', 'work-one'], relay_mesh.RELAY_ROOT)
    view = demand_conduction.preview_sequence(value)
    assert [r['work_id'] for r in view['items']] == ['work-one', 'work-two']
    assert not sequence['calls'] and not sequence['dir'].exists()
    work_sequence.create(sequence['dir'], view['requested_order'], sequence['repo'], sequence['profile'],
                         unlimited=True, expected_selection=view['sha256'])
    result = work_sequence.execute(sequence['dir'])
    assert result['status'] == 'COMPLETED' and len(sequence['calls']) == 10


@pytest.mark.parametrize('fault', ['unprepared', 'outside', 'changed'])
def test_linked_sequence_never_expands_or_executes_changed_review(sequence, fault):
    from relay import work_sequence
    from test_work_sequence import save_graph
    value = demand(sequence['repo'], ['work-one', 'work-two'], relay_mesh.RELAY_ROOT)
    if fault == 'outside':
        value['events'][0]['detail']['work_ids'] = ['work-two']
        with pytest.raises(ValueError): demand_conduction.preview_sequence(value)
    else:
        view = demand_conduction.preview_sequence(value)
        graph = json.loads(relay_mesh.WORK_GRAPH_FILE.read_bytes())
        if fault == 'unprepared':
            graph['nodes'][1]['metrics']['verification_checks'] = []
            save_graph(graph)
            with pytest.raises(ValueError): demand_conduction.preview_sequence(value)
        else:
            graph['nodes'][1]['title'] = 'Changed requirement'
            save_graph(graph)
            with pytest.raises(ValueError, match='SELECTION_CHANGED'):
                work_sequence.create(sequence['dir'], view['requested_order'], sequence['repo'], sequence['profile'],
                                     expected_selection=view['sha256'])
    assert not sequence['calls'] and not (sequence['dir']/work_sequence.FILE).exists()


@pytest.mark.timeout(90)  # Two complete Work lifecycles through the TUI: ~28 s on an idle Windows host, 30 s is the default.
def test_demand_sequence_tui_runs_both_works_then_reopens_without_reexecution(sequence):
    from relay import work_sequence
    from relay.native_catalog import select
    from pathlib import Path
    class Process:
        returncode = None
        def __init__(self, args): self.args = args
        async def communicate(self):
            args = self.args
            directory = Path(args[args.index('--sequence')+1])
            def run():
                work_sequence.create(directory, ['work-two','work-one'], sequence['repo'],
                    select(None,sequence['host'][2]), unlimited=True,
                    expected_selection=args[args.index('--expected-selection')+1])
                return work_sequence.execute(directory, invocation_id=args[args.index('--invocation-id')+1])
            result = await asyncio.to_thread(run)
            self.returncode = 0
            return json.dumps(result).encode(), b''
    class RunBackend(LocalBackend):
        def __init__(self): self.launches = []
        async def launch(self, args, root):
            self.launches.append(args)
            return Process(args)
    async def scenario():
        backend = RunBackend()
        app = WorkConsole(sequence['repo'], backend=backend)
        async with app.run_test(size=(140,52)) as pilot:
            await pilot.pause()
            app.query_one('#mode',Select).value='work'
            app.query_one('#profile',Input).value=str(sequence['host'][2])
            app.query_one('#work-id',Input).value='work-one'
            app.query_one('#message',TextArea).load_text('Keep my draft')
            app.checkpoint('graph_applied', {'work_ids':['work-two','work-one']})
            await app.review_demand_sequence().wait()
            assert app.reviewed_demand_sequence and not sequence['calls']
            app.run_demand_sequence(); app.run_demand_sequence()
            while app.busy: await pilot.pause(.05)
            assert app.last_result['status']=='COMPLETED', app.last_result
            assert len(backend.launches)==1 and len(sequence['calls'])==10
            assert app.selection().mode=='sequence'
            assert app.query_one('#message',TextArea).text=='Keep my draft'
            identity=app.demand_id
        app=WorkConsole(sequence['repo'],backend=LocalBackend())
        async with app.run_test(size=(140,52)) as pilot:
            await pilot.pause();await app.list_demands().wait()
            app.query_one('#saved-demand',Select).value=identity
            app.open_demand();await app.workers.wait_for_complete()
            await app.continue_demand().wait()
            assert app.demand_state['events'][-1]['detail']['action']=='LINKED_WORKS_DONE'
            assert len(sequence['calls'])==10
    asyncio.run(scenario())


def test_reviewed_demand_changed_settings_cannot_start(sequence):
    async def scenario():
        app=WorkConsole(sequence['repo'],backend=LocalBackend())
        async with app.run_test(size=(140,52)) as pilot:
            await pilot.pause();app.checkpoint('graph_applied',{'work_ids':['work-one','work-two']})
            await app.review_demand_sequence().wait()
            assert app.reviewed_demand_sequence
            app.query_one('#profile',Input).value='changed-profile'
            app.run_demand_sequence()
            assert not app.busy and not sequence['calls']
            assert not any(e['kind']=='execution_requested' for e in app.demand_state['events'])
    asyncio.run(scenario())


def test_continue_returns_to_paused_sequence_not_individual_work(sequence):
    from relay import work_sequence
    value=demand(sequence['repo'],['work-one','work-two'],relay_mesh.RELAY_ROOT)
    work_sequence.create(sequence['dir'],['work-one','work-two'],sequence['repo'],sequence['profile'],
                         unlimited=True,wait_before_implement='Proceed?')
    assert work_sequence.execute(sequence['dir'])['status']=='PAUSED'
    value['events'] += [
        {'kind':'execution_requested','selection':{**value['snapshot']['selection'],'mode':'sequence','sequence':str(sequence['dir'])},
         'detail':{'demand_sequence':True}}, {'kind':'execution_observed'}]
    step=demand_conduction.next_step(value)
    assert step['action']=='RESUME_SEQUENCE' and step['sequence']==str(sequence['dir'])
    assert not sequence['calls']
    async def scenario():
        app=WorkConsole(sequence['repo'],backend=LocalBackend())
        async with app.run_test(size=(140,52)) as pilot:
            await pilot.pause()
            app.checkpoint('graph_applied',{'work_ids':['work-one','work-two']})
            app.restore_selection({**asdict(app.selection()),'mode':'sequence','sequence':str(sequence['dir'])})
            app.checkpoint('execution_requested',{'demand_sequence':True})
            app.checkpoint('execution_observed',work_sequence.inspect(sequence['dir']))
            await app.continue_demand().wait()
            assert app.selection().mode=='sequence' and app.selection().sequence==str(sequence['dir'])
            assert app.demand_state['events'][-1]['detail']['action']=='RESUME_SEQUENCE'
            assert not sequence['calls']
    asyncio.run(scenario())


def test_explicitly_refused_sequence_can_be_reviewed_again(sequence):
    value=demand(sequence['repo'],['work-one','work-two'],relay_mesh.RELAY_ROOT)
    choice={**value['snapshot']['selection'],'mode':'sequence','sequence':str(sequence['dir'])}
    value['events'] += [
        {'kind':'execution_requested','selection':choice,'detail':{'demand_sequence':True}},
        {'kind':'execution_observed','selection':choice,'detail':{'status':'REFUSED'}}]
    assert demand_conduction.next_step(value)['action']=='START_WORK'
    assert demand_conduction.preview_sequence(value)['items']
    assert not sequence['calls']
