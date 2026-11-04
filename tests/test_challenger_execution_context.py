"""An admitted alternative receives routing facts without rewriting the shared work or its permissions."""
from copy import deepcopy
import pytest
from relay import challenger_worker as worker, challenger_variants, native_generation
from test_native_agent import deployment


@pytest.mark.parametrize('role',['implementer','auditor'])
def test_admitted_variant_context_is_deduplicated_and_preserves_the_shared_input(tmp_path,monkeypatch,role):
    baseline=deployment();baseline['native_profile']['model']='reference-model'
    alternate=deployment();alternate['native_profile']['model']='admitted-model'
    alternate['native_credential_env']='SECRET_ENV_NAME'
    config={'role':role,'writable':{'repo':['a.py']} if role=='implementer' else {}}
    before=deepcopy(config);session=tmp_path/'session';session.mkdir()
    row={'id':'trial','config':deepcopy(config),'baseline':baseline,'execution_contract':{'delegated_leaf':True,'variant_inbox':1},
        'settings':{'allow_unknown_quota':True,'quota_floor':25}}
    monkeypatch.setattr(challenger_variants,'preflight',lambda *a,**kw:{})
    monkeypatch.setattr(challenger_variants,'price',lambda *a:None)
    monkeypatch.setattr(native_generation,'inference_client',lambda *a,**kw:object())
    seen=[]
    def run(*args,**kwargs):seen.append((args,kwargs));return {'status':'PROPOSED'}
    monkeypatch.setattr(worker,'run_session',run)
    original='Reference implementer must use reference-model. Correct a.py and run the checks.'
    worker.execute(config,alternate,{'identity':{}},session,original,row,object())
    args,options=seen[0]
    assert args[3]==original and config==before
    assert row['config']==before
    assert options['delegated_leaf'] is True
    receive=options['receive_message'];message=receive(set())
    assert 'host-admitted' in message.text
    assert 'reference-model' in message.text and 'admitted-model' in message.text
    assert 'SECRET' not in message.text
    assert receive({message.message_id}) is None
