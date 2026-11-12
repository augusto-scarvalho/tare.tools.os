import hashlib,json
from pathlib import Path
import pytest
from test_work_run import os_root,evidence,_git
from test_work_resume import host,run
from relay import native_agent,relay_mesh,work_run


@pytest.fixture
def reviewers(host,monkeypatch):
    from test_native_agent import deployment,Client
    root,repo,_,_,_=host
    roster=relay_mesh.RELAY_DIR/'round_table_roster.json'
    value=json.loads(roster.read_bytes());profiles=[]
    for name,model in [('one','claude-opus-4.6'),('two','glm-5')]:
        d=deployment('bedrock-converse');d.update(id=name,model=model)
        value['models'][model]['runtime']['native']='fixture'
        p=root/(name+'.json');p.write_text(json.dumps(d));profiles.append(p)
    roster.write_text(json.dumps(value))
    original=native_agent.run_session
    calls=[];failures={};verdicts={}
    def review(config,deployment,custody,instruction,**kw):
        from relay.blocker_rubric import BLOCKER_RUBRIC
        assert BLOCKER_RUBRIC in instruction
        calls.append(deployment['id'])
        sources=[];steps=[]
        for repository,paths in [('review',['PACKET.md','EXECUTION_DESCRIPTOR.json','CANDIDATE_CHANGE.json','EXECUTOR_OBSERVATION.json','diff.patch']),('candidate',['src/value.txt'])]:
            for path in paths:
                sources.append({'repository':repository,'path':path,'sha256':hashlib.sha256((Path(config['repositories'][repository])/path).read_bytes()).hexdigest()})
                steps.append(('read',{'repository':repository,'path':path}))
        verdict=verdicts.get(deployment['id'],'ACCEPT')
        findings = [] if verdict=='ACCEPT' else [{'claim':'Criterion not satisfied.', 'sources':[sources[-1]]}]
        steps.append(('tare_deliver',{'summary':'Reviewed exact candidate.','sources':sources,'remaining':[] if verdict=='ACCEPT' else ['Criterion not satisfied.'],'verdict':verdict,'findings':findings}))
        client=Client('bedrock-converse',steps)
        if deployment['id'] in failures:
            post=client.post
            def fail(path,body,control):
                from tare_tools_agent_runtime import ModelFailure
                if path.endswith('/converse'):raise ModelFailure(failures[deployment['id']])
                return post(path,body,control)
            client.post=fail
        return original(config,deployment,custody,instruction,**kw,client=client,local_counter=client)
    monkeypatch.setattr(native_agent,'run_session',review)
    return profiles,calls,failures,verdicts


def test_independent_audit_is_counted_before_landing(host,reviewers,capsys):
    profiles,calls,_,_=reviewers
    assert run(host,'--auditor-profile',str(profiles[0]))==0,capsys.readouterr().out
    result=json.loads(capsys.readouterr().out)
    assert result['status']=='SETTLED' and result['audit']['verdict']=='ACCEPT'
    observed=result['audit']['observation']
    assert observed['schema']=='tare.tools/work-audit-observation/1'
    assert observed['verdict']=='ACCEPT' and observed['findings']==[]
    assert len(observed['artifact_sha256'])==64 and observed['sources']
    train=work_run.train_dir_for('work-one')
    verdict=json.loads((train/'ADMISSION_VERDICT.json').read_bytes())
    assert verdict['audit']['independent'] and verdict['audit']['counted'] and verdict['policy']['audit_mode']=='required'
    assert calls==['one']
    from relay.work_console import render
    assert 'Auditor: one' in render('run',result)


@pytest.mark.parametrize('failure',['HTTP_429','INVALID_DELIVERY'])
def test_only_causal_operational_failure_uses_next_explicit_route(host,reviewers,capsys,failure):
    profiles,calls,failures,_=reviewers;failures['one']=failure
    code=run(host,*[v for p in profiles for v in ['--auditor-profile',str(p)]])
    result=json.loads(capsys.readouterr().out)
    assert (code==0)==(failure=='HTTP_429')
    assert calls==(['one','two'] if failure=='HTTP_429' else ['one'])
    assert any(s['stage']=='audit_fallback' and s['reason']==failure for s in result['stages'])==(failure=='HTTP_429')


def test_revise_is_preserved_on_resume_without_fallback_or_regeneration(host,reviewers,capsys):
    import tare_ops
    profiles,calls,_,verdicts=reviewers;verdicts['one']='REVISE'
    assert run(host,*[v for p in profiles for v in ['--auditor-profile',str(p)]])==1
    first=json.loads(capsys.readouterr().out);assert first['status']=='AUDIT_REVISE'
    observed=first['audit']['observation']
    assert observed['verdict']=='REVISE'
    assert observed['findings'][0]['claim']=='Criterion not satisfied.'
    assert observed['findings'][0]['sources'][0]['repository']=='candidate'
    assert tare_ops.main(['run','work-one','--resume'])==1
    result=json.loads(capsys.readouterr().out)
    assert result['status']=='AUDIT_REVISE' and calls==['one']
    assert result['audit']['observation']==observed
    assert first['candidate_commit']==result['candidate_commit']
    assert not (work_run.train_dir_for('work-one')/'AUDIT_REPORT.md').exists()
    assert tare_ops.main(['run','work-one','--resume','--retry-audit'])==1
    assert 'AUDIT_RETRY_REQUIRES_FINISHED_OPERATIONAL_FAILURE' in capsys.readouterr().out
    assert calls==['one']


