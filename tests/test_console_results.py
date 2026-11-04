"""Answer-first presentation keeps evidence, history and drafts accessible."""
import asyncio
import json
import os
import sys

import pytest
from textual.selection import Selection
from textual.widgets import Collapsible, TextArea

from relay import relay_mesh
from relay.console_output import AssistantMessage, ExecutionResult, SelectableMessage, Transcript
from relay.native_console import WorkConsole
from relay.native_display import conversation_result, conversation_status
from test_native_console import FakeBackend


def test_result_summary_cannot_promote_model_claims_or_stale_checks():
    result = {'status': 'PROPOSED', 'output': 'All tests passed.',
              'capability_observations': {'effects': 2, 'verifications': {
                  'current': {'returncode': 0, 'effects': 2},
                  'old': {'returncode': 0, 'effects': 1},
                  'failed': {'returncode': 1, 'effects': 2},
                  'unknown': {'effects': 2}}},
              'model_calls': 7, 'tokens': {'total_tokens': {'known_sum': 600, 'unknown_calls': 1}}}
    view = conversation_result(result)
    assert view['body'] == 'All tests passed.'
    assert view['notice'] == 'Named checks this turn: 1 failed · 1 passed · 1 to recheck · 1 unknown'
    assert view['attention'] and '600 (partial)' in view['details']
    assert 'All tests passed.' not in view['details']
    assert conversation_status(result) == 'Response ready'
    changed = conversation_result({**result, 'source_changes': [
        {'repository': 'project', 'path': 'a.py', 'current_sha256': 'new'}]})
    assert '1 passed' in changed['notice'] and '1 to recheck' in changed['notice']
    assert 'Earlier checks do not verify' in changed['notice']
    assert 'a.py' in changed['details']
    assert conversation_result({'status': 'PROPOSED'})['notice'] == ''
    cancelled = conversation_result({'status': 'CANCELLED', 'reason': 'CANCEL_REQUESTED'})
    assert 'Earlier changes may still be present' in cancelled['notice']
    assert 'CANCEL_REQUESTED' in cancelled['details']
    blocked = conversation_result({'status': 'RECONCILIATION_REQUIRED', 'reason': 'unknown effect'})
    assert 'Recovery needs review' in blocked['notice'] and 'unknown effect' in blocked['details']
    bad_exit = conversation_result(result, exit_code=2)
    assert 'Process exited with code 2' in bad_exit['notice'] and 'Process exit: 2' in bad_exit['details']
    assert 'error' in conversation_status(result, exit_code=2)


def test_reply_is_visible_before_deferred_layout_notifications(tmp_path, monkeypatch):
    from textual import events
    monkeypatch.setattr(relay_mesh, 'RELAY_ROOT', tmp_path/'control')

    async def scenario():
        app = WorkConsole(tmp_path, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=(130, 44)) as pilot:
            await app.workers.wait_for_complete()
            message = AssistantMessage('Fixed **batching**.\n\n```python\nreturn items[start:start + size]\n```',
                                       classes='message reply')
            # Reproduce a nested message whose deferred mount/resize notifications
            # have not painted it yet. Source visibility must not depend on them.
            callbacks = []
            message.call_after_refresh = lambda callback, *a, **kw: callbacks.append((callback, a, kw)) or True
            message.disable_messages(events.Resize)
            app.query_one('#output', Transcript).append(message)
            await pilot.pause()
            assert message.is_mounted and message.content_size.width > 0
            answer = message.get_selection(Selection(None, None))[0]
            assert 'return items[start:start + size]' in answer
            assert '**batching**' not in answer
            message.enable_messages(events.Resize)
            for callback, args, kwargs in callbacks:
                callback(*args, **kwargs)
            await pilot.resize_terminal(80, 24)
            await pilot.pause()
            assert message.render_width == message.content_size.width
            assert 'return items[start:start + size]' in message.get_selection(Selection(None, None))[0]
    asyncio.run(scenario())


