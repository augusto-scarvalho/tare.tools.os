"""/routes, "Models per role": the matrix tab edits the routing profile exactly as tare setup --auto would."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from textual.widgets import Button, OptionList, Select, Static, TabbedContent, TextArea

from relay import environment, execution_scope, native_catalog, native_routes, turn_engine, web_research, work_audit
from relay.console_route_matrix import ProveDialog, RouteMatrix
from relay.console_routes import RoutesPanel
from relay.native_console import WorkConsole
from test_cross_vendor_auditors import install, pool  # noqa: F401
from test_native_console import FakeBackend


def stored():
    path = environment.store()
    return json.loads(path.read_text(encoding='utf-8')).get('routing_profile') if path.exists() else None


def journey(tmp_path, steps, size=(130, 44), role='auditor'):
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
            if role:  # else the role the tab opens on
                panel.query_one('#matrix-role', Select).value = role
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
        await saved(pilot, panel)
        assert stored() == environment.set_auto({}, 'auditor:claude/opus@claude-code=off')['routing_profile']
        assert await cell('claude/opus', 'claude-code', 'enter') == '[x]'
        await saved(pilot, panel)
        assert 'routing_profile' not in json.loads(environment.store().read_text(encoding='utf-8'))
        # A role change rebuilds the table; its model column holds the longest name before the table measures cells.
        matrix, table = panel.query_one(RouteMatrix), panel.query_one('#matrix-table')
        matrix.role = 'implementer'
        matrix.build()
        assert table.ordered_columns[0].get_render_width(table) - 2 * table.cell_padding >= max(
            len(str(table.get_row(key)[0])) for key in table.rows)
    journey(tmp_path, steps, size=(80, 24))


async def saved(pilot, panel):
    """Press Save and wait for its answer: it reads and writes the file in a worker thread, under a lock. The status
    is cleared first, so the answer waited for is this Save's and not the "Saved." an earlier one left there.
    The button is pressed, not clicked at its place on screen: clearing a status of several lines moves the button,
    and a click sent before that layout landed on nothing, so no Save ran and the file was read as it was (the
    Windows shards failed that way on three pull requests on 2026-11-15)."""
    panel.query_one('#matrix-status', Static).update('')
    panel.query_one('#matrix-save', Button).press()
    for _ in range(600):
        await pilot.pause(0.05)
        answer = str(panel.query_one('#matrix-status', Static).render())
        if any(s in answer for s in ('Saved.', 'Not saved', 'Nothing to save')):
            return
    raise AssertionError('Save gave no answer in 30 s')


def rendered(panel, row):
    """The table's line for a row as drawn on screen, plain text."""
    table = panel.query_one('#matrix-table')
    return table.render_line(1 + table.get_row_index(row) - round(table.scroll_y)).text


def test_coordinator_comes_first_its_ticks_store_its_own_entries_and_none_ticked_says_so(pool, tmp_path):
    install(pool, 'claude-opus-5-5-cli', 'cli://claude', 'claude-opus-5-5')
    assert environment.main(['--auto', 'implementer:gpt/*@codex=off']) == 0

    async def steps(app, pilot, panel, cell, status):
        select = panel.query_one('#matrix-role', Select)
        assert select.value == 'coordinator'  # the tab opens on it: the model a user meets first
        assert str(select.query_one(OptionList).get_option_at_index(0).prompt) == (
            'Coordinator · the model you talk to, when tare auto starts the conversation')
        assert await cell('claude/opus', 'claude-code') == '[ ]'
        await saved(pilot, panel)
        assert stored() == {'off': {'coordinator': ['claude/opus@claude-code'], 'implementer': ['gpt/*@codex']}}
        assert 'No route is ticked on' not in status()
        panel.query_one(RouteMatrix).bulk('scope:*@*=off')
        await pilot.pause()
        assert ('No route is ticked on for Coordinator: tare auto cannot start a conversation; it stops before any '
                'model call with "tare_auto_no_route_available: No route can start."') in status()
        await saved(pilot, panel)
        assert stored() == {'off': {'coordinator': ['*@*'], 'implementer': ['gpt/*@codex']}}
        # Copy from Implementer: the implementer's entries for the conversation too, in one step.
        panel.query_one(RouteMatrix).bulk('copy:implementer')
        await pilot.pause()
        await saved(pilot, panel)
        assert stored() == {'off': {'coordinator': ['gpt/*@codex'], 'implementer': ['gpt/*@codex']}}
    journey(tmp_path, steps, role=None)


