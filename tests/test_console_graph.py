"""Project graph selection through the real TUI, including saved draft scope."""
import asyncio
from dataclasses import asdict

from textual.widgets import Input, TextArea

from relay import native_agent, relay_mesh
from relay.native_console import WorkConsole
from relay.project_graph import preference_path, read_defaults, save_defaults
from relay.project_graph_panel import ProjectGraphPanel
from test_native_console import FakeBackend
from test_work_console import graph_root


def test_backlog_panel_defaults_project_switch_draft_and_new_session(graph_root, tmp_path, monkeypatch):
    one, two = tmp_path/'one', tmp_path/'two'
    one.mkdir(); two.mkdir()
    first_graph = graph_root/'work-graph.json'
    second_graph = graph_root/'second.json'; second_graph.write_bytes(first_graph.read_bytes())
    state, control = tmp_path/'state', tmp_path/'control'
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(state))
    monkeypatch.setattr(relay_mesh, 'RELAY_ROOT', control)
    monkeypatch.setattr(native_agent, 'session_root', lambda: state/'sessions')
    save_defaults(state/'console', two, control, str(second_graph), 'missing')
    async def scenario():
        backend = FakeBackend()
        app = WorkConsole(one, backend=backend, initial_mode='assistant')
        async with app.run_test(size=(80,24)) as pilot:
            await app.workers.wait_for_complete()
            assert app.selection().graph_path == ''
            app.query_one('#prompt', TextArea).load_text('/backlog')
            app.execute(False)
            await pilot.pause(); await app.screen.workers.wait_for_complete()
            panel = app.screen
            assert isinstance(panel, ProjectGraphPanel)
            panel.query_one('#project-graph-path', Input).value = str(first_graph)
            panel.action_preview(); await panel.workers.wait_for_complete()
            assert read_defaults(state/'console', one, control) == ('', 'missing')
            panel.action_save(); await panel.workers.wait_for_complete()
            panel.action_close(); await pilot.pause()
            assert app.selection().graph_path == str(first_graph) and not backend.launches
            snapshot = asdict(app.selection())
            _, revision = read_defaults(state/'console', one, control)
            save_defaults(state/'console', one, control, '', revision)
            app.new_demand()
            assert app.selection().graph_path == ''
            app.restore_selection(snapshot)
            assert app.selection().graph_path == str(first_graph)
            assert app.open_project(str(two), app.project_navigation_scope())
            await app.workers.wait_for_complete(); await pilot.pause()
            assert app.selection().graph_path == str(second_graph)
            assert app.open_project(str(one), app.project_navigation_scope())
            await app.workers.wait_for_complete(); await pilot.pause()
            assert app.selection().graph_path == ''
    asyncio.run(scenario())


def test_corrupt_graph_preferences_block_new_session_and_preserve_file(tmp_path, monkeypatch):
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
            app.query_one('#prompt', TextArea).load_text('Inspect the project')
            app.execute(False); await pilot.pause()
            assert not backend.launches and app.graph_profile_error
            assert not app.assistant_session and path.read_text() == '{corrupt'
            app.restore_selection(asdict(app.selection()))
            app.execute(False); await pilot.pause()
            assert not backend.launches and not app.assistant_session
            assert path.read_text() == '{corrupt'
    asyncio.run(scenario())
