"""Progress text must not finish a delegated implementation before tools run."""
import json
import pytest
from relay import native_agent
from test_native_agent import setup,Client,deployment,steps  # noqa:F401


class ProgressClient(Client):
    def __init__(self,repeat=False):
        super().__init__(steps=steps())
        self.repeat=repeat

    def post(self,path,body,control):
        if path=='/chat/completions' and (not self.generations or self.repeat):
            self.calls.append((path,body));self.generations+=1
            return {'model':'fixture','usage':{'prompt_tokens':10,'completion_tokens':5,'total_tokens':15},
                'choices':[{'finish_reason':'stop','message':{'role':'assistant',
                    'content':'Running the focused verifier first to inspect the preserved candidate.'}}]}
        return super().post(path,body,control)


def test_worker_continues_after_progress_text_and_delivers_after_real_tools(setup):
    config,custody,root=setup;client=ProgressClient()
    outcome=native_agent.run_session(config,deployment(),custody,'Change and verify the value.',
        delegated_leaf=True,client=client,local_counter=client,limits={'max_model_calls':8,'max_seconds':60})
    assert outcome['status']=='PROPOSED' and outcome['model_calls']==5
    assert (root/'a.py').read_bytes()==b'value = 2\n'
    assert outcome['output']=='Changed and verified the value.'
    events=[json.loads(line)['event'] for line in (custody/'journal.jsonl').read_text().splitlines()]
    replies=[e['reply'] for e in events if e['kind']=='model_returned']
    assert not replies[0]['proposes_delivery'] and replies[-1]['proposes_delivery']
    assert any(e['kind']=='effect_requested' and e['tool_call']['name']=='verify' for e in events)


def test_repeated_progress_text_stops_within_original_budget_without_delivery(setup):
    config,custody,root=setup;client=ProgressClient(repeat=True)
    outcome=native_agent.run_session(config,deployment(),custody,'Change and verify the value.',
        delegated_leaf=True,client=client,local_counter=client,limits={'max_model_calls':8,'max_seconds':60})
    assert outcome['status']=='BLOCKED' and outcome['reason']=='NO_PROGRESS'
    assert outcome['model_calls']<=3 and (root/'a.py').read_bytes()==b'value = 1\n'


@pytest.mark.parametrize('local',[False,True])
def test_worker_reserves_last_remote_call_for_honest_explicit_delivery(setup,monkeypatch,local):
    monkeypatch.setattr('relay.challenger.local',lambda _:local)
    config,custody,root=setup;config['verifiers']={}
    client=Client(steps=[('read',{'repository':'repo','path':'a.py'}),
        ('tare_deliver',{'summary':'Inspected only; the requested change remains unfinished.'})])
    outcome=native_agent.run_session(config,deployment(),custody,'Inspect before changing.',
        delegated_leaf=True,client=client,local_counter=client,limits={'max_model_calls':2,'max_seconds':60})
    last=[body for path,body in client.calls if path=='/chat/completions'][-1]
    assert last['tool_choice']==('auto' if local else {'type':'function','function':{'name':'tare_deliver'}})
    assert outcome['status']=='PROPOSED' and outcome['model_calls']==2
    assert 'unfinished' in outcome['output'] and (root/'a.py').read_bytes()==b'value = 1\n'
