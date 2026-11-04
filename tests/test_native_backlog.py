"""Direct TUI conversations record project progress without terminal permission."""
import json
import hashlib

import pytest

from compute_plane.native_journal import NativeJournal
from graph_backlog.jsonutil import compute_revision_hash
from relay import native_agent
from relay.project_graph import observe
from test_native_agent import Client, deployment, setup
from test_backlog_delivery import record, read, source, save
from test_work_console import graph_root


def bind(setup, graph_root):
    config, custody, root = setup
    path = root/'work-graph.json'
    path.write_bytes((graph_root/'work-graph.json').read_bytes())
    config['project_graph'] = {'repository': 'repo', 'path': str(path)}
    config['writable'] = {'repo': ['work-graph.json']}
    config['capabilities'] = ['read', 'edit', 'list']
    return config, custody, path


def events(custody):
    return [r.event() for r in NativeJournal(custody/'journal.jsonl').snapshot().records]


@pytest.mark.parametrize('protocol', ['openai-chat', 'bedrock-converse'])
def test_direct_progress_then_delivery_and_resume_without_shell(setup, graph_root, protocol):
    config, custody, path = bind(setup, graph_root)
    original = read(path)
    def steps():
        yield 'backlog_read', {'work_id': 'ready'}
        yield 'backlog_record', {'record': record(remaining=['One real obligation.']),
                                'expected_revision': compute_revision_hash(read(path))}
        yield 'backlog_read', {'work_id': 'ready'}
        yield 'backlog_record', {'record': record(), 'expected_revision': compute_revision_hash(read(path))}
        yield 'tare_deliver', {'summary': 'Progress and delivery recorded.'}
    client = Client(protocol, steps())
    result = native_agent.run_session(config, deployment(protocol), custody, 'Deliver ready and track progress.',
                                     client=client, local_counter=client)
    assert result['status'] == 'PROPOSED'
    activities = result['tool_activity']['items']
    assert len(activities) == 4 and all(a['target'] == 'ready' and a['state'] == 'succeeded' for a in activities)
    graph = read(path)
    node = graph['nodes'][0]
    assert node['completion']['status'] == 'DONE'
    assert node['work_status'] == 'done'
    assert node['admission_state'] == original['nodes'][0]['admission_state']
    assert graph['nodes'][1:] == original['nodes'][1:] and graph['edges'] == original['edges']
    history = node['metrics']['delivery_history']
    assert len(history) == 2 and history[1]['previous_completion']['residuals'] == ['One real obligation.']
    assert all(h['operation']['session_id'] == result['session_id'] for h in history)
    assert 'DONE' in observe(str(path))['summary']
    receipts = [json.loads(e['result']['output_json']) for e in events(custody)
                if e['kind'] == 'effect_returned']
    recorded = [r for r in receipts if r.get('recorded') == 'ready']
    assert [(r['completion_status'], r['work_status'], r['remaining']) for r in recorded] == [
        ('PARTIAL', 'IN_PROGRESS', ['One real obligation.']), ('DONE', 'done', [])]
    before = path.read_bytes()
    next_client = Client(protocol, [('backlog_read', {'work_id': 'ready'}), ('tare_deliver', {'summary': 'Still recorded.'})])
    native_agent.run_session(None, None, custody, resume=True, follow_up='Inspect the recorded node.',
                             message_id='inspect', client=next_client, local_counter=next_client)
    assert path.read_bytes() == before
    assert 'backlog_record' in json.dumps(next_client.calls)


