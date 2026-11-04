"""Tests for TurnChangesPanel and ChangesPanel turn review integration."""
import asyncio
from pathlib import Path
import threading

import pytest
from textual.app import App
from textual.widgets import Button, Select, Static

from relay.changes_panel import ChangesPanel, FileViewer
from relay.turn_changes_panel import TurnChangesPanel, TurnUndoConfirmDialog


def content(widget):
    return str(getattr(widget, 'content', widget.render()))


async def until(pilot, condition, max_ticks=80, pause=0.05):
    for _ in range(max_ticks):
        await pilot.pause(pause)
        if condition():
            return
    assert condition()


def sample_turn_review(invocation_id='inv-42', undo_status='READY', undo_plan='plan-42'):
    return {
        'session_id': 'sess-test',
        'invocation_id': invocation_id,
        'files': [
            {
                'file_id': 'f1',
                'repository': 'repo',
                'path': 'pkg/alpha.py',
                'effect_id': 'eff-alpha',
                'edit_count': 3,
                'diff': '--- a/pkg/alpha.py\n+++ b/pkg/alpha.py\n@@ -1,2 +1,3 @@\n+line_new = True\n-old_line = False\n',
                'segments': 2,
            },
            {
                'file_id': 'f2',
                'repository': 'repo',
                'path': 'pkg/beta.py',
                'effect_id': 'eff-beta',
                'edit_count': 1,
                'diff': '--- a/pkg/beta.py\n+++ b/pkg/beta.py\n@@ -10,1 +10,2 @@\n+added = 42\n',
                'segments': 1,
            },
            {
                'file_id': 'f3',
                'repository': 'repo',
                'path': 'pkg/gamma.py',
                'effect_id': 'eff-gamma',
                'edit_count': 1,
                'diff': None,
                'diff_unavailable': 'Binary or snapshot corrupted',
                'segments': 1,
            },
        ],
        'undo': {
            'status': undo_status,
            'plan': undo_plan,
            'reason': 'Conflicting edit exists' if undo_status == 'UNAVAILABLE' else None,
            'files': 3,
            'edits': 5,
        },
        'scope': 'Historical turn 42 changes (file-tool edits only)',
    }


def completed_result(invocation_id):
    return {'status':'UNDONE','invocation_id':invocation_id,
            'files':[{'repository':f['repository'],'path':f['path'],'status':'UNDONE'} for f in sample_turn_review()['files']]}


@pytest.mark.parametrize('size', [(80, 24), (130, 44)])
def test_turn_panel_grouped_file_selection_diff_and_copy(tmp_path, size):
    project = tmp_path / 'project'
    session = tmp_path / 'custody'
    turn_id = 'inv-42'
    copies = []

    def fake_reader(sess, *, project=None, invocation_id=None, **kwargs):
        assert invocation_id == turn_id
        return sample_turn_review(invocation_id=turn_id)

    async def scenario():
        app = App()
        app.copy_to_clipboard = copies.append
        async with app.run_test(size=size) as pilot:
            panel = TurnChangesPanel(session, project, turn_id, reader=fake_reader)
            app.push_screen(panel)

            # Wait for panel to load review
            await until(pilot, lambda: '3 files · 5 recorded edits' in content(panel.query_one('#turn-changes-summary', Static)))

            # Summary checks: historical scope, per-file notices, no raw session path
            summary_txt = content(panel.query_one('#turn-changes-summary', Static))
            assert str(session) not in summary_txt
            assert 'Notice (repo/pkg/alpha.py): discontinuous snapshot chain (2 segments' in summary_txt
            assert 'Notice (repo/pkg/gamma.py): snapshot unavailable (Binary or snapshot corrupted)' in summary_txt

            # Verify geometry fits within bounds
            assert panel.query_one('#turn-changes-close').region.bottom <= size[1]
            assert panel.query_one('#turn-changes-actions').region.bottom <= size[1]

            # File selector grouped options check
            file_sel = panel.query_one('#turn-changes-file', Select)
            assert file_sel.value == 'f1'

            # Initial diff is for f1
            diff_widget = panel.query_one('#turn-changes-diff', Static)
            assert '+line_new = True' in content(diff_widget)

            # Copy diff for f1
            copy_btn = panel.query_one('#turn-changes-copy', Button)
            assert not copy_btn.disabled
            copy_btn.press()
            await pilot.pause()
            assert len(copies) == 1
            assert '+line_new = True' in copies[0]
            assert copy_btn.label == 'Copied'

            # Switch file to f2
            file_sel.value = 'f2'
            await pilot.pause()
            await until(pilot, lambda: '+added = 42' in content(panel.query_one('#turn-changes-diff', Static)))
            assert copy_btn.label == 'Copy diff'
            copy_btn.press()
            await pilot.pause()
            assert len(copies) == 2
            assert '+added = 42' in copies[1]

            # Switch file to f3 (diff unavailable)
            file_sel.value = 'f3'
            await pilot.pause()
            await until(pilot, lambda: 'Diff unavailable: Binary or snapshot corrupted' in content(panel.query_one('#turn-changes-diff', Static)))
            assert copy_btn.disabled

            # Back button closes panel
            panel.query_one('#turn-changes-close', Button).press()
            await pilot.pause()
            assert app.screen is not panel

    asyncio.run(scenario())


