import copy
import json
import pytest
from relay import native_routes, work_audit, relay_mesh, work_run
from test_work_run import os_root
from test_work_resume import host, run
from test_work_audit import reviewers


@pytest.fixture
def candidates(host, reviewers, monkeypatch):
    profiles, _, _, _ = reviewers
    rows = [json.loads(host[2].read_bytes()), *[json.loads(p.read_bytes()) for p in profiles]]
    for i, row in enumerate(rows):
        row.update(id='author' if i == 0 else row['id'], roles=['auditor'], kind='primary', cost_tier='free-credit')
    table = {'schema_version': 1, 'deployments': rows}
    monkeypatch.setattr(native_routes, 'configured', lambda: copy.deepcopy(table))
    return table


def test_cost_order_independence_and_unconfigured_runners(host, candidates):
    rows = candidates['deployments'];rows[0]['cost_tier']='free';rows[2]['cost_tier']='free'
    rows += [{**rows[1], 'id':'cli', 'runner':'claude', 'cost_tier':'free'},
             {**rows[1], 'id':'not-auditor', 'roles':['explorer']}]
    policy=work_audit.prepare(json.loads(host[2].read_bytes()), 'auto', 'optional')
    assert policy['status']=='READY' and policy['mode']=='required'
    assert [r['deployment']['id'] for r in policy['routes']]==['two','one']
    seen={r['deployment']:r for r in policy['selection']['candidates']}
    assert seen['author']['reason']=='AUDITOR_NOT_INDEPENDENT'
    assert seen['cli']['reason']=='RUNNER_NOT_SUPPORTED_BY_NATIVE_WORK'
    assert seen['not-auditor']['reason']=='ROLE_NOT_CONFIGURED'
    assert policy['selection']['generation_calls']==0


@pytest.mark.parametrize('fault',['identity','credential','cost','role'])
def test_no_eligible_candidate_blocks_before_implementation(host,candidates,monkeypatch,capsys,fault):
    rows=candidates['deployments'];rows[:]=rows[1:2]
    if fault=='identity':rows[0]['model']='unknown'
    elif fault=='credential':
        rows[0]['native_credential_env']='TEST_ROUTE_TOKEN'
        monkeypatch.setattr(native_routes,'credential_available',lambda _:False)
    elif fault=='cost':rows[0]['cost_tier']='unknown'
    else:rows[0]['roles']=['explorer']
    assert run(host,'--auditor-deployment','auto')==1
    result=json.loads(capsys.readouterr().out)
    assert result['audit_routing']['status']=='BLOCKED'
    assert not host[3].generations and not host[4]


def test_named_profile_prevents_using_stale_table_parameters(host,candidates,monkeypatch,tmp_path):
    from relay import native_catalog
    row=candidates['deployments'][1];row['native_catalog']='current-profile'
    current=copy.deepcopy(row);current['native_profile']['output_tokens']=1200
    monkeypatch.setenv('TARE_PROFILE_DIR', str(tmp_path))
    (tmp_path/'current-profile.json').write_text(json.dumps(current))
    monkeypatch.setattr(native_catalog,'select',lambda name: copy.deepcopy(current))
    row['native_profile']['output_tokens']=10
    policy=work_audit.prepare(json.loads(host[2].read_bytes()),'auto','optional')
    assert policy['routes'][0]['deployment']['native_profile']['output_tokens']==1200


@pytest.mark.parametrize('failure',[None,'HTTP_429','HTTP_503','INVALID_DELIVERY','REVISE'])
def test_auto_uses_existing_execution_fallback_and_does_not_shop_for_verdicts(host,reviewers,candidates,capsys,failure):
    profiles,calls,failures,verdicts=reviewers
    if failure=='REVISE':verdicts['one']=failure
    elif failure:failures['one']=failure
    code=run(host,'--auditor-deployment','auto','--max-bytes','65536');result=json.loads(capsys.readouterr().out)
    assert (code==0)==(failure in (None,'HTTP_429','HTTP_503')), (result.get('stage'),result.get('status'),result.get('error'),result.get('admission'))
    assert calls==(['one','two'] if failure in ('HTTP_429','HTTP_503') else ['one']), result
    assert result['audit_routing']['selection']['role']=='auditor'
    origin=json.loads((work_run.train_dir_for('work-one')/'WORK_RUN.json').read_bytes())
    assert origin['audit']['selection']==result['audit_routing']['selection']
    if failure in ('INVALID_DELIVERY','REVISE'):
        import tare_ops
        before=len(host[3].generations)
        candidates['deployments'].clear()
        assert tare_ops.main(['run','work-one','--resume'])==1
        assert len(host[3].generations)==before and calls==['one']


def test_auto_conflicts_with_off_and_explicit_routes(host,candidates):
    with pytest.raises(ValueError,match='OFF_POLICY'):
        work_audit.prepare(json.loads(host[2].read_bytes()),'auto','off')
    with pytest.raises(ValueError,match='CANNOT_MIX'):
        work_audit.select_deployments(['auto','bedrock-opus'])


def test_console_preserves_auto_on_new_run_only(tmp_path):
    from relay.console_backend import Selection,command
    selected=Selection(root=str(tmp_path),mode='work',work='one',auditor='auto')
    args=command(selected)
    assert args[args.index('--auditor-deployment')+1]=='auto'
    assert '--auditor-deployment' not in command(selected,resume=True)


def test_cross_vendor_preference_precedes_cost(host,candidates):
    roster=relay_mesh.RELAY_DIR/'round_table_roster.json';value=json.loads(roster.read_bytes())
    value['models']['glm-5']['vendor']=value['models']['qwen-3.8-27b']['vendor']
    roster.write_text(json.dumps(value))
    candidates['deployments'][2]['cost_tier']='free'
    policy=work_audit.prepare(json.loads(host[2].read_bytes()),'auto','optional')
    assert [r['deployment']['id'] for r in policy['routes']]==['one','two']
    assert policy['routes'][1]['eligible'] and not policy['routes'][1]['cross_vendor']


def test_shared_credential_checked_once_without_persisting_secret(host,candidates,monkeypatch):
    calls=[]
    for row in candidates['deployments'][1:]:row['native_credential_env']='TEST_ROUTE_TOKEN'
    monkeypatch.setattr(native_routes,'credential_available',lambda name: calls.append(name) or True)
    policy=work_audit.prepare(json.loads(host[2].read_bytes()),'auto','optional')
    assert calls==['TEST_ROUTE_TOKEN'] and len(policy['routes'])==2
    assert all(r['availability']=='CREDENTIAL_PRESENT_HOST_UNPROBED' for r in policy['selection']['candidates'] if r['eligible'])
