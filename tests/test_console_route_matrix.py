"""/routes, "Models per role": the matrix tab edits the routing profile exactly as tare setup --auto would."""
import asyncio
import json

from textual.widgets import Button, OptionList, Select, Static, TabbedContent

from relay import environment
from relay.console_route_matrix import RouteMatrix
from relay.console_routes import RoutesPanel
from relay.native_console import WorkConsole
from test_cross_vendor_auditors import install, pool  # noqa: F401
from test_native_console import FakeBackend


def stored():
    path = environment.store()
    return json.loads(path.read_text(encoding='utf-8')).get('routing_profile') if path.exists() else None


def journey(tmp_path, steps, size=(130, 44)):
    async def scenario():
        app = WorkConsole(tmp_path, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            assert app.dispatch_conversation_command('/routes', '')
            await pilot.pause()
            panel = app.screen
            assert isinstance(panel, RoutesPanel)
            panel.query_one('#routes-tabs', TabbedContent).active = 'routes-matrix'
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            panel.query_one('#matrix-role', Select).value = 'auditor'
            await pilot.pause()

            async def cell(row, column, key='space'):
                table = panel.query_one('#matrix-table')
                table.focus()
                table.move_cursor(row=table.get_row_index(row), column=table.get_column_index(column))
                await pilot.press(key)
                await pilot.pause()
                return str(table.get_cell(row, column))

            def status():
                return str(panel.query_one('#matrix-status', Static).render())
            await steps(app, pilot, panel, cell, status)
            assert not app.backend.launches
    asyncio.run(scenario())


def test_untick_and_tick_back_store_what_tare_setup_auto_stores_and_nothing_more(pool, tmp_path):
    install(pool, 'claude-opus-5-5-cli', 'cli://claude', 'claude-opus-5-5')

    async def steps(app, pilot, panel, cell, status):
        save = panel.query_one('#matrix-save', Button).region
        assert save.area and panel.region.contains_region(save)
        assert await cell('claude/opus', 'claude-code') == '[ ]'
        assert stored() is None  # nothing is written before Save
        await pilot.click('#matrix-save')
        await pilot.pause()
        assert stored() == environment.set_auto({}, 'auditor:claude/opus@claude-code=off')['routing_profile']
        assert await cell('claude/opus', 'claude-code', 'enter') == '[x]'
        await pilot.click('#matrix-save')
        await pilot.pause()
        assert 'routing_profile' not in json.loads(environment.store().read_text(encoding='utf-8'))
    journey(tmp_path, steps, size=(80, 24))


def test_a_vendor_off_then_one_line_on_stores_one_broad_off_and_one_on(pool, tmp_path):
    install(pool, 'claude-opus-5-5-cli', 'cli://claude', 'claude-opus-5-5')

    async def steps(app, pilot, panel, cell, status):
        panel.query_one('#matrix-bulk', Select).value = 'scope:claude/*@*=off'
        await pilot.pause()
        assert await cell('claude/opus', 'claude-code') == '[x]'
        await pilot.click('#matrix-save')
        await pilot.pause()
        assert stored() == {'off': {'auditor': ['claude/*@*']}, 'on': {'auditor': ['claude/opus@claude-code']}}
    journey(tmp_path, steps)


def test_a_cell_a_rule_decides_cannot_be_ticked(pool, tmp_path):
    install(pool, 'claude-opus-5-5-cli', 'cli://claude', 'claude-opus-5-5')
    install(pool, 'claude-fable-5-2-cli', 'cli://claude', 'claude-fable-5-2')  # registered, not qualified here
    install(pool, 'codex-cli-gpt-5-6-sol', 'cli://codex', 'gpt-5.6-sol')  # a model Codex replaced

    async def steps(app, pilot, panel, cell, status):
        assert (await cell('claude/fable', 'claude-code')).startswith('[x]!')
        assert 'NOT_QUALIFIED_PIN_ONLY' in status() and 'a tick cannot change it' in status()
        assert (await cell('claude/opus', 'codex')).strip() == '.'
        assert 'codex does not carry it' in status()
        panel.query_one('#matrix-role', Select).value = 'implementer'
        await pilot.pause()
        assert (await cell('gpt/sol', 'codex')).startswith('[x]!')
        assert 'OLDER_MODEL' in status()
        assert not panel.query_one(RouteMatrix).dirty()
        await pilot.click('#matrix-save')
        await pilot.pause()
        assert stored() is None
    journey(tmp_path, steps)


def test_unticking_the_last_eligible_auditor_warns_before_save_and_esc_asks(pool, tmp_path):
    install(pool, 'claude-opus-5-5-cli', 'cli://claude', 'claude-opus-5-5')

    async def steps(app, pilot, panel, cell, status):
        assert 'no eligible route' not in status()
        await cell('claude/opus', 'claude-code')
        assert 'Auditor: no eligible route left; the change is not audited.' in status()
        await pilot.press('escape')
        assert app.screen is panel and 'Press Esc again' in status()
        await pilot.press('escape')
        assert app.screen is not panel and stored() is None
    journey(tmp_path, steps)


def test_an_entry_the_cli_made_that_no_cell_draws_is_listed_and_can_be_removed(pool, tmp_path):
    install(pool, 'claude-opus-5-5-cli', 'cli://claude', 'claude-opus-5-5')
    assert environment.main(['--auto', 'auditor:*@*=off', '--auto', 'auditor:claude/opus-5.5@claude-code=on']) == 0
    value = environment.read()
    value['routing_profile']['off']['auditor'].append('nope@x')  # malformed: profile() names it and ignores it
    environment.save(value)

    async def steps(app, pilot, panel, cell, status):
        extra = panel.query_one('#matrix-extra', OptionList)
        labels = [str(extra.get_option_at_index(i).prompt) for i in range(extra.option_count)]
        assert labels == ['off  *@*', "off  'nope@x' (cannot be read; ignored)", 'on   claude/opus-5.5@claude-code']
        assert str(panel.query_one('#matrix-table').get_cell('claude/opus', 'claude-code')) == '[ ]~'  # versions differ
        extra.focus()
        extra.highlighted = 1
        await pilot.press('enter')
        await pilot.pause()
        assert extra.option_count == 2
        await pilot.click('#matrix-save')
        await pilot.pause()
        assert stored() == {'off': {'auditor': ['*@*']}, 'on': {'auditor': ['claude/opus-5.5@claude-code']}}
    journey(tmp_path, steps)
