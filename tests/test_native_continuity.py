import json
from pathlib import Path

import pytest

from compute_plane.native_journal import read_json, save_json
from relay import native_agent, native_continuity
from relay.native_messages import enqueue
from relay.native_profiles import usage
from tare_tools_agent_runtime import ModelFailure
from test_native_agent import Client, deployment, setup, steps


class QuotaClient(Client):
    def post(self, path, body, control):
        if path not in {'/apply-template', '/tokenize'} and self.generations == 2:
            self.generations += 1
            raise ModelFailure('HTTP_RATE_LIMIT', retryable=False)
        return super().post(path, body, control)


def exhausted(setup, *, calls=12):
    config, custody, root = setup
    client = QuotaClient(steps=steps())
    result = native_agent.run_session(config, deployment(), custody, 'Set value to 2 and verify. Keep a.py only.',
        client=client, local_counter=client, accept_messages=True, limits={'max_model_calls': calls, 'max_seconds': None})
    assert result['status'] == 'BLOCKED'
    assert (root/'a.py').read_text() == 'value = 2\n'
    return custody, root


def test_quota_switch_protocol_preserves_files_history_usage_and_remaining_budget(setup):
    custody, root = exhausted(setup)
    enqueue(custody, 'Also explain the verification.', message_id='late', project=root)
    view = native_continuity.preview(custody, root)
    assert 'Also explain' in view['text']
    assert 'value = 1' not in view['text']
    assert 'reasoning retained' not in view['text']
    target = custody.parent/'successor'
    native_continuity.save_request(target, view, view['text'], deployment('bedrock-converse'))
    client = Client('bedrock-converse', [
        ('conversation_history', {}),
        ('read', {'repository': 'repo', 'path': 'a.py'}),
        ('verify', {'name': 'unit'}), ('tare_deliver', {'summary': 'Verified existing change.'})])
    result = native_agent.run_session(None, None, target, client=client, local_counter=client)
    assert result['status'] == 'PROPOSED', result
    assert result['model_calls'] == 4
    state = read_json(target/'native-agent.json')
    assert state['budget']['max_model_calls'] == 9
    assert state['config'] == read_json(custody/'native-agent.json')['config']
    history = read_json(target/'continuity-history.json')['rows']
    assert any(r['kind'] == 'queued_user' for r in history)
    assert 'reasoning retained' not in json.dumps(history)
    assert 'opaque' not in json.dumps(history)
    first = next(body for path, body in client.calls if path.endswith('/converse'))
    assert 'value = 1' not in json.dumps(first)
    totals = usage(target)
    assert totals['model_calls'] == 7
    assert totals['provider_sessions'] == 2
    assert totals['effective_limits']['max_model_calls'] == 12
    assert totals['tokens']['total_tokens']['unknown_calls'] == 1
    with pytest.raises(ValueError, match='successor'):
        native_agent.run_session(None, None, custody, resume=True, client=Client())
    with pytest.raises(ValueError, match='successor'):
        enqueue(custody, 'late stale send', project=root)
    # Reopen the successor: it replays its completed outcome without another call.
    assert native_agent.run_session(None, None, target, resume=True, client=Client(), local_counter=Client())['status'] == 'PROPOSED'
    third = custody.parent/'third'
    view = native_continuity.preview(target, root)
    assert view['text'].count('Objective and user constraints:') == 1
    assert view['text'].count('Pending: finish the objective') == 1
    assert 'Set value to 2 and verify' in view['text'] and 'Also explain the verification.' in view['text']
    native_continuity.save_request(third, view, view['text'], deployment())
    client = Client(steps=[('tare_deliver', {'summary': 'Verification retained in history.'})])
    assert native_agent.run_session(None, None, third, client=client, local_counter=client)['status'] == 'PROPOSED'
    assert read_json(third/'native-agent.json')['budget']['max_model_calls'] == 5
    assert usage(third)['model_calls'] == 8
    assert usage(third)['provider_sessions'] == 3


def test_stale_preview_and_exhausted_budget_never_call_new_provider(setup):
    custody, root = exhausted(setup, calls=3)
    view = native_continuity.preview(custody, root)
    target = custody.parent/'successor'
    native_continuity.save_request(target, view, view['text'], deployment())
    client = Client()
    with pytest.raises(ValueError, match='call limit is exhausted'):
        native_agent.run_session(None, None, target, client=client)
    assert client.generations == 0
    enqueue(custody, 'New constraint', project=root)
    with pytest.raises(ValueError, match='conversation changed'):
        native_agent.run_session(None, None, target, client=client)
    assert not (target/'native-agent.json').exists()


def test_cli_continuation_reads_full_saved_deployment(setup, monkeypatch):
    custody, root = exhausted(setup)
    state = read_json(custody/'native-agent.json')
    # Real vendor catalogs can exceed the small standalone-config file budget.
    state['deployment']['catalog_note'] = 'x' * 70000
    save_json(custody/'native-agent.json', state)
    view = native_continuity.preview(custody, root)
    target = custody.parent/'large-catalog-successor'
    successor = {**deployment(), 'catalog_note': 'y' * 70000}
    native_continuity.save_request(target, view, view['text'], successor)
    observed = []
    original_run = native_agent.run_session
    client = Client(steps=[('tare_deliver', {'summary': 'Continuation started.'})])

    def run(config, selected, directory, prompt, **options):
        observed.append((config, selected, directory, prompt))
        return original_run(config, selected, directory, prompt,
                            client=client, local_counter=client, **options)

    monkeypatch.setattr(native_agent, 'run_session', run)
    native_agent.main(['--continue-provider', '--session', str(target)])
    assert observed == [(state['config'], successor, target, view['text'])]
    assert read_json(target/'native-agent.json')['continuation']['source'] == str(custody)
    assert client.generations > 0


