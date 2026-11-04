"""Tests for ProjectGraphPanel modal support panel."""
import asyncio
from pathlib import Path
import threading
import time

import pytest
from rich.text import Text
from textual.app import App
from textual.theme import Theme
from textual.widgets import Button, Input, Static
from textual.containers import VerticalScroll

from relay.project_graph_panel import ProjectGraphPanel


def register_tare_themes(app: App):
    """Register tare themes for geometry and theme testing."""
    for name, primary, foreground, background, panel, dark in (
        ('tare-dark', '#8bd5c4', '#e2e8ed', '#14171b', '#1c2229', True),
        ('tare-light', '#176b60', '#25313b', '#faf9f6', '#eeefec', False),
        ('tare-contrast', '#97d9ff', '#ffffff', '#000000', '#101010', True),
    ):
        app.register_theme(
            Theme(
                name=name,
                primary=primary,
                secondary=primary,
                accent=primary,
                foreground=foreground,
                background=background,
                surface=background,
                panel=panel,
                dark=dark,
                warning='#d5b778' if dark else '#805e13',
                error='#e89999' if dark else '#a32d35',
                success='#a6c598' if dark else '#34652f',
                variables={
                    'text-muted': foreground if name == 'tare-contrast' else '#9daab6' if dark else '#596671'
                },
            )
        )


def widget_plain(widget) -> str:
    """Extract plain text representation from a widget."""
    renderable = getattr(widget, 'content', None)
    if renderable is None:
        renderable = widget.render()
    if isinstance(renderable, Text):
        return renderable.plain
    return str(renderable)


def test_load_initializes_view_and_enables_actions(tmp_path):
    """Verify reader synchronously loads path, effective_path, summary, revision, enabling actions."""
    project_dir = tmp_path / 'my_project'
    project_dir.mkdir()
    graph_file = project_dir / 'work-graph.json'

    initial_view = {
        'revision': 'rev-init-1',
        'path': str(graph_file),
        'effective_path': str(graph_file),
        'summary': 'Tasks: 12 total, 3 ready\nFrontier: #101, #102',
    }

    def reader():
        return dict(initial_view)

    async def scenario():
        app = App()
        panel = ProjectGraphPanel(project_dir, reader=reader)
        dismissed = []

        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(panel, callback=dismissed.append)
            await pilot.pause()
            await panel.workers.wait_for_complete()
            await pilot.pause()

            # Path input matches loaded path
            path_input = panel.query_one('#project-graph-path', Input)
            assert path_input.value == str(graph_file)
            assert not path_input.disabled

            # Effective path matches loaded effective_path
            eff_widget = panel.query_one('#project-graph-effective', Static)
            assert eff_widget.tooltip.plain == str(graph_file)
            assert widget_plain(eff_widget).endswith('…')
            assert len(widget_plain(eff_widget)) <= eff_widget.content_size.width

            # Summary matches loaded summary
            summary_widget = panel.query_one('#project-graph-summary', Static)
            summary_text = widget_plain(summary_widget)
            assert 'Tasks: 12 total, 3 ready' in summary_text
            assert 'Frontier: #101, #102' in summary_text

            summary_label = panel.query_one('#project-graph-summary-label', Static)
            assert widget_plain(summary_label) == 'Graph summary'

            # Revision and authority
            assert panel.revision == 'rev-init-1'

            # Tooltip on title matches full project path
            title_widget = panel.query_one('#project-graph-title', Static)
            assert str(project_dir.resolve()) in title_widget.tooltip

            # Status shows loaded
            status_widget = panel.query_one('#project-graph-status', Static)
            assert 'Project binding loaded.' in widget_plain(status_widget)

            # Buttons are enabled
            assert not panel.query_one('#project-graph-save', Button).disabled
            assert not panel.query_one('#project-graph-preview', Button).disabled
            assert not panel.query_one('#project-graph-refresh', Button).disabled
            assert not panel.query_one('#project-graph-back', Button).disabled

            # Close without saving dismisses None
            panel.action_close()
            await pilot.pause()
            assert dismissed == [None]

    asyncio.run(scenario())


