"""The planner's descriptive labels cannot stand in for host executable check identities."""
import json
from pathlib import Path
import pytest
from compute_plane.native_journal import save_json
from compute_plane.native_models import encode
from tare_tools_agent_runtime import EffectResult
from test_conversation_tasks import host, intake, planned, effect  # noqa: F401


@pytest.mark.parametrize('configured',[True,False])
def test_planner_handoff_exposes_host_check_action_without_changing_cached_tools(host,monkeypatch,configured):
    tools,control,*_=host
    if configured:
        tools.state['config']['delegated_verifiers']={'project-check':{'repository':'repo','argv':['python','-m','pytest','tests/test_graph.py']}}
    intake(host)
    task=planned(host)
    artifact=Path(task['plan']['artifact'])
    raw=json.loads(artifact.read_text())
    raw['payload']['steps']=['Run the descriptive challenger-focused validation.']
    raw['payload']['tasks'][0]['verifiers']=['project-check'] if configured else []
    save_json(artifact,raw)
    original=artifact.read_bytes()
    specs=tools.tool_specs_json
    seen=[]
    def dispatch(request,control):
        seen.append(request.tool_call.name)
        return EffectResult(request.effect_id,'succeeded',encode({'status':'PLANNED','artifact':str(artifact),'plan':raw['payload']}),True)
    monkeypatch.setattr(tools.tools,'execute',dispatch)
    result=tools.execute(effect('delegate_plan',{'instruction':'Plan the delivery.'}),control)
    assert result.status=='succeeded',result.output_json
    output=json.loads(result.output_json)
    handoff=output['execution_checks']
    assert handoff['names']==(['project-check'] if configured else [])
    if configured:
        assert handoff['action']=={'tool':'check','arguments':{'names':['project-check']}}
    else:
        assert 'action' not in handoff
    assert seen==['delegate_plan']
    continued=tools.execute(effect('demand',{'intent':'deliver','relation':'continue'},key='continue'),control)
    assert continued.status=='succeeded',continued.output_json
    assert json.loads(continued.output_json)['task']['plan']['unit']['verifiers']==handoff['names']
    assert seen==['delegate_plan']
    assert artifact.read_bytes()==original
    assert tools.tool_specs_json==specs
