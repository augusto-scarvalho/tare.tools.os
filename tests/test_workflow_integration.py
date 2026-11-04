"""The small compiler analyzes data; execution remains with the existing Work owner."""
import json
from pathlib import Path

import pytest

from graph_backlog.jsonutil import sha256_canonical
from relay import relay_mesh, work_run, workflow_compiler
from test_work_run import os_root, evidence
from test_work_console import graph_root
from test_work_resume import host

pytestmark = pytest.mark.verifies('SPEC-OS-WORKFLOW-IR-COMPILER-001', 'AC-01')


def test_public_analysis_needs_no_graph_or_storage(tmp_path, monkeypatch, capsys):
    from relay.native_installation import main
    root = tmp_path/'absent-control'
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'absent-storage'))
    original = relay_mesh.RELAY_ROOT
    try:
        assert main(['--os-root', str(root), 'workflow', 'analyze', '--format', 'json']) == 0
        result = json.loads(capsys.readouterr().out)
        assert result['operator_ref'] == 'work.execute/v1'
        assert [n['operator_ref'] for n in result['nodes']] == [
            'ground/v1', 'implement/v1', 'validate/v1', 'audit/v1', 'settle/v1']
        assert not root.exists() and not (tmp_path/'absent-storage').exists()
    finally:
        relay_mesh.rebind_root(original)


def test_public_invalid_analysis_has_named_error_and_no_writes(tmp_path, capsys):
    from tare_ops import main
    plan = workflow_compiler.recipe()
    plan['nodes'][-1]['obligations'] = []
    path = tmp_path/'invalid.json'
    path.write_text(json.dumps(plan), encoding='utf-8')
    before = path.read_bytes()
    assert main(['workflow', 'analyze', '--plan', str(path)]) == 2
    text = capsys.readouterr().out
    assert 'WORKFLOW_' in text
    assert list(tmp_path.iterdir()) == [path] and path.read_bytes() == before


def test_explain_projects_recipe_without_starting_work(graph_root):
    from relay.work_console import inspect_work, render
    path = graph_root/'work-graph.json'
    before = path.read_bytes()
    result = inspect_work(work_id='ready', graph_path=str(path))
    assert result['authority'] == 'NONE'
    assert result['workflow']['operator_ref'] == 'work.execute/v1'
    assert 'Analysis only' in render('explain', result)
    assert path.read_bytes() == before and set(graph_root.iterdir()) == {path}


def test_bad_recipe_is_rejected_before_work_custody_or_agent(os_root, monkeypatch):
    root, repo = os_root
    before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    bad = workflow_compiler.recipe()
    bad['nodes'][1]['operator_ref'] = 'execute-prose/v1'
    monkeypatch.setattr(workflow_compiler, 'recipe', lambda: bad)
    with pytest.raises(ValueError, match='WORKFLOW_'):
        work_run.run_work('work-one', repository_root=repo,
            plan_auditor=lambda *a: pytest.fail('invalid plan reached auditor'),
            implementer=lambda *a: pytest.fail('invalid plan reached implementer'))
    after = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    assert after == before


def test_compiled_work_reuses_real_verifiers_admission_and_landing(os_root):
    root, repo = os_root
    calls = []
    def implement(worktree, descriptor):
        calls.append('implement')
        (worktree/'src/value.txt').write_text('strict\n', encoding='utf-8')
        return {'status': 'SUCCEEDED'}
    result = work_run.run_work('work-one', repository_root=repo,
        plan_auditor=lambda *a: {'verdict': 'ACCEPT', 'findings': []},
        implementer=implement, evidence_factory=evidence)
    assert result['status'] == 'SETTLED', result
    assert calls == ['implement'] and (repo/'src/value.txt').read_text() == 'strict\n'
    assert [s['stage'] for s in result['stages']] == [
        'ground', 'descriptor', 'plan_audit', 'workspace', 'implement', 'candidate', 'admission', 'landing']
    assert result['workflow_sha256'] == sha256_canonical(result['workflow'])
    assert result['workflow']['operator_ref'] == 'work.execute/v1'
    train = work_run.train_dir_for('work-one')
    descriptor = json.loads((train/'EXECUTION_DESCRIPTOR.json').read_bytes())
    assert 'workflow' not in descriptor and 'recipe' not in descriptor
    stored = json.loads((work_run.receipt_path(result['run_id'])).read_bytes())
    assert stored['workflow'] == result['workflow']


