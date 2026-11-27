"""Tests for consent-gated executable `mode: "gui"` checks and bounded read-only `inspect_image` (`VisionTools`)."""
import asyncio
import hashlib
import io
import json
import math
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
from PIL import Image
from textual.widgets import Button, Checkbox

from compute_plane.native_attachments import NativeAttachmentStore
from compute_plane.native_models import encode
from tare_tools_agent_runtime import BindingRef, CancellationToken, ExecutionControl, EffectRequest, ToolCall

from relay import (
    agent_signals,
    conversation_permissions as permissions,
    conversation_review,
    conversation_tasks,
    execution_scope as scope,
    native_activity,
    native_agent,
    project_permissions,
    relay_mesh,
    turn_engine,
    vision_tools,
    work_units,
)
from relay.execution_permission_panel import ExecutionPermissionPanel
from relay.native_console import WorkConsole
from relay.permissions_panel import PermissionsPanel
from test_native_console import FakeBackend


def _make_png() -> bytes:
    buf = io.BytesIO()
    Image.new('RGB', (4, 4), (32, 128, 255)).save(buf, format='PNG')
    return buf.getvalue()


PNG_BYTES = _make_png()


def _git(cwd: Path, *args: str):
    env = {
        **os.environ,
        'GIT_AUTHOR_NAME': 'Test',
        'GIT_AUTHOR_EMAIL': 'test@example.com',
        'GIT_COMMITTER_NAME': 'Test',
        'GIT_COMMITTER_EMAIL': 'test@example.com',
    }
    return subprocess.run(['git', *args], cwd=cwd, env=env, check=True, capture_output=True, text=True)


def test_project_checks_supports_executable_gui_mode_while_keeping_manual_documentary_and_verifiers_headless(
    tmp_path, monkeypatch
):
    root = tmp_path / 'game'
    root.mkdir()
    (root / '.tare').mkdir()
    monkeypatch.setattr(turn_engine, 'project_python', lambda _p: sys.executable)
    monkeypatch.setattr(turn_engine.shutil, 'which', lambda name: '/usr/bin/godot_console' if name == 'godot_console' else None)

    doc = {
        'checks': {
            'smoke': {'argv': ['godot', '--headless', '--path', '.', '-s', 'tests/smoke.gd'], 'timeout_seconds': 60},
            'visual-review': {
                'mode': 'gui',
                'argv': ['godot', '--path', '.', '-s', 'tests/visual_review.gd'],
                'timeout_seconds': 120,
                'paths': ['tests/visual_review.gd'],
                'description': 'Render neon screenshots with desktop GPU.',
            },
            'doc-only-gui': {'mode': 'gui', 'paths': ['visual_lab/poc/compare.gd'], 'description': 'Manual GPU comparison.'},
            'manual-qa': {'mode': 'manual', 'description': 'Playtest on phone.'},
        }
    }
    (root / '.tare' / 'checks.json').write_text(json.dumps(doc), encoding='utf-8')

    parsed = turn_engine.project_checks(root)
    # Headless verifiers NEVER include mode="gui" or mode="manual".
    assert set(parsed['verifiers']) == {'smoke'}
    assert parsed['timeouts'] == {'smoke': 60}
    assert set(parsed['manual']) == {'visual-review', 'doc-only-gui', 'manual-qa'}
    assert parsed['manual']['visual-review']['argv'] == ['/usr/bin/godot_console', '--path', '.', '-s', 'tests/visual_review.gd']
    assert parsed['manual']['visual-review']['timeout_seconds'] == 120
    assert 'argv' not in parsed['manual']['doc-only-gui']

    gui = turn_engine.project_gui_checks(root)
    assert set(gui) == {'visual-review'}
    assert gui['visual-review']['argv'] == ['/usr/bin/godot_console', '--path', '.', '-s', 'tests/visual_review.gd']
    assert gui['visual-review']['timeout_seconds'] == 120

    cfg = turn_engine.config_for(root, mode='read-only')
    assert 'visual-review' not in cfg['verifiers']

    # Strict validation: manual still forbids argv; gui forbids timeout without argv and unsafe executables.
    with pytest.raises(ValueError, match='cannot specify argv'):
        turn_engine.validate_project_checks_data(root, {'checks': {'m': {'mode': 'manual', 'argv': ['python', '-V']}}}, strict=True)
    with pytest.raises(ValueError, match='requires argv'):
        turn_engine.validate_project_checks_data(root, {'checks': {'g': {'mode': 'gui', 'timeout_seconds': 30}}}, strict=True)
    with pytest.raises(ValueError, match='bare command name or safe relative path'):
        turn_engine.validate_project_checks_data(root, {'checks': {'g': {'mode': 'gui', 'argv': ['../evil.exe']}}}, strict=True)

    # propose_checks accepts executable mode="gui" cleanly.
    prepared = agent_signals.prepare_checks_proposal(root, {
        'checks': [{'name': 'visual-compare', 'mode': 'gui', 'argv': ['python', '-V'], 'paths': ['visual_lab/compare.gd']}],
    })
    assert 'visual-compare' in prepared['added']
    assert prepared['after']['visual-compare']['argv'] == ['python', '-V']


