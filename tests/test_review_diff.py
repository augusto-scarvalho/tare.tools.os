"""Keep large JSON reviews faithful without changing source bytes or raising the review envelope."""
import hashlib
import json
from pathlib import Path

import pytest

from relay.challenger import git
from relay import conversation_review, review_diff, native_agent, turn_engine
from test_conversation_tasks import host, intake, planned, effect  # noqa: F401
from relay import conversation_tasks as tasks, work_units


def commit(root):
    git(root,'init');git(root,'add','--all')
    git(root,'-c','user.name=Fixture','-c','user.email=fixture@localhost','-c','commit.gpgsign=false',
        '-c','core.hooksPath=','commit','-m','Input')


def graph(**meta):
    from graph_backlog.adapters import MarkdownAdapter
    value=MarkdownAdapter.from_markdown('');value['meta'].update(meta)
    return value


def test_canonical_json_diff_retains_values_code_and_exact_hashes_without_rewriting(tmp_path):
    root=tmp_path/'project';root.mkdir()
    before=graph(items=list(range(25000)))
    (root/'data.json').write_bytes(json.dumps(before).encode())
    (root/'code.py').write_bytes(b'value = 1\n');commit(root)
    old=git(root,'show','HEAD:data.json')
    after=json.loads(json.dumps(before));after['meta']['items'][12000]=-1
    new=(json.dumps(after,indent=2)+'\n').encode();(root/'data.json').write_bytes(new)
    (root/'code.py').write_bytes(b'value = 2\n')
    raw=git(root,'diff','--binary','HEAD','--');assert len(raw)>131072
    projected,records,views=review_diff.compact_json_diff(root,{'data.json','code.py'})
    assert len(projected)<2000 and b'-      12000,' in projected and b'+      -1,' in projected
    assert b'+value = 2' in projected and b'not an application patch' in projected
    assert records[0]['base_blob_sha256']==hashlib.sha256(old).hexdigest()
    assert records[0]['candidate_file_sha256']==hashlib.sha256(new).hexdigest()
    assert records[0]['largest_changed_values'][0]['path']=='$/meta/items/12000'
    assert json.loads(views[records[0]['canonical_snapshot']['path']])==after
    assert (root/'data.json').read_bytes()==new and git(root,'diff','--binary','HEAD','--')==raw


@pytest.mark.parametrize('bad',['duplicate','nonfinite','precision','negativezero'])
def test_ambiguous_json_keeps_its_complete_raw_diff(tmp_path,bad):
    root=tmp_path/'project';root.mkdir()
    original=json.dumps(graph(body='x'*150000,n=1))
    pairs={'duplicate':('1, "n": 2','1, "n": 3'),'nonfinite':('NaN','Infinity'),
        'precision':('0.100000000000000000001','0.100000000000000000002'),'negativezero':('-0','0')}
    a,b=pairs[bad]
    before=original.replace('"n": 1','"n": '+a);after=original.replace('"n": 1','"n": '+b)
    (root/'data.json').write_bytes(before.encode());commit(root);(root/'data.json').write_bytes(after.encode())
    raw=git(root,'diff','--binary','HEAD','--');assert len(raw)>131072
    projected,records,views=review_diff.compact_json_diff(root,{'data.json'})
    assert projected==raw and records==[] and views=={}


def test_non_graph_json_preserves_object_order_in_the_raw_review(tmp_path):
    root=tmp_path/'project';root.mkdir();body='x'*150000
    (root/'config.json').write_bytes(json.dumps({'body':body,'a':1,'b':2}).encode());commit(root)
    (root/'config.json').write_bytes(json.dumps({'b':2,'a':1,'body':body}).encode())
    raw=git(root,'diff','--binary','HEAD','--')
    projected,records,views=review_diff.compact_json_diff(root,{'config.json'})
    assert projected==raw and records==[] and views=={}


def test_a_bounded_real_change_between_128_and_256_kib_reaches_native_context_admission(tmp_path):
    root=tmp_path/'project';root.mkdir()
    (root/'code.py').write_bytes(b'a'*90000+b'\n');commit(root)
    (root/'code.py').write_bytes(b'b'*90000+b'\n')
    diff,_,_,_,failure=review_diff.prepare(root,{'code.py'})
    assert 131072<len(diff)<review_diff.MAX_REVIEW_DIFF_BYTES and failure is None


def test_meaningful_oversize_requires_candidate_revision_before_any_auditor(host,monkeypatch):
    tools,execution,root,*_=host
    before=graph(body='x'*150000)
    (root/'data.json').write_bytes(json.dumps(before).encode());commit(root)
    before['meta']['body']='y'*150000
    new=json.dumps(before).encode();(root/'data.json').write_bytes(new)
    def unexpected(*a,**kw):raise AssertionError('Oversize must not spend a reviewer call.')
    monkeypatch.setattr(turn_engine,'pick_auditors',unexpected);monkeypatch.setattr(native_agent,'run_session',unexpected)
    value=conversation_review.review(tools,{'id':'task'},root,{}, {},{'data.json':new},{},execution)
    assert value['verdict']=='INCONCLUSIVE' and value['requires_candidate_revision']
    assert value['failure_code']=='REVIEW_PACKET_OVERSIZED' and 'No auditor started' in value['reason']
    section=value['diff_projection'][0]['largest_changed_values'][0]
    assert section['path']=='$/meta/body' and section['after_bytes']>131072


