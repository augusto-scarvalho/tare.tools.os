"""Heads-up and explorer choose from the role table by class and value, never from hand-written defaults;
owner overrides, local GPU avoidance, and max_usd consent still hold (owner, 2026-11-19)."""
import json

from relay import (challenger_variants, cli_discovery, delegation, environment,
                   heads_up, model_signals, native_routes, turn_engine)
from test_cross_vendor_auditors import pool  # noqa: F401
from test_local_model_grants import GEMMA, gateway, why


def _install_clis(pool, monkeypatch, names):
    templates = json.loads(cli_discovery.TEMPLATES.read_text(encoding='utf-8'))
    pool.mkdir(exist_ok=True)
    for name in names:
        (pool / (name + '.json')).write_text(
            json.dumps(cli_discovery.deployment(templates[name], __file__)), encoding='utf-8')
    monkeypatch.setattr(environment, 'older_models', lambda: set())


def test_wanted_stays_configurable_roles_only():
    assert native_routes.wanted('explorer') is None
    assert native_routes.wanted('heads_up') is None
    assert 'explorer' not in native_routes.WANTED and 'heads_up' not in native_routes.WANTED


def test_heads_up_ranks_middling_before_frontier_and_paid_and_excludes_small_or_older(pool, monkeypatch):
    _install_clis(pool, monkeypatch, (
        'claude-sonnet-5-5-cli',
        'codex-cli-gpt-6-1-sol',
        'claude-opus-5-5-cli',
        'codex-cli',
        'agy-cli',
        'codex-cli-gpt-6-luna',
        'claude-haiku-5-5-cli',
        'codex-cli-gpt-5-6-sol',
    ))
    monkeypatch.setenv('OPENAI_API_KEY', 'fixture')
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'fixture')
    monkeypatch.delenv('GEMINI_API_KEY', raising=False)
    values = {'openai-api': 200.0, 'claude-api-opus-5-5': 50.0}
    monkeypatch.setattr(model_signals, 'task', lambda d: {'value': values[d['id']]} if d['id'] in values else None)

    names = turn_engine.heads_up_routes()
    for excluded in ('agy-cli', 'codex-cli-gpt-6-luna', 'claude-haiku-5-5-cli', 'codex-cli-gpt-5-6-sol', 'local-qwen'):
        assert excluded not in names
    assert names[:3] == ['claude-sonnet-5-5-cli', 'claude-opus-5-5-cli', 'codex-cli-gpt-6-1-sol']
    assert names.index('codex-cli-gpt-6-1-sol') < names.index('codex-cli')
    assert names.index('codex-cli') < names.index('openai-api')
    assert names.index('openai-api') < names.index('claude-api-opus-5-5')


