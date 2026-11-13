"""Offline production composition controls; live native approval has separate dogfood."""
from dataclasses import replace
import json
import os
from pathlib import Path
from types import SimpleNamespace
import time

import pytest
from compute_plane.action_control import ActionRefused
from compute_plane.command_mediation import CommandFacts
from tare_tools_agent_runtime import BindingRef, CancellationToken, ExecutionControl
from relay import command_protection as protection
from relay.action_control import ActionDecision


@pytest.fixture
def host(tmp_path,monkeypatch):
    root=tmp_path/'project';root.mkdir()
    runtime=tmp_path/'runtime';runtime.mkdir()
    executable=runtime/'python.exe';executable.write_bytes(b'owned offline executable')
    cli=runtime/'codex.exe';cli.write_bytes(b'owned offline Codex fixture')
    custody=tmp_path/'custody';custody.mkdir()
    deployment={'native_profile':{'endpoint':'cli://codex','model':'gpt-6.1-sol','parameters_json':'{}'},
                'native_cli':{'executable':str(cli),'model_catalog':{'slug':'gpt-6.1-sol'}}}
    monkeypatch.setattr(protection,'sys',SimpleNamespace(platform='win32',base_prefix=str(runtime),
                        prefix=str(runtime),executable=str(executable)))
    monkeypatch.setenv('SystemRoot',str(runtime))
    monkeypatch.setattr(protection,'MxcPolicy',lambda read,write,denied,registry_read,enumerate_only=():
                        SimpleNamespace(read_only=read,read_write=write,denied=denied,enumerate_only=enumerate_only))
    monkeypatch.setattr(protection,'policy_sha256',lambda policy:'e'*64)
    native=[];effects=[];claims=set();activities=[]
    class Gate:
        def __init__(self,*args,**kwargs):self.options=kwargs
        def run(self,action,authorization,control,effect,*,public_context,before_effect):
            if not self.options['reserve_review'](action,control):raise ActionRefused('native budget refused')
            native.append(action.action_sha256)
            before_effect()
            result=effect()
            self.options['record_receipt']({'status':'approved','billing':'unknown','cleanup_verified':True})
            return result
    monkeypatch.setattr('compute_plane.codex_auto_review.CodexNativeCommandReview',Gate)
    def isolation(**options):
        def bind(**session):return options['mediation_factory'](**session)
        return bind
    monkeypatch.setattr(protection,'windows_isolation_factory',isolation)
    binding=BindingRef('work','binding','operator',1,'opaque')
    control=ExecutionControl(binding,CancellationToken(),time.monotonic()+120)
    state=SimpleNamespace(control=control)
    def claim(action,authorization):
        if action.effect_id in claims:return False
        claims.add(action.effect_id);return True
    capabilities=SimpleNamespace(claim_action=claim,names=())
    config={'repositories':{'project':str(root)},'writable':{'project':['.']},
            'command_protection':protection.prepare({'review':'codex','max_reviews':1},deployment)}
    def build():
        return protection.factory(config,deployment,custody,'Run the owned offline check once.','invocation',
            activities.append)(session_id='session',binding=binding,control=state.control,config=config,
                                custody=custody,scratch=custody/'scratch',capabilities=capabilities)
    def facts(number='1',args=None):
        return CommandFacts('command','session','project',str(root),'a'*64,'b'*64,
                            tuple(args or [str(executable),'-c','print(1)']),json.dumps({'check':number}),
                            session_id='session',effect_id=number,work_id='work',binding_id='binding',fence_seq=1)
    def effect():effects.append('effect');return {'exit_code':0,'cleanup_verified':True}
    return SimpleNamespace(**locals())


@pytest.mark.parametrize('value',[{'review':'bogus'},{'max_reviews':0},{'max_reviews':True},
                                  {'review_seconds':121},{'unknown':'value'}])
def test_invalid_protection_configuration(value):
    with pytest.raises(ValueError):protection.validate(value)


def test_granted_reference_folder_is_read_only_in_worker_isolation(host, monkeypatch, tmp_path):
    from relay.folder_access import grant
    reference = tmp_path/'old-project'; (reference/'api').mkdir(parents=True)
    (reference/'api/.env').write_text('FAKE_SECRET_FIXTURE')
    store = tmp_path/'grants.json'; grant(store, str(reference))
    host.config['folder_access'] = {'grants': str(store)}
    policies = []
    original = protection.MxcPolicy
    def capture(*args, **kwargs):
        policy = original(*args, **kwargs); policies.append(policy); return policy
    monkeypatch.setattr(protection, 'MxcPolicy', capture)
    host.build()
    assert str(reference) in policies[-1].read_only
    assert str(reference) not in policies[-1].read_write
    assert str(host.root/'.git') not in policies[-1].read_write
    assert str(reference/'api/.env') in policies[-1].denied


def test_verifier_runtime_includes_the_virtual_environments_site_packages(tmp_path):
    environment = tmp_path/'project-env'
    (environment/'Scripts').mkdir(parents=True)
    (environment/'pyvenv.cfg').write_text('home = fixture\n')
    python = environment/'Scripts/python.exe'; python.write_bytes(b'fixture; not executed')
    assert protection.verifier_runtime(python) == environment


