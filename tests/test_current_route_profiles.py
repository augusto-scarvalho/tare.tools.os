"""TUI -> role admission -> exact model facts -> helper input, without inference."""
import json
from types import SimpleNamespace

import pytest

from relay import native_catalog, native_routes, route_facts, route_plan, round_table_roster
from test_model_choices import profile


@pytest.fixture
def installed(tmp_path, monkeypatch):
    monkeypatch.setenv('TARE_PROFILE_DIR', str(tmp_path))
    monkeypatch.delenv('TARE_ROLE_DEPLOYMENTS', raising=False)
    monkeypatch.delenv('TARE_ROUTE_EVIDENCE', raising=False)
    # Real checked-in role policy and roster; only portable host data is authored.
    rows = {'claude-cli': ('claude-opus-5-5', 'low'),
            'claude-sonnet-5-5-cli': ('claude-sonnet-5-5', 'high'),
            'claude-fable-5-1-cli': ('claude-fable-5-1', 'high')}
    for name, (model, effort) in rows.items():
        value = profile(model=model, effort=effort)
        value['id'] = name
        (tmp_path/(name+'.json')).write_text(json.dumps(value))
    monkeypatch.setattr(native_routes, 'credential_available', lambda _: False)
    original = route_facts.collect
    def collect(deployment, **kwargs):
        return original(deployment, live=False, evidence={})
    monkeypatch.setattr(route_facts, 'collect', collect)
    return tmp_path


def test_installed_versions_efforts_and_sources_reach_actual_route_observer(installed):
    selected, receipt = native_routes.ranked('implementer', check_resources=True)
    assert [d['id'] for d in selected] == ['claude-cli', 'claude-sonnet-5-5-cli']
    assert all(d['native_profile']['protocol'] == 'vendor-cli' for d in selected)
    missing = next(d for d in receipt['candidates'] if d['deployment'] == 'agy-cli')
    assert missing['reason'] == 'PROFILE_NOT_INSTALLED'
    requests = []
    def assess(command, request, **kwargs):
        requests.append(request)
        return {'status': 'OBSERVED', 'inference_attempts': 0,
                'answer': {'choice': 'insufficient', 'usage': {'input_tokens': 0}}}
    policy = {'mode': 'shadow', 'candidates': {'implementer': [
        {'deployment': d['id']} for d in selected]}}
    result = route_plan.observe({'title': 'Repair concurrency'}, selected[0], {}, policy,
        SimpleNamespace(is_cancelled=lambda: False), installed, assess, automatic_implementation=True)
    cards = json.loads(requests[0]['state'])['candidates']
    assert [(c['wire_model'], c['protocol'], c['effort']) for c in cards] == [
        ('claude-opus-5-5', 'vendor-cli', 'low'), ('claude-sonnet-5-5', 'vendor-cli', 'high')]
    assert [c['facts']['quality']['records'][0]['value'] for c in cards] == [57.8, 55.5]
    # Anthropic publishes Opus 5.5's CursorBench 4.0 without an effort: unknown, never assumed.
    assert cards[0]['facts']['quality']['records'][0]['applicability']['effort'] == 'UNKNOWN'
    for card in cards:
        assert card['facts']['quality']['omitted_records'] == 1
        assert len(card['facts']['quality']['records']) == 1
        assert card['facts']['quota']['status'] == 'UNKNOWN'
        assert card['facts']['configured_cost_tier'] == 'flat'
        assert 'price_reference' not in card['facts']
        assert not card['facts']['quality']['records'][0]['applicability']['current_profile_qualified']
    assert result['applied'] is False and result['roles']['implementer']['primary'] is None
    # TUI files are not rewritten by identity resolution or observation.
    assert json.loads((installed/'claude-fable-5-1-cli.json').read_text())['model'] == 'claude-fable-5-1'


@pytest.mark.parametrize('fault,reason', [('absent', 'PROFILE_NOT_INSTALLED'),
    ('wrong_version', 'PROFILE_OR_IDENTITY_INVALID'), ('missing_executable', 'CLI_EXECUTABLE_UNAVAILABLE')])
