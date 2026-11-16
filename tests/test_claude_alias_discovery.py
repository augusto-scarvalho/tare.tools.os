"""Claude Code has no model listing: discovery reads which model id each family alias resolves to (zero tokens, once
per executable file) and offers a newer version of a known line without a template written for it. Offline: the
reading (compute_plane.vendor_guidance.claude_models) is replaced by a fixture; no CLI starts."""
import json

import pytest

from relay import cli_discovery, native_profiles, native_routes
from test_cross_vendor_auditors import pool  # noqa: F401

TEMPLATES = json.loads(cli_discovery.TEMPLATES.read_text(encoding='utf-8'))
CLAUDE = sorted(n for n, t in TEMPLATES.items() if t['native_profile']['endpoint'] == 'cli://claude')
NEW = 'claude-fable-5-3-cli'


class Reader:
    def __init__(self, models=None, error=None):
        self.models, self.error, self.calls = models or {}, error, []

    def __call__(self, executable):
        self.calls.append(executable)
        if self.error:
            raise self.error
        return {'cli_version': '2.1.400', 'models': {'fable': None, 'opus': None, 'sonnet': None, 'haiku': None,
                                                     **self.models}}


@pytest.fixture
def claude(pool, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))
    exe = tmp_path/'claude.exe'
    exe.write_bytes(b'v1')
    monkeypatch.setattr(cli_discovery, 'locate', {'cli://claude': str(exe)}.get)
    monkeypatch.setattr(cli_discovery, 'logged_in', lambda endpoint, executable: True)
    return exe


def read(monkeypatch, reader):
    monkeypatch.setattr(cli_discovery, 'claude_aliases', reader)
    return next(r for r in cli_discovery.discover() if r['endpoint'] == 'cli://claude')


def test_a_new_fable_becomes_a_profile_then_a_pin_only_role_row_copied_from_the_previous_version(claude, monkeypatch):
    reader = Reader({'fable': 'claude-fable-5-3', 'opus': 'claude-opus-5-5'})
    report = read(monkeypatch, reader)
    assert NEW in report['added'] and sorted(report['added']) == sorted(CLAUDE+[NEW])
    value = json.loads(native_profiles.local_path(NEW).read_text(encoding='utf-8'))
    old = json.loads(native_profiles.local_path('claude-fable-5-2-cli').read_text(encoding='utf-8'))
    assert value['id'] == NEW and value['model'] == value['native_profile']['model'] == 'claude-fable-5-3'
    assert value['native_cli']['executable'] == str(claude)
    assert 'Claude Code 2.1.400 alias reading' in value['native_profile']['source'] and 'claude-fable-5-2-cli' in value['native_profile']['source']
    assert 'account availability unchecked' in value['native_profile']['source']
    strip = lambda v: {**v, 'id': 0, 'model': 0, 'native_profile': {**v['native_profile'], 'model': 0, 'source': 0}}  # noqa: E731
    assert strip(value) == strip(old)

    row = {r['id']: r for r in native_routes.configured()['deployments']}[NEW]
    assert row['qualified'] is False and 'claude-fable-5-2-cli' in row['note'] and row['model'] == 'claude-fable-5-3'
    chosen, selection = native_routes.ranked('planner')
    assert {c['deployment']: c['reason'] for c in selection['candidates']}[NEW] == 'NOT_QUALIFIED_PIN_ONLY'
    assert NEW not in [d['id'] for d in chosen]


def test_the_reading_is_taken_once_per_executable_file(claude, monkeypatch):
    reader = Reader({'fable': 'claude-fable-5-3'})
    read(monkeypatch, reader)
    read(monkeypatch, reader)
    assert len(reader.calls) == 1
    claude.write_bytes(b'v2 longer')  # a Claude Code update
    read(monkeypatch, reader)
    assert len(reader.calls) == 2
    assert native_profiles.local_path(NEW).is_file()


@pytest.mark.parametrize('resolved', ['claude-fable-5-2', 'claude-fable-5-1', None, 'claude-fable-5-3-preview',
                                      'claude-fable-5-3-20261101', 'claude-nova-9', 'claude-fable', 'claude-fable-5.3'])
def test_an_alias_that_is_not_a_newer_known_version_adds_nothing(claude, monkeypatch, resolved):
    report = read(monkeypatch, Reader({'fable': resolved}))
    assert sorted(report['added']) == CLAUDE


def test_a_failed_reading_leaves_the_templates_and_is_not_repeated_until_the_file_changes(claude, monkeypatch):
    reader = Reader(error=ValueError('Claude Code 2.1.1 cannot be read'))
    report = read(monkeypatch, reader)
    assert sorted(report['added']) == CLAUDE
    read(monkeypatch, reader)
    assert len(reader.calls) == 1


def test_a_later_reading_that_no_longer_resolves_to_it_retires_the_profile(claude, monkeypatch):
    read(monkeypatch, Reader({'fable': 'claude-fable-5-3'}))
    claude.write_bytes(b'v2 longer')
    report = read(monkeypatch, Reader({'fable': 'claude-fable-5-2'}))
    assert report['removed'] == [NEW] and not native_profiles.local_path(NEW).exists()


def test_a_profile_the_user_edited_is_never_touched(claude, monkeypatch):
    read(monkeypatch, Reader({'fable': 'claude-fable-5-3'}))
    path = native_profiles.local_path(NEW)
    value = json.loads(path.read_text(encoding='utf-8'))
    value['native_profile']['output_tokens'] = 1234
    edited = json.dumps(value)
    path.write_text(edited, encoding='utf-8')
    claude.write_bytes(b'v2 longer')
    assert read(monkeypatch, Reader({'fable': 'claude-fable-5-3'}))['updated'] == []
    claude.write_bytes(b'v3 even longer')
    assert read(monkeypatch, Reader())['removed'] == []
    assert path.read_text(encoding='utf-8') == edited
