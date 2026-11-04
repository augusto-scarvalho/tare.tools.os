"""Real provider wire contracts, secret boundaries and bounded search recovery; offline."""
from contextlib import nullcontext
from io import BytesIO
import json
import os
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest

from relay import environment, web_search as search

KEY = 'fixture-search-key-do-not-print'
ADDRESS = 'https://docs.example.org/guide'


@pytest.fixture
def wire(monkeypatch):
    observed = []
    response = {'value': None}
    def opener(*handlers):
        assert isinstance(handlers[0], search.NoRedirect)
        def send(request, **kwargs):
            observed.append((request, kwargs))
            if isinstance(response['value'], Exception):
                raise response['value']
            return nullcontext(BytesIO(json.dumps(response['value']).encode()))
        return SimpleNamespace(open=send)
    monkeypatch.setattr(search, 'build_opener', opener)
    return response, observed


@pytest.mark.parametrize('provider', search.PROVIDERS)
def test_each_search_api_uses_its_documented_wire_contract_and_normalizes_results(wire, provider):
    response, observed = wire
    row = {'url': ADDRESS, 'link': ADDRESS, 'title': 'Official guide', 'snippet': 'A short source excerpt',
           'description': 'A short source excerpt', 'content': 'Do not include entire pages', 'date': '2026-10-25'}
    response['value'] = {'deepseek': {'results': [row]}, 'serpapi': {'organic_results': [row]},
                         'firecrawl': {'success': True, 'data': {'web': [row]}, 'creditsUsed': 2}}[provider]
    value = search.fetch(provider, 'API documentation', 3, KEY)
    assert value['status'] == 'COMPLETED' and value['provider'] == provider
    assert value['results'] == [{'url': ADDRESS, 'title': row['title'], 'snippet': row['snippet'], 'date': row['date']}]
    assert value['untrusted'] and value['authority'] == 'NONE' and 'not been read' in value['evidence']
    assert 'entire pages' not in json.dumps(value) and KEY not in json.dumps(value)
    request, options = observed[0]
    assert options == {'timeout': 15}
    assert request.full_url.startswith(search.PROVIDERS[provider]['endpoint'])
    if provider == 'serpapi':
        assert request.get_method() == 'GET'
        assert parse_qs(urlsplit(request.full_url).query) == {
            'api_key': [KEY], 'engine': ['google'], 'num': ['3'], 'q': ['API documentation']}
    else:
        assert request.get_method() == 'POST' and request.headers['Authorization'] == 'Bearer '+KEY
        body = json.loads(request.data)
        assert body == ({'query': 'API documentation', 'count': 3, 'need_content': False} if provider == 'deepseek'
                        else {'query': 'API documentation', 'limit': 3, 'sources': ['web'],
                              'domainTools': False, 'timeout': 15000})
        assert 'scrapeOptions' not in body  # Discovery does not silently crawl all results.


@pytest.mark.parametrize('code,safe', [(401, True), (402, True), (403, True), (429, True),
                                     (400, False), (302, False), (500, False), (504, False)])
def test_only_explicit_auth_or_quota_refusals_allow_fallback_without_echoing_secrets(wire, code, safe):
    response, _ = wire
    response['value'] = HTTPError('https://serpapi.com/search.json?api_key='+KEY, code, KEY, {}, None)
    value = search.fetch('serpapi', 'docs', 2, KEY)
    assert value['http_status'] == code and value['safe_fallback'] is safe
    assert KEY not in json.dumps(value) and 'api_key=' not in json.dumps(value)


def test_results_are_deduplicated_validated_and_bounded_by_utf8_bytes(wire):
    response, _ = wire
    response['value'] = {'results': [
        {'url': 'file:///private', 'title': 'Reject'}, {'url': 'https://a:b@example.org', 'title': 'Reject'},
        {'url': ADDRESS, 'title': KEY+'\x1b'+('é'*500), 'snippet': '🙂'*2000},
        {'url': ADDRESS, 'title': 'Duplicate'}, {'url': 'https://example.org/'+KEY},
        {'url': 'https://example.org/'+('x'*2100)}]}
    value = search.fetch('deepseek', 'docs', 5, KEY)
    assert len(value['results']) == 1
    assert KEY not in json.dumps(value) and '\x1b' not in value['results'][0]['title']
    assert len(value['results'][0]['snippet'].encode()) <= 1200
    assert len(value['results'][0]['title'].encode()) <= 256
    assert search.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://elsewhere.test') is None


