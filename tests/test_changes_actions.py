"""Tests for ChangesPanel actions (Open file, Undo edit, stale guards, and safe in-TUI viewer)."""
import asyncio
from pathlib import Path
import threading
import xml.etree.ElementTree as ET

import pytest
from textual.app import App
from textual.widgets import Button, Select, Static

from relay.changes_panel import ChangesPanel, FileViewer, UndoConfirmDialog


def content(widget):
    return str(getattr(widget, 'content', widget.render()))


async def wait_for(pilot, predicate, attempts=60, step=0.03):
    for _ in range(attempts):
        await pilot.pause(step)
        if predicate():
            return True
    return predicate()


def sample_report(*, edits=None, checks=None, turns=None, unresolved=None, changed=None, unavailable=None):
    if edits is None:
        edits = [
            {
                'effect_id': 'eff-001',
                'repository': 'repo',
                'path': 'src/app.py',
                'status': 'succeeded',
                'diff': '--- before/src/app.py\n+++ after/src/app.py\n@@ -1 +1 @@\n-old_val = 1\n+new_val = 2\n',
            }
        ]
    if checks is None:
        checks = [
            {
                'name': 'pytest',
                'status': 'succeeded',
                'returncode': 0,
                'freshness': 'OBSERVED_FILES_MATCH',
                'observed_files': 1,
            }
        ]
    if turns is None:
        turns = [{'id': 'turn-1', 'status': 'succeeded'}]
    return {
        'session_id': 'sess-1',
        'session': '/tmp/sess',
        'task': 'Update app config',
        'edits': edits,
        'checks': checks,
        'turns': turns,
        'unresolved_effects': unresolved or [],
        'changed_since_observation': changed or [],
        'unavailable_files': unavailable or [],
        'diff_scope': 'Each diff is one confirmed session edit.',
    }


@pytest.mark.parametrize('size', [(80, 24), (130, 44)])
def test_sizes_and_layout(tmp_path, size):
    session = tmp_path / 'session'
    project = tmp_path / 'project'

    def fake_reader(sess, **kwargs):
        return sample_report()

    async def scenario():
        app = App()
        async with app.run_test(size=size) as pilot:
            panel = ChangesPanel(session, project, reader=fake_reader)
            app.push_screen(panel)
            ok = await wait_for(pilot, lambda: '+new_val = 2' in content(panel.query_one('#changes-diff', Static)))
            assert ok
            assert panel.query_one('#changes-close').region.bottom <= size[1]
            for button_id in ('changes-close', 'changes-open', 'changes-undo', 'changes-refresh', 'changes-copy'):
                btn = panel.query_one('#' + button_id, Button)
                assert panel.region.contains_region(btn.region), f"Button {button_id} out of bounds"
                assert str(btn.label) in btn.render_line(0).text, f"Label {btn.label} clipped"

    asyncio.run(scenario())


@pytest.mark.parametrize('size', [(80, 24), (130, 44)])
def test_file_viewer_open_copy_and_return(tmp_path, size):
    session = tmp_path / 'session'
    project = tmp_path / 'project'
    file_reader_calls = []

    def fake_reader(sess, **kwargs):
        return sample_report()

    def fake_file_reader(sess, *, project, effect_id, invocation_id=None):
        file_reader_calls.append({'session': sess, 'project': project, 'effect_id': effect_id, 'invocation_id': invocation_id})
        return {
            'path': 'src/app.py',
            'content': 'new_val = 2\n# current content on disk\n',
            'sha256': 'abc123def456',
        }

    async def scenario():
        app = App()
        copies = []
        app.copy_to_clipboard = copies.append
        async with app.run_test(size=size) as pilot:
            panel = ChangesPanel(session, project, reader=fake_reader, file_reader=fake_file_reader)
            app.push_screen(panel)
            await wait_for(pilot, lambda: '+new_val = 2' in content(panel.query_one('#changes-diff', Static)))

            open_btn = panel.query_one('#changes-open', Button)
            assert not open_btn.disabled
            open_btn.press()

            # Wait for FileViewer to be pushed
            await wait_for(pilot, lambda: isinstance(app.screen, FileViewer) and bool(app.screen.query('#file-viewer-back')))
            viewer = app.screen
            assert isinstance(viewer, FileViewer)

            # Check path, notice, and content
            assert 'app.py' in content(viewer.query_one('#file-viewer-heading', Static))
            assert 'src/app.py' in content(viewer.query_one('#file-viewer-notice', Static))
            assert 'Current file content on disk' in content(viewer.query_one('#file-viewer-notice', Static))
            assert 'new_val = 2' in content(viewer.query_one('#file-viewer-content', Static))

            # Test Copy content
            copy_btn = viewer.query_one('#file-viewer-copy', Button)
            copy_btn.press()
            await pilot.pause()
            assert copies == ['new_val = 2\n# current content on disk\n']
            assert str(copy_btn.label) == 'Copied'

            # Test Back navigation via Esc
            await pilot.press('escape')
            await wait_for(pilot, lambda: app.screen is panel)
            assert app.screen is panel

            # Verify diff selection and scroll preserved
            assert '+new_val = 2' in content(panel.query_one('#changes-diff', Static))
            assert panel.query_one('#changes-file', Select).value == 'eff-001'

            # Open again and test back navigation via Back button
            panel.query_one('#changes-open', Button).press()
            await wait_for(pilot, lambda: isinstance(app.screen, FileViewer) and bool(app.screen.query('#file-viewer-back')))
            viewer2 = app.screen
            viewer2.query_one('#file-viewer-back', Button).press()
            await wait_for(pilot, lambda: app.screen is panel)
            assert app.screen is panel

    asyncio.run(scenario())


