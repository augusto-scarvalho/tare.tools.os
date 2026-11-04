import asyncio
import copy
import json

import pytest
from compute_plane.native_journal import save_json
from graph_backlog.jsonutil import UsageError, compute_revision_hash
from relay import work_replacement, work_order, work_run, relay_mesh, demand_conduction
from relay.native_console import WorkConsole
from test_work_order import case
from test_mutation_recovery import LocalBackend, LostCheckpoint
from test_demand_conduction import demand
from textual.widgets import Input, Select


def test_replacement_transfers_dependencies_and_preserves_old_criteria_and_evidence(case):
    root,before=case;graph=relay_mesh.WORK_GRAPH_FILE
    # A completed prerequisite is not edited or invalidated by replacing its consumer.
    edge={**copy.deepcopy(before['edges'][0]),'from':'done','to':'a'}
    before['edges'].append(edge);before['revision']=compute_revision_hash(before);save_json(graph,before)
    frozen=graph.read_bytes();view=work_replacement.preview(root,{'a':'c'},'Use the revised task')
    assert graph.read_bytes()==frozen
    result=work_replacement.apply(view);after=json.loads(graph.read_bytes());nodes={n['id']:n for n in after['nodes']}
    assert result['status']=='WORKS_REPLACED'
    assert nodes['a']['completion']['status']=='SUPERSEDED' and nodes['a']['completion']['superseded_by']=='c'
    assert nodes['a']['exit_criteria']==before['nodes'][0]['exit_criteria']
    assert nodes['done']==before['nodes'][-1]
    assert work_order.current('c')['prerequisites']==['done']
    assert work_order.current('b')['prerequisites']==['c']
    assert any(row['before']['notes']==['Preserve this provenance'] for row in view['replacement']['relations'])
    assert all(not e['source_refs'] for e in view['replacement']['added_edges'])
    frozen=graph.read_bytes();assert work_replacement.apply(view)==result and graph.read_bytes()==frozen


def test_many_to_one_merges_internal_dependencies_and_demand_follows_confirmed_successor(case):
    root,_=case;graph=relay_mesh.WORK_GRAPH_FILE
    raw=json.loads(graph.read_bytes())
    for n in raw['nodes'][:3]:n['admission_state']='PROPOSED_FROM_PLAN_NOT_ADMITTED'
    raw['revision']=compute_revision_hash(raw);save_json(graph,raw)
    value=demand(root,['a','b'],relay_mesh.RELAY_ROOT)
    result=work_replacement.apply(work_replacement.preview(root,{'a':'c','b':'c'},'One bounded successor'))
    assert work_order.current('c')['prerequisites']==[]
    assert demand_conduction.next_step(value)['action']=='BLOCKED'  # No implicit adoption from a bare graph pointer.
    value['events'].append({'kind':'work_replacement_applied','selection':value['snapshot']['selection'],'detail':result})
    step=demand_conduction.next_step(value)
    assert step['work_ids']==['c'] and step['work_id']=='c' and step['action']=='PREPARE_WORK'


@pytest.mark.parametrize('fault',['self','unknown','chain','done','done-consumer','active','requirements','cycle'])
def test_unsafe_replacement_does_not_modify_graph(case,fault):
    root,_=case;graph=relay_mesh.WORK_GRAPH_FILE;raw=json.loads(graph.read_bytes());mapping={'a':'c'}
    if fault=='self':mapping={'a':'a'}
    elif fault=='unknown':mapping={'a':'absent'}
    elif fault=='chain':mapping={'a':'b','b':'c'}
    elif fault=='done':mapping={'done':'c'}
    elif fault=='done-consumer':raw['nodes'][1]['completion'].update(status='DONE',dod_satisfied=True)
    elif fault=='active':
        train=work_run.train_dir_for('c');train.mkdir(parents=True);save_json(train/'STATE.json',{'status':'PLAN_APPROVED'})
    elif fault=='requirements':raw['edges'][0]['requirements']={'criteria':['AC-01']}
    elif fault=='cycle':raw['edges'].append({**copy.deepcopy(raw['edges'][0]),'from':'b','to':'c'})
    raw['revision']=compute_revision_hash(raw);save_json(graph,raw);before=graph.read_bytes()
    expected = UsageError if fault == 'cycle' else ValueError
    with pytest.raises(expected):work_replacement.preview(root,mapping,'Review replacement')
    assert graph.read_bytes()==before


