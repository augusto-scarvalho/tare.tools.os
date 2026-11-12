"""/guidance: the owner's edits of tare's role guidance and choices about what each vendor CLI adds on its own."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from textual.widgets import Button, Select, TextArea

from relay import guidance, native_agent
from relay.console_guidance import GuidancePanel
from relay.native_console import WorkConsole
from test_native_agent import Client, deployment, setup, steps  # noqa: F401  (setup is a fixture)
from test_native_console import FakeBackend

IMPLEMENTER = 'role#role/implementer#system'
EMAIL = 'vendor#cli://claude#user_email'
DATE = 'vendor#cli://claude#date'


def test_choices_round_trip_one_by_one_and_back_to_every_default():
    assert not guidance.path().exists() and guidance.vendor_choices('cli://claude') == {}
    guidance.save(IMPLEMENTER, 'Implement carefully.')
    guidance.save(EMAIL, None)
    guidance.save(DATE, 'Today is a working day.')
    saved = json.loads(guidance.path().read_text(encoding='utf-8'))
    assert saved == {'schema': guidance.SCHEMA, 'capsules': {'role/implementer': {'system': 'Implement carefully.'}},
                     'vendor': {'cli://claude': {'user_email': None, 'date': 'Today is a working day.'}}}
    states = {item['id']: item['state'] for item in guidance.items()}
    assert (states[IMPLEMENTER], states[EMAIL], states[DATE]) == ('yours', 'removed', 'yours')
    for refused in [(EMAIL, 'another text'),  # Claude Code can only drop the email block
                    ('vendor#cli://codex#environment_context', 'another text'),  # Codex never rewrites
                    ('transport#claude-native', 'another text'), ('vendor#cli://agy#anything', None)]:
        with pytest.raises(ValueError):
            guidance.save(*refused)
    assert json.loads(guidance.path().read_text(encoding='utf-8')) == saved  # a refused save changes nothing
    guidance.reset(DATE)
    assert guidance.vendor_choices('cli://claude') == {'user_email': None}
    guidance.save(IMPLEMENTER, '  ')  # an empty edit is tare's default
    assert guidance.capsule_edits() == {}
    guidance.reset()
    assert not guidance.path().exists()
    assert all(item['state'] == 'default' for item in guidance.items())


def test_the_panel_lists_role_guidance_vendor_blocks_and_protocol_texts():
    rows = {item['id']: item for item in guidance.items()}
    assert rows[IMPLEMENTER]['owner'] == 'tare' and rows[IMPLEMENTER]['controls'] == ('edit',)
    assert rows[EMAIL]['owner'] == 'vendor' and rows[EMAIL]['controls'] == ('remove',)
    assert '<account email>' in rows[EMAIL]['default'] and rows[EMAIL]['measured'] == 'Claude Code 2.1.322'
    assert rows['vendor#cli://claude#identity']['controls'] == ()
    assert rows['transport#claude-native']['controls'] == () and rows['transport#claude-native']['text']
    assert not any(key.startswith('vendor#cli://agy#') for key in rows)  # AGY cannot be inspected


def test_a_role_edit_reaches_new_sessions_and_reset_restores_tares_exact_capsule(setup):  # noqa: F811
    config, custody, root = setup
    before = native_agent.prompt_capsule(deployment(), 'implementer')
    guidance.save(IMPLEMENTER, 'Implement only what the user asked; say what you did not check.')
    edited = native_agent.prompt_capsule(deployment(), 'implementer')
    assert edited['system'] == 'Implement only what the user asked; say what you did not check.'
    assert edited['version'] == before['version'] + '-edited' and edited['sha256'] != before['sha256']
    client = Client(steps=steps())
    native_agent.run_session(config, deployment(), custody, 'Change the value to 2.', client=client,
                             local_counter=client)
    first = next(body for path, body in client.calls if path == '/chat/completions')
    assert 'Implement only what the user asked; say what you did not check.' in first['messages'][0]['content']
    assert before['system'] not in first['messages'][0]['content']
    frozen = json.loads((custody/'native-agent.json').read_bytes())['prompt_capsule']
    assert frozen['sha256'] == edited['sha256']
    guidance.reset(IMPLEMENTER)
    assert native_agent.prompt_capsule(deployment(), 'implementer') == before


def test_the_vendor_choices_reach_the_cli_client(tmp_path):
    from relay.native_generation import inference_client
    guidance.save(EMAIL, None)
    executable = str(tmp_path/'claude.exe')
    profile = SimpleNamespace(protocol='vendor-cli', endpoint='cli://claude')
    client = inference_client({'native_cli': {'executable': executable, 'version': '2.1.322'}}, profile,
                              credential=None, metadata_only=True)
    assert client.vendor_guidance == {'user_email': None}
    codex = inference_client({'native_cli': {'executable': executable, 'version': '0.166.0',
                                             'model_catalog': {'slug': 'fixture'}}},
                             SimpleNamespace(protocol='vendor-cli', endpoint='cli://codex'),
                             credential=None, metadata_only=True)
    assert codex.vendor_guidance == {}  # no choice: exactly what tare sent before


@pytest.mark.parametrize('size', [(80, 24), (130, 44)])
def test_guidance_in_the_tui_edits_removes_and_goes_back_to_each_default_without_a_model_call(tmp_path, size):
    async def scenario():
        app = WorkConsole(tmp_path, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            assert app.dispatch_conversation_command('/guidance', '')
            await pilot.pause()
            panel = app.screen
            assert isinstance(panel, GuidancePanel)
            choice, text = panel.query_one('#guidance-item', Select), panel.query_one('#guidance-text', TextArea)
            buttons = {b.id: b for b in panel.query(Button)}
            assert panel.region.contains_region(buttons['guidance-save'].region)

            choice.value = 'transport#claude-native'
            await pilot.pause()
            assert text.read_only and buttons['guidance-save'].disabled and buttons['guidance-remove'].disabled

            choice.value = EMAIL
            await pilot.pause()
            assert text.read_only and not buttons['guidance-remove'].disabled
            assert str(buttons['guidance-default'].label) == "Vendor's default" and buttons['guidance-default'].disabled
            await pilot.click('#guidance-remove')
            await pilot.pause()
            assert guidance.vendor_choices('cli://claude') == {'user_email': None}
            assert not buttons['guidance-default'].disabled and buttons['guidance-remove'].disabled
            await pilot.click('#guidance-default')
            await pilot.pause()
            assert guidance.vendor_choices('cli://claude') == {}

            choice.value = IMPLEMENTER
            await pilot.pause()
            assert not text.read_only and str(buttons['guidance-default'].label) == "tare's default"
            text.load_text('Implement carefully.')
            await pilot.click('#guidance-save')
            await pilot.pause()
            assert guidance.capsule_edits() == {'role/implementer': {'system': 'Implement carefully.'}}
            assert 'New sessions' in str(panel.query_one('#guidance-status').render())
            await pilot.click('#guidance-default')
            await pilot.pause()
            assert not guidance.path().exists() and text.text == native_agent.prompt_capsule(
                deployment(), 'implementer')['system']
            assert not app.backend.launches
            await pilot.press('escape')
            assert app.screen is not panel
    asyncio.run(scenario())