def test_turn_panel_read_only_open_and_back(tmp_path):
    project = tmp_path / 'project'
    session = tmp_path / 'custody'
    turn_id = 'inv-42'

    def fake_reader(sess, *, project=None, invocation_id=None, **kwargs):
        return sample_turn_review(invocation_id=turn_id)

    file_read_calls = []

    def fake_file_reader(sess, *, project=None, effect_id=None, invocation_id=None):
        file_read_calls.append((effect_id, invocation_id))
        return {
            'path': 'pkg/alpha.py',
            'content': 'def alpha():\n    return 42\n',
            'sha256': 'sha-alpha',
        }

    async def scenario():
        app = App()
        async with app.run_test(size=(100, 30)) as pilot:
            panel = TurnChangesPanel(session, project, turn_id, reader=fake_reader, file_reader=fake_file_reader)
            app.push_screen(panel)
            await until(pilot, lambda: not panel.query_one('#turn-changes-open', Button).disabled)

            # Open file
            panel.query_one('#turn-changes-open', Button).press()
            await until(pilot, lambda: isinstance(app.screen, FileViewer))

            viewer = app.screen
            assert 'def alpha():\n    return 42' in content(viewer.query_one('#file-viewer-content', Static))
            assert len(file_read_calls) == 1
            assert file_read_calls[0] == ('eff-alpha', turn_id)

            # Esc returns to TurnChangesPanel
            await pilot.press('escape')
            await until(pilot, lambda: app.screen is panel)
            assert isinstance(app.screen, TurnChangesPanel)

    asyncio.run(scenario())


def test_changes_panel_selected_turn_binding_and_all_turns_disabled(tmp_path):
    project = tmp_path / 'project'
    session = tmp_path / 'custody'
    reloads = []

    def fake_changes_reader(sess, *, project=None, invocation_id=None, **kwargs):
        reloads.append(invocation_id)
        return {
            'turns': [
                {'id': 'turn-1', 'status': 'succeeded'},
                {'id': 'turn-2', 'status': 'succeeded'},
            ],
            'edits': [
                {'effect_id': 'e1', 'repository': 'repo', 'path': 'a.py', 'status': 'succeeded'}
            ],
            'checks': [],
        }

    def fake_turn_reader(sess, *, project=None, invocation_id=None, **kwargs):
        return sample_turn_review(invocation_id=invocation_id)

    async def scenario():
        app = App()
        async with app.run_test(size=(100, 30)) as pilot:
            panel = ChangesPanel(session, project, reader=fake_changes_reader, turn_reader=fake_turn_reader)
            app.push_screen(panel)
            await until(pilot, lambda: bool(panel.rows))

            review_turn_btn = panel.query_one('#changes-review-turn', Button)
            turn_sel = panel.query_one('#changes-turn', Select)

            # "All turns" disables Review turn button
            assert turn_sel.value == ''
            assert review_turn_btn.disabled

            # Select specific turn
            turn_sel.value = 'turn-2'
            await until(pilot, lambda: panel.turn == 'turn-2')
            assert not review_turn_btn.disabled

            # Open Review turn
            review_turn_btn.press()
            await until(pilot, lambda: isinstance(app.screen, TurnChangesPanel))
            turn_panel = app.screen
            assert turn_panel.invocation_id == 'turn-2'
            assert turn_panel.read_lock is panel.read_lock

            # Returning refreshes parent review and preserves selected turn
            reloads_before = len(reloads)
            turn_panel.action_close()
            await until(pilot, lambda: app.screen is panel)
            assert panel.turn == 'turn-2'
            await until(pilot, lambda: len(reloads) > reloads_before)
            assert panel.query_one('#changes-turn', Select).value == 'turn-2'

            # Switching back to All turns disables button again
            turn_sel.value = ''
            await pilot.pause()
            assert review_turn_btn.disabled

    asyncio.run(scenario())


