"""Finite conversation budgets stay explicit and never rewrite saved custody."""
import asyncio
from dataclasses import asdict
import json

import pytest
from textual.widgets import Checkbox, Input

from relay.console_backend import Selection, command
from relay.native_console import WorkConsole


def test_finite_budget_flags_precede_verifier_and_blank_inherits(tmp_path, monkeypatch):
    monkeypatch.setattr('relay.native_agent.session_root', lambda: tmp_path/'custody')
    selection = Selection(str(tmp_path), role='implementer', write_paths=['.'], prompt='Implement', unlimited=False,
                          max_model_calls='32', max_seconds='600', verifier='["python", "check.py"]')
    args = command(selection)
    assert args[args.index('--max-model-calls')+1] == '32'
    assert args[args.index('--max-seconds')+1] == '600.0'
    assert args[-3:] == ['--verify-command', 'python', 'check.py']
    assert '--unlimited' not in args
    selection.max_model_calls = selection.max_seconds = ''
    args = command(selection)
    assert '--max-model-calls' not in args and '--max-seconds' not in args


@pytest.mark.parametrize('name,value', [('max_model_calls', '0'), ('max_model_calls', '1.5'),
    ('max_model_calls', 'unlimited'), ('max_seconds', '-1'), ('max_seconds', 'nan'),
    ('max_seconds', 'inf'), ('max_seconds', 'none')])
def test_invalid_finite_budget_refused_before_creating_session(tmp_path, name, value):
    selected = Selection(str(tmp_path), prompt='Task', unlimited=False, **{name: value})
    with pytest.raises(ValueError, match='must be a positive'):
        command(selected)
    assert not selected.session


def test_resume_ignores_new_budget_fields_and_keeps_saved_custody(tmp_path):
    session = tmp_path/'session'; session.mkdir()
    path = session/'native-agent.json'
    original = json.dumps({'config': {'repositories': {'repo': str(tmp_path)}},
                           'limits': {'max_model_calls': 16, 'max_seconds': 300}})
    path.write_text(original)
    selected = Selection(str(tmp_path), session=str(session), unlimited=False,
                         max_model_calls='32', max_seconds='600')
    args = command(selected, resume=True)
    assert '--max-model-calls' not in args and '--max-seconds' not in args
    assert path.read_text() == original


def test_unlimited_choice_ignores_inactive_finite_fields(tmp_path, monkeypatch):
    monkeypatch.setattr('relay.native_agent.session_root', lambda: tmp_path/'custody')
    args = command(Selection(str(tmp_path), prompt='Task', unlimited=True,
                             max_model_calls='32', max_seconds='600'))
    assert '--unlimited' in args
    assert '--max-model-calls' not in args and '--max-seconds' not in args


def test_tui_budget_fields_roundtrip_and_follow_new_conversation(tmp_path):
    class Backend:
        def catalog(self, root):
            return {'models': [{'deployment': 'local-qwen', 'model': 'Qwen'}],
                    'sessions': [], 'works': [], 'errors': []}

    async def scenario():
        app = WorkConsole(tmp_path, backend=Backend())
        async with app.run_test(size=(130, 44)) as pilot:
            await pilot.pause()
            assert app.query_one('#max-model-calls', Input).disabled
            selected = app.selection()
            selected.unlimited = False
            selected.max_model_calls = '32'
            selected.max_seconds = '600'
            app.restore_selection(asdict(selected))
            await pilot.pause()
            assert not app.query_one('#max-model-calls', Input).disabled
            assert not app.query_one('#max-seconds', Input).disabled
            assert app.selection().max_model_calls == '32'
            assert app.selection().max_seconds == '600'
            app.new_demand()
            await pilot.pause()
            assert app.selection().max_model_calls == '32'
            assert app.selection().max_seconds == '600'
            app.query_one('#unlimited', Checkbox).value = True
            await pilot.pause()
            app.controls(True)
            app.controls(False)
            for name in ('max-model-calls', 'max-seconds'):
                assert app.query_one('#'+name, Input).disabled
            app.query_one('#unlimited', Checkbox).value = False
            await pilot.pause()
            app.controls(True)
            for name in ('max-model-calls', 'max-seconds'):
                assert app.query_one('#'+name, Input).disabled
            app.controls(False)
            for name in ('max-model-calls', 'max-seconds'):
                assert not app.query_one('#'+name, Input).disabled

    asyncio.run(scenario())


def test_a_budget_toggle_arriving_while_the_screen_closes_is_ignored(tmp_path):
    async def scenario():
        from test_native_console import FakeBackend
        app = WorkConsole(tmp_path, backend=FakeBackend())
        async with app.run_test(size=(130, 44)) as pilot:
            await pilot.pause()
            for widget in list(app.screen.query('#unlimited')):
                await widget.remove()  # what teardown does before a late Changed message
            app.sync_budget_fields()
    asyncio.run(scenario())