def test_oversized_user_requirements_are_not_silently_trimmed(setup):
    custody, root = exhausted(setup)
    enqueue(custody, 'Constraint: '+('é' * 5000), project=root)
    view = native_continuity.preview(custody, root)
    assert view['bytes'] > native_continuity.MAX_BRIEF_BYTES
    assert 'é' * 5000 in view['text']
    with pytest.raises(ValueError, match='8192'):
        native_continuity.save_request(custody.parent/'large', view, view['text'], deployment())


def test_prepared_resume_keeps_project_scope_and_does_not_discard_new_draft(setup):
    from relay.console_backend import Selection, command
    custody, root = exhausted(setup)
    view = native_continuity.preview(custody, root)
    target = custody.parent/'successor'
    native_continuity.save_request(target, view, view['text'], deployment())
    selected = Selection(str(root), session=str(target))
    assert '--continue-provider' in command(selected, resume=True)
    selected.prompt = 'New requirement after preparing the switch'
    with pytest.raises(ValueError, match='new draft'):
        command(selected, resume=True)
    selected.prompt = ''
    selected.root = str(root.parent)
    with pytest.raises(ValueError, match='another project'):
        command(selected, resume=True)


@pytest.mark.parametrize('boundary', ['parent_link', 'history_read'])
def test_interrupted_switch_reopens_without_repeating_completed_effects(setup, monkeypatch, boundary):
    custody, root = exhausted(setup)
    view = native_continuity.preview(custody, root)
    target = custody.parent/'successor'
    native_continuity.save_request(target, view, view['text'], deployment())
    client = Client(steps=[('conversation_history', {'record': 0}), ('tare_deliver', {'summary': 'Continued.'})])
    with monkeypatch.context() as patch:
        if boundary == 'parent_link':
            save = native_agent.save_json
            def crash(path, value, **kw):
                if Path(path) == custody/'native-agent.json' and value.get('continued_to'):
                    raise KeyboardInterrupt('crash between child and parent')
                return save(path, value, **kw)
            patch.setattr(native_agent, 'save_json', crash)
        else:
            def crash(*args):
                raise KeyboardInterrupt('crash before saving immutable history page')
            patch.setattr(native_continuity.HistoryTools, '_read', crash)
        with pytest.raises(KeyboardInterrupt):
            native_agent.run_session(None, None, target, client=client, local_counter=client)
    resumed = native_agent.run_session(None, None, target, resume=True, client=client, local_counter=client)
    assert resumed['status'] == 'PROPOSED', resumed
    assert client.generations == 2
    assert read_json(custody/'native-agent.json')['continued_to'] == str(target)
    assert (root/'a.py').read_text() == 'value = 2\n'


def test_expired_time_limit_and_unresolved_effect_block_before_dispatch(setup):
    from compute_plane.native_journal import NativeJournal
    custody, root = exhausted(setup)
    state = read_json(custody/'native-agent.json')
    state['deadline_utc'] = 1
    state['timeout'] = state['limits']['max_seconds'] = 60
    save_json(custody/'native-agent.json', state)
    view = native_continuity.preview(custody, root)
    target = custody.parent/'successor'
    native_continuity.save_request(target, view, view['text'], deployment())
    client = Client()
    with pytest.raises(ValueError, match='time limit expired'):
        native_agent.run_session(None, None, target, client=client, local_counter=client)
    NativeJournal(custody/'journal.jsonl').append({'kind': 'effect_requested', 'effect_id': 'unresolved',
        'tool_call': {'name': 'verify', 'call_id': 'old', 'arguments_json': '{"name":"unit"}'}})
    with pytest.raises(ValueError, match='reconcile unfinished tools'):
        native_continuity.preview(custody, root)
    assert not client.calls


@pytest.mark.parametrize('size', [(80, 24), (140, 46)])
def test_brief_panel_requires_explicit_bounded_input(tmp_path, monkeypatch, size):
    import asyncio
    from textual.widgets import Button, TextArea
    from relay.continuity_panel import ContinuityPanel
    from relay.native_console import WorkConsole
    from test_native_console import FakeBackend
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))
    async def scenario():
        app = WorkConsole(tmp_path, backend=FakeBackend(), initial_mode='assistant')
        result = []
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            panel = ContinuityPanel('é' * 5000, 'Selected provider')
            app.push_screen(panel, result.append)
            await pilot.pause()
            button = panel.query_one('#continuity-go', Button)
            assert button.disabled
            assert app.screen.region.contains_region(button.region)
            panel.query_one(TextArea).load_text('Keep the requirement. Finish the pending check.')
            # TextArea.Changed is queued on a different widget; wait for the
            # visible validation result, not one idle turn of the event loop.
            for _ in range(40):
                await pilot.pause(.05)
                if not button.disabled:
                    break
            assert not button.disabled
            await pilot.click('#continuity-go')
            assert result == ['Keep the requirement. Finish the pending check.']
    asyncio.run(scenario())
