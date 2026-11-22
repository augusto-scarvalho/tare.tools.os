"""Offline tests for managed configurable skills, frozen session activation, CLI, and /skills TUI (SKL-01)."""

import asyncio
import json
from pathlib import Path

import pytest
from textual.widgets import Button, Input, OptionList, TextArea

from relay import console_backend
from relay import managed_skills as managed
from relay import native_agent
from relay import skill_bundles as bundles
from relay.console_skills import SkillRolesDialog, SkillsPanel
from relay.console_workbench import WorkbenchPanel
from relay.native_console import WorkConsole
import tare_ops
from test_native_agent import Client, deployment, setup
from test_native_console import FakeBackend


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8', newline='\n')
    return path


def test_managed_skill_lifecycle_parameters_overlays_project_overrides_and_upstream_update(tmp_path):
    storage = tmp_path / 'storage'
    src = tmp_path / 'pxl-skill'
    proj_a = tmp_path / 'proj-a'
    proj_b = tmp_path / 'proj-b'
    proj_a.mkdir()
    proj_b.mkdir()

    _write(src, 'SKILL.md', '---\nname: PXL Skill\ndescription: Draw sprites\n---\nUse palette <palette> at scale <scale>.\n')
    v1 = bundles.capture_bundle(src, storage_root=storage)

    adopted = managed.adopt_snapshot('pxl-skill', v1['snapshot_id'], storage_root=storage)
    assert adopted['skill_id'] == 'pxl-skill'
    assert adopted['authority'] == 'NONE' and adopted['effects_authorized'] is False
    assert adopted['origin']['snapshot_id'] == v1['snapshot_id']
    assert adopted['text_source'] == 'base'
    assert adopted['upstream_update']['available'] is False
    # Kept strictly separate from permanent /guidance store.
    assert not (storage / 'serve' / 'guidance.json').exists()
    assert (storage / 'serve' / 'skills' / 'managed.json').is_file()

    # Typed parameters + validation.
    managed.set_parameter('pxl-skill', 'palette', 'warm', param_type='choice',
                          choices=['warm', 'cool'], description='Color palette', storage_root=storage)
    managed.set_parameter('pxl-skill', 'scale', '2', param_type='int', storage_root=storage)
    with pytest.raises(ValueError, match='allowed choices'):
        managed.set_parameter('pxl-skill', 'palette', 'neon', storage_root=storage)
    with pytest.raises(ValueError, match='Invalid int'):
        managed.set_parameter('pxl-skill', 'scale', 'not-an-int', storage_root=storage)

    eff_global = managed.effective_view('pxl-skill', storage_root=storage)
    assert 'Use palette warm at scale 2.' in eff_global['effective_text']

    # Per-project parameter & overlay overrides do not mutate global config or other projects.
    managed.set_parameter('pxl-skill', 'palette', 'cool', project=proj_a, storage_root=storage)
    managed.set_overlay('pxl-skill', 'Project A custom <palette> scale <scale>.\n', project=proj_a, storage_root=storage)
    eff_a = managed.effective_view('pxl-skill', project=proj_a, storage_root=storage)
    eff_b = managed.effective_view('pxl-skill', project=proj_b, storage_root=storage)
    assert eff_a['effective_text'] == 'Project A custom cool scale 2.\n'
    assert eff_a['text_source'] == 'project_overlay'
    assert 'Use palette warm at scale 2.' in eff_b['effective_text']
    assert eff_b['text_source'] == 'base'

    # Global overlay preserves base_text and exposes reviewable diff.
    managed.set_overlay('pxl-skill', 'Custom global <palette> scale <scale>.\n', storage_root=storage)
    eff_edited = managed.effective_view('pxl-skill', storage_root=storage)
    assert eff_edited['has_overlay'] is True
    assert '-Use palette <palette> at scale <scale>.' in eff_edited['overlay_diff']
    assert '+Custom global <palette> scale <scale>.' in eff_edited['overlay_diff']

    # Capturing a newer inert snapshot surfaces an explicit upstream update without auto-mutating managed text.
    _write(src, 'SKILL.md', '---\nname: PXL Skill\ndescription: Draw sprites\n---\nUpstream v2 <palette> scale <scale>.\n')
    v2 = bundles.capture_bundle(src, storage_root=storage)
    before_update = managed.effective_view('pxl-skill', storage_root=storage)
    assert before_update['upstream_update']['available'] is True
    assert before_update['upstream_update']['latest_snapshot_id'] == v2['snapshot_id']
    assert '+Upstream v2 <palette> scale <scale>.' in before_update['upstream_update']['skill_md_diff']
    assert before_update['effective_text'] == 'Custom global warm scale 2.\n'

    # Apply upstream while keeping overlay updates base only.
    kept = managed.apply_upstream_update('pxl-skill', v2['snapshot_id'], keep_overlay=True, storage_root=storage)
    assert kept['origin']['snapshot_id'] == v2['snapshot_id']
    assert kept['has_overlay'] is True
    assert kept['effective_text'] == 'Custom global warm scale 2.\n'
    assert kept['upstream_update']['available'] is False

    # Apply upstream without keep_overlay switches to upstream base while preserving parameters.
    switched = managed.apply_upstream_update('pxl-skill', v2['snapshot_id'], keep_overlay=False, storage_root=storage)
    assert switched['has_overlay'] is False
    assert 'Upstream v2 warm scale 2.' in switched['effective_text']


