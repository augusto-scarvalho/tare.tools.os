"""Resolve optional execution limits once; null means no ceiling, absent inherits."""
import argparse
import math

UNSET = object()
# One retry of a retryable model failure (429, 502 to 504, a connection refused before sending): a single 503
# ended a session in the phase 3 comparison (2026-10-06). Explorers and challengers keep their own 0.
BUDGET_DEFAULTS = {'max_model_calls': 16, 'max_stagnant_turns': 3, 'max_model_retries': 1}
# Unlimited lifts calls and time, not the no-progress stop: a model that repeats itself with nobody watching
# would never end. 3 (the default) was too tight: replaying 372 saved sessions (2026-11-12), the longest run of
# replies without progress inside a session that then delivered was 4. At the limit the session pauses and
# any message continues it.
UNLIMITED_STAGNANT_TURNS = 6
TIME_FIELDS = ('max_seconds', 'request_seconds', 'connect_seconds', 'read_seconds')
# A model call is judged by the silence of its stream, not by a clock of its own (owner, 2026-11-14): a call whose
# bytes keep arriving is never stopped by a total, one that sent nothing for this long is. The session's own budget
# (max_seconds) still applies.
SILENCE_SECONDS = 600
FIELDS = {*BUDGET_DEFAULTS, *TIME_FIELDS}


def validate(values):
    if not isinstance(values, dict) or set(values) - FIELDS:
        raise ValueError('invalid_native_execution_limits')
    for name, value in values.items():
        if value is None:
            continue
        if name in BUDGET_DEFAULTS:
            minimum = 0 if name == 'max_model_retries' else 1
            if type(value) is not int or value < minimum:
                raise ValueError('invalid_native_limit:' + name)
        elif type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError('invalid_native_limit:' + name)
    return values


def streams(deployment):
    """Whether this route shows life while the model works: a streaming HTTP request, or a vendor CLI (the Kernel
    judges each by what its stdout says: rows while it works, or its report of a failed attempt). Those get a
    silence window."""
    profile = deployment.get('native_profile') or {}
    if profile.get('protocol') == 'vendor-cli':
        return True
    from .native_generation import streaming
    return bool(profile) and streaming(deployment)


def resolve(deployment, overrides=None):
    overrides = {} if overrides is None else overrides
    values = {**validate(deployment.get('native_execution_limits', {})), **validate(overrides)}
    resolved = {**BUDGET_DEFAULTS, 'max_seconds': 300, **values}
    # urllib tries DNS candidates in order. One unreachable address must
    # yield to the next before the whole inference deadline expires.
    codex = deployment.get('native_profile', {}).get('protocol') == 'openai-codex-responses'
    if streams(deployment):
        # No total of its own, from any source; connecting and reading wait one silence window, unless the caller
        # named a number. A null (the unlimited conversation) is no choice: a hung connection must end.
        resolved['request_seconds'] = None
        for name, window in (('connect_seconds', 5 if codex else SILENCE_SECONDS), ('read_seconds', SILENCE_SECONDS)):
            if overrides.get(name) is None:
                resolved[name] = window
    # Preserve the old default relationship, without turning explicit null into
    # a numeric fallback. Shorter enclosing deadlines still constrain requests.
    resolved.setdefault('request_seconds', resolved['max_seconds'])
    connect = resolved['request_seconds']
    if codex:
        connect = 5 if connect is None else min(5, connect)
    resolved.setdefault('connect_seconds', connect)
    resolved.setdefault('read_seconds', resolved['request_seconds'])
    return resolved


def from_state(state):
    if state.get('schema') == 'tare.tools/native-agent/2' and 'limits' not in state:
        raise ValueError('native_agent_limits_missing')
    # Old persisted sessions retain their exact numeric budgets/deadline.
    values = resolve({}, {**state['budget'], 'max_seconds': state['timeout']})
    if 'limits' in state:
        stored = validate(state['limits'])
        identity = dict(values)
        grant = state.get('operator_call_budget') or {}
        # The explicit OS pool includes delegated calls; it does not change
        # the tactical NativeBudget bound into the immutable runtime journal.
        if (state.get('conversation_flow') and not state.get('delegated_leaf')
                and grant.get('source') == 'operator_cli' and isinstance(grant.get('message_id'), str)
                and grant['message_id'] and type(grant.get('total_calls')) is int
                and type(grant.get('original_limit')) is int
                and grant['original_limit'] == values['max_model_calls']
                and grant['original_limit'] <= grant['total_calls'] <= 10000):
            identity['max_model_calls'] = grant['total_calls']
        if set(stored) != FIELDS or any(stored[k] != identity[k] for k in (*BUDGET_DEFAULTS, 'max_seconds')):
            raise ValueError('native_agent_limits_identity_mismatch')
        values = dict(stored)
    deadline = state['deadline_utc']
    if deadline is not None and (type(deadline) not in (int, float) or not math.isfinite(deadline)):
        raise ValueError('native_agent_invalid_deadline')
    if (deadline is None) != (values['max_seconds'] is None):
        raise ValueError('native_agent_deadline_identity_mismatch')
    return values


def http_options(values):
    return {'timeout_seconds': values['request_seconds'],
            'connect_timeout_seconds': values['connect_seconds'],
            'read_timeout_seconds': values['read_seconds']}


def cli_limit(kind):
    def parse(text):
        if text.lower() in {'none', 'unlimited'}:
            return None
        try:
            value = kind(text)
            if not math.isfinite(value) or value < 0:
                raise ValueError()
            return value
        except ValueError:
            raise argparse.ArgumentTypeError('use a number or unlimited') from None
    return parse
