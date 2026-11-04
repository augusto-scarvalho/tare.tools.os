"""An operator's check belongs to delegated work, not coordinator authority."""
import copy
import json
import sys

import pytest

from relay import native_agent
from relay.conversation_tasks import detected_checks
from test_native_agent import Client, deployment


def test_cli_conversation_preserves_check_without_granting_verification(tmp_path, monkeypatch):
    profile=tmp_path/'profile.json'
    profile.write_text(json.dumps(deployment()))
    command=[sys.executable,'-m','pytest','tests/test_scoped.py','-q']
    observed=[]
    def run(config,*args,**kwargs):
        observed.append((native_agent.configuration(config),kwargs))
        return {'status':'PROPOSED','summary':'No inference in this fixture.'}
    monkeypatch.setattr(native_agent,'run_session',run)
    status=native_agent.main(['--native-deployment',str(profile),'--root',str(tmp_path),
        '--role','explorer','--conversation','--instruction','Fix the bug',
        '--session',str(tmp_path/'session'),'--verify-command',*command])
    assert status==0 and len(observed)==1
    config,options=observed[0]
    assert options['conversation'] is True
    assert config['role']=='explorer' and config['writable']=={}
    assert config['verifiers']=={} and 'verify' not in config['capabilities']
    assert detected_checks(config)=={'check':{'repository':tmp_path.name,'argv':command}}


@pytest.mark.parametrize('role,checks',[
    ('auditor',{}),('implementer',{}),('explorer',[]),
    ('explorer',{'check':{'repository':'other','argv':['python','check.py']}}),
    ('explorer',{'check':{'repository':'repo','argv':[]}}),
    ('explorer',{'check':{'repository':'repo','argv':['python','bad\0value']}}),
])
def test_invalid_delegated_check_is_refused(tmp_path,role,checks):
    config={'schema':'tare.tools/native-agent-config/1','role':role,
        'repositories':{'repo':str(tmp_path)},'writable':{},'verifiers':{},
        'capabilities':['read'],'delegated_verifiers':checks}
    with pytest.raises(ValueError):native_agent.configuration(config)


def test_delegated_checks_are_frozen_and_do_not_enable_explorer_effects(tmp_path):
    command=[sys.executable,'-m','pytest','tests/test_scoped.py','-q']
    config={'schema':'tare.tools/native-agent-config/1','role':'explorer',
        'repositories':{'repo':str(tmp_path)},'writable':{},'verifiers':{},
        'capabilities':['read'],'delegated_verifiers':{'check':{'repository':'repo','argv':command}}}
    saved=native_agent.configuration(config)
    selected=detected_checks(saved)
    selected['check']['argv'].append('--changed-by-caller')
    assert saved['delegated_verifiers']['check']['argv']==command
    assert saved['verifiers']=={}
    direct=copy.deepcopy(saved);direct['capabilities'].append('verify')
    with pytest.raises(ValueError):native_agent.configuration(direct)
    direct=copy.deepcopy(saved);direct['verifiers']=direct['delegated_verifiers']
    with pytest.raises(ValueError):native_agent.configuration(direct)


def test_real_coordinator_lifecycle_keeps_checks_and_read_only_tools_on_resume(tmp_path):
    project=tmp_path/'project';project.mkdir()
    custody=tmp_path/'session'
    command=[sys.executable,'-c','raise SystemExit("coordinator must not run this")']
    config={'schema':'tare.tools/native-agent-config/1','role':'explorer',
        'repositories':{'repo':str(project)},'writable':{},'verifiers':{},
        'capabilities':['read','list','search'],'specgraph_edit_context':False,
        'delegated_verifiers':{'check':{'repository':'repo','argv':command}}}
    client=Client(steps=[('tare_deliver',{'summary':'Ready to delegate the scoped check.'}),
                         ('tare_deliver',{'summary':'The configured check remains available.'})])
    first=native_agent.run_session(config,deployment(),custody,'Explain the next step.',
        conversation=True,client=client,local_counter=client)
    assert first['status']=='PROPOSED'
    saved=json.loads((custody/'native-agent.json').read_bytes())
    assert saved['conversation_flow'] is True
    assert detected_checks(saved['config'])==config['delegated_verifiers']
    assert saved['config']['verifiers']=={} and 'verify' not in saved['config']['capabilities']
    second=native_agent.run_session(None,None,custody,resume=True,follow_up='Continue explaining.',
        message_id='follow-up',client=client,local_counter=client)
    assert second['status']=='PROPOSED' and client.generations==2
    assert json.loads((custody/'native-agent.json').read_bytes())['config']==saved['config']


def test_leaf_explorer_cannot_silently_accept_delegated_checks(tmp_path):
    project=tmp_path/'project';project.mkdir()
    config={'schema':'tare.tools/native-agent-config/1','role':'explorer',
        'repositories':{'repo':str(project)},'writable':{},'verifiers':{},
        'capabilities':['read'],'delegated_verifiers':{'check':{
            'repository':'repo','argv':[sys.executable,'-m','pytest']}}}
    with pytest.raises(ValueError,match='require_conversation'):
        native_agent.run_session(config,deployment(),tmp_path/'session','Explore only.')
    assert not (tmp_path/'session').exists()
