"""Explicit choices use immutable deployments and keep role policy separate."""
from copy import deepcopy
import pytest
from relay import coordinator_policy as policy, native_catalog, model_choices


def fixture_catalog(monkeypatch,name):
    value=native_catalog.select(name)
    monkeypatch.setattr(native_catalog,'available_models',lambda:[{'deployment':name}])
    monkeypatch.setattr(native_catalog,'select',lambda key:deepcopy(value))
    monkeypatch.setattr(policy,'eligible',lambda value:value)
    return value


def test_google_only_high_is_realized_without_changing_default(monkeypatch):
    original=fixture_catalog(monkeypatch,'gemini-api-3-8-flash')
    before=deepcopy(original)
    chosen=policy.select('google/gemini-3.8-flash@high')
    assert chosen['native_model_choice']['effort']=='high'
    assert original==before


def test_local_low_and_unsupported_high(monkeypatch):
    fixture_catalog(monkeypatch,'qwen38-gsq')
    assert policy.select('ninfer/qwen38-gsq@low')['native_model_choice']['effort']=='low'
    with pytest.raises(ValueError):policy.select('ninfer/qwen38-gsq@high')


@pytest.mark.parametrize('raw',['google','google/gemini-3.8-flash','google/gemini-3.8-flash@low,high'])
def test_coordinator_is_exactly_one_model_and_level(raw):
    with pytest.raises(ValueError):policy.select(raw)
