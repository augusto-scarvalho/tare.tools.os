"""Ways of working, phase 2: a child implementer in a worktree; tare verifies before and after and applies only a
VERIFIED change whose files still hold the bytes the child started from."""
import hashlib
import json
from pathlib import Path

import pytest

from relay import challenger as c, delegation, native_agent, turn_engine, work_units
from test_native_agent import Client, deployment, setup  # noqa: F401 (fixture)
from test_work_console import graph_root  # noqa: F401


@pytest.mark.parametrize('unbounded', [False, True])
def test_host_checks_honor_the_operator_time_scope(tmp_path,monkeypatch,unbounded):
    import subprocess
    from dataclasses import replace
    from relay import execution_scope
    scope=replace(execution_scope.current(),unlimited=('time',) if unbounded else ())
    monkeypatch.setattr(execution_scope,'current',lambda:scope)
    observed=[]
    def check(argv,**kwargs):
        observed.append(kwargs['timeout'])
        return subprocess.CompletedProcess(argv,0,'passed','')
    token=work_units.CHECK_PORT.set(check)
    try:
        assert work_units.run_checks(tmp_path,{'test':['declared-check']})[0]=='green'
    finally:
        work_units.CHECK_PORT.reset(token)
    assert observed==[None if unbounded else 300]


@pytest.mark.parametrize('unbounded', [False, True])
def test_host_checks_run_a_real_managed_process_with_the_operator_time_scope(tmp_path,monkeypatch,unbounded):
    import math
    import sys
    from dataclasses import replace
    from compute_plane import managed_process
    from relay import execution_scope
    scope=replace(execution_scope.current(),unlimited=('time',) if unbounded else ())
    monkeypatch.setattr(execution_scope,'current',lambda:scope)
    observed=[];original=managed_process.run_pipe_process
    def run(*args,**kwargs):
        observed.append(kwargs['timeout'])
        return original(*args,**kwargs)
    monkeypatch.setattr(managed_process,'run_pipe_process',run)
    token=work_units.CHECK_PORT.set(None)
    try:
        state,output=work_units.run_checks(tmp_path,{'check':[sys.executable,'-B','-c','print("verified")']})
    finally:
        work_units.CHECK_PORT.reset(token)
    assert state=='green' and 'verified' in output
    assert len(observed)==1
    assert math.isinf(observed[0]) if unbounded else observed[0]==300


@pytest.mark.parametrize('fault',['unplanned','ambiguous','missing','invalid','outside'])
def test_private_graph_binding_requires_one_valid_planned_target(tmp_path,graph_root,fault):
    from relay.conversation_tasks import BACKLOG_CHECK
    tree=tmp_path/'candidate';tree.mkdir()
    path=tree/'work-graph.json';path.write_bytes((graph_root/'work-graph.json').read_bytes())
    unit={'target_paths':['work-graph.json'],'verifiers':[BACKLOG_CHECK]}
    if fault=='unplanned':unit['verifiers']=[]
    if fault=='ambiguous':unit['target_paths'].append('other.json')
    if fault=='missing':path.unlink()
    if fault=='invalid':path.write_text('{}')
    if fault=='outside':unit['target_paths']=[str(graph_root/'work-graph.json')]
    assert work_units.planned_backlog(unit,'repo',tree) is None


def test_planned_graph_worker_reuses_backlog_transactions_in_private_candidate(setup,graph_root,monkeypatch):
    from graph_backlog.jsonutil import compute_revision_hash
    from relay import conversation_tasks as tasks
    from test_backlog_delivery import record,read
    config,custody,root=setup
    path=root/'work-graph.json';path.write_bytes((graph_root/'work-graph.json').read_bytes())
    repository(root)
    unit={'title':'Record real progress','target_paths':['work-graph.json'],'criteria':['Keep remaining work'],
          'verifiers':[tasks.BACKLOG_CHECK],'test_paths':[]}
    config.update(conversation_flow=True,writable={'repo':['work-graph.json']},
        verifiers=tasks.host_checks(config,{**unit,'repository':'repo'}))
    monkeypatch.setattr(work_units,'routes',lambda *a,**kw:iter([('worker',deployment())]))
    real_run=native_agent.run_session
    def worker(child,dep,session,instruction,**kwargs):
        candidate=Path(child['repositories']['repo'])/'work-graph.json'
        assert child.get('project_graph')=={'repository':'repo','path':str(candidate)}
        assert candidate!=path and 'project_graph' not in config
        def steps():
            yield 'backlog_read',{'work_id':'ready'}
            yield 'backlog_record',{'record':record(remaining=['One real obligation.']),
                'expected_revision':compute_revision_hash(read(candidate))}
            yield 'tare_deliver',{'summary':'Recorded progress with remaining obligation.'}
        client=Client(steps=steps())
        return real_run(child,dep,session,instruction,client=client,local_counter=client,
            limits=kwargs['limits'],delegated_leaf=True)
    monkeypatch.setattr(native_agent,'run_session',worker)
    result=work_units.run_unit(custody,{'config':config,'conversation_flow':True,'deployment':deployment(),
        'limits':{'max_model_calls':30},'deadline_utc':None},unit,lambda:False,
        review=lambda *a:{'verdict':'ACCEPT','sources':[],'reference_sources':[]})
    assert result['verdict']=='VERIFIED' and result['applied'],result.get('application')
    graph=read(path);assert graph['nodes'][0]['completion']['status']=='PARTIAL'
    assert graph['nodes'][0]['completion']['residuals']==['One real obligation.']
    assert graph['revision']==compute_revision_hash(graph) and 'project_graph' not in config