def test_broken_host_candidate_is_excluded_without_blocking_other_profiles(installed, fault, reason):
    path = installed/'claude-sonnet-5-5-cli.json'
    value = json.loads(path.read_text())
    if fault == 'absent': path.unlink()
    else:
        if fault == 'wrong_version': value['native_profile']['model'] = 'claude-opus-5-5'
        else: value['native_cli']['executable'] = str(installed/'absent.exe')
        path.write_text(json.dumps(value))
    selected, receipt = native_routes.ranked('implementer')
    row = next(r for r in receipt['candidates'] if r['deployment'] == 'claude-sonnet-5-5-cli')
    assert not row['eligible'] and row['reason'] == reason
    assert 'claude-cli' in [d['id'] for d in selected]


def test_family_independence_and_version_aliases_are_preserved(installed):
    author = native_catalog.select('claude-cli')
    selected, receipt = native_routes.ranked('auditor', author)
    assert all(d['model'] != 'claude-opus-5.5' for d in selected)
    assert next(r for r in receipt['candidates'] if r['deployment'] == 'claude-cli')['reason'] == 'AUDITOR_NOT_INDEPENDENT'
    assert native_catalog.select('claude-fable-5-1-cli')['model'] == 'fable-5.1'


def test_local_only_admits_the_local_model_to_every_role_and_no_other_mode_changes(installed):
    """--local_only (the owner's choice, 2026-11-13): the local model plans, implements and audits; its audit of a
    Qwen implementer is admitted only where a user can confirm, labelled. Every other mode keeps today's roles."""
    from relay import execution_scope as scope
    producer = native_catalog.select('qwen38-gsq')
    def local_row(role, *author):
        _, receipt = native_routes.ranked(role, *author)
        return next(r for r in receipt['candidates'] if r['deployment'] == 'local-qwen')
    for mode in (scope.Scope(), scope.Scope(prefer='local'), scope.Scope(prefer='api')):
        with scope.activated(mode), scope.confirmable_audit(True):
            assert local_row('implementer')['eligible']
            assert local_row('planner')['reason'] == local_row('auditor', producer)['reason'] == 'ROLE_NOT_CONFIGURED'
            claude = native_catalog.select('claude-cli')
            assert next(r for r in native_routes.ranked('auditor', claude)[1]['candidates']
                        if r['deployment'] == 'claude-cli')['reason'] == 'AUDITOR_NOT_INDEPENDENT'
    with scope.activated(scope.Scope(only='local')):
        planners, _ = native_routes.ranked('planner')
        assert [d['id'] for d in planners] == ['fleet-qwen38-gsq'] and local_row('implementer')['eligible']
        assert local_row('auditor', producer)['reason'] == 'AUDITOR_NOT_INDEPENDENT'  # nobody to confirm
        with scope.confirmable_audit(True):
            auditors, _ = native_routes.ranked('auditor', producer)
            assert [d['id'] for d in auditors] == ['fleet-qwen38-gsq']
            assert local_row('auditor', producer)['reason'] == 'SAME_FAMILY_LOCAL_ONLY_NOT_INDEPENDENT'
    roster = round_table_roster.load_roster()
    policy = round_table_roster.load_role_deployments(roster=roster)
    policy['deployments'][0]['local_only_roles'] = ['chair']
    with pytest.raises(ValueError, match='local_only_roles must list roles'):
        round_table_roster.validate_role_deployments(policy, roster)


def test_catalog_only_policy_is_portable_and_rejects_path_traversal():
    roster = round_table_roster.load_roster()
    policy = round_table_roster.load_role_deployments(roster=roster)
    row = next(r for r in policy['deployments'] if r['id'] == 'claude-sonnet-5-5-cli')
    assert 'native_profile' not in row and row['effort'] is None
    row['native_catalog'] = '../profile'
    with pytest.raises(ValueError, match='ROLE_CATALOG_NAME_INVALID'):
        round_table_roster.validate_role_deployments(policy, roster)


