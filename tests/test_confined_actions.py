"""Owner mediation signs exact results without granting settlement authority."""
import hashlib
import json
from pathlib import Path
import time
from types import SimpleNamespace
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from assurance_plane.producer_receipts import ProducerSigner, verify_receipt
from compute_plane.action_control import ActionAuthorization, ActionSnapshot, ExactActionGuard
from compute_plane.native_capabilities import NativeCapabilities
from tare_tools_agent_runtime import BindingRef, CancellationToken, ExecutionControl, EffectRequest, ToolCall
from relay.action_control import ActionDecision
from relay.confined_actions import confined_mediation_factory


def fixture(tmp_path, monkeypatch, *, allowed=True, sink_error=False):
    root=tmp_path/'input';root.mkdir();scratch=tmp_path/'custody';scratch.mkdir()
    binding=BindingRef('work','binding','operator',3,'opaque')
    control=ExecutionControl(binding,CancellationToken(),time.monotonic()+20)
    capabilities=NativeCapabilities(session_id='session',binding=binding,repositories={'repo':str(root)},
        writable={},verifiers={'unit':{'repository':'repo','argv':['fixture.exe','arg']}},
        scratch_root=scratch,specgraph_edit_context=False)
    observed=[]; saved=[]
    class Profile:
        input_root=root;source_sha256='a'*64;runtime_sha256='b'*64;profile_sha256='c'*64
        environment={'TMP':'owned'};seconds=2;run_id='run'
        def validate(self):return True
        def __call__(self,*args,**kwargs):pytest.fail('fixture runner must be injected')
        def collect(self,process):
            return {'stdout.txt':b'checked output','stderr.txt':b'',
                    'process.json':json.dumps(process,sort_keys=True).encode()}
    profile=Profile();signer=ProducerSigner(Ed25519PrivateKey.generate())
    keys={signer.producer_id:signer.public_key_bytes}
    class Broker:
        def assess(self,*args):return ActionDecision(allowed,'fixture')
    def snapshot(facts,live):
        return ActionSnapshot(facts.work_id,facts.session_id,facts.effect_id,facts.binding_id,facts.fence_seq,
            facts.operation,facts.repository,facts.cwd,facts.environment_sha256,'d'*64,profile.source_sha256,
            'e'*64,facts.grant_sha256,'windows-lpac/1:'+profile.profile_sha256,control.deadline_monotonic,
            argv=facts.argv,payload_json=facts.payload_json)
    def permit(action,*args):
        return ActionAuthorization('operator','fixture',action.action_sha256,action.session_id,action.effect_id,
                                   action.binding_id,action.fence_seq,action.deadline)
    def sink(receipt,artifacts,live):
        if sink_error:raise OSError('fixture custody unavailable')
        assert live is control
        saved.append((receipt,artifacts))
    def runner(argv,**kwargs):
        observed.append(argv)
        Path(kwargs['dest'],'stdout.txt').write_bytes(b'checked output')
        Path(kwargs['dest'],'stderr.txt').write_bytes(b'')
        return dict(pid=1234,exit_code=0,cleanup_verified=True,cancelled=False,timeout=False,output_limit=False)
    monkeypatch.setattr('compute_plane.managed_process.run_process',runner)
    factory=confined_mediation_factory(profile=profile,broker=Broker(),snapshot_factory=snapshot,
        context_factory=lambda *args:'fixture public context',authorization_factory=permit,
        guard_factory=lambda caps,ctrl:ExactActionGuard(lambda a,p:p.issuer=='operator',caps.claim_action),
        signer=signer,admitted_keys=keys,result_sink=sink)
    kwargs=dict(session_id='session',binding=binding,control=control,config={'repositories':{'repo':str(root)}},
                custody=tmp_path,scratch=scratch,capabilities=capabilities)
    port=factory(**kwargs);capabilities.bind_command_mediation(port)
    request=EffectRequest('session','session/effect/1',ToolCall('call','verify','{"name":"unit"}'))
    return capabilities,control,request,factory,kwargs,observed,saved,keys


def test_exact_authorized_effect_returns_admitted_signature_and_durable_claim(tmp_path,monkeypatch):
    caps,control,request,factory,kwargs,observed,saved,keys=fixture(tmp_path,monkeypatch)
    result=caps.execute(request,control)
    assert result.status=='succeeded'
    data=json.loads(result.output_json);receipt=data['process']['producer_receipt']
    assert data['process']['authority']=='NONE' and len(observed)==len(saved)==1
    expected=receipt['payload']['subject']
    assert expected['effect_id']==request.effect_id and expected['fence_seq']==3
    assert verify_receipt(receipt,saved[0][1],expected,keys)['producer_id'] in keys
    replay=caps.execute(request,control)
    assert replay.status!='succeeded' and len(observed)==1
    with pytest.raises(ValueError,match='already_used'):factory(**kwargs)