def test_file_viewer_error_handling(tmp_path):
    session = tmp_path / 'session'
    project = tmp_path / 'project'

    def fake_reader(sess, **kwargs):
        return sample_report()

    def fake_file_reader(sess, *, project, effect_id, invocation_id=None):
        raise ValueError('File exceeds owner size limit')

    async def scenario():
        app = App()
        async with app.run_test(size=(80, 24)) as pilot:
            panel = ChangesPanel(session, project, reader=fake_reader, file_reader=fake_file_reader)
            app.push_screen(panel)
            await wait_for(pilot, lambda: '+new_val = 2' in content(panel.query_one('#changes-diff', Static)))

            panel.query_one('#changes-open', Button).press()
            await wait_for(pilot, lambda: 'File unavailable: File exceeds owner size limit' in content(panel.query_one('#changes-summary', Static)))

            # FileViewer must NOT be pushed
            assert app.screen is panel
            assert 'File unavailable: File exceeds owner size limit' in content(panel.query_one('#changes-summary', Static))

    asyncio.run(scenario())


def test_file_viewer_late_read_discarded(tmp_path):
    session = tmp_path / 'session'
    project = tmp_path / 'project'
    read_event = threading.Event()
    continue_event = threading.Event()

    def fake_reader(sess, **kwargs):
        return sample_report(edits=[
            {'effect_id': 'eff-001', 'repository': 'repo', 'path': 'src/a.py', 'status': 'succeeded', 'diff': 'diff-a'},
            {'effect_id': 'eff-002', 'repository': 'repo', 'path': 'src/b.py', 'status': 'succeeded', 'diff': 'diff-b'},
        ])

    def slow_file_reader(sess, *, project, effect_id, invocation_id=None):
        read_event.set()
        continue_event.wait(timeout=5.0)
        return {'path': 'src/a.py', 'content': 'content-a', 'sha256': 'sha'}

    async def scenario():
        app = App()
        async with app.run_test(size=(80, 24)) as pilot:
            panel = ChangesPanel(session, project, reader=fake_reader, file_reader=slow_file_reader)
            app.push_screen(panel)
            await wait_for(pilot, lambda: 'diff-a' in content(panel.query_one('#changes-diff', Static)))

            # Click open file (starts slow read in background)
            panel.query_one('#changes-open', Button).press()
            await wait_for(pilot, lambda: read_event.is_set())

            # Change file selection before read finishes
            panel.query_one('#changes-file', Select).value = 'eff-002'
            await wait_for(pilot, lambda: 'diff-b' in content(panel.query_one('#changes-diff', Static)))

            # Allow slow read to return
            continue_event.set()
            await pilot.pause(.1)

            # FileViewer must NOT be pushed because selection changed
            assert app.screen is panel

    asyncio.run(scenario())


