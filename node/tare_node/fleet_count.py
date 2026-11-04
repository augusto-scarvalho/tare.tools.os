"""Text request counting and stateless route checks under the fleet request lock.

Uses the selected server's public template/tokenizer APIs. A binding detects
configuration/request drift; it is not a lease, signature or scheduling promise.
"""
import hashlib
import json
import time
from urllib import request


SCHEMA = 'tare.tools/fleet-count/1'
BINDING_FIELD = 'tare_fleet_binding'
MAX_JSON = 8 * 1024 * 1024


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(encode(value)).hexdigest()


class BindingMismatch(ValueError):
    pass


def backend_json(runtime, path, operations, body=None):
    started = time.monotonic()
    row = {'path': path, 'method': 'GET' if body is None else 'POST', 'status': 'unknown'}
    operations.append(row)
    try:
        req = request.Request(runtime.backend_url(path), data=None if body is None else encode(body),
                              headers={'Content-Type': 'application/json'})
        with request.urlopen(req, timeout=30) as response:
            raw = response.read(MAX_JSON + 1)
        if len(raw) > MAX_JSON:
            raise ValueError('fleet_backend_json_over_budget')
        result = json.loads(raw)
        row['status'] = 'ok'
        return result
    finally:
        row['duration_ms'] = round((time.monotonic()-started)*1000, 3)


def effective_profile(runtime, model, operations):
    props = backend_json(runtime, '/props', operations)
    slots = backend_json(runtime, '/slots', operations)
    card = runtime.config['models'][model]
    if (props.get('model_path') != card['artifact']['path'] or props.get('model_alias') != model
            or not isinstance(props.get('chat_template'), str) or not props['chat_template']):
        raise BindingMismatch('fleet_backend_identity_unverified')
    if (not isinstance(slots, list) or not slots or len(slots) != props.get('total_slots')
            or any(type(s.get('n_ctx')) is not int or s['n_ctx'] <= 0 for s in slots)):
        raise ValueError('fleet_slot_window_unavailable')
    profile = {'model': model, 'context_window': min(s['n_ctx'] for s in slots),
        'slot_windows': [s['n_ctx'] for s in slots],
        'template_sha256': hashlib.sha256(props['chat_template'].encode()).hexdigest(),
        'build_info': props.get('build_info'), 'artifact_sha256': card['artifact']['sha256'],
        'artifact_identity_source': 'qualified_registry_and_live_model_path_not_fresh_gguf_hash',
        'runtime_configuration_sha256': digest(card['runtime'])}
    return profile | {'fingerprint': digest(profile)}


def text_request(payload):
    if (not isinstance(payload, dict) or not isinstance(payload.get('model'), str) or not payload['model']
            or BINDING_FIELD in payload or len(encode(payload)) > 1024*1024
            or not isinstance(payload.get('messages'), list) or not payload['messages']):
        raise ValueError('fleet_count_requires_explicit_text_chat_request')
    for message in payload['messages']:
        if not isinstance(message, dict) or message.get('content') is not None and not isinstance(message['content'], str):
            raise ValueError('fleet_count_text_only')
    return payload


def count_request(runtime, requested, payload, operations):
    """Caller holds request_lock for selection, metadata, formatting and tokens."""
    text_request(payload)
    if payload['model'] != requested:
        raise ValueError('fleet_count_model_mismatch')
    model, _ = runtime.ensure_model(requested)
    profile = effective_profile(runtime, model, operations)
    wire = dict(payload, model=model)
    template = backend_json(runtime, '/apply-template', operations, wire)
    if not isinstance(template.get('prompt'), str):
        raise ValueError('fleet_template_prompt_unavailable')
    result = backend_json(runtime, '/tokenize', operations, {
        'content': template['prompt'], 'add_special': True, 'parse_special': True})
    tokens = result.get('tokens')
    if not isinstance(tokens, list) or any(type(token) is not int or token < 0 for token in tokens):
        raise ValueError('fleet_token_count_unavailable')
    binding = {'schema': SCHEMA, 'requested_model': requested, 'model': model,
               'request_sha256': digest(payload), 'fingerprint': profile['fingerprint']}
    return {'schema': SCHEMA, 'input_tokens': len(tokens), 'profile': profile, 'binding': binding,
            'method': 'selected_server_apply_template_tokenize', 'backend_operations': operations,
            'backend_operation_coverage': 'metadata_template_tokenizer_only_excludes_health_and_loading',
            'scope': 'text_input_tokens_not_generation_usage_or_cache_savings'}


def check_binding(runtime, requested, payload, binding, operations):
    text_request(payload)
    model, _ = runtime.ensure_model(requested)
    profile = effective_profile(runtime, model, operations)
    expected = {'schema': SCHEMA, 'requested_model': requested, 'model': model,
                'request_sha256': digest(payload), 'fingerprint': profile['fingerprint']}
    if binding != expected:
        raise BindingMismatch('fleet_count_binding_changed_recount_required')
    return profile
