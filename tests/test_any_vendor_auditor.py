"""--any_vendor_auditor and /routes turn off "another vendor first" in auditor selection (owner, 2026-11-14)."""
import argparse
import json
import os
import subprocess
import sys

import pytest

from relay import execution_scope as scope, native_catalog, native_routes, turn_engine
from relay.landing_policy import SAME_VENDOR_AUDIT
from test_audit_vendor_independence import FABLE, only
from test_cross_vendor_auditors import install, pool  # noqa: F401  (fixture)


def switch(monkeypatch, how, **policy):
    """Turn the switch off by the launch flag or the saved policy; 'on' leaves it as tare ships it."""
    if how == 'flag':
        policy['any_vendor_auditor'] = True
    if policy:
        monkeypatch.setenv(scope.ENV, json.dumps(policy))
    if how == 'policy':
        turn_engine.save_policy({}, any_vendor_auditor=True)


@pytest.mark.parametrize('how', ['on', 'flag', 'policy'])
def test_prefer_subscription_takes_the_claude_subscription_once_the_switch_is_off(pool, monkeypatch, how):
    install(pool, 'claude-opus-5-5-cli', 'cli://claude', 'claude-opus-5-5')  # Claude subscription, another bias domain
    monkeypatch.setattr(native_routes, 'credential_available', only('OPENAI_API_KEY'))  # GPT paid per token
    switch(monkeypatch, how, prefer='subscription')
    fable = native_catalog.select(FABLE)
    selected, receipt = native_routes.reviewers(fable)
    expected = 'openai-api' if how == 'on' else 'claude-opus-5-5-cli'
    assert selected[0]['id'] == expected
    assert turn_engine.cross_vendor_first() == (how == 'on')
    assert receipt['reason'] == ('AUTO_CROSS_VENDOR_THEN_COST_AND_CONFIGURED_ORDER' if how == 'on'
                                 else 'AUTO_COST_AND_CONFIGURED_ORDER')
    opus = next(c for c in receipt['candidates'] if c['deployment'] == 'claude-opus-5-5-cli')
    assert opus['reason'] == 'SAME_VENDOR_NOT_INDEPENDENT'  # on or off, it is never called independent
    # The conversation audit line names who audited, with the label when it is the implementer's own vendor.
    monkeypatch.setattr('relay.native_agent.run_session', lambda *a, **kw: {'status': 'PROPOSED', 'artifact_file': 'x'})
    monkeypatch.setattr('relay.native_artifacts.read_input', lambda path: {'artifact': {'payload': {
        'verdict': 'ACCEPT', 'summary': 'Fine', 'findings': [], 'remaining': []}}})
    shown = []
    record, _ = turn_engine.audit_turn([{'path': 'a.py', 'diff': '+x\n'}], pool.parent, 'Change a.py', fable, shown.append)
    assert record['route'] == expected
    label = f' · {SAME_VENDOR_AUDIT}' if how != 'on' else ''
    assert f'_tare audit · {expected}{label} · 1 changed lines_' in ''.join(shown)
    assert (record.get('independence') == SAME_VENDOR_AUDIT) == (how != 'on')


@pytest.mark.parametrize('how', ['flag', 'policy'])
def test_the_switch_off_never_admits_the_same_model_or_bias_domain(pool, monkeypatch, how):
    install(pool, 'claude-fable-5-1-cli', 'cli://claude', 'claude-fable-5-1')  # Fable's own model
    monkeypatch.setattr(native_routes, 'credential_available', only('ANTHROPIC_API_KEY'))
    monkeypatch.setattr(turn_engine, 'available', lambda name, **kw: (native_catalog.select(name), None))
    switch(monkeypatch, how)
    fable = native_catalog.select(FABLE)
    selected, receipt = native_routes.reviewers(fable)
    refused = {'claude-fable-5-1-cli', FABLE}
    assert not refused & {d['id'] for d in selected}
    assert {c['reason'] for c in receipt['candidates'] if c['deployment'] in refused} == {'AUDITOR_NOT_INDEPENDENT'}
    listed = ['claude-fable-5-1-cli', 'claude-api-fable-5-2', 'claude-api-opus-5-5']  # same model, same bias domain
    assert [name for name, _ in turn_engine.pick_auditors(fable, listed)] == ['claude-api-opus-5-5']


@pytest.mark.parametrize('how', ['on', 'flag', 'policy'])
def test_the_owners_list_keeps_its_order_with_the_switch_off(pool, monkeypatch, how):
    monkeypatch.setattr(turn_engine, 'available', lambda name, **kw: (native_catalog.select(name), None))
    switch(monkeypatch, how)
    turn_engine.save_policy({'auditor': [['claude-api-opus-5-5'], ['openai-api']]}, auditor_selection='configured')
    selected, receipt = native_routes.reviewers(native_catalog.select(FABLE))
    order = [d['id'] for d in selected]
    assert order == (['openai-api', 'claude-api-opus-5-5'] if how == 'on' else ['claude-api-opus-5-5', 'openai-api'])
    assert receipt['reason'] == ('OPERATOR_AUDITOR_ROUTES_CROSS_VENDOR_FIRST' if how == 'on'
                                 else 'OPERATOR_AUDITOR_ROUTES_IN_ORDER')
    assert dict((c['deployment'], c['reason']) for c in receipt['candidates'])['claude-api-opus-5-5'] == (
        'SAME_VENDOR_NOT_INDEPENDENT')


@pytest.mark.parametrize('how', ['on', 'flag'])
def test_a_list_set_aside_by_a_preference_is_remapped_only_while_another_vendor_goes_first(pool, monkeypatch, how):
    install(pool, 'claude-opus-5-5-cli', 'cli://claude', 'claude-opus-5-5')
    monkeypatch.setattr(native_routes, 'credential_available', only('OPENAI_API_KEY'))
    monkeypatch.setattr(turn_engine, 'available', lambda name, **kw: (native_catalog.select(name), None))
    switch(monkeypatch, how, prefer='subscription')  # sets the owner's API route aside
    chosen = [name for name, _ in turn_engine.pick_auditors(native_catalog.select(FABLE), ['openai-api', 'claude-opus-5-5-cli'])]
    if how == 'on':
        assert chosen[0] == 'openai-api'
    else:
        assert chosen == ['claude-opus-5-5-cli']  # the owner's list as it was before #160


@pytest.mark.parametrize('spelling', ['--any_vendor_auditor', '--any-vendor-auditor'])
def test_the_launch_flag_is_parsed_shown_and_inherited_by_a_child(monkeypatch, spelling):
    parser = argparse.ArgumentParser()
    scope.add_arguments(parser)
    parsed = scope.from_arguments(parser.parse_args(scope.normalize_arguments([spelling])))
    assert parsed == scope.Scope(any_vendor_auditor=True) and not parsed.changes('auditor')  # a list stays a list
    assert parsed.label() == 'Auditors of another vendor first: off'
    with scope.activated(parsed):
        child = subprocess.run([sys.executable, '-B', '-c',
            'from relay import execution_scope, turn_engine; '
            'print(execution_scope.current().any_vendor_auditor, turn_engine.cross_vendor_first())'],
            capture_output=True, text=True, check=True)
    assert child.stdout.split() == ['True', 'False']
    assert scope.ENV not in os.environ
    with pytest.raises(ValueError, match='booleans'):
        scope.Scope(any_vendor_auditor='yes').validate()
