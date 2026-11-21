import asyncio
import json
from pathlib import Path

from textual.widgets import OptionList, Select, TextArea
from textual.widgets.text_area import Selection as TextSelection

from relay import console_entry
from relay.console_backend import Selection
from relay.console_navigation import (
    INPUT_HISTORY_MAX_ENTRIES,
    INPUT_HISTORY_MAX_ENTRY_BYTES,
    INPUT_HISTORY_MAX_FILE_BYTES,
    INPUT_HISTORY_SCHEMA,
    append_input_history,
    input_history_path,
    read_input_history,
)
from relay.native_console import WorkConsole
from test_native_console import FakeBackend, Process


class InstantBackend(FakeBackend):
    def __init__(self):
        super().__init__()
        self.sent = []
        self.hold = None

    async def launch(self, args, root):
        self.launches.append(args)
        proc = self.hold or Process()
        if self.hold is None:
            proc.done.set()
        return proc

    def send_message(self, selection, text, message_id):
        self.sent.append((selection.session, text, message_id))
        return {'message_id': message_id}


async def settle(pilot, ticks=3):
    for _ in range(ticks):
        await pilot.pause()


def _install_session(workspace, session_dir):
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / 'native-agent.json').write_text(
        json.dumps({'config': {'repositories': {'repo': str(workspace)}}}),
        encoding='utf-8',
    )


def test_storage_dedup_caps_size_limits_and_corrupt_safety(tmp_path):
    storage = tmp_path / 'storage' / 'console'
    ws_one = tmp_path / 'ws-one'
    ws_two = tmp_path / 'ws-two'
    ws_one.mkdir()
    ws_two.mkdir()

    assert read_input_history(storage, ws_one) == []
    assert append_input_history(storage, '   \n  ', ws_one) is None
    assert append_input_history(storage, 'a' * (INPUT_HISTORY_MAX_ENTRY_BYTES + 1), ws_one) is None
    assert not input_history_path(storage, ws_one).exists()

    exact = 'x' * INPUT_HISTORY_MAX_ENTRY_BYTES
    assert append_input_history(storage, exact, ws_one) == [exact]
    assert append_input_history(storage, 'first', ws_one) == [exact, 'first']
    assert append_input_history(storage, '  first  ', ws_one) == [exact, 'first']
    assert append_input_history(storage, 'second', ws_one) == [exact, 'first', 'second']
    assert append_input_history(storage, 'first', ws_one) == [exact, 'first', 'second', 'first']

    for i in range(INPUT_HISTORY_MAX_ENTRIES + 15):
        append_input_history(storage, f'item-{i}', ws_one)
    rows = read_input_history(storage, ws_one)
    assert len(rows) == INPUT_HISTORY_MAX_ENTRIES
    assert rows[0] == 'item-15' and rows[-1] == f'item-{INPUT_HISTORY_MAX_ENTRIES + 14}'

    # File-size budget prunes oldest entries so payload stays <= INPUT_HISTORY_MAX_FILE_BYTES.
    huge_dir = tmp_path / 'storage-budget' / 'console'
    chunk = 'b' * 16000
    for i in range(40):
        append_input_history(huge_dir, f'{i:03d}-' + chunk, ws_one)
    budget_file = input_history_path(huge_dir, ws_one)
    assert budget_file.stat().st_size <= INPUT_HISTORY_MAX_FILE_BYTES
    assert len(read_input_history(huge_dir, ws_one)) < 40

    # Workspace isolation and no workspace pollution.
    assert read_input_history(storage, ws_two) == []
    assert list(ws_one.iterdir()) == [] and list(ws_two.iterdir()) == []

    # Corrupt file is preserved rather than overwritten, and schema mismatch is rejected.
    corrupt_file = input_history_path(storage, ws_two)
    corrupt_file.parent.mkdir(parents=True, exist_ok=True)
    corrupt_raw = b'{not-json'
    corrupt_file.write_bytes(corrupt_raw)
    try:
        read_input_history(storage, ws_two)
        raise AssertionError('Expected ValueError for corrupt JSON')
    except ValueError:
        pass
    try:
        append_input_history(storage, 'hello', ws_two)
        raise AssertionError('Expected ValueError preserving corrupt file')
    except ValueError:
        pass
    assert corrupt_file.read_bytes() == corrupt_raw

    # Schema mismatch raises ValueError too.
    bad_schema = json.dumps({'schema': 'tare.tools/console-input-history/99', 'entries': ['hi']}).encode('utf-8')
    corrupt_file.write_bytes(bad_schema)
    try:
        read_input_history(storage, ws_two)
        raise AssertionError('Expected ValueError for unsupported schema')
    except ValueError:
        pass
    assert corrupt_file.read_bytes() == bad_schema