@pytest.mark.parametrize('private,always',[(True,False),(True,True),(False,False)])
def test_named_check_uses_its_runtime_provenance_only_in_an_automatic_private_candidate(host,monkeypatch,tmp_path,private,always):
    from compute_plane.native_journal import NativeJournal
    storage=tmp_path/'storage';root=storage/'wt'/'0123abcd' if private else host.root
    root.mkdir(parents=True,exist_ok=True);monkeypatch.setenv('TARE_STORAGE_ROOT',str(storage))
    host.config['repositories']={'project':str(root)}
    argv=[str(host.executable),'-c','print(1)']
    host.config['verifiers']={'unit':{'repository':'project','argv':argv}}
    host.config['command_protection']=protection.prepare({'review':'codex' if always else 'auto','max_reviews':1,
        **({'review_policy':'always'} if always else {})},host.deployment)
    journal=NativeJournal(host.custody/'journal.jsonl')
    def requested(effect):
        journal.append({'kind':'effect_requested','effect_id':effect,'tool_call':{
            'name':'verify','call_id':'call-'+effect,'arguments_json':json.dumps({'name':'unit'})}})
    requested('named')
    port=host.build()
    result=port.execute(replace(host.facts('named'),cwd=str(root),argv=tuple(argv)),host.effect)
    skipped=private and not always
    assert bool(host.native) is not skipped and host.claims=={'named'}
    assert (result['command_protection'].get('review_basis')=='host_named_verifier') is skipped
    if skipped:
        # Exact same argv through the raw command port still requires native review.
        port.execute(replace(host.facts('raw'),cwd=str(root),argv=tuple(argv)),host.effect)
        assert len(host.native)==1
        requested('after-budget')
        port.execute(replace(host.facts('after-budget'),cwd=str(root),argv=tuple(argv)),host.effect)
        assert len(host.native)==1 and host.claims=={'named','raw','after-budget'}


def test_hidden_host_check_runtime_is_admitted_for_worker_commands_without_writes(host, tmp_path, monkeypatch):
    runtime=tmp_path/'git-runtime'; (runtime/'cmd').mkdir(parents=True); (runtime/'mingw64').mkdir()
    executable=runtime/'cmd/git.exe'; executable.write_bytes(b'owned fixture; not executed')
    with pytest.raises(ActionRefused,match='outside the configured runtime'):
        host.build().execute(host.facts(args=[str(executable),'--version']),host.effect)
    host.config['command_runtimes']=[str(executable)]
    policies=[]; original=protection.MxcPolicy
    def capture(*args,**kwargs):
        value=original(*args,**kwargs);policies.append(value);return value
    monkeypatch.setattr(protection,'MxcPolicy',capture)
    host.build().execute(host.facts(args=[str(executable),'--version']),host.effect)
    assert host.effects==['effect'] and str(runtime) in policies[-1].read_only
    assert str(runtime) not in policies[-1].read_write


def test_protected_pytest_temp_is_explicitly_inside_writable_scratch(host, monkeypatch):
    import shlex
    captured=[]; original=protection.windows_isolation_factory
    def capture(**kwargs):
        captured.append(kwargs); return original(**kwargs)
    monkeypatch.setattr(protection,'windows_isolation_factory',capture)
    mediator=host.build()
    options=shlex.split(captured[0]['environment']['PYTEST_ADDOPTS'])
    assert captured[0]['environment']['PYTHONUTF8']=='1' and captured[0]['environment']['PYTHONIOENCODING']=='utf-8'
    test_root=Path(next(value.split('=',1)[1] for value in options if value.startswith('--basetemp='))).parent
    assert Path(captured[0]['environment']['TMPDIR'])==test_root
    assert test_root.name.startswith('tare-test-') and test_root.exists()
    assert not test_root.is_relative_to(host.custody)
    assert str(test_root) in captured[0]['policy'].read_write
    assert str(host.custody/'command-tmp') in captured[0]['policy'].read_write
    # PSEC enumeration covers a whole subtree; any entry (the volume root above all) lists folders outside the scopes.
    assert captured[0]['policy'].enumerate_only==()
    mediator.close()
    assert not test_root.exists()


def test_staged_recovery_is_only_enabled_for_the_hosts_private_candidate(host,monkeypatch,tmp_path):
    captured=[];original=protection.windows_isolation_factory
    def capture(**kwargs):
        captured.append(kwargs);return original(**kwargs)
    monkeypatch.setattr(protection,'windows_isolation_factory',capture)
    host.build().close()
    assert captured[-1]['staged_effects'] is False
    from relay import native_installation
    monkeypatch.setattr(native_installation,'storage_root',lambda:tmp_path)
    candidate=tmp_path/'wt/0123abcd';candidate.mkdir(parents=True)
    metadata=tmp_path/'git/worktrees/candidate';metadata.mkdir(parents=True)
    (metadata/'commondir').write_text('../..')
    (candidate/'.git').write_text('gitdir: '+str(metadata))
    (candidate/'vendor/clone/.git').mkdir(parents=True);(candidate/'.env').write_text('FAKE_SECRET_FIXTURE')
    host.config['repositories']={'project':str(candidate)}
    host.build().close()
    assert captured[-1]['staged_effects'] is True
    assert str(candidate) in captured[-1]['policy'].read_write
    # Git may read its metadata here, a nested repository's included; other secrets stay denied.
    assert list(captured[-1]['policy'].denied)==[str(candidate/'.env')]
    assert str(metadata) in captured[-1]['policy'].read_only and str(tmp_path/'git') in captured[-1]['policy'].read_only
    assert not any(str(p) in captured[-1]['policy'].read_write for p in (candidate/'.git',metadata,tmp_path/'git'))
    assert captured[-1]['environment']['GIT_OPTIONAL_LOCKS']=='0'
    assert 'GIT_WORK_TREE' not in captured[-1]['environment']


@pytest.mark.parametrize('backend,installed',[('mxc',False),('codex',False),('mxc',True)])
def test_both_windows_backends_give_python_the_sandbox_socketpair(host,monkeypatch,backend,installed):
    captured=[];original=protection.windows_isolation_factory
    def capture(**kwargs):
        captured.append(kwargs);return original(**kwargs)
    monkeypatch.setattr(protection,'windows_isolation_factory',capture)
    shim=protection.SANDBOX_PYTHON
    if installed:  # a release installs relay inside its venv, which is already a readable runtime root
        monkeypatch.setattr(protection.sys,'prefix',str(shim.parents[1]))
    host.config['command_protection']=protection.prepare({'review':'codex','backend':backend,'max_reviews':1},host.deployment)
    host.build().close()
    environment,policy=captured[-1]['environment'],captured[-1]['policy']
    assert captured[-1]['backend']==backend and (shim/'sitecustomize.py').is_file()
    assert (environment['PYTHONPATH'],environment['TARE_SANDBOX_SOCKETPAIR'])==(str(shim),'1')
    assert (str(shim) in policy.read_only) is not installed and str(shim) not in policy.read_write


