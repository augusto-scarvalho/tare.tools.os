"""Offline project-admission UI boundaries; no provider or operator storage."""
import asyncio
from dataclasses import asdict
import json
from types import SimpleNamespace

import pytest
from textual.app import App
from textual.widgets import Button, Checkbox, Input, Select, Static, TextArea

from relay import project_admission as admission
from relay.console_backend import Selection, command
from relay.console_resources import discover_skills
from relay.native_console import WorkConsole
from relay.permissions_panel import PermissionsPanel
from relay.project_permissions import permissions_view, save_defaults
from test_native_console import FakeBackend
from test_permissions_panel import widget_plain


@pytest.mark.parametrize('policy', [dict(admission.QUARANTINE),
    {'mode': 'adopted', 'root_sha256': 'a' * 64, 'nested': False}, None])
def test_admitted_sessions_do_not_spawn_the_legacy_background_auditor(tmp_path, monkeypatch, policy):
    from relay.console_models import ConversationModels
    from relay import turn_engine
    source = tmp_path/'session'
    source.mkdir()
    config = {} if policy is None else {'project_admission': policy}
    (source/'native-agent.json').write_text(json.dumps({'config': config, 'deployment': {'id': 'fixture'}}))
    calls = []
    monkeypatch.setattr(turn_engine, 'audit_turn', lambda *a, **kw: calls.append(a) or (None, None))
    host = SimpleNamespace(call_from_thread=lambda callback, *args: callback(*args),
                           notify=lambda *a, **kw: pytest.fail('Unexpected error notification'))
    ConversationModels._audit_changes(host, str(source), [{'path': 'docs/result.txt'}],
                                      str(tmp_path), 'Write the selected snapshot')
    assert len(calls) == (1 if policy is None else 0)


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    storage = tmp_path/'storage'
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(storage))
    monkeypatch.setattr(WorkConsole, 'permissions_directory', lambda self, project: storage/'preferences')
    monkeypatch.setattr('relay.native_agent.session_root', lambda: storage/'sessions')
    root = tmp_path/'project'
    root.mkdir()
    (root/'AGENTS.md').write_text('Reviewed root rules', encoding='utf-8')
    return root, storage/'preferences', tmp_path/'control'


def test_panel_stale_preview_refresh_and_separate_nested(isolated):
    root, directory, control = isolated
    def reader():
        return permissions_view(directory, root, control)
    def saver(defaults, revision):
        save_defaults(directory, root, control, defaults, revision)
        return reader()
    async def scenario():
        app = App()
        panel = PermissionsPanel(root, reader=reader, saver=saver)
        async with app.run_test(size=(130, 44)) as pilot:
            app.push_screen(panel)
            await pilot.pause()
            await panel.workers.wait_for_complete()
            await pilot.pause()
            reviewed = panel.reviewed_preview
            text = widget_plain(panel.query_one('#permissions-instructions-text', Static))
            assert str(root.resolve()) in text
            assert str(root/'AGENTS.md') in text
            assert reviewed['root_sha256'] in text
            assert reviewed['text'] in text
            root_choice = panel.query_one('#permissions-root', Checkbox)
            nested = panel.query_one('#permissions-nested', Checkbox)
            assert not root_choice.value and not nested.value and nested.disabled
            root_choice.value = True
            await pilot.pause()
            assert not nested.value and not nested.disabled
            (root/'AGENTS.md').write_text('Changed root rules', encoding='utf-8')
            panel.action_save()
            await pilot.pause()
            assert 'preview changed' in widget_plain(panel.query_one('#permissions-status', Static))
            assert panel.reviewed_preview == reviewed
            assert reader()['revision'] == 'missing'
            panel.action_refresh()
            await panel.workers.wait_for_complete()
            await pilot.pause()
            assert panel.reviewed_preview['text'] == 'Changed root rules'
            root_choice.value = True
            await pilot.pause()
            panel.action_save()
            await panel.workers.wait_for_complete()
            await pilot.pause()
            policy = reader()['defaults']['project_admission']
            assert policy['mode'] == 'adopted' and policy['nested'] is False
            nested.value = True
            await pilot.pause()
            panel.action_save()
            await panel.workers.wait_for_complete()
            await pilot.pause()
            assert reader()['defaults']['project_admission']['nested'] is True
    asyncio.run(scenario())


