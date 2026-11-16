"""A newer version a vendor's CLI lists is mirrored to that vendor's API once the API's own list has it: the CLIs are
the whitelist, the API's list only a check, and a stored key is sent to the vendor's own host and nowhere else.
Offline: the CLI readings, the stored key and the one GET (cli_discovery.api_listing) are fixtures; nothing starts."""
import io
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from urllib.request import OpenerDirector, Request

import pytest

from relay import cli_discovery, environment, native_catalog, native_profiles, native_routes
from test_cross_vendor_auditors import pool  # noqa: F401

REAL_LISTING = cli_discovery.api_listing  # before the autouse guard replaces it
KEY = 'sk-fixture-SECRET-0123456789'
HOSTS = {'OPENAI_API_KEY': 'https://api.openai.com/v1', 'ANTHROPIC_API_KEY': 'https://api.anthropic.com/v1',
         'GEMINI_API_KEY': 'https://generativelanguage.googleapis.com/v1beta/openai'}
OPENAI = dict(name='openai-api-gpt-6-2-sol', model='gpt-6.2-sol', sibling='openai-api', env='OPENAI_API_KEY')
CLAUDE = dict(name='claude-api-fable-5-3', model='claude-fable-5-3', sibling='claude-api-fable-5-2', env='ANTHROPIC_API_KEY')
GEMINI = dict(name='gemini-api-4-1-argon', model='gemini-4.1-argon', sibling='gemini-api', env='GEMINI_API_KEY')


class Listing:
    """The fixture of api_listing: the ids per endpoint, or an error; records every request (endpoint, key)."""
    def __init__(self):
        self.ids, self.error, self.calls = {}, None, []

    def __call__(self, deployment, key):
        endpoint = deployment['native_profile']['endpoint']
        self.calls.append((endpoint, key))
        if self.error:
            raise self.error
        return self.ids.get(endpoint, [])


@pytest.fixture
def world(pool, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))
    monkeypatch.setenv('CODEX_HOME', str(tmp_path/'codex'))
    exes = {}
    for endpoint, name in (('cli://claude', 'claude'), ('cli://codex', 'codex'), ('cli://agy', 'agy')):
        (tmp_path/(name+'.exe')).write_bytes(b'v1')
        exes[endpoint] = str(tmp_path/(name+'.exe'))
    monkeypatch.setattr(cli_discovery, 'locate', exes.get)
    monkeypatch.setattr(cli_discovery, 'logged_in', lambda endpoint, executable: True)
    w = SimpleNamespace(claude={}, agy=[], keys={name: KEY for name in HOSTS}, listing=Listing())
    monkeypatch.setattr(cli_discovery, 'claude_aliases', lambda exe: {'cli_version': '2.1.400', 'models': dict(w.claude)})
    monkeypatch.setattr(cli_discovery, 'agy_listing', lambda exe: list(w.agy))
    monkeypatch.setattr(cli_discovery, 'api_key', lambda name: w.keys.get(name))
    monkeypatch.setattr(cli_discovery, 'api_listing', w.listing)

    def codex(*slugs):  # Codex's own catalogue for the signed-in account
        (tmp_path/'codex').mkdir(exist_ok=True)
        records = [{'slug': s, 'visibility': 'list', 'default_reasoning_level': 'high', 'context_window': 400000,
                    'supported_reasoning_levels': [{'effort': e} for e in ('low', 'medium', 'high', 'xhigh')]} for s in slugs]
        (tmp_path/'codex'/'models_cache.json').write_text(json.dumps({'models': records}), encoding='utf-8')
    w.codex = codex
    return w


def mirror():
    """The API mirror's report row of one discovery, or None when it changed nothing."""
    return next((r for r in cli_discovery.discover() if r['endpoint'] == 'api://mirror'), None)


def profile(name):
    return json.loads(native_profiles.local_path(name).read_text(encoding='utf-8'))