@pytest.mark.parametrize('restriction', ['read-only', 'outside-scope', 'external-graph'])
def test_graph_binding_does_not_grant_edit_authority(setup, graph_root, restriction):
    config, custody, path = bind(setup, graph_root)
    if restriction == 'read-only':
        config.update(role='explorer', writable={}, verifiers={}, capabilities=['read', 'list'])
    elif restriction == 'outside-scope':
        config['writable'] = {'repo': ['a.py']}
    else:
        path = graph_root/'work-graph.json'
        config['project_graph']['path'] = str(path)
    before = path.read_bytes()
    client = Client(steps=[('backlog_read', {'work_id': 'ready'}), ('tare_deliver', {'summary': 'Read only.'})])
    native_agent.run_session(config, deployment(), custody, 'Inspect.', client=client, local_counter=client)
    calls = [b for p, b in client.calls if p.endswith('/completions')]
    names = {t['function']['name'] for t in calls[0]['tools']}
    assert 'backlog_read' in names and 'backlog_record' not in names
    outputs = [json.loads(e['result']['output_json']) for e in events(custody) if e['kind'] == 'effect_returned']
    assert outputs[0]['can_record'] is False
    assert path.read_bytes() == before


@pytest.mark.parametrize('fault', ['stale', 'managed', 'missing-evidence', 'missing-grade',
                                   'missing-reason', 'A-without-source', 'B-without-source',
                                   'unknown-source', 'bad-source-type'])
def test_refused_record_preserves_graph(setup, graph_root, fault):
    config, custody, path = bind(setup, graph_root)
    raw = read(path)
    if fault == 'managed':
        raw['nodes'][0]['metrics']['verification_checks'] = [{'criterion': 'unit'}]
        path.write_text(json.dumps(raw), encoding='utf-8')
    row = record()
    if fault == 'missing-evidence':
        row['evidence'] = []
    if fault == 'missing-grade':
        row.pop('evidence_grade')
    elif fault == 'missing-reason':
        row.pop('evidence_grade_reason')
    elif fault in ('A-without-source', 'B-without-source'):
        row['evidence_grade'] = fault[0]
    elif fault == 'unknown-source':
        row['source_refs'] = ['not-registered']
    elif fault == 'bad-source-type':
        row['source_refs'] = 42
    revision = 'stale' if fault == 'stale' else compute_revision_hash(raw)
    before = path.read_bytes()
    client = Client(steps=[('backlog_record', {'record': row, 'expected_revision': revision}),
                           ('tare_deliver', {'summary': 'Refusal reported.'})])
    native_agent.run_session(config, deployment(), custody, 'Record delivery.', client=client, local_counter=client)
    result = next(e['result'] for e in events(custody) if e['kind'] == 'effect_returned')
    assert result['status'] == 'refused' and 'reason' in json.loads(result['output_json'])
    assert path.read_bytes() == before


@pytest.mark.parametrize('committed', [True, False])
def test_resume_reconciles_interrupted_record_without_repeating_write(setup, graph_root, monkeypatch, committed):
    config, custody, path = bind(setup, graph_root)
    client = Client(steps=[('backlog_record', {'record': record(remaining=['Qualification remains.']),
                            'expected_revision': compute_revision_hash(read(path))}),
                          ('tare_deliver', {'summary': 'Resumed.'})])
    if committed:
        original = NativeJournal.append
        def crash(self, event):
            if event['kind'] == 'effect_returned':
                raise SystemExit('after graph commit, before journal receipt')
            return original(self, event)
        target, name = NativeJournal, 'append'
    else:
        from relay import backlog_delivery
        def crash(*args, **kwargs):
            raise SystemExit('before graph commit')
        target, name = backlog_delivery, 'apply'
    with monkeypatch.context() as injected:
        injected.setattr(target, name, crash)
        with pytest.raises(SystemExit):
            native_agent.run_session(config, deployment(), custody, 'Track progress.', client=client, local_counter=client)
    before, stamp = path.read_bytes(), path.stat().st_mtime_ns
    result = native_agent.run_session(None, None, custody, resume=True, client=client, local_counter=client)
    assert result['status'] == 'PROPOSED'
    assert path.read_bytes() == before and path.stat().st_mtime_ns == stamp
    receipts = [e['result'] for e in events(custody) if e['kind'] == 'effect_returned']
    assert receipts[0]['status'] == ('succeeded' if committed else 'refused')
    assert sum(e['kind'] == 'effect_requested' for e in events(custody)) == 1


