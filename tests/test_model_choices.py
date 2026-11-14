from copy import deepcopy
import json
from pathlib import Path

import pytest

from relay import model_choices as models
from scripts.configure_vendor_models import expand


def profile(endpoint='cli://claude', model='claude-opus-5', effort='low'):
    value = {'id': 'vendor', 'runner': 'native-http', 'model': model,
             'native_profile': {'protocol': 'vendor-cli', 'endpoint': endpoint, 'model': model,
                                'context_window': 200000, 'output_tokens': 16384, 'source': 'test',
                                'parameters_json': json.dumps({'effort': effort})},
             'native_counter_protocol': 'utf8-envelope-estimate-v1',
             'native_cli': {'executable': __file__, 'version': '2.1.278'}}
    if endpoint == 'cli://codex':
        value['native_cli']['model_catalog'] = {'slug': model, 'supported_reasoning_levels':
                                             [{'effort': e} for e in ('low', 'high', 'max', 'ultra')]}
    return value


def catalog(monkeypatch, values):
    monkeypatch.setattr(models.native_catalog, 'available_models', lambda: [{'deployment': n} for n in values])
    monkeypatch.setattr(models.native_catalog, 'select', lambda name: deepcopy(values[name]))
    return models.inventory()


def test_internal_planner_bindings_do_not_create_duplicate_picker_connections(monkeypatch):
    choices = catalog(monkeypatch, {'claude-cli':profile(), 'planner-'+('a'*24):profile()})
    assert len(choices) == 1
    assert [r['id'] for r in choices[0]['routes']] == ['claude-cli']


def test_models_group_connections_and_hide_alias_without_network(monkeypatch):
    cli = profile('cli://codex', 'gpt-6-astra')
    direct = deepcopy(cli)
    direct['native_profile'].update(protocol='openai-codex-responses', endpoint='https://chatgpt.com/backend-api/codex',
                                    parameters_json='{"reasoning":{"effort":"high"}}')
    direct['native_reasoning_levels'] = ['low', 'high', 'max', 'ultra']
    choices = catalog(monkeypatch, {'codex-cli': cli, 'codex-native': direct})
    assert len(choices) == 1
    assert choices[0]['label'] == 'GPT-6 Astra'
    assert len(choices[0]['routes']) == 2
    assert choices[0]['routes'][0]['id'] == 'codex-native'
    assert all('ultra' not in r['efforts'] for r in choices[0]['routes'])
    local = {'native_profile': {'protocol': 'openai-chat', 'endpoint': 'http://localhost:8080', 'model': 'qwen38'}}
    choices = catalog(monkeypatch, {'qwen38-gsq': local, 'local-qwen': local, 'qwen38': local})  # aliases of qwen38-gsq
    assert len(choices) == 1 and len(choices[0]['routes']) == 1


def test_reasoning_materializes_immutable_profile_and_remembers_per_model(tmp_path, monkeypatch):
    base = profile(); original = deepcopy(base)
    choices = catalog(monkeypatch, {'claude-cli': base})
    chosen = {'key': 'claude-opus-5', 'connection': 'claude-cli', 'reasoning': 'max'}
    first = models.materialize(choices, chosen, tmp_path)
    saved = json.loads(__import__('pathlib').Path(first).read_text())
    assert json.loads(saved['native_profile']['parameters_json']) == {'effort': 'max'}
    assert base == original
    second = models.materialize(choices, {**chosen, 'reasoning': 'medium'}, tmp_path)
    assert first != second and json.loads(__import__('pathlib').Path(first).read_text()) == saved
    models.remember(tmp_path, chosen, first)
    next_choice = {'key': 'different', 'connection': 'other', 'reasoning': 'low'}
    models.remember(tmp_path, next_choice, second)
    prefs = models.read_preferences(tmp_path)
    assert prefs['recent'] == ['different', 'claude-opus-5']
    assert prefs['models']['claude-opus-5']['reasoning'] == 'max'


