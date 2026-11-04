import asyncio
import copy
import json

import pytest
from compute_plane.native_journal import save_json
from graph_backlog.core import WorkGraph
from graph_backlog.jsonutil import compute_revision_hash
from graph_backlog.mutations import add_node_to_graph
from relay import work_order, relay_mesh, work_run, demand_conduction
from relay.console_backend import Selection
from relay.native_console import WorkConsole
from relay import order_console
from test_mutation_recovery import LocalBackend, LostCheckpoint
from test_demand_conduction import demand
from textual.widgets import Select, Input, Button
from textual.selection import Selection as TextSelection
from relay.console_output import SelectableMessage


@pytest.fixture
def case(tmp_path, monkeypatch):
    original = relay_mesh.RELAY_ROOT
    control=tmp_path/'control';control.mkdir();root=tmp_path/'project';root.mkdir()
    relay_mesh.rebind_root(control)
    monkeypatch.setenv('TARE_STORAGE_ROOT',str(tmp_path/'state'))
    raw={'meta':{'schema':'tare.tools/work-graph/0.5'},'sources':{},'nodes':[],'edges':[]}
    for key, priority in [('a','P2'),('b','P1'),('c','P3'),('done','P2')]:
        raw=add_node_to_graph(WorkGraph(raw),key,key,priority=priority)
    raw['nodes'][-1]['completion'].update(status='DONE',dod_satisfied=True)
    raw['edges']=[{'from':'a','to':'b','type':'UNLOCKS','semantic':True,'source_refs':[],
        'source_details':[],'confidence':{'score':1},'notes':['Preserve this provenance'],'requirements':{}}]
    raw['revision']=compute_revision_hash(raw);save_json(relay_mesh.WORK_GRAPH_FILE,raw)
    yield root,raw
    relay_mesh.rebind_root(original)


def edits(**kw):
    return {key:{'priority':priority,'prerequisites':deps} for key,(priority,deps) in kw.items()}


def test_batch_reorder_preserves_completed_nodes_and_existing_edge_evidence(case):
    root,before=case;graph=relay_mesh.WORK_GRAPH_FILE;bytes_before=graph.read_bytes()
    view=work_order.preview(root,edits(a=('P0',[]),b=('P2',['a']),c=('P1',['b'])),'Prioritize the prerequisite')
    assert graph.read_bytes()==bytes_before
    result=work_order.apply(view);after=json.loads(graph.read_bytes())
    assert result['status']=='ORDER_UPDATED' and result['generation_calls']==0
    assert after['nodes'][-1]==before['nodes'][-1]
    assert after['edges'][0]==before['edges'][0]
    assert before['sources']==after['sources'] and before['meta']==after['meta']
    assert after['nodes'][0]['priority']=='P0'
    assert work_order.current('c')['prerequisites']==['b']
    frozen=graph.read_bytes();assert work_order.apply(view)==result and graph.read_bytes()==frozen


def test_review_names_graph_revision_proposal_and_blockers(case):
    root,_=case
    view=work_order.preview(root,edits(a=('P0',[]),b=('P2',['a'])),'Prioritize the prerequisite')
    text=work_order.render(view)
    assert str(relay_mesh.WORK_GRAPH_FILE.resolve()) in text
    assert view['order']['base_revision'] in text and view['sha256'] in text
    assert 'a: P2 → P0' in text and 'Prerequisites: none → none' in text
    assert 'b readiness: Blocked → Blocked' in text and 'Waiting for: a' in text
    assert '"prerequisites"' not in text


def test_tui_review_interval_restarts_after_refresh_and_survives_lost_ack(case,monkeypatch):
    root,_=case
    clock=[100.0]
    monkeypatch.setattr(order_console,'monotonic',lambda:clock[0])
    async def scenario():
        app=LostCheckpoint(root,backend=LocalBackend())
        async with app.run_test(size=(145,55)) as pilot:
            await app.workers.wait_for_complete()
            app.query_one('#mode',Select).value='work'
            app.query_one('#work-id',Input).value='a'
            await app.load_order().wait()
            app.query_one('#order-priority',Select).value='P0'
            app.stage_order()
            app.query_one('#order-reason',Input).value='Prioritize prerequisite'
            await app.preview_order().wait()
            await pilot.pause()
            rendered=app.query_one('.scheduling-review',SelectableMessage).get_selection(TextSelection(None,None))[0]
            assert '\nBase revision:' in rendered and '\nProposal:' in rendered
            assert '\na: P2 → P0\n' in rendered
            clock[0]=110.0
            app.query_one('#order-reason',Input).value='Changed reason'
            app.confirm_order()
            assert app.reviewed_order is None and app.order_review_started is None
            assert work_order.current('a')['priority']=='P2'
            await app.preview_order().wait()
            digest=app.reviewed_order[1]['sha256']
            clock[0]=112.5
            app.confirm_order()
            while app.busy:await pilot.pause(.05)
            assert app.last_result['status']=='ORDER_UPDATED'
            request=next(e for e in app.demand_state['events'] if e['kind']=='work_order_requested')
            observed=request['detail']['review_observation']
            assert observed['proposal_sha256']==digest and observed['elapsed_ms']==2500
            assert observed['actor']=='unattributed'
            assert observed['preview_ready_at'] and observed['decision_at'] and observed['decision']=='confirmed'
            identity=app.demand_id
        reopened=WorkConsole(root,backend=LocalBackend())
        async with reopened.run_test(size=(145,55)) as pilot:
            await pilot.pause();await reopened.list_demands().wait()
            reopened.query_one('#saved-demand',Select).value=identity
            reopened.open_demand();await reopened.workers.wait_for_complete()
            assert reopened.last_result['status']=='ORDER_UPDATED'
            request=next(e for e in reopened.demand_state['events'] if e['kind']=='work_order_requested')
            assert request['detail']['review_observation']==observed
            assert reopened.reviewed_order is None and reopened.order_review_started is None
    asyncio.run(scenario())


