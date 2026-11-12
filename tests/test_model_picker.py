import asyncio
import json

import pytest
from textual.widgets import Button, Input, OptionList, Select, TextArea

from relay import model_choices
from relay.model_picker import ModelPicker
from relay.native_console import WorkConsole
from test_native_console import FakeBackend
from test_model_choices import profile, catalog
from test_console_slash import edit, settle, shown


CHOICES = [
    {'key': 'astra', 'label': 'GPT-6 Astra', 'group': 'OpenAI', 'routes': [
        {'id':'direct','label':'Codex Direct','efforts':['low','high','max'],'default_effort':'high'},
        {'id':'cli','label':'Codex CLI','efforts':['low','high'],'default_effort':'low'}]},
    {'key': 'sonnet', 'label': 'Claude Sonnet 5', 'group': 'Anthropic', 'routes': [
        {'id':'claude','label':'Claude CLI','efforts':['low','medium','high'],'default_effort':'high'}]},
]


@pytest.mark.parametrize('size', [(80,24), (130,44)])
def test_picker_filters_connections_reasoning_keyboard_and_empty_results(tmp_path, monkeypatch, size):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))
    async def scenario():
        app = WorkConsole(tmp_path, backend=FakeBackend(), initial_mode='assistant')
        result = []
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            picker = ModelPicker(CHOICES, current={'key':'astra','connection':'direct','reasoning':'max'},
                                 preferences={'recent':['sonnet'],'models':{'sonnet':{'connection':'claude','reasoning':'medium'}}})
            app.push_screen(picker, result.append); await pilot.pause()
            assert picker.chosen['reasoning'] == 'max'
            reasoning = picker.query_one('#model-reasoning', Select)
            reasoning.focus(); await pilot.pause()
            current = reasoning.query_one('SelectCurrent')
            label = current.query_one('#label')
            def visible_label():
                assert label.region.width > 0 and label.region.height > 0
                return ''.join(segment.text for segment in label.render_line(0))
            assert 'max' in visible_label().lower()
            assert current.styles.border_top[0] == ''
            reasoning.expanded = True; await pilot.pause()
            assert current.styles.border_top[0] == ''
            assert 'max' in visible_label().lower()
            reasoning.expanded = False
            picker.query_one('#model-search', Input).focus(); await pilot.pause()
            for name in ('model-search','model-list','model-reasoning','model-apply','model-cancel'):
                assert app.screen.region.contains_region(picker.query_one('#'+name).region), name
            assert picker.visible_keys.count('sonnet') == 1
            picker.query_one('#model-connection', Select).value = 'cli'; await pilot.pause()
            assert picker.chosen['reasoning'] == 'default'
            search = picker.query_one('#model-search', Input)
            search.value = 'Sonnet'; await pilot.pause()
            assert picker.chosen == {'key':'sonnet','connection':'claude','reasoning':'medium'}
            assert not picker.query_one('#model-connection').display
            search.value = 'unknown'; await pilot.pause()
            assert picker.query_one('#model-apply', Button).disabled
            await pilot.press('enter'); await pilot.pause()
            assert app.screen is picker and not result
            search.value = 'astra'; await pilot.pause()
            await pilot.press('enter'); await pilot.pause()
            assert result == [{'key':'astra','connection':'direct','reasoning':'default'}]
            assert not app.backend.launches
    asyncio.run(scenario())


def test_apply_keeps_saved_session_and_resume_profile_then_new_conversation_uses_choice(tmp_path, monkeypatch):
    root = tmp_path/'project'; root.mkdir()
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))
    choices = catalog(monkeypatch, {'claude-cli': profile()})
    monkeypatch.setattr(model_choices, 'inventory', lambda: choices)
    async def scenario():
        app = WorkConsole(root, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=(80,24)) as pilot:
            await app.workers.wait_for_complete()
            session = tmp_path/'saved'; session.mkdir()
            saved = {'schema':'tare.tools/native-agent/2', 'config':{'repositories':{'repo':str(root)}},
                     'deployment':profile()}
            (session/'native-agent.json').write_text(json.dumps(saved))
            original = (session/'native-agent.json').read_bytes()
            app.assistant_session = str(session); app.update_mode()
            app.query_one('#prompt', TextArea).load_text('Keep my draft')
            app.action_model(); await pilot.pause()
            picker = app.screen
            picker.query_one('#model-reasoning', Select).value = 'max'; await pilot.pause()
            await pilot.click('#model-apply'); await pilot.pause()
            assert app.assistant_session == str(session)
            assert (session/'native-agent.json').read_bytes() == original
            assert app.query_one('#prompt', TextArea).text == 'Keep my draft'
            selected = app.selection()
            value = json.loads(__import__('pathlib').Path(selected.profile).read_text())
            assert json.loads(value['native_profile']['parameters_json'])['effort'] == 'max'
            from relay.console_backend import command
            from dataclasses import replace
            args = command(replace(selected, mode='session', session=str(session)), resume=True)
            assert '--native-deployment' not in args and '--deployment' not in args
            args = command(replace(selected, mode='session', session=''), resume=False)
            assert args[args.index('--native-deployment')+1] == selected.profile
            assert not app.backend.launches
    asyncio.run(scenario())


