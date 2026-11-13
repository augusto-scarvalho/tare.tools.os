"""Sequence orchestration with real graph/Git/native tools and scripted provider."""
import asyncio
import copy
import json
from pathlib import Path

import pytest
from graph_backlog.core import WorkGraph
from graph_backlog.jsonutil import compute_revision_hash
from graph_backlog.mutations import add_node_to_graph
from relay import relay_mesh, work_sequence, work_run, human_wait
from test_work_resume import host
from test_work_run import os_root, _git


@pytest.fixture
def sequence(host, monkeypatch):
    from compute_plane import native_models
    from relay.native_catalog import select
    root, repo = host[:2]
    (repo/'src/second.txt').write_text('base\n')
    _git('add','src/second.txt',cwd=repo); _git('commit','-qm','second fixture',cwd=repo)
    old = json.loads(relay_mesh.WORK_GRAPH_FILE.read_bytes())['nodes'][0]
    data = {'meta': {'schema':'work-graph-poc/0.5','title':'Sequence fixture','snapshot':'test',
                    'generated_at':'2026-09-13','projection_run_id':'sequence-test'},
            'sources':{},'nodes':[],'edges':[]}
    for key, path in [('work-one','src/value.txt'),('work-two','src/second.txt')]:
        node = add_node_to_graph(WorkGraph(data), key, key, exit_criteria=['content-is-valid'])['nodes'][-1]
        node.update(copy.deepcopy(old)); node['id'] = key
        node.update(canonical_system='local',canonical_id=key,canonical_revision='r1',staleness_state='FRESH')
        node['completion'] = {'status':'NOT_DONE','dod_satisfied':False}
        node['repository_scopes'][0]['target_paths'] = [path]
        node['metrics']['verification_checks'][0]['command'][-1] = (
            "from pathlib import Path; assert Path("+repr(path)+").read_text() == 'strict\\n'")
        data['nodes'].append(node)
    data['edges'] = [{'from':'work-one','to':'work-two','type':'UNLOCKS','semantic':True,
                      'source_refs':[],'source_details':[],'confidence':{'score':1},'notes':[],'requirements':{}}]
    save_graph(data)
    calls=[]
    class Client:
        endpoint='http://fixture.test'
        def post(self, path, body, control):
            if path.endswith('/count-tokens'): return {'inputTokens':500}
            calls.append(body)
            turn=sum(m['role']=='assistant' for m in body['messages'])+1
            target='src/second.txt' if 'work-two' in json.dumps(body) else 'src/value.txt'
            if turn==1: name,args='read',{'repository':'repo-one','path':target}
            elif turn in (2,4): name,args='verify',{'name':'content-is-valid'}
            elif turn==3:
                reads=[json.loads(b['toolResult']['content'][0]['text']) for m in body['messages']
                       for b in m['content'] if 'toolResult' in b]
                sha=next(v['sha256'] for v in reads if 'sha256' in v)
                name,args='edit',{'repository':'repo-one','path':target,'sha256':sha,'search':'base','replace':'strict'}
            elif turn==5: name,args='tare_deliver',{'summary':'fixture changed and verified'}
            else: raise AssertionError('unexpected generation '+str(turn))
            return {'stopReason':'tool_use','output':{'message':{'role':'assistant','content':[
                {'toolUse':{'toolUseId':str(turn),'name':name,'input':args}}]}},
                'usage':{'inputTokens':500,'outputTokens':20,'cacheReadInputTokens':0,'cacheWriteInputTokens':0}}
    class HttpFixture:
        def __new__(cls,*a,**kw): return Client()
    monkeypatch.setattr(native_models,'NativeHttpClient',HttpFixture)
    monkeypatch.setattr(work_sequence,'sequence_root',lambda:root/'sequences')
    return {'host':host,'repo':repo,'dir':root/'sequences'/'selected','profile':select(None,host[2]),'calls':calls}


def save_graph(data):
    data['revision']=compute_revision_hash(data)
    relay_mesh.WORK_GRAPH_FILE.write_text(json.dumps(data))


def create(case, **kwargs):
    return work_sequence.create(case['dir'], ['work-two','work-one'], case['repo'],case['profile'],unlimited=True,**kwargs)


