"""Declared pytest coverage avoids a duplicate runner, without accepting filters as full coverage."""
import sys
import pytest
from relay import work_units


def project_python(tmp_path):
    runner=tmp_path/'.venv/Scripts/python.exe'
    runner.parent.mkdir(parents=True)
    runner.write_bytes(b'Compiler fixture only; never executed.')
    return str(runner)


@pytest.mark.parametrize('tail,duplicate',[
    (['tests/test_a.py','-q','--basetemp=.pytest_cache'],True),
    (['--quiet','tests/test_a.py'],True),
    (['tests/test_a.py','--basetemp','owned-scratch'],True),
    (['tests/test_a.py','-k','one_case'],False),
    (['tests/test_a.py','-m','slow'],False),
    (['tests/test_a.py','--ignore=tests/test_a.py'],False),
    (['tests/test_a.py::test_one'],False),
    (['tests/test_b.py'],False),
    (['tests/test_a.py','--basetemp'],False),
    (['tests/test_a.py','--some-plugin-option'],False),
])
def test_only_a_declared_unfiltered_matching_scope_replaces_the_automatic_check(tmp_path,tail,duplicate):
    explicit=[project_python(tmp_path),'-B','-m','pytest',*tail]
    config={'conversation_flow':True,'repositories':{'repo':str(tmp_path)},
            'verifiers':{'check':{'repository':'repo','argv':explicit}}}
    unit={'verifiers':['check'],'test_paths':['tests/test_a.py']}
    compiled=work_units.checks(config,unit,'repo',tmp_path)
    assert ('pytest-tests:tests/test_a.py' not in compiled)==duplicate
    # Original configured commands remain authoritative, including their flags.
    assert compiled['check'][:len(explicit)]==explicit


def test_explicitly_requested_checks_and_nonpytest_commands_are_preserved(tmp_path):
    argv=[project_python(tmp_path),'-B','-m','pytest','tests/test_a.py','-q']
    config={'conversation_flow':True,'repositories':{'repo':str(tmp_path)},'verifiers':{
        'first':{'repository':'repo','argv':argv},'second':{'repository':'repo','argv':argv},
        'lint':{'repository':'repo','argv':[sys.executable,'-m','ruff','check','tests/test_a.py']}}}
    unit={'verifiers':['first','second','lint'],'test_paths':['tests/test_a.py']}
    assert set(work_units.checks(config,unit,'repo',tmp_path))=={'first','second','lint'}


def test_another_python_environment_does_not_supply_the_automatic_check(tmp_path):
    project_python(tmp_path)
    config={'conversation_flow':True,'repositories':{'repo':str(tmp_path)},'verifiers':{
        'check':{'repository':'repo','argv':[sys.executable,'-m','pytest','tests/test_a.py']}}}
    unit={'verifiers':['check'],'test_paths':['tests/test_a.py']}
    assert set(work_units.checks(config,unit,'repo',tmp_path))=={'check','pytest-tests:tests/test_a.py'}


@pytest.mark.parametrize('extra',[[],['-k','one_case']])
def test_an_explicit_automatic_style_name_is_neither_replaced_nor_dropped(tmp_path,extra):
    python=project_python(tmp_path)
    explicit=[python,'-B','-m','pytest','tests/test_a.py','-q',*extra]
    config={'conversation_flow':True,'repositories':{'repo':str(tmp_path)},'verifiers':{
        'check':{'repository':'repo','argv':[python,'-B','-m','pytest','tests/test_a.py','-q']},
        'pytest-tests':{'repository':'repo','argv':explicit}}}
    unit={'verifiers':['check','pytest-tests'],'test_paths':['tests/test_a.py']}
    compiled=work_units.checks(config,unit,'repo',tmp_path)
    assert set(compiled)=={'check','pytest-tests'}
    assert compiled['pytest-tests'][:len(explicit)]==explicit
