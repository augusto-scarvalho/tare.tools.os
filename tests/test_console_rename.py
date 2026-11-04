"""Tests for renaming saved conversations in console_demand and native console UI."""
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import threading
import pytest
from rich.text import Text
from textual.widgets import Button, Input, OptionList, Select, Static, TextArea

from relay import console_demand, relay_mesh
from relay.console_backend import Selection
from relay.console_navigation import RenameDialog
from relay.native_console import WorkConsole
from test_native_console import FakeBackend


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path/'project'; root.mkdir()
    control = tmp_path/'control'; control.mkdir()
    monkeypatch.setattr(relay_mesh, 'RELAY_ROOT', control)
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))
    return root, control


def make_snapshot(root, prompt='Initial task prompt'):
    return {
        'selection': asdict(Selection(str(root), prompt=prompt)),
        'interface_mode': 'assistant',
        'assistant_session': None,
        'assistant_history': [],
        'entry_history': [],
        'entry_questions': [],
        'entry_proposal': None,
        'entry_selection': None,
        'last_result': None,
        'message_draft': '',
        'order_draft': {},
    }


def test_rename_persistence_and_subsequent_save_reopen(project):
    root, control = project
    first = console_demand.save(None, 0, root, control, 'draft_saved', make_snapshot(root, 'Task One'))
    identity = first['id']
    assert first['revision'] == 1
    assert 'title' not in first

    rows, errors = console_demand.inventory(root, control)
    assert not errors
    assert len(rows) == 1
    assert rows[0]['title'] == 'Task One'

    renamed = console_demand.rename(identity, 1, root, control, 'Custom Project Alpha')
    assert renamed['title'] == 'Custom Project Alpha'
    assert renamed['revision'] == 2
    assert renamed['snapshot'] == first['snapshot']
    assert renamed['events'] == first['events']

    read_back = console_demand.read(identity, root, control)
    assert read_back == renamed
    assert read_back['title'] == 'Custom Project Alpha'

    rows_after, errors_after = console_demand.inventory(root, control)
    assert not errors_after
    assert len(rows_after) == 1
    assert rows_after[0]['title'] == 'Custom Project Alpha'

    subsequent = console_demand.save(identity, 2, root, control, 'draft_saved', make_snapshot(root, 'Task One Continued'))
    assert subsequent['revision'] == 3
    assert subsequent['title'] == 'Custom Project Alpha'

    read_subsequent = console_demand.read(identity, root, control)
    assert read_subsequent['title'] == 'Custom Project Alpha'
    assert read_subsequent['revision'] == 3

    rows_final, _ = console_demand.inventory(root, control)
    assert rows_final[0]['title'] == 'Custom Project Alpha'


def test_rename_legacy_titles(project):
    root, control = project
    record = console_demand.save(None, 0, root, control, 'draft_saved', make_snapshot(root, 'Legacy Task Prompt'))
    identity = record['id']

    data = json.loads(console_demand.path(identity).read_text('utf-8'))
    data.pop('title', None)
    console_demand.path(identity).write_text(json.dumps(data), 'utf-8')

    rows, _ = console_demand.inventory(root, control)
    assert len(rows) == 1
    assert rows[0]['title'] == 'Legacy Task Prompt'

    read_legacy = console_demand.read(identity, root, control)
    assert 'title' not in read_legacy

    renamed = console_demand.rename(identity, 1, root, control, 'Modernized Title')
    assert renamed['title'] == 'Modernized Title'
    assert renamed['revision'] == 2

    rows_after, _ = console_demand.inventory(root, control)
    assert rows_after[0]['title'] == 'Modernized Title'


def test_inventory_corrupt_title_becomes_unavailable_record(project):
    root, control = project
    record = console_demand.save(None, 0, root, control, 'draft_saved', make_snapshot(root, 'Good Record'))
    identity = record['id']

    data = json.loads(console_demand.path(identity).read_text('utf-8'))
    data['title'] = 12345
    console_demand.path(identity).write_text(json.dumps(data), 'utf-8')

    rows, errors = console_demand.inventory(root, control)
    assert len(rows) == 0
    assert len(errors) == 1
    assert identity in errors[0]


