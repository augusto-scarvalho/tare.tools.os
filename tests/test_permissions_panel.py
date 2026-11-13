"""Tests for PermissionsPanel modal support panel."""
import asyncio
import os
from pathlib import Path
import threading
import time

import pytest
from rich.text import Text
from textual.app import App
from textual.theme import Theme
from textual.widgets import Button, Checkbox, Select, Static, TextArea

from relay.permissions_panel import PermissionsPanel


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


def test_load_initializes_view_and_enables_save(tmp_path):
    """Verify reader synchronously loads defaults, effective text, and revision, enabling Save."""
    project_dir = tmp_path / 'my_project'
    project_dir.mkdir()

    initial_view = {
        'project': str(project_dir),
        'revision': 'rev-init-100',
        'defaults': {
            'mode': 'edit',
            'write_paths': ['src/core', 'docs/api'],
        },
        'effective_text': 'Checks: ruff check, pytest tests/',
        'instructions_text': '# Project Rules\n1. Follow strict typing.\n2. No side effects.',
    }

    def synchronous_reader():
        return dict(initial_view)

    async def scenario():
        app = App()
        panel = PermissionsPanel(project_dir, reader=synchronous_reader)
        dismissed = []

        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(panel, callback=dismissed.append)
            await pilot.pause()
            await panel.workers.wait_for_complete()
            await pilot.pause()

            # Mode Select matches defaults
            mode_select = panel.query_one('#permissions-mode', Select)
            assert mode_select.value == 'edit'
            assert not mode_select.disabled

            # Permitted paths match defaults
            paths_area = panel.query_one('#permissions-paths', TextArea)
            assert 'src/core' in paths_area.text
            assert 'docs/api' in paths_area.text
            assert not paths_area.disabled

            # Commands warning is hidden for 'edit' mode
            warning_widget = panel.query_one('#permissions-command-warning', Static)
            assert warning_widget.display is False

            # Scrollable details populated with plain text
            eff_widget = panel.query_one('#permissions-effective-text', Static)
            assert 'Checks: ruff check, pytest tests/' in widget_plain(eff_widget)

            inst_widget = panel.query_one('#permissions-instructions-text', Static)
            assert '# Project Rules' in widget_plain(inst_widget)

            # Revision stored and Save button enabled
            assert panel.revision == 'rev-init-100'
            save_btn = panel.query_one('#permissions-save', Button)
            assert not save_btn.disabled

            # Refresh and Back buttons enabled
            assert not panel.query_one('#permissions-refresh', Button).disabled
            assert not panel.query_one('#permissions-back', Button).disabled

            # Tooltip set to full path
            title_widget = panel.query_one('#permissions-title', Static)
            assert str(project_dir.resolve()) in title_widget.tooltip

            # Status shows loaded
            status_widget = panel.query_one('#permissions-status', Static)
            assert 'Permissions loaded.' in widget_plain(status_widget)

            # Close without saving dismisses None
            panel.action_close()
            await pilot.pause()
            assert dismissed == [None]

    asyncio.run(scenario())