def test_heads_up_write_executes_ranked_and_inline_and_allows_explicit_local_grant(pool, tmp_path, monkeypatch):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path / 'storage'))
    monkeypatch.setattr(heads_up, '_token', None)
    _install_clis(pool, monkeypatch, ('claude-sonnet-5-5-cli', 'codex-cli-gpt-6-1-sol'))
    calls = []

    def fake_generate(deployment, model, prompt, timeout, **kwargs):
        calls.append((deployment['id'], model))
        return True, '- Ranked note.', {'attempt_id': 'a1', 'usage': None, 'cost': {}}

    monkeypatch.setattr('relay.native_generation.generate_text', fake_generate)
    notes, attempts = heads_up.write('{}', [])
    assert notes == ['Ranked note.'] and calls == [('claude-sonnet-5-5-cli', 'claude-sonnet-5-5')]
    assert attempts[0]['route'] == 'claude-sonnet-5-5-cli'

    turn_engine.save_policy({'heads_up': [['codex-cli-gpt-6-1-sol']]})
    notes, _ = heads_up.write('{}', [])
    assert notes == ['Ranked note.'] and calls[-1][0] == 'codex-cli-gpt-6-1-sol'
    turn_engine.reset_policy()

    assert environment.main(['--auto', 'heads_up:*@*=off']) == 0
    assert heads_up.write('{}', []) == ([], [])

    # Explicit local grant unlocks a local model for heads_up without automatic admission.
    gateway('qwen38-gsq', 'gemma-4')
    assert why(GEMMA, 'heads_up') == 'ROLE_NOT_CONFIGURED'
    assert environment.main(['--auto', f'heads_up:{GEMMA}=on']) == 0
    assert why(GEMMA, 'heads_up') is None

    # Inline middling CLI row in TARE_ROLE_DEPLOYMENTS executes directly through write().
    assert environment.main(['--auto', 'heads_up:*@*=default']) == 0
    templates = json.loads(cli_discovery.TEMPLATES.read_text(encoding='utf-8'))
    row = cli_discovery.deployment(templates['codex-cli-gpt-6-1-sol'], __file__)
    row.update(id='inline-heads-up', model='gpt-6.1-sol', roles=['implementer'], kind='primary',
               cost_tier='flat', quota_domain='openai-cli', endpoint_domain='openai-cli')
    roles_file = tmp_path / 'roles.json'
    roles_file.write_text(json.dumps({'schema_version': 1, 'deployments': [row]}), encoding='utf-8')
    monkeypatch.setenv('TARE_ROLE_DEPLOYMENTS', str(roles_file))
    calls.clear()
    notes, _ = heads_up.write('{}', [])
    assert notes == ['Ranked note.'] and calls == [('inline-heads-up', 'gpt-6.1-sol')]


