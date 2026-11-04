"""Permissions defaults at actual console startup, switching, and execution."""
import asyncio
from dataclasses import asdict
import pytest

from textual.widgets import Select, TextArea

from relay import native_agent, relay_mesh
from relay.native_console import WorkConsole
from relay.permissions_panel import PermissionsPanel
from relay.project_permissions import preference_path, read_defaults, save_defaults
from test_native_console import FakeBackend
from relay.console_backend import Selection, command


@pytest.mark.parametrize('role', ['explorer', 'auditor'])
def test_new_terminal_session_rejects_read_only_role_before_allocating_session(tmp_path, role):
    selected=Selection(str(tmp_path),role=role,prompt='Inspect with commands',terminal=True,write_paths=[])
    with pytest.raises(ValueError,match='System commands require.*project permissions'):
        command(selected,assistant=True)
    assert not selected.session


def test_terminal_control_tracks_read_only_permissions_and_role_changes(tmp_path, monkeypatch):
    from textual.widgets import Checkbox
    state,control=tmp_path/'state',tmp_path/'control'
    project=tmp_path/'project';project.mkdir()
    monkeypatch.setenv('TARE_STORAGE_ROOT',str(state))
    monkeypatch.setattr(relay_mesh,'RELAY_ROOT',control)
    save_defaults(state/'console',project,control,{'mode':'read','write_paths':[]},'missing')
    async def scenario():
        backend=FakeBackend();app=WorkConsole(project,backend=backend,initial_mode='assistant')
        async with app.run_test(size=(130,44)) as pilot:
            await app.workers.wait_for_complete()
            terminal=app.query_one('#terminal',Checkbox)
            assert terminal.disabled and not terminal.value
            assert 'project permissions' in str(terminal.tooltip)
            app.set_pending_permissions({'mode':'commands','write_paths':['reports']},str(project))
            assert not terminal.disabled and terminal.value
            assert app.selection().write_paths==['reports']
            app.query_one('#session-role',Select).value='explorer'
            await pilot.pause()
            assert terminal.disabled and not terminal.value
            assert not app.assistant_session and not backend.launches
            app.query_one('#session-role',Select).value='implementer'
            await pilot.pause()
            assert not terminal.disabled and not terminal.value
            assert app.selection().write_paths==['reports']
    asyncio.run(scenario())


def test_role_change_updates_terminal_before_queued_select_messages(tmp_path, monkeypatch):
    from textual.widgets import Checkbox
    monkeypatch.setattr(relay_mesh, 'RELAY_ROOT', tmp_path/'control')

    async def scenario():
        backend = FakeBackend()
        app = WorkConsole(tmp_path, backend=backend, initial_mode='assistant')
        async with app.run_test(size=(130, 44)) as pilot:
            await app.workers.wait_for_complete()
            app.set_pending_permissions({'mode':'commands','write_paths':['reports']}, str(tmp_path))
            terminal = app.query_one('#terminal', Checkbox)
            role = app.query_one('#session-role', Select)
            assert terminal.value and not terminal.disabled
            # Changed messages may be deferred or suppressed while applying a
            # selection. Controls must agree with the actual role immediately.
            with app.prevent(Select.Changed):
                role.value = 'explorer'
                assert terminal.disabled and not terminal.value
                assert not app.selection().terminal
                app.controls(True)
                app.controls(False)
                assert terminal.disabled and not terminal.value
                role.value = 'implementer'
                assert not terminal.disabled and not terminal.value
                assert app.selection().write_paths == ['reports']
            assert not app.assistant_session and not backend.launches
    asyncio.run(scenario())


