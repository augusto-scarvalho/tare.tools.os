"""The routing profile (`routing_profile` in environment.json): which routes automatic routing may use, per role."""
import copy
from dataclasses import replace
import json
from pathlib import Path

import pytest

from relay import cli_discovery, environment, execution_scope, native_agent, native_catalog, native_routes, research_cli
from relay import serve_agent, turn_engine
from relay import work_units
from relay.scheduling_diagnostics import UnschedulableError
from test_cross_vendor_auditors import install, pool  # noqa: F401
from test_implement_unit import repository
from test_native_agent import Client, deployment, setup  # noqa: F401
from test_native_plan import proposal  # noqa: F401
from test_planning_tools import execution, planner, request, stub_child, wrap  # noqa: F401
from test_research_tools import effect, host, report  # noqa: F401
from test_serve_agent import copilot, runtime  # noqa: F401


def saved(off=None, on=None, **extra):
    environment.save({'routing_profile': {**({'off': off} if off else {}), **({'on': on} if on else {}), **extra}})


def ranked(role, producer=None):
    chosen, selection = native_routes.ranked(role, producer and native_catalog.select(producer))
    return [d['id'] for d in chosen], {c['deployment']: c['reason'] for c in selection['candidates']}


def state(role, name, deployment=None):
    return environment.profile_state(role, deployment or native_catalog.select(name))


def test_entries_name_a_route_id_or_a_line_on_a_source_and_rank_by_specificity():
    assert environment.entry('claude/fable-5.2@api') == (7, ('claude', 'fable', (5, 2), 'api'))
    assert environment.entry('claude/fable@*') == (4, ('claude', 'fable', None, '*'))
    assert environment.entry('claude/*@claude-code') == (3, ('claude', None, None, 'claude-code'))
    assert environment.entry('*@openrouter') == (1, (None, None, None, 'openrouter'))
    assert environment.entry('openrouter-muse-wonder') == (8, 'openrouter-muse-wonder')
    for bad in ('claude/fable@nowhere', 'Claude/fable@api', 'claude/fable', 'a/b/c@api', '', None, 5, '@api'):
        assert environment.entry(bad) is None
    assert environment.route_label(native_catalog.select('claude-api-fable-5-2')) == 'claude/fable-5.2@api'
    assert environment.route_label(native_catalog.select('openrouter-muse-wonder')) == '@openrouter'


def test_the_most_specific_entry_wins_and_off_wins_a_tie(pool):
    install(pool, 'claude-fable-5-1-cli', 'cli://claude', 'claude-fable-5-1')
    saved(off={'auditor': ['claude/*@*', 'claude/fable-5.1@api', 'gpt/sol@api']},
          on={'auditor': ['claude/fable@api', 'gpt/sol@api']})
    assert state('auditor', 'claude-api-fable-5-1') == ('off', 'claude/fable-5.1@api')  # version inside a line "on"
    assert state('auditor', 'claude-api-fable-5-2') == ('on', 'claude/fable@api')  # line "on" inside a vendor "off"
    assert state('auditor', 'claude-fable-5-1-cli') == ('off', 'claude/*@*')  # another source keeps the vendor "off"
    assert state('auditor', 'openai-api') == ('off', 'gpt/sol@api')  # a tie
    assert state('planner', 'claude-api-fable-5-1') is None  # another role keeps tare's default
    # A new version of the line inherits the line's entry; a version entry exists only when made.
    newer = copy.deepcopy(native_catalog.select('claude-api-fable-5-2'))
    newer['id'] = 'claude-api-fable-5-3'
    newer['native_profile']['model'] = 'claude-fable-5-3'
    assert state('auditor', 'claude-api-fable-5-3', newer) == ('on', 'claude/fable@api')


def test_an_off_line_on_a_source_removes_exactly_those_routes_for_that_role(pool):
    install(pool, 'claude-opus-5-5-cli', 'cli://claude', 'claude-opus-5-5')
    before = {role: ranked(role, 'openai-api' if 'auditor' in role else None)
              for role in ('auditor', 'plan-auditor', 'planner', 'implementer')}
    saved(off={'auditor': ['claude/opus@api']})
    after = {role: ranked(role, 'openai-api' if 'auditor' in role else None) for role in before}
    assert after['auditor'][0] == [n for n in before['auditor'][0] if n != 'claude-api-opus-5-5']
    assert 'claude-opus-5-5-cli' in after['auditor'][0]  # the same line on Claude Code stays on
    assert {n: r for n, r in after['auditor'][1].items() if r != before['auditor'][1][n]} == {
        'claude-api-opus-5-5': 'SWITCHED_OFF_FOR_ROLE'}
    assert all(after[role] == before[role] for role in ('plan-auditor', 'planner', 'implementer'))


def test_a_route_with_no_line_is_named_by_its_route_id_or_its_source(pool):
    saved(off={'auditor': ['openrouter-muse-wonder'], 'planner': ['*@openrouter']})
    names, reasons = ranked('auditor', 'claude-api-opus-5-5')
    assert 'openrouter-muse-wonder' not in names and 'openrouter-kimi-k3-1-pro' in names
    assert reasons['openrouter-muse-wonder'] == 'SWITCHED_OFF_FOR_ROLE'
    names, reasons = ranked('planner')
    assert not [n for n in names if n.startswith('openrouter-')]
    assert reasons['openrouter-kimi-k3-1-pro'] == 'SWITCHED_OFF_FOR_ROLE'


def test_on_never_revives_a_route_a_rule_excludes(pool):
    install(pool, 'codex-cli-gpt-5-6-sol', 'cli://codex', 'gpt-5.6-sol')
    environment.save({'api_providers': {'items': {'GEMINI_API_KEY': {'use': False}}},
                      'routing_profile': {'off': {'auditor': ['*@*'], 'implementer': ['*@*']},
                                          'on': {'auditor': ['claude/fable-5.2@api', 'claude/opus@api',
                                                             'gemini/argon@api'],
                                                 'implementer': ['gpt/sol-5.6@codex']}}})
    names, reasons = ranked('auditor', 'claude-api-opus-5-5')
    assert names == []
    assert reasons['claude-api-fable-5-2'] == 'NOT_QUALIFIED_PIN_ONLY'
    assert reasons['claude-api-opus-5-5'] == 'AUDITOR_NOT_INDEPENDENT'
    assert reasons['gemini-api'] == 'NOT_USED_ON_THIS_MACHINE'
    assert ranked('implementer')[1]['codex-cli-gpt-5-6-sol'] == 'OLDER_MODEL'


