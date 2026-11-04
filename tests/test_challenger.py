import copy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Thread
import time
import tempfile
from pathlib import Path

import pytest

from relay import challenger as c
from relay import challenger_variants as variants
from relay import challenger_workspace as workspace
from relay import native_agent
from test_native_agent import Client, deployment, response, steps


@pytest.fixture
def tmp_path():
    # These fixtures create nested Git worktrees. Their metadata paths must fit
    # even when the suite itself runs inside an isolated comparison workspace.
    with tempfile.TemporaryDirectory(prefix='tare-challenger-') as directory:
        yield Path(directory)


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path/'project'; root.mkdir()
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))
    monkeypatch.setenv('TARE_PROFILE_DIR', str(tmp_path/'profiles'))
    monkeypatch.setattr(native_agent, 'SESSION_ROOT', tmp_path/'sessions')
    c.git(root, 'init')
    c.git(root, 'config', 'user.name', 'Test'); c.git(root, 'config', 'user.email', 'test@localhost')
    (root/'a.py').write_bytes(b'value = 1\n')
    c.git(root, 'add', '.'); c.git(root, '-c', 'commit.gpgsign=false', 'commit', '-m', 'Base')
    return root


def profile(monkeypatch, value):
    monkeypatch.setattr('relay.native_catalog.select', lambda *a, **kw: copy.deepcopy(value))


def test_variants_preserve_full_parameters_and_resource_domain(monkeypatch):
    value = deployment(endpoint='http://127.0.0.1:8888')
    profile(monkeypatch, value)
    one = variants.realize({'profile': 'fixture', 'parameters': {'reasoning_budget_tokens': 2048}})
    two = variants.realize({'profile': 'fixture', 'parameters': {'reasoning_budget_tokens': 8192}})
    assert one['key'] != two['key']
    assert one['resource_pool'] == two['resource_pool']
    assert value == deployment(endpoint='http://127.0.0.1:8888')
    with pytest.raises(ValueError, match='serving identity'):
        variants.realize({'profile': 'fixture', 'parameters': {'lora': 'unconfirmed'}})
    gateway = variants.realize({'profile': 'fixture', 'accounting': 'usd'})
    assert variants.accounting(gateway['deployment']) == 'usd'
    assert not c.local(gateway['deployment'])
    # Same model, different context policy: a distinct arm.
    elide = variants.realize({'profile': 'fixture', 'context_projection': {'elide_edited_reads': True}})
    assert elide['deployment']['native_context_projection'] == {'elide_edited_reads': True}
    assert elide['key'] != variants.realize('fixture')['key']
    with pytest.raises(ValueError, match='native_context_projection'):
        variants.realize({'profile': 'fixture', 'context_projection': {'elide_edited_reads': 'yes'}})


@pytest.mark.parametrize('endpoint,protocol,expected', [
    ('http://127.0.0.1:8080', 'openai-chat', 'local'),
    ('http://100.64.0.10:8080/v1', 'openai-chat', 'local'),
    ('https://openrouter.ai/api/v1', 'openai-chat', 'usd'),
    ('https://bedrock-runtime.us-east-1.amazonaws.com', 'bedrock-converse', 'usd'),
    ('cli://codex', 'vendor-cli', 'quota'), ('cli://claude', 'vendor-cli', 'quota'),
    ('cli://agy', 'vendor-cli', 'quota'), ('https://chatgpt.com/backend-api/codex', 'openai-codex-responses', 'quota')])
def test_billing_is_not_inferred_from_open_weights(endpoint, protocol, expected):
    assert variants.accounting({'native_profile': {'endpoint': endpoint, 'protocol': protocol}}) == expected


def test_quota_needs_headroom_or_explicit_bounded_unknown_probe(monkeypatch):
    facts = {'quota': {'status': 'UNKNOWN'}, 'resource': {}}
    monkeypatch.setattr('relay.route_facts.collect', lambda *a, **kw: facts)
    variant = {'deployment': {}, 'accounting': 'quota'}
    with pytest.raises(ValueError, match='Quota balance is unknown'):
        variants.preflight(variant)
    variants.preflight(variant, allow_unknown_quota=True)
    facts['quota'] = {'status': 'AVAILABLE', 'remaining': 20, 'unit': 'percent'}
    with pytest.raises(ValueError, match='headroom'):
        variants.preflight(variant, allow_unknown_quota=True)
    facts['quota'] = {'status': 'EXHAUSTED'}
    with pytest.raises(ValueError, match='QUOTA_EXHAUSTED'):
        variants.preflight(variant, allow_unknown_quota=True)


def test_effective_adapter_mismatch_stops_before_generation():
    from tare_tools_agent_runtime import ModelFailure
    class Transport:
        def post(self, *args):
            return {'profile': {'adapter_sha256': 'wrong'}}
    client = variants.IdentityClient(Transport(), {'adapter_sha256': 'expected'})
    with pytest.raises(ModelFailure, match='CHALLENGER_EFFECTIVE_CONFIGURATION_MISMATCH'):
        client.post('/fleet/count', {}, None)


def test_provider_price_refresh_keeps_same_tariff_identity_and_rejects_missing(project, monkeypatch):
    from decimal import Decimal
    value = c.bounded(deployment(endpoint='https://api.example.test'), c.DEFAULT, Decimal('.25'))
    quote = {'observed_at': '2026-09-24T12:00:00Z', 'source': 'provider catalog',
        'rates': {'uncached_input_tokens': '1', 'cache_read_tokens': '.1', 'cache_write_tokens': '0', 'output_tokens': '2'}}
    monkeypatch.setattr('relay.provider_prices.quote', lambda *a, **kw: quote)
    variants.price(value)
    first = copy.deepcopy(value['native_usage_tariff'])
    value.pop('native_usage_tariff')
    quote['observed_at'] = '2026-09-24T13:00:00Z'
    variants.price(value)
    assert value['native_usage_tariff'] == first
    value.pop('native_usage_tariff'); quote['rates'] = None
    with pytest.raises(ValueError, match='unavailable or conditional'):
        variants.price(value)


def test_snapshot_keeps_dirty_and_untracked_inputs_without_staging_original(project, tmp_path):
    (project/'a.py').write_text('value = 10\n')
    (project/'untracked.txt').write_text('operator work')
    before = c.git(project, 'status', '--porcelain=v1')
    destination = tmp_path/'snapshot'
    commit = workspace.snapshot(project, destination)
    assert (destination/'a.py').read_text() == 'value = 10\n'
    assert (destination/'untracked.txt').read_text() == 'operator work'
    assert not c.git(destination, 'status', '--porcelain=v1')
    assert c.git(project, 'status', '--porcelain=v1') == before
    assert c.git(project, 'rev-parse', 'HEAD').decode().strip() != commit