def test_machine_reference_and_runtime_hints_preserve_the_existing_brief_prefix():
    unit={'title':'Compare evidence','target_paths':['result.txt'],'criteria':['Preserve evidence'],
          'planner_advice':'Observed source and concrete steps.'}
    original=work_units.brief(unit,{'check':['fixture']},'Current correction.')
    updated=work_units.brief(unit,{'check':['fixture']},'Current correction.',
        references=[r'C:\reference'],executables=[r'C:\runtime\git.exe'])
    assert updated.startswith(original+'\n')
    assert 'folder_read' in updated and r'C:\reference' in updated and r'C:\runtime\git.exe' in updated
    reviewed=work_units.brief(unit,{'check':['fixture']},'Current correction.',
        references=[r'C:\reference'],executables=[r'C:\runtime\git.exe'],host_review=True)
    assert reviewed.startswith(updated+'\n')  # review guidance appends; cached planner/machine prefix stays exact


@pytest.mark.parametrize('before,after,changed,protected,retry,expected', [
    ('red', 'green', {'a.py': b'2'}, [], 'red', 'VERIFIED'),
    ('green', 'green', {'a.py': b'2'}, [], 'red', 'VERIFIED'),          # the change keeps them green
    ('green', 'red', {'a.py': b'2'}, [], 'green', 'VERIFIED'),          # a flaky check, green on the second run
    ('green', 'red', {'a.py': b'2'}, [], 'red', 'NOT_VERIFIED'),
    ('red', 'red', {'a.py': b'2'}, [], 'red', 'NOT_VERIFIED'),
    ('red', 'green', {}, [], 'red', 'INCONCLUSIVE'),                    # nothing changed
    ('red', 'green', {'tests/test_a.py': b''}, ['tests/test_a.py'], 'red', 'INCONCLUSIVE'),  # it moved the ruler
    ('red', 'inconclusive', {'a.py': b'2'}, [], 'red', 'INCONCLUSIVE'),
])
def test_the_verdict_comes_from_the_checks_never_from_the_implementer(before, after, changed, protected, retry, expected):
    assert work_units.verdict(before, after, changed, protected, lambda: retry)[0] == expected


def repository(root):
    c.git(root, 'init'); c.git(root, 'config', 'user.name', 'T'); c.git(root, 'config', 'user.email', 't@localhost')
    c.git(root, 'add', '.'); c.git(root, '-c', 'commit.gpgsign=false', 'commit', '-m', 'base')


def test_a_verified_change_is_applied_only_where_the_bytes_did_not_move(tmp_path):
    root, tree = tmp_path/'root', tmp_path/'tree'
    root.mkdir(); (root/'a.py').write_bytes(b'value = 1\n'); (root/'b.py').write_bytes(b'b = 1\n')
    repository(root)
    from relay.challenger_workspace import snapshot
    base = snapshot(root, tree)
    changed = {'a.py': b'value = 2\n'}
    assert work_units.apply(root, tree, base, changed, ['b.py']) == ('OUTSIDE', ['a.py'])
    (root/'a.py').write_bytes(b'value = 9\n')  # the session edited it meanwhile
    assert work_units.apply(root, tree, base, changed, ['.']) == ('CONFLICT', ['a.py'])
    assert (root/'a.py').read_bytes() == b'value = 9\n'  # nothing written
    (root/'a.py').write_bytes(b'value = 1\n')
    assert work_units.apply(root, tree, base, changed, ['.']) == ('APPLIED', ['a.py'])
    assert (root/'a.py').read_bytes() == b'value = 2\n'
    # A Windows checkout holds b.py as CRLF while git's base is LF: the same text, not a conflict (bd6ee62).
    (root/'b.py').write_bytes(b'b = 1\r\n')
    assert work_units.apply(root, tree, base, {'b.py': b'b = 2\n'}, ['.']) == ('APPLIED', ['b.py'])


@pytest.mark.parametrize('reference',[False,True])
def test_the_session_hands_a_unit_to_a_child_and_gets_it_back_verified(setup, tmp_path, monkeypatch,reference):
    config, custody, root = setup
    repository(root)
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'storage'))
    monkeypatch.delenv('TARE_DELEGATION_POLICY')
    delegation.save_experiments(implement_child=True)
    monkeypatch.setattr(turn_engine, 'policy', lambda: {'implementer': [['child-route']]})
    monkeypatch.setattr(turn_engine, 'available', lambda name: (deployment(), None))
    digest = hashlib.sha256(b'value = 1\n').hexdigest()
    extra=[]
    if reference:
        from relay.folder_access import grant
        external=tmp_path/'reference';external.mkdir();(external/'facts.md').write_text('Observed facts.\n')
        store=tmp_path/'grants.json';grant(store,str(external))
        config['folder_access']={'grants':str(store)}
        extra=[('folder_read',{'folder':str(external),'path':'facts.md'})]
    child = Client(steps=[*extra,('edit', {'repository': 'repo', 'path': 'a.py', 'sha256': digest,
                                    'search': 'value = 1', 'replace': 'value = 2'}),
                          ('verify', {'name': 'unit'}), ('tare_deliver', {'summary': 'Set the value to 2.'})])
    monkeypatch.setattr(native_agent, 'inference_client', lambda *a, **kw: child)
    from compute_plane import native_models
    monkeypatch.setattr(native_models, 'NativeHttpClient', lambda *a, **kw: child)
    unit = {'title': 'Set the value to 2', 'target_paths': ['a.py'], 'criteria': ['a.py holds 2'], 'verifiers': ['unit']}
    parent = Client(steps=[('implement', unit), ('tare_deliver', {'summary': 'Done.'})])
    result = native_agent.run_session(config, deployment(), custody, 'Change the value.', client=parent,
                                      local_counter=parent, limits={'max_model_calls': 16})
    assert result['status'] == 'PROPOSED' and child.generations == 3+len(extra)
    assert (root/'a.py').read_bytes() == b'value = 2\n'  # verified red to green, then applied
    ledger = json.loads((custody/'work.json').read_text(encoding='utf-8'))
    (key, entry), = ledger['units'].items()
    assert entry['state'] == 'APPLIED' and entry['history'][-1]['verdict'] == 'VERIFIED'
    receipt = json.loads(next((custody/'units').glob('*/receipt.json')).read_text(encoding='utf-8'))
    assert receipt['rungs'][0]['before_state'] == 'red' and receipt['rungs'][0]['after_state'] == 'green'
    assert not any((tmp_path/'storage'/'wt').iterdir())  # the child's worktree is gone
    child_state = native_agent.read_json(next((custody/'units').glob('*/session-1'))/'native-agent.json')
    assert child_state.get('delegated_leaf') and 'delegation' not in child_state
    if reference:
        assert str(external) in child_state['instruction'] and 'folder_read' in child_state['instruction']
        assert (external/'facts.md').read_text()=='Observed facts.\n'


