"""Pure projections of authoritative scheduling decisions; no admission or retry logic."""
from collections.abc import Mapping
from copy import deepcopy
from typing import Literal, TypedDict


class _RequiredReason(TypedDict):
    code: str
    category: Literal['resource', 'policy', 'dependency', 'configuration', 'unknown']
    message: str
    action: str
    retryable: bool


class UnschedulableReason(_RequiredReason, total=False):
    retry_after_seconds: int


# Only observed capacity limits and known transient states are retryable hints.
# These hints never grant permission, schedule a retry, or replace owner decisions.
_CATALOG = {
    'MACHINE_POLICY': ('policy', 'This route is excluded by machine policy.', 'Choose a permitted route or review the machine setup.', False),
    'LOCAL_GPU_DISABLED': ('policy', 'Local GPU use is disabled for this selection.', 'Choose a permitted route or review the local GPU setup.', False),
    'PROFILE_UNAVAILABLE': ('configuration', 'The route profile is unavailable.', 'Review the configured profile or choose another route.', False),
    'CLI_UNAVAILABLE': ('dependency', 'The required CLI is unavailable.', 'Review the CLI installation or choose another route.', False),
    'QUOTA_EXHAUSTED': ('resource', 'The route quota is exhausted.', 'Wait for quota renewal or choose another permitted route.', True),
    'CREDENTIAL_UNAVAILABLE': ('dependency', 'The required credential is unavailable.', 'Review credential setup or choose another permitted route.', False),
    'LOGIN_UNAVAILABLE': ('dependency', 'The selected local Codex login is unavailable or expired.', 'Sign in with Codex CLI, use its CLI route, or choose another permitted connection.', False),
    'GPU_BUSY': ('resource', 'The GPU is currently busy.', 'Wait for capacity or choose another permitted route.', True),
    'GPU_RESERVED': ('resource', 'The GPU capacity is reserved.', 'Wait for the reservation to end or choose another permitted route.', True),
    'GATEWAY_UNREACHABLE': ('dependency', 'The gateway could not be reached.', 'Review gateway connectivity or choose another permitted route.', True),
    'GATEWAY_ERROR': ('dependency', 'The gateway reported an error.', 'Review the gateway status or choose another permitted route.', False),
    'RESOURCE_OBSERVATION_INVALID': ('unknown', 'The resource observation is invalid.', 'Review the authoritative resource observation before selecting this route.', False),
    'GPU_STATUS_UNKNOWN': ('unknown', 'The GPU status is unknown.', 'Obtain a current authoritative GPU observation or choose another route.', False),
    'GPU_STATUS_EXPIRED': ('unknown', 'The GPU status observation has expired.', 'Obtain a current authoritative GPU observation or choose another route.', False),
    'GPU_MODEL_NOT_ADVERTISED': ('configuration', 'The GPU service does not advertise the requested model.', 'Review the model configuration or choose an advertised route.', False),
    'GPU_IMAGE_WAITING': ('resource', 'An image job is waiting for the shared GPU.', 'Wait for queued image work or choose another permitted route.', True),
    'GPU_LEASE_HELD': ('resource', 'The GPU lease is held by another owner.', 'Wait for lease release or choose another permitted route.', True),
    'ROLE_NOT_CONFIGURED': ('configuration', 'This route is not configured for the requested role.', 'Choose a configured route or review role setup.', False),
    'NOT_QUALIFIED_PIN_ONLY': ('configuration', 'This route is registered from provider metadata and not yet qualified.', 'Pin it exactly (tare models --role ROLE) or qualify it through tare.', False),
    'RUNNER_NOT_SUPPORTED_BY_NATIVE_WORK': ('configuration', 'This runner is not supported by native Work.', 'Choose a supported native Work runner or review setup.', False),
    'RUNNER_NOT_SUPPORTED_BY_NATIVE_FLOW': ('configuration', 'This runner is not supported by native flow.', 'Choose a supported native flow runner or review setup.', False),
    'COST_OR_PREFERENCE_UNKNOWN': ('configuration', 'The route cost tier or preference is unknown.', 'Review the configured cost tier and route preference.', False),
    'PROFILE_NOT_INSTALLED': ('dependency', 'The required profile is not installed.', 'Review profile installation or choose another route.', False),
    'PROFILE_OR_IDENTITY_INVALID': ('configuration', 'The route profile or identity is invalid.', 'Review the configured profile and model identity.', False),
    'NOT_USED_ON_THIS_MACHINE': ('policy', 'This route is not permitted on this machine.', 'Choose a permitted route or review the machine setup.', False),
    'OLDER_MODEL': ('policy', 'The model is excluded by the model selection policy.', 'Choose a permitted model or review the selection setup.', False),
    'AUDITOR_NOT_INDEPENDENT': ('policy', 'The auditor is not independent of the producer.', 'Choose an independent auditor that satisfies the audit policy.', False),
    'CLI_EXECUTABLE_UNAVAILABLE': ('dependency', 'The configured CLI executable is unavailable.', 'Review the executable installation and configuration.', False),
    'DUPLICATE_REALIZATION': ('configuration', 'This route duplicates an existing realization.', 'Choose a distinct configured route or review duplicate configuration.', False),
    'UNKNOWN': ('unknown', 'The route is unavailable for an unspecified reason.', 'Review authoritative route diagnostics or choose another permitted route.', False),
}