def test_a_pin_and_an_explicit_choice_still_reach_a_route_switched_off(pool, monkeypatch):
    install(pool, 'claude-opus-5-5-cli', 'cli://claude', 'claude-opus-5-5')
    saved(off={'implementer': ['claude/opus@claude-code'], 'auditor': ['claude/opus@claude-code']})
    assert 'claude-opus-5-5-cli' not in ranked('implementer')[0]
    # Explicit: the picker's concrete route, and availability itself.
    chosen = native_catalog.select('claude-opus-5-5-cli')
    assert turn_engine.resolve_auto(chosen) is chosen
    assert turn_engine.available('claude-opus-5-5-cli')[0] is not None
    # A pin for this launch.
    monkeypatch.setenv('TARE_SESSION_SCOPE', json.dumps({'pins': [['implementer', 'anthropic/claude-opus-5-5-cli@high'],
                                                                  ['auditor', 'anthropic/claude-opus-5-5-cli@high']]}))
    names, selection = native_routes.ranked('implementer')
    assert [d['native_model_choice']['connection'] for d in names] == ['claude-opus-5-5-cli']
    assert selection['reason'] == 'EXACT_TEMPORARY_PIN'
    assert [n for n, _ in turn_engine.pick_auditors(native_catalog.select('openai-api'), None)] == [
        'claude-opus-5-5-cli']


@pytest.fixture
def tiers(monkeypatch):
    profiles = {name: native_catalog.select(name) for name in ('claude-api-sonnet-5-5', 'openai-api', 'gemini-api')}
    monkeypatch.setattr(turn_engine, 'policy', lambda: {**turn_engine.DEFAULT_POLICY, 'implementer': [
        ['claude-api-sonnet-5-5'], ['openai-api', 'gemini-api']], 'vision': list(profiles), 'heads_up': list(profiles)})
    monkeypatch.setattr(turn_engine, 'available', lambda name: (copy.deepcopy(profiles[name]), None))
    return profiles


def test_tare_auto_skips_an_off_route_with_its_reason_and_a_saved_order_does_not_override_it(tiers):
    saved(off={'implementer': ['claude/sonnet@api']})
    tier, name, _, skipped = turn_engine.choose(turn_engine.policy()['implementer'])
    assert (tier, name) == (1, 'openai-api')
    assert skipped[0]['route'] == 'claude-api-sonnet-5-5'
    assert skipped[0]['diagnostic']['code'] == 'SWITCHED_OFF_FOR_ROLE'
    assert 'claude/sonnet@api' in skipped[0]['reason']
    resolved = turn_engine.resolve_auto({'native_auto_routes': 'implementer'}, request='Fix the parser.')
    assert resolved['id'] == 'openai-api'
    assert resolved['native_auto_selection']['skipped'][0]['diagnostic']['code'] == 'SWITCHED_OFF_FOR_ROLE'
    # The same route stays on for another role: /plan runs the planner's list under its own role.
    assert turn_engine.choose([['claude-api-sonnet-5-5']], role='planner')[1] == 'claude-api-sonnet-5-5'


def test_a_role_left_with_no_route_behaves_as_when_none_is_available(tiers, monkeypatch):
    saved(off={'implementer': ['*@*'], 'vision': ['*@*'], 'heads_up': ['*@api']})
    with pytest.raises(UnschedulableError, match='tare_auto_no_route_available') as raised:
        turn_engine.resolve_auto({'native_auto_routes': 'implementer'})
    assert {r['code'] for r in raised.value.report['reasons']} == {'SWITCHED_OFF_FOR_ROLE'}
    text = turn_engine.describe_files(['image.png'], lambda _: None)
    assert 'no description is available' in text and 'switched off for vision' in text
    monkeypatch.setattr('relay.native_generation.generate_text', lambda *a, **k: pytest.fail('no writer is on'))
    from relay import heads_up
    assert heads_up.write('{}', []) == ([], [])


def test_no_auditor_left_leaves_the_change_unaudited_as_today(pool):
    saved(off={'auditor': ['*@*']})
    selected, receipt = native_routes.reviewers(native_catalog.select('openai-api'))
    assert selected == [] and receipt['generation_calls'] == 0
    assert 'SWITCHED_OFF_FOR_ROLE' in {c['reason'] for c in receipt['candidates']}


def test_a_malformed_profile_is_ignored_with_a_diagnostic_and_grants_nothing(pool):
    plain = {role: ranked(role, 'openai-api' if 'auditor' in role else None) for role in ('auditor', 'planner')}
    for broken in ('everything', ['*@*'], {'off': 'auditor'}, {'off': {'auditor': '*@*'}},
                   {'off': {'nobody': ['*@*']}, 'floor': {'auditor': 'middling'}}):
        environment.save({'routing_profile': broken})
        assert environment.profile()[1], broken
        assert {role: ranked(role, 'openai-api' if 'auditor' in role else None) for role in plain} == plain, broken
    saved(off={'auditor': ['claude/opus@api', 5, 'claude/opus@@api', 'Claude/Opus@api']}, on={'auditor': ['nope@x']})
    rules, problems = environment.profile()
    assert [text for text, *_ in rules['auditor']['off']] == ['claude/opus@api'] and rules['auditor']['on'] == ()
    assert len(problems) == 4
    assert ranked('auditor', 'openai-api')[1]['claude-api-opus-5-5'] == 'SWITCHED_OFF_FOR_ROLE'
    # An unreadable machine file already refuses every route; the profile adds nothing to it.
    environment.store().write_text('{', encoding='utf-8')
    assert environment.profile() == ({}, ())


