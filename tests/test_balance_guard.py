"""An API route is left out while its account's known balance is spent (owner, 2026-11-16: the balance is a guard)."""
import time

import pytest

from relay import provider_quota, provider_spend, turn_engine

DAY = 86400
API = {'id': 'api', 'native_credential_env': 'OPENAI_API_KEY',
       'native_profile': {'endpoint': 'https://api.openai.com', 'model': 'gpt-6', 'protocol': 'openai-responses'}}
OPENROUTER = {'id': 'or', 'native_credential_env': 'OPENROUTER_API_KEY',
              'native_profile': {'endpoint': 'https://openrouter.ai/api', 'model': 'x/y', 'protocol': 'openai-chat'}}


@pytest.fixture(autouse=True)
def storage(tmp_path, monkeypatch):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path))
    return tmp_path


def spend(deployment, usd, at):
    assert provider_spend.record(deployment, [{'call_id': str(at), 'cost': {'currency': 'USD', 'known_amount': usd}}], at) == 1


def reading(remaining, at):
    from relay.relay_io import write_json_atomic
    path = provider_quota.cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, {'schema': provider_quota.SCHEMA, 'observed_at': at, 'accounts': [
        {'account': 'openrouter', 'name': 'OpenRouter', 'kind': 'credit', 'status': 'OBSERVED', 'observed_at': at,
         'credit': {'currency': 'USD', 'total': 20, 'remaining': remaining}}]})


def test_no_known_balance_leaves_nothing_out():
    now = time.time()
    assert turn_engine.usage_limited(API, now) is None  # unknown is not zero
    assert turn_engine.usage_limited(OPENROUTER, now) is None  # no reading at all


def test_a_stated_balance_counted_down_to_zero_leaves_the_route_out_until_a_new_one_is_stated():
    now = time.time()
    provider_spend.declare('openai-api', 10, now=now - DAY)
    spend(API, 9.5, now - 3600)
    assert turn_engine.usage_limited(API, now) is None  # something is left
    spend(API, 0.5, now - 60)
    assert turn_engine.usage_limited(API, now) == now + turn_engine.USAGE_LIMIT_DEFAULT_SECONDS
    assert turn_engine.usage_limited(OPENROUTER, now) is None  # another account
    line = provider_quota.render({'observed_at': now, 'accounts': [{'account': 'openai-api', 'name': 'OpenAI API',
        'kind': 'credit', 'status': 'DECLARED', 'observed_at': now, 'credit': provider_spend.remaining('openai-api', now)}]},
        now).splitlines()[1]
    assert line.startswith('  OpenAI API: none left of US$ 10.00') and line.endswith(' · its routes are left out while nothing is left')
    provider_spend.declare('openai-api', 5, now=now)
    assert turn_engine.usage_limited(API, now + 1) is None  # lifted at once


def test_a_spent_credit_that_renews_is_left_out_until_it_renews_and_a_lapsed_one_is_unknown_again():
    now = time.time()
    provider_spend.declare('openai-api', 10, now=now - DAY, expires=now + 2*DAY)
    spend(API, 10, now - 60)
    assert turn_engine.usage_limited(API, now) == pytest.approx(now + 2*DAY)
    assert turn_engine.usage_limited(API, now + 3*DAY) is None  # the credit lapsed: no known balance


def test_openrouters_own_reading_at_zero_counts_only_while_it_is_fresh():
    now = time.time()
    reading(0, now - 60)
    assert turn_engine.usage_limited(OPENROUTER, now) == now + turn_engine.USAGE_LIMIT_DEFAULT_SECONDS
    assert turn_engine.usage_limited(API, now) is None  # OpenRouter's reading says nothing of another account
    reading(0, now - provider_quota.FRESH - 60)
    assert turn_engine.usage_limited(OPENROUTER, now) is None  # an old reading: it may have been topped up since
    reading(3.2, now - 60)
    assert turn_engine.usage_limited(OPENROUTER, now) is None


def refusal(code, **reason):
    return ({'code': code, 'call_id': 'c1'},
            [{'call_id': 'c1', 'transport_calls': [{'provider_error': reason}] if reason else [{}]}])


def test_a_refusal_for_lack_of_credit_is_told_from_a_passing_rate_limit_and_any_other_refusal():
    """Owner, 2026-11-17: what a provider's reply says of spent credit. Tokens are no sign; a refusal is."""
    no_credit = provider_spend.no_credit
    assert no_credit(OPENROUTER, *refusal('HTTP_402'))  # Payment Required, whatever the body says
    assert no_credit(API, *refusal('HTTP_429', type='insufficient_quota'))  # OpenAI
    anthropic = {'id': 'a', 'native_credential_env': 'ANTHROPIC_API_KEY', 'native_profile': {}}
    assert no_credit(anthropic, *refusal('HTTP_402', type='billing_error'))
    assert no_credit(anthropic, *refusal('HTTP_400', type='invalid_request_error',
                                         message='Your credit balance is too low to access the Anthropic API.'))
    assert no_credit(anthropic, {'reason': 'HTTP_402', 'call_id': 'c1'}, [])  # a context summary says reason
    assert not no_credit(API, *refusal('HTTP_429', type='rate_limit_exceeded'))  # a passing rate limit
    assert not no_credit(API, *refusal('HTTP_429'))
    assert not no_credit(anthropic, *refusal('HTTP_400', type='invalid_request_error', message='max_tokens is too large'))
    assert not no_credit(API, *refusal('HTTP_503', type='insufficient_quota'))  # not a refusal of the request
    assert not no_credit({'id': 'cli', 'native_profile': {'endpoint': 'cli://claude'}}, *refusal('HTTP_402'))  # no API account
    assert not no_credit(API, None, [])