def test_snapshot_of_a_crlf_checkout_copies_only_what_changed(project, tmp_path):
    # Git for Windows checks text out with CRLF (core.autocrlf=true): a worktree with other line endings made every
    # file differ and refused any repository past 512 of them, the tare repository included.
    c.git(project, 'config', 'core.autocrlf', 'true')
    for index in range(600):
        (project/f'f{index}.txt').write_bytes(b'line\n')
    c.git(project, 'add', '.'); c.git(project, '-c', 'commit.gpgsign=false', 'commit', '-m', 'Many')
    for path in project.glob('f*.txt'):
        path.unlink()
    c.git(project, 'checkout', '--', '.')  # the operator's checkout: CRLF on disk
    assert (project/'f0.txt').read_bytes() == b'line\r\n'
    (project/'a.py').write_text('value = 2\n')
    destination = tmp_path/'snapshot'
    workspace.snapshot(project, destination)
    assert (destination/'f0.txt').read_bytes() == b'line\r\n' and (destination/'a.py').read_text() == 'value = 2\n'
    assert c.git(destination, 'show', '--stat', '--format=', 'HEAD').decode().count('|') == 1  # a.py alone


def test_toggle_and_daily_limits_persist_without_resolving_disabled_models(project, monkeypatch):
    value = copy.deepcopy(c.DEFAULT)
    value.update(models=['removed-profile'], local_percent=33, external_percent=17, daily_quota_calls=12)
    monkeypatch.setattr('relay.native_catalog.select', lambda *a, **kw: pytest.fail('Disabled mode resolved a model'))
    c.configure(project, value)
    assert c.policy(project) == value
    assert c.inspect(project)['committed_usd'] == '0'
    with pytest.raises(ValueError, match='local_percent'):
        c.configure(project, {**value, 'local_percent': 101})


def test_unknown_api_charge_keeps_daily_reservation(project, tmp_path, monkeypatch):
    import psutil
    from compute_plane.native_journal import save_json
    candidate = deployment(endpoint='https://api.example.test')
    profile(monkeypatch, candidate)
    monkeypatch.setattr(c, 'spawn', lambda job: (os.getpid(), psutil.Process().create_time()))
    settings = copy.deepcopy(c.DEFAULT)
    settings.update(enabled=True, models=['fixture'], external_percent=100, daily_usd='.50', trial_usd='.40')
    c.configure(project, settings)
    config = {'role': 'implementer', 'repositories': {'repo': str(project)}, 'writable': {'repo': ['a.py']},
              'verifiers': {'check': {'repository': 'repo', 'argv': [sys.executable, '-m', 'pytest']}}}
    for index in range(2):
        custody = tmp_path/('session'+str(index)); custody.mkdir()
        state = {'config': config, 'session_id': str(index)*32, 'deployment': deployment(), 'instruction': 'Fix a.py'}
        c.start(custody, state)
        marker = native_agent.read_json(custody/'challenger.json')
        if not index:
            assert marker['status'] == 'RUNNING'
            path = Path(marker['job'])/'run.json'
            row = native_agent.read_json(path, 1024*1024)
            row['status'] = 'INTERRUPTED'
            save_json(path, row, max_bytes=1024*1024)
        else:
            assert marker['status'] == 'SKIPPED' and 'budget reached' in marker['reason']
    assert c.inspect(project)['committed_usd'] == '0.40'


@pytest.mark.timeout(90)
def test_paired_trials_automatically_form_study_and_keep_failed_attempt(project, tmp_path, monkeypatch):
    import psutil
    from relay import challenger_worker as worker
    from relay import delivery_comparison
    from relay.native_profiles import install
    primary_dep = deployment()
    candidate = deployment(endpoint='http://127.0.0.1:8081')
    reviewer = deployment(endpoint='http://127.0.0.1:8082')
    for name, dep in [('experiment', candidate), ('reviewer', reviewer)]:
        path = tmp_path/(name+'.json'); path.write_text(json.dumps(dep))
        install(name, path)
    monkeypatch.setattr(c, 'spawn', lambda job: (os.getpid(), psutil.Process().create_time()))
    monkeypatch.setattr('relay.work_audit.prepare', lambda *a: {'status': 'READY'})
    fail = False
    def execute(config, dep, variant, session, instruction, row, cancellation):
        if config['role'] == 'implementer':
            script = [('tare_deliver', {'summary': 'Attempt without a fix.'})] if fail else steps()
        else:
            root = Path(config['repositories']['repo'])
            raw = (root/'a.py').read_bytes()
            good = raw == b'value = 2\n'
            script = [('read', {'repository': 'repo', 'path': 'a.py'}), ('verify', {'name': 'unit'}),
                ('tare_deliver', {'summary': 'Reviewed observed implementation.',
                    'sources': [{'repository': 'repo', 'path': 'a.py', 'sha256': hashlib.sha256(raw).hexdigest()}],
                    'remaining': [], 'verdict': 'ACCEPT' if good else 'REVISE',
                    'findings': [] if good else [{'claim': 'The requested change is missing.',
                        'sources': [{'repository': 'repo', 'path': 'a.py', 'sha256': hashlib.sha256(raw).hexdigest()}]}]})]
        client = Client(steps=script)
        client.endpoint = dep['native_profile']['endpoint']
        return native_agent.run_session(config, dep, session, instruction, client=client,
                                        local_counter=client, cancellation=cancellation)
    monkeypatch.setattr(worker, 'execute', execute)
    settings = copy.deepcopy(c.DEFAULT)
    settings.update(enabled=True, models=['experiment'], reviewer='reviewer', local_percent=100, seconds=10, calls=5)
    c.configure(project, settings)
    config = {'schema': 'tare.tools/native-agent-config/1', 'role': 'implementer',
        'repositories': {'repo': str(project)}, 'writable': {'repo': ['a.py']},
        'specgraph_edit_context': False, 'capabilities': ['read', 'list', 'search', 'edit', 'verify'],
        'verifiers': {'unit': {'repository': 'repo', 'argv': [sys.executable, '-c',
            "from pathlib import Path; assert Path('a.py').read_text() == 'value = 2\\n'"]}}}
    for index in range(3):
        fail = index == 2
        (project/'a.py').write_bytes(b'value = 1\n')
        client = Client(steps=steps()); custody = tmp_path/('source'+str(index))
        result = native_agent.run_session(config, primary_dep, custody, 'Set value to 2, case '+str(index),
                                         client=client, local_counter=client)
        assert result['status'] == 'PROPOSED'
        job = Path(native_agent.read_json(custody/'challenger.json')['job'])
        worker.run(job)
        row = native_agent.read_json(job/'run.json', 1024*1024)
        assert 'study_error' not in row, row
        if fail:
            assert row['status'] == 'FAILED' and row['checks_passed'] is False, row
            assert row['reference_verdict'] == row['challenger_verdict'] == 'unknown'
            assert not (job/'reference-audit').exists() and not (job/'challenger-audit').exists()
        else:
            assert row['status'] == 'COMPLETED', row
            assert row['reference_verdict'] == row['challenger_verdict'] == 'accepted', row
    study = native_agent.read_json(Path(row['study']), 256*1024)
    assert len(study['cases']) == 3 and len(study['trials']) == 6
    report = delivery_comparison.compare(Path(row['study']))
    assert report['winner'] is None  # Local hardware/fixture billing has not been priced.
    assert report['candidates'][1]['accepted'] == 2
    assert not any('PROFILE_CHANGED' in issue for cand in report['candidates'] for issue in cand['issues'])
    # Old failures remain in custody; a bounded live window can keep measuring.
    from compute_plane.native_journal import save_json
    reference = native_agent.read_json(job/'reference.json', 1024*1024)
    changed = copy.deepcopy(reference)
    changed['outcome']['last_message_id'] = 'operator-changed-the-goal'
    save_json(job/'reference.json', changed, max_bytes=1024*1024)
    steered = native_agent.read_json(Path(c.study(job)), 256*1024)
    assert all(t['verdict'] == 'unknown' for t in steered['trials'] if t['case'] == row['id'])
    save_json(job/'reference.json', reference, max_bytes=1024*1024)
    stored = native_agent.read_json(job/'run.json', 1024*1024)
    for index in range(101):
        extra = copy.deepcopy(stored)
        extra.update(id='history-'+str(index), started_at=stored['started_at']+index+1)
        folder = job.parent/extra['id']; folder.mkdir()
        save_json(folder/'run.json', extra, max_bytes=1024*1024)
    recent = native_agent.read_json(Path(c.study(job)), 256*1024)
    assert len(recent['cases']) == 100 and 'history-0' not in recent['cases']
    assert len(c.records(job.parent.parent)) == 104
    assert all(t['verdict'] == 'unknown' for t in recent['trials'] if t['candidate'] == 'challenger')
    stored['reference_reuse']={'job':'prior-real-reference'}
    save_json(job/'run.json',stored,max_bytes=1024*1024)
    duplicate=copy.deepcopy(stored);duplicate['id']='another-retry'
    (job.parent/duplicate['id']).mkdir()
    save_json(job.parent/duplicate['id']/'run.json',duplicate,max_bytes=1024*1024)
    isolated=native_agent.read_json(Path(c.study(job)),256*1024)
    assert len(isolated['cases'])==1 and len(isolated['trials'])==2


