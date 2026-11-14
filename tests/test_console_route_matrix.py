"""/routes, "Models per role": the matrix tab edits the routing profile exactly as tare setup --auto would."""
import asyncio
import json

from textual.widgets import Button, OptionList, Select, Static, TabbedContent

from relay import environment, turn_engine
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
        assert '[ ]' in rendered(panel, 'claude/opus') and '[x]' in rendered(panel, 'claude/fable')  # not markup
        assert stored() is None  # nothing is written before Save
        await pilot.click('#matrix-save')
        await pilot.pause()
        assert stored() == environment.set_auto({}, 'auditor:claude/opus@claude-code=off')['routing_profile']
        assert await cell('claude/opus', 'claude-code', 'enter') == '[x]'
        await pilot.click('#matrix-save')
        await pilot.pause()
        assert 'routing_profile' not in json.loads(environment.store().read_text(encoding='utf-8'))
        # A role change rebuilds the table; its model column holds the longest name before the table measures cells.
        matrix, table = panel.query_one(RouteMatrix), panel.query_one('#matrix-table')
        matrix.role = 'implementer'
        matrix.build()
        assert table.ordered_columns[0].get_render_width(table) - 2 * table.cell_padding >= max(
            len(str(table.get_row(key)[0])) for key in table.rows)
    journey(tmp_path, steps, size=(80, 24))


def rendered(panel, row):
    """The table's line for a row as drawn on screen, plain text."""
    table = panel.query_one('#matrix-table')
    return table.render_line(1 + table.get_row_index(row) - round(table.scroll_y)).text


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


def keys(monkeypatch):
    for name in ('ANTHROPIC_API_KEY', 'OPENAI_API_KEY'):
        monkeypatch.setenv(name, 'fixture')  # present, never used: nothing is called


def cells(panel, family=None, where=None):
    """{(row, source): [route]} of the selected role's cells, by vendor or by source."""
    matrix = panel.query_one(RouteMatrix)
    return {(row, src): [r for _, r, _, _ in routes] for (row, src), routes in matrix.cells[matrix.role].items()
            if (family is None or row.startswith(family + '/')) and (where is None or src == where)}


def test_a_vendor_on_after_a_source_off_turns_every_line_of_it_on_and_leaves_the_others_off(pool, tmp_path,
                                                                                               monkeypatch):
    install(pool, 'claude-opus-5-5-cli', 'cli://claude', 'claude-opus-5-5')
    keys(monkeypatch)

    async def steps(app, pilot, panel, cell, status):
        bulk, table = panel.query_one('#matrix-bulk', Select), panel.query_one('#matrix-table')
        bulk.value = 'scope:*@api=off'
        await pilot.pause()
        bulk.value = 'scope:claude/*@*=on'
        await pilot.pause()
        claude, openai = cells(panel, 'claude'), cells(panel, 'gpt', 'api')
        assert claude and openai
        assert all(str(table.get_cell(row, src)).startswith('[x]') for row, src in claude)
        assert all(str(table.get_cell(row, src)).startswith('[ ]') for row, src in openai)
        await pilot.click('#matrix-save')
        await pilot.pause()
        assert stored()['off'] == {'auditor': ['*@api']}  # one "on" per Claude line on the API, none elsewhere
        assert sorted(stored()['on']['auditor']) == sorted(f'{row}@api' for row, src in claude if src == 'api')
        # Automatic routing reads the saved file the same way.
        assert all(environment.profile_state('auditor', r) in (None, ('on', f'{row}@api'))
                   for (row, src), routes in claude.items() for r in routes)
        assert all(environment.profile_state('auditor', r) == ('off', '*@api') for routes in openai.values() for r in routes)
        # A vendor "off" is one broad entry and drops the "on" entries inside it that would outrank it.
        bulk.value = 'scope:claude/*@*=off'
        await pilot.pause()
        assert all(str(table.get_cell(row, src)).startswith('[ ]') for row, src in claude)
        await pilot.click('#matrix-save')
        await pilot.pause()
        assert stored() == {'off': {'auditor': ['*@api', 'claude/*@*']}}
    journey(tmp_path, steps)