def test_undo_confirm_and_cancel(tmp_path):
    session = tmp_path / 'session'
    project = tmp_path / 'project'
    undo_calls = []

    def fake_reader(sess, **kwargs):
        return sample_report()

    def fake_undoer(sess, *, project, effect_id, invocation_id=None):
        undo_calls.append({'session': sess, 'project': project, 'effect_id': effect_id, 'invocation_id': invocation_id})
        return {'status': 'UNDONE', 'effect_id': effect_id, 'path': 'src/app.py'}

    async def scenario():
        app = App()
        async with app.run_test(size=(80, 24)) as pilot:
            panel = ChangesPanel(session, project, reader=fake_reader, undoer=fake_undoer)
            app.push_screen(panel)
            await wait_for(pilot, lambda: '+new_val = 2' in content(panel.query_one('#changes-diff', Static)))

            # 1. Request undo and Cancel
            panel.query_one('#changes-undo', Button).press()
            # A screen enters the stack before Textual mounts its child controls.
            await wait_for(pilot, lambda: isinstance(app.screen, UndoConfirmDialog)
                           and bool(app.screen.query('#undo-confirm')))
            dialog = app.screen
            assert 'repo/src/app.py' in content(dialog.query_one('#undo-confirm-target', Static))
            warning_text = content(dialog.query_one('#undo-confirm-warning', Static))
            assert 'Only this confirmed file-tool edit will be restored' in warning_text
            assert 'Newer changes cause refusal' in warning_text
            assert 'Shell effects are not undone' in warning_text
            assert 'Tests must run again' in warning_text

            dialog.query_one('#undo-cancel', Button).press()
            await wait_for(pilot, lambda: app.screen is panel)
            assert len(undo_calls) == 0  # Cancellation makes no call!

            # 2. Request undo and Confirm
            panel.query_one('#changes-undo', Button).press()
            # A screen enters the stack before Textual mounts its child controls.
            await wait_for(pilot, lambda: isinstance(app.screen, UndoConfirmDialog)
                           and bool(app.screen.query('#undo-confirm')))
            dialog2 = app.screen
            dialog2.query_one('#undo-confirm', Button).press()

            # Wait for undo to complete and panel to refresh
            await wait_for(pilot, lambda: app.screen is panel and panel.restoration_result is not None)
            assert len(undo_calls) == 1
            assert undo_calls[0]['effect_id'] == 'eff-001'
            assert undo_calls[0]['project'] == project
            assert 'Restored src/app.py' in panel.restoration_result
            assert 'Restoration result: Restored src/app.py' in content(panel.query_one('#changes-summary', Static))

    asyncio.run(scenario())


def test_undo_stale_confirmation_cancelled_on_selection_change(tmp_path):
    session = tmp_path / 'session'
    project = tmp_path / 'project'
    undo_calls = []

    def fake_reader(sess, **kwargs):
        return sample_report(edits=[
            {'effect_id': 'eff-001', 'repository': 'repo', 'path': 'src/a.py', 'status': 'succeeded', 'diff': 'diff-a'},
            {'effect_id': 'eff-002', 'repository': 'repo', 'path': 'src/b.py', 'status': 'succeeded', 'diff': 'diff-b'},
        ])

    def fake_undoer(sess, *, project, effect_id, invocation_id=None):
        undo_calls.append(effect_id)
        return {'status': 'UNDONE', 'effect_id': effect_id, 'path': 'src/a.py'}

    async def scenario():
        app = App()
        async with app.run_test(size=(80, 24)) as pilot:
            panel = ChangesPanel(session, project, reader=fake_reader, undoer=fake_undoer)
            app.push_screen(panel)
            await wait_for(pilot, lambda: 'diff-a' in content(panel.query_one('#changes-diff', Static)))

            # Click undo for eff-001
            panel.query_one('#changes-undo', Button).press()
            # A screen enters the stack before Textual mounts its child controls.
            await wait_for(pilot, lambda: isinstance(app.screen, UndoConfirmDialog)
                           and bool(app.screen.query('#undo-confirm')))

            # Change file selection before confirming
            panel.query_one('#changes-file', Select).value = 'eff-002'
            await wait_for(pilot, lambda: app.screen is panel)

            # Confirm dialog should be dismissed and NO undo call made
            assert len(undo_calls) == 0

    asyncio.run(scenario())


