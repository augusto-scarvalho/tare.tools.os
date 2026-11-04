"""Diagnostic observations must neither execute nor manufacture a root cause."""
from importlib import metadata
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from packaging.requirements import Requirement

import tare_ops
from relay import native_diagnostics as diagnostic, native_installation
from relay.native_catalog import select


def dist(tmp_path, version, *, files=True):
    relative = Path('compute_plane/native_models.py')
    return SimpleNamespace(version=version, files=[relative] if files else None,
                           locate_file=lambda p: tmp_path/p)


@pytest.mark.parametrize('version,expected', [
    ('0.9.1','VERSION_OUTSIDE_REQUIREMENT'), ('0.9.2','AVAILABLE'),
    ('0.10.0','VERSION_OUTSIDE_REQUIREMENT'), ('0.9.2rc1','VERSION_OUTSIDE_REQUIREMENT'),
    ('nonsense','UNAVAILABLE')])
def test_exact_dependency_range_handles_patch_upper_bound_and_prerelease(tmp_path,monkeypatch,version,expected):
    monkeypatch.setattr(diagnostic.metadata,'distribution',lambda name:dist(tmp_path,version))
    monkeypatch.setattr(diagnostic,'import_module',lambda name:SimpleNamespace(__file__=tmp_path/'compute_plane/native_models.py'))
    report=diagnostic.package('tare-kernel','compute_plane.native_models',Requirement('tare-kernel>=0.9.2,<0.10'))
    assert report['status']==expected
    assert report['distribution_root']==str(tmp_path.resolve())


@pytest.mark.parametrize('files,shadow',[(False,False),(True,True)])
def test_editable_or_shadow_import_does_not_inherit_distribution_attestation(tmp_path,monkeypatch,files,shadow):
    monkeypatch.setattr(diagnostic.metadata,'distribution',lambda name:dist(tmp_path,'0.9.2',files=files))
    monkeypatch.setattr(diagnostic,'import_module',lambda name:SimpleNamespace(
        __file__=tmp_path/('checkout/native_models.py' if shadow else 'compute_plane/native_models.py')))
    assert diagnostic.package('tare-kernel','compute_plane.native_models',Requirement('tare-kernel>=0.9.2'))['status']=='IMPORT_ORIGIN_UNCONFIRMED'


def test_running_source_requirements_do_not_use_old_installed_metadata(monkeypatch):
    monkeypatch.setattr(diagnostic,'__file__',str(Path(__file__).resolve().parents[1]/'relay/native_diagnostics.py'))
    def forbidden(*args): raise AssertionError('installed metadata must not replace source requirements')
    monkeypatch.setattr(diagnostic.metadata,'distribution',forbidden)
    req,basis=diagnostic.requirements()
    assert basis=='source_pyproject' and '0.9.1' not in req['tare-kernel'].specifier
    assert '0.13.1' not in req['tare-tools-agent-runtime'].specifier


def test_installed_requirements_branch_uses_distribution_metadata(tmp_path,monkeypatch):
    monkeypatch.setattr(diagnostic,'__file__',str(tmp_path/'relay/native_diagnostics.py'))
    monkeypatch.setattr(diagnostic.metadata,'distribution',lambda name:SimpleNamespace(
        version='0.1.4',requires=['tare-kernel>=0.9.2,<0.10','tare-tools-agent-runtime>=0.13.2,<0.14',
            'tare-tools-specgraph>=0.7.1,<0.8','tare-tools-backlog-graph>=1.2,<2']))
    req,basis=diagnostic.requirements()
    assert basis=='installed_metadata' and str(req['tare-tools-os'])=='tare-tools-os==0.1.4'


@pytest.mark.parametrize('name',['local-qwen','hauhaucs','fable-tc','qwen36-moe','bedrock-opus','bedrock-sonnet','bedrock-glm'])
def test_named_profiles_static_inspection_has_no_http_or_credential_access(name,monkeypatch):
    from compute_plane.native_models import NativeHttpClient
    from relay import native_generation
    calls=[]
    def forbidden(*a,**kw):calls.append(True);raise AssertionError('unexpected access')
    monkeypatch.setattr(NativeHttpClient,'post_observed',forbidden)
    monkeypatch.setattr(native_generation,'vault_secret',forbidden)
    report=diagnostic.profile(select(name))
    assert report['status']=='CONFIGURATION_VALID' and report['provider_readiness']=='NOT_PROBED'
    assert not calls
    assert report['counting']==('ESTIMATED' if name in ('bedrock-glm','local-qwen') else 'MEASURED_REQUIRED')  # NInfer has no /tokenize


def test_estimated_spending_conflict_and_unpriced_categories_are_actionable():
    from dataclasses import asdict
    from test_native_spend import tariff
    dep=select('bedrock-glm')
    dep.update(native_usage_tariff=asdict(tariff(output_tokens=None)),
               native_spend_policy={'max_amount':'1','currency':'USD'})
    report=diagnostic.profile(dep)
    assert report['issues']==['SPEND_REQUIRES_MEASURED_COUNT_BUT_PROFILE_ESTIMATES','SPEND_TARIFF_INCOMPLETE']


