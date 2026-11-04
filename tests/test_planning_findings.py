import asyncio
import copy
import hashlib
import json
import shutil
from pathlib import Path
from compute_plane.native_journal import io_path

import pytest
from compute_plane.native_journal import save_json
from relay import planning_findings as findings, parallel_explore, native_plan, native_catalog, native_agent
from test_native_plan import proposal
from test_native_agent import deployment, setup
from test_research_flow import study


@pytest.fixture
def reports(proposal, monkeypatch):
    root=proposal[0].parent/'project';directory=root.parent/'batch'
    monkeypatch.setenv('TARE_STORAGE_ROOT',str(root.parent/'state'))
    file=root/'finding.txt';file.write_bytes(b'TTL is configurable.\n')
    parallel_explore.create(directory,root,['Check default','Check override','Unknown area'],deployment())
    state=parallel_explore.read(directory);digest=hashlib.sha256(file.read_bytes()).hexdigest()
    for index,summary in enumerate(['Default is five minutes.','Override permits ten minutes.'],1):
        source={'repository':'project','path':'finding.txt','sha256':digest}
        artifact={'schema':'tare.tools/native-artifact/1','role':'explorer','kind':'exploration','authority':'NONE',
            'session_id':'author-'+str(index),'call_id':'call','profile_digest':str(index)*64,
            'payload':{'summary':summary,'remaining':['Needs a runtime check.'],'sources':[source]}}
        (directory/str(index)).mkdir();save_json(directory/str(index)/'artifact.json',artifact)
        state['items'][index-1]['status']='PROPOSED'
    state['status']='PAUSED';save_json(directory/'batch.json',state)
    return directory,root


def saved(reports):
    view=findings.preview('parallel',reports[0]);return findings.save(view,view['sha256'],'Compare both claims; defaults may differ.')


def test_snapshot_preserves_authorship_limits_omissions_and_deduplicates(reports):
    view=findings.preview('parallel',reports[0]);assert len(view['documents'])==1
    packet=saved(reports);value=findings.load(packet['path'],packet['sha256'],reports[1])
    assert [r['artifact']['session_id'] for r in value['reports']]==['author-1','author-2']
    assert all(r['artifact']['payload']['remaining'] for r in value['reports'])
    assert value['omitted'][0]['id']=='3' and value['omitted'][0]['status']=='PENDING'
    assert value['authority']=='NONE' and 'defaults' in value['review']['note']
    assert findings.save(view,view['sha256'],'Compare both claims; defaults may differ.')==packet
    subset=findings.preview('parallel',reports[0],['2'])
    assert len(subset['reports'])==1 and len(subset['omitted'])==2
    assert 'Override' in findings.render(subset)


@pytest.mark.parametrize('change',['source','artifact','scope'])
def test_changed_preview_refuses_capture(reports, change):
    view=findings.preview('parallel',reports[0])
    if change=='source': (reports[1]/'finding.txt').write_bytes(b'changed')
    elif change=='artifact':
        path=reports[0]/'1/artifact.json';value=json.loads(path.read_bytes());value['payload']['summary']='changed';save_json(path,value)
    else: view['reports'].pop()
    with pytest.raises(ValueError): findings.save(view,view['sha256'])


@pytest.mark.parametrize('fault',['source','identity','project'])
def test_packet_rejects_tampering_and_wrong_project(reports, fault):
    packet=saved(reports);root=reports[1]
    if fault=='source': io_path(next((Path(packet['path']).parent/'sources').glob('*.txt'))).write_bytes(b'changed')
    elif fault=='identity': packet['sha256']='a'*64
    else: root=root.parent
    with pytest.raises(ValueError):findings.load(packet['path'],packet['sha256'],root)