@pytest.mark.parametrize('context', ['small', 'sources', 'node'])
@pytest.mark.parametrize('external', [False, True])
def test_tool_read_exposes_only_referenced_context_with_shared_byte_limit(setup, graph_root, context, external):
    config, custody, path = bind(setup, graph_root)
    if external:
        path = graph_root/'work-graph.json'
    else:
        nested = path.parent/'backlogs'/'work-graph.json'
        nested.parent.mkdir()
        path.rename(nested)
        path = nested
        config['writable'] = {'repo': ['backlogs/work-graph.json']}
    config['project_graph']['path'] = str(path)
    raw = read(path)
    raw['sources'] = {'report': source(), 'unrelated': {**source(), 'title': 'z'*30000}}
    node = raw['nodes'][0]
    node['source_refs'] = ['report']
    node['provenance'] = ['Historical reviewed reference.']
    node['completion']['dod_evidence'] = ['Recorded evidence, not independent attestation.']
    node['metrics']['delivery_latest'] = record(remaining=['Review remains.'])
    if context == 'sources':
        node['source_refs'] = ['source-'+str(i) for i in range(52)]
        raw['sources'].update({ref: {**source(), 'title': '界'*800}
                               for ref in node['source_refs']})
    elif context == 'node':
        node['provenance'].append('界'*9000)
    save(path, raw)
    before, stamp = path.read_bytes(), path.stat().st_mtime_ns
    client = Client(steps=[('backlog_read', {'work_id': 'ready'}),
                           ('tare_deliver', {'summary': 'Node read attempted; source contents not reviewed.'})])
    native_agent.run_session(config, deployment(), custody, 'Inspect the node context.',
                             client=client, local_counter=client)
    result = next(e['result'] for e in events(custody) if e['kind'] == 'effect_returned')
    output = json.loads(result['output_json'])
    assert len(result['output_json'].encode()) <= 24576
    if context == 'node':
        assert result['status'] == 'refused'
        assert output['reason'] == (
            'Node response exceeds 24 KiB even without source records. '
            'Read selected JSON fields from '+str(path))
    else:
        assert result['status'] == 'succeeded'
        assert output['path'] == str(path)
        assert output['revision'] == compute_revision_hash(raw)
        assert output['can_record'] is (not external)
        assert output['authority'] == 'NONE'
        assert output['scoped_project_instructions'] == ([] if external else [{
            'repository': 'repo', 'path': 'backlogs/AGENTS.md', 'sha256': None, 'text': None}])
        assert output['node'] == {key: node.get(key) for key in (
            'id', 'canonical_system', 'canonical_id', 'canonical_revision',
            'title', 'kind', 'summary', 'readiness', 'work_status', 'exit_criteria',
            'completion', 'admission_state', 'provenance', 'source_refs')}
        assert output['node']['canonical_revision'] == node['canonical_revision'] == 'r1'
        assert output['node']['canonical_revision'] != output['revision']
        assert output['delivery'] == node['metrics']['delivery_latest']
        if context == 'sources':
            assert output['sources'] == {}
            assert output['sources_complete'] is False
            assert output['sources_omitted'] == 52
            assert output['sources_location'] == {'path': str(path), 'json_pointer': '/sources'}
            assert 'omitted sources have not been inspected' in output['sources_notice']
            assert 'No new authority or permission' in output['sources_notice']
            if external:
                assert 'sources_read' not in output
                assert 'external paths require existing folder access' in output['sources_notice']
            else:
                assert output['sources_read'] == {
                    'repository': 'repo', 'path': 'backlogs/work-graph.json', 'json_pointer': '/sources'}
        else:
            assert output['sources'] == {'report': source()}
            assert set(output) == {'path', 'revision', 'can_record', 'node', 'sources',
                                   'delivery', 'scoped_project_instructions', 'authority'}
        assert 'unrelated' not in output['sources']
        assert 'z'*30000 not in result['output_json']
    assert path.read_bytes() == before and path.stat().st_mtime_ns == stamp


