import asyncio

import pytest
from textual.widgets import Button, Input, Switch

from relay.challenger import policy
from relay.console_challenger import ChallengerPanel
from relay.native_console import WorkConsole
from test_native_console import FakeBackend
from test_challenger import project


@pytest.mark.parametrize('size', [(80, 24), (130, 44)])
def test_toggle_sampling_and_budgets_persist_from_tui_without_inference(project, size):
    async def scenario():
        app = WorkConsole(project, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            assert app.dispatch_conversation_command('/challenger', '')
            await pilot.pause()
            panel = app.screen
            assert isinstance(panel, ChallengerPanel)
            for name in ('save', 'refresh', 'compare', 'patch', 'use', 'retry', 'stop'):
                assert panel.region.contains_region(panel.query_one('#challenger-'+name).region)
            for name in ('local_percent', 'external_percent', 'daily_usd', 'trial_usd'):
                widget = panel.query_one('#challenger-'+name)
                assert widget.parent.region.contains_region(widget.region)
            await pilot.press('ctrl+enter', 'f2', 'f3', 'f4')
            assert app.screen is panel and not app.backend.launches
            panel.query_one('#challenger-enabled', Switch).value = True
            panel.query_one('#challenger-local_percent', Input).value = '30'
            panel.query_one('#challenger-external_percent', Input).value = '10'
            panel.query_one('#challenger-daily_quota_calls', Input).value = '12'
            await pilot.click('#challenger-save'); await pilot.pause()
            saved = policy(project)
            assert saved['enabled'] and saved['local_percent'] == 30
            assert saved['external_percent'] == 10 and saved['daily_quota_calls'] == 12
            panel.query_one('#challenger-enabled', Switch).value = False
            for _ in range(20):
                if not panel.running and not panel.query_one('#challenger-save', Button).has_class('-active'):
                    break
                await pilot.pause(.05)
            await pilot.click('#challenger-save'); await pilot.pause()
            assert not policy(project)['enabled']
            assert panel.query_one('#challenger-use', Button).disabled
            await pilot.press('escape')
            assert app.screen is not panel and not app.backend.launches
    asyncio.run(scenario())
