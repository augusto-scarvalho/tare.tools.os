"""OpenCode gateway facts that the catalog, the picker and the HTTP client share; no discovery or inference.

The profiles (relay/native_deployments/opencode-*.json) come from scripts/ops/opencode_profiles.py and declare
`native_opencode`; the endpoint alone is never taken as evidence of the gateway.
"""
import os

CREDENTIAL = 'OPENCODE_API_KEY'


def is_route(value):
    return bool(value.get('native_opencode'))


def subscription(value):
    """An OpenCode subscription profile (Go, Go Plus, ...: the reply's billing_source says which); the others are
    the pay-per-token API, charged to the account balance."""
    return (value.get('native_opencode') or {}).get('plan') == 'sub'


def provider(value):
    return 'opencode-sub' if subscription(value) else 'opencode-api'


def connection(value):
    return 'OpenCode Subscription' if subscription(value) else 'OpenCode API'


def user_agent():
    """OpenCode asks each client to name itself instead of its HTTP library."""
    return 'tare/' + (os.environ.get('TARE_RELEASE_ID') or 'dev')[:16]


# OpenCode's 429 type for a subscription model whose allowance window is spent (observed 2026-11-09, with Retry-After).
SPENT = frozenset({'GoUsageLimitError'})
# ponytail: a reply billed to the balance gives no reset time; pause for the shortest allowance window (5 hours). A
# spent weekly or monthly allowance then costs one more paid call per pause; a ledger of usage.opencode.allowance
# would know the real reset.
BILLED_PAUSE = 5 * 3600


def plan_limit(deployment, failed, observations, now):
    """When a subscription model's own allowance is gone: the kernel's model_failed event `failed` was OpenCode's
    spent-plan 429 (its Retry-After is the reset) or a reply billed outside the plan (BILLING_SOURCE_REFUSED: Extra
    Usage charged the balance once). Returns {'kind': 'model', 'resets_at', 'cause'} for note_usage_limit, else None."""
    if not subscription(deployment) or not failed:
        return None
    code = failed.get('code') or failed.get('reason')  # a model call says code; a context summary says reason
    if code == 'BILLING_SOURCE_REFUSED':
        source = next((row.get('billing_source') for row in reversed(observations)
                       if row.get('call_id') == failed.get('call_id') and row.get('billing_source')), None)
        return {'kind': 'model', 'resets_at': now + BILLED_PAUSE, 'billing_source': source,
                'cause': 'billed_to_balance' if source in ('balance', 'byok') else 'unknown_billing_source'}
    if code != 'HTTP_429':
        return None
    receipt = next((call for row in reversed(observations) if row.get('call_id') == failed.get('call_id')
                    for call in reversed(row.get('transport_calls') or [])
                    if (call.get('provider_error') or {}).get('type') in SPENT), None)
    if receipt is None:
        return None  # a passing rate limit, not a spent plan
    after = receipt.get('retry_after')
    return {'kind': 'model', 'resets_at': now + after if type(after) is int else None, 'cause': 'allowance_spent'}


def names_delivery_tool(value):
    """Whether the last call may name the delivery tool: OpenCode lists the tool_choice values each model accepts,
    and an empty list is unknown, so those models keep automatic choice and rely on the budget notice."""
    return not is_route(value) or 'tool' in value['native_opencode'].get('tool_choice', [])
