"""Bounded web evidence, independent of repository exploration and delivery."""
from copy import deepcopy
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import socket
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


PUBLIC_ONLY = 'tare reads only public web addresses, never this machine, its local network or Tailscale.'
LOCAL_NAMES = ('.localhost', '.local', '.internal', '.ts.net', '.home.arpa', '.lan')
NAT64, GLOBAL_UNICAST_V6 = ipaddress.ip_network('64:ff9b::/96'), ipaddress.ip_network('2000::/3')


def public(address):
    """Only a global unicast address; anything that does not parse is refused. is_global already refuses
    loopback, RFC 1918, link-local, 100.64.0.0/10, reserved and (3.12.4+) IPv4-mapped forms of those, but it
    accepts multicast and IPv4-compatible or site-local IPv6."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if ip.version == 6 and (ip.ipv4_mapped is not None or ip in NAT64):
        ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)  # the IPv4 address it reaches
    return ip.is_global and not ip.is_multicast and (ip.version == 4 or ip in GLOBAL_UNICAST_V6)


def refusal(address):
    """Why this URL is refused before any DNS query, or None. A name that resolves to a non-public address is
    refused later, when the worker connects (guard_sockets)."""
    try:
        host = (urlsplit(address).hostname or '').encode('idna').decode('ascii').rstrip('.')
    except UnicodeError:
        host = ''
    try:  # inet_aton reads every IPv4 form a resolver accepts: 2130706433, 0x7f000001, 0177.0.0.1, 127.1
        literal = host if ':' in host else socket.inet_ntoa(socket.inet_aton(host))
    except OSError:
        literal = None
    allowed = public(literal) if literal else '.' in host and not host.endswith(LOCAL_NAMES)
    return None if allowed else 'Refused '+(host or 'this host')+': '+PUBLIC_ONLY


def guard_sockets():
    """Fail closed for every connection this worker process opens unless the numeric address it actually
    dials is public. Redirects, every resolver path, each of several A/AAAA records and DNS rebinding all end
    in this connect, after resolution, so it is the check nothing can route around. Proxies (which would
    resolve the destination themselves) and libcurl (which opens sockets outside Python) are turned off."""
    refused = []
    def check(connect):
        def guarded(sock, address):
            if not (isinstance(address, tuple) and public(address[0])):
                refused.append(address)
                raise PermissionError('Refused a non-public address: '+PUBLIC_ONLY)
            return connect(sock, address)
        return guarded
    socket.socket.connect, socket.socket.connect_ex = check(socket.socket.connect), check(socket.socket.connect_ex)
    for name in [n for n in os.environ if n.lower().endswith('_proxy')]:
        del os.environ[name]
    os.environ['no_proxy'] = '*'  # also keeps urllib from reading the Windows registry proxy
    sys.modules['pycurl'] = None  # Trafilatura prefers pycurl when it is installed
    return refused


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


def read_page(address, control, *, directory, limit=MAX_TEXT):
    """The existing Trafilatura reader in a supervised worker: timeout, cancellation and bounded output."""
    from compute_plane.managed_process import run_pipe_process
    address = url(address)
    refused = refusal(address)
    if refused:
        return {'url': address, 'status': 'REFUSED', 'error': refused}
    roots = [str(Path(__file__).resolve().parents[1])]
    for name in ('compute_plane', 'tare_tools_agent_runtime', 'trafilatura'):
        spec = importlib.util.find_spec(name)
        if spec and spec.origin:
            roots.append(str(Path(spec.origin).resolve().parent.parent))
    program = ('import sys; sys.path[:0] = ' + repr(list(dict.fromkeys(roots)))
               + '; from relay.web_research import read_worker; read_worker(*sys.argv[1:])')
    physical, raw, _ = run_pipe_process([sys.executable, '-I', '-B', '-X', 'utf8', '-c', program, address, str(limit)],
        cwd=directory, timeout=min(30, control.remaining_seconds()), cancelled=control.should_stop,
        max_output=max(512*1024, 2*limit))
    if (not physical.get('cleanup_verified') or physical.get('exit_code') != 0
            or any(physical.get(k) for k in ('timeout', 'cancelled', 'output_limit'))):
        return {'url': address, 'status': 'UNAVAILABLE', 'error':
                'URL read interrupted, timed out or exceeded its output limit.', 'process': physical}
    value = json.loads(raw)
    value['url'] = address
    return value


def document(address, limit=1024*1024):
    """read_page for tare's other readers (links in a serve request, /research, research flows) as the
    WebDocument they expect. The limit is above each caller's own cut (research keeps 200,000 bytes), so the
    caller still decides and reports truncation."""
    from types import SimpleNamespace
    from .web_reader import WebDocument
    try:
        value = read_page(address, SimpleNamespace(remaining_seconds=lambda: 30, should_stop=lambda: False),
                          directory=None, limit=limit)
    except (ValueError, OSError) as exc:  # not an HTTP(S) URL tare reads, or the worker could not run
        value = {'status': 'ERROR', 'error': str(exc)[:400]}
    text = value.get('text') or ''
    return WebDocument(url=address, title=value.get('title') or address, markdown=text, raw_size_bytes=len(text),
        clean_size_bytes=len(text), reduction_pct=0.0, method=value.get('method') or 'none',
        status=value.get('status', 'ERROR'), error=value.get('error'), final_url=value.get('final_url'))


def read_worker(address, limit=MAX_TEXT):
    limit = int(limit)
    refused = guard_sockets()
    from .web_reader import WebReader
    doc = WebReader.fetch_clean_markdown(url(address))
    if refused:  # even when another DNS record or a later fallback answered
        sys.stdout.buffer.write(encode({'url': address, 'status': 'REFUSED', 'error': 'Refused: this URL, a redirect '
            'or its DNS records lead to a non-public address. '+PUBLIC_ONLY}).encode('utf-8')+b'\n')
        return
    raw = (doc.markdown or '').encode()
    text = raw[:limit].decode('utf-8', 'ignore')
    value = {'url': address, 'final_url': doc.final_url, 'title': (doc.title or '')[:512],
        'status': doc.status, 'error': doc.error, 'method': doc.method, 'text': text,
        'sha256': hashlib.sha256(text.encode()).hexdigest(), 'truncated': len(raw) > limit,
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