def test_a_vendor_off_then_one_line_on_stores_one_broad_off_and_one_on(pool, tmp_path):
    install(pool, 'claude-opus-5-5-cli', 'cli://claude', 'claude-opus-5-5')

    async def steps(app, pilot, panel, cell, status):
        panel.query_one('#matrix-bulk', Select).value = 'scope:claude/*@*=off'
        await pilot.pause()
        assert await cell('claude/opus', 'claude-code') == '[x]'
        await saved(pilot, panel)
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
        await saved(pilot, panel)
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
        await saved(pilot, panel)
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
        await saved(pilot, panel)
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
        await saved(pilot, panel)
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


UNAUDITED = 'No auditor ticked on may audit implementer '
SAME_VENDOR = 'is of the same vendor: its audits run, labelled not independent.'


async def only_opus_audits(pilot, panel, cell):
    panel.query_one(RouteMatrix).bulk('scope:*@*=off')
    await pilot.pause()
    assert await cell('claude/opus', 'claude-code') == '[x]'


def test_the_independent_auditor_warning_stays_while_its_edit_is_unsaved_whatever_role_is_shown(pool, tmp_path):
    install(pool, 'claude-opus-5-5-cli', 'cli://claude', 'claude-opus-5-5')

    async def steps(app, pilot, panel, cell, status):
        assert UNAUDITED not in status() and SAME_VENDOR not in status()
        await only_opus_audits(pilot, panel, cell)  # Opus is left to audit Opus
        assert UNAUDITED in status()
        panel.query_one('#matrix-role', Select).value = 'explorer'
        await pilot.pause()
        assert UNAUDITED in status()
    journey(tmp_path, steps)


def test_an_implementer_of_the_auditors_own_line_is_left_unaudited(pool, tmp_path):
    install(pool, 'claude-opus-5-5-cli', 'cli://claude', 'claude-opus-5-5')

    async def steps(app, pilot, panel, cell, status):
        await only_opus_audits(pilot, panel, cell)
        assert 'No auditor ticked on may audit implementer claude/opus-5.5@claude-code (same model or line): its ' \
               'changes are not audited.' in status()  # the first Opus implementer; Opus auditors are refused for it
    journey(tmp_path, steps)


def test_a_claude_implementer_with_only_opus_ticked_to_audit_gets_the_same_vendor_line(pool, tmp_path):
    install(pool, 'claude-opus-5-5-cli', 'cli://claude', 'claude-opus-5-5')

    async def steps(app, pilot, panel, cell, status):
        await only_opus_audits(pilot, panel, cell)
        assert 'Every auditor ticked on for claude/sonnet-5.5@claude-code is of the same vendor: its audits run, ' \
               'labelled not independent.' in status()  # Claude Sonnet: Opus audits it, labelled not independent
    journey(tmp_path, steps)


def test_an_auditor_of_another_vendor_ticked_on_leaves_no_line(pool, tmp_path):
    install(pool, 'claude-opus-5-5-cli', 'cli://claude', 'claude-opus-5-5')

    async def steps(app, pilot, panel, cell, status):
        await only_opus_audits(pilot, panel, cell)
        assert SAME_VENDOR in status()  # Opus alone audits a Claude Sonnet implementer as its own vendor
        assert await cell('gpt/sol', 'codex') == '[x]!'  # GPT-6.1 Sol on Codex, not set up here: still a tick
        assert UNAUDITED not in status() and SAME_VENDOR not in status()  # another vendor audits them all now
    journey(tmp_path, steps)


def test_save_keeps_what_changed_outside_for_another_role(pool, tmp_path):
    install(pool, 'claude-opus-5-5-cli', 'cli://claude', 'claude-opus-5-5')

    async def steps(app, pilot, panel, cell, status):
        await cell('claude/opus', 'claude-code')
        assert environment.main(['--auto', 'planner:gpt/*@codex=off']) == 0  # another shell, meanwhile
        await saved(pilot, panel)
        assert stored() == {'off': {'planner': ['gpt/*@codex'], 'auditor': ['claude/opus@claude-code']}}
    journey(tmp_path, steps)


