"""Vision and researcher choose from the role table by class and value, never from hand-written route lists; a list
the owner saved still decides (owner, 2026-11-19)."""
import json

from relay import cli_discovery, environment, model_signals, native_routes, provider_quota, turn_engine, web_research
from test_cross_vendor_auditors import install, pool  # noqa: F401
from test_derived_role_rows import SOL, discovered
from test_local_model_grants import GEMMA, gateway, routes, why


def _install_vision_and_research_clis(pool, monkeypatch):
    templates = json.loads(cli_discovery.TEMPLATES.read_text(encoding='utf-8'))
    pool.mkdir(exist_ok=True)
    for name in ('agy-cli', 'codex-cli-gpt-6-luna', 'claude-haiku-5-5-cli',
                 'claude-sonnet-5-5-cli', 'codex-cli', 'codex-cli-gpt-5-5'):
        (pool / (name + '.json')).write_text(
            json.dumps(cli_discovery.deployment(templates[name], __file__)), encoding='utf-8')
    install(pool, 'codex-native-gpt-6-luna', 'cli://codex', 'gpt-6-luna')
    monkeypatch.setattr(environment, 'older_models', lambda: set())


def test_wanted_remains_for_configurable_roles_only():
    assert native_routes.wanted('researcher') is None
    assert native_routes.wanted('vision') is None
    assert 'researcher' not in native_routes.WANTED and 'vision' not in native_routes.WANTED


def test_vision_ranks_clis_by_class_and_excludes_api_or_local_routes(pool, monkeypatch):
    monkeypatch.setenv('OPENROUTER_API_KEY', 'fixture')
    _install_vision_and_research_clis(pool, monkeypatch)
    names = turn_engine.vision_routes()
    assert names == [
        'agy-cli',
        'codex-cli-gpt-6-luna',
        'claude-haiku-5-5-cli',
        'claude-sonnet-5-5-cli',
        'codex-cli',
        'codex-cli-gpt-5-5',
    ]
    for excluded in ('openai-api', 'claude-api-opus-5-5', 'gemini-api', 'openrouter-glm-5-3',
                     'local-qwen', 'codex-native-gpt-6-luna'):
        assert excluded not in names


def test_vision_respects_routing_profile_surplus_older_and_saved_list(pool, monkeypatch):
    _install_vision_and_research_clis(pool, monkeypatch)
    monkeypatch.setattr(
        provider_quota,
        'surplus',
        lambda d, now=None, cache=None: (
            {'lead': 0.8, 'remaining': 0.9, 'time_left': 0.05, 'resets_at': 1000.0, 'allowance': 'codex weekly'}
            if d['id'] == 'codex-cli-gpt-6-luna' else None
        ),
    )
    assert turn_engine.vision_routes()[0] == 'codex-cli-gpt-6-luna'

    monkeypatch.setattr(environment, 'older_models', lambda: {'gpt-6-luna'})
    assert 'codex-cli-gpt-6-luna' not in turn_engine.vision_routes()
    monkeypatch.setattr(environment, 'older_models', lambda: set())

    assert environment.main(['--auto', 'vision:gpt/luna@codex=off']) == 0
    assert 'codex-cli-gpt-6-luna' not in turn_engine.vision_routes()
    assert turn_engine.vision_routes()[0] == 'agy-cli'

    turn_engine.save_policy({'vision': [['claude-haiku-5-5-cli', 'agy-cli']]})
    assert turn_engine.vision_routes() == ['claude-haiku-5-5-cli', 'agy-cli']


def test_describe_files_uses_ranked_vision_routes_falls_back_and_reports_switched_off(pool, tmp_path, monkeypatch):
    _install_vision_and_research_clis(pool, monkeypatch)
    image = tmp_path / 'shot.png'
    image.write_bytes(b'\x89PNG\r\n\x1a\n')
    seen = []

    def fake_see(deployment, prompt, paths, timeout=240):
        seen.append(deployment['id'])
        if deployment['id'] == 'agy-cli':
            raise RuntimeError('rate limited')
        return 'ok from ' + deployment['id']

    monkeypatch.setattr(turn_engine, 'see', fake_see)
    out = turn_engine.describe_files([image], lambda _: None)
    assert seen == ['agy-cli', 'codex-cli-gpt-6-luna']
    assert 'described by codex-cli-gpt-6-luna' in out and 'ok from codex-cli-gpt-6-luna' in out

    assert environment.main(['--auto', 'vision:*@*=off']) == 0
    refused = turn_engine.describe_files([image], lambda _: None)
    assert 'no description is available' in refused and 'switched off for vision' in refused