def test_research_snapshot_retains_url_and_survives_origin_retirement(study, monkeypatch):
    from relay import research_flow
    directory,root,calls,fetched,collect=study
    monkeypatch.setenv('TARE_STORAGE_ROOT',str(directory.parent/'state'))
    assert research_flow.execute(directory,fetch=collect)['status']=='COMPLETED'
    view=findings.preview('research',directory);packet=findings.save(view,view['sha256'])
    shutil.rmtree(directory)
    value=findings.load(packet['path'],packet['sha256'],root)
    assert value['reports'][0]['web_citations'][0]['url']=='https://docs.example.com/guide'
    assert len(calls)==len(fetched)==1


@pytest.fixture
def provider(proposal,monkeypatch):
    calls=[];missing=[]
    def run(config,deployment,custody,instruction,**kw):
        calls.append((copy.deepcopy(config),instruction))
        if config['role']=='planner':
            payload=copy.deepcopy(proposal[2]['plan']);repo='request';path=Path(config['repositories'][repo])
            request=json.loads((path/'request.json').read_bytes())
            names=(['findings/manifest.json'] if request.get('findings') else [])+(['request.json'] if 'previous_plan' in request else [])
        else:
            payload={'summary':'Checked claims against demand','remaining':[],'findings':[],'verdict':'ACCEPT','sources':[]}
            repo='proposal';path=Path(config['repositories'][repo]);names=['proposal.json','request.json','findings/manifest.json']
            request=json.loads((path/'request.json').read_bytes())
        if request.get('promotion_template'):
            names.append(request['promotion_template']['path'])
        for name in names:
            if config['role'] in missing and name=='findings/manifest.json':continue
            if config['role']+':template' in missing and name=='findings/intake.md':continue
            payload['sources'].append({'repository':repo,'path':name,'sha256':hashlib.sha256((path/name).read_bytes()).hexdigest()})
        artifact={'schema':'tare.tools/native-artifact/1','role':config['role'],'kind':'plan' if config['role']=='planner' else 'review',
            'authority':'NONE','session_id':'test','call_id':'call','profile_digest':'f'*64,'payload':payload}
        custody.mkdir(parents=True);save_json(custody/'artifact.json',artifact)
        return {'status':'PROPOSED','artifact_file':str(custody/'artifact.json')}
    monkeypatch.setattr(native_agent,'run_session',run)
    return calls,missing


def generate(proposal,reports,packet):
    return native_plan.generate(reports[1],proposal[1],reports[1].parent/'new-plan','Use research to plan a bounded change',
        native_catalog.select('local-qwen'),findings=packet['path'],findings_sha256=packet['sha256'],unlimited=True)


def test_plan_and_review_receive_pinned_sources_and_apply(proposal,reports,provider):
    criteria=['Report 1 / AC-01: Given value 1, When setting 2, Then saved value equals 2.',
              'Report 1 / AC-02: Given value 1, When setting invalid input, Then reject and preserve 1.']
    proposal[2]['plan']['tasks'][0]['criteria']=criteria
    packet=saved(reports);result=generate(proposal,reports,packet)
    assert result['status']=='REVIEWED' and len(provider[0])==2
    assert all('findings/manifest.json' in instruction for _,instruction in provider[0])
    assert all('findings/intake.md' in instruction for _,instruction in provider[0])
    manifest=Path(result['plan_dir'])/'input/findings/manifest.json'
    assert findings.load(manifest,packet['sha256'],reports[1])['review']['note']
    template=manifest.parent/'intake.md'
    assert template.read_text(encoding='utf-8')==findings.promotion_template()
    assert len(template.read_bytes())<=8192
    assert result['proposal']['promotion_template']=={'path':'findings/intake.md',
        'sha256':hashlib.sha256(template.read_bytes()).hexdigest()}
    applied=native_plan.apply(result['plan_dir'],result['proposal_sha256'])
    assert applied['status']=='APPLIED' and not applied['execution_ready']
    node=next(n for n in json.loads(proposal[1].read_bytes())['nodes'] if n['id']==applied['work_ids'][0])
    assert node['exit_criteria']==criteria and node['admission_state']=='PROPOSED_FROM_PLAN_NOT_ADMITTED'
    from relay.work_run import load_node
    with pytest.raises(ValueError,match='WORK_REQUIRES_PREPARATION'):
        load_node(proposal[1],node['id'])
    assert native_plan.apply(result['plan_dir'],result['proposal_sha256'])['status']=='ALREADY_APPLIED'