def test_corrupt_history_file_does_not_crash_workconsole_or_get_clobbered(tmp_path, monkeypatch):
    storage = tmp_path / 'storage'
    ws = tmp_path / 'project'
    ws.mkdir()
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(storage))
    corrupt_file = input_history_path(storage / 'console', ws)
    corrupt_file.parent.mkdir(parents=True, exist_ok=True)
    corrupt_raw = json.dumps({'schema': INPUT_HISTORY_SCHEMA, 'entries': [123]}).encode('utf-8')
    corrupt_file.write_bytes(corrupt_raw)

    async def scenario():
        app = WorkConsole(ws, backend=InstantBackend(), initial_mode='assistant',
                          storage_preferred=storage, storage_fallback=storage)
        async with app.run_test(size=(120, 36)) as pilot:
            await app.workers.wait_for_complete()
            await settle(pilot)
            assert app.input_history == []
            prompt = app.query_one('#prompt', TextArea)
            prompt.focus()
            prompt.load_text('hello there')
            await pilot.press('enter')
            await settle(pilot)
            await app.workers.wait_for_complete()
            assert app.input_history == ['hello there']
            assert corrupt_file.read_bytes() == corrupt_raw

    asyncio.run(scenario())


def test_messages_queued_and_valid_commands_recorded_while_rejected_excluded(tmp_path, monkeypatch):
    storage = tmp_path / 'storage'
    ws = tmp_path / 'project'
    ws.mkdir()
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(storage))

    async def scenario():
        backend = InstantBackend()
        app = WorkConsole(ws, backend=backend, initial_mode='assistant',
                          storage_preferred=storage, storage_fallback=storage)
        app.action_routes = lambda: None
        async with app.run_test(size=(120, 36)) as pilot:
            await app.workers.wait_for_complete()
            await settle(pilot)
            prompt = app.query_one('#prompt', TextArea)
            prompt.focus()

            # 1. Ordinary assistant message.
            prompt.load_text('first prompt')
            await settle(pilot)
            await pilot.press('enter')
            await settle(pilot)
            await app.workers.wait_for_complete()

            # 2. Rejected slash commands are never recorded.
            assert app.dispatch_conversation_command('/routes', 'extra-arg')
            prompt.load_text('/resume')
            await settle(pilot)
            app.execute(False)  # No saved session yet -> refused.
            await settle(pilot)
            assert app.dispatch_conversation_command('/missed', 'missed detail')  # No assistant_session -> rejected.
            app.assistant_session = str(tmp_path / 'missing-session')
            assert app.dispatch_conversation_command('/missed', 'missed detail')  # missed() returns False -> rejected.
            app.assistant_session = None
            # Unknown command and incomplete flow command in conversation mode.
            app.query_one('#mode', Select).value = 'conversation'
            await settle(pilot)
            proposals = []
            app.backend.propose_entry = lambda *a, **k: proposals.append(a) or {
                'directory': str(tmp_path / 'entry-dir'),
                'status': 'INVALID',
                'reason': console_entry.HELP,
                'question': '',
            }
            for invalid in ('/unknowncmd', '/plan'):
                prompt.load_text(invalid)
                await settle(pilot)
                app.begin_entry()
                await settle(pilot)
                await app.workers.wait_for_complete()
            # Valid /status and /help in conversation mode DO record (refocus after begin_entry's controls(True)).
            prompt.focus()
            prompt.load_text('/status')
            await settle(pilot)
            await pilot.press('enter')
            await settle(pilot)
            prompt.load_text('/help')
            await settle(pilot)
            await pilot.press('enter')
            await settle(pilot)

            # Switch back to assistant mode and run /routes + queued message.
            app.query_one('#mode', Select).value = 'assistant'
            await settle(pilot)
            prompt.focus()
            prompt.load_text('/routes')
            await settle(pilot)
            await pilot.press('enter')
            await settle(pilot)
            await app.workers.wait_for_complete()

            session_dir = tmp_path / 'session-1'
            _install_session(ws, session_dir)
            app.busy = True
            app.assistant_session = str(session_dir)
            app.active = Selection(str(ws), mode='session', session=str(session_dir))
            prompt.load_text('queued followup')
            await settle(pilot)
            app.send_operator_message()
            await settle(pilot)
            await app.workers.wait_for_complete()
            app.busy = False

            assert app.input_history == ['first prompt', '/status', '/help', '/routes', 'queued followup']
            assert read_input_history(storage / 'console', ws) == [
                'first prompt', '/status', '/help', '/routes', 'queued followup',
            ]

    asyncio.run(scenario())