def test_explicit_save_updates_revision_and_dismisses_saved_view(tmp_path):
    """Verify explicit save invokes saver, updates revision, and dismisses latest view on close."""
    project_dir = tmp_path / 'proj_save'
    project_dir.mkdir()

    initial_view = {
        'project': str(project_dir),
        'revision': 'rev-v1',
        'defaults': {'mode': 'edit', 'write_paths': ['src']},
        'effective_text': 'Checks v1',
        'instructions_text': 'Instructions v1',
    }

    saved_calls = []

    def synchronous_reader():
        return dict(initial_view)

    def synchronous_saver(defaults, expected_revision):
        saved_calls.append((defaults, expected_revision))
        return {
            'project': str(project_dir),
            'revision': 'rev-v2',
            'defaults': defaults,
            'effective_text': 'Checks v2 updated',
            'instructions_text': 'Instructions v2 updated',
        }

    async def scenario():
        app = App()
        panel = PermissionsPanel(project_dir, reader=synchronous_reader, saver=synchronous_saver)
        dismissed = []

        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(panel, callback=dismissed.append)
            await pilot.pause()
            await panel.workers.wait_for_complete()
            await pilot.pause()

            assert panel.revision == 'rev-v1'

            # User edits mode to 'commands'
            mode_select = panel.query_one('#permissions-mode', Select)
            mode_select.value = 'commands'
            await pilot.pause()

            # User edits permitted paths
            paths_area = panel.query_one('#permissions-paths', TextArea)
            paths_area.load_text('src/core\nbin/runner\n.')
            await pilot.pause()

            # Explicit save action
            save_btn = panel.query_one('#permissions-save', Button)
            save_btn.press()
            await pilot.pause()
            await panel.workers.wait_for_complete()
            await pilot.pause()

            # Verify saver invocation
            assert len(saved_calls) == 1
            passed_defaults, passed_rev = saved_calls[0]
            assert passed_defaults['mode'] == 'commands'
            assert passed_defaults['write_paths'] == ['src/core', 'bin/runner', '.']
            assert passed_rev == 'rev-v1'

            # Revision replaced with new revision from saver
            assert panel.revision == 'rev-v2'
            assert panel.saved_view is not None
            assert panel.saved_view['revision'] == 'rev-v2'

            # Status updated
            status_widget = panel.query_one('#permissions-status', Static)
            assert 'Permissions saved for new conversations and Work.' in widget_plain(status_widget)

            # Details updated with new view
            eff_widget = panel.query_one('#permissions-effective-text', Static)
            assert 'Checks v2 updated' in widget_plain(eff_widget)

            # Closing dismisses latest successfully saved view
            panel.action_close()
            await pilot.pause()
            assert len(dismissed) == 1
            assert dismissed[0]['revision'] == 'rev-v2'

    asyncio.run(scenario())


def test_no_autosave_on_open_change_or_refresh(tmp_path):
    """Verify panel NEVER applies automatically merely by opening, changing fields, or refreshing."""
    project_dir = tmp_path / 'proj_no_auto'
    project_dir.mkdir()

    initial_view = {
        'project': str(project_dir),
        'revision': 'rev-auto-0',
        'defaults': {'mode': 'edit', 'write_paths': ['lib']},
        'effective_text': 'Checks',
        'instructions_text': 'Instructions',
    }

    save_calls = []

    def reader():
        return dict(initial_view)

    def saver(defaults, rev):
        save_calls.append((defaults, rev))
        return dict(initial_view)

    async def scenario():
        app = App()
        panel = PermissionsPanel(project_dir, reader=reader, saver=saver)
        dismissed = []

        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(panel, callback=dismissed.append)
            await pilot.pause()
            await panel.workers.wait_for_complete()
            await pilot.pause()

            # Opening panel did not call saver
            assert len(save_calls) == 0

            # Changing Select mode did not call saver
            mode_select = panel.query_one('#permissions-mode', Select)
            mode_select.value = 'commands'
            await pilot.pause()
            assert len(save_calls) == 0

            mode_select.value = 'read'
            await pilot.pause()
            assert len(save_calls) == 0

            # Editing TextArea did not call saver
            paths_area = panel.query_one('#permissions-paths', TextArea)
            paths_area.load_text('some/uncommitted/path')
            await pilot.pause()
            assert len(save_calls) == 0

            # Refreshing did not call saver
            panel.action_refresh()
            await pilot.pause()
            await panel.workers.wait_for_complete()
            await pilot.pause()
            assert len(save_calls) == 0

            # Closing without explicit save dismisses None
            panel.action_close()
            await pilot.pause()
            assert dismissed == [None]
            assert len(save_calls) == 0

    asyncio.run(scenario())