def test_an_empty_profile_or_one_for_other_roles_changes_no_choice(pool):
    install(pool, 'claude-opus-5-5-cli', 'cli://claude', 'claude-opus-5-5')
    roles = {'planner': None, 'implementer': None, 'explorer': None, 'auditor': 'openai-api', 'plan-auditor': 'gemini-api'}
    plain = {role: ranked(role, producer) for role, producer in roles.items()}
    for profile in ({}, {'off': {}, 'on': {'auditor': []}}, {'off': {'vision': ['*@*'], 'heads_up': ['*@*']}}):
        environment.save({'routing_profile': profile})
        assert {role: ranked(role, producer) for role, producer in roles.items()} == plain


def test_the_cli_sets_shows_and_resets_and_the_file_holds_only_differences(pool, capsys, monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'fixture')  # present, never used: nothing is called
    assert environment.main(['--auto', 'auditor:claude/opus@api=off', '--auto', 'auditor:claude/opus-5.5@api=on',
                             '--auto', 'vision:gpt/luna@codex=off']) == 0
    out = capsys.readouterr().out
    assert 'claude-api-opus-5-5' in out and 'claude/opus-5.5@api' in out and 'on (claude/opus-5.5@api)' in out
    assert json.loads(environment.store().read_text(encoding='utf-8'))['routing_profile'] == {
        'off': {'auditor': ['claude/opus@api'], 'vision': ['gpt/luna@codex']}, 'on': {'auditor': ['claude/opus-5.5@api']}}
    assert environment.main(['--auto', 'auditor:claude/opus-5.5@api=default']) == 0
    assert 'off (claude/opus@api)' in capsys.readouterr().out
    assert environment.main(['--auto-reset', 'auditor']) == 0
    assert 'off (' not in capsys.readouterr().out
    assert json.loads(environment.store().read_text(encoding='utf-8'))['routing_profile'] == {
        'off': {'vision': ['gpt/luna@codex']}}
    assert environment.main(['--auto-show', 'planner', '--json']) == 0
    rows = json.loads(capsys.readouterr().out)['roles']['planner']
    assert {'route': 'claude-api-opus-5-5', 'line': 'claude/opus-5.5@api', 'profile': 'on', 'now': 'usable'} in rows
    assert environment.main(['--auto-reset']) == 0
    assert 'routing_profile' not in json.loads(environment.store().read_text(encoding='utf-8'))
    for bad in (['--auto', 'auditor:claude/opus@api=maybe'], ['--auto', 'nobody:*@*=off'],
                ['--auto', 'auditor:claude/opus=off'], ['--auto-show', 'nobody']):
        assert environment.main(bad) == 1
        assert 'tare setup:' in capsys.readouterr().err


def pin(monkeypatch, role, raw):
    monkeypatch.setattr(execution_scope, 'current', lambda: replace(execution_scope.Scope(), pins=((role, raw),)))


OFF_UNIT = ('SWITCHED_OFF_FOR_ROLE: claude-opus-5-5-cli is switched off for implementer in the routing profile '
            '(claude/opus@claude-code); the user can switch it on '
            '(tare setup --auto implementer:claude/opus-5.5@claude-code=on) '
            'or pin it')


def test_a_unit_route_the_coordinator_names_runs_only_when_on_or_pinned(pool, monkeypatch):
    install(pool, 'claude-opus-5-5-cli', 'cli://claude', 'claude-opus-5-5')
    parent = native_catalog.select('claude-opus-5-5-cli')
    saved(off={'implementer': ['claude/opus@claude-code']})
    refused = []
    assert list(work_units.routes(parent, chosen='claude-opus-5-5-cli', refused=refused)) == []
    assert refused == [OFF_UNIT]
    # The user's pin of that route is the user's choice: it runs.
    monkeypatch.setenv('TARE_SESSION_SCOPE', json.dumps({'pins': [['implementer', 'anthropic/claude-opus-5-5-cli@high']]}))
    assert [n for n, _ in work_units.routes(parent, chosen='claude-opus-5-5-cli')] == ['claude-opus-5-5-cli']
    monkeypatch.delenv('TARE_SESSION_SCOPE')
    environment.save({})  # on again: it runs as before
    assert [n for n, _ in work_units.routes(parent, chosen='claude-opus-5-5-cli')][0] == 'claude-opus-5-5-cli'


def test_a_unit_on_an_off_route_starts_nothing_and_tells_the_coordinator_why(setup, tmp_path, monkeypatch):
    config, custody, root = setup
    repository(root)
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'storage'))
    monkeypatch.setattr(turn_engine, 'policy', lambda: {'implementer': [['child-route']]})
    monkeypatch.setattr(turn_engine, 'available', lambda name: (deployment(), None))
    called = []
    monkeypatch.setattr(native_agent, 'run_session', lambda *a, **kw: called.append(1))
    saved(off={'implementer': ['fixture']})  # the route's own id, whatever the policy calls it
    custody.mkdir(parents=True, exist_ok=True)
    result = work_units.run_unit(custody, {'config': config, 'deployment': deployment()},
                                 {'title': 'Set the value', 'target_paths': ['a.py'], 'criteria': ['a.py holds 2'],
                                  'verifiers': ['unit'], 'route': 'child-route'}, lambda: False)
    assert result['verdict'] == 'INCONCLUSIVE' and not called
    assert result['reason'].startswith('SWITCHED_OFF_FOR_ROLE: child-route is switched off for implementer')
    assert 'tare setup --auto implementer:fixture=on' in result['reason']


