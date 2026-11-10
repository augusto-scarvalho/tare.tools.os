"""Real role admission and exact lineage, with offline credentials/host fixtures."""
import copy
import base64
import json

import pytest

from relay import native_catalog, native_routes, round_table_roster, turn_engine, work_audit
from test_model_choices import profile


@pytest.fixture
def pool(tmp_path, monkeypatch):
    monkeypatch.setenv('TARE_PROFILE_DIR', str(tmp_path/'profiles'))
    monkeypatch.delenv('TARE_ROLE_DEPLOYMENTS', raising=False)
    monkeypatch.setattr(native_routes, 'credential_available', lambda _: True)
    return tmp_path/'profiles'


SUBSCRIPTION_REVIEWER = 'opencode-sub-gemini-4-argon'


def install(pool, name, endpoint, model):
    value = profile(endpoint, model, 'high')
    value['id'] = name
    if name.startswith('codex-native'):
        value.pop('native_cli')
        value['native_streaming'] = True
        value['native_codex_auth'] = {'path':str(pool.parent/'login.json'),'account_id':'fixture'}
        claims=base64.urlsafe_b64encode(b'{"exp":10000000000}').decode().rstrip('=')
        (pool.parent/'login.json').write_text(json.dumps({'auth_mode':'chatgpt',
            'tokens':{'access_token':'fixture.'+claims+'.signature','account_id':'fixture'}}),encoding='utf-8')
        value['native_profile'].update(protocol='openai-codex-responses',
            endpoint='https://chatgpt.com/backend-api/codex', parameters_json='{"reasoning":{"effort":"high"}}')
    pool.mkdir(exist_ok=True)
    (pool/(name+'.json')).write_text(json.dumps(value), encoding='utf-8')


@pytest.mark.parametrize('planner,expected', [
    ('claude-api-fable-5-1','openai-api'), ('claude-api-opus-5-5','openai-api'),
    ('openai-api','claude-api-opus-5-5'), ('openai-api-gpt-6-astra','claude-api-opus-5-5'),
    ('gemini-api','openai-api')])
@pytest.mark.parametrize('role', ['auditor','plan-auditor'])
def test_automatic_frontier_pair_follows_actual_model_vendor_and_cost_preference(pool, planner, expected, role):
    producer = native_catalog.select(planner)
    candidates, receipt = native_routes.reviewers(producer, role)
    # The subscription Gemini 4 Argon reviews before any route paid per token, except work of its own vendor;
    # `expected` is the first of the paid ones.
    names = [d['id'] for d in candidates]
    assert (names[0] == SUBSCRIPTION_REVIEWER) == (planner != 'gemini-api')
    assert [n for n in names if n != SUBSCRIPTION_REVIEWER][0] == expected
    assert receipt['generation_calls'] == 0
    author = work_audit.identity(producer, 'implementer')
    critic = work_audit.identity(candidates[0], 'auditor')
    assert critic.vendor != author.vendor and critic.bias_domain != author.bias_domain
    from relay.execution_scope import is_pro
    assert not any(is_pro(d) for d in candidates)


def test_codex_subscription_prefers_sol_and_native_cache_route_before_astra_or_api(pool):
    install(pool,'codex-cli','cli://codex','gpt-6-astra')
    install(pool,'codex-cli-gpt-6-1-sol','cli://codex','gpt-6.1-sol')
    install(pool,'codex-native-gpt-6-1-sol','cli://codex','gpt-6.1-sol')
    selected, receipt = native_routes.reviewers(native_catalog.select('claude-api-fable-5-1'))
    assert selected[0]['id'] == 'codex-native-gpt-6-1-sol', [r for r in receipt['candidates'] if '6-1-sol' in r['deployment']]
    names = [d['id'] for d in selected]
    assert names.index('codex-cli-gpt-6-1-sol') < names.index('codex-cli') < names.index('openai-api')


def test_claude_subscription_prefers_opus_before_fable_and_metered_api(pool):
    install(pool,'claude-fable-5-1-cli','cli://claude','claude-fable-5-1')
    install(pool,'claude-opus-5-5-cli','cli://claude','claude-opus-5-5')
    selected, _ = native_routes.reviewers(native_catalog.select('openai-api-gpt-6-astra'))
    assert [d['id'] for d in selected[:2]] == ['claude-opus-5-5-cli','claude-fable-5-1-cli']