def test_save_refuses_when_the_edited_role_changed_outside_and_reload_reads_it(pool, tmp_path):
    install(pool, 'claude-opus-5-5-cli', 'cli://claude', 'claude-opus-5-5')

    async def steps(app, pilot, panel, cell, status):
        await cell('claude/opus', 'claude-code')
        assert environment.main(['--auto', 'auditor:gpt/sol@api=off']) == 0
        before = environment.store().read_bytes()
        await saved(pilot, panel)
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
        await saved(pilot, panel)
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
        await app.workers.wait_for_complete()  # the matrix stays hidden: only the Save reads its rows again
        await pilot.pause()
        assert tabs.active == 'routes-order'
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


def test_an_inline_table_row_named_like_a_catalogue_route_is_shown_and_ticked_as_itself(pool, tmp_path, monkeypatch):
    table = json.loads((Path(native_routes.__file__).with_name('role_deployments.json')).read_text(encoding='utf-8'))
    opus = next(r for r in table['deployments'] if r.get('native_catalog') == 'claude-api-opus-5-5')
    deployment = native_catalog.select('claude-api-opus-5-5')
    table['deployments'] = [r for r in table['deployments'] if 'openai-api' not in (r['id'], r.get('native_catalog'))]
    table['deployments'].append({**{k: v for k, v in opus.items() if k != 'native_catalog'}, 'id': 'openai-api',
                                 'qualified': False, 'native_profile': deployment['native_profile'],
                                 'native_credential_env': deployment['native_credential_env']})  # Opus, not GPT
    (tmp_path/'roles.json').write_text(json.dumps(table), encoding='utf-8')
    monkeypatch.setenv('TARE_ROLE_DEPLOYMENTS', str(tmp_path/'roles.json'))

    async def steps(app, pilot, panel, cell, status):
        found = {key: [r['id'] for r in routes] for key, routes in cells(panel).items()}
        assert 'openai-api' in found[('claude/opus', 'api')] and ('gpt/sol', 'api') not in found
        await cell('claude/opus', 'api')
        await saved(pilot, panel)
        assert stored() == {'off': {'auditor': ['claude/opus@api']}}
    journey(tmp_path, steps)


def test_a_route_that_runs_another_model_under_its_id_is_judged_again_when_the_rows_are_read(pool, tmp_path,
                                                                                               monkeypatch):
    turn_engine.save_policy({'auditor': [['openrouter-muse-wonder']]}, auditor_selection='configured')
    runs, real = ['gpt-6.1-sol', 'OpenAI', 'openai/gpt'], work_audit.identity

    def identity(route, role):  # a route with no line is judged by its roster identity
        if route['id'] == 'openrouter-muse-wonder':
            return SimpleNamespace(model_id=runs[0], vendor=runs[1], bias_domain=runs[2])
        return real(route, role)
    monkeypatch.setattr(work_audit, 'identity', identity)

    async def steps(app, pilot, panel, cell, status):
        assert UNAUDITED in status()  # its one auditor runs a GPT, like the GPT implementers
        runs[:] = ['fixture-model', 'Fixture', 'fixture/one']  # the same id now runs another vendor's model
        panel.query_one(RouteMatrix).open()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert UNAUDITED not in status() and SAME_VENDOR not in status()
    journey(tmp_path, steps)


def test_a_researcher_list_route_needs_the_research_tools_own_admission(pool, monkeypatch):
    keys(monkeypatch)
    # GPT-6 Luna on the API: no implementer row either, so tare auto's tiers (the /research link summary) miss it.
    turn_engine.save_policy({'researcher': [['openai-api-gpt-6-luna', 'codex-cli-gpt-6-luna']]})
    assert turn_engine.available('openai-api-gpt-6-luna', probe=False)[0] is not None
    assert 'openai-api-gpt-6-luna' in web_research.snapshot()['unavailable']  # not a research CLI nor OpenRouter
    assert {n: now for n, _, now in environment.auto_routes('researcher')}['openai-api-gpt-6-luna'] == \
        'ROLE_NOT_CONFIGURED'


def test_an_empty_saved_heads_up_list_reads_as_its_writer_reads_it(pool, monkeypatch):
    install(pool, 'claude-sonnet-5-5-cli', 'cli://claude', 'claude-sonnet-5-5')
    install(pool, 'claude-haiku-5-5-cli', 'cli://claude', 'claude-haiku-5-5')
    keys(monkeypatch)
    turn_engine.policy_path().parent.mkdir(parents=True, exist_ok=True)
    turn_engine.policy_path().write_text(json.dumps({'heads_up': []}), encoding='utf-8')
    usable = {n for n, _, now in environment.auto_routes('heads_up') if now == 'usable'}
    eligible = {c['catalog'] for c in native_routes.ranked('heads_up')[1]['candidates'] if c['eligible']}
    assert usable == eligible and 'claude-sonnet-5-5-cli' in usable and 'claude-haiku-5-5-cli' not in usable