def test_a_planner_the_coordinator_names_runs_only_when_on_or_pinned(planner, monkeypatch):
    parent, state, candidate = planner
    calls = []
    stub_child(monkeypatch, candidate, calls)
    saved(off={'planner': ['fixture-planner']})  # the route's own id, whatever the coordinator calls it
    tool = wrap(parent, state, execution())
    with pytest.raises(ValueError, match=r'^SWITCHED_OFF_FOR_ROLE: worker is switched off for planner .*'
                                         r'tare setup --auto planner:fixture-planner=on'):
        tool.choose({'route': 'worker'})
    refused = tool.execute(request({'instruction': 'Plan.', 'route': 'worker'}), execution())
    assert refused.status == 'refused' and 'SWITCHED_OFF_FOR_ROLE: worker' in refused.output_json
    assert calls == [] and not (parent/'planning').exists()
    # The user's pin of that route runs it; with the route on, the coordinator's choice runs as before.
    pin(monkeypatch, 'planner', 'openai/worker@high')
    monkeypatch.setattr(execution_scope, 'pinned', lambda role: dict(copy.deepcopy(candidate),
                                                                    native_model_choice={'connection': 'worker'}))
    assert tool.choose({'route': 'worker'})[0] == 'worker'
    monkeypatch.setattr(execution_scope, 'current', lambda: execution_scope.Scope())
    environment.save({})
    assert tool.choose({'route': 'worker'})[0] == 'worker'


def test_a_research_route_the_coordinator_names_runs_only_when_on_or_pinned(host, monkeypatch):
    tools, control, _ = host
    called = []
    monkeypatch.setattr(research_cli, 'research', lambda deployment, *a, **k: called.append(deployment['id']) or {
        'status': 'COMPLETED', 'report': report(), 'calls_used': 1, 'usage': None})
    saved(off={'researcher': ['gpt/luna@codex']})
    refused = json.loads(tools.execute(effect('research', {'question': 'TTL?', 'route': 'luna'}), control).output_json)
    assert refused['status'] == 'UNAVAILABLE' and called == []
    assert refused['details'][0]['route'] == 'luna'
    assert refused['details'][0]['reason'].startswith('SWITCHED_OFF_FOR_ROLE: luna is switched off for researcher')
    pin(monkeypatch, 'researcher', 'openai/luna@medium')
    monkeypatch.setattr(research_cli, 'effort', lambda deployment: 'medium')
    pinned = json.loads(tools.execute(effect('research', {'question': 'TTL?', 'route': 'luna'}, 'pinned'), control).output_json)
    assert pinned['status'] == 'COMPLETED' and called == ['luna']
    monkeypatch.setattr(execution_scope, 'current', lambda: execution_scope.Scope())
    environment.save({})
    on = json.loads(tools.execute(effect('research', {'question': 'TTL now?', 'route': 'luna'}, 'on'), control).output_json)
    assert on['status'] == 'COMPLETED' and called == ['luna', 'luna']


# Review of 2026-11-14 (another vendor's model): four blocking defects and two minor ones.

def test_with_every_implementer_off_a_unit_never_falls_back_to_the_sessions_model(pool, monkeypatch):
    install(pool, 'claude-opus-5-5-cli', 'cli://claude', 'claude-opus-5-5')
    parent = native_catalog.select('claude-opus-5-5-cli')
    saved(off={'implementer': ['*@*']})
    refused = []
    assert list(work_units.routes(parent, allow_parent=True, refused=refused)) == []
    assert refused[0].startswith('SWITCHED_OFF_FOR_ROLE: claude-opus-5-5-cli is switched off for implementer')
    # The user's pin for implementer on that route still wins.
    monkeypatch.setenv('TARE_SESSION_SCOPE', json.dumps({'pins': [['implementer', 'anthropic/claude-opus-5-5-cli@high']]}))
    assert [n for n, _ in work_units.routes(parent, allow_parent=True)] == ['claude-opus-5-5-cli']


def test_a_configured_review_list_reads_the_profile_of_its_own_review_role(pool, monkeypatch):
    for key in ('OPENAI_API_KEY', 'GEMINI_API_KEY'):
        monkeypatch.setenv(key, 'fixture')  # present, never used: nothing is called
    turn_engine.save_policy({'auditor': [['openai-api', 'gemini-api']]}, auditor_selection='configured')
    producer = native_catalog.select('claude-api-opus-5-5')

    def names(role):
        return [d['id'] for d in native_routes.reviewers(producer, role, check_resources=False)[0]]
    assert names('auditor') == names('plan-auditor') == ['openai-api', 'gemini-api']
    saved(off={'plan-auditor': ['*@*']})
    assert names('plan-auditor') == [] and names('auditor') == ['openai-api', 'gemini-api']
    saved(off={'auditor': ['*@*']})
    assert names('auditor') == [] and names('plan-auditor') == ['openai-api', 'gemini-api']


def test_research_reads_the_researcher_profile_in_both_of_its_branches(monkeypatch):
    helper = {'id': 'codex-cli-gpt-6-luna', 'native_cli': {'executable': 'codex'},
              'native_profile': {'protocol': 'vendor-cli', 'endpoint': 'cli://codex', 'model': 'gpt-6-luna'}}
    profiles = {'codex-cli-gpt-6-luna': helper, 'claude-api-sonnet-5-5': native_catalog.select('claude-api-sonnet-5-5')}
    monkeypatch.setattr(turn_engine, 'policy', lambda: {**turn_engine.DEFAULT_POLICY,
                                                        'implementer': [['claude-api-sonnet-5-5']]})
    monkeypatch.setattr(turn_engine, 'available', lambda name: (copy.deepcopy(profiles[name]), None))
    monkeypatch.setattr(turn_engine, 'search_links', lambda text: [])  # no results: the Codex web search answers
    started = []

    def run(argv, **kwargs):
        started.append('codex')
        Path(argv[argv.index('-o') + 1]).write_text('Answer.', encoding='utf-8')
    monkeypatch.setattr(turn_engine.subprocess, 'run', run)

    def research(*a, **k):
        started.append('summary')
        raise ValueError('fixture: nothing to read')
    monkeypatch.setattr('relay.research.research', research)
    saved(off={'researcher': ['*@*']})
    record, answer = turn_engine.run_research('What changed?', lambda text: None)
    assert record is None and 'web search is not available' in answer and 'SWITCHED_OFF_FOR_ROLE' in answer
    record, answer = turn_engine.read_links('What changed?', ['https://docs.example/x'], lambda text: None,
                                            mode='links', max_pages=1, max_depth=0)
    assert record is None and 'no route is available for /research' in answer and 'SWITCHED_OFF_FOR_ROLE' in answer
    assert started == []
    # Only implementer entries off: research is unaffected.
    saved(off={'implementer': ['*@*']})
    emitted = []
    turn_engine.read_links('What changed?', ['https://docs.example/x'], emitted.append, mode='links', max_pages=1,
                           max_depth=0)
    assert 'claude-api-sonnet-5-5' in ''.join(emitted)
    assert turn_engine.run_research('What changed?', lambda text: None)[1] == 'Answer.'
    assert started == ['summary', 'codex']


