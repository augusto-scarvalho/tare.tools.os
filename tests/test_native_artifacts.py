import hashlib,json
import pytest
from relay import native_agent, native_artifacts
from test_native_agent import setup, Client, run


def test_typed_handoff_roundtrip_under_deep_external_history(tmp_path):
    from compute_plane.native_journal import io_path
    from tare_tools_agent_runtime import ConversationLog
    root=tmp_path/('history-'+'x'*70)/('session-'+'y'*70)
    io_path(root).mkdir(parents=True)
    value={'summary':'A saved proposal','sources':[],'remaining':[]}
    journal=ConversationLog()
    journal.append({'kind':'model_returned','call_id':'fixture-call',
                    'reply':{'metadata_json':json.dumps({'delivery_payload':value})}})
    path=native_artifacts.save_output(root,'explorer','fixture-session','a'*64,journal)
    assert len(path)>260 and path.startswith(str(root))
    incoming=native_artifacts.read_input(path)
    assert incoming['artifact']['payload']==value and incoming['authority']=='NONE'
    assert native_agent.read_json(path)==incoming['artifact']
    assert incoming['source_verification']=='UNVERIFIED_IN_THIS_SESSION'
    assert json.loads(io_path(root/'artifact.json').read_text())==incoming['artifact']


def payload(role, root):
    source={'repository':'repo','path':'a.py','sha256':hashlib.sha256((root/'a.py').read_bytes()).hexdigest()}
    value={'summary':'Observed proposal','sources':[source],'remaining':[]}
    if role=='planner':value.update(steps=['Change a.py'],criteria=['Run the selected verifier'])
    if role in {'auditor','plan-auditor'}:value.update(verdict='ACCEPT',findings=[])
    return value


@pytest.mark.parametrize('role',list(native_artifacts.MODELS))
@pytest.mark.parametrize('explicit_null',[False,True])
def test_codex_object_codec_preserves_actual_role_handoffs(setup,role,explicit_null):
    from compute_plane.native_cli import cli_request
    from compute_plane.native_cli_codex import decode
    _,_,root=setup
    owner_schema=native_artifacts.schema(role)
    before=json.dumps(owner_schema,sort_keys=True)
    value=payload(role,root)
    wire=json.loads(json.dumps(value))
    if role=='planner':
        task=dict(key='change',title='Change source',summary='One change',cluster='fixture',
                  priority='P1',criteria=['Selected verifier passes'],target_paths=['a.py'],depends_on=[])
        value['tasks']=[task]
        wire['tasks']=[dict(task,verifiers=None,test_paths=None)]
    if role=='explorer':
        location=dict(repository='repo',path='a.py',line=1,claim='Observed source')
        value['locations']=[location]
        wire['locations']=[dict(location,status={'omitted':True})]
        if explicit_null: value['conclusion']=None
        wire['conclusion']={'value':None} if explicit_null else {'omitted':True}
    tool={'function':{'name':'tare_deliver','parameters':owner_schema}}
    request=cli_request(dict(model='fixture',messages=[],tools=[tool]),structured=True,native_arguments=True)
    catalog=json.loads(request['prompt'])['action_catalog']
    assert '$ref' not in json.dumps(catalog[0]['wire_arguments_schema'])
    decision=dict(text='',actions=[dict(action='tare_deliver',arguments=wire)])
    rows=[dict(type='turn.started'),dict(type='item.completed',item=dict(type='agent_message',text=json.dumps(decision))),
          dict(type='turn.completed')]
    decoded=decode(rows,'fixture',structured=True,argument_schemas={'tare_deliver':owner_schema},
                   argument_encoding=json.loads(request['prompt'])['argument_encoding'])
    call=decoded['choices'][0]['message']['tool_calls'][0]
    assert call['function']['name']=='tare_deliver'
    actual=json.loads(call['function']['arguments'])
    assert actual==value
    assert native_artifacts.validate(role,actual)==native_artifacts.validate(role,value)
    assert json.dumps(owner_schema,sort_keys=True)==before


