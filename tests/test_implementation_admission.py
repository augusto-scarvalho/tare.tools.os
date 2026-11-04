"""An exhausted implementation budget is diagnosed before requesting consent."""
import json,time

import pytest

from compute_plane.native_journal import NativeJournal
from relay import conversation_tasks as tasks
from test_conversation_tasks import effect,host,intake,planned  # noqa: F401


@pytest.mark.parametrize('exhausted',['calls','time'])
def test_preflight_preserves_plan_without_asking_consent_or_starting_a_child(host,monkeypatch,exhausted):
    tools,control,_,_=host
    intake(host);plan=planned(host)['plan']
    log=NativeJournal(tools.root/'journal.jsonl')
    log.append({'kind':'session_started','session_id':tools.session_id})
    for n in range(8):log.append({'kind':'model_requested','call_id':str(n)})
    if exhausted=='calls':tools.state['limits']['max_model_calls']=12
    else:tools.state['deadline_utc']=time.time()+119
    def forbidden(*a,**kw):pytest.fail('No permission or implementer dispatch should occur')
    monkeypatch.setattr('relay.conversation_permissions.authorize',forbidden)
    monkeypatch.setattr('relay.work_units.run_unit',forbidden)
    result=tools.execute(effect('implement',{},key='implementation'),control)
    assert result.status=='refused' and 'too little left for a child' in result.output_json
    saved=tasks.current(tasks.load(tools.root))
    assert saved['stage']=='PENDING' and saved['plan']==plan
    assert 'delivery_invocation' not in saved and not (tools.root/'units').exists()