def test_a_route_id_covers_every_route_of_its_model_on_its_source(capsys):
    templates = json.loads(cli_discovery.TEMPLATES.read_text(encoding='utf-8'))
    saved(off={'auditor': ['claude-opus-5-5-cli']})  # Claude Code's Opus 5.5 under one of its ids
    for name in ('claude-cli', 'claude-opus-5-5-cli'):
        assert environment.profile_state('auditor', templates[name]) == ('off', 'claude-opus-5-5-cli')
    assert any('claude-opus-5-5-cli runs a model line; read as claude/opus-5.5@claude-code' in p
               for p in environment.profile()[1])
    # The refusal the coordinator relays names the entry to use.
    off = environment.switched_off('auditor', templates['claude-cli'], 'claude-cli')
    assert 'tare setup --auto auditor:claude/opus-5.5@claude-code=on' in off
    # tare setup refuses such an id and prints the equivalent entries.
    assert environment.main(['--auto', 'auditor:claude-cli=on']) == 1
    error = capsys.readouterr().err
    assert 'auditor:claude/opus-5.5@claude-code=on' in error and 'auditor:claude/opus@claude-code=on' in error
    # Switching the suggested entry on replaces the saved id instead of tying with it.
    assert environment.main(['--auto', 'auditor:claude/opus-5.5@claude-code=on']) == 0
    assert json.loads(environment.store().read_text(encoding='utf-8'))['routing_profile'] == {
        'on': {'auditor': ['claude/opus-5.5@claude-code']}}
    assert environment.profile_state('auditor', templates['claude-cli'])[0] == 'on'


def test_an_alias_is_checked_as_the_route_it_realises(pool, monkeypatch):
    # local-default is the fleet's default route: an entry for that route covers it, as tare models lists it.
    saved(off={'implementer': ['qwen38-gsq']})
    tier, chosen, _, skipped = turn_engine.choose([['local-default']], probe=False)
    assert chosen is None and skipped[0]['diagnostic']['code'] == 'SWITCHED_OFF_FOR_ROLE'
    # local-auto on a busy or silent host is its own profile, a route of its own (finding D: never "the same model").
    assert turn_engine.choose([['local-auto']], probe=False)[1] == 'local-auto'
    saved(off={'implementer': ['local-auto']})
    assert turn_engine.choose([['local-auto']], probe=False)[1] is None
    assert turn_engine.choose([['local-default']], probe=False)[1] == 'local-default'
    # local-auto on an idle host realises Swift Flash-Next: that route's own entry covers it, local-auto's does not.
    monkeypatch.setattr('compute_plane.model_facts.read_json_observation', lambda url, timeout: {
        'status': 'OBSERVED', 'value': {'model': 'swift-next', 'mode': 'solo'}})
    deployment, _ = turn_engine.available('local-auto', probe=False)
    assert deployment['native_profile']['model'] == 'swift-next'
    assert turn_engine.choose([['local-auto']], probe=False)[1] == 'local-auto'
    saved(off={'implementer': ['swift-next']})
    assert turn_engine.choose([['local-auto']], probe=False)[1] is None
    # select('auto') takes the first ranked candidate: each was checked, under every id of its model (round one, 4).
    install(pool, 'claude-cli', 'cli://claude', 'claude-opus-5-5')
    install(pool, 'claude-opus-5-5-cli', 'cli://claude', 'claude-opus-5-5')
    saved(off={'explorer': ['claude-cli']})
    reasons = ranked('explorer')[1]
    assert reasons['claude-cli'] == reasons['claude-opus-5-5-cli'] == 'SWITCHED_OFF_FOR_ROLE'
    saved(off={'explorer': ['*@*']})
    with pytest.raises(UnschedulableError, match='NO_ELIGIBLE_NATIVE_ROLE:explorer'):
        native_catalog.select('auto', role='explorer')
    # A name of automatic routing is not a route: tare setup refuses it, and a saved one is ignored and named.
    assert environment.main(['--auto', 'implementer:local-default=off']) == 1
    saved(off={'implementer': ['local-default', 'tare-auto']})
    assert environment.profile()[0] == {} and len(environment.profile()[1]) == 2


def test_a_version_too_long_to_read_is_a_malformed_entry():
    huge = 'claude/fable-' + '9' * 5000 + '@api'
    assert environment.entry(huge) is None
    assert environment.entry('claude/fable-5.2.1@api') is not None and environment.entry('gpt/sol-1.2.3.4@api') is None
    saved(off={'planner': [huge], 'auditor': ['claude/fable@api']})
    fable = native_catalog.select('claude-api-fable-5-2')
    assert environment.profile_state('auditor', fable) == ('off', 'claude/fable@api')
    assert any('is not a route id or MODEL@SOURCE' in p for p in environment.profile()[1])


def test_the_profile_is_parsed_once_per_version_of_the_file_and_a_rewrite_is_seen(monkeypatch):
    saved(off={'auditor': ['claude/opus@api']})
    parsed = []
    real = environment._profile
    monkeypatch.setattr(environment, '_profile', lambda value, role: parsed.append(role) or real(value, role))
    opus = native_catalog.select('claude-api-opus-5-5')
    for _ in range(5):
        assert environment.profile_state('auditor', opus) == ('off', 'claude/opus@api')
    assert parsed == ['auditor']  # one parse, of the role asked
    # Another process rewrites the file (tare setup --auto there): the next decision reads it.
    environment.store().write_text(json.dumps({'schema': environment.SCHEMA, 'routing_profile': {
        'off': {'auditor': ['claude/opus-5.5@api']}}}), encoding='utf-8')
    assert environment.profile_state('auditor', opus) == ('off', 'claude/opus-5.5@api')
    assert parsed == ['auditor', 'auditor']