def flat(items):
    return [n for item in items for n in (item if isinstance(item, list) else [item])]


def test_the_matrix_rows_agree_with_the_paths_that_route(pool, monkeypatch):
    """auto_routes against the real paths on the fixture catalogue, with no profile. Ranked roles: the routes only the
    role table reaches, against native_routes.ranked (review roles: the union over a producer of each vendor, since
    the matrix checks no independence; the coordinator on the implementer rows). tare auto's list path (the owner's
    list, else local-default) for implementer and coordinator, and its tiers for the researcher's /research link
    summary: against turn_engine.choose(probe=False), name by name. Researcher: against web_research.snapshot(). Not
    compared, because their loops live inline in their callers: the planner, vision, or heads_up list when the owner
    saves one, the web search helper (turn_engine.ask_cli) and the owner's auditor list (turn_engine.pick_auditors,
    which needs a producer)."""
    install(pool, 'claude-opus-5-5-cli', 'cli://claude', 'claude-opus-5-5')
    install(pool, 'claude-fable-5-2-cli', 'cli://claude', 'claude-fable-5-2')
    install(pool, 'codex-cli-gpt-5-6-sol', 'cli://codex', 'gpt-5.6-sol')
    keys(monkeypatch)
    rules = turn_engine.policy()
    saved = rules.get('implementer') or [['local-default']]
    producers = [native_catalog.select(name) for name in ('claude-api-opus-5-5', 'openai-api', 'gemini-api')]
    for role in ('implementer', 'coordinator', 'explorer', 'planner', 'plan-auditor', 'auditor',
                 'researcher', 'vision', 'heads_up'):
        rows = {name: now for name, _, now in environment.auto_routes(role)}
        listed = (set(flat(turn_engine.implementer_tiers('researcher'))) | set(turn_engine.HELPER_ROUTES)
                  | set(flat(rules.get('researcher') or [])) if role == 'researcher'
                  else set(flat(saved if role in ('implementer', 'coordinator') else rules.get(role) or []))
                  if role in ('implementer', 'coordinator', 'planner', 'vision', 'heads_up') else set())
        eligible = set()
        for producer in producers if 'auditor' in role else [None]:
            eligible |= {c['catalog'] for c in native_routes.ranked(role, producer)[1]['candidates'] if c['eligible']}
        assert {n for n, now in rows.items() if now == 'usable' and n not in listed} == eligible - listed, role
    table = {r.get('native_catalog', r['id']) for r in native_routes.configured()['deployments']}
    tiers = flat(turn_engine.implementer_tiers('researcher'))
    for role, names in (('implementer', flat(saved)), ('coordinator', flat(saved)), ('researcher', tiers)):
        rows = {name: now for name, _, now in environment.auto_routes(role)}
        for name in set(names) - table - set(rules.get('researcher') or []):
            taken = turn_engine.choose([[name]], probe=False, role=role)[2] is not None
            assert (rows[name] == 'usable') == taken, (role, name)
    rows, snapshot = {n: now for n, _, now in environment.auto_routes('researcher')}, web_research.snapshot()
    ranked_deployments, ranked_selection = native_routes.ranked('researcher')
    ranked_names = {c['selected_deployment']: c['catalog'] for c in ranked_selection['candidates'] if c['eligible']}
    assert snapshot['route_order'] == [ranked_names[d['id']] for d in ranked_deployments]
    for name in set(snapshot['routes']) | (set(rows) & set(snapshot['unavailable'])):
        assert (rows[name] == 'usable') == (name in snapshot['routes']), name


def test_a_row_shows_its_models_class_and_a_cell_the_routes_own_record_as_implementer(pool, tmp_path):
    install(pool, 'claude-opus-5-5-cli', 'cli://claude', 'claude-opus-5-5')
    from relay.native_installation import storage_root
    job = storage_root()/'agent-sessions'/'s1'/'units'/'a'
    job.mkdir(parents=True)
    (job/'receipt.json').write_text(json.dumps({'rungs': [{'route': 'claude-opus-5-5-cli', 'verdict': 'VERIFIED'}],
        'result': {'route': 'claude-opus-5-5-cli', 'applied': True}}), encoding='utf-8')

    async def steps(app, pilot, panel, cell, status):
        assert 'Claude Opus · middling' in rendered(panel, 'claude/opus')
        await cell('claude/opus', 'claude-code')
        assert '(as implementer here: 1 runs; 1 passed the checks; 0 of 0 audits accepted; 1 applied)' in status()
    journey(tmp_path, steps)