def test_session_freezes_managed_skill_and_later_edits_never_mutate_running_or_resumed_session(setup, tmp_path, monkeypatch):
    config, custody, root = setup
    storage = tmp_path / 'storage'
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(storage))
    src = tmp_path / 'skill-src'
    _write(src, 'SKILL.md', '---\nname: Review Helper\n---\nFrozen rule <mode> v1.\n')
    bundles.capture_bundle(src, storage_root=storage)
    managed.adopt_snapshot('review-helper', storage_root=storage)
    managed.set_parameter('review-helper', 'mode', 'strict', storage_root=storage)

    config.update(capabilities=['read', 'list'], verifiers={}, managed_skills=['review-helper'])
    client1 = Client(steps=[('tare_deliver', {'summary': 'Turn 1 complete.'})])
    result1 = native_agent.run_session(config, deployment(), custody, 'Inspect repo.',
                                       client=client1, local_counter=client1)
    assert result1['status'] == 'PROPOSED'

    state = json.loads((custody / 'native-agent.json').read_text(encoding='utf-8'))
    assert len(state['managed_skills']) == 1
    assert state['managed_skills'][0]['skill_id'] == 'review-helper'
    assert state['managed_skills'][0]['authority'] == 'NONE'
    assert state['managed_skills'][0]['effects_authorized'] is False

    systems_turn1 = [m['content'] for path, body in client1.calls if path == '/chat/completions'
                     for m in body['messages'] if m['role'] == 'system']
    assert any('Frozen rule strict v1.' in s for s in systems_turn1)
    assert any('auxiliary files, scripts, and external bindings remain inert data' in s for s in systems_turn1)

    # Mutate managed skill, parameters, and even remove it after session started.
    managed.set_parameter('review-helper', 'mode', 'relaxed', storage_root=storage)
    managed.set_overlay('review-helper', 'MUTATED v2 <mode>.\n', storage_root=storage)
    managed.remove_managed('review-helper', storage_root=storage)

    # Resuming the saved session MUST keep the exact frozen v1 content.
    client2 = Client(steps=[('tare_deliver', {'summary': 'Turn 2 complete.'})])
    result2 = native_agent.run_session(None, None, custody, resume=True, follow_up='Continue.', message_id='m2',
                                       client=client2, local_counter=client2)
    assert result2['status'] == 'PROPOSED'
    systems_turn2 = [m['content'] for path, body in client2.calls if path == '/chat/completions'
                     for m in body['messages'] if m['role'] == 'system']
    assert any('Frozen rule strict v1.' in s for s in systems_turn2)
    assert not any('MUTATED v2' in s for s in systems_turn2)


def test_freeze_for_session_fails_closed_when_backing_snapshot_was_tampered(tmp_path):
    storage = tmp_path / 'storage'
    src = tmp_path / 'skill'
    _write(src, 'SKILL.md', '---\nname: Guarded\n---\nSafe content.\n')
    cap = bundles.capture_bundle(src, storage_root=storage)
    managed.adopt_snapshot('guarded', storage_root=storage)
    # Tamper with stored snapshot bytes.
    (Path(cap['snapshot_dir']) / 'files' / 'SKILL.md').write_text('TAMPERED', encoding='utf-8')
    with pytest.raises(ValueError, match='hash mismatch|size mismatch'):
        managed.freeze_for_session(['guarded'], storage_root=storage)


