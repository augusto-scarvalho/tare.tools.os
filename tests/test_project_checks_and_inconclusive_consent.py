"""Tests for .tare/checks.json, first-class Godot test discovery, per-check timeouts,
and TUI human confirmation for independent INCONCLUSIVE audits with zero findings."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from relay import (
    conversation_permissions as permissions,
    conversation_review,
    conversation_tasks,
    execution_scope as scope,
    native_agent,
    relay_mesh,
    turn_engine,
    work_forms,
    work_units,
)


def test_project_checks_parses_headless_manual_timeouts_and_rejects_unsafe_entries(tmp_path, monkeypatch):
    root = tmp_path / 'proj'
    root.mkdir()
    (root / 'scripts').mkdir()
    local_runner = root / 'scripts' / 'check.py'
    local_runner.write_text('print("ok")\n', encoding='utf-8')
    outside = tmp_path / 'outside.py'
    outside.write_text('print("bad")\n', encoding='utf-8')
    monkeypatch.setattr(turn_engine, 'project_python', lambda p: sys.executable)
    monkeypatch.setattr(turn_engine.shutil, 'which', lambda name: '/usr/bin/godot_console' if name == 'godot_console' else None)

    tare_dir = root / '.tare'
    tare_dir.mkdir()
    (tare_dir / 'checks.json').write_text(json.dumps({
        'checks': {
            'unit-py': {'argv': ['python', '-m', 'pytest', '-q'], 'timeout_seconds': 45},
            'smoke': {'argv': ['godot', '--headless', '--fixed-fps', '60', '--path', '.', '-s', 'tests/smoke.gd'],
                      'timeout_seconds': 90},
            'local-script': {'argv': ['scripts/check.py', '--fast']},
            'visual-review': {'mode': 'gui', 'description': 'Requires GPU renderer.', 'paths': ['tests/visual_review.gd']},
            'manual-qa': {'mode': 'manual', 'description': 'Inspect screenshots.'},
            # Rejected entries:
            'conversation-diff': {'argv': ['python', '-c', 'pass']},
            'bad name!': {'argv': ['python', '-c', 'pass']},
            'abs-exe': {'argv': [str(outside)]},
            'traversal': {'argv': ['../outside.py']},
            'bad-timeout-bool': {'argv': ['python', '-c', 'pass'], 'timeout_seconds': True},
            'bad-timeout-small': {'argv': ['python', '-c', 'pass'], 'timeout_seconds': 2},
            'bad-mode-list': {'mode': []},
            'unknown-key': {'argv': ['python', '-c', 'pass'], 'shell': True},
            'missing-bin': {'argv': ['definitely-not-installed-bin', '--version']},
        }
    }), encoding='utf-8')

    declared = turn_engine.project_checks(root)
    assert set(declared['verifiers']) == {'unit-py', 'smoke', 'local-script'}
    assert declared['verifiers']['unit-py'] == {'repository': 'workspace', 'argv': [sys.executable, '-m', 'pytest', '-q']}
    assert declared['verifiers']['smoke']['argv'][0] == '/usr/bin/godot_console'
    assert declared['verifiers']['local-script']['argv'] == [str(local_runner.resolve()), '--fast']
    assert declared['timeouts'] == {'unit-py': 45, 'smoke': 90}
    assert declared['manual'] == {
        'visual-review': {'mode': 'gui', 'description': 'Requires GPU renderer.', 'paths': ['tests/visual_review.gd']},
        'manual-qa': {'mode': 'manual', 'description': 'Inspect screenshots.'},
    }

    # In a Godot project, config_for auto-prepends godot-import before declared Godot script checks.
    (root / 'project.godot').write_text('config_version=5\n', encoding='utf-8')
    cfg = turn_engine.config_for(root, mode='read-only')
    assert list(cfg['verifiers']) == ['godot-import', 'unit-py', 'smoke', 'local-script']
    # Every verifier strictly preserves native_agent.configuration()'s {'repository', 'argv'} shape.
    assert all(set(v) == {'repository', 'argv'} for v in cfg['verifiers'].values())


def test_project_checks_fails_closed_on_malformed_or_oversized_file(tmp_path):
    root = tmp_path / 'proj'
    (root / '.tare').mkdir(parents=True)
    (root / '.tare' / 'checks.json').write_text('{broken json', encoding='utf-8')
    assert turn_engine.project_checks(root) == {'verifiers': {}, 'timeouts': {}, 'manual': {}}
    (root / '.tare' / 'checks.json').write_text(' ' * (turn_engine.PROJECT_CHECKS_MAX_BYTES + 10), encoding='utf-8')
    assert turn_engine.project_checks(root) == {'verifiers': {}, 'timeouts': {}, 'manual': {}}


def test_config_for_falls_back_to_autodetect_when_checks_json_only_declares_manual_checks(tmp_path, monkeypatch):
    root = tmp_path / 'godot-proj'
    (root / '.tare').mkdir(parents=True)
    (root / 'tests').mkdir()
    (root / 'project.godot').write_text('config_version=5\n', encoding='utf-8')
    (root / 'tests' / 'smoke.gd').write_text('extends SceneTree\n', encoding='utf-8')
    (root / '.tare' / 'checks.json').write_text(json.dumps({
        'checks': {'visual-review': {'mode': 'gui', 'paths': ['tests/visual_review.gd']}}
    }), encoding='utf-8')
    monkeypatch.setattr(turn_engine.shutil, 'which', lambda name: '/usr/bin/godot_console' if name == 'godot_console' else None)
    cfg = turn_engine.config_for(root, mode='read-only')
    assert list(cfg['verifiers']) == ['godot-import', 'tests']


def test_godot_ecosystem_compiles_per_script_checks_skips_gui_manual_paths_and_prepends_import(tmp_path, monkeypatch):
    root = tmp_path / 'game'
    tree = tmp_path / 'tree'
    for d in (root, tree):
        (d / 'tests').mkdir(parents=True)
        (d / 'project.godot').write_text('config_version=5\n', encoding='utf-8')
        for script in ('smoke.gd', 'experience.gd', 'visual_review.gd'):
            (d / 'tests' / script).write_text('extends SceneTree\n', encoding='utf-8')
    (root / '.tare').mkdir()
    (root / '.tare' / 'checks.json').write_text(json.dumps({
        'checks': {'visual-review': {'mode': 'gui', 'paths': ['tests/visual_review.gd']}}
    }), encoding='utf-8')
    monkeypatch.setattr(work_forms.shutil, 'which', lambda name: '/usr/bin/godot_console' if name == 'godot_console' else None)

    ecos = work_forms.ecosystems(tree)
    assert any(e['name'] == 'godot' and e['runner'] == '/usr/bin/godot_console' for e in ecos)

    config = {
        'conversation_flow': True,
        'repositories': {'game': str(root)},
        'verifiers': {
            'godot-import': {'repository': 'game', 'argv': ['/usr/bin/godot_console', '--headless', '--path', '.', '--import', '--quiet']},
            'tests': {'repository': 'game', 'argv': ['/usr/bin/godot_console', '--headless', '--fixed-fps', '60', '--path', '.', '-s', 'tests/smoke.gd']},
        },
    }
    unit = {
        'verifiers': ['tests'],
        'test_paths': ['tests/smoke.gd', 'tests/experience.gd', 'tests/visual_review.gd'],
    }
    planned = {}
    compiled = work_units.checks(config, unit, 'game', tree, planned)
    # godot-import prepended; tests/smoke.gd not duplicated; tests/experience.gd added; visual_review.gd skipped (gui).
    assert list(compiled) == ['godot-import', 'tests', 'godot-tests:tests/experience.gd']
    assert compiled['godot-tests:tests/experience.gd'] == [
        '/usr/bin/godot_console', '--headless', '--fixed-fps', '60', '--path', '.', '-s', 'tests/experience.gd']
    assert planned == {'godot-tests:tests/experience.gd': ['tests/experience.gd']}


def test_run_checks_applies_per_check_timeout_even_when_supervision_timeout_is_none(tmp_path, monkeypatch):
    seen = []

    def fake_port(argv, *, cwd, timeout, **kwargs):
        seen.append((argv, timeout))
        return SimpleNamespace(returncode=0, stdout='ok\n', stderr='')

    monkeypatch.setattr(work_units, 'supervision', lambda: (None, None, None))
    token = work_units.CHECK_PORT.set(fake_port)
    try:
        state, _ = work_units.run_checks(tmp_path, {'fast': ['cmd', '1'], 'default': ['cmd', '2']},
                                         timeouts={'fast': 45})
    finally:
        work_units.CHECK_PORT.reset(token)
    assert state == 'green'
    assert seen == [(['cmd', '1'], 45), (['cmd', '2'], None)]


def test_planner_mission_surfaces_manual_checks_from_tare_checks_json(tmp_path, monkeypatch):
    from test_plan_critic import PLAN, Stub, execution, request, wrap
    from test_native_agent import deployment
    from compute_plane.native_journal import NativeJournal

    parent = tmp_path / 'parent'
    parent.mkdir()
    journal = NativeJournal(parent / 'journal.jsonl')
    journal.append({'kind': 'session_started', 'session_id': 'parent',
                    'instruction': 'TUI.\n\nOperator message:\nPolish HOME animations.'})
    journal.append({'kind': 'model_requested', 'call_id': 'parent/model/1'})
    source = tmp_path / 'project'
    (source / '.tare').mkdir(parents=True)
    (source / '.tare' / 'checks.json').write_text(json.dumps({
        'checks': {'visual-review': {'mode': 'gui', 'description': 'Needs GPU renderer for PNGs.',
                                     'paths': ['tests/visual_review.gd']}}
    }), encoding='utf-8')
    candidate = dict(deployment(), id='fixture-planner')
    params = json.loads(candidate['native_profile']['parameters_json'])
    params['chat_template_kwargs']['reasoning_effort'] = 'high'
    candidate['native_profile']['parameters_json'] = json.dumps(params, sort_keys=True, separators=(',', ':'))
    state = {'deployment': deployment(), 'planning': {'routes': {'worker': candidate}},
             'limits': {'max_model_calls': 15}, 'deadline_utc': None, 'conversation_flow': True,
             'config': {'repositories': {'repo': str(source)}, 'writable': {'repo': ['a.py']},
                        'verifiers': {}, 'capabilities': ['read', 'edit']}}
    stub = Stub(monkeypatch, candidate, PLAN)
    stub.verdict = {'verdict': 'ACCEPT', 'summary': 'Sound.', 'findings': [], 'remaining': [], 'sources': []}
    control = execution()
    tool = wrap(parent, state, control)
    conversation_tasks.ConversationTools(tool, parent, state, 'turn').understand({
        'intent': 'deliver', 'relation': 'new', 'objective': 'Polish HOME.', 'criteria': [], 'kinds': ['coding']})
    value = json.loads(tool.execute(request(), control).output_json)
    assert value['status'] == 'PLANNED'
    planner_brief = stub.sessions[0][3]
    assert 'manual_checks' in planner_brief
    assert 'Needs GPU renderer for PNGs.' in planner_brief


INDEPENDENT_INCONCLUSIVE = {
    'verdict': 'INCONCLUSIVE',
    'summary': 'Code defect fixed; no new defect found.',
    'findings': [],
    'sources': [],
    'remaining': ['Criterion 7: tests/visual_review.gd requires GPU renderer.',
                  'Criterion 8: 4 PNG captures unavailable headless.'],
    'route': 'codex-cli-gpt-6-2-sol',
    'actor': {'role': 'auditor', 'model': 'gpt-6.2-sol', 'route': 'codex-cli-gpt-6-2-sol'},
}


from test_conversation_permissions import (  # noqa: F401
    go_straight_on,
    host,
    intake,
    permission_worker,
    planned,
    wait_request,
)
from test_conversation_tasks import effect


@pytest.mark.parametrize('choice', ['once', 'deny', 'findings'])
def test_tui_lets_user_confirm_or_decline_independent_inconclusive_audit_with_zero_findings(
        host, tmp_path, monkeypatch, choice):
    tools, control, root, _ = host
    intake(host); task = planned(host)
    tools.state.update(session_id='parent')
    tools.permission_directory = tmp_path / 'prefs'
    go_straight_on(tools.permission_directory, root)

    review = deepcopy(INDEPENDENT_INCONCLUSIVE)
    if choice == 'findings':
        review.update(verdict='REVISE', findings=[{'claim': 'Real bug.', 'sources': []}])
    monkeypatch.setattr(conversation_review, 'review', lambda *a, **kw: deepcopy(review))
    audits = []

    def fake_run_unit(custody, st, args, stop, review=None, **kwargs):
        audit = review('tree', st['deployment'], task['plan']['unit'], {'a.py': b'value = 2\n'}, {})
        audits.append(audit)
        applied = audit['verdict'] == 'ACCEPT' or audit.get('user_decision') == permissions.USER_APPLIES
        return {'verdict': 'VERIFIED' if applied else 'NOT_REVIEWED', 'applied': applied, 'audit': audit, 'files': ['a.py']}

    monkeypatch.setattr(work_units, 'run_unit', fake_run_unit)
    with permission_worker(control) as pool:
        running = pool.submit(tools.execute, effect('implement', {}, 'exec-indep'), control)
        if choice == 'findings':
            output = json.loads(running.result(timeout=3).output_json)
            assert audits[0] == review and not output['applied'] and not permissions.pending(tools.root)
            return
        req = wait_request(tools.root)
        assert req['objective'] == permissions.APPLY_UNACCEPTED_INDEPENDENT_QUESTION
        assert req['audit']['label'] == 'independent'
        assert req['audit']['verdict'] == 'INCONCLUSIVE'
        assert req['audit']['open_items'] == INDEPENDENT_INCONCLUSIVE['remaining']
        permissions.answer(tools.root, req['id'], choice, project=root, directory=tools.permission_directory,
                           control_root=relay_mesh.RELAY_ROOT)
        output = json.loads(running.result(timeout=3).output_json)

    if choice == 'once':
        assert output['applied'] and output['audit']['user_decision'] == permissions.USER_APPLIES
    else:
        assert not output['applied'] and 'user_decision' not in output['audit']
        assert permissions.NOT_APPLIED_UNACCEPTED_INDEPENDENT in output['reason']
        assert output['next'].startswith('The user chose not to apply this change')


def test_independent_inconclusive_never_asks_or_applies_headless_and_same_family_still_requires_local_only(tmp_path):
    from test_planning_tools import execution
    root = tmp_path / 'repo'; root.mkdir()
    session = tmp_path / 'session'; session.mkdir()
    state = {'session_id': 'parent', 'config': {'repositories': {'repo': str(root)}}}
    task = {'id': 't1', 'plan': {'sha256': 'a' * 64, 'unit': {'repository': 'repo'}}}
    control = execution()
    # Headless (directory=None): never asks, never sets user_decision.
    assert permissions.confirm_apply(session, state, task, {'a.py': b''}, INDEPENDENT_INCONCLUSIVE, 'e', control, None) == INDEPENDENT_INCONCLUSIVE
    assert not permissions.pending(session)
    # Same-family INCONCLUSIVE outside --local_only still returns untouched even with a TUI directory.
    same_family = {**INDEPENDENT_INCONCLUSIVE, 'audit_label': 'same-family audit, not independent'}
    assert permissions.confirm_apply(session, state, task, {'a.py': b''}, same_family, 'e', control, tmp_path) == same_family
    assert not permissions.pending(session)
    # Independent failure_code never asks even with a TUI directory.
    failed = {**INDEPENDENT_INCONCLUSIVE, 'failure_code': 'DEADLINE'}
    assert permissions.confirm_apply(session, state, task, {'a.py': b''}, failed, 'e', control, tmp_path) == failed
    assert not permissions.pending(session)


def test_run_unit_applies_independent_inconclusive_on_user_decision_without_audit_label(tmp_path, monkeypatch):
    from test_implement_unit import deployment, repository
    root = tmp_path / 'repo'; root.mkdir()
    (root / 'README.md').write_text('base\n', encoding='utf-8')
    repository(root)
    custody = tmp_path / 'custody'; custody.mkdir()
    config = {'repositories': {'repo': str(root)}, 'writable': {'repo': ['docs/new.md']},
              'conversation_flow': True,
              'verifiers': {'conversation-diff': {'repository': 'repo',
                  'argv': ['git', 'diff', '--check', 'HEAD', '--', 'docs/new.md']}}}
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path / 'storage'))
    monkeypatch.setattr(work_units, 'routes', lambda *a, **kw: iter([('worker', deployment())]))

    def worker(child, *args, **kwargs):
        target = Path(child['repositories']['repo']) / 'docs/new.md'
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(b'New content.\n')
        return {'status': 'PROPOSED', 'model_calls': 1}

    monkeypatch.setattr(native_agent, 'run_session', worker)
    audit = {**INDEPENDENT_INCONCLUSIVE, 'sources': [], 'reference_sources': [],
             'user_decision': permissions.USER_APPLIES}
    result = work_units.run_unit(custody, {'config': config, 'conversation_flow': True,
        'deployment': deployment(), 'limits': {'max_model_calls': 30}, 'deadline_utc': None},
        {'title': 'New document', 'target_paths': ['docs/new.md'], 'criteria': ['Write it'],
         'verifiers': ['conversation-diff']}, lambda: False, review=lambda *a: audit)
    assert result['applied'] and (root / 'docs/new.md').read_bytes() == b'New content.\n'
    assert 'although the independent audit did not accept it' in result['next']
    assert 'never say it was audited or accepted' in result['next']


def test_tui_panel_for_independent_inconclusive_audit_defaults_focus_to_do_not_apply(tmp_path):
    from textual.widgets import Button, Static
    from relay.execution_permission_panel import ExecutionPermissionPanel
    from test_console_enter_permissions import FakeBackend, WorkConsole

    project, session = tmp_path / 'project', tmp_path / 'session'
    project.mkdir(); (session / 'execution-permission').mkdir(parents=True)
    request = {'schema': permissions.SCHEMA, 'kind': 'audit', 'session': 'parent', 'effect': 'e', 'task': 't',
               'plan': 'a' * 64, 'project': str(project.resolve()), 'paths': ['hub.gd'], 'checks': {}, 'tests': [],
               'objective': permissions.APPLY_UNACCEPTED_INDEPENDENT_QUESTION, 'status': 'WAITING',
               'audit': {'label': 'independent', 'verdict': 'INCONCLUSIVE', 'route': 'codex-cli-gpt-6-2-sol',
                         'summary': 'No code defects.', 'open_items': ['Criterion 8: PNG captures missing.'],
                         'open_items_total': 1}}
    request['id'] = permissions.identity(request)
    permissions.save_json(session / 'execution-permission' / 'request.json', request)

    async def scenario():
        app = WorkConsole(tmp_path, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=(100, 40)) as pilot:
            await app.workers.wait_for_complete(); await pilot.pause()
            app.refresh_execution_permission(SimpleNamespace(session=str(session), root=str(project)))
            await pilot.pause()
            panel = app.screen
            assert isinstance(panel, ExecutionPermissionPanel)
            shown = '\n'.join(str(s.content) for s in panel.query(Static))
            assert 'Audit (independent): INCONCLUSIVE by codex-cli-gpt-6-2-sol (not accepted)' in shown
            assert '- Criterion 8: PNG captures missing.' in shown
            assert [str(b.label) for b in panel.query(Button)] == ['Apply anyway', 'Do not apply']
            assert app.focused.id == 'permission-deny'

    asyncio.run(scenario())


def test_manual_gui_exclusions_apply_to_autodetect_and_non_godot_ecosystems(tmp_path, monkeypatch):
    root = tmp_path / 'proj'
    tree = tmp_path / 'tree'
    for d in (root, tree):
        (d / 'tests').mkdir(parents=True)
        (d / 'project.godot').write_text('config_version=5\n', encoding='utf-8')
        (d / 'tests' / 'smoke.gd').write_text('extends SceneTree\n', encoding='utf-8')
        (d / 'tests' / 'test_gui.py').write_text('def test_gui(): pass\n', encoding='utf-8')
        (d / 'tests' / 'test_unit.py').write_text('def test_ok(): pass\n', encoding='utf-8')
    (root / '.tare').mkdir()
    (root / '.tare' / 'checks.json').write_text(json.dumps({
        'checks': {
            'smoke-gui': {'mode': 'gui', 'paths': ['tests/smoke.gd']},
            'py-gui': {'mode': 'gui', 'paths': ['tests/test_gui.py']},
        }
    }), encoding='utf-8')
    monkeypatch.setattr(turn_engine, 'project_python', lambda p: sys.executable)
    monkeypatch.setattr(turn_engine.shutil, 'which', lambda name: '/usr/bin/godot_console' if name == 'godot_console' else None)
    # Remove Python tests to verify Godot smoke.gd autodetect skips gui-marked tests/smoke.gd.
    for f in (root / 'tests').glob('test_*.py'):
        f.unlink()
    cfg = turn_engine.config_for(root, mode='read-only')
    assert 'tests' not in cfg['verifiers']

    # Non-Godot pytest compilation also skips gui-marked tests/test_gui.py.
    config = {'conversation_flow': True, 'repositories': {'repo': str(root)}, 'verifiers': {}}
    unit = {'verifiers': [], 'test_paths': ['tests/test_gui.py', 'tests/test_unit.py']}
    compiled = work_units.checks(config, unit, 'repo', tree)
    assert 'pytest-tests:tests/test_unit.py' in compiled
    assert all('test_gui.py' not in ' '.join(argv) for argv in compiled.values())


def test_creating_new_tare_checks_json_is_blocked_as_protected_check_tampering(tmp_path, monkeypatch):
    from test_implement_unit import deployment, repository
    root = tmp_path / 'repo'; root.mkdir()
    (root / 'README.md').write_text('base\n', encoding='utf-8')
    (root / '.tare').mkdir()
    (root / '.tare' / 'keep').write_text('', encoding='utf-8')
    repository(root)
    custody = tmp_path / 'custody'; custody.mkdir()
    config = {'repositories': {'repo': str(root)}, 'writable': {'repo': ['.tare/checks.json', 'a.py']},
              'verifiers': {'ok': {'repository': 'repo', 'argv': [sys.executable, '-c', 'pass']}}}
    monkeypatch.setattr(work_units, 'routes', lambda *a, **kw: iter([('worker', deployment())]))

    def tampering_worker(child, *args, **kwargs):
        repo = Path(child['repositories']['repo'])
        (repo / 'a.py').write_text('x = 1\n', encoding='utf-8')
        (repo / '.tare' / 'checks.json').write_text('{"checks":{"tests":{"argv":["python","-c","pass"]}}}', encoding='utf-8')
        return {'status': 'PROPOSED', 'model_calls': 1}

    monkeypatch.setattr(native_agent, 'run_session', tampering_worker)
    result = work_units.run_unit(custody, {'config': config, 'deployment': deployment(),
        'limits': {'max_model_calls': 30}, 'deadline_utc': None},
        {'title': 'Tamper', 'target_paths': ['a.py', '.tare/checks.json'], 'criteria': ['Done'],
         'verifiers': ['ok']}, lambda: False, review=None)
    assert result['verdict'] == 'INCONCLUSIVE'
    assert '.tare/checks.json' in result['reason']
    assert not (root / '.tare' / 'checks.json').exists()


def test_suite_states_and_sandboxed_verifier_enforce_declared_timeout(tmp_path, monkeypatch):
    seen_timeouts = []

    def fake_port(argv, *, cwd, timeout, **kwargs):
        seen_timeouts.append((argv, timeout))
        return SimpleNamespace(returncode=0, stdout='ok\n', stderr='')

    monkeypatch.setattr(work_units, 'supervision', lambda: (None, None, None))
    token = work_units.CHECK_PORT.set(fake_port)
    try:
        work_units.suite_states(tmp_path, {'fast': ['cmd', '1']}, timeouts={'fast': 12})
    finally:
        work_units.CHECK_PORT.reset(token)
    assert seen_timeouts == [(['cmd', '1'], 12)]

    # Verify conversation_verifier.sandboxed tightens ExecutionControl.deadline_monotonic to kwargs['timeout'].
    from relay import command_protection
    from tare_tools_agent_runtime import BindingRef, CancellationToken, ExecutionControl
    seen_controls = []

    class FakeCaps:
        def __init__(self, **kw):
            pass

        def bind_command_mediation(self, port):
            pass

        def execute(self, request, ctrl):
            seen_controls.append(ctrl)
            return SimpleNamespace(status='succeeded', output_json=json.dumps({
                'returncode': 0, 'output': 'ok', 'process': {'cleanup_verified': True}}))

    monkeypatch.setattr(command_protection, 'sandbox_available', lambda: True)
    monkeypatch.setattr('compute_plane.native_capabilities.NativeCapabilities', FakeCaps)
    monkeypatch.setattr(command_protection, 'factory', lambda *a, **kw: (lambda **fkw: SimpleNamespace(close=lambda: None)))
    monkeypatch.setattr(command_protection, 'prepared_checks', lambda cmds: dict(cmds))
    ctrl = ExecutionControl(BindingRef('w', 'b', 'o', 1, 't'), CancellationToken(), 1000.0, monotonic=lambda: 100.0)
    root = tmp_path / 'r'; root.mkdir()
    verifier = command_protection.conversation_verifier(
        ctrl, tmp_path / 'custody', {'repositories': {'repo': str(root)}}, {}, root,
        {'key': 'u1', 'repository': 'repo', 'target_paths': ['a.py'], 'title': 't', 'criteria': ['c']},
        {'fast': ['cmd', '1']})
    done = verifier(['cmd', '1'], cwd=root, timeout=15, env={})
    assert len(seen_controls) == 1, getattr(done, 'execution_limitation', done)
    assert seen_controls[0].deadline_monotonic == 115.0


def test_godot_script_refusing_headless_is_skipped_from_auto_checks_and_surfaced_to_planner(tmp_path, monkeypatch):
    from test_plan_critic import PLAN, Stub, execution, request, wrap
    from test_native_agent import deployment
    from compute_plane.native_journal import NativeJournal

    root = tmp_path / 'game'
    tree = tmp_path / 'tree'
    headless_refuser = (
        'extends SceneTree\n'
        'func _initialize() -> void:\n'
        '\tif DisplayServer.get_name() == "headless":\n'
        '\t\tpush_error("visual_review requer renderizador; execute sem --headless")\n'
        '\t\tquit(1)\n'
        '\t\treturn\n'
    )
    graceful_skip = (
        'extends SceneTree\n'
        'func _initialize() -> void:\n'
        '\tif DisplayServer.get_name() == "headless":\n'
        '\t\treturn\n'
    )
    for d in (root, tree):
        (d / 'tests').mkdir(parents=True)
        (d / 'project.godot').write_text('config_version=5\n', encoding='utf-8')
        (d / 'tests' / 'experience.gd').write_text('extends SceneTree\n', encoding='utf-8')
        (d / 'tests' / 'visual_review.gd').write_text(headless_refuser, encoding='utf-8')
        (d / 'tests' / 'soft_skip.gd').write_text(graceful_skip, encoding='utf-8')
        (d / 'tests' / 'smoke.gd').write_text(headless_refuser, encoding='utf-8')

    assert work_units._godot_script_refuses_headless(root / 'tests' / 'visual_review.gd') is True
    assert work_units._godot_script_refuses_headless(root / 'tests' / 'experience.gd') is False
    assert work_units._godot_script_refuses_headless(root / 'tests' / 'soft_skip.gd') is False
    assert work_units.headless_refusing_godot_scripts(root) == ['tests/smoke.gd', 'tests/visual_review.gd']

    monkeypatch.setattr(work_forms.shutil, 'which', lambda name: '/usr/bin/godot_console' if name == 'godot_console' else None)
    monkeypatch.setattr(turn_engine.shutil, 'which', lambda name: '/usr/bin/godot_console' if name == 'godot_console' else None)

    # config_for() does not auto-register a headless-refusing tests/smoke.gd.
    assert turn_engine.config_for(root, mode='read-only')['verifiers'] == {}

    # work_units.checks() skips visual_review.gd even without .tare/checks.json, keeping experience.gd and soft_skip.gd.
    config = {'conversation_flow': True, 'repositories': {'game': str(root)}, 'verifiers': {}}
    unit = {'verifiers': [], 'test_paths': ['tests/experience.gd', 'tests/visual_review.gd', 'tests/soft_skip.gd']}
    planned_map = {}
    compiled = work_units.checks(config, unit, 'game', tree, planned_map)
    assert list(compiled) == ['godot-import', 'godot-tests:tests/experience.gd', 'godot-tests:tests/soft_skip.gd']
    assert 'godot-tests:tests/visual_review.gd' not in compiled

    # Planner mission surfaces detected_gui_only_scripts even before .tare/checks.json exists.
    parent = tmp_path / 'parent'; parent.mkdir()
    journal = NativeJournal(parent / 'journal.jsonl')
    journal.append({'kind': 'session_started', 'session_id': 'parent',
                    'instruction': 'TUI.\n\nOperator message:\nPolish HOME.'})
    journal.append({'kind': 'model_requested', 'call_id': 'parent/model/1'})
    candidate = dict(deployment(), id='fixture-planner')
    params = json.loads(candidate['native_profile']['parameters_json'])
    params['chat_template_kwargs']['reasoning_effort'] = 'high'
    candidate['native_profile']['parameters_json'] = json.dumps(params, sort_keys=True, separators=(',', ':'))
    state = {'deployment': deployment(), 'planning': {'routes': {'worker': candidate}},
             'limits': {'max_model_calls': 15}, 'deadline_utc': None, 'conversation_flow': True,
             'config': {'repositories': {'game': str(root)}, 'writable': {'game': ['hub.gd']},
                        'verifiers': {}, 'capabilities': ['read', 'edit']}}
    stub = Stub(monkeypatch, candidate, PLAN)
    stub.verdict = {'verdict': 'ACCEPT', 'summary': 'Sound.', 'findings': [], 'remaining': [], 'sources': []}
    control = execution()
    tool = wrap(parent, state, control)
    conversation_tasks.ConversationTools(tool, parent, state, 'turn').understand({
        'intent': 'deliver', 'relation': 'new', 'objective': 'Polish HOME.', 'criteria': [], 'kinds': ['games']})
    res = json.loads(tool.execute(request(), control).output_json)
    assert res['status'] == 'PLANNED'
    planner_brief = stub.sessions[0][3]
    assert 'detected_gui_only_scripts' in planner_brief
    assert 'tests/visual_review.gd' in planner_brief
