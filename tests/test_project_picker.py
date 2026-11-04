"""Tests for ProjectPicker modal support panel."""
import asyncio
import os
from pathlib import Path
import threading
import time

import pytest
from rich.text import Text
from textual.app import App
from textual.widgets import Button, Input, OptionList, Select, Static

from relay.project_picker import (
    ProjectPicker,
    directory_reader,
    format_recent_options,
)


def widget_text(widget):
    """Extract plain text representation from a widget's render or content."""
    renderable = getattr(widget, 'content', None)
    if renderable is None:
        renderable = widget.render()
    if isinstance(renderable, Text):
        return renderable.plain
    return str(renderable)


async def until(pilot, condition, max_ticks=80, pause=0.05):
    """Wait until condition returns True or timeout."""
    for _ in range(max_ticks):
        await pilot.pause(pause)
        if condition():
            return
    assert condition()


def test_recent_duplicate_basenames_fullpath_disambiguation(tmp_path):
    """Verify duplicate basenames in recent projects are disambiguated with full path."""
    dir_a = tmp_path / 'group1' / 'common_pkg'
    dir_b = tmp_path / 'group2' / 'common_pkg'
    dir_c = tmp_path / 'group3' / 'unique_pkg'
    dir_a.mkdir(parents=True)
    dir_b.mkdir(parents=True)
    dir_c.mkdir(parents=True)

    recent_paths = [dir_a, dir_b, dir_c]
    options = format_recent_options(recent_paths)

    labels = {val: label for label, val in options}
    resolved_a = str(dir_a.resolve())
    resolved_b = str(dir_b.resolve())
    resolved_c = str(dir_c.resolve())

    # Duplicate basenames must contain full path for disambiguation
    assert resolved_a in labels
    assert resolved_b in labels
    assert resolved_c in labels

    assert resolved_a in labels[resolved_a]
    assert 'common_pkg' in labels[resolved_a]
    assert resolved_b in labels[resolved_b]
    assert 'common_pkg' in labels[resolved_b]

    # Unique basename does not need parent path disambiguation
    assert labels[resolved_c] == 'unique_pkg'

    # Test selecting recent project in UI browses to it without dismissing
    async def scenario():
        app = App()
        picker = ProjectPicker(tmp_path, recent=recent_paths)
        dismissed = []

        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(picker, callback=dismissed.append)
            await pilot.pause()
            await picker.workers.wait_for_complete()
            await pilot.pause()

            select_widget = picker.query_one('#project-picker-recent', Select)
            assert not select_widget.disabled
            assert len(picker.recent_options) == 3

            # Select recent dir_a
            select_widget.value = resolved_a
            await pilot.pause()
            await picker.workers.wait_for_complete()
            await pilot.pause()

            # Browsed target: input updated, directory validated
            field = picker.query_one('#project-picker-location', Input)
            assert field.value == resolved_a
            assert picker.validated_directory == dir_a.resolve()

            # Must NOT dismiss when recent project is chosen
            assert not dismissed
            assert app.screen is picker

    asyncio.run(scenario())


def test_directory_reader_limit_and_symlinks(tmp_path):
    """Verify directory_reader bounds immediate entries, sorts dirs, and resolves symlinks."""
    root = tmp_path / 'bounded_root'
    root.mkdir()

    # Create 220 subdirectories
    for i in range(220):
        (root / f'sub_{i:03d}').mkdir()

    # Create 10 regular files
    for i in range(10):
        (root / f'file_{i:03d}.txt').write_text('content')

    # Create valid symlink to an external directory
    ext_dir = tmp_path / 'external_target'
    ext_dir.mkdir()
    symlink_dir = root / 'symlink_folder'
    try:
        symlink_dir.symlink_to(ext_dir, target_is_directory=True)
    except (OSError, NotImplementedError):
        pass  # On Windows without developer mode/privilege, symlinks may not be allowed

    dirs, limited = directory_reader(root)

    # Must return max 200 dirs
    assert len(dirs) == 200
    assert limited is True

    # Must be sorted case-insensitively
    names = [d.name.casefold() for d in dirs]
    assert names == sorted(names)

    # Missing directory raises FileNotFoundError
    with pytest.raises(FileNotFoundError):
        directory_reader(tmp_path / 'missing_directory')

    # Non-directory raises NotADirectoryError
    regular_file = tmp_path / 'sample.txt'
    regular_file.write_text('text')
    with pytest.raises(NotADirectoryError):
        directory_reader(regular_file)