def test_existing_generic_claude_opus_profile_is_a_frontier_backup(pool):
    install(pool,'claude-cli','cli://claude','claude-opus-5-5')
    selected, _ = native_routes.reviewers(native_catalog.select('openai-api'))
    assert selected[0]['id']=='claude-cli'


def test_argon_is_the_google_frontier_not_flash(pool, monkeypatch):
    install(pool,'agy-gemini-4-argon-cli','cli://agy','gemini-4-argon-high')
    monkeypatch.setattr(native_routes,'credential_available',lambda _:False)
    selected, _ = native_routes.reviewers(native_catalog.select('openai-api'))
    assert [d['id'] for d in selected] == ['agy-gemini-4-argon-cli']


def test_missing_codex_login_skips_direct_route_and_keeps_cli_backup(pool):
    install(pool,'codex-native-gpt-6-1-sol','cli://codex','gpt-6.1-sol')
    install(pool,'codex-cli-gpt-6-1-sol','cli://codex','gpt-6.1-sol')
    (pool.parent/'login.json').unlink()
    selected, receipt = native_routes.reviewers(native_catalog.select('claude-api-fable-5-1'))
    assert selected[0]['id'] == 'codex-cli-gpt-6-1-sol'
    assert next(r for r in receipt['candidates'] if r['deployment']=='codex-native-gpt-6-1-sol')['reason']=='LOGIN_UNAVAILABLE'
    value,why=turn_engine.available('codex-native-gpt-6-1-sol')
    assert value is None and why.diagnostic['code']=='LOGIN_UNAVAILABLE'


def test_known_codex_quota_cooldown_excludes_native_and_cli_without_repeating_a_call(pool):
    install(pool,'codex-cli-gpt-6-1-sol','cli://codex','gpt-6.1-sol')
    install(pool,'codex-native-gpt-6-1-sol','cli://codex','gpt-6.1-sol')
    turn_engine.note_usage_limit(native_catalog.select('codex-cli-gpt-6-1-sol'))
    selected, receipt = native_routes.reviewers(native_catalog.select('claude-api-fable-5-1'))
    assert not any(d['id'].startswith('codex-') for d in selected)
    # Another subscription first, then the separate enabled API balance.
    assert [d['id'] for d in selected[:2]] == [SUBSCRIPTION_REVIEWER, 'openai-api']
    assert {r['reason'] for r in receipt['candidates'] if r['deployment'].startswith('codex-')
            and '6-1-sol' in r['deployment']} == {'QUOTA_EXHAUSTED'}


def test_no_eligible_frontier_is_reported_without_promoting_a_cheap_helper(pool, monkeypatch):
    monkeypatch.setattr(native_routes,'credential_available',lambda _:False)
    selected, receipt = native_routes.reviewers(native_catalog.select('openai-api'))
    assert selected == [] and receipt['generation_calls'] == 0
    assert next(r for r in receipt['candidates'] if r['deployment']=='local-qwen')['reason']=='ROLE_NOT_CONFIGURED'


@pytest.mark.parametrize('name,vendor', [('claude-api','Anthropic'),
    ('claude-api-sonnet-5-5','Anthropic'),('claude-api-opus-5-5','Anthropic'),
    ('claude-api-fable-5-1','Anthropic'),('openai-api','OpenAI'),('gemini-api','Google')])
def test_api_authorship_uses_registered_vendor_without_rewriting_the_wire_profile(name,vendor):
    before=native_catalog.bundled(name)
    resolved=native_catalog.select(name)
    assert work_audit.identity(resolved,'implementer').vendor==vendor
    assert before['native_profile']==resolved['native_profile']


def test_unknown_author_never_gets_an_implicitly_independent_review(pool,monkeypatch):
    monkeypatch.setattr(turn_engine,'available',lambda name:(native_catalog.select(name),None))
    assert turn_engine.pick_auditors({'id':'unknown','model':'unregistered'},['openai-api'])==[]


