import json
import os
import subprocess

import pytest

from relay import ci_watch

REAL_GH = ci_watch.gh  # before the fixture replaces it


@pytest.fixture(autouse=True)
def working_login(monkeypatch):
    """gh's login works unless a test says otherwise: a not-found then proves its subject gone."""
    monkeypatch.setattr(ci_watch, 'logged_in', lambda: True)


@pytest.fixture
def github(tmp_path, monkeypatch):
    """A scripted `gh`: each runs_for call returns the next snapshot of the commit's runs."""
    monkeypatch.setenv('TARE_CI_ALERTS', str(tmp_path/'ci'))
    snapshots, notified = [], []
    def gh(args, cwd):
        if args[:2] == ['repo', 'view']:
            return json.dumps({'nameWithOwner': 'me/os'})
        if args[:2] == ['run', 'list']:
            return json.dumps(snapshots.pop(0) if len(snapshots) > 1 else snapshots[0])
        if args[:2] == ['run', 'view']:
            return ('ci (win)\tRun tests\t2026-09-30T16:31:16.0709032Z FAILED tests/test_a.py::one - TypeError: boom\n'
                    'ci (win)\tRun tests\t2026-09-30T16:31:16.0709032Z FAILED tests/test_a.py::two - TypeError: boom\n'
                    'ci (win)\tRun tests\t2026-09-30T16:31:16.0709032Z collected 9 items\n'
                    'ci (win)\tRun tests\t2026-09-30T16:31:16.0709032Z 2 failed, 7 passed in 1.0s\n')
        raise AssertionError(args)
    monkeypatch.setattr(ci_watch, 'gh', gh)
    monkeypatch.setattr(ci_watch, 'notify', notified.append)
    return snapshots, notified


def run(id, status, conclusion=None, created='2026-09-30T16:00:00Z', workflow='CI'):
    return {'databaseId': id, 'status': status, 'conclusion': conclusion, 'workflowName': workflow,
            'headBranch': 'main', 'url': f'https://gh/run/{id}', 'createdAt': created, 'displayTitle': 'feat: x'}


def test_a_red_run_opens_one_alert_with_its_errors_and_a_later_green_run_closes_it(github, tmp_path):
    snapshots, notified = github
    snapshots += [[], [run(1, 'in_progress')], [run(1, 'completed', 'failure')]]
    opened = ci_watch.watch(tmp_path, 'abc', sleep=lambda s: None)
    assert len(opened) == 1 and notified == opened
    [alert] = ci_watch.open_alerts()
    assert alert['errors'] == ['ci (win): FAILED tests/test_a.py::one - TypeError: boom (+1 with the same error)',
                               'ci (win): 2 failed, 7 passed in 1.0s']
    snapshots[:] = [[run(2, 'completed', 'success', created='2026-09-30T15:00:00Z')]]
    ci_watch.watch(tmp_path, 'older', sleep=lambda s: None)
    assert ci_watch.open_alerts(), 'an older green run finishing late must not close a newer red one'
    snapshots[:] = [[run(3, 'completed', 'success', created='2026-09-30T17:00:00Z')]]
    ci_watch.watch(tmp_path, 'fix', sleep=lambda s: None)
    assert ci_watch.open_alerts() == []


def test_a_shepherd_that_stopped_beating_is_started_again_once(tmp_path, monkeypatch):
    # A pull request's CI alerts only through its shepherd: one that died, or whose machine restarted, is revived.
    monkeypatch.setenv('TARE_CI_ALERTS', str(tmp_path/'ci'))
    monkeypatch.chdir(tmp_path)  # a beat written before shepherds recorded a repository: only its checkout says
    (tmp_path/'a').mkdir()  # whether there is anything left to follow
    started = []
    monkeypatch.setattr(ci_watch, 'detached', lambda *args, session=None: started.append((*args, session)))
    ci_watch.update(lambda beats: beats.update({
        'a#1': {'repo_dir': 'a', 'number': 1, 'session': 's', 'at': 1000.0},
        'a#2': {'repo_dir': 'a', 'number': 2, 'session': None, 'at': 1000.0 + ci_watch.SHEPHERD_SILENCE}}),
        'shepherds.json')
    now = 1001.0 + ci_watch.SHEPHERD_SILENCE
    assert ci_watch.revive(now) == [1] and started == [('shepherd', 'a', 1, 's')]
    assert ci_watch.revive(now + 1) == [] and len(started) == 1  # revived: silent again only after a new silence


def test_git_runs_in_the_folder_it_is_given_after_a_hook_named_another(tmp_path, monkeypatch):
    # The pre-push hook starts `tare ci watch` with the pushing worktree in GIT_DIR and GIT_INDEX_FILE; inherited,
    # a shepherd's `git -C <throwaway tree>` merged and pushed that worktree's branch instead (2026-10-08).
    pushing, other = tmp_path/'pushing', tmp_path/'other'
    for repo in (pushing, other):
        subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    monkeypatch.setenv('GIT_DIR', str(pushing/'.git'))
    monkeypatch.setenv('GIT_INDEX_FILE', str(pushing/'.git'/'index'))
    monkeypatch.setenv('GIT_AUTHOR_DATE', '@1791461944 -0300')
    monkeypatch.setenv('GIT_EDITOR', ':')
    ci_watch.forget_pushing_worktree()
    top = subprocess.run(['git', '-C', str(other), 'rev-parse', '--absolute-git-dir'], capture_output=True, text=True,
                         check=True).stdout.strip()
    assert top.replace('\\', '/').lower() == str(other/'.git').replace('\\', '/').lower()
    assert 'GIT_AUTHOR_DATE' not in os.environ
    assert os.environ['GIT_EDITOR'] == ':'  # not the worktree's: kept


def test_a_shepherd_beats_while_it_runs_and_stops_when_its_pull_request_is_done(tmp_path, monkeypatch):
    monkeypatch.setenv('TARE_CI_ALERTS', str(tmp_path/'ci'))
    views, beats = iter([{'mergeStateStatus': 'CLEAN'}, {'state': 'MERGED'}]), []
    def gh(args, cwd):
        if args[:2] == ['repo', 'view']:
            return json.dumps({'nameWithOwner': 'me/os'})
        if args[:2] == ['run', 'list']:
            return '[]'
        return json.dumps({'state': 'OPEN', 'autoMergeRequest': {'mergeMethod': 'MERGE'}, 'headRefName': 'feature',
                           'headRefOid': 'h', 'url': 'u', 'title': 't', **next(views)})
    monkeypatch.setattr(ci_watch, 'gh', gh)
    assert ci_watch.shepherd('repo', 9, sleep=lambda s: beats.append(ci_watch.read('shepherds.json'))) == 'MERGED'
    assert list(beats[0]) == ['repo#9'] and ci_watch.read('shepherds.json') == {}


def test_a_failed_pull_request_run_is_left_to_its_shepherd(github, tmp_path):
    # Every first failure alerted on 2026-10-07 while the shepherd was already running it again.
    snapshots, notified = github
    snapshots += [[{**run(1, 'completed', 'failure'), 'event': 'pull_request'}]]
    assert ci_watch.watch(tmp_path, 'abc', sleep=lambda s: None) == [] and ci_watch.open_alerts() == []


def test_github_failing_at_the_first_call_does_not_kill_the_watcher(github, tmp_path, monkeypatch):
    snapshots, notified = github
    snapshots += [[run(1, 'completed', 'failure')]]
    scripted, failures = ci_watch.gh, [1]
    def flaky(args, cwd):
        if failures:
            failures.pop()
            raise ci_watch.subprocess.CalledProcessError(1, ['gh', *args])
        return scripted(args, cwd)
    monkeypatch.setattr(ci_watch, 'gh', flaky)
    assert len(ci_watch.watch(tmp_path, 'abc', sleep=lambda s: None)) == 1


def test_cancelled_runs_and_commits_without_ci_leave_no_alert(github, tmp_path, monkeypatch):
    snapshots, notified = github
    snapshots += [[run(1, 'completed', 'cancelled')]]
    assert ci_watch.watch(tmp_path, 'abc', sleep=lambda s: None) == [] and ci_watch.open_alerts() == []
    clock = iter(range(0, 10000, 60))
    monkeypatch.setattr(ci_watch.time, 'monotonic', lambda: next(clock))
    snapshots[:] = [[]]
    assert ci_watch.watch(tmp_path, 'docs-only', sleep=lambda s: None) == []


def test_the_stop_hook_holds_only_the_session_that_pushed_once_per_stop(github, tmp_path, capsys):
    assert ci_watch.stop_hook('{"session_id": "pusher"}') == 0 and capsys.readouterr().out == ''  # nothing open
    snapshots, _ = github
    snapshots += [[run(1, 'completed', 'failure')]]
    ci_watch.watch(tmp_path, 'abc', session='pusher', sleep=lambda s: None)
    assert ci_watch.stop_hook('{"session_id": "pusher", "stop_hook_active": false}') == 0
    decision = json.loads(capsys.readouterr().out)
    assert decision['decision'] == 'block' and 'me/os / CI (main)' in decision['reason'] and 'boom' in decision['reason']
    assert ci_watch.stop_hook('{"session_id": "pusher", "stop_hook_active": true}') == 0 and capsys.readouterr().out == ''
    # Another agent on this machine did not push: it is never held.
    assert ci_watch.stop_hook('{"session_id": "bystander"}') == 0 and capsys.readouterr().out == ''
    assert ci_watch.main(['check']) == 1  # the person still sees it


def test_an_alert_from_a_push_outside_claude_code_holds_no_session(github, tmp_path, capsys):
    snapshots, _ = github
    snapshots += [[run(1, 'completed', 'failure')]]
    ci_watch.watch(tmp_path, 'abc', sleep=lambda s: None)
    assert ci_watch.open_alerts() and ci_watch.stop_hook('{"session_id": "any"}') == 0
    assert capsys.readouterr().out == ''


def test_a_nightly_alert_belongs_to_the_session_that_last_pushed_there(github, tmp_path, monkeypatch, capsys):
    snapshots, _ = github
    snapshots += [[{**run(1, 'completed', 'success'), 'event': 'push'}]]
    monkeypatch.setenv('CLAUDE_CODE_SESSION_ID', 'last-pusher')
    assert ci_watch.main(['watch', str(tmp_path), 'abc']) == 0
    snapshots[:] = [[{**run(2, 'completed', 'failure', created='2026-10-02T05:17:00Z'), 'event': 'schedule'}]]
    [alert] = ci_watch.nightly(tmp_path.resolve())
    assert alert['session'] == 'last-pusher'
    capsys.readouterr()
    ci_watch.stop_hook('{"session_id": "last-pusher"}')
    assert json.loads(capsys.readouterr().out)['decision'] == 'block'


def test_each_frontend_shows_an_alert_once(github, tmp_path):
    snapshots, _ = github
    snapshots += [[run(1, 'completed', 'failure')]]
    ci_watch.watch(tmp_path, 'abc', sleep=lambda s: None)
    serve, tui = set(), set()
    [alert] = ci_watch.fresh_alerts(serve)
    assert ci_watch.notice([alert]) == 'CI red: os (CI, "feat: x"). Details: tare ci check'
    assert ci_watch.fresh_alerts(serve) == [] and len(ci_watch.fresh_alerts(tui)) == 1