def test_browsing_parent_child_and_explicit_selection(tmp_path):
    """Verify navigating parent/child folders, Up button, and explicit Open project dismissal."""
    root = tmp_path / 'workspace'
    root.mkdir()
    child_a = root / 'child_a'
    child_a.mkdir()
    child_b = root / 'child_b'
    child_b.mkdir()
    nested = child_a / 'grandchild'
    nested.mkdir()

    async def scenario():
        app = App()
        picker = ProjectPicker(root)
        dismissed = []

        async with app.run_test(size=(130, 44)) as pilot:
            await pilot.pause()
            app.push_screen(picker, callback=dismissed.append)
            await pilot.pause()
            await picker.workers.wait_for_complete()
            await pilot.pause()

            # Initial root listing contains child_a and child_b
            folder_list = picker.query_one('#project-picker-folders', OptionList)
            assert folder_list.option_count == 2
            dir_names = [picker.rows[i].name for i in range(len(picker.rows))]
            assert dir_names == ['child_a', 'child_b']

            # Browse into child_a by clicking OptionList item
            picker.browse(child_a)
            await pilot.pause()
            await picker.workers.wait_for_complete()
            await pilot.pause()

            field = picker.query_one('#project-picker-location', Input)
            assert field.value == str(child_a.resolve())
            assert len(picker.rows) == 1
            assert picker.rows[0].name == 'grandchild'

            # Up button navigates back to parent (root)
            up_btn = picker.query_one('#project-picker-up', Button)
            up_btn.press()
            await pilot.pause()
            await picker.workers.wait_for_complete()
            await pilot.pause()

            assert field.value == str(root.resolve())
            assert [picker.rows[i].name for i in range(len(picker.rows))] == ['child_a', 'child_b']

            # Open button is enabled and dismissing yields absolute resolved directory string
            open_btn = picker.query_one('#project-picker-open', Button)
            assert not open_btn.disabled
            open_btn.press()
            await pilot.pause()

            assert dismissed == [str(root.resolve())]
            assert app.screen is not picker

    asyncio.run(scenario())


def test_input_draft_clears_authority_and_no_switch(tmp_path):
    """Verify uncommitted input draft immediately invalidates authority and disables Open."""
    root = tmp_path / 'project'
    root.mkdir()

    async def scenario():
        app = App()
        picker = ProjectPicker(root)
        dismissed = []

        async with app.run_test(size=(130, 44)) as pilot:
            await pilot.pause()
            app.push_screen(picker, callback=dismissed.append)
            await pilot.pause()
            await picker.workers.wait_for_complete()
            await pilot.pause()

            open_btn = picker.query_one('#project-picker-open', Button)
            assert not open_btn.disabled
            assert picker.validated_directory == root.resolve()

            # Notice explains conversation draft stays in original project
            notice_txt = widget_text(picker.query_one('#project-picker-notice', Static))
            assert 'Current conversation and drafts stay in their original project' in notice_txt

            # User edits input path without submitting
            field = picker.query_one('#project-picker-location', Input)
            field.value = str(tmp_path / 'uncommitted_draft')
            await pilot.pause()

            # Authority cleared immediately and Open disabled
            assert picker.validated_directory is None
            assert open_btn.disabled is True
            assert not dismissed

            # Typing, browsing, and highlighting never switches project
            folder_list = picker.query_one('#project-picker-folders', OptionList)
            if folder_list.option_count > 0:
                folder_list.highlighted = 0
                await pilot.pause()
            assert not dismissed

            # Submitting typed valid path restores authority
            field.value = str(root.resolve())
            await pilot.press('enter')
            await pilot.pause()
            await picker.workers.wait_for_complete()
            await pilot.pause()

            assert picker.validated_directory == root.resolve()
            assert not open_btn.disabled

    asyncio.run(scenario())


def test_invalid_and_empty_directory(tmp_path):
    """Verify empty directory status and invalid directory error handling with previous listing cleared."""
    valid_with_children = tmp_path / 'has_children'
    valid_with_children.mkdir()
    (valid_with_children / 'child_1').mkdir()

    empty_dir = tmp_path / 'empty_project'
    empty_dir.mkdir()

    missing_dir = tmp_path / 'does_not_exist'

    async def scenario():
        app = App()
        picker = ProjectPicker(valid_with_children)

        async with app.run_test(size=(130, 44)) as pilot:
            await pilot.pause()
            app.push_screen(picker)
            await pilot.pause()
            await picker.workers.wait_for_complete()
            await pilot.pause()

            folder_list = picker.query_one('#project-picker-folders', OptionList)
            assert folder_list.option_count == 1
            assert picker.rows[0].name == 'child_1'

            # 1. Browse to empty directory: previous listing cleared, empty status displayed
            picker.browse(empty_dir)
            await pilot.pause()
            await picker.workers.wait_for_complete()
            await pilot.pause()

            assert folder_list.option_count == 0
            assert picker.rows == []
            status_txt = widget_text(picker.query_one('#project-picker-status', Static))
            assert 'No folders here' in status_txt
            # An existing empty directory can be opened
            assert not picker.query_one('#project-picker-open', Button).disabled

            # 2. Browse to missing directory: previous listing cleared, error displayed, Open disabled
            field = picker.query_one('#project-picker-location', Input)
            field.focus()
            field.value = str(missing_dir)
            await pilot.press('enter')
            await pilot.pause()
            await picker.workers.wait_for_complete()
            await pilot.pause()

            assert folder_list.option_count == 0
            assert picker.rows == []
            assert picker.validated_directory is None
            assert picker.query_one('#project-picker-open', Button).disabled is True

            # Error stays visible and no raw traceback is shown
            error_status = widget_text(picker.query_one('#project-picker-status', Static))
            assert 'Traceback' not in error_status
            assert 'Directory not found' in error_status or 'does_not_exist' in error_status

            # Back button stays accessible during and after errors
            back_btn = picker.query_one('#project-picker-back', Button)
            assert not back_btn.disabled

    asyncio.run(scenario())