def test_tool_record_schema_declares_optional_evidence_without_automatic_grade():
    from relay.native_backlog import RECORD, SPECS
    assert set(RECORD['required']) == {'work_id', 'summary', 'evidence', 'remaining', 'criteria'}
    assert RECORD['properties']['evidence_grade']['enum'] == ['A', 'B', 'C', 'D']
    assert RECORD['properties']['source_refs']['items']['type'] == 'string'
    assert RECORD['properties']['evidence_grade_reason']['minLength'] == 1
    assert RECORD['additionalProperties'] is False
    assert 'No automatic grade' in SPECS['backlog_record']['description']


def test_node_read_preserves_analysis_only_scope_instead_of_exposing_only_a_generic_title(setup,graph_root):
    config,custody,path=bind(setup,graph_root)
    raw=read(path)
    summary='Architecture analysis only. implementation_authority: NONE. A functioning deployment is outside this task.'
    raw['nodes'][0].update(title='Owner console',summary=summary)
    save(path,raw)
    client=Client(steps=[('backlog_read',{'work_id':'ready'}),('tare_deliver',{'summary':'Analysis scope inspected.'})])
    native_agent.run_session(config,deployment(),custody,'Inspect the task scope.',client=client,local_counter=client)
    result=next(e['result'] for e in events(custody) if e['kind']=='effect_returned')
    assert result['status']=='succeeded' and json.loads(result['output_json'])['node']['summary']==summary


@pytest.mark.parametrize('external',[False,True])
def test_selected_source_records_remain_readable_when_the_bound_graph_exceeds_the_file_reader_limit(setup,graph_root,external):
    from compute_plane.mediated_tools import MAX_READ_BYTES
    config,custody,path=bind(setup,graph_root)
    if external:
        path=graph_root/'work-graph.json'; config['project_graph']['path']=str(path)
    raw=read(path)
    refs=['source-'+str(i) for i in range(52)]
    raw['nodes'][0]['source_refs']=refs
    raw['sources']={ref:{**source(),'title':'界'*800} for ref in refs}
    raw['sources']['unrelated']={**source(),'title':'x'*MAX_READ_BYTES}
    save(path,raw)
    assert path.stat().st_size>MAX_READ_BYTES
    before=path.read_bytes()
    steps=[('backlog_read',{'work_id':'ready'})]
    if not external:
        steps.append(('read',{'repository':'repo','path':'work-graph.json','json_pointer':'/sources'}))
    steps.extend([('backlog_read',{'work_id':'ready','source_ids':[refs[0],refs[-1]]}),
        ('tare_deliver',{'summary':'Selected referenced sources inspected.'})])
    client=Client(steps=steps)
    native_agent.run_session(config,deployment(),custody,'Inspect referenced source records.',client=client,local_counter=client)
    results=[e['result'] for e in events(custody) if e['kind']=='effect_returned']
    assert [r['status'] for r in results]==(['succeeded','succeeded'] if external else ['succeeded','refused','succeeded'])
    if not external:
        assert 'file_too_large_request_another_source' in results[1]['output_json']
    first,selected=[json.loads(r['output_json']) for r in (results[0],results[-1])]
    assert first['sources_complete'] is False and 'sources_read' not in first
    assert first['sources_request']=={'work_id':'ready','source_ids':refs[:8]}
    assert selected['sources']=={ref:raw['sources'][ref] for ref in [refs[0],refs[-1]]}
    assert selected['source_ids_selected']==[refs[0],refs[-1]]
    assert selected['sources_complete'] is False and selected['sources_omitted']==50
    assert first['node']==selected['node'] and first['revision']==selected['revision']==compute_revision_hash(raw)
    assert first['node']['canonical_system']==selected['node']['canonical_system']=='local'
    assert first['node']['canonical_id']==selected['node']['canonical_id']==raw['nodes'][0]['canonical_id']
    assert first['node']['canonical_revision']==selected['node']['canonical_revision']=='r1'
    assert selected['node']['canonical_revision']!=selected['revision']
    assert selected['can_record'] is (not external) and selected['authority']=='NONE'
    assert all(len(r['output_json'].encode())<=24576 for r in results)
    assert path.read_bytes()==before


