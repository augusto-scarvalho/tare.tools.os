"""AGY lists its models (`agy models`, per account): discovery offers a newer version of a line its templates have,
read once per executable file and per day. Offline: the one function that runs `agy models` is replaced by a
fixture; no CLI starts."""
import json
from datetime import datetime, timedelta

import pytest

from relay import cli_discovery, model_choices, native_profiles, native_routes
from test_cross_vendor_auditors import pool  # noqa: F401

REAL_AGY_LISTING = cli_discovery.agy_listing  # before the autouse guard replaces it

TEMPLATES = json.loads(cli_discovery.TEMPLATES.read_text(encoding='utf-8'))
AGY = sorted(n for n, t in TEMPLATES.items() if t['native_profile']['endpoint'] == 'cli://agy')
NEW = 'agy-gemini-4-1-argon-cli'
ARGON = ['gemini-4.1-argon-low', 'gemini-4.1-argon-medium', 'gemini-4.1-argon-high']
# `agy models` on 2026-11-16, ids only.
TODAY = ['gemini-4-argon-medium', 'gemini-4-argon-high', 'gemini-4-argon-low', 'gemini-4-hydrogen-high',
         'gemini-4-hydrogen-xhigh', 'gemini-4-hydrogen-medium', 'gemini-4-hydrogen-low', 'gemini-pro-orchestrator-g1-pro',
         'gemini-3.8-flash-high', 'gemini-3.8-flash-medium', 'gemini-3.8-flash-low', 'claude-opus-5-5-low',
         'claude-opus-5-5-medium', 'claude-opus-5-5-high', 'claude-sonnet-5-5-low', 'claude-sonnet-5-5-medium',
         'claude-sonnet-5-5-high', 'gpt-oss-120b-medium']


class Lister:
    def __init__(self, ids=(), error=None):
        self.ids, self.error, self.calls = list(ids), error, []

    def __call__(self, executable):
        self.calls.append(executable)
        if self.error:
            raise self.error
        return self.ids


@pytest.fixture
def agy(pool, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))
    exe = tmp_path/'agy.exe'
    exe.write_bytes(b'v1')
    monkeypatch.setattr(cli_discovery, 'locate', {'cli://agy': str(exe)}.get)
    monkeypatch.setattr(cli_discovery, 'logged_in', lambda endpoint, executable: True)
    return exe


def read(monkeypatch, lister):
    monkeypatch.setattr(cli_discovery, 'agy_listing', lister)
    return next(r for r in cli_discovery.discover() if r['endpoint'] == 'cli://agy')


def profile(name):
    return json.loads(native_profiles.local_path(name).read_text(encoding='utf-8'))


def age(hours):  # move the last attempt back, as time passing would
    cache = json.loads(cli_discovery.cache_file().read_text(encoding='utf-8'))
    held = cache['agy_models']
    held['checked_at'] = (datetime.fromisoformat(held['checked_at']) - timedelta(hours=hours)).isoformat()
    cli_discovery.cache_file().write_text(json.dumps(cache), encoding='utf-8')


def test_a_new_argon_becomes_a_profile_then_a_pin_only_role_row_copied_from_the_previous_version(agy, monkeypatch):
    report = read(monkeypatch, Lister(ARGON + TODAY))
    assert sorted(report['added']) == sorted(AGY+[NEW])
    value, old = profile(NEW), profile('agy-gemini-4-argon-cli')
    assert value['id'] == NEW and value['model'] == value['native_profile']['model'] == 'gemini-4.1-argon-high'
    assert value['native_reasoning_models'] == {e: f'gemini-4.1-argon-{e}' for e in ('low', 'medium', 'high')}
    assert value['native_profile']['parameters_json'] == '{"effort":"high"}'
    assert value['native_cli']['executable'] == str(agy)
    source = value['native_profile']['source']
    assert 'agy models 20' in source and 'agy-gemini-4-argon-cli' in source and 'account availability unchecked' in source
    strip = lambda v: {**v, 'id': 0, 'model': 0, 'native_reasoning_models': 0,  # noqa: E731
                       'native_profile': {**v['native_profile'], 'model': 0, 'source': 0}}
    assert strip(value) == strip(old)

    row = {r['id']: r for r in native_routes.configured()['deployments']}[NEW]
    assert row['qualified'] is False and 'agy-gemini-4-argon-cli' in row['note'] and row['model'] == 'gemini-4.1-argon-high'
    chosen, selection = native_routes.ranked('planner')
    assert {c['deployment']: c['reason'] for c in selection['candidates']}[NEW] == 'NOT_QUALIFIED_PIN_ONLY'
    assert NEW not in [d['id'] for d in chosen]


