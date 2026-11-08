"""Completed partial findings can be corrected; reference grants do not become write authority."""
import hashlib
import json
from pathlib import Path

import pytest

from relay import conversation_review, conversation_tasks as tasks, native_agent, turn_engine, work_units
from relay.folder_access import grant
from test_conversation_tasks import host, intake, planned, effect, user_reply  # noqa: F401
from test_native_agent import Client, deployment, setup  # noqa: F401


def test_review_receives_earlier_operator_authorization_and_preserves_spend_uncertainty(reviewed):
    from compute_plane.native_journal import NativeJournal,save_json
    host,run,*_=reviewed;tools=host[0]
    user_reply(host,'Autorizo Claude API com teto de USD 7 para esta entrega.')
    user_reply(host,'Repita somente os checks e a auditoria.')
    tools.state['operator_call_budget']={'source':'operator_cli','total_calls':120,'original_limit':30}
    tools.state['operator_active_time_budget']={'source':'operator_cli','total_seconds':3600}
    child=tools.root/'units'/'metered'/'session-1';child.mkdir(parents=True)
    save_json(child/'native-agent.json',{'deployment':{'native_spend_policy':{'max_amount':'7','currency':'USD'}},
        'invocations':[{'id':'worker','ended_at':1}]})
    save_json(child/'outcome.json',{'invocation_id':'worker','model_calls':2,
        'spend':{'currency':'USD','known_amount':'.25','committed_amount':'.55','uncertain_calls':1}})
    NativeJournal(child/'journal.jsonl').append({'kind':'terminal','status':'proposed','output':'not copied'})
    original_state=(child/'native-agent.json').read_bytes()
    run('INCONCLUSIVE')
    assert (child/'native-agent.json').read_bytes()==original_state
    request=next((tools.root/'reviews').rglob('request.json'))
    data=json.loads(request.read_text())
    assert 'teto de USD 7' not in data['latest_user_instruction']
    assert any('teto de USD 7' in r['text'] and r['record_sha256'] for r in data['recent_user_instructions'])
    controls=data['operator_execution_controls']
    assert controls['call_override']['total_calls']==120 and controls['active_time_override']['total_seconds']==3600
    assert controls['metered_worker_history'][0]['spend']['uncertain_calls']==1
    accounting=controls['cumulative_accounting']
    assert any(r['session']=='units/metered/session-1' and r['status']=='UNKNOWN' for r in accounting['journals'])
    assert accounting['unknown_journals']>=1
    assert 'not copied' not in json.dumps(data)


def test_cumulative_review_accounting_uses_owner_journals_without_rewriting_history(setup,monkeypatch):
    from types import SimpleNamespace
    config,custody,_=setup
    client=Client(steps=[('tare_deliver',{'summary':'Private delivery text must stay out of accounting.'})])
    native_agent.run_session(config,deployment(),custody,'Private user instruction.',client=client,local_counter=client)
    state=json.loads((custody/'native-agent.json').read_text())
    state['operator_active_time_budget']={'total_seconds':1000}
    state['deadline_utc']=500
    monkeypatch.setattr(conversation_review.time,'time',lambda:100)
    before={p.name:p.read_bytes() for p in custody.iterdir() if p.is_file()}
    report=conversation_review.operational_accounting(SimpleNamespace(root=custody,state=state))
    assert report['shared_calls']['coordinator_requested']==1
    assert report['active_time']['consumed_seconds']==600
    assert report['usage']['generation_intents']==1
    assert report['usage']['unpriced_or_partial_calls']==1
    assert report['unknown_journals']==0
    assert len(report['journals'][0]['last_record_sha256'])==64
    assert 'Private' not in json.dumps(report)
    assert before=={p.name:p.read_bytes() for p in custody.iterdir() if p.is_file()}


def test_large_completed_review_receipt_replays_without_another_auditor(reviewed):
    host,run,*_=reviewed
    tools=host[0]
    audit=run('INCONCLUSIVE')
    receipt=next((tools.root/'reviews').glob('*/receipt.json'))
    row=json.loads(receipt.read_text());row['bounded-host-diagnostic']='x'*70000
    from compute_plane.native_journal import save_json
    save_json(receipt,row,max_bytes=conversation_review.MAX_RECEIPT_BYTES)
    assert conversation_review.records(tools.root)[0]['result']['summary']==audit['summary']
    found=conversation_review.recorded_failure(tools.root,audit)
    assert found is not None and found[1]['result']['verdict']=='INCONCLUSIVE'


