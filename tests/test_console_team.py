import asyncio

import pytest
from textual.widgets import Button, Static, TextArea

from relay.console_output import ToolActivity
from relay.console_workbench import WorkbenchPanel
from relay.execution_team import observe_session
from relay.native_console import WorkConsole
from test_execution_team import state, write_state
from test_native_console import FakeBackend


@pytest.mark.parametrize('size', [(80, 24), (130, 44)])
def test_team_is_visible_and_read_only_with_a_legacy_low_planner(tmp_path, size):
    project = tmp_path/'project'; project.mkdir(); session = tmp_path/'saved'
    original = state('planner'); original['deployment']['native_profile']['parameters_json'] = '{"effort":"low"}'
    write_state(session, project, original)
    before = (session/'native-agent.json').read_bytes()
    async def scenario():
        backend = FakeBackend(); app = WorkConsole(project, backend=backend, initial_mode='assistant')
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            app.assistant_session = str(session)
            prompt = app.query_one('#prompt', TextArea); prompt.load_text('Keep my unfinished request')
            await pilot.click('#toolbar-team'); await pilot.pause()
            assert isinstance(app.screen, WorkbenchPanel)
            assert 'Planner' in app.screen.rows[0]['body']
            assert any('low' in row['label'] and 'Below minimum high' in row['label'] for row in app.screen.rows)
            await pilot.press('escape'); await pilot.pause()
            assert prompt.text == 'Keep my unfinished request' and not backend.launches
            assert (session/'native-agent.json').read_bytes() == before
            app.busy = True  # a read-only view also works during a long execution
            assert app.dispatch_conversation_command('/team', '')
            await pilot.pause()
            assert isinstance(app.screen, WorkbenchPanel) and not backend.launches
    asyncio.run(scenario())


@pytest.mark.parametrize('size', [(80, 24), (130, 44)])
def test_live_row_names_the_real_child_and_restores_its_identity_without_full_output(tmp_path, size):
    async def scenario():
        app = WorkConsole(tmp_path, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            events = []
            with observe_session(state('planner'), {'id': 'one'}, events.append) as result:
                result.update(status='PROPOSED', model_calls=2)
            for event in events:
                app.assistant_activity(event)
            await pilot.pause()
            row = app.query_one(ToolActivity)
            assert 'Planner' in row.title and 'GPT-6.1 Sol' in row.title and 'high' in row.title
            assert 'Result ready' in row.title and 'Done' not in row.title
            assert not row.query_one('.tool-full-output', Button).display
            assert 'Requested reasoning: high' in str(row.query_one('.tool-receipt', Static).render())
            app.checkpoint('draft_saved'); identity = app.demand_id
            app.new_demand(); await app.list_demands().wait()
            from textual.widgets import Select
            app.query_one('#saved-demand', Select).value = identity
            app.open_demand(); await app.workers.wait_for_complete(); await pilot.pause()
            restored = app.query_one(ToolActivity)
            assert restored.value['operation'] == 'agent_run'
            assert restored.value['actor'] == events[-1]['activity']['actor']
            assert 'Planner' in restored.title and not app.backend.launches
    asyncio.run(scenario())
