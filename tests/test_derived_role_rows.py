"""A newer version of a known line that a vendor's own CLI lists gets its role row at load time, pin only until it
answers once here (native_routes.derived). Offline: fixture profiles and a fixture Codex catalogue record."""
import json
import re

from graph_backlog.jsonutil import sha256_canonical

from relay import (cli_discovery, environment, model_choices, model_signals, native_catalog, native_routes,
                   round_table_roster, vendor_models)
from test_console_route_matrix import journey
from test_cross_vendor_auditors import install, pool  # noqa: F401

SOL = 'codex-cli-gpt-6-2-sol'
FABLE = 'claude-fable-5-3-cli'


def discovered(pool):
    """The profile discovery writes for gpt-6.2-sol from a Codex catalogue that lists it (vendor_models.expand)."""
    templates = json.loads(cli_discovery.TEMPLATES.read_text(encoding='utf-8'))
    record = {'slug': 'gpt-6.2-sol', 'visibility': 'list', 'default_reasoning_level': 'high', 'context_window': 400000,
              'supported_reasoning_levels': [{'effort': e} for e in ('low', 'medium', 'high', 'xhigh')]}
    (value,) = vendor_models.expand({'codex-cli': cli_discovery.deployment(templates['codex-cli'], __file__)},
                                    [record]).values()
    pool.mkdir(exist_ok=True)
    (pool/(SOL+'.json')).write_text(json.dumps(value), encoding='utf-8')


def rows():
    return {r['id']: r for r in native_routes.configured()['deployments']}


def ranked(role):
    chosen, selection = native_routes.ranked(role)
    return [d['id'] for d in chosen], {c['deployment']: c['reason'] for c in selection['candidates']}


def test_a_new_version_on_its_vendors_cli_copies_the_newest_sibling_row_pin_only(pool):
    discovered(pool)
    install(pool, FABLE, 'cli://claude', 'claude-fable-5-3')
    table = rows()
    for name, sibling, model in ((SOL, 'codex-cli-gpt-6-1-sol', 'gpt-6.2-sol'),
                                 (FABLE, 'claude-fable-5-2-cli', 'claude-fable-5-3')):
        row, theirs = table[name], table[sibling]
        assert row['qualified'] is False and row['native_catalog'] == name and row['model'] == model
        assert sibling in row['note']
        # The newest version on the same source: not 5.6 Sol's implementer roles, not Fable's metered API rows.
        assert {k: v for k, v in row.items() if k not in ('id', 'native_catalog', 'model', 'qualified', 'note')} == \
            {k: v for k, v in theirs.items() if k not in ('id', 'native_catalog', 'model', 'qualified', 'note')}
    assert table[SOL]['roles'] == ['planner', 'plan-auditor', 'auditor'] and table[SOL]['cost_tier'] == 'flat'
    assert table[FABLE]['roles'] == ['planner', 'plan-auditor', 'auditor', 'explorer']
    assert table[FABLE]['cost_tier'] == 'flat' and table[FABLE]['endpoint_domain'] == 'anthropic-cli'


def test_routes_that_do_not_qualify_leave_the_shipped_table_exactly_as_it_was(pool):
    install(pool, 'codex-cli-gpt-6-1-terra', 'cli://codex', 'gpt-6.1-terra')  # a written row: it wins
    install(pool, 'codex-cli-gpt-7-nova', 'cli://codex', 'gpt-7-nova')  # a line with no row in model_lines.json
    reseller = native_catalog.bundled('opencode-sub-gpt-6-luna')  # a shipped row of the line on this source exists
    for model in ('gpt-6.1-luna', 'gpt-6.2-sol'):
        value = json.loads(json.dumps(reseller))
        value['id'] = name = 'opencode-sub-' + model.replace('.', '-')
        value['model'] = value['native_profile']['model'] = model
        (pool/(name+'.json')).write_text(json.dumps(value), encoding='utf-8')
    roster = native_routes.read_json(native_catalog.roster_path(), 4*1024*1024)
    shipped = round_table_roster.load_role_deployments(round_table_roster.ROLE_DEPLOYMENTS, roster=roster)
    assert json.dumps(native_routes.configured()) == json.dumps(shipped)
    assert native_routes.ranked('explorer')[1]['policy_sha256'] == sha256_canonical(shipped)


def test_pin_only_until_proven_then_ranked_as_its_lines_newest_version(pool, monkeypatch):
    discovered(pool)
    older = native_catalog.select('openai-api')  # GPT-6.1 Sol, a qualified row
    assert model_signals.klass(older)['class'] == 'middling'  # an unproven 6.2 lowers nobody
    names, reasons = ranked('planner')
    assert SOL not in names and reasons[SOL] == 'NOT_QUALIFIED_PIN_ONLY'
    (_, known, now), = [r for r in environment.auto_routes('planner') if r[0] == SOL]
    assert now == 'NOT_QUALIFIED_PIN_ONLY' and environment.route_key(known)[0] == 'gpt'  # the matrix's provable cell

    choice = {r['deployment']: r for r in model_choices.projection(include_unconfigured=True)}[SOL]
    pin = next(a.split(' ', 1)[1] for a in choice['role_arguments']['planner'] if a.endswith('@high'))
    assert model_choices.select_exact(pin, 'planner')['native_model_choice']['connection'] == SOL
    monkeypatch.setenv('TARE_SESSION_SCOPE', json.dumps({'pins': [['planner', pin]]}))
    chosen, selection = native_routes.ranked('planner')
    assert [d['id'] for d in chosen] == [SOL] and selection['reason'] == 'EXACT_TEMPORARY_PIN'
    monkeypatch.delenv('TARE_SESSION_SCOPE')

    # tare setup --prove reaches it; the call and the role check are stand-ins (no real call).
    monkeypatch.setattr(environment, 'cost_line', lambda deployment: 'fixture cost')
    monkeypatch.setattr(cli_discovery, 'acknowledge', lambda deployment: (True, ''))
    monkeypatch.setattr(environment, 'role_check', lambda deployment: {'ok': True, 'reason': 'fixture'})
    assert environment.prove(SOL, lambda line: True)['ok'] is True
    names, reasons = ranked('planner')
    assert SOL in names and reasons[SOL] != 'NOT_QUALIFIED_PIN_ONLY'
    assert dict((n, now) for n, _, now in environment.auto_routes('planner'))[SOL] == 'usable'
    assert 'older_than' not in model_signals.klass(native_catalog.select(SOL))
    assert model_signals.klass(older) == {'class': 'cheap', 'from': 'line', 'older_than': '6.2'}


def test_the_matrix_offers_to_prove_a_derived_route(pool, tmp_path):
    install(pool, FABLE, 'cli://claude', 'claude-fable-5-3')

    async def steps(app, pilot, panel, cell, status):
        from relay.console_route_matrix import RouteMatrix
        await cell('claude/fable', 'claude-code')
        assert re.search(FABLE + r' [^;]*, NOT_QUALIFIED_PIN_ONLY \(press p to prove it once\)', status()), status()
        assert FABLE in [name for name, _ in panel.query_one(RouteMatrix).provable()]
    journey(tmp_path, steps)