def test_explicit_candidate_recheck_skips_child_budget_but_keeps_host_pipeline(host,monkeypatch):
    tools,execution,*_=host
    intake(host);planned(host)
    value=tasks.load(tools.root);task=tasks.current(value)
    previous={'verdict':'NOT_VERIFIED','candidate':{'patch':'retained'}}
    task['delivery']=previous;tasks.save(tools.root,value)
    monkeypatch.setattr(work_units,'child_limits',lambda *a,**kw:pytest.fail('No implementer is authorized'))
    seen=[]
    monkeypatch.setattr(work_units,'run_unit',lambda *a,**kw:seen.append(kw) or previous)
    result=tools.execute(effect('implement',{'recheck_candidate':True},'recheck'),execution)
    assert result.status=='succeeded' and len(seen)==1
    assert seen[0]['candidate']=={'patch':'retained','review_only':True}
    assert seen[0]['review'] is not None


@pytest.mark.parametrize('args',[
    {'recheck_candidate':True}, {'recheck_candidate':'true'},
    {'recheck_candidate':True,'route':'other'}, {'recheck_candidate':True,'effort':'high'}])
def test_recheck_refuses_missing_candidate_or_producer_change(host,monkeypatch,args):
    tools,execution,*_=host
    intake(host);planned(host)
    monkeypatch.setattr(work_units,'run_unit',lambda *a,**kw:pytest.fail('No worker or review'))
    result=tools.execute(effect('implement',args,'recheck-invalid'),execution)
    assert result.status=='refused'


@pytest.fixture
def reviewed(host,monkeypatch,tmp_path):
    tools,execution,root,*_=host
    intake(host);task=planned(host)
    from relay.challenger import git
    git(root,'init');git(root,'add','.')
    git(root,'-c','user.name=Fixture','-c','user.email=fixture@localhost','-c','commit.gpgsign=false','commit','-m','Input')
    changed=b'value = 2\n';(root/'a.py').write_bytes(changed)
    references=tmp_path/'references';references.mkdir()
    facts=references/'facts.txt';facts.write_bytes(b'Observed criterion.\n')
    store=tmp_path/'grants.json';grant(store,str(references))
    grant(store,str(tmp_path))  # real dogfood also grants an archive containing audit custody
    tools.state['config']['folder_access']={'grants':str(store)}
    candidate={'repository':'candidate','path':'a.py','sha256':hashlib.sha256(changed).hexdigest()}
    ci=root/'.github/workflows/ci.yml';ci.parent.mkdir(parents=True)
    ci.write_text('name: CI\njobs: {}\n',encoding='utf8')
    reference={'repository':'reference-1','path':'facts.txt','sha256':hashlib.sha256(facts.read_bytes()).hexdigest()}
    monkeypatch.setattr(turn_engine,'pick_auditors',lambda *a,**kw:[('independent',deployment())])
    actual=native_agent.run_session
    calls=[]
    def run(verdict,*,after=None,with_findings=True,expected_calls=1,continuation=False,observed_checks=None,changed_files=None,
            inspect=None,runtime_citations=False,candidate_tree=None):
        if changed_files is not None:
            candidate['sha256']=hashlib.sha256(changed_files['a.py']).hexdigest()
        payload={'verdict':verdict,'summary':'Observed source and evidence.',
            'findings':[{'claim':'This criterion needs a correction.','sources':[candidate]}] if verdict in {'INCONCLUSIVE','REVISE'} and with_findings else [],
            'remaining':['Other obligations remain unreviewed.'] if verdict=='INCONCLUSIVE' else [],
            'sources':[candidate,reference]}
        def child(config,dep,session,instruction,**kw):
            calls.append(session)
            request_text=(Path(config['repositories']['review'])/'request.json').read_text(encoding='utf8')
            request=json.loads(request_text)
            if continuation:
                assert request['coverage_continuation']['previous']['remaining']==['Other obligations remain unreviewed.']
                assert request['coverage_continuation']['previous']['findings']==[]
            assert len(request_text.splitlines())>10  # No one-line packet inaccessible to bounded line reads.
            assert request['host_execution_selection']['basis']=='HOST_SELECTED_CONFIGURATION'
            assert request['host_execution_selection']['implementer']['model']==deployment()['native_profile']['model']
            assert request['host_execution_selection']['auditor']['model']==dep['native_profile']['model']
            assert any(r['path']=='.github/workflows/ci.yml' and r['sha256']==hashlib.sha256(ci.read_bytes()).hexdigest()
                       for r in request['source_references_to_read'])
            assert 'pyproject alone is not exhaustive' in instruction and 'separate integration gate' in instruction
            assert 'missing coverage only in remaining' in instruction and 'INCONCLUSIVE with findings=[]' in instruction
            assert config['repositories']['reference-1']==str(references.resolve())
            assert not any(session.resolve().is_relative_to(Path(p).resolve()) for p in config['repositories'].values())
            assert config['writable']=={} and config['instruction_repositories']==['candidate']
            assert 'reference-1' in instruction
            if inspect:inspect(config,request,instruction)
            runtime_reads=[('read',{'repository':r['alias'],'path':'__init__.py'})
                for r in request['satellite_sources']['sources'] if runtime_citations and 'root' in r]
            if runtime_citations:
                payload['sources'].extend({'repository':r['alias'],'path':'__init__.py',
                    'sha256':hashlib.sha256((Path(r['root'])/'__init__.py').read_bytes()).hexdigest()}
                    for r in request['satellite_sources']['sources'] if 'root' in r)
            client=Client(steps=[('read',{'repository':'candidate','path':'a.py'}),
                ('read',{'repository':'reference-1','path':'facts.txt'}),*runtime_reads,('tare_deliver',payload)])
            result=actual(config,dep,session,instruction,client=client,local_counter=client,**kw)
            if after:after(facts,store)
            return result
        monkeypatch.setattr(native_agent,'run_session',child)
        checks={'after':'passed'}
        if observed_checks is not None:checks['observed_named_checks']=observed_checks
        args=(tools,task,candidate_tree or root,deployment(),task['plan']['unit'],{'a.py':changed} if changed_files is None else changed_files,checks,execution)
        value=conversation_review.review(*args)
        assert conversation_review.review(*args)==value and len(calls)==expected_calls  # no verdict resampling
        return value
    run.task=task
    return host,run,references


