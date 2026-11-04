"""A failed DNS candidate yields while explicit operator limits remain authoritative."""
from relay.native_limits import resolve


def test_native_codex_connection_budget_leaves_time_for_another_address_and_response():
    deployment={'native_profile':{'protocol':'openai-codex-responses'},
                'native_execution_limits':{'request_seconds':120}}
    limits=resolve(deployment,{'max_seconds':60})
    assert 0 < limits['connect_seconds'] < min(limits['max_seconds'],limits['request_seconds'])
    assert limits['read_seconds']==120
    # Explicit limits and saved sessions retain their original policy.
    assert resolve(deployment,{'connect_seconds':90})['connect_seconds']==90
    assert resolve(deployment,{'connect_seconds':None})['connect_seconds'] is None
    assert resolve({'native_execution_limits':{'request_seconds':120}})['connect_seconds']==120


def test_a_shorter_explicit_request_deadline_is_not_increased():
    limits=resolve({'native_profile':{'protocol':'openai-codex-responses'}},{'request_seconds':0.1})
    assert limits['connect_seconds'] <= limits['request_seconds'] == 0.1