@pytest.mark.parametrize('backend',['mxc','codex'])
def test_automatic_codex_review_preserves_the_selected_sandbox(host,monkeypatch,backend):
    captured=[];original=protection.windows_isolation_factory
    def capture(**kwargs):
        captured.append(kwargs);return original(**kwargs)
    monkeypatch.setattr(protection,'windows_isolation_factory',capture)
    host.config['command_protection']=protection.prepare({'review':'auto','backend':backend,'max_reviews':1},host.deployment)
    result=host.build().execute(host.facts(),host.effect)
    assert captured[-1]['backend']==backend and len(host.native)==1 and host.effects==['effect']
    assert result['command_protection']['review']=='codex'


@pytest.mark.parametrize('review,readonly,expected_native',[('auto',True,0),('auto',False,1),('codex',True,1)])
def test_auto_codex_trusted_search_keeps_claims_but_saves_reviews_while_commands_and_always_still_review(
        host,monkeypatch,review,readonly,expected_native):
    host.capabilities.names=('search',)
    executable=host.runtime/'rg.exe';executable.write_bytes(b'owned fixture')
    monkeypatch.setattr('compute_plane.managed_process.host_executable',lambda name:str(executable))
    host.config['command_protection']=protection.prepare({'review':review,'max_reviews':1},host.deployment)
    facts=replace(host.facts(args=[str(executable),'--no-config','--files']),
                  payload_json=json.dumps({'read_only':readonly}))
    mediator=host.build();result=mediator.execute(facts,host.effect)
    assert len(host.native)==expected_native and host.effects==['effect'] and host.claims=={'1'}
    assert result['command_protection']['reserved_calls']==expected_native
    if expected_native==0:
        assert result['command_protection']['review_status']=='not_required'
        assert 'codex' not in result['command_protection']['usage']
        raw=replace(host.facts('2',args=[str(executable)]),payload_json='{"read_only":false}')
        mediator.execute(raw,host.effect)  # use the whole native review budget
        later=replace(host.facts('3',args=[str(executable),'--files']),payload_json='{"read_only":true}')
        mediator.execute(later,host.effect)
        assert len(host.native)==1 and host.claims=={'1','2','3'} and host.effects==['effect']*3


@pytest.mark.parametrize('protocol,endpoint,credential,field', [
    ('openai-codex-responses','https://example.invalid/codex','', 'reasoning'),
    ('openai-responses','https://api.openai.com/v1','OPENAI_API_KEY', 'reasoning'),
    ('anthropic-messages','https://api.anthropic.com','ANTHROPIC_API_KEY', 'output_config'),
    ('openai-chat','https://example.invalid/gemini','GEMINI_API_KEY', 'reasoning_effort'),
])
def test_action_reasoning_uses_the_connection_format_and_preserves_cache(protocol,endpoint,credential,field):
    from relay.model_choices import reasoning_parameters
    value={'native_profile':{'protocol':protocol,'endpoint':endpoint,
        'parameters_json':json.dumps({'prompt_cache_key':'same-cache','temperature':0})},
        'native_credential_env':credential}
    params=reasoning_parameters(value,'high')
    assert params[field] == ('high' if field=='reasoning_effort' else {'effort':'high'})
    assert params['prompt_cache_key']=='same-cache' and params['temperature']==0 and 'effort' not in params
    assert json.loads(value['native_profile']['parameters_json']) == {'prompt_cache_key':'same-cache','temperature':0}


def test_native_gate_executes_original_effect_and_enforces_budget_before_another_claim(host):
    mediator=host.build()
    result=mediator.execute(host.facts(),host.effect)
    assert host.effects==['effect'] and len(host.native)==1 and host.claims=={'1'}
    assert result['command_protection']['reserved_calls']==1
    assert result['command_protection']['billing']=='UNKNOWN'
    with pytest.raises(ActionRefused):mediator.execute(host.facts('2'),host.effect)
    assert host.effects==['effect'] and len(host.native)==1 and host.claims=={'1'}


@pytest.mark.parametrize('unbounded', [False, True])
def test_conversation_host_verifier_uses_the_real_mediation_gate(host,monkeypatch,unbounded):
    import sys
    from relay import execution_scope
    scope=replace(execution_scope.current(),unlimited=('time',) if unbounded else ())
    monkeypatch.setattr(execution_scope,'current',lambda:scope)
    bindings=[];original_binding=protection.windows_isolation_factory
    def bind(**kwargs):
        bindings.append(kwargs)
        return original_binding(**kwargs)
    monkeypatch.setattr(protection,'windows_isolation_factory',bind)
    protection.sys.base_prefix, protection.sys.prefix, protection.sys.executable = sys.base_prefix, sys.prefix, sys.executable
    command = [sys.executable, '-B', '-c', "print('verified')"]
    failing = [sys.executable, '-B', '-c', "print('assertion failed'); raise SystemExit(2)"]
    host.config['command_protection']['max_reviews'] = 2
    contexts=[];original=protection.factory;policies=[];policy_constructor=protection.MxcPolicy
    (host.root/'.git').mkdir()
    def capture_policy(*a,**kw):
        policy=policy_constructor(*a,**kw);policies.append(policy);return policy
    monkeypatch.setattr(protection,'MxcPolicy',capture_policy)
    def capture(config,deployment,custody,context,*args,**kwargs):
        contexts.append(json.loads(context))
        return original(config,deployment,custody,context,*args,**kwargs)
    monkeypatch.setattr(protection,'factory',capture)
    run = protection.conversation_verifier(host.control, host.custody/'conversation', host.config,
        host.deployment, host.root,
        {'repository': 'project', 'title': 'Run the named check', 'criteria': [], 'target_paths': ['.']},
        {'check': command, 'failing': failing})
    expected_commands = {name: [os.path.normcase(Path(argv[0]).resolve()), *argv[1:]]
                         for name, argv in {'check': command, 'failing': failing}.items()}
    actual_commands = {name: [os.path.normcase(argv[0]), *argv[1:]]
                       for name, argv in contexts[0]['verification']['commands'].items()}
    assert actual_commands == expected_commands
    assert 'no project application or network' in contexts[0]['verification']['scope']
    assert policies[0].enumerate_only==()
    result = run(command)
    assert result.returncode == 0 and 'verified' in result.stdout
    assert len(host.native) == 1
    result = run(failing)
    assert result.returncode == 2 and 'assertion failed' in result.stdout
    assert len(host.native) == 2
    assert bindings and all(row.get('unbounded_verification',False) is unbounded for row in bindings)
    with pytest.raises(ValueError,match='not declared'):
        run([sys.executable,'-c','print("unregistered")'])
    with pytest.raises(ValueError, match='refused'):
        run(command)  # the frozen review budget applies here too
    assert len(host.native) == 2


