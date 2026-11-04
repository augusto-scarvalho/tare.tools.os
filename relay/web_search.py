"""Bounded search APIs: host-held keys, short results, no extra model or crawler."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from compute_plane.native_journal import regular, save_json
from compute_plane.native_models import encode

from . import web_research as web

PROVIDERS = {
    'deepseek': {'label': 'DeepSeek Search', 'key': 'DEEPSEEK_API_KEY',
                 'endpoint': 'https://api.deepseek.com/v1/tools/search'},
    'serpapi': {'label': 'SerpApi · Google', 'key': 'SERPAPI_API_KEY',
                'endpoint': 'https://serpapi.com/search.json'},
    'firecrawl': {'label': 'Firecrawl Search', 'key': 'FIRECRAWL_API_KEY',
                  'endpoint': 'https://api.firecrawl.dev/v2/search'},
}
CREDENTIALS = {p['key']: name for name, p in PROVIDERS.items()}
SCHEMA = 'tare.tools/web-search/1'
MAX_REQUESTS, MAX_RESPONSE = 8, 256*1024
SPEC = {'description': 'Search the internet directly through a configured search API, without another model. '
    'Use for direct lookup and whenever the user requests configured search providers: set provider to the '
    'named service. These API tools are already implemented; using them needs no API client code or planner. '
    'Returns short untrusted search snippets and source URLs, not verified page contents: use read_url on '
    'relevant results before making source-backed claims. Never send repository contents, credentials or '
    'chat history as a query. Automatic selects connected services, with bounded fallback only after an '
    'explicit authentication/quota refusal. Timeouts and uncertain results are not automatically repeated. '
    'Recent identical queries reuse results for one hour. Refresh only when fresh results are requested.',
    'parameters': {'type': 'object', 'properties': {
        'query': {'type': 'string', 'minLength': 1, 'maxLength': 128},
        'limit': {'type': 'integer', 'minimum': 1, 'maximum': 10},
        'provider': {'type': 'string', 'enum': ['auto', *PROVIDERS]},
        'refresh': {'type': 'boolean'}}, 'required': ['query'], 'additionalProperties': False}}


def secret(name):
    from .native_generation import vault_secret
    env = PROVIDERS[name]['key']
    return (os.environ.get(env) or vault_secret(env) or '').strip()


def selection(provider='auto'):
    from .environment import declined, read
    if provider not in ('auto', *PROVIDERS):
        raise ValueError('Choose auto, deepseek, serpapi or firecrawl.')
    value = read()
    section = value.get('search_providers') or {}
    if 'error' in value or section.get('use') is False:
        return [], [{'reason': 'Web search APIs are disabled or machine settings could not be read.'}]
    preferred = section.get('preferred', 'auto')
    items = section.get('items') or {}
    if preferred not in ('auto', *PROVIDERS) or not isinstance(items, dict):
        return [], [{'reason': 'Web search settings are invalid. Review Web search in setup.'}]
    order = list(PROVIDERS) if provider == 'auto' else [provider]
    if provider == 'auto' and preferred != 'auto':
        order.remove(preferred)
        order.insert(0, preferred)
    ready, skipped = [], []
    for name in order:
        if declined(items.get(name)):
            skipped.append({'provider': name, 'reason': 'Disabled in machine settings.'})
        elif not secret(name):
            skipped.append({'provider': name, 'reason': 'No API key configured. Add it in setup · Web search.'})
        else:
            ready.append(name)
    return ready, skipped


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # A search key belongs only to the fixed provider endpoint.


def fetch(provider, query, limit, key):
    """Worker-only HTTP boundary. Never return raw errors, request URLs or provider bodies."""
    endpoint = PROVIDERS[provider]['endpoint']
    headers = {'Accept': 'application/json'}
    body = None
    if provider == 'serpapi':
        endpoint += '?' + urlencode({'engine': 'google', 'q': query, 'num': limit, 'api_key': key})
    else:
        headers.update(Authorization='Bearer '+key, **{'Content-Type': 'application/json'})
        fields = {'query': query, 'count': limit, 'need_content': False} if provider == 'deepseek' else {
            'query': query, 'limit': limit, 'sources': ['web'], 'domainTools': False, 'timeout': 15000}
        body = encode(fields).encode()
    try:
        with build_opener(NoRedirect()).open(Request(endpoint, data=body, headers=headers), timeout=15) as reply:
            raw = reply.read(MAX_RESPONSE+1)
        if len(raw) > MAX_RESPONSE:
            return {'status': 'UNKNOWN', 'reason': 'Search response exceeded its size limit.', 'safe_fallback': False}
        value = json.loads(raw)
        if not isinstance(value, dict) or 'error' in value or value.get('success') is False:
            raise ValueError('unusable provider response')
        rows = value.get('results') if provider == 'deepseek' else value.get('organic_results', []) \
            if provider == 'serpapi' else (value.get('data') or {}).get('web')
        if not isinstance(rows, list):
            raise ValueError('missing result list')
        results, seen = [], set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                address = web.url(row.get('link' if provider == 'serpapi' else 'url'))
            except ValueError:
                continue
            if address in seen or key in address or len(address.encode()) > 2048:
                continue
            seen.add(address)
            def clean(value, cap):
                value = ''.join(c for c in str(value or '').replace(key, '[redacted]') if ord(c) >= 32)
                return value.encode()[:cap].decode('utf-8', 'ignore')
            results.append({'url': address, 'title': clean(row.get('title'), 256),
                'snippet': clean(row.get('snippet') or row.get('description') or row.get('summary'), 1200),
                'date': clean(row.get('date'), 64) or None})
            if len(results) == limit:
                break
        return {'status': 'COMPLETED', 'results': results, 'safe_fallback': False,
                'fetched_at': time.time(), 'provider': provider, 'untrusted': True,
                'evidence': 'search snippets; pages have not been read', 'authority': 'NONE'}
    except HTTPError as exc:
        status = {401: 'AUTH_REQUIRED', 402: 'QUOTA_UNAVAILABLE', 403: 'ACCESS_DENIED',
                  429: 'RATE_LIMITED'}.get(exc.code, 'UNKNOWN')
        return {'status': status, 'http_status': exc.code, 'safe_fallback': exc.code in {401, 402, 403, 429},
                'reason': 'Search service refused or failed this request. Check its key, access and credit.'}
    except (OSError, URLError, ValueError, TypeError, AttributeError):
        return {'status': 'UNKNOWN', 'reason': 'Search did not return a usable result. No automatic retry.',
                'safe_fallback': False}


def worker():
    try:
        data = json.loads(sys.stdin.buffer.read(16385))
        result = fetch(data['provider'], data['query'], data['limit'], data['key'])
    except Exception:
        result = {'status': 'UNKNOWN', 'reason': 'Search worker could not complete. No automatic retry.',
                  'safe_fallback': False}
    sys.stdout.buffer.write(encode(result).encode('utf-8')+b'\n')


def request(provider, query, limit, control, directory):
    from compute_plane.managed_process import run_pipe_process
    key = secret(provider)
    if not key:
        return {'status': 'AUTH_REQUIRED', 'reason': 'Search key is no longer available.', 'safe_fallback': True}
    roots = [str(Path(__file__).resolve().parents[1])]
    spec = importlib.util.find_spec('compute_plane')
    if spec and spec.origin:
        roots.append(str(Path(spec.origin).resolve().parent.parent))
    program = ('import sys; sys.path[:0] = '+repr(list(dict.fromkeys(roots)))+
               '; from relay.web_search import worker; worker()')
    physical, raw, _ = run_pipe_process([sys.executable, '-I', '-B', '-X', 'utf8', '-c', program],
        stdin=encode({'provider': provider, 'query': query, 'limit': limit, 'key': key}).encode(),
        cwd=directory, timeout=min(20, control.remaining_seconds()), cancelled=control.should_stop,
        max_output=64*1024)
    if (not physical.get('cleanup_verified') or physical.get('exit_code') != 0 or
            any(physical.get(k) for k in ('timeout', 'cancelled', 'output_limit'))):
        return {'status': 'CANCELLED' if physical.get('cancelled') else 'UNKNOWN',
                'reason': 'Search was interrupted or timed out. No automatic retry.', 'safe_fallback': False}
    try:
        result = json.loads(raw)
        if isinstance(result, dict) and key not in encode(result):
            return result
    except ValueError:
        pass
    return {'status': 'UNKNOWN', 'reason': 'Search worker returned an invalid result.', 'safe_fallback': False}


def records(root):
    from .native_agent import read_json
    return [(p, read_json(regular(p), 64*1024)) for p in regular(Path(root)/'web-search').glob('*/receipt.json')]


def summary(root):
    rows = [r for _, r in records(root)]
    return {'requests': sum(r.get('requests_used', r.get('requests_reserved', 0)) for r in rows),
            'observed_requests': len(rows), 'cost_usd': None}


def search(root, args, effect_id, session_id, control):
    """One owner of a finite per-user request budget; unknown effects never replay themselves."""
    if (set(args)-{'query', 'limit', 'provider', 'refresh'} or not isinstance(args.get('query'), str)
            or not 1 <= len(args['query'].strip()) <= 128 or any(ord(c) < 32 for c in args['query'])
            or type(args.get('limit', 5)) is not int or not 1 <= args.get('limit', 5) <= 10
            or type(args.get('refresh', False)) is not bool):
        raise ValueError('Use one search query of 1–128 characters and a limit from 1 to 10.')
    order, skipped = selection(args.get('provider', 'auto'))
    if not order:
        return {'status': 'UNAVAILABLE', 'reason': 'No search API is ready. Configure setup · Web search.',
                'details': skipped, 'authority': 'NONE'}
    query, limit = args['query'].strip(), args.get('limit', 5)
    identity = web.key({'query': query, 'limit': limit, 'providers': order})
    from compute_plane.native_journal import NativeJournal
    messages = [r.event() for r in NativeJournal(Path(root)/'journal.jsonl').snapshot().records
                if r.event()['kind'] == 'user_message']
    input_id = messages[-1]['message_id'] if messages else 'initial-'+session_id
    earlier = sorted(records(root), key=lambda item: item[1].get('finished_at', 0), reverse=True)
    if not args.get('refresh'):
        for _, row in earlier:
            if row.get('query_sha256') != identity:
                continue
            if row.get('result') and row.get('status') == 'COMPLETED' and \
                    time.time()-row.get('finished_at', 0) < web.CACHE_SECONDS:
                return {**row['result'], 'reused': True}
            if row.get('input_id') == input_id and row.get('status') in {'RUNNING', 'UNKNOWN', 'CANCELLED'}:
                return {**(row.get('result') or {'status': 'UNKNOWN', 'reason': 'Search outcome is unknown; no automatic replay.'}),
                        'reused': True, 'authority': 'NONE'}
    used = sum(r.get('requests_used', r.get('requests_reserved', 0)) for _, r in earlier if r.get('input_id') == input_id)
    if used >= MAX_REQUESTS or control.should_stop() or control.remaining_seconds() < 2:
        return {'status': 'LIMIT_REACHED', 'reason': 'Search stopped or this user request used its search budget.', 'authority': 'NONE'}
    job = regular(Path(root)/'web-search'/web.key(effect_id))
    job.mkdir(parents=True, exist_ok=True)
    row = {'schema': SCHEMA, 'request_sha256': web.key(args), 'query_sha256': identity, 'input_id': input_id,
           'status': 'RUNNING', 'requests_reserved': min(len(order), MAX_REQUESTS-used), 'requests_used': 0}
    save_json(job/'receipt.json', row)
    result, attempts = {}, []
    for provider in order[:row['requests_reserved']]:
        if control.should_stop() or control.remaining_seconds() < 2:
            result = {'status': 'CANCELLED', 'reason': 'Search stopped before another request.'}
            break
        row.update(requests_used=row['requests_used']+1, provider=provider)
        save_json(job/'receipt.json', row)  # Reservation survives a crash during HTTP.
        result = request(provider, query, limit, control, job)
        attempts.append({'provider': provider, 'status': result['status']})
        if result['status'] == 'COMPLETED' or not result.get('safe_fallback'):
            break
    result.pop('safe_fallback', None)
    result.update(provider=row.get('provider'), attempts=attempts, skipped=skipped,
                  requests_used=row['requests_used'], reused=False, authority='NONE')
    row.update(status=result.get('status', 'UNKNOWN'), result=result, finished_at=time.time())
    save_json(job/'receipt.json', row)
    # Eight small result sets, then metadata only; no raw HTML, screenshots or corpus.
    full = sorted(((p, r) for p, r in records(root) if r.get('result')),
                  key=lambda item: item[1].get('finished_at', 0), reverse=True)
    for target, saved in full[8:]:
        saved.pop('result')
        save_json(target, saved)
    return result
