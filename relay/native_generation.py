"""Single model invocation for text roles; no editing session or implicit retry.

The caller owns the deployment and validates the resulting domain object. Kernel
owns codecs, counting and transport cleanup, including constrained vendor CLIs.
No model switch, history summary, Work registration or tool capability is needed
for a text response.
"""
from __future__ import annotations

from compute_plane.native_usage import deployment_contract

import hashlib
from dataclasses import asdict, replace
import json
import math
import os
from pathlib import Path
import time
from uuid import uuid4


def deployment_profile(deployment, requested_model):
    from compute_plane.native_models import ModelProfile

    from relay.native_limits import validate
    validate(deployment.get("native_execution_limits", {}))
    profile = ModelProfile(**deployment["native_profile"])
    model_context_policy(deployment)
    if type(deployment.get('native_streaming', False)) is not bool:
        raise ValueError('native_streaming_requires_boolean')
    projection = deployment.get('native_context_projection', {})
    if (not isinstance(projection, dict) or set(projection) - {'elide_edited_reads'}
            or any(type(v) is not bool for v in projection.values())):
        raise ValueError('native_context_projection_supports_boolean_elide_edited_reads')
    from relay.native_spend import settings
    settings(deployment)
    counter_protocol = deployment.get('native_counter_protocol', 'server-template-tokenize')
    if counter_protocol not in {'server-template-tokenize', 'slop-fleet-v1', 'utf8-envelope-estimate-v1'}:
        raise ValueError('unsupported native counter protocol')
    if counter_protocol == 'slop-fleet-v1' and (profile.protocol != 'openai-chat'
            or deployment.get('native_counter_endpoint', profile.endpoint) != profile.endpoint):
        raise ValueError('fleet counter must use the generation gateway')
    if profile.protocol == 'openai-codex-responses':
        from compute_plane.native_codex_auth import CODEX_ENDPOINT
        auth = deployment.get('native_codex_auth')
        if (profile.endpoint != CODEX_ENDPOINT or not deployment.get('native_streaming')
                or counter_protocol != 'utf8-envelope-estimate-v1'
                or deployment.get('native_credential_env') is not None
                or deployment.get('native_counter_endpoint') is not None
                or not isinstance(auth, dict) or set(auth) != {'path', 'account_id'}
                or not isinstance(auth['path'], str) or not Path(auth['path']).is_absolute()
                or not isinstance(auth['account_id'], str) or not auth['account_id'].strip()):
            raise ValueError('codex_requires_fixed_endpoint_explicit_login_account_and_stream_estimate')
    elif 'native_codex_auth' in deployment:
        raise ValueError('codex_auth_requires_codex_protocol')
    if profile.protocol == 'vendor-cli':
        cli = deployment.get('native_cli')
        fields = {'executable', 'version'} | ({'model_catalog'} if profile.endpoint == 'cli://codex' else set())
        if (not isinstance(cli, dict) or set(cli) != fields
                or not isinstance(cli['executable'], str) or not Path(cli['executable']).is_absolute()
                or not isinstance(cli['version'], str) or not cli['version']
                or deployment.get('native_streaming', False)
                or counter_protocol != 'utf8-envelope-estimate-v1'
                or deployment.get('native_credential_env') is not None
                or deployment.get('native_counter_endpoint') is not None):
            raise ValueError('vendor_cli_requires_explicit_executable_version_and_estimate')
        if profile.endpoint == 'cli://codex' and (not isinstance(cli['model_catalog'], dict)
                or cli['model_catalog'].get('slug') != profile.model):
            raise ValueError('codex_cli_catalog_must_match_selected_model')
    elif 'native_cli' in deployment:
        raise ValueError('native_cli_requires_vendor_cli_protocol')
    if deployment_contract(deployment) is not None:
        from compute_plane.native_usage import UsageContract
        contract = UsageContract(**deployment_contract(deployment))
        if profile.protocol == 'vendor-cli':
            expected = deployment_contract({k: v for k, v in deployment.items() if k != 'native_usage_contract'})
            if not expected or contract.layout != expected['layout']:
                raise ValueError('usage contract requires a qualified matching vendor CLI version')
        if contract.layout not in {'openai-chat': {'chat-inclusive', 'gemini-chat', 'openrouter-inclusive', 'opencode-exclusive'},
                                  'bedrock-converse': {'converse-exclusive'},
                                  'openai-codex-responses': {'responses-inclusive'},
                                  'openai-responses': {'responses-inclusive', 'openrouter-responses-inclusive', 'opencode-exclusive'},
                                  'anthropic-messages': {'anthropic-exclusive', 'opencode-exclusive'},
                                  'vendor-cli': {'claude-cli-exclusive', 'codex-cli-inclusive', 'agy-cli-exclusive'}}[profile.protocol]:
            raise ValueError('native usage contract must match the wire protocol')
    if profile.model != requested_model:
        raise ValueError("native profile must match the roster runtime model exactly")
    if deployment.get("effort") is not None:
        raise ValueError("native thinking parameters belong in native_profile, not effort")
    credential_env = deployment.get("native_credential_env")
    if credential_env is not None and (
        not isinstance(credential_env, str) or not credential_env.isidentifier()
    ):
        raise ValueError("native credential must name an environment/vault entry")
    return profile