def test_project_defaults_panel_switch_draft_and_readonly_assistant(tmp_path, monkeypatch):
    one, two = tmp_path/'one', tmp_path/'two'
    one.mkdir(); two.mkdir()
    state, control = tmp_path/'state', tmp_path/'control'
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(state))
    monkeypatch.setattr(relay_mesh, 'RELAY_ROOT', control)
    monkeypatch.setattr(native_agent, 'session_root', lambda: state/'sessions')
    save_defaults(state/'console', one, control, {'mode': 'read', 'write_paths': []}, 'missing')
    save_defaults(state/'console', two, control, {'mode': 'edit', 'write_paths': ['src']}, 'missing')
    async def scenario():
        backend = FakeBackend()
        app = WorkConsole(one, backend=backend, initial_mode='assistant')
        async with app.run_test(size=(80,24)) as pilot:
            await app.workers.wait_for_complete()
            assert app.selection().role == 'explorer' and not app.selection().terminal
            assert app.selection().write_paths == []
            app.query_one('#prompt', TextArea).load_text('/permissions')
            app.execute(False)
            await pilot.pause(); await app.screen.workers.wait_for_complete()
            panel = app.screen
            assert isinstance(panel, PermissionsPanel)
            panel.query_one('#permissions-mode', Select).value = 'edit'
            panel.query_one('#permissions-paths', TextArea).load_text('docs')
            await pilot.pause()
            panel.action_save(); await panel.workers.wait_for_complete()
            panel.action_close(); await pilot.pause()
            assert app.selection().role == 'implementer'
            assert app.selection().write_paths == ['docs'] and not backend.launches
            snapshot = asdict(app.selection())
            _, revision = read_defaults(state/'console', one, control)
            save_defaults(state/'console', one, control, {'mode': 'read', 'write_paths': []}, revision)
            app.new_demand()
            assert app.selection().role == 'explorer'
            app.restore_selection(snapshot)
            assert app.selection().write_paths == ['docs']
            assert app.selection().role == 'implementer'
            assert app.open_project(str(two), app.project_navigation_scope())
            await app.workers.wait_for_complete(); await pilot.pause()
            assert app.selection().write_paths == ['src']
            assert app.open_project(str(one), app.project_navigation_scope())
            await app.workers.wait_for_complete(); await pilot.pause()
            assert app.selection().role == 'explorer'
            app.query_one('#prompt', TextArea).load_text('Inspect this project')
            app.execute(False)
            for _ in range(40):
                if backend.launches: break
                await pilot.pause(.05)
            argv = backend.launches[0]
            assert argv[argv.index('--role')+1] == 'explorer'
            assert '--write-path' not in argv and '--terminal' not in argv
            app.action_cancel()
            await app.workers.wait_for_complete()
    asyncio.run(scenario())


def test_concurrent_save_does_not_change_pending_draft_observation(tmp_path, monkeypatch):
    from relay import console_permissions
    from relay.console_backend import Selection
    project=tmp_path/'project'; project.mkdir()
    state=tmp_path/'state'; control=tmp_path/'control'
    monkeypatch.setattr(relay_mesh,'RELAY_ROOT',control)
    class Host(console_permissions.ProjectPermissions):
        screen=object()
        workspace_root=str(project)
        assistant_session=None
        def navigation_matches_workspace(self): return True
        def selection(self): return Selection(str(project),mode='assistant',write_paths=['src'])
        def project_navigation_scope(self): return ('fixed-scope',)
        def permissions_directory(self,root): return state
        def push_screen(self,panel,closed): self.panel=panel
    host=Host();host.action_permissions()
    first=host.panel.reader()
    real_save=console_permissions.save_defaults
    def racing_save(*args):
        saved,revision=real_save(*args)
        real_save(state,project,control,{'mode':'commands','write_paths':['docs']},revision)
        return saved,revision
    monkeypatch.setattr(console_permissions,'save_defaults',racing_save)
    with pytest.raises(ValueError,match='changed elsewhere'):
        host.panel.saver({'mode':'read','write_paths':[]},first['revision'])
    refreshed=host.panel.reader()
    assert refreshed['defaults']=={'mode':'commands','write_paths':['docs']}
    assert refreshed['effective_text']==first['effective_text']


def test_corrupt_defaults_block_new_session_without_overwriting_file(tmp_path, monkeypatch):
    project = tmp_path/'project'; project.mkdir()
    state, control = tmp_path/'state', tmp_path/'control'
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(state))
    monkeypatch.setattr(relay_mesh, 'RELAY_ROOT', control)
    path, _, _ = preference_path(state/'console', project, control)
    path.parent.mkdir(parents=True); path.write_text('{corrupt')
    async def scenario():
        backend = FakeBackend()
        app = WorkConsole(project, backend=backend, initial_mode='assistant')
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            app.query_one('#prompt', TextArea).load_text('Edit a file')
            app.execute(False); await pilot.pause()
            assert not backend.launches and app.permission_profile_error
            assert not app.assistant_session
            assert path.read_text() == '{corrupt'
            app.restore_selection(asdict(app.selection()))
            assert app.permission_profile_error is None
            app.execute(False); await pilot.pause()
            assert not backend.launches and not app.assistant_session
            assert path.read_text() == '{corrupt'
    asyncio.run(scenario())
