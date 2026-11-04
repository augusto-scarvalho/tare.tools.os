import asyncio
import json

import pytest
from textual.widgets import Button, Select, TextArea

from relay.console_output import ToolActivity, observe_process
from relay.native_console import WorkConsole
from test_native_console import FakeBackend


def activity(state='running', identity='session/effect/1', target='parser tests', **kwargs):
    return dict(kind='tool-activity', id=identity, operation='verify', target=target,
                state=state, detail='Check output\nline two', receipt='4 · '+'a'*64,
                duration_ms=None if state=='running' else 1200, **kwargs)


@pytest.mark.parametrize('size', [(80, 24), (130, 44)])
@pytest.mark.parametrize('theme', ['tare-dark', 'tare-light', 'tare-contrast'])
def test_tool_rows_update_expand_failures_copy_and_restore_history(tmp_path, monkeypatch, size, theme):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))
    root = tmp_path/'project'; root.mkdir()
    async def scenario():
        app = WorkConsole(root, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete(); app.theme = theme
            prompt = app.query_one('#prompt', TextArea); prompt.load_text('Keep this draft')
            app.assistant_activity({'channel': 'activity', 'activity': activity()})
            await pilot.pause()
            widget = app.query_one(ToolActivity)
            assert widget.collapsed and 'Awaiting result' in widget.title
            failed = activity('failed', target='src/'+'日本語/'*20+'parser.py')
            app.assistant_activity({'channel': 'activity', 'activity': failed})
            await pilot.pause()
            assert len(app.query(ToolActivity)) == 1 and not widget.collapsed
            assert 'Failed' in widget.title and '1.2s' in widget.title
            title = widget.query_one('CollapsibleTitle')
            visible = ''.join(title.render_line(line).text for line in range(title.size.height))
            assert 'Failed' in visible and '1.2s' in visible
            copies = []; app.copy_to_clipboard = copies.append
            await pilot.click(widget.query_one('.copy-tool-output', Button)); await pilot.pause()
            assert copies == ['Check output\nline two']
            widget.collapsed = True
            app.assistant_activity({'channel': 'activity', 'activity': failed})
            assert widget.collapsed  # A duplicate final record must not override the reader.
            app.assistant_activity({'channel': 'activity', 'activity': activity(identity='session/effect/2')})
            app.finish_activity(); await pilot.pause()
            assert app.assistant_history[-1]['state'] == 'unconfirmed'
            assert len(app.assistant_history) == 2 and prompt.text == 'Keep this draft'
            app.checkpoint('draft_saved'); saved_id = app.demand_id
            app.new_demand()
            await app.list_demands().wait()
            app.query_one('#saved-demand', Select).value = saved_id
            app.open_demand(); await app.workers.wait_for_complete(); await pilot.pause()
            assert len(app.query(ToolActivity)) == 2
            assert all(row.collapsed for row in app.query(ToolActivity))
            assert prompt.text == 'Keep this draft' and not app.backend.launches
    asyncio.run(scenario())


def test_activity_pipe_filters_private_and_malformed_events():
    async def scenario():
        class Process:
            def __init__(self):
                self.stdout = asyncio.StreamReader(); self.stderr = asyncio.StreamReader()
            async def wait(self): return 0
        process = Process(); events = []; diagnostics = []
        process.stdout.feed_data(b'{"status":"PROPOSED"}'); process.stdout.feed_eof()
        values = [{'channel': 'reasoning', 'text': 'private'},
                  {'channel': 'activity', 'activity': {'text': 'private'}},
                  {'channel': 'activity', 'activity': activity('succeeded')},
                  {'channel': 'text', 'text': 'Visible answer'},
                  {'channel': 'narration', 'call_id': 's/model/1', 'text': '[red]Vou mapear\x1b[2J'}]
        process.stderr.feed_data(('\n'.join(map(json.dumps, values))+'\n').encode()); process.stderr.feed_eof()
        stdout, _ = await observe_process(process, events.append, diagnostics.append)
        assert len(events) == 3 and events[0]['activity']['state'] == 'succeeded'
        assert events[2]['channel'] == 'narration' and '\x1b' not in events[2]['text']
        assert 'private' not in json.dumps(events) and not diagnostics
        assert json.loads(stdout)['status'] == 'PROPOSED'
    asyncio.run(scenario())


def test_full_output_is_explicit_and_does_not_replace_the_draft(tmp_path, monkeypatch):
    calls = []
    def read(session, project, value):
        calls.append((session, project, value['receipt']))
        return 'Full output\n'+'line\n'*2000
    monkeypatch.setattr('relay.native_activity.full_output', read)
    async def scenario():
        app = WorkConsole(tmp_path, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            app.assistant_session = str(tmp_path/'saved')
            prompt = app.query_one('#prompt', TextArea); prompt.load_text('Unsent question')
            app.assistant_activity({'channel': 'activity', 'activity': activity('failed', truncated=True)})
            await pilot.pause()
            widget = app.query_one(ToolActivity)
            assert 'Excerpt only' in str(widget.query_one('.tool-detail').render())
            assert str(widget.query_one('.copy-tool-output', Button).label) == 'Copy excerpt'
            assert 'Copy excerpt' in widget.query_one('.copy-tool-output').render_line(0).text
            app.busy = True
            widget.query_one('.tool-full-output', Button).press(); await pilot.pause()
            assert not calls
            app.busy = False
            widget.query_one('.tool-full-output', Button).press()
            await pilot.pause(); await app.workers.wait_for_complete(); await pilot.pause()
            assert calls == [(str(tmp_path/'saved'), str(tmp_path), activity()['receipt'])]
            assert widget.full_text.endswith('line\n'*2000)
            assert not widget.query_one('.tool-full-output').display
            assert str(widget.query_one('.copy-tool-output', Button).label) == 'Copy output'
            copies = []; app.copy_to_clipboard = copies.append
            widget.query_one('.copy-tool-output', Button).press(); await pilot.pause()
            assert copies == [widget.full_text] and prompt.text == 'Unsent question'
            assert not app.backend.launches
    asyncio.run(scenario())


def test_chatty_tools_cannot_evict_conversation_messages(tmp_path, monkeypatch):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))
    root = tmp_path/'project'; root.mkdir()
    async def scenario():
        app = WorkConsole(root, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            app.assistant_say('You: Keep the original requirement.')
            app.assistant_say('I will preserve the parser interface.')
            for number in range(130):
                app.assistant_activity({'channel': 'activity', 'activity': activity('succeeded', identity=f'tool/{number}')})
            await pilot.pause()
            assert app.assistant_history[:2] == ['You: Keep the original requirement.', 'I will preserve the parser interface.']
            assert len(app.query(ToolActivity)) == 100
            assert len(app.query_one('#output').items) == 102
            assert len(app.query_one('#output').tool_rows) == 100
            app.checkpoint('draft_saved'); identity = app.demand_id
            app.new_demand(); await app.list_demands().wait()
            app.query_one('#saved-demand', Select).value = identity
            app.open_demand(); await app.workers.wait_for_complete(); await pilot.pause()
            assert app.assistant_history[:2] == ['You: Keep the original requirement.', 'I will preserve the parser interface.']
            assert len(app.query(ToolActivity)) == 100 and not app.backend.launches
    asyncio.run(scenario())


def test_a_long_exploration_question_reads_from_its_start(tmp_path, monkeypatch):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))
    root = tmp_path/'project'; root.mkdir()
    question = 'Map how todo.py persists and loads tasks: where the storage file path comes from and how it is validated'
    async def scenario():
        app = WorkConsole(root, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            row = {**activity(), 'operation': 'explore', 'target': question}
            app.assistant_activity({'channel': 'activity', 'activity': row})
            await pilot.pause()
            title = app.query_one(ToolActivity).title
            assert 'Map how todo.py persists' in title and '…' in title  # not "…sts and loads tasks"
    asyncio.run(scenario())


def test_a_refused_tool_row_stays_closed_and_a_failed_one_opens(tmp_path, monkeypatch):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))
    root = tmp_path/'project'; root.mkdir()
    async def scenario():
        app = WorkConsole(root, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            for identity in ('s/effect/1', 's/effect/2'):
                app.assistant_activity({'channel': 'activity', 'activity': activity(identity=identity)})
            await pilot.pause()
            app.assistant_activity({'channel': 'activity', 'activity': activity('refused', identity='s/effect/1')})
            app.assistant_activity({'channel': 'activity', 'activity': activity('failed', identity='s/effect/2')})
            await pilot.pause()
            refused, failed = app.query(ToolActivity)
            assert refused.collapsed and 'Refused' in refused.title  # routing guidance for the model, not the user
            assert not failed.collapsed
    asyncio.run(scenario())
