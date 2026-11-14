"""A proof recorded on this machine (tare setup --prove) admits a pin-only route of a known line automatically."""
import pytest

from relay import environment, native_catalog, native_generation, native_routes
from test_console_route_matrix import journey
from test_cross_vendor_auditors import install, pool  # noqa: F401
from test_cross_vendor_auditors import (
    test_new_models_have_an_author_take_a_pin_and_wait_for_qualification_before_any_automatic_role as wait_for_proof)

OK = {'at': '2026-11-14T00:00:00+00:00', 'ok': True, 'cost': 'fixture'}


def ranked(role):
    producer = native_catalog.select('qwen38-gsq') if 'auditor' in role else None
    chosen, selection = native_routes.ranked(role, producer)
    return [d['id'] for d in chosen], {c['deployment']: c['reason'] for c in selection['candidates']}


def test_a_successful_proof_admits_gemini_carbon_on_the_api_and_a_failed_one_nothing(pool, monkeypatch):
    monkeypatch.setenv('GEMINI_API_KEY', 'fixture')  # present, never used: generation is a stand-in
    monkeypatch.setattr(environment, 'cost_line', lambda deployment: 'fixture cost')
    answer = [(False, '', {'failure_class': 'HTTP_400'})]
    monkeypatch.setattr(native_generation, 'generate_text', lambda *args, **kwargs: answer[0])
    name = 'gemini-api-4-carbon'
    roles = next(r for r in native_routes.configured()['deployments'] if r['id'] == name)['roles']

    def state():
        return {role: (name in ranked(role)[0], ranked(role)[1][name],
                       {r['route']: r['now'] for r in environment.auto_rows(role)}[name]) for role in roles}
    pin_only = {role: (False, 'NOT_QUALIFIED_PIN_ONLY', 'NOT_QUALIFIED_PIN_ONLY') for role in roles}
    assert state() == pin_only
    assert environment.prove(name, lambda line: True)['ok'] is False
    assert state() == pin_only  # a failed proof admits nothing
    answer[0] = (True, 'OK', {})
    assert environment.prove(name, lambda line: True)['ok'] is True  # recorded under the name the user typed
    assert all(chosen and reason != 'NOT_QUALIFIED_PIN_ONLY' and now == 'usable'
               for chosen, reason, now in state().values()), state()


@pytest.mark.parametrize('name', ['opencode-sub-kimi-k3', 'opencode-sub-gemini-4-argon'])
def test_a_route_with_no_line_stays_pin_only_with_a_successful_proof(pool, monkeypatch, name):
    # Resellers: Kimi has no vendor route here; Gemini 4 Argon on OpenCode is spelled like a line, not Google's.
    table = native_routes.configured()
    table['deployments'] = [{**r, 'qualified': False} if r['id'] == name else r for r in table['deployments']]
    monkeypatch.setattr(native_routes, 'configured', lambda: table)
    environment.save({'proofs': {name: OK}})
    assert ranked('explorer')[1][name] == 'NOT_QUALIFIED_PIN_ONLY'


def test_a_proof_of_a_models_cli_route_does_not_admit_its_api_route(pool):
    install(pool, 'claude-fable-5-2-cli', 'cli://claude', 'claude-fable-5-2')
    environment.save({'proofs': {'claude-fable-5-2-cli': OK}})
    names, reasons = ranked('planner')
    assert 'claude-fable-5-2-cli' in names
    assert reasons['claude-api-fable-5-2'] == 'NOT_QUALIFIED_PIN_ONLY'


def test_with_no_proofs_key_every_role_ranks_as_the_existing_expectations_say(pool):
    environment.save({'clis': {}})
    wait_for_proof(pool)


def test_the_matrix_says_how_to_lift_a_pin_only_route_of_a_known_line(pool, tmp_path):
    install(pool, 'claude-fable-5-2-cli', 'cli://claude', 'claude-fable-5-2')

    async def steps(app, pilot, panel, cell, status):
        await cell('claude/fable', 'claude-code')
        assert 'NOT_QUALIFIED_PIN_ONLY (prove it once: tare setup --prove claude-fable-5-2-cli)' in status()
    journey(tmp_path, steps)
