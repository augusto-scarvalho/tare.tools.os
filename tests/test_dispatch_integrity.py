import json
import pytest
from pathlib import Path
from relay import native_agent,work_units,execution_scope as scope
from test_native_agent import setup
from test_implement_unit import repository
from test_role_pin_dispatch import pin_catalog
from test_role_pins import parse
from test_conversation_tasks import host
from test_conversation_review_recovery import reviewed


def test_temporary_worker_pin_is_recorded_as_an_explicit_pin(setup,pin_catalog,monkeypatch):
    config,custody,root=setup;repository(root)
    monkeypatch.setattr(work_units,'run_checks',lambda *a:('green','fixture check'))
    sent=[]
    def worker(child,value,session,instruction,**kwargs):
        sent.append(value)
        (Path(child['repositories']['repo'])/'a.py').write_text('value = 2\n')
        return {'status':'PROPOSED','model_calls':1}
    monkeypatch.setattr(native_agent,'run_session',worker)
    state={'config':config,'conversation_flow':True,'deployment':pin_catalog['producer-cli'],
           'limits':{'max_model_calls':30},'deadline_utc':None}
    class Review:
        def preflight(self,value):return True
        def __call__(self,*a):return {'verdict':'REVISE'}
    with scope.activated(parse(['--implementer','anthropic/test-cli@max'])):
        with scope.for_role('coordinator'):
            result=work_units.run_unit(custody,state,{'title':'Set two','target_paths':['a.py'],
                'criteria':['two'],'verifiers':['unit']},lambda:False,review=Review())
    row=json.loads(Path(result['receipt']).read_text(encoding='utf8'))['rungs'][0]
    assert len(sent)==1 and scope.pin_matches('anthropic/test-cli@max',sent[0])
    assert row['attempts'][0]['reasoning']=='max'
    assert row['executed_decision']['mode']=='EXACT_TEMPORARY_PIN'


def test_audit_preparation_error_does_not_claim_an_executed_attempt(reviewed,monkeypatch):
    host,run,*_=reviewed;tools=host[0]
    original=Path.write_text
    def write(self,*a,**kw):
        if self.name=='request.json' and 'reviews' in self.parts:
            raise OSError('simulated disk full before auditor dispatch')
        return original(self,*a,**kw)
    monkeypatch.setattr(Path,'write_text',write)
    with pytest.raises(OSError,match='simulated disk full before auditor dispatch'):
        run('INCONCLUSIVE',expected_calls=0)
    row=json.loads(next((tools.root/'reviews').glob('*/receipt.json')).read_text(encoding='utf8'))
    session=next((tools.root/'reviews').glob('*/receipt.json')).parent/'session'
    assert not (session/'native-agent.json').exists() and not (session/'journal.jsonl').exists()
    assert not row.get('attempts') and not row.get('executed_decision')
