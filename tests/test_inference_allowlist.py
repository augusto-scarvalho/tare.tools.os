"""Offline canonical set syntax and negative controls."""
import pytest
from relay.inference_allowlist import canonical, parse


def test_permutation_and_duplicates_are_a_set():
    assert canonical('google;openai/gpt-6.1-sol@high;google') == canonical('openai/gpt-6.1-sol@high,google')
    assert parse('ninfer/qwen38-gsq@low,medium,xhigh')[0][2] == ('low','medium','xhigh')


@pytest.mark.parametrize('raw',['', ' ', 'gogle', 'google/', 'google/model@ultra', 'google,,openai', 'google@high@low'])
def test_invalid_syntax_is_rejected(raw):
    with pytest.raises(ValueError):parse(raw)


@pytest.mark.parametrize('raw,name,level', [
    ('ninfer/qwen3.8-gsq@low', 'qwen38-gsq', 'low'),
    ('strata/qwen-next@medium', 'swift-next', 'medium'),
    ('anthropic/opus@high', 'claude-cli', 'high'),
])
def test_friendly_alias_resolves_registered_capabilities(raw, name, level):
    import json
    from pathlib import Path
    from relay import native_catalog, model_choices
    from relay.inference_allowlist import matches, validate_catalog
    if name == 'claude-cli':
        value = json.loads((Path(native_catalog.__file__).parent / 'native_deployments' /
                            'vendor_cli_templates.json').read_text(encoding='utf8'))[name]
    else:
        value = native_catalog.select(name)
    validate_catalog(raw, [(name, value)])
    assert matches(raw, name, value, effort=level)
    assert level in model_choices.describe(name, value)['route']['efforts']


@pytest.mark.parametrize('raw', ['ninfer', 'ninfer/swift27b', 'ninfer/swift27b@xhigh'])
def test_swift27b_is_ninfer_without_new_capabilities_or_coordinator_status(raw):
    from copy import deepcopy
    from relay import native_catalog, model_choices, coordinator_policy
    from relay.inference_allowlist import matches, validate_catalog
    value = native_catalog.select('swift27b')
    before = deepcopy(value)
    validate_catalog(raw, [('swift27b', value)])
    assert matches(raw, 'swift27b', value)
    assert model_choices.describe('swift27b', value)['route']['efforts'] == ['xhigh']
    assert not matches('strata', 'swift27b', value)
    for level in ('low', 'medium', 'high', 'max'):
        with pytest.raises(ValueError, match='unsupported'):
            validate_catalog('ninfer/swift27b@' + level, [('swift27b', value)])
    with pytest.raises(ValueError, match='not an approved coordinator'):
        coordinator_policy.eligible(value)
    assert value == before
