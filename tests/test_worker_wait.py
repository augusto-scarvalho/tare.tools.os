"""Consent resumes the actual Runtime journal, keeping scope, budget and the independent acceptance gate."""
import hashlib
import json
from pathlib import Path

import pytest

from relay import native_agent, work_units, worker_wait
from test_native_agent import Client, deployment, setup  # noqa: F401


@pytest.mark.parametrize('saved_calls', [None, 60])
@pytest.mark.parametrize('changed', ['none', 'project', 'candidate'])
def test_folder_wait_keeps_the_worker_and_rejects_changed_bases(setup, tmp_path, monkeypatch, changed, saved_calls):
    from relay.folder_access import grant
    from relay.work_forms import record_plan
    config, custody, root = setup
    custody.mkdir()
    monkeypatch.setenv('TARE_STORAGE_ROOT',str(tmp_path/'storage'))
    external=tmp_path/'reference';external.mkdir();(external/'facts.txt').write_text('two')
    store=tmp_path/'grants.json';config['folder_access']={'grants':str(store)}
    config['conversation_flow']=True
    unit={'key':'wait','repository':'repo','title':'Set two','target_paths':['a.py'],
          'criteria':['value equals two'],'verifiers':['unit'],
          'review_feedback':'A full independent finding. '+('evidence '*700)}
    record_plan(custody,config,{'units':[unit],'criteria':unit['criteria']})
    state={'config':config,'conversation_flow':True,'deployment':deployment(),
           'limits':{'max_model_calls':60},'deadline_utc':None}
    monkeypatch.setattr(work_units,'routes',lambda *a,**kw:iter([('fixture',deployment())]))
    client=Client(steps=[('request_folder_access',{'path':str(external),'reason':'Read the requested value.'}),
        ('tare_deliver',{'summary':'Waiting for read permission.'}),
        ('folder_read',{'folder':str(external),'path':'facts.txt'}),
        ('edit',{'repository':'repo','path':'a.py','sha256':hashlib.sha256(b'value = 1\n').hexdigest(),
                 'search':'value = 1','replace':'value = 2'}),
        ('verify',{'name':'unit'}),('tare_deliver',{'summary':'Changed and checked.'})])
    monkeypatch.setattr(native_agent,'inference_client',lambda *a,**kw:client)
    monkeypatch.setattr('compute_plane.native_models.NativeHttpClient',lambda *a,**kw:client)
    audits=[]
    def audit(tree,*a):
        audits.append(True)
        assert (root/'a.py').read_bytes()==b'value = 1\n'
        return {'verdict':'ACCEPT'}
    actual_child_limits = work_units.child_limits
    monkeypatch.setattr(work_units, 'child_limits', lambda *a, **kw: {
        'max_model_calls': saved_calls, 'max_seconds': 600, 'request_seconds': 600})
    first=work_units.run_unit(custody,state,{'unit':'wait'},lambda:False,review=audit)
    monkeypatch.setattr(work_units, 'child_limits', actual_child_limits)
    state['limits']['max_model_calls'] = work_units.SESSION_RESERVE + 12
    assert first['verdict']=='INCONCLUSIVE' and first['worker'] and not audits
    worker=first['worker'];tree=Path(worker['tree'])
    session=Path(worker['session'])
    frozen=json.loads((session/'native-agent.json').read_text(encoding='utf8'))
    assert 'A full independent finding.' not in frozen['instruction']
    rows=[json.loads(s)['event'] for s in (session/'journal.jsonl').read_text(encoding='utf8').splitlines()]
    messages=[e['text'] for e in rows if e['kind']=='user_message']
    assert len(messages)==1 and unit['review_feedback'] in messages[0]
    assert client.generations==2 and tree.is_dir() and (root/'a.py').read_bytes()==b'value = 1\n'
    from relay.folder_access import REQUEST
    (custody/REQUEST).write_text(json.dumps({'path':str(external),'reason':'Read it.'}))  # the coordinator asked
    with pytest.raises(ValueError,match='read permission'):  # and the user has not answered yet
        work_units.run_unit(custody,state,{'unit':'wait'},lambda:False,review=audit,worker=worker)
    assert client.generations==2
    grant(store,str(external))
    unit.pop('review_feedback')  # a folder reply need not repeat an already-consumed review packet
    record_plan(custody,config,{'units':[unit],'criteria':unit['criteria']})
    if changed!='none':
        (root if changed=='project' else tree).joinpath('a.py').write_text('value = 9\n')
        with pytest.raises(ValueError,match='changed'):
            work_units.run_unit(custody,state,{'unit':'wait'},lambda:False,review=audit,worker=worker)
        assert client.generations==2 and not audits
        return
    second=work_units.run_unit(custody,state,{'unit':'wait'},lambda:False,review=audit,worker=worker)
    saved=json.loads((session/'native-agent.json').read_text(encoding='utf8'))
    assert second['applied'] and audits==[True] and client.generations==6
    assert saved['budget']['max_model_calls'] is saved_calls or saved['budget']['max_model_calls'] == saved_calls
    receipt=json.loads(Path(second['receipt']).read_text(encoding='utf8'))
    assert receipt['rungs'][0]['calls_reserved'] == 10  # two prior calls remain spent
    rows_after=[json.loads(s)['event'] for s in (session/'journal.jsonl').read_text(encoding='utf8').splitlines()]
    assert rows_after[0] == rows[0]
    assert saved['session_id']==frozen['session_id'] and len(saved['invocations'])==2
    for key in ('config','deployment','instruction','budget','limits','prompt','tool_specs'):
        assert saved.get(key)==frozen.get(key)
    assert work_units.unit_calls(custody)==6  # cumulative resumed usage is not double counted
    assert (root/'a.py').read_bytes()==b'value = 2\n' and not tree.exists()


