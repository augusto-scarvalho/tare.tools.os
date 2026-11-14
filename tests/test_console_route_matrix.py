"""/routes, "Models per role": the matrix tab edits the routing profile exactly as tare setup --auto would."""
import asyncio
import json
from pathlib import Path

from textual.widgets import Button, OptionList, Select, Static, TabbedContent, TextArea

from relay import environment, execution_scope, native_catalog, native_routes, turn_engine, web_research
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


def test_a_cell_a_rule_marks_still_ticks_and_the_rule_keeps_its_own_reason(pool, tmp_path):
    install(pool, 'claude-opus-5-5-cli', 'cli://claude', 'claude-opus-5-5')
    install(pool, 'claude-fable-5-2-cli', 'cli://claude', 'claude-fable-5-2')  # registered, not qualified here
    install(pool, 'codex-cli-gpt-5-6-sol', 'cli://codex', 'gpt-5.6-sol')  # a model Codex replaced

    async def steps(app, pilot, panel, cell, status):
        assert await cell('claude/fable', 'claude-code') == '[ ]!'
        assert 'off by you; not usable now either: NOT_QUALIFIED_PIN_ONLY' in status()
        assert await cell('claude/fable', 'claude-code') == '[x]!'
        assert 'on, but not used automatically now: NOT_QUALIFIED_PIN_ONLY' in status()
        assert 'a tick does not override this rule' in status()
        assert await cell('claude/fable', 'claude-code') == '[ ]!'
        assert (await cell('claude/opus', 'codex')).strip() == '.'  # nothing to name: nothing to tick
        assert 'this source does not carry it here' in status()
        panel.query_one('#matrix-role', Select).value = 'implementer'
        await pilot.pause()
        assert await cell('gpt/sol', 'codex') == '[ ]!'
        assert 'OLDER_MODEL' in status()
        await pilot.click('#matrix-save')
        await pilot.pause()
        assert stored() == {'off': {'auditor': ['claude/fable@claude-code'], 'implementer': ['gpt/sol@codex']}}
        # Automatic routing still refuses those routes for their own rule, not for the tick.
        reasons = {c['deployment']: c['reason'] for c in native_routes.ranked(
            'auditor', native_catalog.select('openai-api'))[1]['candidates']}
        assert reasons['claude-fable-5-2-cli'] == 'NOT_QUALIFIED_PIN_ONLY'
        reasons = {c['deployment']: c['reason'] for c in native_routes.ranked('implementer')[1]['candidates']}
        assert reasons['codex-cli-gpt-5-6-sol'] == 'OLDER_MODEL'
    journey(tmp_path, steps)


def test_the_status_line_says_what_the_ticks_leave_before_save_and_esc_asks(pool, tmp_path):
    install(pool, 'claude-opus-5-5-cli', 'cli://claude', 'claude-opus-5-5')

    async def steps(app, pilot, panel, cell, status):
        assert 'none usable now' not in status() and 'No route is ticked on' not in status()
        panel.query_one(RouteMatrix).bulk('scope:*@*=off')
        await pilot.pause()
        assert 'No route is ticked on for Auditor: the change is not audited.' in status()  # certain, from ticks
        assert await cell('claude/fable', 'claude-code') == '[x]!'  # on again, but marked not usable here
        assert 'Auditor: 2 route(s) on, none usable now (NOT_QUALIFIED_PIN_ONLY, PROFILE_NOT_INSTALLED).' in status()  # advisory
        assert 'No route is ticked on' not in status()
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
        assert UNAUDITED in status()  # an Opus implementer has no other auditor ticked on
    journey(tmp_path, steps)


UNAUDITED = 'Every auditor ticked on shares a line with implementer '


def test_the_independent_auditor_warning_stays_while_its_edit_is_unsaved_whatever_role_is_shown(pool, tmp_path):
    install(pool, 'claude-opus-5-5-cli', 'cli://claude', 'claude-opus-5-5')

    async def steps(app, pilot, panel, cell, status):
        assert 'independent audit' not in status()
        panel.query_one(RouteMatrix).bulk('scope:*@*=off')
        await pilot.pause()
        assert await cell('claude/opus', 'claude-code') == '[x]'  # Opus is left to audit Opus
        assert UNAUDITED in status()
        panel.query_one('#matrix-role', Select).value = 'explorer'
        await pilot.pause()
        assert UNAUDITED in status()
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