def test_each_ecosystem_runs_only_the_units_tests():
    n = work_units.narrowed
    assert n('pytest', 'py', ['tests/test_a.py'])[-1] == 'tests/test_a.py'
    assert n('pytest', 'py', ['tests/test_a.py::test_b'])[-1] == 'tests/test_a.py::test_b'  # one test
    assert n('cargo', 'cargo', ['tests/api.rs::x']) == ['cargo', 'test', '--test', 'api']
    assert n('npm', 'npm', ['src/a.test.ts']) == ['npm', 'test', '--', 'src/a.test.ts']
    assert n('cargo', 'cargo', ['tests/api.rs', 'tests/db.rs']) == ['cargo', 'test', '--test', 'api', '--test', 'db']
    assert n('go', 'go', ['pkg/a/a_test.go', 'pkg/a/b_test.go']) == ['go', 'test', './pkg/a']
    assert n('maven', 'mvn', ['src/test/java/ATest.java']) == ['mvn', '-q', 'test', '-Dtest=ATest']
    assert n('gradle', 'gradlew', ['src/test/kotlin/BTest.kt']) == ['gradlew', 'test', '--tests', 'BTest']
    assert n('dotnet', 'dotnet', ['T/CTests.cs']) == ['dotnet', 'test', '--filter', 'FullyQualifiedName~CTests']


@pytest.mark.parametrize('file_count', [1, 3])
def test_conversation_unit_runs_only_its_pytest_scope_through_final_application(setup, tmp_path, monkeypatch, file_count):
    import copy
    import subprocess
    import sys
    from pathlib import Path
    config, custody, root = setup
    repository(root)
    (root/'tests').mkdir()
    (root/'tests/test_selected.py').write_text(
        'from pathlib import Path\ndef test_value(): assert "value = 2" in Path("a.py").read_text()\n')
    (root/'tests/test_unrelated.py').write_text('def test_unrelated(): assert False, "outside unit scope"\n')
    paths = ['tests/test_selected.py']
    for i in range(1, file_count):
        path = f'tests/test_selected_{i}.py'
        paths.append(path)
        (root/path).write_text('def test_value(): pass\n')
    python = sys.executable
    monkeypatch.setattr(turn_engine, 'project_python', lambda path: python)
    config.update(conversation_flow=True, verifiers={
        'tests': {'repository': 'repo', 'argv': [python, '-m', 'pytest', '-q']}})
    original = copy.deepcopy(config)
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'storage'))
    monkeypatch.setattr(work_units, 'routes', lambda *a, **kw: iter([('worker', deployment())]))
    def worker(child, *args, **kwargs):
        (Path(child['repositories']['repo'])/'a.py').write_text('value = 2\n')
        return {'status': 'PROPOSED', 'model_calls': 1}
    monkeypatch.setattr(native_agent, 'run_session', worker)
    commands = []
    def run(argv, **kwargs):
        commands.append(list(argv))
        if file_count == 3:
            from types import SimpleNamespace
            selected = [p for p in paths if p in argv]
            assert selected
            assert kwargs['timeout'] == 300
            if 120 * len(selected) > kwargs['timeout']:
                raise subprocess.TimeoutExpired(argv, kwargs['timeout'])
            red = 'value = 2' not in (Path(kwargs['cwd'])/'a.py').read_text()
            return SimpleNamespace(returncode=int(red), stdout='', stderr='')
        return subprocess.run(argv, **kwargs)
    token = work_units.CHECK_PORT.set(run)
    try:
        result = work_units.run_unit(custody, {'config': config, 'conversation_flow': True,
            'deployment': deployment(), 'limits': {'max_model_calls': 30}, 'deadline_utc': None},
            {'title': 'Set two', 'target_paths': ['a.py'], 'criteria': ['value equals two'],
             'verifiers': ['tests'], 'test_paths': paths}, lambda: False,
            review=lambda *a: {'verdict': 'ACCEPT', 'sources': [], 'reference_sources': []})
    finally:
        work_units.CHECK_PORT.reset(token)
    assert result['verdict'] == 'VERIFIED' and result['applied'] is True
    assert commands and all(sum(p in argv for p in paths) == 1 and '--basetemp' in argv for argv in commands)
    assert all(any(p in argv for argv in commands) for p in paths)
    assert all('tests/test_unrelated.py' not in argv for argv in commands)
    assert config == original
    receipt = json.loads(Path(result['receipt']).read_text(encoding='utf8'))
    for field, code in (('checks_before', 1), ('checks_after', 0)):
        for path in paths:
            assert f'$ pytest-tests:{path} (exit {code})' in receipt['result'][field]
    assert receipt['rungs'][0]['before_state'] == 'red'
    assert receipt['rungs'][0]['after_state'] == 'green'


