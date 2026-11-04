"""Review role evidence comes from the bound host custody, never a planner's prose."""
import hashlib
import json
import pytest
from relay import conversation_review as review


@pytest.fixture
def recorded(tmp_path):
    root=tmp_path/'owner'; session=root/'planning'/'request'/'session'
    session.mkdir(parents=True)
    artifact=session/'artifact.json'
    artifact.write_text(json.dumps({'role':'planner','session_id':'planner-session','payload':{}}))
    deployment={'id':'openai-api','native_credential_env':'OPENAI_API_KEY',
        'native_profile':{'model':'gpt-6.1-sol','protocol':'openai-responses',
            'parameters_json':json.dumps({'reasoning':{'effort':'high'},'api_key':'SECRET'})}}
    state={'session_id':'planner-session','config':{'role':'planner'},'deployment':deployment,
        'invocations':[{'status':'PROPOSED','ended_at':1}]}
    (session/'native-agent.json').write_text(json.dumps(state))
    plan={'artifact':str(artifact),'sha256':hashlib.sha256(artifact.read_bytes()).hexdigest()}
    return root,plan,state,session


def test_bound_completed_planner_is_reported_with_pro_and_reasoning(recorded):
    root,plan,state,session=recorded
    deployment,proof=review.planner_source(root,plan)
    facts=review.execution_selection(deployment,deployment,planner=deployment)
    assert proof['status']=='RECORDED' and proof['artifact_sha256']==plan['sha256']
    assert facts['planner']['requested_reasoning']=='high'
    assert facts['planner']['pro_model'] is False
    assert 'SECRET' not in json.dumps(facts) and 'OPENAI_API_KEY' not in json.dumps(facts)
    assert facts['provider_internal_reasoning_attested'] is False


@pytest.mark.parametrize('fault',['outside','artifact_changed','wrong_role','wrong_session','unfinished','missing'])
def test_unbound_or_incomplete_planner_stays_unknown(recorded,tmp_path,fault):
    root,plan,state,session=recorded
    if fault=='outside':root=tmp_path/'another-owner'
    elif fault=='artifact_changed':(session/'artifact.json').write_text('{}')
    elif fault=='wrong_role':state['config']['role']='auditor'
    elif fault=='wrong_session':state['session_id']='unrelated'
    elif fault=='unfinished':state['invocations'][0].pop('ended_at')
    elif fault=='missing':plan={}
    (session/'native-agent.json').write_text(json.dumps(state))
    deployment,proof=review.planner_source(root,plan)
    assert deployment is None and proof['status']=='UNKNOWN'


def test_missing_planner_is_explicit_and_pro_is_not_inferred_from_effort(recorded):
    _,_,state,_=recorded
    dep=state['deployment']
    dep['native_profile']['parameters_json']=json.dumps({'reasoning':{'effort':'high','mode':'pro'}})
    facts=review.execution_selection(dep,dep)
    assert facts['planner'] is None and facts['implementer']['pro_model'] is True