@pytest.mark.parametrize('layout',[False,True])
def test_reuse_preserves_later_edits_and_recovers_partial_application(project, tmp_path, monkeypatch, layout):
    from compute_plane.native_journal import save_json
    from relay.challenger_worker import session_config
    (project/'b.py').write_bytes(b'value = 1\n')
    c.git(project, 'add', '.'); c.git(project, '-c', 'commit.gpgsign=false', 'commit', '-m', 'Second file')
    job = tmp_path/'state/challengers/project/runs/job'; job.mkdir(parents=True)
    contract={'workspace_layout':1} if layout else {}
    save_json(job/'run.json',{'execution_contract':contract})
    candidate_root, reference_root=workspace.tree(job,'candidate'),workspace.tree(job,'reference')
    candidate_root.parent.mkdir(parents=True,exist_ok=True)
    base = workspace.snapshot(project, candidate_root)
    for name in ('a.py', 'b.py'):
        (project/name).write_bytes(b'value = 3\n')
        (candidate_root/name).write_bytes(b'value = 2\n')
    reference = workspace.snapshot(project, reference_root)
    row = {'status': 'COMPLETED', 'id': 'a'*32, 'project': str(project), 'path': str(job),
        'base': base, 'challenger_verdict': 'accepted', 'repository': 'repo', 'execution_contract':contract,
        'files': workspace.freeze(job, base), 'config': {
            'schema': 'tare.tools/native-agent-config/1', 'role': 'implementer',
            'repositories': {'repo': str(project)}, 'writable': {'repo': ['a.py', 'b.py']},
            'specgraph_edit_context': False, 'capabilities': ['read', 'list', 'search', 'edit', 'verify'],
            'verifiers': {'unit': {'repository': 'repo', 'argv': [sys.executable, '-c',
                "from pathlib import Path; assert all(Path(p).read_text() == 'value = 2\\n' for p in ('a.py','b.py'))"]}}}}
    save_json(job/'run.json', row)
    save_json(job/'reference.json', {'snapshot': reference})
    payload = {'summary': 'Both files satisfy the task.', 'sources': [
        {'repository': 'repo', 'path': name, 'sha256': hashlib.sha256(b'value = 2\n').hexdigest()}
        for name in ('a.py', 'b.py')], 'remaining': [], 'verdict': 'ACCEPT', 'findings': []}
    client = Client(steps=[('read', {'repository': 'repo', 'path': name}) for name in ('a.py', 'b.py')]
                    + [('verify', {'name': 'unit'}), ('tare_deliver', payload)])
    outcome = native_agent.run_session(session_config(row, candidate_root, 'auditor'), deployment(), job/'challenger-audit',
        'Review both changes.', client=client, local_counter=client)
    assert outcome['status'] == 'PROPOSED'
    (project/'a.py').write_bytes(b'operator edit\n')
    with pytest.raises(ValueError, match='Project changed'):
        workspace.reuse(job)
    assert (project/'b.py').read_bytes() == b'value = 3\n'
    (project/'a.py').write_bytes(b'value = 3\n')
    original = os.replace
    def crash(source, target):
        if Path(target) == project/'b.py':
            raise OSError('simulated crash')
        return original(source, target)
    monkeypatch.setattr(os, 'replace', crash)
    with pytest.raises(OSError, match='simulated crash'):
        workspace.reuse(job)
    assert (project/'a.py').read_bytes() == b'value = 2\n'
    monkeypatch.setattr(os, 'replace', original)
    assert workspace.reuse(job)['status'] == 'APPLIED'
    assert (project/'b.py').read_bytes() == b'value = 2\n'
    assert workspace.reuse(job)['status'] == 'APPLIED'
    (candidate_root/'a.py').write_bytes(b'unreviewed change\n')
    with pytest.raises(ValueError, match='Reviewed challenger files changed'):
        workspace.reuse(job)


def test_native_session_automatically_runs_one_isolated_challenger(project, tmp_path, monkeypatch):
    calls = []
    script = iter(steps())
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append((self.path, body))
            if self.path == '/apply-template':
                value = {'prompt': 'fixture'}
            elif self.path == '/tokenize':
                value = {'tokens': [1]*80}
            else:
                value = response('openai-chat', *next(script))
            raw = json.dumps(value).encode()
            self.send_response(200); self.send_header('Content-Length', str(len(raw)))
            self.end_headers(); self.wfile.write(raw)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        candidate = deployment(endpoint='http://127.0.0.1:'+str(server.server_port))
        from relay.native_profiles import install
        file = tmp_path/'fixture.json'; file.write_text(json.dumps(candidate))
        install('challenger-fixture', file)
        policy = copy.deepcopy(c.DEFAULT)
        policy.update(enabled=True, models=['challenger-fixture'], local_percent=100, seconds=15, calls=5)
        c.configure(project, policy)
        config = {'schema': 'tare.tools/native-agent-config/1', 'role': 'implementer',
            'repositories': {'repo': str(project)}, 'writable': {'repo': ['a.py']},
            'specgraph_edit_context': False, 'capabilities': ['read', 'list', 'search', 'edit', 'verify'],
            'verifiers': {'unit': {'repository': 'repo', 'argv': [sys.executable, '-c',
                "from pathlib import Path; assert Path('a.py').read_text() == 'value = 2\\n'"]}}}
        custody = tmp_path/'primary'
        primary = Client(steps=steps())
        outcome = native_agent.run_session(config, deployment(), custody, 'Change value to 2 and verify.',
                                           client=primary, local_counter=primary)
        assert outcome['status'] == 'PROPOSED'
        marker = native_agent.read_json(custody/'challenger.json')
        assert marker['status'] == 'RUNNING', marker
        job = Path(marker['job'])
        deadline = time.monotonic()+25
        while time.monotonic() < deadline:
            row = c.read_run(job/'run.json')
            if row['status'] not in c.ACTIVE:
                break
            time.sleep(.1)
        assert row['status'] == 'COMPLETED', (row, (job/'worker.log').read_text())
        assert row['checks_passed'] and row['challenger_verdict'] == 'unknown'
        assert row['committed_usd'] == '0'
        assert (workspace.tree(job,'candidate')/'a.py').read_text() == 'value = 2\n'
        assert (project/'a.py').read_text() == 'value = 2\n'
        assert (job/'challenger.patch').is_file()
        assert len([path for path, body in calls if path.endswith('/chat/completions')]) == 4
        count = len(calls)
        from relay.challenger_worker import run
        run(job)
        assert len(calls) == count  # Recovery cannot replay effects or spend.
        with pytest.raises(ValueError, match='independent review'):
            workspace.reuse(job)
    finally:
        server.shutdown(); server.server_close()


