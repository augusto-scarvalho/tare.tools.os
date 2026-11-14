"""A model of a known line has an identity without a roster record, only on its vendor's own route. A roster record
always decides; any other id keeps today's refusal, and no automatic role picks a model for its line."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from relay import native_catalog, native_routes, round_table_roster, turn_engine, work_audit
from relay.landing_policy import (LandingPolicyRejected, _identity_from_payload, attest_identity, read_identity,
                                  verify_identity_against_roster, write_identity)
from relay.model_choices import model_author
from test_cross_vendor_auditors import install, pool  # noqa: F401  (pool is a fixture)

# Codex's catalogue as measured on 2026-11-13, for expand(); levels do not matter to identity.
CODEX = [{'slug': s, 'visibility': 'list', 'supported_reasoning_levels': [{'effort': 'low'}, {'effort': 'high'}],
          'default_reasoning_level': 'low', 'context_window': 400000}
         for s in ('gpt-6.1-sol', 'gpt-6-astra', 'gpt-6.1-terra', 'gpt-6-sol', 'gpt-6-luna',
                   'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna')]
U = 'LandingPolicyRejected:IDENTITY_ROSTER_UNAVAILABLE'
# Identity (vendor, bias domain, model id) or refusal, and model_author, of every shipped deployment and CLI
# template, every AGY effort it realizes and Codex discovery's routes: printed by this file's __main__ run with
# relay/ at origin/main 6975894d.
BEFORE = {
    'agy-cli': [['Google', 'google/gemini', 'gemini-3.8-flash-high'], 'Google'],
    'agy-cli@high': [['Google', 'google/gemini', 'gemini-3.8-flash-high'], 'Google'],
    'agy-cli@low': [U, 'UNKNOWN'],
    'agy-cli@medium': [U, 'UNKNOWN'],
    'agy-gemini-4-argon-cli': [['Google', 'google/gemini', 'gemini-4-argon-high'], 'Google'],
    'agy-gemini-4-argon-cli@high': [['Google', 'google/gemini', 'gemini-4-argon-high'], 'Google'],
    'agy-gemini-4-argon-cli@low': [['Google', 'google/gemini', 'gemini-4-argon-low'], 'Google'],
    'agy-gemini-4-argon-cli@medium': [['Google', 'google/gemini', 'gemini-4-argon-medium'], 'Google'],
    'agy-gemini-4-hydrogen-cli': [['Google', 'google/gemini', 'gemini-4-hydrogen-high'], 'Google'],
    'agy-gemini-4-hydrogen-cli@high': [['Google', 'google/gemini', 'gemini-4-hydrogen-high'], 'Google'],
    'agy-gemini-4-hydrogen-cli@low': [['Google', 'google/gemini', 'gemini-4-hydrogen-low'], 'Google'],
    'agy-gemini-4-hydrogen-cli@medium': [['Google', 'google/gemini', 'gemini-4-hydrogen-medium'], 'Google'],
    'bedrock-glm': [['Z.ai', 'zai/glm-family', 'glm-5'], 'Z.ai'],
    'bedrock-opus': [['Anthropic', 'anthropic/opus', 'claude-opus-4.6'], 'Anthropic'],
    'bedrock-sonnet': [['Anthropic', 'anthropic/sonnet', 'claude-sonnet-4.6'], 'Anthropic'],
    'claude-api': [['Anthropic', 'anthropic/haiku', 'claude-haiku-5.5'], 'Anthropic'],
    'claude-api-fable-5-1': [['Anthropic', 'anthropic/fable', 'fable-5.1'], 'Anthropic'],
    'claude-api-fable-5-2': [['Anthropic', 'anthropic/fable', 'fable-5.2'], 'Anthropic'],
    'claude-api-opus-5-5': [['Anthropic', 'anthropic/opus', 'claude-opus-5.5'], 'Anthropic'],
    'claude-api-sonnet-5-5': [['Anthropic', 'anthropic/sonnet', 'claude-sonnet-5.5'], 'Anthropic'],
    'claude-cli': [['Anthropic', 'anthropic/opus', 'claude-opus-5.5'], 'Anthropic'],
    'claude-fable-5-1-cli': [['Anthropic', 'anthropic/fable', 'fable-5.1'], 'Anthropic'],
    'claude-fable-5-2-cli': [['Anthropic', 'anthropic/fable', 'fable-5.2'], 'Anthropic'],
    'claude-haiku-5-5-cli': [['Anthropic', 'anthropic/haiku', 'claude-haiku-5.5'], 'Anthropic'],
    'claude-opus-5-5-cli': [['Anthropic', 'anthropic/opus', 'claude-opus-5.5'], 'Anthropic'],
    'claude-sonnet-5-5-cli': [['Anthropic', 'anthropic/sonnet', 'claude-sonnet-5.5'], 'Anthropic'],
    'codex-cli': [['OpenAI', 'openai/gpt', 'gpt-6-astra'], 'OpenAI'],
    'codex-cli-gpt-5-5': [['OpenAI', 'openai/gpt', 'gpt-5.5'], 'OpenAI'],
    'codex-cli-gpt-5-6-luna': [['OpenAI', 'openai/gpt', 'gpt-5.6-luna'], 'OpenAI'],
    'codex-cli-gpt-5-6-sol': [['OpenAI', 'openai/gpt', 'gpt-5.6-sol'], 'OpenAI'],
    'codex-cli-gpt-5-6-terra': [['OpenAI', 'openai/gpt', 'gpt-5.6-terra'], 'OpenAI'],
    'codex-cli-gpt-6-1-luna': [U, 'UNKNOWN'],
    'codex-cli-gpt-6-1-sol': [['OpenAI', 'openai/gpt', 'gpt-6.1-sol'], 'OpenAI'],
    'codex-cli-gpt-6-luna': [['OpenAI', 'openai/gpt', 'gpt-6-luna'], 'OpenAI'],
    'fable-tc': [['Local', 'qwen/qwen-family', 'fable-tc'], 'Local'],
    'gemini-api': [['Google', 'google/gemini', 'gemini-4-argon'], 'Google'],
    'gemini-api-3-8-flash': [U, 'UNKNOWN'],
    'gemini-api-4-carbon': [['Google', 'google/gemini', 'gemini-4-carbon'], 'Google'],
    'gemini-api-4-hydrogen': [['Google', 'google/gemini', 'gemini-4-hydrogen'], 'Google'],
    'hauhaucs': [['Qwen', 'qwen/qwen-family', 'hauhaucs-qwen38'], 'Qwen'],
    'local-auto': [['Qwen', 'qwen/qwen-family', 'qwen-3.8-27b'], 'Qwen'],
    'openai-api': [['OpenAI', 'openai/gpt', 'gpt-6.1-sol'], 'OpenAI'],
    'openai-api-gpt-6-1-sol-pro': [['OpenAI', 'openai/gpt', 'gpt-6.1-sol'], 'OpenAI'],
    'openai-api-gpt-6-1-terra': [['OpenAI', 'openai/gpt', 'gpt-6.1-terra'], 'OpenAI'],
    'openai-api-gpt-6-astra': [['OpenAI', 'openai/gpt', 'gpt-6-astra'], 'OpenAI'],
    'openai-api-gpt-6-astra-pro': [['OpenAI', 'openai/gpt', 'gpt-6-astra'], 'OpenAI'],
    'openai-api-gpt-6-luna': [['OpenAI', 'openai/gpt', 'gpt-6-luna'], 'OpenAI'],
    'opencode-sub-claude-haiku-5-5': [['Anthropic', 'anthropic/haiku', 'claude-haiku-5.5'], 'Anthropic'],
    'opencode-sub-deepseek-v4-1-flash': [['DeepSeek', 'deepseek/deepseek', 'deepseek-v4.1-flash'], 'DeepSeek'],
    'opencode-sub-gemini-4-argon': [['Google', 'google/gemini', 'gemini-4-argon'], 'Google'],
    'opencode-sub-glm-5-3-flash': [['Z.ai', 'zai/glm-family', 'glm-5.3-flash'], 'Z.ai'],
    'opencode-sub-gpt-6-luna': [['OpenAI', 'openai/gpt', 'gpt-6-luna'], 'OpenAI'],
    'opencode-sub-grok-4-7': [['xAI', 'xai/grok', 'grok-4.7'], 'xAI'],
    'opencode-sub-kimi-k3': [['Moonshot AI', 'kimi/kimi-k3', 'kimi-k3'], 'Moonshot AI'],
    'opencode-sub-mimo-v2-6-flash': [['Xiaomi', 'xiaomi/mimo-family', 'mimo-v2.6-flash'], 'Xiaomi'],
    'openrouter-deepseek-v4-1-flash': [['DeepSeek', 'deepseek/deepseek', 'deepseek/deepseek-v4.1-flash'], 'DeepSeek'],
    'openrouter-deepseek-v4-1-flash-1017': [['DeepSeek', 'deepseek/deepseek', 'deepseek/deepseek-v4.1-flash-1017'], 'DeepSeek'],
    'openrouter-glm-5-3': [['Z.ai', 'zai/glm-family', 'z-ai/glm-5.3'], 'Z.ai'],
    'openrouter-glm-5-3-flash': [['Z.ai', 'zai/glm-family', 'z-ai/glm-5.3-flash'], 'Z.ai'],
    'openrouter-grok-4-7': [['xAI', 'xai/grok', 'x-ai/grok-4.7'], 'xAI'],
    'openrouter-kimi-k3-1-pro': [['Moonshot AI', 'kimi/kimi-k3', 'moonshotai/kimi-k3.1-pro'], 'Moonshot AI'],
    'openrouter-ling-3-1-flash': [['inclusionAI', 'inclusionai/ling-family', 'inclusionai/ling-3.1-flash'], 'inclusionAI'],
    'openrouter-mimo-v2-6-pro': [['Xiaomi', 'xiaomi/mimo-family', 'xiaomi/mimo-v2.6-pro'], 'Xiaomi'],
    'openrouter-mistral-large-4': [['Mistral AI', 'mistral/mistral', 'mistralai/mistral-large-4-0'], 'Mistral AI'],
    'openrouter-muse-spark-1-3-contributor': [['Meta', 'meta/muse-family', 'meta/muse-spark-1.3-contributor'], 'Meta'],
    'openrouter-muse-wonder': [['Meta', 'meta/muse-family', 'meta/muse-wonder'], 'Meta'],
    'openrouter-qwen3-8-omni-flash': [['Qwen', 'qwen/qwen-family', 'qwen/qwen3.8-omni-flash'], 'Qwen'],
    'openrouter-step-5-preview': [['StepFun', 'stepfun/step-family', 'stepfun/step-5-preview'], 'StepFun'],
    'qwen36-moe': [['Qwen', 'qwen/qwen-family', 'qwen36-moe'], 'Qwen'],
    'qwen38-gsq': [['Qwen', 'qwen/qwen-family', 'qwen-3.8-27b'], 'Qwen'],
    'swift-next': ["KeyError:'model'", 'UNKNOWN'],
    'swift27b': ["KeyError:'model'", 'UNKNOWN'],
    'tare-auto': [['Qwen', 'qwen/qwen-family', 'qwen-3.8-27b'], 'Qwen'],
    'expand:codex-cli': [['OpenAI', 'openai/gpt', 'gpt-6-astra'], 'OpenAI'],
    'expand:codex-cli-gpt-5-6-luna': [['OpenAI', 'openai/gpt', 'gpt-5.6-luna'], 'OpenAI'],
    'expand:codex-cli-gpt-5-6-sol': [['OpenAI', 'openai/gpt', 'gpt-5.6-sol'], 'OpenAI'],
    'expand:codex-cli-gpt-5-6-terra': [['OpenAI', 'openai/gpt', 'gpt-5.6-terra'], 'OpenAI'],
    'expand:codex-cli-gpt-6-1-sol': [['OpenAI', 'openai/gpt', 'gpt-6.1-sol'], 'OpenAI'],
    'expand:codex-cli-gpt-6-1-terra': [['OpenAI', 'openai/gpt', 'gpt-6.1-terra'], 'OpenAI'],
    'expand:codex-cli-gpt-6-luna': [['OpenAI', 'openai/gpt', 'gpt-6-luna'], 'OpenAI'],
    'expand:codex-cli-gpt-6-sol': [['OpenAI', 'openai/gpt', 'gpt-6-sol'], 'OpenAI'],
}
# What this change gives the four rows above that had no record: their line's identity, marked with the line.
CHANGED = {
    'agy-cli@low': [['Google', 'google/gemini', 'gemini-3.8-flash-low', 'gemini-flash'], 'Google'],
    'agy-cli@medium': [['Google', 'google/gemini', 'gemini-3.8-flash-medium', 'gemini-flash'], 'Google'],
    'codex-cli-gpt-6-1-luna': [['OpenAI', 'openai/gpt', 'gpt-6.1-luna', 'gpt-luna'], 'OpenAI'],
    'gemini-api-3-8-flash': [['Google', 'google/gemini', 'gemini-3.8-flash', 'gemini-flash'], 'Google'],
}
# The AGY Claude templates, added later: their high level has the roster record their role row needs (a row must name
# one), the other levels their line's identity.
AGY_CLAUDE = {
    'agy-claude-opus-5-5-cli': [['Anthropic', 'anthropic/opus', 'claude-opus-5-5-high'], 'Anthropic'],
    'agy-claude-opus-5-5-cli@high': [['Anthropic', 'anthropic/opus', 'claude-opus-5-5-high'], 'Anthropic'],
    'agy-claude-opus-5-5-cli@low': [['Anthropic', 'anthropic/opus', 'claude-opus-5-5-low', 'claude-opus'], 'Anthropic'],
    'agy-claude-opus-5-5-cli@medium': [['Anthropic', 'anthropic/opus', 'claude-opus-5-5-medium', 'claude-opus'], 'Anthropic'],
    'agy-claude-sonnet-5-5-cli': [['Anthropic', 'anthropic/sonnet', 'claude-sonnet-5-5-high'], 'Anthropic'],
    'agy-claude-sonnet-5-5-cli@high': [['Anthropic', 'anthropic/sonnet', 'claude-sonnet-5-5-high'], 'Anthropic'],
    'agy-claude-sonnet-5-5-cli@low': [['Anthropic', 'anthropic/sonnet', 'claude-sonnet-5-5-low', 'claude-sonnet'], 'Anthropic'],
    'agy-claude-sonnet-5-5-cli@medium': [['Anthropic', 'anthropic/sonnet', 'claude-sonnet-5-5-medium', 'claude-sonnet'], 'Anthropic'],
}


def realized():
    from relay.cli_discovery import TEMPLATES, deployment
    from relay.vendor_models import expand
    for name, value, _ in native_catalog.offline_deployments(include_unconfigured=True):
        yield name, value
        for level, model in sorted((value.get('native_reasoning_models') or {}).items()):
            variant = deepcopy(value)  # as model_choices.realization sets an AGY level
            variant['native_profile']['model'] = variant['model'] = model
            yield name+'@'+level, variant
    # An absolute path on every platform: the profile check refuses a relative executable, and a Windows path is
    # relative on POSIX. The file need not exist; identity does not read it.
    base = deployment(json.loads(TEMPLATES.read_text(encoding='utf-8'))['codex-cli'],
                      str(Path(__file__).resolve().parent/'fixture-codex.exe'))
    for name, value in sorted(expand({'codex-cli': base}, CODEX).items()):
        yield 'expand:'+name, native_catalog.canonical_identity(value)


def shown(values):
    rows = {}
    for name, value in values:
        try:
            got = work_audit.identity(value, 'implementer')
            identity = [got.vendor, got.bias_domain, got.model_id, *([got.payload['line']] if 'line' in got.payload else [])]
        except (ValueError, KeyError, TypeError, OSError) as exc:
            identity = type(exc).__name__+':'+str(exc)
        rows[name] = [identity, model_author(value)]
    return rows


def route(endpoint, model, name='fixture'):
    return {'id': name, 'model': model, 'native_profile': {'model': model, 'endpoint': endpoint}}


def test_every_shipped_identity_is_unchanged_except_four_ids_with_no_record_that_gain_their_line():
    assert all(BEFORE[name] == [U, 'UNKNOWN'] for name in CHANGED)
    assert shown(realized()) == {**BEFORE, **CHANGED, **AGY_CLAUDE}


def test_what_the_four_gain_is_audit_never_an_automatic_role(pool):
    rows = {row.get('native_catalog', row['id']) for row in native_routes.configured()['deployments']}
    named = set()
    for value in turn_engine.DEFAULT_POLICY.values():
        for item in value if isinstance(value, list) else []:
            named.update(item if isinstance(item, list) else [item])
    # No role row or default names them; agy-cli's row and defaults are its high level, which keeps its record
    # (the table above), so ranking it is unchanged.
    assert not {'codex-cli-gpt-6-1-luna', 'gemini-api-3-8-flash'} & (rows | named)
    # Gemini 3.8 Flash on the Gemini API (an approved coordinator) had no auditor; now another line reviews it.
    producer = native_catalog.select('gemini-api-3-8-flash')
    reviewers, receipt = native_routes.reviewers(producer)
    assert reviewers and receipt['producer_sha256']
    assert all(work_audit.identity(r, 'auditor').bias_domain != 'google/gemini' for r in reviewers)


NEW = {  # endpoint, wire id: vendor, bias domain, line
    ('cli://claude', 'claude-fable-5-3'): ('Anthropic', 'anthropic/fable', 'claude-fable'),
    ('https://api.anthropic.com/v1', 'claude-fable-5-3'): ('Anthropic', 'anthropic/fable', 'claude-fable'),
    ('cli://codex', 'gpt-6.2-terra'): ('OpenAI', 'openai/gpt', 'gpt-terra'),
    ('https://api.openai.com/v1', 'gpt-6.2-terra'): ('OpenAI', 'openai/gpt', 'gpt-terra'),
    ('https://chatgpt.com/backend-api/codex', 'gpt-6.2-terra'): ('OpenAI', 'openai/gpt', 'gpt-terra'),
    ('https://generativelanguage.googleapis.com/v1beta/openai', 'gemini-4.1-argon'): ('Google', 'google/gemini', 'gemini-argon'),
    ('cli://agy', 'gemini-4.1-argon-high'): ('Google', 'google/gemini', 'gemini-argon'),
    ('cli://agy', 'claude-opus-5-5-medium'): ('Anthropic', 'anthropic/opus', 'claude-opus'),  # -high has a record
}
REFUSED = [
    # No line: an unknown line or family, a non-chat model, a dated snapshot, an alias with no version.
    ('https://api.openai.com/v1', 'gpt-7-nova'), ('cli://claude', 'claude-mythos-1'),
    ('https://generativelanguage.googleapis.com/v1beta', 'gemini-5-neon'),
    ('https://api.openai.com/v1', 'gpt-realtime-2.1'), ('https://api.openai.com/v1', 'gpt-image-2.5-flare'),
    ('https://generativelanguage.googleapis.com/v1beta', 'gemini-3.8-flash-tts'),
    ('cli://agy', 'gpt-oss-120b-medium'), ('cli://agy', 'gemini-pro-orchestrator-g1-pro'),
    ('https://api.anthropic.com/v1', 'claude-haiku-4-5-20251001'), ('https://api.openai.com/v1', 'gpt-6.1-terra-2026-11-01'),
    ('cli://claude', 'claude-opus'),
    # A reseller's id, or a first-party spelling on a reseller or a local gateway: the mirror's job, not this one.
    ('https://openrouter.ai/api/v1', 'anthropic/claude-fable-5.2'), ('https://openrouter.ai/api/v1', 'claude-fable-5-3'),
    ('https://bedrock-runtime.us-east-1.amazonaws.com', 'us.anthropic.claude-fable-5-2'),
    ('https://opencode.ai/inference/go/anthropic/v1', 'claude-fable-5-3'),
    ('https://opencode.ai/inference/go/openai/v1', 'gpt-6.2-terra'),
    ('http://127.0.0.1:8080/v1', 'gpt-6.2-terra'),
    # Another vendor's route, a look-alike host, plain HTTP, an effort suffix off AGY.
    ('cli://claude', 'gpt-6.2-terra'), ('cli://codex', 'claude-fable-5-3'), ('cli://agy', 'gpt-6.2-terra-high'),
    ('https://api.openai.com/v1', 'gemini-4.1-argon'), ('https://api.openai.com.example.test/v1', 'gpt-6.2-terra'),
    ('http://api.openai.com/v1', 'gpt-6.2-terra'), ('cli://claude', 'claude-opus-5-5-medium'),
    ('https://generativelanguage.googleapis.com/v1beta/openai', 'gemini-4.1-argon-high'),
]


@pytest.mark.parametrize('endpoint,model', list(NEW))
def test_a_new_version_of_a_known_line_gets_its_line_identity_on_its_vendors_own_route(endpoint, model):
    got = work_audit.identity(route(endpoint, model), 'implementer')
    assert (got.vendor, got.bias_domain, got.payload['line']) == NEW[endpoint, model]
    assert got.model_id == got.payload['runtime_model'] == model
    assert model_author(route(endpoint, model)) == NEW[endpoint, model][0]


@pytest.mark.parametrize('endpoint,model', REFUSED)
def test_anything_else_stays_unknown_as_today(endpoint, model):
    with pytest.raises(LandingPolicyRejected, match='IDENTITY_ROSTER_UNAVAILABLE'):
        work_audit.identity(route(endpoint, model), 'implementer')
    assert model_author(route(endpoint, model)) == 'UNKNOWN'


def test_a_record_always_decides_and_only_the_wire_id_on_a_native_route_is_derived(tmp_path):
    roster = native_catalog.roster_path()
    args = dict(roster_path=roster, agent_id='a', role='implementer', runtime_key='native')
    # A record names the wire id (claude-opus-5.5 runs claude-opus-5-5): that record's id is the only identity.
    with pytest.raises(LandingPolicyRejected):
        attest_identity(model_id='claude-opus-5-5', runtime_model='claude-opus-5-5', endpoint='cli://claude', **args)
    recorded = work_audit.identity(native_catalog.select('claude-api-fable-5-2'), 'implementer')
    assert recorded.model_id == 'fable-5.2' and 'line' not in recorded.payload
    # A model id other than the wire id, a caller that names no route, a runtime other than native: refused.
    for extra in (dict(model_id='fable-5.3', runtime_model='claude-fable-5-3', endpoint='cli://claude'),
                  dict(model_id='claude-fable-5-3', runtime_model='claude-fable-5-3'),
                  dict(args, runtime_key='claude', model_id='claude-fable-5-3', runtime_model='claude-fable-5-3',
                       endpoint='cli://claude')):
        with pytest.raises(LandingPolicyRejected):
            attest_identity(**{**args, **extra})
    # A record added for the new version wins over its line, whatever it says.
    data = json.loads(roster.read_text(encoding='utf-8'))
    data['models']['fable-5.3'] = {'vendor': 'Anthropic', 'bias_domain': 'anthropic/fable-next', 'display_name': 'x',
                                   'runtime': {'native': 'claude-fable-5-3'}}
    (tmp_path/'roster.json').write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(LandingPolicyRejected):
        attest_identity(**{**args, 'roster_path': tmp_path/'roster.json'}, model_id='claude-fable-5-3',
                        runtime_model='claude-fable-5-3', endpoint='cli://claude')
    got = attest_identity(**{**args, 'roster_path': tmp_path/'roster.json'}, model_id='fable-5.3',
                          runtime_model='claude-fable-5-3', endpoint='cli://claude')
    assert got.bias_domain == 'anthropic/fable-next' and 'line' not in got.payload


def test_a_derived_receipt_round_trips_and_is_refused_when_the_roster_or_its_line_changes(tmp_path):
    roster = tmp_path/'roster.json'
    roster.write_bytes(native_catalog.roster_path().read_bytes())
    got = attest_identity(roster_path=roster, agent_id='a', role='auditor', model_id='claude-fable-5-3',
                          runtime_key='native', runtime_model='claude-fable-5-3', endpoint='cli://claude')
    write_identity(tmp_path/'AUDITOR_IDENTITY.json', got)
    saved = read_identity(tmp_path/'AUDITOR_IDENTITY.json')
    assert saved == got and saved.payload['line'] == 'claude-fable'
    verify_identity_against_roster(saved, roster)
    forged = _identity_from_payload({**got.payload, 'line': 'claude-opus'})
    with pytest.raises(LandingPolicyRejected, match='IDENTITY_ROSTER_UNAVAILABLE'):
        verify_identity_against_roster(forged, roster)
    roster.write_text(roster.read_text(encoding='utf-8') + '\n', encoding='utf-8')
    with pytest.raises(LandingPolicyRejected, match='IDENTITY_ROSTER_STALE'):
        verify_identity_against_roster(saved, roster)


def test_independence_holds_for_derived_identities_as_for_recorded_ones():
    shipped = {name: value for name, value, _ in native_catalog.offline_deployments(include_unconfigured=True)}
    fable_5_3, agy_opus = route('cli://claude', 'claude-fable-5-3', 'f'), route('cli://agy', 'claude-opus-5-5-medium', 'o')
    cases = [  # implementer, auditor: independent, cross-vendor
        (native_catalog.select('claude-api-fable-5-2'), fable_5_3, False, False),  # same bias domain
        (fable_5_3, fable_5_3, False, False),  # same model
        (shipped['claude-opus-5-5-cli'], agy_opus, False, False),  # Opus on AGY, Opus on Claude Code
        (fable_5_3, agy_opus, True, False),  # Opus reviews Fable: another bias domain of the same vendor
        (fable_5_3, native_catalog.select('claude-api-opus-5-5'), True, False),
        (route('cli://codex', 'gpt-6.2-terra'), fable_5_3, True, True),
        (route('cli://agy', 'gemini-4.1-argon-high'), native_catalog.select('gemini-api'), False, False),
    ]
    for implementer, auditor, independent, cross in cases:
        (row,) = work_audit.prepare(implementer, [auditor], 'optional')['routes']
        assert (row['eligible'], row['cross_vendor']) == (independent, cross), (implementer['model'], auditor['model'])


def test_a_derived_implementer_gets_independent_auditors_another_vendor_first(pool):
    install(pool, 'codex-cli-gpt-6-2-terra', 'cli://codex', 'gpt-6.2-terra')
    producer = native_catalog.select('codex-cli-gpt-6-2-terra')
    assert work_audit.identity(producer, 'implementer').payload['line'] == 'gpt-terra'
    for role in ('auditor', 'plan-auditor'):
        reviewers, _ = native_routes.reviewers(producer, role)
        critics = [work_audit.identity(r, 'auditor') for r in reviewers]
        assert critics and critics[0].vendor != 'OpenAI'
        assert all(c.bias_domain != 'openai/gpt' for c in critics)
    # A configured auditor list: its own model and another GPT line are refused, Claude Opus reviews.
    install(pool, 'codex-cli', 'cli://codex', 'gpt-6-astra')
    install(pool, 'claude-opus-5-5-cli', 'cli://claude', 'claude-opus-5-5')
    picked = turn_engine.pick_auditors(producer, ['codex-cli-gpt-6-2-terra', 'codex-cli', 'claude-opus-5-5-cli'],
                                       probe=False)
    assert [name for name, _ in picked] == ['claude-opus-5-5-cli']


def test_no_automatic_role_picks_a_route_for_its_line_alone(pool, monkeypatch):
    new = {'claude-fable-5-3-cli': ('cli://claude', 'claude-fable-5-3'), 'codex-cli-gpt-6-2-terra': ('cli://codex', 'gpt-6.2-terra'),
           'agy-gemini-4-1-argon-cli': ('cli://agy', 'gemini-4.1-argon-high')}
    for name, (endpoint, model) in new.items():
        install(pool, name, endpoint, model)
        assert 'line' in work_audit.identity(native_catalog.select(name), 'implementer').payload
    producer = native_catalog.select('codex-cli-gpt-6-2-terra')
    for role in ('planner', 'plan-auditor', 'auditor', 'implementer', 'explorer'):
        chosen, selection = native_routes.ranked(role, producer if 'auditor' in role else None)
        assert chosen, role
        assert not {d['id'] for d in chosen} & set(new)
        assert not any('line' in work_audit.identity(d, 'auditor').payload for d in chosen)
        assert not {c['deployment'] for c in selection['candidates']} & set(new)
    # Automatic roles come only from role rows, and a row must name a roster record: one naming the new model is
    # refused, and one that points a recorded model at the new profile is not that model.
    roster = json.loads(native_catalog.roster_path().read_text(encoding='utf-8'))
    row = {'id': 'claude-fable-5-3-cli', 'native_catalog': 'claude-fable-5-3-cli', 'model': 'claude-fable-5-3',
           'runner': 'native-http', 'provider': 'Anthropic', 'roles': ['auditor'], 'kind': 'primary',
           'cost_tier': 'flat', 'quota_domain': 'q', 'endpoint_domain': 'e'}
    with pytest.raises(ValueError, match='unregistered model'):
        round_table_roster.validate_role_deployments({'schema_version': 1, 'deployments': [row]}, roster)
    monkeypatch.setattr(native_routes, 'configured', lambda: {'schema_version': 1, 'deployments': [dict(row, model='fable-5.2')]})
    chosen, selection = native_routes.ranked('auditor', producer)
    assert not chosen and selection['candidates'][0]['reason'] == 'PROFILE_OR_IDENTITY_INVALID'


if __name__ == '__main__':
    # Prints BEFORE as the checked-out relay/ shows it, with empty scratch storage.
    import os
    import sys
    import tempfile
    sys.stdout.reconfigure(encoding='utf-8')
    scratch = tempfile.mkdtemp()
    os.environ.update(TARE_STORAGE_ROOT=scratch, TARE_PROFILE_DIR=scratch, CODEX_HOME=scratch,
                      TARE_LOCAL_GATEWAY='http://127.0.0.1:8080',
                      TARE_ENVIRONMENT_STORE=os.path.join(scratch, 'environment.json'))
    for name, row in shown(realized()).items():
        print(f'    {name!r}: {row!r},'.replace(repr(U), 'U'))