@pytest.mark.parametrize('selection',[None,[],['unrelated'],['missing'],['report']*9,[42]])
def test_source_selection_rejects_unreferenced_or_invalid_ids_without_graph_changes(setup,graph_root,selection):
    config,custody,path=bind(setup,graph_root)
    raw=read(path); raw['nodes'][0]['source_refs']=['report']
    raw['sources']={'report':source(),'unrelated':source()}; save(path,raw)
    before=path.read_bytes()
    client=Client(steps=[('backlog_read',{'work_id':'ready','source_ids':selection}),
        ('tare_deliver',{'summary':'Selection refusal reported.'})])
    native_agent.run_session(config,deployment(),custody,'Inspect.',client=client,local_counter=client)
    result=next(e['result'] for e in events(custody) if e['kind']=='effect_returned')
    assert result['status']=='refused'
    assert 'source_ids' in json.loads(result['output_json'])['reason']
    assert path.read_bytes()==before


@pytest.mark.parametrize('selection',[None,['report'],['report-'+str(i) for i in range(8)]])
def test_codex_codec_preserves_optional_source_selection_without_expanding_the_schema(selection):
    from compute_plane.native_cli import cli_request
    from compute_plane.native_cli_codex import decode
    from relay.native_backlog import SPECS
    schema=SPECS['backlog_read']['parameters']
    body=cli_request(dict(model='fixture',messages=[],tools=[{'function':{'name':'backlog_read','parameters':schema}}]),
                     structured=True,native_arguments=True)
    decision=dict(text='',actions=[dict(action='backlog_read',arguments=dict(work_id='ready',source_ids=selection))])
    rows=[dict(type='turn.started'),dict(type='item.completed',item=dict(type='agent_message',text=json.dumps(decision))),
          dict(type='turn.completed')]
    decoded=decode(rows,'fixture',structured=True,argument_schemas={'backlog_read':schema},
                   argument_encoding=json.loads(body['prompt'])['argument_encoding'])
    function=decoded['choices'][0]['message']['tool_calls'][0]['function']
    assert function['name']=='backlog_read'
    assert json.loads(function['arguments'])==({'work_id':'ready'} if selection is None else {'work_id':'ready','source_ids':selection})


def test_individually_oversized_selected_source_is_explicitly_refused_with_the_original_response_bound(setup,graph_root):
    config,custody,path=bind(setup,graph_root)
    raw=read(path); raw['nodes'][0]['source_refs']=['report']
    raw['sources']={'report':{**source(),'title':'x'*30000}}; save(path,raw)
    before=path.read_bytes()
    client=Client(steps=[('backlog_read',{'work_id':'ready','source_ids':['report']}),
        ('tare_deliver',{'summary':'Individual source size limit reported.'})])
    native_agent.run_session(config,deployment(),custody,'Inspect.',client=client,local_counter=client)
    result=next(e['result'] for e in events(custody) if e['kind']=='effect_returned')
    assert result['status']=='refused' and 'Selected source record exceeds the 24 KiB' in result['output_json']
    assert len(result['output_json'].encode())<=24576 and path.read_bytes()==before


