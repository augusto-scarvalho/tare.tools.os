import json
from relay import conversation_tasks as tasks,planning_tools
from test_conversation_tasks import host,effect,intake,planned  # noqa: F401


def test_reopen_pending_task_with_null_delivery_preserves_record_and_dispatches_nothing(host):
    tools,control,_,seen=host
    intake(host)
    value=tasks.load(tools.root)
    tasks.current(value)['delivery']=None
    tasks.save(tools.root,value)
    before=(tools.root/'conversation-tasks.json').read_bytes()
    reopened=tasks.ConversationTools(tools.tools,tools.root,tools.state,'turn-2')
    assert tasks.current(tasks.load(reopened.root))['delivery'] is None
    assert (tools.root/'conversation-tasks.json').read_bytes()==before and seen==[]


def test_misspelled_intake_gets_actual_frozen_schema_without_admitting_it(host):
    tools,control,_,seen=host
    original=tools.tool_specs_json
    result=tools.execute(effect('demand',{'intent':'deliver','relation':'new','objec':'Fix it.','criteria':['Correct.']}),control)
    body=json.loads(result.output_json)
    assert result.status=='refused' and body['correction']['unknown_fields']==['objec']
    assert 'objective' in body['correction']['parameters']['properties']
    assert tools.tool_specs_json==original and seen==[]
    assert tasks.current(tasks.load(tools.root)) is None


def test_oversized_planning_gets_size_and_schema_without_copying_payload_or_dispatch(host):
    tools,control,_,seen=host;intake(host)
    specs=json.loads(tools.tool_specs_json);specs['delegate_plan']=planning_tools.SPEC['delegate_plan']
    tools.tool_specs_json=json.dumps(specs);original=tools.tool_specs_json
    result=tools.execute(effect('delegate_plan',{'instruction':'Plan the saved goal.','context':'private-marker-'*700}),control)
    body=json.loads(result.output_json);repair=body['correction']
    assert result.status=='refused' and repair['arguments_bytes']>repair['max_arguments_bytes']==8192
    assert repair['unknown_fields']==[] and 'context' in repair['parameters']['properties']
    assert 'private-marker' not in result.output_json and seen==[] and tools.tool_specs_json==original


def test_oversized_implementation_note_reports_the_actual_bound_without_starting_work(host,monkeypatch):
    from relay import work_units
    tools,control,_,seen=host;intake(host);planned(host)
    original=tools.tool_specs_json
    calls=[]
    monkeypatch.setattr(work_units,'run_unit',lambda *a,**kw:calls.append(True))
    result=tools.execute(effect('implement',{'note':'private-note-'*170,'effort':'high'}),control)
    body=json.loads(result.output_json)
    assert result.status=='refused' and body['correction']['unknown_fields']==[]
    assert body['correction']['note_characters']==2210 and body['correction']['max_note_characters']==2000
    assert 'No worker, check or auditor started' in body['next']
    assert 'private-note-' not in result.output_json and not calls and seen==[]
    assert tools.tool_specs_json==original
