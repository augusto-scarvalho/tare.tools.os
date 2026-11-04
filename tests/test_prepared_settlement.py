"""Prepared proposal incorporation uses real candidate verification, Git and settlement."""
import json
from pathlib import Path

import pytest

from relay import relay_mesh, work_run, work_prepared, work_audit
from test_work_run import os_root, _git
from test_work_resume import host, interrupt_after_git


@pytest.fixture
def prepared(host, monkeypatch):
    import hashlib
    from relay.native_catalog import select
    root, repo, profile_path, client, plans = host
    directory = root/'prepared'; directory.mkdir()
    (directory/'implementation.patch').write_bytes(b'fixture patch\n')
    deployment = select(path=profile_path)
    source = dict(work_id='work-one', repository_root=str(repo.resolve()), deployment=deployment,
                  sha256='a'*64, session=str(directory/'session'), artifact=str(directory),
                  proposal='session/artifact.json', proposal_payload={'summary': 'Prepared fixture', 'remaining': []},
                  hashes={'implementation.patch': hashlib.sha256(b'fixture patch\n').hexdigest(),
                          'session/artifact.json': 'b'*64},
                  node=work_prepared._node(work_run.load_node(relay_mesh.WORK_GRAPH_FILE, 'work-one')))
    calls = []
    monkeypatch.setattr(work_prepared, 'inspect', lambda path: dict(source))
    monkeypatch.setattr(work_prepared, 'identity', lambda path: work_audit.identity(deployment, 'implementer'))
    def apply(path, descriptor, lease):
        calls.append(str(lease.root))
        target = lease.root/'src/value.txt'
        assert target.read_text() in {'base\n', 'strict\n'}
        target.write_text('strict\n', encoding='utf-8')
        return {'status': 'APPLIED', 'sha256': source['sha256'], 'session': source['session']}
    monkeypatch.setattr(work_prepared, 'apply', apply)
    return host, directory, source, calls


def start(case):
    host, directory, _, _ = case
    return work_run.run_work('work-one', repository_root=host[1], prepared_implementation=directory)


def test_prepared_settlement_keeps_native_lineage_without_claim_or_model_reexecution(prepared):
    host, directory, source, calls = prepared
    result = start(prepared)
    assert result['status'] == 'SETTLED', result
    assert result['execution'] == 'prepared'
    assert result['prepared_implementation']['sha256'] == source['sha256']
    assert not host[3].generations and len(host[4]) == 1 and len(calls) == 1
    train = work_run.train_dir_for('work-one')
    assert not (train/'NATIVE_CLAIM.json').exists()
    assert not (train/'NATIVE_ACCEPTANCE.json').exists()
    origin = json.loads((train/'WORK_RUN.json').read_bytes())
    assert origin['implementation_custody'] == source['session']
    assert (host[1]/'src/value.txt').read_text() == 'strict\n'
    repeated = work_run.run_work('work-one', resume=True)
    assert repeated['status'] == 'SETTLED', repeated
    assert repeated['recovery']['new_implementation_model_calls'] == 0
    assert repeated['recovery']['new_verifier_calls'] == 0


@pytest.mark.parametrize('cut', ['apply', 'pin', 'candidate', 'git_landed'])
def test_prepared_crash_recovers_without_reimplementing_or_reapplying_pinned_bytes(prepared, monkeypatch, cut):
    from compute_plane import candidate_git
    host, directory, source, calls = prepared
    with monkeypatch.context() as fault:
        if cut == 'apply':
            original = work_prepared.apply
            def stop(*a, **kw):
                original(*a, **kw)
                raise SystemExit('applied before host checkpoint')
            fault.setattr(work_prepared, 'apply', stop)
        elif cut == 'pin':
            original = candidate_git.pin_candidate_commit
            def stop(*a, **kw):
                original(*a, **kw)
                raise SystemExit('pinned before verification')
            fault.setattr(candidate_git, 'pin_candidate_commit', stop)
        elif cut == 'candidate':
            original = relay_mesh.transition_train
            def stop(train_id, status, *a, **kw):
                if status == 'AUDIT_PENDING':
                    raise SystemExit('candidate recorded before audit transition')
                return original(train_id, status, *a, **kw)
            fault.setattr(relay_mesh, 'transition_train', stop)
        else:
            interrupt_after_git(fault)
        with pytest.raises(SystemExit):
            start(prepared)
    result = work_run.run_work('work-one', resume=True)
    assert result['status'] == 'SETTLED', result
    assert len(calls) == (2 if cut == 'apply' else 1)
    assert not host[3].generations and len(host[4]) == 1
    assert _git('status', '--porcelain', cwd=host[1]) == ''
    assert (host[1]/'src/value.txt').read_text() == 'strict\n'


