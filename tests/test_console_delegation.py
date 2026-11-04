import asyncio

import pytest
from textual.widgets import Button, Input, Select, Static, Switch, TextArea

from relay.console_delegation import DelegationPanel
from relay.native_console import WorkConsole
from relay import delegation
from test_native_console import FakeBackend
from test_delegation import enabled


@pytest.mark.parametrize('size', [(80, 24), (130, 44)])
def test_tui_saves_exploration_and_returns_without_model_call(tmp_path, enabled, monkeypatch, size):
    async def scenario():
        app = WorkConsole(tmp_path, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            assert app.dispatch_conversation_command('/delegation', '')
            await pilot.pause()
            panel = app.screen
            assert isinstance(panel, DelegationPanel)
            assert panel.region.contains_region(panel.query_one('#delegation-save', Button).region)
            panel.query_one('#delegation-enabled', Switch).value = True
            panel.query_one('#delegation-calls', Input).value = '3'
            panel.query_one('#delegation-profile', TextArea).load_text('"fixture"')
            panel.query_one('#delegation-decider', Select).value = 'shadow'
            panel.query_one('#delegation-contradiction_flags', Switch).value = True
            await pilot.click('#delegation-save')
            await pilot.pause()
            assert delegation.policy_path().is_file()
            from relay.native_agent import read_json
            assert read_json(delegation.policy_path())['calls'] == 3
            assert delegation.experiments() == dict(delegation.EXPERIMENTS, decider='shadow', contradiction_flags=True)
            assert not app.backend.launches
            await pilot.press('escape')
            assert app.screen is not panel
    asyncio.run(scenario())


@pytest.mark.parametrize('delayed_preview',[False,True])
def test_planner_picker_saves_separate_model_and_allowance_without_a_model_call(tmp_path, enabled, monkeypatch,delayed_preview):
    from relay import model_choices, turn_engine
    from relay.console_delegation import planner_label
    from relay.model_picker import ModelPicker
    from test_model_choices import profile, catalog
    from relay import environment, native_routes
    real_select = model_choices.native_catalog.select
    catalog(monkeypatch, {'claude-cli': profile()})
    monkeypatch.setattr(model_choices.native_catalog, 'select', lambda name:
        profile() if name == 'claude-cli' else real_select(name))
    # Exercise the real keyword-only inventory(role=...) and policy projection,
    # including the planner's minimum high, over an isolated offline catalog.
    monkeypatch.setattr(model_choices.native_catalog, 'offline_deployments',
                        lambda **kwargs: [('claude-cli', profile(), True)])
    monkeypatch.setattr(native_routes, 'configured', lambda: {'deployments': [
        {'id': 'claude-cli', 'roles': ['planner']}]})
    monkeypatch.setattr(environment, 'read', lambda: {})
    monkeypatch.setattr(environment, 'listed', lambda *args: True)
    import threading
    from relay import console_delegation
    preview_started,preview_release=threading.Event(),threading.Event()
    original_preview=console_delegation.plan_reviewer_label
    def preview(profile=None):
        if delayed_preview and not preview_started.is_set():
            preview_started.set()
            assert preview_release.wait(5),'The test must release its local preview worker.'
        return original_preview(profile)
    monkeypatch.setattr(console_delegation,'plan_reviewer_label',preview)
    project = tmp_path/'project'
    project.mkdir()
    async def scenario():
        app = WorkConsole(project, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            original = app.selection().profile
            app.dispatch_conversation_command('/delegation', '')
            await pilot.pause()
            panel = app.screen
            if delayed_preview:
                assert await asyncio.to_thread(preview_started.wait,5)
                preview_release.set()
            # The panel starts its own asynchronous reviewer preview after the
            # app's initial workers have finished. Wait for that layout change.
            await app.workers.wait_for_complete()
            await pilot.pause()
            button = panel.query_one('#delegation-planner-model-choose', Button)
            button.scroll_visible(animate=False,immediate=True)
            await pilot.pause()
            viewport=panel.query_one('#delegation-scroll').scrollable_content_region
            assert viewport.contains_region(button.region),(viewport,button.region)
            assert await pilot.click('#delegation-planner-model-choose')
            await pilot.pause()
            assert isinstance(app.screen, ModelPicker),str(panel.query_one('#delegation-status', Static).render())
            assert 'Minimum reasoning: high' in str(app.screen.query_one('#model-note', Static).render())
            assert all('low' not in route['efforts'] and 'medium' not in route['efforts']
                       for item in app.screen.choices for route in item['routes'])
            app.screen.query_one('#model-reasoning', Select).value = 'high'
            await pilot.click('#model-apply')
            await pilot.pause()
            assert app.screen is panel and panel.planner_profile is not None, str(panel.query_one('#delegation-status', Static).render())
            panel.query_one('#delegation-planner_child', Switch).value = True
            panel.query_one('#delegation-child_routes_any', Switch).value = True
            routes_before = turn_engine.policy()['planner']
            panel.query_one('#delegation-calls', Input).value = '0'
            await pilot.click('#delegation-save')
            await pilot.pause()
            assert turn_engine.policy()['planner'] == routes_before
            assert 'Invalid exploration limit' in str(panel.query_one('#delegation-status', Static).render())
            panel.query_one('#delegation-calls', Input).value = '4'
            await pilot.pause(.4)  # Let the button's click debounce end after validation feedback.
            await pilot.click('#delegation-save')
            await pilot.pause()
            assert delegation.experiments()['planner_child'] and delegation.experiments()['child_routes_any'], str(panel.query_one('#delegation-status', Static).render())
            assert turn_engine.policy()['planner'][0].startswith('planner-')
            assert 'Claude' in planner_label() and 'high' in planner_label()
            assert app.selection().profile == original and not app.backend.launches
            await app.workers.wait_for_complete()
            button.scroll_visible(animate=False,immediate=True)
            await pilot.pause()
            await pilot.click('#delegation-planner-model-choose')
            await pilot.pause()
            assert app.screen.chosen['connection'] == 'claude-cli'
            assert app.screen.chosen['reasoning'] == 'high'
            await pilot.press('escape')
    asyncio.run(scenario())