@pytest.mark.parametrize('requires_revision',[False,True])
def test_packet_overflow_can_revise_the_candidate_instead_of_repeating_audit_only(host,monkeypatch,requires_revision):
    intake(host);planned(host)
    tools,execution,*_=host
    folder=tools.root/'units'/'previous';folder.mkdir(parents=True)
    audit={'verdict':'INCONCLUSIVE','reason':'Review preparation failed.',
        **({'requires_candidate_revision':True,'failure_code':'REVIEW_PACKET_OVERSIZED'} if requires_revision else {})}
    receipt=folder/'receipt.json';receipt.write_text(json.dumps({'result':{'audit':audit}}))
    previous={'verdict':'NOT_REVIEWED','receipt':str(receipt),'candidate':{'patch':'retained'},'audit':audit}
    value=tasks.load(tools.root);tasks.current(value)['delivery']=previous;tasks.save(tools.root,value)
    seen=[]
    def run(*a,**kw):
        seen.append(kw['candidate']['review_only'])
        return {'verdict':'INCONCLUSIVE','reason':'owned stop'}
    monkeypatch.setattr(work_units,'run_unit',run)
    result=tools.execute(effect('implement',{}),execution)
    assert result.status=='succeeded' and seen==[not requires_revision]


@pytest.mark.parametrize('permitted',[True,False])
def test_retained_oversize_stops_before_repeated_checks_or_provider_calls(host,tmp_path,monkeypatch,permitted):
    from relay.work_forms import record_plan
    from test_native_agent import deployment
    tools,_,root,*_=host
    original=json.dumps(graph(body='x'*150000)).encode()
    (root/'data.json').write_bytes(original);commit(root)
    updated=json.dumps(graph(body='y'*150000)).encode()
    (root/'data.json').write_bytes(updated)
    folder=tools.root/'units'/'retained';folder.mkdir(parents=True)
    patch=folder/'change.diff';patch.write_bytes(git(root,'diff','--binary','HEAD','--'))
    (root/'data.json').write_bytes(original)
    targets={'repo':['data.json']}
    candidate={'patch':str(patch),'sha256':hashlib.sha256(patch.read_bytes()).hexdigest(),
        'producer':deployment(),'targets':targets,'basis':tasks.source_state(tools.state['config'],targets)}
    unit={'key':'review','title':'Review retained graph','target_paths':['data.json'] if permitted else ['a.py'],
        'criteria':['Preserve the complete evidence.']}
    record_plan(tools.root,tools.state['config'],{'units':[unit],'criteria':unit['criteria']})
    monkeypatch.setenv('TARE_STORAGE_ROOT',str(tmp_path/'storage'))
    def unexpected(*a,**kw):raise AssertionError('Packet preflight must precede checks and paid calls.')
    monkeypatch.setattr(work_units,'run_checks',unexpected)
    monkeypatch.setattr(native_agent,'run_session',unexpected)
    unexpected.packet_preflight=conversation_review.packet_preflight
    value=work_units.run_unit(tools.root,tools.state,{'unit':'review'},lambda:False,
        candidate={**candidate,'review_only':True},review=unexpected)
    if permitted:
        assert value['verdict']=='NOT_REVIEWED' and value['candidate']==candidate
        assert value['audit']['failure_code']=='REVIEW_PACKET_OVERSIZED'
    else:
        assert value['verdict']=='INCONCLUSIVE' and 'exceeds this revised plan' in value['reason']
    assert not value.get('applied')
    receipt=json.loads(Path(value['receipt']).read_text())
    assert receipt['rungs'][0].get('model_calls',0)==receipt['rungs'][0].get('calls_reserved',0)==0
    assert (root/'data.json').read_bytes()==original and not list(tools.root.glob('units/*/session-*'))


@pytest.mark.parametrize('failure',['packet','unknown','dispatched'])
def test_only_an_undispatched_packet_failure_allows_one_same_turn_correction(host,monkeypatch,failure):
    intake(host);planned(host)
    tools,execution,*_=host
    audit={'verdict':'INCONCLUSIVE','reason':'Known preparation failure.',
        'failure_code':'REVIEW_PACKET_OVERSIZED' if failure!='unknown' else 'NO_PROGRESS',
        'requires_candidate_revision':True}
    if failure=='dispatched':audit['session']='an-uncertain-dispatch'
    folder=tools.root/'units'/'previous';folder.mkdir(parents=True)
    receipt=folder/'receipt.json';receipt.write_text(json.dumps({'result':{'audit':audit}}))
    previous={'verdict':'NOT_REVIEWED','receipt':str(receipt),'candidate':{'patch':'retained'},'audit':audit}
    value=tasks.load(tools.root);task=tasks.current(value)
    task.update(delivery=previous,delivery_invocation=tools.invocation_id);tasks.save(tools.root,value)
    seen=[]
    monkeypatch.setattr(work_units,'run_unit',lambda *a,**kw:seen.append(kw) or previous)
    for index in range(2):
        result=tools.execute(effect('implement',{},key='attempt-'+str(index)),execution)
        assert result.status=='succeeded'
    assert len(seen)==(1 if failure=='packet' else 0)
    assert 'Stop and explain' in json.loads(result.output_json)['next']
    if seen:
        assert not seen[0]['candidate']['review_only']
        assert tasks.current(tasks.load(tools.root))['candidate_correction_invocation']==tools.invocation_id
