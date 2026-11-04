"""Real panel actions over real Kernel edits and saved Runtime journals."""
import asyncio

import pytest
from textual.app import App
from textual.widgets import Button, Select, Static

from relay.changes_panel import ChangesPanel, FileViewer, UndoConfirmDialog
from relay.native_review import review
from test_native_review_actions import edited, setup
from test_changes_actions import content, wait_for, sample_report


@pytest.mark.parametrize('size', [(80,24), (130,44)])
@pytest.mark.parametrize('newer_change', [False, True])
def test_actual_review_open_confirm_restore_and_reopen(edited, size, newer_change):
    _, custody, root, effect, before, client = edited
    recorded_calls = len(client.calls)
    journal = (custody/'journal.jsonl').read_bytes()
    async def scenario():
        app = App()
        async with app.run_test(size=size) as pilot:
            panel = ChangesPanel(custody, root)
            app.push_screen(panel)
            assert await wait_for(pilot, lambda: panel.diff_text is not None)
            panel.query_one('#changes-open', Button).press()
            assert await wait_for(pilot, lambda: isinstance(app.screen, FileViewer))
            assert 'value = 2' in app.screen.content
            await pilot.press('escape')
            assert await wait_for(pilot, lambda: app.screen is panel)
            panel.query_one('#changes-undo', Button).press()
            # The screen switches before its buttons mount; a loaded runner can query in between.
            assert await wait_for(pilot, lambda: isinstance(app.screen, UndoConfirmDialog)
                                  and len(app.screen.query('#undo-confirm')) == 1)
            if newer_change: (root/'a.py').write_bytes(b'# newer user note\r\nvalue = 3\r\n')
            app.screen.query_one('#undo-confirm', Button).press()
            assert await wait_for(pilot, lambda: app.screen is panel and ('Undo failed:' in content(panel.query_one('#changes-summary',Static)) or panel.restoration_result is not None))
            if newer_change:
                assert (root/'a.py').read_bytes() == b'# newer user note\r\nvalue = 3\r\n'
                assert 'newer changes were preserved' in content(panel.query_one('#changes-summary', Static))
            else:
                assert (root/'a.py').read_bytes() == before
                assert await wait_for(pilot, lambda: panel.rows.get(effect, {}).get('undo_status') == 'UNDONE')
            await pilot.press('escape')
            assert await wait_for(pilot, lambda: app.screen is not panel)
            if not newer_change:
                reopened = ChangesPanel(custody, root)
                app.push_screen(reopened)
                assert await wait_for(pilot, lambda: reopened.diff_text is not None)
                assert reopened.query_one('#changes-undo', Button).disabled
                assert reopened.rows[effect]['undo_status'] == 'UNDONE'
    asyncio.run(scenario())
    assert len(client.calls) == recorded_calls
    assert (custody/'journal.jsonl').read_bytes() == journal


def test_selection_change_cannot_use_previous_displayed_diff(tmp_path):
    async def scenario():
        app = App()
        async with app.run_test(size=(80,24)) as pilot:
            report = sample_report()
            report['edits'].append({**report['edits'][0], 'effect_id':'other', 'path':'other.py'})
            panel = ChangesPanel(tmp_path, tmp_path, reader=lambda *a, **k: report)
            app.push_screen(panel)
            assert await wait_for(pilot, lambda: panel.diff_text is not None)
            # Reactive value changes before the queued Select.Changed handler.
            panel.query_one('#changes-file', Select).value = 'other'
            panel.request_undo()
            assert panel.active_confirmation is None
    asyncio.run(scenario())


def test_enter_defaults_to_cancel_and_confirmation_cannot_stack(tmp_path):
    async def scenario():
        app = App()
        calls = []
        async with app.run_test(size=(80,24)) as pilot:
            panel = ChangesPanel(tmp_path, tmp_path, reader=lambda *a, **k: sample_report(),
                                 undoer=lambda *a, **k: calls.append(k))
            app.push_screen(panel)
            assert await wait_for(pilot, lambda: panel.diff_text is not None)
            panel.request_undo()
            # The screen switches before its buttons mount; a loaded runner can query in between.
            assert await wait_for(pilot, lambda: isinstance(app.screen, UndoConfirmDialog)
                                  and len(app.screen.query('#undo-confirm')) == 1)
            dialog = app.screen
            panel.request_undo()
            assert panel.active_confirmation is dialog
            await pilot.press('enter')
            assert await wait_for(pilot, lambda: app.screen is panel)
            assert calls == []
    asyncio.run(scenario())
