"""The identity check of a push: only unpublished commits are looked at, and only noreply addresses pass."""
from pathlib import Path
import subprocess
import sys

import pytest

from relay import push_preflight

PERSONAL = 't@example.org'
REAL_NOREPLY = ['augusto-scarvalho@users.noreply.github.com', '21094776+augusto-scarvalho@users.noreply.github.com',
                'noreply@github.com', '41898282+github-actions[bot]@users.noreply.github.com',
                'Noreply@GitHub.com']


def commit(sha='1a2b3c4d5e6f', author=REAL_NOREPLY[0], committer=REAL_NOREPLY[0]):
    return {'sha': sha, 'author': author, 'committer': committer}


@pytest.mark.parametrize('address', REAL_NOREPLY)
def test_a_github_noreply_address_is_accepted(address):
    assert push_preflight.identity_refusals([commit(author=address, committer=address)]) == []


def test_a_personal_author_is_refused_naming_the_commit_and_the_role():
    assert push_preflight.identity_refusals([commit(author=PERSONAL)]) == [
        f'commit 1a2b3c4d: author email {PERSONAL} is not a GitHub noreply address']


def test_a_personal_committer_alone_is_refused():
    assert push_preflight.identity_refusals([commit(committer=PERSONAL)]) == [
        f'commit 1a2b3c4d: committer email {PERSONAL} is not a GitHub noreply address']


@pytest.mark.parametrize('address', ['t@example.org.users.noreply.github.com.evil.io', 'x@noreply.github.com',
                                     'a b@users.noreply.github.com', ''])
def test_an_address_that_only_looks_like_noreply_is_refused(address):
    assert push_preflight.identity_refusals([commit(author=address)])


def test_an_allowed_identity_is_accepted_whatever_its_case():
    assert push_preflight.identity_refusals([commit(author=PERSONAL, committer='T@Example.org')],
                                            allow=['t@EXAMPLE.org']) == []


def checkout(tmp_path):
    """A clone whose origin/main has a personal-email commit already; `feature` adds one more."""
    remote, work = tmp_path/'remote.git', tmp_path/'work'
    subprocess.run(['git', 'init', '-q', '--bare', '-b', 'main', str(remote)], check=True)
    subprocess.run(['git', 'clone', '-q', str(remote), str(work)], check=True, capture_output=True)
    def git(*args, email=PERSONAL):
        return subprocess.run(['git', '-C', str(work), '-c', 'user.name=T', '-c', f'user.email={email}',
                               '-c', 'core.hooksPath=/dev/null', *args], capture_output=True, text=True,
                              check=True).stdout.strip()
    (work/'a.txt').write_text('1')
    git('add', '.')
    git('commit', '-qm', 'published long ago')
    git('push', '-q', 'origin', 'HEAD:main')
    git('checkout', '-qb', 'feature')
    (work/'a.txt').write_text('2')
    git('commit', '-qam', 'new')
    return work, git


def test_only_the_commits_no_remote_branch_has_are_listed(tmp_path):
    work, git = checkout(tmp_path)
    found = push_preflight.unpublished(work, 'HEAD', 'origin')
    assert [(c['sha'], c['author'], c['committer']) for c in found] == [
        (git('rev-parse', 'HEAD'), PERSONAL, PERSONAL)]
    assert push_preflight.identity_refusals(found) == [
        f"commit {git('rev-parse', 'HEAD')[:8]}: author email {PERSONAL} is not a GitHub noreply address",
        f"commit {git('rev-parse', 'HEAD')[:8]}: committer email {PERSONAL} is not a GitHub noreply address"]


def test_without_a_remote_branch_all_of_the_history_would_be_published(tmp_path):
    work, git = checkout(tmp_path)
    assert len(push_preflight.unpublished(work, 'HEAD', 'nowhere')) == 2


def test_main_names_each_refusal_and_the_fix_on_stderr(tmp_path, capsys):
    work, git = checkout(tmp_path)
    assert push_preflight.main(['origin', 'HEAD', '--repo', str(work)]) == 1
    error = capsys.readouterr().err.splitlines()
    assert len(error) == 3 and all(git('rev-parse', 'HEAD')[:8] in line for line in error[:2])
    assert 'git config user.email' in error[2] and 'not published yet' in error[2]


def test_main_is_silent_and_succeeds_when_the_push_is_clean(tmp_path, capsys):
    work, git = checkout(tmp_path)
    (work/'a.txt').write_text('3')
    git('commit', '-qam', 'clean', email='noreply@github.com')
    # The personal commit before the tip is unpublished too, so publish it first (a branch of its own).
    git('push', '-q', 'origin', 'HEAD~1:refs/heads/older')
    assert push_preflight.main(['origin', 'HEAD', '--repo', str(work)]) == 0
    assert capsys.readouterr() == ('', '')


def test_the_module_runs_as_a_script_with_a_bare_python(tmp_path):
    work, git = checkout(tmp_path)
    script = Path(push_preflight.__file__)
    result = subprocess.run([sys.executable, str(script), 'origin', 'HEAD', '--repo', str(work)], capture_output=True, text=True)
    assert result.returncode == 1 and 'author email' in result.stderr


def test_a_commit_is_unsigned_unless_it_carries_a_signature_header(tmp_path):
    work, git = checkout(tmp_path)
    head, _, message = git('cat-file', 'commit', 'HEAD').partition(chr(10) * 2)
    signature = ['gpgsig -----BEGIN PGP SIGNATURE-----', ' ', ' abc', ' -----END PGP SIGNATURE-----']
    raw = chr(10).join([head, *signature, '', message, ''])  # the same commit with a signature header; no gpg runs
    signed = subprocess.run(['git', '-C', str(work), 'hash-object', '-t', 'commit', '-w', '--stdin'],
                            input=raw.encode(), capture_output=True, check=True).stdout.decode().strip()
    assert push_preflight.unsigned(work, 'HEAD') == [git('rev-parse', 'HEAD')]
    assert push_preflight.unsigned(work, signed) == []