def test_preview_does_not_save_or_mutate_effective(tmp_path):
    """Verify preview invokes inspector with typed path, labels summary, and leaves effective path & saver untouched."""
    project_dir = tmp_path / 'proj_preview'
    project_dir.mkdir()
    old_graph = str(project_dir / 'old.json')
    new_graph = str(project_dir / 'new.json')

    initial_view = {
        'revision': 'rev-v1',
        'path': old_graph,
        'effective_path': old_graph,
        'summary': 'Old graph summary',
    }

    saver_calls = []
    inspector_calls = []

    def reader():
        return dict(initial_view)

    def saver(path, expected_rev):
        saver_calls.append((path, expected_rev))
        return dict(initial_view)

    def inspector(path):
        inspector_calls.append(path)
        return f"Inspected graph at {path}: 4 tasks."

    async def scenario():
        app = App()
        panel = ProjectGraphPanel(project_dir, reader=reader, saver=saver, inspector=inspector)
        dismissed = []

        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(panel, callback=dismissed.append)
            await pilot.pause()
            await panel.workers.wait_for_complete()
            await pilot.pause()

            # User edits path input
            path_input = panel.query_one('#project-graph-path', Input)
            path_input.value = new_graph
            await pilot.pause()

            # User clicks Preview
            preview_btn = panel.query_one('#project-graph-preview', Button)
            preview_btn.press()
            await pilot.pause()
            await panel.workers.wait_for_complete()
            await pilot.pause()

            # Inspector called with typed path
            assert inspector_calls == [new_graph]

            # Saver was NOT called
            assert len(saver_calls) == 0

            # Revision unchanged
            assert panel.revision == 'rev-v1'

            # Summary label clearly labels itself as preview of typed path
            summary_label = panel.query_one('#project-graph-summary-label', Static)
            assert f"Preview: {new_graph}" in widget_plain(summary_label)

            # Summary content shows inspector output
            summary_widget = panel.query_one('#project-graph-summary', Static)
            assert f"Inspected graph at {new_graph}: 4 tasks." in widget_plain(summary_widget)

            # Effective path remains untouched (does not replace displayed effective binding)
            eff_widget = panel.query_one('#project-graph-effective', Static)
            assert eff_widget.tooltip.plain == old_graph
            assert eff_widget.tooltip.plain != new_graph

            # Status shows preview ready
            status_widget = panel.query_one('#project-graph-status', Static)
            assert 'Preview ready (not saved).' in widget_plain(status_widget)

            # Closing without save dismisses None
            panel.action_close()
            await pilot.pause()
            assert dismissed == [None]

    asyncio.run(scenario())


def test_preview_empty_path_shows_no_graph_preview(tmp_path):
    """Verify preview on empty input invokes inspector with empty string and labels preview."""
    project_dir = tmp_path / 'proj_empty_preview'
    project_dir.mkdir()

    initial_view = {
        'revision': 'rev-v1',
        'path': '/some/path.json',
        'effective_path': '/some/path.json',
        'summary': 'Active graph',
    }

    inspector_calls = []

    def reader():
        return dict(initial_view)

    def inspector(path):
        inspector_calls.append(path)
        return "No project graph configured."

    async def scenario():
        app = App()
        panel = ProjectGraphPanel(project_dir, reader=reader, inspector=inspector)
        dismissed = []

        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(panel, callback=dismissed.append)
            await pilot.pause()
            await panel.workers.wait_for_complete()
            await pilot.pause()

            path_input = panel.query_one('#project-graph-path', Input)
            path_input.value = ''
            await pilot.pause()

            panel.action_preview()
            await pilot.pause()
            await panel.workers.wait_for_complete()
            await pilot.pause()

            assert inspector_calls == ['']
            summary_label = panel.query_one('#project-graph-summary-label', Static)
            assert 'Preview: (no graph)' in widget_plain(summary_label)

            summary_widget = panel.query_one('#project-graph-summary', Static)
            assert 'No project graph configured.' in widget_plain(summary_widget)

            panel.action_close()
            await pilot.pause()
            assert dismissed == [None]

    asyncio.run(scenario())