def test_busy_receipt_read_never_omits_a_budget_row(tmp_path, monkeypatch):
    path = tmp_path/'runs'/'one'/'run.json'; path.parent.mkdir(parents=True)
    path.write_text('{}')
    row = {'id': 'one', 'status': 'COMPLETED', 'committed_usd': '1.25'}
    calls = []
    def read(*args):
        calls.append(args)
        if len(calls) == 1:
            raise PermissionError('atomic replacement busy')
        return row
    monkeypatch.setattr(c, 'read_json', read)
    monkeypatch.setattr(c.time, 'sleep', lambda _: None)
    assert c.records(tmp_path) == [row] and len(calls) == 2
    calls.clear()
    def denied(*args):
        calls.append(args)
        raise PermissionError('persistent denial')
    monkeypatch.setattr(c, 'read_json', denied)
    with pytest.raises(PermissionError, match='persistent denial'):
        c.records(tmp_path)
    assert len(calls) == 3


def test_policy_variant_of_the_primary_model_is_preferred_and_identical_arms_skipped(project, tmp_path, monkeypatch):
    import psutil
    from relay.native_profiles import install
    primary_dep = deployment(endpoint='http://127.0.0.1:8080')
    for name, dep in [('same', deployment(endpoint='http://127.0.0.1:8080')),
                      ('other', deployment(endpoint='http://127.0.0.1:8081')),
                      ('reviewer', deployment(endpoint='http://127.0.0.1:8082'))]:
        path = tmp_path/(name+'.json'); path.write_text(json.dumps(dep))
        install(name, path)
    monkeypatch.setattr(c, 'spawn', lambda job: (os.getpid(), psutil.Process().create_time()))
    monkeypatch.setattr('relay.work_audit.prepare', lambda *a: {'status': 'READY'})
    config = {'schema': 'tare.tools/native-agent-config/1', 'role': 'implementer',
        'repositories': {'repo': str(project)}, 'writable': {'repo': ['a.py']},
        'specgraph_edit_context': False, 'capabilities': ['read', 'list', 'search', 'edit', 'verify'],
        'verifiers': {'unit': {'repository': 'repo', 'argv': [sys.executable, '-c', 'pass']}}}

    def dispatched(models, case):
        settings = copy.deepcopy(c.DEFAULT)
        settings.update(enabled=True, models=models, reviewer='reviewer', local_percent=100, seconds=10, calls=5)
        c.configure(project, settings)
        (project/'a.py').write_bytes(b'value = 1\n')
        custody = tmp_path/('source-'+case)
        client = Client(steps=steps())
        client.endpoint = primary_dep['native_profile']['endpoint']
        native_agent.run_session(config, primary_dep, custody, 'Set value to 2, '+case, client=client, local_counter=client)
        record = native_agent.read_json(custody/'challenger.json')
        if 'job' not in record:
            return None, record
        run = c.read_run(Path(record['job'])/'run.json')
        for row in c.records(Path(record['job']).parent.parent):
            (Path(row['path'])/'run.json').write_text(json.dumps({**row, 'status': 'CANCELLED'}))
        return run, record

    elide = {'profile': 'same', 'label': 'same elide', 'context_projection': {'elide_edited_reads': True}}
    run, record = dispatched(['other', elide], 'a')
    assert run is not None, record
    assert run['variant']['label'] == 'same elide', run['variant']
    # The plain same profile is identical to the primary arm: no challenger.
    run, record = dispatched(['same'], 'b')
    assert run is None and 'Outside the configured task/sample' in json.dumps(record)


@pytest.fixture
def linked_job(project, tmp_path):
    """Reproducible job custody: no private commits, providers or worker processes."""
    from compute_plane.native_journal import save_json
    job = c.directory(project)/'runs'/'linked'; job.mkdir(parents=True)
    save_json(job.parent.parent/'policy.json', {**c.DEFAULT, 'enabled': True})
    config = {'schema': 'tare.tools/native-agent-config/1', 'role': 'implementer',
        'repositories': {'repo': str(project)}, 'writable': {'repo': ['a.py']},
        'specgraph_edit_context': False, 'capabilities': ['read', 'edit', 'verify'],
        'verifiers': {'unit': {'repository': 'repo', 'argv': [sys.executable, '-c', 'pass']}}}
    row = dict(id='linked', status='RUNNING', path=str(job), project=str(project),
        repository='repo', config=config, baseline=deployment(), challenger=deployment(),
        reviewer=deployment(), variant={'accounting': 'local'}, review_variant={'accounting': 'local'},
        instruction='Set value to 2 and verify.', reserved_usd='0.25', primary_pid=os.getpid(),
        base=workspace.snapshot(project, job/'candidate'))
    save_json(job/'run.json', row, max_bytes=1024*1024)
    custody = tmp_path/'primary'; custody.mkdir()
    save_json(custody/'challenger.json', {'status': 'RUNNING', 'job': str(job)})
    state = {'config': config, 'invocations': [{'id': 'offline'}]}
    other = job.parent/'other'; other.mkdir()
    (other/'run.json').write_bytes(b'{"status":"RUNNING","reserved_usd":"2.50"}')
    (other/'receipt.json').write_bytes(b'other job receipt\n')
    return custody, state, job, other


@pytest.mark.parametrize('status', ['CANCELLED', 'INTERRUPTED'])
@pytest.mark.parametrize('stage', ['PREPARING', 'RUNNING', 'WAITING_REFERENCE', 'REVIEWING',
                                    'COMPLETED', 'CANCELLED', 'FAILED', 'INTERRUPTED'])
def test_finish_stops_only_linked_job_without_replacing_evidence(linked_job, monkeypatch, status, stage):
    from relay import challenger_worker as worker
    custody, state, job, other = linked_job
    row = c.read_run(job/'run.json'); row['status'] = stage
    (job/'run.json').write_text(json.dumps(row))
    # result.json is retained if supplied by an owner; the worker's native receipt
    # is outcome.json, and its job result is run.json. None gates stop publication.
    for name in ('reference.json', 'result.json', 'outcome.json', 'challenger.patch', 'receipt.json'):
        (job/name).write_bytes((name+' original\n').encode())
    before = {p: p.read_bytes() for p in [*job.glob('*.*'), *other.iterdir(), custody/'challenger.json']}
    monkeypatch.setattr(c, 'spawn', lambda *_: pytest.fail('finish must not launch a worker'))
    monkeypatch.setattr(workspace, 'snapshot', lambda *_: pytest.fail('reference already retained'))
    c.finish(custody, state, {'status': status})
    signal_time = (job/'cancel').stat().st_mtime_ns
    c.finish(custody, state, {'status': status})
    assert (job/'cancel').stat().st_mtime_ns == signal_time
    assert all(p.read_bytes() == raw for p, raw in before.items())
    assert not (other/'cancel').exists()
    if stage not in c.ACTIVE:
        worker.run(job)
        assert all(p.read_bytes() == raw for p, raw in before.items())