def test_lost_audit_ack_recovers_without_repeating_review_or_implementation(host,reviewers,monkeypatch,capsys):
    import tare_ops
    from relay import work_audit
    profiles,calls,_,_=reviewers;original=work_audit.run
    with monkeypatch.context() as fault:
        def cut(*args):
            assert original(*args)
            raise SystemExit('lost audit ack')
        fault.setattr(work_audit,'run',cut)
        with pytest.raises(SystemExit):run(host,'--auditor-profile',str(profiles[0]))
    capsys.readouterr();before=len(host[3].generations)
    assert tare_ops.main(['run','work-one','--resume'])==0,capsys.readouterr().out
    result=json.loads(capsys.readouterr().out)
    assert result['status']=='SETTLED' and calls==['one'] and len(host[3].generations)==before


def test_same_bias_and_missing_required_route_stop_before_paid_stages(host,capsys,monkeypatch):
    assert run(host,'--auditor-profile',str(host[2]))==1
    result=json.loads(capsys.readouterr().out)
    assert 'INDEPENDENT_AUDITOR_ROUTE_REQUIRED' in result['error']
    assert not host[3].generations and not host[4]
    monkeypatch.setenv('TARE_LANDING_AUDIT_MODE','required')
    assert run(host)==1
    assert not host[3].generations and not host[4]


def test_console_passes_auditor_only_for_new_work(tmp_path):
    from relay.console_backend import Selection,command
    value=Selection(root=str(tmp_path),mode='work',work='one',auditor='bedrock-opus')
    args=command(value);assert args[args.index('--auditor-deployment')+1]=='bedrock-opus'
    assert '--auditor-deployment' not in command(value,resume=True)


def test_cancel_after_review_resumes_without_new_review(host,reviewers,monkeypatch,capsys):
    import tare_ops
    profiles,calls,_,_=reviewers;original=native_agent.run_session
    with monkeypatch.context() as fault:
        def cancel(*args,**kw):
            result=original(*args,**kw)
            kw['cancellation'].cancel()
            return result
        fault.setattr(native_agent,'run_session',cancel)
        assert run(host,'--auditor-profile',str(profiles[0]))==1
    result=json.loads(capsys.readouterr().out)
    assert result['status']=='CANCELLED'
    assert tare_ops.main(['run','work-one','--resume'])==0,capsys.readouterr().out
    assert calls==['one']


def test_candidate_drift_after_review_cannot_be_admitted(host,reviewers,monkeypatch,capsys):
    profiles,calls,_,_=reviewers;original=native_agent.run_session
    def drift(*args,**kw):
        result=original(*args,**kw)
        (Path(args[0]['repositories']['candidate'])/'src/value.txt').write_text('unexpected')
        return result
    monkeypatch.setattr(native_agent,'run_session',drift)
    assert run(host,'--auditor-profile',str(profiles[0]))==1
    result=json.loads(capsys.readouterr().out)
    assert 'AUDIT_CANDIDATE_WORKTREE_CHANGED' in result['error']
    assert not (work_run.train_dir_for('work-one')/'AUDIT_REPORT.md').exists()


def test_explicit_audit_retry_preserves_failure_and_uses_saved_routes(host,reviewers,capsys):
    import tare_ops
    profiles,calls,failures,_=reviewers;failures['one']='INVALID_DELIVERY'
    assert run(host,'--auditor-profile',str(profiles[0]))==1
    first=json.loads(capsys.readouterr().out)
    old=Path(first['audit']['session'])/'audit-result.json';raw=old.read_bytes()
    failures.clear()
    assert tare_ops.main(['run','work-one','--resume','--retry-audit'])==0,capsys.readouterr().out
    result=json.loads(capsys.readouterr().out)
    assert result['status']=='SETTLED' and calls==['one','one'] and old.read_bytes()==raw
    assert result['audit']['session']!=first['audit']['session']


def test_accepted_audit_alone_lands_nothing_when_admission_refuses(host,reviewers,monkeypatch,capsys):
    profiles,calls,_,_=reviewers
    base=_git('rev-parse','HEAD',cwd=host[1])
    monkeypatch.setattr(relay_mesh,'materialize_admission_evidence',lambda tid:evidence(tid,('cas_ready',)))
    assert run(host,'--auditor-profile',str(profiles[0]))==1
    result=json.loads(capsys.readouterr().out)
    assert result['audit']['verdict']=='ACCEPT' and calls==['one']
    assert result['status']=='ADMISSION_REFUSED' and 'CHECK_FAILED:cas_ready' in result['admission']['reason_codes']
    assert not result['landing'] and _git('rev-parse','HEAD',cwd=host[1])==base
    assert json.loads((work_run.train_dir_for('work-one')/'STATE.json').read_bytes())['status']=='AUDIT_PENDING'