def test_the_profile_is_read_again_after_a_save_in_this_process():
    saved(off={'auditor': ['claude/opus@api']})
    opus = native_catalog.select('claude-api-opus-5-5')
    assert environment.profile_state('auditor', opus) == ('off', 'claude/opus@api')
    environment.save({'routing_profile': {'on': {'auditor': ['claude/opus@api']}}})  # tare setup --auto, here
    assert environment.profile_state('auditor', opus) == ('on', 'claude/opus@api')


# Review of 2026-11-14, round two: three paths and two matching rules let a switched-off route run, and two minor ones.

def test_a_planner_never_reviews_its_own_plan_on_a_route_off_for_plan_auditor(proposal, monkeypatch):  # A
    from relay import native_plan
    directory, graph, value, _ = proposal
    monkeypatch.setattr('relay.native_routes.credential_available', lambda _: False)  # no independent reviewer
    monkeypatch.setattr('relay.native_agent.run_session', lambda *a, **k: pytest.fail('no model call'))
    saved(off={'plan-auditor': ['qwen38-gsq']})  # on for the planner
    planner = native_catalog.select('local-qwen')
    result = native_plan.generate(directory.parent/'project', graph, directory.parent/'new-plan', 'Change and test',
                                  planner)
    assert result['status'] == 'ROUTE_BLOCKED' and result['model_calls'] == 0
    assert result['reason'].startswith('No eligible plan-auditor: SWITCHED_OFF_FOR_ROLE: ')
    assert 'switched off for plan-auditor' in result['reason']
    assert result['routes']['plan-auditor']['reason'] == 'SWITCHED_OFF_FOR_ROLE'
    assert result['routes']['planner']['deployment'] == planner['id']


def test_tare_auto_reads_the_profile_of_the_role_it_runs_in(tiers, monkeypatch):  # B
    saved(off={'researcher': ['claude/sonnet@api']})
    auto = {'native_auto_routes': 'implementer'}
    assert turn_engine.resolve_auto(auto)['id'] == 'claude-api-sonnet-5-5'  # the implementer's entries
    for request in (None, 'Find the release notes.'):
        resolved = turn_engine.resolve_auto(auto, request=request, role='researcher')
        assert resolved['id'] == 'openai-api'
        assert resolved['native_auto_selection']['skipped'][0]['diagnostic']['code'] == 'SWITCHED_OFF_FOR_ROLE'
    saved(off={'researcher': ['gpt/sol@api']})
    blocked = {**native_catalog.select('claude-api-sonnet-5-5'), 'native_auto_selection': {'tier': 0}}
    assert turn_engine.next_auto(blocked)['id'] == 'openai-api'
    assert turn_engine.next_auto(blocked, role='researcher')['id'] == 'gemini-api'
    # /research's own tare auto asks for the researcher's entries.
    from relay import research
    asked = []

    def resolve(deployment, start=0, request=None, role='implementer'):
        asked.append(role)
        raise RuntimeError('stop here')
    monkeypatch.setattr(turn_engine, 'resolve_auto', resolve)
    with pytest.raises(RuntimeError, match='stop here'):
        research.research(Path('jobs'), 'What changed?', ['https://docs.example/x'], domains=['docs.example'],
                          max_pages=1, max_depth=0)
    assert asked == ['researcher']


def test_a_fork_or_mode_change_onto_a_route_now_off_starts_on_the_next_one(runtime, monkeypatch):  # C
    project, state, _ = runtime
    monkeypatch.setattr(turn_engine, 'available', lambda name: ({'id': name, 'native_profile': {
        'protocol': 'openai-chat', 'endpoint': 'https://gateway.example/v1', 'model': name}}, None))
    monkeypatch.setattr(turn_engine, 'snapshot', lambda root: (None, 'test'))
    monkeypatch.setattr(turn_engine, 'terminal_for', lambda session, config: None)
    monkeypatch.setattr(turn_engine, 'session_terminal', lambda session: None)
    monkeypatch.setattr('relay.native_continuity.preview', lambda source, project: {'text': 'BRIEF'})
    serve_agent.run_turn(copilot(project, 'first'), lambda text: None)
    assert state['calls'][-1]['route'] == 'local-a'
    saved(off={'implementer': ['local-a']})
    # The same session goes on: it started while its route was on.
    serve_agent.run_turn(copilot(project, 'first', 'second'), lambda text: None)
    assert state['calls'][-1]['resume'] is True and state['calls'][-1]['custody'] == state['calls'][0]['custody']
    # A mode change starts a fresh session: a new start, on the next route that is on.
    serve_agent.run_turn(copilot(project, 'first', 'second', '/read-only look again'), lambda text: None)
    assert state['calls'][-1]['route'] == 'local-b' and 'resume' not in state['calls'][-1]
    # A fork (an edited earlier message) too, from what the client shows.
    saved(off={'implementer': ['local-a', 'local-b']})
    serve_agent.run_turn(copilot(project, 'first', 'second, but differently'), lambda text: None)
    assert state['calls'][-1]['route'] == 'cheap'
    assert state['calls'][-1]['instruction'].startswith('Conversation so far')


def http(pool, name, endpoint, model, protocol='openai-chat'):
    value = {'id': name, 'runner': 'native-http', 'native_counter_protocol': 'utf8-envelope-estimate-v1',
             'native_profile': {'protocol': protocol, 'endpoint': endpoint, 'model': model, 'context_window': 8192,
                                'output_tokens': 1024, 'source': 'test', 'parameters_json': '{}'}}
    pool.mkdir(exist_ok=True)
    (pool/(name+'.json')).write_text(json.dumps(value), encoding='utf-8')
    return native_catalog.select(name)