FABLE = 'claude-fable-5-2-cli'


def prover(monkeypatch, ok=True, failed=None):
    """No real call: environment.prove is replaced by a stand-in that, like the real one, asks `confirm` first and
    keeps its record in the setup file (never a reply). `failed`: the record of a failed proof. Returns the names it
    was called for."""
    calls = []
    monkeypatch.setattr(environment, 'route_for', lambda name: ({'id': name}, None))
    monkeypatch.setattr(environment, 'cost_line', lambda deployment: 'one minimal call: fixture cost')

    def prove(name, confirm):
        assert confirm(f'{name}: one minimal call: fixture cost.')
        calls.append(name)
        record = {'ok': ok, 'cost': 'fixture', **({} if ok else failed or {'reason': 'credential_rejected'})}
        value = environment.read()
        value.setdefault('proofs', {})[name] = record
        environment.save(value)
        return record
    monkeypatch.setattr(environment, 'prove', prove)
    return calls


async def proved(pilot, panel, status, answer):
    """Wait for the status line to carry `answer`: the proof and the reload run in worker threads."""
    for _ in range(600):
        await pilot.pause(0.05)
        if answer in status():
            return
    raise AssertionError(f'no {answer!r} in 30 s: {status()}')


def test_a_pin_only_cell_is_proven_from_the_matrix_after_a_confirmation_naming_the_route_and_its_cost(pool, tmp_path,
                                                                                                    monkeypatch):
    install(pool, FABLE, 'cli://claude', 'claude-fable-5-2')
    calls = prover(monkeypatch)

    async def steps(app, pilot, panel, cell, status):
        assert await cell('claude/fable', 'claude-code', 'p') == '[x]!'  # on, but a rule holds it
        assert 'press p to prove it once' in status() and not panel.query_one('#matrix-prove', Button).disabled
        dialog = app.screen
        assert isinstance(dialog, ProveDialog)
        shown = ' '.join(dialog.lines)
        assert FABLE in shown and 'one minimal call: fixture cost' in shown
        assert calls == []  # nothing is called before Prove
        dialog.query_one('#prove-go', Button).press()
        await proved(pilot, panel, status, 'answered and did the task; used automatically')
        assert calls == [FABLE] and app.screen is panel
        assert str(panel.query_one('#matrix-table').get_cell('claude/fable', 'claude-code')) == '[x]'  # lost its "!"
        assert 'the reply' not in json.dumps(environment.read()['proofs'])
    journey(tmp_path, steps)


def test_the_prove_button_opens_the_same_confirmation_as_the_key(pool, tmp_path, monkeypatch):
    install(pool, FABLE, 'cli://claude', 'claude-fable-5-2')
    calls = prover(monkeypatch)

    async def steps(app, pilot, panel, cell, status):
        table = panel.query_one('#matrix-table')
        table.focus()  # select the cell without ticking or proving it
        table.move_cursor(row=table.get_row_index('claude/fable'), column=table.get_column_index('claude-code'))
        await pilot.pause()
        panel.query_one('#matrix-prove', Button).press()
        for _ in range(50):
            await pilot.pause(0.1)
            if isinstance(app.screen, ProveDialog):
                break
        assert isinstance(app.screen, ProveDialog) and FABLE in ' '.join(app.screen.lines) and calls == []
        await pilot.press('escape')
    journey(tmp_path, steps)


def test_cancel_and_esc_call_nothing(pool, tmp_path, monkeypatch):
    install(pool, FABLE, 'cli://claude', 'claude-fable-5-2')
    calls = prover(monkeypatch)

    async def steps(app, pilot, panel, cell, status):
        await cell('claude/fable', 'claude-code', 'p')
        assert isinstance(app.screen, ProveDialog)
        await pilot.press('escape')
        await pilot.pause()
        assert app.screen is panel
        await cell('claude/fable', 'claude-code', 'p')
        app.screen.query_one('#prove-cancel', Button).press()
        await pilot.pause()
        assert app.screen is panel and calls == [] and 'proofs' not in environment.read()
        assert str(panel.query_one('#matrix-table').get_cell('claude/fable', 'claude-code')) == '[x]!'
    journey(tmp_path, steps)


