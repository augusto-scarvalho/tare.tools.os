from pathlib import Path
import json

import pytest
from relay import native_agent,native_generation
from test_native_agent import setup,deployment
from native_stream_fixture import serving


@pytest.mark.parametrize('protocol',['openai-chat','bedrock-converse'])
def test_native_text_reports_real_stream_progress_usage_and_completion(protocol):
    with serving(protocol,gate=False) as (url,seen,*_):
        dep=deployment(protocol,url);dep['native_streaming']=True
        # Converse has a real count endpoint in this fixture; Chat uses an explicit estimate.
        if protocol=='openai-chat':dep['native_counter_protocol']='utf8-envelope-estimate-v1'
        events=[]
        ok,content,receipt=native_generation.generate_text(dep,'fixture','Say hello',10,on_progress=events.append)
        assert ok and content=='olá mundo' and receipt['stream_status']=='COMPLETE'
        assert receipt['first_content_ms'] is not None and events and receipt['usage'] is not None
        assert receipt['generation_calls']==1


def test_native_session_persists_streaming_and_rejects_mode_change_on_resume(setup):
    config,custody,repo=setup
    with serving('bedrock-converse',tool=True) as (url,seen,*_):
        dep=deployment('bedrock-converse',url);dep['native_streaming']=True
        result=native_agent.run_session(config,dep,custody,'Propose a greeting.',on_progress=lambda e:None)
        assert result['status']=='PROPOSED' and result['streaming']
        changed={**dep,'native_streaming':False}
        with pytest.raises(ValueError,match='configuration_changed'):
            native_agent.run_session(None,changed,custody,resume=True)
        assert len([p for p,b in seen if p.endswith('/converse-stream')])==1


def test_streaming_respects_spend_admission_before_http_generation(setup):
    from dataclasses import asdict
    from test_native_spend import tariff
    config,custody,repo=setup
    with serving('bedrock-converse',tool=True) as (url,seen,*_):
        dep=deployment('bedrock-converse',url)
        dep.update(native_streaming=True,native_usage_tariff=asdict(tariff()),
                   native_spend_policy={'max_amount':'0','currency':'USD'})
        result=native_agent.run_session(config,dep,custody,'Propose a greeting.')
        assert result['reason']=='SPEND_LIMIT' and all(p.endswith('/count-tokens') for p,b in seen)


def test_cli_stream_keeps_provisional_json_separate_from_final_result(tmp_path,monkeypatch,capsys):
    from relay import native_catalog
    seen=[]
    def run(config,dep,custody,instruction,**kw):
        assert config['role'] == 'explorer' and not config['writable']
        assert config['project_admission'] == {'mode': 'quarantine', 'root_sha256': None, 'nested': False}
        seen.append(dep)
        kw['on_progress']({'kind':'delta','channel':'text','text':'a\x1b\nb','provisional':True})
        kw['on_progress']({'kind':'delta','channel':'reasoning_content','text':'private reasoning','provisional':True})
        return {'status':'PROPOSED'}
    monkeypatch.setattr(native_agent,'run_session',run)
    def select(name=None, path=None, *, role):
        assert role == 'explorer'
        return deployment()
    monkeypatch.setattr(native_catalog,'select',select)
    assert native_agent.main(['--root',str(tmp_path),'--session',str(tmp_path/'session'),
                              '--instruction','hello','--stream'])==0
    output=capsys.readouterr()
    events=[json.loads(line) for line in output.err.splitlines()]
    assert json.loads(output.out)=={'status':'PROPOSED'} and all(e['provisional'] for e in events)
    assert len(events)==2 and 'text' not in events[1]
    assert '\x1b' not in output.err and 'private reasoning' not in output.err
    assert seen[0]['native_streaming'] is True


@pytest.mark.parametrize('resume', [False, True])
def test_console_stream_preference_preserves_vendor_cli_transport(tmp_path, monkeypatch, resume):
    from relay import native_catalog
    from copy import deepcopy
    dep = deployment()
    dep['native_profile'].update(protocol='vendor-cli', endpoint='cli://claude',
                                 parameters_json='{"effort":"medium"}')
    dep['native_counter_protocol'] = 'utf8-envelope-estimate-v1'
    dep.pop('native_counter_endpoint')
    dep.pop('native_usage_contract')
    dep['native_cli'] = {'executable': str(tmp_path/'claude.exe'), 'version': '2.1.280'}
    original = deepcopy(dep)
    monkeypatch.setattr(native_catalog, 'select', lambda *args, **kwargs: dep)
    custody = tmp_path/'session'
    custody.mkdir()
    if resume:
        (custody/'native-agent.json').write_text(json.dumps({'deployment': dep, 'config': {}}))
    observed = []
    def run(config, selected, session, instruction, **kwargs):
        effective = original if resume else selected
        native_generation.deployment_profile(effective, effective['native_profile']['model'])
        assert kwargs['on_progress'] is None
        assert not effective.get('native_streaming', False)
        observed.append(kwargs['resume'])
        return {'status': 'PROPOSED'}
    monkeypatch.setattr(native_agent, 'run_session', run)
    args = ['--session', str(custody), '--stream']
    args += ['--resume'] if resume else ['--root', str(tmp_path), '--instruction', 'hello']
    assert native_agent.main(args) == 0
    assert dep == original and observed == [resume]
