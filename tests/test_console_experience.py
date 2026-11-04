import asyncio
import json
import sys

import pytest
from textual.widgets import Button, TextArea

from relay.console_output import AssistantMessage, Transcript, observe_process, settled
from relay.native_console import WorkConsole
from test_native_console import FakeBackend
from test_review_checks_panel import until


def test_process_text_arrives_before_completion_and_reasoning_is_not_rendered():
    async def scenario():
        code = '''import json,sys
sys.stderr.buffer.write(b'{"channel":"reasoning","text":"PRIVATE"}\\n')
line=json.dumps({"channel":"text","call_id":"1","text":"Ol\u00e1"},ensure_ascii=False).encode('utf-8')+b'\\n'
for byte in line:
    sys.stderr.buffer.write(bytes([byte])); sys.stderr.buffer.flush()
sys.stdin.buffer.read(1)
sys.stderr.buffer.write(b'x'*70000+b'\\nlast diagnostic\\n'); sys.stderr.buffer.flush()
print('{"status":"PROPOSED"}',flush=True)
'''
        process = await asyncio.create_subprocess_exec(sys.executable, '-u', '-c', code,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        received, events, diagnostics = asyncio.Event(), [], []
        def on_event(event):
            events.append(event); received.set()
        task = asyncio.create_task(observe_process(process, on_event, diagnostics.append))
        try:
            await asyncio.wait_for(received.wait(), 5)
            assert process.returncode is None and not task.done()
            assert events == [{'channel': 'text', 'call_id': '1', 'text': 'Olá'}]
            process.stdin.write(b'x'); await process.stdin.drain(); process.stdin.close()
            stdout, stderr = await asyncio.wait_for(task, 5)
            assert json.loads(stdout)['status'] == 'PROPOSED' and stderr == b''
            assert diagnostics == ['last diagnostic']
        finally:
            if process.returncode is None:
                process.kill(); await process.wait()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())