@pytest.mark.parametrize('fault',['custody','duplicate','unknown','unprepared'])
def test_invalid_sequence_selection_is_refused_before_dispatch(sequence,fault):
    directory=sequence['repo']/'src'/'control' if fault=='custody' else sequence['dir']
    ids=['work-one','work-one'] if fault=='duplicate' else ['missing'] if fault=='unknown' else ['work-one']
    if fault=='unprepared':
        data=json.loads(relay_mesh.WORK_GRAPH_FILE.read_bytes())
        data['nodes'][0]['admission_state']='PROPOSED_FROM_PLAN_NOT_ADMITTED';save_graph(data)
    with pytest.raises(ValueError):work_sequence.create(directory,ids,sequence['repo'],sequence['profile'])
    assert not (directory/work_sequence.FILE).exists() and not sequence['calls']


@pytest.mark.parametrize('relation',['UNLOCKS','DEPENDS_ON'])
def test_orders_and_settles_two_works_without_repeating_completed_items(sequence,relation):
    if relation=='DEPENDS_ON':
        data=json.loads(relay_mesh.WORK_GRAPH_FILE.read_bytes())
        # The installed owner's taxonomy defines source as prerequisite for both labels.
        data['edges'][0]['type']='DEPENDS_ON';save_graph(data)
    original={n['id']:copy.deepcopy(n) for n in json.loads(relay_mesh.WORK_GRAPH_FILE.read_bytes())['nodes']}
    view=create(sequence)
    assert [r['work_id'] for r in view['items']]==['work-one','work-two']
    assert not sequence['calls']
    result=work_sequence.execute(sequence['dir'])
    assert result['status']=='COMPLETED',result
    assert result['completed']==2 and len(sequence['calls'])==10 and len(sequence['host'][4])==2
    assert (sequence['repo']/'src/value.txt').read_text()==(sequence['repo']/'src/second.txt').read_text()=='strict\n'
    assert work_sequence.execute(sequence['dir'])==result
    assert len(sequence['calls'])==10
    runs=[r['attempts'][-1]['receipt'] for r in result['items']]
    receipts=[json.loads(Path(p).read_bytes()) for p in runs]
    assert all(r['status']=='SETTLED' for r in receipts)
    assert receipts[0]['candidate_commit']!=receipts[1]['candidate_commit']
    landed={n['id']:n for n in json.loads(relay_mesh.WORK_GRAPH_FILE.read_bytes())['nodes']}
    for row, receipt in zip(result['items'], receipts):
        node=landed[row['work_id']]
        before=original[row['work_id']]
        completion=node['completion']
        assert completion['status']=='DONE' and completion['dod_satisfied'] is True
        assert completion['evidence_grade']=='C'
        assert node['work_status']=='done'
        assert node['provenance'][:-1]==before.get('provenance',[])
        assert completion['dod_evidence'][:-1]==before['completion'].get('dod_evidence',[])
        reference=node['provenance'][-1]
        assert reference==completion['dod_evidence'][-1]
        assert reference.startswith('relay-signoff:')
        signoff=json.loads(reference[len('relay-signoff:'):])
        assert signoff['train']==row['train_id']
        assert signoff['candidate_commit']==receipt['candidate_commit']
        assert 'artifacts' in signoff
        assert work_sequence.identity(node)==work_sequence.identity(before)==row['identity']
        assert row['completed_node_sha256']==work_sequence.sha256_canonical(node)


def _with_sequence_landing(node, *, strict=True, authority='SMART_POLICY'):
    landed=copy.deepcopy(node)
    receipt={'train':work_run.train_id_for(node['id'],node), 'readyRevision':5,
             'authority':authority, 'capability_sha256':'b'*64, 'artifacts':{}}
    if strict:
        receipt.update(descriptor_sha256='c'*64,candidate_sha256='d'*64,candidate_commit='e'*40)
    reference='relay-signoff:'+json.dumps(receipt,sort_keys=True)
    landed.setdefault('provenance',[]).append(reference)
    landed['completion'].update(status='DONE',dod_satisfied=True)
    landed['completion'].setdefault('dod_evidence',[]).append(reference)
    landed['work_status']='done'
    return landed


