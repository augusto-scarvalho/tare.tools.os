"""Temporary choices reach each dispatch, while saved prompts and policies stay intact."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from relay import console_backend,delegation,execution_scope as scope,native_agent,native_catalog
from relay import native_generation,native_plan,native_routes,planning_tools,turn_engine
from test_native_agent import Client,setup,deployment  # noqa:F401
from test_planning_tools import planner,wrap,execution  # noqa:F401


@pytest.fixture
def choices(planner,monkeypatch):
    _,state,local=planner
    for value in (local,state['deployment']):value['native_profile']['endpoint']='http://127.0.0.1:8080/v1'
    monkeypatch.setenv('TARE_PROFILE_DIR',str(Path(native_catalog.__file__).with_name('native_deployments')))
    api=native_catalog.select('openai-api-gpt-6-1-sol-pro')
    api['id']='api'
    api['native_profile']['parameters_json']='{"reasoning":{"effort":"high"}}'
    cli=deepcopy(local);cli['id']='subscription'
    cli['native_profile'].update(protocol='vendor-cli',endpoint='cli://claude',model='claude-opus-5-5',
        parameters_json='{"effort":"high"}')
    table={'local':local,'api':api,'subscription':cli,
        **{name:native_catalog.select(name) for name in ('openai-api-gpt-6-1-sol-pro','openai-api-gpt-6-astra-pro')}}
    def available(name,**kwargs):
        value=table.get(name,state['planning']['routes'].get(name))
        return (deepcopy(value),None) if value and scope.current().allows(value)[0] else (None,'excluded')
    monkeypatch.setattr(turn_engine,'available',available)
    monkeypatch.setattr(native_routes,'ranked',lambda role,*a,**kw:(
        [deepcopy(v) for k,v in table.items() if 'pro' not in k],{'candidates':[
            {'catalog':k,'selected_deployment':v['id'],'eligible':True} for k,v in table.items() if 'pro' not in k]}))
    return table


@pytest.mark.parametrize('prefer,expected',[('api','api'),('subscription','subscription')])
def test_planner_preference_changes_dispatch_without_changing_frozen_schema(planner,choices,prefer,expected):
    root,state,_=planner;tool=wrap(root,state,execution())
    before=deepcopy(state);schema=tool.tool_specs_json
    with scope.activated(scope.Scope(prefer=prefer)):
        name,selected=tool.choose({})
        assert name==expected and scope.kind(selected)==expected
    assert state==before and tool.tool_specs_json==schema
    assert tool.choose({})[0]=='worker'


def test_preference_falls_back_when_the_preferred_connection_is_unavailable(planner,choices,monkeypatch):
    root,state,local=planner;tool=wrap(root,state,execution())
    original=turn_engine.available
    monkeypatch.setattr(turn_engine,'available',lambda name,**kw:(None,'offline') if name=='api' else original(name,**kw))
    with scope.activated(scope.Scope(prefer='api')):
        assert tool.choose({})[1]['id']==local['id']


@pytest.mark.parametrize('selected_scope',[scope.Scope(prefer='subscription'),scope.Scope(only='subscription')])
def test_configured_available_auditor_is_preserved_inside_the_selected_provider_family(choices,selected_scope,monkeypatch):
    from relay import work_audit
    def identity(value,role):
        vendor='google' if value['id']=='chosen-argon' else 'openai'
        return SimpleNamespace(model_id=value['native_profile']['model'],bias_domain=vendor,vendor=vendor)
    monkeypatch.setattr(work_audit,'identity',identity)
    argon=deepcopy(choices['subscription']);argon['id']='chosen-argon'
    argon['native_profile'].update(endpoint='cli://agy',model='gemini-4-argon')
    choices['chosen-argon']=argon
    with scope.activated(selected_scope):
        candidates=turn_engine.pick_auditors(choices['api'],['chosen-argon'],probe=False)
    assert [name for name,_ in candidates]==['chosen-argon']


def test_pro_is_a_high_planner_override_and_leaves_cached_history_untouched(planner,choices):
    root,state,_=planner
    state.update(conversation_tool_specs='cached schema',conversation_flow_guidance='cached guidance',
        history=[{'role':'user','content':'Keep this prefix.'}])
    tool=wrap(root,state,execution());before=deepcopy(state)
    with scope.activated(scope.Scope(pro_planner=True)):
        name,selected=tool.choose({})
        assert name=='openai-api-gpt-6-1-sol-pro' and scope.is_pro(selected)
        assert json.loads(selected['native_profile']['parameters_json'])['reasoning']=={'effort':'high','mode':'pro'}
        assert not scope.current().allows(selected,'auditor')[0]
        direct,receipt=native_plan.select_route(state['deployment'],'planner','configured')
        assert direct==selected and receipt['reason']=='TEMPORARY_TUI_EXECUTION_POLICY'
        with pytest.raises(ValueError,match='minimum reasoning is high'):tool.choose({'effort':'medium'})
    assert state==before and tool.choose({})[0]=='worker'


def test_temporary_auditor_routes_still_use_the_independence_owner(monkeypatch):
    producer={'id':'producer'};seen=[]
    monkeypatch.setattr(turn_engine,'policy',lambda:{'auditor_selection':'configured','auditor':['saved']})
    monkeypatch.setattr(native_routes,'ranked',lambda role,author,**kw:(seen.append((role,author)) or [],{}))
    with scope.activated(scope.Scope(prefer='subscription')):
        native_routes.reviewers(producer,'plan-auditor')
        native_routes.reviewers(producer,'auditor')
    assert seen==[('plan-auditor',producer),('auditor',producer)]


def test_conversation_auditor_uses_its_own_role_even_when_coordinator_is_local(monkeypatch):
    selected={'id':'independent-api'}
    def reviewers(producer,**kwargs):
        assert scope.ROLE.get()=='auditor'
        return [selected],{}
    monkeypatch.setattr(native_routes,'reviewers',reviewers)
    with scope.activated(scope.Scope(prefer='local')):
        assert turn_engine.pick_auditors({'id':'local-author'},None)==[('independent-api',selected)]


@pytest.mark.parametrize('selected_scope',[scope.Scope(prefer='subscription'),scope.Scope(only='subscription')])
@pytest.mark.parametrize('fault',['offline','not_independent','unknown_author'])
def test_configured_auditor_failure_does_not_choose_a_different_model(monkeypatch,selected_scope,fault):
    from relay import work_audit
    producer={'id':'producer','native_profile':{'model':'sol','endpoint':'cli://codex','protocol':'vendor-cli'}}
    configured={'id':'chosen-argon','native_profile':{'model':'argon','endpoint':'cli://agy','protocol':'vendor-cli'}}
    monkeypatch.setattr(native_catalog,'select',lambda name:configured)
    monkeypatch.setattr(turn_engine,'available',lambda name,**kw:(None,'offline') if fault=='offline' else (configured,None))
    def identity(value,role):
        if role=='implementer' and fault=='unknown_author':raise KeyError('identity missing')
        vendor='google' if role=='auditor' and fault!='not_independent' else 'openai'
        return SimpleNamespace(model_id=value['native_profile']['model'],bias_domain=vendor,vendor=vendor)
    monkeypatch.setattr(work_audit,'identity',identity)
    def forbidden(*args,**kwargs):raise AssertionError('Configured auditor was silently replaced')
    monkeypatch.setattr(native_routes,'reviewers',forbidden)
    with scope.activated(selected_scope):
        assert turn_engine.pick_auditors(producer,['chosen-argon'],probe=False)==[]


@pytest.mark.parametrize('selected_scope',[scope.Scope(prefer='subscription'),scope.Scope(only='subscription'),scope.Scope(prefer='local')])
def test_explicit_family_override_can_still_remap_an_incompatible_configured_auditor(monkeypatch,selected_scope):
    from relay import work_audit
    category='http://127.0.0.1:8080/v1' if selected_scope.prefer=='local' else 'https://api.example/v1'
    configured={'native_profile':{'model':'configured','endpoint':category,'protocol':'openai-chat'}}
    selected={'id':'independent-subscription'}
    monkeypatch.setattr(native_catalog,'select',lambda name:configured)
    monkeypatch.setattr(work_audit,'identity',lambda *args:SimpleNamespace(model_id='author',bias_domain='openai',vendor='openai'))
    monkeypatch.setattr(native_routes,'reviewers',lambda *args,**kw:([selected],{}))
    with scope.activated(selected_scope):
        assert turn_engine.pick_auditors({'id':'author'},['configured'])==[(selected['id'],selected)]


def test_saved_explorer_is_replaced_in_memory_under_only_mode(planner,choices,monkeypatch):
    from relay import challenger_variants
    root,state,local=planner
    def variant(name):
        return {'deployment':deepcopy(choices[name]),'accounting':'local' if name=='local' else 'usd',
                'key':name,'task_types':['exploration'],'resource_pool':name,'identity':{},'label':name}
    frozen={'variant':variant('local'),'experimental':False,'settings':{'calls':4},'selection':'CONFIGURED'}
    before=deepcopy(frozen)
    monkeypatch.setattr(challenger_variants,'realize',lambda value:variant(value['profile']))
    with scope.activated(scope.Scope(only='api')):
        chosen=delegation.scoped(frozen,state['deployment'])
        assert chosen['variant']['key']=='api' and chosen['settings']==frozen['settings']
    assert frozen==before


def test_inference_guard_refuses_an_excluded_transport_before_creating_a_client():
    with scope.activated(scope.Scope(only='subscription')):
        with pytest.raises(ValueError,match='no inference was started'):
            native_generation.inference_client(deployment(),SimpleNamespace(protocol='openai-chat'),credential=lambda:None)


def test_restricted_auto_execution_can_reach_an_admitted_api_outside_the_saved_ladder(planner,choices):
    tiers=[['local']]
    assert scope.execution_tiers(tiers) is tiers
    with scope.activated(scope.Scope(only='api')):
        projected=scope.execution_tiers(tiers)
        _,name,selected,_=turn_engine.choose(projected)
        assert name=='api' and scope.kind(selected)=='api'
    assert tiers==[['local']]


def test_local_initial_routing_uses_only_the_resident_judge(monkeypatch):
    import compute_plane.ninfer_litjev as resident
    calls=[]
    monkeypatch.setattr(turn_engine,'available',lambda name,**kw:({'native_profile':{'model':name}},None))
    monkeypatch.setattr(turn_engine,'local_judge',lambda:'http://local-fixture')
    monkeypatch.setattr(resident,'assess',lambda *a,**kw:calls.append(kw) or {
        'choice':'tier_1','probabilities':{'tier_0':.1,'tier_1':.9},
        'model_identity':{'model':turn_engine.LOCAL_JUDGE},'usage':{'input_tokens':10}})
    monkeypatch.setattr(turn_engine,'jev_key',lambda:pytest.fail('No hosted Jev in local-only'))
    with scope.activated(scope.Scope(only='local')):
        _,_,selected,_=turn_engine.initial_choice([['first'],['second']],'A bounded task.')
    assert len(calls)==1 and selected['native_auto_selection']['decision']['source']=='local_judge'


@pytest.mark.parametrize('system',['nt','posix'])
def test_no_local_only_implementer_session_starts_where_its_checks_are_not_sandboxed(setup,monkeypatch,system):
    """The backstop for every implementer session (/session tasks, Work, resumes); conversations refuse earlier."""
    import os
    config,custody,_=setup
    monkeypatch.setattr(scope,'os',SimpleNamespace(name=system,environ=os.environ))
    client=Client(steps=[('tare_deliver',{'summary':'Done.'})])
    with scope.activated(scope.Scope(only='local')):
        with pytest.raises(ValueError) as refused:
            native_agent.run_session(config,deployment(),custody,'Change a.py.',client=client,local_counter=client)
    assert str(refused.value)==(scope.LOCAL_ONLY_UNSANDBOXED if system=='nt' else scope.LOCAL_ONLY_NO_SANDBOX)
    assert not client.calls and not custody.exists()
    with scope.activated(scope.Scope(prefer='local')):  # other modes: unchanged
        assert scope.local_only_unsandboxed({}) is None


def test_excluded_resume_does_not_rewrite_history_or_spend_a_model_call(setup):
    config,custody,_=setup
    client=Client(steps=[('tare_deliver',{'summary':'Done.'})])
    native_agent.run_session(config,deployment(),custody,'Retain this prompt.',client=client,local_counter=client)
    before={p.name:p.read_bytes() for p in custody.iterdir() if p.is_file()}
    with scope.activated(scope.Scope(only='subscription')):
        with pytest.raises(ValueError,match='no inference was started'):
            native_agent.run_session(None,None,custody,resume=True,follow_up='Continue.',message_id='message',
                                     client=client,local_counter=client)
    assert client.generations==1
    assert {p.name:p.read_bytes() for p in custody.iterdir() if p.is_file()}==before


def test_child_inherits_only_the_process_scope_and_project_permission_mode(tmp_path,monkeypatch):
    import os
    captured={}
    async def launch(*args,**kwargs):captured.update(kwargs);return SimpleNamespace()
    monkeypatch.setattr(asyncio,'create_subprocess_exec',launch)
    monkeypatch.setattr(turn_engine,'project_permission_mode',lambda root:'commands')
    monkeypatch.setenv('TARE_STORAGE_ROOT',str(tmp_path/'state'))
    backend=console_backend.Backend()
    with scope.activated(scope.Scope(disable_judge=True)):
        asyncio.run(backend.launch(['status'],str(tmp_path/'project')))
        assert json.loads(captured['env'][scope.ENV])['disable_judge']
        assert captured['env']['TARE_SESSION_OPERATOR_MODE']=='commands'
        assert 'TARE_ACTION_APPROVAL_ROOT' not in os.environ
    assert not scope.current().active and not (tmp_path/'state').exists()


def test_unresolved_auto_header_does_not_claim_or_probe_its_local_seed_model(tmp_path,monkeypatch):
    from relay import console_connection,console_header
    value=deployment();value['native_auto_routes']='implementer'
    monkeypatch.setattr(console_connection,'deployment_for',lambda key:value)
    key=(str(tmp_path),'','','tare-auto',False)
    with scope.activated(scope.Scope(only='api')):
        details=console_header.read_details(key)
        assert details.model=='tare auto' and details.reasoning==''
        assert console_connection.target_for(key) is None
