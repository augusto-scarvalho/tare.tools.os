"""Public flag compatibility without providers or operator storage."""
import argparse
import pytest
from relay import execution_scope as scope,native_product,native_catalog


def parse(values):
    parser=argparse.ArgumentParser()
    parser.add_argument('project',nargs='?')
    scope.add_arguments(parser)
    return parser.parse_args(scope.normalize_arguments(values))


@pytest.mark.parametrize('flag',['--prefer_cli','--prefer-subscription','--api_only','--local-only'])
def test_legacy_flag_does_not_consume_project(flag):
    args=parse([flag,'C:/project'])
    assert args.project=='C:/project'
    assert scope.from_arguments(args).restrictions==()


def test_attached_selection_forwarding():
    assert native_product.console_arguments(['--coordinator=google/gemini-3.8-flash@high'])==[
        'console','--coordinator=google/gemini-3.8-flash@high']


def test_explicit_empty_list_fails_before_catalog(monkeypatch):
    monkeypatch.setattr(native_catalog,'available_models',lambda:pytest.fail('catalog accessed'))
    with pytest.raises(ValueError):scope.from_arguments(parse(['--api_only=']))


@pytest.mark.parametrize('flag,family,providers', [
    ('--prefer_cli', 'subscription', ['anthropic', 'openai']),
    ('--api_only', 'api', ['openrouter', 'anthropic']),
])
def test_space_separated_list_is_one_set(monkeypatch, flag, family, providers):
    from relay import inference_allowlist
    monkeypatch.delenv(scope.ENV, raising=False)
    monkeypatch.setattr(native_catalog, 'available_models', lambda: [])
    monkeypatch.setattr(inference_allowlist, 'validate_catalog', lambda raw, values: None)
    value = scope.from_arguments(parse([flag, *providers, 'C:/project']))
    assert parse([flag, *providers, 'C:/project']).project == 'C:/project'
    assert value.restrictions == ((family, inference_allowlist.canonical(';'.join(providers))),)
    assert value == scope.from_arguments(parse([flag, *reversed(providers), providers[0]]))


def test_google_only_agy_wire_model_is_an_eligible_high_coordinator(monkeypatch, tmp_path):
    import json
    from copy import deepcopy
    from pathlib import Path
    from relay import coordinator_policy, environment, native_routes, route_facts
    value = json.loads((Path(native_catalog.__file__).parent / 'native_deployments' /
                        'vendor_cli_templates.json').read_text(encoding='utf8'))['agy-cli']
    executable = tmp_path / 'agy.exe'
    executable.write_bytes(b'offline fixture')
    value['native_cli']['executable'] = str(executable)
    before = deepcopy(value)
    monkeypatch.delenv(scope.ENV, raising=False)
    monkeypatch.setattr(native_catalog, 'available_models', lambda: [{'deployment': 'agy-cli'}])
    monkeypatch.setattr(native_catalog, 'select', lambda name: deepcopy(value))
    monkeypatch.setattr(environment, 'allows', lambda deployment: (True, None))
    monkeypatch.setattr(route_facts, 'collect', lambda *args, **kwargs: {})
    monkeypatch.setattr(route_facts, 'admission_reason', lambda facts: None)
    chosen = coordinator_policy.select('google/gemini-3.8-flash@high')
    assert chosen['native_profile']['model'] == 'gemini-3.8-flash-high'
    assert chosen['native_model_choice']['effort'] == 'high'
    assert value == before