def test_readonly_mode_disables_paths_and_preserves_draft(tmp_path):
    """Verify Read mode disables permitted edit paths while preserving draft for switching back."""
    project_dir = tmp_path / 'proj_readonly'
    project_dir.mkdir()

    initial_view = {
        'project': str(project_dir),
        'revision': 'rev-draft-1',
        'defaults': {'mode': 'edit', 'write_paths': ['src/core', 'lib/helper']},
        'effective_text': 'Checks',
        'instructions_text': 'Instructions',
    }

    save_calls = []

    def reader():
        return dict(initial_view)

    def saver(defaults, rev):
        save_calls.append((defaults, rev))
        return {
            'project': str(project_dir),
            'revision': 'rev-draft-2',
            'defaults': defaults,
            'effective_text': 'Checks',
            'instructions_text': 'Instructions',
        }

    async def scenario():
        app = App()
        panel = PermissionsPanel(project_dir, reader=reader, saver=saver)

        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(panel)
            await pilot.pause()
            await panel.workers.wait_for_complete()
            await pilot.pause()

            paths_area = panel.query_one('#permissions-paths', TextArea)
            mode_select = panel.query_one('#permissions-mode', Select)

            # Initially edit mode, paths enabled
            assert mode_select.value == 'edit'
            assert not paths_area.disabled
            assert 'src/core' in paths_area.text

            # Switch to 'read': paths disabled but text preserved
            mode_select.value = 'read'
            await pilot.pause()
            assert paths_area.disabled is True
            assert 'src/core' in paths_area.text
            assert 'lib/helper' in paths_area.text

            # Switch to 'commands': paths re-enabled, draft still present
            mode_select.value = 'commands'
            await pilot.pause()
            assert paths_area.disabled is False
            assert 'src/core' in paths_area.text
            assert 'lib/helper' in paths_area.text

            # Switch back to 'read' and save
            mode_select.value = 'read'
            await pilot.pause()
            assert paths_area.disabled is True

            save_btn = panel.query_one('#permissions-save', Button)
            save_btn.press()
            await pilot.pause()
            await panel.workers.wait_for_complete()
            await pilot.pause()

            # In read mode, saver received write_paths=[]
            assert len(save_calls) == 1
            defaults, _ = save_calls[0]
            assert defaults['mode'] == 'read'
            assert defaults['write_paths'] == []

            # Draft remains preserved in the UI TextArea for switching back
            assert 'src/core' in paths_area.text
            assert 'lib/helper' in paths_area.text

    asyncio.run(scenario())


def test_commands_warning_visibility(tmp_path):
    """Verify Commands mode shows warning about system commands outside edit scopes and network."""
    project_dir = tmp_path / 'proj_warn'
    project_dir.mkdir()

    initial_view = {
        'project': str(project_dir),
        'revision': 'rev-w1',
        'defaults': {'mode': 'read', 'write_paths': []},
        'effective_text': 'Checks',
        'instructions_text': 'Instructions',
    }

    async def scenario():
        app = App()
        panel = PermissionsPanel(project_dir, reader=lambda: dict(initial_view))

        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(panel)
            await pilot.pause()
            await panel.workers.wait_for_complete()
            await pilot.pause()

            warning = panel.query_one('#permissions-command-warning', Static)
            mode_select = panel.query_one('#permissions-mode', Select)

            # Initially read mode: warning hidden
            assert warning.display is False

            # Switch to edit mode: warning hidden
            mode_select.value = 'edit'
            await pilot.pause()
            assert warning.display is False

            # Switch to commands mode: warning visible
            mode_select.value = 'commands'
            await pilot.pause()
            assert warning.display is True
            warning_text = widget_plain(warning)
            assert 'directly on this machine as you, with network access' in warning_text
            assert 'no sandbox and no review' in warning_text
            assert 'A local model never gets this terminal' in warning_text
            assert 'Where checks run is under Effective permissions' in warning_text  # not "checks run on this machine"

            # Switch back to read mode: warning hidden again
            mode_select.value = 'read'
            await pilot.pause()
            assert warning.display is False

    asyncio.run(scenario())


def test_save_failure_preserves_form_and_shows_error(tmp_path):
    """Verify saver failure preserves edited form values and displays error without clearing revision."""
    project_dir = tmp_path / 'proj_fail'
    project_dir.mkdir()

    initial_view = {
        'project': str(project_dir),
        'revision': 'rev-safe-1',
        'defaults': {'mode': 'edit', 'write_paths': ['original/path']},
        'effective_text': 'Checks',
        'instructions_text': 'Instructions',
    }

    def failing_saver(defaults, rev):
        raise RuntimeError('Parent validation: write path outside repository boundary.')

    async def scenario():
        app = App()
        panel = PermissionsPanel(project_dir, reader=lambda: dict(initial_view), saver=failing_saver)
        dismissed = []

        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(panel, callback=dismissed.append)
            await pilot.pause()
            await panel.workers.wait_for_complete()
            await pilot.pause()

            # Edit mode and paths
            mode_select = panel.query_one('#permissions-mode', Select)
            mode_select.value = 'commands'
            await pilot.pause()

            paths_area = panel.query_one('#permissions-paths', TextArea)
            paths_area.load_text('modified/custom/path')
            await pilot.pause()

            # Attempt save -> fails
            save_btn = panel.query_one('#permissions-save', Button)
            save_btn.press()
            await pilot.pause()
            await panel.workers.wait_for_complete()
            await pilot.pause()

            # Status displays error message cleanly
            status = panel.query_one('#permissions-status', Static)
            assert 'Save failed: Parent validation' in widget_plain(status)

            # Form edits are PRESERVED
            assert mode_select.value == 'commands'
            assert paths_area.text == 'modified/custom/path'

            # Revision authority is preserved (not None, not corrupted) so user can retry
            assert panel.revision == 'rev-safe-1'
            assert not save_btn.disabled
            assert not mode_select.disabled

            # Dismissing without successful save yields None
            panel.action_close()
            await pilot.pause()
            assert dismissed == [None]

    asyncio.run(scenario())