@pytest.fixture
def host(tmp_path, monkeypatch):
    root = tmp_path/'session'; root.mkdir()
    monkeypatch.setattr(search, 'secret', lambda name: KEY)
    control = SimpleNamespace(should_stop=lambda: False, remaining_seconds=lambda: 90)
    calls = []
    def request(provider, query, limit, control, directory):
        calls.append(provider)
        return {'status': 'COMPLETED', 'provider': provider, 'results': [], 'fetched_at': 100,
                'untrusted': True, 'safe_fallback': False}
    monkeypatch.setattr(search, 'request', request)
    return root, control, calls


def test_explicit_service_and_machine_choices_are_honored_before_any_http(host):
    root, control, calls = host
    value = environment.choose({}, 'search:deepseek', False)
    value['search_providers']['preferred'] = 'firecrawl'
    environment.save(value)
    assert search.selection()[0] == ['firecrawl', 'serpapi']
    assert not search.selection('deepseek')[0]
    value['search_providers']['use'] = False
    environment.save(value)
    assert search.search(root, {'query': 'docs'}, 'one', 's', control)['status'] == 'UNAVAILABLE'
    assert calls == []


def test_queries_are_reused_without_resetting_evidence_age_and_refresh_is_explicit(host):
    root, control, calls = host
    first = search.search(root, {'query': 'docs'}, 'one', 's', control)
    second = search.search(root, {'query': 'docs'}, 'two', 's', control)
    assert calls == ['deepseek'] and second['reused'] and second['fetched_at'] == first['fetched_at'] == 100
    search.search(root, {'query': 'docs', 'refresh': True}, 'three', 's', control)
    assert calls == ['deepseek', 'deepseek']
    assert search.summary(root)['requests'] == 2 and search.summary(root)['cost_usd'] is None


def test_api_fallback_is_serial_and_stops_on_uncertain_or_paid_results(host, monkeypatch):
    root, control, calls = host
    answers = [{'status': 'RATE_LIMITED', 'safe_fallback': True},
               {'status': 'UNKNOWN', 'safe_fallback': False}]
    def request(provider, *args):
        calls.append(provider)
        return answers.pop(0)
    monkeypatch.setattr(search, 'request', request)
    value = search.search(root, {'query': 'docs'}, 'one', 's', control)
    assert value['status'] == 'UNKNOWN' and calls == ['deepseek', 'serpapi']
    assert value['requests_used'] == 2
    again = search.search(root, {'query': 'docs'}, 'two', 's', control)
    assert again['reused'] and calls == ['deepseek', 'serpapi']


def test_search_budget_and_retention_do_not_become_a_second_web_archive(host, monkeypatch):
    root, control, calls = host
    monkeypatch.setattr(search, 'MAX_REQUESTS', 20)
    for i in range(10):
        search.search(root, {'query': 'query '+str(i)}, str(i), 's', control)
    rows = [v for _, v in search.records(root)]
    assert len(rows) == 10 and sum('result' in r for r in rows) == 8
    assert search.summary(root)['requests'] == 10 and len(calls) == 10
    monkeypatch.setattr(search, 'MAX_REQUESTS', 10)
    assert search.search(root, {'query': 'over budget'}, 'extra', 's', control)['status'] == 'LIMIT_REACHED'
    assert len(calls) == 10


@pytest.mark.parametrize('args', [{'query': ''}, {'query': 'x'*129}, {'query': 'line\nbreak'},
    {'query': 'docs', 'limit': True}, {'query': 'docs', 'limit': 11}, {'query': 'docs', 'provider': 'other'},
    {'query': 'docs', 'api_key': KEY}, {'query': 'docs', 'refresh': 'yes'}])
def test_invalid_queries_never_dispatch(host, args):
    root, control, calls = host
    with pytest.raises(ValueError):
        search.search(root, args, 'one', 's', control)
    assert not calls


