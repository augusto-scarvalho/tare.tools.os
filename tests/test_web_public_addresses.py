"""tare's own reader reaches only public addresses: non-public literals and names are refused before any DNS
query, and the worker refuses every non-public connection, whatever name, record, rebinding or redirect led to it.
The same worker reads links in a serve request, /research pages and research-flow sources."""
import contextlib
import io
import ipaddress
import json
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest

from relay import web_research as web
from test_research_tools import effect, host  # noqa: F401 (fixture)

READ_PAGE = web.read_page  # the host fixture replaces it
CONTROL = SimpleNamespace(should_stop=lambda: False, remaining_seconds=lambda: 30)
PUBLIC_IP = '93.184.215.14'  # a global address that the fixture internet serves from a loopback test server


@pytest.mark.parametrize('address', [
    'http://127.0.0.1:8080/', 'http://2130706433/', 'http://0x7f000001/', 'http://0177.0.0.1/', 'http://127.1/',
    'http://0/', 'http://[::1]/', 'http://[::ffff:127.0.0.1]/', 'http://[::ffff:100.64.0.1]/', 'http://[::127.0.0.1]/',
    'http://[64:ff9b::a00:1]/', 'http://[fe80::1%25eth0]/', 'http://169.254.169.254/latest/meta-data/',
    'http://100.107.245.30:8080/v1/models', 'http://10.0.0.1/', 'http://172.16.0.1/', 'http://192.168.0.1/',
    'http://[fd7a:115c:a1e0::1]/', 'http://[fec0::1]/', 'http://224.0.0.1/', 'http://[ff02::1]/',
    'http://255.255.255.255/', 'http://240.0.0.1/', 'http://localhost:3000/docs', 'http://LOCALHOST./',
    'http://app.localhost/', 'http://printer.local/', 'http://metadata.google.internal/',
    'http://aaaaa.tail1234.ts.net/', 'http://aaaaa:8080/', 'http://１２７．０．０．１/'])
def test_non_public_literals_and_names_never_reach_dns_or_the_worker(address, monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: pytest.fail('DNS query'))
    monkeypatch.setattr('compute_plane.managed_process.run_pipe_process', lambda *a, **k: pytest.fail('worker'))
    value = web.read_page(address, CONTROL, directory=None)
    assert value['status'] == 'REFUSED' and value['error'].endswith(web.PUBLIC_ONLY)


@pytest.mark.parametrize('address', ['https://example.org/docs', f'http://{PUBLIC_IP}/', 'https://[2606:4700::1111]/',
                                     'https://[::ffff:8.8.8.8]/', 'https://bücher.example/'])
def test_public_names_and_addresses_pass_the_literal_check(address):
    assert web.refusal(address) is None


def test_read_url_refusal_is_a_refused_tool_result_and_is_never_cached(host, monkeypatch):  # noqa: F811
    tools, execution, _ = host
    monkeypatch.setattr(web, 'read_page', READ_PAGE)
    monkeypatch.setattr('compute_plane.managed_process.run_pipe_process', lambda *a, **k: pytest.fail('worker'))
    result = tools.execute(effect('read_url', {'url': 'http://169.254.169.254/latest/meta-data/'}), execution)
    assert result.status == 'refused' and web.PUBLIC_ONLY in json.loads(result.output_json)['reason']
    assert not list((tools.root/'web-pages').glob('*'))