@pytest.mark.parametrize('files,lines,expected',[
    (0,0,8),(1,20,8),(2,0,9),(0,399,8),(0,400,9),(17,1500,19),(100,10000,24)])
def test_audit_call_budget(files,lines,expected):
    assert conversation_review.audit_call_budget(files,lines)==expected


@pytest.mark.parametrize('large,maximum,unlimited,expected',[
    (False,None,False,8),(True,None,False,19),(True,10,False,3),
    (True,None,True,None),(True,10,True,3)])
def test_review_reserves_size_based_calls_with_parent_ceiling(reviewed,monkeypatch,large,maximum,unlimited,expected):
    from relay import delegation, delegation_tools, execution_resources, review_diff
    host,run,*_=reviewed
    tools,execution,root,*_=host
    changed={'a.py':b'value = 2\n'}
    if large:
        changed['a.py']=b'value = 2\n'*1500
        changed.update({f'extra_{i}.py':b'value = 2\n' for i in range(16)})
        for path,data in changed.items():
            (root/path).write_bytes(data)
    prepared=review_diff.prepare(root,changed)
    assert not prepared[-1]
    assert len(changed)==(17 if large else 1)
    assert conversation_review.audit_call_budget(len(changed),len(prepared[0].splitlines()))==(19 if large else 8)
    tools.state['limits']['max_model_calls']=maximum
    monkeypatch.setattr(delegation_tools,'count_calls',lambda *a:3)
    monkeypatch.setattr(delegation,'child_calls',lambda *a:2)
    selected=execution_resources.selected
    monkeypatch.setattr(execution_resources,'selected',lambda name,*a,**kw:
        unlimited if name=='calls' else selected(name,*a,**kw))
    assert run('ACCEPT',changed_files=changed)['verdict']=='ACCEPT'
    receipt=json.loads(next((tools.root/'reviews').glob('*/receipt.json')).read_text())
    state=json.loads(next((tools.root/'reviews').glob('*/session/native-agent.json')).read_text())
    assert receipt['calls_reserved']==expected
    assert state['limits']['max_model_calls']==expected


