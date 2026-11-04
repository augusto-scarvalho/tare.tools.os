"""Public Usage withdrawal actions with synthetic owner inputs only."""
import asyncio
import json

from textual.widgets import Button, Input, Static

from relay import relay_mesh, work_run
from relay.delivery_review_panel import DeliveryReviewPanel
from test_delivery_evidence import FINAL, receipt, review
from test_completed_work_usage import owner, saved_work_rows
from test_behavioral_profiles import saved_audit
from test_inference_telemetry import save


def test_public_withdraw_reason_cancel_confirm_and_reopen(tmp_path, monkeypatch):
    from relay.native_console import WorkConsole
    from test_native_console import FakeBackend
    control, custody = owner(tmp_path)
    monkeypatch.setattr(relay_mesh, 'RELAY_ROOT', control)
    monkeypatch.setattr(work_run, 'lock_root_for', lambda *a, **kw: custody/'locks'/'W')
    monkeypatch.setattr('relay.flow_usage.completed_work_usage_rows', saved_work_rows)
    monkeypatch.setattr('relay.flow_usage.flow_usage_rows', lambda *a, **kw: [])
    saved = receipt(audit={'observation': saved_audit()})
    saved['delivery_review'] = review(saved)
    path = custody/FINAL/'run.json'; save(path, saved)
    before = path.read_bytes()

    async def scenario():
        backend = FakeBackend()
        app = WorkConsole(tmp_path, backend=backend, initial_mode='assistant')
        async with app.run_test(size=(130, 44)) as pilot:
            await app.workers.wait_for_complete()
            app.action_usage(); await pilot.pause()
            panel = app.screen
            panel.show_row(next(i for i, r in enumerate(panel.rows) if r['id'] == 'behavioral-profiles'))
            await pilot.pause(); await pilot.click('#workbench-review'); await pilot.pause()
            assert isinstance(app.screen, DeliveryReviewPanel)
            assert not app.screen.query_one('#review-withdraw', Button).disabled
            assert path.read_bytes() == before
            await pilot.click('#review-withdraw'); await pilot.pause()
            app.screen.query_one('#review-withdraw-reason', Input).value = 'Mistake cancelled'
            await pilot.click('#review-withdraw-cancel'); await pilot.pause()
            assert path.read_bytes() == before
            await pilot.click('#review-withdraw'); await pilot.pause()
            await pilot.click('#review-withdraw-confirm'); await pilot.pause()
            assert path.read_bytes() == before
            assert 'short withdrawal reason' in str(app.screen.query_one('#review-status', Static).render())
            # Textual suppresses repeated presses during the button's active effect.
            await pilot.pause(0.5)
            app.screen.query_one('#review-withdraw-reason', Input).value = 'Reviewed the wrong result'
            await pilot.click('#review-withdraw-confirm'); await pilot.pause()
            assert app.screen.query_one('#review-withdraw', Button).disabled
            assert 'Review withdrawn' in str(app.screen.query_one('#review-saved-state', Static).render())
            assert json.loads(path.read_bytes())['delivery_review']['withdrawal']['reason'] == 'Reviewed the wrong result'
            withdrawn = path.read_bytes()
            await pilot.press('escape'); await pilot.pause()
            assert 'Accepted without material rework: Unknown' in app.screen.current['body']
            assert 'Review withdrawn' in app.screen.current['body']
            await pilot.click('#workbench-review'); await pilot.pause()
            assert app.screen.query_one('#review-withdraw', Button).disabled
            assert 'Reviewed the wrong result' in str(app.screen.query_one('#review-saved-state', Static).render())
            await pilot.press('escape'); await pilot.pause()
            assert path.read_bytes() == withdrawn and not backend.launches
    asyncio.run(scenario())