@pytest.mark.parametrize('role',['planner','plan-auditor'])
def test_missing_template_citation_cannot_apply(proposal,reports,provider,role):
    before=proposal[1].read_bytes();provider[1].append(role+':template')
    if role=='planner':
        with pytest.raises(ValueError,match='must cite.*template'):generate(proposal,reports,saved(reports))
        assert len(provider[0])==1
    else:
        result=generate(proposal,reports,saved(reports))
        assert result['status']=='REVIEW_REQUIRED'
        with pytest.raises(ValueError,match='exact proposal'):
            native_plan.apply(result['plan_dir'],result['proposal_sha256'])
    assert proposal[1].read_bytes()==before


@pytest.mark.parametrize('fault',['changed','missing','redirected'])
def test_frozen_template_failure_blocks_retry_and_apply_before_effects(proposal,reports,provider,fault):
    provider[1].append('plan-auditor:template')
    result=generate(proposal,reports,saved(reports));directory=Path(result['plan_dir'])
    before=proposal[1].read_bytes();calls=len(provider[0]);template=directory/'input/findings/intake.md'
    if fault=='changed':template.write_bytes(b'changed')
    elif fault=='missing':template.unlink()
    else:
        path=directory/'input/proposal.json';value=json.loads(path.read_bytes())
        value['promotion_template']['path']='../outside.md';save_json(path,value)
    for operation in [lambda:native_plan.preview(directory),lambda:native_plan.retry_review(directory),
                      lambda:native_plan.apply(directory,result['proposal_sha256'])]:
        with pytest.raises((OSError,ValueError)):operation()
    assert len(provider[0])==calls and proposal[1].read_bytes()==before


def test_review_retry_uses_frozen_template_and_original_inputs(proposal,reports,provider,monkeypatch):
    provider[1].append('plan-auditor:template');result=generate(proposal,reports,saved(reports))
    directory=Path(result['plan_dir']);inputs={p:p.read_bytes() for p in (directory/'input').rglob('*') if p.is_file()}
    def forbidden_refresh():pytest.fail('Retry must reuse the frozen template, not the current package.')
    monkeypatch.setattr(findings,'promotion_template',forbidden_refresh)
    provider[1].clear();reviewed=native_plan.retry_review(directory)
    assert reviewed['status']=='REVIEWED' and len(provider[0])==3
    assert all(p.read_bytes()==raw for p,raw in inputs.items())
    assert provider[0][1]==provider[0][2]


def test_historical_findings_plan_needs_no_new_template(proposal,reports,provider,monkeypatch):
    result=generate(proposal,reports,saved(reports));directory=Path(result['plan_dir'])
    # Represent the saved pre-template contract, with its original request/review digests.
    for name in ['proposal.json','request.json']:
        path=directory/'input'/name;value=json.loads(path.read_bytes());value.pop('promotion_template')
        if name=='proposal.json':
            value['plan']['sources']=[s for s in value['plan']['sources'] if s['path']!='findings/intake.md']
        save_json(path,value)
    (directory/'input/findings/intake.md').unlink()
    value=json.loads((directory/'input/proposal.json').read_bytes())
    settings=json.loads((directory/'review-settings.json').read_bytes())
    native_plan.save_review_settings(directory,value,settings['config'],settings['deployment'],settings['limits'])
    def forbidden_refresh():pytest.fail('Historical plans must not acquire a new template.')
    monkeypatch.setattr(findings,'promotion_template',forbidden_refresh)
    assert native_plan.preview(directory)['status']=='REVIEW_REQUIRED'
    reviewed=native_plan.retry_review(directory)
    assert reviewed['status']=='REVIEWED' and 'intake.md' not in provider[0][-1][1]
    assert native_plan.apply(directory,reviewed['proposal_sha256'])['status']=='APPLIED'