@pytest.mark.parametrize('remaining,phase,request_limit',[(80,75,75),(400,395,395),(1000,995,600),(2000,1200,600)])
def test_review_request_uses_bounded_phase_budget_not_old_cli_default(reviewed,monkeypatch,remaining,phase,request_limit):
    host,run,*_=reviewed;tools,execution,*_=host
    chosen=deployment()
    chosen['native_execution_limits']={'request_seconds':120}
    monkeypatch.setattr(turn_engine,'pick_auditors',lambda *a,**kw:[('independent',chosen)])
    monkeypatch.setattr(type(execution),'remaining_seconds',lambda self:remaining)
    assert run('ACCEPT')['verdict']=='ACCEPT'
    state=json.loads(next((tools.root/'reviews').glob('*/session/native-agent.json')).read_text())
    assert state['limits']['request_seconds']==request_limit
    assert state['limits']['max_seconds']==phase
    assert chosen['native_execution_limits']=={'request_seconds':120}


@pytest.mark.parametrize('final',['ACCEPT','INCONCLUSIVE'])
def test_fresh_human_can_continue_one_completed_unreviewed_audit_without_implementation(reviewed,final):
    host,run,*_=reviewed;tools,*_=host
    run.task['attempt_input']=tools.input_id()
    first=run('INCONCLUSIVE',with_findings=False)
    run.task['delivery']={'audit':first}
    assert not conversation_review.coverage_recovery_available(tools.root,run.task)
    assert run('ACCEPT')['verdict']=='INCONCLUSIVE'  # same user input cannot renew review
    tools.invocation_id='resume-without-new-human'
    assert run('ACCEPT')['verdict']=='INCONCLUSIVE'
    user_reply(host,'Continue only the independent audit of its remaining unread evidence.')
    tools.invocation_id='coverage-continuation'
    recovery=tasks.recovery_view(tools.root,run.task)
    assert recovery['coverage_continuation_available'] and 'then implement now' in recovery['next']
    assert 'not another implementer' in recovery['next']
    result=run(final,with_findings=False,expected_calls=2,continuation=True)
    assert result['verdict']==final
    run.task['delivery']={'audit':result}
    assert not conversation_review.coverage_recovery_available(tools.root,run.task)
    user_reply(host,'Continue once more.')
    tools.invocation_id='no-third-review'
    assert run('ACCEPT',expected_calls=2)['verdict']==final
    rows=conversation_review.records(tools.root)
    assert len(rows)==2 and sum(bool(r.get('coverage_continuation_of')) for r in rows)==1
    assert all(r['calls_used']>0 for r in rows) and first['remaining']
    assert not conversation_review.coverage_recovery_available(tools.root,run.task)


@pytest.mark.parametrize('verdict',['ACCEPT','REVISE','INCONCLUSIVE'])
def test_fresh_human_does_not_resample_acceptance_or_adverse_findings(reviewed,verdict):
    host,run,*_=reviewed;tools,*_=host
    run.task['attempt_input']=tools.input_id()
    first=run(verdict)
    user_reply(host,'Review it again.')
    tools.invocation_id='no-resampling'
    assert run('ACCEPT')==first


def test_unknown_coverage_dispatch_is_not_replayed(reviewed):
    host,run,*_=reviewed;tools,*_=host
    run.task['attempt_input']=tools.input_id()
    first=run('INCONCLUSIVE',with_findings=False)
    job=Path(first['session']).parent
    previous=conversation_review.records(tools.root)[0]
    target=job.with_name(job.name+'-coverage');target.mkdir()
    (target/'receipt.json').write_text(json.dumps({'status':'RUNNING','request_sha256':previous['request_sha256'],
        'coverage_continuation_of':previous['request_sha256'],'calls_reserved':8}))
    user_reply(host,'Continue the remaining audit.')
    tools.invocation_id='unknown-dispatch'
    result=run('ACCEPT')
    assert result['verdict']=='INCONCLUSIVE' and 'reconciliation' in result['reason']


