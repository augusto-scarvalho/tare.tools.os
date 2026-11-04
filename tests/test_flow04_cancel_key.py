"""The advertised Cancel key must reach a busy TUI with its composer focused."""
import asyncio
from textual.widgets import TextArea

from relay.native_console import WorkConsole
from tare_tools_agent_runtime import CancellationToken
from test_native_console import FakeBackend


def test_cancel_key_stops_busy_entry_without_cutting_queued_draft(tmp_path):
    app = WorkConsole(tmp_path, backend=FakeBackend(), initial_mode='assistant',
                      storage_preferred=tmp_path/'history')
    async def scenario():
        async with app.run_test(size=(120, 42)) as pilot:
            composer = app.query_one('#prompt', TextArea)
            composer.load_text('next demand')
            composer.focus()
            app.busy = True
            app.entry_cancel = CancellationToken()
            await pilot.press('ctrl+x')
            assert app.entry_cancel.is_cancelled()
            assert app.cancel_requested
            assert app.busy  # a request is not proof the owned operation stopped
            assert composer.text == 'next demand'

            # The same key remains TextArea's ordinary cut when no run is active.
            app.busy = False
            app.entry_cancel = None
            await pilot.press('ctrl+x')
            assert composer.text == ''
            assert app.clipboard == 'next demand'

    asyncio.run(scenario())