def test_scoped_pytest_keeps_custom_named_obligations_and_explicit_scratch(tmp_path, monkeypatch):
    import sys
    root = tmp_path/'project'; root.mkdir()
    python = sys.executable
    monkeypatch.setattr(turn_engine, 'project_python', lambda path: python)
    explicit = [python, '-m', 'pytest', '-q', 'tests/test_required.py', '--basetemp=owned-explicit']
    config = {'conversation_flow': True, 'repositories': {'repo': str(root)}, 'verifiers': {
        'tests': {'repository': 'repo', 'argv': [python, '-m', 'pytest', '-q']},
        'required': {'repository': 'repo', 'argv': explicit},
        'lint': {'repository': 'repo', 'argv': ['ruff', 'check', '.']}}}
    unit = {'test_paths': ['tests/test_selected.py'], 'verifiers': ['tests', 'required', 'lint']}
    monkeypatch.setattr('relay.work_forms.ecosystems', lambda path: [
        {'name': 'pytest', 'runner': python, 'narrow': 'pytest <file>'}])
    commands = work_units.checks(config, unit, 'repo', root)
    assert commands['required'] == explicit and commands['lint'] == ['ruff', 'check', '.']
    assert 'tests/test_selected.py' in commands['pytest-tests:tests/test_selected.py'] and 'tests' not in commands
    assert commands['pytest-tests:tests/test_selected.py'][-2] == '--basetemp'
    ordinary = work_units.checks({**config, 'conversation_flow': False}, unit, 'repo', root)
    assert ordinary['tests'] == [python, '-m', 'pytest', '-q']
    legacy = {**config, 'conversation_flow': False, 'verifiers': {
        'required': {'repository': 'repo', 'argv': work_units.narrowed('pytest', python, unit['test_paths'])}}}
    assert set(work_units.checks(legacy, {**unit, 'verifiers': ['required']}, 'repo', root)) == {
        'required', 'pytest-tests:tests/test_selected.py'}
    no_scope = work_units.checks(config, {'verifiers': ['tests']}, 'repo', root)
    assert 'tests/test_selected.py' not in no_scope['tests'] and '--basetemp' in no_scope['tests']


def test_cancelled_candidate_retains_patch_without_new_checks_audit_or_application(setup, tmp_path, monkeypatch):
    config, custody, root = setup
    config['conversation_flow'] = True
    repository(root)
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'storage'))
    monkeypatch.setattr(work_units, 'routes', lambda *a, **kw: iter([('worker', deployment())]))
    stopped = False
    checks = []
    def check(tree, commands):
        assert not stopped, 'A cancellation must not dispatch post-implementation checks.'
        checks.append(commands)
        return 'green', 'passed'
    monkeypatch.setattr(work_units, 'run_checks', check)
    def worker(child, *args, **kwargs):
        nonlocal stopped
        from pathlib import Path
        (Path(child['repositories']['repo'])/'a.py').write_text('value = 2\n')
        stopped = True
        return {'status': 'CANCELLED', 'model_calls': 1}
    monkeypatch.setattr(native_agent, 'run_session', worker)
    unit = {'title': 'Set two', 'target_paths': ['a.py'], 'criteria': ['value equals two'], 'verifiers': ['unit']}
    result = work_units.run_unit(custody, {'config': config, 'conversation_flow': True, 'deployment': deployment(),
        'limits': {'max_model_calls': 30}, 'deadline_utc': None}, unit, lambda: stopped,
        review=lambda *a: pytest.fail('No audit after cancellation'))
    assert len(checks) == 1
    assert result['verdict'] == 'INCONCLUSIVE' and result['reason'] == 'Execution was stopped; no candidate was applied.'
    assert result['checks_after'] == 'Not run: execution was stopped.'
    assert (root/'a.py').read_text() == 'value = 1\n'
    from pathlib import Path
    assert Path(result['patch']).is_file() and b'value = 2' in Path(result['patch']).read_bytes()
    candidate=result['candidate']
    assert candidate['patch']==result['patch']
    assert candidate['sha256']==hashlib.sha256(Path(result['patch']).read_bytes()).hexdigest()
    assert work_units.candidate_producer(custody,candidate['producer'])==deployment()
    assert candidate['targets']=={'repo':['a.py']}
    assert candidate['basis']=={'repo:a.py':hashlib.sha256(b'value = 1\n').hexdigest()}


@pytest.mark.parametrize('extra_blank', [False, True])
def test_new_file_whitespace_is_checked_before_any_paid_audit(setup, tmp_path, monkeypatch, extra_blank):
    from pathlib import Path
    config, custody, root = setup
    repository(root)
    config.update(conversation_flow=True, writable={'repo': ['docs/new.md']},
        verifiers={'conversation-diff': {'repository': 'repo',
            'argv': ['git', 'diff', '--check', 'HEAD', '--', 'docs/new.md']}})
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'storage'))
    monkeypatch.setattr(work_units, 'routes', lambda *a, **kw: iter([('worker', deployment())]))
    def worker(child, *args, **kwargs):
        target = Path(child['repositories']['repo'])/'docs/new.md'
        target.parent.mkdir(); target.write_bytes(b'New content.\n'+(b'\n' if extra_blank else b''))
        return {'status': 'PROPOSED', 'model_calls': 1}
    monkeypatch.setattr(native_agent, 'run_session', worker)
    audits = []
    def review(*args):
        audits.append(True)
        return {'verdict': 'ACCEPT', 'sources': [], 'reference_sources': []}
    result = work_units.run_unit(custody, {'config': config, 'conversation_flow': True,
        'deployment': deployment(), 'limits': {'max_model_calls': 30}, 'deadline_utc': None},
        {'title': 'New document', 'target_paths': ['docs/new.md'], 'criteria': ['Write the document'],
            'verifiers': ['conversation-diff']}, lambda: False, review=review)
    assert len(audits) == int(not extra_blank)
    assert result.get('applied', False) is (not extra_blank)
    if extra_blank:
        assert result['verdict'] == 'NOT_VERIFIED' and 'new blank line at EOF' in result['checks_after']
        assert not (root/'docs/new.md').exists()
    else:
        assert result['verdict'] == 'VERIFIED' and (root/'docs/new.md').read_bytes() == b'New content.\n'