def test_a_route_with_no_line_is_one_route_never_another_of_the_same_wire_name(pool):  # D
    first = http(pool, 'gateway-a-default', 'http://10.0.0.1:8080/v1', 'default')
    second = http(pool, 'gateway-b-default', 'http://10.0.0.2:8080/v1', 'default')
    high = http(pool, 'gateway-a-default-high', 'http://10.0.0.1:8080/v1', 'default-high')
    assert {environment.route_label(d) for d in (first, second, high)} == {'@local'}
    saved(off={'implementer': ['gateway-a-default']})
    assert state('implementer', '', first) == ('off', 'gateway-a-default')
    assert state('implementer', '', second) is None and state('implementer', '', high) is None
    saved(off={'implementer': ['*@local']}, on={'implementer': ['gateway-b-default']})
    assert state('implementer', '', second) == ('on', 'gateway-b-default')
    assert state('implementer', '', first) == state('implementer', '', high) == ('off', '*@local')


def test_a_custom_endpoint_has_no_line_whatever_its_wire_name(pool):  # E
    reseller = http(pool, 'reseller-fable', 'https://reseller.example/v1', 'claude-fable-5-2', 'anthropic-messages')
    fable = native_catalog.select('claude-api-fable-5-2')
    assert environment.route_label(reseller) == '@other-api' and environment.route_label(fable) == 'claude/fable-5.2@api'
    saved(off={'auditor': ['reseller-fable']})
    assert state('auditor', '', reseller) == ('off', 'reseller-fable') and state('auditor', '', fable) is None
    saved(off={'auditor': ['claude/fable@*']})
    assert state('auditor', '', reseller) is None and state('auditor', '', fable) == ('off', 'claude/fable@*')
    # Saving the first-party entry keeps the reseller's own entry.
    environment.save({})
    assert environment.main(['--auto', 'auditor:reseller-fable=off', '--auto', 'auditor:claude/fable-5.2@api=on']) == 0
    assert json.loads(environment.store().read_text(encoding='utf-8'))['routing_profile'] == {
        'off': {'auditor': ['reseller-fable']}, 'on': {'auditor': ['claude/fable-5.2@api']}}
    # AGY serves Anthropic's line under its own id, effort and all: that line on AGY.
    templates = json.loads(cli_discovery.TEMPLATES.read_text(encoding='utf-8'))
    agy = templates['agy-claude-opus-5-5-cli']
    low = copy.deepcopy(agy)
    low['native_profile']['model'] = 'claude-opus-5-5-low'
    assert environment.route_label(agy) == environment.route_label(low) == 'claude/opus-5.5@agy'


def test_the_profile_handed_out_cannot_be_changed_by_a_caller():  # F
    saved(off={'auditor': ['claude/opus@api']})
    opus = native_catalog.select('claude-api-opus-5-5')
    for role in (None, 'auditor'):  # what tare setup shows, and what each decision for the role reads
        rules, problems = environment.profile(role=role)
        for change in (lambda: rules['auditor']['off'].clear(), lambda: rules['auditor'].update(off=[]),
                       lambda: rules.pop('auditor'), lambda: problems.append('x')):
            try:
                change()
            except (AttributeError, TypeError):
                pass
    assert environment.profile_state('auditor', opus) == ('off', 'claude/opus@api')
    assert environment.profile(role='auditor')[0]['auditor']['off'][0][0] == 'claude/opus@api'


def test_an_entry_is_replaced_by_one_read_as_the_same_rule():  # G
    saved(off={'auditor': ['claude/fable-05.2@api']})
    assert environment.main(['--auto', 'auditor:claude/fable-5.2@api=on']) == 0
    assert json.loads(environment.store().read_text(encoding='utf-8'))['routing_profile'] == {
        'on': {'auditor': ['claude/fable-5.2@api']}}


# The backstop: every new start of a role passes execution_scope.admit, in session_role for sessions.

def test_a_session_started_by_a_path_with_no_gate_is_refused(setup):
    config, custody, root = setup
    saved(off={'implementer': ['fixture']})
    client = Client(steps=[('read', {'repository': 'repo', 'path': 'a.py'}),
                           ('tare_deliver', {'summary': 'Inspected a.py.'})])

    def ungated():  # picks a route and starts it, asking nothing about the routing profile
        return native_agent.run_session(dict(config, verifiers={}), deployment(), custody, 'Inspect a.py.',
                                        client=client, local_counter=client)
    with pytest.raises(ValueError, match=r'^SWITCHED_OFF_FOR_ROLE: fixture is switched off for implementer'):
        ungated()
    assert client.generations == 0 and not (custody/'native-agent.json').exists()
    # The same start, marked as the user's own choice of this route for this role, runs.
    with execution_scope.user_choice('implementer', deployment()):
        assert ungated()['status'] == 'PROPOSED'
    assert client.generations == 2


def test_only_the_users_choice_of_this_route_for_this_role_passes_the_backstop(monkeypatch):
    saved(off={'implementer': ['*@*'], 'auditor': ['*@*'], 'plan-auditor': ['*@*']})
    route = native_catalog.select('claude-api-opus-5-5')
    other = native_catalog.select('claude-api-sonnet-5-5')

    def refused(role, value=route):
        try:
            execution_scope.admit(value, role)
        except ValueError as exc:
            assert str(exc).startswith('SWITCHED_OFF_FOR_ROLE: ')
            return True
        return False
    assert refused('implementer') and refused('auditor')
    # The picker's choice (user_choice) is for one role and one route.
    with execution_scope.user_choice('implementer', route):
        assert not refused('implementer') and refused('auditor') and refused('implementer', other)
    # A pin of this route for this role; an auditor pin also covers plan reviews; another role or route does not.
    pin(monkeypatch, 'implementer', 'anthropic/claude-api-opus-5-5@high')
    assert not refused('implementer') and refused('auditor') and refused('implementer', other)
    pin(monkeypatch, 'auditor', 'anthropic/claude-api-opus-5-5@high')
    assert not refused('auditor') and not refused('plan-auditor') and refused('implementer')
    # --coordinator: the conversation's model, a launch flag; a conversation otherwise reads the implementer entries.
    monkeypatch.setattr(execution_scope, 'current', lambda: execution_scope.Scope())
    assert refused('coordinator')
    monkeypatch.setattr(execution_scope, 'current', lambda: execution_scope.Scope(
        coordinator='anthropic/claude-api-opus-5-5@high'))
    assert not refused('coordinator') and refused('coordinator', other) and refused('implementer')
    # A role the profile does not know is not its to refuse.
    assert not refused('editorial-reviewer')