def test_gui_check_consent_fail_closed_headless_exact_argv_binding_local_restriction_and_revocation(
    tmp_path, monkeypatch
):
    storage = tmp_path / 'state'
    project = tmp_path / 'game'
    session = tmp_path / 'session'
    for d in (storage, project, session):
        d.mkdir(parents=True)
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(storage))
    monkeypatch.setattr(relay_mesh, 'RELAY_ROOT', storage / 'relay')

    state = {'session_id': 'sess-gui-1', 'config': {'repositories': {'game': str(project)}}}
    task = {'id': 'task-1', 'plan': {'sha256': 'a' * 64}}
    control = ExecutionControl(
        BindingRef('session:sess-gui-1', 'sess-gui-1', 'operator', 1, 'sess-gui-1'),
        CancellationToken(),
        math.inf,
    )
    argv_v1 = [sys.executable, '-c', 'print("ok")']
    argv_v2 = [sys.executable, '-c', 'print("changed")']

    # 1. Headless (directory=None) ALWAYS fails closed even if checks_outside_sandbox or gui check was remembered.
    turn_engine.set_checks_outside_sandbox(project, True)
    turn_engine.set_gui_check_allowed(project, 'visual-review', argv_v1, True)
    decide_headless = permissions.gui_check_permission(
        session, state, task, 'eff-1', project, control, None, 'inv-1'
    )
    ok, why = decide_headless('visual-review', argv_v1)
    assert ok is False and 'no interactive TUI' in why
    turn_engine.clear_gui_checks_allowed(project)

    # 2. checks_outside_sandbox alone does NOT authorize a GUI check; exact argv binding is enforced.
    assert not turn_engine.gui_check_allowed(project, 'visual-review', argv_v1)

    # 3. Interactive TUI modal: default focus is Skip GUI check; local model hides Always and rejects choice="project".
    async def check_modal(always_allowed: bool):
        req = {
            'schema': permissions.SCHEMA,
            'kind': 'gui_check',
            'session': 'sess-gui-1',
            'effect': 'eff-modal',
            'task': 'task-1',
            'plan': 'a' * 64,
            'project': str(project.resolve()),
            'paths': [],
            'checks': {'visual-review': argv_v1},
            'tests': [],
            'objective': 'Run declared GUI check?',
            'status': 'WAITING',
            'gui_check': {'check': 'visual-review', 'description': 'Render home screen',
                          **({} if always_allowed else {'always': False})},
        }
        app = WorkConsole(project, backend=FakeBackend(), initial_mode='assistant', storage_preferred=storage)
        async with app.run_test(size=(120, 36)) as pilot:
            await app.workers.wait_for_complete()
            panel = ExecutionPermissionPanel(req)
            app.push_screen(panel)
            await pilot.pause()
            # Default focus MUST be on the safe Skip button.
            assert app.focused is panel.query_one('#permission-deny', Button)
            project_btns = list(panel.query('#permission-project'))
            assert bool(project_btns) is always_allowed
            await pilot.press('escape')
            await pilot.pause()

    asyncio.run(check_modal(True))
    asyncio.run(check_modal(False))

    # 4. answer() rejects choice='project' for local work, and remembers exact (name, argv) for hosted work.
    folder = session / 'execution-permission'
    folder.mkdir(exist_ok=True)
    local_req = {
        'schema': permissions.SCHEMA, 'kind': 'gui_check', 'session': 'sess-gui-1', 'effect': 'eff-loc',
        'task': 'task-1', 'plan': 'a' * 64, 'project': str(project.resolve()), 'paths': [],
        'checks': {'visual-review': argv_v1}, 'tests': [], 'objective': 'Run?', 'status': 'WAITING',
        'gui_check': {'check': 'visual-review', 'always': False},
    }
    local_req['id'] = permissions.identity(local_req)
    (folder / 'request.json').write_text(json.dumps(local_req), encoding='utf-8')
    with pytest.raises(ValueError, match="local model's GUI checks"):
        permissions.answer(session, local_req['id'], 'project', project=project, directory=storage / 'console',
                           control_root=relay_mesh.RELAY_ROOT)

    hosted_req = {**local_req, 'effect': 'eff-host', 'gui_check': {'check': 'visual-review'}}
    hosted_req['id'] = permissions.identity(hosted_req)
    (folder / 'request.json').write_text(json.dumps(hosted_req), encoding='utf-8')
    assert permissions.answer(session, hosted_req['id'], 'project', project=project, directory=storage / 'console',
                              control_root=relay_mesh.RELAY_ROOT) == 'project'
    assert turn_engine.gui_check_allowed(project, 'visual-review', argv_v1) is True
    # Changing ANY arg invalidates the remembered approval!
    assert turn_engine.gui_check_allowed(project, 'visual-review', argv_v2) is False

    # 5. Interactive TUI honors remembered exact binding for hosted work, NEVER for local work.
    decide_tui = permissions.gui_check_permission(
        session, state, task, 'eff-2', project, control, storage / 'console', 'inv-2'
    )
    ok_hosted, why_hosted = decide_tui('visual-review', argv_v1, local_producer=False)
    assert ok_hosted is True and 'always for this exact GUI check' in why_hosted

    # Local work must ask (stub wait_for_answer to deny to prove it asked instead of auto-approving).
    asked = []
    monkeypatch.setattr(permissions, 'wait_for_answer', lambda _f, r, _c: asked.append(r) or {'choice': 'deny'})
    ok_local, _ = decide_tui('visual-review', argv_v1, local_producer=True)
    assert ok_local is False and len(asked) == 1 and asked[0]['gui_check']['always'] is False

    # 6. Revocable in /permissions (project_permissions.read_defaults / save_defaults).
    defaults, rev = project_permissions.read_defaults(storage / 'console', project, relay_mesh.RELAY_ROOT)
    assert defaults.get('gui_checks_allowed') is True
    updated, _ = project_permissions.save_defaults(
        storage / 'console', project, relay_mesh.RELAY_ROOT,
        {**defaults, 'gui_checks_allowed': False}, rev,
    )
    assert not updated.get('gui_checks_allowed')
    assert turn_engine.gui_check_allowed(project, 'visual-review', argv_v1) is False