def test_rename_invalid_input_rejected(project):
    root, control = project
    record = console_demand.save(None, 0, root, control, 'draft_saved', make_snapshot(root, 'Baseline'))
    identity = record['id']
    original_bytes = console_demand.path(identity).read_bytes()

    invalid_titles = [
        '',
        '   ',
        None,
        123,
        ['title'],
        {'title': 'abc'},
        'A' * 121,
        'Embedded\nNewline',
        'Embedded\rCarriage',
        'Embedded\tTab',
        'Embedded\x1bEscape',
        'Embedded\x00Null',
        'Embedded\x7fDelete',
        'Embedded\u2028LineSeparator',
        'Embedded\u2029ParagraphSeparator',
        'Embedded\x85NextLine',
        # Controls before strip must not disappear and be accepted
        '\nLeadingNewline',
        'TrailingNewline\n',
        '\rLeadingCR',
        'TrailingCR\r',
        '\tLeadingTab',
        'TrailingTab\t',
        '\x1bLeadingEscape',
        'TrailingEscape\x1b',
    ]

    for invalid in invalid_titles:
        with pytest.raises(ValueError):
            console_demand.validate_title(invalid)
        with pytest.raises(ValueError):
            console_demand.rename(identity, 1, root, control, invalid)
        assert console_demand.path(identity).read_bytes() == original_bytes

    valid_title_1 = 'A'
    assert console_demand.validate_title(valid_title_1) == 'A'

    valid_title_120 = 'Z' * 120
    assert console_demand.validate_title(valid_title_120) == valid_title_120

    unicode_title = 'Café ☕ 日本語 🚀'
    assert console_demand.validate_title(unicode_title) == unicode_title

    # Normal outer spaces trim cleanly
    padded_title = '   Padded Title   '
    assert console_demand.validate_title(padded_title) == 'Padded Title'


def test_rename_stale_revision_rejected(project):
    root, control = project
    record = console_demand.save(None, 0, root, control, 'draft_saved', make_snapshot(root))
    identity = record['id']
    original_bytes = console_demand.path(identity).read_bytes()

    with pytest.raises(ValueError, match='another console'):
        console_demand.rename(identity, 0, root, control, 'Stale 0')
    assert console_demand.path(identity).read_bytes() == original_bytes

    with pytest.raises(ValueError, match='another console'):
        console_demand.rename(identity, 2, root, control, 'Stale 2')
    assert console_demand.path(identity).read_bytes() == original_bytes


def test_rename_wrong_project_or_control_root_rejected(project, tmp_path):
    root, control = project
    wrong_root = tmp_path/'wrong_project'; wrong_root.mkdir()
    wrong_control = tmp_path/'wrong_control'; wrong_control.mkdir()

    record = console_demand.save(None, 0, root, control, 'draft_saved', make_snapshot(root))
    identity = record['id']
    original_bytes = console_demand.path(identity).read_bytes()

    with pytest.raises(ValueError, match='another project'):
        console_demand.rename(identity, 1, wrong_root, control, 'Wrong Project')
    assert console_demand.path(identity).read_bytes() == original_bytes

    with pytest.raises(ValueError, match='another project'):
        console_demand.rename(identity, 1, root, wrong_control, 'Wrong Control')
    assert console_demand.path(identity).read_bytes() == original_bytes


def test_rename_missing_record_never_created(project):
    root, control = project
    missing_id = '0123456789abcdef0123456789abcdef'
    with pytest.raises(FileNotFoundError):
        console_demand.rename(missing_id, 0, root, control, 'Ghost Record')
    assert not console_demand.path(missing_id).exists()


