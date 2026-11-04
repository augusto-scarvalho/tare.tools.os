"""Plan review gets the actual control root; retries preserve completed evidence."""
import json
from pathlib import Path
import pytest
from relay import native_agent, native_work_host, relay_mesh
from relay.work_recovery import new_plan_attempt


@pytest.mark.parametrize('same', [False, True])
def test_plan_control_paths_are_explicit_and_roots_not_duplicated(tmp_path, monkeypatch, same):
    execution=tmp_path/'repo'; control=execution if same else tmp_path/'control'
    monkeypatch.setattr(relay_mesh,'RELAY_ROOT',control)
    monkeypatch.setattr(native_work_host,'plan_context',lambda *args, **kwargs:'PLAN_CONTEXT.test.json')
    seen={}
    def run(config, deployment, custody, instruction, **kwargs):
        seen.update(config=config,instruction=instruction)
        return {'status':'BLOCKED','reason':'test stops before model','session':str(custody)}
    monkeypatch.setattr(native_agent,'run_session',run)
    result=native_work_host.plan_audit({},tmp_path/'train','repo',execution,tmp_path/'audit')
    roots=seen['config']['repositories']
    assert len(roots)==(2 if same else 3)
    assert str(control.resolve()) in roots.values()
    assert 'Work control repository: '+('repo' if same else 'work-control') in seen['instruction']
    assert 'Source reference IDs are keys' in seen['instruction']
    from relay.blocker_rubric import BLOCKER_RUBRIC
    assert BLOCKER_RUBRIC in seen['instruction']
    assert seen['config']['writable']=={} and result['verdict']=='INCONCLUSIVE'
    # The audited repository's rules only: the OS's AGENTS.md reached every plan audit before 2026-10-07.
    assert seen['config']['instruction_repositories']==['repo']


@pytest.mark.parametrize('status', ['PROPOSED','BLOCKED'])
def test_retry_preserves_old_evidence_limits_and_attempt_history(tmp_path,status):
    previous=tmp_path/'plan-audit';previous.mkdir()
    (previous/'native-agent.json').write_text(json.dumps({'invocations':[{'ended_at':1}]}))
    (previous/'outcome.json').write_text(json.dumps({'status':status,'output':'REVISE'}))
    (previous/'writer.lock').write_bytes(b'0')
    before={p.name:p.read_bytes() for p in previous.iterdir()}
    origin={'plan_custody':str(previous),'effective_limits':{'plan':{'max_model_calls':None}}}
    new_plan_attempt(tmp_path,origin)
    assert before=={p.name:p.read_bytes() for p in previous.iterdir()}
    assert origin['plan_audit_attempts']==[str(previous)]
    assert origin['effective_limits']=={'plan':{'max_model_calls':None}}
    assert Path(origin['plan_custody']).parent==tmp_path
    assert not Path(origin['plan_custody']).exists()
    assert json.loads((tmp_path/'WORK_RUN.json').read_text())==origin


def test_retry_refuses_unfinished_review_without_changing_origin(tmp_path):
    previous=tmp_path/'plan-audit';previous.mkdir()
    (previous/'native-agent.json').write_text(json.dumps({'invocations':[{'started_at':1}]}))
    (previous/'outcome.json').write_text(json.dumps({'status':'INTERRUPTED'}))
    origin={'plan_custody':str(previous)}
    with pytest.raises(ValueError,match='FINISHED_ATTEMPT'):
        new_plan_attempt(tmp_path,origin)
    assert origin=={'plan_custody':str(previous)} and not (tmp_path/'WORK_RUN.json').exists()


def test_plan_projection_selects_exact_work_sources_and_preserves_snapshot(tmp_path):
    from relay.train_execution import canonical_json_bytes
    train=tmp_path/'train';train.mkdir()
    graph={'nodes':[{'id':'selected','title':'Actual task','source_refs':['source','missing']},
                    {'id':'unrelated','title':'Do not inject me'}],
           'sources':{'source':{'locator':'owner@commit:docs/task.md'},'other':{'locator':'unrelated'}}}
    graph_path=tmp_path/'work-graph.json';graph_path.write_text(json.dumps(graph))
    descriptor={'schema':'tare.tools/train-execution/1','train_id':'T','work_id':'selected',
        'work_graph':'work-graph.json','execution_repository':'repo','base_commit':'a'*40,
        'target_ref':'refs/heads/main','library_manifest':'manifest.json','tracked_paths':['src'],
        'verification_checks':[{'criterion':'AC-01','command':['python','-V']}]}
    (train/'EXECUTION_DESCRIPTOR.json').write_bytes(canonical_json_bytes(descriptor))
    name=native_work_host.plan_context(train,tmp_path,tmp_path/'attempt')
    snapshot=(train/name).read_bytes();payload=json.loads(snapshot)
    assert payload['work']['id']=='selected' and set(payload['sources'])=={'source','missing'}
    assert payload['sources']['missing'] is None and b'Do not inject me' not in snapshot
    graph['nodes'][1]['title']='unrelated update';graph_path.write_text(json.dumps(graph))
    assert native_work_host.plan_context(train,tmp_path,tmp_path/'attempt')==name
    assert (train/name).read_bytes()==snapshot
    assert native_work_host.plan_context(train,tmp_path,tmp_path/'new-attempt')!=name


@pytest.mark.parametrize('cites_prepared', [False, True])
def test_pending_prepared_review_accept_requires_source_and_patch_citations(tmp_path, monkeypatch, cites_prepared):
    train = tmp_path/'train'; train.mkdir()
    projection = {'summary': 'Implemented; awaiting semantic review.',
                  'remaining': ['Semantic accuracy requires reviewer assessment.']}
    monkeypatch.setattr(native_work_host, 'plan_context', lambda *a, **kw: 'PLAN_CONTEXT.test.json')
    monkeypatch.setattr(native_work_host, 'prepared_plan_context', lambda *a: projection)
    seen = {}
    def run(config, deployment, custody, instruction, **kwargs):
        seen['instruction'] = instruction
        names = ['PACKET.md', 'EXECUTION_DESCRIPTOR.json']
        if cites_prepared:
            names += ['PREPARED_IMPLEMENTATION.json', 'PREPARED_IMPLEMENTATION.patch']
        artifact = tmp_path/'controlled-review.json'
        artifact.write_text(json.dumps({'payload': {'verdict': 'ACCEPT', 'findings': [], 'remaining': [],
            'summary': 'Controlled review result for citation validation.',
            'sources': [{'repository': 'train', 'path': name, 'sha256': 'a'*64} for name in names]}}))
        return {'status': 'PROPOSED', 'session': str(custody), 'artifact_file': str(artifact)}
    monkeypatch.setattr(native_agent, 'run_session', run)
    result = native_work_host.plan_audit({}, train, 'repo', tmp_path/'repo', tmp_path/'review')
    assert 'Assess every declared remaining obligation' in seen['instruction']
    assert 'retain any genuinely unsatisfied obligation as blocking' in seen['instruction']
    assert result['verdict'] == ('ACCEPT' if cites_prepared else 'INCONCLUSIVE')
    if not cites_prepared:
        assert result['failure_code'] == 'INVALID_DELIVERY'
        assert len(result['findings']) == 2