def test_a_saved_choice_is_realized_from_the_current_catalog(tmp_path, monkeypatch):
    base = profile()
    base['native_execution_limits'] = {'request_seconds': 120}
    chosen = {'key': 'claude-opus-5', 'connection': 'claude-cli', 'reasoning': 'high'}
    saved = models.materialize(catalog(monkeypatch, {'claude-cli': base}), chosen, tmp_path)
    base['native_execution_limits'] = {'request_seconds': 300}  # the template moved on
    catalog(monkeypatch, {'claude-cli': base})
    fresh = models.current(tmp_path, {**chosen, 'profile': saved})
    assert fresh != saved and json.loads(Path(fresh).read_text())['native_execution_limits'] == {'request_seconds': 300}
    assert json.loads(Path(saved).read_text())['native_execution_limits'] == {'request_seconds': 120}


@pytest.mark.parametrize('fault', ['unknown_model', 'connection', 'ultracode', 'changed_profile'])
def test_bad_or_stale_choice_cannot_write_a_profile(tmp_path, monkeypatch, fault):
    base = profile()
    choices = catalog(monkeypatch, {'claude-cli': base})
    chosen = {'key': 'claude-opus-5', 'connection': 'claude-cli', 'reasoning': 'high'}
    if fault == 'unknown_model': chosen['key'] = 'not-a-model'
    if fault == 'connection': chosen['connection'] = 'codex-cli'
    if fault == 'ultracode': chosen['reasoning'] = 'ultracode'
    if fault == 'changed_profile': base['native_profile']['output_tokens'] += 1
    with pytest.raises(ValueError): models.materialize(choices, chosen, tmp_path)
    assert not (tmp_path/'model-selections').exists()


def test_agy_effort_selects_exact_registered_suffix(monkeypatch):
    base = profile('cli://agy', 'gemini-3.8-flash-high', 'high')
    base['native_reasoning_models'] = {'high': 'gemini-3.8-flash-high', 'low': 'gemini-3.8-flash-low'}
    choices = catalog(monkeypatch, {'agy-cli': base})
    value = models.realization(choices, {'key': 'gemini-3.8-flash', 'connection': 'agy-cli', 'reasoning': 'low'})
    assert value['native_profile']['model'] == value['model'] == 'gemini-3.8-flash-low'
    assert json.loads(value['native_profile']['parameters_json']) == {'effort': 'low'}
    assert choices[0]['routes'][0]['efforts'] == ['low', 'high']


@pytest.mark.parametrize('effort', ['default', 'low', 'medium', 'high'])
def test_argon_picker_groups_efforts_and_materializes_exact_model(tmp_path, monkeypatch, effort):
    from relay.cli_discovery import TEMPLATES
    base = json.loads(TEMPLATES.read_text(encoding='utf-8'))['agy-gemini-4-argon-cli']
    base['native_cli']['executable'] = __file__
    original = deepcopy(base)
    choices = catalog(monkeypatch, {'agy-gemini-4-argon-cli': base,
                                  'agy-cli': profile('cli://agy', 'gemini-3.8-flash-high', 'high')})
    argon = next(c for c in choices if c['key'] == 'gemini-4-argon')
    assert len(choices) == 2 and argon['label'] == 'Gemini 4 Argon' and argon['group'] == 'Google'
    assert len(argon['routes']) == 1
    route = argon['routes'][0]
    assert route['efforts'] == ['low', 'medium', 'high'] and route['default_effort'] == 'high'
    chosen = {'key': argon['key'], 'connection': route['id'], 'reasoning': effort}
    saved = json.loads(Path(models.materialize(choices, chosen, tmp_path)).read_text(encoding='utf-8'))
    expected = 'high' if effort == 'default' else effort
    assert saved['native_profile']['model'] == saved['model'] == 'gemini-4-argon-'+expected
    assert json.loads(saved['native_profile']['parameters_json']) == {'effort': expected}
    assert base == original
    for unsupported in ('xhigh', 'max'):
        with pytest.raises(ValueError, match='supported model'):
            models.realization(choices, dict(chosen, reasoning=unsupported))


