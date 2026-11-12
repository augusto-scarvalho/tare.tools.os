"""Activity remains visible through silent inference, operator waits and process cleanup."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from textual.widgets import Static, TextArea

from relay.console_header import ConversationHeader, SPINNER, presence_title
from relay.native_console import WorkConsole
from test_native_console import FakeBackend, Process


@pytest.mark.parametrize('size', [(80, 24), (130, 44)])
@pytest.mark.parametrize('outcome', ['PROPOSED', 'BLOCKED', 'CANCELLED', 'invalid'])
def test_silent_execution_animates_until_result_or_cancellation_cleanup(tmp_path, monkeypatch, size, outcome):
    monkeypatch.setattr('relay.native_agent.session_root', lambda: tmp_path/'custody')

    class Silent(Process):
        async def communicate(self):
            await self.done.wait()
            await cleanup.wait()
            self.returncode = 0 if outcome == 'PROPOSED' else 2
            return (b'invalid' if outcome == 'invalid' else json.dumps({'status': outcome}).encode()), b''

    async def scenario():
        nonlocal cleanup
        cleanup = asyncio.Event()
        backend = FakeBackend(); backend.process = Silent()
        app = WorkConsole(tmp_path, backend=backend, initial_mode='assistant')
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            titles = []
            monkeypatch.setattr(app, 'write_terminal_title', titles.append)
            prompt = app.query_one('#prompt', TextArea)
            prompt.load_text('Explain this project')
            prompt.focus(); await pilot.press('enter'); await pilot.pause(.15)
            assert app.busy and len(backend.launches) == 1
            prompt.load_text('Preserve this unsent correction')
            # No tokens or progress events arrive: the independent timer must still advance.
            before = len(titles)
            await pilot.pause(.55)
            assert len(set(titles[before:])) >= 2
            symbol = app.query_one('#activity-symbol', Static)
            assert str(symbol.render()) in SPINNER
            near = app.query_one('#activity-near-composer', Static)
            text = str(near.render())
            assert text[0] in SPINNER and app.model_name in text and text.endswith('coordinator')
            assert app.query_one('#model-controls').region.contains_region(near.region)
            assert app.query_one('#model-controls').region.bottom <= app.query_one('#composer').region.y
            assert app.screen.region.contains_region(symbol.region)
            assert app.query_one('#start').region.bottom <= size[1]
            assert len(backend.launches) == 1
            if outcome == 'CANCELLED':
                app.action_cancel(); await pilot.pause(.2)
                assert app.busy and str(symbol.render()) in SPINNER  # runner has not stopped
            else:
                backend.process.done.set()
            cleanup.set()
            await app.workers.wait_for_complete(); await pilot.pause()
            expected = 'finished' if outcome == 'PROPOSED' else 'cancelled' if outcome == 'CANCELLED' else 'error'
            assert not app.busy and app.presence_kind == expected
            assert str(near.render()) == ''  # nothing stale once the work stops
            assert prompt.text == 'Preserve this unsent correction'
            settled = str(symbol.render())
            emitted = len(titles)
            await pilot.pause(.3)
            assert str(symbol.render()) == settled and len(titles) == emitted
            assert ('✓' if outcome == 'PROPOSED' else '■' if outcome == 'CANCELLED' else '!') in app.title
            app.new_conversation(); await app.workers.wait_for_complete(); await pilot.pause()
            assert app.presence_kind == 'ready' and str(symbol.render()) == '○'
            assert len(backend.launches) == 1

    cleanup = None
    asyncio.run(scenario())


def test_waiting_for_permission_stops_spinner_and_project_switch_updates_title(tmp_path):
    async def scenario():
        backend = FakeBackend()
        app = WorkConsole(tmp_path, backend=backend, initial_mode='assistant')
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            async def wait_presence(kind):
                # Observe the real timer; a fixed 250 ms sleep races its first
                # resumed tick on a loaded Windows runner. A missing transition
                # must still fail within a finite responsiveness bound.
                deadline = asyncio.get_running_loop().time() + 2
                while app.presence_kind != kind and asyncio.get_running_loop().time() < deadline:
                    await pilot.pause(.05)
                assert app.presence_kind == kind
            app.controls(True)
            panel = SimpleNamespace(is_mounted=True)
            app._command_permission_panel = panel
            await wait_presence('waiting')
            assert app.presence_kind == 'waiting' and app.title.startswith('? - tare - ')
            panel.is_mounted = False
            await wait_presence('working')
            assert app.presence_kind == 'working'
            app.last_result = {'status': 'BLOCKED', 'conversation_task': {'stage': 'WAITING_FOR_HUMAN'}}
            app.controls(False)
            assert app.presence_kind == 'waiting'
            # Saved running tool rows never keep the current conversation busy.
            app.last_result = None
            app.workspace_root = str(tmp_path/'outro projeto')
            app.update_mode()
            assert app.title == '○ - tare - outro projeto'
            assert not backend.launches
    asyncio.run(scenario())


def test_terminal_title_bounds_untrusted_names_without_losing_accents():
    assert presence_title('✓', 'projeto São Paulo') == '✓ - tare - projeto São Paulo'
    title = presence_title('!', 'unsafe\x1b]2;injected\x07\x9c\n\tname')
    assert not any(ord(c) < 32 or 127 <= ord(c) < 160 for c in title)
    assert len(presence_title('○', '🙂'*300).encode('utf-16-le'))//2 < 255


def test_terminal_driver_saves_deduplicates_restores_and_tolerates_closed_output():
    writes = []
    class Driver:
        def write(self, value): writes.append(value)
        def flush(self): pass
    class Header(ConversationHeader):
        _driver = Driver()
        is_headless = is_web = False
        terminal_title = None
        terminal_title_saved = False
        presence_timer = None
        workspace_root = 'project'
    header = Header()
    header.write_terminal_title('○ - tare - project')
    header.write_terminal_title('○ - tare - project')
    header.write_terminal_title('⠋ - tare - project')
    assert writes == ['\x1b[22;2t', '\x1b]2;○ - tare - project\x1b\\', '\x1b]2;⠋ - tare - project\x1b\\']
    header.restore_terminal_title()
    assert writes[-1] == '\x1b]2;○ - tare - project\x1b\\\x1b[23;2t'
    count = len(writes)
    header.is_headless = True
    header.write_terminal_title('ignored')
    assert len(writes) == count
    header.is_headless = False
    def closed(value): raise OSError('closed')
    header._driver.write = closed
    header.write_terminal_title('next')
    header.restore_terminal_title()