def test_rename_write_failure_preserves_bytes(project, monkeypatch):
    root, control = project
    record = console_demand.save(None, 0, root, control, 'draft_saved', make_snapshot(root))
    identity = record['id']
    original_bytes = console_demand.path(identity).read_bytes()

    def fail_save_json(path, value):
        raise OSError('Simulated disk full')

    monkeypatch.setattr(console_demand, 'save_json', fail_save_json)
    with pytest.raises(OSError, match='Simulated disk full'):
        console_demand.rename(identity, 1, root, control, 'Failing Write')

    assert console_demand.path(identity).read_bytes() == original_bytes


def test_rename_metadata_only_byte_for_value(project):
    root, control = project
    snap = make_snapshot(root, 'Test byte-for-value preservation')
    snap['selection']['session'] = '0123456789abcdef0123456789abcdef'
    snap['selection']['work'] = 'WORK-42'
    snap['selection']['context_paths'] = ['docs/spec.md', 'src/main.py']
    record = console_demand.save(None, 0, root, control, 'draft_saved', snap)
    identity = record['id']

    renamed = console_demand.rename(identity, 1, root, control, 'Verified Byte-For-Value')
    assert renamed['snapshot'] == record['snapshot']
    assert renamed['events'] == record['events']
    assert renamed['id'] == record['id']
    assert renamed['root'] == record['root']
    assert renamed['control_root'] == record['control_root']
    assert renamed['schema'] == record['schema']
    assert renamed['revision'] == record['revision'] + 1
    assert renamed['title'] == 'Verified Byte-For-Value'


def test_full_120_char_unicode_literal_markup_searchable(project):
    root, control = project
    # Exact 120 character title with Unicode and literal Rich markup tags
    full_title = 'Café ☕ 日本語 [bold]literal markup[/bold] ' + ('x' * (120 - len('Café ☕ 日本語 [bold]literal markup[/bold] ')))
    assert len(full_title) == 120

    record = console_demand.save(None, 0, root, control, 'draft_saved', make_snapshot(root))
    identity = record['id']

    renamed = console_demand.rename(identity, 1, root, control, full_title)
    assert renamed['title'] == full_title

    read_back = console_demand.read(identity, root, control)
    assert read_back['title'] == full_title

    rows, errors = console_demand.inventory(root, control)
    assert not errors
    assert rows[0]['title'] == full_title
    assert len(rows[0]['title']) == 120

    matching = [r for r in rows if 'literal markup' in (r['title'] + ' ' + r['id']).casefold()]
    assert len(matching) == 1
    assert matching[0]['id'] == identity

    matching_u = [r for r in rows if '日本語' in (r['title'] + ' ' + r['id']).casefold()]
    assert len(matching_u) == 1
    assert matching_u[0]['id'] == identity