@pytest.mark.parametrize('order', [1, -1])
def test_claude_on_agy_is_grouped_under_its_author_whatever_the_route_order(monkeypatch, order):
    from relay.cli_discovery import TEMPLATES
    from relay.inference_allowlist import identities
    templates = json.loads(TEMPLATES.read_text(encoding='utf-8'))
    names = ['agy-claude-opus-5-5-cli', 'claude-opus-5-5-cli'][::order]
    (opus,) = catalog(monkeypatch, {name: dict(templates[name], native_cli={**templates[name]['native_cli'],
                                                                           'executable': __file__}) for name in names})
    assert (opus['key'], opus['label'], opus['group']) == ('claude-opus-5-5', 'Claude Opus 5.5', 'Anthropic')
    routes = {r['id']: r for r in opus['routes']}
    assert routes['agy-claude-opus-5-5-cli']['label'] == 'AGY CLI' and routes['claude-opus-5-5-cli']['label'] == 'Claude CLI'
    assert routes['agy-claude-opus-5-5-cli']['efforts'] == ['low', 'medium', 'high']
    for effort in ('low', 'medium', 'high'):
        value = models.realization([opus], {'key': opus['key'], 'connection': 'agy-claude-opus-5-5-cli', 'reasoning': effort})
        assert value['native_profile']['model'] == value['model'] == 'claude-opus-5-5-'+effort
    # The connection, which pins name, stays AGY's: Google, never the author.
    assert identities('agy-claude-opus-5-5-cli', templates['agy-claude-opus-5-5-cli'])[0] == 'google'
    assert identities('claude-opus-5-5-cli', templates['claude-opus-5-5-cli'])[0] == 'anthropic'


def test_expansion_preserves_connections_and_exact_codex_catalog_records():
    cli = profile('cli://codex', 'gpt-6-astra')
    records = [{'slug': name, 'visibility': visibility, 'context_window': 272000,
                'default_reasoning_level': 'medium', 'supported_reasoning_levels': [{'effort': 'medium'}]}
               for name, visibility in [('gpt-6-astra', 'list'), ('gpt-5.5', 'list'), ('gpt-reserve', 'hide')]]
    sources = {'claude-cli': profile(), 'codex-cli': cli}
    original = deepcopy(sources)
    result = expand(sources, records)
    assert sources == original
    assert 'claude-fable-5-1-cli' in result
    assert not any(n in result for n in ('claude-sonnet-5-cli', 'claude-opus-4-8-cli', 'claude-opus-5-cli'))
    other = result['codex-cli-gpt-5-5']
    assert other['native_cli']['model_catalog'] == records[1]
    assert other['native_profile']['model'] == other['model'] == 'gpt-5.5'
    assert not any('reserve' in n for n in result)
    assert other['native_profile']['context_window'] == 200000


def test_expansion_enables_qualified_codex_direct_batch_only():
    base = profile('cli://codex', 'gpt-6-astra')
    base['native_profile'].update(protocol='openai-codex-responses',
        endpoint='https://chatgpt.com/backend-api/codex',
        parameters_json='{"reasoning":{"effort":"high"},"parallel_tool_calls":false}')
    base['native_reasoning_levels'] = ['medium', 'high']
    base['native_codex_auth'] = {'path': str(Path(__file__).resolve()), 'account_id': 'fixture'}
    base['native_streaming'] = True
    base.pop('native_cli')
    records = [{'slug': name, 'visibility': 'list', 'context_window': 272000,
        'default_reasoning_level': 'medium',
        'supported_reasoning_levels': [{'effort': 'medium'}]}
        for name in ('gpt-5.6-sol', 'gpt-6-sol', 'gpt-6.1-sol', 'gpt-5.6-terra', 'gpt-6-luna')]
    result = expand({'codex-native': base}, records)
    sol = json.loads(result['codex-native-gpt-5-6-sol']['native_profile']['parameters_json'])
    terra = json.loads(result['codex-native-gpt-5-6-terra']['native_profile']['parameters_json'])
    assert sol['parallel_tool_calls'] is True
    assert json.loads(result['codex-native-gpt-6-sol']['native_profile']['parameters_json'])['parallel_tool_calls'] is True
    assert json.loads(result['codex-native-gpt-6-1-sol']['native_profile']['parameters_json'])['parallel_tool_calls'] is True
    assert terra['parallel_tool_calls'] is False
    assert json.loads(result['codex-native-gpt-6-luna']['native_profile']['parameters_json'])['parallel_tool_calls'] is False
    assert json.loads(base['native_profile']['parameters_json'])['parallel_tool_calls'] is False