@pytest.mark.parametrize('verdict,refusal', [('REVISE', True), ('INCONCLUSIVE', False)])
def test_only_an_audit_that_refuses_counts_toward_the_refusal_limit(setup, tmp_path, monkeypatch, verdict, refusal):
    # 2026-11-09: a partial review with no findings was the sixth "refusal" and stopped the task for good.
    from pathlib import Path
    config, custody, root = setup
    repository(root)
    config.update(conversation_flow=True, writable={'repo': ['docs/new.md']},
        verifiers={'conversation-diff': {'repository': 'repo',
            'argv': ['git', 'diff', '--check', 'HEAD', '--', 'docs/new.md']}})
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'storage'))
    monkeypatch.setattr(work_units, 'routes', lambda *a, **kw: iter([('worker', deployment())]))
    def worker(child, *args, **kwargs):
        target = Path(child['repositories']['repo'])/'docs/new.md'
        target.parent.mkdir(); target.write_bytes(b'New content.\n')
        return {'status': 'PROPOSED', 'model_calls': 1}
    monkeypatch.setattr(native_agent, 'run_session', worker)
    result = work_units.run_unit(custody, {'config': config, 'conversation_flow': True,
        'deployment': deployment(), 'limits': {'max_model_calls': 30}, 'deadline_utc': None},
        {'title': 'New document', 'target_paths': ['docs/new.md'], 'criteria': ['Write the document'],
         'verifiers': ['conversation-diff']},
        lambda: False, review=lambda *a: {'verdict': verdict, 'sources': [], 'reference_sources': []})
    assert result['verdict'] == 'NOT_REVIEWED' and not result.get('applied')
    assert work_units.audit_refusal(result) is refusal


@pytest.mark.parametrize('decision', [None, 'ACCEPT', 'APPLY_WITHOUT_ACCEPTED_AUDIT'])
def test_a_not_accepted_audit_applies_only_on_the_users_recorded_decision(setup, tmp_path, monkeypatch, decision):
    from pathlib import Path
    config, custody, root = setup
    repository(root)
    config.update(conversation_flow=True, writable={'repo': ['docs/new.md']},
        verifiers={'conversation-diff': {'repository': 'repo',
            'argv': ['git', 'diff', '--check', 'HEAD', '--', 'docs/new.md']}})
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'storage'))
    monkeypatch.setattr(work_units, 'routes', lambda *a, **kw: iter([('worker', deployment())]))
    def worker(child, *args, **kwargs):
        target = Path(child['repositories']['repo'])/'docs/new.md'
        target.parent.mkdir(); target.write_bytes(b'New content.\n')
        return {'status': 'PROPOSED', 'model_calls': 1}
    monkeypatch.setattr(native_agent, 'run_session', worker)
    audit = {'verdict': 'INCONCLUSIVE', 'summary': 'Static criteria hold.', 'findings': [], 'sources': [],
             'reference_sources': [], 'remaining': ['Running it was not evidenced.'],
             'audit_label': 'same-family audit, not independent',
             **({'user_decision': decision} if decision else {})}  # only confirm_apply's exact value counts
    result = work_units.run_unit(custody, {'config': config, 'conversation_flow': True,
        'deployment': deployment(), 'limits': {'max_model_calls': 30}, 'deadline_utc': None},
        {'title': 'New document', 'target_paths': ['docs/new.md'], 'criteria': ['Write the document'],
         'verifiers': ['conversation-diff']}, lambda: False, review=lambda *a: audit)
    if decision != 'APPLY_WITHOUT_ACCEPTED_AUDIT':
        assert result['verdict'] == 'NOT_REVIEWED' and not result.get('applied') and not (root/'docs/new.md').exists()
        return
    assert result['applied'] and (root/'docs/new.md').read_bytes() == b'New content.\n'
    assert result['audit']['verdict'] == 'INCONCLUSIVE'
    assert result['next'].startswith("Applied by the user's decision after its checks, although its audit (same-family "
                                     'audit, not independent) did not accept it') and 'Running it was not evidenced.' in result['next']
    assert 'never say it was audited or accepted' in result['next']


def test_a_verified_change_waits_for_the_sessions_checks_or_for_coverage(tmp_path, monkeypatch):
    tree = tmp_path/'tree'; (tree/'tests').mkdir(parents=True)
    (tree/'tests'/'test_mod.py').write_text('from pkg import mod\n')
    config = {'verifiers': {'all': {'repository': 'repo', 'argv': ['x']}}}
    unit = {'test_paths': ['tests/test_mod.py']}
    states = iter(['green', 'red', 'red', 'inconclusive', 'inconclusive'])
    monkeypatch.setattr(work_units, 'run_checks', lambda tree, commands: (next(states), 'output'))
    assert work_units.suite_allows(config, unit, 'repo', tree, {'pkg/mod.py': b''})[0]          # whole checks green
    assert not work_units.suite_allows(config, unit, 'repo', tree, {'pkg/mod.py': b''})[0]      # it breaks them: held
    # red before as well (a later unit makes it pass): it settles nothing, the unit's tests cover the change
    assert work_units.suite_allows(config, unit, 'repo', tree, {'pkg/mod.py': b''}, {'all': 'red'})[0]
    assert work_units.suite_allows(config, unit, 'repo', tree, {'pkg/mod.py': b''})[0]          # too long: covered
    allowed, why = work_units.suite_allows(config, unit, 'repo', tree, {'pkg/other.py': b''})   # too long, not covered
    assert not allowed and 'pkg/other.py' in why
    states = iter(['inconclusive'])
    assert work_units.suite_allows(config, unit, 'repo', tree, {'pkg/mod.py': b'', 'docs/NOTES.md': b''})[0]  # prose needs no test