@pytest.fixture
def internet(monkeypatch):
    """Names resolve as each test says (never real DNS) and PUBLIC_IP is served by a loopback HTTP server: the
    test talks to itself below the guard; the guarded reader never may."""
    redirects, records, dialled = {}, {}, []

    class Site(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path in redirects:
                self.send_response(302)
                self.send_header('Location', redirects[self.path])
                self.end_headers()
                return
            body = ('<html><head><title>Public</title></head><body><article><p>'
                    + 'Public page text for the reader. ' * 20 + '</p><p>A page-chosen link: <a href='
                    '"http://internal.public.example/admin">the admin page</a>.</p></article></body></html>').encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass
    server = HTTPServer(('127.0.0.1', 0), Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    real_connect, real_resolve = socket.socket.connect, socket.getaddrinfo

    def connect(sock, address):
        dialled.append(address[0])
        if address[0] != PUBLIC_IP:
            raise ConnectionRefusedError('the fixture internet has no such host')
        return real_connect(sock, ('127.0.0.1', server.server_port))

    def resolve(name, port, *args, **kwargs):
        try:
            ipaddress.ip_address(name)
            return real_resolve(name, port, *args, **kwargs)  # numeric: no query leaves the machine
        except ValueError:
            pass
        if name not in records:
            raise socket.gaierror('fixture DNS knows only the test names')
        answers = records[name]
        rows = answers.pop(0) if len(answers) > 1 else answers[0]  # a later query may answer differently
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, '', (a, port, 0, 0)) if ':' in a else
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', (a, port)) for a in rows]
    monkeypatch.setattr(socket.socket, 'connect', connect)
    monkeypatch.setattr(socket.socket, 'connect_ex', socket.socket.connect_ex)  # restored after the guard
    monkeypatch.setattr(socket, 'getaddrinfo', resolve)
    monkeypatch.setitem(sys.modules, 'pycurl', None)
    for name in [n for n in os.environ if n.lower().endswith('_proxy')] + ['no_proxy']:
        monkeypatch.setenv(name, os.environ.get(name, ''))  # the guard edits the environment; restore it
    yield SimpleNamespace(redirects=redirects, records=records, dialled=dialled)
    server.shutdown()
    server.server_close()


@pytest.mark.parametrize('name, answers, path, redirect, status', [
    ('public.example', [[PUBLIC_IP]], '/page', None, 'OK'),
    ('private.example', [['10.0.0.5', PUBLIC_IP]], '/page', None, 'REFUSED'),
    ('tailnet.example', [['fd7a:115c:a1e0::1', PUBLIC_IP]], '/page', None, 'REFUSED'),
    ('rebind.example', [[PUBLIC_IP], ['127.0.0.1']], '/hop', 'http://rebind.example/page', 'REFUSED'),
    ('public.example', [[PUBLIC_IP]], '/hop', 'http://169.254.169.254/latest/meta-data/', 'REFUSED'),
    ('public.example', [[PUBLIC_IP]], '/hop', 'http://100.107.245.30:8080/v1/models', 'REFUSED'),
])
def test_worker_refuses_every_non_public_connection(internet, capsys, name, answers, path, redirect, status):
    internet.records[name] = answers
    if redirect:
        internet.redirects[path] = redirect
    web.read_worker(f'http://{name}{path}')
    value = json.loads(capsys.readouterr().out)
    assert value['status'] == status, value
    assert set(internet.dialled) <= {PUBLIC_IP}  # nothing non-public was ever dialled
    if status == 'OK':
        assert 'Public page text for the reader.' in value['text']
    else:
        assert value['error'].endswith(web.PUBLIC_ONLY) and 'text' not in value


@pytest.fixture
def reader(internet, monkeypatch):
    """read_page's worker, run in this process on the fixture internet: document() is exercised end to end."""
    def run(argv, **kwargs):
        out = io.TextIOWrapper(io.BytesIO(), encoding='utf-8')
        with contextlib.redirect_stdout(out):
            web.read_worker(*argv[-2:])
        out.flush()
        return {'cleanup_verified': True, 'exit_code': 0}, out.buffer.getvalue(), b''
    monkeypatch.setattr('compute_plane.managed_process.run_pipe_process', run)
    internet.records.update({'public.example': [[PUBLIC_IP]], 'internal.public.example': [['10.0.0.5']]})
    internet.redirects['/hop'] = 'http://169.254.169.254/latest/meta-data/'
    yield internet
    assert set(internet.dialled) <= {PUBLIC_IP}  # nothing non-public was ever dialled


def test_serve_research_reads_public_pages_and_refuses_private_links_and_results(reader, tmp_path):
    from relay import research, turn_engine
    # Links mode: the user's page is read; the private link it carries (depth 1, allowed domain) is refused.
    pages = research.collect(tmp_path, ['http://public.example/page'], domains=['public.example'],
                             max_pages=5, max_depth=1, max_bytes=200000)
    assert [(p['url'], p['status']) for p in pages] == [
        ('http://public.example/page', 'OK'), ('http://internal.public.example/admin', 'REFUSED')]
    # Free search: a search result at a private address keeps only its snippet.
    read = turn_engine.with_snippets([{'href': 'http://public.example/page', 'title': 'P', 'body': 'p'},
                                      {'href': 'http://169.254.169.254/latest/meta-data/', 'title': 'M', 'body': 'm'}])
    assert 'Public page text for the reader.' in read('http://public.example/page').markdown
    assert read('http://169.254.169.254/latest/meta-data/').markdown == 'Search result: M\nm\n\n'


def test_links_in_a_serve_request_read_only_public_addresses(reader):
    from relay import turn_engine
    shown = []
    context = turn_engine.read_pages('compare http://public.example/page, http://localhost:3000/docs and '
                                     'http://public.example/hop', shown.append)
    assert 'Content of http://public.example/page ' in context and 'Public page text for the reader.' in context
    assert 'localhost:3000' not in context and '/hop' not in context
    assert ''.join(shown).count(' ✗ tare reads only public web addresses') == 2


def test_research_flow_collects_public_sources_and_refuses_private_ones(reader, tmp_path, monkeypatch):
    from relay import native_agent, research_flow as flow
    root = tmp_path/'project'
    root.mkdir()
    monkeypatch.setattr(native_agent, 'run_session', lambda *a, **k: {'status': 'FAILED'})
    flow.create(tmp_path/'flow', root, 'q', ['http://public.example/page', 'http://192.168.0.1/admin'],
                {'id': 'fixture'}, unlimited=True)
    sources = {p['url']: p['status'] for p in flow.execute(tmp_path/'flow')['sources']}
    assert sources == {'http://public.example/page': 'OK', 'http://192.168.0.1/admin': 'REFUSED'}
