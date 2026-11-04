"""A human transport change may recover failed planning once, with spend/calls retained."""
from copy import deepcopy
import hashlib
import json
import pytest
from compute_plane.native_journal import NativeJournal,save_json
from relay import conversation_tasks,execution_scope,planning_tools
from test_planning_tools import planner,execution,request,wrap,stub_child  # noqa: F401


@pytest.mark.parametrize('case',['changed-mode','default-mode','same-route','stale-human',
    'prior-plan','already-recovered','parent-budget','still-running','already-applied'])
def test_only_one_fresh_operator_transport_change_can_recover_failed_planning(planner,monkeypatch,case):
    parent,state,candidate=planner
    old=deepcopy(candidate);old['id']='old-cli'
    state.update(conversation_flow=True)
    state['planning']['routes']['old-cli']=old
    state['planning']['other_providers']=True
    task={'id':'same-demand','objective':'Deliver the same bounded change.',
          'criteria':['Preserve the original acceptance criteria.'],'intent':'deliver',
          'intent_input':'human-retry','stage':'INTAKE'}
    if case=='already-applied':task['delivery']={'applied':True}
    conversation_tasks.save(parent,{'schema':'tare.tools/conversation-tasks/1','current':task['id'],'tasks':[task]})
    journal=NativeJournal(parent/'journal.jsonl')
    if case=='stale-human':journal.append({'kind':'user_message','message_id':'human-retry','text':'Continue.'})
    for n in range(3):
        effect='prior-'+str(n)
        journal.append({'kind':'effect_requested','effect_id':effect,'tool_call':{'name':'delegate_plan'}})
        folder=parent/'planning'/hashlib.sha256(effect.encode()).hexdigest()[:24];folder.mkdir(parents=True)
        row={'demand_id':task['id'],'route':'worker' if case=='same-route' else 'old-cli',
             'calls_reserved':6,'calls_used':1,'status':'FINISHED',
             'result':{'status':'EXHAUSTED','reason':'DEADLINE'}}
        if case=='prior-plan' and n==0:row['result']['status']='PLANNED'
        if case=='already-recovered' and n==0:row['route_requalification']={'route':'worker'}
        if case=='still-running' and n==0:row['status']='RUNNING'
        save_json(folder/'receipt.json',row)
    if case!='stale-human':journal.append({'kind':'user_message','message_id':'human-retry','text':'Continue with the newly selected API mode.'})
    monkeypatch.setattr(execution_scope,'current',lambda:execution_scope.Scope() if case=='default-mode' else execution_scope.Scope(prefer='api'))
    monkeypatch.setattr(execution_scope,'planner_candidates',lambda _: [('worker',candidate)])
    monkeypatch.setattr(execution_scope,'kind',lambda dep:'subscription' if dep.get('id')=='old-cli' else 'api')
    if case=='parent-budget':state['limits']['max_model_calls']=4
    calls=[];stub_child(monkeypatch,candidate,calls)
    tool=wrap(parent,state,execution())
    result=tool.execute(request(effect='operator-mode-recovery'),execution())
    if case!='changed-mode':
        assert result.status=='refused' and not calls
        assert len(planning_tools.records(parent))==3
        return
    assert result.status=='succeeded' and json.loads(result.output_json)['status']=='PLANNED'
    assert len(calls)==1 and calls[0][4]['limits']['max_model_calls']==4
    assert state['limits']['max_model_calls']==10 and tool.calls_committed()==5
    rows=planning_tools.records(parent)
    assert len(rows)==4 and sum(bool(row.get('route_requalification')) for row in rows)==1
    assert conversation_tasks.current(conversation_tasks.load(parent))['criteria']==task['criteria']
    repeated=tool.execute(request(effect='repeated-mode-recovery'),execution())
    assert repeated.status=='refused' and len(calls)==1