def test_explicit_api_file_resolves_lineage_but_never_repairs_a_conflicting_claim(pool):
    path=pool.parent/'custom.json'
    raw=native_catalog.bundled('claude-api-opus-5-5')
    path.write_text(json.dumps(raw),encoding='utf-8')
    resolved=native_catalog.select(path=path)
    assert work_audit.identity(resolved,'implementer').vendor=='Anthropic'
    assert json.loads(path.read_text())==raw
    raw['model']='gpt-6.1-sol';path.write_text(json.dumps(raw),encoding='utf-8')
    with pytest.raises(ValueError,match='IDENTITY_RUNTIME_ALIAS_MISMATCH'):
        work_audit.identity(native_catalog.select(path=path),'implementer')


def test_manual_list_and_legacy_owner_choice_remain_explicit(pool, monkeypatch):
    monkeypatch.setattr(turn_engine,'available',lambda name, **kw:(native_catalog.select(name),None))
    turn_engine.policy_path().parent.mkdir(parents=True,exist_ok=True)
    turn_engine.policy_path().write_text(json.dumps({'auditor':['bedrock-opus']}),encoding='utf-8')
    assert turn_engine.policy()['auditor_selection'] == 'configured'
    selected, receipt = native_routes.reviewers(native_catalog.select('openai-api'))
    assert [d['id'] for d in selected] == ['bedrock-opus']
    assert receipt['reason'] == 'OPERATOR_AUDITOR_ROUTES_CROSS_VENDOR_FIRST'


def test_actual_quota_failure_skips_same_account_alias_and_uses_other_frontier(pool, monkeypatch):
    install(pool,'codex-native-gpt-6-1-sol','cli://codex','gpt-6.1-sol')
    install(pool,'codex-cli-gpt-6-1-sol','cli://codex','gpt-6.1-sol')
    install(pool,'agy-gemini-4-argon-cli','cli://agy','gemini-4-argon-high')
    calls=[]
    def run(config,deployment,custody,instruction,**kwargs):
        calls.append(deployment['id'])
        if len(calls)==1:return {'status':'BLOCKED','failure_code':'CLI_USAGE_LIMIT'}
        return {'status':'PROPOSED','artifact_file':'fixture.json'}
    monkeypatch.setattr('relay.native_agent.run_session',run)
    monkeypatch.setattr('relay.native_artifacts.read_input',lambda path:{'artifact':{'payload':{
        'verdict':'ACCEPT','summary':'Fixture review','findings':[],'remaining':[]}}})
    # Pick these three real routes in this order to isolate the dispatch behavior.
    monkeypatch.setattr(turn_engine,'pick_auditors',lambda producer,names:[
        (name,native_catalog.select(name)) for name in (
            'codex-native-gpt-6-1-sol','codex-cli-gpt-6-1-sol','agy-gemini-4-argon-cli')])
    result=turn_engine.run_audit('Change a.py',[{'path':'a.py','diff':'+x\n'}],pool.parent,
        native_catalog.select('claude-api-fable-5-1'),lambda text:None)
    assert result['verdict']=='ACCEPT'
    assert calls == ['codex-native-gpt-6-1-sol','agy-gemini-4-argon-cli']


@pytest.mark.parametrize('mode',['configured','cross_vendor_frontier'])
def test_saving_planner_or_routes_preserves_the_review_mode(pool, mode):
    from relay.planning_tools import configure_model
    turn_engine.save_policy({'auditor':[['openai-api']]},auditor_selection=mode)
    path=pool.parent/'planner.json'
    selected = native_catalog.select('claude-api-fable-5-1')
    params = json.loads(selected['native_profile']['parameters_json'])
    params['output_config'] = {'effort': 'high'}
    selected['native_profile']['parameters_json'] = json.dumps(params)
    path.write_text(json.dumps(selected),encoding='utf-8')
    configure_model(path)
    assert turn_engine.policy()['auditor_selection'] == mode
    assert turn_engine.policy()['planner'][0].startswith('planner-')


def test_review_priority_is_bounded_operator_policy_not_a_quality_score():
    roster=round_table_roster.load_roster()
    table=round_table_roster.load_role_deployments(roster=roster)
    for invalid in (-1,True,1.5,1001):
        bad=copy.deepcopy(table);bad['deployments'][0]['review_priority']=invalid
        with pytest.raises(ValueError,match='REVIEW_PRIORITY_INVALID'):
            round_table_roster.validate_role_deployments(bad,roster)