@pytest.mark.parametrize('mode', ['CANCELLED', 'INTERRUPTED', 'PROPOSED'])
@pytest.mark.parametrize('linked', [False, True])
def test_primary_real_finish_paths(linked_job, monkeypatch, mode, linked):
    from tare_tools_agent_runtime import CancellationToken
    custody, state, job, other = linked_job
    if not linked:
        (custody/'challenger.json').unlink()
    # Keep the actual run_session outcome/exception paths; only suppress scheduler
    # admission so these fixtures cannot spawn or spend on an external challenger.
    monkeypatch.setattr(c, 'start', lambda *_: None)
    cancellation = CancellationToken()
    def after_model():
        if mode == 'CANCELLED':
            cancellation.cancel()
        elif mode == 'INTERRUPTED':
            raise KeyboardInterrupt('offline primary interruption')
    client = Client(steps=steps(), after_model=after_model)
    def run():
        return native_agent.run_session(state['config'], deployment(), custody,
            'Set value to 2 and verify.', client=client, local_counter=client, cancellation=cancellation)
    if mode == 'INTERRUPTED':
        with pytest.raises(KeyboardInterrupt, match='offline primary interruption'):
            run()
    else:
        result = run()
        assert result['status'] == mode
        assert native_agent.read_json(custody/'outcome.json') == result
    assert (job/'cancel').exists() is (linked and mode != 'PROPOSED')
    assert (job/'reference.json').exists() is linked
    assert not (other/'cancel').exists()
    if linked:
        reference = native_agent.read_json(job/'reference.json')
        assert reference['outcome']['status'] == mode
        before = {p: p.read_bytes() for p in (job/'reference.json', custody/'native-agent.json')}
        c.finish(custody, native_agent.read_json(custody/'native-agent.json'), {'status': mode})
        assert all(p.read_bytes() == raw for p, raw in before.items())


@pytest.mark.parametrize('marker', [None, {'status': 'SKIPPED', 'reason': 'disabled'}])
def test_finish_without_link_is_noop(tmp_path, monkeypatch, marker):
    custody = tmp_path/'unlinked'; custody.mkdir()
    if marker is not None:
        (custody/'challenger.json').write_text(json.dumps(marker))
    before = {p.name: p.read_bytes() for p in custody.iterdir()}
    monkeypatch.setattr(workspace, 'snapshot', lambda *_: pytest.fail('no linked snapshot'))
    monkeypatch.setattr(c, 'spawn', lambda *_: pytest.fail('no linked dispatch'))
    for status in ('CANCELLED', 'INTERRUPTED', 'PROPOSED'):
        c.finish(custody, {}, {'status': status})
    assert {p.name: p.read_bytes() for p in custody.iterdir()} == before


@pytest.mark.parametrize('stop_fails', [False, True])
@pytest.mark.parametrize('snapshot_fails', [False, True])
def test_finish_failures_preserve_reference_and_allow_stop_retry(linked_job, monkeypatch, stop_fails, snapshot_fails):
    custody, state, job, other = linked_job
    original_open, snapshot = Path.open, workspace.snapshot
    snapshots = []
    def capture(*args):
        snapshots.append(args)
        assert (job/'cancel').exists() is not stop_fails
        if snapshot_fails:
            raise OSError('offline snapshot failure')
        return snapshot(*args)
    def denied(path, *args, **kwargs):
        if path == job/'cancel':
            raise PermissionError('offline stop publication denied')
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(workspace, 'snapshot', capture)
    with monkeypatch.context() as patch:
        if stop_fails:
            patch.setattr(Path, 'open', denied)
            with pytest.raises(PermissionError, match='offline stop publication denied'):
                c.finish(custody, state, {'status': 'INTERRUPTED'})
        else:
            c.finish(custody, state, {'status': 'INTERRUPTED'})
    reference = (job/'reference.json').read_bytes()
    if snapshot_fails:
        assert native_agent.read_json(job/'reference.json')['error'] == 'offline snapshot failure'
    if stop_fails:
        assert not (job/'cancel').exists()
        error = native_agent.read_json(job/'stop-request-error.json')
        assert error == {'error_type': 'PermissionError', 'error': 'offline stop publication denied'}
    c.finish(custody, state, {'status': 'INTERRUPTED'})
    c.finish(custody, state, {'status': 'INTERRUPTED'})
    assert (job/'cancel').exists() and len(snapshots) == 1
    assert (job/'reference.json').read_bytes() == reference
    assert not (other/'cancel').exists()


@pytest.fixture
def boundary_worker(monkeypatch):
    from relay import challenger_worker as worker
    class NoMonitor:
        def __init__(self, **kwargs):
            pass
        def start(self):
            pass
    # Suppress only the worker monitor, not subprocess I/O/custody threads.
    # Dispatch tests must pass without relying on a one-second monitor tick.
    def thread(*args, **kwargs):
        if getattr(kwargs.get('target'), '__qualname__', '') == 'run.<locals>.watch':
            return NoMonitor(**kwargs)
        return Thread(*args, **kwargs)
    monkeypatch.setattr('threading.Thread', thread)
    monkeypatch.setattr(worker, 'checks', lambda *_: True)
    monkeypatch.setattr(c, 'study', lambda *_: 'offline-study')
    return worker


@pytest.mark.parametrize('boundary', ['entry', 'configured', 'local-release'])
def test_stop_blocks_implementation_dispatch(linked_job, boundary_worker, monkeypatch, boundary):
    custody, state, job, other = linked_job
    worker = boundary_worker
    monkeypatch.setattr(worker, 'execute', lambda *_: pytest.fail('stopped implementation dispatched'))
    def finish():
        c.finish(custody, state, {'status': 'CANCELLED'})
    if boundary == 'entry':
        finish()
    elif boundary == 'configured':
        configure = worker.session_config
        def configured(*args):
            result = configure(*args)
            finish()
            return result
        monkeypatch.setattr(worker, 'session_config', configured)
    else:
        monkeypatch.setattr(c, 'local', lambda _: True)
        monkeypatch.setattr(worker.time, 'sleep', lambda _: finish())
        save = c.save_json
        def publish(path, value, **kwargs):
            if path == job/'reference.json':
                # Publishing reference is the release edge, not the monitor tick.
                assert (job/'cancel').exists()
            return save(path, value, **kwargs)
        monkeypatch.setattr(c, 'save_json', publish)
    worker.run(job)
    row = c.read_run(job/'run.json')
    assert row['status'] == 'CANCELLED', row
    assert row['reserved_usd'] == '0.25'
    before = (job/'run.json').read_bytes()
    worker.run(job)
    assert (job/'run.json').read_bytes() == before
    assert not (other/'cancel').exists()