@pytest.mark.parametrize('backend,code,trace,unavailable', [
    ('windows-mxc', 1, 'asyncio socket.py in _fallback_socketpair in accept Timeout', True),
    ('windows-mxc', 0, 'asyncio socket.py in _fallback_socketpair in accept Timeout', False),
    ('windows-mxc', 1, 'AssertionError: expected value differs', False),
    ('codex', 1, 'asyncio socket.py in _fallback_socketpair in accept Timeout', False)])
def test_internal_ipc_diagnostic_is_not_a_failing_test_assertion(backend, code, trace, unavailable, monkeypatch, tmp_path):
    from relay import work_units
    import subprocess
    value = {'returncode': code, 'output': trace, 'process': {'confinement': {'backend': backend}}}
    diagnostic = protection.verification_limitation(value)
    assert bool(diagnostic) is unavailable
    done = subprocess.CompletedProcess(['fixture'], code, trace, '')
    done.execution_limitation = diagnostic
    token = work_units.CHECK_PORT.set(lambda *a, **kw: done)
    try:
        status, output = work_units.run_checks(tmp_path, {'focused': ['fixture']})
    finally:
        work_units.CHECK_PORT.reset(token)
    assert status == ('inconclusive' if unavailable else 'red' if code else 'green')
    if unavailable:
        assert 'ISOLATION_INTERNAL_IPC_UNAVAILABLE' in output and 'silently enable host networking' in output


# Pytest failure sections as the real sandbox printed them (2026-11-13, Python 3.12, pytest 9.1).
WSAEACCES = ('___________________ test_sync_connect_to_own_listener ___________________\n\n'
             '>               sock.connect(sa)\n'
             'E               PermissionError: [WinError 10013] An attempt was made to access a socket in a way '
             'forbidden by its access permissions\n\n..\\Lib\\socket.py:850: PermissionError\n')
ASYNCIO_CONNECT = ('_______________________ test_tcp_server_and_connect _______________________\n\n'
                   '..\\Lib\\asyncio\\windows_events.py:803: in _poll\n    value = callback(transferred, key, ov)\n'
                   '_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _\n\n'
                   '    def finish_connect(trans, key, ov):\n>       ov.getresult()\n'
                   'E       PermissionError: [WinError 5] Access is denied\n\n'
                   '..\\Lib\\asyncio\\windows_events.py:599: PermissionError\n')
ASSERTION = ('_________________________ test_real_regression _________________________\n\n'
             '>       assert 1 + 1 == 3\nE       assert (1 + 1) == 3\n\ntest_mixed.py:12: AssertionError\n')
OWN_FILE = ('__________________________ test_reads_its_data __________________________\n\n'
            ">       open(path).read()\nE       PermissionError: [WinError 5] Access is denied: "
            "'C:\\\\project\\\\data.txt'\n\ntest_data.py:4: PermissionError\n")


def pytest_report(*sections, banner='FAILURES', end=True):
    # The short summary names a denial in every case: only the sections may count.
    return ('============================= test session starts =============================\n'
            'collected 2 items\n\ntest_x.py FF                                     [100%]\n\n'
            f'================================== {banner} ===================================\n'
            + ''.join(sections) + ('=========================== short test summary info ===========================\n'
            'FAILED test_x.py::test_sync_connect_to_own_listener - PermissionError: [WinError 10013] A...\n'
            '============================== 2 failed in 0.54s ==============================\n' if end else ''))


@pytest.mark.parametrize('output,limited', [
    (pytest_report(WSAEACCES), True),
    (pytest_report(ASYNCIO_CONNECT), True),
    (pytest_report(WSAEACCES.replace('test_sync', 'ERROR at setup of test_sync'), banner='ERRORS'), True),
    (pytest_report(WSAEACCES, ASYNCIO_CONNECT), True),
    (pytest_report(ASSERTION), False),
    (pytest_report(OWN_FILE), False),  # the test's own file error inside the project: never the sandbox's
    (pytest_report(WSAEACCES, ASSERTION), False),  # one real failure keeps the whole check red
    (pytest_report(ASYNCIO_CONNECT.replace('finish_connect', 'read_config')), False),
    (pytest_report(ASSERTION + '----- Captured stdout call -----\nretrying after [WinError 10013]\n'), False),
    ('Traceback (most recent call last):\nPermissionError: [WinError 10013] An attempt was made', False),  # no sections
    (pytest_report(WSAEACCES, end=False), False)])  # a report cut off before its end
def test_a_check_whose_every_failure_is_a_denied_connection_is_a_limitation(output, limited, tmp_path):
    from relay import work_units
    import subprocess
    value = {'returncode': 1, 'output': output, 'process': {'confinement': {'backend': 'windows-mxc'}}}
    diagnostic = protection.verification_limitation(value)
    assert bool(diagnostic) is limited
    if limited:
        assert diagnostic.startswith('ISOLATION_NETWORK_DENIED: ') and 'network connection on this machine' in diagnostic
        for changed in ({'output_truncated': True}, {'returncode': 0},
                        {'process': {'confinement': {'backend': 'codex-mxc'}}}):
            assert protection.verification_limitation({**value, **changed}) is None
    done = subprocess.CompletedProcess(['fixture'], 1, output, '')
    done.execution_limitation = diagnostic
    token = work_units.CHECK_PORT.set(lambda *a, **kw: done)
    try:
        status, receipt = work_units.run_checks(tmp_path, {'focused': ['fixture']})
    finally:
        work_units.CHECK_PORT.reset(token)
    assert status == ('inconclusive' if limited else 'red')
    assert ('focused: ISOLATION_NETWORK_DENIED:' in receipt) is limited  # told apart from a timeout by its code