def test_an_effort_the_template_lacks_is_not_added_and_a_missing_one_takes_the_nearest(agy, monkeypatch):
    read(monkeypatch, Lister(['gemini-4.1-argon-medium', 'gemini-4.1-argon-xhigh', 'gemini-4.1-argon-low']))
    value = profile(NEW)  # the template's high is not listed; xhigh is not a template effort
    assert value['native_reasoning_models'] == {'low': 'gemini-4.1-argon-low', 'medium': 'gemini-4.1-argon-medium'}
    assert value['model'] == 'gemini-4.1-argon-medium' and value['native_profile']['parameters_json'] == '{"effort":"medium"}'
    assert 'nearest listed effort medium' in value['native_profile']['source']


def test_today_s_real_list_adds_nothing(agy, monkeypatch):
    assert sorted(read(monkeypatch, Lister(TODAY))['added']) == AGY


@pytest.mark.parametrize('ids', [['claude-opus-5-5-high'], ['gemini-4-argon-high'], ['gemini-3.7-flash-high'],
                                 ['gemini-pro-orchestrator-g1-pro'], ['gpt-oss-120b-medium'],
                                 ['gemini-9-nova-high'], ['gemini-4.1-argon']])
def test_an_id_that_is_not_a_newer_known_version_adds_nothing(agy, monkeypatch, ids):
    assert sorted(read(monkeypatch, Lister(ids))['added']) == AGY


def test_the_reading_is_taken_once_per_file_and_per_day(agy, monkeypatch):
    lister = Lister(ARGON)
    read(monkeypatch, lister)
    read(monkeypatch, lister)
    assert len(lister.calls) == 1
    agy.write_bytes(b'v2 longer')  # an AGY update
    read(monkeypatch, lister)
    assert len(lister.calls) == 2
    age(23)
    read(monkeypatch, lister)
    assert len(lister.calls) == 2
    age(2)  # now older than a day
    read(monkeypatch, lister)
    assert len(lister.calls) == 3
    assert profile(NEW)['id'] == NEW


@pytest.mark.parametrize('failure', [Lister(error=OSError('network')), Lister()])
def test_a_failed_or_empty_reading_keeps_the_last_one_and_its_profile(agy, monkeypatch, failure):
    read(monkeypatch, Lister(ARGON))
    agy.write_bytes(b'v2 longer')
    report = read(monkeypatch, failure)
    assert len(failure.calls) == 1 and report['removed'] == [] and native_profiles.local_path(NEW).is_file()
    read(monkeypatch, failure)
    assert len(failure.calls) == 1  # the attempt counts: a CLI that cannot list is not started every time


def test_a_failed_first_reading_leaves_the_templates(agy, monkeypatch):
    assert sorted(read(monkeypatch, Lister(error=OSError('network')))['added']) == AGY


def test_a_later_reading_that_no_longer_lists_the_base_retires_the_profile(agy, monkeypatch):
    read(monkeypatch, Lister(ARGON))
    agy.write_bytes(b'v2 longer')
    report = read(monkeypatch, Lister(TODAY))
    assert report['removed'] == [NEW] and not native_profiles.local_path(NEW).exists()


def test_a_profile_the_user_edited_is_never_touched(agy, monkeypatch):
    read(monkeypatch, Lister(ARGON))
    path = native_profiles.local_path(NEW)
    value = json.loads(path.read_text(encoding='utf-8'))
    value['native_profile']['output_tokens'] = 1234
    edited = json.dumps(value)
    path.write_text(edited, encoding='utf-8')
    agy.write_bytes(b'v2 longer')
    assert read(monkeypatch, Lister(ARGON))['updated'] == []
    agy.write_bytes(b'v3 even longer')
    assert read(monkeypatch, Lister(TODAY))['removed'] == []
    assert path.read_text(encoding='utf-8') == edited