@pytest.mark.parametrize('boundary', ['reference-release', 'review-entry', 'between-audits', 'normal', 'failure'])
def test_review_dispatch_boundaries_keep_produced_results(linked_job, boundary_worker, monkeypatch, boundary):
    custody, state, job, other = linked_job
    worker = boundary_worker
    calls = []
    def execute(config, dep, variant, session, instruction, row, cancellation):
        calls.append('implementation')
        (job/'candidate'/'a.py').write_bytes(b'value = 2\n')
        (session/'outcome.json').write_bytes(b'{"status":"PROPOSED"}\n')
        if boundary == 'failure':
            raise RuntimeError('offline implementation failure')
        return {'status': 'PROPOSED'}
    def audit(row, path, variant, cancellation):
        calls.append(variant)
        if boundary == 'between-audits':
            c.finish(custody, state, {'status': 'INTERRUPTED'})
        return 'accepted', 'offline review receipt'
    monkeypatch.setattr(worker, 'execute', execute)
    monkeypatch.setattr(worker, 'audit', audit)
    if boundary != 'reference-release':
        c.finish(custody, state, {'status': 'PROPOSED'})
    else:
        monkeypatch.setattr(worker.time, 'sleep', lambda _: c.finish(custody, state, {'status': 'INTERRUPTED'}))
    save = worker.save_json
    def publish(path, value, **kwargs):
        result = save(path, value, **kwargs)
        if boundary == 'review-entry' and path == job/'run.json' and value['status'] == 'REVIEWING':
            c.finish(custody, state, {'status': 'CANCELLED'})
        return result
    monkeypatch.setattr(worker, 'save_json', publish)
    worker.run(job)
    row = c.read_run(job/'run.json')
    expected = {'normal': 'COMPLETED', 'failure': 'FAILED'}.get(boundary, 'CANCELLED')
    assert row['status'] == expected, row.get('reason', row)
    assert calls == ['implementation'] + (['challenger', 'reference'] if boundary == 'normal'
                                         else ['challenger'] if boundary == 'between-audits' else [])
    if boundary == 'between-audits':
        assert row['challenger_verdict'] == 'accepted' and row['reference_verdict'] == 'unknown'
    if boundary == 'failure':
        assert row['reason'] == 'offline implementation failure'
    evidence = {p: p.read_bytes() for p in (job/'implementation'/'outcome.json', job/'run.json')}
    if boundary != 'failure':
        evidence[job/'challenger.patch'] = (job/'challenger.patch').read_bytes()
        assert b'value = 2' in evidence[job/'challenger.patch']
    # Late notifications also preserve terminal results and cannot replay work.
    c.finish(custody, state, {'status': 'CANCELLED'})
    worker.run(job)
    assert all(p.read_bytes() == raw for p, raw in evidence.items())
    assert not (other/'cancel').exists()


@pytest.mark.parametrize('nested_status', ['PROPOSED', 'INTERRUPTED'])
def test_finish_reentry_does_not_repeat_snapshot_or_replace_result(linked_job, monkeypatch, nested_status):
    custody, state, job, other = linked_job
    snapshot = workspace.snapshot
    calls = []
    def reenter(root, destination):
        calls.append(destination)
        if len(calls) == 1:
            c.finish(custody, state, {'status': nested_status})
        return snapshot(root, destination)
    monkeypatch.setattr(workspace, 'snapshot', reenter)
    (job/'result.json').write_bytes(b'{"status":"COMPLETED","retained":true}\n')
    result = (job/'result.json').read_bytes()
    c.finish(custody, state, {'status': 'PROPOSED'})
    assert calls == [job/'reference']
    reference = native_agent.read_json(job/'reference.json')
    assert reference['outcome']['status'] == 'PROPOSED' and 'snapshot' in reference
    assert (job/'cancel').exists() is (nested_status == 'INTERRUPTED')
    before = (job/'reference.json').read_bytes()
    c.finish(custody, state, {'status': nested_status})
    assert (job/'reference.json').read_bytes() == before
    assert (job/'result.json').read_bytes() == result
    assert not (other/'cancel').exists()


def test_running_worker_monitor_stops_runtime_before_next_model_call(linked_job, monkeypatch):
    from relay import challenger_worker as worker
    custody, state, job, other = linked_job
    clients = []
    other_before = {p: p.read_bytes() for p in other.iterdir()}
    def execute(config, dep, variant, session, instruction, row, cancellation):
        def during_call():
            c.finish(custody, state, {'status': 'INTERRUPTED'})
            # The call was already dispatched. Let the real monitor observe the
            # request, then let Runtime finish that reply with the same token.
            deadline = time.monotonic()+5
            while not cancellation.is_cancelled() and time.monotonic() < deadline:
                time.sleep(.01)
            assert cancellation.is_cancelled(), 'running monitor did not observe linked stop'
        client = Client(steps=[steps()[0]], after_model=during_call)
        clients.append(client)
        return native_agent.run_session(config, dep, session, instruction,
            client=client, local_counter=client, cancellation=cancellation)
    monkeypatch.setattr(worker, 'execute', execute)
    monkeypatch.setattr(worker, 'audit', lambda *_: pytest.fail('audit dispatched after monitored stop'))
    monkeypatch.setattr(worker, 'checks', lambda *_: False)
    monkeypatch.setattr(c, 'study', lambda *_: 'offline-study')
    worker.run(job)
    row = c.read_run(job/'run.json')
    assert row['status'] == 'CANCELLED', row.get('reason', row)
    assert row['challenger_outcome'] == 'CANCELLED'
    assert len(clients) == 1 and clients[0].generations == 1
    assert native_agent.read_json(job/'implementation'/'outcome.json')['status'] == 'CANCELLED'
    evidence = {p: p.read_bytes() for p in (job/'reference.json', job/'run.json', job/'challenger.patch',
                                           job/'implementation'/'outcome.json')}
    c.finish(custody, state, {'status': 'INTERRUPTED'})
    worker.run(job)
    assert len(clients) == 1 and all(p.read_bytes() == raw for p, raw in evidence.items())
    assert not (other/'cancel').exists()
    assert all(p.read_bytes() == raw for p, raw in other_before.items())


@pytest.fixture
def x5_argv(project):
    from relay import test_selection_pilot as pilot
    # Obtain the actual public contract without running its wrapped command.
    return pilot.start(project, [sys.executable, '-m', 'pytest', '-q'])['wrapper_argv']


def forbidden_x5_effect(*args, **kwargs):
    pytest.fail('incompatible X5 comparison performed an experimental effect')


def block_x5_preparation(monkeypatch):
    monkeypatch.setattr(variants, 'realize', forbidden_x5_effect)
    monkeypatch.setattr(c, 'bounded', forbidden_x5_effect)
    monkeypatch.setattr(c, 'spawn', forbidden_x5_effect)
    monkeypatch.setattr(workspace, 'snapshot', forbidden_x5_effect)


@pytest.mark.parametrize('style', ['emitted', 'tuple', 'posix', 'windows'])
def test_x5_exact_signature_is_pure(x5_argv, monkeypatch, style):
    argv = list(x5_argv)
    if style == 'tuple':
        argv = tuple(argv)
    elif style == 'posix':
        argv[0], argv[5] = '/opt/python/bin/python', '/var/lib/pilot'
    elif style == 'windows':
        argv[0], argv[5] = 'C:/Python/python.exe', 'D:/state/pilot'
    config = {'verifiers': {'arbitrary-name': {'argv': argv}}}
    before = copy.deepcopy(config)
    monkeypatch.setattr(subprocess, 'Popen', forbidden_x5_effect)
    monkeypatch.setattr(Path, 'resolve', forbidden_x5_effect)
    with pytest.raises(ValueError, match='Managed X5.*incompatible.*worktrees'):
        c.require_worktree_compatible(config)
    assert config == before


