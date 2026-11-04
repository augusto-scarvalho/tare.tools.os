"""Codex account inference is composed into the existing native session."""
import copy
import hashlib
import json
import pytest
from compute_plane.native_codex_auth import CODEX_ENDPOINT
from relay.native_generation import deployment_profile, inference_client


def deployment(tmp_path):
    return {'id':'codex-fixture','runner':'native-http','native_streaming':True,
        'native_counter_protocol':'utf8-envelope-estimate-v1',
        'native_codex_auth':{'path':str(tmp_path/'auth.json'),'account_id':'selected-account'},
        'native_profile':{'protocol':'openai-codex-responses','model':'fixture','context_window':32768,
            'output_tokens':4096,'source':'fixture','endpoint':CODEX_ENDPOINT},
        'native_usage_contract':{'layout':'responses-inclusive','source':'fixture'}}


@pytest.mark.parametrize('change', ['endpoint','credential','counter','stream','estimate','account','path','usage'])
def test_codex_route_rejects_ambiguous_credentials_and_incompatible_protocol(tmp_path,change):
    dep=deployment(tmp_path)
    if change=='endpoint': dep['native_profile']['endpoint']='https://example.org'
    if change=='credential': dep['native_credential_env']='API_KEY'
    if change=='counter': dep['native_counter_endpoint']=CODEX_ENDPOINT
    if change=='stream': dep['native_streaming']=False
    if change=='estimate': dep['native_counter_protocol']='server-template-tokenize'
    if change=='account': dep['native_codex_auth']['account_id']=''
    if change=='path': dep['native_codex_auth']['path']='relative.json'
    if change=='usage': dep['native_usage_contract']['layout']='chat-inclusive'
    with pytest.raises(ValueError): deployment_profile(dep,'fixture')


def test_factory_retains_explicit_account_and_never_calls_api_key_resolver(tmp_path):
    dep=deployment(tmp_path); profile=deployment_profile(dep,'fixture')
    client=inference_client(dep,profile,credential=lambda:pytest.fail('API key fallback'))
    assert client.endpoint==CODEX_ENDPOINT
    assert client.request_headers.__self__.path==tmp_path/'auth.json'
    assert client.request_headers.__self__.account_id=='selected-account'


def test_os_native_session_owns_read_edit_and_resume(tmp_path,monkeypatch):
    from relay import native_agent
    repo=tmp_path/'repo';repo.mkdir();(repo/'a.py').write_bytes(b'value = 1\n')
    monkeypatch.setattr(native_agent,'SESSION_ROOT',tmp_path/'locks')
    config={'schema':'tare.tools/native-agent-config/1','role':'implementer',
        'repositories':{'repo':str(repo)},'writable':{'repo':['a.py']},
        'capabilities':['read','edit','verify'],'specgraph_edit_context':False,
        'context_policy':{'trigger':.8,'target':.5},'verifiers':{}}
    # The real verifier runs in the live qualification; this fixture checks the
    # host's effect replay and account/route binding with an actual file edit.
    script=iter([('read',{'repository':'repo','path':'a.py'}),
                 ('edit',{'repository':'repo','path':'a.py','sha256':hashlib.sha256(b'value = 1\n').hexdigest(),
                          'search':'value = 1','replace':'value = 2'}),
                 ('tare_deliver',{'summary':'Updated a.py'})]); calls=[]
    class Client:
        endpoint=CODEX_ENDPOINT
        def post_stream_observed(self,path,body,control,**kwargs):
            calls.append(copy.deepcopy(body));name,args=next(script)
            return {'id':'resp','model':'fixture','status':'completed','output':[
                {'id':'fc','type':'function_call','status':'completed','call_id':'call',
                 'name':name,'arguments':json.dumps(args)}]}, {'http_attempts_upper_bound':0,
                 'transport':'fixture','cleanup_verified':True,'remote_stop':'not_applicable'}
    client=Client(); custody=tmp_path/'session'
    result=native_agent.run_session(config,deployment(tmp_path),custody,'Update a.py',client=client)
    assert result['status']=='PROPOSED' and (repo/'a.py').read_bytes()==b'value = 2\n'
    before=(custody/'journal.jsonl').read_bytes()
    assert native_agent.run_session(None,None,custody,resume=True,client=client)['status']=='PROPOSED'
    assert (custody/'journal.jsonl').read_bytes()==before and len(calls)==3
    saved=json.loads((custody/'native-agent.json').read_text())
    assert saved['deployment']['native_codex_auth']['account_id']=='selected-account'