def test_coverage_continuation_keeps_the_shared_parent_call_budget(reviewed):
    host,run,*_=reviewed;tools,*_=host
    run.task['attempt_input']=tools.input_id()
    run('INCONCLUSIVE',with_findings=False)
    tools.state['limits']['max_model_calls']=6
    user_reply(host,'Continue only the remaining audit.')
    tools.invocation_id='bounded-coverage'
    result=run('ACCEPT')
    assert result['verdict']=='INCONCLUSIVE' and 'budget' in result['reason'].lower()
    assert len(conversation_review.records(tools.root))==1


@pytest.mark.parametrize('change',['none','bytes','revoked'])
def test_auditor_reads_granted_references_and_rejects_changed_or_revoked_evidence(reviewed,change):
    host,run,references=reviewed
    def after(facts,store):
        if change=='bytes':facts.write_bytes(b'Changed meanwhile.\n')
        if change=='revoked':
            saved=json.loads(store.read_text());saved['folders']=[];store.write_text(json.dumps(saved))
    value=run('ACCEPT',after=after)
    assert value['verdict']==('ACCEPT' if change=='none' else 'INCONCLUSIVE')
    if change=='none':
        assert value['reference_sources'][0]['folder']==str(references.resolve())
        assert conversation_review.reference_sources_current(host[0].state['config'],value['reference_sources'])
        (references/'facts.txt').write_bytes(b'Changed before application.\n')
        assert not conversation_review.reference_sources_current(host[0].state['config'],value['reference_sources'])
    else:assert 'reference changed or its grant was revoked' in value['reason']


def test_completed_partial_findings_allow_one_candidate_correction_without_resampling(reviewed,monkeypatch):
    host,run,*_=reviewed;tools,execution,*_=host
    audit=run('INCONCLUSIVE')
    assert audit['requires_candidate_revision'] and audit['verdict']=='INCONCLUSIVE'
    folder=tools.root/'units'/'previous';folder.mkdir(parents=True)
    receipt=folder/'receipt.json'
    previous={'verdict':'NOT_REVIEWED','audit':audit,'candidate':{'patch':'retained'},'receipt':str(receipt)}
    receipt.write_text(json.dumps({'result':previous}))
    value=tasks.load(tools.root);task=tasks.current(value)
    task.update(delivery=tasks.retained_delivery(previous),delivery_invocation=tools.invocation_id)
    # Legacy retained metadata omitted this flag; the complete bound audit is still authoritative.
    task['delivery']['audit'].pop('requires_candidate_revision',None)
    tasks.save(tools.root,value)
    seen=[]
    monkeypatch.setattr(work_units,'run_unit',lambda *a,**kw:seen.append(kw) or previous)
    first=tools.execute(effect('implement',{},'correct'),execution)
    second=tools.execute(effect('implement',{},'repeat'),execution)
    assert first.status==second.status=='succeeded' and len(seen)==1
    assert seen[0]['candidate']['review_only'] is False and 'Stop and explain' in second.output_json
    assert 'candidate corrections' in tasks.recovery_view(tools.root,tasks.current(tasks.load(tools.root)))['reason']


def test_execution_selection_records_host_choices_without_secrets_or_provider_claims(monkeypatch):
    from types import SimpleNamespace
    from relay import work_audit
    author={'id':'chosen-author','native_credential_env':'OPENAI_API_KEY',
        'native_profile':{'model':'chosen-model','protocol':'openai-responses',
                          'parameters_json':json.dumps({'reasoning':{'effort':'high'},'api_key':'SECRET'})}}
    reviewer={'id':'chosen-reviewer','native_credential_env':'ANTHROPIC_API_KEY',
        'native_profile':{'model':'other-model','protocol':'anthropic-messages',
                          'parameters_json':json.dumps({'output_config':{'effort':'high'}})}}
    monkeypatch.setattr(work_audit,'identity',lambda d,r:SimpleNamespace(
        vendor='openai' if r=='implementer' else 'anthropic',model_id=d['native_profile']['model'],bias_domain=r))
    facts=conversation_review.execution_selection(author,reviewer)
    assert facts['implementer']['requested_reasoning']=='high' and facts['implementer']['vendor']=='openai'
    assert facts['auditor']['requested_reasoning']=='high' and facts['auditor']['vendor']=='anthropic'
    assert facts['provider_internal_reasoning_attested'] is False and 'SECRET' not in json.dumps(facts)
    monkeypatch.setattr(work_audit,'identity',lambda *a:(_ for _ in ()).throw(ValueError('unknown lineage')))
    unknown=conversation_review.execution_selection(author,reviewer)
    assert unknown['implementer']['vendor'] is None and unknown['auditor']['model_id'] is None