def test_claude_rate_limit_notice_is_visible_in_console(tmp_path, monkeypatch):
    state = tmp_path/'state'
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(state))
    monkeypatch.setattr(relay_mesh, 'RELAY_ROOT', tmp_path/'control')
    result = {'status': 'BLOCKED', 'reason': 'MODEL_FAILURE',
              'failure_code': 'CLI_GENERATION_FAILED',
              'provider_rate_limit_observed': True}

    async def scenario():
        backend = FakeBackend()
        async def communicate():
            backend.process.returncode = 2
            return json.dumps(result).encode(), b''
        backend.process.communicate = communicate
        app = WorkConsole(tmp_path, backend=backend,
                          initial_mode='assistant', storage_preferred=state)
        async with app.run_test(size=(130, 44)) as pilot:
            await app.workers.wait_for_complete()
            app.query_one('#prompt', TextArea).load_text('Fix the failed request.')
            app.execute(False)
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.last_result == result
            block = app.query_one(ExecutionResult)
            notice = str(block.query_one('.result-notice').render())
            assert 'Claude reported an API rate limit' in notice
            assert 'Continue task to choose another provider' in notice
            assert 'quota exhausted' not in notice.lower()
            assert 'Failure: CLI_GENERATION_FAILED' in block.value['details']

    asyncio.run(scenario())


@pytest.mark.parametrize('size', [(80, 24), (130, 44)])
@pytest.mark.parametrize('theme', ['tare-dark', 'tare-light', 'tare-contrast'])
def test_result_details_are_selectable_and_survive_restart_with_legacy_text(tmp_path, monkeypatch, size, theme):
    project = tmp_path/'project'; project.mkdir()
    state = tmp_path/'state'
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(state))
    monkeypatch.setattr(relay_mesh, 'RELAY_ROOT', tmp_path/'control')
    result = {'status': 'PROPOSED', 'output': 'Fixed **batching** in `batching.py`.\n\n'
              '```python\nreturn items[start:start + size]\n```\n\nThe existing input order is preserved.',
              'session': str(state/'agent-sessions'/'a-long-internal-session-location'),
              'capability_observations': {'effects': 1, 'verifications': {
                  'unit tests': {'returncode': 0, 'effects': 1}}},
              'model_calls': 7, 'tokens': {'total_tokens': {'known_sum': 600, 'unknown_calls': 1}}}

    async def scenario():
        backend = FakeBackend()
        async def communicate():
            backend.process.returncode = 0
            return json.dumps(result).encode(), b''
        backend.process.communicate = communicate
        app = WorkConsole(project, backend=backend, initial_mode='assistant', storage_preferred=state)
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            app.theme = theme
            app.assistant_say('A saved message from the older text-only history.')
            app.query_one('#prompt', TextArea).load_text('Fix the batching bug and run the checks.')
            app.execute(False)
            await app.workers.wait_for_complete(); await pilot.pause()
            assert app.last_result == result
            assert 'Response ready' in str(app.query_one('#status').render())
            assert 'PROPOSED' not in str(app.query_one('#status').render())
            output = app.query_one('#output', Transcript)
            block = output.query_one(ExecutionResult)
            details = block.query_one(Collapsible)
            assert details.collapsed
            message = block.query_one(AssistantMessage)
            for _ in range(200):  # Markdown renders after the result; a loaded Linux runner took more than 2 s
                answer = message.get_selection(Selection(None, None))[0]
                if 'return items[start:start + size]' in answer:
                    break
                await pilot.pause(.05)
            assert 'return items[start:start + size]' in answer and 'Session:' not in answer, (
                answer, message.region, message.content_size, message.render_width, block.region)
            assert '**batching**' not in answer
            assert '1 passed' in str(block.query_one('.result-notice').render())
            await pilot.pause()
            assert output.max_scroll_y == 0 or output.is_vertical_scroll_end
            assert block.query_one('.result-notice').region.bottom <= output.region.bottom
            assert details.region.height == 1
            draft = 'Keep this question unsent.'
            app.query_one('#prompt', TextArea).load_text(draft)
            app.checkpoint('draft_saved')
            identity = app.demand_id
            await pilot.pause()
            if lot := os.environ.get('TARE_EXPERIMENT_RUN_DIR'):
                app.save_screenshot(filename=f'answer-first-{sys.platform}-{size[0]}-{theme}.svg', path=lot)
            details.query_one('CollapsibleTitle').focus()
            await pilot.press('enter'); await pilot.pause()
            assert not details.collapsed
            copied = details.query_one(SelectableMessage).get_selection(Selection(None, None))[0]
            assert '600 (partial)' in copied and 'a-long-internal-session-location' in copied
            assert 'Status: PROPOSED' in copied
            assert app.query_one('#prompt', TextArea).text == draft
            clipboard = []
            app.copy_to_clipboard = clipboard.append
            app.controls(True)  # Reading/copying an earlier result stays available during work.
            details.query_one('.copy-result-details').focus()
            await pilot.press('enter'); await pilot.pause()
            assert clipboard == [block.value['details']]
            assert result['session'] in clipboard[0]  # Copy keeps the unwrapped path.
            app.controls(False)
            await pilot.pause()
            if lot:
                app.save_screenshot(filename=f'answer-details-{sys.platform}-{size[0]}-{theme}.svg', path=lot)
            details.query_one('CollapsibleTitle').focus()
            await pilot.press('enter'); await pilot.pause()
            assert details.collapsed and len(backend.launches) == 1
        reopened = WorkConsole(project, backend=backend, initial_mode='assistant', storage_preferred=state)
        async with reopened.run_test(size=size) as pilot:
            await reopened.workers.wait_for_complete()
            reopened.action_conversations(); await reopened.workers.wait_for_complete()
            reopened.open_conversation(next(i for i, row in enumerate(reopened.navigation_shown) if row['id'] == identity))
            await reopened.workers.wait_for_complete(); await pilot.pause()
            assert len(backend.launches) == 1
            block = reopened.query_one(ExecutionResult)
            assert block.query_one(Collapsible).collapsed
            assert 'batching.py' in block.query_one(AssistantMessage).source
            assert reopened.assistant_history[0] == 'A saved message from the older text-only history.'
            assert reopened.query_one('#prompt', TextArea).text == draft
    asyncio.run(scenario())


