"""Local models in the role matrix: every local model in use is a row of each ranked role, and a tick on one grants it
the role (owner, 2026-11-17: the user marks local models; tare never decides alone that one may plan or review).
Offline: a saved gateway record (never asked), the generic profiles tare setup writes for it, no model call."""
import json

import pytest

from relay import environment, execution_scope, landing_policy, model_signals, native_catalog, native_routes
from relay import planner_policy, work_audit
from test_console_route_matrix import journey, saved, stored
from test_cross_vendor_auditors import install, pool  # noqa: F401

GATEWAY = 'http://127.0.0.1:8080'
GEMMA = environment.generic_name(GATEWAY, 'gemma-4')
RANKED = ('implementer', 'explorer', 'planner', 'plan-auditor', 'auditor', 'coordinator')


def gateway(*models, enabled=None, more=()):
    """Save local gateways (a first one at GATEWAY publishing `models`) and write their generic profiles, as tare
    setup does after asking them."""
    value = environment.read()
    record = lambda origin, published, used: {'origin': origin, 'kind': 'openai', 'models': list(published),
                                              'enabled': list(published if used is None else used),
                                              'observed': '2026-11-17T00:00:00+00:00'}
    value['local_models'] = {'use': True, 'gateways': [record(GATEWAY, models, enabled),
                                                       *(record(o, m, None) for o, m in more)]}
    environment.apply(value)


def profile(**sides):
    environment.update(lambda value: {**value, 'routing_profile': sides})


def row(name):
    return next(r for r in native_routes.configured()['deployments'] if r['id'] == name)


def why(name, role, producer=None):
    author = work_audit.identity(native_catalog.select(producer), 'implementer') if producer else None
    return native_routes.admission(row(name), role, author)[2]


def routes(role):
    grants = set()
    return {name: now for name, _, now in environment.auto_routes(role, grants)}, grants


def test_every_local_model_in_use_is_a_row_of_each_ranked_role_unticked_and_one_off_has_none(pool):
    gateway('qwen38-gsq', 'gemma-4', 'swift27b', 'other-model', enabled=['qwen38-gsq', 'gemma-4'])
    assert row(GEMMA)['roles'] == [] and row(GEMMA)['model'] == 'gemma-4'
    for role in RANKED:
        rows, grants = routes(role)
        assert rows[GEMMA] == 'usable' and GEMMA in grants  # checked as if granted; its tick is off until it is
        assert ('local-qwen' in grants) == (role in ('planner', 'plan-auditor', 'auditor'))  # its row keeps its roles
        # Off at the gateway: no generic profile, no row; a shipped profile whose model is off has none either.
        assert not {n for n in rows if 'other-model' in n or n == 'swift27b'}
        assert why(GEMMA, role) == 'ROLE_NOT_CONFIGURED'
    shown = {r['route']: r['profile'] for r in environment.auto_rows('planner')}
    assert shown[GEMMA] == 'off (not granted)' and shown['local-qwen'] == 'off (not granted)'
    # No gateway saved: the table is the shipped one, every local row as it was.
    environment.update(lambda value: {k: v for k, v in value.items() if k != 'local_models'})
    assert GEMMA not in {r['id'] for r in native_routes.configured()['deployments']}


@pytest.mark.parametrize('role', ['planner', 'plan-auditor', 'auditor', 'implementer', 'coordinator'])
def test_an_on_naming_a_local_route_grants_the_role_outside_local_only_and_its_removal_takes_it_back(pool, role):
    gateway('gemma-4')
    producer = 'claude-api-opus-5-5' if role in ('plan-auditor', 'auditor') else None
    assert why(GEMMA, role, producer) == 'ROLE_NOT_CONFIGURED'
    assert environment.main(['--auto', f'{role}:{GEMMA}=on']) == 0
    assert why(GEMMA, role, producer) is None and not execution_scope.current().only
    if not producer:
        assert GEMMA in [d['id'] for d in native_routes.ranked(role)[0]]
    assert why(GEMMA, 'explorer') == 'ROLE_NOT_CONFIGURED'  # one role, the one granted
    assert environment.main(['--auto', f'{role}:{GEMMA}=default']) == 0
    assert why(GEMMA, role, producer) == 'ROLE_NOT_CONFIGURED'