def test_only_granted_leaf_wait_is_excluded_once_without_renewing_the_budget(tmp_path):
    from relay.folder_access import REQUEST, grant
    external=tmp_path/'reference';external.mkdir()
    store=tmp_path/'grants.json';session=tmp_path/'session';session.mkdir()
    (session/REQUEST).write_text(json.dumps({'path':str(external),'reason':'Read evidence.'}))
    state={'delegated_leaf':True,'config':{'folder_access':{'grants':str(store)}},
           'invocations':[{'ended_at':700}], 'deadline_utc':900,'budget':{'max_model_calls':30}}
    worker_wait.resume_clock(session,state,'continue',1700)
    assert state['deadline_utc']==900 and not (session/REQUEST).exists()  # declined: no excluded wait, no request
    (session/REQUEST).write_text(json.dumps({'path':str(external),'reason':'Read evidence.'}))  # asked again
    grant(store,str(external))
    worker_wait.resume_clock(session,state,'continue',1700)
    assert state['deadline_utc']==1900 and state['deadline_utc']-1700==200
    assert state['budget']=={'max_model_calls':30} and state['folder_wait_seconds']==1000
    worker_wait.resume_clock(session,state,'continue',1900)
    assert state['deadline_utc']==1900 and state['folder_wait_seconds']==1000


@pytest.mark.parametrize('after', ['declined', 'replanned', 'never asked'])
def test_a_declined_folder_or_a_new_plan_never_holds_the_waiting_worker(setup, tmp_path, monkeypatch, after):
    from relay.work_forms import record_plan
    config, custody, root = setup
    custody.mkdir()
    monkeypatch.setenv('TARE_STORAGE_ROOT',str(tmp_path/'storage'))
    external=tmp_path/'reference';external.mkdir()
    config['folder_access']={'grants':str(tmp_path/'grants.json')};config['conversation_flow']=True
    unit={'key':'wait','repository':'repo','title':'Set two','target_paths':['a.py'],
          'criteria':['value equals two'],'verifiers':['unit']}
    record_plan(custody,config,{'units':[unit],'criteria':unit['criteria']})
    state={'config':config,'conversation_flow':True,'deployment':deployment(),
           'limits':{'max_model_calls':60},'deadline_utc':None}
    monkeypatch.setattr(work_units,'routes',lambda *a,**kw:iter([('fixture',deployment())]))
    client=Client(steps=[('request_folder_access',{'path':str(external),'reason':'Read the requested value.'}),
        ('tare_deliver',{'summary':'Waiting for read permission.'}),
        ('edit',{'repository':'repo','path':'a.py','sha256':hashlib.sha256(b'value = 1\n').hexdigest(),
                 'search':'value = 1','replace':'value = 2'}),
        ('verify',{'name':'unit'}),('tare_deliver',{'summary':'Changed and checked.'})])
    monkeypatch.setattr(native_agent,'inference_client',lambda *a,**kw:client)
    monkeypatch.setattr('compute_plane.native_models.NativeHttpClient',lambda *a,**kw:client)
    first=work_units.run_unit(custody,state,{'unit':'wait'},lambda:False,review=lambda *a:{'verdict':'ACCEPT'})
    worker=first['worker'];tree=Path(worker['tree']);session=Path(worker['session'])
    if after=='replanned':  # the user changed the goal: the new plan gets a fresh implementer
        unit['criteria']=['value equals two','nothing else changes']
        record_plan(custody,config,{'units':[unit],'criteria':unit['criteria']})
    if after=='never asked':  # the coordinator skipped the question: no refusal the user never gave
        with pytest.raises(ValueError,match='read permission'):
            work_units.run_unit(custody,state,{'unit':'wait'},lambda:False,review=lambda *a:{'verdict':'ACCEPT'},
                                worker=worker)
        return
    if after=='declined':  # an explicit host decision, not an inferred missing reply
        from relay import consent_decisions, folder_access
        scope={'path':str(external),'store':config['folder_access']['grants']}
        key=consent_decisions.register(custody,'worker-wait','folder',scope,
            question=folder_access.question({'path':str(external),'reason':'Read it.'},scope['store']))
        receipt=consent_decisions.record(custody,'worker-wait',key,choice='deny')
        assert receipt['decision']=='no' and 'was not granted' in receipt['notice']
        assert folder_access.declined(custody,str(external))
        assert not folder_access.granted(scope['store'])
    second=work_units.run_unit(custody,state,{'unit':'wait'},lambda:False,review=lambda *a:{'verdict':'ACCEPT'},
                               worker=worker)
    assert second.get('applied'), {k: second.get(k) for k in ('verdict', 'reason', 'checks_after', 'next')}
    assert (root/'a.py').read_bytes()==b'value = 2\n' and not tree.exists()
    told=[body for _,body in client.calls if 'did not grant read access to' in json.dumps(body)]
    if after=='declined':  # the same worker went on, told the folder stays closed
        assert told
        assert folder_access.declined(custody,str(external))
        assert not folder_access.granted(scope['store'])
    else:  # the old worker never resumed: a fresh implementer ran the new plan
        assert not told