def test_turn_undo_confirm_cancel_and_default_enter_cancel(tmp_path):
    project = tmp_path / 'project'
    session = tmp_path / 'custody'
    turn_id = 'inv-42'
    undo_calls = []

    def fake_reader(sess, *, project=None, invocation_id=None, **kwargs):
        return sample_turn_review(invocation_id=turn_id, undo_status='READY', undo_plan='plan-alpha')

    def fake_undoer(sess, *, project=None, invocation_id=None, expected_plan=None):
        undo_calls.append((invocation_id, expected_plan))
        return completed_result(invocation_id)

    async def scenario():
        app = App()
        async with app.run_test(size=(100, 30)) as pilot:
            panel = TurnChangesPanel(session, project, turn_id, reader=fake_reader, undoer=fake_undoer)
            app.push_screen(panel)
            await until(pilot, lambda: not panel.query_one('#turn-changes-undo', Button).disabled)

            # Click Undo turn -> opens TurnUndoConfirmDialog
            panel.query_one('#turn-changes-undo', Button).press()
            await until(pilot, lambda: isinstance(app.screen, TurnUndoConfirmDialog))
            dialog = app.screen

            # Verify dialog elements: file list, counts, warnings
            assert 'Selected turn (3 files · 5 edits)' in content(dialog.query_one('#turn-undo-target', Static))
            files_text = content(dialog.query_one('#turn-undo-files', Static))
            assert 'pkg/alpha.py (3 edits)' in files_text
            assert 'pkg/beta.py (1 edit)' in files_text
            assert 'pkg/gamma.py (1 edit)' in files_text

            warning_text = content(dialog.query_one('#turn-undo-warning', Static))
            assert 'Newer or conflicting changes cause refusal' in warning_text
            assert 'Shell effects are not undone' in warning_text
            assert 'Tests must run again' in warning_text
            assert 'Files restore one at a time' in warning_text

            # Default focus is on Cancel button
            await until(pilot, lambda: dialog.focused is not None)
            assert dialog.focused.id == 'turn-undo-cancel'

            # Default Enter cancels
            await pilot.press('enter')
            await until(pilot, lambda: app.screen is panel)
            assert len(undo_calls) == 0

            # Open again and press Escape -> cancels
            panel.query_one('#turn-changes-undo', Button).press()
            await until(pilot, lambda: isinstance(app.screen, TurnUndoConfirmDialog))
            await pilot.press('escape')
            await until(pilot, lambda: app.screen is panel)
            assert len(undo_calls) == 0

            # Open again and confirm
            panel.query_one('#turn-changes-undo', Button).press()
            await until(pilot, lambda: isinstance(app.screen, TurnUndoConfirmDialog))
            dialog = app.screen
            dialog.query_one('#turn-undo-confirm', Button).press()

            # Wait for undo completion
            await until(pilot, lambda: len(undo_calls) == 1)
            assert undo_calls[0] == (turn_id, 'plan-alpha')
            await until(pilot, lambda: 'undone successfully' in content(panel.query_one('#turn-changes-summary', Static)))

    asyncio.run(scenario())