@pytest.mark.timeout(90)  # A full TUI conversation walk: ~27 s on an idle Windows host, 30 s is the default.
@pytest.mark.parametrize('size', [(80, 24), (130, 44)])
def test_conversation_has_visible_composer_and_retractable_workspace(tmp_path, size):
    async def scenario():
        app = WorkConsole(tmp_path, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete(); await pilot.pause()
            output, prompt = app.query_one('#output', Transcript), app.query_one('#prompt', TextArea)
            send = app.query_one('#start', Button)
            assert output.region.height >= 5
            assert output.region.bottom <= prompt.region.y
            assert send.region.bottom < size[1]
            assert not app.query_one('#selection').display
            await pilot.press('f2'); await pilot.pause()
            assert app.query_one('#selection').display
            await pilot.press('f2'); await pilot.pause()
            assert not app.query_one('#selection').display
            assert not app.backend.launches
            prompt.load_text('Keep every word of this unfinished instruction. '*6)
            await pilot.pause()
            async def assert_composer_height():
                # Wrapping schedules resize_composer after refresh; its style
                # change needs a second layout pass before geometry is final.
                for _ in range(40):
                    await pilot.pause(.025)
                    if prompt.region.height == min(8, prompt.wrapped_document.height):
                        break
                assert prompt.region.height == min(8, prompt.wrapped_document.height)
            for width, height in ((80,24), (130,44)):
                await pilot.resize_terminal(width,height); await pilot.pause()
                await assert_composer_height()
                assert send.region.bottom < height
                assert prompt.text == 'Keep every word of this unfinished instruction. '*6
            app.action_conversations()
            await app.workers.wait_for_complete(); await pilot.pause()
            await assert_composer_height()
            app.close_navigation(); await pilot.pause()
            await assert_composer_height()
    asyncio.run(scenario())


def test_live_transcript_preserves_scroll_and_renders_partial_text_literally(tmp_path):
    async def scenario():
        app = WorkConsole(tmp_path, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=(100, 32)) as pilot:
            await app.workers.wait_for_complete()
            output = app.query_one('#output', Transcript)
            for i in range(20):
                output.write('Earlier message '+str(i)+'\n'*3)
            await pilot.pause()
            output.scroll_home(animate=False); await pilot.pause()
            output.stream({'channel': 'text', 'call_id': '1', 'text': '[red]Olá\x1b[2J'})
            await pilot.pause(.2)
            assert output.scroll_y == 0
            assert output.live_text == '[red]Olá'
            app.query_one('#prompt', TextArea).focus()
            await pilot.press('ctrl+end'); await pilot.pause()
            assert output.is_vertical_scroll_end
            output.finish_stream()
            assert output.live is None
    asyncio.run(scenario())


def test_streamed_reply_redraws_only_its_tail_and_reads_the_same_as_one_message(tmp_path):
    fenced = 'a\n\n```\nx\n\ny\n```\n\n'
    assert settled(fenced+'b\n') == len(fenced)  # never inside a code fence
    assert settled('a\n\n---\n\nb\n') == len('a\n\n')  # Rich puts no blank line after a rule
    assert settled('- a\n\n  more\n') == 0  # an indented line continues the item
    assert settled('- a\n\n- b\n') == 0 and settled('1. a\n\n2. b\n') == 0  # items apart are one list
    assert settled('a\n\n1') == 0  # '1' may still become '1. item'
    def lines(widget):
        return [line.rstrip() for line in str(widget._render()).splitlines()]
    async def scenario():
        app = WorkConsole(tmp_path, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=(100, 32)) as pilot:
            await app.workers.wait_for_complete()
            output = app.query_one('#output', Transcript)
            block = ('A paragraph with **bold** text and `code`. '*4 + '\n\n- one\n- two\n\n'
                     '```python\ndef f(x):\n\n    return x\n```\n\n')
            for _ in range(12):
                output.stream({'channel': 'text', 'call_id': '1', 'text': block})
                await pilot.pause(.1)
            # The 80 ms flush timer may not have ticked after the last block on a slow runner: flush as it would.
            output.flush_live(); await pilot.pause()
            live = output.live
            assert len(live.pieces) >= 2  # finished blocks stopped changing
            streamed = [line for piece in [*live.pieces, live.tail] for line in lines(piece)]
            output.finish_stream(); await pilot.pause(.2)
            message, = live.query(AssistantMessage)
            assert message.source == block*12 and message.label == 'tare · streamed response'
            assert streamed[1:] == lines(message)[1:]
    asyncio.run(scenario())


@pytest.mark.parametrize('size', [(80, 24), (130, 44)])
def test_short_conversations_start_near_header_without_breaking_follow_or_reading(tmp_path, size):
    async def scenario():
        app = WorkConsole(tmp_path, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete(); await pilot.pause()
            output = app.query_one('#output', Transcript)
            assert 0 <= output.query_one('#welcome').region.y - output.content_region.y <= 2
            app.assistant_say('You: Keep the public interface unchanged.')
            app.assistant_say('I will check the parser and its tests.')
            prompt = app.query_one('#prompt', TextArea)
            prompt.load_text('An unsent follow-up.')
            await pilot.pause()
            assert 0 <= output.items[0].region.y - output.content_region.y <= 1
            assert output.scroll_y == 0
            output.stream({'channel': 'text', 'call_id': 'long', 'text': 'Observed paragraph\n\n'*35})
            await until(pilot, lambda: output.max_scroll_y > 0 and output.is_vertical_scroll_end, max_ticks=200)
            assert output.max_scroll_y > 0 and output.is_vertical_scroll_end
            assert output.live.region.bottom <= output.region.bottom
            output.scroll_home(animate=False); await pilot.pause()
            output.stream({'channel': 'text', 'call_id': 'long', 'text': 'Another observed line\n'*5})
            await pilot.pause(.3)
            assert output.scroll_y == 0
            assert output.items[0].region.y >= output.region.y
            prompt.focus(); await pilot.press('ctrl+end'); await pilot.pause()
            assert output.is_vertical_scroll_end
            output.finish_stream()
            output.clear(); output.write('You: A new short conversation.')
            await pilot.pause()
            for width, height in [(130, 44), (80, 24)]:
                await pilot.resize_terminal(width, height); await pilot.pause()
                assert output.scroll_y == 0
                assert 0 <= output.items[0].region.y - output.content_region.y <= 1
            send = app.query_one('#start', Button)
            unfocused = send.styles.background
            send.focus(); await pilot.pause()
            assert send.styles.background != unfocused
            assert prompt.text == 'An unsent follow-up.' and not app.backend.launches
    asyncio.run(scenario())


def test_markdown_reply_keeps_visible_code_selectable_across_theme_and_resize(tmp_path):
    from textual.selection import Selection
    from textual.geometry import Offset
    async def scenario():
        app = WorkConsole(tmp_path, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=(130,44)) as pilot:
            await app.workers.wait_for_complete()
            output = app.query_one('#output',Transcript)
            output.write('You: **Keep this literal** [red]')
            output.stream({'channel':'text','call_id':'md','text':
                '## Result\n\nFixed **batching** in `batching.py`.\n\n- Preserved input\n- Passed tests\n\n```python\ndef chunks(items, size):\n    return [items[i:i + size] for i in range(0, len(items), size)]\n'})
            output.stream({'channel':'reasoning','text':'PRIVATE_REASONING'})
            rendered=lambda part: part in output.query_one(AssistantMessage).get_selection(Selection(None,None))[0]
            # The reply renders asynchronously; a loaded Windows runner needed more than a fixed 0.2 s.
            await until(pilot,lambda:rendered('def chunks(items, size):'),max_ticks=200)
            message=output.query_one(AssistantMessage)
            text=message.get_selection(Selection(None,None))[0]
            assert 'def chunks(items, size):' in text and '## Result' not in text
            assert '```' not in text and '**batching**' not in text
            assert 'PRIVATE_REASONING' not in text
            assert '**Keep this literal** [red]' in output.items[0].get_selection(Selection(None,None))[0]
            selected=output.items[0].get_selection(Selection(Offset(2,1),Offset(6,1)))[0]
            assert selected=='Keep'
            output.stream({'channel':'text','call_id':'md','text':'```\n\n[Docs](https://example.test/docs)\n'})
            output.finish_stream()
            await until(pilot,lambda:rendered('https://example.test/docs'),max_ticks=200)
            for theme in ('tare-light','tare-contrast'):
                app.theme=theme
                await until(pilot,lambda:rendered('return [items'),max_ticks=200)
            await pilot.resize_terminal(80,24)
            await until(pilot,lambda:message.region.width<=80 and rendered('def chunks(items, size):'),max_ticks=200)
            text=message.get_selection(Selection(None,None))[0]
            assert 'def chunks(items, size):' in text and 'Passed tests' in text
            assert message.region.width <= 80
            assert not app.backend.launches
    asyncio.run(scenario())


def test_assistant_uses_same_composer_for_inflight_messages(tmp_path, monkeypatch):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path.parent/'state'))
    async def scenario():
        backend = FakeBackend(); sent = []
        backend.send_message = lambda selection, text, identity: sent.append((selection.session, text))
        backend.message_status = lambda *args: 'IN_CONTEXT'
        app = WorkConsole(tmp_path, backend=backend, initial_mode='assistant')
        async with app.run_test(size=(100, 36)) as pilot:
            await app.workers.wait_for_complete()
            prompt = app.query_one('#prompt', TextArea)
            prompt.focus()
            prompt.load_text('Read the project'); app.execute(False); await pilot.pause()
            assert app.busy and not prompt.disabled
            assert app.focused is prompt
            assert '--stream' in backend.launches[0]
            assert not app.query_one('#message-options').display
            prompt.load_text('Focus on the tests'); app.execute(False)
            for _ in range(50):
                await pilot.pause(.05)
                if not app.sending_message:
                    break
            assert sent == [(app.active.session, 'Focus on the tests')]
            assert len(backend.launches) == 1 and not prompt.text
            assert any('Focus on the tests' in text for text in app.assistant_history)
            app.action_cancel(); await app.workers.wait_for_complete()
    asyncio.run(scenario())


def test_a_progress_sentence_replaces_its_live_preview_and_survives_reopening(tmp_path):
    async def scenario():
        app = WorkConsole(tmp_path, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=(100, 32)) as pilot:
            await app.workers.wait_for_complete()
            output = app.query_one('#output', Transcript)
            output.stream({'channel': 'text', 'call_id': 's/model/1', 'text': 'Vou mapear'})
            await pilot.pause()
            app.assistant_activity({'channel': 'narration', 'call_id': 's/model/1', 'text': 'Vou mapear o armazenamento.'})
            await pilot.pause()
            said = [w for w in output.items if isinstance(w, AssistantMessage)]
            assert output.live is None and len(output.items) == len(said) == 1  # no duplicate preview
            assert 'Vou mapear o armazenamento.' in app.assistant_history
    asyncio.run(scenario())
