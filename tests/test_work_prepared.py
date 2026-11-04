"""Real linked worktrees and Runtime proposals; imports keep admission separate."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from relay import work_prepared as prepared
from relay import native_agent, relay_mesh
from relay.train_execution import canonical_json_bytes, parse_execution_descriptor
from relay.work_run import derive_descriptor
from compute_plane.workspace import provision_linked_worktree
from test_native_agent import Client, deployment


def git(root, *args):
    return subprocess.run(['git', *args], cwd=root, check=True, capture_output=True).stdout.decode().strip()


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    repo = tmp_path/'repo'; repo.mkdir()
    git(repo, 'init', '-b', 'main')
    git(repo, 'config', 'user.email', 'test@example.invalid')
    git(repo, 'config', 'user.name', 'Test')
    (repo/'value.txt').write_bytes(b'base\n')
    (repo/'other.txt').write_bytes(b'other\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'base')
    dep = deployment('bedrock-converse') | {'model': 'fixture-model'}
    control = tmp_path/'relay'; control.mkdir()
    (control/'round_table_roster.json').write_text(json.dumps({'models': {'fixture-model': {
        'runtime': {'native': 'fixture'}, 'vendor': 'fixture', 'bias_domain': 'fixture'}}}))
    monkeypatch.setattr(relay_mesh, 'RELAY_DIR', control)
    monkeypatch.setattr(native_agent, 'SESSION_ROOT', tmp_path/'sessions')
    steps = [('read', {'repository': 'repo', 'path': 'value.txt'}),
        ('edit', {'repository': 'repo', 'path': 'value.txt', 'sha256': hashlib.sha256(b'base\r\n' if os.name == 'nt' else b'base\n').hexdigest(),
                  'search': 'base', 'replace': 'ready'}),
        ('verify', {'name': 'ready'}),
        ('tare_deliver', {'summary': 'Changed value and verified.', 'sources': [], 'remaining': []})]
    client = Client('bedrock-converse', steps)
    monkeypatch.setattr(native_agent, 'inference_client', lambda *a, **k: client)
    node = {'id': 'work-one', 'title': 'Change value', 'summary': 'Change base to ready in value.txt.',
        'repository_scopes': [{'repository': 'repo', 'target_paths': ['value.txt']}],
        'exit_criteria': ['ready'], 'metrics': {'verification_checks': [{'criterion': 'ready',
        'command': [sys.executable, '-B', '-c', "from pathlib import Path; assert Path('value.txt').read_text() == 'ready\\n'"]}]}}
    return tmp_path/'preparation', node, repo, dep, client


def target(fixture, tmp_path):
    directory, node, repo, _, _ = fixture
    descriptor = parse_execution_descriptor(canonical_json_bytes(derive_descriptor(
        node, train_id='run-work-one', repository_root=repo)))
    lease = provision_linked_worktree(repo, tmp_path/'target', base_commit=descriptor.base_commit,
                                     worktree_id='import-target-123')
    return descriptor, lease


def test_real_proposal_import_is_isolated_idempotent_and_has_no_admission(fixture, tmp_path):
    directory, node, repo, dep, client = fixture
    row = prepared.prepare(directory, node, repo, dep)
    assert row['status'] == 'PREPARED', row
    assert row['authority'] == 'NONE' and row['session'] == str(directory/'session')
    assert (repo/'value.txt').read_bytes() == b'base\n'
    assert client.generations == 4
    assert prepared.prepare(directory, node, repo, dep)['sha256'] == row['sha256']
    assert client.generations == 4
    descriptor, lease = target(fixture, tmp_path)
    assert prepared.apply(directory, descriptor, lease)['status'] == 'APPLIED'
    assert prepared.apply(directory, descriptor, lease)['status'] == 'APPLIED'
    assert (lease.root/'value.txt').read_text() == 'ready\n'
    assert (repo/'value.txt').read_text() == 'base\n'
    assert prepared.identity(directory).model_id == 'fixture-model'
    assert not list(directory.rglob('NATIVE_ACCEPTANCE.json'))
    assert not list(directory.rglob('claim.json'))


@pytest.mark.parametrize('overlap', [False, True])
def test_new_base_accepts_only_untouched_blobs(fixture, tmp_path, overlap):
    directory, node, repo, dep, _ = fixture
    prepared.prepare(directory, node, repo, dep)
    (repo/('value.txt' if overlap else 'other.txt')).write_text('advanced\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'advance')
    descriptor, lease = target(fixture, tmp_path)
    result = prepared.apply(directory, descriptor, lease)
    assert result['status'] == ('CONFLICT' if overlap else 'APPLIED')
    assert (lease.root/'value.txt').read_text() == ('advanced\n' if overlap else 'ready\n')
    if overlap:
        assert result['reason'] == 'PREPARED_BASE_OVERLAP'
        assert not git(lease.root, 'status', '--porcelain')


@pytest.mark.parametrize('name', ['implementation.patch', 'session/journal.jsonl', 'session/outcome.json', 'session/native-agent.json'])
def test_changed_evidence_rejected_before_import(fixture, tmp_path, name):
    directory, node, repo, dep, _ = fixture
    prepared.prepare(directory, node, repo, dep)
    with (directory/name).open('ab') as stream:
        stream.write(b'changed')
    descriptor, lease = target(fixture, tmp_path)
    with pytest.raises(ValueError, match='PREPARED_EVIDENCE_CHANGED'):
        prepared.apply(directory, descriptor, lease)
    assert not git(lease.root, 'status', '--porcelain')


def test_crash_after_runtime_proposal_reuses_session_without_model_replay(fixture, monkeypatch):
    directory, node, repo, dep, client = fixture
    original = prepared.save_json
    def crash(path, value, **kwargs):
        if path.name == 'prepared.json':
            raise SystemExit('crash before freeze')
        return original(path, value, **kwargs)
    monkeypatch.setattr(prepared, 'save_json', crash)
    with pytest.raises(SystemExit):
        prepared.prepare(directory, node, repo, dep)
    assert client.generations == 4
    monkeypatch.setattr(prepared, 'save_json', original)
    assert prepared.prepare(directory, node, repo, dep)['status'] == 'PREPARED'
    assert client.generations == 4


def test_scope_and_dirty_target_refuse_without_overwriting(fixture, tmp_path):
    directory, node, repo, dep, _ = fixture
    prepared.prepare(directory, node, repo, dep)
    with pytest.raises(ValueError, match='PREPARED_PATH_OUTSIDE_SCOPE'):
        prepared.check(directory, repo, git(repo, 'rev-parse', 'HEAD'), ['other.txt'])
    descriptor, lease = target(fixture, tmp_path)
    (lease.root/'other.txt').write_text('preserve\n')
    assert prepared.apply(directory, descriptor, lease)['reason'] == 'PREPARED_TARGET_DIRTY'
    assert (lease.root/'other.txt').read_text() == 'preserve\n'
    assert (lease.root/'value.txt').read_text() == 'base\n'


def test_import_retry_does_not_accept_extra_edits_or_changed_goal(fixture, tmp_path, monkeypatch):
    directory, node, repo, dep, _ = fixture
    prepared.prepare(directory, node, repo, dep)
    descriptor, lease = target(fixture, tmp_path)
    qualify = prepared.qualify_ephemeral_git_worktree
    def require_content_check(*args, **kwargs):
        observed = qualify(*args, **kwargs)
        if observed.clean:
            # Force Git to rehash instead of trusting stat data refreshed by
            # Kernel qualification. File bytes and their meaning stay intact.
            path = lease.root/'value.txt'
            before = path.stat()
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 2_000_000_000))
        return observed
    monkeypatch.setattr(prepared, 'qualify_ephemeral_git_worktree', require_content_check)
    imported = prepared.apply(directory, descriptor, lease)
    assert imported['status'] == 'APPLIED', imported
    with (lease.root/'value.txt').open('a') as stream:
        stream.write('do not discard\n')
    git(lease.root, 'add', 'value.txt')
    assert prepared.apply(directory, descriptor, lease)['status'] == 'CONFLICT'
    assert 'do not discard' in (lease.root/'value.txt').read_text()
    with pytest.raises(ValueError, match='PREPARED_INPUT_CHANGED'):
        prepared.prepare(directory, {**node, 'summary': 'Different objective'}, repo, dep)


@pytest.mark.parametrize('cleanup', [True, False])
def test_explicit_resume_reconciles_eligible_model_interruption_without_reediting(fixture, monkeypatch, cleanup):
    from compute_plane.native_journal import NativeJournal
    from compute_plane.native_models import NativeHttpClient, NativeTransportInterrupted
    directory, node, repo, dep, original = fixture
    class Interrupted(Client, NativeHttpClient):
        def post_observed(self, path, body, control):
            if path.endswith('/converse') and self.generations == 2:
                self.generations += 1
                raise NativeTransportInterrupted('HTTP_INCOMPLETE_STREAM', {
                    'started': True, 'cleanup_verified': cleanup, 'remote_stop': 'unknown',
                    'http_attempts_upper_bound': 1})
            return self.post(path, body, control), {'started': True, 'cleanup_verified': True,
                'remote_stop': 'unknown', 'http_attempts_upper_bound': 1}
    client = Interrupted('bedrock-converse', original.steps)
    monkeypatch.setattr(native_agent, 'inference_client', lambda *a, **k: client)
    first = prepared.prepare(directory, node, repo, dep)
    assert first['status'] == 'RECONCILIATION_REQUIRED', first
    assert first['reason'] == 'UNFINISHED_INVOCATION'
    assert (directory/'workspace/value.txt').read_text() == 'ready\n'
    assert client.generations == 3
    result = prepared.prepare(directory, node, repo, dep)
    assert result['status'] == ('PREPARED' if cleanup else 'RECONCILIATION_REQUIRED'), result
    assert client.generations == (5 if cleanup else 3)
    events = [row.event() for row in NativeJournal(directory/'session/journal.jsonl').snapshot().records]
    assert sum(event['kind'] == 'model_abandoned' for event in events) == int(cleanup)
    assert sum(event['kind'] == 'effect_requested' and event['tool_call']['name'] == 'edit'
               for event in events) == 1
    assert (repo/'value.txt').read_text() == 'base\n'


def test_pending_review_is_preserved_without_repeating_completed_implementation(fixture):
    directory, node, repo, dep, client = fixture
    pending = 'Semantic accuracy requires independent reviewer assessment.'
    steps = list(client.steps)
    steps[-1][1]['remaining'] = [pending]
    client.steps = iter(steps)
    row = prepared.prepare(directory, node, repo, dep)
    assert row['status'] == 'PREPARED' and row['authority'] == 'NONE'
    assert row['proposal_payload'] == {
        'summary': 'Changed value and verified.', 'sources': [], 'remaining': [pending]}
    assert (repo/'value.txt').read_text() == 'base\n'
    assert client.generations == 4
    assert prepared.prepare(directory, node, repo, dep)['sha256'] == row['sha256']
    assert client.generations == 4
    native = json.loads((directory/'session/outcome.json').read_bytes())
    assert native['status'] == 'PROPOSED'
    assert native['capability_observations']['verifications']['ready']['returncode'] == 0
    assert not list(directory.rglob('NATIVE_ACCEPTANCE.json'))
    frozen = json.loads((directory/'prepared.json').read_bytes())
    frozen['proposal_payload']['remaining'] = []
    (directory/'prepared.json').write_text(json.dumps(frozen), encoding='utf-8')
    with pytest.raises(ValueError, match='PREPARED_SOURCE_IDENTITY_CHANGED'):
        prepared.inspect(directory)


def test_old_preparation_exposes_hashed_payload_without_rewriting_digest(fixture):
    directory, node, repo, dep, client = fixture
    prepared.prepare(directory, node, repo, dep)
    frozen = json.loads((directory/'prepared.json').read_bytes())
    payload = frozen.pop('proposal_payload')
    (directory/'prepared.json').write_text(json.dumps(frozen), encoding='utf-8')
    digest = hashlib.sha256((directory/'prepared.json').read_bytes()).hexdigest()
    observed = prepared.inspect(directory)
    assert observed['sha256'] == digest and observed['proposal_payload'] == payload
    assert prepared.prepare(directory, node, repo, dep)['sha256'] == digest
    assert client.generations == 4