def test_withdrawn_preview_records_a_decision_without_graph_mutation(case,monkeypatch):
    root,_=case
    clock=[10.0]
    monkeypatch.setattr(order_console,'monotonic',lambda:clock[0])
    async def scenario():
        app=WorkConsole(root,backend=LocalBackend())
        async with app.run_test(size=(145,55)) as pilot:
            await app.workers.wait_for_complete()
            app.query_one('#mode',Select).value='work'
            app.query_one('#work-id',Input).value='a'
            await app.load_order().wait()
            app.query_one('#order-priority',Select).value='P0';app.stage_order()
            app.query_one('#order-reason',Input).value='Review priority'
            before=relay_mesh.WORK_GRAPH_FILE.read_bytes()
            await app.preview_order().wait()
            preview=app.demand_state['events'][-1]
            assert preview['kind']=='order_previewed' and preview['detail']['work_ids']==['a']
            clock[0]=12
            app.clear_order()
            observation=app.demand_state['events'][-1]['detail']['review_observation']
            assert observation['decision']=='withdrawn' and observation['elapsed_ms']==2000
            assert observation['proposal_sha256']==preview['detail']['proposal_sha256']
            assert relay_mesh.WORK_GRAPH_FILE.read_bytes()==before
            assert app.reviewed_order is None and app.order_review_started is None
    asyncio.run(scenario())


def test_stale_console_confirmation_requires_a_new_preview(case):
    root,_=case
    async def scenario():
        app=WorkConsole(root,backend=LocalBackend())
        async with app.run_test(size=(145,55)) as pilot:
            await app.workers.wait_for_complete()
            app.query_one('#mode',Select).value='work'
            app.query_one('#work-id',Input).value='c'
            await app.load_order().wait()
            app.query_one('#order-prerequisites',Input).value='a';app.stage_order()
            app.query_one('#order-reason',Input).value='Require the prerequisite'
            await app.preview_order().wait()
            raw=json.loads(relay_mesh.WORK_GRAPH_FILE.read_bytes())
            raw['nodes'][0]['summary']='Concurrent fixture change'
            raw['revision']=compute_revision_hash(raw);save_json(relay_mesh.WORK_GRAPH_FILE,raw)
            before=relay_mesh.WORK_GRAPH_FILE.read_bytes()
            app.confirm_order()
            while app.busy:await pilot.pause(.05)
            assert relay_mesh.WORK_GRAPH_FILE.read_bytes()==before and app.last_result is None
            assert app.reviewed_order is None and app.order_review_started is None
            assert app.query_one('#confirm-order',Button).disabled
            requests=lambda:sum(e['kind']=='work_order_requested' for e in app.demand_state['events'])
            assert requests()==1
            app.confirm_order();await pilot.pause()
            assert requests()==1  # A refused preview cannot be submitted again.
            await app.preview_order().wait()
            assert not app.query_one('#confirm-order',Button).disabled
            app.confirm_order()
            while app.busy:await pilot.pause(.05)
            assert app.last_result['status']=='ORDER_UPDATED'
            assert work_order.current('c')=={'priority':'P3','prerequisites':['a']}
    asyncio.run(scenario())