# A test reading the project's .env inside the real sandbox (2026-11-13, Python 3.12, pytest 9.1).
DOTENV = ('_______________________ test_settings_from_dotenv _______________________\n\n'
          '    def test_settings_from_dotenv():\n>       assert "KEY" in Path(".env").read_text()\n\n'
          'envtests\\test_reads_env.py:4: \n'
          '_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _\n\n'
          ">       return io.open(self, mode, buffering, encoding, errors, newline)\n"
          "E       PermissionError: [Errno 13] Permission denied: '.env'\n\n"
          '..\\Lib\\pathlib.py:1013: PermissionError\n')


def denied_read(path, error='[Errno 13] Permission denied'):
    return DOTENV.replace("[Errno 13] Permission denied: '.env'", f'{error}: {path}')


@pytest.mark.parametrize('output,limited', [
    (pytest_report(DOTENV), True),
    (pytest_report(denied_read("'C:\\\\project\\\\pkg\\\\.npmrc'", '[WinError 5] Access is denied')), True),
    (pytest_report(denied_read("'keys/.ssh'", '[WinError 5] Access is denied')), True),  # listing a denied folder
    (pytest_report(denied_read("'deploy\\\\.docker\\\\config.json'")), True),
    (pytest_report(denied_read('"it\'s\\\\.ENV.local"')), True),  # repr's other quotes, any case
    (pytest_report(DOTENV, DOTENV.replace('test_settings', 'test_other')), True),
    (pytest_report(DOTENV.replace('test_settings', 'ERROR at setup of test_settings'), banner='ERRORS'), True),
    (pytest_report(OWN_FILE), False),  # an ordinary file of the project
    (pytest_report(denied_read("'config\\\\.env.example'")), False),  # samples stay readable: not the sandbox
    (pytest_report(denied_read("'.ssh\\\\config'")), False),  # only the denied path's own name counts
    (pytest_report(denied_read("'.env")), False),  # a path that does not parse
    (pytest_report(DOTENV, ASSERTION), False),  # one real failure keeps the whole check red
    (pytest_report(ASSERTION.replace('assert (1 + 1) == 3', "AssertionError: KEY missing from '.env'")), False),
    (pytest_report(DOTENV.replace('E       Perm', 'E       RuntimeError: wrapped\nE       Perm')), False),
    (pytest_report(DOTENV.replace('E       ', '        ')), False),  # nothing raised that pytest showed
    ("PermissionError: [Errno 13] Permission denied: '.env'", False),  # no sections
    (pytest_report(DOTENV, end=False), False)])
def test_a_check_whose_every_failure_is_a_denied_secret_is_a_limitation(output, limited, tmp_path):
    from relay import work_units
    import subprocess
    value = {'returncode': 1, 'output': output, 'process': {'confinement': {'backend': 'windows-mxc'}}}
    diagnostic = protection.verification_limitation(value)
    assert bool(diagnostic) is limited
    if limited:
        assert diagnostic.startswith('ISOLATION_SECRET_DENIED: ') and 'secret files such as .env' in diagnostic
        assert 'network' not in diagnostic and 'the user decides' in diagnostic
        for changed in ({'output_truncated': True}, {'returncode': 0},
                        {'process': {'confinement': {'backend': 'codex-mxc'}}}):
            assert protection.verification_limitation({**value, **changed}) is None
    done = subprocess.CompletedProcess(['fixture'], 1, output, '')
    done.execution_limitation = diagnostic
    token = work_units.CHECK_PORT.set(lambda *a, **kw: done)
    try:
        status, receipt = work_units.run_checks(tmp_path, {'focused': ['fixture']})
    finally:
        work_units.CHECK_PORT.reset(token)
    assert status == ('inconclusive' if limited else 'red')
    assert ('focused: ISOLATION_SECRET_DENIED:' in receipt) is limited


def test_denied_secrets_and_connections_in_one_check_are_one_limitation():
    value = {'returncode': 1, 'output': pytest_report(WSAEACCES, DOTENV),
             'process': {'confinement': {'backend': 'windows-mxc'}}}
    diagnostic = protection.verification_limitation(value)
    assert diagnostic.startswith('ISOLATION_SECRET_DENIED: ') and 'a secret file or a network connection' in diagnostic
    assert 'silently enable host networking' in diagnostic
    assert protection.verification_limitation({**value, 'output': pytest_report(WSAEACCES, DOTENV, ASSERTION)}) is None


def test_cancelled_control_sends_no_native_request_and_runs_no_effect(host):
    mediator=host.build();host.control.cancellation.cancel()
    with pytest.raises(ActionRefused):mediator.execute(host.facts(),host.effect)
    assert host.effects==host.native==[] and host.claims==set()


def test_expired_control_runs_no_effect(host):
    host.state.control=replace(host.control,deadline_monotonic=time.monotonic()-1)
    with pytest.raises((ActionRefused,ValueError)):host.build().execute(host.facts(),host.effect)
    assert host.effects==host.native==[]


def test_source_change_while_native_review_waits_is_refused(host,monkeypatch):
    script=host.root/'check.py';script.write_text('print(1)')
    class Changed:
        def __init__(self,*args,**kwargs):pass
        def run(self,action,authorization,control,effect,*,public_context,before_effect):
            script.write_text('print(2)');before_effect();return effect()
    monkeypatch.setattr('compute_plane.codex_auto_review.CodexNativeCommandReview',Changed)
    with pytest.raises(ActionRefused,match='snapshot changed'):
        host.build().execute(host.facts(args=[str(host.executable),str(script)]),host.effect)
    assert host.effects==[]