@pytest.mark.parametrize('grade',['A','B'])
@pytest.mark.parametrize('addressed',[False,True])
def test_observed_report_registration_and_delivery_share_one_transaction_and_replay_is_exact(setup,graph_root,grade,addressed):
    config,custody,path=bind(setup,graph_root)
    if addressed: save(path,read(path))
    original=read(path)
    report=path.parent/'report.md'; report.write_text('unit: verifier and independent report passed\n',encoding='utf-8')
    sha=hashlib.sha256(report.read_bytes()).hexdigest()
    row=record(); row.update(evidence_grade=grade,evidence_grade_reason='Historical verifier report reviewed; no new run.')
    files=[{'repository':'repo','path':'report.md','sha256':sha}]
    first=[]
    def steps():
        yield 'read',{'repository':'repo','path':'report.md'}
        yield 'backlog_read',{'work_id':'ready'}
        yield 'backlog_record',{'record':row,'expected_revision':compute_revision_hash(read(path)),'source_files':files}
        first.append((path.read_bytes(),path.stat().st_mtime_ns))
        yield 'backlog_record',{'record':row,'expected_revision':compute_revision_hash(read(path)),'source_files':files}
        yield 'tare_deliver',{'summary':'Observed report registered with reviewed delivery.'}
    client=Client(steps=steps())
    native_agent.run_session(config,deployment(),custody,'Record reviewed delivery and register the inspected report.',client=client,local_counter=client)
    current=read(path); added=set(current['sources'])-set(original['sources'])
    assert len(added)==1 and all(current['sources'][k]==v for k,v in original['sources'].items())
    key=next(iter(added)); source_record=current['sources'][key]
    assert source_record['locator']==report.resolve().as_posix() and source_record['sha256']==sha
    assert source_record['authority']=='NONE' and 'no producer attestation' in source_record['verification']
    assert key in current['nodes'][0]['source_refs'] and current['nodes'][0]['completion']['evidence_grade']==grade
    assert current['nodes'][0]['admission_state']==original['nodes'][0]['admission_state']
    assert current['nodes'][1:]==original['nodes'][1:] and current['edges']==original['edges']
    assert len(current['nodes'][0]['metrics']['delivery_history'])==1
    assert (path.read_bytes(),path.stat().st_mtime_ns)==first[0]
    registered=[json.loads(e['result']['output_json'])['registered_sources'] for e in events(custody)
                if e['kind']=='effect_returned' and 'registered_sources' in json.loads(e['result']['output_json'])]
    assert registered==[[key],[key]]
    function=next(tool['function'] for _,body in client.calls for tool in body.get('tools',[])
                  if tool['function']['name']=='backlog_record')
    assert function['parameters']['properties']['source_files']['items']['properties']['repository']['enum']==['repo']


@pytest.mark.parametrize('fault',['unread','changed','late-change','forged','managed','missing-grade','stale',
                                  'bad-refs','self-graph','read-only','empty-files','wrong-files'])
def test_source_registration_refusals_never_partially_write_sources_or_delivery(setup,graph_root,monkeypatch,fault):
    config,custody,path=bind(setup,graph_root)
    if fault=='managed':
        raw=read(path); raw['nodes'][0]['metrics']['verification_checks']=[{'criterion':'unit'}]; save(path,raw)
    if fault=='read-only':
        config.update(role='explorer',writable={},verifiers={},capabilities=['read','list'])
    report=path if fault=='self-graph' else path.parent/'report.md'
    if fault!='self-graph': report.write_text('original verifier report\n',encoding='utf-8')
    sha=hashlib.sha256(report.read_bytes()).hexdigest()
    row=record(); row.update(evidence_grade='A',evidence_grade_reason='Original report reviewed.')
    if fault=='missing-grade': row.pop('evidence_grade')
    if fault=='bad-refs': row['source_refs']=[{}]
    files=[{'repository':'repo','path':report.name,'sha256':'e'*64 if fault=='forged' else sha}]
    if fault=='empty-files': files=[]
    if fault=='wrong-files': files='not-a-list'
    if fault=='late-change':
        from relay import backlog_delivery
        original=backlog_delivery.preview
        def changed_after_preview(*args,**kwargs):
            plan=original(*args,**kwargs); report.write_text('changed after preview\n',encoding='utf-8'); return plan
        monkeypatch.setattr(backlog_delivery,'preview',changed_after_preview)
    before,stamp=path.read_bytes(),path.stat().st_mtime_ns
    def steps():
        if fault!='unread': yield 'read',{'repository':'repo','path':report.name}
        yield 'backlog_read',{'work_id':'ready'}
        if fault=='changed': report.write_text('changed after observed read\n',encoding='utf-8')
        yield 'backlog_record',{'record':row,'expected_revision':'stale' if fault=='stale' else compute_revision_hash(read(path)),
                               'source_files':files}
        yield 'tare_deliver',{'summary':'Registration refusal reported; graph preserved.'}
    client=Client(steps=steps())
    native_agent.run_session(config,deployment(),custody,'Register the inspected report and record reviewed delivery.',client=client,local_counter=client)
    results=[e['result'] for e in events(custody) if e['kind']=='effect_returned']
    if fault=='read-only':
        assert not any(e['kind']=='effect_requested' and e['tool_call']['name']=='backlog_record' for e in events(custody))
    else:
        assert results[-1]['status']=='refused'
    assert path.read_bytes()==before and path.stat().st_mtime_ns==stamp