def test_a_child_gets_what_the_session_has_left_and_its_calls_count_against_it(tmp_path, monkeypatch):
    custody = tmp_path/'session'; (custody/'units'/'u1').mkdir(parents=True)
    monkeypatch.setattr('relay.delegation_tools.count_calls', lambda root: 10)
    (custody/'units'/'u1'/'receipt.json').write_text(json.dumps({'rungs': [{'model_calls': 7}, {'model_calls': 5}]}))
    assert work_units.unit_calls(custody) == 12
    assert work_units.child_limits(custody, {'limits': {'max_model_calls': 40}, 'deadline_utc': None}) == \
        {'max_model_calls': 14, 'max_seconds': 900, 'request_seconds': 900}  # 40 - 10 used - 12 by children - 4 for the session to finish
    with pytest.raises(ValueError, match='R5'):
        work_units.child_limits(custody, {'limits': {'max_model_calls': 25}, 'deadline_utc': None})
    with pytest.raises(ValueError, match='7 calls'):  # one given 7 ran out without changing anything: edit directly
        work_units.child_limits(custody, {'limits': {'max_model_calls': 33}, 'deadline_utc': None})


def test_native_command_review_gets_time_without_extra_calls_or_bypassing_parent_deadline(tmp_path,monkeypatch):
    import time
    state={'config':{'command_protection':{'review':'auto'}},'deadline_utc':None}
    native={'native_profile':{'endpoint':'cli://codex','protocol':'vendor-cli'}}
    assert work_units.child_limits(tmp_path,state,deployment=native)=={'max_model_calls':30,'max_seconds':1800,'request_seconds':1800}
    assert work_units.child_limits(tmp_path,state,deployment=deployment())=={'max_model_calls':30,'max_seconds':900,'request_seconds':900}
    monkeypatch.setattr(time,'time',lambda:1000)
    state['deadline_utc']=1500
    assert work_units.child_limits(tmp_path,state,deployment=native)=={'max_model_calls':30,'max_seconds':440,'request_seconds':440}


def test_a_unit_without_checks_tare_can_run_is_refused_before_any_model_call(setup, tmp_path, monkeypatch):
    config, custody, root = setup
    repository(root)
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'storage'))
    monkeypatch.setattr(turn_engine, 'policy', lambda: {'implementer': [['child-route']]})
    monkeypatch.setattr(turn_engine, 'available', lambda name: (deployment(), None))
    called = []
    monkeypatch.setattr(native_agent, 'run_session', lambda *a, **kw: called.append(1))
    state = {'config': config, 'deployment': deployment()}
    custody.mkdir(parents=True, exist_ok=True)
    result = work_units.run_unit(custody, state, {'title': 'x', 'target_paths': ['a.py'], 'criteria': ['y']},
                                 lambda: False)
    assert result['verdict'] == 'INCONCLUSIVE' and 'R0' in result['reason'] and not called
    # A documentation unit: no check of the code can tell it is done (the phase 3 comparison spent 23 calls on one).
    (root/'NOTES.md').write_text('notes\n')
    with pytest.raises(ValueError, match='documentation'):
        work_units.run_unit(custody, state, {'title': 'x', 'target_paths': ['NOTES.md'], 'criteria': ['y'],
                                             'verifiers': ['unit']}, lambda: False)
    assert not called


def test_units_handed_at_once_run_together_and_apply_one_after_another(tmp_path, monkeypatch):
    """D8: two children run at the same time; the later change is checked on the tree the earlier one left and,
    when it no longer fits, run again on that tree."""
    import sys
    import threading
    from pathlib import Path
    from relay import work_forms
    root, custody = tmp_path/'repo', tmp_path/'session'
    root.mkdir(); custody.mkdir()
    (root/'a.py').write_bytes(b'value = 1\n'); (root/'b.py').write_bytes(b'other = 0\n')
    repository(root)
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'storage'))
    config = {'repositories': {'repo': str(root)}, 'writable': {'repo': ['.']}, 'verifiers': {}}
    plan = {'units': [{'key': 'a', 'title': 'A', 'target_paths': ['a.py'], 'criteria': ['a.py holds 2']},
                      {'key': 'b', 'title': 'B', 'target_paths': ['b.py'], 'criteria': ['b.py follows a.py']},
                      {'key': 'c', 'title': 'C', 'target_paths': ['c.py'], 'criteria': ['-'], 'depends_on': ['a']}],
            'criteria': ['done']}
    assert work_forms.record_plan(custody, config, plan)['status'] == 'PLANNED'
    check = {'a': "assert open('a.py').read() == 'value = 2\\n'",
             'b': "v = open('a.py').read().split()[-1]; assert open('b.py').read() == 'other = %s\\n' % v"}
    monkeypatch.setattr(work_units, 'checks', lambda config, unit, *a: {unit['key']: [sys.executable, '-B', '-c', check[unit['key']]]})
    monkeypatch.setattr(work_units, 'suite_allows', lambda *a: (True, 'fixture'))
    monkeypatch.setattr(work_units, 'routes', lambda *a: iter([('child-route', deployment())]))
    together, first = threading.Barrier(2, timeout=20), set()
    applied_a=threading.Event()
    real_apply=work_units.apply
    def observe_apply(root,tree,base,changed,writable):
        result=real_apply(root,tree,base,changed,writable)
        if result[0]=='APPLIED' and 'a.py' in changed:applied_a.set()
        return result
    monkeypatch.setattr(work_units,'apply',observe_apply)

    def child(config, deployment, custody, instruction, **kw):
        tree = Path(config['repositories']['repo'])
        key = config['writable']['repo'][0][0]
        if key not in first:
            first.add(key)
            together.wait()  # both children are running
            if key=='b':
                assert applied_a.wait(20),'A never applied; do not deliver B against an assumed ordering.'
        if key == 'a':
            (tree/'a.py').write_bytes(b'value = 2\n')
        else:
            (tree/'b.py').write_text('other = %s\n' % (tree/'a.py').read_text().split()[-1], newline='\n')
        return {'status': 'PROPOSED', 'model_calls': 2}
    monkeypatch.setattr(native_agent, 'run_session', child)
    state = {'config': config, 'implement_parallel': True}
    with pytest.raises(ValueError, match='depend'):
        work_units.run_units(custody, state, {'units': [{'unit': 'c'}]}, lambda: False)
    report = work_units.run_units(custody, state, {'units': [{'unit': 'a'}, {'unit': 'b'}]}, lambda: False)
    assert report['children_at_once'] == 2
    assert report['units']['a'].get('applied') and report['units']['b'].get('applied') and report['units']['b'].get('run_again'),report
    assert (root/'a.py').read_bytes() == b'value = 2\n' and (root/'b.py').read_bytes() == b'other = 2\n'
    ledger = json.loads((custody/'work.json').read_text(encoding='utf-8'))
    assert {k: v['state'] for k, v in ledger['units'].items()} == {'a': 'APPLIED', 'b': 'APPLIED', 'c': 'PENDING'}
    assert len(ledger['units']['b']['history']) == 2  # the conflict, then the run on the updated tree
    assert not any((tmp_path/'storage'/'wt').iterdir())


