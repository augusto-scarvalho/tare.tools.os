"""A proven local admission failure can be requalified; completed verdicts cannot."""
from copy import deepcopy
import hashlib
import pytest
from compute_plane.native_journal import NativeJournal, save_json
from relay import conversation_review as review
from relay import conversation_tasks as tasks
from test_conversation_review_recovery import reviewed  # noqa: F401
from test_conversation_tasks import host  # noqa: F401
from test_native_agent import setup  # noqa: F401


@pytest.mark.parametrize('case', ['fits','hard_limit','dispatched','no_room','unknown_count'])
def test_identifies_only_an_undispatched_last_call_below_the_hard_ceiling(tmp_path,case):
    session=tmp_path/'session';session.mkdir()
    profile={'protocol':'vendor-cli','model':'fixture','endpoint':'cli://agy','context_window':1000,'output_tokens':100}
    state={'session_id':'audit','deployment':{'native_profile':profile},'budget':{'max_model_calls':8},
        'config':{'context_policy':{'margin':20,'trigger':.8,'target':.5,'max_compactions':1}},
        'model_context_policy':{'allow_estimated_counts':True}}
    save_json(session/'native-agent.json',state)
    journal=NativeJournal(session/'journal.jsonl')
    # Count summaries too: they are real model work, not a free budget slot.
    for i in range(7):journal.append({'kind':'context_summary_requested' if i==0 else 'model_requested','call_id':f'audit/model/{i}'})
    count={'status':'estimated','input_tokens':None,'estimated_input_tokens':
        1000 if case=='hard_limit' else None if case=='unknown_count' else 850,'request_purpose':'model'}
    journal.append({'kind':'context_counted','call_id':'audit/model/8','count':count})
    if case in {'dispatched','no_room'}:journal.append({'kind':'model_requested','call_id':'audit/model/8'})
    before=(session/'journal.jsonl').read_bytes()
    assert review.final_call_compaction_blocked(tmp_path) is (case=='fits')
    original={'verdict':'INCONCLUSIVE','reason':'MODEL_CALL_BUDGET'}
    row={'deployment':state['deployment'],'route':'fixture','calls_reserved':8,'calls_used':7}
    result=review.context_failure(tmp_path,row,deepcopy(original))
    assert (result.get('failure_code')=='FINAL_CALL_COMPACTION') is (case=='fits')
    assert (session/'journal.jsonl').read_bytes()==before
    accepted={'verdict':'ACCEPT','reason':'MODEL_CALL_BUDGET'}
    assert review.context_failure(tmp_path,row,accepted)==accepted


@pytest.mark.parametrize('changed',[False,True])
def test_completed_context_recovery_can_continue_its_unread_coverage_without_replaying_the_old_failure(tmp_path,monkeypatch,changed):
    job=tmp_path/'review'
    job.mkdir()
    contract={'policy':{'trigger':.8},'final_fitting_call_admission':True}
    row={'status':'FINISHED','request_sha256':'a'*64,'route':'same','deployment':{'native_profile':{}},
        'context_contract':{'final_fitting_call_admission':False},
        'result':{'verdict':'INCONCLUSIVE','failure_code':'DEADLINE'}}
    save_json(job/'receipt.json',row)
    monkeypatch.setattr(review,'context_contract',lambda _:contract)
    target=job.with_name(job.name+'-context-'+hashlib.sha256(review.encode(contract).encode()).hexdigest()[:12])
    target.mkdir()
    recovered={**row,'context_contract':contract,'result':{'verdict':'INCONCLUSIVE','summary':'Partial observations.',
        'remaining':['Read the three contract files.'],'findings':[]}}
    if changed:recovered['route']='different'
    save_json(target/'receipt.json',recovered)
    before=(job/'receipt.json').read_bytes()
    assert review.recovered_context_job(job)==(job if changed else target)
    assert (job/'receipt.json').read_bytes()==before


def test_partial_review_guidance_does_not_invent_provider_or_budget_failure():
    audit={'verdict':'INCONCLUSIVE','summary':'Inspected the diff.','findings':[],
        'remaining':['Read the three test contracts.'],'actor':{'model':'argon'}}
    before=deepcopy(audit)
    view=tasks.delivery_view({'audit':audit,'candidate':{'sha256':'a'*64},'applied':False})
    assert view['audit_state']=='COVERAGE_PENDING'
    assert view['audit_remaining']==audit['remaining']
    assert 'coverage gap, not an execution failure' in view['next']
    assert 'failing independent auditor' not in view['next'] and audit==before


@pytest.mark.parametrize('unlimited',[False,True])
def test_review_phase_and_request_deadlines_are_independent(reviewed,monkeypatch,unlimited):
    from dataclasses import replace
    from relay import execution_scope
    host,run,*_=reviewed
    scope=replace(execution_scope.current(),unlimited=('time',) if unlimited else ())
    monkeypatch.setattr(execution_scope,'current',lambda:scope)
    monkeypatch.setattr(type(host[1]),'remaining_seconds',lambda self:float('inf'))
    result=run('ACCEPT')
    import json
    from pathlib import Path
    state=json.loads((Path(result['session'])/'native-agent.json').read_text())
    assert state['limits']['max_seconds']==(None if unlimited else review.AUDIT_SECONDS)
    assert state['limits']['request_seconds']==review.AUDIT_REQUEST_SECONDS
    assert state['limits']['max_model_calls']==8