@pytest.mark.parametrize('fault',['graph','execution','view','lock'])
def test_stale_or_changed_review_refuses_apply(case,fault):
    root,_=case;graph=relay_mesh.WORK_GRAPH_FILE
    view=work_replacement.preview(root,{'a':'c'},'Reviewed successor')
    if fault=='graph':
        raw=json.loads(graph.read_bytes());raw['nodes'][2]['title']='Changed successor'
        raw['revision']=compute_revision_hash(raw);save_json(graph,raw)
    elif fault=='execution':
        train=work_run.train_dir_for('b');train.mkdir(parents=True);save_json(train/'STATE.json',{'status':'PLAN_APPROVED'})
    elif fault=='view':view['replacement']['replacements']={'b':'c'}
    before=graph.read_bytes()
    if fault=='lock':
        from relay.work_change import work_locks
        with work_locks(['c']):
            with pytest.raises(ValueError):work_replacement.apply(view)
    else:
        with pytest.raises(ValueError):work_replacement.apply(view)
    assert graph.read_bytes()==before


def test_tui_stages_replacement_and_recovers_lost_acknowledgement(case):
    root,_=case
    async def scenario():
        app=LostCheckpoint(root,backend=LocalBackend())
        async with app.run_test(size=(145,55)) as pilot:
            await pilot.pause();app.query_one('#mode',Select).value='work';app.query_one('#work-id',Input).value='a'
            app.query_one('#replacement-work',Input).value='c';app.stage_replacement()
            app.query_one('#order-reason',Input).value='Revised implementation task';app.save_demand();identity=app.demand_id
        app=LostCheckpoint(root,backend=LocalBackend())
        async with app.run_test(size=(145,55)) as pilot:
            await pilot.pause();await app.list_demands().wait();app.query_one('#saved-demand',Select).value=identity
            app.open_demand();await app.workers.wait_for_complete()
            assert app.order_draft['replacements']=={'a':'c'} and app.reviewed_order is None
            await app.preview_order().wait();assert app.reviewed_order
            app.confirm_order()
            while app.busy:await pilot.pause(.05)
            assert app.last_result['status']=='WORKS_REPLACED'
        before=relay_mesh.WORK_GRAPH_FILE.read_bytes()
        app=WorkConsole(root,backend=LocalBackend())
        async with app.run_test(size=(145,55)) as pilot:
            await pilot.pause();await app.list_demands().wait();app.query_one('#saved-demand',Select).value=identity
            app.open_demand();await app.workers.wait_for_complete()
            assert app.last_result['replacements']=={'a':'c'} and not app.order_draft.get('replacements')
            assert any(v.get('work')=='c' for v in app.demand_stages.values())
            assert app.demand_state['events'][-1]['kind']=='mutation_reconciled'
            assert relay_mesh.WORK_GRAPH_FILE.read_bytes()==before and app.process is None
    asyncio.run(scenario())


def test_existing_successor_dependency_keeps_its_own_evidence(case):
    root,raw=case;graph=relay_mesh.WORK_GRAPH_FILE
    edge={**copy.deepcopy(raw['edges'][0]),'from':'c','notes':['Evidence belongs to this successor']}
    raw['edges'].append(edge);raw['revision']=compute_revision_hash(raw);save_json(graph,raw)
    work_replacement.apply(work_replacement.preview(root,{'a':'c'},'Consolidate the old task'))
    after=json.loads(graph.read_bytes())
    matching=[e for e in after['edges'] if e['from']=='c' and e['to']=='b']
    assert matching==[edge]
    assert work_order.current('b')['prerequisites']==['c']
