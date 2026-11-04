"""Executable plans must reject ignored reports before the child spends its final call."""
import hashlib,json,subprocess
from copy import deepcopy
from relay import native_agent
from test_native_agent import Client,deployment,setup  # noqa: F401


def test_delegated_planner_receives_target_feedback_and_corrects_within_budget(setup):
    config,custody,root=setup
    subprocess.run(['git','init','-q',str(root)],check=True,capture_output=True)
    (root/'.gitignore').write_text('docs/operations/\n')
    config.update(role='planner',writable={},verifiers={},typed_output=True,capabilities=['read','list','search'])
    source={'repository':'repo','path':'a.py','sha256':hashlib.sha256(b'value = 1\n').hexdigest()}
    bad={'summary':'Plan the report.','steps':['Write the scoped report.'],'criteria':['Recoverable proof.'],
         'sources':[source],'remaining':[], 'tasks':[{'key':'report','title':'Write report','summary':'Bounded proof.',
         'cluster':'Proof','priority':'P2','criteria':['Recoverable proof.'],
         'target_paths':['docs/operations/report.md'],'depends_on':[],'verifiers':[],'test_paths':[]}]}
    good=deepcopy(bad);good['tasks'][0]['target_paths']=['docs/history/report.md']
    client=Client(steps=[('read',{'repository':'repo','path':'a.py'}),('tare_deliver',bad),('tare_deliver',good)])
    result=native_agent.run_session(config,deployment(role='planner'),custody,'Plan a versioned report.',
        client=client,local_counter=client,delegated_leaf=True,limits={'max_model_calls':3,'max_seconds':120})
    assert result['status']=='PROPOSED' and result['model_calls']==3
    assert json.loads((custody/'artifact.json').read_bytes())['payload']==good
    calls=[body for path,body in client.calls if path=='/chat/completions']
    assert 'Git ignores' in json.dumps(calls[-1]) and 'budget_notice' in json.dumps(calls[1])
    assert not (root/'docs').exists()