@pytest.mark.parametrize('relation,revision',[('correct',True),('continue',False)])
def test_completed_missing_evidence_allows_only_a_fresh_explicit_correction(reviewed,monkeypatch,relation,revision):
    host,run,*_=reviewed;tools,execution,*_=host
    audit=run('INCONCLUSIVE',with_findings=False)
    folder=tools.root/'units'/'previous';folder.mkdir(parents=True)
    receipt=folder/'receipt.json'
    previous={'verdict':'NOT_REVIEWED','audit':audit,'candidate':{'patch':'retained'},'receipt':str(receipt)}
    receipt.write_text(json.dumps({'result':previous}))
    value=tasks.load(tools.root);task=tasks.current(value)
    task.update(delivery=tasks.retained_delivery(previous),delivery_invocation=tools.invocation_id,
                attempt_input=tools.input_id())
    tasks.save(tools.root,value)
    seen=[]
    monkeypatch.setattr(work_units,'run_unit',lambda *a,**kw:seen.append(kw) or previous)
    tools.execute(effect('implement',{},'unchanged-input'),execution)
    assert not seen
    user_reply(host,'New host execution facts and actual CI setup support an evidence correction.')
    tools.invocation_id='fresh-human-turn'
    intake(host,relation=relation)
    tools.execute(effect('implement',{},'evidence-correction'),execution)
    assert len(seen)==1 and seen[0]['candidate']['review_only'] is (not revision)
    tools.execute(effect('implement',{},'no-repeat'),execution)
    assert len(seen)==1


def test_readonly_recheck_does_not_consume_a_fresh_human_correction(reviewed,monkeypatch):
    host,run,*_=reviewed;tools,execution,*_=host
    audit=run('INCONCLUSIVE',with_findings=False)
    folder=tools.root/'units'/'previous';folder.mkdir(parents=True)
    receipt=folder/'receipt.json'
    compact={k:audit[k] for k in ('verdict','session','actor','route') if k in audit}
    previous={'verdict':'NOT_REVIEWED','audit':compact,'candidate':{'patch':'retained'},'receipt':str(receipt)}
    receipt.write_text(json.dumps({'result':previous}))
    original_input=tools.input_id()
    value=tasks.load(tools.root);task=tasks.current(value)
    task.update(delivery=tasks.retained_delivery(previous),delivery_invocation=tools.invocation_id,
                attempt_input=original_input)
    tasks.save(tools.root,value)
    seen=[]
    monkeypatch.setattr(work_units,'run_unit',lambda *a,**kw:seen.append(kw) or previous)
    from relay import work_forms
    actual_plan=work_forms.record_plan;recorded=[]
    def record_plan(root,config,payload):
        recorded.extend(payload['units'])
        return actual_plan(root,config,payload)
    monkeypatch.setattr(work_forms,'record_plan',record_plan)
    user_reply(host,'The missing external evidence is now available; correct the same candidate.')
    tools.invocation_id='fresh-evidence'
    intake(host,relation='continue')  # Coordinator initially chooses the wrong relation.
    tools.execute(effect('implement',{},'read-cached-review'),execution)
    assert len(seen)==1 and seen[0]['candidate']['review_only'] is True
    assert tasks.current(tasks.load(tools.root))['attempt_input']==original_input
    intake(host,relation='correct')  # It then corrects that choice within the same human turn.
    tools.execute(effect('implement',{},'correct-candidate'),execution)
    assert len(seen)==2 and seen[1]['candidate']['review_only'] is False
    feedback=json.loads(recorded[-1]['review_feedback'])
    assert feedback['summary']==audit['summary'] and feedback['remaining']==audit['remaining']
    assert tasks.current(tasks.load(tools.root))['attempt_input']==tools.input_id()
    tools.execute(effect('implement',{},'no-repeat'),execution)
    assert len(seen)==2


@pytest.mark.parametrize('statuses', [['succeeded','failed','failed','succeeded'],
                                    ['failed','succeeded','succeeded','succeeded']])