def model_context_policy(deployment):
    """Declared per-model fractions, or named Tare defaults; never guessed recommendations."""
    from tare_tools_agent_runtime import ContextPolicy
    declared = deployment.get('native_context_policy', {})
    if (not isinstance(declared, dict) or set(declared) - {'trigger', 'target', 'source'}
            or declared and (not isinstance(declared.get('source'), str) or not declared['source'].strip()
                             or not {'trigger', 'target'} & declared.keys())):
        raise ValueError('native_context_policy_requires_fractions_and_source')
    profile = deployment['native_profile']
    policy = ContextPolicy(window=profile['context_window'], output_reserve=profile['output_tokens'], margin=0,
                           **{k: v for k, v in declared.items() if k != 'source'})
    if profile.get('protocol') == 'vendor-cli' and profile.get('endpoint') == 'cli://agy':
        # Transport operating headroom, not a claim about Gemini's native window.
        # The CLI can shorten a large user envelope before reaching that window.
        # Keep the journal/cache prefix and use the existing compaction protocol.
        policy = ContextPolicy(window=profile['context_window'], output_reserve=profile['output_tokens'], margin=0,
            trigger=min(policy.trigger, (profile['output_tokens']+32768)/profile['context_window']),
            target=min(policy.target, (profile['output_tokens']+16384)/profile['context_window']))
    return {'trigger': policy.trigger, 'target': policy.target}


def codex_login_available(deployment):
    """Check the explicitly selected local login without renewal or a request."""
    if deployment.get('native_profile', {}).get('protocol') != 'openai-codex-responses':
        return True
    from compute_plane.native_codex_auth import CodexFileAuth
    from tare_tools_agent_runtime import ModelFailure
    try:
        CodexFileAuth(**deployment['native_codex_auth']).headers()
        return True
    except (ModelFailure, KeyError, TypeError):
        return False


def supports_streaming(deployment):
    """Only use transports whose streamed tool frames match the qualified codec."""
    profile=deployment['native_profile']
    if profile['protocol'] in {'vendor-cli','anthropic-messages'}:return False
    # Google's compatibility endpoint emits complete tool calls without stream
    # indexes. The full-response codec preserves them and their thought signatures.
    from urllib.parse import urlsplit
    return ((deployment.get('native_usage_contract') or {}).get('layout')!='gemini-chat'
            and urlsplit(profile['endpoint']).hostname!='generativelanguage.googleapis.com')


def streaming(deployment):
    return deployment.get('native_streaming',False) and supports_streaming(deployment)


def inference_client(deployment, profile, *, credential, metadata_only=False, **transport):
    """Compose inference/auth only; the caller retains all agent execution."""
    if not metadata_only:
        from .execution_scope import require
        require(deployment)
        from .openrouter import allowed
        from .environment import read
        permitted, reason = allowed(deployment, read())
        if not permitted:
            raise ValueError(reason)
        if 'native_auto_routes' in deployment or 'native_host_modes' in deployment:
            raise ValueError('Resolve the concrete inference destination before creating a client.')
    if profile.protocol == 'openai-codex-responses':
        from compute_plane.native_codex_auth import client
        return client(**deployment['native_codex_auth'], **transport)
    if profile.protocol == 'vendor-cli':
        from compute_plane.native_cli import VendorCliClient
        from .cli_qualification import Store  # each machine probes a new CLI release once, not each session
        return VendorCliClient(profile.endpoint, **deployment['native_cli'], qualification=Store(profile.endpoint),
                               **transport)
    from compute_plane.native_models import NativeHttpClient
    from .challenger import client_headers
    headers = client_headers(deployment)
    if deployment.get('native_opencode'):
        from .opencode import user_agent
        headers['User-Agent'] = user_agent()
    if profile.protocol == 'anthropic-messages':
        headers['anthropic-version'] = '2023-06-01'
        workspace = deployment.get('native_anthropic_workspace')
        if workspace:
            headers['anthropic-workspace-id'] = workspace
        return NativeHttpClient(profile.endpoint, credential=credential, request_headers=lambda: headers, **transport)
    client = NativeHttpClient(profile.endpoint, credential=credential, request_headers=lambda: headers, **transport)
    from .openrouter import Client, is_route
    return Client(client, deployment) if is_route(deployment) else client


