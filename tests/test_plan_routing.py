import asyncio,copy,hashlib,json,shutil
from pathlib import Path
import pytest
from relay import native_plan,native_routes,native_catalog,relay_mesh,route_facts
from test_native_plan import proposal,write


@pytest.fixture
def selection(proposal,monkeypatch):
    directory=proposal[0];control=directory.parent/'control';control.mkdir()
    shutil.copyfile(Path(native_routes.__file__).with_name('round_table_roster.json'),control/'round_table_roster.json')
    monkeypatch.setattr(relay_mesh,'RELAY_DIR',control)
    monkeypatch.setattr(native_routes,'credential_available',lambda _:True)
    collect = route_facts.collect
    def fixture_resources(deployment, **kwargs):
        facts = collect(deployment, live=False, evidence={})
        facts['resource'] = {'status': 'FIXTURE_READY', 'immediate': True}
        return facts
    monkeypatch.setattr(route_facts, 'collect', fixture_resources)
    table=native_routes.configured()
    # Routing mechanics use explicit fixture roles, independent of operator policy.
    for row in table['deployments']:
        if row['id'] in {'local-qwen', 'bedrock-opus-explorer', 'bedrock-sonnet-explorer'}:
            row['roles'] = ['implementer', 'explorer', 'planner', 'plan-auditor', 'auditor']
        elif row.get('native_catalog') not in native_catalog.LOCAL:
            row['runner'] = 'fixture-unsupported'
    monkeypatch.setattr(native_routes,'configured',lambda:copy.deepcopy(table))
    return table


@pytest.fixture
def provider(proposal,selection,monkeypatch):
    calls=[];failures=[];verdicts=[]
    def run(config,deployment,custody,instruction,**kw):
        calls.append((config['role'],copy.deepcopy(deployment),kw['limits']))
        if config['role']=='planner':
            payload=copy.deepcopy(proposal[2]['plan'])
            source=Path(config['repositories']['request'])/'request.json'
            if 'previous_plan' in json.loads(source.read_bytes()):
                payload['sources'].append({'repository':'request','path':'request.json','sha256':hashlib.sha256(source.read_bytes()).hexdigest()})
        else:
            if failures:
                code=failures.pop(0)
                return {'status':'BLOCKED','failure_code':code,'reason':code}
            verdict=verdicts.pop(0) if verdicts else 'ACCEPT'
            payload={'summary':'Reviewed the current proposal','verdict':verdict,'findings':[],
                'remaining':[] if verdict=='ACCEPT' else ['Clarify the requirement'],
                'sources':[{'repository':'proposal','path':name,'sha256':hashlib.sha256((Path(config['repositories']['proposal'])/name).read_bytes()).hexdigest()} for name in ('proposal.json','request.json')]}
        artifact={'schema':'tare.tools/native-artifact/1','role':config['role'],'kind':'plan' if config['role']=='planner' else 'review',
            'authority':'NONE','session_id':'fixture','call_id':'call','profile_digest':'a'*64,'payload':payload}
        write(custody/'artifact.json',artifact)
        return {'status':'PROPOSED','artifact_file':str(custody/'artifact.json')}
    monkeypatch.setattr('relay.native_agent.run_session',run)
    return calls,failures,verdicts


def generate(proposal,**kw):
    directory,graph,_,_=proposal
    return native_plan.generate(directory.parent/'project',graph,directory.parent/'new','Change and test','auto',
        reviewer_deployment='auto',unlimited=True,**kw)


def test_auto_plan_reviews_with_other_lineage_and_keeps_exact_profiles(proposal,selection,provider):
    before=proposal[1].read_bytes();result=generate(proposal);calls,_,_=provider
    assert result['status']=='REVIEWED' and proposal[1].read_bytes()==before
    assert [r[0] for r in calls]==['planner','plan-auditor']
    # The subscription Gemini 4 Argon reviews before the credit reserve.
    assert calls[0][1]==native_catalog.select('local-qwen') and calls[1][1]==native_catalog.select('opencode-sub-gemini-4-argon')
    assert all(r[2]['max_model_calls'] is None and r[2]['max_seconds'] is None for r in calls)
    assert result['routes']['plan-auditor']['selection']['producer_sha256']
    assert 'registered model lineage' in result['review']['independence']
    assert native_plan.apply(result['plan_dir'],result['proposal_sha256'])['status']=='APPLIED'


@pytest.mark.parametrize('missing',['planner','plan-auditor'])
def test_empty_role_pool_records_block_before_inference(proposal,selection,provider,missing):
    for row in selection['deployments']:
        row['roles']=[role for role in row['roles'] if role!=missing]
    identity='a'*32;result=generate(proposal,invocation_id=identity)
    assert result['status']=='ROUTE_BLOCKED' and result['model_calls']==0 and provider[0]==[]
    assert Path(result['plan_dir'],'invocation-results',identity+'.json').exists()
    assert result['routes'][missing]['selection']['candidates']