def test_with_your_auditor_routes_the_matrix_shows_only_them_and_warns_when_none_is_independent(pool, tmp_path,
                                                                                                monkeypatch):
    install(pool, 'claude-opus-5-5-cli', 'cli://claude', 'claude-opus-5-5')
    keys(monkeypatch)  # the OpenAI API auditors of the role table are usable, but outside the owner's list
    turn_engine.save_policy({'auditor': [['claude-opus-5-5-cli']]}, auditor_selection='configured')
    assert [r['route'] for r in environment.auto_rows('auditor')] == ['claude-opus-5-5-cli']  # also --auto-show

    async def steps(app, pilot, panel, cell, status):
        assert set(cells(panel)) == {('claude/opus', 'claude-code')}
        assert 'every independent auditor is off or unusable' in status()  # a Claude implementer has none
    journey(tmp_path, steps)


def test_the_independent_auditor_warning_stays_while_its_edit_is_unsaved_whatever_role_is_shown(pool, tmp_path,
                                                                                                 monkeypatch):
    install(pool, 'claude-opus-5-5-cli', 'cli://claude', 'claude-opus-5-5')
    keys(monkeypatch)

    async def steps(app, pilot, panel, cell, status):
        assert 'independent auditor' not in status()
        panel.query_one('#matrix-bulk', Select).value = 'scope:*@api=off'  # Opus on Claude Code is left to audit Opus
        await pilot.pause()
        assert 'every independent auditor is off or unusable' in status()
        panel.query_one('#matrix-role', Select).value = 'explorer'
        await pilot.pause()
        assert 'every independent auditor is off or unusable' in status()
    journey(tmp_path, steps)


def test_save_keeps_what_changed_outside_for_another_role(pool, tmp_path):
    install(pool, 'claude-opus-5-5-cli', 'cli://claude', 'claude-opus-5-5')

    async def steps(app, pilot, panel, cell, status):
        await cell('claude/opus', 'claude-code')
        assert environment.main(['--auto', 'planner:gpt/*@codex=off']) == 0  # another shell, meanwhile
        await pilot.click('#matrix-save')
        await pilot.pause()
        assert stored() == {'off': {'planner': ['gpt/*@codex'], 'auditor': ['claude/opus@claude-code']}}
    journey(tmp_path, steps)


def test_save_refuses_when_the_edited_role_changed_outside_and_reload_reads_it(pool, tmp_path):
    install(pool, 'claude-opus-5-5-cli', 'cli://claude', 'claude-opus-5-5')

    async def steps(app, pilot, panel, cell, status):
        await cell('claude/opus', 'claude-code')
        assert environment.main(['--auto', 'auditor:gpt/sol@api=off']) == 0
        before = environment.store().read_bytes()
        await pilot.click('#matrix-save')
        await pilot.pause()
        assert environment.store().read_bytes() == before
        assert 'Auditor changed outside this screen' in status()
        table = panel.query_one('#matrix-table')
        assert str(table.get_cell('claude/opus', 'claude-code')) == '[ ]'  # the edit stays on screen
        await pilot.click('#matrix-reload')
        await pilot.pause()
        assert str(table.get_cell('claude/opus', 'claude-code')) == '[x]'
        assert str(table.get_cell('gpt/sol', 'api')).startswith('[ ]')
        assert not panel.query_one(RouteMatrix).dirty()
    journey(tmp_path, steps)


def test_entries_that_look_like_markup_are_listed_as_text_and_removed(pool, tmp_path):
    install(pool, 'claude-opus-5-5-cli', 'cli://claude', 'claude-opus-5-5')
    environment.save({'routing_profile': {'off': {'auditor': ['[/]', '[bold]x']}}})

    async def steps(app, pilot, panel, cell, status):
        extra = panel.query_one('#matrix-extra', OptionList)
        assert [str(extra.get_option_at_index(i).prompt) for i in range(extra.option_count)] == [
            "off  '[/]' (cannot be read; ignored)", "off  '[bold]x' (cannot be read; ignored)"]
        extra.focus()
        for _ in range(2):
            extra.highlighted = 0
            await pilot.press('enter')
            await pilot.pause()
        await pilot.click('#matrix-save')
        await pilot.pause()
        assert stored() is None
    journey(tmp_path, steps)