@pytest.mark.parametrize('size', [(80, 24), (130, 44)])
def test_ui_rename_drawer_current_and_noncurrent_both_sizes(tmp_path, monkeypatch, size):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))
    root = tmp_path/'project'; root.mkdir()
    control = tmp_path/'control'; control.mkdir()
    monkeypatch.setattr(relay_mesh, 'RELAY_ROOT', control)

    snap1 = make_snapshot(root, 'Alpha Conversation')
    c1 = console_demand.save(None, 0, root, control, 'draft_saved', snap1)
    snap2 = make_snapshot(root, 'Beta Conversation')
    c2 = console_demand.save(None, 0, root, control, 'draft_saved', snap2)

    async def scenario():
        app = WorkConsole(root, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            prompt = app.query_one('#prompt', TextArea)
            prompt.load_text('Draft before rename')

            await pilot.click('#toolbar-conversations')
            await app.workers.wait_for_complete(); await pilot.pause()
            assert app.panel == 'navigation'

            menu = app.query_one('#conversation-list', OptionList)
            assert len(app.navigation_shown) == 2

            idx_beta = next(i for i, r in enumerate(app.navigation_shown) if r['id'] == c2['id'])
            menu.highlighted = idx_beta
            menu.focus()

            await pilot.press('r'); await pilot.pause()
            assert isinstance(app.screen, RenameDialog)
            rename_input = app.screen.query_one('#rename-input', Input)
            assert rename_input.value == 'Beta Conversation'

            rename_input.value = 'Beta Renamed'
            await pilot.press('enter')
            while isinstance(app.screen, RenameDialog):
                await pilot.pause(0.02)
            await pilot.pause()  # Let the dismissal callback refresh the drawer.

            assert not isinstance(app.screen, RenameDialog)
            assert app.focused is menu
            assert any(r['id'] == c2['id'] and r['title'] == 'Beta Renamed' for r in app.navigation_shown)
            assert app.demand_id is None
            assert prompt.text == 'Draft before rename'

            read_c2 = console_demand.read(c2['id'], root, control)
            assert read_c2['title'] == 'Beta Renamed'

            idx_alpha = next(i for i, r in enumerate(app.navigation_shown) if r['id'] == c1['id'])
            menu.highlighted = idx_alpha
            await pilot.press('enter')
            await app.workers.wait_for_complete(); await pilot.pause()

            assert app.demand_id == c1['id']
            assert app.demand_revision == c1['revision']
            assert app.panel != 'navigation'

            prompt.load_text('Unsent input on active demand')

            await pilot.click('#toolbar-conversations')
            await app.workers.wait_for_complete(); await pilot.pause()

            idx_current = next(i for i, r in enumerate(app.navigation_shown) if r['id'] == c1['id'])
            menu.highlighted = idx_current
            await pilot.click('#conversation-rename'); await pilot.pause()
            assert isinstance(app.screen, RenameDialog)

            rename_input = app.screen.query_one('#rename-input', Input)
            rename_input.value = 'Alpha Renamed'
            await pilot.click('#rename-save')
            while isinstance(app.screen, RenameDialog):
                await pilot.pause(0.02)
            await pilot.pause()  # Let the dismissal callback refresh the drawer.

            assert not isinstance(app.screen, RenameDialog)
            assert app.demand_id == c1['id']
            assert app.demand_revision == 2
            assert app.demand_state['title'] == 'Alpha Renamed'
            assert prompt.text == 'Unsent input on active demand'

            app.checkpoint('draft_saved')
            assert app.demand_revision == 3
            assert app.demand_state['title'] == 'Alpha Renamed'

            read_c1 = console_demand.read(c1['id'], root, control)
            assert read_c1['title'] == 'Alpha Renamed'
            assert read_c1['revision'] == 3

    asyncio.run(scenario())


@pytest.mark.parametrize('size', [(80, 24), (130, 44)])
def test_ui_rename_command_palette_both_sizes(tmp_path, monkeypatch, size):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))
    root = tmp_path/'project'; root.mkdir()
    control = tmp_path/'control'; control.mkdir()
    monkeypatch.setattr(relay_mesh, 'RELAY_ROOT', control)

    snap = make_snapshot(root, 'Palette Start')
    c = console_demand.save(None, 0, root, control, 'draft_saved', snap)

    async def scenario():
        app = WorkConsole(root, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            app.demand_id = c['id']
            app.demand_revision = c['revision']
            app.demand_state = c

            prompt = app.query_one('#prompt', TextArea)
            prompt.load_text('Unsent palette prompt')

            # Assert working before opening drawer (navigation_rows initially empty) and saved-demand selection unchanged
            assert app.navigation_rows == []
            saved_select = app.query_one('#saved-demand', Select)
            initial_saved_selection = saved_select.value

            app.action_rename_conversation()
            await pilot.pause()
            assert isinstance(app.screen, RenameDialog)

            rename_input = app.screen.query_one('#rename-input', Input)
            rename_input.value = 'Palette Renamed'
            await pilot.press('enter')
            while isinstance(app.screen, RenameDialog):
                await pilot.pause(0.02)
            await pilot.pause()  # Let the dismissal callback refresh the drawer.

            assert not isinstance(app.screen, RenameDialog)
            assert app.navigation_rows == []
            assert saved_select.value == initial_saved_selection
            assert app.demand_state['title'] == 'Palette Renamed'
            assert app.demand_revision == 2
            assert prompt.text == 'Unsent palette prompt'

            read_c = console_demand.read(c['id'], root, control)
            assert read_c['title'] == 'Palette Renamed'
            assert read_c['revision'] == 2

    asyncio.run(scenario())


@pytest.mark.parametrize('size', [(80, 24), (130, 44)])
def test_ui_rename_cancel_preserves_state_and_focus(tmp_path, monkeypatch, size):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))
    root = tmp_path/'project'; root.mkdir()
    control = tmp_path/'control'; control.mkdir()
    monkeypatch.setattr(relay_mesh, 'RELAY_ROOT', control)

    snap = make_snapshot(root, 'Cancel Me')
    c = console_demand.save(None, 0, root, control, 'draft_saved', snap)

    async def scenario():
        app = WorkConsole(root, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            app.demand_id = c['id']
            app.demand_revision = c['revision']
            app.demand_state = c

            prompt = app.query_one('#prompt', TextArea)
            prompt.load_text('Keep this draft on cancel')
            prompt.focus()

            app.action_rename_conversation()
            await pilot.pause()
            assert isinstance(app.screen, RenameDialog)

            rename_input = app.screen.query_one('#rename-input', Input)
            rename_input.value = 'Changed But Cancelled'

            await pilot.press('escape')
            await pilot.pause()

            assert not isinstance(app.screen, RenameDialog)
            assert app.focused is prompt
            assert prompt.text == 'Keep this draft on cancel'
            assert app.demand_state.get('title') is None
            assert console_demand.read(c['id'], root, control).get('title') is None

    asyncio.run(scenario())


def test_ui_rename_refusals(tmp_path, monkeypatch):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))
    root = tmp_path/'project'; root.mkdir()
    other = tmp_path/'other'; other.mkdir()
    control = tmp_path/'control'; control.mkdir()
    monkeypatch.setattr(relay_mesh, 'RELAY_ROOT', control)

    snap = make_snapshot(root, 'Refusal Test')
    c = console_demand.save(None, 0, root, control, 'draft_saved', snap)

    async def scenario():
        app = WorkConsole(root, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()

            # 1. Unsaved conversation
            assert app.demand_id is None
            app.action_rename_conversation()
            await pilot.pause()
            assert not isinstance(app.screen, RenameDialog)

            # Set saved conversation
            app.demand_id = c['id']
            app.demand_revision = c['revision']
            app.demand_state = c

            # 2. Busy
            app.busy = True
            app.action_rename_conversation()
            await pilot.pause()
            assert not isinstance(app.screen, RenameDialog)
            app.busy = False

            # 3. Sending message
            app.sending_message = True
            app.action_rename_conversation()
            await pilot.pause()
            assert not isinstance(app.screen, RenameDialog)
            app.sending_message = False

            # 4. Unapplied workspace change
            app.query_one('#root', Input).value = str(other)
            app.action_rename_conversation()
            await pilot.pause()
            assert not isinstance(app.screen, RenameDialog)

    asyncio.run(scenario())


def test_ui_rename_recheck_scope_and_stale_revision_fails_safely(tmp_path, monkeypatch):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))
    root = tmp_path/'project'; root.mkdir()
    control = tmp_path/'control'; control.mkdir()
    other_control = tmp_path/'other_control'; other_control.mkdir()
    other_root = tmp_path/'other_project'; other_root.mkdir()
    monkeypatch.setattr(relay_mesh, 'RELAY_ROOT', control)

    snap = make_snapshot(root, 'Recheck Test')
    c = console_demand.save(None, 0, root, control, 'draft_saved', snap)

    async def scenario():
        app = WorkConsole(root, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            app.demand_id = c['id']
            app.demand_revision = c['revision']
            app.demand_state = c

            # Case A: Stale active revision conflict before save
            app.action_rename_conversation(); await pilot.pause()
            assert isinstance(app.screen, RenameDialog)
            app.demand_revision = 999  # Changed in background
            dialog = app.screen
            dialog.query_one('#rename-input', Input).value = 'Conflict Title'
            await pilot.press('enter'); await pilot.pause()
            assert isinstance(app.screen, RenameDialog)
            assert 'changed' in str(dialog.query_one('#rename-error', Static).render()).lower()
            await pilot.press('escape'); await pilot.pause()
            assert not isinstance(app.screen, RenameDialog)

            # Reset revision
            app.demand_revision = c['revision']

            # Case B: Changed active target
            app.action_rename_conversation(); await pilot.pause()
            assert isinstance(app.screen, RenameDialog)
            dialog = app.screen
            app.demand_id = 'different_demand_id'
            dialog.query_one('#rename-input', Input).value = 'Different Active'
            await pilot.press('enter'); await pilot.pause()
            assert isinstance(app.screen, RenameDialog)
            assert 'active conversation changed' in str(dialog.query_one('#rename-error', Static).render()).lower()
            await pilot.press('escape'); await pilot.pause()

            # Reset active id
            app.demand_id = c['id']

            # Case C: Changed workspace root input
            app.action_rename_conversation(); await pilot.pause()
            assert isinstance(app.screen, RenameDialog)
            dialog = app.screen
            app.screen_stack[0].query_one('#root', Input).value = str(other_root)
            dialog.query_one('#rename-input', Input).value = 'Wrong Workspace'
            await pilot.press('enter'); await pilot.pause()
            assert isinstance(app.screen, RenameDialog)
            assert 'workspace changed' in str(dialog.query_one('#rename-error', Static).render()).lower()
            await pilot.press('escape'); await pilot.pause()
            app.screen_stack[0].query_one('#root', Input).value = str(root)

            # Case D: Changed control root
            app.action_rename_conversation(); await pilot.pause()
            assert isinstance(app.screen, RenameDialog)
            dialog = app.screen
            monkeypatch.setattr(relay_mesh, 'RELAY_ROOT', other_control)
            dialog.query_one('#rename-input', Input).value = 'Wrong Control'
            await pilot.press('enter'); await pilot.pause()
            assert isinstance(app.screen, RenameDialog)
            assert 'control root changed' in str(dialog.query_one('#rename-error', Static).render()).lower()
            await pilot.press('escape'); await pilot.pause()
            monkeypatch.setattr(relay_mesh, 'RELAY_ROOT', control)

            # Case E: Changed navigation generation
            app.action_rename_conversation(); await pilot.pause()
            assert isinstance(app.screen, RenameDialog)
            dialog = app.screen
            app.navigation_generation += 1
            dialog.query_one('#rename-input', Input).value = 'Wrong Gen'
            await pilot.press('enter'); await pilot.pause()
            assert isinstance(app.screen, RenameDialog)
            assert 'conversations list changed' in str(dialog.query_one('#rename-error', Static).render()).lower()
            await pilot.press('escape'); await pilot.pause()

    asyncio.run(scenario())


def test_ui_rename_write_failure_ui_unchanged(project, monkeypatch):
    root, control = project
    snap = make_snapshot(root, 'Write Failure UI Test')
    c = console_demand.save(None, 0, root, control, 'draft_saved', snap)

    def fail_save_json(path, value):
        raise OSError('Disk write fault')

    monkeypatch.setattr(console_demand, 'save_json', fail_save_json)

    async def scenario():
        app = WorkConsole(root, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            app.demand_id = c['id']
            app.demand_revision = c['revision']
            app.demand_state = c

            prompt = app.query_one('#prompt', TextArea)
            prompt.load_text('Draft preserved on failure')
            prompt.focus()

            app.action_rename_conversation(); await pilot.pause()
            assert isinstance(app.screen, RenameDialog)
            dialog = app.screen

            dialog.query_one('#rename-input', Input).value = 'Faulty Save'
            await pilot.press('enter'); await pilot.pause()

            # Dialog must stay open, show error, re-enable buttons
            assert isinstance(app.screen, RenameDialog)
            assert not dialog.saving
            assert not dialog.query_one('#rename-input', Input).disabled
            assert not dialog.query_one('#rename-save', Button).disabled
            assert 'disk write fault' in str(dialog.query_one('#rename-error', Static).render()).lower()

            # State unchanged
            assert app.demand_revision == c['revision']
            assert app.demand_state['revision'] == c['revision']
            assert app.demand_state.get('title') is None
            assert prompt.text == 'Draft preserved on failure'

            # Cancel closes safely
            await pilot.press('escape'); await pilot.pause()
            assert not isinstance(app.screen, RenameDialog)
            assert app.focused is prompt

    asyncio.run(scenario())


def test_ui_rename_saving_flag_blocks_cancel_and_double_save(project, monkeypatch):
    root, control = project
    snap = make_snapshot(root, 'Saving Flag Test')
    c = console_demand.save(None, 0, root, control, 'draft_saved', snap)

    write_started = threading.Event()
    write_proceed = threading.Event()
    real_save_json = console_demand.save_json

    def slow_save_json(path, value):
        write_started.set()
        write_proceed.wait(timeout=5)
        real_save_json(path, value)

    monkeypatch.setattr(console_demand, 'save_json', slow_save_json)

    async def scenario():
        app = WorkConsole(root, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            app.demand_id = c['id']
            app.demand_revision = c['revision']
            app.demand_state = c

            app.action_rename_conversation(); await pilot.pause()
            assert isinstance(app.screen, RenameDialog)
            dialog = app.screen

            dialog.query_one('#rename-input', Input).value = 'Slow Saved Title'
            await pilot.press('enter'); await pilot.pause()

            # Wait until slow_save_json starts
            for _ in range(50):
                if write_started.is_set():
                    break
                await pilot.pause(0.02)
            assert write_started.is_set()

            # Verify saving flag and disabled widgets
            assert dialog.saving is True
            assert dialog.query_one('#rename-input', Input).disabled is True
            assert dialog.query_one('#rename-save', Button).disabled is True
            assert dialog.query_one('#rename-cancel', Button).disabled is True

            # Try to cancel with Escape and click Cancel - must be rejected
            await pilot.press('escape'); await pilot.pause()
            assert isinstance(app.screen, RenameDialog)

            dialog.action_cancel()
            assert isinstance(app.screen, RenameDialog)

            # Try double Save
            dialog.action_save()
            assert isinstance(app.screen, RenameDialog)

            # Allow write to finish
            write_proceed.set()
            while isinstance(app.screen, RenameDialog):
                await pilot.pause(0.02)
            await pilot.pause()  # Let the dismissal callback refresh the drawer.

            assert not isinstance(app.screen, RenameDialog)
            assert app.demand_state['title'] == 'Slow Saved Title'

    asyncio.run(scenario())


def test_ui_keyboard_regression_zero_backend_launches(tmp_path, monkeypatch):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))
    root = tmp_path/'project'; root.mkdir()
    control = tmp_path/'control'; control.mkdir()
    monkeypatch.setattr(relay_mesh, 'RELAY_ROOT', control)

    snap = make_snapshot(root, 'Keyboard Test')
    c = console_demand.save(None, 0, root, control, 'draft_saved', snap)

    async def scenario():
        app = WorkConsole(root, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            app.demand_id = c['id']
            app.demand_revision = c['revision']
            app.demand_state = c

            prompt = app.query_one('#prompt', TextArea)
            prompt.load_text('Danger: do not send')

            app.action_rename_conversation()
            await pilot.pause()
            assert isinstance(app.screen, RenameDialog)

            await pilot.press('ctrl+enter'); await pilot.pause()
            await pilot.press('ctrl+r'); await pilot.pause()
            await pilot.press('ctrl+x'); await pilot.pause()
            await pilot.press('f1'); await pilot.pause()
            await pilot.press('f2'); await pilot.pause()

            assert len(app.backend.launches) == 0
            assert isinstance(app.screen, RenameDialog)
            assert prompt.text == 'Danger: do not send'

            await pilot.press('escape'); await pilot.pause()
            assert not isinstance(app.screen, RenameDialog)
            assert len(app.backend.launches) == 0

    asyncio.run(scenario())