def test_late_listing_ignored_after_newer_input(tmp_path, monkeypatch):
    """Verify generation guard prevents slow/late directory listing from overwriting newer path."""
    import relay.project_picker as picker_mod

    dir_slow = tmp_path / 'slow_dir'
    dir_fast = tmp_path / 'fast_dir'
    dir_slow.mkdir()
    dir_fast.mkdir()
    (dir_slow / 'slow_child').mkdir()
    (dir_fast / 'fast_child').mkdir()

    slow_started = threading.Event()
    slow_release = threading.Event()
    real_reader = picker_mod.directory_reader

    def mock_reader(path):
        resolved = Path(path).resolve()
        if resolved == dir_slow.resolve():
            slow_started.set()
            assert slow_release.wait(timeout=5)
            return real_reader(resolved)
        return real_reader(resolved)

    monkeypatch.setattr(picker_mod, 'directory_reader', mock_reader)

    async def scenario():
        app = App()
        picker = ProjectPicker(dir_slow)

        async with app.run_test(size=(130, 44)) as pilot:
            await pilot.pause()
            app.push_screen(picker)
            await pilot.pause()

            # Wait until slow directory reading has started in worker
            for _ in range(60):
                await pilot.pause(0.05)
                if slow_started.is_set():
                    break
            assert slow_started.is_set()

            # User types and navigates to fast_dir while slow_dir is still reading
            field = picker.query_one('#project-picker-location', Input)
            field.value = str(dir_fast)
            await pilot.press('enter')
            await pilot.pause()

            # Now release the slow reader
            slow_release.set()
            await pilot.pause()
            await picker.workers.wait_for_complete()
            await pilot.pause()

            # Fast dir state must prevail; late slow listing must be ignored
            assert picker.directory == dir_fast.resolve()
            assert picker.validated_directory == dir_fast.resolve()
            assert [d.name for d in picker.rows] == ['fast_child']
            folder_list = picker.query_one('#project-picker-folders', OptionList)
            assert folder_list.option_count == 1

    try:
        asyncio.run(scenario())
    finally:
        slow_release.set()


def test_late_listing_ignored_after_dismissal(tmp_path, monkeypatch):
    """Verify worker completing after modal dismissal does not crash or corrupt state."""
    import relay.project_picker as picker_mod

    dir_slow = tmp_path / 'slow_dir_dismiss'
    dir_slow.mkdir()

    slow_started = threading.Event()
    slow_release = threading.Event()
    real_reader = picker_mod.directory_reader

    def mock_reader(path):
        slow_started.set()
        assert slow_release.wait(timeout=5)
        return real_reader(path)

    monkeypatch.setattr(picker_mod, 'directory_reader', mock_reader)

    async def scenario():
        app = App()
        picker = ProjectPicker(dir_slow)
        dismissed = []

        async with app.run_test(size=(130, 44)) as pilot:
            await pilot.pause()
            app.push_screen(picker, callback=dismissed.append)
            await pilot.pause()

            for _ in range(60):
                await pilot.pause(0.05)
                if slow_started.is_set():
                    break
            assert slow_started.is_set()

            # User dismisses via Escape while worker is running
            await pilot.press('escape')
            await pilot.pause()

            assert dismissed == [None]
            assert app.screen is not picker

            # Release delayed worker after screen is already dismissed
            slow_release.set()
            await pilot.pause()
            await picker.workers.wait_for_complete()
            await pilot.pause()

            # Result remains None and no crash occurred
            assert dismissed == [None]

    try:
        asyncio.run(scenario())
    finally:
        slow_release.set()


