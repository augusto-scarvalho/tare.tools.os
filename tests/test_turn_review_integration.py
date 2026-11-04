import asyncio

import pytest
from textual.app import App
from textual.containers import VerticalScroll
from textual.widgets import Button,Select,Static

from relay.changes_panel import ChangesPanel
from relay.turn_changes_panel import TurnChangesPanel,TurnUndoConfirmDialog
from relay import native_turn_review
from test_native_turn_review import setup,saved_turn
from test_turn_changes_panel import until,content,sample_turn_review


@pytest.mark.parametrize('size', [(80,24), (130,44)])
def test_large_turn_confirmation_supports_keyboard_file_review(size):
    async def scenario():
        app=App()
        async with app.run_test(size=size) as pilot:
            files=[{'repository':'repo','path':f'file-{i:02}.py','edit_count':1} for i in range(20)]
            dialog=TurnUndoConfirmDialog(invocation_id='turn',files=files,file_count=20,edit_count=20,plan='plan')
            app.push_screen(dialog)
            await until(pilot,lambda:dialog.is_mounted and
                bool(dialog.query('#turn-undo-confirm')) and
                dialog.query_one('#turn-undo-confirm',Button).region.height > 0)
            assert app.focused is dialog.query_one('#turn-undo-cancel',Button)
            scroll=dialog.query_one('#turn-undo-file-scroll',VerticalScroll)
            scroll.focus()
            await pilot.pause()
            assert app.focused is scroll and scroll.max_scroll_y > 0
            await pilot.press('end')
            await until(pilot,lambda:scroll.scroll_y == scroll.max_scroll_y)
            assert scroll.scroll_y > 0 and scroll.scroll_y == scroll.max_scroll_y
            for selector in ('#turn-undo-confirm','#turn-undo-cancel'):
                button=dialog.query_one(selector,Button)
                assert button.region.bottom <= size[1] and button.region.height == 1
                assert str(button.label) in button.render_line(0).text
            await pilot.press('escape')
            assert app.screen is not dialog
    asyncio.run(scenario())


@pytest.mark.parametrize('size',[(80,24),(130,44)])
@pytest.mark.parametrize('outcome',['success','conflict','partial'])
def test_real_turn_review_from_changes_and_durable_result(saved_turn,monkeypatch,size,outcome):
    custody,root,turn,before,client=saved_turn
    journal=(custody/'journal.jsonl').read_bytes(); calls=len(client.calls)
    after=(root/'a.py').read_bytes()
    if outcome=='partial':
        owner=native_turn_review.restore_bytes
        def fail(path,before,after,*,before_write):
            if path.name=='b.txt':
                before_write(); raise OSError('injected interruption')
            return owner(path,before,after,before_write=before_write)
        monkeypatch.setattr(native_turn_review,'restore_bytes',fail)
    async def scenario():
        app=App()
        async with app.run_test(size=size) as pilot:
            edits=ChangesPanel(custody,root); app.push_screen(edits)
            await until(pilot,lambda:edits.diff_text is not None)
            edits.query_one('#changes-turn',Select).value=turn
            await until(pilot,lambda:edits.turn==turn and edits.diff_text is not None)
            edits.query_one('#changes-review-turn',Button).press()
            await until(pilot,lambda:isinstance(app.screen,TurnChangesPanel))
            panel=app.screen
            await until(pilot,lambda:panel.diff_text is not None and panel.undo_preview.get('status')=='READY')
            assert len(panel.files)==2 and '-value = 1' in panel.diff_text and '+value = 3' in panel.diff_text
            panel.query_one('#turn-changes-undo',Button).press()
            await until(pilot,lambda:isinstance(app.screen,TurnUndoConfirmDialog))
            dialog=app.screen
            # push_screen() publishes the modal before its async mount/layout completes.
            # Synchronize on usable geometry, not merely app.screen identity, before
            # asserting terminal dimensions or interacting with the confirmation.
            await until(pilot,lambda:dialog.is_mounted and
                dialog.query_one('#turn-undo-confirm',Button).region.height > 0)
            assert '2 files' in content(dialog.query_one('#turn-undo-target',Static))
            assert '3 edits' in content(dialog.query_one('#turn-undo-target',Static))
            confirm=dialog.query_one('#turn-undo-confirm',Button)
            assert confirm.region.bottom <= size[1] and confirm.region.height==1
            assert str(confirm.label) in confirm.render_line(0).text
            if outcome=='conflict':(root/'b.txt').write_bytes(b'newer user file\r\n')
            confirm.press()
            await until(pilot,lambda:panel.mutation_result is not None and not panel.mutating)
            if outcome=='success':
                await until(pilot,lambda:panel.undo_preview.get('status')=='UNDONE')
                assert (root/'a.py').read_bytes()==before and not (root/'b.txt').exists()
            elif outcome=='conflict':
                assert (root/'a.py').read_bytes()==after
                assert (root/'b.txt').read_bytes()==b'newer user file\r\n'
                assert 'undone successfully' not in panel.mutation_result
            else:
                await until(pilot,lambda:panel.undo_preview.get('status')=='PARTIAL')
                assert 'PARTIAL' in panel.mutation_result and 'b.txt: UNKNOWN' in panel.mutation_result
                assert (root/'a.py').read_bytes()==before and (root/'b.txt').exists()
            await pilot.press('escape')
            await until(pilot,lambda:app.screen is edits and edits.diff_text is not None)
            assert edits.turn==turn
    asyncio.run(scenario())
    assert (custody/'journal.jsonl').read_bytes()==journal and len(client.calls)==calls


def test_refresh_failure_discards_old_undo_authority(tmp_path):
    fail=False
    def reader(*args,**kwargs):
        if fail:raise ValueError('review disconnected')
        return sample_turn_review()
    async def scenario():
        nonlocal fail
        app=App()
        async with app.run_test(size=(80,24)) as pilot:
            panel=TurnChangesPanel(tmp_path,tmp_path,'inv-42',reader=reader)
            app.push_screen(panel); await until(pilot,lambda:bool(panel.undo_preview))
            fail=True; panel.action_reload()
            await until(pilot,lambda:'disconnected' in content(panel.query_one('#turn-changes-summary',Static)))
            assert panel.undo_preview=={} and panel.query_one('#turn-changes-undo',Button).disabled
            panel.request_undo(); assert panel.active_confirmation is None
    asyncio.run(scenario())


def test_incomplete_success_receipt_cannot_claim_whole_turn_completion(tmp_path):
    async def scenario():
        app=App()
        async with app.run_test(size=(80,24)) as pilot:
            panel=TurnChangesPanel(tmp_path,tmp_path,'inv-42',reader=lambda *a,**k:sample_turn_review(),
                undoer=lambda *a,**k:{'status':'UNDONE','invocation_id':'inv-42','files':[]})
            app.push_screen(panel); await until(pilot,lambda:bool(panel.undo_preview))
            panel.request_undo();await until(pilot,lambda:isinstance(app.screen,TurnUndoConfirmDialog))
            app.screen.query_one('#turn-undo-confirm',Button).press()
            await until(pilot,lambda:panel.mutation_result is not None)
            assert 'unconfirmed' in panel.mutation_result
            assert 'undone successfully' not in panel.mutation_result
            assert panel.query_one('#turn-changes-undo',Button).disabled
    asyncio.run(scenario())