def test_operational_retry_reuses_review_choice_after_policy_changes(proposal,selection,provider):
    calls,failures,_=provider;failures.append('HTTP_429')
    result=generate(proposal)
    assert result['status']=='REVIEW_REQUIRED' and result['review_attempts'][0]['failure_code']=='HTTP_429'
    selection['deployments'].clear()
    second=native_plan.retry_review(result['plan_dir'])
    assert second['status']=='REVIEWED' and [c[0] for c in calls]==['planner','plan-auditor','plan-auditor']
    assert calls[1][1]==calls[2][1] and calls[1][2]==calls[2][2]
    native_plan.retry_review(result['plan_dir']);assert len(calls)==3


def test_semantic_revision_does_not_trigger_another_model(proposal,selection,provider):
    calls,_,verdicts=provider;verdicts.append('REVISE');result=generate(proposal)
    with pytest.raises(ValueError,match='proposal corrections'):native_plan.retry_review(result['plan_dir'])
    assert len(calls)==2


def test_reviewer_identity_change_refuses_retry(proposal,selection,provider):
    provider[1].append('HTTP_503');result=generate(proposal)
    path=relay_mesh.RELAY_DIR/'round_table_roster.json';value=json.loads(path.read_bytes())
    value['models']['claude-opus-4.6']['bias_domain']='changed';write(path,value)
    with pytest.raises(ValueError,match='identity roster changed'):native_plan.retry_review(result['plan_dir'])
    assert len(provider[0])==2


def test_replan_makes_fresh_choices_and_preserves_parent(proposal,selection,provider):
    first=generate(proposal);parent=Path(first['plan_dir'])
    frozen={p:p.read_bytes() for p in parent.rglob('*') if p.is_file()}
    for row in selection['deployments']:
        if row['id']=='opencode-sub-gemini-4-argon':row['roles'].remove('plan-auditor')
    second=native_plan.replan(parent,parent.parent/'next','Keep scope narrow','auto',reviewer_deployment='auto',unlimited=True)
    assert second['status']=='REVIEWED' and provider[0][-1][1]==native_catalog.select('bedrock-opus')
    assert all(p.read_bytes()==raw for p,raw in frozen.items())


def test_cli_auto_and_mixed_explicit_planner(proposal,selection,provider,capsys):
    target=proposal[0].parent/'cli'
    assert native_plan.main(['--root',str(proposal[0].parent/'project'),'--graph',str(proposal[1]),
        '--plan',str(target),'--instruction','Change and test','--deployment','auto','--reviewer-deployment','auto','--unlimited','--format','json'])==0
    assert json.loads(capsys.readouterr().out)['routes']['planner']['selection']['role']=='planner'
    target=proposal[0].parent/'explicit'
    result=native_plan.generate(proposal[0].parent/'project',proposal[1],target,'Change and test',native_catalog.select('local-qwen'),reviewer_deployment='auto')
    assert 'selection' not in result['routes']['planner'] and result['status']=='REVIEWED'


def test_tui_preserves_auto_choices_without_dispatch(proposal,selection,monkeypatch):
    from relay.native_console import WorkConsole
    from relay.console_backend import command,Selection
    from test_mutation_recovery import LocalBackend
    from textual.widgets import Checkbox,Select,TextArea
    monkeypatch.setenv('TARE_STORAGE_ROOT',str(proposal[0].parent/'state'))
    root=proposal[0].parent/'project'
    async def scenario():
        app=WorkConsole(root,backend=LocalBackend())
        async with app.run_test(size=(145,58)) as pilot:
            await pilot.pause();app.query_one('#mode',Select).value='plan'
            app.query_one('#auto-planner',Checkbox).value=True;app.query_one('#plan-reviewer',Select).value='auto'
            app.query_one('#prompt',TextArea).load_text('Change and test')
            await app.action_reload().wait();app.save_demand();identity=app.demand_id
        app=WorkConsole(root,backend=LocalBackend())
        async with app.run_test(size=(145,58)) as pilot:
            await pilot.pause();await app.list_demands().wait();app.query_one('#saved-demand',Select).value=identity
            app.open_demand();await app.workers.wait_for_complete()
            selected=app.selection();assert selected.auto_planner and selected.plan_reviewer=='auto' and app.process is None
            args=command(selected)
            assert args[args.index('--deployment')+1]=='auto' and args[args.index('--reviewer-deployment')+1]=='auto'
            selected.profile='custom.json'
            with pytest.raises(ValueError,match='not both'):command(selected)
    asyncio.run(scenario())


def test_late_selection_after_screen_detach_is_ignored(proposal):
    from relay.native_console import WorkConsole
    from test_mutation_recovery import LocalBackend
    from textual.widgets import Select
    async def scenario():
        app=WorkConsole(proposal[0].parent/'project',backend=LocalBackend())
        async with app.run_test(size=(140,52)) as pilot:
            await pilot.pause();widget=app.query_one('#mode',Select)
            await widget.remove()
            app.on_select_changed(Select.Changed(widget,'plan'))
    asyncio.run(scenario())
