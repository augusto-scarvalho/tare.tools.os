"""Planner delivery must fit its real call/deadline budget without increasing it."""
import hashlib
import json
import pytest
from relay import native_agent,challenger
from tare_tools_agent_runtime import ExecutionControl
from test_native_agent import Client,deployment,setup  # noqa: F401


@pytest.mark.parametrize('local',[False,True])
@pytest.mark.parametrize('boundary',['calls','time'])
def test_planner_reserves_delivery_within_existing_limits(setup,monkeypatch,local,boundary):
    monkeypatch.setattr(challenger,'local',lambda *args:local)
    remaining=[120]
    monkeypatch.setattr(ExecutionControl,'remaining_seconds',lambda _:remaining[0])
    config,custody,root=setup
    config.update(role='planner',writable={},verifiers={},typed_output=True,
                  capabilities=['read','list','search'])
    source={'repository':'repo','path':'a.py','sha256':hashlib.sha256(b'value = 1\n').hexdigest()}
    plan={'summary':'Bounded plan with an explicit remaining gap.',
          'steps':['Change a.py after authorization; verify the new value.'],
          'criteria':['The requested value is two.'],'sources':[source],
          'remaining':['No implementation or verifier result has been observed.'],'tasks':[]}
    client=Client(steps=[('read',{'repository':'repo','path':'a.py'}),('tare_deliver',plan)],
                  after_model=lambda:remaining.__setitem__(0,30 if boundary=='time' else 120))
    limits={'max_model_calls':2 if boundary=='calls' else 8,'max_seconds':120}
    result=native_agent.run_session(config,deployment(role='planner'),custody,'Plan only.',
        client=client,local_counter=client,delegated_leaf=True,limits=limits)
    calls=[body for path,body in client.calls if path=='/chat/completions']
    assert calls[-1]['tool_choice']==('auto' if local else
        {'type':'function','function':{'name':'tare_deliver'}})
    assert result['status']=='PROPOSED' and result['model_calls']==2
    assert json.loads((custody/'artifact.json').read_bytes())['payload']==plan
    saved=json.loads((custody/'native-agent.json').read_bytes())
    assert all(saved['limits'][key]==value for key,value in limits.items())
    assert (root/'a.py').read_bytes()==b'value = 1\n'