def test_a_red_nightly_run_needs_a_green_full_run_and_never_notifies_twice(github, tmp_path, monkeypatch):
    snapshots, notified = github
    red = {**run(7, 'completed', 'failure', created='2026-09-30T05:17:00Z'), 'event': 'schedule'}
    snapshots += [[red]]
    [alert] = ci_watch.nightly(tmp_path)
    assert alert['workflow'] == 'CI (full suite)' and notified == [alert]
    assert ci_watch.nightly(tmp_path) == [] and len(notified) == 1  # the scheduled task checks again
    snapshots[:] = [[{**run(8, 'completed', 'success', created='2026-09-30T09:00:00Z'), 'event': 'push'}]]
    ci_watch.watch(tmp_path, 'focused-fix', sleep=lambda s: None)
    assert [a['workflow'] for a in ci_watch.open_alerts()] == ['CI (full suite)']
    snapshots[:] = [[red, {**run(9, 'completed', 'success', created='2026-10-01T05:17:00Z'), 'event': 'schedule'}]]
    assert ci_watch.nightly(tmp_path) == [] and ci_watch.open_alerts() == []


def test_only_the_latest_full_run_counts_so_older_reds_never_flap_or_notify_again(github, tmp_path):
    snapshots, notified = github
    old = {**run(1, 'completed', 'failure', created='2026-09-29T11:00:00Z'), 'event': 'schedule'}
    new = {**run(2, 'completed', 'failure', created='2026-09-30T11:00:00Z'), 'event': 'schedule'}
    snapshots += [[new, old]]  # gh lists newest first
    [alert] = ci_watch.nightly(tmp_path)
    assert alert['url'].endswith('/2') and len(notified) == 1
    assert ci_watch.nightly(tmp_path) == [] and len(notified) == 1  # every watched push checks again
    green = {**run(3, 'completed', 'success', created='2026-10-01T11:00:00Z'), 'event': 'workflow_dispatch'}
    snapshots[:] = [[green, new, old]]
    assert ci_watch.nightly(tmp_path) == [] and ci_watch.open_alerts() == []  # an older red does not reopen


def test_a_watched_push_remembers_its_repository_for_the_nightly_check(github, tmp_path, monkeypatch):
    snapshots, notified = github
    snapshots += [[{**run(1, 'completed', 'success'), 'event': 'push'}]]
    monkeypatch.setattr(ci_watch, 'watch', lambda repo_dir, sha, **_: [])
    assert ci_watch.main(['watch', str(tmp_path), 'abc']) == 0
    assert list(ci_watch.read('repos.json')) == [str(tmp_path.resolve())]
    checked = []
    monkeypatch.setattr(ci_watch, 'nightly', lambda repo_dir: checked.append(repo_dir) or [])
    assert ci_watch.main(['nightly']) == 0 and checked == [tmp_path.resolve()]


def test_a_checkout_removed_while_its_run_is_going_does_not_strand_the_watcher(github, tmp_path, monkeypatch):
    snapshots, _ = github
    snapshots += [[run(1, 'in_progress')], [run(1, 'completed', 'failure')]]
    checkout = tmp_path/'worktree'; checkout.mkdir()
    scripted = ci_watch.gh
    def gone_after_start(args, cwd):
        if args[:2] == ['run', 'list']:
            checkout.exists() and checkout.rmdir()  # deleted right after the push
            assert args[2:4] == ['-R', 'me/os'], args  # no longer needs the folder
        return scripted(args, cwd)
    monkeypatch.setattr(ci_watch, 'gh', gone_after_start)
    assert len(ci_watch.watch(checkout, 'abc', sleep=lambda s: None)) == 1