def test_exactly_one_undo_while_operation_blocked(tmp_path):
    session = tmp_path / 'session'
    project = tmp_path / 'project'
    undo_started = threading.Event()
    undo_continue = threading.Event()
    undo_calls = []

    def fake_reader(sess, **kwargs):
        return sample_report()

    def slow_undoer(sess, *, project, effect_id, invocation_id=None):
        undo_calls.append(effect_id)
        undo_started.set()
        undo_continue.wait(timeout=5.0)
        return {'status': 'UNDONE', 'effect_id': effect_id, 'path': 'src/app.py'}

    async def scenario():
        app = App()
        async with app.run_test(size=(80, 24)) as pilot:
            panel = ChangesPanel(session, project, reader=fake_reader, undoer=slow_undoer)
            app.push_screen(panel)
            await wait_for(pilot, lambda: '+new_val = 2' in content(panel.query_one('#changes-diff', Static)))

            # Request undo and confirm
            panel.query_one('#changes-undo', Button).press()
            # A screen enters the stack before Textual mounts its child controls.
            await wait_for(pilot, lambda: isinstance(app.screen, UndoConfirmDialog)
                           and bool(app.screen.query('#undo-confirm')))
            app.screen.query_one('#undo-confirm', Button).press()

            # Wait until undo starts
            await wait_for(pilot, lambda: undo_started.is_set())
            assert panel.mutating is True

            # All actions and closing must be blocked
            assert panel.query_one('#changes-undo', Button).disabled
            assert panel.query_one('#changes-open', Button).disabled
            assert panel.query_one('#changes-refresh', Button).disabled
            assert panel.query_one('#changes-close', Button).disabled
            assert panel.query_one('#changes-turn', Select).disabled
            assert panel.query_one('#changes-file', Select).disabled

            # Attempt closing via Esc - must not close while mutating
            await pilot.press('escape')
            await pilot.pause(.05)
            assert app.screen is panel

            # Attempt duplicate undo press - must not call undoer again
            panel.query_one('#changes-undo', Button).press()
            await pilot.pause(.05)
            assert len(undo_calls) == 1

            # Release slow undoer
            undo_continue.set()
            await wait_for(pilot, lambda: panel.mutating is False)
            assert len(undo_calls) == 1  # Exactly one call!

            # Controls unblocked
            assert not panel.query_one('#changes-refresh', Button).disabled
            assert not panel.query_one('#changes-close', Button).disabled

    asyncio.run(scenario())


def test_confirmed_undo_unlocks_controls_before_a_queued_reload_and_cannot_repeat(tmp_path):
    session=tmp_path/'session'; project=tmp_path/'project'
    started=threading.Event(); release=threading.Event(); reloads=[]; calls=[]
    def reader(sess,**kwargs): return sample_report()
    def undoer(sess,**kwargs):
        calls.append(kwargs['effect_id']); started.set(); release.wait(timeout=5)
        return {'status':'UNDONE','effect_id':kwargs['effect_id'],'path':'src/app.py'}
    async def scenario():
        app=App()
        async with app.run_test(size=(80,24)) as pilot:
            panel=ChangesPanel(session,project,reader=reader,undoer=undoer)
            app.push_screen(panel)
            assert await wait_for(pilot,lambda:'+new_val = 2' in content(panel.query_one('#changes-diff',Static)))
            # Hold the refresh at its scheduling boundary, independently of CPU timing.
            panel.action_reload=lambda:reloads.append(True)
            panel.query_one('#changes-undo',Button).press()
            assert await wait_for(pilot,lambda:isinstance(app.screen,UndoConfirmDialog) and bool(app.screen.query('#undo-confirm')))
            app.screen.query_one('#undo-confirm',Button).press()
            try:
                assert await wait_for(pilot,started.is_set)
                assert panel.mutating and panel.query_one('#changes-refresh',Button).disabled
                release.set()
                assert await wait_for(pilot,lambda:not panel.mutating)
                assert reloads and len(calls)==1
                assert not panel.query_one('#changes-refresh',Button).disabled
                assert not panel.query_one('#changes-close',Button).disabled
                assert panel.query_one('#changes-undo',Button).disabled
                assert panel.rows['eff-001']['undo_status']=='UNDONE'
                panel.request_undo()
                await pilot.pause()
                assert app.screen is panel and len(calls)==1
            finally:
                release.set()
    asyncio.run(scenario())