@pytest.mark.parametrize('resource', ['calls', 'time'])
def test_waiting_implementer_invocation_enforces_dispatch_ceiling(setup, tmp_path, monkeypatch, resource):
    """Real Runtime resume keeps identity and cannot spend the old unlimited balance."""
    from relay.folder_access import grant
    config, session, _ = setup
    config['verifiers'] = {}
    external = tmp_path/'reference'; external.mkdir()
    store = tmp_path/'grants.json'
    config['folder_access'] = {'grants': str(store)}
    first_client = Client(steps=[
        ('request_folder_access', {'path': str(external), 'reason': 'Read reference.'}),
        ('tare_deliver', {'summary': 'Waiting for permission.'})])
    native_agent.run_session(config, deployment(), session, 'Inspect the reference.',
        delegated_leaf=True, client=first_client, local_counter=first_client,
        limits={'max_model_calls': None, 'max_seconds': None})
    frozen = json.loads((session/'native-agent.json').read_text(encoding='utf8'))
    initial = (session/'journal.jsonl').read_text(encoding='utf8').splitlines()[0]
    assert first_client.generations == 2
    grant(store, str(external))
    client = Client(steps=[('read', {'repository': 'repo', 'path': 'a.py'})]*5)
    if resource == 'time':
        original = worker_wait.resume_clock
        def elapsed(*args, **kwargs):
            original(*args, **kwargs)
            # Expire the invocation allowance in preparation, not the saved deadline.
            import time
            time.sleep(.03)
        monkeypatch.setattr(worker_wait, 'resume_clock', elapsed)
    if resource == 'time':
        with pytest.raises(ValueError, match='execution budget exhausted'):
            native_agent.run_session(None, None, session, resume=True,
                follow_up='Permission granted. Continue inspecting.', message_id='consent-resume',
                invocation_limits={'max_model_calls': 2, 'max_seconds': .01},
                client=client, local_counter=client)
        assert client.generations == 0
    else:
        result = native_agent.run_session(None, None, session, resume=True,
            follow_up='Permission granted. Continue inspecting.', message_id='consent-resume',
            invocation_limits={'max_model_calls': 2, 'max_seconds': 120},
            client=client, local_counter=client)
        assert client.generations == 2
        assert result['status'] != 'PROPOSED'
        assert result['effective_limits']['max_model_calls'] == 2
        assert result['effective_limits']['request_seconds'] <= 120
    saved = json.loads((session/'native-agent.json').read_text(encoding='utf8'))
    assert saved['budget'] == frozen['budget'] and saved['limits'] == frozen['limits']
    assert saved['deadline_utc'] is None
    assert (session/'journal.jsonl').read_text(encoding='utf8').splitlines()[0] == initial


@pytest.mark.parametrize('resource', ['calls', 'time'])
def test_implementer_minimum_reports_missing_resource_with_unlimited_counterpart(tmp_path, monkeypatch, resource):
    from relay import execution_scope
    import time
    monkeypatch.setattr(execution_scope, 'current', lambda: execution_scope.Scope(unlimited=('calls', 'time')))
    state = {'config': {}, 'conversation_flow': True,
             'limits': {'max_model_calls': work_units.SESSION_RESERVE + 1 if resource == 'calls' else None},
             'deadline_utc': time.time()+61 if resource == 'time' else None}
    with pytest.raises(ValueError) as error:
        work_units.child_limits(tmp_path, state)
    message = str(error.value)
    assert 'unlimited' in message and 'required minimum' in message
    assert str(work_units.MIN_CHILD_CALLS)+' calls' in message and '120 s' in message
    assert 'No model call started' in message
    assert 'configuration' not in message.lower() and 'another conversation' not in message.lower()