def test_opus_55_requires_current_cli_and_keeps_medium_default(monkeypatch):
    base = profile()
    assert 'claude-opus-5-5-cli' not in expand({'claude-cli': base})
    assert 'claude-sonnet-5-5-cli' not in expand({'claude-cli': base})
    base['native_cli']['version'] = '2.1.280'
    base['native_profile']['expected_response_model'] = 'claude-opus-5'
    original = deepcopy(base)
    result = expand({'claude-cli': base})
    sonnet = result['claude-sonnet-5-5-cli']['native_profile']
    assert sonnet['model'] == 'claude-sonnet-5-5' and '"high"' in sonnet['parameters_json']
    new = result['claude-opus-5-5-cli']
    assert base == original
    assert new['native_profile']['model'] == new['model'] == 'claude-opus-5-5'
    assert new['native_usage_contract']['layout'] == 'claude-cli-exclusive'
    base['native_cli']['version'] = '2.1.301'
    assert 'claude-sonnet-5-5-cli' in expand({'claude-cli': base})  # newer CLIs keep 5.5
    assert 'expected_response_model' not in new['native_profile']
    choices = catalog(monkeypatch, {'claude-opus-5-5-cli': new})
    assert choices[0]['label'] == 'Claude Opus 5.5'
    assert choices[0]['routes'][0]['default_effort'] == 'medium'
    assert choices[0]['routes'][0]['efforts'] == list(models.EFFORTS)
    for effort in models.EFFORTS:
        value = models.realization(choices, {'key': 'claude-opus-5-5',
            'connection': 'claude-opus-5-5-cli', 'reasoning': effort})
        assert json.loads(value['native_profile']['parameters_json']) == {'effort': effort}


def test_haiku_55_keeps_qualified_reference_and_explorer_default(tmp_path, monkeypatch):
    base = profile()
    base['native_cli']['version'] = '2.1.278'
    assert 'claude-haiku-5-5-cli' not in expand({'claude-cli': base})
    base['native_cli']['version'] = '2.1.280'
    base['native_profile']['expected_response_model'] = 'claude-opus-5'
    original = deepcopy(base)
    haiku = expand({'claude-cli': base})['claude-haiku-5-5-cli']
    assert base == original
    assert haiku['model'] == haiku['native_profile']['model'] == 'claude-haiku-5-5'
    assert haiku['native_cli']['version'] == '2.1.280'
    assert haiku['native_usage_contract']['layout'] == 'claude-cli-exclusive'
    assert 'expected_response_model' not in haiku['native_profile']
    choices = catalog(monkeypatch, {'claude-haiku-5-5-cli': haiku})
    assert choices[0]['label'] == 'Claude Haiku 5.5'
    assert choices[0]['routes'][0]['default_effort'] == 'low'
    assert choices[0]['routes'][0]['efforts'] == list(models.EFFORTS)
    chosen = {'key': 'claude-haiku-5-5', 'connection': 'claude-haiku-5-5-cli', 'reasoning': 'default'}
    low_path = models.materialize(choices, chosen, tmp_path)
    high_path = models.materialize(choices, {**chosen, 'reasoning': 'high'}, tmp_path)
    low = json.loads(Path(low_path).read_text())
    high = json.loads(Path(high_path).read_text())
    assert low_path != high_path
    assert json.loads(low['native_profile']['parameters_json']) == {'effort': 'low'}
    assert json.loads(high['native_profile']['parameters_json']) == {'effort': 'high'}
    assert haiku['native_profile']['context_window'] == base['native_profile']['context_window']
    assert json.loads(haiku['native_profile']['parameters_json']) == {'effort': 'low'}


def test_corrupt_preferences_are_not_overwritten(tmp_path):
    (tmp_path/'models.json').write_text('{broken')
    with pytest.raises(ValueError):
        models.remember(tmp_path, {'key':'x','connection':'y','reasoning':'high'}, 'profile')
    assert (tmp_path/'models.json').read_text() == '{broken'


