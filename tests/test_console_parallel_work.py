"""The sequence toggle is reachable and reaches the actual CLI adapter."""
import asyncio
import pytest
from textual.widgets import Checkbox, Input, Select
from relay.console_backend import command
from relay.native_console import WorkConsole
from test_native_console import FakeBackend


@pytest.mark.parametrize('size', [(80, 24), (130, 44)])
def test_parallel_sequence_toggle_uses_bounded_cli_options(tmp_path, monkeypatch, size):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'history'))
    async def scenario():
        app = WorkConsole(tmp_path, backend=FakeBackend(), initial_mode='sequence')
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            if not app.query_one('#selection').display:
                await pilot.press('f2')
            toggle = app.query_one('#sequence-parallel', Checkbox)
            toggle.focus()
            await pilot.pause()
            await pilot.press('space')
            await pilot.pause()
            assert toggle.value and toggle.region.height > 0
            app.query_one('#sequence-works', Input).value = 'WORK-A, WORK-B'
            args = command(app.selection())
            assert args[args.index('--workers')+1] == '2'
            assert '--isolated-effects' in args
            toggle.value = False
            assert '--workers' not in command(app.selection())
    asyncio.run(scenario())