def test_reader_failure_and_stale_refresh_clears_save_authority(tmp_path):
    """Verify reader failure clears revision and save authority, disabling save."""
    project_dir = tmp_path / 'proj_reader_fail'
    project_dir.mkdir()

    should_fail = True

    def toggle_reader():
        if should_fail:
            raise FileNotFoundError('Project permissions config unreadable or missing.')
        return {
            'project': str(project_dir),
            'revision': 'rev-healthy-1',
            'defaults': {'mode': 'edit', 'write_paths': ['src']},
            'effective_text': 'Checks',
            'instructions_text': 'Instructions',
        }

    async def scenario():
        nonlocal should_fail
        app = App()
        panel = PermissionsPanel(project_dir, reader=toggle_reader)

        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(panel)
            await pilot.pause()
            await panel.workers.wait_for_complete()
            await pilot.pause()

            # Initial load failed
            assert panel.revision is None
            save_btn = panel.query_one('#permissions-save', Button)
            assert save_btn.disabled is True

            status = panel.query_one('#permissions-status', Static)
            assert 'Load failed: Project permissions config' in widget_plain(status)

            # Refresh button and Back button remain enabled
            refresh_btn = panel.query_one('#permissions-refresh', Button)
            assert not refresh_btn.disabled
            assert not panel.query_one('#permissions-back', Button).disabled

            # Now reader succeeds
            should_fail = False
            panel.action_refresh()
            await pilot.pause()
            await panel.workers.wait_for_complete()
            await pilot.pause()

            assert panel.revision == 'rev-healthy-1'
            assert save_btn.disabled is False

            # Refresh again with failure: must clear save authority
            should_fail = True
            panel.action_refresh()
            await pilot.pause()
            await panel.workers.wait_for_complete()
            await pilot.pause()

            # Save authority cleared
            assert panel.revision is None
            assert save_btn.disabled is True
            assert 'Load failed:' in widget_plain(status)

    asyncio.run(scenario())


def test_duplicate_saves_prevented_and_back_blocked_while_saving(tmp_path):
    """Verify duplicate saves are prevented and Back is blocked while saving is in progress."""
    project_dir = tmp_path / 'proj_concurrency'
    project_dir.mkdir()

    save_started = threading.Event()
    save_release = threading.Event()
    save_invocations = []

    def reader():
        return {
            'project': str(project_dir),
            'revision': 'rev-c1',
            'defaults': {'mode': 'edit', 'write_paths': ['src']},
            'effective_text': 'Checks',
            'instructions_text': 'Instructions',
        }

    def slow_saver(defaults, rev):
        save_invocations.append((defaults, rev))
        save_started.set()
        assert save_release.wait(10)
        return {
            'project': str(project_dir),
            'revision': 'rev-c2',
            'defaults': defaults,
            'effective_text': 'Checks',
            'instructions_text': 'Instructions',
        }

    async def scenario():
        app = App()
        panel = PermissionsPanel(project_dir, reader=reader, saver=slow_saver)
        dismissed = []

        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(panel, callback=dismissed.append)
            await pilot.pause()
            await panel.workers.wait_for_complete()
            await pilot.pause()

            # Start save
            save_btn = panel.query_one('#permissions-save', Button)
            back_btn = panel.query_one('#permissions-back', Button)
            save_btn.press()
            await pilot.pause(0.05)

            # Wait for saver thread to start
            assert save_started.wait(5)
            await pilot.pause()

            # While saving: save button is disabled, back button is disabled
            assert panel._is_saving is True
            assert save_btn.disabled is True
            assert back_btn.disabled is True
            assert panel.query_one('#permissions-refresh', Button).disabled is True

            # Duplicate save attempts are ignored
            panel.action_save()
            save_btn.press()
            await pilot.pause(0.02)
            assert len(save_invocations) == 1

            # Back / Close attempts are blocked while saving
            panel.action_close()
            await pilot.pause(0.02)
            assert not dismissed
            assert app.screen is panel

            # Release saver thread
            save_release.set()
            await pilot.pause(0.05)
            await panel.workers.wait_for_complete()
            await pilot.pause()

            # Saving completed
            assert panel._is_saving is False
            assert not back_btn.disabled
            assert panel.revision == 'rev-c2'

            # Now Back is allowed
            panel.action_close()
            await pilot.pause()
            assert len(dismissed) == 1
            assert dismissed[0]['revision'] == 'rev-c2'

    asyncio.run(scenario())