def test_turn_undo_stale_preview_after_refresh_or_selection(tmp_path):
    project = tmp_path / 'project'
    session = tmp_path / 'custody'
    turn_id = 'inv-42'
    undo_calls = []

    def fake_reader(sess, *, project=None, invocation_id=None, **kwargs):
        return sample_turn_review(invocation_id=turn_id, undo_status='READY', undo_plan='plan-1')

    def fake_undoer(sess, *, project=None, invocation_id=None, expected_plan=None):
        undo_calls.append((invocation_id, expected_plan))
        return completed_result(invocation_id)

    async def scenario():
        app = App()
        async with app.run_test(size=(100, 30)) as pilot:
            panel = TurnChangesPanel(session, project, turn_id, reader=fake_reader, undoer=fake_undoer)
            app.push_screen(panel)
            await until(pilot, lambda: not panel.query_one('#turn-changes-undo', Button).disabled)

            # Open confirm dialog
            old_generation = panel.revision
            panel.query_one('#turn-changes-undo', Button).press()
            await until(pilot, lambda: isinstance(app.screen, TurnUndoConfirmDialog))
            dialog = app.screen
            assert panel.active_confirmation is dialog

            # Refresh invalidates outstanding confirmation dialog
            panel.action_reload()
            await until(pilot, lambda: panel.active_confirmation is None)

            # Confirming a stale dialog must not trigger undo
            panel.perform_undo_turn(turn_id,'plan-1',old_generation)
            await pilot.pause()
            assert len(undo_calls) == 0

            # Test selection invalidation
            await until(pilot, lambda: not panel.query_one('#turn-changes-undo', Button).disabled)
            old_generation = panel.revision
            panel.query_one('#turn-changes-undo', Button).press()
            await until(pilot, lambda: isinstance(app.screen, TurnUndoConfirmDialog))
            dialog2 = app.screen

            # Change selected file
            panel.query_one('#turn-changes-file', Select).value = 'f2'
            await until(pilot, lambda: panel.active_confirmation is None)
            panel.perform_undo_turn(turn_id,'plan-1',old_generation)
            await pilot.pause()
            assert len(undo_calls) == 0

    asyncio.run(scenario())


def test_exactly_one_mutator_during_blocked_call_and_controls_disabled(tmp_path):
    project = tmp_path / 'project'
    session = tmp_path / 'custody'
    turn_id = 'inv-42'
    mutator_calls = []
    entered_event = threading.Event()
    release_event = threading.Event()

    def fake_reader(sess, *, project=None, invocation_id=None, **kwargs):
        return sample_turn_review(invocation_id=turn_id, undo_status='READY', undo_plan='plan-block')

    def blocking_undoer(sess, *, project=None, invocation_id=None, expected_plan=None):
        mutator_calls.append((invocation_id, expected_plan))
        entered_event.set()
        # Wait until test releases mutation
        release_event.wait(timeout=10.0)
        return completed_result(invocation_id)

    async def scenario():
        app = App()
        async with app.run_test(size=(100, 30)) as pilot:
            panel = TurnChangesPanel(session, project, turn_id, reader=fake_reader, undoer=blocking_undoer)
            app.push_screen(panel)
            await until(pilot, lambda: not panel.query_one('#turn-changes-undo', Button).disabled)

            # Confirm undo
            panel.query_one('#turn-changes-undo', Button).press()
            await until(pilot, lambda: isinstance(app.screen, TurnUndoConfirmDialog))
            app.screen.query_one('#turn-undo-confirm', Button).press()

            # Wait until mutator is entered and running
            await until(pilot, lambda: entered_event.is_set())
            assert panel.mutating

            # Verify mutating controls disable: buttons, selects, actions
            assert panel.query_one('#turn-changes-undo', Button).disabled
            assert panel.query_one('#turn-changes-refresh', Button).disabled
            assert panel.query_one('#turn-changes-open', Button).disabled
            assert panel.query_one('#turn-changes-close', Button).disabled
            assert panel.query_one('#turn-changes-copy', Button).disabled
            assert panel.query_one('#turn-changes-file', Select).disabled

            # Attempt repeat undo / refresh / close / file selection while mutating
            panel.request_undo()
            panel.action_reload()
            panel.action_close()
            assert app.screen is panel
            assert len(mutator_calls) == 1

            # Release blocking Event before waiting for serialized successor reads
            release_event.set()

            # Wait for mutation and successor reload to complete
            await until(pilot, lambda: not panel.mutating)
            assert len(mutator_calls) == 1
            assert 'undone successfully' in content(panel.query_one('#turn-changes-summary', Static))

    asyncio.run(scenario())


