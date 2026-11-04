"""Bounded web evidence, independent of repository exploration and delivery."""
from copy import deepcopy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit, urlunsplit

from compute_plane.native_journal import regular, save_json
from compute_plane.native_models import encode

SCHEMA = 'tare.tools/web-research/1'
MAX_TEXT, MAX_SOURCES, MAX_REQUESTS = 65536, 5, 8
CACHE_SECONDS = 3600
from .openrouter import RESEARCH_ROUTES
DEFAULT_ROUTES = ('codex-cli-gpt-6-luna', 'claude-haiku-5-5-cli', 'agy-cli', *RESEARCH_ROUTES)


def url(value):
    if not isinstance(value, str) or len(value) > 4096 or any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError('Use a concrete HTTP or HTTPS URL.')
    parts = urlsplit(value)
    if parts.scheme not in {'http', 'https'} or not parts.hostname or parts.username or parts.password:
        raise ValueError('Use an HTTP or HTTPS URL without embedded credentials.')
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ''))


def key(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()[:24]


def snapshot():
    from . import native_catalog
    from .turn_engine import policy
    from .execution_scope import current, pinned, for_role
    if current().pin('researcher'):
        with for_role('researcher'):value=pinned('researcher')
        name=value['native_model_choice']['connection']
        return {'version':1,'routes':{name:value},'route_order':[name],'unavailable':{},'search_api':1,
                'origin':'EXACT_TEMPORARY_PIN'}
    routes, skipped = {}, {}
    from .native_routes import configured
    api_routes = {r.get('native_catalog', r['id']) for r in configured()['deployments'] if 'researcher' in r['roles']}
    for name in policy().get('researcher', DEFAULT_ROUTES):
        try:
            value = native_catalog.select(name)
            profile = value['native_profile']
            from .openrouter import is_route
            if not ((profile['protocol'] == 'vendor-cli' and profile['endpoint'] in {'cli://codex', 'cli://claude', 'cli://agy'})
                    or is_route(value) and name in api_routes):
                raise ValueError('Choose an admitted research CLI or API connection.')
            from .environment import allows
            allowed, reason = allows(value)
            if not allowed:
                raise ValueError(str(reason))
            routes[name] = deepcopy(value)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            skipped[name] = str(exc)[:200]
    return {'version': 1, 'routes': routes, 'route_order': list(routes), 'unavailable': skipped, 'search_api': 1,
            'origin': 'CONFIGURED'}


def research_tools(tools, root, state):
    """Project this invocation's researcher without changing a saved session or schema."""
    from .research_tools import ResearchTools
    from .execution_scope import current, pinned, for_role
    class ScopedResearchTools(ResearchTools):
        def investigate(self, request, args, control):
            with for_role('researcher'):
                if not current().pin('researcher'):
                    return super().investigate(request,args,control)
                value=pinned('researcher')
                name=value['native_model_choice']['connection']
                if args.get('route') not in (None,name):
                    raise ValueError('Research route conflicts with --researcher; consult tare models --role researcher.')
                transient=object.__new__(ResearchTools)
                transient.__dict__.update(self.__dict__)
                transient.state={**self.state,'research':{'routes':{name:value},'route_order':[name],
                                                       'origin':'EXACT_TEMPORARY_PIN'}}
                result=ResearchTools.investigate(transient,request,{**args,'route':name},control)
                if isinstance(result,dict) and not result.get('reused') and 'effort' not in result:
                    result={**result,'effort':value['native_model_choice']['reasoning']}
                return result
    return ScopedResearchTools(tools,root,state)


def read_page(address, control, *, directory):
    """The existing Trafilatura reader in a supervised worker: timeout, cancellation and bounded output."""
    from compute_plane.managed_process import run_pipe_process
    address = url(address)
    roots = [str(Path(__file__).resolve().parents[1])]
    for name in ('compute_plane', 'tare_tools_agent_runtime', 'trafilatura'):
        spec = importlib.util.find_spec(name)
        if spec and spec.origin:
            roots.append(str(Path(spec.origin).resolve().parent.parent))
    program = ('import sys; sys.path[:0] = ' + repr(list(dict.fromkeys(roots)))
               + '; from relay.web_research import read_worker; read_worker(sys.argv[1])')
    physical, raw, _ = run_pipe_process([sys.executable, '-I', '-B', '-X', 'utf8', '-c', program, address],
        cwd=directory, timeout=min(30, control.remaining_seconds()), cancelled=control.should_stop,
        max_output=512*1024)
    if (not physical.get('cleanup_verified') or physical.get('exit_code') != 0
            or any(physical.get(k) for k in ('timeout', 'cancelled', 'output_limit'))):
        return {'url': address, 'status': 'UNAVAILABLE', 'error':
                'URL read interrupted, timed out or exceeded its output limit.', 'process': physical}
    value = json.loads(raw)
    value['url'] = address
    return value


def read_worker(address):
    from .web_reader import WebReader
    doc = WebReader.fetch_clean_markdown(url(address))
    raw = (doc.markdown or '').encode()
    text = raw[:MAX_TEXT].decode('utf-8', 'ignore')
    value = {'url': address, 'final_url': doc.final_url, 'title': (doc.title or '')[:512],
        'status': doc.status, 'error': doc.error, 'method': doc.method, 'text': text,
        'sha256': hashlib.sha256(text.encode()).hexdigest(), 'truncated': len(raw) > MAX_TEXT,
        'hash_basis': 'UTF-8 cleaned page text, capped at 64 KiB; not original HTTP response bytes.',
        'fetched_at': time.time(), 'untrusted': True}
    sys.stdout.buffer.write(encode(value).encode('utf-8')+b'\n')


def page(root, address, control, *, refresh=False):
    address = url(address)
    cache = regular(Path(root)/'web-pages')
    cache.mkdir(parents=True, exist_ok=True)
    target = regular(cache/(key(address)+'.json'))
    if target.exists() and not refresh:
        from .native_agent import read_json
        saved = read_json(target, 512*1024)
        if saved.get('url') == address and time.time()-saved.get('fetched_at', 0) < CACHE_SECONDS:
            return {**saved, 'reused': True}
    value = read_page(address, control, directory=cache)
    if value.get('status') == 'OK':
        save_json(target, value)
        # Small session-local evidence cache, never a second corpus/archive.
        older = sorted(cache.glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)[8:]
        for old in older:
            regular(old).unlink()
    return {**value, 'reused': False}


def records(root):
    from .native_agent import read_json
    return [read_json(regular(p), 128*1024) for p in sorted(regular(Path(root)/'web-research').glob('*/receipt.json'))]


def committed(root):
    counts = [row.get('calls_used', row.get('calls_reserved', 0)) for row in records(root)]
    return sum(counts) if all(value is not None for value in counts) else None


def trim_reports(root):
    """Keep eight reusable reports; old receipts retain accounting, not another full text archive."""
    from .native_agent import read_json
    full = []
    for target in regular(Path(root)/'web-research').glob('*/receipt.json'):
        value = read_json(regular(target), 128*1024)
        if 'result' in value and value.get('status') != 'RUNNING':
            full.append((value.get('finished_at', target.stat().st_mtime), target, value))
    for _, target, value in sorted(full, key=lambda row: row[0], reverse=True)[8:]:
        value.pop('result')
        value.pop('proposal', None)
        save_json(target, value)


def summary(root):
    rows = records(root)
    return {'requests': len(rows), 'model_calls_committed': committed(root), 'authority': 'NONE',
        'records': [{k: r.get(k) for k in ('route', 'model', 'effort', 'status', 'usage', 'session_reused', 'error')} for r in rows],
        'cost_scope': 'Research CLI usage is separate from the coordinator usage file; absent usage stays unknown.'}


def visible_text(text):
    """Compare visible text, not Markdown's code/link delimiters or escaped punctuation."""
    from markdown_it import MarkdownIt
    parts = []
    for token in MarkdownIt().parse(text):
        if token.type == 'inline':
            parts.append(''.join(child.content if child.type not in {'softbreak', 'hardbreak'} else ' '
                                 for child in token.children or []))
        elif token.type in {'code_block', 'fence', 'html_block'}:
            parts.append(token.content)
    return ' '.join('\n'.join(parts).split())


def verified_report(root, proposed, control, *, refresh=False):
    """The model locates evidence; the host reads the actual source excerpt, as in project exploration."""
    sources, findings, remaining, rejected = {}, [], list(proposed['remaining']), []
    for item in proposed['sources'][:MAX_SOURCES]:
        address = url(item['url'])
        observed = page(root, address, control, refresh=refresh)
        sources[address] = {k: observed.get(k) for k in ('url', 'final_url', 'title', 'status', 'sha256',
            'hash_basis', 'truncated', 'fetched_at', 'error')}
        sources[address]['text'] = observed.get('text', '')
    for item in proposed['findings']:
        address = url(item['url'])
        source = sources.get(address)
        anchor = ' '.join(item.get('anchor', '').strip('`').split())
        lines = (source or {}).get('text', '').splitlines()
        located = next((i for i, line in enumerate(lines) if anchor and
                        (anchor in ' '.join(line.split()) or anchor in visible_text(line))), None)
        if source and source['status'] == 'OK' and located is not None:
            start, end = max(0, located-1), min(len(lines), located+4)
            excerpt = '\n'.join(lines[start:end])
            findings.append({**item, 'quote': excerpt[:1600], 'start_line': start+1, 'end_line': end,
                'excerpt_truncated': len(excerpt) > 1600, 'source_sha256': source.get('sha256'),
                'evidence_basis': 'Host-read source range; the claim meaning is not independently audited.'})
        else:
            remaining.append('Could not locate the proposed evidence anchor in: '+address)
            rejected.append({'url': address, 'proposed_anchor': item.get('anchor', '')[:240],
                'reason': 'anchor_not_found' if source and source['status'] == 'OK' else 'source_unavailable'})
    for source in sources.values():
        source.pop('text', None)
    complete = proposed['status'] == 'COMPLETED' and bool(findings) and not remaining
    return {'schema': SCHEMA, 'status': 'COMPLETED' if complete else 'PARTIAL',
        'summary': proposed['summary'] if complete else 'Verified findings and remaining gaps are listed below.',
        'findings': findings, 'sources': list(sources.values()), 'remaining': remaining,
        'rejected_excerpts': rejected,
        'authority': 'NONE', 'untrusted': True}
