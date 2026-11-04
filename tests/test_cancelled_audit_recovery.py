"""Cancelled audit recovery needs a fresh human request and confirmed local cleanup."""
import json
import pytest
from relay import conversation_review,native_model_recovery
from test_conversation_review_recovery import reviewed  # noqa: F401
from test_conversation_tasks import host,user_reply  # noqa: F401


@pytest.mark.parametrize('status,reason',[('CANCELLED','CANCEL_REQUESTED'),
    ('RECONCILIATION_REQUIRED','UNFINISHED_INVOCATION')])
@pytest.mark.parametrize('fault',[None,'cleanup','transport','active','writable','role','missing','accepted','command','terminal','reason'])
def test_cancelled_review_requires_completed_readonly_custody_and_cleanup(tmp_path,monkeypatch,fault,status,reason):
    job=tmp_path/'audit';session=job/'session';session.mkdir(parents=True)
    state={'config':{'role':'auditor','writable':{},'capabilities':['read','list','search']},'session_id':'audit',
        'invocations':[{'status':status,'ended_at':100}],
        'budget':{'max_model_calls':8},'deadline_utc':200}
    proof={'local_cleanup_verified':fault!='cleanup','transport_eligible':fault!='transport'}
    if fault=='active':state['invocations'][0]['ended_at']=None
    elif fault=='writable':state['config']['writable']={'candidate':['.']}
    elif fault=='role':state['config']['role']='implementer'
    elif fault=='command':state['config']['capabilities'].append('command')
    elif fault=='terminal':state['config']['terminal']={'connection':'unconfirmed'}
    path=session/'native-agent.json';path.write_text(json.dumps(state))
    if fault=='missing':path.unlink()
    monkeypatch.setattr(native_model_recovery,'inspect',lambda *args,**kwargs:proof)
    row={'status':'FINISHED','calls_used':5,'amount_committed':'unknown'}
    result={'verdict':'ACCEPT' if fault=='accepted' else 'INCONCLUSIVE',
        'reason':'CANCEL_REQUESTED' if fault=='reason' and reason!='CANCEL_REQUESTED' else
            'UNFINISHED_INVOCATION' if fault=='reason' else reason}
    before=json.dumps(row)
    assert conversation_review.cancelled_review_recoverable(job,row,result)==(fault is None)
    assert json.dumps(row)==before


@pytest.mark.parametrize('clean',[True,False])
def test_one_fresh_human_can_retry_only_cancelled_audit_without_replaying_a_verdict(reviewed,monkeypatch,clean):
    host,run,*_=reviewed;tools=host[0]
    run.task['attempt_input']=tools.input_id()
    run('INCONCLUSIVE',with_findings=False)
    path=next((tools.root/'reviews').glob('*/receipt.json'))
    row=json.loads(path.read_text())
    row['result']={'verdict':'INCONCLUSIVE','reason':'CANCEL_REQUESTED','session':str(path.parent/'session')}
    path.write_text(json.dumps(row));before=path.read_bytes()
    monkeypatch.setattr(conversation_review,'cancelled_review_recoverable',lambda *args:clean)
    assert run('ACCEPT')['reason']=='CANCEL_REQUESTED'
    tools.invocation_id='automatic-resume'
    assert run('ACCEPT')['reason']=='CANCEL_REQUESTED'
    user_reply(host,'Resume only the cancelled independent audit.')
    tools.invocation_id='fresh-human-review'
    result=run('ACCEPT',expected_calls=2 if clean else 1)
    assert result.get('verdict')==('ACCEPT' if clean else 'INCONCLUSIVE')
    assert path.read_bytes()==before  # Prior usage and cancellation remain history.
    user_reply(host,'Again.')
    tools.invocation_id='another-human'
    assert run('REVISE',expected_calls=2 if clean else 1)==result