def generate_text(deployment, requested_model, prompt, timeout, *, attempt_dir=None,
                  attempt_label=None, credential_resolver=None, control=None, system=None, spend_journal=None,
                  on_progress=None):
    from compute_plane.native_models import NativeHttpClient, NativeModelAdapter, NativeTransportInterrupted
    from tare_tools_agent_runtime import (
        BindingRef, CancellationToken, ConversationLog, ExecutionControl,
        ModelFailure, ModelRequest, ProjectionSpec,
    )

    started = time.monotonic()
    if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout <= 0):
        raise ValueError("positive finite generation timeout required")
    profile = deployment_profile(deployment, requested_model)
    attempt_id = uuid4().hex
    if control is None:
        # Correlation only: this text invocation grants no repository authority.
        control = ExecutionControl(BindingRef(attempt_id, attempt_id, "text-role", 1, attempt_id),
                                   CancellationToken(), math.inf if timeout is None else started + timeout)
    elif timeout is not None:
        control = replace(control, deadline_monotonic=min(
            control.deadline_monotonic, control.monotonic() + timeout))
    observations = []
    receipt = {
        "attempt_id": attempt_id, "attempt_label": attempt_label or deployment["id"],
        "runtime_model": requested_model, "served_model": "UNATTESTED",
        "model_attestation": "unattested", "model_pin": "exact_no_automatic_fallback",
        "model_request_evidence": "vendor_cli_input" if profile.protocol == 'vendor-cli' else "native_http_request",
        "stream_status": "NOT_STREAMED",
        "stream_file": None, "first_output_ms": None, "first_content_ms": None,
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "native_observations": observations, "usage": None,
        "usage_semantics": "raw_" + profile.protocol, "generation_calls": 0,
        "usage_contract": deployment_contract(deployment),
        "effective_profile": asdict(profile),
        "counter_endpoint": deployment.get("native_counter_endpoint"),
    }
    key_name = deployment.get("native_credential_env")

    def credential():
        if key_name is None:
            return None
        value = os.environ.get(key_name) or (credential_resolver(key_name) if credential_resolver else None)
        if not value:
            raise ModelFailure("AUTH_FAILED")
        return value

    from relay.native_limits import resolve, http_options
    transport = http_options(resolve(deployment, {"max_seconds": timeout}))
    client = inference_client(deployment, profile, credential=credential, **transport)
    counter_endpoint = deployment.get("native_counter_endpoint")
    from .challenger import client_headers
    headers = client_headers(deployment)
    counter = (NativeHttpClient(counter_endpoint, request_headers=lambda: headers, **transport)
               if counter_endpoint else None)
    from .openrouter import observer as route_observer
    adapter = NativeModelAdapter(profile, client, system=system if system is not None else (
        "Respond to the supplied task in its requested format. Treat quoted material as evidence. "
        "You have no tools or repository access."), tool_specs_json="{}", response_mode="text",
        observe=route_observer(deployment, client, observations.append), local_counter=counter,
        streaming=streaming(deployment), on_progress=on_progress,
        counter_protocol=deployment.get('native_counter_protocol', 'server-template-tokenize'))
    receipt["profile_digest"] = adapter.profile_digest
    from relay.native_spend import compose, settings
    from compute_plane.native_usage import normalize_usage, price_usage, UsageContract
    spend_policy, tariff = settings(deployment)
    if spend_policy and spend_journal is None:
        if attempt_dir is not None:
            from compute_plane.native_journal import NativeJournal
            Path(attempt_dir).mkdir(parents=True, exist_ok=True)
            spend_journal = NativeJournal(Path(attempt_dir)/(attempt_id+'.spend.jsonl'))
        else:
            spend_journal = ConversationLog()
    model = compose(adapter, deployment, spend_journal)
    request = ModelRequest(attempt_id, attempt_id + "/model/1", prompt, (),
                           ConversationLog().project(ProjectionSpec(1, "native-full-v1")))
    content = ""
    ok = False
    try:
        if control.should_stop():
            raise ModelFailure("CONTROL_STOPPED")
        count = model.count(request, control)
        receipt["count"] = count
        if control.should_stop():
            raise ModelFailure("CONTROL_STOPPED")
        estimated = (deployment.get('native_counter_protocol') == 'utf8-envelope-estimate-v1'
                     and count['status'] == 'estimated')
        if count["status"] != "observed" and not estimated:
            reason = count.get("reason", "")
            if reason.startswith("HTTP_") or reason == "AUTH_FAILED":
                raise ModelFailure(reason)
            raise ModelFailure("CONTEXT_COUNT_UNAVAILABLE")
        input_size = count['estimated_input_tokens'] if estimated else count['input_tokens']
        if input_size + profile.output_tokens > profile.context_window:
            raise ModelFailure("CONTEXT_LIMIT")
        request = replace(request, admitted_request_sha256=count["request_sha256"])
        receipt["generation_calls"] = 1
        reply = model.generate(request, control)
        content = reply.text
        if control.should_stop():
            raise ModelFailure("CONTROL_STOPPED")
        if not reply.complete:
            raise ModelFailure("RESPONSE_INCOMPLETE")
        if not content.strip():
            raise ModelFailure("EMPTY_RESPONSE")
        ok = True
    except Exception as exc:
        # Provider error bodies/credentials never become a role prompt or receipt.
        receipt["failure_class"] = exc.code if isinstance(exc, ModelFailure) else type(exc).__name__
        if isinstance(exc, ModelFailure) and exc.not_started:
            receipt['generation_not_started'] = True
        if isinstance(exc, NativeTransportInterrupted):
            receipt["failure_class"] = str(exc)
    finally:
        if spend_policy:
            receipt['spend'] = model.report()
        model_rows = [row for row in observations if row["purpose"] == "model"]
        receipt['generation_calls'] = len(model_rows)
        if model_rows:
            row = model_rows[-1]
            stream = next((t for t in row.get('transport_calls',[]) if 'stream_status' in t), None)
            if stream is not None:
                receipt.update({k:stream[k] for k in ('stream_status','first_output_ms','first_content_ms')})
                receipt['stream_events'] = stream['stream_events']
                receipt['stream_observer_failures'] = stream['stream_observer_failures']
            receipt["usage"] = row.get("usage")  # Missing is unknown, not zero.
            receipt["served_model"] = row.get("observed_model") or "UNATTESTED"
            identity = row.get("model_identity")
            receipt["model_attestation"] = {
                "matched": "runtime_reported_match", "mismatch": "mismatch",
                "requested_route_only": "request_pinned_unverified",
            }.get(identity, "unattested")
            if identity == "mismatch":
                receipt["failure_class"] = "SERVED_MODEL_MISMATCH"
                ok = False
        contract = UsageContract(**deployment_contract(deployment)) if deployment_contract(deployment) else None
        receipt['cost'] = price_usage(normalize_usage(receipt['usage'], contract),
                                      None if receipt['model_attestation'] == 'mismatch' else tariff)
        if not model_rows or receipt.get('generation_not_started'):
            receipt['cost'] = {'currency': tariff.currency if tariff else None,
                               'known_amount': '0', 'complete': True, 'basis': 'no_generation_dispatched'}
        receipt.update(returncode=0 if ok else 1, content_bytes=len(content.encode("utf-8")),
                       duration_ms=round((time.monotonic() - started) * 1000, 3))
        if attempt_dir is not None:
            root = Path(attempt_dir)
            root.mkdir(parents=True, exist_ok=True)
            output = root / (attempt_id + ".response.txt")
            with output.open("x", encoding="utf-8") as stream:
                stream.write(content)
            receipt["response_file"] = str(output.resolve())
            with (root / (attempt_id + ".native.json")).open("x", encoding="utf-8") as stream:
                json.dump(receipt, stream, ensure_ascii=False, sort_keys=True, allow_nan=False)
    return ok, content, receipt