@pytest.mark.parametrize('status,reason,category',[
    ('BLOCKED','SPEND_LIMIT','SPENDING_ADMISSION'),
    ('FAILED','HTTP_429','PROVIDER_THROTTLING'),
    ('FAILED','HTTP_401','PROVIDER_ACCESS'),
    ('FAILED','INVALID_PROVIDER_RESPONSE','RESPONSE_CONTRACT'),
    ('BLOCKED','CONTEXT_COUNT_UNAVAILABLE','CONTEXT_PREPARATION'),
    ('RECONCILIATION_REQUIRED','UNKNOWN','EXECUTION_UNCERTAIN'),
    ('CANCELLED','CANCELLED','EXECUTION_STOPPED'),
    ('FAILED','HTTP_500','PROVIDER_TRANSPORT'),
    ('EXHAUSTED','MODEL_CALL_BUDGET','BUDGET_EXHAUSTED_WITHOUT_DELIVERY'),
    ('PROPOSED','DELIVERY_PROPOSED','RESPONSE_OBSERVED'),
    ('BLOCKED','MODEL_FAILURE','MODEL_INVOCATION_FAILED'),
    ('BLOCKED','untrusted secret contents','UNDETERMINED'),
])
def test_receipts_preserve_evidence_limits_without_echoing_text(tmp_path,status,reason,category):
    path=tmp_path/'outcome.json'
    path.write_text(json.dumps({'execution':'native','status':status,'reason':reason,
        'output':'SECRET <tool_call>fake delivery</tool_call>','usage_file':'must-not-open'}))
    before=path.read_bytes();report=diagnostic.receipt(path)
    assert report['category']==category and report['task_correctness']=='NOT_EVALUATED'
    assert 'SECRET' not in json.dumps(report) and 'untrusted secret contents' not in json.dumps(report)
    assert path.read_bytes()==before and len(list(tmp_path.iterdir()))==1


def test_text_receipt_and_pending_work_attempt(tmp_path):
    path=tmp_path/'receipt.json'
    path.write_text(json.dumps({'attempt_id':'a','effective_profile':{},'returncode':1,
                               'failure_class':'SPEND_LIMIT','generation_not_started':True}))
    assert diagnostic.receipt(path)['dispatch']=='NOT_STARTED'
    path.write_text(json.dumps({'execution':'native','outcome':{'status':'cancelled','reason':'CANCELLED','pending_id':'s/model/1'}}))
    assert diagnostic.receipt(path)['category']=='EXECUTION_UNCERTAIN'


def test_malformed_receipt_does_not_prevent_other_layers_or_disclose_exception(tmp_path,monkeypatch):
    monkeypatch.setattr(native_installation,'storage_root',lambda:tmp_path)
    monkeypatch.setattr(diagnostic,'package',lambda *a:{'status':'AVAILABLE'})
    path=tmp_path/'receipt.json';path.write_text('secret malformed body')
    result=diagnostic.doctor(deployment_name='bedrock-glm',receipt_path=path)
    assert result['profile']['status']=='CONFIGURATION_VALID'
    assert result['receipt']['status']=='UNAVAILABLE' and 'secret' not in json.dumps(result)
    assert result['operational_health']=='ACTION_REQUIRED'


def test_missing_storage_is_not_reported_configured_or_created(tmp_path,monkeypatch):
    root=tmp_path/'absent'
    monkeypatch.setattr(native_installation,'storage_root',lambda:root)
    report=diagnostic.doctor()
    assert report['storage']['status']=='DIRECTORY_MISSING' and not root.exists()


def test_cli_forwards_only_explicit_native_options(tmp_path,monkeypatch,capsys):
    seen=[]
    monkeypatch.setattr(native_installation,'doctor',lambda **kw:seen.append(kw) or {'operational_health':'CHECKS_PASSED'})
    assert tare_ops.main(['doctor','--native','--deployment','bedrock-glm','--receipt',str(tmp_path/'r.json')])==0
    assert seen==[{'deployment':None,'deployment_name':'bedrock-glm','receipt_path':tmp_path/'r.json',
                   'root':Path.cwd().resolve()}]
    assert tare_ops.main(['doctor','--deployment','bedrock-glm'])==2
    assert len(seen)==1


def test_input_budget_and_unsupported_shape(tmp_path):
    path=tmp_path/'r.json';path.write_text('x'*20)
    with pytest.raises(ValueError,match='over_budget'):diagnostic.read_object(path,10)
    path.write_text('{}')
    with pytest.raises(ValueError,match='unsupported'):diagnostic.receipt(path)


def test_missing_ripgrep_needs_action(tmp_path,monkeypatch):
    monkeypatch.setattr(native_installation,'storage_root',lambda:tmp_path)
    monkeypatch.setattr(diagnostic,'requirements',lambda:({},'test'))
    monkeypatch.setattr(diagnostic,'MODULES',{})
    monkeypatch.setattr(diagnostic,'host_executable',lambda name:'C:/bin/rg.exe')
    assert diagnostic.doctor()['operational_health']=='CHECKS_PASSED'
    monkeypatch.setattr(diagnostic,'host_executable',lambda name:None)
    report=diagnostic.doctor()
    assert report['ripgrep'].startswith('NOT_ON_PATH') and report['operational_health']=='ACTION_REQUIRED'


@pytest.mark.parametrize('platform,hint', [('win32', 'winget install BurntSushi.ripgrep.MSVC'),
    ('darwin', 'brew install ripgrep'), ('linux', 'apt install ripgrep')])
def test_missing_ripgrep_hint_names_the_platform_installer(tmp_path, monkeypatch, platform, hint):
    monkeypatch.setattr(native_installation,'storage_root',lambda:tmp_path)
    monkeypatch.setattr(diagnostic,'requirements',lambda:({},'test'))
    monkeypatch.setattr(diagnostic,'MODULES',{})
    monkeypatch.setattr(diagnostic,'host_executable',lambda name:None)
    monkeypatch.setattr(diagnostic.sys,'platform',platform)
    assert hint in diagnostic.doctor()['ripgrep']