def test_a_refused_call_leaves_the_api_account_out_for_a_while_and_says_so(storage):
    now = time.time()
    assert turn_engine.usage_limited(API, now) is None
    until = turn_engine.note_usage_limit(API, now=now)
    assert until == now + turn_engine.USAGE_LIMIT_DEFAULT_SECONDS
    other = {**API, 'id': 'api-2', 'native_profile': {**API['native_profile'], 'model': 'gpt-6-mini'}}
    assert turn_engine.usage_limited(API, now + 1) == until and turn_engine.usage_limited(other, now + 1) == until
    assert turn_engine.usage_limited(OPENROUTER, now + 1) is None  # another account
    assert turn_engine.usage_limited(API, until + 1) is None  # asked again after it
    assert provider_quota.parked('openai-api', now + 1) == until
    from relay.native_display import provider_limit_guidance
    text = provider_limit_guidance({'failure_code': 'API_CREDIT_SPENT', 'usage_limit': {
        'kind': 'account', 'cause': 'no_credit', 'account': 'openai-api', 'until': until}})
    assert 'the openai-api account has no credit left' in text and '/quota' in text
    assert 'API_CREDIT_SPENT' in turn_engine.USAGE_LIMIT_CODES


from test_native_agent import Client, setup, steps  # noqa: E402,F401


def test_a_session_refused_for_lack_of_credit_ends_with_that_code_and_parks_the_account(setup, storage):  # noqa: F811
    from tare_tools_agent_runtime import ModelFailure
    from relay import native_agent, native_catalog
    config, custody, root = setup
    route = native_catalog.select('openrouter-glm-5-3-flash')

    class Refused(Client):
        endpoint = route['native_profile']['endpoint']

        def post_observed(self, path, body, control, **kwargs):
            failure = ModelFailure('HTTP_402')
            failure.transport_observation = {'transport': 'fixture', 'started': True, 'response_received': True,
                                             'provider_error': {'message': 'Insufficient credits'}}
            raise failure

    before = time.time()
    result = native_agent.run_session(config, route, custody, 'Change and verify a.py', client=Refused(steps=steps()))
    assert result['failure_code'] == 'API_CREDIT_SPENT', result
    assert result['usage_limit']['account'] == 'openrouter' and result['usage_limit']['cause'] == 'no_credit'
    until = turn_engine.usage_limited(route)
    assert until == result['usage_limit']['until'] and until >= before + turn_engine.USAGE_LIMIT_DEFAULT_SECONDS


CODEX_DIRECT = {'id': 'codex-direct', 'native_profile': {'endpoint': 'https://chatgpt.com', 'model': 'gpt-6-luna',
                                                         'protocol': 'openai-codex-responses'}}


def test_a_codex_direct_refusal_for_a_spent_plan_is_told_from_a_passing_rate_limit():
    """Observed on this route in 2026-11: HTTP 429 with the type usage_limit_reached and no Retry-After. tare read it
    as a passing rate limit and left the account in."""
    spent = [{'call_id': 'c1', 'transport_calls': [{'provider_error': {'type': 'usage_limit_reached'}}]}]
    failed = {'code': 'HTTP_429', 'call_id': 'c1'}
    assert turn_engine.codex_limit(CODEX_DIRECT, failed, spent, 1000.0) == {
        'kind': 'account', 'resets_at': None, 'cause': 'allowance_spent'}
    timed = [{'call_id': 'c1', 'transport_calls': [{'provider_error': {'type': 'usage_limit_reached'}, 'retry_after': 600}]}]
    assert turn_engine.codex_limit(CODEX_DIRECT, {'reason': 'HTTP_429', 'call_id': 'c1'}, timed, 1000.0)['resets_at'] == 1600.0
    passing = [{'call_id': 'c1', 'transport_calls': [{'provider_error': {'type': 'rate_limit_exceeded'}, 'retry_after': 3}]}]
    assert turn_engine.codex_limit(CODEX_DIRECT, failed, passing, 1000.0) is None
    assert turn_engine.codex_limit(CODEX_DIRECT, {'code': 'HTTP_500', 'call_id': 'c1'}, spent, 1000.0) is None
    assert turn_engine.codex_limit(API, failed, spent, 1000.0) is None  # another protocol: an API key's 429 is not a plan's
    assert turn_engine.codex_limit(CODEX_DIRECT, None, spent, 1000.0) is None



def test_the_session_reads_a_codex_direct_spent_plan_where_it_reads_opencodes(monkeypatch, storage):
    """The session's failure path asks the Codex reading where it asks OpenCode's (native_agent.run_session): the
    same branch, the same code and parking. The reading itself is the test above; here the account it parks."""
    limit = turn_engine.codex_limit(CODEX_DIRECT, {'code': 'HTTP_429', 'call_id': 'c1'},
                                    [{'call_id': 'c1', 'transport_calls': [{'provider_error': {'type': 'usage_limit_reached'}}]}], time.time())
    now = time.time()
    until = turn_engine.note_usage_limit(CODEX_DIRECT, limit['resets_at'], kind=limit['kind'], now=now)
    assert until == now + turn_engine.USAGE_LIMIT_DEFAULT_SECONDS  # no Retry-After: asked again after the default
    cli = {'id': 'codex-cli', 'native_profile': {'endpoint': 'cli://codex', 'model': 'gpt-6.1-sol', 'protocol': 'vendor-cli'}}
    assert turn_engine.usage_limited(cli, now + 1) == until  # the same ChatGPT account: the CLI's routes too
    assert turn_engine.usage_limited(API, now + 1) is None
    import inspect
    from relay import native_agent
    assert 'codex_limit(deployment, last, raw_observations' in inspect.getsource(native_agent.run_session)