@pytest.mark.parametrize('strict,authority',[(False,'HUMAN_OPERATOR'),(True,'SMART_POLICY')])
def test_identity_projects_only_bound_final_native_receipt(sequence,strict,authority):
    node=copy.deepcopy(json.loads(relay_mesh.WORK_GRAPH_FILE.read_bytes())['nodes'][0])
    node['provenance']=['original provenance']
    node['source_refs']=['original source']
    before=copy.deepcopy(node)
    landed=_with_sequence_landing(node,strict=strict,authority=authority)
    snapshot=copy.deepcopy(landed)
    assert work_sequence.identity(landed)==work_sequence.identity(node)
    assert landed==snapshot and node==before
    for field in ('provenance','source_refs'):
        changed=copy.deepcopy(landed)
        changed[field][0]='changed original input'
        assert work_sequence.identity(changed)!=work_sequence.identity(node)
    historical=copy.deepcopy(node)
    historical.pop('provenance')
    expected={k:v for k,v in historical.items() if k not in {'completion','work_status'}}
    assert work_sequence.identity(historical)==work_sequence.sha256_canonical(expected)
    assert work_sequence.identity(historical)!=work_sequence.identity(node)


@pytest.mark.parametrize('fault',[
    'no-dod','not-last-dod','malformed','other-train','unknown-field','partial-strict',
    'bad-capability','bad-commit','bad-artifacts','bad-artifact-digest','bad-revision-text',
    'bad-revision-bool','bad-authority','not-done','not-work-done','not-final'])
def test_identity_retains_unrecognized_signoff_references(sequence,fault):
    node=copy.deepcopy(json.loads(relay_mesh.WORK_GRAPH_FILE.read_bytes())['nodes'][0])
    node['provenance']=['original provenance']
    landed=_with_sequence_landing(node)
    receipt=json.loads(landed['provenance'][-1][len('relay-signoff:'):])
    if fault=='other-train':receipt['train']='another-train'
    elif fault=='unknown-field':receipt['unexpected']=True
    elif fault=='partial-strict':receipt.pop('descriptor_sha256')
    elif fault=='bad-capability':receipt['capability_sha256']='not-a-digest'
    elif fault=='bad-commit':receipt['candidate_commit']=False
    elif fault=='bad-artifacts':receipt['artifacts']='not-an-artifact-collection'
    elif fault=='bad-artifact-digest':receipt['artifacts']={'AUDIT_REPORT.md':'not-a-digest'}
    elif fault=='bad-revision-text':receipt['readyRevision']='5'
    elif fault=='bad-revision-bool':receipt['readyRevision']=True
    elif fault=='bad-authority':receipt['authority']='NONE'
    reference='relay-signoff:'+json.dumps(receipt)
    if fault=='malformed':reference='relay-signoff:{broken'
    landed['provenance'][-1]=landed['completion']['dod_evidence'][-1]=reference
    if fault=='no-dod':landed['completion']['dod_evidence']=[]
    elif fault=='not-last-dod':landed['completion']['dod_evidence'].append('later evidence')
    elif fault=='not-done':landed['completion']['status']='NOT_DONE'
    elif fault=='not-work-done':landed['work_status']='in_progress'
    elif fault=='not-final':landed['provenance'].append('later provenance')
    expected={k:v for k,v in landed.items() if k not in {'completion','work_status'}}
    assert work_sequence.identity(landed)==work_sequence.sha256_canonical(expected)
    assert work_sequence.identity(landed)!=work_sequence.identity(node)
    assert not sequence['calls']