def test_an_off_wins_and_a_broad_on_or_an_on_for_a_hosted_or_reseller_route_grants_nothing(pool):
    gateway('gemma-4')
    install(pool, 'claude-haiku-5-5-cli', 'cli://claude', 'claude-haiku-5-5')
    profile(on={'planner': [GEMMA]}, off={'planner': [GEMMA]})
    assert why(GEMMA, 'planner') == 'ROLE_NOT_CONFIGURED'  # the same entry both ways: off wins the tie
    profile(on={'planner': ['*@local']}, off={'planner': ['*@*']})
    assert why(GEMMA, 'planner') == 'ROLE_NOT_CONFIGURED'  # a broader "on" only undoes a broader "off"
    # claude-haiku-5-5-cli is an explorer only; its id is read as its line on Claude Code. Kimi on OpenRouter has no
    # implementer row and no line, so an "on" names it by its id.
    profile(on={'planner': ['claude/haiku@claude-code', 'claude-haiku-5-5-cli'], 'implementer': ['openrouter-kimi-k3-1-pro']})
    assert why('claude-haiku-5-5-cli', 'planner') == 'ROLE_NOT_CONFIGURED'
    assert why('openrouter-kimi-k3-1-pro', 'implementer') == 'ROLE_NOT_CONFIGURED'
    assert not environment.granted('implementer', native_catalog.select('openrouter-kimi-k3-1-pro'))


def test_a_local_route_with_no_grant_is_as_before_and_a_grant_is_not_held_back_by_pin_only(pool, tmp_path, monkeypatch):
    gateway('qwen38-gsq', 'gemma-4')
    qwen = row('local-qwen')
    assert native_routes.roles(qwen, execution_scope.Scope()) == ['implementer', 'explorer']
    assert native_routes.roles(qwen, execution_scope.Scope(only='local')) == ['implementer', 'explorer', 'planner', 'auditor']
    assert why('local-qwen', 'planner') == 'ROLE_NOT_CONFIGURED' and why('local-qwen', 'implementer') is None
    # A local row that is pin only stays so after a proof, unless the user grants it a role.
    table = json.loads(native_routes.Path(native_routes.__file__).with_name('role_deployments.json').read_text('utf-8'))
    next(r for r in table['deployments'] if r['id'] == 'local-qwen')['qualified'] = False
    (tmp_path/'roles.json').write_text(json.dumps(table), encoding='utf-8')
    monkeypatch.setenv('TARE_ROLE_DEPLOYMENTS', str(tmp_path/'roles.json'))
    environment.update(lambda value: {**value, 'proofs': {'local-qwen': {'ok': True}}})
    assert why('local-qwen', 'implementer') == 'NOT_QUALIFIED_PIN_ONLY'
    assert environment.main(['--auto', 'plan-auditor:local-qwen=on']) == 0
    assert why('local-qwen', 'plan-auditor', 'claude-api-opus-5-5') is None
    assert why('local-qwen', 'implementer') == 'NOT_QUALIFIED_PIN_ONLY'  # a grant is for the role granted


def test_a_granted_local_planner_runs_at_its_own_default_unless_it_declares_levels(pool):
    gateway('gemma-4', 'lowly')
    lowly = environment.generic_name(GATEWAY, 'lowly')
    path = pool/(lowly + '.json')
    value = json.loads(path.read_text(encoding='utf-8'))
    path.write_text(json.dumps({**value, 'native_reasoning_levels': ['low', 'medium']}), encoding='utf-8')
    with pytest.raises(ValueError, match='at least high'):  # not granted: the rule as before
        planner_policy.effective(native_catalog.select(GEMMA))
    for name in (GEMMA, lowly):
        assert environment.main(['--auto', f'planner:{name}=on']) == 0
    gemma = native_catalog.select(GEMMA)
    assert planner_policy.effective(gemma) == gemma and planner_policy.unlevelled(gemma)
    with pytest.raises(ValueError, match='at least high'):
        planner_policy.effective(native_catalog.select(lowly))
    qwen = native_catalog.select('qwen38-gsq')  # the shipped Qwen declares its levels: it is raised to one at high or above
    assert planner_policy.effective(qwen)['native_profile']['parameters_json'] != qwen['native_profile']['parameters_json']