@pytest.mark.parametrize('case', [
    'extra', 'missing', 'order', 'module', 'action', 'python-flag', 'relative-python',
    'relative-store', 'short-id', 'uppercase-id', 'nonhex-id', 'newline-id',
    'store-option', 'id-option', 'mention', 'pytest', 'custom', 'empty'])
def test_x5_lookalikes_and_ordinary_commands_are_not_classified(x5_argv, monkeypatch, case):
    argv = list(x5_argv)
    if case == 'extra':
        argv += ['--verbose']
    elif case == 'missing':
        argv.pop()
    elif case == 'order':
        argv[4:8] = [argv[6], argv[7], argv[4], argv[5]]
    elif case == 'module':
        argv[2] += '_custom'
    elif case == 'action':
        argv[3] = 'status'
    elif case == 'python-flag':
        argv.insert(1, '-B')
    elif case == 'relative-python':
        argv[0] = 'python'
    elif case == 'relative-store':
        argv[5] = 'state/pilot'
    elif case == 'short-id':
        argv[7] = 'a'*31
    elif case == 'uppercase-id':
        argv[7] = 'A'*32
    elif case == 'nonhex-id':
        argv[7] = 'g'*32
    elif case == 'newline-id':
        argv[7] += '\n'
    elif case == 'store-option':
        argv[4] = '--storage'
    elif case == 'id-option':
        argv[6] = '--id'
    elif case == 'mention':
        argv = [sys.executable, '-c', "print('relay.test_selection_pilot')"]
    elif case == 'pytest':
        argv = [sys.executable, '-B', '-m', 'pytest', 'tests/test_challenger.py', '-q']
    elif case == 'custom':
        argv = ['custom-check', '--module', 'relay.test_selection_pilot']
    else:
        argv = []
    config = {'verifiers': {'check': {'argv': argv}}}
    before = copy.deepcopy(config)
    monkeypatch.setattr(subprocess, 'Popen', forbidden_x5_effect)
    c.require_worktree_compatible(config)
    assert config == before


@pytest.mark.parametrize('entry', ['start', 'retry', 'work'])
def test_x5_admission_precedes_all_experimental_preparation(project, tmp_path, monkeypatch, x5_argv, entry):
    from types import SimpleNamespace
    custody = tmp_path/'admission'; custody.mkdir()
    settings = {**c.DEFAULT, 'enabled': True}
    monkeypatch.setattr(c, 'policy', lambda _: settings)
    block_x5_preparation(monkeypatch)
    # Even constructing the experimental directory is too late for this guard.
    monkeypatch.setattr(c, 'directory', forbidden_x5_effect)
    config = {'role': 'implementer', 'repositories': {'repo': str(project)},
              'verifiers': {'check': {'repository': 'repo', 'argv': x5_argv}}}
    state = {'config': config, 'session_id': 'primary'}
    before = copy.deepcopy(state)
    if entry == 'work':
        monkeypatch.setattr('relay.native_work_context.work_packet', forbidden_x5_effect)
        descriptor = SimpleNamespace(execution_repository='repo', tracked_paths=['a.py'],
            verification_checks=[SimpleNamespace(criterion='check', command=x5_argv)])
        c.start_work({'custody': str(custody)}, tmp_path/'train', descriptor, project, None)
    elif entry == 'retry':
        c.start(custody, state, retry={'id': 'retry', 'config': config})
    else:
        c.start(custody, state)
    marker_path = custody/'challenger-retries/retry.json' if entry == 'retry' else custody/'challenger.json'
    marker = native_agent.read_json(marker_path)
    assert marker['status'] == 'SKIPPED'
    assert 'Managed X5' in marker['reason'] and 'worktrees' in marker['reason']
    assert 'original project root' in marker['reason']
    assert state == before
    assert not (custody/'challenger-input.json').exists()
    assert not list(tmp_path.rglob('run.json'))


def test_x5_skip_keeps_primary_delivery_and_original_checks(project, tmp_path, monkeypatch, x5_argv):
    custody = tmp_path/'primary-x5'
    monkeypatch.setattr(c, 'policy', lambda _: {**c.DEFAULT, 'enabled': True})
    block_x5_preparation(monkeypatch)
    config = {'schema': 'tare.tools/native-agent-config/1', 'role': 'implementer',
        'repositories': {'repo': str(project)}, 'writable': {'repo': ['a.py']},
        'specgraph_edit_context': False, 'capabilities': ['read', 'edit', 'verify'],
        'verifiers': {
            'x5': {'repository': 'repo', 'argv': x5_argv},
            'unit': {'repository': 'repo', 'argv': [sys.executable, '-c',
                "from pathlib import Path; assert Path('a.py').read_text() == 'value = 2\\n'"]}}}
    before = copy.deepcopy(config)
    # Run only the ordinary custom check; never run the root-bound wrapper here.
    client = Client(steps=steps())
    result = native_agent.run_session(config, deployment(), custody, 'Set value to 2.',
                                      client=client, local_counter=client)
    assert result['status'] == 'PROPOSED'
    assert (project/'a.py').read_bytes() == b'value = 2\n'
    assert native_agent.read_json(custody/'challenger.json')['status'] == 'SKIPPED'
    assert config == before
    assert native_agent.read_json(custody/'native-agent.json')['config']['verifiers'] == before['verifiers']


@pytest.mark.parametrize('same_root', [False, True])
@pytest.mark.parametrize('role', ['implementer', 'auditor'])
def test_x5_session_config_rejects_before_copy_or_rebinding(linked_job, x5_argv, monkeypatch, same_root, role):
    from relay import challenger_worker as worker
    _, _, job, _ = linked_job
    row = c.read_run(job/'run.json')
    row['config']['verifiers']['unit']['argv'] = x5_argv
    before = copy.deepcopy(row)
    root = Path(row['project']) if same_root else job/'new-tree'
    monkeypatch.setattr(worker, 'deepcopy', forbidden_x5_effect)
    with pytest.raises(ValueError, match='Managed X5.*worktrees'):
        worker.session_config(row, root, role)
    assert row == before


def test_session_config_preserves_custom_checks_and_pytest_scratch(linked_job):
    from relay.challenger_worker import session_config
    from relay.work_units import pytest_scratch
    _, _, job, _ = linked_job
    row = c.read_run(job/'run.json')
    old = Path(row['project'])
    scratch = str(old.parent/('.'+old.name+'-pytest'))
    commands = {
        'custom': [sys.executable, '-c', "print('relay.test_selection_pilot')"],
        'pytest': [sys.executable, '-m', 'pytest', '-q'],
        'owned': [sys.executable, '-B', '-m', 'pytest', '--basetemp', scratch],
        'explicit': [sys.executable, '-m', 'pytest', '--basetemp=custom-temp']}
    row['config']['verifiers'] = {key: {'repository': 'repo', 'argv': argv} for key, argv in commands.items()}
    before = copy.deepcopy(row)
    root = job/'new-tree'
    config = session_config(row, root, 'implementer')
    assert config['repositories'] == {'repo': str(root)}
    for key, argv in commands.items():
        assert config['verifiers'][key]['argv'] == pytest_scratch(argv, argv[0], root, previous_tree=old)
    assert config['verifiers']['custom']['argv'] == commands['custom']
    assert config['verifiers']['explicit']['argv'] == commands['explicit']
    assert config['verifiers']['owned']['argv'][-1] != scratch
    assert row == before