def test_cli_managed_skills_and_agent_skill_flag_wiring(tmp_path, monkeypatch, capsys):
    storage = tmp_path / 'storage'
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(storage))
    src = tmp_path / 'demo-skill'
    _write(src, 'SKILL.md', '---\nname: Demo CLI\n---\nHello <who>.\n')

    assert tare_ops.main(['skills', 'capture', str(src), '--json']) == 0
    capsys.readouterr()
    assert tare_ops.main(['skills', 'adopt', 'demo-cli', '--json']) == 0
    adopted = json.loads(capsys.readouterr().out)
    assert adopted['skill_id'] == 'demo-cli'

    assert tare_ops.main(['skills', 'param', 'demo-cli', 'who', 'world', '--json']) == 0
    param_view = json.loads(capsys.readouterr().out)
    assert 'Hello world.' in param_view['effective_text']

    assert tare_ops.main(['skills', 'managed', '--json']) == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 1 and rows[0]['skill_id'] == 'demo-cli'

    assert tare_ops.main(['skills', 'bindings', '--json']) == 0
    bindings = json.loads(capsys.readouterr().out)
    assert {b['runtime'] for b in bindings} >= {'Claude CLI', 'Codex CLI', 'AGY CLI', 'Direct HTTP API', 'Local Gateway'}

    # console_backend.command passes --skill for new sessions only.
    proj = tmp_path / 'project'
    proj.mkdir()
    sel = console_backend.Selection(root=str(proj), mode='session', prompt='Do work', managed_skills=['demo-cli'])
    argv = console_backend.command(sel, resume=False)
    assert '--skill' in argv and argv[argv.index('--skill') + 1] == 'demo-cli'

    # native_agent CLI refuses --skill with --resume.
    rc = native_agent.main(['--resume', '--session', str(tmp_path / 's'), '--skill', 'demo-cli', '--format', 'json'])
    assert rc == 2
    err = json.loads(capsys.readouterr().out)
    assert 'native_agent_startup_options_are_saved' in err['reason']