@pytest.mark.parametrize('committed',[False,True])
def test_source_registration_recovers_only_its_atomic_delivery_receipt(setup,graph_root,monkeypatch,committed):
    config,custody,path=bind(setup,graph_root)
    report=path.parent/'report.md'; report.write_text('unit: independently reviewed result\n',encoding='utf-8')
    sha=hashlib.sha256(report.read_bytes()).hexdigest()
    row=record(); row.update(evidence_grade='A',evidence_grade_reason='Original verifier report reviewed.')
    client=Client(steps=[('read',{'repository':'repo','path':'report.md'}),
        ('backlog_record',{'record':row,'expected_revision':compute_revision_hash(read(path)),
            'source_files':[{'repository':'repo','path':'report.md','sha256':sha}]}),
        ('tare_deliver',{'summary':'Recovered without repeating registration.'})])
    from relay import backlog_delivery
    with monkeypatch.context() as injected:
        if committed:
            original=NativeJournal.append
            def crash(self,event):
                if event['kind']=='effect_returned' and 'registered_sources' in json.loads(event['result']['output_json']):
                    raise SystemExit('after atomic source and delivery write')
                return original(self,event)
            injected.setattr(NativeJournal,'append',crash)
        else:
            def crash(*args,**kwargs): raise SystemExit('before atomic source and delivery write')
            injected.setattr(backlog_delivery,'apply',crash)
        with pytest.raises(SystemExit):
            native_agent.run_session(config,deployment(),custody,'Register observed source and record delivery.',client=client,local_counter=client)
    before,stamp=path.read_bytes(),path.stat().st_mtime_ns
    native_agent.run_session(None,None,custody,resume=True,client=client,local_counter=client)
    assert path.read_bytes()==before and path.stat().st_mtime_ns==stamp
    assert (read(path)['nodes'][0]['completion']['status']=='DONE') is committed


@pytest.mark.parametrize('files',[None,[{'repository':'repo','path':'report.md','sha256':'a'*64}]])
def test_codex_codec_keeps_source_registration_explicit_and_optional(files):
    from compute_plane.native_cli import cli_request
    from compute_plane.native_cli_codex import decode
    from relay.native_backlog import SPECS
    schema=SPECS['backlog_record']['parameters']
    row=record()
    body=cli_request(dict(model='fixture',messages=[],tools=[{'function':{'name':'backlog_record','parameters':schema}}]),
                     structured=True,native_arguments=True)
    arguments=dict(record={**row,'source_refs':None},expected_revision='revision',source_files=files)
    decision=dict(text='',actions=[dict(action='backlog_record',arguments=arguments)])
    rows=[dict(type='turn.started'),dict(type='item.completed',item=dict(type='agent_message',text=json.dumps(decision))),
          dict(type='turn.completed')]
    decoded=decode(rows,'fixture',structured=True,argument_schemas={'backlog_record':schema},
                   argument_encoding=json.loads(body['prompt'])['argument_encoding'])
    function=decoded['choices'][0]['message']['tool_calls'][0]['function']
    assert function['name']=='backlog_record'
    expected=dict(record=row,expected_revision='revision')
    if files is not None: expected['source_files']=files
    assert json.loads(function['arguments'])==expected