def test_explicit_save_updates_view_and_dismisses_saved_view(tmp_path):
    """Verify explicit save invokes saver, updates revision and view, and dismisses latest saved view."""
    project_dir = tmp_path / 'proj_save'
    project_dir.mkdir()
    new_graph = str(project_dir / 'updated.json')

    initial_view = {
        'revision': 'rev-v1',
        'path': '/old.json',
        'effective_path': '/old.json',
        'summary': 'Summary v1',
    }

    saver_calls = []

    def reader():
        return dict(initial_view)

    def saver(path, expected_rev):
        saver_calls.append((path, expected_rev))
        return {
            'revision': 'rev-v2',
            'path': path,
            'effective_path': path,
            'summary': f"Updated summary for {path}",
        }

    async def scenario():
        app = App()
        panel = ProjectGraphPanel(project_dir, reader=reader, saver=saver)
        dismissed = []

        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(panel, callback=dismissed.append)
            await pilot.pause()
            await panel.workers.wait_for_complete()
            await pilot.pause()

            assert panel.revision == 'rev-v1'

            # Edit path input
            path_input = panel.query_one('#project-graph-path', Input)
            path_input.value = new_graph
            await pilot.pause()

            # Press Save binding
            save_btn = panel.query_one('#project-graph-save', Button)
            save_btn.press()
            await pilot.pause()
            await panel.workers.wait_for_complete()
            await pilot.pause()

            # Saver was invoked with typed path and expected revision
            assert saver_calls == [(new_graph, 'rev-v1')]

            # Revision and saved view updated
            assert panel.revision == 'rev-v2'
            assert panel.saved_view is not None
            assert panel.saved_view['revision'] == 'rev-v2'

            # View updated
            eff_widget = panel.query_one('#project-graph-effective', Static)
            assert eff_widget.tooltip.plain == new_graph

            summary_widget = panel.query_one('#project-graph-summary', Static)
            assert f"Updated summary for {new_graph}" in widget_plain(summary_widget)

            status_widget = panel.query_one('#project-graph-status', Static)
            assert 'Project graph binding saved.' in widget_plain(status_widget)

            # Closing modal returns saved view
            panel.action_close()
            await pilot.pause()
            assert len(dismissed) == 1
            assert dismissed[0]['revision'] == 'rev-v2'

    asyncio.run(scenario())


def test_disconnect_defaults_with_empty_path(tmp_path):
    """Verify empty path passed to saver disconnects defaults and updates view to empty/None."""
    project_dir = tmp_path / 'proj_disconnect'
    project_dir.mkdir()

    initial_view = {
        'revision': 'rev-conn-1',
        'path': '/previously/bound.json',
        'effective_path': '/previously/bound.json',
        'summary': 'Bound graph',
    }

    saver_calls = []

    def reader():
        return dict(initial_view)

    def saver(path, expected_rev):
        saver_calls.append((path, expected_rev))
        return {
            'revision': 'rev-conn-2',
            'path': '',
            'effective_path': '',
            'summary': '',
        }

    async def scenario():
        app = App()
        panel = ProjectGraphPanel(project_dir, reader=reader, saver=saver)
        dismissed = []

        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(panel, callback=dismissed.append)
            await pilot.pause()
            await panel.workers.wait_for_complete()
            await pilot.pause()

            # Clear path to disconnect
            path_input = panel.query_one('#project-graph-path', Input)
            path_input.value = ''
            await pilot.pause()

            # Save binding
            panel.action_save()
            await pilot.pause()
            await panel.workers.wait_for_complete()
            await pilot.pause()

            assert saver_calls == [('', 'rev-conn-1')]
            assert panel.revision == 'rev-conn-2'

            # Effective path is displayed as None when empty
            eff_widget = panel.query_one('#project-graph-effective', Static)
            assert widget_plain(eff_widget) == 'None'

            # Summary displays default empty text
            summary_widget = panel.query_one('#project-graph-summary', Static)
            assert widget_plain(summary_widget) == 'No project graph configured.'

            panel.action_close()
            await pilot.pause()
            assert len(dismissed) == 1
            assert dismissed[0]['revision'] == 'rev-conn-2'

    asyncio.run(scenario())