@pytest.mark.parametrize('failed,answer', [
    (None, 'failed: credential_rejected'),
    ({'reason': 'answered, but failed the role check: x', 'role_check': {
        'ok': False, 'reason': 'the session delivered but did not change value.txt to 42'}},
     'answered, but failed the role check: the session delivered but did not change value.txt to 42; it stays pin only')])
def test_a_failed_proof_says_why_and_the_cell_stays_as_it_was(pool, tmp_path, monkeypatch, failed, answer):
    install(pool, FABLE, 'cli://claude', 'claude-fable-5-2')
    calls = prover(monkeypatch, ok=False, failed=failed)

    async def steps(app, pilot, panel, cell, status):
        await cell('claude/fable', 'claude-code', 'p')
        app.screen.query_one('#prove-go', Button).press()
        await proved(pilot, panel, status, answer)
        assert calls == [FABLE]
        assert str(panel.query_one('#matrix-table').get_cell('claude/fable', 'claude-code')) == '[x]!'
        assert not panel.query_one(RouteMatrix).dirty()
    journey(tmp_path, steps)


def test_a_cell_with_nothing_to_prove_says_so_and_opens_no_dialog(pool, tmp_path, monkeypatch):
    install(pool, 'claude-opus-5-5-cli', 'cli://claude', 'claude-opus-5-5')
    calls = prover(monkeypatch)

    async def steps(app, pilot, panel, cell, status):
        assert await cell('claude/opus', 'claude-code', 'p') == '[x]'
        assert 'Nothing to prove in this cell' in status() and app.screen is panel and calls == []
        assert panel.query_one('#matrix-prove', Button).disabled
    journey(tmp_path, steps)


def test_an_unsaved_tick_survives_a_proof_and_save_writes_the_tick_and_keeps_the_proof(pool, tmp_path, monkeypatch):
    install(pool, 'claude-opus-5-5-cli', 'cli://claude', 'claude-opus-5-5')
    install(pool, FABLE, 'cli://claude', 'claude-fable-5-2')
    prover(monkeypatch)

    async def steps(app, pilot, panel, cell, status):
        assert await cell('claude/opus', 'claude-code') == '[ ]'  # unsaved
        await cell('claude/fable', 'claude-code', 'p')
        app.screen.query_one('#prove-go', Button).press()
        await proved(pilot, panel, status, 'answered and did the task; used automatically')
        table = panel.query_one('#matrix-table')
        assert str(table.get_cell('claude/opus', 'claude-code')) == '[ ]' and panel.query_one(RouteMatrix).dirty()
        assert environment.read()['proofs'][FABLE]['ok'] is True and stored() is None
        await saved(pilot, panel)
        assert 'Saved.' in status() and 'Not saved' not in status()
        assert stored() == environment.set_auto({}, 'auditor:claude/opus@claude-code=off')['routing_profile']
        assert environment.read()['proofs'][FABLE]['ok'] is True
    journey(tmp_path, steps)


def test_paint_updates_only_changed_cells_and_build_uses_precomputed_classes(pool, tmp_path, monkeypatch):
    install(pool, 'claude-opus-5-5-cli', 'cli://claude', 'claude-opus-5-5')
    install(pool, FABLE, 'cli://claude', 'claude-fable-5-2')

    async def steps(app, pilot, panel, cell, status):
        matrix = panel.query_one(RouteMatrix)
        table = panel.query_one('#matrix-table')
        updates = []
        real_update = table.update_cell

        def recording_update(row_key, col_key, value, *args, **kwargs):
            updates.append((row_key.value, col_key.value if hasattr(col_key, 'value') else col_key))
            return real_update(row_key, col_key, value, *args, **kwargs)

        monkeypatch.setattr(table, 'update_cell', recording_update)
        assert await cell('claude/opus', 'claude-code') == '[ ]'
        assert updates == [('claude/opus', 'claude-code')]

        updates.clear()
        matrix.paint()
        assert updates == []

        from relay import model_signals
        monkeypatch.setattr(model_signals, 'klass', lambda *a, **k: (_ for _ in ()).throw(
            AssertionError('build must use precomputed row classes instead of scanning model_signals.klass')))
        matrix.role = 'implementer'
        matrix.build()
    journey(tmp_path, steps)