def test_review_receives_actual_failed_and_passed_checks_without_worker_reasoning(tmp_path,statuses):
    from compute_plane.native_journal import NativeJournal
    journal=NativeJournal(tmp_path/'journal.jsonl')
    journal.append({'kind':'model_requested','call_id':'not-copied','private_reasoning':'not-copied'})
    for index,status in enumerate(statuses):
        request=effect('verify',{'name':'focused'},key='check-'+str(index))
        journal.append({'kind':'effect_requested','effect_id':request.effect_id,'tool_call':{
            'name':'verify','call_id':request.tool_call.call_id,'arguments_json':request.tool_call.arguments_json}})
        journal.append({'kind':'effect_returned','result':{'effect_id':request.effect_id,'status':status,
            'output_json':json.dumps({'output':('passed' if status=='succeeded' else 'assertion failed')+' '+str(index)}),'progressed':True}})
    rows=work_units.verification_evidence(tmp_path)
    assert [r['status'] for r in rows]==statuses
    assert rows[0]['output']==('passed' if statuses[0]=='succeeded' else 'assertion failed')+' 0'
    assert rows[-1]['output']=='passed 3'
    assert all(len(r['result_sha256'])==64 for r in rows) and 'not-copied' not in json.dumps(rows)


def verification_journal(folder,outputs):
    from compute_plane.native_journal import NativeJournal
    journal=NativeJournal(folder/'journal.jsonl')
    for index,output in enumerate(outputs):
        request=effect('verify',{'name':'focused'},key='measured-'+str(index))
        journal.append({'kind':'effect_requested','effect_id':request.effect_id,'tool_call':{
            'name':'verify','call_id':request.tool_call.call_id,'arguments_json':request.tool_call.arguments_json}})
        journal.append({'kind':'effect_returned','result':{'effect_id':request.effect_id,'status':'succeeded',
            'output_json':json.dumps({'output':output,'returncode':0}),'progressed':True}})


def test_audit_packet_retains_intermediate_observed_times_instead_of_only_latest_pass(reviewed,tmp_path):
    host,run,*_=reviewed;tools=host[0]
    timings=['21.46 / 23.547','32.45 / 36.031','25.70 / 29.25']
    verification_journal(tmp_path,timings)
    before=(tmp_path/'journal.jsonl').read_bytes()
    observed=work_units.verification_evidence(tmp_path)
    assert run('ACCEPT',observed_checks=observed)['verdict']=='ACCEPT'
    packet=json.loads(next((tools.root/'reviews').rglob('request.json')).read_text())
    assert [r['output'] for r in packet['host_verification']['observed_named_checks']]==timings
    assert (tmp_path/'journal.jsonl').read_bytes()==before


def test_verification_projection_has_a_visible_ceiling_without_erasing_the_journal(tmp_path):
    verification_journal(tmp_path,[str(i) for i in range(70)])
    before=(tmp_path/'journal.jsonl').read_bytes()
    rows=work_units.verification_evidence(tmp_path)
    assert len(rows)==64 and rows[0]['output']=='0' and rows[-1]['output']=='69'
    assert rows[0]['projection']=={'total_observations':70,'included_observations':64,'omitted_observations':6}
    assert (tmp_path/'journal.jsonl').read_bytes()==before


@pytest.mark.parametrize('code,relation,revise',[('INVALID_DELIVERY','correct',True),
    ('PREPARATION_FAILED','correct',True),('INVALID_DELIVERY','continue',False),('NETWORK_FAILURE','correct',False)])
def test_explicit_later_correction_can_update_evidence_after_a_known_local_refusal(host,monkeypatch,code,relation,revise):
    intake(host);planned(host);tools,execution,*_=host
    folder=tools.root/'units'/'previous';folder.mkdir(parents=True)
    receipt=folder/'receipt.json';audit={'verdict':'INCONCLUSIVE','failure_code':code}
    previous={'verdict':'NOT_REVIEWED','candidate':{'patch':'retained'},'audit':audit,'receipt':str(receipt)}
    receipt.write_text(json.dumps({'result':previous}))
    value=tasks.load(tools.root);task=tasks.current(value)
    task.update(delivery=previous,attempt_input=tools.input_id(),delivery_invocation=tools.invocation_id)
    tasks.save(tools.root,value)
    user_reply(host,'New source evidence requires correcting the candidate.')
    tools.invocation_id='new-user-turn'
    intake(host,relation=relation)
    seen=[]
    monkeypatch.setattr(work_units,'run_unit',lambda *a,**kw:seen.append(kw) or previous)
    result=tools.execute(effect('implement',{},'new-attempt'),execution)
    assert result.status=='succeeded' and seen[0]['candidate']['review_only'] is (not revise)
    tools.execute(effect('implement',{},'no-loop'),execution)
    assert len(seen)==1


