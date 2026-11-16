"""A pin-only route that serves, under another catalogue, a model another row already serves automatically (a mirror)
is admitted after one proof on this machine, for the roles both rows have, as that row's reserve (owner, 2026-11-13
and 2026-11-16: GLM 5.3 on Bedrock mirrors GLM 5.3 on OpenRouter)."""
import dataclasses
import json

import pytest

from relay import environment, model_choices, native_catalog, native_routes, turn_engine, work_audit
from relay import model_signals as signals
from relay.vendor_models import same_model
from test_class_anchors import other, twice
from test_console_route_matrix import journey, proved, prover
from test_cross_vendor_auditors import install, pool  # noqa: F401
from test_tiers_by_class import everything  # noqa: F401

OK = {'at': '2026-11-16T00:00:00+00:00', 'ok': True, 'cost': 'fixture'}
MIRROR, ROW, ORIGIN = 'bedrock-glm-5-3', 'bedrock-glm-5-3-explorer', 'openrouter-glm-5-3'
BR, OR = 'https://bedrock-runtime.us-east-1.amazonaws.com', 'https://openrouter.ai/api/v1'
OC = 'https://opencode.ai/inference/go/openai/v1'


def ranked(role, producer=None):
    chosen, selection = native_routes.ranked(role, producer)
    return [d['id'] for d in chosen], {c['deployment']: c['reason'] for c in selection['candidates']}


def table(monkeypatch, change):
    """The shipped table with `change(row)` applied to each row (None drops it)."""
    value = native_routes.configured()
    value['deployments'] = [r for r in map(change, value['deployments']) if r]
    monkeypatch.setattr(native_routes, 'configured', lambda: value)


def edit(name, **fields):
    return lambda r: {**r, **fields} if r['id'] == name else r


def route(model, endpoint, protocol='openai-chat', **extra):
    return {'id': 'fixture', 'model': model, 'native_profile': {'model': model, 'endpoint': endpoint, 'protocol': protocol},
            **extra}


def test_glm_5_3_on_bedrock_is_pin_only_until_a_proof_then_automatic_for_the_roles_both_rows_have(pool, monkeypatch):
    for role in ('implementer', 'coordinator', 'explorer'):
        assert ranked(role)[1][ROW] == 'NOT_QUALIFIED_PIN_ONLY'
    # A pin reaches it before any proof.
    monkeypatch.setenv('AWS_BEARER_TOKEN_BEDROCK', 'fixture')  # present, never used: nothing is called
    choice = {r['deployment']: r for r in model_choices.projection(include_unconfigured=True)}[MIRROR]
    pin = choice['role_arguments']['implementer'][0].split(' ', 1)[1]
    monkeypatch.setenv('TARE_SESSION_SCOPE', json.dumps({'pins': [['implementer', pin]]}))
    chosen, selection = native_routes.ranked('implementer')
    assert [d['id'] for d in chosen] == [MIRROR] and selection['reason'] == 'EXACT_TEMPORARY_PIN'
    monkeypatch.delenv('TARE_SESSION_SCOPE')

    environment.save({'proofs': {MIRROR: OK}})
    for role in ('implementer', 'coordinator', 'explorer'):
        names, reasons = ranked(role)
        assert MIRROR in names and reasons[ROW] == 'CONFIGURED_NATIVE_ROLE'
        assert {r['route']: r['now'] for r in environment.auto_rows(role)}[MIRROR] == 'usable'
    for role in ('planner', 'plan-auditor', 'auditor'):
        assert ranked(role, native_catalog.select('qwen38-gsq'))[1][ROW] == 'ROLE_NOT_CONFIGURED'
    row = next(r for r in native_routes.configured()['deployments'] if r['id'] == ROW)
    assert native_routes.proven(row, {})['id'] == ORIGIN  # any role: what model_signals._newest reads


def test_a_role_only_the_mirror_has_stays_pin_only(pool, monkeypatch):
    table(monkeypatch, edit(ROW, roles=['implementer', 'explorer', 'planner']))  # OpenRouter's GLM 5.3 plans nothing
    environment.save({'proofs': {MIRROR: OK}})
    assert ranked('planner')[1][ROW] == 'NOT_QUALIFIED_PIN_ONLY'
    assert MIRROR in ranked('implementer')[0]