def test_run_gui_checks_runs_consented_captures_artifacts_and_fails_closed_on_candidate_mutation(tmp_path):
    repo = tmp_path / 'proj'
    repo.mkdir()
    _git(repo, 'init', '-q')
    (repo / '.gitignore').write_text('build/\n', encoding='utf-8')
    (repo / 'app.py').write_text('x = 1\n', encoding='utf-8')
    _git(repo, 'add', '.')
    _git(repo, 'commit', '-q', '-m', 'init')
    base = _git(repo, 'rev-parse', 'HEAD').stdout.strip()

    # Candidate edits app.py.
    (repo / 'app.py').write_text('x = 2\n', encoding='utf-8')
    changed = work_units.changed_files(repo, base)
    job = tmp_path / 'job'
    job.mkdir()

    # 1. Skipped GUI check never runs.
    ran = []
    state_skip, text_skip, details_skip = work_units.run_gui_checks(
        repo,
        {'visual': {'argv': [sys.executable, '-c', 'import sys; sys.exit(99)']}},
        lambda n, a, d='': (ran.append(n), (False, 'skipped by user'))[1],
        job=job, base=base, changed_before_gui=changed,
    )
    assert state_skip == 'skipped' and ran == ['visual']
    assert details_skip['visual']['status'] == 'skipped'

    # 2. Consented GUI check writes both a gitignored PNG (build/home.png) and a non-ignored PNG (preview.png).
    render_code = (
        'from pathlib import Path; '
        f'raw = {PNG_BYTES!r}; '
        'Path("build").mkdir(exist_ok=True); '
        'Path("build/home.png").write_bytes(raw); '
        'Path("preview.png").write_bytes(raw); '
        'print("rendered 2 frames")'
    )
    state_ok, text_ok, details_ok = work_units.run_gui_checks(
        repo,
        {'visual': {'argv': [sys.executable, '-c', render_code], 'timeout_seconds': 30}},
        lambda _n, _a, _d='': (True, 'for this turn'),
        job=job, base=base, changed_before_gui=changed,
    )
    assert state_ok == 'green'
    assert 'rendered 2 frames' in text_ok
    # Non-ignored preview.png was moved out of candidate tree to <job>/gui-artifacts so changed_files stays pure!
    assert work_units.changed_files(repo, base) == changed
    assert not (repo / 'preview.png').exists()
    assert (repo / 'build' / 'home.png').is_file()
    arts = details_ok['visual']['artifacts']
    paths = {(a['repository'], a['path']) for a in arts}
    assert ('candidate', 'build/home.png') in paths
    assert ('review', 'preview.png') in paths
    # delivery_view strips internal saved_path custody strings.
    pub = conversation_tasks.delivery_view({'verdict': 'VERIFIED', 'gui_checks': details_ok})
    for a in pub['gui_checks']['visual']['artifacts']:
        assert 'saved_path' not in a

    # 3. GUI check mutating a candidate source file fails closed as red.
    mutate_code = 'from pathlib import Path; Path("app.py").write_text("x = 999\\n", encoding="utf-8")'
    state_mut, _, details_mut = work_units.run_gui_checks(
        repo,
        {'evil-gui': {'argv': [sys.executable, '-c', mutate_code], 'timeout_seconds': 30}},
        lambda _n, _a, _d='': (True, 'for this turn'),
        job=job, base=base, changed_before_gui=changed,
    )
    assert state_mut == 'red'
    assert 'modified candidate files' in details_mut['evil-gui']['reason']