def test_denial_never_executes_or_signs(tmp_path,monkeypatch):
    caps,control,request,_,_,observed,saved,_=fixture(tmp_path,monkeypatch,allowed=False)
    assert caps.execute(request,control).status=='refused'
    assert not observed and not saved


def test_custody_failure_is_unknown_and_cannot_reexecute(tmp_path,monkeypatch):
    caps,control,request,_,_,observed,saved,_=fixture(tmp_path,monkeypatch,sink_error=True)
    assert caps.execute(request,control).status=='unknown'
    assert caps.execute(request,control).status!='succeeded'
    assert len(observed)==1 and not saved


def test_invalid_signature_never_reaches_sink_and_cannot_reexecute(tmp_path,monkeypatch):
    original=ProducerSigner.sign
    def forged(self,*args):
        receipt=original(self,*args);receipt['signature']='A'*86+'=='
        return receipt
    monkeypatch.setattr(ProducerSigner,'sign',forged)
    caps,control,request,_,_,observed,saved,_=fixture(tmp_path,monkeypatch)
    assert caps.execute(request,control).status=='unknown'
    assert caps.execute(request,control).status!='succeeded'
    assert len(observed)==1 and not saved


@pytest.mark.skipif(__import__('os').name!='nt',reason='Windows native bridge qualification')
def test_real_confined_verifier_is_signed_and_recorded_through_existing_owner(tmp_path):
    import os
    from uuid import uuid4
    from compute_plane.confined_workspace import ConfinedWorkspace
    runtime={'cmd.exe':(Path(os.environ['SystemRoot'])/'System32/cmd.exe').read_bytes()}
    custody=tmp_path/'custody';custody.mkdir()
    saved=[]
    with ConfinedWorkspace(tmp_path/('tare-confined.'+uuid4().hex),runtime,{'input.txt':b'fixture'}) as profile:
        binding=BindingRef('work','binding','operator',2,'opaque')
        control=ExecutionControl(binding,CancellationToken(),time.monotonic()+20)
        argv=[str(profile.runtime_root/'cmd.exe'),'/d','/c','echo confined proof']
        caps=NativeCapabilities(session_id='session',binding=binding,repositories={'repo':str(profile.input_root)},
            writable={},verifiers={'unit':{'repository':'repo','argv':argv}},scratch_root=custody,specgraph_edit_context=False)
        signer=ProducerSigner(Ed25519PrivateKey.generate());keys={signer.producer_id:signer.public_key_bytes}
        class Broker:
            def assess(self,*args):return ActionDecision(True,'finite owned fixture')
        def snapshot(facts,live):
            return ActionSnapshot(facts.work_id,facts.session_id,facts.effect_id,facts.binding_id,facts.fence_seq,
                facts.operation,facts.repository,facts.cwd,facts.environment_sha256,'d'*64,profile.source_sha256,
                'e'*64,facts.grant_sha256,'windows-lpac/1:'+profile.profile_sha256,control.deadline_monotonic,
                argv=facts.argv,payload_json=facts.payload_json)
        def permit(action,*args):
            return ActionAuthorization('operator','fixture',action.action_sha256,action.session_id,action.effect_id,
                                       action.binding_id,action.fence_seq,action.deadline)
        factory=confined_mediation_factory(profile=profile,broker=Broker(),snapshot_factory=snapshot,
            context_factory=lambda *args:'owned copied fixture',authorization_factory=permit,
            guard_factory=lambda cap,ctrl:ExactActionGuard(lambda a,p:p.issuer=='operator',cap.claim_action),
            signer=signer,admitted_keys=keys,result_sink=lambda receipt,artifacts,live:saved.append((receipt,artifacts)))
        port=factory(session_id='session',binding=binding,control=control,
                     config={'repositories':{'repo':str(profile.input_root)}},custody=tmp_path,scratch=custody,capabilities=caps)
        caps.bind_command_mediation(port)
        request=EffectRequest('session','session/effect/1',ToolCall('call','verify','{"name":"unit"}'))
        result=caps.execute(request,control)
        assert result.status=='succeeded',result.output_json
        data=json.loads(result.output_json)
        assert data['process']['confinement']['capability_count']==0 and data['process']['cleanup_verified']
        assert len(saved)==1 and saved[0][1]['stdout.txt'].strip()==b'confined proof'
        receipt,artifacts=saved[0]
        expected=receipt['payload']['subject']
        assert expected['action_sha256']==data['process']['producer_receipt']['payload']['subject']['action_sha256']
        verify_receipt(receipt,artifacts,expected,keys)
        assert caps.execute(request,control).status!='succeeded'
        assert len(saved)==1