@pytest.mark.parametrize('size', [(80, 24), (130, 44)])
def test_tui_skills_panel_capture_manage_edit_activate_and_bindings_without_models(tmp_path, monkeypatch, size):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path / 'state'))
    root = tmp_path / 'project'
    root.mkdir()
    ext = tmp_path / 'external-skill'
    _write(ext, 'SKILL.md', '---\nname: Local PXL\n---\nBase instructions.\n')

    async def scenario():
        app = WorkConsole(root, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            app.action_skills()
            await pilot.pause()
            panel = app.screen
            assert isinstance(panel, WorkbenchPanel)
            assert isinstance(panel, SkillsPanel)
            assert panel.region.contains_region(panel.query_one('#workbench-add').region)
            assert panel.region.contains_region(panel.query_one('#skills-toggle').region)

            # 1. Capture local folder inertly inside /skills.
            panel.query_one('#skills-capture-path', Input).value = str(ext)
            await pilot.click('#skills-capture')
            await pilot.pause()
            assert not app.backend.launches
            assert panel.current is not None and panel.current['row_kind'] == 'snapshot'
            assert panel.current['bundle_id'] == 'local-pxl'

            # 2. Promote snapshot to managed skill.
            await pilot.click('#skills-manage')
            await pilot.pause()
            assert panel.current['row_kind'] == 'managed'
            assert panel.current['skill_id'] == 'local-pxl'

            # 3. Edit and save overlay.
            await pilot.click('#skills-save')
            await pilot.pause()
            preview = panel.query_one('#workbench-preview', TextArea)
            assert preview.read_only is False
            preview.load_text('Edited overlay instructions.\n')
            await pilot.pause(.4)  # Textual's pressed animation suppresses an immediate second click.
            await pilot.click('#skills-save')
            await pilot.pause()
            assert preview.read_only is True
            assert 'Edited overlay instructions.' in preview.text

            # 4. Activate for next new conversation.
            await pilot.click('#skills-toggle')
            await pilot.pause()
            assert app.selected_managed_skills == ['local-pxl']
            assert app.selection().managed_skills == ['local-pxl']

            # 5. Cycle Diff / Bindings view.
            await pilot.click('#skills-view')
            await pilot.pause(.4)  # Textual's pressed animation suppresses an immediate second click.
            assert 'Base -> Effective' in panel.query_one('#workbench-preview', TextArea).text
            await pilot.click('#skills-view')
            await pilot.pause()
            assert 'Codex CLI' in panel.query_one('#workbench-preview', TextArea).text
            assert not app.backend.launches

    asyncio.run(scenario())


def test_parameter_type_change_rejects_incompatible_project_overrides(tmp_path):
    storage = tmp_path / 'storage'
    src = tmp_path / 'skill'
    proj = tmp_path / 'proj'
    proj.mkdir()
    _write(src, 'SKILL.md', '---\nname: Typed\n---\nLimit <limit>.\n')
    bundles.capture_bundle(src, storage_root=storage)
    managed.adopt_snapshot('typed', storage_root=storage)
    managed.set_parameter('typed', 'limit', '10', param_type='string', storage_root=storage)
    managed.set_parameter('typed', 'limit', 'unlimited', project=proj, storage_root=storage)

    # Incompatible type change must fail closed without mutating stored parameter.
    with pytest.raises(ValueError, match='Existing project override'):
        managed.set_parameter('typed', 'limit', '20', param_type='int', storage_root=storage)
    after = managed.effective_view('typed', project=proj, storage_root=storage)
    assert after['parameters']['limit']['type'] == 'string'
    assert after['parameters']['limit']['value'] == 'unlimited'

    # Compatible project override coerces cleanly on type change.
    managed.set_parameter('typed', 'limit', '15', project=proj, storage_root=storage)
    managed.set_parameter('typed', 'limit', '20', param_type='int', storage_root=storage)
    coerced = managed.effective_view('typed', project=proj, storage_root=storage)
    assert coerced['parameters']['limit']['type'] == 'int'
    assert coerced['parameters']['limit']['value'] == 15


def test_provider_continuation_inherits_parent_frozen_managed_skills(setup, tmp_path, monkeypatch):
    config, custody, root = setup
    storage = tmp_path / 'storage'
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(storage))
    src = tmp_path / 'skill-src'
    _write(src, 'SKILL.md', '---\nname: Cont Skill\n---\nParent frozen text v1.\n')
    bundles.capture_bundle(src, storage_root=storage)
    managed.adopt_snapshot('cont-skill', storage_root=storage)

    config.update(capabilities=['read', 'list'], verifiers={}, managed_skills=['cont-skill'])
    client1 = Client(steps=[('tare_deliver', {'summary': 'Parent turn.'})])
    native_agent.run_session(config, deployment(), custody, 'Start task.', client=client1, local_counter=client1)

    # Remove the managed skill after parent started; continuation MUST still inherit frozen v1.
    managed.remove_managed('cont-skill', storage_root=storage)

    from relay import native_continuity
    child_custody = tmp_path / 'child-session'
    prev = native_continuity.preview(custody, root)
    native_continuity.save_request(child_custody, prev, prev['text'], deployment())
    parent_state = json.loads((custody / 'native-agent.json').read_text(encoding='utf-8'))
    client2 = Client(steps=[('tare_deliver', {'summary': 'Continued turn.'})])
    result2 = native_agent.run_session(
        parent_state['config'], deployment(), child_custody, prev['text'],
        client=client2, local_counter=client2,
    )
    assert result2['status'] == 'PROPOSED'
    child_state = json.loads((child_custody / 'native-agent.json').read_text(encoding='utf-8'))
    assert child_state['managed_skills'] == parent_state['managed_skills']
    systems = [m['content'] for path, body in client2.calls if path == '/chat/completions'
               for m in body['messages'] if m['role'] == 'system']
    assert any('Parent frozen text v1.' in s for s in systems)