def test_native_denial_cannot_select_an_unreviewed_path(host,monkeypatch):
    class Denied:
        def __init__(self,*args,**kwargs):pass
        def run(self,*args,**kwargs):raise ActionRefused('native denied')
    monkeypatch.setattr('compute_plane.codex_auto_review.CodexNativeCommandReview',Denied)
    with pytest.raises(ActionRefused,match='native denied'):host.build().execute(host.facts(),host.effect)
    assert host.effects==[]


def test_saved_cli_change_is_refused_without_refreshing_its_identity(host):
    before=dict(host.config['command_protection']);host.cli.write_bytes(b'changed')
    with pytest.raises(ValueError,match='saved Codex executable changed'):host.build()
    assert host.config['command_protection']==before


def test_policy_refuses_pty_and_platform_without_fallback(host,monkeypatch):
    host.config['terminal']=True
    with pytest.raises(ValueError,match='interactive terminal'):host.build()
    host.config.pop('terminal')
    monkeypatch.setattr(protection,'sys',SimpleNamespace(platform='linux'))
    with pytest.raises(ValueError,match='require Windows'):host.build()


def test_own_reviews_keep_low_classifier_high_reviewer_and_require_both_approvals(host,monkeypatch):
    efforts=[]
    class Port:
        def __init__(self,deployment,**kwargs):efforts.append(json.loads(deployment['native_profile']['parameters_json'])['effort'])
    monkeypatch.setattr(protection,'TareActionClassifier',Port)
    monkeypatch.setattr(protection,'TareActionReviewer',Port)
    class Broker:
        def __init__(self,*args):pass
        def assess(self,*args):return ActionDecision(False,'own reviewer denied')
    monkeypatch.setattr(protection,'ActionReviewBroker',Broker)
    host.config['command_protection']=protection.prepare({'review':'both','max_reviews':3},host.deployment)
    with pytest.raises(ActionRefused,match='assessment denied'):host.build().execute(host.facts(),host.effect)
    assert efforts==['low','high'] and host.effects==host.native==[] and host.claims==set()


@pytest.mark.parametrize('levels,expected', [(None, 'xhigh'), (['low', 'medium'], None)])
def test_a_local_implementers_reviewer_binds_at_a_level_its_model_declares(host, monkeypatch, levels, expected):
    """Local-only, Action review Selected model (tare): the local judge classifies, the implementer's own local model
    reviews. Its policy level high becomes xhigh, the lowest level qwen38-gsq declares at or above it."""
    from relay import execution_scope as scope
    from relay.execution_team import reasoning
    from relay.native_catalog import select
    local = select('qwen38-gsq')
    if levels:
        local['native_reasoning_levels'] = levels
    built = []
    monkeypatch.setattr(protection, 'TareJevActionClassifier', lambda **kwargs: built.append(('classifier', kwargs['provider'])))
    monkeypatch.setattr(protection, 'TareActionReviewer', lambda deployment, **kwargs: built.append(('reviewer', reasoning(deployment))))
    monkeypatch.setattr(protection, 'TareActionClassifier', lambda *a, **kw: pytest.fail('The local judge classifies here.'))
    config = {**host.config, 'command_protection': protection.prepare({'review': 'tare', 'max_reviews': 2}, local)}
    def bind():
        return protection.factory(config, local, host.custody, 'Run the owned offline check once.', 'invocation')(
            session_id='session', binding=host.binding, control=host.control, config=config, custody=host.custody,
            scratch=host.custody/'scratch', capabilities=host.capabilities)
    with scope.activated(scope.Scope(only='local')):
        if expected is None:
            with pytest.raises(ValueError, match='declares no reasoning level at or above high .it declares low, medium.'):
                bind()
        else:
            bind()
    assert built == ([('classifier', 'local'), ('reviewer', 'xhigh')] if expected else [('classifier', 'local')])


def test_jev_and_following_reviewer_receive_the_same_observed_reference_grants(host, monkeypatch, tmp_path):
    from relay.folder_access import grant
    reference = tmp_path/'approved-evidence'; reference.mkdir()
    store = tmp_path/'grants.json'; grant(store, str(reference))
    host.config['folder_access'] = {'grants': str(store)}
    ports = []
    class Port:
        def __init__(self, *args, **kwargs):
            self.facts = kwargs['facts_factory']; ports.append(self)
    monkeypatch.setattr(protection, 'TareJevActionClassifier', Port)
    monkeypatch.setattr(protection, 'TareActionReviewer', Port)
    class Broker:
        def __init__(self, *args): pass
        def assess(self, action, *args):
            one, two = [port.facts(action) for port in ports]
            assert one == two
            assert one['read_only_references'] == [str(reference)]
            assert str(reference) not in one['command_write_roots'] and one['network'] == 'denied'
            assert one['unknowns'] and one['referenced_code'] == []
            return ActionDecision(False, 'Offline stop after checking the actual composed inputs.')
    monkeypatch.setattr(protection, 'ActionReviewBroker', Broker)
    host.config['command_protection'] = protection.prepare({'review': 'tare', 'classifier': 'jev',
        'review_policy': 'on_demand'}, host.deployment)
    with pytest.raises(ActionRefused):
        host.build().execute(host.facts(), host.effect)
    assert len(ports) == 2 and host.effects == [] and host.claims == set()


def test_context_is_observed_at_command_time_and_changes_invalidate_waiting_approval(host,monkeypatch):
    context=['Initial task']
    host.config['command_protection']['max_reviews']=2
    factory=protection.factory(host.config,host.deployment,host.custody,lambda:context[0],'request')
    context[0]='Run this command'
    class ChangedContext:
        def __init__(self,*args,**kwargs):pass
        def run(self,action,authorization,control,effect,*,public_context,before_effect):
            assert 'Run this command' in public_context
            context[0]='Stop; do not run it';before_effect();return effect()
    monkeypatch.setattr('compute_plane.codex_auto_review.CodexNativeCommandReview',ChangedContext)
    mediator=factory(session_id='session',binding=host.binding,control=host.control,config=host.config,
        custody=host.custody,scratch=host.custody/'scratch',capabilities=host.capabilities)
    with pytest.raises(ActionRefused,match='snapshot changed'):mediator.execute(host.facts(),host.effect)
    assert host.effects==[]