def test_explorer_auto_default_ranks_selects_overflow_respects_gpu_and_max_usd(pool, tmp_path, monkeypatch):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path / 'storage'))
    monkeypatch.delenv('TARE_DELEGATION_POLICY', raising=False)
    monkeypatch.delenv(turn_engine.AVOID_LOCAL_GPU, raising=False)
    assert delegation.DEFAULT['profile'] == 'auto'
    delegation.configure(dict(delegation.DEFAULT, enabled=True))
    assert ('/delegation profile', 'auto') not in environment.configured_routes()

    monkeypatch.setattr(native_routes, 'credential_available', lambda key: False)
    gateway('qwen38-gsq')
    _install_clis(pool, monkeypatch, ('claude-sonnet-5-5-cli',))
    frozen = delegation.snapshot('s1', 'implementer')
    assert frozen['selection'] == 'AUTO_EXPLORER'
    assert frozen['variant']['accounting'] == 'local'
    assert frozen['variant']['deployment']['id'] == 'fleet-qwen38-gsq'
    assert frozen['variant']['label'] == 'local-qwen'
    assert frozen['overflow']['deployment']['id'] == 'claude-sonnet-5-5-cli'

    # Avoid-local-GPU skips the local explorer and promotes the ranked hosted route.
    monkeypatch.setattr(turn_engine, 'avoid_local_gpu', lambda: True)
    avoided = delegation.snapshot('s2', 'implementer')
    assert avoided['selection'] == 'AUTO_EXPLORER'
    assert avoided['variant']['deployment']['id'] == 'claude-sonnet-5-5-cli'
    assert 'overflow' not in avoided

    # Local swapping parent also skips mismatched local explorer.
    monkeypatch.setattr(turn_engine, 'avoid_local_gpu', lambda: False)
    local_other = {'native_profile': {'protocol': 'openai-chat', 'endpoint': 'http://127.0.0.1:8080/v1',
                                      'model': 'other-local'}}
    assert delegation.snapshot('s3', 'implementer', local_other)['variant']['deployment']['id'] == 'claude-sonnet-5-5-cli'

    # Zero max_usd refuses metered API routes even when no free/quota explorer exists; positive max_usd allows them.
    (pool / 'claude-sonnet-5-5-cli.json').unlink()
    setup = environment.read()
    setup['local_models']['use'] = False
    environment.apply(setup)
    monkeypatch.setattr(native_routes, 'local_models', lambda: {})
    monkeypatch.setattr(native_routes, 'credential_available', lambda key: key == 'OPENAI_API_KEY')
    assert delegation.snapshot('s4', 'implementer') is None
    delegation.configure(dict(delegation.DEFAULT, enabled=True, max_usd='1.50'))
    paid = delegation.snapshot('s5', 'implementer')
    assert paid['selection'] == 'AUTO_EXPLORER' and paid['variant']['accounting'] == 'usd'

    # Explicit non-auto profile keeps CONFIGURED_EXPLORER and pins keep EXACT_TEMPORARY_PIN.
    delegation.configure(dict(delegation.DEFAULT, enabled=True, profile='openai-api', max_usd='1.50'))
    explicit = delegation.snapshot('s6', 'implementer')
    assert explicit['selection'] == 'CONFIGURED_EXPLORER'
    monkeypatch.setenv('OPENAI_API_KEY', 'fixture')
    monkeypatch.setenv('TARE_SESSION_SCOPE', json.dumps({'pins': [['explorer', 'openai/openai-api@high']]}))
    pinned = delegation.snapshot('s7', 'implementer')
    assert pinned['selection'] == 'EXACT_TEMPORARY_PIN'
    monkeypatch.delenv('TARE_SESSION_SCOPE', raising=False)

    # Finding 1 & 2 regressions: ranked constraints/efforts are retained and configured overflow survives GPU skip.
    monkeypatch.setattr(native_routes, 'credential_available', lambda key: key == 'OPENAI_API_KEY')
    monkeypatch.setenv('TARE_SESSION_SCOPE', json.dumps({'restrictions': [['api', 'openai/openai-api@low']]}))
    delegation.configure(dict(delegation.DEFAULT, enabled=True, max_usd='1.50'))
    low_constrained = delegation.snapshot('s8', 'implementer')
    assert low_constrained['selection'] == 'AUTO_EXPLORER'
    assert low_constrained['variant']['deployment']['id'] == 'openai-api'
    assert json.loads(low_constrained['variant']['deployment']['native_profile']['parameters_json']) == {
        'reasoning': {'effort': 'low'}}
    monkeypatch.delenv('TARE_SESSION_SCOPE', raising=False)

    gateway('qwen38-gsq')
    monkeypatch.setattr(native_routes, 'credential_available', lambda key: False)
    monkeypatch.setattr(turn_engine, 'avoid_local_gpu', lambda: True)
    delegation.configure(dict(delegation.DEFAULT, enabled=True, overflow='openrouter-kimi-k3-1-pro', max_usd='1.50'))
    overflowed = delegation.snapshot('s9', 'implementer')
    assert overflowed['selection'] == 'OVERFLOW_EXPLORER'
    assert overflowed['variant']['deployment']['id'] == 'openrouter-kimi-k3-1-pro'


def test_heads_up_granted_local_respects_gpu_avoidance_and_lifts_default_effort(pool, tmp_path, monkeypatch):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path / 'storage'))
    monkeypatch.setattr(heads_up, '_token', None)
    gateway('qwen38-gsq')
    assert environment.main(['--auto', 'heads_up:*@*=off']) == 0
    assert environment.main(['--auto', 'heads_up:local-qwen=on']) == 0
    calls = []
    monkeypatch.setattr(
        'relay.native_generation.generate_text',
        lambda dep, *a, **kw: calls.append((
            dep['id'],
            json.loads(dep['native_profile']['parameters_json']).get('chat_template_kwargs', {}).get('reasoning_effort')
        )) or (True, '- Local heads-up.', {'attempt_id': 'a1', 'usage': None, 'cost': {}}),
    )
    monkeypatch.setattr(turn_engine, 'avoid_local_gpu', lambda: True)
    assert heads_up.write('{}', []) == ([], []) and calls == []

    monkeypatch.setattr(turn_engine, 'avoid_local_gpu', lambda: False)
    monkeypatch.setattr(turn_engine, 'local_gpu_busy', lambda dep: None)
    notes, _ = heads_up.write('{}', [])
    assert notes == ['Local heads-up.'] and calls == [('fleet-qwen38-gsq', 'xhigh')]