def test_prove_all_proves_available_pending_routes_across_roles_and_fits_compact_screen(pool, tmp_path, monkeypatch):
    install(pool, 'claude-opus-5-5-cli', 'cli://claude', 'claude-opus-5-5')
    install(pool, FABLE, 'cli://claude', 'claude-fable-5-2')
    monkeypatch.setenv('OPENAI_API_KEY', 'fixture-key')
    available_targets = {FABLE, 'openai-api-gpt-6-1-terra'}
    calls = []
    monkeypatch.setattr(environment, 'route_for',
                        lambda name: ({'id': name}, None) if name in available_targets else (None, 'unavailable'))
    monkeypatch.setattr(environment, 'cost_line', lambda deployment: 'one minimal call: fixture cost')

    def prove(name, confirm):
        assert confirm(f'{name}: one minimal call: fixture cost.')
        calls.append(name)
        record = {'ok': True, 'cost': 'fixture'}
        value = environment.read()
        value.setdefault('proofs', {})[name] = record
        environment.save(value)
        return record
    monkeypatch.setattr(environment, 'prove', prove)

    async def steps(app, pilot, panel, cell, status):
        async def wait_prove_dialog():
            for _ in range(200):
                await pilot.pause(0.05)
                if (isinstance(app.screen, ProveDialog) and app.screen.is_mounted
                        and app.screen.query('#prove-go') and app.screen.query('#prove-cancel')):
                    await pilot.pause()
                    return app.screen
            raise AssertionError(f'ProveDialog did not mount in 10 s: {app.screen!r}')

        matrix = panel.query_one(RouteMatrix)
        assert sorted(matrix.provable_all()) == sorted(available_targets)
        assert panel.query_one('#matrix-save', Button).region and panel.region.contains_region(
            panel.query_one('#matrix-save', Button).region)
        assert panel.region.contains_region(panel.query_one('#matrix-prove-all', Button).region)

        # Even while an auditor cell with no proof and a vendor header row are active, P / Prove all finds both routes.
        table = panel.query_one('#matrix-table')
        table.focus()
        table.move_cursor(row=0, column=0)  # vendor header row
        await pilot.pause()
        await pilot.press('P')
        dialog = await wait_prove_dialog()
        shown = ' '.join(dialog.lines)
        assert FABLE in shown and 'openai-api-gpt-6-1-terra' in shown and calls == []
        await pilot.press('escape')
        await pilot.pause()
        assert app.screen is panel and calls == []

        # Confirm via button: unsaved tick stays, all available pin-only routes across roles are proven once.
        assert await cell('claude/opus', 'claude-code') == '[ ]'
        panel.query_one('#matrix-prove-all', Button).press()
        dialog = await wait_prove_dialog()
        dialog.query_one('#prove-go', Button).press()
        await pilot.pause()
        await proved(pilot, panel, status, 'Proved 2 of 2 route(s).')
        assert sorted(calls) == sorted(available_targets)
        assert str(table.get_cell('claude/opus', 'claude-code')) == '[ ]' and matrix.dirty()
        assert str(table.get_cell('claude/fable', 'claude-code')) == '[x]'
        assert matrix.provable_all() == [] and panel.query_one('#matrix-prove-all', Button).disabled
    journey(tmp_path, steps, size=(80, 24))