def test_large_context_does_not_prevent_startup_but_refuses_command_before_review(host):
    factory=protection.factory(host.config,host.deployment,host.custody,'x'*8193,'request')
    mediator=factory(session_id='session',binding=host.binding,control=host.control,config=host.config,
        custody=host.custody,scratch=host.custody/'scratch',capabilities=host.capabilities)
    with pytest.raises(ActionRefused,match='context exceeds'):mediator.execute(host.facts(),host.effect)
    assert host.effects==host.native==[] and host.claims==set()


@pytest.fixture
def jev_composition(host,monkeypatch):
    from compute_plane.action_assessment import AssessmentObservation
    calls=[];choice=['ROUTINE_ELIGIBLE']
    class Jev:
        def __init__(self,**kwargs):self.kwargs=kwargs
        def assess(self,request,control):
            calls.append('jev')
            return AssessmentObservation('classifier','tare','jev-fixture',request.request_sha256,
                request.action.action_sha256,request.action.context_sha256,request.action.policy_sha256,
                'OBSERVED',choice[0],('FIXTURE',),0,'offline-jev',usage_json='{"input_tokens":300,"output_tokens":0}')
    class Reviewer:
        def __init__(self,*args,**kwargs):pass
        def assess(self,request,control):
            calls.append('reviewer')
            return AssessmentObservation('reviewer','tare','review-fixture',request.request_sha256,
                request.action.action_sha256,request.action.context_sha256,request.action.policy_sha256,
                'OBSERVED','APPROVE_RECOMMENDED',('FIXTURE',),0,'offline-reviewer')
    monkeypatch.setattr(protection,'TareJevActionClassifier',Jev)
    monkeypatch.setattr(protection,'TareActionReviewer',Reviewer)
    def forbidden(*args,**kwargs):raise AssertionError('Frontier classifier must not be created for Jev')
    monkeypatch.setattr(protection,'TareActionClassifier',forbidden)
    def select(review='codex',maximum=3,policy='on_demand'):
        host.config['command_protection']=protection.prepare({'review':review,'max_reviews':maximum,
            'classifier':'jev','review_policy':policy},host.deployment)
        return host.build()
    return SimpleNamespace(host=host,calls=calls,choice=choice,select=select)


@pytest.mark.parametrize('review',['codex','tare','both'])
def test_jev_routine_runs_once_without_native_or_frontier_review(jev_composition,review):
    h=jev_composition;m=h.select(review,maximum=1)
    result=m.execute(h.host.facts(),h.host.effect)
    assert h.calls==['jev'] and h.host.native==[] and h.host.effects==['effect']
    report=result['command_protection']
    assert report['classifier']=='jev' and report['review_status']=='not_required'
    assert report['reserved_calls']==1 and report['classification']=='ROUTINE_ELIGIBLE'


@pytest.mark.parametrize('review,expected,native_count,maximum',[
    ('codex',['jev'],1,2),('tare',['jev','reviewer'],0,2),('both',['jev','reviewer'],1,3)])
def test_jev_escalation_uses_only_the_configured_reviews(jev_composition,review,expected,native_count,maximum):
    h=jev_composition;h.choice[0]='REVIEW_REQUIRED'
    result=h.select(review,maximum).execute(h.host.facts(),h.host.effect)
    assert h.calls==expected and len(h.host.native)==native_count and h.host.effects==['effect']
    assert result['command_protection']['reserved_calls']==maximum
    assert result['command_protection']['review_status']=='approved'


@pytest.mark.parametrize('verdict',['DENY_RECOMMENDED','INSUFFICIENT_EVIDENCE'])
def test_jev_denial_and_missing_context_do_not_invoke_a_reviewer(jev_composition,verdict):
    h=jev_composition;h.choice[0]=verdict
    with pytest.raises(ActionRefused):h.select('both').execute(h.host.facts(),h.host.effect)
    assert h.calls==['jev'] and h.host.native==h.host.effects==[] and h.host.claims==set()


def test_jev_native_budget_is_checked_before_claiming_an_escalated_command(jev_composition):
    h=jev_composition;h.choice[0]='REVIEW_REQUIRED'
    with pytest.raises(ActionRefused):h.select('codex',maximum=1).execute(h.host.facts(),h.host.effect)
    assert h.calls==['jev'] and h.host.native==h.host.effects==[] and h.host.claims==set()


def test_jev_always_review_still_requires_the_native_gate(jev_composition):
    h=jev_composition
    result=h.select('codex',maximum=2,policy='always').execute(h.host.facts(),h.host.effect)
    assert len(h.host.native)==1 and h.host.effects==['effect']
    assert result['command_protection']['review_status']=='approved'


@pytest.mark.parametrize('readonly,policy,expected',[(True,'on_demand',[]),
    (False,'on_demand',['jev']),(True,'always',['jev','reviewer'])])
def test_host_owned_search_does_not_assess_every_read_but_raw_commands_keep_policy(
        jev_composition,monkeypatch,readonly,policy,expected):
    h=jev_composition;h.host.capabilities.names=('search',)
    executable=h.host.runtime/'rg.exe';executable.write_bytes(b'owned search fixture')
    monkeypatch.setattr('compute_plane.managed_process.host_executable',lambda name:str(executable))
    facts=replace(h.host.facts(args=[str(executable),'--no-config','--files']),
                  payload_json=json.dumps({'read_only':readonly}))
    h.select('tare',policy=policy).execute(facts,h.host.effect)
    assert h.calls==expected and h.host.effects==['effect'] and not h.host.native


