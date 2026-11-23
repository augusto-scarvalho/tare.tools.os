import copy,json
import pytest
from relay import conversation_review as review,native_generation
from compute_plane.native_journal import NativeJournal
from test_conversation_review_recovery import reviewed
from test_conversation_tasks import user_reply,host
from test_native_agent import setup


def deployment(endpoint='cli://agy'):
    return {'native_profile':{'protocol':'vendor-cli','endpoint':endpoint,
        'context_window':1048576,'output_tokens':16384}}


def test_agy_operating_compaction_budget_preserves_declared_model_and_cache_input():
    dep=deployment();before=copy.deepcopy(dep)
    policy=native_generation.model_context_policy(dep)
    assert policy=={'trigger':49152/1048576,'target':32768/1048576}
    assert dep==before
    ordinary=native_generation.model_context_policy(deployment('cli://claude'))
    assert ordinary=={'trigger':.8,'target':.5}


def test_tighter_operator_compaction_policy_is_not_relaxed():
    dep=deployment();dep['native_context_policy']={'trigger':.03,'target':.02,'source':'fixture'}
    assert native_generation.model_context_policy(dep)=={'trigger':.03,'target':.02}


@pytest.mark.parametrize('verdict',['INCONCLUSIVE','REVISE','ACCEPT'])
@pytest.mark.parametrize('endpoint',['cli://agy','cli://claude'])
def test_only_inconclusive_legacy_agy_exchange_can_receive_changed_host_recovery(tmp_path,verdict,endpoint):
    session=tmp_path/'session';session.mkdir()
    journal=NativeJournal(session/'journal.jsonl')
    journal.append({'kind':'model_returned','call_id':'fixture', 'reply':{'metadata_json':json.dumps({
        'count_anchor':{'call_id':'fixture','request_bytes':338000}})}})
    row={'deployment':deployment(endpoint),'context_contract':{'policy':{'trigger':.8,'target':.5}}}
    result={'verdict':verdict,'summary':'Observed incomplete evidence.',
        'findings':[{'claim':'Earlier source obligation remains open.','sources':[]}],
        'remaining':['Read the required source.']}
    before=(session/'journal.jsonl').read_bytes();original=copy.deepcopy(result)
    value=review.context_failure(tmp_path,row,result)
    assert result==original and (session/'journal.jsonl').read_bytes()==before
    if verdict=='INCONCLUSIVE' and endpoint=='cli://agy':
        assert value['failure_code']=='CLI_INPUT_BYTE_LIMIT_EXCEEDED'
        assert value['findings']==result['findings'] and value['remaining']==result['remaining']
        assert value['requires_candidate_revision'] is False
    elif verdict=='INCONCLUSIVE':assert value['requires_candidate_revision'] is True
    else:assert value==result


@pytest.mark.parametrize('size',[None,100000,True])
def test_missing_or_small_measured_envelopes_do_not_authorize_recovery(tmp_path,size):
    session=tmp_path/'session';session.mkdir()
    NativeJournal(session/'journal.jsonl').append({'kind':'model_returned','call_id':'one',
        'reply':{'metadata_json':json.dumps({'count_anchor':{'call_id':'one','request_bytes':size}})}})
    row={'deployment':deployment()}
    assert not review.legacy_agy_input_overflow(tmp_path,row)
    row['context_contract']={'cli_input_byte_limit':163840}
    assert not review.legacy_agy_input_overflow(tmp_path,row)


def test_proven_changed_host_input_failure_can_be_requalified_once_without_losing_old_findings(reviewed,monkeypatch):
    host,run,*_=reviewed;tools=host[0]
    first=run('INCONCLUSIVE')
    receipt=next((tools.root/'reviews').glob('*/receipt.json'))
    row=json.loads(receipt.read_text())
    row['context_contract']['legacy_input_fixture']=True
    from compute_plane.native_journal import save_json
    save_json(receipt,row,max_bytes=review.MAX_RECEIPT_BYTES)
    before=receipt.read_bytes()
    monkeypatch.setattr(review,'legacy_agy_input_overflow',
        lambda job,record:bool((record.get('context_contract') or {}).get('legacy_input_fixture')))
    user_reply(host,'Continue only after the host input correction; preserve all prior findings.')
    assert run('ACCEPT',expected_calls=2)['verdict']=='ACCEPT'
    assert receipt.read_bytes()==before
    requests=[json.loads(p.read_text()) for p in (tools.root/'reviews').rglob('request.json')]
    recovery=next(r['context_recovery'] for r in requests if 'context_recovery' in r)
    assert recovery['previous']['findings']==first['findings']
    assert recovery['previous']['remaining']==first['remaining']


@pytest.mark.parametrize('max_compactions',[1,None])
def test_agy_turn_zero_with_large_managed_skill_admits_within_hard_limit(setup,tmp_path,max_compactions):
    """Turn 0 on cli://agy with a 14 KB skill crosses the 49,152 soft trigger under byte estimation
    before any usage anchor exists, but fits hard_limit and AGY_REQUEST_BYTE_LIMIT."""
    from relay import native_agent
    config,custody,_=setup
    config['verifiers']={}
    config['context_policy']={'max_compactions':max_compactions}
    dep={
        'id':'agy-gemini-4-carbon-cli','runner':'native-http',
        'native_cli':{'executable':str(tmp_path/'agy.exe'),'version':'1.2.7'},
        'native_counter_protocol':'utf8-envelope-estimate-v1',
        'native_profile':{
            'protocol':'vendor-cli','endpoint':'cli://agy','model':'gemini-4-carbon-high',
            'context_window':200000,'output_tokens':16384,'parameters_json':'{"effort":"high"}',
            'source':'test-fixture-not-a-qualified-model',
        },
    }
    class AgyStubClient:
        endpoint='cli://agy'
        def __init__(self): self.calls=[]
        def post_observed(self,path,body,control):
            self.calls.append((path,body))
            return ({
                'model':'gemini-4-carbon-high',
                'usage':{'input_tokens':16500,'cache_read_tokens':0,'output_tokens':40,'total_tokens':16540},
                'choices':[{'finish_reason':'tool_calls','message':{'role':'assistant','content':None,'tool_calls':[
                    {'id':'cli-action-1','type':'function','function':{
                        'name':'tare_deliver','arguments':json.dumps({'summary':'Completed with skill.'})}}]}}],
            },{'transport':'official_vendor_cli','started':True,'cleanup_verified':True})
    import hashlib
    skill_body=('pxl pixel art guide line.\n'*1800)  # ~45 KB so raw turn-0 wire > 49,152 units and < 160 KB
    skill={
        'skill_id':'pxl-pixel-art','skill_name':'pxl-pixel-art','snapshot_id':'s'*16,'text_source':'base',
        'authority':'NONE','effects_authorized':False,'origin_path':str(tmp_path/'claude-pxl'),
        'effective_roles':['implementer','planner'],'min_model_class':'frontier',
        'text':skill_body,'effective_sha256':hashlib.sha256(skill_body.encode('utf-8')).hexdigest(),
    }
    client=AgyStubClient()
    result=native_agent.run_session(config,dep,custody,'Create a 16x16 potion icon.',
                                    client=client,inherited_managed_skills=[skill])
    assert result['status']=='PROPOSED' and result['model_calls']==1 and len(client.calls)==1
    counts=[r.event()['count'] for r in NativeJournal(custody/'journal.jsonl').snapshot().records
            if r.event()['kind']=='context_counted']
    assert counts and counts[0]['estimated_input_tokens']>49152
