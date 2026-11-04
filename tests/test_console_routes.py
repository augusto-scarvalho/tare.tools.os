import asyncio
import json

import pytest
from textual.widgets import Button, Select, TextArea

from relay import turn_engine
from relay.console_routes import RoutesPanel, as_text, from_text
from relay.native_console import WorkConsole
from test_native_console import FakeBackend


def test_the_owners_routes_replace_tares_role_by_role_and_unknown_names_are_refused():
    tiers = [['qwen38-gsq', 'swift27b'], ['bedrock-sonnet'], ['codex-native']]  # codex-native: tare's, not installed
    assert from_text(as_text(tiers)) == tiers
    value = turn_engine.save_policy({'implementer': tiers, 'auditor': [['bedrock-opus'], ['bedrock-sonnet']],
                                     'planner': []})
    assert value['implementer'] == tiers and value['auditor'] == ['bedrock-opus', 'bedrock-sonnet']
    assert value['planner'] == turn_engine.DEFAULT_POLICY['planner']  # empty keeps tare's default
    assert set(json.loads(turn_engine.policy_path().read_text(encoding='utf-8'))) == {'implementer', 'auditor', 'auditor_selection'}
    with pytest.raises(ValueError, match='nonsense-route'):
        turn_engine.save_policy({'implementer': [['nonsense-route']]})
    assert turn_engine.policy()['implementer'] == tiers  # a refused save changes nothing
    assert turn_engine.reset_policy() == turn_engine.DEFAULT_POLICY and not turn_engine.policy_path().exists()


@pytest.mark.parametrize('size', [(80, 24), (130, 44)])
def test_routes_in_the_tui_save_and_go_back_to_tares_defaults_without_a_model_call(tmp_path, size):
    async def scenario():
        app = WorkConsole(tmp_path, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            assert app.dispatch_conversation_command('/routes', '')
            await pilot.pause()
            panel = app.screen
            assert isinstance(panel, RoutesPanel)
            assert panel.query_one('#routes-auditor', TextArea).disabled
            panel.query_one('#routes-auditor-selection', Select).value='configured'
            await pilot.pause()
            assert not panel.query_one('#routes-auditor', TextArea).disabled
            assert panel.region.contains_region(panel.query_one('#routes-save', Button).region)
            panel.query_one('#routes-implementer', TextArea).load_text('bedrock-sonnet\nqwen38-gsq, swift27b\n')
            await pilot.click('#routes-save')
            await pilot.pause()
            assert turn_engine.policy()['implementer'] == [['bedrock-sonnet'], ['qwen38-gsq', 'swift27b']]
            assert turn_engine.policy()['auditor_selection']=='configured'
            await pilot.click('#routes-default')
            await pilot.pause()
            assert not turn_engine.policy_path().exists()
            assert panel.query_one('#routes-auditor-selection', Select).value=='cross_vendor_frontier'
            assert from_text(panel.query_one('#routes-implementer', TextArea).text) == turn_engine.DEFAULT_POLICY['implementer']
            assert not app.backend.launches
            await pilot.press('escape')
            assert app.screen is not panel
    asyncio.run(scenario())


def test_auditor_editor_follows_selection_without_waiting_for_changed_messages(tmp_path):
    async def scenario():
        app = WorkConsole(tmp_path, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            assert app.dispatch_conversation_command('/routes', '')
            await pilot.pause()
            panel = app.screen
            choice = panel.query_one('#routes-auditor-selection', Select)
            editor = panel.query_one('#routes-auditor', TextArea)
            # Restoration can suppress Changed, and an older notification can
            # still be queued. Neither may leave the editor behind its choice.
            with choice.prevent(Select.Changed):
                choice.value = 'configured'
            assert not editor.disabled
            panel.post_message(Select.Changed(choice, 'cross_vendor_frontier'))
            await pilot.pause()
            assert choice.value == 'configured' and not editor.disabled
            with choice.prevent(Select.Changed):
                choice.value = 'cross_vendor_frontier'
            assert editor.disabled
            panel.post_message(Select.Changed(choice, 'configured'))
            await pilot.pause()
            assert choice.value == 'cross_vendor_frontier' and editor.disabled
            assert not app.backend.launches
    asyncio.run(scenario())