def test_tui_editing_project_overlay_writes_back_to_project_not_global(tmp_path, monkeypatch):
    storage = tmp_path / 'state'
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(storage))
    proj_a = tmp_path / 'proj-a'
    proj_b = tmp_path / 'proj-b'
    proj_a.mkdir()
    proj_b.mkdir()
    ext = tmp_path / 'ext'
    _write(ext, 'SKILL.md', '---\nname: Scoped\n---\nBase text.\n')
    bundles.capture_bundle(ext, storage_root=storage)
    managed.adopt_snapshot('scoped', storage_root=storage)
    managed.set_overlay('scoped', 'Project A v1.\n', project=proj_a, storage_root=storage)

    async def scenario():
        app = WorkConsole(proj_a, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=(130, 44)) as pilot:
            await app.workers.wait_for_complete()
            app.action_skills()
            await pilot.pause()
            panel = app.screen
            assert panel.current['row_kind'] == 'managed'
            await pilot.click('#skills-save')
            await pilot.pause()
            panel.query_one('#workbench-preview', TextArea).load_text('Project A v2.\n')
            await pilot.pause(.4)  # Textual's pressed animation suppresses an immediate second click.
            await pilot.click('#skills-save')
            await pilot.pause()
            assert 'Project A v2.' in panel.query_one('#workbench-preview', TextArea).text

    asyncio.run(scenario())
    eff_a = managed.effective_view('scoped', project=proj_a, storage_root=storage)
    eff_b = managed.effective_view('scoped', project=proj_b, storage_root=storage)
    assert eff_a['effective_text'] == 'Project A v2.\n'
    assert eff_a['text_source'] == 'project_overlay'
    assert eff_b['effective_text'] == '---\nname: Scoped\n---\nBase text.\n'
    assert eff_b['text_source'] == 'base'


def test_role_policy_defaults_double_gate_and_project_overrides(tmp_path):
    storage = tmp_path / 'storage'
    src = tmp_path / 'craft'
    proj_a = tmp_path / 'proj-a'
    proj_b = tmp_path / 'proj-b'
    proj_a.mkdir()
    proj_b.mkdir()
    _write(src, 'SKILL.md', '---\nname: Craft\n---\nCraft rule.\n')
    bundles.capture_bundle(src, storage_root=storage)
    adopted = managed.adopt_snapshot('craft', storage_root=storage)

    # Fail-closed defaults: explorer, researcher, vision, heads_up disabled; core work roles enabled.
    pol = managed.effective_role_policy(storage_root=storage)
    assert pol['explorer'] is False and pol['researcher'] is False
    assert pol['vision'] is False and pol['heads_up'] is False
    assert pol['coordinator'] is True and pol['implementer'] is True
    assert pol['planner'] is True and pol['plan-auditor'] is True and pol['auditor'] is True
    assert adopted['effective_roles'] == ['coordinator', 'implementer', 'planner', 'plan-auditor', 'auditor']

    # Restrict skill to implementer, planner, auditor (excluding coordinator).
    managed.set_skill_roles('craft', ['implementer', 'planner', 'auditor', 'explorer'], storage_root=storage)
    v_coord = managed.effective_view('craft', role='coordinator', storage_root=storage)
    assert v_coord['role_access'] == {'role': 'coordinator', 'allowed': False, 'reason': 'role_not_allowed_on_skill'}
    # Double gate: explorer is listed on the skill but still blocked by disabled role policy.
    v_expl = managed.effective_view('craft', role='explorer', storage_root=storage)
    assert v_expl['role_access'] == {'role': 'explorer', 'allowed': False, 'reason': 'role_disabled_by_policy'}
    assert v_expl['disabled_by_policy_roles'] == ['explorer']
    assert 'explorer' not in v_expl['effective_roles']
    assert managed.list_managed(role='explorer', storage_root=storage) == []
    assert [r['skill_id'] for r in managed.list_managed(role='implementer', storage_root=storage)] == ['craft']

    # Per-project role policy & skill-role override do not leak across projects.
    managed.set_role_policy('explorer', True, project=proj_a, storage_root=storage)
    assert managed.effective_view('craft', project=proj_a, role='explorer', storage_root=storage)['role_access']['allowed'] is True
    assert managed.effective_view('craft', project=proj_b, role='explorer', storage_root=storage)['role_access']['allowed'] is False

    managed.set_skill_roles('craft', ['auditor'], project=proj_a, storage_root=storage)
    managed.set_overlay('craft', 'Project A overlay.\n', project=proj_a, storage_root=storage)
    # Clearing overlay on proj_a must preserve proj_a's allowed_roles override.
    managed.set_overlay('craft', None, project=proj_a, storage_root=storage)
    va = managed.effective_view('craft', project=proj_a, storage_root=storage)
    vb = managed.effective_view('craft', project=proj_b, storage_root=storage)
    assert va['allowed_roles'] == ['auditor'] and va['roles_source'] == 'project'
    assert vb['allowed_roles'] == ['implementer', 'planner', 'auditor', 'explorer'] and vb['roles_source'] == 'global'

    with pytest.raises(ValueError, match='Unknown skill role'):
        managed.set_skill_roles('craft', ['not-a-role'], storage_root=storage)
    with pytest.raises(ValueError, match='Unknown skill role'):
        managed.set_role_policy('not-a-role', False, storage_root=storage)