def test_invalid_preview_can_save_quarantine_without_revoking_edit(isolated):
    root, directory, control = isolated
    policy = admission.configure(root, admission.preview(root), nested=True)
    save_defaults(directory, root, control,
                  {'mode': 'edit', 'write_paths': ['src'], 'project_admission': policy}, 'missing')
    (root/'AGENTS.md').write_bytes(b'\xff')
    def reader():
        return permissions_view(directory, root, control)
    def saver(defaults, revision):
        save_defaults(directory, root, control, defaults, revision)
        return reader()
    async def scenario():
        app = App()
        panel = PermissionsPanel(root, reader=reader, saver=saver)
        async with app.run_test(size=(130, 44)) as pilot:
            app.push_screen(panel)
            await pilot.pause()
            await panel.workers.wait_for_complete()
            await pilot.pause()
            assert panel.reviewed_preview is None
            assert panel.query_one('#permissions-root', Checkbox).disabled
            assert not panel.query_one('#permissions-root', Checkbox).value
            assert not panel.query_one('#permissions-nested', Checkbox).value
            assert not panel.query_one('#permissions-save', Button).disabled
            panel.action_save()
            await panel.workers.wait_for_complete()
            await pilot.pause()
            saved = reader()['defaults']
            assert saved['project_admission'] == admission.QUARANTINE
            assert saved['mode'] == 'edit' and saved['write_paths'] == ['src']
    asyncio.run(scenario())


def test_new_transport_resume_and_wrong_project(isolated):
    root, _, _ = isolated
    policy = admission.configure(root, admission.preview(root), nested=True)
    selected = Selection(str(root), deployment='fixture', prompt='Inspect', project_admission=policy)
    args = command(selected)
    assert args[args.index('--project-admission')+1] == 'adopted'
    assert args[args.index('--root-instruction-pin')+1] == policy['root_sha256']
    assert '--nested-instructions' in args
    from pathlib import Path
    session = Path(selected.session)
    session.mkdir(parents=True)
    (session/'native-agent.json').write_text(json.dumps({'config': {
        'repositories': {'repo': str(root)}, 'project_admission': policy}}), encoding='utf-8')
    before = (session/'native-agent.json').read_bytes()
    selected.project_admission = dict(admission.QUARANTINE)
    resumed = command(selected, True)
    assert '--project-admission' not in resumed and '--nested-instructions' not in resumed
    assert (session/'native-agent.json').read_bytes() == before
    other = root.parent/'other'
    other.mkdir()
    selected.root = str(other)
    with pytest.raises(ValueError, match='another project'):
        command(selected, True)