def test_tui_template_is_available_without_generation(reports):
    from relay.native_console import WorkConsole
    from test_mutation_recovery import LocalBackend
    from textual.widgets import Button,Select
    async def run():
        app=WorkConsole(reports[1],backend=LocalBackend())
        async with app.run_test(size=(145,58)) as pilot:
            await app.workers.wait_for_complete();await pilot.press('f2')
            app.query_one('#mode',Select).value='parallel';await pilot.pause()
            button=app.query_one('#view-promotion-template',Button);button.scroll_visible()
            await pilot.pause();await pilot.click('#view-promotion-template');await pilot.pause()
            assert app.process is None and not app.selection().findings
            assert 'Reading it starts no model or Work' in app.query_one('#status').render().plain
    asyncio.run(run())


@pytest.mark.parametrize('role',['planner','plan-auditor'])
def test_missing_findings_citation_cannot_apply(proposal,reports,provider,role):
    provider[1].append(role)
    if role=='planner':
        with pytest.raises(ValueError,match='must cite'):generate(proposal,reports,saved(reports))
        assert len(provider[0])==1
    else:
        result=generate(proposal,reports,saved(reports));assert result['status']=='REVIEW_REQUIRED'
        with pytest.raises(ValueError,match='exact proposal'):native_plan.apply(result['plan_dir'],result['proposal_sha256'])


def test_changed_saved_source_blocks_apply_before_graph_write(proposal,reports,provider):
    result=generate(proposal,reports,saved(reports));before=proposal[1].read_bytes()
    next((Path(result['plan_dir'])/'input/findings/sources').glob('*.txt')).write_bytes(b'changed')
    with pytest.raises(ValueError,match='source changed'):native_plan.apply(result['plan_dir'],result['proposal_sha256'])
    assert proposal[1].read_bytes()==before


def test_replan_uses_own_snapshot_after_external_custody_retired(proposal,reports,provider):
    packet=saved(reports);first=generate(proposal,reports,packet)
    shutil.rmtree(reports[0]);shutil.rmtree(io_path(Path(packet['path']).parent))
    second=native_plan.replan(first['plan_dir'],reports[1].parent/'successor','Clarify scope',native_catalog.select('local-qwen'))
    assert second['status']=='REVIEWED'
    assert second['proposal']['findings']==first['proposal']['findings'] and len(provider[0])==4


def test_cli_preview_capture_and_plan(proposal,reports,provider,capsys):
    import tare_ops
    args=['findings','--kind','parallel','--source',str(reports[0]),'--item','1']
    assert tare_ops.main(args)==0;view=json.loads(capsys.readouterr().out)
    assert tare_ops.main([*args,'--save',view['sha256'],'--note','Reviewed'])==0
    packet=json.loads(capsys.readouterr().out)
    assert native_plan.main(['--root',str(reports[1]),'--graph',str(proposal[1]),'--plan',str(reports[1].parent/'cli-plan'),
        '--instruction','Plan this change','--findings',packet['path'],'--findings-sha256',packet['sha256'],'--format','json'])==0
    assert json.loads(capsys.readouterr().out)['status']=='REVIEWED'


def test_tui_review_attach_reopen_and_resume_arguments(reports,monkeypatch):
    from relay.native_console import WorkConsole
    from relay.console_backend import command
    from test_mutation_recovery import LocalBackend
    from textual.widgets import Select,Input,TextArea
    async def run():
        app=WorkConsole(reports[1],backend=LocalBackend())
        async with app.run_test(size=(145,58)) as pilot:
            await pilot.pause();app.query_one('#mode',Select).value='parallel'
            app.query_one('#batch',Input).value=str(reports[0]);app.query_one('#prompt',TextArea).load_text('Plan the next change')
            app.query_one('#finding-note',Input).value='Preserve both alternatives'
            await app.preview_findings().wait();assert app.reviewed_findings
            await app.use_findings().wait();assert app.selection().mode=='plan'
            packet=app.selection().findings;identity=app.demand_id
            assert packet and app.process is None
        app=WorkConsole(reports[1],backend=LocalBackend())
        async with app.run_test(size=(145,58)) as pilot:
            await pilot.pause();await app.list_demands().wait();app.query_one('#saved-demand',Select).value=identity
            app.open_demand();await app.workers.wait_for_complete()
            selected=app.selection();assert selected.findings==packet and app.process is None
            args=command(selected);assert args[args.index('--findings')+1]==packet
    asyncio.run(run())