def test_up_down_navigation_draft_restore_popup_rules_multiline_and_selection(tmp_path, monkeypatch):
    storage = tmp_path / 'storage'
    ws = tmp_path / 'project'
    ws.mkdir()
    (ws / 'spec.md').write_text('spec\n', encoding='utf-8')
    (ws / 'src').mkdir()
    (ws / 'src' / 'a.py').write_text('x = 1\n', encoding='utf-8')
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(storage))

    entries = [
        'Explain @src/a.py',
        'multi line 1\nmulti line 2',
        '/routes',
    ]
    for item in entries:
        append_input_history(storage / 'console', item, ws)

    async def scenario():
        app = WorkConsole(ws, backend=InstantBackend(), initial_mode='assistant',
                          storage_preferred=storage, storage_fallback=storage)
        async with app.run_test(size=(120, 40)) as pilot:
            await app.workers.wait_for_complete()
            await settle(pilot)
            prompt = app.query_one('#prompt', TextArea)
            prompt.focus()

            # Active draft is preserved while browsing with Up/Down, and popups stay suppressed.
            prompt.load_text('draft in progress')
            prompt.move_cursor((0, len('draft in progress')))
            await settle(pilot)

            await pilot.press('up')
            await settle(pilot)
            assert prompt.text == '/routes'
            assert not app.query_one('#slash-box').display

            await pilot.press('up')
            await settle(pilot)
            assert prompt.text == 'multi line 1\nmulti line 2'
            assert prompt.cursor_location[0] == 0

            await pilot.press('up')
            await settle(pilot)
            assert prompt.text == 'Explain @src/a.py'
            assert not app.query_one('#context-box').display

            # Clamps at oldest entry on extra Up.
            await pilot.press('up')
            await settle(pilot)
            assert prompt.text == 'Explain @src/a.py'

            # Stepping Down moves cleanly through multiline entry's internal row before leaving it.
            await pilot.press('down')
            await settle(pilot)
            assert prompt.text == 'multi line 1\nmulti line 2'
            # Cursor was placed on last row (row 1); Down advances to '/routes'.
            await pilot.press('down')
            await settle(pilot)
            assert prompt.text == '/routes'
            # One more Down restores draft.
            await pilot.press('down')
            await settle(pilot)
            assert prompt.text == 'draft in progress'
            assert app.input_history_index is None

            # Multiline cursor inside prompt: row > 0 navigates inside TextArea before history.
            prompt.load_text('line a\nline b')
            prompt.move_cursor((1, 2))
            await settle(pilot)
            await pilot.press('up')
            await settle(pilot)
            assert prompt.text == 'line a\nline b' and prompt.cursor_location == (0, 2)
            assert app.input_history_index is None
            # Pressing Up from row 0 recalls history; then moving cursor up from row 1 inside recalled multiline stays in entry.
            await pilot.press('up')
            await settle(pilot)
            assert prompt.text == '/routes'
            await pilot.press('up')
            await settle(pilot)
            assert prompt.text == 'multi line 1\nmulti line 2'
            prompt.move_cursor((0, 1))
            await settle(pilot)
            await pilot.press('down')
            await settle(pilot)
            assert prompt.text == 'multi line 1\nmulti line 2' and prompt.cursor_location[0] == 1

            # Typing edits exit browsing mode cleanly.
            await pilot.press('!')
            await settle(pilot)
            assert app.input_history_index is None

            # Active selection guard: Up leaves TextArea selection behavior alone.
            prompt.load_text('abc')
            prompt.selection = TextSelection((0, 0), (0, 3))
            await settle(pilot)
            await pilot.press('up')
            await settle(pilot)
            assert prompt.text == 'abc' and app.input_history_index is None

            # Open #slash-box gets priority over Up/Down history.
            prompt.load_text('/')
            prompt.move_cursor((0, 1))
            await settle(pilot)
            assert app.query_one('#slash-box').display
            slash_menu = app.query_one('#slash-menu', OptionList)
            await pilot.press('down')
            await settle(pilot)
            assert prompt.text == '/' and slash_menu.highlighted == 1 and app.input_history_index is None

            # Open #context-box gets priority over Up/Down history.
            prompt.load_text('@s')
            prompt.move_cursor((0, 2))
            await pilot.pause(0.3)
            await settle(pilot)
            assert app.query_one('#context-box').display
            ctx_menu = app.query_one('#context-menu', OptionList)
            await pilot.press('down')
            await settle(pilot)
            assert prompt.text == '@s' and ctx_menu.highlighted == 1 and app.input_history_index is None

    asyncio.run(scenario())