def test_local_auto_resolves_once_from_the_host_recommendation():
    from relay import native_catalog
    auto = native_catalog.select('local-auto')
    auto['native_model_choice'] = {'key': 'local-auto'}
    assert models.describe('local-auto', auto)['label'] == 'Local auto'
    seen = []

    def observe(model):
        def fetch(url, timeout):
            seen.append(url)
            return {'status': 'OBSERVED', 'value': {'mode': 'x', 'model': model, 'reason': 'r', 'host': {}}}
        return fetch
    solo = native_catalog.resolve_host_mode(auto, observe=observe('swift-next'))
    assert solo['native_profile']['model'] == 'swift-next' and 'native_host_modes' not in solo
    assert solo['native_model_choice'] == {'key': 'local-auto'} and solo['native_host_selection']['model'] == 'swift-next'
    shared = native_catalog.resolve_host_mode(auto, observe=observe('qwen38'))
    assert shared['native_profile'] == auto['native_profile'] and 'native_host_modes' not in shared
    for broken in (lambda url, timeout: {'status': 'UNKNOWN', 'reason': 'MODEL_FACTS_UNAVAILABLE'},
                   observe('unregistered')):
        fallback = native_catalog.resolve_host_mode(auto, observe=broken)
        assert fallback['native_profile'] == auto['native_profile']
        assert fallback['native_host_selection']['reason'] == 'RECOMMEND_UNAVAILABLE'
    assert seen == [auto['native_host_modes']['recommend']] * 3
    # Profiles without host modes and resumed sessions are untouched.
    plain = native_catalog.select('qwen38')
    assert native_catalog.resolve_host_mode(plain, observe=None) is plain


def test_local_model_roles_follow_dispatch_aliases_and_local_only_mode():
    """tare models and exact pins list what dispatch uses: the local implementer in every mode (the role table and
    the implementer ladder name it as local-qwen and local-default); planner and auditor only under --local_only."""
    from relay.execution_scope import Scope
    from relay.execution_team import reasoning
    def roles(scope):
        row = next(r for r in models.projection(include_unconfigured=True, scope=scope) if r['deployment'] == 'qwen38-gsq')
        return {role: [a.rsplit('@', 1)[1] for a in arguments] for role, arguments in row['role_arguments'].items()}
    default = roles(Scope())
    assert default['implementer'] == ['low', 'medium', 'xhigh'] and not {'planner', 'auditor'} & set(default)
    assert roles(Scope(prefer='local').validate()) == default
    local = roles(Scope(only='local').validate())
    assert local['planner'] == ['xhigh'] and local['auditor'] == local['implementer'] == ['low', 'medium', 'xhigh']
    assert reasoning(models.select_exact('ninfer/qwen38-gsq@low', 'implementer', scope=Scope())) == 'low'
    assert reasoning(models.select_exact('ninfer/qwen38-gsq@xhigh', 'planner', scope=Scope(only='local'))) == 'xhigh'
    for role in ('planner', 'auditor'):
        with pytest.raises(ValueError, match='Pin is unknown, unconfigured or incompatible'):
            models.select_exact('ninfer/qwen38-gsq@xhigh', role, scope=Scope(prefer='local').validate())


def test_shipped_local_profiles_use_this_machines_gateway(tmp_path, monkeypatch):
    from relay import native_catalog
    monkeypatch.delenv('TARE_LOCAL_GATEWAY', raising=False)
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path))
    assert native_catalog.select('qwen38')['native_profile']['endpoint'] == 'http://127.0.0.1:8080/v1'
    (tmp_path/'local-gateway.json').write_text('{"origin": "http://10.0.0.5:8080"}', encoding='utf-8')
    auto = native_catalog.select('local-auto')
    assert auto['native_profile']['endpoint'] == 'http://10.0.0.5:8080/v1'
    assert auto['native_host_modes']['recommend'] == 'http://10.0.0.5:8080/v1/fleet/recommend'
    monkeypatch.setenv('TARE_LOCAL_GATEWAY', 'http://gpu-box:9000/')
    assert native_catalog.select('swift27b')['native_profile']['endpoint'] == 'http://gpu-box:9000/v1'
    for bad in ('gpu-box:9000', 'http://gpu-box:9000/v1', 'ftp://gpu-box'):
        monkeypatch.setenv('TARE_LOCAL_GATEWAY', bad)
        with pytest.raises(ValueError, match='local_gateway'):
            native_catalog.select('qwen38')