def test_vision_tools_confinement_magic_bytes_cap_idempotency_and_attachments(tmp_path, monkeypatch):
    repo = tmp_path / 'proj'
    custody = tmp_path / 'session'
    attachments = tmp_path / 'parent-attachments'
    outside = tmp_path / 'outside.png'
    for d in (repo, custody, attachments):
        d.mkdir()
    outside.write_bytes(PNG_BYTES)

    (repo / 'shot.png').write_bytes(PNG_BYTES)
    (repo / 'fake.png').write_text('API_KEY=super-secret-value\n', encoding='utf-8')
    (repo / '.env').write_bytes(PNG_BYTES)
    (repo / 'code.py').write_text('print(1)\n', encoding='utf-8')
    (repo / 'big.png').write_bytes(PNG_BYTES + b'\x00' * (vision_tools.MAX_IMAGE_BYTES + 10))
    link = repo / 'link.png'
    try:
        link.symlink_to(outside)
        has_symlink = True
    except OSError:
        has_symlink = False

    store = NativeAttachmentStore(attachments)
    att_ref = store.put_bytes('user-screen.png', PNG_BYTES, 'image/png')

    # Mock vision backend.
    vision_calls = []

    def fake_describe(paths, emit, *, question=None, on_vision=None):
        vision_calls.append((question, paths[0].read_bytes()))
        if on_vision:
            on_vision({'phase': 'start', 'route': 'codex-vision'})
            on_vision({'phase': 'finish', 'route': 'codex-vision', 'ok': True, 'duration_ms': 12.0})
        return f'Observed skyline clearly for question: {question}'

    monkeypatch.setattr(turn_engine, 'describe_files', fake_describe)

    binding = BindingRef('session:s1', 's1', 'operator', 1, 's1')
    control = ExecutionControl(binding, CancellationToken(), math.inf)
    base_tools = SimpleNamespace(
        session_id='s1',
        binding=binding,
        names=('read',),
        tool_specs_json=encode({'read': {'description': 'Read', 'parameters': {'type': 'object'}}}),
        execute=lambda req, ctrl: None,
    )

    # Role restriction.
    with pytest.raises(ValueError, match='implementers and auditors'):
        vision_tools.VisionTools(base_tools, {'role': 'explorer', 'repositories': {'p': str(repo)}}, custody)

    vt = vision_tools.VisionTools(
        base_tools,
        {'role': 'auditor', 'repositories': {'candidate': str(repo)},
         'image_inspection': {'attachments_root': str(attachments)}},
        custody,
    )
    assert 'inspect_image' in vt.names

    def call(eid, repository, path, question='Check skyline'):
        req = EffectRequest('s1', eid, ToolCall(eid, 'inspect_image', encode({
            'repository': repository, 'path': path, 'question': question,
        })))
        res = vt.execute(req, control)
        return res.status, json.loads(res.output_json)

    # Confinement & magic-byte refusals (zero vision calls made).
    assert call('r1', 'candidate', '../outside.png')[0] == 'refused'
    assert call('r2', 'candidate', 'fake.png')[1]['reason'] == 'inspect_image_invalid_image_bytes'
    assert call('r3', 'candidate', '.env')[1]['reason'] == 'inspect_image_disallowed_file'
    assert call('r4', 'candidate', 'code.py')[1]['reason'] == 'inspect_image_unsupported_extension'
    assert call('r5', 'candidate', 'big.png')[1]['reason'] == 'inspect_image_size_out_of_bounds'
    assert call('r6', 'unknown-repo', 'shot.png')[0] == 'refused'
    if has_symlink:
        assert call('r7', 'candidate', 'link.png')[1]['reason'] == 'inspect_image_symlink_refused'
    assert len(vision_calls) == 0

    # Valid repository image inspection.
    st, out = call('ok-1', 'candidate', 'shot.png', 'Are buildings visible?')
    assert st == 'succeeded'
    assert out['status'] == 'OBSERVED' and out['authority'] == 'NONE'
    assert out['sha256'] == hashlib.sha256(PNG_BYTES).hexdigest()
    assert out['route'] == 'codex-vision'
    assert out['remaining_inspections'] == vision_tools.MAX_IMAGE_INSPECTIONS - 1
    assert 'Are buildings visible?' in out['observation']
    assert len(vision_calls) == 1

    # Replay with same effect_id is idempotent (no second vision call).
    st_dup, out_dup = call('ok-1', 'candidate', 'shot.png', 'Are buildings visible?')
    assert st_dup == 'succeeded' and out_dup == out and len(vision_calls) == 1

    # Valid preserved attachment inspection by exact 64-hex sha256.
    st_att, out_att = call('ok-2', 'attachments', att_ref.sha256, 'What is broken in user screenshot?')
    assert st_att == 'succeeded' and out_att['sha256'] == att_ref.sha256 and len(vision_calls) == 2

    # Cap enforcement after 6 distinct calls.
    for i in range(3, vision_tools.MAX_IMAGE_INSPECTIONS + 1):
        st_i, _ = call(f'ok-{i}', 'candidate', 'shot.png', f'Check #{i}')
        assert st_i == 'succeeded'
    st_cap, out_cap = call('ok-over', 'candidate', 'shot.png', 'One too many')
    assert st_cap == 'refused' and 'inspect_image_limit_reached' in out_cap['reason']

    # native_agent.configuration() enforces role restriction for image_inspection.
    with pytest.raises(ValueError, match='image_inspection_role_not_allowed'):
        native_agent.configuration({
            'schema': 'tare.tools/native-agent-config/1',
            'role': 'explorer',
            'repositories': {'p': str(repo)},
            'image_inspection': True,
        })
    cfg_ok = native_agent.configuration({
        'schema': 'tare.tools/native-agent-config/1',
        'role': 'auditor',
        'repositories': {'p': str(repo)},
        'image_inspection': {'attachments_root': str(attachments)},
    })
    assert cfg_ok['image_inspection']['attachments_root'] == str(attachments.resolve())

    # ActivityJournal projects inspect_image cleanly with repo: path target.
    row = native_activity.activity_view({
        'kind': 'tool-activity', 'id': 'eff-img', 'operation': 'inspect_image',
        'state': 'succeeded', 'target': 'candidate: build/home.png',
    })
    assert row['operation'] == 'inspect_image' and row['target'] == 'candidate: build/home.png'


