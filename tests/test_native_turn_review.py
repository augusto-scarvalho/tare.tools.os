"""Turn review and restoration over real native journals and Kernel edits."""
import json

import pytest

from compute_plane.native_review import digest,receipt_path
from relay import native_agent, native_turn_review as turn_review
from relay.native_review import review
from relay.native_review_actions import undo_edit
from tare_tools_agent_runtime import NativeBudget
from test_native_agent import setup,Client,run


def make_turn(setup, *, interposed=False):
    config,custody,root=setup
    before=b'# earlier user note\r\nvalue = 1\r\n'
    first=before.replace(b'= 1',b'= 2')
    middle=first+(b'# intervening user note\r\n' if interposed else b'')
    (root/'a.py').write_bytes(before)
    config['writable']={'repo':['a.py','b.txt']}
    client=Client(steps=[('read',{'repository':'repo','path':'a.py'}),
        ('edit',{'repository':'repo','path':'a.py','sha256':digest(before),'search':'value = 1','replace':'value = 2'}),
        ('edit',{'repository':'repo','path':'a.py','sha256':digest(middle),'search':'value = 2','replace':'value = 3'}),
        ('edit',{'repository':'repo','path':'b.txt','sha256':None,'search':'','replace':'created\r\n'}),
        ('verify',{'name':'unit'}),('tare_deliver',{'summary':'Changed two files.'})])
    if interposed:
        client.after_model=lambda: (root/'a.py').write_bytes(middle) if client.generations==3 else None
    result=run(config,custody,client,budget=NativeBudget(max_model_calls=12))
    assert result['status']=='PROPOSED'
    turn=review(custody)['turns'][0]['id']
    return custody,root,turn,before,client


@pytest.fixture
def saved_turn(setup):
    return make_turn(setup)


def inspect(saved):
    custody,root,turn,*_=saved
    return turn_review.review_turn(custody,project=root,invocation_id=turn)


def restore(saved,plan):
    custody,root,turn,*_=saved
    return turn_review.undo_turn(custody,project=root,invocation_id=turn,expected_plan=plan)


def test_grouped_net_diff_and_complete_turn_undo(saved_turn):
    custody,root,turn,before,client=saved_turn
    journal=(custody/'journal.jsonl').read_bytes(); calls=len(client.calls)
    view=inspect(saved_turn)
    assert len(view['files'])==2
    file=view['files'][0]
    assert file['edit_count']==2 and file['segments']==1
    assert '-value = 1' in file['diff'] and '+value = 3' in file['diff']
    assert 'value = 2' not in file['diff']
    assert view['undo']['status']=='READY' and view['undo']['edits']==3
    result=restore(saved_turn,view['undo']['plan'])
    assert result['status']=='UNDONE'
    assert [r['status'] for r in result['files']]==['UNDONE','UNDONE']
    assert (root/'a.py').read_bytes()==before and not (root/'b.txt').exists()
    assert all(r['undo_status']=='UNDONE' for r in review(custody)['edits'])
    assert inspect(saved_turn)['undo']['status']=='UNDONE'
    with pytest.raises(ValueError):restore(saved_turn,view['undo']['plan'])
    assert (custody/'journal.jsonl').read_bytes()==journal and len(client.calls)==calls


def test_intervening_user_edit_splits_history_and_refuses_bulk(setup):
    saved=make_turn(setup,interposed=True)
    custody,root,turn,*_=saved
    original=(root/'a.py').read_bytes()
    view=inspect(saved)
    assert view['files'][0]['segments']==2
    assert 'intervening changes excluded' in view['files'][0]['diff']
    assert '+# intervening user note' not in view['files'][0]['diff']
    assert view['undo']['status']=='UNAVAILABLE'
    with pytest.raises(ValueError,match='Intervening changes'):restore(saved,'wrong')
    assert (root/'a.py').read_bytes()==original and (root/'b.txt').exists()
    assert not turn_review.turn_receipt_path(custody,turn).exists()


def test_preflight_all_files_before_any_write(saved_turn):
    custody,root,turn,*_=saved_turn
    plan=inspect(saved_turn)['undo']['plan']; first=(root/'a.py').read_bytes()
    (root/'b.txt').write_bytes(b'newer user change\r\n')
    with pytest.raises(ValueError,match='newer changes'):restore(saved_turn,plan)
    assert (root/'a.py').read_bytes()==first
    assert (root/'b.txt').read_bytes()==b'newer user change\r\n'
    assert not turn_review.turn_receipt_path(custody,turn).exists()


def test_individual_undo_invalidates_confirmation_then_remaining_prefix_can_restore(saved_turn):
    custody,root,turn,before,*_=saved_turn
    old=inspect(saved_turn)
    undo_edit(custody,project=root,invocation_id=turn,effect_id=old['files'][0]['effect_id'])
    fresh=inspect(saved_turn)
    assert fresh['undo']['status']=='READY' and fresh['undo']['edits']==2
    with pytest.raises(ValueError,match='preview changed'):restore(saved_turn,old['undo']['plan'])
    assert restore(saved_turn,fresh['undo']['plan'])['status']=='UNDONE'
    assert (root/'a.py').read_bytes()==before


