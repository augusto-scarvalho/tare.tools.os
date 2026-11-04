"""Parallel arms may share check semantics, never the host-generated destructive pytest temp root."""
from copy import deepcopy
import sys
import pytest
from relay.challenger_worker import session_config


@pytest.mark.parametrize('joined',[False,True])
def test_host_generated_pytest_scratch_is_unique_per_workspace(tmp_path,joined):
    primary=tmp_path/'primary';candidate=tmp_path/'candidate';reference=tmp_path/'reference'
    original=str(primary.parent/('.'+primary.name+'-pytest'))
    args=[sys.executable,'-B','-m','pytest','tests/test_a.py']
    args+=['--basetemp='+original] if joined else ['--basetemp',original]
    row={'repository':'repo','project':str(primary),'config':{
        'repositories':{'repo':str(primary)},'writable':{'repo':['a.py']},
        'capabilities':['read','edit','verify'],'verifiers':{'unit':{'repository':'repo','argv':args}}}}
    before=deepcopy(row)
    results=[]
    for root,role in ((candidate,'implementer'),(reference,'auditor')):
        selected=session_config(row,root,role)
        observed=selected['verifiers']['unit']['argv']
        target=str(root.parent/('.'+root.name+'-pytest'))
        assert observed[:-1]==args[:-1]
        assert observed[-1]==('--basetemp='+target if joined else target)
        results.append(observed[-1])
    assert len(set(results+[args[-1]]))==3
    assert row==before


@pytest.mark.parametrize('command',['explicit','relative','other-program','python-c'])
def test_explicit_or_non_pytest_checks_are_not_rewritten(tmp_path,command):
    primary=tmp_path/'primary';scratch=str(tmp_path/'.primary-pytest')
    args=[sys.executable,'-m','pytest','--basetemp',str(tmp_path/'user-chosen')]
    if command=='relative':args[-1]='.pytest_cache'
    elif command=='other-program':args=['custom-runner','--basetemp',scratch]
    elif command=='python-c':args=[sys.executable,'-c','pass','--basetemp',scratch]
    row={'repository':'repo','project':str(primary),'config':{'capabilities':['read','verify'],
        'verifiers':{'unit':{'repository':'repo','argv':args}},'writable':{}}}
    selected=session_config(row,tmp_path/'candidate','implementer')
    assert selected['verifiers']['unit']['argv']==args