@pytest.mark.parametrize('fault',['cycle','missing','duplicate','done','done-descendant','active','priority','noop'])
def test_invalid_or_historical_changes_never_write(case,fault):
    root,_=case;graph=relay_mesh.WORK_GRAPH_FILE
    change=edits(a=('P0',[]))
    if fault=='cycle':change=edits(a=('P0',['b']))
    elif fault=='missing':change=edits(a=('P0',['missing']))
    elif fault=='duplicate':change=edits(a=('P0',['c','c']))
    elif fault=='done':change=edits(done=('P0',[]))
    elif fault=='done-descendant':
        raw=json.loads(graph.read_bytes());raw['nodes'][1]['completion'].update(status='DONE',dod_satisfied=True)
        raw['revision']=compute_revision_hash(raw);save_json(graph,raw);change=edits(a=('P0',['c']))
    elif fault=='active':
        path=work_run.train_dir_for('a');path.mkdir(parents=True);save_json(path/'STATE.json',{'status':'PLAN_APPROVED'})
    elif fault=='priority':change=edits(a=('X',[]))
    elif fault=='noop':change=edits(a=('P2',[]))
    before=graph.read_bytes()
    with pytest.raises(ValueError):work_order.preview(root,change,'Review order')
    assert graph.read_bytes()==before


@pytest.mark.parametrize('fault',['graph','execution','view','lock'])
def test_preview_cannot_apply_after_concurrent_changes(case,fault):
    root,_=case;graph=relay_mesh.WORK_GRAPH_FILE;view=work_order.preview(root,edits(a=('P0',[])),'Review order')
    if fault=='graph':
        raw=json.loads(graph.read_bytes());raw['nodes'][2]['title']='Concurrent title'
        raw['revision']=compute_revision_hash(raw);save_json(graph,raw)
    elif fault=='execution':
        path=work_run.train_dir_for('a');path.mkdir(parents=True);save_json(path/'STATE.json',{'status':'PLAN_APPROVED'})
    elif fault=='view':view['order']['edits']['a']['priority']='P3'
    before=graph.read_bytes()
    if fault=='lock':
        from relay.work_change import work_locks
        with work_locks(['a']):
            with pytest.raises(ValueError):work_order.apply(view)
    else:
        with pytest.raises(ValueError):work_order.apply(view)
    assert graph.read_bytes()==before


def test_continue_uses_reviewed_priority_among_unblocked_linked_works(case):
    root,_=case
    # Use proposed Works to observe preparation selection, without inventing execution readiness.
    graph=relay_mesh.WORK_GRAPH_FILE;raw=json.loads(graph.read_bytes())
    for node in raw['nodes'][:3]:node['admission_state']='PROPOSED_FROM_PLAN_NOT_ADMITTED'
    raw['revision']=compute_revision_hash(raw);save_json(graph,raw)
    value=demand(root,['a','c'],relay_mesh.RELAY_ROOT)
    assert demand_conduction.next_step(value)['work_id']=='a'
    work_order.apply(work_order.preview(root,edits(c=('P0',[])),'Small quick gain first'))
    assert demand_conduction.next_step(value)['work_id']=='c'


def test_tui_staged_set_survives_reopen_and_lost_apply_checkpoint(case):
    root,_=case
    async def scenario():
        app=LostCheckpoint(root,backend=LocalBackend())
        async with app.run_test(size=(145,55)) as pilot:
            await pilot.pause();app.query_one('#mode',Select).value='work'
            for key,priority,deps in [('a','P0',''),('c','P1','b')]:
                app.query_one('#work-id',Input).value=key;await app.load_order().wait()
                app.query_one('#order-priority',Select).value=priority
                app.query_one('#order-prerequisites',Input).value=deps;app.stage_order()
            app.query_one('#order-reason',Input).value='Priority and prerequisite review';app.save_demand()
            identity=app.demand_id
        app=LostCheckpoint(root,backend=LocalBackend())
        async with app.run_test(size=(145,55)) as pilot:
            await pilot.pause();await app.list_demands().wait();app.query_one('#saved-demand',Select).value=identity
            app.open_demand();await app.workers.wait_for_complete()
            assert set(app.order_draft['edits'])=={'a','c'} and app.reviewed_order is None
            await app.preview_order().wait();assert app.reviewed_order is not None
            app.query_one('#order-reason',Input).value='Changed reason';app.confirm_order()
            assert app.reviewed_order is None
            await app.preview_order().wait();app.confirm_order()
            while app.busy:await pilot.pause(.05)
            assert app.last_result['status']=='ORDER_UPDATED'
        before=relay_mesh.WORK_GRAPH_FILE.read_bytes()
        app=WorkConsole(root,backend=LocalBackend())
        async with app.run_test(size=(145,55)) as pilot:
            await pilot.pause();await app.list_demands().wait();app.query_one('#saved-demand',Select).value=identity
            app.open_demand();await app.workers.wait_for_complete()
            assert app.last_result['status']=='ORDER_UPDATED' and not app.order_draft.get('edits')
            assert app.demand_state['events'][-1]['kind']=='mutation_reconciled'
            assert app.reviewed_order is None and app.process is None
            assert relay_mesh.WORK_GRAPH_FILE.read_bytes()==before
    asyncio.run(scenario())