@pytest.fixture
def paired_packages(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace
    site = tmp_path/'paired-site'; site.mkdir()
    for module in ('compute_plane', 'tare_tools_agent_runtime'):
        root = site/module; root.mkdir()
        (root/'__init__.py').write_bytes(b'# running runtime\n')
        metadata = site/(module+'-1.0.dist-info'); metadata.mkdir()
        (metadata/'METADATA').write_text('Metadata-Version: 2.1\nName: '+module+'\nVersion: 1.0\n', encoding='utf8')
        (metadata/'RECORD').write_text(module+'/__init__.py,,\n'+metadata.name+'/METADATA,,\n', encoding='utf8')
        monkeypatch.setattr(sys.modules[module], '__file__', str(root/'__init__.py'))
    return site


def test_running_paired_packages_have_narrow_host_identity(paired_packages):
    records = conversation_review.paired_satellite_references()
    assert len(records) == 2
    assert {r['satellite'] for r in records} == {'satellites/tare.tools.kernel', 'satellites/tare.tools.agent-runtime'}
    assert len({r['alias'] for r in records}) == 2
    for row in records:
        assert row['version'] == '1.0'
        assert Path(row['root']).parent == paired_packages
        path = Path(row['root'])/'__init__.py'
        citation = {'folder': row['root'], 'path': '__init__.py', 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'runtime': row}
        assert conversation_review.reference_sources_current({}, [citation])
        path.write_bytes(b'# changed\n')
        assert not conversation_review.reference_sources_current({}, [citation])
        path.write_bytes(b'# running runtime\n')
        Path(row['metadata']).write_text('Metadata-Version: 2.1\nName: '+row['module']+'\nVersion: 2.0\n', encoding='utf8')
        assert not conversation_review.reference_sources_current({}, [citation])


def test_paired_metadata_missing_and_ambiguous(paired_packages):
    import shutil
    metadata = paired_packages/'compute_plane-1.0.dist-info'
    shutil.copytree(metadata, paired_packages/'compute_plane-duplicate.dist-info')
    records = conversation_review.paired_satellite_references()
    assert records[0]['missing'] == 'compute_plane: matching installation metadata missing or ambiguous'
    assert records[1]['version'] == '1.0'
    shutil.rmtree(metadata)
    shutil.rmtree(paired_packages/'compute_plane-duplicate.dist-info')
    assert 'missing' in conversation_review.paired_satellite_references()[0]


def test_runtime_alias_write_authority_is_denied(paired_packages,setup,tmp_path):
    """Automatic runtime references are not writable even with an observed hash."""
    import time
    from compute_plane.native_capabilities import NativeCapabilities
    from tare_tools_agent_runtime import BindingRef, CancellationToken, ExecutionControl, EffectRequest, ToolCall
    row=conversation_review.paired_satellite_references()[0]
    binding=BindingRef('w','b','o',1,'t')
    scratch=tmp_path/'readonly-scratch';scratch.mkdir()
    tools=NativeCapabilities(session_id='s',binding=binding,
        repositories={row['alias']:row['root']},writable={},verifiers={},
        scratch_root=scratch,review_root=tmp_path/'readonly-review',
        capabilities=['read','list','search'],specgraph_edit_context=False)
    assert 'edit' not in tools.names
    path=Path(row['root'])/'__init__.py'
    before=path.read_bytes()
    args={'repository':row['alias'],'path':'__init__.py',
        'sha256':hashlib.sha256(before).hexdigest(),
        'changes':[{'search':'# running runtime','replace':'# unauthorized'}]}
    result=tools.execute(EffectRequest('s','denied',ToolCall('t','edit',json.dumps(args))),
        ExecutionControl(binding,CancellationToken(),time.monotonic()+30))
    assert result.status!='succeeded'
    assert path.read_bytes()==before