def test_permissions_panel_displays_and_revokes_remembered_gui_checks_and_detected_gui_checks(tmp_path, monkeypatch):
    storage = tmp_path / 'state'
    project = tmp_path / 'game'
    storage.mkdir()
    project.mkdir()
    (project / '.tare').mkdir()
    (project / '.tare' / 'checks.json').write_text(json.dumps({
        'checks': {
            'visual-review': {'mode': 'gui', 'argv': ['python', '-c', 'print("gui ok")'], 'description': 'Desktop GPU check'},
        }
    }), encoding='utf-8')
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(storage))
    monkeypatch.setattr(relay_mesh, 'RELAY_ROOT', storage / 'relay')
    monkeypatch.setattr(turn_engine, 'project_python', lambda _p: sys.executable)

    gui_map = conversation_tasks.detected_gui_checks({'repositories': {'game': str(project)}})
    assert set(gui_map) == {'visual-review'}
    assert gui_map['visual-review']['repository'] == 'game'

    argv = gui_map['visual-review']['argv']
    turn_engine.set_gui_check_allowed(project, 'visual-review', argv, True)
    assert turn_engine.gui_check_allowed(project, 'visual-review', argv) is True

    async def scenario():
        app = WorkConsole(project, backend=FakeBackend(), initial_mode='assistant', storage_preferred=storage)
        async with app.run_test(size=(120, 36)) as pilot:
            await app.workers.wait_for_complete()
            app.action_permissions()
            for _ in range(40):
                if isinstance(app.screen, PermissionsPanel) and app.screen.revision is not None:
                    break
                await pilot.pause(0.05)
            panel = app.screen
            assert isinstance(panel, PermissionsPanel)
            cb = panel.query_one('#permissions-gui-checks', Checkbox)
            assert cb.display is True and cb.value is True
            cb.value = False
            await pilot.click('#permissions-save')
            for _ in range(80):
                if not turn_engine.gui_check_allowed(project, 'visual-review', argv) and not panel._is_saving:
                    break
                await pilot.pause(0.05)
            await app.workers.wait_for_complete()
            assert turn_engine.gui_check_allowed(project, 'visual-review', argv) is False
            await pilot.press('escape')
            await pilot.pause()

    asyncio.run(scenario())