def test_save_holds_the_file_from_its_read_to_its_write_against_tare_setup_auto(pool, tmp_path, monkeypatch):
    install(pool, 'claude-opus-5-5-cli', 'cli://claude', 'claude-opus-5-5')

    async def steps(app, pilot, panel, cell, status):
        await cell('claude/opus', 'claude-code')
        real, started, outside = environment.read, [], []

        def read():  # Save has read the file: another shell's tare setup --auto tries to write now
            value = real()
            if not started:
                started.append(1)
                outside.append(environment.main(['--auto', 'planner:gpt/*@codex=off']))
            return value
        monkeypatch.setattr(environment, 'read', read)
        await pilot.click('#matrix-save')
        for _ in range(100):
            await pilot.pause(0.1)
            if 'Saved.' in status():
                break
        monkeypatch.setattr(environment, 'read', real)
        assert outside == [1]  # refused while Save held the file, instead of being overwritten
        assert stored() == {'off': {'auditor': ['claude/opus@claude-code']}}
        assert environment.main(['--auto', 'planner:gpt/*@codex=off']) == 0
        assert stored() == {'off': {'auditor': ['claude/opus@claude-code'], 'planner': ['gpt/*@codex']}}
    journey(tmp_path, steps)


def test_the_matrix_reads_its_rows_again_after_the_order_tab_saves(pool, tmp_path):
    install(pool, 'claude-opus-5-5-cli', 'cli://claude', 'claude-opus-5-5')

    async def steps(app, pilot, panel, cell, status):
        assert await cell('claude/opus', 'claude-code') == '[ ]'  # an unsaved tick whose cell stays
        assert await cell('gpt/sol', 'codex') == '[ ]!'  # one whose cell goes
        tabs = panel.query_one('#routes-tabs', TabbedContent)
        tabs.active = 'routes-order'
        await pilot.pause()
        panel.query_one('#routes-auditor-selection', Select).value = 'configured'
        await pilot.pause()
        panel.query_one('#routes-auditor', TextArea).load_text('claude-opus-5-5-cli')
        await pilot.click('#routes-save')
        await pilot.pause()
        tabs.active = 'routes-matrix'
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert set(cells(panel)) == {('claude/opus', 'claude-code')}
        assert str(panel.query_one('#matrix-table').get_cell('claude/opus', 'claude-code')) == '[ ]'
        extra = panel.query_one('#matrix-extra', OptionList)
        assert 'off  gpt/sol@codex' in [str(extra.get_option_at_index(i).prompt) for i in range(extra.option_count)]
        assert 'No route is ticked on for Auditor' in status()  # the warnings follow the new rows
    journey(tmp_path, steps)


def test_availability_is_checked_as_the_role_it_is_for(pool, monkeypatch):
    install(pool, 'claude-opus-5-5-cli', 'cli://claude', 'claude-opus-5-5')
    monkeypatch.setattr(execution_scope, 'current', lambda: execution_scope.Scope(prefer='local'))
    with execution_scope.for_role('auditor'):  # --prefer_local keeps planner and auditor external
        assert turn_engine.available('claude-opus-5-5-cli', probe=False)[0] is not None
    assert {name: now for name, _, now in environment.auto_routes('auditor')}['claude-opus-5-5-cli'] == 'usable'


def test_a_route_a_routing_list_reaches_is_judged_as_that_list_judges_it(pool, monkeypatch):
    keys(monkeypatch)  # Fable 5.2 on the API: an implementer row of the role table, not qualified there
    turn_engine.save_policy({'implementer': [['claude-api-fable-5-2']]})
    assert turn_engine.choose(turn_engine.policy()['implementer'], probe=False)[1] == 'claude-api-fable-5-2'
    assert {n: now for n, _, now in environment.auto_routes('implementer')}['claude-api-fable-5-2'] == 'usable'
    assert {n: now for n, _, now in environment.auto_routes('auditor')}['claude-api-fable-5-2'] == 'NOT_QUALIFIED_PIN_ONLY'