def test_matrix_prove_skips_unpreviewed_and_changed_routes_after_confirmation(pool, tmp_path, monkeypatch):
    install(pool, 'claude-opus-5-5-cli', 'cli://claude', 'claude-opus-5-5')
    install(pool, FABLE, 'cli://claude', 'claude-fable-5-2')
    monkeypatch.setenv('OPENAI_API_KEY', 'fixture-key')
    terra = 'openai-api-gpt-6-1-terra'
    live = {
        FABLE: {'id': FABLE, 'native_profile': {'model': 'claude-fable-5-2', 'protocol': 'vendor-cli', 'endpoint': 'cli://claude'}},
        terra: {'id': terra, 'native_credential_env': 'OPENAI_API_KEY',
                'native_profile': {'model': 'gpt-6-1-terra', 'protocol': 'openai-responses', 'endpoint': 'https://api.openai.com/v1/responses'}},
    }
    costs = {FABLE: 'cost for fable', terra: 'cost for terra'}
    calls = []
    monkeypatch.setattr(environment, 'route_for', lambda name: (live[name], None) if live.get(name) else (None, 'unavailable'))
    monkeypatch.setattr(environment, 'cost_line', lambda deployment: costs[deployment['id']])

    def prove(name, confirm):
        assert confirm(f'{name}: {costs[name]}.')
        calls.append(name)
        record = {'ok': True, 'cost': costs[name]}
        value = environment.read()
        value.setdefault('proofs', {})[name] = record
        environment.save(value)
        return record
    monkeypatch.setattr(environment, 'prove', prove)

    async def steps(app, pilot, panel, cell, status):
        async def wait_prove_dialog():
            for _ in range(200):
                await pilot.pause(0.05)
                if (isinstance(app.screen, ProveDialog) and app.screen.is_mounted
                        and app.screen.query('#prove-go') and app.screen.query('#prove-cancel')):
                    await pilot.pause()
                    return app.screen
            raise AssertionError(f'ProveDialog did not mount in 10 s: {app.screen!r}')

        # 1. Preview has terra unavailable; terra reappears before user clicks Prove -> must NOT be proved.
        saved_terra = live[terra]
        live[terra] = None
        panel.query_one('#matrix-prove-all', Button).press()
        dialog = await wait_prove_dialog()
        shown = ' '.join(dialog.lines)
        assert f'{FABLE}: cost for fable' in shown and f'{terra}: cannot be proven: unavailable' in shown
        live[terra] = saved_terra  # reappears after dialog opened without cost
        dialog.query_one('#prove-go', Button).press()
        await pilot.pause()
        await proved(pilot, panel, status, 'Proved 1 of 1 route(s); 1 unavailable before confirmation.')
        assert calls == [FABLE]

        # 2. Now only terra remains pending; its cost/model changes before Prove -> skipped with zero calls.
        calls.clear()
        panel.query_one('#matrix-prove-all', Button).press()
        dialog = await wait_prove_dialog()
        assert f'{terra}: cost for terra' in ' '.join(dialog.lines)
        costs[terra] = 'cost spiked after dialog opened'
        dialog.query_one('#prove-go', Button).press()
        await pilot.pause()
        await proved(pilot, panel, status, 'changed after confirmation')
        assert calls == []

        # 3. If no pending route is available when confirm_prove runs, ProveDialog must not open.
        live[terra] = None
        panel.query_one('#matrix-prove-all', Button).press()
        await proved(pilot, panel, status, 'Nothing to prove:')
        assert app.screen is panel and calls == []

    journey(tmp_path, steps, size=(100, 32))


def test_mirror_routes_of_known_lines_align_under_their_origin_line_and_source(pool, tmp_path, monkeypatch):
    install(pool, 'claude-opus-5-5-cli', 'cli://claude', 'claude-opus-5-5')

    async def steps(app, pilot, panel, cell, status):
        matrix = panel.query_one(RouteMatrix)
        found = cells(panel)
        assert ('gemini/argon', 'opencode-sub') in found
        assert [r['id'] for r in found['gemini/argon', 'opencode-sub']] == ['opencode-sub-gemini-4-argon']
        assert 'opencode-sub-gemini-4-argon' not in {row for row, _ in found}

        # Verify vendor grouping places Gemini Argon under Google, not Other.
        grouped = dict(matrix.groups())
        assert 'gemini/argon' in grouped['Google'] and 'gemini/argon' not in grouped.get('Other', ())

        # Ticking the mirror cell stores the exact line@source rule matching tare setup --auto.
        assert await cell('gemini/argon', 'opencode-sub') == '[ ]'
        await saved(pilot, panel)
        assert stored() == environment.set_auto({}, 'auditor:gemini/argon@opencode-sub=off')['routing_profile']
        assert environment.profile_state('auditor', native_catalog.select('opencode-sub-gemini-4-argon')) == (
            'off', 'gemini/argon@opencode-sub')

        # Also check implementer mirrors (Haiku and GPT Luna on OpenCode sub) and coordinator Bedrock mirrors.
        panel.query_one('#matrix-role', Select).value = 'implementer'
        await pilot.pause()
        impl_cells = cells(panel)
        assert [r['id'] for r in impl_cells['claude/haiku', 'opencode-sub']] == ['opencode-sub-claude-haiku-5-5']
        assert [r['id'] for r in impl_cells['gpt/luna', 'opencode-sub']] == ['opencode-sub-gpt-6-luna']

        panel.query_one('#matrix-role', Select).value = 'coordinator'
        await pilot.pause()
        coord_cells = cells(panel)
        assert [r['id'] for r in coord_cells['claude/opus', 'bedrock']] == ['bedrock-opus']
        assert 'bedrock-opus' not in {row for row, _ in coord_cells}
        coord_grouped = dict(matrix.groups())
        assert 'claude/opus' in coord_grouped['Anthropic'] and 'claude/opus' not in coord_grouped.get('Other', ())

    journey(tmp_path, steps)
