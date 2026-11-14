"""Keyboard access and primary action visibility at ordinary terminal sizes."""
import asyncio
import pytest
from textual.widgets import Button, Collapsible
from relay.native_console import WorkConsole
from test_mutation_recovery import LocalBackend


@pytest.mark.parametrize('size', [(140, 48), (120, 40), (80, 24)])
def test_primary_actions_visible_and_workspace_accessible_by_keyboard(tmp_path, size):
    async def scenario():
        app = WorkConsole(tmp_path, backend=LocalBackend(), initial_mode='conversation')
        async with app.run_test(size=size) as pilot:
            await pilot.pause(); await app.workers.wait_for_complete()
            assert app.query_one('#model-settings', Collapsible).collapsed
            start = app.query_one('#start', Button)
            assert start.region.width >= 10
            assert start.region.y >= 0 and start.region.bottom <= size[1]-1
            if size[0] < 100:
                assert not app.query_one('#selection').display
                await pilot.press('f2'); await pilot.pause()
                assert app.query_one('#selection').display
                await pilot.press('f2'); await pilot.pause()
                assert not app.query_one('#selection').display
            await pilot.press('f6'); await app.workers.wait_for_complete()
            assert app.demand_id is None and app.process is None
            assert not app.busy
    asyncio.run(scenario())


@pytest.mark.parametrize('size', [(130, 44), (80, 24)])
@pytest.mark.parametrize('mode', ['parallel', 'plan', 'work', 'sequence', 'research', 'conversation', 'session'])
def test_flow_layout_keeps_setup_drafts_and_review_actions_accessible(tmp_path, monkeypatch, size, mode):
    from textual.widgets import Select, Static, TextArea
    from test_native_console import FakeBackend
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'history'))

    async def scenario():
        app = WorkConsole(tmp_path, backend=FakeBackend(), initial_mode='parallel')
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            app.query_one('#mode', Select).value = mode
            await pilot.pause()
            prompt = app.query_one('#prompt', TextArea)
            prompt.load_text('Keep this unfinished task.\nAnd its second line.')
            await pilot.pause()
            assert not app.query_one('#selection').display
            assert not app.query_one('#session-context').display
            assert app.query_one('#output').region.height >= 8
            assert str(app.query_one('#toolbar-settings', Button).label) == 'Setup'
            start = app.query_one('#start', Button)
            assert start.region.height == 1
            assert 0 <= start.region.y < size[1]
            if mode != 'conversation':
                assert '/ commands' not in str(app.query_one('#composer-hint', Static).render())
            await pilot.click('#toolbar-settings')
            # Opening Setup focuses the workspace and schedules a scroll. CPU
            # idle alone may precede that animation's final layout on Windows.
            await pilot.wait_for_scheduled_animations()
            assert app.query_one('#selection').display
            assert app.query_one('#flow-progress', Collapsible).collapsed
            for box in app.query('#plan-options, #sequence-options, #parallel-options, #research-options, #findings-options'):
                if box.display:
                    children = [child for child in box.children if child.display]
                    assert all(child.region.bottom <= box.region.bottom for child in children)
                    children[-1].scroll_visible(animate=False, force=True)
                    await pilot.wait_for_scheduled_animations()
                    for _ in range(100):  # the scroll's layout can land after the animations report idle
                        if children[-1].region.bottom <= size[1]:
                            break
                        await pilot.pause(0.05)
                    assert 0 <= children[-1].region.y < children[-1].region.bottom <= size[1]
            if size[0] < 100:
                assert not app.query_one('#detail').display
            await pilot.click('#settings-close'); await pilot.pause()
            assert not app.query_one('#selection').display
            assert prompt.text == 'Keep this unfinished task.\nAnd its second line.'
            assert app.focused is (start if mode in {'work', 'sequence'} else prompt)
            if mode == 'plan':
                assert 'Retry review' in app.query_one('#resume', Button).render_line(0).text
                assert app.query_one('#apply-plan', Button).disabled
                app.reviewed_plan = ('saved-plan', 'digest', str(tmp_path))
                app.update_mode(); await pilot.pause()
                apply = app.query_one('#apply-plan', Button)
                assert not apply.disabled and apply.region.height == 1
                assert 0 <= apply.region.y < size[1]
            if mode == 'work':
                for name, field in (('confirm-preparation', 'reviewed_preparation'),
                                    ('confirm-revision', 'reviewed_revision'),
                                    ('approve-decision', 'reviewed_decision')):
                    assert app.query_one('#'+name, Button).disabled
                    setattr(app, field, (app.decision_selection(app.selection()),
                        {'status':'WAITING_FOR_HUMAN', 'request_sha256':'a'*64})
                        if field == 'reviewed_decision' else ('reviewed',))
                app.update_mode(); await pilot.pause()
                assert app.query_one('#reconsider-decision', Button).disabled
                for name in ('confirm-preparation', 'confirm-revision', 'approve-decision', 'decline-decision'):
                    button = app.query_one('#'+name, Button)
                    assert not button.disabled and button.region.height == 1
                    assert 0 <= button.region.y < size[1]
            app.controls(True); await pilot.pause()
            assert not app.query_one('#cancel', Button).disabled
            assert app.query_one('#start', Button).disabled
            await pilot.press('f2'); await pilot.pause()
            assert app.query_one('#selection').display
            assert not app.query_one('#appearance', Select).disabled
            await pilot.press('f2'); await pilot.pause()
            assert app.query_one('#cancel').region.bottom <= size[1]
            app.controls(False)
            assert not app.backend.launches
    asyncio.run(scenario())