def test_a_resumed_session_goes_on_and_a_new_one_on_an_off_route_is_refused(tmp_path):
    saved(off={'implementer': ['fixture']})
    run = execution_scope.session_role(lambda config, deployment, custody, *a, **k: 'started')
    config = {'role': 'implementer'}
    with pytest.raises(ValueError, match='SWITCHED_OFF_FOR_ROLE'):  # a fork, a mode change, a retry: new sessions
        run(config, deployment(), tmp_path/'new', 'Go.')
    with pytest.raises(ValueError, match='SWITCHED_OFF_FOR_ROLE'):  # a conversation: tare auto's implementer tiers
        run(config, deployment(), tmp_path/'chat', 'Go.', conversation=True)
    (tmp_path/'saved').mkdir()
    (tmp_path/'saved'/'native-agent.json').write_text(json.dumps({'config': config, 'deployment': deployment()}))
    assert run(None, None, tmp_path/'saved', resume=True, follow_up='More.', message_id='m') == 'started'
    # A session it starts is a start of its own: the user's choice for this session does not reach it.
    outer = execution_scope.session_role(lambda config, deployment, custody, *a, **k: run(
        config, deployment, Path(custody)/'child', 'Go.'))
    with execution_scope.user_choice('implementer', deployment()):
        assert run(config, deployment(), tmp_path/'chosen', 'Go.') == 'started'
        with pytest.raises(ValueError, match='SWITCHED_OFF_FOR_ROLE'):
            outer(config, deployment(), tmp_path/'outer', 'Go.')


def test_a_provider_switch_starts_only_when_the_user_chose_its_route(tmp_path):
    from relay import native_continuity
    saved(off={'implementer': ['fixture']})
    run = execution_scope.session_role(lambda config, deployment, custody, *a, **k: 'started')
    source = tmp_path/'conversation'
    source.mkdir()
    (source/'native-agent.json').write_text(json.dumps({'config': {'role': 'implementer'}, 'conversation_flow': True}))
    view = {'source': str(source), 'project': str(tmp_path/'project'), 'text': 'BRIEF', 'user_context': 'BRIEF'}
    native_continuity.save_request(tmp_path/'escalated', view, 'BRIEF', deployment())  # tare auto's next tier
    with pytest.raises(ValueError, match='SWITCHED_OFF_FOR_ROLE'):
        run(None, None, tmp_path/'escalated')
    native_continuity.save_request(tmp_path/'picked', view, 'BRIEF', deployment(), chosen=True)  # the TUI's switch
    assert run(None, None, tmp_path/'picked') == 'started'


def test_the_routes_the_user_names_are_marked_for_their_own_role(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(native_agent, 'run_session', lambda config, deployment, custody, *a, **k: seen.append(
        (deployment['id'], set(execution_scope.CHOSEN.get()))) or {'status': 'PROPOSED', 'output': 'ok',
                                                                    'authority': 'NONE', 'model_calls': 0})
    common = ['--root', str(tmp_path), '--instruction', 'Inspect', '--no-context']
    assert native_agent.main([*common, '--session', str(tmp_path/'a'), '--role', 'explorer',
                              '--deployment', 'claude-api-opus-5-5']) == 0
    assert seen[-1] == ('claude-api-opus-5-5', {('explorer', 'claude-api-opus-5-5')})
    assert native_agent.main([*common, '--session', str(tmp_path/'b'), '--conversation',
                              '--deployment', 'claude-api-opus-5-5']) == 0
    assert seen[-1] == ('claude-api-opus-5-5', {('coordinator', 'claude-api-opus-5-5')})


def test_the_explorers_set_in_delegation_are_the_users_choice_and_a_sampled_challenger_is_not(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from relay import delegation, delegation_tools
    saved(off={'explorer': ['fixture']})
    started = []
    monkeypatch.setattr(native_agent, 'run_session', execution_scope.session_role(
        lambda config, deployment, custody, *a, **k: started.append(deployment['id']) or {'status': 'PROPOSED'}))
    monkeypatch.setattr(delegation, 'experiments', lambda route: {'leaf_project_instructions': False})
    variant = {'identity': {}}
    outcomes = {}
    for selection, experimental in (('CONFIGURED_EXPLORER', False), ('OVERFLOW_EXPLORER', False),
                                    ('CONFIGURED_EXPLORER', True), ('TEMPORARY_TUI_EXECUTION_POLICY', False)):
        tools = object.__new__(delegation_tools.ExplorationTools)
        tools.settings, tools.state, tools.route_class = {'seconds': 30}, {'deadline_utc': None, 'config': {}}, None
        tools.frozen = {'selection': selection, 'experimental': experimental, 'variant': variant}
        tools.finish = lambda folder, row: None
        folder = tmp_path/f'{selection}-{experimental}'
        folder.mkdir()
        row = {'repository': 'repo', 'repository_root': str(tmp_path), 'source_path': '.', 'question': 'Where?',
               'calls_reserved': 4, 'status': 'RUNNING'}
        tools.investigate(folder, row, deployment(), variant, {'others': [], 'part': None, 'map': None},
                          SimpleNamespace(should_stop=lambda: False))
        outcomes[(selection, experimental)] = row.get('reason', 'started')
    assert outcomes[('CONFIGURED_EXPLORER', False)] == outcomes[('OVERFLOW_EXPLORER', False)] == 'started'
    assert outcomes[('CONFIGURED_EXPLORER', True)].startswith('SWITCHED_OFF_FOR_ROLE: ')
    assert outcomes[('TEMPORARY_TUI_EXECUTION_POLICY', False)].startswith('SWITCHED_OFF_FOR_ROLE: ')
    assert started == ['fixture', 'fixture']