def test_settled_receipt_mutation_is_rejected_by_complete_node_hash(sequence):
    data=json.loads(relay_mesh.WORK_GRAPH_FILE.read_bytes())
    data['nodes'][0]['provenance']=['original provenance']
    save_graph(data)
    create(sequence)
    node=_with_sequence_landing(data['nodes'][0])
    data['nodes'][0]=node
    save_graph(data)
    path=sequence['dir']/work_sequence.FILE
    state=json.loads(path.read_bytes())
    row=state['items'][0]
    assert work_sequence.identity(node)==row['identity']
    row.update(status='SETTLED',completed_node_sha256=work_sequence.sha256_canonical(node))
    path.write_text(json.dumps(state))
    receipt=json.loads(node['provenance'][-1][len('relay-signoff:'):])
    receipt['capability_sha256']='f'*64
    reference='relay-signoff:'+json.dumps(receipt)
    node['provenance'][-1]=node['completion']['dod_evidence'][-1]=reference
    assert work_sequence.identity(node)==row['identity']
    assert work_sequence.sha256_canonical(node)!=row['completed_node_sha256']
    save_graph(data)
    result=work_sequence.execute(sequence['dir'])
    assert result['status']=='NEEDS_REVIEW'
    assert result['reason']=='Completion changed: work-one'
    assert result['items'][0]['completed_node_sha256']==row['completed_node_sha256']
    assert not sequence['calls'] and not sequence['host'][4]


def test_pause_second_work_and_resume_retains_first_result(sequence):
    create(sequence,wait_before_implement='Proceed?')
    first=work_sequence.execute(sequence['dir'])
    assert first['status']=='PAUSED' and first['completed']==0 and not sequence['calls']
    value=human_wait.inspect(work_run.train_dir_for('work-one'))
    human_wait.answer('work-one',value['request_sha256'],'approve')
    second=work_sequence.execute(sequence['dir'])
    assert second['status']=='PAUSED' and second['completed']==1 and len(sequence['calls'])==5,second
    retained=copy.deepcopy(second['items'][0])
    assert work_sequence.execute(sequence['dir'])['completed']==1 and len(sequence['calls'])==5
    value=human_wait.inspect(work_run.train_dir_for('work-two'))
    human_wait.answer('work-two',value['request_sha256'],'approve')
    result=work_sequence.execute(sequence['dir'])
    assert result['status']=='COMPLETED',result
    assert result['items'][0]==retained and len(sequence['calls'])==10 and len(sequence['host'][4])==2


def test_lost_sequence_ack_recovers_work_settlement_without_reimplementation(sequence,monkeypatch):
    create(sequence)
    original=work_run.run_work
    def lost(*args,**kwargs):
        result=original(*args,**kwargs)
        assert result['status']=='SETTLED',result
        raise SystemExit('lost result after landing')
    with monkeypatch.context() as cut:
        cut.setattr(work_run,'run_work',lost)
        with pytest.raises(SystemExit):work_sequence.execute(sequence['dir'])
    assert len(sequence['calls'])==5
    result=work_sequence.execute(sequence['dir'])
    assert result['status']=='COMPLETED' and len(sequence['calls'])==10,result
    assert result['items'][0]['attempts'][-1]['resume'] is True
    assert len(sequence['host'][4])==2


@pytest.mark.parametrize('fault',['work','dependency','dispatch','cancel','lock','external'])
def test_stale_or_blocked_sequence_does_not_start_a_model(sequence,monkeypatch,fault):
    if fault=='external':
        data=json.loads(relay_mesh.WORK_GRAPH_FILE.read_bytes())
        node=copy.deepcopy(data['nodes'][0]);node.update(id='external',canonical_id='external')
        data['nodes'].append(node)
        edge=copy.deepcopy(data['edges'][0]);edge.update({'from':'external','to':'work-one'})
        data['edges'].append(edge);save_graph(data)
    create(sequence)
    if fault in {'work','dependency'}:
        data=json.loads(relay_mesh.WORK_GRAPH_FILE.read_bytes())
        if fault=='work':data['nodes'][0]['exit_criteria']=['new criterion']
        else:data['edges'][0]['requirements']={'minimum_evidence_grade':'A'}
        save_graph(data)
    elif fault=='dispatch':
        original=work_run.run_work
        def race(*args,**kwargs):
            data=json.loads(relay_mesh.WORK_GRAPH_FILE.read_bytes());data['nodes'][0]['title']='Changed';save_graph(data)
            return original(*args,**kwargs)
        monkeypatch.setattr(work_run,'run_work',race)
    from tare_tools_agent_runtime import CancellationToken
    cancel=CancellationToken()
    if fault=='cancel':cancel.cancel()
    if fault=='lock':
        from compute_plane.native_journal import custody_lock
        with custody_lock(sequence['dir']):
            with pytest.raises(ValueError):work_sequence.execute(sequence['dir'])
    else:
        result=work_sequence.execute(sequence['dir'],cancellation=cancel)
        assert result['status']=={'work':'NEEDS_REVIEW','dependency':'NEEDS_REVIEW','dispatch':'PAUSED',
                                  'cancel':'CANCELLED','external':'BLOCKED'}[fault],result
    assert not sequence['calls'] and not sequence['host'][4]