def test_legal_recipe_does_not_bypass_real_failed_verifier(os_root):
    root, repo = os_root
    def implement(worktree, descriptor):
        (worktree/'src/value.txt').write_text('wrong\n', encoding='utf-8')
        return {'status': 'SUCCEEDED'}
    result = work_run.run_work('work-one', repository_root=repo,
        plan_auditor=lambda *a: {'verdict': 'ACCEPT', 'findings': []},
        implementer=implement, evidence_factory=lambda *a: pytest.fail('bad candidate reached admission'))
    assert result['workflow']['operator_ref'] == 'work.execute/v1'
    assert result['status'] == 'FAILED' and result['landing'] is None
    assert (repo/'src/value.txt').read_text() == 'base\n'


@pytest.mark.parametrize('case', ['saved', 'legacy', 'changed'])
def test_resume_preserves_recipe_without_recompile_or_model_reexecution(host, monkeypatch, case):
    root, repo, profile, client, plans = host
    from relay.native_catalog import select
    deployment = select(None, profile)
    with monkeypatch.context() as cut:
        def stop(*args):
            raise SystemExit('after candidate; before admission evidence')
        cut.setattr(relay_mesh, 'materialize_admission_evidence', stop)
        with pytest.raises(SystemExit):
            result = work_run.run_work('work-one', repository_root=repo, native_deployment=deployment, unlimited=True)
            pytest.fail('did not reach injected cut: '+str(result))
    train = work_run.train_dir_for('work-one')
    origin_path = train/'WORK_RUN.json'
    origin = json.loads(origin_path.read_bytes())
    saved_path = work_run.receipt_path(origin['run_id'])
    saved = json.loads(saved_path.read_bytes())
    if case == 'legacy':
        origin.pop('workflow_sha256')
        origin_path.write_text(json.dumps(origin), encoding='utf-8')
        saved.pop('workflow'); saved.pop('workflow_sha256')
        saved_path.write_text(json.dumps(saved), encoding='utf-8')
    elif case == 'changed':
        saved['workflow']['obligations'] = []
        saved_path.write_text(json.dumps(saved), encoding='utf-8')
    calls = len(client.generations)
    monkeypatch.setattr(workflow_compiler, 'compile_recipe', lambda *a, **kw: pytest.fail('resume recompiled'))
    result = work_run.run_work('work-one', repository_root=repo, resume=True)
    assert len(client.generations) == calls
    if case == 'changed':
        assert result['status'] == 'FAILED' and 'WORK_RESUME_WORKFLOW_CHANGED' in result['error']
        assert result['landing'] is None and (repo/'src/value.txt').read_text() == 'base\n'
    else:
        assert result['status'] == 'SETTLED', result
        assert (repo/'src/value.txt').read_text() == 'strict\n'
        if case == 'saved':
            assert result['workflow'] == saved['workflow']
            assert result['workflow_sha256'] == saved['workflow_sha256']
        else:
            assert 'workflow' not in result


@pytest.mark.parametrize('size', [(80, 24), (150, 52)])
def test_enter_on_manual_work_id_inspects_literal_recipe_without_dispatch(tmp_path, size):
    import asyncio
    from textual.widgets import Input
    from textual.selection import Selection as TextSelection
    from relay.native_console import WorkConsole
    from relay.console_output import SelectableMessage
    from test_native_console import FakeBackend
    compiled = workflow_compiler.compile_recipe(workflow_compiler.recipe(),
        capabilities=workflow_compiler.owner_capabilities(), allowed_effects=workflow_compiler.owner_effects())
    expected = 'Graph: C:\\my_project\\work_graph.json\n'+workflow_compiler.render_compiled(compiled)
    class Backend(FakeBackend):
        def __init__(self):
            super().__init__()
            self.inspected = []
        def inspect(self, selected):
            self.inspected.append(selected.work)
            return expected
    async def scenario():
        backend = Backend()
        app = WorkConsole(tmp_path, backend=backend, initial_mode='work')
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            field = app.query_one('#work-id', Input)
            field.value = 'unlisted-work'
            field.focus()
            await pilot.press('enter')
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert backend.inspected == ['unlisted-work'] and not backend.launches
            reports = list(app.query('.owner-report'))
            assert reports and isinstance(reports[-1], SelectableMessage)
            assert reports[-1].get_selection(TextSelection(None, None))[0] == expected
    asyncio.run(scenario())


@pytest.mark.parametrize('content,code', [(b'{' , 'WORKFLOW_JSON_INVALID'),
    (b'x'*65537, 'WORKFLOW_PLAN_TOO_LARGE'), (b'{}', 'WORKFLOW_PLAN_FIELDS_INVALID')],
    ids=['invalid-json', 'oversize', 'missing-fields'])
def test_external_recipe_refusals_are_bounded_and_named(tmp_path, capsys, content, code):
    path = tmp_path/'recipe.json'
    path.write_bytes(content)
    assert workflow_compiler.main(['analyze', '--plan', str(path)]) == 2
    assert json.loads(capsys.readouterr().out)['error'] == code
    assert path.read_bytes() == content and list(tmp_path.iterdir()) == [path]