@pytest.mark.parametrize('changes,effects,expected', [
    ([], 0, False),
    ([{'repository': 'project', 'path': 'a.py', 'current_sha256': 'new'}], 0, True),
    ([], 1, True),
])
def test_review_hint_requires_observed_changes_or_effects(tmp_path, monkeypatch, changes, effects, expected):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))
    monkeypatch.setattr(relay_mesh, 'RELAY_ROOT', tmp_path/'control')
    result = {'status': 'PROPOSED', 'output': 'A direct answer.',
              'source_changes': changes, 'capability_observations': {'effects': effects}}

    async def scenario():
        backend = FakeBackend()
        async def communicate():
            backend.process.returncode = 0
            return json.dumps(result).encode(), b''
        backend.process.communicate = communicate
        app = WorkConsole(tmp_path, backend=backend, initial_mode='assistant',
                          storage_preferred=tmp_path/'state')
        async with app.run_test(size=(130, 44)) as pilot:
            await app.workers.wait_for_complete()
            app.query_one('#prompt', TextArea).load_text('Explain this.')
            app.execute(False)
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert ('F3 review changes' in str(app.query_one('#status').render())) is expected
            if not expected:
                assert app.query_one(ExecutionResult).value['notice'] == ''
    asyncio.run(scenario())


def test_a_delivered_change_is_announced_once_by_the_status_line():
    result = {'status': 'PROPOSED', 'output': 'Adicionei o comando count.',
              'conversation_task': {'stage': 'DELIVERED', 'intent': 'deliver'},
              'capability_observations': {'effects': 1, 'verifications': {'tests': {'returncode': 0, 'effects': 1}}}}
    assert conversation_status(result) == 'Change verified and reviewed'
    assert conversation_result(result)['notice'] == ''  # no English sentence repeating it under the reply