def test_a_kept_patch_is_found_for_the_answer_to_point_at(tmp_path):
    # Ways of working: tare serve's answer names the patch of a unit that was not applied.
    import time
    custody = tmp_path/'session'
    for name, result in (('a-1', {'verdict': 'NOT_VERIFIED', 'patch': 'p/a.diff'}),
                         ('b-1', {'verdict': 'VERIFIED', 'applied': True, 'patch': None}),
                         ('c-1', {'verdict': 'VERIFIED', 'application': 'CONFLICT', 'patch': 'p/c.diff'})):
        (custody/'units'/name).mkdir(parents=True)
        (custody/'units'/name/'receipt.json').write_text(json.dumps({'unit': {'key': name[0]}, 'result': result}))
    assert work_units.kept_patches(custody, 0) == [('a', 'NOT_VERIFIED', 'p/a.diff'), ('c', 'CONFLICT', 'p/c.diff')]
    assert work_units.kept_patches(custody, time.time() + 60) == []  # only what this turn ran
def test_a_unit_whose_tests_need_another_units_code_is_run_again_after_it(tmp_path, monkeypatch):
    """The phase 3 comparison: run alone, a unit whose tests call another unit's new code fails; once the other is
    applied it is run again on that tree, before the session pays a call for it."""
    import sys
    import threading
    from pathlib import Path
    from relay import work_forms
    root, custody = tmp_path/'repo', tmp_path/'session'
    root.mkdir(); custody.mkdir()
    (root/'a.py').write_bytes(b'value = 1\n'); (root/'b.py').write_bytes(b'other = 0\n')
    repository(root)
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'storage'))
    config = {'repositories': {'repo': str(root)}, 'writable': {'repo': ['.']}, 'verifiers': {}}
    plan = {'units': [{'key': 'a', 'title': 'A', 'target_paths': ['a.py'], 'criteria': ['a.py holds 2']},
                      {'key': 'b', 'title': 'B', 'target_paths': ['b.py'], 'criteria': ['b.py uses a.py']}],
            'criteria': ['done']}
    assert work_forms.record_plan(custody, config, plan)['status'] == 'PLANNED'
    check = {'a': "assert open('a.py').read() == 'value = 2\\n'",
             'b': "assert open('a.py').read() == 'value = 2\\n' and open('b.py').read() == 'other = 2\\n'"}
    monkeypatch.setattr(work_units, 'checks', lambda config, unit, *a: {unit['key']: [sys.executable, '-B', '-c', check[unit['key']]]})
    monkeypatch.setattr(work_units, 'suite_allows', lambda *a: (True, 'fixture'))
    monkeypatch.setattr(work_units, 'routes', lambda *a: iter([('child-route', deployment())]))
    together, first = threading.Barrier(2, timeout=20), set()

    def child(config, deployment, custody, instruction, **kw):
        tree = Path(config['repositories']['repo'])
        key = config['writable']['repo'][0][0]
        if key not in first:
            first.add(key)
            together.wait()  # both copies were taken before either change is applied
        (tree/f'{key}.py').write_bytes(b'value = 2\n' if key == 'a' else b'other = 2\n')
        return {'status': 'PROPOSED', 'model_calls': 2}
    monkeypatch.setattr(native_agent, 'run_session', child)
    report = work_units.run_units(custody, {'config': config}, {'units': [{'unit': 'a'}, {'unit': 'b'}],
                                                                'parallel': True}, lambda: False)
    assert report['units']['b']['applied'] and 'alone' in report['units']['b']['run_again']
    assert (root/'b.py').read_bytes() == b'other = 2\n'


