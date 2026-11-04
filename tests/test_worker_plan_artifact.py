from copy import deepcopy
import hashlib
import json
import pytest
from relay import native_agent, work_units


@pytest.fixture
def large_plan(tmp_path):
    payload={'summary':'Complete large plan.', 'steps':['Detailed source-bound action. '*900],
        'criteria':['Preserve all obligations.'], 'sources':[], 'remaining':[], 'tasks':[]}
    artifact={'schema':'tare.tools/native-artifact/1','role':'planner','kind':'plan','authority':'NONE',
        'session_id':'planner','call_id':'planner/1','profile_digest':'a'*64,'payload':payload}
    path=tmp_path/'artifact.json'
    path.write_text(json.dumps(artifact),encoding='utf8')
    unit={'title':'Implement the complete plan','target_paths':['a.py'],
        'criteria':['Preserve all obligations.'],'planner_advice':json.dumps(payload),
        'planner_input':{'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}}
    return unit,artifact,path


def test_large_plan_reaches_worker_complete_through_existing_artifact_channel(large_plan):
    unit,artifact,path=large_plan;before=deepcopy(unit);raw=path.read_bytes()
    text,inputs=work_units.worker_brief(unit,{},None)
    assert len(text.encode())<=native_agent.MAX_INITIAL_INSTRUCTION_BYTES
    assert inputs[0]['artifact']==artifact and inputs[0]['authority']=='NONE'
    assert inputs[0]['sha256']==hashlib.sha256(raw).hexdigest()
    assert all(c in text for c in unit['criteria'])
    assert unit==before and path.read_bytes()==raw


def test_changed_plan_is_rejected_instead_of_silently_rebound(large_plan):
    unit,_,path=large_plan
    value=json.loads(path.read_text());value['payload']['summary']='Changed after planning.'
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError,match='artifact changed'):
        work_units.worker_brief(unit,{},None)


def test_oversized_criteria_remain_refused_instead_of_truncated(large_plan):
    unit,_,_=large_plan
    unit['criteria']=['Required obligation. '*1200]
    with pytest.raises(ValueError,match='bounded instruction limit'):
        work_units.worker_brief(unit,{},None)


def test_small_legacy_brief_preserves_exact_prefix_and_no_extra_artifact(large_plan):
    unit,_,_=large_plan;unit['planner_advice']='Short complete plan.'
    text,inputs=work_units.worker_brief(unit,{},None)
    assert text==work_units.brief(unit,{},None) and inputs==[]