def test_late_read_after_dismissal_handled_cleanly(tmp_path):
    """Verify slow reader completing after modal dismissal is discarded safely without errors."""
    project_dir = tmp_path / 'proj_late'
    project_dir.mkdir()

    read_started = threading.Event()
    read_release = threading.Event()

    def delayed_reader():
        read_started.set()
        assert read_release.wait(10)
        return {
            'project': str(project_dir),
            'revision': 'rev-late',
            'defaults': {'mode': 'edit', 'write_paths': ['src']},
            'effective_text': 'Checks',
            'instructions_text': 'Instructions',
        }

    async def scenario():
        app = App()
        panel = PermissionsPanel(project_dir, reader=delayed_reader)
        dismissed = []

        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(panel, callback=dismissed.append)
            await pilot.pause(0.05)

            # Read started in background thread
            assert read_started.wait(5)

            # Dismiss panel while reading is in progress (Back allowed while reading)
            panel.action_close()
            await pilot.pause()
            assert dismissed == [None]

            # Allow reader thread to complete
            read_release.set()
            await pilot.pause(0.05)
            await panel.workers.wait_for_complete()
            await pilot.pause()

            # No crashes, unmounted guard kept state clean
            assert panel not in app.screen_stack
            assert not panel._active

    asyncio.run(scenario())


@pytest.mark.parametrize('size', [(80, 24), (130, 44)])
@pytest.mark.parametrize('theme', ['tare-dark', 'tare-light', 'tare-contrast'])
def test_geometry_narrow_controls_and_keyboard_scrollable(tmp_path, size, theme):
    """Verify 80x24 and 130x44 geometries across tare themes with keyboard scrollable details."""
    project_dir = tmp_path / 'proj_geo'
    project_dir.mkdir()

    # Generate long details to test vertical scrolling
    long_effective = '\n'.join([f'Check {i}: verify target contract {i}' for i in range(80)])
    long_instructions = '\n'.join([f'Rule {i}: instruction line {i}' for i in range(80)])
    paths_list = [f'path_{i}' for i in range(25)]

    def reader():
        return {
            'project': str(project_dir),
            'revision': 'rev-geo-1',
            'defaults': {'mode': 'commands', 'write_paths': paths_list},
            'effective_text': long_effective,
            'instructions_text': long_instructions,
        }

    async def scenario():
        app = App()
        register_tare_themes(app)
        panel = PermissionsPanel(project_dir, reader=reader)
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

            # Ensure header, footer actions fit within bounds
            header = panel.query_one('#permissions-header')
            actions = panel.query_one('#permissions-actions')
            back_btn = panel.query_one('#permissions-back', Button)
            refresh_btn = panel.query_one('#permissions-refresh', Button)
            save_btn = panel.query_one('#permissions-save', Button)

            assert header.region.bottom <= size[1]
            assert actions.region.bottom <= size[1]
            assert back_btn.region.bottom <= size[1]
            assert refresh_btn.region.bottom <= size[1]
            assert save_btn.region.bottom <= size[1]

            # Keyboard scrollable details
            details_scroll = panel.query_one('#permissions-details-scroll')
            details_scroll.focus()
            await pilot.pause()
            assert details_scroll.has_focus

            # Keyboard scroll down
            await pilot.press('down', 'down', 'page_down')
            await pilot.pause()

            assert details_scroll.scroll_y > 0

            # Escape key dismisses with None
            await pilot.press('escape')
            await pilot.pause()
            assert dismissed == [None]

    asyncio.run(scenario())