def test_coordinator_freezes_per_role_pool_and_isolates_child_roles_without_live_reread(setup, tmp_path, monkeypatch):
    config, custody, root = setup
    storage = tmp_path / 'storage'
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(storage))

    for slug, title, body, roles in (
        ('coord-playbook', 'Coord Playbook', 'COORD_ONLY_DIRECTIVE_V1.\n', ['coordinator']),
        ('worker-craft', 'Worker Craft', 'WORKER_CRAFT_BODY_V1.\n', ['implementer', 'planner', 'auditor']),
        ('explorer-blocked', 'Explorer Blocked', 'EXPLORER_BLOCKED_BODY.\n', ['explorer']),
    ):
        src = tmp_path / f'src-{slug}'
        _write(src, 'SKILL.md', f'---\nname: {title}\n---\n{body}')
        bundles.capture_bundle(src, name=slug, storage_root=storage)
        managed.adopt_snapshot(slug, allowed_roles=roles, storage_root=storage)

    # Conversation runs as read-only explorer tool role with conversation_role='coordinator'.
    coord_config = {
        **config,
        'role': 'explorer',
        'writable': {},
        'verifiers': {},
        'capabilities': ['read', 'list', 'search'],
        'managed_skills': ['coord-playbook', 'worker-craft', 'explorer-blocked'],
    }
    c_coord = Client(steps=[('tare_deliver', {'summary': 'Coordinated turn.'})])
    res_coord = native_agent.run_session(
        coord_config, deployment(), custody, 'Coordinate work.',
        conversation=True, client=c_coord, local_counter=c_coord,
    )
    assert res_coord['status'] == 'PROPOSED'

    parent_state = json.loads((custody / 'native-agent.json').read_text(encoding='utf-8'))
    assert [s['skill_id'] for s in parent_state['managed_skills']] == ['coord-playbook']
    withheld_map = {w['skill_id']: w['reason'] for w in parent_state['managed_skills_withheld']}
    assert withheld_map == {
        'worker-craft': 'role_not_allowed_on_skill',
        'explorer-blocked': 'role_not_allowed_on_skill',
    }
    expl_active, expl_withheld = managed.partition_pool_for_role(parent_state['managed_skills_pool'], 'explorer')
    assert expl_active == []
    assert all(w['reason'] == 'role_disabled_by_policy' for w in expl_withheld)

    coord_systems = [m['content'] for path, body in c_coord.calls if path == '/chat/completions'
                     for m in body['messages'] if m['role'] == 'system']
    assert any('COORD_ONLY_DIRECTIVE_V1.' in s for s in coord_systems)
    assert any('worker-craft -> implementer, planner, auditor' in s for s in coord_systems)
    assert not any('WORKER_CRAFT_BODY_V1.' in s for s in coord_systems)
    assert not any('EXPLORER_BLOCKED_BODY.' in s for s in coord_systems)

    # Mutate managed.json AFTER conversation started (edit text, enable explorer, delete coord-playbook).
    managed.set_overlay('worker-craft', 'MUTATED_V2_SHOULD_NOT_APPEAR.\n', storage_root=storage)
    managed.set_role_policy('explorer', True, storage_root=storage)
    managed.remove_managed('coord-playbook', storage_root=storage)

    # Child slices MUST come strictly from parent_state['managed_skills_pool'] frozen at conversation start.
    assert managed.frozen_for_role(parent_state, 'explorer') == []
    assert managed.frozen_for_role(parent_state, 'researcher') == []
    assert managed.frozen_for_role(parent_state, 'plan-auditor') == []

    for child_role, caps, typed in (
        ('implementer', ['read', 'list'], False),
        ('planner', ['read', 'list'], False),
        ('auditor', ['read', 'list'], False),
    ):
        child_skills = managed.frozen_for_role(parent_state, child_role)
        assert [s['skill_id'] for s in child_skills] == ['worker-craft']
        child_cfg = {
            **config,
            'role': child_role,
            'writable': {} if child_role != 'implementer' else config['writable'],
            'verifiers': {},
            'capabilities': caps,
            'typed_output': typed,
        }
        c_child = Client(steps=[('tare_deliver', {'summary': f'{child_role} done.'})])
        child_custody = tmp_path / f'child-{child_role}'
        child_dep = deployment()
        if child_role == 'planner':
            child_dep = {
                **child_dep,
                'native_profile': {**child_dep['native_profile'], 'parameters_json': '{"reasoning_effort": "high"}'},
            }
        out = native_agent.run_session(
            child_cfg, child_dep, child_custody, f'Run as {child_role}.',
            delegated_leaf=True, inherited_managed_skills=child_skills,
            client=c_child, local_counter=c_child,
        )
        assert out['status'] == 'PROPOSED'
        child_sys = [m['content'] for path, body in c_child.calls if path == '/chat/completions'
                     for m in body['messages'] if m['role'] == 'system']
        assert any('WORKER_CRAFT_BODY_V1.' in s for s in child_sys)
        assert not any('COORD_ONLY_DIRECTIVE_V1.' in s for s in child_sys)
        assert not any('MUTATED_V2_SHOULD_NOT_APPEAR.' in s for s in child_sys)
        child_state = json.loads((child_custody / 'native-agent.json').read_text(encoding='utf-8'))
        # Child custody never stores the parent's full multi-role pool.
        assert 'managed_skills_pool' not in child_state

    # Fail-closed enforcement: wrong role or tampered inherited frozen skill is refused.
    coord_only_frozen = managed.frozen_for_role(parent_state, 'coordinator')
    with pytest.raises(ValueError, match='not authorized for role'):
        native_agent.run_session(
            {**config, 'role': 'implementer', 'capabilities': ['read'], 'verifiers': {}},
            deployment(), tmp_path / 'bad-role', 'Do work.',
            delegated_leaf=True, inherited_managed_skills=coord_only_frozen,
            client=Client(steps=[]), local_counter=Client(steps=[]),
        )
    tampered = [{**managed.frozen_for_role(parent_state, 'implementer')[0], 'text': 'TAMPERED'}]
    with pytest.raises(ValueError, match='effective_sha256 mismatch'):
        native_agent.run_session(
            {**config, 'role': 'implementer', 'capabilities': ['read'], 'verifiers': {}},
            deployment(), tmp_path / 'bad-hash', 'Do work.',
            delegated_leaf=True, inherited_managed_skills=tampered,
            client=Client(steps=[]), local_counter=Client(steps=[]),
        )
    with pytest.raises(ValueError, match='applies only to new sessions'):
        native_agent.run_session(
            None, None, custody, resume=True, follow_up='More.', message_id='m-bad',
            inherited_managed_skills=coord_only_frozen,
            client=Client(steps=[]), local_counter=Client(steps=[]),
        )

    # Provider continuation preserves both coordinator skills and frozen child-role pool.
    from relay import native_continuity
    cont_custody = tmp_path / 'cont-coord'
    prev = native_continuity.preview(custody, root)
    native_continuity.save_request(cont_custody, prev, prev['text'], deployment())
    c_cont = Client(steps=[('tare_deliver', {'summary': 'Continued coordinator.'})])
    native_agent.run_session(
        parent_state['config'], deployment(), cont_custody, prev['text'],
        conversation=True, client=c_cont, local_counter=c_cont,
    )
    cont_state = json.loads((cont_custody / 'native-agent.json').read_text(encoding='utf-8'))
    assert [s['skill_id'] for s in cont_state['managed_skills']] == ['coord-playbook']
    assert [s['skill_id'] for s in managed.frozen_for_role(cont_state, 'implementer')] == ['worker-craft']
    assert managed.frozen_for_role(cont_state, 'implementer')[0]['text'].endswith('WORKER_CRAFT_BODY_V1.\n')
    assert managed.frozen_for_role(cont_state, 'explorer') == []