def test_describe_files_executes_inline_role_table_vision_deployment(tmp_path, monkeypatch):
    templates = json.loads(cli_discovery.TEMPLATES.read_text(encoding='utf-8'))
    row = cli_discovery.deployment(templates['codex-cli-gpt-6-luna'], __file__)
    row.update(id='inline-vision', model='gpt-6-luna', roles=['explorer'], kind='primary', cost_tier='flat',
               quota_domain='openai-cli', endpoint_domain='openai-cli')
    roles_file = tmp_path / 'roles.json'
    roles_file.write_text(json.dumps({'schema_version': 1, 'deployments': [row]}), encoding='utf-8')
    monkeypatch.setenv('TARE_ROLE_DEPLOYMENTS', str(roles_file))
    monkeypatch.setattr(environment, 'older_models', lambda: set())
    image = tmp_path / 'shot.png'
    image.write_bytes(b'\x89PNG\r\n\x1a\n')
    seen = []
    monkeypatch.setattr(turn_engine, 'see', lambda dep, prompt, paths, timeout=240: seen.append(dep['id']) or 'inline ok')
    out = turn_engine.describe_files([image], lambda _: None)
    assert seen == ['inline-vision'] and 'described by inline-vision' in out and 'inline ok' in out


def test_researcher_ranks_clis_before_openrouter_by_value_and_admits_proven_arrivals(pool, monkeypatch):
    monkeypatch.setenv('OPENROUTER_API_KEY', 'fixture')
    _install_vision_and_research_clis(pool, monkeypatch)
    values = {'openrouter-glm-5-3-flash': 300.0, 'openrouter-deepseek-v4-1-flash-1017': 80.0}
    monkeypatch.setattr(model_signals, 'task', lambda d: {'value': values[d['id']]} if d['id'] in values else None)
    names = web_research.snapshot()['route_order']
    assert names == turn_engine.researcher_routes()
    assert names[:3] == ['agy-cli', 'codex-cli-gpt-6-luna', 'claude-haiku-5-5-cli']
    assert 'openrouter-glm-5-3-flash' in names and 'openrouter-deepseek-v4-1-flash-1017' in names
    assert names.index('claude-haiku-5-5-cli') < names.index('openrouter-glm-5-3-flash')
    assert names.index('openrouter-glm-5-3-flash') < names.index('openrouter-deepseek-v4-1-flash-1017')
    assert 'openrouter-kimi-k3-1-pro' not in names

    # Newly discovered and proven CLI version joins automatically without changing any list.
    discovered(pool)
    assert SOL not in web_research.snapshot()['route_order']
    setup = environment.read()
    setup.setdefault('proofs', {})[SOL] = {'ok': True}
    environment.save(setup)
    assert SOL in web_research.snapshot()['route_order']

    # Saved owner list overrides automatic ranking.
    turn_engine.save_policy({'researcher': [['codex-cli-gpt-6-luna', 'openrouter-glm-5-3']]})
    assert web_research.snapshot()['route_order'] == ['codex-cli-gpt-6-luna', 'openrouter-glm-5-3']


def test_local_model_grant_never_admits_researcher_or_vision(pool):
    gateway('qwen38-gsq', 'gemma-4')
    for role in ('researcher', 'vision'):
        rows, grants = routes(role)
        assert GEMMA not in rows
        assert grants == set()
        if role == 'vision':
            assert 'local-qwen' not in rows
        assert environment.main(['--auto', f'{role}:{GEMMA}=on']) == 0
        assert why(GEMMA, role) == 'ROLE_NOT_CONFIGURED'
        assert why('local-qwen', role) == 'ROLE_NOT_CONFIGURED'
        assert GEMMA not in web_research.snapshot()['routes']
        assert 'local-qwen' not in web_research.snapshot()['routes']
        assert GEMMA not in turn_engine.vision_routes()
        assert 'local-qwen' not in turn_engine.vision_routes()
