"""A proof recorded on this machine (tare setup --prove) admits a pin-only route of a known line automatically: its
minimal call answered and its role check, one short implementer session, edited the file and delivered."""
import hashlib
import json
from pathlib import Path

import pytest

from relay import environment, native_agent, native_catalog, native_generation, native_routes
from test_console_route_matrix import journey
from test_cross_vendor_auditors import install, pool  # noqa: F401
from test_cross_vendor_auditors import (
    test_new_models_have_an_author_take_a_pin_and_wait_for_qualification_before_any_automatic_role as wait_for_proof)
from test_native_agent import Client, deployment as fixture

OK = {'at': '2026-11-14T00:00:00+00:00', 'ok': True, 'cost': 'fixture'}
CARBON = 'gemini-api-4-carbon'
READ = ('read', {'repository': 'proof', 'path': 'value.txt'})
EDIT = ('edit', {'repository': 'proof', 'path': 'value.txt', 'sha256': hashlib.sha256(b'41\n').hexdigest(),
                 'search': '41', 'replace': '42'})
DELIVER = ('tare_deliver', {'summary': 'DELIVERY-TEXT changed the value.'})
RUN_SESSION = native_agent.run_session  # the real one: scripted() replaces it per test


def ranked(role):
    producer = native_catalog.select('qwen38-gsq') if 'auditor' in role else None
    chosen, selection = native_routes.ranked(role, producer)
    return [d['id'] for d in chosen], {c['deployment']: c['reason'] for c in selection['candidates']}


def scripted(monkeypatch, client):
    """The role check's real session with a scripted model (no real call): its config, custody, limits and options
    pass to run_session unchanged, only the model is `client` on a fixture deployment. Returns what each got."""
    seen = []

    def run(config, deployment, custody, instruction, **options):
        seen.append({'config': config, 'deployment': deployment['id'], 'custody': Path(custody), **options})
        return RUN_SESSION(config, fixture(), custody, instruction, client=client, local_counter=client, **options)
    monkeypatch.setattr(native_agent, 'run_session', run)
    return seen


def test_a_proof_admits_gemini_carbon_only_when_it_answers_and_does_the_task(pool, monkeypatch, capsys):
    monkeypatch.setenv('GEMINI_API_KEY', 'fixture')  # present, never used: generation is a stand-in
    monkeypatch.setattr(environment, 'cost_line', lambda deployment: 'fixture cost')
    answer = [(False, '', {'failure_class': 'HTTP_400'})]
    monkeypatch.setattr(native_generation, 'generate_text', lambda *args, **kwargs: answer[0])
    roles = next(r for r in native_routes.configured()['deployments'] if r['id'] == CARBON)['roles']

    def state():
        return {role: (CARBON in ranked(role)[0], ranked(role)[1][CARBON],
                       {r['route']: r['now'] for r in environment.auto_rows(role)}[CARBON]) for role in roles}
    pin_only = {role: (False, 'NOT_QUALIFIED_PIN_ONLY', 'NOT_QUALIFIED_PIN_ONLY') for role in roles}
    assert state() == pin_only
    lazy = scripted(monkeypatch, Client(steps=[READ, DELIVER]))
    record = environment.prove(CARBON, lambda line: True)
    assert record['ok'] is False and 'role_check' not in record and lazy == []  # no answer: no task either
    assert state() == pin_only  # a failed proof admits nothing

    answer[0] = (True, 'REPLY-TEXT', {})
    assert environment.main(['--prove', CARBON, '--yes']) == 1  # the session delivers without editing
    assert (f'{CARBON}: not proven: answered, but failed the role check: the session delivered but did not change '
            'value.txt to 42; a pin-only route stays pin only') in capsys.readouterr().out
    record = environment.read()['proofs'][CARBON]
    assert record['ok'] is False and record['role_check'] == {
        'ok': False, 'reason': 'the session delivered but did not change value.txt to 42'}
    assert state() == pin_only

    done = scripted(monkeypatch, Client(steps=[READ, EDIT, DELIVER]))
    record = environment.prove(CARBON, lambda line: True)
    assert record['ok'] is True and record['role_check']['ok'] is True and 'reason' not in record, record
    assert all(chosen and reason != 'NOT_QUALIFIED_PIN_ONLY' and now == 'usable'
               for chosen, reason, now in state().values()), state()
    run = done[0]
    assert run['deployment'] == CARBON and run['delegated_leaf'] is True
    assert run['limits'] == {'max_model_calls': 6, 'max_seconds': 300}
    assert run['config']['writable'] == {'proof': ['value.txt']} and run['config']['capabilities'] == ['read', 'edit']
    for run in (lazy[0], done[0]):  # project and custody shared one temporary folder, gone afterwards
        assert Path(run['config']['repositories']['proof']).parent == run['custody'].parent
        assert run['custody'].parent.name.startswith('tare-proof-') and not run['custody'].parent.exists()
    kept = json.dumps(environment.read()['proofs'])
    assert 'REPLY-TEXT' not in kept and 'DELIVERY-TEXT' not in kept  # never the reply or the session's text


def test_a_model_that_fails_in_the_session_did_not_finish(pool, monkeypatch):
    from tare_tools_agent_runtime import ModelFailure

    class Failing(Client):
        def post(self, path, body, control):
            if path == '/chat/completions':
                raise ModelFailure('HTTP_400')
            return super().post(path, body, control)
    monkeypatch.setenv('GEMINI_API_KEY', 'fixture')
    monkeypatch.setattr(environment, 'cost_line', lambda deployment: 'fixture cost')
    monkeypatch.setattr(native_generation, 'generate_text', lambda *args, **kwargs: (True, 'OK', {}))
    scripted(monkeypatch, Failing())
    record = environment.prove(CARBON, lambda line: True)
    assert record['ok'] is False and record['role_check']['ok'] is False
    assert record['role_check']['reason'] == 'the session did not finish: BLOCKED MODEL_FAILURE HTTP_400'
    assert environment.read()['proofs'][CARBON] == record


def test_a_proof_recorded_before_the_role_check_still_admits(pool, monkeypatch):
    monkeypatch.setenv('GEMINI_API_KEY', 'fixture')
    environment.save({'proofs': {CARBON: OK}})  # no role_check: nothing proves again by itself
    role = next(r for r in native_routes.configured()['deployments'] if r['id'] == CARBON)['roles'][0]
    assert CARBON in ranked(role)[0]


@pytest.mark.parametrize('name', ['opencode-sub-kimi-k3'])
def test_a_route_with_no_line_stays_pin_only_with_a_successful_proof(pool, monkeypatch, name):
    # A reseller's Kimi K3: no other catalogue route serves it automatically (Gemini 4 Argon on OpenCode mirrors
    # Google's own route, so a proof admits it: test_mirror_admission.py).
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
        assert 'NOT_QUALIFIED_PIN_ONLY (press p to prove it once)' in status()
    journey(tmp_path, steps)