def test_known_google_lines_unlocked_on_agy_become_pin_only_profiles_and_exclude_xhigh(agy, monkeypatch):
    unlocked = [
        'gemini-4-carbon-low', 'gemini-4-carbon-medium', 'gemini-4-carbon-high',
        'gemini-4-neon-low', 'gemini-4-neon-medium', 'gemini-4-neon-high', 'gemini-4-neon-xhigh',
    ]
    report = read(monkeypatch, Lister(TODAY + unlocked))
    carbon, neon = 'agy-gemini-4-carbon-cli', 'agy-gemini-4-neon-cli'
    assert sorted(report['added']) == sorted(AGY + [carbon, neon])

    table = {r['id']: r for r in native_routes.configured()['deployments']}
    chosen, selection = native_routes.ranked('planner')
    reasons = {c['deployment']: c['reason'] for c in selection['candidates']}

    for name, base, label in ((carbon, 'gemini-4-carbon', 'Gemini 4 Carbon'),
                              (neon, 'gemini-4-neon', 'Gemini 4 Neon')):
        value = profile(name)
        assert value['id'] == name and value['model'] == value['native_profile']['model'] == f'{base}-high'
        assert value['native_reasoning_models'] == {e: f'{base}-{e}' for e in ('low', 'medium', 'high')}
        row = table[name]
        assert row['qualified'] is False and row['cost_tier'] == 'flat'
        assert row['roles'] == ['planner', 'plan-auditor', 'auditor', 'explorer', 'implementer']
        assert reasons[name] == 'NOT_QUALIFIED_PIN_ONLY' and name not in [d['id'] for d in chosen]
        shown = model_choices.describe(name, value)
        assert (shown['label'], shown['group'], shown['route']['label'], shown['route']['efforts']) == (
            label, 'Google', 'AGY CLI', ['low', 'medium', 'high']
        )


def test_orchestrator_models_are_explicitly_excluded_by_listing_discovery_and_cli_ids(agy, monkeypatch):
    import subprocess
    stdout = '\n'.join([
        'gemini-pro-orchestrator-neon\tOrchestrator (Neon)',
        'gemini-pro-orchestrator\tOrchestrator (Carbon)',
        'gemini-pro-orchestrator-argon\tOrchestrator (Argon)',
        'gemini-4-neon-high\tOrchestrator (Neon)',
        'gemini-4-neon-high\tGemini 4 Neon (High)',
        'gemini-4-argon-high\tGemini 4 Argon (High)',
    ])
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess(a[0], 0, stdout, ''))
    assert REAL_AGY_LISTING(str(agy)) == ['gemini-4-neon-high', 'gemini-4-argon-high']

    cached_with_orch = ['gemini-4-orchestrator-high', 'gemini-pro-orchestrator-neon', 'gemini-pro-orchestrator-high']
    report = read(monkeypatch, Lister(TODAY + cached_with_orch))
    assert sorted(report['added']) == AGY
    assert cli_discovery.cli_ids('cli://agy', {'ids': cached_with_orch + ['gemini-4-neon-high']}) == {'gemini-4-neon'}


def test_template_less_anthropic_lines_stay_ignored_and_multiple_google_versions_pick_newest(agy, monkeypatch):
    extra = [
        'claude-haiku-5-5-low', 'claude-haiku-5-5-medium', 'claude-haiku-5-5-high',
        'claude-fable-5-2-low', 'claude-fable-5-2-medium', 'claude-fable-5-2-high',
        'gemini-3-carbon-high', 'gemini-4-carbon-high',
    ]
    report = read(monkeypatch, Lister(TODAY + extra))
    assert sorted(report['added']) == sorted(AGY + ['agy-gemini-4-carbon-cli'])
    assert not native_profiles.local_path('agy-gemini-3-carbon-cli').exists()
    assert not native_profiles.local_path('agy-claude-haiku-5-5-cli').exists()
    assert not native_profiles.local_path('agy-claude-fable-5-2-cli').exists()