def test_save_failure_preserves_form_and_shows_error(tmp_path):
    """Verify saver exception preserves edited form, leaves revision intact, and shows error message."""
    project_dir = tmp_path / 'proj_fail_save'
    project_dir.mkdir()

    initial_view = {
        'revision': 'rev-safe-1',
        'path': '/safe/path.json',
        'effective_path': '/safe/path.json',
        'summary': 'Safe summary',
    }

    def failing_saver(path, expected_rev):
        raise ValueError("Invalid graph schema: missing tasks root.")

    async def scenario():
        app = App()
        panel = ProjectGraphPanel(project_dir, reader=lambda: dict(initial_view), saver=failing_saver)
        dismissed = []

        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(panel, callback=dismissed.append)
            await pilot.pause()
            await panel.workers.wait_for_complete()
            await pilot.pause()

            # Edit path input
            path_input = panel.query_one('#project-graph-path', Input)
            path_input.value = '/invalid/schema.json'
            await pilot.pause()

            # Attempt save
            save_btn = panel.query_one('#project-graph-save', Button)
            save_btn.press()
            await pilot.pause()
            await panel.workers.wait_for_complete()
            await pilot.pause()

            # Error message displayed
            status_widget = panel.query_one('#project-graph-status', Static)
            assert 'Save failed: Invalid graph schema: missing tasks root.' in widget_plain(status_widget)

            # Form is PRESERVED
            assert path_input.value == '/invalid/schema.json'

            # Revision authority is preserved so user can fix and retry
            assert panel.revision == 'rev-safe-1'
            assert not save_btn.disabled
            assert not path_input.disabled

            # Dismissing yields None (save failed)
            panel.action_close()
            await pilot.pause()
            assert dismissed == [None]

    asyncio.run(scenario())


def test_read_failure_clears_revision_and_save_authority(tmp_path):
    """Verify failed load clears revision and save authority, preventing save."""
    project_dir = tmp_path / 'proj_fail_read'
    project_dir.mkdir()

    saver_calls = []

    def failing_reader():
        raise FileNotFoundError("Backlog repository config unreadable.")

    def dummy_saver(path, rev):
        saver_calls.append((path, rev))
        return {}

    async def scenario():
        app = App()
        panel = ProjectGraphPanel(project_dir, reader=failing_reader, saver=dummy_saver)
        dismissed = []

        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(panel, callback=dismissed.append)
            await pilot.pause()
            await panel.workers.wait_for_complete()
            await pilot.pause()

            # Revision cleared
            assert panel.revision is None

            # Save button is disabled
            save_btn = panel.query_one('#project-graph-save', Button)
            assert save_btn.disabled is True

            # Path input disabled
            path_input = panel.query_one('#project-graph-path', Input)
            assert path_input.disabled is True

            # Preview disabled
            assert panel.query_one('#project-graph-preview', Button).disabled is True

            # Status shows load failure
            status_widget = panel.query_one('#project-graph-status', Static)
            assert 'Load failed: Backlog repository config unreadable.' in widget_plain(status_widget)

            # Calling save action does nothing
            panel.action_save()
            await pilot.pause()
            assert len(saver_calls) == 0

            # Refresh and Back buttons remain enabled
            assert not panel.query_one('#project-graph-refresh', Button).disabled
            assert not panel.query_one('#project-graph-back', Button).disabled

            panel.action_close()
            await pilot.pause()
            assert dismissed == [None]

    asyncio.run(scenario())


def test_refresh_recovers_from_read_failure(tmp_path):
    """Verify refresh button allows recovering from an initial load failure."""
    project_dir = tmp_path / 'proj_refresh_recover'
    project_dir.mkdir()

    should_fail = True

    def toggle_reader():
        if should_fail:
            raise ConnectionError("Service temporarily unavailable.")
        return {
            'revision': 'rev-recovered-1',
            'path': '/recovered/path.json',
            'effective_path': '/recovered/path.json',
            'summary': 'Recovered graph summary',
        }

    async def scenario():
        nonlocal should_fail
        app = App()
        panel = ProjectGraphPanel(project_dir, reader=toggle_reader)

        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(panel)
            await pilot.pause()
            await panel.workers.wait_for_complete()
            await pilot.pause()

            assert panel.revision is None
            assert panel.query_one('#project-graph-save', Button).disabled is True

            # Fix reader and hit refresh
            should_fail = False
            refresh_btn = panel.query_one('#project-graph-refresh', Button)
            refresh_btn.press()
            await pilot.pause()
            await panel.workers.wait_for_complete()
            await pilot.pause()

            assert panel.revision == 'rev-recovered-1'
            assert not panel.query_one('#project-graph-save', Button).disabled
            assert not panel.query_one('#project-graph-path', Input).disabled
            assert widget_plain(panel.query_one('#project-graph-status', Static)) == 'Project binding loaded.'

    asyncio.run(scenario())


