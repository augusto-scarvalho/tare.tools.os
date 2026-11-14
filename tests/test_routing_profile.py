"""The routing profile (`routing_profile` in environment.json): which routes automatic routing may use, per role."""
import copy
import json

import pytest

from relay import environment, native_catalog, native_routes, turn_engine, work_units
from relay.scheduling_diagnostics import UnschedulableError
from test_cross_vendor_auditors import install, pool  # noqa: F401


def saved(off=None, on=None, **extra):
    environment.save({'routing_profile': {**({'off': off} if off else {}), **({'on': on} if on else {}), **extra}})


def ranked(role, producer=None):
    chosen, selection = native_routes.ranked(role, producer and native_catalog.select(producer))
    return [d['id'] for d in chosen], {c['deployment']: c['reason'] for c in selection['candidates']}


def state(role, name, deployment=None):
    return environment.profile_state(role, deployment or native_catalog.select(name), name)


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
    # Explicit: the picker's concrete route, a unit's chosen route, and availability itself.
    chosen = native_catalog.select('claude-opus-5-5-cli')
    assert turn_engine.resolve_auto(chosen) is chosen
    assert turn_engine.available('claude-opus-5-5-cli')[0] is not None
    assert [n for n, _ in work_units.routes(chosen, chosen='claude-opus-5-5-cli')][0] == 'claude-opus-5-5-cli'
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
    assert [text for text, *_ in rules['auditor']['off']] == ['claude/opus@api'] and rules['auditor']['on'] == []
    assert len(problems) == 4
    assert ranked('auditor', 'openai-api')[1]['claude-api-opus-5-5'] == 'SWITCHED_OFF_FOR_ROLE'
    # An unreadable machine file already refuses every route; the profile adds nothing to it.
    environment.store().write_text('{', encoding='utf-8')
    assert environment.profile() == ({}, [])


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