@pytest.mark.parametrize('conversation', [False, True])
@pytest.mark.parametrize('linked', [False, True])
def test_repository_path_contract_parent_workspace_and_resume(setup, graph_root, conversation, linked):
    config, custody, other = setup
    workspace = other.parent/'projects'
    workspace.mkdir()
    backlog_dir = workspace/'tare.tools.os'
    backlog_dir.mkdir()
    graph = backlog_dir/'work-graph.json'
    graph.write_bytes((graph_root/'work-graph.json').read_bytes())
    relative = '_archives/x03-legacy-reconciliation-20261015/attempts/recent-controls/packet.json'
    fixtures = ((workspace/relative, 'workspace-packet'),
                (backlog_dir/relative, 'wrong-backlog-packet'),
                (other/relative, 'other-root-packet'))
    for path, marker in fixtures:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'marker': marker}), encoding='utf-8')
    missing = 'tare.tools.os/tare.tools.os/' + relative
    config.update(repositories={'projects': str(workspace), 'repo': str(other)},
                  writable={'projects': ['tare.tools.os/work-graph.json'], 'repo': ['a.py']},
                  verifiers={}, capabilities=['read', 'edit', 'list'])
    if linked:
        config['project_graph'] = {'repository': 'projects', 'path': str(graph)}
    original = json.loads(json.dumps(config))
    before = {path: path.read_bytes() for path, _ in fixtures}
    before[graph] = graph.read_bytes()

    def first_packet(client):
        first = next(body for path, body in client.calls if path == '/chat/completions')
        # Inspect the actual first model request, before returning any tool call.
        content = first['messages'][1]['content']
        packet = json.JSONDecoder().raw_decode(content.split('\n\n', 1)[1])[0]
        assert packet['repositories'] == original['repositories']
        if conversation:
            assert 'writable' not in packet
            assert packet['execution']['coordinator'] == 'read_and_delegate'
            assert 'inline' in packet['execution']['worker_permission']
        else:
            assert packet['writable'] == original['writable']
        assert packet['verifiers'] == original['verifiers']
        assert packet.get('project_graph') == original.get('project_graph')
        contract = packet['path_contract']
        assert 'repository identifier from repositories, not a filesystem path' in contract['repository']
        assert 'relative to repositories[repository]' in contract['path']
        assert 'Preserve the user-supplied path' in contract['path']
        assert 'do not prepend' in contract['path'] and 'guess another location' in contract['path']
        assert 'parent directory is not a tool path base' in contract['project_graph']
        assert 'never changes repository roots or permissions' in contract['project_graph']
        assert 'retain their original repository roots on resume' in contract['project_graph']
        assert 'exists:false means the file was not found' in contract['exists_false']
        assert 'no file content was inspected' in contract['exists_false']
        assert 'do not silently retry a guessed path' in contract['exists_false']

    def initial_steps():
        first_packet(client)
        yield 'read', {'repository': 'projects', 'path': relative}
        yield 'read', {'repository': 'repo', 'path': relative}
        yield 'read', {'repository': 'projects', 'path': missing}
        yield 'tare_deliver', {'summary': 'Read the packets and reported the missing file.'}

    client = Client(steps=initial_steps())
    result = native_agent.run_session(config, deployment(), custody, 'Read ' + relative,
                                     conversation=conversation, client=client, local_counter=client)
    assert result['status'] == 'PROPOSED'

    def resumed_steps():
        first_packet(resumed)
        yield 'read', {'repository': 'projects', 'path': relative}
        yield 'read', {'repository': 'repo', 'path': relative}
        yield 'tare_deliver', {'summary': 'Read again from the saved roots.'}

    resumed = Client(steps=resumed_steps())
    result = native_agent.run_session(None, None, custody, resume=True,
                                     follow_up='Read the same packets again.', message_id='same-packets',
                                     client=resumed, local_counter=resumed)
    assert result['status'] == 'PROPOSED'
    saved = json.loads((custody/'native-agent.json').read_bytes())['config']
    for key in ('repositories', 'writable', 'verifiers', 'project_graph'):
        assert saved.get(key) == original.get(key)
    outputs = [json.loads(e['result']['output_json']) for e in events(custody)
               if e['kind'] == 'effect_returned']
    assert len(outputs) == 5
    for index, marker in ((0, 'workspace-packet'), (1, 'other-root-packet'),
                          (3, 'workspace-packet'), (4, 'other-root-packet')):
        assert marker in outputs[index]['text']
        assert 'wrong-backlog-packet' not in outputs[index]['text']
    assert outputs[2]['exists'] is False
    assert not outputs[2].get('text')
    assert all(path.read_bytes() == data for path, data in before.items())
    assert config == original