@pytest.mark.parametrize('size', [(80,24),(132,44)], ids=['narrow','wide'])
@pytest.mark.parametrize('status', ['UNDONE','UNKNOWN'])
def test_long_file_paths_keep_the_current_status_visible(tmp_path,size,status):
    row={'effect_id':'long','repository':'repo','path':'nested/'*24+'value.txt',
        'status':'succeeded','undo_status':status,'diff':'recorded change'}
    async def scenario():
        app=App()
        async with app.run_test(size=size) as pilot:
            panel=ChangesPanel(tmp_path/'session',tmp_path/'project',
                reader=lambda *a,**k:sample_report(edits=[row]))
            app.push_screen(panel)
            assert await wait_for(pilot,lambda:panel.diff_text=='recorded change')
            visible=''.join(ET.fromstring(app.export_screenshot()).itertext())
            assert status in visible
            assert panel.query_one('#changes-undo',Button).disabled
    asyncio.run(scenario())


def test_undo_disabled_for_undo_status_and_errors(tmp_path):
    session = tmp_path / 'session'
    project = tmp_path / 'project'

    def fake_reader(sess, **kwargs):
        edits = [
            {'effect_id': 'eff-undone', 'repository': 'repo', 'path': 'src/a.py', 'status': 'succeeded', 'undo_status': 'UNDONE', 'diff': 'diff-undone'},
            {'effect_id': 'eff-unknown', 'repository': 'repo', 'path': 'src/b.py', 'status': 'succeeded', 'undo_status': 'UNKNOWN', 'diff': 'diff-unknown'},
            {'effect_id': 'eff-unavail', 'repository': 'repo', 'path': 'src/c.py', 'status': 'succeeded', 'undo_status': 'UNAVAILABLE', 'diff': 'diff-unavail'},
            {'effect_id': 'eff-failed', 'repository': 'repo', 'path': 'src/d.py', 'status': 'failed', 'diff': 'diff-failed'},
            {'effect_id': 'eff-nodiff', 'repository': 'repo', 'path': 'src/e.py', 'status': 'succeeded', 'diff': None, 'diff_unavailable': 'No snapshot'},
        ]
        return sample_report(edits=edits)

    async def scenario():
        app = App()
        async with app.run_test(size=(80, 24)) as pilot:
            panel = ChangesPanel(session, project, reader=fake_reader)
            app.push_screen(panel)
            assert await wait_for(pilot, lambda: bool(panel.query('#changes-file')))
            file_select = panel.query_one('#changes-file', Select)
            undo_btn = panel.query_one('#changes-undo', Button)

            # Test UNDONE
            await wait_for(pilot, lambda: 'diff-undone' in content(panel.query_one('#changes-diff', Static)))
            assert undo_btn.disabled

            # Test UNKNOWN
            file_select.value = 'eff-unknown'
            await wait_for(pilot, lambda: 'diff-unknown' in content(panel.query_one('#changes-diff', Static)))
            assert undo_btn.disabled

            # Test UNAVAILABLE
            file_select.value = 'eff-unavail'
            await wait_for(pilot, lambda: 'diff-unavail' in content(panel.query_one('#changes-diff', Static)))
            assert undo_btn.disabled

            # Test failed status
            file_select.value = 'eff-failed'
            await wait_for(pilot, lambda: 'diff-failed' in content(panel.query_one('#changes-diff', Static)))
            assert undo_btn.disabled

            # Test diff unavailable
            file_select.value = 'eff-nodiff'
            await wait_for(pilot, lambda: 'Diff unavailable' in content(panel.query_one('#changes-diff', Static)))
            assert undo_btn.disabled

    asyncio.run(scenario())


def test_undo_failure_reports_no_unproven_success_and_allows_refresh(tmp_path):
    session = tmp_path / 'session'
    project = tmp_path / 'project'

    def fake_reader(sess, **kwargs):
        return sample_report()

    def failing_undoer(sess, *, project, effect_id, invocation_id=None):
        raise RuntimeError('File modified externally')

    async def scenario():
        app = App()
        async with app.run_test(size=(80, 24)) as pilot:
            panel = ChangesPanel(session, project, reader=fake_reader, undoer=failing_undoer)
            app.push_screen(panel)
            await wait_for(pilot, lambda: '+new_val = 2' in content(panel.query_one('#changes-diff', Static)))

            panel.query_one('#changes-undo', Button).press()
            # A screen enters the stack before Textual mounts its child controls.
            await wait_for(pilot, lambda: isinstance(app.screen, UndoConfirmDialog)
                           and bool(app.screen.query('#undo-confirm')))
            app.screen.query_one('#undo-confirm', Button).press()

            # Wait for failure
            await wait_for(pilot, lambda: 'Undo failed: File modified externally' in content(panel.query_one('#changes-summary', Static)))
            assert panel.restoration_result is None  # No unproven success
            assert panel.mutating is False

            # Refresh must be allowed
            refresh_btn = panel.query_one('#changes-refresh', Button)
            assert not refresh_btn.disabled
            refresh_btn.press()
            await wait_for(pilot, lambda: '1 recorded edit' in content(panel.query_one('#changes-summary', Static)))

    asyncio.run(scenario())