def test_cli_roles_and_role_policy_subcommands(tmp_path, monkeypatch, capsys):
    storage = tmp_path / 'storage'
    proj = tmp_path / 'proj'
    proj.mkdir()
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(storage))
    src = tmp_path / 'pxl-role'
    _write(src, 'SKILL.md', '---\nname: PXL Role\n---\nDraw cleanly.\n')

    assert tare_ops.main(['skills', 'capture', str(src), '--json']) == 0
    capsys.readouterr()
    assert tare_ops.main(['skills', 'adopt', 'pxl-role', '--roles', 'implementer,planner,auditor', '--json']) == 0
    adopted = json.loads(capsys.readouterr().out)
    assert adopted['effective_roles'] == ['implementer', 'planner', 'auditor']

    assert tare_ops.main(['skills', 'managed', '--role', 'coordinator', '--json']) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert tare_ops.main(['skills', 'managed', '--role', 'auditor', '--json']) == 0
    assert [r['skill_id'] for r in json.loads(capsys.readouterr().out)] == ['pxl-role']

    assert tare_ops.main(['skills', 'effective', 'pxl-role', '--role', 'explorer', '--json']) == 0
    eff = json.loads(capsys.readouterr().out)
    assert eff['role_access'] == {'role': 'explorer', 'allowed': False, 'reason': 'role_disabled_by_policy'}

    assert tare_ops.main(['skills', 'roles', 'pxl-role', 'coordinator', '--project', str(proj), '--json']) == 0
    proj_view = json.loads(capsys.readouterr().out)
    assert proj_view['effective_roles'] == ['coordinator'] and proj_view['roles_source'] == 'project'

    assert tare_ops.main(['skills', 'role-policy', 'researcher', '--disable', '--json']) == 0
    pol = json.loads(capsys.readouterr().out)
    assert pol['researcher'] == {'enabled': False, 'source': 'global'}

    assert tare_ops.main(['skills', 'roles', 'pxl-role', '--none', '--json']) == 0
    assert json.loads(capsys.readouterr().out)['effective_roles'] == []

    assert tare_ops.main(['skills', 'roles', 'pxl-role', '--reset', '--json']) == 0
    assert json.loads(capsys.readouterr().out)['effective_roles'] == list(managed.DEFAULT_ENABLED_ROLES)