def test_gemini_4_argon_on_opencode_mirrors_googles_own_route_for_every_role_both_have(pool, monkeypatch):
    # The case test_proof_admits.py had as "no line, stays pin only": Google's API serves the same model automatically.
    name = 'opencode-sub-gemini-4-argon'
    table(monkeypatch, edit(name, qualified=False))
    assert ranked('planner')[1][name] == 'NOT_QUALIFIED_PIN_ONLY'
    environment.save({'proofs': {name: OK}})
    for role in ('planner', 'explorer'):
        assert ranked(role)[1][name] == 'CONFIGURED_NATIVE_ROLE'


@pytest.mark.parametrize('record', [{'ok': False, 'reason': 'HTTP_400'}, {'ok': None, 'reason': 'not confirmed'},
                                    {'ok': None, 'reason': environment.STARTED}])
def test_a_failed_declined_or_reserved_proof_admits_nothing(pool, record):
    environment.save({'proofs': {MIRROR: {'at': OK['at'], **record}}})
    for role in ('implementer', 'explorer'):
        assert ranked(role)[1][ROW] == 'NOT_QUALIFIED_PIN_ONLY'


def test_a_mirror_of_a_pin_only_row_admits_nothing_even_when_both_answered(pool, monkeypatch):
    table(monkeypatch, edit(ORIGIN, qualified=False))
    environment.save({'proofs': {MIRROR: OK, ORIGIN: OK}})  # OpenRouter has no line: its proof admits neither
    reasons = ranked('implementer')[1]
    assert reasons[ROW] == reasons[ORIGIN] == 'NOT_QUALIFIED_PIN_ONLY'


def test_a_row_whose_author_differs_admits_nothing(pool, monkeypatch):
    real = work_audit.identity

    def identity(deployment, role):  # one model name, another creator
        found = real(deployment, role)
        if deployment['native_profile']['model'] != 'z-ai/glm-5.3':
            return found
        return dataclasses.replace(found, payload={**found.payload, 'vendor': 'Acme', 'bias_domain': 'acme/glm'})
    monkeypatch.setattr(work_audit, 'identity', identity)
    environment.save({'proofs': {MIRROR: OK}})
    assert ranked('implementer')[1][ROW] == 'NOT_QUALIFIED_PIN_ONLY'


ONE_AUTHOR = [  # (route a, route b, same model): ids only, one author for all (identity stands in)
    (route('us.zai.glm-5.3', BR, 'bedrock-converse'), route('z-ai/glm-5.3', OR), True),
    (route('us.anthropic.claude-opus-4-6-v1:0', BR, 'bedrock-converse'),
     route('claude-opus-4-6', 'https://api.anthropic.com/v1', 'anthropic-messages'), True),
    (route('glm-5.3', OC, native_opencode={'plan': 'sub'}), route('z-ai/glm-5.3', OR), True),
    (route('claude-opus-5-5-high', 'cli://agy', 'vendor-cli'), route('claude-opus-5-5', 'cli://claude', 'vendor-cli'),
     True),
    (route('us.zai.glm-5.3', BR, 'bedrock-converse'), route('z-ai/glm-5.3-flash', OR), False),  # Flash
    (route('us.zai.glm-5.3', BR, 'bedrock-converse'), route('zai.glm-5', BR, 'bedrock-converse'), False),  # GLM 5
    (route('kimi-k3', OC, native_opencode={'plan': 'sub'}), route('moonshotai/kimi-k3.1-pro', OR), False),
    (route('kimi-k3.1', OC, native_opencode={'plan': 'sub'}), route('moonshotai/kimi-k3.1-pro', OR), False),  # -pro
    (route('deepseek-v4.1-flash', OC, native_opencode={'plan': 'sub'}), route('deepseek/deepseek-v4.1-flash-1017', OR),
     False),  # dated
    (route('step-5', OC, native_opencode={'plan': 'sub'}), route('stepfun/step-5-preview', OR), False),  # preview
    (route('us.zai.glm-5.3', BR, 'bedrock-converse'), route('z-ai/glm-5.3:free', OR), False),  # a variant
    (route('glm-5.3', 'http://127.0.0.1:8080/v1'), route('z-ai/glm-5.3', OR), False),  # a local model
    (route('glm-5.3', 'https://example.com/v1'), route('z-ai/glm-5.3', OR), False),  # another endpoint
]


