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


def names_delivery_tool(value):
    """Whether the last call may name the delivery tool: OpenCode lists the tool_choice values each model accepts,
    and an empty list is unknown, so those models keep automatic choice and rely on the budget notice."""
    return not is_route(value) or 'tool' in value['native_opencode'].get('tool_choice', [])