@pytest.mark.parametrize('allow', [False, True])
def test_unavailable_jev_uses_exact_human_approval_without_frontier_fallback(jev_composition, monkeypatch, allow):
    h = jev_composition
    original = protection.TareJevActionClassifier.assess
    def unavailable(self, request, control):
        result = replace(original(self, request, control), status='ABSTAINED', verdict=None,
                         reason_codes=('ASSESSMENT_INPUT_OVERFLOW',))
        from dataclasses import asdict  # recorded under the assessed action, as the real port does
        self.kwargs['journal_factory']().append(dict(asdict(result), kind='action_assessment_result'))
        return result
    monkeypatch.setattr(protection.TareJevActionClassifier, 'assess', unavailable)
    asked = []
    def approve(action, public, control):
        h.calls.append('human'); asked.append(action.action_sha256)
        time.sleep(.05)  # a later answer: the executed action gets a new window
        return allow
    monkeypatch.setattr('relay.command_permission.approve', approve)
    mediator = h.select('tare')
    if allow:
        result = mediator.execute(h.host.facts(), h.host.effect)
        assert h.host.effects == ['effect']
        assert result['command_protection']['usage']['classifier'] == {'input_tokens': 300, 'output_tokens': 0}
    else:
        with pytest.raises(ActionRefused): mediator.execute(h.host.facts(), h.host.effect)
        assert not h.host.effects and not h.host.claims
    assert h.calls == ['jev', 'human'] and not h.host.native
    rows=[r.event() for r in protection.NativeJournal(h.host.custody/'command-reviews.jsonl').snapshot().records]
    decision=next(r for r in rows if r['kind']=='command_review_decision')
    assert decision['assessments'][0]['reason_codes'] == ['ASSESSMENT_INPUT_OVERFLOW']
    assert decision['action_sha256'] == asked[0]  # the action the user was asked about
    if allow:  # executed under the same effect, with only its deadline renewed
        executed = next(r for r in rows if r['kind'] == 'command_protected_effect')
        assert executed['effect_id'] == decision['effect_id'] and executed['action_sha256'] != asked[0]


def test_unavailable_jev_cannot_skip_an_always_required_review(jev_composition, monkeypatch):
    h = jev_composition
    original = protection.TareJevActionClassifier.assess
    monkeypatch.setattr(protection.TareJevActionClassifier, 'assess', lambda self, request, control:
        replace(original(self, request, control), status='UNAVAILABLE', verdict=None))
    def forbidden(*args): raise AssertionError('A required review must not be bypassed')
    monkeypatch.setattr('relay.command_permission.approve', forbidden)
    with pytest.raises(ActionRefused): h.select('tare', policy='always').execute(h.host.facts(), h.host.effect)
    assert not h.host.effects and not h.host.native


def test_jev_profile_denies_explicit_credentials_before_classification(jev_composition):
    h=jev_composition;secret=h.host.root/'.env';secret.write_text('FAKE_SECRET_FIXTURE')
    m=h.select()
    with pytest.raises(ActionRefused,match='denied'):
        m.execute(h.host.facts(args=[str(h.host.executable),str(secret)]),h.host.effect)
    assert h.calls==[] and h.host.native==h.host.effects==[]


def test_secrets_are_denied_at_any_depth_without_a_classifier(host, monkeypatch):
    captured=[];original=protection.windows_isolation_factory
    def capture(**kwargs):
        captured.append(kwargs);return original(**kwargs)
    monkeypatch.setattr(protection,'windows_isolation_factory',capture)
    holds=[]
    class Hold:  # the real one opens Windows handles (tests/test_sandbox_secrets.py)
        def __init__(self,paths):self.paths,self.closed=list(paths),False;holds.append(self)
        def close(self):self.closed=True
    monkeypatch.setattr(protection.sandbox_secrets,'Hold',Hold)
    for name in ('.env','services/api/.env','services/api/.env.example','pkg/.npmrc','.git/config','src/app.py'):
        path=host.root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('FAKE_SECRET_FIXTURE')
    mediator=host.build()  # Codex review: neither Jev nor the local classifier
    assert list(captured[-1]['policy'].denied)==sorted(str(host.root/p) for p in
        ('.env','.git','pkg/.npmrc','services/api/.env'))
    # The project is writable: what a renamed folder could carry off is held, not what sits in the project's root.
    assert holds[-1].paths==[str(host.root/'pkg/.npmrc'),str(host.root/'services/api/.env')] and not holds[-1].closed
    with pytest.raises(ActionRefused,match='denied'):
        mediator.execute(host.facts(args=[str(host.executable),str(host.root/'services/api/.env')]),host.effect)
    assert host.effects==host.native==[]
    mediator.close()
    assert holds[-1].closed
    def unavailable(paths):raise ValueError('tare could not hold it')
    monkeypatch.setattr(protection.sandbox_secrets,'Hold',unavailable)
    with pytest.raises(ValueError,match='could not hold'):host.build()
    monkeypatch.setattr(protection.sandbox_secrets,'Hold',Hold)
    for n in range(29):  # with the four above, one more than the sandbox can deny
        (host.root/f'p{n:02d}').mkdir();(host.root/f'p{n:02d}/.env').write_text('FAKE_SECRET_FIXTURE')
    with pytest.raises(ValueError,match='33 secret files or folders'):
        host.build()


def test_legacy_saved_protection_shape_is_not_migrated_implicitly(host):
    before=dict(host.config['command_protection'])
    assert 'classifier' not in before and 'review_policy' not in before
    assert protection.prepare(before,host.deployment)==before==protection.saved(before)



def test_command_temp_is_separate_from_owner_effect_receipts(jev_composition,monkeypatch):
    h=jev_composition
    captured=[]
    original=protection.MxcPolicy
    def capture(*args,**kwargs):
        policy=original(*args,**kwargs)
        captured.append(policy)
        return policy
    monkeypatch.setattr(protection,'MxcPolicy',capture)
    h.select()
    policy=captured[0]
    owner_scratch=h.host.custody/'scratch'
    child_temp=h.host.custody/'command-tmp'
    assert str(child_temp) in policy.read_write
    assert all(not owner_scratch.resolve().is_relative_to(Path(p).resolve())
               for p in (*policy.read_only,*policy.read_write))
    assert not owner_scratch.exists()
