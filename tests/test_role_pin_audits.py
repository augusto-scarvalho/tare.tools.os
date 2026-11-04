"""Real independent auditor selection reaches packets, receipts and calls unchanged."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from relay import conversation_review, native_agent, native_plan, turn_engine, work_audit
from relay import execution_scope as scope
from test_role_pin_dispatch import pin_catalog  # noqa: F401
from test_role_pins import parse
from test_conversation_tasks import host, intake, planned  # noqa: F401


def test_independent_pin_survives_picker_packet_receipt_and_dispatch(host,pin_catalog,monkeypatch):
    tools,control,root,_=host
    intake(host);task=planned(host)
    from test_implement_unit import repository
    repository(root)
    changed=b'value = 2\n';(root/'a.py').write_bytes(changed)
    producer=deepcopy(pin_catalog['producer-cli'])
    tools.state['deployment']=producer
    seen=[]
    def run(config,value,session,instruction,**kwargs):
        packet=json.loads((Path(config['repositories']['review'])/'request.json').read_text())
        seen.append((deepcopy(value),packet))
        session.mkdir(parents=True)
        (session/'native-agent.json').write_text('{}')
        return {'status':'BLOCKED','reason':'Offline boundary','failure_code':'FIXTURE_FAILURE'}
    monkeypatch.setattr(native_agent,'run_session',run)
    with scope.activated(parse(['--auditor','anthropic/test-cli@max'])):
        picked=turn_engine.pick_auditors(producer,['test-cli'])
        assert scope.pin_matches('anthropic/test-cli@max',picked[0][1])
        author=work_audit.identity(producer,'implementer')
        critic=work_audit.identity(picked[0][1],'auditor')
        assert author.bias_domain!=critic.bias_domain and author.model_id!=critic.model_id
        arguments=(tools,task,root,producer,task['plan']['unit'],{'a.py':changed},{'after':'green'},control)
        result=conversation_review.review(*arguments)
        receipt=next((tools.root/'reviews').glob('*/receipt.json'))
        old=receipt.read_bytes()
        assert conversation_review.review(*arguments)==result
        assert receipt.read_bytes()==old
    assert len(seen)==1 and result['verdict']=='INCONCLUSIVE'
    value,packet=seen[0]
    assert scope.pin_matches('anthropic/test-cli@max',value)
    row=json.loads(old)
    assert row['deployment']==value
    assert row['context_contract']==conversation_review.context_contract(value)
    assert packet['host_execution_selection']==conversation_review.execution_selection(producer,value,planner=None)
    assert producer==pin_catalog['producer-cli']


@pytest.mark.parametrize('role',['auditor','plan-auditor'])
def test_same_lineage_is_rejected_without_replacement(pin_catalog,role):
    with scope.activated(parse(['--auditor','anthropic/test-cli@max'])):
        with pytest.raises(ValueError,match='not independent'):
            scope.pinned(role,pin_catalog['test-cli'])


def test_plan_auditor_uses_actual_planner_producer(pin_catalog):
    with scope.activated(parse(['--auditor','anthropic/test-cli@max'])):
        value,receipt=native_plan.select_route('auto','plan-auditor','fixture',pin_catalog['producer-cli'])
    assert scope.pin_matches('anthropic/test-cli@max',value)
    assert receipt['reason']=='EXACT_TEMPORARY_PIN'


def test_serve_audit_keeps_pin_and_stops_after_failure(pin_catalog,monkeypatch,tmp_path):
    monkeypatch.setattr(native_agent,'session_root',lambda:tmp_path/'sessions')
    calls=[]
    def run(config,value,*args,**kwargs):
        calls.append(deepcopy(value))
        return {'status':'BLOCKED','reason':'fixture failure'}
    monkeypatch.setattr(native_agent,'run_session',run)
    with scope.activated(parse(['--auditor','anthropic/test-cli@max'])):
        turn_engine.run_audit('Review',[],tmp_path,pin_catalog['producer-cli'],lambda text:None)
    assert len(calls)==1 and scope.pin_matches('anthropic/test-cli@max',calls[0])