def test_initial_completion_is_observed_and_invalidated_completion_is_not_skipped(sequence):
    data=json.loads(relay_mesh.WORK_GRAPH_FILE.read_bytes())
    data['nodes'][0]['completion']={'status':'DONE','dod_satisfied':True}
    save_graph(data)
    view=create(sequence)
    assert view['items'][0]['status']=='OBSERVED_DONE'
    data['nodes'][0]['completion']={'status':'NOT_DONE','dod_satisfied':False};save_graph(data)
    result=work_sequence.execute(sequence['dir'])
    assert result['status']=='NEEDS_REVIEW' and not sequence['calls']


def test_unprepared_sequence_reports_actionable_refusal(sequence,capsys):
    data=json.loads(relay_mesh.WORK_GRAPH_FILE.read_bytes())
    data['nodes'][0]['admission_state']='PROPOSED_FROM_PLAN_NOT_ADMITTED';save_graph(data)
    result=work_sequence.main(['--works','work-one','--root',str(sequence['repo']),
        '--native-deployment',str(sequence['host'][2]),'--sequence',str(sequence['dir']),'--format','json'])
    assert result==2
    view=json.loads(capsys.readouterr().out)
    assert 'WORK_REQUIRES_PREPARATION' in work_sequence.render(view)
    assert not (sequence['dir']/work_sequence.FILE).exists() and not sequence['calls']


def test_tui_dispatch_and_reopen_active_human_wait(sequence):
    from relay.native_console import WorkConsole
    from relay.console_backend import Backend,Selection,command
    from textual.widgets import Input,Select,Button
    selected=Selection(root=str(sequence['repo']),mode='sequence',sequence_works='work-two, work-one')
    args=command(selected)
    assert args[:1]==['sequence'] and '--unlimited' in args and '--works' in args
    create(sequence,wait_before_implement='Proceed?')
    assert work_sequence.execute(sequence['dir'])['status']=='PAUSED'
    selected.sequence=str(sequence['dir'])
    assert '--works' not in command(selected,resume=True)
    class LocalBackend(Backend):
        def catalog(self,root):return {'models':[{'deployment':'local-qwen','model':'fixture'}],'sessions':[],'works':[],'errors':[]}
    async def scenario():
        app=WorkConsole(sequence['repo'],backend=LocalBackend())
        async with app.run_test(size=(140,48)) as pilot:
            await pilot.pause()
            app.query_one('#mode',Select).value='sequence'
            app.query_one('#sequence',Input).value=str(sequence['dir'])
            await app.review_selection().wait()
            await app.open_sequence_work().wait()
            assert app.query_one('#mode',Select).value=='work'
            assert app.query_one('#work-id',Input).value=='work-one'
            assert not app.query_one('#approve-decision',Button).disabled
            app.answer_decision('approve')
            while app.busy:await pilot.pause(.05)
            assert app.last_result['status']=='HUMAN_APPROVED'
            app.query_one('#mode',Select).value='sequence'
            assert app.selection().sequence==str(sequence['dir'])
    asyncio.run(scenario())


def test_cli_rejects_stale_preview_before_creating_sequence(sequence, capsys):
    view=work_sequence.preview(['work-one','work-two'],sequence['repo'])
    graph=json.loads(relay_mesh.WORK_GRAPH_FILE.read_bytes())
    graph['nodes'][1]['title']='Changed since review';save_graph(graph)
    assert work_sequence.main(['--works','work-one','work-two','--root',str(sequence['repo']),
        '--native-deployment',str(sequence['host'][2]),'--sequence',str(sequence['dir']),
        '--expected-selection',view['sha256'],'--format','json'])==2
    assert 'SELECTION_CHANGED' in json.loads(capsys.readouterr().out)['error']
    assert not sequence['calls'] and not (sequence['dir']/work_sequence.FILE).exists()