def test_switching_models_refreshes_the_visible_default_reasoning_label(tmp_path):
    choices = [{'key': 'fixed', 'label': 'Fixed model', 'group': 'Local', 'routes': [
        {'id': 'fixed', 'label': 'Local', 'efforts': [], 'default_effort': ''}]},
        {'key': 'argon', 'label': 'Gemini 4 Argon', 'group': 'Google', 'routes': [
        {'id': 'agy', 'label': 'AGY CLI', 'efforts': ['low', 'medium', 'high'], 'default_effort': 'high'}]}]
    async def scenario():
        app = WorkConsole(tmp_path, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=(130, 44)) as pilot:
            await app.workers.wait_for_complete()
            picker = ModelPicker(choices, current={'key': 'fixed', 'connection': 'fixed', 'reasoning': 'default'})
            app.push_screen(picker)
            await pilot.pause()
            reasoning = picker.query_one('#model-reasoning', Select)
            search = picker.query_one('#model-search', Input)
            def visible():
                return ''.join(s.text for s in reasoning.query_one('SelectCurrent #label').render_line(0))
            assert 'Profile default' in visible() and reasoning.disabled
            search.value = 'Argon'
            await pilot.pause()
            assert picker.chosen['reasoning'] == 'default'
            assert 'Default \u00b7 High' in visible() and not reasoning.disabled
            search.value = 'Fixed'
            await pilot.pause()
            assert 'Profile default' in visible() and reasoning.disabled
            assert not app.backend.launches
    asyncio.run(scenario())


def test_reasoning_command_completion_cancel_and_modal_blocks_global_send(tmp_path, monkeypatch):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))
    monkeypatch.setattr(model_choices, 'inventory', lambda: CHOICES)
    async def scenario():
        app = WorkConsole(tmp_path, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=(80,24)) as pilot:
            await app.workers.wait_for_complete()
            prompt = app.query_one('#prompt', TextArea)
            await edit(pilot, prompt, '/reas', (0,5))
            assert shown(app) == ['/reasoning']
            await pilot.press('tab'); await settle(pilot)
            assert prompt.text == '/reasoning'
            await pilot.press('ctrl+enter'); await pilot.pause()
            assert isinstance(app.screen, ModelPicker)
            assert app.focused.id == 'model-reasoning'
            await pilot.press('ctrl+enter','f2'); await pilot.pause()
            assert isinstance(app.screen, ModelPicker) and not app.backend.launches
            await pilot.press('escape'); await pilot.pause()
            assert not isinstance(app.screen, ModelPicker)
            assert not app.query_one('#profile', Input).value
    asyncio.run(scenario())


@pytest.mark.parametrize('size', [(150, 40), (80, 24)])
def test_model_button_grows_with_its_label_and_ellipsizes_only_when_the_row_is_full(tmp_path, size):
    from relay.console_header import HeaderDetails
    async def scenario():
        app = WorkConsole(tmp_path, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            button = app.query_one('#choose-model', Button)
            row = app.query_one('#model-controls')
            def shown_label():
                return ''.join(s.text for s in button.render_line(0)).strip()
            app.render_model_controls(HeaderDetails('main', 'x', 'Saved model', display_model='Claude Sonnet 5.5 Extended'))
            await pilot.pause()
            assert shown_label() == 'Claude Sonnet 5.5 Extended \u25be'  # was cut at the width of the first label
            app.render_model_controls(HeaderDetails('main', 'Reading saved model\u2026', 'Saved model'))
            # Checked at once: after a pause the app's own header refresh may have relabelled the button.
            assert str(button.label) == 'Claude Sonnet 5.5 Extended \u25be'  # the name stays while the header reads the model
            app.render_model_controls(HeaderDetails('main', 'x', 'Saved model', display_model='very-long-model-name-' * 5))
            await pilot.pause()
            assert row.region.contains_region(button.region)
            assert shown_label().endswith('\u2026')  # an ellipsis, not a cut mid-word
    asyncio.run(scenario())