def test_gh_runs_without_a_working_directory_that_no_longer_exists(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(ci_watch.subprocess, 'run', lambda argv, **kwargs: seen.append(kwargs['cwd']) or
                        ci_watch.subprocess.CompletedProcess(argv, 0, stdout='[]'))
    REAL_GH(['run', 'list'], tmp_path)
    REAL_GH(['run', 'list'], tmp_path/'removed')
    assert seen == [tmp_path, None]


NOREPLY_EMAIL = '21094776+me@users.noreply.github.com'


def core(result):
    assert 'preflight' in result  # `tare ci pr` prints what it read
    return {key: result[key] for key in ('number', 'url', 'state')}


def api_answer(endpoint, protected=True, settings=None, classic=None, rules=None):
    """The scripted `gh api` of a public repository whose main is protected; each argument changes one reading."""
    if endpoint == 'repos/{owner}/{repo}':
        return json.dumps({'private': False, 'visibility': 'public', **(settings or {})})
    if endpoint.endswith('/branches/main/protection'):
        return json.dumps(classic or {})
    if endpoint.endswith('/rules/branches/main'):
        return json.dumps(rules or [])
    return json.dumps({'protected': protected})


def feature_checkout(tmp_path, email):
    """A clone of a bare `main` with a pushed base commit and a `feature` branch carrying one unpublished commit."""
    remote, work = tmp_path/'remote.git', tmp_path/'work'
    subprocess.run(['git', 'init', '-q', '--bare', '-b', 'main', str(remote)], check=True)
    subprocess.run(['git', 'clone', '-q', str(remote), str(work)], check=True, capture_output=True)
    def git(*args):
        return subprocess.run(['git', '-C', str(work), *args], capture_output=True, text=True, check=True).stdout
    git('config', 'user.email', email); git('config', 'user.name', 'T'); git('config', 'core.hooksPath', '/dev/null')
    (work/'a.txt').write_text('1'); git('add', '.'); git('commit', '-qm', 'base'); git('push', '-q', 'origin', 'HEAD:main')
    git('checkout', '-qb', 'feature'); (work/'a.txt').write_text('2'); git('commit', '-qam', 'Change a')
    return remote, work, git


def on_remote(remote, ref='feature'):
    return subprocess.run(['git', '-C', str(remote), 'rev-parse', ref], capture_output=True).returncode == 0


def scripted_github(monkeypatch, answer=api_answer, merge_stderr=None):
    """A `gh` for `tare ci pr`: `answer(endpoint)` serves `gh api`; returns the recorded calls."""
    calls, prs = [], []
    def gh(args, cwd):
        calls.append(args)
        if args[0] == 'api':
            return answer(args[1])
        if args[:2] == ['pr', 'list']:
            return json.dumps(prs)
        if args[:2] == ['pr', 'create']:
            prs.append({'number': 5, 'url': 'https://github.com/o/r/pull/5'})
            return prs[0]['url']
        if args[:2] == ['pr', 'view']:
            return json.dumps({'mergeStateStatus': 'BLOCKED'})
        return ''
    monkeypatch.setattr(ci_watch, 'gh', gh)
    monkeypatch.setattr(ci_watch, 'detached', lambda *args: None)
    return calls


@pytest.mark.parametrize('protection', [True, False, None])
@pytest.mark.parametrize('auto_merge', [True, False])
@pytest.mark.parametrize('visibility', ['ready', 'empty', 'zero'])
def test_a_branch_reaches_main_through_a_pull_request_that_merges_itself(tmp_path, monkeypatch, protection, auto_merge, visibility):
    import subprocess
    remote, work = tmp_path/'remote.git', tmp_path/'work'
    subprocess.run(['git', 'init', '-q', '--bare', '-b', 'main', str(remote)], check=True)
    subprocess.run(['git', 'clone', '-q', str(remote), str(work)], check=True, capture_output=True)
    def git(*args):
        return subprocess.run(['git', '-C', str(work), *args], capture_output=True, text=True, check=True).stdout
    git('config', 'user.email', NOREPLY_EMAIL); git('config', 'user.name', 'T'); git('config', 'core.hooksPath', '/dev/null')
    (work/'a.txt').write_text('1'); git('add', '.'); git('commit', '-qm', 'base'); git('push', '-q', 'origin', 'HEAD:main')
    git('checkout', '-qb', 'feature'); (work/'a.txt').write_text('2'); git('commit', '-qam', 'Change a')
    calls, prs = [], []
    pending = False
    def gh(args, cwd):
        nonlocal pending
        calls.append(args[:2])
        if args[0] == 'api':
            return api_answer(args[1], protected=protection)
        if args[:2] == ['pr', 'list']:
            if pending:
                pending = False
                if visibility == 'empty':return '[]'
                if visibility == 'zero':return json.dumps([{'number': 0, 'url': prs[0]['url']}])
            return json.dumps(prs)
        if args[:2] == ['pr', 'create']:
            prs.append({'number': 5, 'url': 'https://github.com/o/r/pull/5', 'title': args[args.index('--title') + 1]})
            pending = True
            return prs[0]['url'] if visibility != 'ready' else ''
        if args[:2] == ['pr', 'view']:
            if args[2] == 'https://github.com/o/r/pull/5':
                return json.dumps({**prs[0], 'headRefName': 'feature', 'baseRefName': 'main'})
            return json.dumps({'mergeStateStatus': 'BLOCKED'})
        if args[:2] == ['pr', 'merge']:
            assert args[2] == '5'  # never merge a transient zero-number list result
        if args[:2] == ['pr', 'merge'] and not auto_merge:
            raise subprocess.CalledProcessError(1, ['gh', *args],
                stderr='GraphQL: Auto merge is not allowed for this repository (enablePullRequestAutoMerge)')
        return ''
    monkeypatch.setattr(ci_watch, 'gh', gh)
    started = []
    monkeypatch.setattr(ci_watch, 'detached', lambda *args: started.append(args))
    if protection is not True:
        result = ci_watch.pull_request(work)
        assert core(result) == {'number': 5, 'url': 'https://github.com/o/r/pull/5',
            'state': 'UNPROTECTED_AWAITING_INTEGRATION' if protection is False else 'PROTECTION_UNCONFIRMED'}
        assert ['pr', 'merge'] not in calls and not started and prs
        return
    if not auto_merge:
        assert core(ci_watch.pull_request(work)) == {'number': 5, 'url': 'https://github.com/o/r/pull/5',
            'state': 'AUTO_MERGE_DISABLED_AWAITING_INTEGRATION'}
        assert not started and prs
        return
    result = ci_watch.pull_request(work)
    assert core(result) == {'number': 5, 'url': 'https://github.com/o/r/pull/5', 'state': 'BLOCKED'}
    assert result['preflight']['method'] == 'merge' and result['preflight']['protected'] is True
    assert started == [('shepherd', work, 5)]
    assert prs[0]['title'] == 'Change a' and ['pr', 'merge'] in calls
    assert subprocess.run(['git', '-C', str(remote), 'rev-parse', 'feature'], capture_output=True).returncode == 0
    calls.clear()
    assert ci_watch.pull_request(work)['number'] == 5 and ['pr', 'create'] not in calls  # the open one is reused
    git('checkout', '-q', 'main')
    with pytest.raises(ValueError, match='branch'):
        ci_watch.pull_request(work)


@pytest.mark.parametrize('number,url,valid', [(0, 'https://github.com/o/r/pull/5', False),
    (True, 'https://github.com/o/r/pull/1', False), ('5', 'https://github.com/o/r/pull/5', False),
    (5, 'https://github.com/o/r/pull/6', False), (5, 'https://github.com/o/r/pull/5', True)])
def test_auto_merge_requires_an_observed_positive_matching_pr_identity(number, url, valid):
    assert ci_watch.pr_identity({'number': number, 'url': url}) is valid
    assert ci_watch.pr_identity(None) is False


def test_two_pull_requests_that_each_add_a_changelog_entry_both_reach_main(tmp_path, monkeypatch):
    # main requires a branch up to date and a personal account has no merge queue: when one pull request merges the
    # other falls behind, and since both added their CHANGELOG entry on top GitHub's update would conflict. The
    # shepherd merges main in with CHANGELOG.md as a union and pushes; the local branch then absorbs that merge.
    import subprocess
    remote, work = tmp_path/'remote.git', tmp_path/'work'
    subprocess.run(['git', 'init', '-q', '--bare', '-b', 'main', str(remote)], check=True)
    subprocess.run(['git', 'clone', '-q', str(remote), str(work)], check=True, capture_output=True)
    def git(*args):
        return subprocess.run(['git', '-C', str(work), *args], capture_output=True, text=True, check=True).stdout
    git('config', 'user.email', 't@localhost'); git('config', 'user.name', 'T'); git('config', 'core.hooksPath', '/dev/null')
    (work/'CHANGELOG.md').write_text('## Unreleased\n\n- old\n')  # a branch from before .gitattributes said union
    git('add', '.'); git('commit', '-qm', 'base'); git('push', '-q', 'origin', 'HEAD:main')
    for branch, entry in (('one', 'first'), ('two', 'second')):
        git('checkout', '-q', '-b', branch, 'origin/main')
        (work/'CHANGELOG.md').write_text(f'## Unreleased\n\n- {entry}\n- old\n')
        git('commit', '-qam', entry); git('push', '-q', 'origin', branch)
    git('push', '-q', 'origin', 'one:main')  # the first pull request merges
    views = iter([{'mergeStateStatus': 'DIRTY'}, {'mergeStateStatus': 'BLOCKED'}, {'state': 'MERGED'}])
    def gh(args, cwd):
        if args[:2] == ['repo', 'view']:
            return json.dumps({'nameWithOwner': 'me/os'})
        if args[:2] in (['run', 'list'], ['pr', 'list']):
            return '[]'
        return json.dumps({'state': 'OPEN', 'autoMergeRequest': {'mergeMethod': 'MERGE'}, 'headRefName': 'two',
                           'headRefOid': 'h', 'url': 'u', 'title': 't', **next(views)})
    monkeypatch.setattr(ci_watch, 'gh', gh)
    assert ci_watch.shepherd(work, 6, sleep=lambda s: None) == 'MERGED'
    merged = git('show', 'origin/two:CHANGELOG.md')
    assert '- first' in merged and '- second' in merged and '<<<<' not in merged
    (work/'b.txt').write_text('later'); git('add', 'b.txt'); git('commit', '-qm', 'later')  # the session goes on
    ci_watch.absorb(work, 'two')
    git('push', '-q', 'origin', 'two')  # a fast-forward now


def test_the_shepherd_alerts_on_the_first_failure_without_repeating_tests(tmp_path, monkeypatch):
    monkeypatch.setenv('TARE_CI_ALERTS', str(tmp_path/'ci'))
    notified, seen = [], []
    monkeypatch.setattr(ci_watch, 'notify', notified.append)
    heads = iter(['abc', 'abc', 'abc', 'fix', 'fix'])
    rounds = iter([[{'attempt': 1, 'conclusion': 'failure'}], [{'attempt': 2, 'conclusion': 'failure'}],
                   [{'attempt': 2, 'conclusion': 'failure'}], [{'attempt': 1, 'conclusion': 'success'}]])
    runs, calls = [], []
    def gh(args, cwd):
        calls.append(args[:2])
        if args[:2] == ['pr', 'view']:
            head = next(heads, None)
            runs[:] = next(rounds, [])  # one snapshot of the head's runs per round
            return json.dumps({'state': 'OPEN' if head else 'MERGED', 'mergeStateStatus': 'BLOCKED',
                               'autoMergeRequest': {'mergeMethod': 'MERGE'}, 'headRefName': 'feature',
                               'headRefOid': head or 'fix', 'url': 'u', 'title': 'Change a'})
        if args[:2] == ['run', 'list']:
            return json.dumps([{'databaseId': 10 * r['attempt'] + (r['conclusion'] == 'success'), 'status': 'completed',
                                'workflowName': 'tare.tools CI', 'headBranch': 'feature', 'event': 'pull_request',
                                'url': 'run-url', 'displayTitle': 'Change a',
                                'createdAt': '2026-10-06T16:0%d:00Z' % (9 if r['conclusion'] == 'success' else r['attempt']),
                                **r} for r in runs])  # the fixed head's run is the newest
        if args[:2] == ['repo', 'view']:
            return json.dumps({'nameWithOwner': 'me/os'})
        if args[:2] == ['run', 'view']:
            return 'ci (win)\tRun tests\t2026-10-06T16:31:16.0709032Z FAILED tests/test_a.py::one - TypeError: boom\n'
        return ''
    monkeypatch.setattr(ci_watch, 'gh', gh)
    original = ci_watch.update
    def update(change, name='alerts.json'):
        original(change, name)
        seen.append(dict(ci_watch.read()))
    monkeypatch.setattr(ci_watch, 'update', update)
    assert ci_watch.shepherd('.', 7, sleep=lambda s: None) == 'MERGED'
    assert ['run', 'rerun'] not in calls
    # The first failure already opened the alert, before any second attempt.
    first_alert = next(value for value in seen if value)
    assert 'CI failed:' in next(iter(first_alert.values()))['commit']
    # One alert, for the failing head, with its failing tests; the run's own failures raised none (the shepherd's).
    assert [n['errors'] for n in notified] == [['tare.tools CI: run-url',
                                                'ci (win): FAILED tests/test_a.py::one - TypeError: boom']]
    assert any(seen) and not ci_watch.read()  # raised while it failed, cleared once the fixed head passed


def test_pull_requests_behind_main_are_brought_up_to_date_oldest_first(monkeypatch):
    # Every merge leaves every other open pull request behind; updating them all at once restarted every CI run
    # and starved the oldest (six runs cancelled on 2026-10-06). The others wait for the oldest.
    open_prs = [{'number': 3, 'createdAt': '2026-10-06T13:00:00Z', 'autoMergeRequest': {'mergeMethod': 'MERGE'}},
                {'number': 5, 'createdAt': '2026-10-06T15:00:00Z', 'autoMergeRequest': {'mergeMethod': 'MERGE'}},
                {'number': 4, 'createdAt': '2026-10-06T14:00:00Z', 'autoMergeRequest': None}]  # no auto-merge: not in line
    views = iter([{'mergeStateStatus': 'BEHIND'}, {'mergeStateStatus': 'BEHIND'}, {'state': 'MERGED'}])
    updated = []
    def gh(args, cwd):
        if args[:2] == ['pr', 'list']:
            return json.dumps(open_prs)
        if args[:2] == ['run', 'list']:
            return '[]'
        if args[:2] == ['repo', 'view']:
            return json.dumps({'nameWithOwner': 'me/os'})
        return json.dumps({'state': 'OPEN', 'autoMergeRequest': {'mergeMethod': 'MERGE'}, 'headRefName': 'five',
                           'headRefOid': 'h', 'url': 'u', 'title': 't', **next(views)})
    monkeypatch.setattr(ci_watch, 'gh', gh)
    monkeypatch.setattr(ci_watch, 'catch_up', lambda repo_dir, branch: updated.append(branch) or [])
    def sleep(seconds):
        open_prs.pop(0)  # the oldest merges
    assert ci_watch.shepherd('.', 5, sleep=sleep) == 'MERGED'
    assert updated == ['five']  # once #3 is gone, not while it waits


def test_the_shepherd_settles_the_runs_of_the_pushes_it_made(tmp_path, monkeypatch):
    # Its pushes start no watcher: two green runs on 2026-10-06 left their branches' alerts open for hours.
    monkeypatch.setenv('TARE_CI_ALERTS', str(tmp_path/'ci'))
    monkeypatch.setattr(ci_watch, 'notify', lambda alert: None)
    ci_watch.update(lambda alerts: alerts.__setitem__('me/os|tare.tools CI|feature', {
        'repo': 'me/os', 'workflow': 'tare.tools CI', 'branch': 'feature', 'commit': 'c', 'url': 'u',
        'created_at': '2026-10-06T15:00:00Z', 'errors': [], 'session': None}))
    views = iter([{'mergeStateStatus': 'BLOCKED'}, {'state': 'MERGED'}])
    def gh(args, cwd):
        if args[:2] == ['repo', 'view']:
            return json.dumps({'nameWithOwner': 'me/os'})
        if args[:2] == ['run', 'list']:
            return json.dumps([{'databaseId': 1, 'status': 'completed', 'conclusion': 'success', 'event': 'pull_request',
                                'workflowName': 'tare.tools CI', 'headBranch': 'feature', 'url': 'u',
                                'createdAt': '2026-10-06T16:00:00Z', 'displayTitle': 'c'}])
        return json.dumps({'state': 'OPEN', 'autoMergeRequest': {'mergeMethod': 'MERGE'}, 'headRefName': 'feature',
                           'headRefOid': 'h', 'url': 'u', 'title': 't', **next(views)})
    monkeypatch.setattr(ci_watch, 'gh', gh)
    assert ci_watch.shepherd('.', 9, sleep=lambda s: None) == 'MERGED'
    assert not ci_watch.read()  # the green run closed the older alert


def test_a_rerun_that_passes_closes_the_alert_of_its_first_attempt(tmp_path, monkeypatch):
    # A rerun keeps its run's id: settled by id alone, a green second attempt never closed the alert of its first.
    monkeypatch.setenv('TARE_CI_ALERTS', str(tmp_path/'ci'))
    monkeypatch.setattr(ci_watch, 'notify', lambda alert: None)
    current = {'attempt': 1, 'conclusion': 'failure'}
    views = iter([{'mergeStateStatus': 'BLOCKED'}, {'mergeStateStatus': 'CLEAN'}, {'state': 'MERGED'}])
    def gh(args, cwd):
        if args[:2] == ['repo', 'view']:
            return json.dumps({'nameWithOwner': 'me/os'})
        assert args[:2] != ['run', 'rerun']
        if args[:1] == ['run']:
            return '' if args[1] != 'list' else json.dumps([{
                'databaseId': 1, 'status': 'completed', 'event': 'pull_request', 'workflowName': 'tare.tools CI',
                'headBranch': 'feature', 'url': 'u', 'createdAt': '2026-10-06T16:00:00Z', 'displayTitle': 'c', **current}])
        return json.dumps({'state': 'OPEN', 'autoMergeRequest': {'mergeMethod': 'MERGE'}, 'headRefName': 'feature',
                           'headRefOid': 'h', 'url': 'u', 'title': 't', **next(views)})
    monkeypatch.setattr(ci_watch, 'gh', gh)
    # A separate explicit rerun may still pass; observing it must clear the original alert.
    assert ci_watch.shepherd('.', 9, sleep=lambda s: current.update(attempt=2, conclusion='success')) == 'MERGED'
    assert current['attempt'] == 2 and not ci_watch.read()


def test_a_manifest_conflict_is_resolved_by_regenerating_it_from_the_merged_tree(tmp_path, monkeypatch):
    # The disposition manifest holds every relay module's hash: two pull requests that touch code rewrite neighbouring
    # lines and conflict (#42 x #43, #42 x #47 on 2026-10-06). Its content is a function of the files, so the
    # shepherd regenerates it on the merged tree instead of raising an alert, keeping the modules of both sides: taking
    # main's copy alone dropped the module a pull request added (relay/gpu.py, #84, 2026-10-08).
    import hashlib
    import shutil
    import subprocess
    from pathlib import Path as P
    remote, work = tmp_path/'remote.git', tmp_path/'work'
    subprocess.run(['git', 'init', '-q', '--bare', '-b', 'main', str(remote)], check=True)
    subprocess.run(['git', 'clone', '-q', str(remote), str(work)], check=True, capture_output=True)
    def git(*args):
        return subprocess.run(['git', '-C', str(work), *args], capture_output=True, text=True, check=True).stdout
    git('config', 'user.email', 't@localhost'); git('config', 'user.name', 'T'); git('config', 'core.hooksPath', '/dev/null')
    (work/'relay').mkdir(); (work/'scripts').mkdir(); (work/'contracts'/'v1').mkdir(parents=True)
    shutil.copy(P(ci_watch.__file__).resolve().parents[1]/'scripts'/'sync_disposition_manifest.py', work/'scripts')
    def manifest(*extra):
        entry = lambda n: {'name': n, 'path': 'tare.tools.os/relay/' + n, 'summary': n, 'disposition': 'RETAIN_CORE',
                           'target_repository': 'tare.tools.os', 'rationale': 'r',
                           'size_bytes': len((work/'relay'/n).read_bytes()),
                           'sha256': hashlib.sha256((work/'relay'/n).read_bytes()).hexdigest()}
        names = ('a.py', 'b.py', *extra)
        (work/ci_watch.MANIFEST).write_text(json.dumps({'modules': [entry(n) for n in names],
                                                        'total_modules': len(names)}, indent=2) + '\n')
    for n in ('a.py', 'b.py'):
        (work/'relay'/n).write_text('x = 1\n')
    manifest(); git('add', '.'); git('commit', '-qm', 'base'); git('push', '-q', 'origin', 'HEAD:main')
    for branch, name in (('one', 'b.py'), ('two', 'a.py')):
        git('checkout', '-q', '-b', branch, 'origin/main')
        (work/'relay'/name).write_text('x = 2\n')
        if branch == 'two':  # a new module, known only to this branch's manifest
            (work/'relay'/'c.py').write_text('x = 3\n'); git('add', 'relay/c.py')
        manifest(*(['c.py'] if branch == 'two' else []))
        git('commit', '-qam', branch); git('push', '-q', 'origin', branch)
    git('push', '-q', 'origin', 'one:main')  # the first pull request merges; two's manifest now conflicts
    assert ci_watch.catch_up(work, 'two') == []
    git('fetch', '-q', 'origin'); git('checkout', '-q', 'two'); git('reset', '-q', '--hard', 'origin/two')
    assert (work/'relay'/'a.py').read_text() == 'x = 2\n' and (work/'relay'/'b.py').read_text() == 'x = 2\n'
    value = json.loads((work/ci_watch.MANIFEST).read_text())
    lf = lambda n: (work/'relay'/n).read_bytes().replace(b'\r\n', b'\n')  # the script hashes LF, as git stores
    assert all(m['sha256'] == hashlib.sha256(lf(m['name'])).hexdigest() for m in value['modules'])
    assert sorted(m['name'] for m in value['modules']) == ['a.py', 'b.py', 'c.py'] and value['total_modules'] == 3


def test_a_shepherd_waits_as_long_as_its_pull_request_does(tmp_path, monkeypatch):
    # A 6-hour limit stopped every shepherd of a queue overnight on 2026-10-07: it keeps going until the pull
    # request is done, however long the queue takes.
    monkeypatch.setenv('TARE_CI_ALERTS', str(tmp_path/'ci'))
    clock = iter(range(0, 10**9, 5*3600))  # five hours pass between rounds
    monkeypatch.setattr(ci_watch.time, 'monotonic', lambda: next(clock))
    views = iter([{'mergeStateStatus': 'CLEAN'}]*4 + [{'state': 'MERGED'}])
    def gh(args, cwd):
        if args[:2] == ['repo', 'view']:
            return json.dumps({'nameWithOwner': 'me/os'})
        if args[:2] == ['run', 'list']:
            return '[]'
        return json.dumps({'state': 'OPEN', 'autoMergeRequest': {'mergeMethod': 'MERGE'}, 'headRefName': 'feature',
                           'headRefOid': 'h', 'url': 'u', 'title': 't', **next(views)})
    monkeypatch.setattr(ci_watch, 'gh', gh)
    assert ci_watch.shepherd('repo', 9, sleep=lambda s: None) == 'MERGED'


@pytest.mark.parametrize('read_it_first', [True, False])
def test_a_shepherd_failing_round_after_round_is_an_alert_until_it_recovers(tmp_path, monkeypatch, read_it_first):
    # On 2026-10-08 every shepherd of the queue failed its push once a minute for 2 hours, beating all along, and
    # nothing told anyone: revive() only sees a shepherd that stopped beating.
    monkeypatch.setenv('TARE_CI_ALERTS', str(tmp_path/'ci'))
    notified, alerts = [], []
    monkeypatch.setattr(ci_watch, 'notify', notified.append)
    rounds = iter(['CLEAN'] * read_it_first + ['fail'] * ci_watch.STUCK_ROUNDS + ['CLEAN', 'MERGED'])
    def gh(args, cwd):
        if args[:2] == ['repo', 'view']:
            return json.dumps({'nameWithOwner': 'me/os'})
        if args[:2] == ['run', 'list']:
            return '[]'
        now = next(rounds)
        if now == 'fail':
            raise subprocess.CalledProcessError(1, ['gh'], stderr='HTTP 401: Bad credentials\n')
        state = {'state': 'MERGED'} if now == 'MERGED' else {'mergeStateStatus': now}
        return json.dumps({'state': 'OPEN', 'autoMergeRequest': {'mergeMethod': 'MERGE'}, 'headRefName': 'feature',
                           'headRefOid': 'h', 'url': 'u', 'title': 't', **state})
    monkeypatch.setattr(ci_watch, 'gh', gh)
    assert ci_watch.shepherd('repo', 9, sleep=lambda s: alerts.append(ci_watch.read())) == 'MERGED'
    stuck = alerts[read_it_first + ci_watch.STUCK_ROUNDS - 1]
    assert len(notified) == 1 and len(stuck) == 1 and alerts[read_it_first + ci_watch.STUCK_ROUNDS - 2] == {}
    [alert] = stuck.values()
    assert 'shepherd stuck' in alert['commit'] and 'Bad credentials' in alert['errors'][0]
    assert alerts[-1] == {}, 'a shepherd that recovered leaves no alert behind'


def test_a_shepherd_sees_its_pull_request_merge_after_its_worktree_is_gone(tmp_path, monkeypatch):
    # The worktree parked after the merge left `gh pr view` with "no git remotes found": the shepherd never saw
    # MERGED and raised "shepherd stuck". Only the one owner/repo lookup may need the checkout.
    monkeypatch.setenv('TARE_CI_ALERTS', str(tmp_path/'ci'))
    monkeypatch.setattr(ci_watch, 'notify', lambda alert: pytest.fail(f'alerted: {alert}'))
    worktree, states, lookups = tmp_path/'worktree', iter(['OPEN', 'MERGED', 'MERGED']), []
    worktree.mkdir()
    def gh(args, cwd):
        if '-R' not in args and not os.path.isdir(cwd):
            raise subprocess.CalledProcessError(1, ['gh', *args], stderr='no git remotes found\n')
        if args[:2] == ['repo', 'view']:
            if 'isArchived' in args:  # the repository check names it positionally
                return json.dumps({'isArchived': False})
            return lookups.append(cwd) or json.dumps({'nameWithOwner': 'me/os'})
        if args[:2] == ['run', 'list']:
            return '[]'
        return json.dumps({'state': next(states), 'mergeStateStatus': 'CLEAN', 'headRefName': 'feature',
                           'autoMergeRequest': {'mergeMethod': 'MERGE'}, 'headRefOid': 'h', 'url': 'u', 'title': 't'})
    monkeypatch.setattr(ci_watch, 'gh', gh)
    rounds = []
    def park(seconds):
        assert len(rounds) < 3, 'the shepherd kept running after its pull request merged'
        rounds.append(worktree.exists() and worktree.rmdir())  # parked between two rounds
    assert ci_watch.shepherd(worktree, 9, sleep=park) == 'MERGED' and lookups == [worktree]
    # A shepherd revived after the worktree went (its process died) takes owner/repo from its last beat.
    ci_watch.update(lambda beats: beats.__setitem__(f'{worktree}#9', {'repo_dir': str(worktree), 'number': 9,
                                                                     'repo': 'me/os', 'session': None, 'at': 0}),
                    'shepherds.json')
    assert ci_watch.shepherd(worktree, 9, sleep=park) == 'MERGED' and lookups == [worktree]
    assert ci_watch.read('shepherds.json') == {} and ci_watch.read() == {}


@pytest.fixture
def previous_git_writer():
    """Frozen before-control: the pre-guard Git writers, without history lookup.

    Keep the unguarded merge/push behavior here rather than loading a private
    custody commit or deriving a baseline from the current implementation.
    Only the Git transport/executable helpers are shared with the real owner;
    all repositories and pushes are disposable and local to each test.
    """
    import shutil
    import sys
    import tempfile
    from pathlib import Path
    from types import SimpleNamespace

    git_in = ci_watch.git_in
    host_executable = ci_watch.host_executable
    MANIFEST = ci_watch.MANIFEST

    def catch_up(repo_dir, branch):
        git_in(repo_dir, 'fetch', '--quiet', 'origin', 'main', branch)
        tree = Path(tempfile.mkdtemp(prefix='tare-pr-'))
        rules = tree.with_suffix('.attributes')
        rules.write_text('CHANGELOG.md merge=union\n', encoding='utf-8')
        try:
            git_in(repo_dir, 'worktree', 'add', '--quiet', '--detach', str(tree), f'origin/{branch}')
            try:
                git_in(tree, '-c', f'core.attributesFile={rules}', 'merge', '--no-edit', '--quiet', 'origin/main')
            except subprocess.CalledProcessError:
                conflicts = git_in(tree, 'diff', '--name-only', '--diff-filter=U').splitlines() or ['(the merge failed)']
                if conflicts != [MANIFEST]:
                    return conflicts
                ours, theirs = (json.loads(git_in(tree, 'show', f':{stage}:{MANIFEST}')) for stage in (2, 3))
                known = {m['path'] for m in theirs['modules']}
                theirs['modules'] += [m for m in ours['modules'] if m['path'] not in known]
                (tree/MANIFEST).write_text(json.dumps(theirs, indent=2, ensure_ascii=False) + '\n', encoding='utf-8',
                                           newline='\n')
                subprocess.run([sys.executable, str(tree/'scripts'/'sync_disposition_manifest.py'), str(tree)],
                               check=True, capture_output=True, timeout=120)
                git_in(tree, 'add', '--', MANIFEST)
                git_in(tree, '-c', 'core.hooksPath=', 'commit', '--no-edit', '--quiet')
            git_in(tree, '-c', 'core.hooksPath=', 'push', '--quiet', 'origin', f'HEAD:{branch}')
            return []
        finally:
            rules.unlink(missing_ok=True)
            try:
                git_in(repo_dir, 'worktree', 'remove', '--force', str(tree))
            except (OSError, subprocess.SubprocessError):
                shutil.rmtree(tree, ignore_errors=True)
                subprocess.run([host_executable('git', required=True), '-C', str(repo_dir), 'worktree', 'prune'], capture_output=True)

    def absorb(repo_dir, branch):
        try:
            git_in(repo_dir, 'fetch', '--quiet', 'origin', branch)
        except subprocess.CalledProcessError:
            return
        if subprocess.run([host_executable('git', required=True), '-C', str(repo_dir), 'merge-base', '--is-ancestor', f'origin/{branch}', 'HEAD']).returncode:
            try:
                git_in(repo_dir, 'merge', '--no-edit', '--quiet', f'origin/{branch}')
            except subprocess.CalledProcessError:
                subprocess.run([host_executable('git', required=True), '-C', str(repo_dir), 'merge', '--abort'], capture_output=True)
                raise ValueError(f'origin/{branch} has commits that conflict with this branch: merge them first.')

    return SimpleNamespace(catch_up=catch_up, absorb=absorb)


@pytest.mark.parametrize('revision', ['previous', 'corrected'])
@pytest.mark.parametrize('writer', ['catch_up', 'absorb'])
@pytest.mark.parametrize('justified', [False, 'direct', 'dependent'])
def test_real_git_writer_preserves_delivery_context(tmp_path, monkeypatch, previous_git_writer, writer, justified, revision):
    """A one-sided stale graph replacement used to merge cleanly and erase proof.

    Both real writers must reject that loss, not restore an obsolete DONE over
    a justified Work revision. All pushes below target a disposable local bare repo.
    """
    import copy
    from graph_backlog.jsonutil import sha256_canonical
    from test_work_console import graph_root
    from relay import backlog_delivery
    from graph_backlog.validation import validate_work_graph

    owner = previous_git_writer if revision == 'previous' else ci_watch

    root = graph_root.__wrapped__(tmp_path)
    path = root/'work-graph.json'
    records = [{'work_id': 'ready', 'summary': 'Reviewed local qualification',
                'evidence': ['local:qualification'], 'remaining': [],
                'criteria': [{'criterion': 'unit', 'evidence': 'local:qualification'}],
                'evidence_grade': 'C', 'evidence_grade_reason': 'Local test only'}]
    plan = backlog_delivery.preview(path.resolve(), records)
    backlog_delivery.apply(path.resolve(), plan, plan['revision'])
    original = json.loads(path.read_text(encoding='utf-8'))
    original['nodes'][1]['completion'].update(status='PARTIAL', dod_satisfied=False,
                                             residuals=['X2/AC-03 remains unproved'])
    if justified == 'dependent':
        dependent = copy.deepcopy(original['nodes'][0])
        dependent.update(id='dependent', canonical_id='dependent')
        original['nodes'].append(dependent)
        edge = copy.deepcopy(original['edges'][0])
        edge['to'] = 'dependent'
        original['edges'].append(edge)
    path.write_text(json.dumps(original), encoding='utf-8')
    remote = tmp_path/'remote.git'
    ci_watch.git_in(tmp_path, 'init', '-q', '--bare', '-b', 'main', str(remote))
    ci_watch.git_in(root, 'init', '-q', '-b', 'main')
    def git(*args):
        return ci_watch.git_in(root, *args)
    git('config', 'user.email', 't@localhost')
    git('config', 'user.name', 'T')
    git('config', 'core.hooksPath', '')
    git('remote', 'add', 'origin', str(remote))
    git('add', '.'); git('commit', '-qm', 'proven deliveries')
    git('push', '-q', 'origin', 'main')
    git('checkout', '-qb', 'feature')
    (root/'feature.txt').write_text('independent work')
    git('add', '.'); git('commit', '-qm', 'feature')
    git('push', '-q', 'origin', 'feature')
    before_head = git('rev-parse', 'HEAD')
    git('checkout', '-q', 'main')
    if writer == 'absorb':
        git('merge', '--ff-only', 'feature')  # the remote advances from its current tip
    stale = copy.deepcopy(original)
    node = stale['nodes'][0]
    node['completion'] = {'status': 'NOT_DONE', 'dod_satisfied': False}
    if justified:
        receipt_path = tmp_path/'revision.json'
        node['canonical_revision'] = 'revision-real-change'
        node['metrics']['work_change_receipts'] = [str(receipt_path)]
        node['exit_criteria'] = ['changed unit']
        node['notes'].append('Requires new verification after revision of ready: changed criterion')
        revised = [node]
        previous = [original['nodes'][0]]
        if justified == 'dependent':
            dependent = stale['nodes'][2]
            dependent['completion'] = {'status': 'NOT_DONE', 'dod_satisfied': False}
            dependent['canonical_revision'] = node['canonical_revision']
            dependent['metrics']['work_change_receipts'] = [str(receipt_path)]
            dependent['notes'].append('Requires new verification after revision of ready: changed criterion')
            revised.append(dependent)
            previous.append(original['nodes'][2])
            assert dependent['exit_criteria'] == original['nodes'][2]['exit_criteria']
        change = {'schema': 'tare.tools/work-change/1', 'reason': 'changed criterion',
                  'dependency_cone': [item['id'] for item in previous],
                  'before': previous, 'after': copy.deepcopy(revised)}
        receipt_path.write_text(json.dumps({'change': change, 'sha256': sha256_canonical(change)}), encoding='utf-8')
    else:
        node['metrics'] = {}
        node['provenance'] = []
    path.write_text(json.dumps(stale), encoding='utf-8')
    git('commit', '-qam', 'revision' if justified else 'stale replacement')
    git('push', '-q', 'origin', 'main' if writer == 'catch_up' else 'HEAD:feature')
    git('checkout', '-q', 'feature')
    remote_before = git('ls-remote', 'origin', 'refs/heads/feature')
    blocked = not justified and revision == 'corrected'
    if writer == 'catch_up':
        assert owner.catch_up(root, 'feature') == (['work-graph.json'] if blocked else [])
        if blocked:
            assert git('ls-remote', 'origin', 'refs/heads/feature') == remote_before
        else:
            owner.absorb(root, 'feature')
    elif blocked:
        with pytest.raises(ValueError, match='conflict'):
            owner.absorb(root, 'feature')
    else:
        owner.absorb(root, 'feature')
    result = json.loads(path.read_text(encoding='utf-8'))
    assert result == (original if blocked else stale)
    if revision == 'previous' and not justified:
        # Negative control: the previous real writer erased this exact proof,
        # history and DONE state using the same local Git topology.
        assert result['nodes'][0]['completion']['status'] == 'NOT_DONE'
        assert not result['nodes'][0]['metrics']
        assert not result['nodes'][0]['provenance']
    assert result['nodes'][1]['completion']['status'] == 'PARTIAL'
    assert result['nodes'][1]['completion']['residuals'] == ['X2/AC-03 remains unproved']
    assert git('status', '--porcelain') == ''
    assert len(git('worktree', 'list', '--porcelain').split('worktree ')) == 2
    if blocked:
        assert git('rev-parse', 'HEAD') == before_head
    result.pop('revision', None)
    assert validate_work_graph(result)['status'] == 'PASS'


@pytest.mark.parametrize('scenario', ['conflict', 'concurrent_push'])
def test_catch_up_never_overwrites_conflict_or_concurrent_advance(tmp_path, monkeypatch, scenario):
    remote, root = tmp_path/'remote.git', tmp_path/'work'
    root.mkdir()
    real_git = ci_watch.git_in
    real_git(tmp_path, 'init', '-q', '--bare', '-b', 'main', str(remote))
    real_git(root, 'init', '-q', '-b', 'main')
    def git(*args):
        return real_git(root, *args)
    git('config', 'user.email', 't@localhost')
    git('config', 'user.name', 'T')
    git('config', 'core.hooksPath', '')
    git('remote', 'add', 'origin', str(remote))
    (root/'shared.txt').write_text('base\n')
    git('add', '.'); git('commit', '-qm', 'base')
    git('push', '-q', 'origin', 'main')
    git('checkout', '-qb', 'feature')
    (root/'shared.txt' if scenario == 'conflict' else root/'feature.txt').write_text('feature\n')
    git('add', '.'); git('commit', '-qm', 'feature')
    git('push', '-q', 'origin', 'feature')
    feature = git('rev-parse', 'HEAD')
    git('checkout', '-q', 'main')
    (root/'shared.txt' if scenario == 'conflict' else root/'main.txt').write_text('main\n')
    git('add', '.'); git('commit', '-qm', 'main advance')
    git('push', '-q', 'origin', 'main')
    git('checkout', '-q', 'feature')
    concurrent = []
    def advance_before_push(cwd, *args):
        if 'push' in args and scenario == 'concurrent_push':
            commit = git('commit-tree', 'HEAD^{tree}', '-p', feature, '-m', 'concurrent delivery')
            real_git(root, 'push', '-q', 'origin', f'{commit}:feature')
            concurrent.append(commit)
        return real_git(cwd, *args)
    monkeypatch.setattr(ci_watch, 'git_in', advance_before_push)
    if scenario == 'conflict':
        assert ci_watch.catch_up(root, 'feature') == ['shared.txt']
    else:
        with pytest.raises(subprocess.CalledProcessError):
            ci_watch.catch_up(root, 'feature')
        assert len(concurrent) == 1
    assert real_git(remote, 'rev-parse', 'feature') == (concurrent[0] if concurrent else feature)
    assert git('rev-parse', 'HEAD') == feature
    assert git('status', '--porcelain') == ''
    assert len(git('worktree', 'list', '--porcelain').split('worktree ')) == 2


def ci_alert(repo, workflow='CI', branch='main', commit='c'):
    return {'repo': repo, 'workflow': workflow, 'branch': branch, 'commit': commit, 'url': 'u',
            'created_at': '2026-11-04T10:00:00Z', 'errors': [], 'session': None}


def test_a_removed_pull_request_ends_the_shepherd_gone_with_its_alerts_and_beat(tmp_path, monkeypatch):
    # SPEC-OS-SHIP-001 AC-05: nothing merges any more, so nothing is left to fix; a deleted pull request used to
    # fail STUCK_ROUNDS rounds and then alert as "shepherd stuck".
    monkeypatch.setenv('TARE_CI_ALERTS', str(tmp_path/'ci'))
    monkeypatch.setattr(ci_watch, 'notify', lambda alert: pytest.fail(f'alerted: {alert}'))
    worktree = tmp_path/'worktree'
    worktree.mkdir()
    ci_watch.update(lambda beats: beats.__setitem__(f'{worktree}#9', {'repo_dir': str(worktree), 'number': 9,
                                                                     'repo': 'me/os', 'session': None, 'at': 0}),
                    'shepherds.json')
    ci_watch.update(lambda alerts: alerts.update({
        'me/os|pull request|feature': ci_alert('me/os', 'pull request', 'feature', '#9 CI failed: t'),
        'me/os|CI|feature': ci_alert('me/os', 'CI', 'feature')}))  # a push run of the same branch

    state = iter(['OPEN', 'gone'])
    def gh(args, cwd):
        if args[:2] == ['repo', 'view']:
            return json.dumps({'nameWithOwner': 'me/os', 'isArchived': False})
        if args[:2] == ['run', 'list']:
            return '[]'
        if next(state) == 'gone':  # it read the pull request in an earlier round: the branch's alerts are known
            raise subprocess.CalledProcessError(1, ['gh', *args], stderr='HTTP 404: Not Found (https://api.github.com)\n')
        return json.dumps({'state': 'OPEN', 'mergeStateStatus': 'CLEAN', 'autoMergeRequest': {'mergeMethod': 'MERGE'},
                           'headRefName': 'feature', 'headRefOid': 'h', 'url': 'u', 'title': 't'})
    monkeypatch.setattr(ci_watch, 'gh', gh)
    slept = []
    assert ci_watch.shepherd(worktree, 9, sleep=slept.append) == 'GONE'
    assert len(slept) == 1  # the round it was removed in ended it
    assert ci_watch.read('shepherds.json') == {} and ci_watch.read() == {}


def test_a_removed_repository_ends_the_shepherd_gone_with_its_stand_in_alert(tmp_path, monkeypatch):
    monkeypatch.setenv('TARE_CI_ALERTS', str(tmp_path/'ci'))
    monkeypatch.setattr(ci_watch, 'notify', lambda alert: pytest.fail(f'alerted: {alert}'))
    ci_watch.update(lambda alerts: alerts.__setitem__('repo|pull request|#9', ci_alert(
        'repo', 'pull request', '#9', '#9 shepherd stuck: repo')))

    def gh(args, cwd):
        raise subprocess.CalledProcessError(1, ['gh', *args], stderr="GraphQL: Could not resolve to a Repository "
                                                                   "with the name 'me/os'. (repository)\n")
    monkeypatch.setattr(ci_watch, 'gh', gh)
    assert ci_watch.shepherd('repo', 9, sleep=lambda s: pytest.fail('it waited another round')) == 'GONE'
    assert ci_watch.read() == {} and ci_watch.read('shepherds.json') == {}


def test_a_not_found_from_a_broken_login_stays_a_visible_stuck_shepherd(tmp_path, monkeypatch):
    # A private repository answers 404 to a login without access (an expired token): that is no subject gone, and
    # its alerts must not vanish silently. It keeps the "shepherd stuck" path until the login works again.
    monkeypatch.setenv('TARE_CI_ALERTS', str(tmp_path/'ci'))
    monkeypatch.setattr(ci_watch, 'logged_in', lambda: False)
    notified = []
    monkeypatch.setattr(ci_watch, 'notify', notified.append)
    failing = iter(range(ci_watch.STUCK_ROUNDS))
    def gh(args, cwd):
        if next(failing, None) is not None:
            raise subprocess.CalledProcessError(1, ['gh', *args], stderr="GraphQL: Could not resolve to a Repository "
                                                                       "with the name 'me/os'. (repository)\n")
        if args[:2] == ['repo', 'view']:
            return json.dumps({'nameWithOwner': 'me/os', 'isArchived': False})
        if args[:2] == ['run', 'list']:
            return '[]'
        return json.dumps({'state': 'MERGED', 'mergeStateStatus': 'CLEAN', 'autoMergeRequest': None,
                           'headRefName': 'feature', 'headRefOid': 'h', 'url': 'u', 'title': 't'})
    monkeypatch.setattr(ci_watch, 'gh', gh)
    assert ci_watch.shepherd('repo', 9, sleep=lambda s: None) == 'MERGED'
    assert len(notified) == 1 and 'stuck' in json.dumps(notified[0])


def test_an_archived_repository_ends_the_shepherd_gone_in_one_round(tmp_path, monkeypatch):
    # Nothing merges into an archived repository, so its pull request's alerts end with it (AC-05).
    monkeypatch.setenv('TARE_CI_ALERTS', str(tmp_path/'ci'))
    monkeypatch.setattr(ci_watch, 'notify', lambda alert: pytest.fail(f'alerted: {alert}'))
    ci_watch.update(lambda beats: beats.__setitem__('repo#9', {'repo_dir': 'repo', 'number': 9, 'repo': None,
                                                              'session': None, 'at': 0}), 'shepherds.json')
    ci_watch.update(lambda alerts: alerts.update({
        'repo|pull request|#9': ci_alert('repo', 'pull request', '#9', '#9 shepherd stuck: repo'),
        'me/os|pull request|feature': ci_alert('me/os', 'pull request', 'feature', '#9 CI failed: t')}))

    def gh(args, cwd):
        if args[:2] == ['repo', 'view']:
            return json.dumps({'nameWithOwner': 'me/os', 'isArchived': True})
        if args[:2] == ['pr', 'view']:
            return json.dumps({'headRefName': 'feature'})
        raise AssertionError(args)
    monkeypatch.setattr(ci_watch, 'gh', gh)
    assert ci_watch.shepherd('repo', 9, sleep=lambda s: pytest.fail('it waited another round')) == 'GONE'
    assert ci_watch.read('shepherds.json') == {} and ci_watch.read() == {}


def test_an_archived_repository_is_noticed_for_a_known_or_revived_beat(tmp_path, monkeypatch):
    # AC-05: a beat that already records its repository must check it each round; a repository archived after the
    # first round (or before a revived shepherd starts) ends GONE like an unknown one does.
    monkeypatch.setenv('TARE_CI_ALERTS', str(tmp_path/'ci'))
    monkeypatch.setattr(ci_watch, 'notify', lambda alert: pytest.fail(f'alerted: {alert}'))
    worktree = tmp_path/'worktree'
    worktree.mkdir()
    ci_watch.update(lambda beats: beats.__setitem__(f'{worktree}#9', {
        'repo_dir': str(worktree), 'number': 9, 'repo': 'me/os', 'session': None, 'at': 0}), 'shepherds.json')
    ci_watch.update(lambda alerts: alerts.__setitem__(
        'me/os|pull request|feature', ci_alert('me/os', 'pull request', 'feature', '#9 CI failed: t')))

    def gh(args, cwd):
        if args[:2] == ['repo', 'view']:
            return json.dumps({'isArchived': True})
        if args[:2] == ['pr', 'view']:
            return json.dumps({'headRefName': 'feature'})
        raise AssertionError(args)
    monkeypatch.setattr(ci_watch, 'gh', gh)
    assert ci_watch.shepherd(worktree, 9, sleep=lambda s: pytest.fail('it waited another round')) == 'GONE'
    assert ci_watch.read('shepherds.json') == {} and ci_watch.read() == {}


def test_the_repository_check_names_the_repository_positionally(tmp_path, monkeypatch):
    # `gh repo view` takes the repository as a positional argument; `-R` is not one of its flags, so a check that
    # named it that way failed every round and no archived repository was ever noticed (AC-05).
    monkeypatch.setenv('TARE_CI_ALERTS', str(tmp_path/'ci'))
    calls = []

    def gh(args, cwd):
        calls.append((args, cwd))
        return json.dumps({'isArchived': False})
    monkeypatch.setattr(ci_watch, 'gh', gh)
    assert ci_watch.repository_view('me/os') == {'isArchived': False}
    assert calls == [(['repo', 'view', 'me/os', '--json', 'isArchived'], '.')]


def test_a_pull_request_removed_before_its_first_read_cleans_the_persisted_branch_alert(tmp_path, monkeypatch):
    # The first gh call after a restart may already answer not-found: the branch identity comes from the alert an
    # earlier shepherd persisted, so `me/os|pull request|feature` and the push alert of that branch go with the
    # stand-in (AC-05).
    monkeypatch.setenv('TARE_CI_ALERTS', str(tmp_path/'ci'))
    monkeypatch.setattr(ci_watch, 'notify', lambda alert: pytest.fail(f'alerted: {alert}'))
    worktree = tmp_path/'worktree'
    worktree.mkdir()
    ci_watch.update(lambda beats: beats.__setitem__(f'{worktree}#9', {
        'repo_dir': str(worktree), 'number': 9, 'repo': 'me/os', 'session': None, 'at': 0}), 'shepherds.json')
    ci_watch.update(lambda alerts: alerts.update({
        'me/os|pull request|feature': ci_alert('me/os', 'pull request', 'feature', '#9 CI failed: t'),
        'me/os|CI|feature': ci_alert('me/os', 'CI', 'feature')}))  # a push run of the same branch

    def gh(args, cwd):
        if args[:2] == ['repo', 'view']:
            return json.dumps({'isArchived': False})
        raise subprocess.CalledProcessError(1, ['gh', *args],
            stderr='GraphQL: Could not resolve to a PullRequest with the number of 9.\n')
    monkeypatch.setattr(ci_watch, 'gh', gh)
    assert ci_watch.shepherd(worktree, 9, sleep=lambda s: pytest.fail('it waited another round')) == 'GONE'
    assert ci_watch.read('shepherds.json') == {} and ci_watch.read() == {}


def test_a_transient_github_failure_is_not_taken_for_a_gone_subject(tmp_path, monkeypatch):
    monkeypatch.setenv('TARE_CI_ALERTS', str(tmp_path/'ci'))
    monkeypatch.setattr(ci_watch, 'notify', lambda alert: None)
    rounds = iter(['fail'] * 3 + ['MERGED'])

    def gh(args, cwd):
        if args[:2] == ['repo', 'view']:
            return json.dumps({'nameWithOwner': 'me/os'})
        if args[:2] == ['run', 'list']:
            return '[]'
        now = next(rounds)
        if now == 'fail':
            raise subprocess.CalledProcessError(1, ['gh'], stderr='HTTP 401: Bad credentials\n')
        return json.dumps({'state': 'MERGED', 'autoMergeRequest': {'mergeMethod': 'MERGE'}, 'headRefName': 'feature',
                           'headRefOid': 'h', 'url': 'u', 'title': 't'})
    monkeypatch.setattr(ci_watch, 'gh', gh)
    assert ci_watch.shepherd('repo', 9, sleep=lambda s: None) == 'MERGED'
    assert ci_watch.read('shepherds.json') == {} and ci_watch.read() == {}


def test_revive_retires_a_beat_with_neither_checkout_nor_repository(tmp_path, monkeypatch):
    # ADR-072: a beat whose worktree is gone and that never recorded its repository has nothing left to follow.
    monkeypatch.setenv('TARE_CI_ALERTS', str(tmp_path/'ci'))
    started = []
    monkeypatch.setattr(ci_watch, 'detached', lambda *args, session=None: started.append((*args, session)))
    removed, kept = str(tmp_path/'removed'), str(tmp_path/'kept')
    ci_watch.update(lambda beats: beats.update({
        f'{removed}#1': {'repo_dir': removed, 'number': 1, 'repo': None, 'session': None, 'at': 1000.0},
        f'{kept}#2': {'repo_dir': kept, 'number': 2, 'repo': 'me/os', 'session': 's', 'at': 1000.0}}),
        'shepherds.json')
    ci_watch.update(lambda alerts: alerts.update({
        f'{removed}|pull request|#1': ci_alert(removed, 'pull request', '#1', '#1 shepherd stuck: removed'),
        'me/os|CI|main': ci_alert('me/os')}))
    now = 1001.0 + ci_watch.SHEPHERD_SILENCE
    assert ci_watch.revive(now) == [2] and started == [('shepherd', kept, 2, 's')]
    assert list(ci_watch.read('shepherds.json')) == [f'{kept}#2']
    assert list(ci_watch.read()) == ['me/os|CI|main']  # the orphan's stand-in alert went with its beat


def test_a_failure_after_the_subject_was_read_does_not_end_the_shepherd_gone(tmp_path, monkeypatch):
    # Only the queries about the pull request and its repository may end it as GONE. The run listing failing with
    # HTTP 404 says nothing about the subject: the pull request stays monitored, with its alert and beat.
    monkeypatch.setenv('TARE_CI_ALERTS', str(tmp_path/'ci'))
    monkeypatch.setattr(ci_watch, 'notify', lambda alert: pytest.fail(f'alerted: {alert}'))
    worktree = tmp_path/'worktree'
    worktree.mkdir()
    ci_watch.update(lambda beats: beats.__setitem__(f'{worktree}#9', {'repo_dir': str(worktree), 'number': 9,
                                                                     'repo': 'me/os', 'session': None, 'at': 0}),
                    'shepherds.json')
    ci_watch.update(lambda alerts: alerts.__setitem__(
        'me/os|pull request|feature', ci_alert('me/os', 'pull request', 'feature', '#9 CI failed: t')))
    steps = iter(['OPEN', 'runs-gone', 'OPEN', '[]', 'MERGED', '[]'])

    def gh(args, cwd):
        if args[:2] == ['repo', 'view']:
            return json.dumps({'nameWithOwner': 'me/os', 'isArchived': False})
        step = next(steps)
        if step == 'runs-gone':  # the pull request was read: only listing its runs failed
            raise subprocess.CalledProcessError(1, ['gh', *args],
                                               stderr='HTTP 404: Not Found (https://api.github.com)\n')
        if args[:2] == ['run', 'list']:
            return step
        return json.dumps({'state': step, 'mergeStateStatus': 'CLEAN', 'autoMergeRequest': {'mergeMethod': 'MERGE'},
                           'headRefName': 'feature', 'headRefOid': 'h', 'url': 'u', 'title': 't'})
    monkeypatch.setattr(ci_watch, 'gh', gh)
    seen = []
    assert ci_watch.shepherd(worktree, 9, sleep=lambda s: seen.append(
        ('me/os|pull request|feature' in ci_watch.read(), f'{worktree}#9' in ci_watch.read('shepherds.json')))) == 'MERGED'
    assert seen[0] == (True, True)  # the round that failed kept monitoring: not retired as GONE
    assert ci_watch.read() == {} and ci_watch.read('shepherds.json') == {}


def test_revive_retires_an_orphan_whose_relative_checkout_is_gone(tmp_path, monkeypatch):
    # The repository is unknown and the checkout is not there: a relative `repo_dir` proves nothing either.
    monkeypatch.setenv('TARE_CI_ALERTS', str(tmp_path/'ci'))
    monkeypatch.chdir(tmp_path)
    started = []
    monkeypatch.setattr(ci_watch, 'detached', lambda *args, session=None: started.append((*args, session)))
    ci_watch.update(lambda beats: beats.__setitem__(
        'gone#1', {'repo_dir': 'gone', 'number': 1, 'session': None, 'at': 1000.0}), 'shepherds.json')
    ci_watch.update(lambda alerts: alerts.__setitem__(
        'gone|pull request|#1', ci_alert('gone', 'pull request', '#1', '#1 shepherd stuck: gone')))
    assert ci_watch.revive(1001.0 + ci_watch.SHEPHERD_SILENCE) == [] and started == []
    assert ci_watch.read('shepherds.json') == {} and ci_watch.read() == {}


def test_tare_ci_dismiss_drops_one_alert_and_refuses_an_unknown_key(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('TARE_CI_ALERTS', str(tmp_path/'ci'))
    monkeypatch.setattr(ci_watch, 'detached', lambda *args, session=None: pytest.fail('dismiss started a shepherd'))
    ci_watch.update(lambda alerts: alerts.update({'me/os|CI|main': ci_alert('me/os'),
                                                  'me/other|CI|main': ci_alert('me/other')}))
    assert ci_watch.main(['dismiss', 'me/os|CI|main']) == 0
    assert list(ci_watch.read()) == ['me/other|CI|main'] and 'me/os / CI (main)' in capsys.readouterr().out
    assert ci_watch.main(['dismiss', 'gone/os|CI|main']) == 1
    assert 'tare ci dismiss: no alert gone/os|CI|main' in capsys.readouterr().err
    assert list(ci_watch.read()) == ['me/other|CI|main']


def test_tare_ci_check_prune_removes_the_alerts_whose_subject_is_gone(tmp_path, monkeypatch, capsys):
    # AC-06: the subjects gh proves gone go, with their shepherd beats; the one gh could not resolve stays.
    monkeypatch.setenv('TARE_CI_ALERTS', str(tmp_path/'ci'))
    monkeypatch.setattr(ci_watch, 'detached', lambda *args, session=None: pytest.fail('check started a shepherd'))
    worktree = tmp_path/'worktree'
    worktree.mkdir()
    ci_watch.update(lambda alerts: alerts.update({
        'me/os|pull request|feature': ci_alert('me/os', 'pull request', 'feature', '#9 CI failed: t'),
        'gone/os|CI|main': ci_alert('gone/os'),
        'old/os|CI|main': ci_alert('old/os'),
        'busy/os|CI|main': ci_alert('busy/os'),
        'open/os|pull request|main': ci_alert('open/os', 'pull request', 'main', '#12 CI failed: t')}))
    ci_watch.update(lambda beats: beats.update({
        f'{worktree}#9': {'repo_dir': str(worktree), 'number': 9, 'repo': 'me/os', 'session': None,
                          'at': ci_watch.time.time()},
        'old/os#7': {'repo_dir': 'old', 'number': 7, 'repo': 'old/os', 'session': None,
                     'at': ci_watch.time.time()}}), 'shepherds.json')

    def gh(args, cwd):
        if args[:2] == ['repo', 'view']:
            assert args[3:] == ['--json', 'isArchived'], args  # `gh repo view` takes the repository positionally
            repo = args[2]
            if repo == 'gone/os':
                raise subprocess.CalledProcessError(1, ['gh', *args],
                                                    stderr='HTTP 404: Not Found (https://api.github.com)\n')
            if repo == 'busy/os':
                raise subprocess.CalledProcessError(1, ['gh', *args], stderr='HTTP 500: Internal Server Error\n')
            return json.dumps({'isArchived': repo == 'old/os'})
        if args[:2] == ['pr', 'view']:
            return json.dumps({'state': 'MERGED' if args[2] == '9' else 'OPEN'})
        raise AssertionError(args)
    monkeypatch.setattr(ci_watch, 'gh', gh)
    assert ci_watch.main(['check']) == 1 and len(ci_watch.read()) == 5  # without --prune nothing goes
    capsys.readouterr()
    assert ci_watch.main(['check', '--prune']) == 1 and 'pruned 3' in capsys.readouterr().out
    assert sorted(ci_watch.read()) == ['busy/os|CI|main', 'open/os|pull request|main']
    assert ci_watch.read('shepherds.json') == {}
    capsys.readouterr()
    assert ci_watch.main(['check', '--prune']) == 1 and 'pruned 0' in capsys.readouterr().out


def test_tare_ci_check_prune_takes_the_branch_alerts_with_their_pull_request(tmp_path, monkeypatch, capsys):
    # AC-06: a pull request gh proves gone takes the push alerts of its branch with it. The alert that named the
    # branch goes too, so nothing would ever find them again: both are pruned and both are counted.
    monkeypatch.setenv('TARE_CI_ALERTS', str(tmp_path/'ci'))
    monkeypatch.setattr(ci_watch, 'detached', lambda *args, session=None: pytest.fail('check started a shepherd'))
    worktree = tmp_path/'worktree'
    worktree.mkdir()
    ci_watch.update(lambda alerts: alerts.update({
        'me/os|pull request|feature': ci_alert('me/os', 'pull request', 'feature', '#9 CI failed: t'),
        'me/os|CI|feature': ci_alert('me/os', 'CI', 'feature'),  # a push run of the same branch
        'me/os|CI|main': ci_alert('me/os', 'CI', 'main')}))  # another branch's alert stays
    ci_watch.update(lambda beats: beats.__setitem__(f'{worktree}#9', {
        'repo_dir': str(worktree), 'number': 9, 'repo': 'me/os', 'session': None, 'at': ci_watch.time.time()}),
        'shepherds.json')

    def gh(args, cwd):
        if args[:2] == ['repo', 'view']:
            return json.dumps({'isArchived': False})
        if args[:2] == ['pr', 'view']:
            raise subprocess.CalledProcessError(
                1, ['gh', *args],
                stderr='GraphQL: Could not resolve to a PullRequest with the number of 9.\n')
        raise AssertionError(args)
    monkeypatch.setattr(ci_watch, 'gh', gh)
    assert ci_watch.main(['check', '--prune']) == 1 and 'pruned 2' in capsys.readouterr().out
    assert list(ci_watch.read()) == ['me/os|CI|main']
    assert ci_watch.read('shepherds.json') == {}


def test_a_restart_remembers_the_head_branch_so_the_gone_round_cleans_its_push_alert(tmp_path, monkeypatch):
    # An earlier shepherd read the pull request and keeps its head branch in the beat before it waits. A restart
    # whose very next `gh pr view` already answers not-found has no view and no pull request alert left to name the
    # branch, so without it the push alert of that branch would outlive its pull request (AC-05).
    monkeypatch.setenv('TARE_CI_ALERTS', str(tmp_path/'ci'))
    monkeypatch.setattr(ci_watch, 'notify', lambda alert: pytest.fail(f'alerted: {alert}'))
    worktree = tmp_path/'worktree'
    worktree.mkdir()
    name = f'{worktree}#9'
    ci_watch.update(lambda beats: beats.__setitem__(name, {
        'repo_dir': str(worktree), 'number': 9, 'repo': 'me/os', 'session': None, 'at': 0}), 'shepherds.json')
    ci_watch.update(lambda alerts: alerts.__setitem__('me/os|CI|feature', ci_alert('me/os', 'CI', 'feature')))

    def first(args, cwd):  # the shepherd that read the pull request, before the machine stopped
        if args[:2] == ['repo', 'view']:
            return json.dumps({'isArchived': False})
        if args[:2] == ['run', 'list']:
            return '[]'
        return json.dumps({'state': 'OPEN', 'mergeStateStatus': 'CLEAN', 'autoMergeRequest': {'mergeMethod': 'MERGE'},
                           'headRefName': 'feature', 'headRefOid': 'h', 'url': 'u', 'title': 't'})
    monkeypatch.setattr(ci_watch, 'gh', first)

    class Stopped(Exception):
        pass

    def stopped(_seconds):  # the process died while it waited for its next round
        raise Stopped
    with pytest.raises(Stopped):
        ci_watch.shepherd(worktree, 9, sleep=stopped)
    assert ci_watch.read('shepherds.json')[name]['branch'] == 'feature'  # persisted before it waited

    def restarted(args, cwd):  # the first query about the pull request after the restart answers not-found
        if args[:2] == ['repo', 'view']:
            return json.dumps({'isArchived': False})
        raise subprocess.CalledProcessError(1, ['gh', *args],
                                            stderr='HTTP 404: Not Found (https://api.github.com)\n')
    monkeypatch.setattr(ci_watch, 'gh', restarted)
    assert ci_watch.shepherd(worktree, 9, sleep=lambda s: pytest.fail('it waited another round')) == 'GONE'
    assert ci_watch.read('shepherds.json') == {} and ci_watch.read() == {}


def test_a_personal_email_in_a_push_to_a_public_repository_is_refused_before_anything_is_pushed(tmp_path, monkeypatch):
    remote, work, git = feature_checkout(tmp_path, 't@example.org')
    calls = scripted_github(monkeypatch)
    sha = git('rev-parse', 'HEAD').strip()
    with pytest.raises(ValueError, match='Preflight refused') as refused:
        ci_watch.pull_request(work)
    assert sha[:8] in str(refused.value) and 'author email t@example.org' in str(refused.value)
    assert not on_remote(remote) and ['pr', 'create'] not in [call[:2] for call in calls]


def test_a_refused_identity_reads_well_on_the_command_line(tmp_path, monkeypatch, capsys):
    remote, work, git = feature_checkout(tmp_path, 't@example.org')
    scripted_github(monkeypatch)
    assert ci_watch.main(['pr', '--repo-dir', str(work)]) == 1
    error = capsys.readouterr().err
    assert error.startswith('tare ci pr: Preflight refused: commit ') and error.endswith('yet).\n') and '..' not in error
    assert 'git config user.email' in error and not on_remote(remote)


def test_a_personal_email_is_pushed_to_a_private_repository(tmp_path, monkeypatch):
    remote, work, git = feature_checkout(tmp_path, 't@example.org')
    scripted_github(monkeypatch, lambda endpoint: api_answer(endpoint, settings={'private': True, 'visibility': 'private'}))
    assert ci_watch.pull_request(work)['number'] == 5
    assert on_remote(remote)


def test_a_repository_whose_visibility_is_not_shown_counts_as_public(tmp_path, monkeypatch):
    remote, work, git = feature_checkout(tmp_path, 't@example.org')
    scripted_github(monkeypatch, lambda endpoint: json.dumps({}) if endpoint == 'repos/{owner}/{repo}' else api_answer(endpoint))
    with pytest.raises(ValueError, match='Preflight refused'):
        ci_watch.pull_request(work)
    assert not on_remote(remote)


def test_nothing_is_pushed_when_github_cannot_be_read(tmp_path, monkeypatch):
    remote, work, git = feature_checkout(tmp_path, NOREPLY_EMAIL)
    def unreadable(endpoint):
        raise subprocess.CalledProcessError(1, ['gh', 'api', endpoint], stderr='HTTP 502')
    calls = scripted_github(monkeypatch, unreadable)
    with pytest.raises(subprocess.CalledProcessError):
        ci_watch.pull_request(work)
    assert not on_remote(remote) and [call[0] for call in calls] == ['api']


def test_protection_that_cannot_be_read_is_unknown_and_no_rule_is_assumed(tmp_path, monkeypatch):
    remote, work, git = feature_checkout(tmp_path, NOREPLY_EMAIL)
    def partly(endpoint):
        if endpoint == 'repos/{owner}/{repo}':
            return api_answer(endpoint)
        raise subprocess.CalledProcessError(1, ['gh', 'api', endpoint], stderr='HTTP 403')
    scripted_github(monkeypatch, partly)
    result = ci_watch.pull_request(work)
    assert result['state'] == 'PROTECTION_UNCONFIRMED' and result['preflight']['method'] == 'merge'
    assert on_remote(remote)


MERGE_ONLY = {'type': 'pull_request', 'parameters': {'allowed_merge_methods': ['merge']}}
SQUASH_AND_REBASE = {'type': 'pull_request', 'parameters': {'allowed_merge_methods': ['squash', 'rebase']}}


@pytest.mark.parametrize('settings, classic, rules, method', [
    ({}, {}, [], 'merge'),  # the defaults
    ({'allow_merge_commit': True, 'allow_squash_merge': True, 'allow_rebase_merge': True}, {}, [], 'merge'),
    ({'allow_merge_commit': False}, {}, [], 'squash'),
    ({}, {'required_linear_history': {'enabled': True}}, [], 'squash'),  # classic protection
    ({}, {}, [{'type': 'required_linear_history'}], 'squash'),  # ruleset
    ({}, {'required_linear_history': {'enabled': False}}, [], 'merge'),
    ({}, {}, [SQUASH_AND_REBASE], 'squash'),
    ({}, {}, [MERGE_ONLY], 'merge'),
    ({'allow_merge_commit': None}, {}, [], 'merge'),  # absent or null: unknown, which is allowed
])
def test_the_merge_method_is_a_merge_commit_unless_the_repository_rules_it_out(settings, classic, rules, method):
    plan = ci_watch.merge_plan(settings, {}, classic, rules)
    assert plan['method'] == method


@pytest.mark.parametrize('settings, classic, rules, reason', [
    ({'allow_merge_commit': False, 'allow_squash_merge': False}, {}, [], 'allow_merge_commit is off'),
    ({'allow_squash_merge': False}, {'required_linear_history': {'enabled': True}}, [], 'linear history is required'),
    ({}, {}, [{'type': 'required_linear_history'}, {'type': 'pull_request',
                                                    'parameters': {'allowed_merge_methods': ['rebase']}}],
     'a ruleset allows only rebase'),
])
def test_a_repository_that_leaves_only_rebase_refuses_with_the_reason(settings, classic, rules, reason):
    with pytest.raises(ValueError, match='^Preflight refused: no merge method is left') as refused:
        ci_watch.merge_plan(settings, {}, classic, rules)
    assert reason in str(refused.value) and 'rebase is never used' in str(refused.value)


def test_the_plan_collects_checks_reviews_and_signing_from_every_source():
    branch = {'protected': True, 'protection': {'required_status_checks': {'contexts': ['changelog', 'lint']}}}
    classic = {'required_status_checks': {'strict': True, 'contexts': ['changelog', 'Tests (linux)']},
               'required_pull_request_reviews': {'required_approving_review_count': 1},
               'required_signatures': {'enabled': False}, 'required_linear_history': {'enabled': False}}
    rules = [{'type': 'required_status_checks', 'parameters': {'required_status_checks': [{'context': 'ci'}]}},
             {'type': 'pull_request', 'parameters': {'required_approving_review_count': 2,
                                                     'allowed_merge_methods': ['merge', 'squash', 'rebase']}},
             {'type': 'required_signatures'}]
    plan = ci_watch.merge_plan({'visibility': 'public', 'allow_auto_merge': True}, branch, classic, rules)
    assert plan == {'method': 'merge', 'auto_merge': True, 'protected': True, 'visibility': 'public',
                    'required_checks': ['Tests (linux)', 'changelog', 'ci', 'lint'], 'required_reviews': 2,
                    'signed_commits': True, 'linear_history': False}
    bare = ci_watch.merge_plan({}, {}, {}, [])
    assert bare['required_checks'] == [] and bare['required_reviews'] == 0 and bare['auto_merge'] is None
    assert not bare['signed_commits'] and not bare['linear_history'] and bare['protected'] is None


def test_the_method_the_plan_chose_is_the_one_auto_merge_is_turned_on_with(tmp_path, monkeypatch):
    remote, work, git = feature_checkout(tmp_path, NOREPLY_EMAIL)
    calls = scripted_github(monkeypatch, lambda endpoint: api_answer(endpoint, settings={'allow_merge_commit': False}))
    assert ci_watch.pull_request(work)['preflight']['method'] == 'squash'
    assert ['pr', 'merge', '5', '--auto', '--squash'] in calls and ['pr', 'merge', '5', '--auto', '--merge'] not in calls


def test_a_repository_that_turned_auto_merge_off_is_not_asked_to_merge(tmp_path, monkeypatch):
    remote, work, git = feature_checkout(tmp_path, NOREPLY_EMAIL)
    calls = scripted_github(monkeypatch, lambda endpoint: api_answer(endpoint, settings={'allow_auto_merge': False}))
    assert core(ci_watch.pull_request(work))['state'] == 'AUTO_MERGE_DISABLED_AWAITING_INTEGRATION'
    assert 'merge' not in [call[1] for call in calls if call[0] == 'pr'] and on_remote(remote)


def test_an_unsigned_commit_is_refused_where_signed_commits_are_required(tmp_path, monkeypatch):
    from relay import push_preflight
    remote, work, git = feature_checkout(tmp_path, NOREPLY_EMAIL)  # git here cannot sign: every commit is unsigned
    scripted_github(monkeypatch, lambda endpoint: api_answer(endpoint, classic={'required_signatures': {'enabled': True}}))
    sha = git('rev-parse', 'HEAD').strip()
    with pytest.raises(ValueError, match='Preflight refused') as refused:
        ci_watch.pull_request(work)
    assert f'commit {sha[:8]} is not signed' in str(refused.value) and not on_remote(remote)


def test_an_unsigned_commit_is_pushed_where_signing_is_not_required(tmp_path, monkeypatch):
    remote, work, git = feature_checkout(tmp_path, NOREPLY_EMAIL)
    scripted_github(monkeypatch)
    assert ci_watch.pull_request(work)['number'] == 5 and on_remote(remote)