def test_workconsole_pending_draft_and_project_switch(isolated):
    root, _, _ = isolated
    other = root.parent/'other'
    other.mkdir()
    policy = admission.configure(root, admission.preview(root), nested=True)
    async def scenario():
        backend = FakeBackend()
        app = WorkConsole(root, backend=backend, initial_mode='assistant')
        async with app.run_test(size=(130, 44)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.selection().project_admission == admission.QUARANTINE
            app.set_pending_permissions({'mode': 'edit', 'write_paths': ['src'],
                                         'project_admission': policy}, root)
            snapshot = asdict(app.selection())
            app.project_admission = dict(admission.QUARANTINE)
            app.restore_selection(snapshot)
            await pilot.pause()
            assert app.selection().project_admission == policy
            assert app.selection().write_paths == ['src']
            app.query_one('#root', Input).value = str(other)
            app.prepare_workspace()
            await pilot.pause()
            assert app.selection().project_admission == admission.QUARANTINE
            assert app.selection().write_paths == []
            assert backend.launches == []
    asyncio.run(scenario())


def test_skill_snapshot_mutation_and_atomic_rejections(isolated, monkeypatch):
    root, _, _ = isolated
    skill = root/'skills'/'fixture'/'SKILL.md'
    skill.parent.mkdir(parents=True)
    skill.write_text('Exact café snapshot\r\n', encoding='utf-8')
    row = discover_skills(root)[0]
    checkpoints = []
    monkeypatch.setattr(WorkConsole, 'checkpoint', lambda self, *a, **k: checkpoints.append(a))
    async def scenario():
        backend = FakeBackend()
        app = WorkConsole(root, backend=backend, initial_mode='assistant')
        async with app.run_test(size=(130, 44)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            prompt = app.query_one('#prompt', TextArea)
            prompt.load_text('Keep existing draft')
            scope = app.workbench_scope()
            app.add_workbench_draft(scope, row, '', '', skill=True)
            captured = prompt.text
            payload = json.loads(captured[captured.index('{'):])
            assert payload['path'] == row['path']
            assert payload['sha256'] == row['sha256']
            assert payload['size_bytes'] == row['size_bytes']
            assert payload['text'].encode('utf-8') == skill.read_bytes()
            skill.write_text('Changed after Add', encoding='utf-8')
            assert prompt.text == captured
            count = len(checkpoints)
            with pytest.raises(ValueError, match='changed'):
                app.add_workbench_draft(scope, row, '', '', skill=True)
            assert prompt.text == captured and len(checkpoints) == count
            current = discover_skills(root)[0]
            prompt.load_text('é'*8192)
            before = prompt.text
            with pytest.raises(ValueError, match='16 KiB'):
                app.add_workbench_draft(scope, current, '', '', skill=True)
            assert prompt.text == before and len(checkpoints) == count
            prompt.load_text('Original draft')
            def fail(*a, **k):
                raise OSError('fixture storage failure')
            monkeypatch.setattr(app, 'checkpoint', fail)
            with pytest.raises(ValueError, match='could not be saved'):
                app.add_workbench_draft(scope, current, '', '', skill=True)
            assert prompt.text == 'Original draft'
            assert backend.launches == []
    asyncio.run(scenario())


def test_reopen_keeps_reviewed_skill_and_pending_consent_when_defaults_change(isolated, monkeypatch):
    from relay import console_demand, relay_mesh
    from relay.project_permissions import read_defaults
    root, directory, control = isolated
    monkeypatch.setattr(relay_mesh, 'RELAY_ROOT', control)
    skill = root/'skills'/'demo'/'SKILL.md'
    skill.parent.mkdir(parents=True)
    original = 'Use the reviewed version, not a later replacement.\n'
    skill.write_bytes(original.encode('utf-8'))
    policy = admission.configure(root, admission.preview(root))
    save_defaults(directory, root, control,
                  {'mode': 'edit', 'write_paths': ['docs'], 'project_admission': policy}, 'missing')

    async def scenario():
        backend = FakeBackend()
        app = WorkConsole(root, backend=backend, initial_mode='assistant')
        async with app.run_test(size=(130, 44)) as pilot:
            await app.workers.wait_for_complete()
            app.action_skills()
            await pilot.pause()
            await pilot.click('#workbench-add')
            await pilot.pause()
            identity, draft = app.demand_id, app.query_one('#prompt', TextArea).text
            saved = console_demand.read(identity, root, control)['snapshot']['selection']
            assert saved['project_admission'] == policy
            assert saved['write_paths'] == ['docs'] and saved['prompt'] == draft
            payload = json.loads(draft[draft.index('{'):])
            assert payload['text'] == original
        skill.write_text('Unreviewed replacement', encoding='utf-8')
        _, revision = read_defaults(directory, root, control)
        save_defaults(directory, root, control,
                      {'mode': 'read', 'write_paths': [], 'project_admission': dict(admission.QUARANTINE)}, revision)
        reopened = WorkConsole(root, backend=backend, initial_mode='assistant')
        async with reopened.run_test(size=(130, 44)) as pilot:
            await reopened.workers.wait_for_complete()
            assert reopened.selection().project_admission == admission.QUARANTINE
            await reopened.list_demands().wait()
            reopened.query_one('#saved-demand', Select).value = identity
            reopened.open_demand()
            await reopened.workers.wait_for_complete()
            await pilot.pause()
            assert reopened.selection().project_admission == policy
            assert reopened.selection().role == 'implementer'
            assert reopened.selection().write_paths == ['docs']
            assert reopened.query_one('#prompt', TextArea).text == draft
            assert not backend.launches
    asyncio.run(scenario())


@pytest.mark.parametrize('leaf_instructions', [False, True])
def test_child_cannot_expand_quarantined_parent(isolated, monkeypatch, leaf_instructions):
    from relay.delegation_tools import ExplorationTools
    root, _, custody = isolated
    tool = object.__new__(ExplorationTools)
    tool.state = {'deadline_utc': None, 'config': {'project_admission': dict(admission.QUARANTINE)}}
    tool.settings = {'seconds': 30}
    tool.route_class = None
    captured = []
    monkeypatch.setattr('relay.delegation.experiments',
                        lambda *a: {'leaf_project_instructions': leaf_instructions})
    def run(config, *a, **k):
        captured.append(config)
        return {}
    monkeypatch.setattr('relay.native_agent.run_session', run)
    monkeypatch.setattr(tool, 'finish', lambda *a: None)
    monkeypatch.setattr('relay.delegation_tools.explorer_instruction', lambda *a: 'Inspect data')
    # reserve() creates the custody folder and identifies the selected profile
    # before investigate(); keep those real dispatch preconditions in this fixture.
    folder = custody/'child'
    folder.mkdir(parents=True)
    deployment = {'id': 'explorer-fixture', 'native_profile': {'protocol': 'vendor-cli'}}
    row = {'calls_reserved': 2, 'repository': 'repo', 'repository_root': str(root), 'source_path': '.'}
    tool.investigate(folder, row, deployment,
                     {'identity': None}, {}, SimpleNamespace(should_stop=lambda: False))
    assert len(captured) == 1
    config = captured[0]
    assert config['project_admission'] == admission.QUARANTINE
    assert config['role'] == 'explorer'
    assert config['writable'] == {} and config['verifiers'] == {}
    assert config['capabilities'] == ['read', 'list', 'search']
    assert (folder/'receipt.json').is_file() and not (root/'child').exists()