def test_conversation_check_tool_runs_consented_gui_check_and_review_copies_gui_artifacts(tmp_path, monkeypatch):
    from compute_plane.native_journal import NativeJournal
    from test_conversation_tasks import deployment

    storage = tmp_path / 'state'
    storage.mkdir()
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(storage))
    monkeypatch.setattr(relay_mesh, 'RELAY_ROOT', storage / 'relay')
    monkeypatch.setattr(turn_engine, 'project_python', lambda _p: sys.executable)

    project = tmp_path / 'proj'
    project.mkdir()
    _git(project, 'init', '-q')
    (project / 'a.py').write_text('value = 1\n', encoding='utf-8')
    (project / '.tare').mkdir()
    (project / '.tare' / 'checks.json').write_text(json.dumps({
        'checks': {
            'unit': {'argv': ['python', '-c', 'print("unit ok")']},
            'visual-review': {
                'mode': 'gui',
                'argv': ['python', '-c', f'from pathlib import Path; Path("shot.png").write_bytes({PNG_BYTES!r}); print("gui ok")'],
                'description': 'Render desktop preview',
            },
        }
    }), encoding='utf-8')
    _git(project, 'add', '.')
    _git(project, 'commit', '-q', '-m', 'init')

    session = tmp_path / 'session'
    session.mkdir()
    binding = BindingRef('session:s-conv', 's-conv', 'operator', 1, 's-conv')
    control = ExecutionControl(binding, CancellationToken(), math.inf)
    state = {
        'session_id': 's-conv',
        'deployment': deployment(),
        'limits': {'max_model_calls': 20, 'max_seconds': 600},
        'config': {
            'schema': 'tare.tools/native-agent-config/1',
            'role': 'explorer',
            'repositories': {'repo': str(project)},
            'writable': {},
            'verifiers': {},
        },
    }
    base_tools = SimpleNamespace(
        session_id='s-conv', binding=binding, names=('read',),
        tool_specs_json=encode({'read': {'description': 'Read', 'parameters': {'type': 'object'}}}),
        execute=lambda req, ctrl: None,
    )
    conv = conversation_tasks.ConversationTools(
        base_tools, session, state, invocation_id='a' * 32, permission_directory=storage / 'console',
    )

    # 1. Invalid check name refusal surfaces available_gui_checks separately from available_checks.
    bad_req = EffectRequest('s-conv', 'e-bad', ToolCall('e-bad', 'check', encode({'names': ['unknown-check']})))
    bad_res = json.loads(conv.execute(bad_req, control).output_json)
    assert 'visual-review' not in bad_res['available_checks']
    assert 'visual-review' in bad_res['available_gui_checks']

    # 2. Denied GUI check stays inconclusive without running.
    monkeypatch.setattr(permissions, 'wait_for_answer', lambda _f, _r, _c: {'choice': 'deny'})
    deny_req = EffectRequest('s-conv', 'e-deny', ToolCall('e-deny', 'check', encode({'names': ['visual-review']})))
    deny_res = json.loads(conv.execute(deny_req, control).output_json)
    assert deny_res['status'] == 'inconclusive'
    assert deny_res['gui_checks']['visual-review']['status'] == 'skipped'

    # 3. Consented GUI check executes cleanly and reports green + gui_checks.
    monkeypatch.setattr(permissions, 'wait_for_answer', lambda _f, _r, _c: {'choice': 'once'})
    ok_req = EffectRequest('s-conv', 'e-ok', ToolCall('e-ok', 'check', encode({'names': ['visual-review']})))
    ok_res = json.loads(conv.execute(ok_req, control).output_json)
    assert ok_res['status'] == 'green'
    assert ok_res['gui_checks']['visual-review']['status'] == 'green'
    assert 'gui ok' in ok_res['checks']

    # 4. conversation_review copies saved_path GUI artifacts into review:gui-artifacts/... and enables image_inspection.
    (project / 'a.py').write_text('value = 2\n', encoding='utf-8')
    saved_shot = tmp_path / 'saved-shot.png'
    saved_shot.write_bytes(PNG_BYTES)
    shot_sha = hashlib.sha256(PNG_BYTES).hexdigest()
    NativeJournal(session / 'journal.jsonl').append({'kind': 'session_started', 'session_id': 's-conv'})
    task = {
        'id': 'task-1',
        'intent': 'deliver',
        'objective': 'Update value and verify visual frame.',
        'criteria': ['value equals two'],
        'plan': {
            'sha256': 'b' * 64,
            'artifact': str(session / 'plan.json'),
            'sources': [],
            'unit': {'repository': 'repo', 'target_paths': ['a.py'], 'criteria': ['value equals two'], 'verifiers': []},
        },
    }
    monkeypatch.setattr(turn_engine, 'pick_auditors', lambda *a, **kw: [('aud-1', deployment())])
    seen_configs = []

    def fake_run_session(cfg, dep, custody_dir, instruction, **kwargs):
        seen_configs.append((cfg, instruction))
        return {'status': 'FAILED', 'reason': 'STOPPED_IN_TEST'}

    monkeypatch.setattr(native_agent, 'run_session', fake_run_session)
    conversation_review.review(
        conv, task, project, deployment(), task['plan']['unit'],
        {'a.py': b'value = 2\n'},
        {
            'before': 'green',
            'after': 'green',
            'gui_checks': {
                'visual-review': {
                    'status': 'green',
                    'exit_code': 0,
                    'artifacts': [{'repository': 'review', 'path': 'shot.png', 'sha256': shot_sha, 'saved_path': str(saved_shot)}],
                }
            },
        },
        control,
    )
    assert len(seen_configs) == 1
    aud_cfg, aud_prompt = seen_configs[0]
    assert aud_cfg['image_inspection'] is True
    assert 'inspect_image' in aud_prompt
    review_dir = Path(aud_cfg['repositories']['review'])
    copied = review_dir / 'gui-artifacts' / 'visual-review' / 'shot.png'
    assert copied.is_file() and copied.read_bytes() == PNG_BYTES
    req_doc = json.loads((review_dir / 'request.json').read_text(encoding='utf-8'))
    gui_arts = req_doc['host_verification']['gui_checks']['visual-review']['artifacts']
    assert gui_arts == [{'repository': 'review', 'path': 'gui-artifacts/visual-review/shot.png', 'sha256': shot_sha}]