def age(hours):  # move the last attempt back, as time passing would
    cache = json.loads(cli_discovery.cache_file().read_text(encoding='utf-8'))
    for held in cache['api_models'].values():
        held['checked_at'] = (datetime.fromisoformat(held['checked_at']) - timedelta(hours=hours)).isoformat()
    cli_discovery.cache_file().write_text(json.dumps(cache), encoding='utf-8')


def openai(w, api=('gpt-6.2-sol', 'gpt-6.1-sol', 'gpt-realtime-2.1')):
    w.codex('gpt-6.2-sol', 'gpt-6.1-sol')
    w.listing.ids[HOSTS['OPENAI_API_KEY']] = list(api)


@pytest.mark.parametrize('case', [OPENAI, CLAUDE, GEMINI], ids=lambda c: c['model'])
def test_a_model_the_cli_lists_and_the_api_confirms_becomes_a_profile_then_a_pin_only_role_row(world, case):
    if case is OPENAI:
        openai(world)
    elif case is CLAUDE:
        world.claude = {'fable': 'claude-fable-5-3', 'opus': 'claude-opus-5-5'}
        world.listing.ids[HOSTS[case['env']]] = ['claude-fable-5-3', 'claude-haiku-4-5-20251001']
    else:
        world.agy = [f"gemini-4.1-argon-{e}" for e in ('low', 'medium', 'high')] + ['gemini-4-argon-high']
        world.listing.ids[HOSTS[case['env']]] = ['gemini-4.1-argon']
    assert mirror()['added'] == [case['name']]
    assert [c[0] for c in world.listing.calls] == [HOSTS[case['env']]]  # one request, to this vendor only
    value, old = profile(case['name']), native_catalog.bundled(case['sibling'])
    assert value['id'] == case['name'] and value['model'] == value['native_profile']['model'] == case['model']
    source = value['native_profile']['source']
    assert f"Mirrors {case['model']}" in source and f"copied from {case['sibling']}" in source and 'list 20' in source
    strip = lambda v: {**v, 'id': 0, 'model': 0, 'native_profile': {**v['native_profile'], 'model': 0, 'source': 0}}  # noqa: E731
    assert strip(value) == strip(old)  # limits, parameters and credential inherited; no price added
    assert 'native_usage_tariff' not in value

    table = {r['id']: r for r in native_routes.configured()['deployments']}
    row, theirs = table[case['name']], table[case['sibling']]
    assert row['qualified'] is False and row['model'] == case['model'] and f"from {case['sibling']}, the version" in row['note']
    assert row['roles'] == theirs['roles'] and row['cost_tier'] == theirs['cost_tier']
    chosen, selection = native_routes.ranked('planner')
    assert {c['deployment']: c['reason'] for c in selection['candidates']}[case['name']] == 'NOT_QUALIFIED_PIN_ONLY'
    assert case['name'] not in [d['id'] for d in chosen]


def test_a_claude_model_agy_carries_is_not_agys_to_whitelist(world):
    world.agy = [f'claude-opus-5-6-{e}' for e in ('low', 'medium', 'high')]
    world.listing.ids[HOSTS['ANTHROPIC_API_KEY']] = ['claude-opus-5-6']
    assert mirror() is None and world.listing.calls == []  # only Claude Code speaks for Anthropic's API


def test_pro_is_not_mirrored_the_copy_comes_from_the_plain_profile(world):
    openai(world)
    mirror()
    value = profile(OPENAI['name'])
    assert 'pro' not in json.dumps(value['native_profile']['parameters_json']) and 'sol-pro' not in value['native_profile']['source']
    assert not [n for n in native_profiles.names() if n.endswith('-pro')]


def test_no_stored_key_sends_nothing_and_writes_nothing(world):
    openai(world)
    world.keys = {}
    assert mirror() is None and world.listing.calls == [] and native_profiles.names().count(OPENAI['name']) == 0