def test_mid_turn_failure_records_partial_and_never_retries(saved_turn,monkeypatch):
    custody,root,turn,before,*_=saved_turn
    owner=turn_review.restore_bytes
    calls=[]
    def fail_second(path,before,after,*,before_write):
        calls.append(path.name)
        if path.name=='b.txt':
            before_write()
            raise OSError('injected write failure')
        return owner(path,before,after,before_write=before_write)
    monkeypatch.setattr(turn_review,'restore_bytes',fail_second)
    preview=inspect(saved_turn)
    result=restore(saved_turn,preview['undo']['plan'])
    assert result['status']=='PARTIAL'
    assert [r['status'] for r in result['files']]==['UNDONE','UNKNOWN']
    assert (root/'a.py').read_bytes()==before
    assert (root/'b.txt').read_bytes()==b'created\r\n'
    assert inspect(saved_turn)['undo']['status']=='PARTIAL'
    with pytest.raises(ValueError):restore(saved_turn,preview['undo']['plan'])
    assert calls==['a.py','b.txt']
    assert review(custody)['edits'][-1]['undo_status']=='UNKNOWN'


@pytest.mark.parametrize('damage',['snapshot','permission','identity','turn'])
def test_changed_evidence_or_scope_cannot_use_old_confirmation(saved_turn,damage):
    custody,root,turn,*_=saved_turn
    preview=inspect(saved_turn); before={p.name:p.read_bytes() for p in root.iterdir() if p.is_file()}
    if damage=='snapshot':
        path=receipt_path(custody/'review',preview['files'][1]['effect_id'])
        receipt=json.loads(path.read_text()); (custody/'review'/'blobs'/receipt['after']).write_bytes(b'corrupt')
    else:
        state=json.loads((custody/'native-agent.json').read_text())
        if damage=='permission':state['config']['writable']={'repo':['a.py']}
        if damage=='identity':state['session_id']='changed'
        if damage=='turn':state['invocations'][0]['journal_end']=0
        (custody/'native-agent.json').write_text(json.dumps(state))
    with pytest.raises(ValueError):restore(saved_turn,preview['undo']['plan'])
    assert before=={p.name:p.read_bytes() for p in root.iterdir() if p.is_file()}


def test_later_turn_is_not_included_in_grouped_diff(saved_turn):
    custody,root,turn,before,*_=saved_turn
    raw=(root/'a.py').read_bytes()
    client=Client(steps=[('edit',{'repository':'repo','path':'a.py','sha256':digest(raw),'search':'value = 3','replace':'value = 9'}),
                         ('verify',{'name':'unit'}),('tare_deliver',{'summary':'Changed in a later turn.'})])
    native_agent.run_session(None,None,custody,resume=True,client=client,local_counter=client,
                             follow_up='Change again',message_id='next-turn')
    original=inspect(saved_turn)
    assert 'value = 9' not in original['files'][0]['diff']
    assert original['undo']['status']=='UNAVAILABLE'
    later=review(custody)['turns'][-1]['id']
    view=turn_review.review_turn(custody,project=root,invocation_id=later)
    assert len(view['files'])==1 and '+value = 9' in view['files'][0]['diff']
    turn_review.undo_turn(custody,project=root,invocation_id=later,expected_plan=view['undo']['plan'])
    assert (root/'a.py').read_bytes()==raw


def test_no_all_turns_or_cross_project_mutation(saved_turn):
    custody,root,turn,*_=saved_turn
    for kwargs in [{'project':root,'invocation_id':None},{'project':root.parent,'invocation_id':turn}]:
        with pytest.raises(ValueError):turn_review.review_turn(custody,**kwargs)


def test_preview_budget_is_explicit_and_intent_failure_changes_no_file(saved_turn,monkeypatch):
    custody,root,turn,*_=saved_turn
    limited=turn_review.review_turn(custody,project=root,invocation_id=turn,max_diff_bytes=1)
    assert all(r['diff'] is None and 'budget' in r['diff_unavailable'] for r in limited['files'])
    preview=inspect(saved_turn); before=(root/'a.py').read_bytes()
    def no_storage(*args,**kwargs):raise OSError('full disk')
    monkeypatch.setattr(turn_review,'save_json',no_storage)
    with pytest.raises(OSError,match='full disk'):restore(saved_turn,preview['undo']['plan'])
    assert (root/'a.py').read_bytes()==before and (root/'b.txt').exists()


@pytest.mark.parametrize('damage',['missing_files','wrong_effect','incomplete','edit_receipt'])
def test_corrupt_completion_receipt_is_unknown_not_success(saved_turn,damage):
    custody,root,turn,before,*_=saved_turn
    restore(saved_turn,inspect(saved_turn)['undo']['plan'])
    path=turn_review.turn_receipt_path(custody,turn)
    saved=json.loads(path.read_text())
    if damage=='missing_files':saved.pop('files')
    elif damage=='wrong_effect':saved['files'][0]['effect_ids']=['another-effect']
    elif damage=='incomplete':saved['files'][0]['status']='PENDING'
    else:
        from relay.native_review_actions import undo_path
        child=undo_path(custody,saved['files'][0]['effect_ids'][0])
        value=json.loads(child.read_text());value['after']='0'*64;child.write_text(json.dumps(value))
    path.write_text(json.dumps(saved))
    assert inspect(saved_turn)['undo']['status']=='UNKNOWN'
    assert (root/'a.py').read_bytes()==before