@pytest.mark.parametrize('status,reason', [
    ('PARTIAL', 'pkg/beta.py had conflicting modifications'),
    ('UNKNOWN', 'Snapshot custody connection interrupted'),
])
def test_partial_and_unknown_not_shown_as_complete(tmp_path, status, reason):
    project = tmp_path / 'project'
    session = tmp_path / 'custody'
    turn_id = 'inv-42'

    def fake_reader(sess, *, project=None, invocation_id=None, **kwargs):
        return sample_turn_review(invocation_id=turn_id, undo_status='READY', undo_plan='plan-part')

    def fake_undoer(sess, *, project=None, invocation_id=None, expected_plan=None):
        return {
            'status': status,
            'invocation_id': invocation_id,
            'files': [{'repository': 'repo', 'path': 'pkg/alpha.py', 'status': 'UNDONE'}],
            'reason': reason,
        }

    async def scenario():
        app = App()
        async with app.run_test(size=(100, 30)) as pilot:
            panel = TurnChangesPanel(session, project, turn_id, reader=fake_reader, undoer=fake_undoer)
            app.push_screen(panel)
            await until(pilot, lambda: not panel.query_one('#turn-changes-undo', Button).disabled)

            # Confirm undo
            panel.query_one('#turn-changes-undo', Button).press()
            await until(pilot, lambda: isinstance(app.screen, TurnUndoConfirmDialog))
            app.screen.query_one('#turn-undo-confirm', Button).press()

            # Wait until reload finishes
            await until(pilot, lambda: not panel.mutating)

            summary = content(panel.query_one('#turn-changes-summary', Static))
            assert 'undone successfully' not in summary
            assert f'Turn undo {status}: {reason}' in summary

    asyncio.run(scenario())


def test_turn_panel_error_handling(tmp_path):
    project = tmp_path / 'project'
    session = tmp_path / 'custody'
    turn_id = 'inv-42'

    def error_reader(sess, *, project=None, invocation_id=None, **kwargs):
        raise RuntimeError('Custody database locked')

    async def scenario():
        app = App()
        async with app.run_test(size=(100, 30)) as pilot:
            panel = TurnChangesPanel(session, project, turn_id, reader=error_reader)
            app.push_screen(panel)
            await until(pilot, lambda: 'Review unavailable: Custody database locked' in content(panel.query_one('#turn-changes-summary', Static)))
            assert panel.query_one('#turn-changes-undo', Button).disabled
            assert panel.query_one('#turn-changes-open', Button).disabled

    asyncio.run(scenario())


def test_turn_panel_undo_mutation_error_handling(tmp_path):
    project = tmp_path / 'project'
    session = tmp_path / 'custody'
    turn_id = 'inv-42'

    def fake_reader(sess, *, project=None, invocation_id=None, **kwargs):
        return sample_turn_review(invocation_id=turn_id, undo_status='READY', undo_plan='plan-err')

    def error_undoer(sess, *, project=None, invocation_id=None, expected_plan=None):
        raise ValueError('Stale undo plan: file was modified')

    async def scenario():
        app = App()
        async with app.run_test(size=(100, 30)) as pilot:
            panel = TurnChangesPanel(session, project, turn_id, reader=fake_reader, undoer=error_undoer)
            app.push_screen(panel)
            await until(pilot, lambda: not panel.query_one('#turn-changes-undo', Button).disabled)

            panel.query_one('#turn-changes-undo', Button).press()
            await until(pilot, lambda: isinstance(app.screen, TurnUndoConfirmDialog))
            app.screen.query_one('#turn-undo-confirm', Button).press()

            await until(pilot, lambda: not panel.mutating)
            summary = content(panel.query_one('#turn-changes-summary', Static))
            assert 'Undo failed: Stale undo plan: file was modified' in summary
            assert not panel.query_one('#turn-changes-refresh', Button).disabled

    asyncio.run(scenario())