def test_load_worker_stale_guards(tmp_path):
    session = tmp_path / 'session'
    project = tmp_path / 'project'
    slow_reload_event = threading.Event()
    continue_slow_reload = threading.Event()
    call_counts = {'fast': 0, 'slow': 0}

    def controlled_reader(sess, **kwargs):
        if kwargs.get('invocation_id') == 'turn-slow':
            call_counts['slow'] += 1
            slow_reload_event.set()
            continue_slow_reload.wait(timeout=5.0)
            return sample_report(edits=[
                {'effect_id': 'eff-stale', 'repository': 'repo', 'path': 'src/stale.py', 'status': 'succeeded', 'diff': 'diff-stale'}
            ])
        call_counts['fast'] += 1
        return sample_report(edits=[
            {'effect_id': 'eff-fresh', 'repository': 'repo', 'path': 'src/fresh.py', 'status': 'succeeded', 'diff': 'diff-fresh'}
        ], turns=[{'id': 'turn-fast', 'status': 'succeeded'}, {'id': 'turn-slow', 'status': 'succeeded'}])

    async def scenario():
        app = App()
        async with app.run_test(size=(80, 24)) as pilot:
            panel = ChangesPanel(session, project, reader=controlled_reader)
            app.push_screen(panel)
            await wait_for(pilot, lambda: 'diff-fresh' in content(panel.query_one('#changes-diff', Static)))

            # Trigger slow reload by setting turn to 'turn-slow'
            panel.turn = 'turn-slow'
            panel.action_reload()
            await wait_for(pilot, lambda: slow_reload_event.is_set())

            # Trigger fast reload with 'turn-fast' while slow reload is still waiting
            panel.turn = 'turn-fast'
            panel.action_reload()
            # Reads deliberately serialize, so release the older read before
            # waiting for the newer selection to finish.
            continue_slow_reload.set()
            assert await wait_for(pilot, lambda: 'diff-fresh' in content(panel.query_one('#changes-diff', Static)))

            # Stale response must be discarded; diff-stale must NOT overwrite diff-fresh
            assert 'diff-fresh' in content(panel.query_one('#changes-diff', Static))
            assert panel.query_one('#changes-file', Select).value == 'eff-fresh'

    asyncio.run(scenario())


def test_old_row_schema_compatibility(tmp_path):
    session = tmp_path / 'session'
    project = tmp_path / 'project'

    def old_schema_reader(sess, **kwargs):
        # Old schema has no 'undo_status' and no 'turns'
        return {
            'session_id': 'sess-old',
            'session': str(session),
            'task': 'old schema task',
            'edits': [
                {
                    'effect_id': 'eff-old',
                    'repository': 'repo',
                    'path': 'old.py',
                    'status': 'succeeded',
                    'diff': '+old = True\n',
                }
            ],
            'checks': [],
            'unresolved_effects': [],
            'changed_since_observation': [],
            'unavailable_files': [],
            'diff_scope': 'scope',
        }

    async def scenario():
        app = App()
        async with app.run_test(size=(80, 24)) as pilot:
            panel = ChangesPanel(session, project, reader=old_schema_reader)
            app.push_screen(panel)
            await wait_for(pilot, lambda: '+old = True' in content(panel.query_one('#changes-diff', Static)))

            # Undo must be enabled because status == 'succeeded' and diff is displayed and undo_status is absent
            undo_btn = panel.query_one('#changes-undo', Button)
            assert not undo_btn.disabled

            # Open file button must also be enabled
            open_btn = panel.query_one('#changes-open', Button)
            assert not open_btn.disabled

    asyncio.run(scenario())
