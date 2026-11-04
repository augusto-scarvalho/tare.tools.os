"""Corrective input after cancellation uses the same saved conversation, not replay."""
import json
import pytest
from compute_plane.native_journal import NativeJournal
from tare_tools_agent_runtime import CancellationToken, NativeBudget
from relay import native_agent, native_messages
from test_native_agent import setup, deployment, Client


@pytest.mark.parametrize('queued',[False,True])
def test_cancelled_conversation_accepts_correction_with_saved_prefix_limits_and_inbox(setup,queued,monkeypatch):
    config,custody,project=setup
    config.update(writable={},verifiers={},capabilities=['read','list','search'])
    instant=[1000.0]
    monkeypatch.setattr(native_agent.time,'time',lambda:instant[0])
    cancel=CancellationToken()
    first=Client(steps=[('read',{'repository':'repo','path':'a.py'})],after_model=cancel.cancel)
    stopped=native_agent.run_session(config,deployment(),custody,'Use the available tools.',conversation=True,
        accept_messages=True,budget=NativeBudget(max_model_calls=4),cancellation=cancel,client=first,local_counter=first)
    assert stopped['status']=='CANCELLED'
    prior=[r.event() for r in NativeJournal(custody/'journal.jsonl').snapshot().records]
    original=json.loads((custody/'native-agent.json').read_text())
    instant[0]+=600  # Completed human wait must not consume the active allowance.
    if queued:native_messages.enqueue(custody,'Earlier user correction',message_id='queued')
    second=Client(steps=[('tare_deliver',{'summary':'Received your correction; old action was not run.'})])
    result=native_agent.run_session(None,None,custody,resume=True,follow_up='Use configured APIs, not API client implementation.',
        message_id='new-message',client=second,local_counter=second)
    assert result['status']=='PROPOSED' and result['model_calls']==2
    rows=[r.event() for r in NativeJournal(custody/'journal.jsonl').snapshot().records]
    assert rows[:len(prior)]==prior and not any(r['kind']=='effect_requested' for r in rows)
    assert [r['message_id'] for r in rows if r['kind']=='user_message']==(['queued','new-message'] if queued else ['new-message'])
    after=json.loads((custody/'native-agent.json').read_text())
    assert all(after[k]==original[k] for k in ('session_id','budget','limits','timeout','deployment','prompt_capsule','conversation_tool_specs'))
    assert after['deadline_utc']==original['deadline_utc']+600
    assert after['conversation_idle_clock']['excluded_seconds']==600
    wire=[body for path,body in second.calls if path=='/chat/completions'][0]
    prefix=[body for path,body in first.calls if path=='/chat/completions'][0]
    assert wire['messages'][:len(prefix['messages'])]==prefix['messages'] and wire['tools']==prefix['tools']
    assert 'CANCELLED_BEFORE_EXECUTION' in json.dumps(wire)


@pytest.mark.parametrize('cleanup',[True,False])
@pytest.mark.parametrize('code',['HTTP_CANCELLED','CLI_INCOMPLETE_EXCHANGE'])
def test_correction_after_cancelled_request_requires_proven_local_cleanup(setup,cleanup,code):
    from compute_plane.native_models import NativeHttpClient, NativeTransportInterrupted
    config,custody,project=setup
    config.update(writable={},verifiers={},capabilities=['read','list','search'])
    class Interrupted(Client,NativeHttpClient):
        def post_observed(self,path,body,control):
            if path.endswith('/chat/completions'):
                self.generations+=1
                raise NativeTransportInterrupted(code,{'started':True,'cleanup_verified':cleanup,
                    'remote_stop':'unknown','http_attempts_upper_bound':1})
            return self.post(path,body,control),{'started':True,'cleanup_verified':True}
    first=Interrupted()
    initial=native_agent.run_session(config,deployment(),custody,'Read the project.',conversation=True,
        budget=NativeBudget(max_model_calls=4),client=first,local_counter=first)
    assert initial['pending_id']
    second=Client(steps=[('tare_deliver',{'summary':'Corrective input received.'})])
    if not cleanup:
        with pytest.raises(ValueError,match='completed proposal'):
            native_agent.run_session(None,None,custody,resume=True,follow_up='Stop that and answer this.',
                message_id='new',client=second,local_counter=second)
        assert second.generations==0
        return
    result=native_agent.run_session(None,None,custody,resume=True,follow_up='Stop that and answer this.',
        message_id='new',client=second,local_counter=second)
    assert result['status']=='PROPOSED' and result['model_calls']==2
    rows=[r.event() for r in NativeJournal(custody/'journal.jsonl').snapshot().records]
    assert sum(r['kind']=='model_abandoned' for r in rows)==1
    assert not any(r['kind']=='effect_requested' for r in rows)


def test_new_conversation_message_refreshes_host_applied_files_without_manual_refresh(setup):
    config,custody,project=setup
    config.update(writable={},verifiers={},capabilities=['read','list','search'])
    first=Client(steps=[('read',{'repository':'repo','path':'a.py'}),('tare_deliver',{'summary':'Read source.'})])
    native_agent.run_session(config,deployment(),custody,'Read the source.',conversation=True,client=first,local_counter=first)
    before=(custody/'journal.jsonl').read_bytes()
    (project/'a.py').write_text('value = 2\n')  # Host-applied candidate, or a user edit between messages.
    fresh=Client(steps=[('read',{'repository':'repo','path':'a.py'}),('tare_deliver',{'summary':'Current value is two.'})])
    result=native_agent.run_session(None,None,custody,resume=True,follow_up='Now inspect the current change.',
        message_id='current-change',client=fresh,local_counter=fresh)
    assert result['status']=='PROPOSED' and (custody/'journal.jsonl').read_bytes().startswith(before)
    rows=[r.event() for r in NativeJournal(custody/'journal.jsonl').snapshot().records]
    correction=next(r for r in rows if r.get('message_id')=='current-change')
    assert 'Host-observed changes' in correction['text'] and 'Now inspect the current change.' in correction['text']
    assert result['model_calls']==4