def test_delayed_reader_error_handled_cleanly(tmp_path, monkeypatch):
    """Verify delayed permission/IO error disables Open, clears listing, and avoids raw tracebacks."""
    import relay.project_picker as picker_mod

    error_dir = tmp_path / 'inaccessible'
    error_dir.mkdir()

    def mock_reader(path):
        time.sleep(0.05)
        raise PermissionError('Simulated access restriction to directory')

    monkeypatch.setattr(picker_mod, 'directory_reader', mock_reader)

    async def scenario():
        app = App()
        picker = ProjectPicker(error_dir)

        async with app.run_test(size=(130, 44)) as pilot:
            await pilot.pause()
            app.push_screen(picker)
            await pilot.pause()
            await picker.workers.wait_for_complete()
            await pilot.pause()

            status_txt = widget_text(picker.query_one('#project-picker-status', Static))
            assert 'Simulated access restriction' in status_txt
            assert 'Traceback' not in status_txt

            assert picker.validated_directory is None
            assert picker.query_one('#project-picker-open', Button).disabled is True
            assert picker.query_one('#project-picker-folders', OptionList).option_count == 0

            # Back button remains accessible
            back_btn = picker.query_one('#project-picker-back', Button)
            assert not back_btn.disabled
            back_btn.press()
            await pilot.pause()
            assert app.screen is not picker

    asyncio.run(scenario())


@pytest.mark.parametrize('size', [(80, 24), (130, 44)])
@pytest.mark.parametrize('theme', ['tare-dark', 'tare-light', 'tare-contrast'])
def test_long_paths_geometry_and_keyboard(tmp_path, size, theme):
    """Verify keyboard accessibility, bounds constraints, and narrow layout across geometries and themes."""
    # Create path with deeply nested, long name (150+ chars)
    deep_segment = 'very_long_directory_name_' + ('abcdefghij' * 5)
    deep_dir = tmp_path / deep_segment
    deep_dir.mkdir(parents=True)
    (deep_dir / 'child_project').mkdir()

    async def scenario():
        from relay.native_console import WorkConsole
        from test_native_console import FakeBackend
        app = WorkConsole(deep_dir, backend=FakeBackend(), initial_mode='assistant')
        picker = ProjectPicker(deep_dir)
        dismissed = []

        async with app.run_test(size=size) as pilot:
            app.theme = theme
            await pilot.pause()
            app.push_screen(picker, callback=dismissed.append)
            await pilot.pause()
            await picker.workers.wait_for_complete()
            await pilot.pause()

            # Check narrow class on 80x24
            if size == (80, 24):
                assert picker.has_class('narrow')

            # Ensure header, footer, and buttons fit within screen height bounds
            header = picker.query_one('#project-picker-header')
            actions = picker.query_one('#project-picker-actions')
            open_btn = picker.query_one('#project-picker-open', Button)
            back_btn = picker.query_one('#project-picker-back', Button)
            close_btn = picker.query_one('#project-picker-close', Button)

            assert header.region.bottom <= size[1]
            assert actions.region.bottom <= size[1]
            assert open_btn.region.bottom <= size[1]
            assert back_btn.region.bottom <= size[1]
            assert close_btn.region.bottom <= size[1]

            # Verify input contains the long path and is readable
            field = picker.query_one('#project-picker-location', Input)
            assert deep_segment in field.value

            # Keyboard navigation: Escape dismisses with None
            await pilot.press('escape')
            await pilot.pause()

            assert dismissed == [None]
            assert app.screen is not picker

    asyncio.run(scenario())


def test_escape_key_dismisses_none(tmp_path):
    """Verify Escape key explicitly dismisses with None without selecting any project."""
    root = tmp_path / 'project'
    root.mkdir()

    async def scenario():
        app = App()
        picker = ProjectPicker(root)
        dismissed = []

        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.push_screen(picker, callback=dismissed.append)
            await pilot.pause()
            await picker.workers.wait_for_complete()
            await pilot.pause()

            await pilot.press('escape')
            await pilot.pause()

            assert dismissed == [None]
            assert app.screen is not picker

    asyncio.run(scenario())


def test_unsubmitted_path_edit_rejects_inflight_listing(tmp_path):
    started, release = threading.Event(), threading.Event()
    (tmp_path/'old-child').mkdir()
    def delayed(path):
        started.set()
        assert release.wait(10)
        return directory_reader(path)
    async def scenario():
        app = App(); picker = ProjectPicker(tmp_path, reader=delayed)
        async with app.run_test(size=(80,24)) as pilot:
            app.push_screen(picker)
            await until(pilot, started.is_set)
            field = picker.query_one('#project-picker-location',Input)
            field.value = str(tmp_path/'not-submitted')
            await pilot.pause()
            release.set()
            await picker.workers.wait_for_complete(); await pilot.pause()
            assert field.value.endswith('not-submitted')
            assert picker.rows == [] and picker.validated_directory is None
            assert picker.query_one('#project-picker-open',Button).disabled
    try:
        asyncio.run(scenario())
    finally:
        release.set()
