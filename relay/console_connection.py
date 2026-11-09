"""Explicit read-only model observations. Never selects, loads or generates a model."""
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time


def deployment_for(key):
    root, session, profile, deployment, automatic = key
    if session:
        from compute_plane.native_journal import io_path, regular
        with io_path(regular(Path(session)/'native-agent.json')).open('rb') as stream:
            raw = stream.read(1024*1024+1)
        if len(raw) > 1024*1024:
            raise ValueError('descriptor_over_budget')
        saved = json.loads(raw)
        if saved['schema'] not in {'tare.tools/native-agent/1', 'tare.tools/native-agent/2'}:
            raise ValueError('unknown_session_schema')
        roots = saved['config']['repositories']
        if not isinstance(roots, dict) or not any(
                isinstance(path, str) and Path(path).is_absolute()
                and Path(path).resolve() == Path(root).resolve() for path in roots.values()):
            raise ValueError('session_project_mismatch')
        return saved['deployment']
    if automatic and not profile:
        return None
    if '@' in str(deployment):  # a coordinator pin (provider/route@level); catalog route names have no '@'
        from .coordinator_policy import select as pinned
        from .execution_scope import for_role
        with for_role('coordinator'):
            return pinned(deployment)
    from .native_catalog import select
    return select(None if profile else deployment, Path(profile) if profile else None)


@dataclass(frozen=True)
class Target:
    identity: str
    model: str
    endpoint: str
    protocol: str
    fleet: bool
    credential_env: str | None


def target_for(key):
    return _target_for(key, deployment_for(key))


def _target_for(key, deployment):
    if deployment is None or 'native_auto_routes' in deployment:
        return None
    from .native_generation import deployment_profile
    profile = deployment_profile(deployment, deployment['native_profile']['model'])
    return Target(deployment_identity(key, deployment), profile.model, profile.endpoint, profile.protocol,
                  deployment.get('native_counter_protocol') == 'slop-fleet-v1',
                  deployment.get('native_credential_env'))


def deployment_identity(key, deployment):
    return hashlib.sha256(json.dumps([key[0], key[1], deployment],
        sort_keys=True, allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class Observation:
    identity: str | None
    status: str
    detail: str
    observed_at: str
    monotonic: float
    duration_ms: int
    transport: dict | None = None

    def summary(self, now=None):
        age = max(0, int((time.monotonic() if now is None else now)-self.monotonic))
        age_text = str(age)+'s ago' if age < 60 else str(age//60)+'m ago'
        return self.status+' · '+age_text+(' · stale' if age >= 60 else '')


def interpret(target, value):
    if target.fleet:
        if (value.get('role') != 'qualified-model-gateway' or value.get('status') != 'ok'
                or type(value.get('backend_healthy')) is not bool
                or any(value.get(k) is not None and not isinstance(value[k], str)
                       for k in ('current_model', 'requested_name'))):
            raise ValueError('invalid_fleet_status')
        matched = target.model in (value.get('current_model'), value.get('requested_name'))
        if not matched:
            return 'Gateway reached', 'The requested model was not observed as resident. This check does not load or switch models.'
        if not value['backend_healthy']:
            return 'Backend not ready', 'The gateway reports that the requested resident model is not healthy.'
        return 'Model healthy', 'The gateway reports the requested resident model as healthy. This is not a generation or quality test.'
    rows = value.get('data')
    if (not isinstance(rows, list) or len(rows) > 10000
            or any(not isinstance(row, dict) or not isinstance(row.get('id'), str) for row in rows)):
        raise ValueError('invalid_model_list')
    if any(row['id'] == target.model for row in rows):
        return 'Model listed', 'The endpoint responded and lists the requested model. Generation and model readiness were not tested.'
    return 'Model not listed', 'The endpoint responded but did not list the exact requested model. Aliases and generation remain unverified.'


def probe(key, cancellation, *, timeout=5):
    from compute_plane.native_models import NativeHttpClient, NativeTransportInterrupted
    from tare_tools_agent_runtime import BindingRef, ExecutionControl, ModelFailure
    started = time.monotonic()
    target = None
    receipt = None
    def result(status, detail):
        return Observation(target.identity if target else None, status, detail,
            datetime.now(timezone.utc).isoformat(), time.monotonic(),
            round((time.monotonic()-started)*1000), receipt)
    try:
        deployment = deployment_for(key)
        target = _target_for(key, deployment)
    except Exception:
        return result('Profile unavailable', 'The project-bound model profile could not be read or validated. Check Settings.')
    if target is None:
        return result('Model not selected', 'Automatic routing chooses a model when execution starts. Check again after a session is created.')
    if target.protocol not in {'openai-chat', 'openai-responses', 'anthropic-messages'}:
        return result('Check unavailable', 'This model protocol has no configured read-only health endpoint. No request was sent.')
    control = ExecutionControl(BindingRef('connection-check', target.identity, 'console', 1, target.identity),
                               cancellation, started+timeout)
    def credential():
        if not target.credential_env:
            return None
        from .native_generation import vault_secret
        value = os.environ.get(target.credential_env) or vault_secret(target.credential_env)
        if not value:
            raise ModelFailure('AUTH_FAILED')
        return value
    try:
        from .native_generation import inference_client, deployment_profile
        profile = deployment_profile(deployment, target.model)
        client = inference_client(deployment, profile, credential=credential, timeout_seconds=timeout,metadata_only=True)
        value, receipt = client.get_observed('/fleet/status' if target.fleet else '/models', control)
        status, detail = interpret(target, value)
        return result(status, detail)
    except NativeTransportInterrupted as exc:
        receipt = exc.transport_observation
        if not exc.transport_observation.get('cleanup_verified'):
            return result('Check unconfirmed', 'The connection check could not confirm transport cleanup.')
        if cancellation.is_cancelled():
            return result('Check cancelled', 'The read-only connection check was stopped.')
        if exc.transport_observation.get('timeout') or control.remaining_seconds() <= 0:
            return result('Check timed out', 'The endpoint did not complete the check within its time limit.')
        return result('Connection unavailable', 'The HTTP exchange did not complete. Check the endpoint, network and server.')
    except ModelFailure as exc:
        receipt = getattr(exc, 'transport_observation', None)
        if exc.code == 'AUTH_FAILED':
            return result('Credential unavailable', 'The profile credential is unavailable. No request was sent.')
        if exc.code in {'HTTP_401', 'HTTP_403'}:
            return result('Access refused', 'The endpoint responded but refused access to the check.')
        if exc.code in {'HTTP_404', 'HTTP_405'}:
            return result('Check unavailable', 'The endpoint responded but does not expose this read-only check.')
        if exc.code == 'HTTP_429':
            return result('Rate limited', 'The endpoint refused the check because of rate limiting. No retry was made.')
        return result('Check failed', 'The endpoint returned an error or an invalid response. Response bodies are not displayed.')
    except Exception:
        return result('Check failed', 'The response did not satisfy the configured check. No retry or model switch was made.')
