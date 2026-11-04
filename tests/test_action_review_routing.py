"""Route by the connection executing the action, including HTTP models named like Codex models."""
from copy import deepcopy
import json
from pathlib import Path
import shlex
import pytest
from compute_plane.action_control import ActionRefused
from relay import command_protection as protection
from test_command_protection import host, jev_composition  # noqa: F401


def test_automatic_codex_uses_only_native_review_and_preserves_declared_policy(jev_composition):
    h=jev_composition
    h.host.config['command_protection']=protection.prepare({'review':'auto','max_reviews':1},h.host.deployment)
    saved=deepcopy(h.host.config)
    result=h.host.build().execute(h.host.facts(),h.host.effect)
    assert h.calls==[] and len(h.host.native)==1 and h.host.effects==['effect']
    assert result['command_protection']['review']=='codex'
    assert result['command_protection']['backend']==saved['command_protection']['backend']=='mxc'
    assert result['command_protection']['routing']=='automatic_by_execution_connection'
    assert h.host.config==saved


@pytest.mark.parametrize('endpoint',['http://fixture','cli://claude','cli://agy'])
def test_automatic_other_connections_use_jev_without_a_codex_dependency(jev_composition,endpoint):
    h=jev_composition
    h.host.deployment['native_profile']['endpoint']=endpoint
    h.host.deployment.pop('native_cli')
    h.host.config['command_protection']=protection.prepare({'review':'auto','max_reviews':1},h.host.deployment)
    result=h.host.build().execute(h.host.facts(),h.host.effect)
    assert h.calls==['jev'] and h.host.native==[] and h.host.effects==['effect']
    assert result['command_protection']['review']=='tare'
    assert result['command_protection']['backend']=='mxc'
    assert result['command_protection']['review_status']=='not_required'


@pytest.mark.parametrize('verdict,expected,executed',[
    ('REVIEW_REQUIRED',['jev','reviewer'],True),
    ('DENY_RECOMMENDED',['jev'],False),
    ('INSUFFICIENT_EVIDENCE',['jev'],False),
])
def test_automatic_jev_keeps_review_and_denial_semantics(jev_composition,verdict,expected,executed):
    h=jev_composition
    h.host.deployment['native_profile']['endpoint']='http://fixture'
    h.choice[0]=verdict
    h.host.config['command_protection']=protection.prepare({'review':'auto','max_reviews':2},h.host.deployment)
    if executed:
        h.host.build().execute(h.host.facts(),h.host.effect)
    else:
        with pytest.raises(ActionRefused):h.host.build().execute(h.host.facts(),h.host.effect)
    assert h.calls==expected and h.host.native==[]
    assert bool(h.host.effects)==executed


def test_automatic_policy_cannot_mix_a_manual_classifier_or_route_identity():
    for value in ({'review':'auto','classifier':'model'},
                  {'review':'auto','review_policy':'always'},
                  {'review':'auto','codex_sha256':'a'*64},
                  {'review':'auto','jev_model':'jev-1.13.0'}):
        with pytest.raises(ValueError):protection.saved(value)


def test_protected_pytest_cache_uses_scratch_without_project_write_access(host,monkeypatch):
    seen=[]
    def isolation(**options):
        seen.append(options)
        return options['mediation_factory']
    monkeypatch.setattr(protection,'windows_isolation_factory',isolation)
    host.config['writable']={'project':[]}
    mediator=host.build()
    options=seen[0]
    args=shlex.split(options['environment']['PYTEST_ADDOPTS'])
    test_root=Path(options['environment']['TMPDIR'])
    assert args==['-o','cache_dir='+str(test_root/'cache'),'--basetemp='+str(test_root/'tests')]
    assert test_root.name.startswith('tare-test-') and test_root.exists()
    assert not test_root.is_relative_to(host.root) and not test_root.is_relative_to(host.custody)
    assert str(test_root) in options['policy'].read_write
    assert str(host.root) not in options['policy'].read_write
    assert str(host.custody/'command-tmp') in options['policy'].read_write
    mediator.close()
    assert not test_root.exists()


def test_only_a_private_candidate_gets_artifact_write_scope(host,monkeypatch,tmp_path):
    from relay import native_installation
    storage=tmp_path/'storage'
    candidate=storage/'wt'/'cafe1234';candidate.mkdir(parents=True)
    (candidate/'.git').mkdir()
    monkeypatch.setattr(native_installation,'storage_root',lambda:storage)
    seen=[]
    monkeypatch.setattr(protection,'windows_isolation_factory',lambda **options:
        seen.append(options) or options['mediation_factory'])
    host.config['repositories']={'project':str(candidate)}
    host.config['writable']={'project':['changed.py']}
    mediator=host.build()
    policy=seen[0]['policy']
    assert str(candidate) in policy.read_write and str(candidate.parent) not in policy.read_write
    assert str(candidate/'.git') in policy.read_only
    assert str(candidate/'.git') not in policy.read_write
    assert str(candidate/'.git') not in policy.denied
    mediator.close()


def test_host_metadata_exception_cannot_be_used_by_command_capabilities(host):
    host.capabilities.names=('read','verify','command')
    with pytest.raises(ValueError,match='read/verify-only'):
        protection.factory(host.config,host.deployment,host.custody,'Run tests','inv',host_checks=True)(
            capabilities=host.capabilities,control=host.control,binding=host.binding)


@pytest.mark.parametrize('key,reason',[('timeout','timed out'),('cancelled','cancelled'),
                                     ('output_limit','exceeded its output limit')])
def test_interrupted_host_check_names_the_check_and_cause(host,monkeypatch,key,reason):
    from compute_plane.native_capabilities import NativeCapabilities
    from tare_tools_agent_runtime import EffectResult
    command=[str(host.executable),'-c','print(1)']
    run=protection.conversation_verifier(host.control,host.custody,host.config,host.deployment,
        host.root,{'title':'Run tests','criteria':[],'target_paths':[]},{'tests':command})
    monkeypatch.setattr(NativeCapabilities,'execute',lambda self,request,control:
        EffectResult(request.effect_id,'failed',json.dumps({'error_type':'CommandInterrupted',
            'process':{key:True,'secret_or_noisy_payload':'x'*3000}})))
    with pytest.raises(ValueError,match='tests: '+reason) as error:run(command)
    assert len(str(error.value))<130