@pytest.mark.parametrize('protocol',['openai-chat','bedrock-converse'])
@pytest.mark.parametrize('role',list(native_artifacts.MODELS))
def test_role_artifact_has_observed_sources_and_replay_preserves_it(setup,role,protocol):
    config,custody,root=setup
    config.update(role=role,typed_output=True,writable={},verifiers={})
    client=Client(protocol,[('read',{'repository':'repo','path':'a.py'}),('tare_deliver',payload(role,root))])
    result=run(config,custody,client)
    assert result['status']=='PROPOSED'
    value=json.loads((custody/'artifact.json').read_text())
    assert value['role']==role and value['authority']=='NONE' and value['payload']==payload(role,root)
    assert native_artifacts.read_input(result['artifact_file'])['source_verification']=='UNVERIFIED_IN_THIS_SESSION'
    replay=Client(protocol)
    repeated=native_agent.run_session(None,None,custody,resume=True,client=replay,local_counter=replay)
    assert repeated['artifact_file']==result['artifact_file'] and not replay.calls


@pytest.mark.parametrize('fault',['unread','wrong-hash','accept-with-findings','unknown-field'])
def test_false_source_and_invalid_review_never_become_an_artifact(setup,fault):
    config,custody,root=setup;config.update(role='auditor',typed_output=True,writable={},verifiers={})
    value=payload('auditor',root)
    if fault=='wrong-hash':value['sources'][0]['sha256']='f'*64
    if fault=='accept-with-findings':value['remaining']=['Unverified obligation']
    if fault=='unknown-field':value['authority']='ADMITTED'
    steps=[] if fault=='unread' else [('read',{'repository':'repo','path':'a.py'})]
    result=run(config,custody,Client(steps=steps+[('tare_deliver',value),('tare_deliver',value)]))
    assert result['status']=='BLOCKED' and not (custody/'artifact.json').exists()


@pytest.mark.parametrize('fault',['wrong-hash','unread'])
def test_auditor_can_correct_citations_once_with_existing_reads_and_call_budget(setup,fault):
    from copy import deepcopy
    config,custody,root=setup;config.update(role='auditor',typed_output=True,writable={},verifiers={})
    good=payload('auditor',root);bad=deepcopy(good)
    if fault=='wrong-hash':bad['sources'][0]['sha256']='f'*64
    else:
        bad['sources'].append({'repository':'repo','path':'unread.py','sha256':'a'*64})
        good.update(verdict='INCONCLUSIVE',remaining=['unread.py was not read; that obligation remains open.'])
    client=Client(steps=[('read',{'repository':'repo','path':'a.py'}),('tare_deliver',bad),('tare_deliver',good)])
    result=run(config,custody,client,limits={'max_model_calls':3})
    assert result['status']=='PROPOSED' and result['model_calls']==3
    assert json.loads((custody/'artifact.json').read_text())['payload']==good
    events=[json.loads(s)['event'] for s in (custody/'journal.jsonl').read_text().splitlines()]
    assert sum(e['kind']=='effect_requested' for e in events)==1  # no repeated discovery or effects
    recovery=next(json.loads(e['reply']['metadata_json'])['recovery'] for e in events
        if e['kind']=='model_returned' and 'recovery' in json.loads(e['reply']['metadata_json']))
    assert recovery['issues']==[{'field':'sources.'+('0' if fault=='wrong-hash' else '1'),
        'code':'native_artifact_source_not_observed'}]


def test_handoff_is_context_not_permissions(setup,tmp_path):
    config,custody,root=setup;config.update(role='planner',typed_output=True,writable={},verifiers={})
    result=run(config,custody,Client(steps=[('read',{'repository':'repo','path':'a.py'}),('tare_deliver',payload('planner',root))]))
    incoming=native_artifacts.read_input(result['artifact_file'])
    config.update(role='explorer',typed_output=False,input_artifacts=[incoming])
    client=Client(steps=[('tare_deliver',{'summary':'Reviewed handoff'})])
    result=run(config,tmp_path/'next',client)
    assert result['status']=='PROPOSED'
    wire=client.calls[-1][1]
    assert 'UNVERIFIED_IN_THIS_SESSION' in wire['messages'][1]['content']
    assert not any(t['function']['name']=='edit' for t in wire['tools'])