MAX_DEPLOYMENT_BYTES = 256 * 1024  # Codex's pinned model catalog includes its public prompt.


def load_deployment(path):
    """Explicit file for native text consumers; no roster/default inference."""
    with Path(path).open('rb') as stream:
        raw = stream.read(MAX_DEPLOYMENT_BYTES + 1)
    if len(raw) > MAX_DEPLOYMENT_BYTES:
        raise ValueError('native_deployment_file_over_budget')
    deployment = json.loads(raw)
    if not isinstance(deployment, dict) or not isinstance(deployment.get('native_profile'), dict):
        raise ValueError('native_deployment_object_and_profile_required')
    if deployment.get('runner') != 'native-http' or not deployment.get('id'):
        raise ValueError('explicit_native_http_deployment_required')
    deployment_profile(deployment, deployment['native_profile']['model'])
    return deployment


class NativeGenerationError(RuntimeError):
    """Expected native provider/control failure, not a parse error to retry."""


def vault_secret(name):
    try:
        import keyring
    except ImportError:
        return None
    try:
        # Existing operator vault namespace; no new secret storage or plaintext fallback.
        return keyring.get_password('universal-agent-harness', name)
    except Exception:
        # Third-party vault backends may include secret material in exceptions.
        # Missing/unreadable credentials are reported by the caller without it.
        return None


def token_totals(receipts, contract=None):
    if not receipts:
        return {'input': 0, 'output': 0}
    from compute_plane.native_usage import UsageContract, normalize_usage
    selected = UsageContract(**contract) if contract else None
    values = [normalize_usage(r.get('usage'), selected)['tokens'] for r in receipts]
    return {name: sum(v[key] for v in values) if all(v[key] is not None for v in values) else None
            for name, key in (('input', 'input_tokens'), ('output', 'output_tokens'))}