def test_a_provider_declined_in_the_setup_sends_nothing_and_writes_nothing(world, monkeypatch):
    openai(world)
    monkeypatch.setattr(environment, 'read', lambda: {'schema': environment.SCHEMA,
                                                      'api_providers': {'items': {'OPENAI_API_KEY': False}}})
    assert mirror() is None and world.listing.calls == [] and OPENAI['name'] not in native_profiles.names()


@pytest.mark.parametrize('slugs', [['gpt-6.1-sol', 'gpt-6-sol'], ['gpt-6.1-terra'], ['gpt-7-nova'], ['gpt-6.2-sol-extra']])
def test_a_version_the_bundled_files_have_an_older_one_or_an_unknown_line_adds_nothing_and_asks_nothing(world, slugs):
    world.codex(*slugs)
    world.listing.ids[HOSTS['OPENAI_API_KEY']] = slugs
    assert mirror() is None and world.listing.calls == []


def test_an_id_the_api_list_does_not_have_gets_no_profile(world):
    openai(world, api=['gpt-6.1-sol', 'gpt-realtime-2.1'])
    assert mirror() is None and len(world.listing.calls) == 1 and OPENAI['name'] not in native_profiles.names()


def test_only_whitelisted_ids_are_looked_up_so_snapshots_and_non_chat_ids_never_become_profiles(world):
    world.codex('gpt-6.2-sol')
    world.claude = {'haiku': 'claude-haiku-5-5'}
    world.listing.ids[HOSTS['OPENAI_API_KEY']] = ['gpt-6.2-sol', 'gpt-realtime-2.1', 'gpt-6.3-sol', 'gpt-6.2-sol-2026-11-01']
    world.listing.ids[HOSTS['ANTHROPIC_API_KEY']] = ['claude-haiku-4-5-20251001', 'claude-fable-5-4']
    assert mirror()['added'] == [OPENAI['name']]
    assert not [n for n in native_profiles.names() if 'realtime' in n or '20251001' in n or '6-3' in n or '5-4' in n]


def test_the_list_is_read_once_a_day(world):
    openai(world)
    mirror()
    mirror()
    assert len(world.listing.calls) == 1
    age(23)
    mirror()
    assert len(world.listing.calls) == 1
    age(2)
    mirror()
    assert len(world.listing.calls) == 2


def test_a_failed_or_empty_read_keeps_the_last_reading_and_its_profile(world):
    openai(world)
    mirror()
    for failure in (OSError('network'), None):
        age(25)
        world.listing.error = failure
        world.listing.ids[HOSTS['OPENAI_API_KEY']] = []
        before = len(world.listing.calls)
        assert mirror() is None and len(world.listing.calls) == before+1
        assert native_profiles.local_path(OPENAI['name']).is_file()
        mirror()
        assert len(world.listing.calls) == before+1  # the attempt counts
    held = json.loads(cli_discovery.cache_file().read_text(encoding='utf-8'))['api_models']['OpenAI']
    assert 'gpt-6.2-sol' in held['ids']


def test_a_successful_read_that_drops_the_id_retires_the_profile(world):
    openai(world)
    mirror()
    age(25)
    world.listing.ids[HOSTS['OPENAI_API_KEY']] = ['gpt-6.1-sol']
    assert mirror()['removed'] == [OPENAI['name']] and not native_profiles.local_path(OPENAI['name']).exists()


def test_the_cli_dropping_the_id_retires_the_profile_without_a_request_or_a_key_read(world, monkeypatch):
    openai(world)
    mirror()
    world.codex('gpt-6.1-sol')
    read = []
    monkeypatch.setattr(cli_discovery, 'api_key', lambda name: read.append(name))
    assert mirror()['removed'] == [OPENAI['name']] and len(world.listing.calls) == 1 and read == []