def test_review_controls_save_selected_backend_limit_and_keep_actions_visible(tmp_path):
    from textual.widgets import Input
    saved=[]
    view={'project':str(tmp_path),'revision':'initial',
          'defaults':{'mode':'commands','write_paths':['.']},'effective_text':'Owned command scope',
          'instructions_text':'Project instructions'}
    def saver(defaults,revision):
        assert revision=='initial'
        saved.append(defaults)
        return {**view,'revision':'saved','defaults':defaults}
    async def scenario():
        app=App();panel=PermissionsPanel(tmp_path,reader=lambda:view,saver=saver)
        async with app.run_test(size=(80,24)) as pilot:
            app.push_screen(panel);await pilot.pause();await panel.workers.wait_for_complete();await pilot.pause()
            panel.query_one('#permissions-review',Select).value='both';await pilot.pause()
            assert not panel.query_one('#permissions-isolation',Select).disabled
            panel.query_one('#permissions-isolation',Select).value='codex'
            panel.query_one('#permissions-review-limit',Input).value='3'
            await pilot.pause();panel.action_save();await panel.workers.wait_for_complete();await pilot.pause()
            assert saved[0]['command_protection']=={'review':'both','backend':'codex','max_reviews':3,'review_seconds':60,'classifier':'jev','review_policy':'on_demand'}
            assert panel.query_one('#permissions-actions').region.bottom<=24
            panel.query_one('#permissions-mode',Select).value='read';await pilot.pause()
            assert panel.query_one('#permissions-review',Select).disabled
            panel.action_close();await pilot.pause()
    asyncio.run(scenario())



def test_the_saved_always_for_blocked_checks_is_shown_and_removable(tmp_path):
    from relay.turn_engine import checks_outside_sandbox, set_checks_outside_sandbox
    from relay.project_permissions import save_defaults
    from relay import relay_mesh
    project, directory = tmp_path/'project', tmp_path/'console'
    project.mkdir()
    set_checks_outside_sandbox(project, True)  # what the sandbox question's "always" saves
    def reader():
        from relay.project_permissions import permissions_view
        return permissions_view(directory, project, relay_mesh.RELAY_ROOT)
    def saver(defaults, revision):
        save_defaults(directory, project, relay_mesh.RELAY_ROOT, defaults, revision)
        return reader()
    async def scenario():
        app = App(); panel = PermissionsPanel(project, reader=reader, saver=saver)
        async with app.run_test(size=(100, 30)) as pilot:
            app.push_screen(panel); await pilot.pause(); await panel.workers.wait_for_complete(); await pilot.pause()
            box = panel.query_one('#permissions-checks-outside', Checkbox)
            assert box.value and not box.disabled
            assert 'Run checks the sandbox blocks outside it' in widget_plain(panel.query_one('#permissions-effective-text'))
            box.value = False; await pilot.pause()
            panel.action_save(); await panel.workers.wait_for_complete(); await pilot.pause()
            assert not panel.query_one('#permissions-checks-outside', Checkbox).value
    asyncio.run(scenario())
    assert not checks_outside_sandbox(project)


def test_saved_legacy_review_is_not_changed_to_jev_auto_on_open(tmp_path):
    view={'project':str(tmp_path),'revision':'saved','defaults':{'mode':'commands','write_paths':['.'],
        'command_protection':{'review':'codex','backend':'mxc','max_reviews':6,'review_seconds':60}},
        'effective_text':'Saved native review','instructions_text':'No instructions'}
    async def scenario():
        app=App();panel=PermissionsPanel(tmp_path,reader=lambda:view)
        async with app.run_test(size=(80,24)) as pilot:
            app.push_screen(panel);await pilot.pause();await panel.workers.wait_for_complete();await pilot.pause()
            assert panel.query_one('#permissions-classifier',Select).value=='model'
            panel.query_one('#permissions-classifier',Select).value='jev_auto';await pilot.pause()
            assert 'TypeSafe' in str(panel.query_one('#permissions-command-warning').render())
            assert panel.query_one('#permissions-actions').region.bottom<=24
            panel.action_close();await pilot.pause()
    asyncio.run(scenario())