def test_history_persists_across_instances_and_reloads_on_workspace_switch(tmp_path, monkeypatch):
    storage = tmp_path / 'storage'
    ws_one = tmp_path / 'ws-one'
    ws_two = tmp_path / 'ws-two'
    ws_one.mkdir()
    ws_two.mkdir()
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(storage))

    async def first_run():
        app = WorkConsole(ws_one, backend=InstantBackend(), initial_mode='assistant',
                          storage_preferred=storage, storage_fallback=storage)
        async with app.run_test(size=(120, 36)) as pilot:
            await app.workers.wait_for_complete()
            await settle(pilot)
            prompt = app.query_one('#prompt', TextArea)
            prompt.focus()
            prompt.load_text('alpha task')
            await pilot.press('enter')
            await settle(pilot)
            await app.workers.wait_for_complete()

    asyncio.run(first_run())

    async def second_run():
        app = WorkConsole(ws_one, backend=InstantBackend(), initial_mode='assistant',
                          storage_preferred=storage, storage_fallback=storage)
        async with app.run_test(size=(120, 36)) as pilot:
            await app.workers.wait_for_complete()
            await settle(pilot)
            prompt = app.query_one('#prompt', TextArea)
            prompt.focus()
            await pilot.press('up')
            await settle(pilot)
            assert prompt.text == 'alpha task'

            # Reset prompt and switch workspace -> history isolates and reloads for ws_two.
            prompt.load_text('')
            await settle(pilot)
            assert app.open_project(str(ws_two), app.project_navigation_scope(), [])
            await settle(pilot)
            assert app.input_history == [] and app.input_history_index is None
            prompt.load_text('beta task')
            await pilot.press('enter')
            await settle(pilot)
            await app.workers.wait_for_complete()

            assert app.open_project(str(ws_one), app.project_navigation_scope(), [])
            await settle(pilot)
            assert app.input_history == ['alpha task']
            prompt.focus()
            await pilot.press('up')
            await settle(pilot)
            assert prompt.text == 'alpha task'

    asyncio.run(second_run())
    assert list(ws_one.iterdir()) == [] and list(ws_two.iterdir()) == []
    assert read_input_history(storage / 'console', ws_one) == ['alpha task']
    assert read_input_history(storage / 'console', ws_two) == ['beta task']
