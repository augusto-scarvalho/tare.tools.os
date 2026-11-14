"""Automatic execution routing with real sessions and saved flow state."""
import asyncio
import copy
import hashlib
import json
from pathlib import Path

import pytest
from relay import native_agent, native_catalog, native_routes, parallel_explore, research_flow, work_sequence, work_run
from test_native_agent import Client, deployment
from test_native_plan import proposal
from test_plan_routing import selection
from test_work_run import os_root
from test_work_resume import host
from test_work_audit import reviewers
from test_native_routes import candidates
from test_work_sequence import sequence


@pytest.mark.parametrize('role', ['implementer', 'explorer'])
def test_auto_resolves_current_profile_and_reserve(selection, role):
    chosen = native_catalog.select('auto', role=role)
    assert chosen['native_profile'] == native_catalog.select('local-qwen')['native_profile']
    receipt = chosen['native_selection']
    assert receipt['role'] == role and receipt['generation_calls'] == 0
    for row in selection['deployments']:
        if row['model'] == 'qwen-3.8-27b': row['roles'].remove(role)
    reserve = native_catalog.select('auto', role=role)
    # A subscription reserve comes before credit: the first OpenCode subscription model in the operator's order.
    assert reserve['native_profile'] == native_catalog.select('opencode-sub-deepseek-v4-1-flash')['native_profile']
    for row in selection['deployments']:
        if row['id'].startswith('opencode-sub-'): row['roles'].remove(role)
    reserve = native_catalog.select('auto', role=role)
    assert reserve['native_profile'] == native_catalog.select('bedrock-opus')['native_profile']
    # A CLI route is never the automatic reserve: its runner is not supported here, or it is a pin-only row.
    assert all(r['reason'].startswith(('RUNNER_NOT_SUPPORTED', 'NOT_QUALIFIED_PIN_ONLY'))
               for r in reserve['native_selection']['candidates']
               if r['deployment'].endswith('-cli') and r['reason'] != 'ROLE_NOT_CONFIGURED')


def test_empty_pool_and_conflicting_inputs_fail_before_session(selection, tmp_path, monkeypatch):
    for row in selection['deployments']: row['roles'] = []
    with pytest.raises(ValueError, match='NO_ELIGIBLE_NATIVE_ROLE:explorer'):
        native_catalog.select('auto', role='explorer')
    with pytest.raises(ValueError, match='use_deployment_name_or'):
        native_catalog.select('auto', tmp_path/'profile.json', role='implementer')
    with pytest.raises(ValueError, match='REQUIRES_IMPLEMENTER_OR_EXPLORER'):
        native_catalog.select('auto', role='auditor')
    monkeypatch.setattr(native_agent, 'run_session', lambda *a, **kw: pytest.fail('dispatch with empty pool'))
    target=tmp_path/'session'
    assert native_agent.main(['--root',str(tmp_path),'--session',str(target),'--deployment','auto',
        '--role','explorer','--instruction','Inspect','--no-context']) == 2
    assert not target.exists()


@pytest.mark.parametrize('role', ['implementer', 'explorer'])
def test_real_session_resume_keeps_profile_and_role(selection, proposal, monkeypatch, role):
    root=proposal[0].parent/'project';target=root.parent/'session'
    (root/'sample.txt').write_bytes(b'before\n')
    steps=[('read',{'repository':root.name,'path':'sample.txt'})]
    if role=='implementer':
        steps.append(('edit',{'repository':root.name,'path':'sample.txt','sha256':hashlib.sha256(b'before\n').hexdigest(),
                             'search':'before','replace':'after'}))
    steps += [('tare_deliver',{'summary':'Inspected the source.'}),('tare_deliver',{'summary':'Follow-up complete.'})]
    from relay import relay_mesh
    row=selection['deployments'][0]
    profile=deployment();profile.update(model=row['model'],roles=row['roles'],kind='primary',cost_tier='free')
    selection['deployments'][:]=[profile]
    roster=relay_mesh.RELAY_DIR/'round_table_roster.json';value=json.loads(roster.read_bytes())
    value['models']['qwen-3.8-27b']['runtime']['native']='fixture';roster.write_text(json.dumps(value))
    client=Client(steps=steps); original=native_agent.run_session; calls=[]
    def run(*args,**kw):
        result=original(*args,client=client,local_counter=client,**kw);calls.append(result);return result
    monkeypatch.setattr(native_agent,'run_session',run)
    assert native_agent.main(['--root',str(root),'--session',str(target),'--deployment','auto','--role',role,
        '--instruction','Inspect sample.txt','--no-context','--unlimited']) == 0
    state=json.loads((target/'native-agent.json').read_bytes());saved=copy.deepcopy(state['deployment'])
    assert state['config']['role']==role and ('edit' in state['config']['capabilities']) == (role=='implementer')
    assert state['limits']['max_model_calls'] is None and state['limits']['max_seconds'] is None
    selection['deployments'].clear()
    assert native_agent.main(['--session',str(target),'--resume','--follow-up','Confirm','--message-id','followup'])==0
    assert json.loads((target/'native-agent.json').read_bytes())['deployment']==saved
    assert (root/'sample.txt').read_text()==('after\n' if role=='implementer' else 'before\n')
    before=client.generations
    assert native_agent.main(['--session',str(target),'--resume','--deployment','auto'])==2
    assert client.generations==before


@pytest.mark.parametrize('owner,flag,filename,extra', [
    (parallel_explore,'--batch','batch.json',['--question','Inspect','--workers','1']),
    (research_flow,'--research','research.json',['--question','Investigate','--url','https://example.com'])])