def test_generic_local_models_have_an_identity_of_their_own(pool):
    gateway('gemma-4', 'llama-5')
    gemma, llama = native_catalog.select(GEMMA), native_catalog.select(environment.generic_name(GATEWAY, 'llama-5'))
    me = work_audit.identity(gemma, 'implementer')
    assert (me.vendor, me.bias_domain, me.payload['line']) == ('Local', 'gemma-4', 'local')
    landing_policy.verify_identity_against_roster(me, native_catalog.roster_path())  # re-derived on a later read
    assert work_audit.independence(gemma, llama) == 'SAME_VENDOR_NOT_INDEPENDENT'
    assert work_audit.independence(gemma, gemma) == 'AUDITOR_NOT_INDEPENDENT'
    assert work_audit.independence(gemma, native_catalog.select('claude-api-opus-5-5')) == 'INDEPENDENT'
    assert work_audit.identity(native_catalog.select('qwen38-gsq'), 'auditor').vendor == 'Qwen'  # the roster's record
    hosted = {**gemma, 'native_profile': {**gemma['native_profile'], 'endpoint': 'https://models.example.com/v1'}}
    with pytest.raises(ValueError):  # the same unknown model elsewhere than this machine has no identity
        work_audit.identity(hosted, 'auditor')


def test_prefer_local_refuses_a_local_review_route_unless_the_user_granted_it_that_role(pool, monkeypatch):
    gateway('gemma-4')
    gemma = native_catalog.select(GEMMA)
    scope = execution_scope.Scope(prefer='local')
    assert scope.allows(gemma, 'auditor') == (False, '--prefer_local requires an external planner and auditor.')
    assert environment.main(['--auto', f'auditor:{GEMMA}=on']) == 0
    assert scope.allows(gemma, 'auditor') == (True, None) and not scope.allows(gemma, 'planner')[0]


def test_a_granted_local_model_with_no_class_goes_after_every_route_with_one_in_the_order_the_matrix_shows(
        pool, monkeypatch):
    gateway('zeta-model', more=[('http://127.0.0.1:8081', ['alpha-model'])])
    zeta, alpha = environment.generic_name(GATEWAY, 'zeta-model'), environment.generic_name('http://127.0.0.1:8081', 'alpha-model')
    names = [r['id'] for r in native_routes.configured()['deployments']]
    assert names.index(zeta) < names.index(alpha)  # the table's order is not the matrix's
    for name in (zeta, alpha):
        assert environment.main(['--auto', f'planner:{name}=on']) == 0
    chosen = [d['id'] for d in native_routes.ranked('planner')[0]]
    assert chosen[-2:] == [alpha, zeta] and len(chosen) > 2  # every route with a class first, then by label
    klass = model_signals.klass  # a board that places zeta: it is ranked by its class as any route
    monkeypatch.setattr(model_signals, 'klass', lambda d: {'class': 'frontier', 'from': 'reading'}
                        if d['native_profile']['model'] == 'zeta-model' else klass(d))
    chosen = [d['id'] for d in native_routes.ranked('planner')[0]]
    assert chosen[-1] == alpha and chosen.index(zeta) < len(chosen) - 1


def test_the_matrix_shows_a_local_model_unticked_a_tick_grants_it_and_saves_only_the_grant(pool, tmp_path):
    gateway('gemma-4')

    async def steps(app, pilot, panel, cell, status):
        assert await cell(GEMMA, 'local', 'enter') == '[x]'
        assert 'granted by you' in status() and 'a tick on a Local model grants it the role' in str(
            panel.query('.matrix-note').first().render())
        await saved(pilot, panel)
        assert stored() == {'on': {'planner': [GEMMA]}}
        assert await cell(GEMMA, 'local') == '[ ]' and 'not granted' in status()
        await saved(pilot, panel)
        assert stored() is None
    journey(tmp_path, steps, role='planner')