@pytest.mark.parametrize('size', [(80, 24), (130, 44)])
def test_tui_roles_dialog_configures_skill_roles_and_role_policy(tmp_path, monkeypatch, size):
    storage = tmp_path / 'state'
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(storage))
    root = tmp_path / 'project'
    root.mkdir()
    src = tmp_path / 'pxl-tui'
    _write(src, 'SKILL.md', '---\nname: PXL TUI\n---\nInstructions.\n')
    bundles.capture_bundle(src, storage_root=storage)
    managed.adopt_snapshot('pxl-tui', storage_root=storage)

    async def scenario():
        app = WorkConsole(root, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            app.action_skills()
            await pilot.pause()
            panel = app.screen
            assert isinstance(panel, SkillsPanel)
            assert panel.region.contains_region(panel.query_one('#skills-roles').region)
            assert panel.region.contains_region(panel.query_one('#skills-view').region)

            # Open Roles dialog on the highlighted managed skill.
            await pilot.click('#skills-roles')
            await pilot.pause()
            dialog = app.screen
            assert isinstance(dialog, SkillRolesDialog)
            assert dialog.region.contains_region(dialog.query_one('#roles-toggle').region)
            assert dialog.region.contains_region(dialog.query_one('#roles-reset').region)

            # Index 0 is coordinator: toggle it OFF for pxl-tui.
            await pilot.click('#roles-toggle')
            await pilot.pause()
            view = managed.effective_view('pxl-tui', storage_root=storage)
            assert 'coordinator' not in view['allowed_roles']

            # Switch to Role policy mode and verify explorer starts OFF, then toggle explorer ON and reset back OFF.
            await pilot.click('#roles-mode')
            await pilot.pause()
            roles_list = dialog.query_one('#roles-list', OptionList)
            expl_idx = managed.SKILL_ROLES.index('explorer')
            roles_list.highlighted = expl_idx
            await pilot.pause(.4)  # Textual's pressed animation suppresses an immediate second click.
            await pilot.click('#roles-toggle')
            await pilot.pause()
            assert managed.effective_role_policy(storage_root=storage)['explorer'] is True
            await pilot.click('#roles-reset')
            await pilot.pause()
            assert managed.effective_role_policy(storage_root=storage)['explorer'] is False

            # Close dialog and verify SkillsPanel reflects updated role tags.
            await pilot.click('#roles-close')
            await pilot.pause()
            assert isinstance(app.screen, SkillsPanel)
            assert 'impl,plan,p-aud,aud' in app.screen.current['label']

    asyncio.run(scenario())
