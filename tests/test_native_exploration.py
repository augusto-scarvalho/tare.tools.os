"""Real source/index/CLI and loopback-provider qualification, without Aider."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Thread

import pytest

import tare_ops
from relay import native_exploration, research


@pytest.fixture
def project(tmp_path):
    from specgraph.indexer import index_repository
    root = tmp_path/'repo'; (root/'src').mkdir(parents=True)
    (root/'src/lib.py').write_text('def leaf(): return 1\ndef caller(): return leaf()\n')
    state = tmp_path/'index'
    index_repository(root, state_dir=state)
    return root, state, tmp_path/'custody'


@pytest.fixture
def provider():
    state = {'calls': [], 'model': 'fixture', 'status': 200, 'usage': None, 'input_tokens': 100}
    def reply(prompt):
        packet = json.loads(prompt)
        ref = packet['evidence']['sources'][0]['id']
        return {'summary': 'caller invokes leaf.', 'evidence': [{'ref': ref, 'claim': 'Calls leaf.'}], 'remaining': []}
    state['reply'] = reply
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            state['calls'].append((self.path, body))
            status = 200
            if self.path == '/apply-template':
                value = {'prompt': 'fixture-template'}
            elif self.path == '/tokenize':
                value = {'tokens': list(range(state['input_tokens']))}
            elif self.path.endswith('/count-tokens'):
                value = {'inputTokens': state['input_tokens']}
            else:
                converse = self.path.endswith('/converse')
                prompt = body['messages'][-1]['content']
                if converse: prompt = prompt[0]['text']
                text = json.dumps(state['reply'](prompt))
                status = state['status']
                if converse:
                    value = {'stopReason': 'end_turn', 'output': {'message': {
                        'role': 'assistant', 'content': [{'text': text}]}}, 'usage': state['usage']}
                else:
                    value = {'model': state['model'], 'choices': [{'finish_reason': 'stop',
                        'message': {'role': 'assistant', 'content': text}}], 'usage': state['usage']}
            raw = json.dumps(value).encode()
            self.send_response(status); self.send_header('Content-Length', str(len(raw)))
            self.end_headers(); self.wfile.write(raw)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    state['url'] = f'http://127.0.0.1:{server.server_port}'
    try: yield state
    finally: server.shutdown(); server.server_close(); thread.join(timeout=2)


def deployment(provider, protocol='openai-chat'):
    params = {'chat_template_kwargs': {'enable_thinking': True, 'thinking_budget': 256}} if protocol == 'openai-chat' else {
        'additionalModelRequestFields': {'thinking': {'type': 'enabled', 'budget_tokens': 256}}}
    return {'runner': 'native-http', 'id': 'fixture', 'native_profile': {
        'protocol': protocol, 'model': 'fixture', 'context_window': 4096, 'output_tokens': 512,
        'source': 'fixture-only', 'endpoint': provider['url'], 'parameters_json': json.dumps(params)},
        'native_counter_endpoint': provider['url']}


def explore(project, **kwargs):
    root, state, custody = project
    return native_exploration.explore(root, 'caller', custody=custody, objective='Find calls', index_state=state,
                                      timeout=15, **kwargs)


def test_sources_only_expands_real_structure_without_specs_and_hashes_live_bytes(project):
    root, _, custody = project
    result = explore(project)
    assert result['status'] == 'SOURCES_READY' and result['tokens'] == {'input': 0, 'output': 0}
    source = result['sources'][0]
    assert source['path'] == 'src/lib.py' and source['sha256'] == hashlib.sha256((root/source['path']).read_bytes()).hexdigest()
    saved = json.loads((Path(result['custody'])/(source['id']+'.json')).read_text())
    assert 'return leaf()' in saved['text']
    assert result['context']['role'] == 'explorer' and result['context']['symbols'][0]['neighbors']
    assert not result['absence_proven'] and result['native_calls'] == []
    assert not (custody/'explorer.lock').exists()


@pytest.mark.parametrize('replacement,reason', [
    ('def replaced(): return 9\n', 'read_range_outside_file:'),
    ('def replaced(): return 9\n# removed caller\n', 'exploration_source_changed'),
])
def test_stale_index_refuses_automatic_ranges_but_explicit_new_focus_is_available(project, replacement, reason):
    root, _, _ = project
    (root/'src/lib.py').write_text(replacement)
    result = explore(project)
    assert result['status'] == 'NEEDS_EVIDENCE' and not result['sources']
    assert result['missing'][0]['reason'].startswith(reason)
    (root/'new.py').write_text('untracked = True\n')
    result = explore(project, focus=[{'path': 'new.py', 'start_line': 1, 'end_line': 1}])
    assert result['status'] == 'SOURCES_READY' and result['sources'][0]['path'] == 'new.py'


@pytest.mark.parametrize('protocol', ['openai-chat', 'bedrock-converse'])
def test_native_exploration_http_validates_citations_and_keeps_unknown_usage(project, provider, protocol):
    result = explore(project, deployment=deployment(provider, protocol))
    assert result['status'] == 'COMPLETED', result.get('error')
    assert result['report']['evidence'][0]['ref'] == result['sources'][0]['id']
    assert result['tokens'] == {'input': None, 'output': None}
    assert len(result['native_calls']) == 1
    assert 'tools' not in provider['calls'][-1][1] and 'toolConfig' not in provider['calls'][-1][1]


@pytest.mark.parametrize('fault', ['forged-ref', 'source-changed', '429', 'model-mismatch'])
def test_failed_native_exploration_keeps_receipt_and_releases_shared_lock(project, provider, fault):
    original = provider['reply']
    def reply(prompt):
        value = original(prompt)
        if fault == 'forged-ref': value['evidence'][0]['ref'] = 'X-invented'
        if fault == 'source-changed': (project[0]/'src/lib.py').write_text('changed = True\n')
        return value
    provider['reply'] = reply
    if fault == '429': provider['status'] = 429
    if fault == 'model-mismatch': provider['model'] = 'wrong'
    result = explore(project, deployment=deployment(provider))
    assert result['status'] == 'FAILED' and result['report'] is None
    assert len(result['native_calls']) == 1 and not (project[2]/'explorer.lock').exists()
    assert json.loads((Path(result['custody'])/'result.json').read_text())['status'] == 'FAILED'


def test_busy_lock_and_unsafe_focus_do_not_invoke_model(project, provider):
    custody = project[2]; custody.mkdir(); lock = custody/'explorer.lock'; lock.write_text('existing owner')
    result = explore(project, deployment=deployment(provider))
    assert result['status'] == 'FAILED' and lock.read_text() == 'existing owner' and not provider['calls']
    lock.unlink()
    result = explore(project, deployment=deployment(provider), focus=[{'path': '../secret', 'start_line': 1, 'end_line': 2}])
    assert result['status'] == 'FAILED' and not provider['calls']


def test_receipt_write_failure_still_releases_lock(project, monkeypatch):
    import compute_plane.native_journal as journal
    original = journal.save_json
    def save(path, value):
        if Path(path).name == 'result.json': raise OSError('disk full')
        return original(path, value)
    monkeypatch.setattr(journal, 'save_json', save)
    with pytest.raises(OSError, match='disk full'): explore(project)
    assert not (project[2]/'explorer.lock').exists()


@pytest.mark.parametrize('with_model', [False, True])
def test_native_cli_runs_with_imports_of_aider_and_vendor_sdk_forbidden(project, provider, with_model):
    root, state, custody = project
    code = '''import importlib.abc,sys
class Forbidden(importlib.abc.MetaPathFinder):
 def find_spec(self, fullname, path=None, target=None):
  if fullname.split('.')[0] in ('aider','openai') or fullname in ('compute_plane.aider_driver','compute_plane.audit_driver'):
   raise RuntimeError('forbidden dependency: '+fullname)
sys.meta_path.insert(0,Forbidden())
import tare_ops
raise SystemExit(tare_ops.main(sys.argv[1:]))
'''
    args = ['explore', '--root', str(root), '--question', 'caller',
            '--index-state', str(state), '--custody', str(custody)]
    if with_model:
        config = root.parent/'deployment.json'; config.write_text(json.dumps(deployment(provider)))
        args += ['--native-deployment', str(config)]
    else:
        args.append('--sources-only')
    done = subprocess.run([sys.executable, '-B', '-c', code, *args], capture_output=True, timeout=20)
    assert done.returncode == 0, done.stderr.decode()
    assert json.loads(done.stdout)['status'] == ('COMPLETED' if with_model else 'SOURCES_READY')


def research_fetch(url):
    from relay.web_reader import WebDocument
    return WebDocument(url=url, title='Fixture', markdown='The cache TTL is five minutes.',
        raw_size_bytes=30, clean_size_bytes=30, reduction_pct=0, method='fixture', status='OK')


def research_reply(prompt):
    if 'SOURCES: ' in prompt:
        sources = json.loads(prompt.split('SOURCES: ', 1)[1])
        return {'facts': [{'ref': sources[0]['ref'], 'quote': 'The cache TTL is five minutes.', 'fact': 'TTL is five minutes.'}]}
    facts = json.loads(prompt.split('FACTS: ', 1)[1])
    return {'summary': 'TTL is five minutes.', 'evidence': [{'ref': facts[0]['ref'], 'claim': 'TTL is five minutes.'}],
            'remaining': []}


@pytest.mark.parametrize('protocol', ['openai-chat', 'bedrock-converse'])
def test_native_research_real_http_map_reduce_and_stage_usage(tmp_path, provider, protocol):
    provider['reply'] = research_reply
    dep = deployment(provider, protocol)
    dep['native_usage_contract'] = {'layout': 'chat-inclusive' if protocol == 'openai-chat' else 'converse-exclusive',
                                    'source': 'fixture-only'}
    provider['usage'] = ({'prompt_tokens': 100, 'completion_tokens': 10} if protocol == 'openai-chat' else {
        'inputTokens': 100, 'outputTokens': 10, 'cacheReadInputTokens': 20, 'cacheWriteInputTokens': 30})
    result = research.research(tmp_path, 'TTL?', ['https://fixture.test'], domains=['fixture.test'], max_pages=1,
        max_depth=0, fetch=research_fetch, native_deployment=dep)
    assert result['status'] == 'COMPLETED', result['error']
    assert [r['attempt_label'] for r in result['native_calls']] == ['map', 'reduce']
    assert result['generation_calls'] == 2
    assert result['tokens'] == {'input': 200 if protocol == 'openai-chat' else 300, 'output': 20}
    assert result['envelope']['evidence'][0]['ref'].startswith('W-')
    assert not (tmp_path/'explorer.lock').exists()


@pytest.mark.parametrize('fault', ['429', 'call-limit', 'context-limit', 'cancel'])
def test_native_research_failure_stops_without_hidden_retry(tmp_path, provider, fault):
    def reply(prompt):
        if fault == 'cancel':
            next(tmp_path.glob('research-*')).joinpath('cancel').touch()
        return research_reply(prompt)
    provider['reply'] = reply
    if fault == '429': provider['status'] = 429
    if fault == 'context-limit': provider['input_tokens'] = 4000
    result = research.research(tmp_path, 'TTL?', ['https://fixture.test'], domains=['fixture.test'], max_pages=1,
        max_depth=0, fetch=research_fetch, native_deployment=deployment(provider),
        max_model_calls=1 if fault == 'call-limit' else 10)
    assert result['status'] == 'FAILED', result
    assert result['generation_calls'] <= 1
    assert not (tmp_path/'explorer.lock').exists()
    assert (tmp_path/result['job_id']/'result.json').exists()