def test_concurrent_ops_guarded_and_serialized(tmp_path):
    """Verify while busy, edit/preview/refresh/save are disabled and overlapping calls are prevented."""
    project_dir = tmp_path / 'proj_concurrency'
    project_dir.mkdir()

    read_started = threading.Event()
    read_release = threading.Event()
    reader_invocations = 0

    def slow_reader():
        nonlocal reader_invocations
        reader_invocations += 1
        read_started.set()
        assert read_release.wait(10)
        return {
            'revision': 'rev-conc-1',
            'path': '/slow.json',
            'effective_path': '/slow.json',
            'summary': 'Slow summary',
        }

    async def scenario():
        app = App()
        panel = ProjectGraphPanel(project_dir, reader=slow_reader)

        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(panel)
            await pilot.pause(0.05)

            assert read_started.wait(5)
            await pilot.pause()

            # Busy state active
            assert panel._is_busy is True
            assert panel._is_reading is True

            # Buttons disabled
            assert panel.query_one('#project-graph-save', Button).disabled is True
            assert panel.query_one('#project-graph-preview', Button).disabled is True
            assert panel.query_one('#project-graph-refresh', Button).disabled is True
            assert panel.query_one('#project-graph-path', Input).disabled is True

            # Back button is allowed during read
            assert panel.query_one('#project-graph-back', Button).disabled is False

            # Duplicate action triggers while busy are ignored
            panel.action_refresh()
            panel.action_preview()
            panel.action_save()
            await pilot.pause(0.02)
            assert reader_invocations == 1

            # Release reader
            read_release.set()
            await pilot.pause(0.05)
            await panel.workers.wait_for_complete()
            await pilot.pause()

            assert panel._is_busy is False
            assert panel.revision == 'rev-conc-1'

    asyncio.run(scenario())


def test_close_while_pending_read_dismisses_none_and_ignores_late_result(tmp_path):
    """Verify closing while read is in flight dismisses None immediately and discards late thread return."""
    project_dir = tmp_path / 'proj_close_pending_read'
    project_dir.mkdir()

    read_started = threading.Event()
    read_release = threading.Event()

    def delayed_reader():
        read_started.set()
        assert read_release.wait(10)
        return {
            'revision': 'rev-late',
            'path': '/late.json',
            'effective_path': '/late.json',
            'summary': 'Late summary',
        }

    async def scenario():
        app = App()
        panel = ProjectGraphPanel(project_dir, reader=delayed_reader)
        dismissed = []

        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(panel, callback=dismissed.append)
            await pilot.pause(0.05)

            assert read_started.wait(5)

            # Close modal while read is running (allowed during read)
            panel.action_close()
            await pilot.pause()
            assert dismissed == [None]

            # Release reader thread
            read_release.set()
            await pilot.pause(0.05)
            await panel.workers.wait_for_complete()
            await pilot.pause()

            # Active guard ensured no crash and modal unmounted
            assert not panel._active
            assert panel not in app.screen_stack

    asyncio.run(scenario())