def _valid_delay(value):
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 86400


def diagnostic(code, *, retry_after_seconds=None) -> UnschedulableReason:
    """Return a fresh fixed projection, never interpreting human-readable text.

    Invalid explicit retry delays raise ValueError rather than being coerced.
    """
    if retry_after_seconds is not None and not _valid_delay(retry_after_seconds):
        raise ValueError('retry_after_seconds must be an integer from 0 to 86400')
    code = code if isinstance(code, str) and code in _CATALOG else 'UNKNOWN'
    category, message, action, retryable = _CATALOG[code]
    result: UnschedulableReason = dict(
        code=code, category=category, message=message, action=action, retryable=retryable
    )
    if retry_after_seconds is not None:
        result['retry_after_seconds'] = retry_after_seconds
    return result


class Unavailable(str):
    """Legacy reason string with a separate, explicit machine-readable reason."""

    def __new__(cls, message, code, *, retry_after_seconds=None):
        value = super().__new__(cls, message)
        value.diagnostic = diagnostic(code, retry_after_seconds=retry_after_seconds)
        return value


def _project(row):
    reason = row.get('reason')
    source = reason.diagnostic if isinstance(reason, Unavailable) else row.get('diagnostic')
    if isinstance(source, Mapping):
        delay = source.get('retry_after_seconds')
        return diagnostic(source.get('code'), retry_after_seconds=delay if _valid_delay(delay) else None)
    # Exact catalog codes only: never substring matching or exception parsing.
    return diagnostic(reason)


def _route_name(value):
    if not isinstance(value, str):
        return '(unnamed route)'
    visible = ''.join(char if char.isprintable() else '?' for char in value)
    return (visible[:63] + '…' if len(visible) > 64 else visible) or '(unnamed route)'


class UnschedulableError(ValueError):
    """Bounded report of existing skipped routes or ineligible role candidates."""

    def __init__(self, context, rows):
        reasons = []
        omitted = 0
        for row in rows:
            if row.get('eligible') is True:
                continue
            if len(reasons) == 64:
                omitted += 1
                continue
            reasons.append({'route': _route_name(row.get('route', row.get('deployment'))), **_project(row)})
        self._report = {
            'schema': 'tare.tools/unschedulable/1', 'context': context,
            'reasons': reasons, 'omitted': omitted,
        }
        super().__init__(render_report(self._report))

    @property
    def report(self):
        """A detached snapshot: callers cannot mutate the exception's report."""
        return deepcopy(self._report)

    def __str__(self):
        return render_report(self._report)


def render_report(report):
    """Plain factual text; does not assert whether any model or command started."""
    lines = [f"{report['context']}: No route can start."]
    if not report['reasons']:
        lines.append('No candidate routes were supplied. Review the route configuration or choose an installed model.')
    for reason in report['reasons']:
        lines.append(f"- {reason['route']} [{reason['code']}]: {reason['message']} Next step: {reason['action']}")
    if report['omitted']:
        lines.append(f"{report['omitted']} additional route(s) omitted.")
    return '\n'.join(lines)