@pytest.mark.parametrize('control_roster', [True, False])
def test_haiku_candidate_uses_low_profile_without_implementer_or_governance_admission(installed, monkeypatch, control_roster):
    if not control_roster:
        from relay import relay_mesh
        monkeypatch.setattr(relay_mesh, 'RELAY_DIR', installed/'managed-control'/'relay')
    value = profile(model='claude-haiku-5-5', effort='low')
    value['id'] = 'claude-haiku-5-5-cli'
    (installed/'claude-haiku-5-5-cli.json').write_text(json.dumps(value))
    explorers, receipt = native_routes.ranked('explorer')
    haiku = next(d for d in explorers if d['id'] == value['id'])
    assert haiku['model'] == 'claude-haiku-5.5'
    assert json.loads(haiku['native_profile']['parameters_json']) == {'effort': 'low'}
    assert next(r for r in receipt['candidates'] if r['deployment'] == value['id'])['eligible']
    implementers, receipt = native_routes.ranked('implementer')
    assert value['id'] not in [d['id'] for d in implementers]
    assert next(r for r in receipt['candidates'] if r['deployment'] == value['id'])['reason'] == 'ROLE_NOT_CONFIGURED'
    roster = round_table_roster.load_roster()
    assert 'claude-haiku-5.5' in roster['models']
    assert 'claude-haiku-5.5' not in json.dumps({k: roster[k] for k in ('chairs', 'mediators', 'scribes')})


def test_existing_control_roster_still_owns_model_aliases(installed, monkeypatch):
    from relay import relay_mesh
    control = installed/'managed-control'/'relay'
    control.mkdir(parents=True)
    roster = round_table_roster.load_roster()
    roster['models']['operator-haiku'] = roster['models'].pop('claude-haiku-5.5')
    (control/'round_table_roster.json').write_text(json.dumps(roster))
    monkeypatch.setattr(relay_mesh, 'RELAY_DIR', control)
    value = profile(model='claude-haiku-5-5', effort='low')
    value['id'] = 'claude-haiku-5-5-cli'
    path = installed/'claude-haiku-5-5-cli.json'
    path.write_text(json.dumps(value))
    before = path.read_bytes()
    assert native_catalog.select(value['id'])['model'] == 'operator-haiku'
    assert path.read_bytes() == before


def test_argon_frontier_roles_preserve_google_family_independence(installed):
    from relay.cli_discovery import TEMPLATES
    from relay import model_choices, work_audit
    templates = json.loads(TEMPLATES.read_text(encoding='utf-8'))
    for name in ('agy-cli', 'agy-gemini-4-argon-cli'):
        value = templates[name]
        value['native_cli']['executable'] = __file__
        (installed/(name+'.json')).write_text(json.dumps(value))
    name = 'agy-gemini-4-argon-cli'
    planners, _ = native_routes.ranked('planner')
    assert name in [d['id'] for d in planners]
    auditors, receipt = native_routes.ranked('auditor', native_catalog.select('claude-cli'))
    assert name in [d['id'] for d in auditors] and receipt['generation_calls'] == 0
    argon = native_catalog.select(name)
    facts = route_facts.collect(argon, live=False, evidence={})
    assert facts['quality']['status'] == facts['quota']['status'] == 'UNKNOWN'
    auditors, receipt = native_routes.ranked('auditor', native_catalog.select('agy-cli'))
    assert name not in [d['id'] for d in auditors]
    assert next(r for r in receipt['candidates'] if r['deployment'] == name)['reason'] == 'AUDITOR_NOT_INDEPENDENT'
    choices = model_choices.inventory()
    for effort in ('low', 'medium', 'high'):
        value = model_choices.realization(choices, {
            'key': 'gemini-4-argon', 'connection': name, 'reasoning': effort})
        identity = work_audit.identity(value, 'implementer')
        assert identity.model_id == 'gemini-4-argon-'+effort
        assert identity.bias_domain == 'google/gemini'