@pytest.mark.parametrize('a,b,same', ONE_AUTHOR)
def test_the_same_model_is_decided_by_construction(monkeypatch, a, b, same):
    found = work_audit.identity(native_catalog.select(ORIGIN), 'auditor')
    monkeypatch.setattr(work_audit, 'identity', lambda deployment, role: found)
    assert same_model(a, b) is same and same_model(b, a) is same


@pytest.mark.parametrize('mirror,change,admitted', [
    (MIRROR, lambda r: None if r['id'] == ORIGIN else r, False),  # left: GLM 5.3 Flash and GLM 5 only
    ('opencode-sub-kimi-k3', edit('openrouter-kimi-k3-1-pro', roles=['explorer']), False),  # Kimi K3.1 Pro only
    ('opencode-sub-deepseek-v4-1-flash', lambda r: None if r['id'] == 'openrouter-deepseek-v4-1-flash' else r,
     False),  # the dated 1017 only
    ('opencode-sub-deepseek-v4-1-flash', lambda r: r, True),  # OpenRouter's plain one serves it
])
def test_a_proof_admits_only_a_mirror_of_the_very_same_model(pool, monkeypatch, mirror, change, admitted):
    table(monkeypatch, lambda r: change(edit(mirror, qualified=False)(r)))
    row = next(r for r in native_routes.configured()['deployments'] if r.get('native_catalog') == mirror)
    environment.save({'proofs': {mirror: OK}})
    assert (ranked('explorer')[1][row['id']] != 'NOT_QUALIFIED_PIN_ONLY') is admitted


def test_auditor_selection_treats_both_glm_routes_as_one_author(pool):
    environment.save({'proofs': {MIRROR: OK}})
    a, b = native_catalog.select(MIRROR), native_catalog.select(ORIGIN)
    assert work_audit.independence(a, b) == work_audit.independence(b, a) == 'AUDITOR_NOT_INDEPENDENT'


def test_tied_up_to_the_allowance_a_mirror_goes_after_the_row_that_admitted_it(pool, monkeypatch):
    # Same tier and kind as OpenRouter's row and before it in the table: only the admission term orders them.
    table(monkeypatch, edit(ROW, cost_tier='metered', kind='primary'))
    environment.save({'proofs': {MIRROR: OK}})
    names = ranked('implementer')[0]
    assert names.index(ORIGIN) < names.index(MIRROR)


def first_floor(role):
    return turn_engine.implementer_tiers(role)[0]


@pytest.mark.parametrize('reading', [False, True])
def test_where_the_mirror_lands_on_the_first_floor_of_the_shipped_table(everything, monkeypatch, reading):
    if reading:  # both routes get the class of GLM 5.3: OpenRouter's by its permaslug, Bedrock's by native_measured_as
        saved = twice(other('z-ai', 'GLM 5.3', 55, permaslug='z-ai/glm-5.3-20261020'))
        monkeypatch.setattr(signals, 'reading', lambda: saved)
    for role in ('implementer', 'coordinator'):
        environment.save({})
        before = first_floor(role)
        environment.save({'proofs': {MIRROR: OK}})
        after = first_floor(role)
        assert MIRROR not in before and [n for n in after if n != MIRROR] == before  # nothing else moves
        at = after.index(MIRROR)
        # One model, one class (or none) on both routes: the allowance decides, and free credit goes before pay per
        # token, so the mirror precedes the row that admitted it.
        assert at < after.index(ORIGIN)
        if reading:
            assert MIRROR in turn_engine.implementer_tiers(role)[1]  # the middling floor, with OpenRouter's
        else:
            assert after[at - 1] == 'bedrock-glm' and after[at + 1] == ORIGIN  # with no class: among the last resorts


def test_the_matrix_offers_to_prove_a_mirror_and_says_it_is_a_reserve(pool, tmp_path, monkeypatch):
    calls = prover(monkeypatch)

    async def steps(app, pilot, panel, cell, status):
        from relay.console_route_matrix import ProveDialog, RouteMatrix
        await cell(MIRROR, 'bedrock', 'p')
        assert f'{MIRROR} on, NOT_QUALIFIED_PIN_ONLY (press p to prove it once)' in status(), status()
        assert [n for n, _ in panel.query_one(RouteMatrix).provable()] == [MIRROR]
        assert isinstance(app.screen, ProveDialog)
        app.screen.query_one('#prove-go').press()
        await proved(pilot, panel, status, f'used automatically where it is ticked on, as a reserve after {ORIGIN}')
        assert calls == [MIRROR]
    journey(tmp_path, steps, role='implementer')