def test_tui_changed_selection_requires_another_preview(reports):
    from relay.native_console import WorkConsole
    from test_mutation_recovery import LocalBackend
    from textual.widgets import Select,Input
    async def run():
        app=WorkConsole(reports[1],backend=LocalBackend())
        async with app.run_test(size=(145,58)) as pilot:
            await pilot.pause();app.query_one('#mode',Select).value='parallel'
            app.query_one('#batch',Input).value=str(reports[0]);await app.preview_findings().wait()
            assert app.reviewed_findings
            app.query_one('#finding-items',Input).value='2'
            await app.use_findings().wait()
            assert app.selection().mode=='parallel' and not app.selection().findings and app.reviewed_findings is None
    asyncio.run(run())


def test_invalid_packet_blocks_before_any_generation(proposal,reports,provider):
    packet=saved(reports);packet['sha256']='f'*64
    with pytest.raises(ValueError,match='identity changed'):generate(proposal,reports,packet)
    assert provider[0]==[] and not (reports[1].parent/'new-plan').exists()


def test_native_tools_can_read_snapshot_and_deliver_citation(reports):
    from test_native_agent import Client
    packet=saved(reports);root=Path(packet['path']).parent
    manifest=json.loads((root/'manifest.json').read_bytes());source=manifest['reports'][0]['sources'][0]
    payload={'summary':'Inspected the pinned sources','remaining':['Historical snapshot; inspect current code.'],
        'sources':[{'repository':'findings','path':'manifest.json','sha256':hashlib.sha256((root/'manifest.json').read_bytes()).hexdigest()},
                   {'repository':'findings','path':source['snapshot'],'sha256':source['sha256']}]}
    client=Client(steps=[('read',{'repository':'findings','path':'manifest.json'}),
                        ('read',{'repository':'findings','path':source['snapshot']}),('tare_deliver',payload)])
    config={'schema':'tare.tools/native-agent-config/1','role':'explorer','repositories':{'findings':str(root)},
        'capabilities':['read','list','search'],'writable':{},'typed_output':True}
    result=native_agent.run_session(config,deployment(),reports[1].parent/'snapshot-reader','Read the snapshot',client=client,local_counter=client)
    assert result['status']=='PROPOSED' and client.generations==3


def test_a_sessions_plan_reaches_plan_as_reviewed_findings(setup, tmp_path, monkeypatch):
    # Ways of working: the plan a session wrote (its ledger) goes to /plan as findings, through the same review.
    from relay import work_forms
    config, custody, root = setup
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))
    custody.mkdir(parents=True, exist_ok=True)
    save_json(custody/'native-agent.json', {'config': config})
    plan = {'units': [{'key': 'value', 'title': 'Set the value', 'target_paths': ['a.py'], 'criteria': ['a.py holds 2'],
                       'verifiers': ['unit']}], 'criteria': ['done']}
    assert work_forms.record_plan(custody, config, plan)['status'] == 'PLANNED'
    view = findings.preview('session', custody)
    assert view['root'] == str(root.resolve()) and 'checked by unit' in findings.render(view)
    packet = findings.save(view, view['sha256'], 'The session planned this.')
    value = findings.load(packet['path'], packet['sha256'], root)
    assert value['reports'][0]['plan']['units'][0]['key'] == 'value'
    save_json(custody/'work.json', {'plan': plan, 'problems': ['a problem'], 'units': {}})
    with pytest.raises(ValueError, match='no checked plan'):
        findings.preview('session', custody)