def test_worker_receives_key_only_in_finite_memory_pipe_and_honors_cancellation(tmp_path, monkeypatch):
    monkeypatch.setattr(search, 'secret', lambda name: KEY)
    seen = []
    def run(argv, **kwargs):
        seen.append((argv, kwargs))
        return {'cleanup_verified': True, 'exit_code': 0, 'cancelled': True}, b'', b''
    monkeypatch.setattr('compute_plane.managed_process.run_pipe_process', run)
    control = SimpleNamespace(should_stop=lambda: True, remaining_seconds=lambda: 5)
    result = search.request('deepseek', 'docs', 3, control, tmp_path)
    assert result['status'] == 'CANCELLED' and not result['safe_fallback']
    argv, kwargs = seen[0]
    assert KEY not in json.dumps(argv) and 'key' not in ' '.join(argv)
    assert json.loads(kwargs['stdin'])['key'] == KEY and kwargs['timeout'] == 5
    assert kwargs['cancelled'] is control.should_stop and not list(tmp_path.iterdir())


@pytest.mark.parametrize('provider', search.PROVIDERS)
def test_search_keys_use_the_existing_vault_and_environment_precedence(monkeypatch, provider):
    env = search.PROVIDERS[provider]['key']
    seen = []
    def vault(name):
        seen.append(name)
        return KEY
    monkeypatch.setattr('relay.native_generation.vault_secret', vault)
    monkeypatch.setenv(env, 'fixture-environment-key')
    assert search.secret(provider) == 'fixture-environment-key' and not seen
    monkeypatch.delenv(env)
    assert search.secret(provider) == KEY and seen == [env]


def test_an_unreadable_policy_never_reaches_a_provider_or_looks_up_its_key(host, monkeypatch):
    root, control, calls = host
    environment.store().parent.mkdir(parents=True, exist_ok=True)
    environment.store().write_text('{broken')
    monkeypatch.setattr(search, 'secret', lambda name: pytest.fail('Unreadable settings must fail closed'))
    assert search.search(root, {'query': 'docs'}, 'one', 's', control)['status'] == 'UNAVAILABLE'
    assert not calls


@pytest.mark.parametrize('kind', ['search', 'page'])
def test_real_supervised_workers_keep_unicode_under_a_non_utf8_host_locale(tmp_path, monkeypatch, kind):
    from compute_plane.managed_process import run_pipe_process
    from relay import web_research
    text = 'Ol\u00e1 \U0001f600'
    physical = []
    def exchange(argv, **kwargs):
        argv = list(argv)
        program = argv.index('-c')+1
        if kind == 'search':
            hook = ('import relay.web_search as m; m.fetch=lambda *a: '+
                    repr({'status': 'COMPLETED', 'results': [{'url': ADDRESS, 'title': text}],
                          'safe_fallback': False})+'; m.worker()')
            # ASCII program arguments also work when the child decodes argv under C.
            hook = hook.encode('ascii', 'backslashreplace').decode()
            argv[program] = argv[program].replace('from relay.web_search import worker; worker()', hook)
        else:
            hook = ('from types import SimpleNamespace; from relay.web_reader import WebReader; '
                    'WebReader.fetch_clean_markdown=lambda address: SimpleNamespace(markdown='+ascii(text)+
                    ',final_url=address,title='+ascii(text)+',status="OK",error=None,method="fixture"); '
                    'from relay.web_research import read_worker; read_worker(sys.argv[1])')
            argv[program] = argv[program].replace('from relay.web_research import read_worker; read_worker(sys.argv[1])', hook)
        # Simulate non-UTF-8 startup even when the normal launch selects UTF-8.
        # The worker's wire encoding must be explicit, independent of TextIO.
        argv[1:1] = ['-X', 'utf8=0']
        kwargs['env'] = {**os.environ, 'LC_ALL': 'C', 'LANG': 'C'}
        result = run_pipe_process(argv, **kwargs)
        assert result[0]['exit_code'] == 0, result[2].decode('utf-8', 'replace')[-400:]
        physical.append({**result[0], 'diagnostic': result[2].decode('utf-8', 'replace')[-400:]})
        return result
    monkeypatch.setattr('compute_plane.managed_process.run_pipe_process', exchange)
    monkeypatch.setattr(search, 'secret', lambda name: KEY)
    control = SimpleNamespace(should_stop=lambda: False, remaining_seconds=lambda: 60)
    value = (search.request('deepseek', 'docs', 1, control, tmp_path) if kind == 'search'
             else web_research.read_page(ADDRESS, control, directory=tmp_path))
    assert value['status'] == ('COMPLETED' if kind == 'search' else 'OK'), (value, physical)
    assert (value['results'][0]['title'] if kind == 'search' else value['text']) == text
    assert physical[0]['cleanup_verified'] and not list(tmp_path.iterdir())