def test_prepared_recovery_refuses_changed_source_before_settlement(prepared, monkeypatch):
    host, directory, source, calls = prepared
    with monkeypatch.context() as fault:
        fault.setattr(relay_mesh, 'materialize_admission_evidence',
                      lambda *a: (_ for _ in ()).throw(SystemExit('candidate ready')))
        with pytest.raises(SystemExit):
            start(prepared)
    before = _git('rev-parse', 'HEAD', cwd=host[1])
    source['sha256'] = 'b'*64
    result = work_run.run_work('work-one', resume=True)
    assert result['status'] == 'FAILED', result
    assert 'PREPARED_IMPLEMENTATION_CHANGED' in result['error']
    assert _git('rev-parse', 'HEAD', cwd=host[1]) == before
    assert len(calls) == 1 and not host[3].generations


def test_cancelled_prepared_verification_preserves_target_and_can_resume(prepared, monkeypatch):
    from tare_tools_agent_runtime import CancellationToken
    host, directory, _, _ = prepared
    token = CancellationToken()
    with monkeypatch.context() as fault:
        original = work_prepared.apply
        def cancel(*args):
            result = original(*args)
            token.cancel()
            return result
        fault.setattr(work_prepared, 'apply', cancel)
        result = work_run.run_work('work-one', repository_root=host[1],
                                   prepared_implementation=directory, cancellation=token)
    assert result['status'] == 'CANCELLED', result
    assert (host[1]/'src/value.txt').read_text() == 'base\n'
    assert work_run.run_work('work-one', resume=True)['status'] == 'SETTLED'
    assert not host[3].generations


@pytest.mark.parametrize('verdict', ['REVISE', 'REJECT'])
def test_prepared_obligation_reaches_plan_reviewer_and_rejection_blocks_incorporation(prepared, monkeypatch, verdict):
    from relay import native_work_host
    host, directory, source, calls = prepared
    source['proposal_payload'] = {'summary': 'Code prepared, semantic requirement still unresolved.',
                                  'remaining': ['The guide still needs the required escalation procedure.']}
    def review(deployment, train, repository, root, custody, **kwargs):
        name = native_work_host.plan_context(train, relay_mesh.RELAY_ROOT, custody, repository_root=root)
        context = json.loads((train/name).read_bytes())['prepared_implementation']
        assert context['summary'] == source['proposal_payload']['summary']
        assert context['remaining'] == source['proposal_payload']['remaining']
        assert context['source']['sha256'] == source['sha256']
        assert context['proposal']['sha256'] == source['hashes']['session/artifact.json']
        assert (train/context['patch']['path']).read_bytes() == (directory/'implementation.patch').read_bytes()
        assert context['authority'] == 'NONE'
        # Controlled reviewer result tests routing; it does not claim a live model assessed this fixture.
        return {'verdict': verdict, 'findings': ['Escalation procedure missing.'],
                'remaining': source['proposal_payload']['remaining']}
    monkeypatch.setattr(native_work_host, 'plan_audit', review)
    result = start(prepared)
    assert result['status'] == 'PLAN_REJECTED', result
    assert not calls and result['candidate_commit'] is None and result['landing'] is None
    assert result['stages'][-1]['remaining'] == source['proposal_payload']['remaining']
    assert (host[1]/'src/value.txt').read_text() == 'base\n'


def test_real_runtime_proposal_incorporates_on_advanced_target_without_second_implementation(host, monkeypatch):
    import hashlib
    import os
    from relay import native_agent
    from relay.native_catalog import select
    from test_native_agent import Client
    root, repo, profile_path, _, plans = host
    client = Client('bedrock-converse', [
        ('read', {'repository': 'repo-one', 'path': 'src/value.txt'}),
        ('edit', {'repository': 'repo-one', 'path': 'src/value.txt',
                  'sha256': hashlib.sha256(b'base\r\n' if os.name == 'nt' else b'base\n').hexdigest(),
                  'search': 'base', 'replace': 'strict'}),
        ('verify', {'name': 'content-is-valid'}),
        ('tare_deliver', {'summary': 'Changed and checked.', 'sources': [], 'remaining': []})])
    monkeypatch.setattr(native_agent, 'inference_client', lambda *a, **kw: client)
    source = root/'prepared-real'
    row = work_prepared.prepare(source, work_run.load_node(relay_mesh.WORK_GRAPH_FILE, 'work-one'),
                                repo, select(path=profile_path))
    assert row['status'] == 'PREPARED', row
    assert client.generations == 4
    (repo/'other.txt').write_text('unrelated advance\n', encoding='utf-8')
    _git('add', 'other.txt', cwd=repo); _git('commit', '-qm', 'unrelated', cwd=repo)
    new_base = _git('rev-parse', 'HEAD', cwd=repo)
    result = work_run.run_work('work-one', repository_root=repo, prepared_implementation=source)
    assert result['status'] == 'SETTLED', result
    assert client.generations == 4 and len(plans) == 1
    assert _git('rev-parse', 'HEAD^', cwd=repo) == new_base
    assert (repo/'other.txt').read_text() == 'unrelated advance\n'
    assert (repo/'src/value.txt').read_text() == 'strict\n'
    assert work_prepared.inspect(source)['sha256'] == row['sha256']
    assert work_run.run_work('work-one', resume=True)['status'] == 'SETTLED'
    assert client.generations == 4