@pytest.mark.parametrize('completed', [False, True])
@pytest.mark.parametrize('wrapper_in', ['both', 'saved', 'producer'])
def test_public_retry_revalidates_legacy_x5_without_new_work(linked_job, x5_argv, monkeypatch, completed, wrapper_in):
    from compute_plane.native_journal import save_json
    from relay import challenger_recovery as recovery
    custody, _, job, other = linked_job
    row = c.read_run(job/'run.json')
    producer_config = copy.deepcopy(row['config'])
    if wrapper_in in {'both', 'saved'}:
        row['config']['verifiers']['unit']['argv'] = list(x5_argv)
    if wrapper_in in {'both', 'producer'}:
        producer_config['verifiers']['unit']['argv'] = list(x5_argv)
    state = {'config': producer_config, 'deployment': row['baseline'], 'instruction': row['instruction'],
             'invocations': [{'id': 'finished', 'status': 'PROPOSED', 'ended_at': '2026-10-29T00:00:00Z'}]}
    save_json(custody/'native-agent.json', state)
    reference = workspace.snapshot(Path(row['project']), workspace.tree(job, 'reference', row))
    save_json(job/'reference.json', {'snapshot': reference, 'invocation': 'finished', 'outcome': {'status': 'PROPOSED'}})
    row.update(source=str(custody), status='FAILED', checks_passed=completed,
               challenger_outcome='PROPOSED' if completed else 'FAILED',
               challenger_verdict='unknown', committed_usd='0.17', local_seconds_reserved=900,
               quota_calls_reserved=30)
    implementation = job/'implementation'; implementation.mkdir()
    save_json(implementation/'native-agent.json', {
        'config': producer_config, 'deployment': row['challenger'], 'instruction': row['instruction'],
        'invocations': [{'status': 'PROPOSED' if completed else 'FAILED', 'ended_at': 'finished'}]})
    save_json(implementation/'outcome.json', {'status': 'PROPOSED' if completed else 'FAILED'})
    workspace.freeze(job, row['base'])
    assert 'execution_contract' not in row
    save_json(job/'run.json', row, max_bytes=1024*1024)
    evidence = {p: p.read_bytes() for p in [job/'run.json', job/'reference.json', job/'challenger.patch',
        custody/'native-agent.json', *implementation.iterdir(), *other.iterdir()]}
    jobs = set(job.parent.iterdir())
    block_x5_preparation(monkeypatch)
    monkeypatch.setattr(recovery, 'snapshot', forbidden_x5_effect)
    monkeypatch.setattr(recovery, 'publish_reference', forbidden_x5_effect)
    monkeypatch.setattr(c, 'start', forbidden_x5_effect)
    with pytest.raises(ValueError, match='Managed X5.*worktrees'):
        recovery.retry(Path(row['project']), job)
    assert set(job.parent.iterdir()) == jobs
    assert not (custody/'challenger-retries').exists()
    assert all(p.read_bytes() == raw for p, raw in evidence.items())


@pytest.mark.parametrize('implementation', ['absent', 'existing', 'reused'])
def test_worker_rejects_legacy_x5_before_sessions_or_reuse(linked_job, boundary_worker, x5_argv, monkeypatch, implementation):
    from compute_plane.native_journal import save_json
    _, _, job, other = linked_job
    worker = boundary_worker
    row = c.read_run(job/'run.json')
    row['config']['verifiers']['unit']['argv'] = x5_argv
    row.update(committed_usd='0.17', known_usd='0.12', uncertain_calls=1,
               local_seconds_reserved=900, quota_calls_reserved=30)
    if implementation != 'absent':
        source = job/('retained' if implementation == 'reused' else 'implementation')
        source.mkdir()
        (source/'native-agent.json').write_bytes(b'original completed producer\n')
        (source/'outcome.json').write_bytes(b'{"status":"PROPOSED"}\n')
        if implementation == 'reused':
            row['implementation_reuse'] = {'source': str(source)}
    assert 'execution_contract' not in row
    save_json(job/'run.json', row, max_bytes=1024*1024)
    evidence = {p: p.read_bytes() for p in [*job.rglob('*.json'), *other.iterdir()] if p != job/'run.json'}
    for symbol in ('execute', 'audit', 'session_config', 'costs'):
        monkeypatch.setattr(worker, symbol, forbidden_x5_effect)
    monkeypatch.setattr(workspace, 'tree', forbidden_x5_effect)
    monkeypatch.setattr(workspace, 'freeze', forbidden_x5_effect)
    block_x5_preparation(monkeypatch)
    worker.run(job)
    rejected = c.read_run(job/'run.json')
    assert rejected == {**row, 'status': 'FAILED', 'reason': rejected['reason']}
    assert 'Managed X5' in rejected['reason'] and 'worktrees' in rejected['reason']
    assert all(p.read_bytes() == raw for p, raw in evidence.items())
    assert not (job/'implementation').exists() if implementation != 'existing' else (job/'implementation').is_dir()
    assert not (job/'challenger-audit').exists() and not (job/'reference-audit').exists()
    before = (job/'run.json').read_bytes()
    worker.run(job)
    assert (job/'run.json').read_bytes() == before


@pytest.mark.parametrize('wrapper_in', ['row', 'dispatch'])
def test_execute_revalidates_x5_before_preflight_or_client(linked_job, x5_argv, monkeypatch, wrapper_in):
    from relay import challenger_worker as worker
    _, _, job, _ = linked_job
    row = c.read_run(job/'run.json')
    config = copy.deepcopy(row['config'])
    target = row['config'] if wrapper_in == 'row' else config
    target['verifiers']['unit']['argv'] = x5_argv
    monkeypatch.setattr(variants, 'preflight', forbidden_x5_effect)
    monkeypatch.setattr(variants, 'price', forbidden_x5_effect)
    monkeypatch.setattr('relay.native_generation.inference_client', forbidden_x5_effect)
    monkeypatch.setattr(worker, 'run_session', forbidden_x5_effect)
    with pytest.raises(ValueError, match='Managed X5.*worktrees'):
        worker.execute(config, row['challenger'], row['variant'], job/'implementation', '', row, None)
    assert not (job/'implementation').exists()


def test_audit_revalidates_x5_before_session_creation(linked_job, x5_argv, monkeypatch):
    from relay import challenger_worker as worker
    _, _, job, _ = linked_job
    row = c.read_run(job/'run.json')
    row['config']['verifiers']['unit']['argv'] = x5_argv
    monkeypatch.setattr('relay.work_audit.prepare', forbidden_x5_effect)
    monkeypatch.setattr(worker, 'execute', forbidden_x5_effect)
    with pytest.raises(ValueError, match='Managed X5.*worktrees'):
        worker.audit(row, job, 'challenger', None)
    assert not (job/'challenger-audit').exists()