def test_a_local_session_runs_its_units_in_series_even_when_asked_for_parallel(tmp_path, monkeypatch):
    # Its children share the session's GPU, which serves one request at a time: parallel would only queue them.
    from relay import work_forms
    root, custody = tmp_path/'repo', tmp_path/'session'
    root.mkdir(); custody.mkdir()
    (root/'a.py').write_bytes(b'1\n'); (root/'b.py').write_bytes(b'1\n')
    repository(root)
    config = {'repositories': {'repo': str(root)}, 'writable': {'repo': ['.']}, 'verifiers': {}}
    plan = {'units': [{'key': 'a', 'title': 'A', 'target_paths': ['a.py'], 'criteria': ['x']},
                      {'key': 'b', 'title': 'B', 'target_paths': ['b.py'], 'criteria': ['x']}], 'criteria': ['done']}
    assert work_forms.record_plan(custody, config, plan)['status'] == 'PLANNED'
    seen = []
    monkeypatch.setattr(work_units, 'run_unit', lambda custody, state, args, stop, batch=None: seen.append(batch.share) or
                        {'verdict': 'INCONCLUSIVE', 'reason': 'fixture'})
    local = deployment(endpoint='http://192.168.0.10:8080/v1')
    report = work_units.run_units(custody, {'config': config, 'deployment': local},
                                  {'units': [{'unit': 'a'}, {'unit': 'b'}], 'parallel': True}, lambda: False)
    assert report['children_at_once'] == 1 and seen == [1, 1] and 'one request at a time' in report['parallel']
    hosted = deployment(endpoint='https://api.example.test/v1')
    report = work_units.run_units(custody, {'config': config, 'deployment': hosted},
                                  {'units': [{'unit': 'a'}, {'unit': 'b'}], 'parallel': True}, lambda: False)
    assert report['children_at_once'] == 2 and 'parallel' not in report


def test_a_child_stays_on_the_sessions_bill(monkeypatch, tmp_path):
    # The TUI walkthrough of 2026-10-07: a Codex quota session's children climbed the ladder into Bedrock dollars
    # and the Claude CLI's quota. A child takes a route of the session's class and vendor; another only by name with
    # the child_routes_any experiment on.
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'storage'))
    fleet = {'codex-native-gpt-6-1-sol': {'id': 'codex-native-gpt-6-1-sol', 'native_profile': {'protocol': 'openai-codex-responses', 'endpoint': 'https://api.openai.com/v1'}},
             'codex-cli-gpt-6-luna': {'id': 'codex-cli-gpt-6-luna', 'native_profile': {'protocol': 'vendor-cli', 'endpoint': 'cli://codex'}},
             'claude-opus-5-5-cli': {'id': 'claude-opus-5-5-cli', 'native_profile': {'protocol': 'vendor-cli', 'endpoint': 'cli://claude'}},
             'bedrock-sonnet': {'id': 'bedrock-sonnet', 'native_profile': {'protocol': 'bedrock-converse', 'endpoint': 'https://bedrock.amazonaws.com'}},
             'qwen38-gsq': {'id': 'qwen38-gsq', 'native_profile': {'protocol': 'openai-chat', 'endpoint': 'http://192.168.0.10:8080/v1'}}}
    monkeypatch.setattr(turn_engine, 'policy', lambda: {'implementer': [list(fleet)]})
    monkeypatch.setattr(turn_engine, 'available', lambda name, probe=True: (fleet.get(name), None))
    codex = fleet['codex-native-gpt-6-1-sol']
    assert [n for n, _ in work_units.routes(codex)] == ['codex-native-gpt-6-1-sol', 'codex-cli-gpt-6-luna']
    assert [n for n, _ in work_units.routes(fleet['bedrock-sonnet'])] == ['bedrock-sonnet']
    assert [n for n, _ in work_units.routes(fleet['qwen38-gsq'])] == ['qwen38-gsq']
    with pytest.raises(ValueError, match='bill'):
        list(work_units.routes(codex, chosen='bedrock-sonnet'))
    delegation.save_experiments('quota', child_routes_any=True)
    assert [n for n, _ in work_units.routes(codex)] == [n for n in fleet if n != 'qwen38-gsq']  # hosted, never local
    assert [n for n, _ in work_units.routes(codex, chosen='claude-opus-5-5-cli')] == ['claude-opus-5-5-cli']


def test_a_busy_local_gpu_sends_a_local_sessions_unit_to_the_next_route_at_once(monkeypatch, tmp_path):
    # A unit is a session's delegated implementer: a conversation waits on it, so nothing waits for a busy GPU
    # (owner, 2026-10-08). A local session's unit then takes the next routes of the policy, as the session would;
    # with the GPU free it stays on its local bill, and on the coordinator's chosen route.
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'storage'))
    fleet = {'qwen38-gsq': {'id': 'qwen38-gsq', 'native_profile': {'protocol': 'openai-chat', 'endpoint': 'http://192.168.0.10:8080/v1'}},
             'codex-native-gpt-6-1-sol': {'id': 'codex-native-gpt-6-1-sol', 'native_profile': {'protocol': 'openai-codex-responses', 'endpoint': 'https://api.openai.com/v1'}}}
    busy = [True]
    def available(name, probe=True):
        return (None, turn_engine.GPU_BUSY) if name == 'qwen38-gsq' and busy[0] else (fleet[name], None)
    monkeypatch.setattr(turn_engine, 'policy', lambda: {'implementer': [['qwen38-gsq'], ['codex-native-gpt-6-1-sol']]})
    monkeypatch.setattr(turn_engine, 'available', available)
    local = fleet['qwen38-gsq']
    assert [n for n, _ in work_units.routes(local)] == ['codex-native-gpt-6-1-sol']
    # The coordinator's chosen route comes first; busy, the policy's routes follow (the owner's routing.json, else tare's).
    assert [n for n, _ in work_units.routes(local, chosen='qwen38-gsq')] == ['codex-native-gpt-6-1-sol']
    busy[0] = False
    assert [n for n, _ in work_units.routes(local)] == ['qwen38-gsq']
    assert [n for n, _ in work_units.routes(local, chosen='qwen38-gsq')] == ['qwen38-gsq']