def test_busy_save_blocks_close(tmp_path):
    """Verify close is blocked while save is in flight and Back buttons are disabled."""
    project_dir = tmp_path / 'proj_busy_save'
    project_dir.mkdir()

    save_started = threading.Event()
    save_release = threading.Event()
    saver_calls = []

    def reader():
        return {
            'revision': 'rev-s1',
            'path': '/path1.json',
            'effective_path': '/path1.json',
            'summary': 'Summary 1',
        }

    def slow_saver(path, rev):
        saver_calls.append((path, rev))
        save_started.set()
        assert save_release.wait(10)
        return {
            'revision': 'rev-s2',
            'path': path,
            'effective_path': path,
            'summary': 'Summary 2',
        }

    async def scenario():
        app = App()
        panel = ProjectGraphPanel(project_dir, reader=reader, saver=slow_saver)
        dismissed = []

        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(panel, callback=dismissed.append)
            await pilot.pause()
            await panel.workers.wait_for_complete()
            await pilot.pause()

            # Trigger save
            save_btn = panel.query_one('#project-graph-save', Button)
            back_btn = panel.query_one('#project-graph-back', Button)
            close_btn = panel.query_one('#project-graph-close', Button)

            save_btn.press()
            await pilot.pause(0.05)

            assert save_started.wait(5)
            await pilot.pause()

            # While saving: Back buttons are disabled
            assert panel._is_saving is True
            assert back_btn.disabled is True
            assert close_btn.disabled is True

            # Attempting close while saving is blocked
            panel.action_close()
            await pilot.pause(0.02)
            assert not dismissed
            assert app.screen is panel

            # Release saver thread
            save_release.set()
            await pilot.pause(0.05)
            await panel.workers.wait_for_complete()
            await pilot.pause()

            # Saving finished
            assert panel._is_saving is False
            assert not back_btn.disabled
            assert panel.revision == 'rev-s2'

            # Now close is allowed and dismisses saved view
            panel.action_close()
            await pilot.pause()
            assert len(dismissed) == 1
            assert dismissed[0]['revision'] == 'rev-s2'

    asyncio.run(scenario())


@pytest.mark.parametrize('size', [(80, 24), (130, 44)])
@pytest.mark.parametrize('theme', ['tare-dark', 'tare-light', 'tare-contrast'])
def test_geometry_narrow_controls_and_keyboard_scrollable(tmp_path, size, theme):
    """Verify 80x24 and 130x44 geometries across tare themes with long path and keyboard scrollable summary."""
    project_dir = tmp_path / 'proj_geo'
    project_dir.mkdir()

    long_path = '/very/deep/nested/workspace/directory/' + ('sub/' * 15) + 'work-graph.json'
    long_summary = '\n'.join([f"Task #{i}: implement sub-objective {i} with strict verification" for i in range(60)])

    def reader():
        return {
            'revision': 'rev-geo-1',
            'path': long_path,
            'effective_path': long_path,
            'summary': long_summary,
        }

    async def scenario():
        app = App()
        register_tare_themes(app)
        panel = ProjectGraphPanel(project_dir, reader=reader)
        dismissed = []

        async with app.run_test(size=size) as pilot:
            app.theme = theme
            await pilot.pause()
            app.push_screen(panel, callback=dismissed.append)
            await pilot.pause()
            await panel.workers.wait_for_complete()
            await pilot.pause()

            if size == (80, 24):
                assert panel.has_class('narrow')

            header = panel.query_one('#project-graph-header')
            actions = panel.query_one('#project-graph-actions')
            back_btn = panel.query_one('#project-graph-back', Button)
            refresh_btn = panel.query_one('#project-graph-refresh', Button)
            preview_btn = panel.query_one('#project-graph-preview', Button)
            save_btn = panel.query_one('#project-graph-save', Button)

            # All buttons visible within size bounds
            assert header.region.bottom <= size[1]
            assert actions.region.bottom <= size[1]
            assert back_btn.region.bottom <= size[1]
            assert refresh_btn.region.bottom <= size[1]
            assert preview_btn.region.bottom <= size[1]
            assert save_btn.region.bottom <= size[1]

            # Horizontal bounds
            assert back_btn.region.right <= size[0]
            assert refresh_btn.region.right <= size[0]
            assert preview_btn.region.right <= size[0]
            assert save_btn.region.right <= size[0]

            # Path input contains long path
            path_input = panel.query_one('#project-graph-path', Input)
            assert path_input.value == long_path

            # Vertical scroll focus and keyboard scrolling
            summary_scroll = panel.query_one('#project-graph-summary-scroll', VerticalScroll)
            summary_scroll.focus()
            await pilot.pause()
            assert summary_scroll.has_focus

            await pilot.press('down', 'down', 'page_down')
            await pilot.pause()
            assert summary_scroll.scroll_y > 0

            # Escape key dismisses modal
            await pilot.press('escape')
            await pilot.pause()
            assert dismissed == [None]

    asyncio.run(scenario())
