"""Real fixture verifier, ordinary Kernel capability and durable one-effect receipt."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
import time

import pytest
from compute_plane.action_assessment import AssessmentObservation
from compute_plane.action_control import ActionAuthorization, ActionSnapshot, ExactActionGuard
from compute_plane.native_capabilities import NativeCapabilities
from tare_tools_agent_runtime import BindingRef, CancellationToken, ExecutionControl, EffectRequest, ToolCall
from relay.action_control import ActionPolicy, ActionReviewBroker
from relay.action_mediation import CommandActionMediator


@pytest.mark.parametrize('deny', [False,True])
def test_existing_verifier_path_checks_approval_and_claim_before_real_effect(tmp_path,deny):
    project=tmp_path/'project';project.mkdir();scratch=tmp_path/'scratch';scratch.mkdir()
    binding=BindingRef('w','binding','operator',3,'opaque')
    control=ExecutionControl(binding,CancellationToken(),time.monotonic()+20)
    context='Write one owned fixture sentinel; no network.'
    tools=NativeCapabilities(session_id='s',binding=binding,repositories={'r':str(project)},writable={},
        verifiers={'check':{'repository':'r','argv':[sys.executable,'-c',
            "from pathlib import Path; Path('sentinel').write_text('once')"]}},
        scratch_root=scratch,specgraph_edit_context=False)
    calls=[]
    class Port:
        def assess(self,request,live):
            calls.append(request.stage)
            verdict=('DENY_RECOMMENDED' if deny else 'APPROVE_RECOMMENDED') if request.stage=='reviewer' else 'REVIEW_REQUIRED'
            return AssessmentObservation(request.stage,request.backend,'fixture',request.request_sha256,
                request.action.action_sha256,request.action.context_sha256,request.action.policy_sha256,
                'OBSERVED',verdict,('FIXTURE',),0.0,'fixture')
    broker=ActionReviewBroker(ActionPolicy(),Port(),{'tare':Port()},lambda *a:True,lambda *a:True)
    def snapshot(facts,live):
        assert facts.session_id=='s' and facts.work_id=='w' and facts.binding_id=='binding' and facts.fence_seq==3
        return ActionSnapshot(facts.work_id,facts.session_id,facts.effect_id,facts.binding_id,facts.fence_seq,
            facts.operation,facts.repository,facts.cwd,facts.environment_sha256,
            hashlib.sha256(context.encode()).hexdigest(),'a'*64,'b'*64,facts.grant_sha256,
            'mediation-only',control.deadline_monotonic,argv=facts.argv,payload_json=facts.payload_json)
    permits=[]
    def authorize(action,decision,live):
        permit=ActionAuthorization('operator','nonce',action.action_sha256,action.session_id,
            action.effect_id,action.binding_id,action.fence_seq,action.deadline)
        permits.append(permit);return permit
    guard=ExactActionGuard(lambda a,p:p in permits,tools.claim_action)
    mediator=CommandActionMediator(broker,snapshot,lambda *a:context,authorize,guard,lambda:control)
    tools.bind_command_mediation(mediator)
    request=EffectRequest('s','single-effect',ToolCall('call','verify','{"name":"check"}'))
    result=tools.execute(request,control)
    assert result.status==('refused' if deny else 'succeeded')
    assert (project/'sentinel').exists() is (not deny)
    assert calls==['classifier','reviewer']
    if deny: assert not permits
    else:
        again=tools.execute(request,control)
        assert again.status=='refused' and (project/'sentinel').read_text()=='once'
        receipt=json.loads(next((scratch/'effect-receipts').glob('*.json')).read_text())
        assert receipt['result']['status']=='succeeded' and receipt['action_control']['fence_seq']==3