def test_a_profile_the_user_edited_is_never_touched(world):
    openai(world)
    mirror()
    path = native_profiles.local_path(OPENAI['name'])
    value = profile(OPENAI['name'])
    value['native_profile']['output_tokens'] = 1234
    edited = json.dumps(value)
    path.write_text(edited, encoding='utf-8')
    age(25)
    assert mirror() is None
    world.codex('gpt-6.1-sol')
    assert mirror() is None and path.read_text(encoding='utf-8') == edited


def test_the_key_is_not_in_the_cache_file(world):
    openai(world)
    mirror()
    cache = cli_discovery.cache_file().read_text(encoding='utf-8')
    assert KEY not in cache and 'gpt-6.2-sol' in cache and 'api_models' in cache
    assert KEY not in native_profiles.local_path(OPENAI['name']).read_text(encoding='utf-8')


class Reply(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.mark.parametrize('case', [OPENAI, CLAUDE, GEMINI], ids=lambda c: c['env'])
def test_the_request_goes_to_the_bundled_host_with_the_key_in_a_header_only(case, monkeypatch):
    seen = []

    def opened(self, request, data=None, timeout=None):
        seen.append((request, timeout, [type(h).__name__ for h in self.handlers]))
        return Reply(json.dumps({'data': [{'id': 'models/'+case['model']}, {'id': 'other'}, {'name': 'x'}]}).encode())
    monkeypatch.setattr(OpenerDirector, 'open', opened)
    deployment = native_catalog.bundled(case['sibling'])
    assert REAL_LISTING(deployment, KEY) == sorted([case['model'], 'other'])
    (request, timeout, handlers), = seen
    assert isinstance(request, Request) and request.full_url.startswith(HOSTS[case['env']]+'/models') and timeout == 15
    assert KEY not in request.full_url and request.get_method() == 'GET'
    headers = dict(request.header_items())
    if case is CLAUDE:
        assert headers['X-api-key'] == KEY and headers['Anthropic-version'] and request.full_url.endswith('/models?limit=1000')
        assert 'Authorization' not in headers
    else:
        assert headers['Authorization'] == 'Bearer '+KEY and 'X-api-key' not in headers
    assert '_NoRedirect' in handlers and 'HTTPRedirectHandler' not in handlers  # a redirect never carries the key on


def test_a_redirect_is_refused(monkeypatch):
    assert cli_discovery._NoRedirect().redirect_request(None, None, 302, 'Found', {}, 'https://elsewhere.example/') is None


@pytest.mark.parametrize('endpoint', ['http://api.openai.com/v1', 'https://api.openai.com.evil.example/v1',
                                      'https://evil.example/v1', 'https://api.openai.com@evil.example/v1'])
def test_the_key_is_never_sent_to_another_host_or_over_http(endpoint, monkeypatch):
    monkeypatch.setattr(OpenerDirector, 'open', lambda *a, **k: pytest.fail('a request was made'))
    deployment = json.loads(json.dumps(native_catalog.bundled('openai-api')))
    deployment['native_profile']['endpoint'] = endpoint
    with pytest.raises(ValueError) as error:
        REAL_LISTING(deployment, KEY)
    assert KEY not in str(error.value)


def test_a_failure_raises_an_error_that_does_not_carry_the_key(monkeypatch):
    def refused(self, request, data=None, timeout=None):
        raise OSError(f'rejected {request.get_header("Authorization")} {KEY}')
    monkeypatch.setattr(OpenerDirector, 'open', refused)
    with pytest.raises(OSError) as error:
        REAL_LISTING(native_catalog.bundled('openai-api'), KEY)
    assert KEY not in str(error.value) and 'OSError' in str(error.value)
    assert error.value.__context__ is None and error.value.__cause__ is None
    monkeypatch.setattr(OpenerDirector, 'open', lambda *a, **k: Reply(b'not json ' + KEY.encode()))
    with pytest.raises(OSError) as error:
        REAL_LISTING(native_catalog.bundled('openai-api'), KEY)
    assert KEY not in str(error.value)
