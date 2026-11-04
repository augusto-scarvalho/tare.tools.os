import pytest
from relay import challenger as c
from test_challenger import project,linked_job,boundary_worker,tmp_path


@pytest.mark.parametrize('status,passed',[('BLOCKED',True),('PROPOSED',False)])
def test_failed_implementation_or_checks_never_dispatch_paid_reviews(
        linked_job,boundary_worker,monkeypatch,status,passed):
    custody,state,job,other=linked_job;worker=boundary_worker
    c.finish(custody,state,{'status':'PROPOSED'})
    monkeypatch.setattr(worker,'execute',lambda *a:{'status':status})
    monkeypatch.setattr(worker,'checks',lambda *a:passed)
    monkeypatch.setattr(worker,'audit',lambda *a:pytest.fail('review after failed arm'))
    worker.run(job)
    row=c.read_run(job/'run.json')
    assert row['status']=='FAILED' and 'without spending on paired reviews' in row['reason']
    assert row['reference_verdict']==row['challenger_verdict']=='unknown'
    assert (job/'challenger.patch').exists()


def test_incomplete_first_review_does_not_dispatch_the_second(linked_job,boundary_worker,monkeypatch):
    custody,state,job,other=linked_job;worker=boundary_worker;calls=[]
    c.finish(custody,state,{'status':'PROPOSED'})
    monkeypatch.setattr(worker,'execute',lambda *a:{'status':'PROPOSED'})
    monkeypatch.setattr(worker,'audit',lambda row,path,variant,token:
        calls.append(variant) or ('unknown','transport did not complete'))
    worker.run(job)
    row=c.read_run(job/'run.json')
    assert row['status']=='FAILED' and calls==['challenger']
    assert row['challenger_review']=='transport did not complete' and row['reference_verdict']=='unknown'


@pytest.mark.parametrize('code,visible',[('CLI_UNEXPECTED_TOOL',True),('CLI_ACTION_LIMIT_EXCEEDED',True),('PRIVATE-ERROR',False)])
def test_incomplete_review_exposes_only_public_transport_codes(linked_job,boundary_worker,monkeypatch,code,visible):
    custody,state,job,other=linked_job;worker=boundary_worker
    monkeypatch.setattr('relay.work_audit.prepare',lambda *a:{'status':'READY'})
    monkeypatch.setattr(worker,'execute',lambda *a:{'status':'BLOCKED','failure_code':code,'output':'PRIVATE-BODY'})
    row=c.read_run(job/'run.json')
    verdict,reason=worker.audit(row,job,'challenger',object())
    assert verdict=='unknown' and (code in reason) is visible
    assert 'PRIVATE' not in reason and 'execution receipt' in reason