def test_review_findings_gui_cross_repo_turn_cap_cjk_overflow_cleanup_and_identity(tmp_path, monkeypatch):
    from compute_plane.native_journal import NativeJournal
    from test_conversation_tasks import deployment

    storage = tmp_path / 'state'
    repo_a = tmp_path / 'repo-a'
    repo_b = tmp_path / 'repo-b'
    session = tmp_path / 'session'
    attachments = tmp_path / 'attachments'
    for d in (storage, repo_a, repo_b, session, attachments):
        d.mkdir(parents=True)
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(storage))
    monkeypatch.setattr(relay_mesh, 'RELAY_ROOT', storage / 'relay')

    # 1. P1: One-turn GUI consent in repo A NEVER authorizes identically named command in repo B.
    binding = BindingRef('session:s-x', 's-x', 'operator', 1, 's-x')
    control = ExecutionControl(binding, CancellationToken(), math.inf)
    state = {'session_id': 's-x', 'config': {'repositories': {'a': str(repo_a), 'b': str(repo_b)}}}
    task = {'id': 't1', 'plan': {'sha256': 'c' * 64}}
    argv = [sys.executable, '-c', 'print("ok")']
    asked = []
    monkeypatch.setattr(permissions, 'wait_for_answer', lambda _f, r, _c: asked.append(r['project']) or {'choice': 'once'})
    decide_a = permissions.gui_check_permission(session, state, task, 'e1', repo_a, control, storage / 'console', 'inv-1')
    decide_b = permissions.gui_check_permission(session, state, task, 'e2', repo_b, control, storage / 'console', 'inv-1')
    assert decide_a('visual', argv)[0] is True
    assert decide_a('visual', argv)[0] is True  # cached for repo A in this turn
    assert len(asked) == 1
    assert decide_b('visual', argv)[0] is True  # MUST ask separately for repo B!
    assert len(asked) == 2

    # 2. P2: Attachment reads strictly require 64-hex SHA-256 (unverified.png is refused) AND 6 CJK observations
    #    cannot overflow vision-inspections.json or bypass the 6-call cap.
    (attachments / 'unverified.png').write_bytes(PNG_BYTES)
    (repo_a / 'shot.png').write_bytes(PNG_BYTES)
    cjk_calls = []

    def fake_cjk_describe(paths, emit, *, question=None, on_vision=None):
        cjk_calls.append(question)
        if on_vision:
            on_vision({'phase': 'finish', 'route': 'codex-vision', 'ok': True, 'duration_ms': 5.0})
        return ('\x01' * 1000) + ('"' * 3000) + ('画' * 4000)  # control chars + JSON-escaped quotes + CJK

    monkeypatch.setattr(turn_engine, 'describe_files', fake_cjk_describe)
    base_tools = SimpleNamespace(
        session_id='s-x', binding=binding, names=('read',),
        tool_specs_json=encode({'read': {'description': 'Read', 'parameters': {'type': 'object'}}}),
        execute=lambda req, ctrl: None,
    )
    vt = vision_tools.VisionTools(
        base_tools,
        {'role': 'implementer', 'repositories': {'a': str(repo_a)},
         'image_inspection': {'attachments_root': str(attachments)}},
        session,
    )
    bad_att = vt.execute(
        EffectRequest('s-x', 'att-bad', ToolCall('att-bad', 'inspect_image', encode({
            'repository': 'attachments', 'path': 'unverified.png', 'question': 'Read unverified',
        }))),
        control,
    )
    assert bad_att.status == 'refused'
    assert json.loads(bad_att.output_json)['reason'] == 'inspect_image_attachment_sha256_required'
    assert len(cjk_calls) == 0

    for i in range(vision_tools.MAX_IMAGE_INSPECTIONS):
        res = vt.execute(
            EffectRequest('s-x', f'cjk-{i}', ToolCall(f'cjk-{i}', 'inspect_image', encode({
                'repository': 'a', 'path': 'shot.png', 'question': f'CJK #{i}',
            }))),
            control,
        )
        assert res.status == 'succeeded'
        out = json.loads(res.output_json)
        assert out['truncated'] is True
        assert len(out['observation'].encode('utf-8')) <= vision_tools.MAX_OBSERVATION_BYTES
    over = vt.execute(
        EffectRequest('s-x', 'cjk-over', ToolCall('cjk-over', 'inspect_image', encode({
            'repository': 'a', 'path': 'shot.png', 'question': '7th call',
        }))),
        control,
    )
    assert over.status == 'refused' and 'inspect_image_limit_reached' in json.loads(over.output_json)['reason']
    assert len(cjk_calls) == vision_tools.MAX_IMAGE_INSPECTIONS

    # 3. P2: CheckCleanupUnknown MUST propagate out of run_gui_checks so candidate retention triggers.
    def fail_cleanup(*_a, **_kw):
        raise work_units.CheckCleanupUnknown('cleanup unconfirmed')

    monkeypatch.setattr(work_units, 'managed_check', fail_cleanup)
    with pytest.raises(work_units.CheckCleanupUnknown):
        work_units.run_gui_checks(
            repo_a, {'visual': {'argv': argv}}, lambda _n, _a, _d='': (True, 'for this turn'),
        )

    # 4. P2: Changing GUI check argv or output produces a distinct review identity (never reuses stale ACCEPT).
    _git(repo_a, 'init', '-q')
    (repo_a / 'a.py').write_text('value = 1\n', encoding='utf-8')
    _git(repo_a, 'add', '.')
    _git(repo_a, 'commit', '-q', '-m', 'init')
    (repo_a / 'a.py').write_text('value = 2\n', encoding='utf-8')
    rev_session = tmp_path / 'rev-session'
    rev_session.mkdir()
    NativeJournal(rev_session / 'journal.jsonl').append({'kind': 'session_started', 'session_id': 's-rev'})
    rev_state = {
        'session_id': 's-rev', 'deployment': deployment(),
        'limits': {'max_model_calls': 20, 'max_seconds': 600},
        'config': {'schema': 'tare.tools/native-agent-config/1', 'role': 'explorer',
                   'repositories': {'repo': str(repo_a)}, 'writable': {}, 'verifiers': {}},
    }
    owner = conversation_tasks.ConversationTools(
        base_tools, rev_session, rev_state, invocation_id='b' * 32, permission_directory=storage / 'console',
    )
    rev_task = {
        'id': 'task-rev', 'intent': 'deliver', 'objective': 'Verify GUI', 'criteria': ['ok'],
        'plan': {'sha256': 'd' * 64, 'artifact': str(rev_session / 'plan.json'), 'sources': [],
                 'unit': {'repository': 'repo', 'target_paths': ['a.py'], 'criteria': ['ok'], 'verifiers': []}},
    }
    monkeypatch.setattr(turn_engine, 'pick_auditors', lambda *a, **kw: [('aud-1', deployment())])
    dispatched = []
    monkeypatch.setattr(native_agent, 'run_session', lambda cfg, dep, cdir, instr, **kw: (
        dispatched.append(str(cdir)), {'status': 'FAILED', 'reason': 'TEST'})[1])
    for output_msg in ('frame ok: 12 buildings', 'frame ok: 0 buildings occluded'):
        conversation_review.review(
            owner, rev_task, repo_a, deployment(), rev_task['plan']['unit'], {'a.py': b'value = 2\n'},
            {'before': 'green', 'after': 'green',
             'gui_checks': {'visual': {'status': 'green', 'exit_code': 0, 'argv': argv, 'output': output_msg}}},
            control,
        )
    assert len(dispatched) == 2 and dispatched[0] != dispatched[1]