@pytest.mark.parametrize('protocol',['openai-chat','bedrock-converse'])
def test_invalid_delivery_explains_missing_field_in_result_and_display(setup,protocol):
    from relay.native_display import render_result
    config,custody,root=setup
    config.update(role='explorer',typed_output=True,writable={},verifiers={})
    value=payload('explorer',root);del value['remaining']
    client=Client(protocol,[('read',{'repository':'repo','path':'a.py'}),('tare_deliver',value)])
    result=run(config,custody,client,limits={'max_model_calls':2})
    assert result['status']=='BLOCKED' and result['reason']=='MODEL_FAILURE'
    assert result['failure_code']=='INVALID_DELIVERY'
    assert result['delivery_validation']['issues']==[{'field':'remaining','code':'missing'}]
    assert 'remaining: missing' in render_result(result)
    from relay import research_flow, parallel_explore
    research_view={'directory':'research','status':'BLOCKED','question':'question',
                   'sources':[],'session':str(custody),'result':result}
    parallel_view={'batch':'batch','status':'PAUSED','completed':0,'total':1,'workers':1,
                   'items':[{'id':'one','status':'BLOCKED','question':'question',
                             'session':str(custody),'result':result}]}
    for text in (research_flow.render(research_view),parallel_explore.render(parallel_view)):
        assert 'INVALID_DELIVERY' in text and 'remaining: missing' in text
    assert not (custody/'artifact.json').exists() and client.generations==2


@pytest.mark.parametrize('protocol',['openai-chat','bedrock-converse'])
def test_explorer_corrects_a_missing_location_path_without_repeating_discovery(setup,protocol):
    config,custody,root=setup
    config.update(role='explorer',typed_output=True,writable={},verifiers={})
    bad=payload('explorer',root)
    bad['locations']=[{'repository':'repo','line':1,'claim':'value is assigned here'}]
    good={**bad,'locations':[{**bad['locations'][0],'path':'a.py'}]}
    client=Client(protocol,[('read',{'repository':'repo','path':'a.py'}),
                            ('tare_deliver',bad),('tare_deliver',good)])
    result=run(config,custody,client,limits={'max_model_calls':3})
    assert result['status']=='PROPOSED' and result['model_calls']==3
    artifact=json.loads((custody/'artifact.json').read_text())
    assert artifact['payload']['locations'][0]['path']=='a.py'
    events=[json.loads(s)['event'] for s in (custody/'journal.jsonl').read_text().splitlines()]
    assert sum(e['kind']=='effect_requested' for e in events)==1
    replies=[json.loads(e['reply']['metadata_json']) for e in events if e['kind']=='model_returned']
    assert replies[1]['recovery']['issues']==[{'field':'locations.0.path','code':'missing'}]
    assert 'delivery_payload' not in replies[1]


def test_an_explorer_listing_places_gets_what_tare_can_derive(tmp_path):
    from types import SimpleNamespace
    (tmp_path/'a.py').write_text('one\ntwo\n', encoding='utf-8')
    check = native_artifacts.validator('explorer', SimpleNamespace(observations=lambda: {'reads': {}}),
                                       {'repo': str(tmp_path)})
    result = check({'locations': [{'repository': 'repo', 'path': './a.py', 'line': 2, 'claim': 'two'},
                                  {'repository': 'repo', 'path': 'a.py', 'line': 9, 'claim': 'beyond the file'}]})
    assert result['summary'] == '2 locations listed.' and result['sources'] == []
    assert result['locations'] == [{'repository': 'repo', 'path': 'a.py', 'line': 2, 'claim': 'two'}]
    assert result['remaining'] == ['No such line: a.py:9']
