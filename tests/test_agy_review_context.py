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