def test_exploration_owners_save_choice_and_resume_without_reranking(selection, proposal, monkeypatch, owner,flag,filename,extra):
    root=proposal[0].parent/'project';directory=root.parent/'flow';profiles=[]
    def execute(path,**kw):
        state=json.loads((path/filename).read_bytes());profiles.append(state['deployment']);return {'status':'COMPLETED'}
    monkeypatch.setattr(owner,'execute',execute)
    assert owner.main([flag,str(directory),'--root',str(root),'--deployment','auto','--unlimited','--format','json',*extra])==0
    selection['deployments'].clear()
    assert owner.main([flag,str(directory),'--resume','--format','json'])==0
    assert len(profiles)==2 and profiles[0]==profiles[1] and profiles[0]['native_selection']['role']=='explorer'


def test_work_auto_lands_and_resume_does_not_select_again(host, candidates, monkeypatch, capsys):
    import tare_ops
    for row in candidates['deployments']: row['roles'].append('implementer')
    candidates['deployments'][0]['cost_tier']='free'
    assert tare_ops.main(['run','work-one','--repo-root',str(host[1]),'--deployment','auto','--unlimited'])==0
    result=json.loads(capsys.readouterr().out)
    assert result['status']=='SETTLED' and result['implementer_routing']['role']=='implementer'
    origin=json.loads((work_run.train_dir_for('work-one')/'WORK_RUN.json').read_bytes())
    assert origin['native_deployment']['native_selection']==result['implementer_routing']
    calls=len(host[3].generations);candidates['deployments'].clear()
    assert tare_ops.main(['run','work-one','--resume'])==0
    assert len(host[3].generations)==calls


@pytest.mark.parametrize('mode,extra', [
    ('session',{'role':'explorer','prompt':'Inspect'}),('work',{'work':'one'}),
    ('sequence',{'sequence_works':'one,two'}),('parallel',{'prompt':'Inspect'}),
    ('research',{'prompt':'Investigate','urls':'https://example.com'})])
def test_console_auto_execution_arguments(tmp_path,mode,extra):
    from relay.console_backend import Selection,command
    selected=Selection(str(tmp_path),mode=mode,auto_execution=True,**extra)
    args=command(selected)
    assert args[args.index('--deployment')+1]=='auto' and '--unlimited' in args
    selected.profile='custom.json'
    with pytest.raises(ValueError,match='not both'):command(selected)


def test_tui_auto_execution_survives_reopen(proposal,monkeypatch):
    from relay.native_console import WorkConsole
    from test_mutation_recovery import LocalBackend
    from textual.widgets import Checkbox,Select,TextArea
    monkeypatch.setenv('TARE_STORAGE_ROOT',str(proposal[0].parent/'state'))
    root=proposal[0].parent/'project'
    async def scenario():
        app=WorkConsole(root,backend=LocalBackend())
        async with app.run_test(size=(145,58)) as pilot:
            await pilot.pause();app.query_one('#mode',Select).value='session'
            app.query_one('#auto-execution',Checkbox).value=True
            app.query_one('#session-role',Select).value='explorer'
            app.query_one('#prompt',TextArea).load_text('Inspect')
            app.save_demand();identity=app.demand_id
        app=WorkConsole(root,backend=LocalBackend())
        async with app.run_test(size=(145,58)) as pilot:
            await pilot.pause();await app.list_demands().wait();app.query_one('#saved-demand',Select).value=identity
            app.open_demand();await app.workers.wait_for_complete()
            assert app.selection().auto_execution and app.selection().role=='explorer' and app.process is None
    asyncio.run(scenario())


def test_sequence_cli_pins_implementer_once(sequence, candidates, monkeypatch):
    for row in candidates['deployments']: row['roles'].append('implementer')
    candidates['deployments'][0]['cost_tier']='free'
    profiles=[]
    def execute(path,**kw):
        state=json.loads((path/work_sequence.FILE).read_bytes());profiles.append(state['config']['native_deployment']);return {'status':'COMPLETED'}
    monkeypatch.setattr(work_sequence,'execute',execute)
    assert work_sequence.main(['--sequence',str(sequence['dir']),'--root',str(sequence['repo']),
        '--works','work-one','work-two','--deployment','auto','--unlimited','--format','json'])==0
    candidates['deployments'].clear()
    assert work_sequence.main(['--sequence',str(sequence['dir']),'--resume','--format','json'])==0
    assert len(profiles)==2 and profiles[0]==profiles[1] and profiles[0]['native_selection']['role']=='implementer'


def test_config_default_role_is_used_for_auto(selection, tmp_path, monkeypatch):
    config=tmp_path/'config.json'
    config.write_text(json.dumps({'schema':'tare.tools/native-agent-config/1','repositories':{'project':str(tmp_path)}}))
    calls=[]
    def run(configuration, chosen, *args, **kwargs):
        calls.append(chosen['native_selection']['role']);return {'status':'PROPOSED','authority':'NONE'}
    monkeypatch.setattr(native_agent,'run_session',run)
    assert native_agent.main(['--config',str(config),'--deployment','auto','--instruction','Inspect'])==0
    assert calls==['implementer']


def test_auto_implementer_composes_with_independent_auto_auditor(host, candidates, reviewers, capsys):
    import tare_ops
    candidates['deployments'][0]['roles'].append('implementer')
    assert tare_ops.main(['run','work-one','--repo-root',str(host[1]),'--deployment','auto',
        '--auditor-deployment','auto','--unlimited','--max-bytes','65536'])==0
    result=json.loads(capsys.readouterr().out)
    assert result['status']=='SETTLED' and reviewers[1]==['one']
    decisions=result['audit_routing']['selection']['candidates']
    assert next(r for r in decisions if r['deployment']=='author')['reason']=='AUDITOR_NOT_INDEPENDENT'