def test_an_inline_role_table_route_is_its_own_row(pool, tmp_path, monkeypatch):
    keys(monkeypatch)
    table = json.loads((Path(native_routes.__file__).with_name('role_deployments.json')).read_text(encoding='utf-8'))
    row = next(r for r in table['deployments'] if r.get('native_catalog') == 'openai-api')
    deployment = native_catalog.select('openai-api')
    table['deployments'].append({**{k: v for k, v in row.items() if k != 'native_catalog'}, 'id': 'inline-auditor',
                                 'native_profile': deployment['native_profile'],
                                 'native_credential_env': deployment['native_credential_env']})
    (tmp_path/'roles.json').write_text(json.dumps(table), encoding='utf-8')
    monkeypatch.setenv('TARE_ROLE_DEPLOYMENTS', str(tmp_path/'roles.json'))
    assert 'inline-auditor' in [d['id'] for d in native_routes.ranked('auditor', native_catalog.select(
        'claude-api-opus-5-5'))[0]]
    route, now = {n: (r, now) for n, r, now in environment.auto_routes('auditor')}['inline-auditor']
    assert route['id'] == 'inline-auditor' and now == 'usable'


def test_a_researcher_list_route_needs_the_research_tools_own_admission(pool, monkeypatch):
    keys(monkeypatch)
    turn_engine.save_policy({'researcher': [['openai-api', 'codex-cli-gpt-6-luna']]})
    assert turn_engine.available('openai-api', probe=False)[0] is not None
    assert 'openai-api' in web_research.snapshot()['unavailable']  # not a research CLI nor an OpenRouter route
    assert {n: now for n, _, now in environment.auto_routes('researcher')}['openai-api'] == 'ROLE_NOT_CONFIGURED'


def test_an_empty_saved_heads_up_list_reads_as_its_writer_reads_it():
    turn_engine.policy_path().parent.mkdir(parents=True, exist_ok=True)
    turn_engine.policy_path().write_text(json.dumps({'heads_up': []}), encoding='utf-8')
    assert [n for n, _, _ in environment.auto_routes('heads_up')] == list(dict.fromkeys(
        turn_engine.DEFAULT_POLICY['heads_up']))


def flat(items):
    return [n for item in items for n in (item if isinstance(item, list) else [item])]


def test_the_matrix_rows_agree_with_the_paths_that_route(pool, monkeypatch):
    """auto_routes against the real paths on the fixture catalogue, with no profile. Ranked roles: the routes only the
    role table reaches, against native_routes.ranked (review roles: the union over a producer of each vendor, since
    the matrix checks no independence). tare auto's tiers, for implementer and for the researcher's /research link
    summary: against turn_engine.choose(probe=False), name by name. The researcher list: against
    web_research.snapshot(). Not compared, because their loops live inline in their callers: the planner list
    (planning_tools), vision (turn_engine.describe_files), heads_up (heads_up.write), the web search helper
    (turn_engine.ask_cli) and the owner's auditor list (turn_engine.pick_auditors, which needs a producer)."""
    install(pool, 'claude-opus-5-5-cli', 'cli://claude', 'claude-opus-5-5')
    install(pool, 'claude-fable-5-2-cli', 'cli://claude', 'claude-fable-5-2')
    install(pool, 'codex-cli-gpt-5-6-sol', 'cli://codex', 'gpt-5.6-sol')
    keys(monkeypatch)
    rules = turn_engine.policy()
    producers = [native_catalog.select(name) for name in ('claude-api-opus-5-5', 'openai-api', 'gemini-api')]
    for role in ('implementer', 'explorer', 'planner', 'plan-auditor', 'auditor'):
        rows = {name: now for name, _, now in environment.auto_routes(role)}
        listed = set(flat(rules.get(role) or [])) if role in ('implementer', 'planner') else set()
        eligible = set()
        for producer in producers if 'auditor' in role else [None]:
            eligible |= {c['catalog'] for c in native_routes.ranked(role, producer)[1]['candidates'] if c['eligible']}
        assert {n for n, now in rows.items() if now == 'usable' and n not in listed} == eligible - listed, role
    table = {r.get('native_catalog', r['id']) for r in native_routes.configured()['deployments']}
    for role in ('implementer', 'researcher'):
        rows = {name: now for name, _, now in environment.auto_routes(role)}
        for name in set(flat(rules['implementer'])) - table - set(rules['researcher']):
            taken = turn_engine.choose([[name]], probe=False, role=role)[2] is not None
            assert (rows[name] == 'usable') == taken, (role, name)
    rows, snapshot = {n: now for n, _, now in environment.auto_routes('researcher')}, web_research.snapshot()
    for name in set(rules['researcher']) - set(flat(rules['implementer'])) - set(turn_engine.HELPER_ROUTES):
        assert (rows[name] == 'ROLE_NOT_CONFIGURED') == ('admitted research' in snapshot['unavailable'].get(name, '')), name
        assert rows[name] != 'usable' or name in snapshot['routes'], name
