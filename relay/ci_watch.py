"""Red CI reaches the agents without anyone watching.

`tare ci watch <repo-dir> <sha>`: started detached by the repositories' pre-push hook. It waits for the
commit's GitHub workflow runs; a failed run becomes an open alert (failing jobs and error lines), shown as
a Windows notification. A later green run of the same workflow and branch resolves it.
`tare ci nightly [repo-dir...]`: the same for the latest scheduled (full-suite) runs of the given repositories,
or of every repository a push was watched in. Pushes only prove the tests that ran the changed code, so a red
nightly run stays open until a later full run is green. Each push also checks its repository's nightly; a
Windows scheduled task (Tare-CI-Nightly, like the other Tare-* tasks) covers the days without pushes:
  Register-ScheduledTask Tare-CI-Nightly -Action (New-ScheduledTaskAction conhost.exe ('--headless cmd /c "' +
  "$env:LOCALAPPDATA/tare.tools/product/tare.cmd" + '" ci nightly')) -Trigger (New-ScheduledTaskTrigger -Daily
  -At 10am), (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME) -Settings (New-ScheduledTaskSettingsSet
  -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 10))
`tare ci check [--prune]`: prints the open alerts, exit 1 when any (tare frontends read `open_alerts()`);
`--prune` first removes the alerts whose subject is gone -- its pull request merged or closed, its repository
removed or archived -- with the shepherd beats they left, and keeps the ones gh could not resolve.
`tare ci dismiss KEY`: drops one alert, given the key `tare ci check` prints, once the operator decided not to fix it.
`tare ci pr [--body TEXT]`: main is protected and takes changes only through pull requests that merge themselves
(a merge commit) once CI is green: this pushes the current branch, opens its pull request (or reuses the open one),
turns on auto-merge and brings the branch up to date when main moved. Before anything is pushed it reads
GitHub (repository settings, branch protection, rulesets) and refuses, with the reason, a push to a public
repository whose unpublished commits carry an author or committer email that is not a GitHub noreply address,
unsigned commits where signing is required, a repository whose settings leave no merge method and, when the branch's
commits touch the Windows sandbox (SANDBOX_PATHS), a failed `scripts/ops/sandbox_probe.py` on this machine; otherwise the
method is chosen from those settings (merge commit, else squash, never rebase). CI on main then skips a merge whose
bytes already passed in the pull request (scripts/ci_suite.py). It leaves `tare ci shepherd <repo-dir> <number>` running
detached: main requires a branch up to date with it and a personal account has no merge queue, so a pull request
falls behind whenever another one merges; the shepherd merges main into it (CI runs again) until it merges or
closes. Every pull request adds its CHANGELOG entry on top, so it merges here, where CHANGELOG.md is a union
(.gitattributes), not with GitHub's update, which would conflict. Any other conflict becomes an alert. A failed CI
run on the pull request becomes an alert immediately; tests are never automatically retried to obtain green.
An alert lasts while its problem does: the shepherd clears it once the pull request merges again
cleanly, passes, merges or closes, and keeps following the pull request meanwhile. A pull request or its repository
removed or archived ends the shepherd with `GONE` in that round, taking its beat and that pull request's alerts
with it: nothing is left to fix. Pull requests behind main are
brought up to date one at a time, oldest first (a merge queue by hand): every merge leaves every other open pull
request behind, and updating them all at once restarted every CI run and starved the oldest (six runs cancelled in
three hours on 2026-10-06). The others keep their own green run and wait; a stuck pull request holds the queue
until its auto-merge is turned off. The shepherd's own pushes start no watcher (they come from a throwaway tree,
hooks off), so it settles the head's completed runs itself: a green run closes the branch's CI alert.
`tare ci stop-hook`: Claude Code Stop hook (`tare.cmd ci stop-hook || true`). The Claude Code session that
pushed (CLAUDE_CODE_SESSION_ID, which the detached watcher inherits through the pre-push hook) cannot end its
turn while its alert is open; a nightly alert belongs to the session that last pushed to that repository.
Other sessions, and alerts of pushes made outside Claude Code, never block. It is told once per stop
(`stop_hook_active` lets the next stop through, so a failure it cannot fix never traps it).
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
from compute_plane.managed_process import host_executable
import sys
import time

FAILED = {'failure', 'timed_out', 'startup_failure'}
FULL_EVENTS = {'schedule', 'workflow_dispatch'}  # runs that prove the whole suite
ERROR_LINE = re.compile(r'FAILED |ERROR |Error:|error:|^E  |AssertionError|Traceback|[0-9]+ failed')


def store():
    """The machine's alert folder (alerts.json and its writer lock)."""
    if os.environ.get('TARE_CI_ALERTS'):
        return Path(os.environ['TARE_CI_ALERTS'])
    base = os.environ.get('LOCALAPPDATA') or Path.home()/'.local/state'
    return Path(base)/'tare.tools'/'ci'


def read(name='alerts.json'):
    try:
        return json.loads((store()/name).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def update(change, name='alerts.json'):
    """Read-modify-write one store file under the folder's writer lock; watchers run concurrently."""
    from compute_plane.native_journal import custody_lock
    folder = store()
    folder.mkdir(parents=True, exist_ok=True)
    for _ in range(100):
        try:
            with custody_lock(folder):
                alerts = read(name)
                change(alerts)
                tmp = folder/(name+'.tmp')
                tmp.write_text(json.dumps(alerts, indent=1), encoding='utf-8')
                os.replace(tmp, folder/name)
                return alerts
        except ValueError:  # another watcher is writing
            time.sleep(.1)
    raise TimeoutError('ci alerts store stayed locked')


def alert_entries():
    """The open alerts with their store keys, oldest first; `tare ci dismiss` takes the key."""
    return sorted(read().items(), key=lambda item: item[1].get('created_at') or '')


def open_alerts():
    return [alert for _, alert in alert_entries()]


def fresh_alerts(seen):
    """Open alerts this frontend has not shown yet (it owns `seen`); they count as shown from now on."""
    key = lambda a: f"{a['repo']}|{a['workflow']}|{a['branch']}|{a['created_at']}"
    new = [a for a in open_alerts() if key(a) not in seen]
    seen.update(key(a) for a in new)
    return new


def notice(alerts):
    return ('CI red: ' + '; '.join(f"{a['repo'].split('/')[-1]} ({a['workflow']}, \"{a['commit'][:60]}\")"
                                        for a in alerts) + '. Details: tare ci check')


def gh(args, cwd):
    # A removed checkout cannot be a working directory; `-R` calls do not need it.
    return subprocess.run(['gh', *args], cwd=cwd if Path(cwd).is_dir() else None, capture_output=True, text=True,
                          encoding='utf-8', errors='replace', timeout=120, check=True).stdout


FIELDS = 'databaseId,attempt,status,conclusion,workflowName,headBranch,url,createdAt,displayTitle,event'


def attempt(run):
    """A rerun keeps its run's id: each attempt is settled on its own."""
    return run['databaseId'], run.get('attempt', 1)


def runs_for(repo_dir, sha, repo):
    # -R: the watcher outlives the push, and a temporary checkout may be gone before the run ends.
    return json.loads(gh(['run', 'list', '-R', repo, '--commit', sha, '--json', FIELDS], repo_dir) or '[]')


def excerpt(repo_dir, run_id, repo, limit=12):
    """The error lines of the failed jobs, without GitHub's job/step/timestamp columns; failed tests sharing
    one error message collapse into the first with a count."""
    try:
        log = gh(['run', 'view', '-R', repo, str(run_id), '--log-failed'], repo_dir)
    except (OSError, subprocess.SubprocessError):
        return []
    seen = {}
    for raw in log.splitlines():
        job, _, rest = raw.partition('\t')
        text = re.sub(r'^[^\t]*\t\S+Z ', '', rest).rstrip()
        if not ERROR_LINE.search(text):
            continue
        message = text.split(' - ', 1)[1] if text.startswith('FAILED ') and ' - ' in text else text
        entry = seen.setdefault(job + '|' + message[:120], [f'{job}: {text}'[:300], 0])
        entry[1] += 1
    lines = [line + (f' (+{n - 1} with the same error)' if n > 1 else '') for line, n in seen.values()]
    return lines[-limit:]


def settle(repo, repo_dir, run, session=None):
    """Record one completed run: a failure opens (or replaces) its alert, owned by the Claude Code `session`
    that pushed (if any); a success resolves older ones. Returns the alert only when it is new, so settling
    a run again never notifies twice. A pull request's failed run is its shepherd's: the first failure alerts,
    without automatically repeating tests.
    ponytail: a pull request without a shepherd (no `tare ci pr`) is never alerted; it cannot merge red either."""
    key = f"{repo}|{run['workflowName']}|{run['headBranch']}"
    full = run.get('event') in FULL_EVENTS
    if run['conclusion'] in FAILED:
        if run.get('event') == 'pull_request' or read().get(key+'|full' if full else key, {}).get('created_at') == run['createdAt']:
            return None
        alert = {'repo': repo, 'workflow': run['workflowName'] + (' (full suite)' if full else ''),
                 'branch': run['headBranch'], 'commit': run['displayTitle'], 'url': run['url'],
                 'created_at': run['createdAt'], 'errors': excerpt(repo_dir, run['databaseId'], repo), 'session': session}
        update(lambda alerts: alerts.__setitem__(key+'|full' if full else key, alert))
        return alert
    if run['conclusion'] == 'success':
        def resolve(alerts):
            # A green push run proves only the tests that ran its change; a green full run proves both.
            for done in [key, key+'|full'] if full else [key]:
                if done in alerts and alerts[done]['created_at'] <= run['createdAt']:  # a newer red run stays
                    del alerts[done]
        update(resolve)
    return None


def nightly(repo_dir):
    """Settle the repository's latest completed full run (scheduled or dispatched) of each workflow and branch:
    it alone says whether the whole suite passes; older runs would flap the alert and notify again. Returns the
    alerts it opened."""
    repo = json.loads(gh(['repo', 'view', '--json', 'nameWithOwner'], repo_dir))['nameWithOwner']
    latest = {}
    for event in sorted(FULL_EVENTS):
        for run in json.loads(gh(['run', 'list', '-R', repo, '--event', event, '--limit', '5', '--json', FIELDS], repo_dir) or '[]'):
            key = (run['workflowName'], run['headBranch'])
            if run['status'] == 'completed' and run['createdAt'] > latest.get(key, {}).get('createdAt', ''):
                latest[key] = run
    pushed = read('repos.json').get(str(repo_dir))
    session = pushed.get('session') if isinstance(pushed, dict) else None  # who last pushed here
    opened = []
    for run in latest.values():
        alert = settle(repo, repo_dir, run, session)
        if alert:
            opened.append(alert)
            notify(alert)
    return opened


def notify(alert):
    """A Windows notification for the person at the machine; silent elsewhere."""
    if os.name != 'nt':
        return
    title = f"CI red: {alert['repo'].split('/')[-1]} ({alert['workflow']})"
    body = alert['commit'][:120]
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null;"
        "$x = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
        "$t = $x.GetElementsByTagName('text'); $t.Item(0).AppendChild($x.CreateTextNode($env:T)) > $null;"
        "$t.Item(1).AppendChild($x.CreateTextNode($env:B)) > $null;"
        "$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe';"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show("
        "[Windows.UI.Notifications.ToastNotification]::new($x))")
    subprocess.run(['powershell', '-NoProfile', '-Command', script], env={**os.environ, 'T': title, 'B': body},
                   capture_output=True, timeout=30)


def watch(repo_dir, sha, *, session=None, appear=180, limit=5400, every=30, sleep=time.sleep):
    """Follow the commit's runs to completion; alerts belong to the pushing `session`. Returns those opened."""
    repo, started, settled, opened = None, time.monotonic(), set(), []
    while time.monotonic() - started < limit:
        try:
            repo = repo or json.loads(gh(['repo', 'view', '--json', 'nameWithOwner'], repo_dir))['nameWithOwner']
            runs = runs_for(repo_dir, sha, repo)
        except (OSError, subprocess.SubprocessError, ValueError, KeyError):
            runs = None  # GitHub unreachable for a moment (even the first call): keep waiting
        for run in runs or []:
            if run['status'] == 'completed' and attempt(run) not in settled:
                settled.add(attempt(run))
                alert = settle(repo, repo_dir, run, session)
                if alert:
                    opened.append(alert)
                    notify(alert)
        if runs is not None and (runs and all(r['status'] == 'completed' for r in runs)
                                 or not runs and time.monotonic() - started >= appear):
            break
        sleep(every)
    return opened


def describe(alerts, keys=None):
    """One line per alert, then its error lines; `keys` names each alert's store key, the `tare ci dismiss`
    argument, where the caller knows it."""
    lines = []
    for i, a in enumerate(alerts):
        lines.append(f"- {a['repo']} / {a['workflow']} ({a['branch']}): \"{a['commit']}\" {a['url']}"
                     + (f" [{keys[i]}]" if keys else ''))
        lines.extend('    ' + e for e in a['errors'][-8:])
    return '\n'.join(lines)


def stop_hook(stdin):
    try:
        event = json.loads(stdin or '{}')
    except ValueError:
        event = {}
    # Only the session that pushed is held: another agent on this machine has nothing to fix.
    alerts = [a for a in open_alerts() if a.get('session') and a['session'] == event.get('session_id')]
    if event.get('stop_hook_active') or not alerts:
        return 0
    # A JSON decision with exit 0, so a failing hook command (exit != 0) never blocks anyone by accident.
    print(json.dumps({'decision': 'block', 'reason': 'CI is red on a push from this session. Fix it (or tell the '
                      'user why not) before stopping; a green run of the same workflow closes the alert:\n'
                      + describe(alerts)}))  # ASCII escapes: Windows writes stdout in the local code page
    return 0


MANIFEST = 'contracts/v1/harness_disposition_manifest.json'


def git_in(cwd, *args):
    return subprocess.run([host_executable('git', required=True), '-C', str(cwd), *args], capture_output=True, text=True, encoding='utf-8',
                          errors='replace', timeout=300, check=True).stdout.strip()


GRAPH = 'work-graph.json'


def graph_merge_losses(cwd, before):
    """Fail closed on discarded delivery context; never synthesize completion.

    The regression guard covers the real Git writer (catch_up/absorb), tested by reproducing the loss before the fix and the preservation after it, keeping justified invalidations and the X2/AC-03 caveat.

    Git is the writer here, not the graph transaction owner. Refuse ambiguous
    loss for explicit CLI reconciliation rather than restoring a snapshot or
    implementing a second CAS. A Work revision may invalidate completion, but
    must retain its previous context in the owner's revision receipt.
    """
    def load(ref):
        names = git_in(cwd, 'ls-tree', '--name-only', ref, '--', GRAPH)
        return json.loads(git_in(cwd, 'show', f'{ref}:{GRAPH}')) if names else None

    def revised_with_receipt(node, merged):
        from graph_backlog.jsonutil import sha256_canonical
        old_receipts = node.get('metrics', {}).get('work_change_receipts', [])
        for ref in merged.get('metrics', {}).get('work_change_receipts', []):
            if ref in old_receipts:
                continue
            try:
                receipt = json.loads(Path(ref).read_text(encoding='utf-8'))
                change = receipt['change']
                if (receipt['sha256'] == sha256_canonical(change)
                        and change['schema'] == 'tare.tools/work-change/1'
                        and change['reason'].strip()
                        and merged.get('canonical_revision') != node.get('canonical_revision')
                        # The owner also resets completed dependents without
                        # changing their criteria. Accept only the exact reset
                        # pinned by the receipt, never an inferred invalidation.
                        and node['id'] in change['dependency_cone']
                        and node in change['before'] and merged in change['after']
                        and merged['completion']['status'] == 'NOT_DONE'):
                    return True
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return False

    old = load(before)
    if old is None:
        return []
    path = Path(cwd)/GRAPH
    new = json.loads(path.read_text(encoding='utf-8')) if path.exists() else None
    if new is None:
        return [GRAPH]
    nodes = {n['id']: n for n in new['nodes']}
    for node in old['nodes']:
        merged = nodes.get(node['id'])
        if merged is None:
            return [GRAPH]
        # Explicit owner revisions invalidate proofs instead of resurrecting
        # obsolete DONE. Unexplained resets must be reconciled by the CLI.
        if revised_with_receipt(node, merged):
            continue
        old_completion, completion = node.get('completion', {}), merged.get('completion', {})
        if (old_completion.get('status') in ('DONE', 'PARTIAL')
                and completion.get('status') != old_completion['status']):
            return [GRAPH]
        for field in ('dod_evidence', 'residuals'):
            if any(v not in completion.get(field, []) for v in old_completion.get(field, []) or []):
                return [GRAPH]
        for field in ('provenance', 'source_refs', 'notes'):
            if any(v not in merged.get(field, []) for v in node.get(field, [])):
                return [GRAPH]
        history = node.get('metrics', {}).get('delivery_history', [])
        if any(v not in merged.get('metrics', {}).get('delivery_history', []) for v in history):
            return [GRAPH]
        for field in ('evidence_grade', 'evidence_grade_reason', 'completed_at', 'evidence_observed_at'):
            if old_completion.get(field) is not None and completion.get(field) != old_completion[field]:
                return [GRAPH]
    return []


def catch_up(repo_dir, branch):
    """Merge main into the pushed `branch` in a throwaway worktree and push the merge (its CI runs again). Returns
    [] when pushed, or the files that conflict (nothing is pushed)."""
    import shutil
    import tempfile
    git_in(repo_dir, 'fetch', '--quiet', 'origin', 'main', branch)
    tree = Path(tempfile.mkdtemp(prefix='tare-pr-'))
    rules = tree.with_suffix('.attributes')  # the union also for branches opened before .gitattributes said so
    rules.write_text('CHANGELOG.md merge=union\n', encoding='utf-8')
    try:
        git_in(repo_dir, 'worktree', 'add', '--quiet', '--detach', str(tree), f'origin/{branch}')
        try:
            git_in(tree, '-c', f'core.attributesFile={rules}', 'merge', '--no-commit', '--no-ff', '--no-edit', '--quiet', 'origin/main')
        except subprocess.CalledProcessError:
            conflicts = git_in(tree, 'diff', '--name-only', '--diff-filter=U').splitlines() or ['(the merge failed)']
            if conflicts != [MANIFEST]:
                return conflicts
            # The manifest holds every relay module's hash: any two pull requests that touch code rewrite neighbouring
            # lines of it. Its merged content is a function of the merged files, so regenerate it (2026-10-06), from
            # the modules of both sides: main's copy alone lacked the module the branch added (gpu.py, #84).
            ours, theirs = (json.loads(git_in(tree, 'show', f':{stage}:{MANIFEST}')) for stage in (2, 3))
            known = {m['path'] for m in theirs['modules']}
            theirs['modules'] += [m for m in ours['modules'] if m['path'] not in known]
            (tree/MANIFEST).write_text(json.dumps(theirs, indent=2, ensure_ascii=False) + '\n', encoding='utf-8',
                                       newline='\n')
            subprocess.run([sys.executable, str(tree/'scripts'/'sync_disposition_manifest.py'), str(tree)],
                           check=True, capture_output=True, timeout=120)
            git_in(tree, 'add', '--', MANIFEST)
        if graph_merge_losses(tree, 'HEAD') or graph_merge_losses(tree, 'origin/main'):
            return [GRAPH]
        if git_in(tree, 'ls-files', '--unmerged'):
            return [GRAPH]
        if subprocess.run([host_executable('git', required=True), '-C', str(tree), 'rev-parse', '--quiet', '--verify', 'MERGE_HEAD'], capture_output=True).returncode == 0:
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


def queue_ahead(repo_dir, number, repo):
    """Older open pull requests into main with auto-merge on: they are brought up to date before this one."""
    rows = json.loads(gh(['pr', 'list', '-R', repo, '--base', 'main', '--state', 'open', '--json',
                          'number,createdAt,autoMergeRequest'], repo_dir) or '[]')
    mine = next((r for r in rows if r['number'] == number), None)
    if mine is None:
        return []
    return sorted(r['number'] for r in rows if r.get('autoMergeRequest') and r['number'] != number
                  and (r['createdAt'], r['number']) < (mine['createdAt'], number))


def failed_runs(repo_dir, sha, repo):
    """Return completed failures for this head without changing or repeating any run."""
    runs = json.loads(gh(['run', 'list', '-R', repo, '--commit', sha, '--json',
                          'databaseId,attempt,status,conclusion,workflowName,url'], repo_dir) or '[]')
    return [run for run in runs if run['status'] == 'completed' and run['conclusion'] in FAILED]


def pr_alert(repo, number, view, what, errors, session):
    alert = {'repo': repo, 'workflow': 'pull request', 'branch': view['headRefName'],
             'commit': f"#{number} {what}: {view['title']}", 'url': view['url'],
             'created_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'errors': errors[:20],
             'session': session}
    update(lambda alerts: alerts.__setitem__(f"{repo}|pull request|{view['headRefName']}", alert))
    notify(alert)


SHEPHERD_SILENCE = 900  # a shepherd beats once a round (a minute, a few with a catch-up); this long silent, it is gone
STUCK_ROUNDS = 10  # rounds failing in a row (some 10 minutes): longer than GitHub's usual blips

NOT_FOUND = ('http 404', 'could not resolve to a', 'not found')


def not_found(error):
    """gh answered that its subject is gone (a removed pull request or repository), not that it was unreachable:
    the two must not share a path, or a deleted pull request only alerted as "shepherd stuck" ten rounds later."""
    return any(marker in f"{getattr(error, 'stderr', None) or ''} {error}".lower() for marker in NOT_FOUND)


def logged_in():
    """gh's login works. A private repository answers 404 to a login without access (an expired token) to hide
    that it exists, so a not-found from a broken login must stay a visible "shepherd stuck", not read as gone."""
    try:
        gh(['api', 'user', '--jq', '.login'], '.')
        return True
    except (OSError, subprocess.SubprocessError, ValueError):
        return False


def gone(error):
    """A not-found answer from a working login. A valid login without access to a private repository still reads
    as gone: GitHub answers both the same way."""
    return not_found(error) and logged_in()


def orphan(beat):
    """A stale beat whose checkout is gone and that records no repository has nothing left to follow (ADR-072):
    starting its shepherd again could only fail. The repository is unknown, so a `repo_dir` that is not there is a
    checkout that is gone, relative or not: a relative path proves nothing from the directory a check runs in."""
    return not beat.get('repo') and not Path(beat['repo_dir']).is_dir()


def recorded_branch(repo, number):
    """The head branch of a pull request an earlier shepherd read, from the alert it left: a first round answered
    not-found has no view, and the push alerts of that branch are found by its name alone (AC-05)."""
    if not repo:
        return None
    named = re.compile(rf'#{number}(?!\d)')
    for alert in read().values():
        if (alert.get('repo') == repo and alert.get('workflow') == 'pull request'
                and named.search(alert.get('commit') or '')):
            return alert.get('branch')
    return None


def retire(name, repo_dir, number, repo=None, branch=None):
    """End a shepherd's subject: its beat, the stand-in alert of a pull request it never read and -- once it read
    one -- its alert and every alert of that repository and head branch, a push run's included. A pull request the
    shepherd never read is still found by the number its alert names, so a first round answered not-found leaves
    nothing behind."""
    stand_in = f'{repo_dir}|pull request|#{number}'

    def forget(alerts):
        alerts.pop(stand_in, None)
        if repo and branch:
            alerts.pop(f'{repo}|pull request|{branch}', None)
        if repo:
            named = re.compile(rf'#{number}(?!\d)')
            for key in [k for k, alert in alerts.items()
                        if alert.get('repo') == repo
                        and (named.search(alert.get('commit') or '') or branch and alert.get('branch') == branch)]:
                del alerts[key]
    update(forget)
    update(lambda beats: beats.pop(name, None), 'shepherds.json')


def revive(now=None):
    """Start again each shepherd that stopped beating (the machine restarted or the process died):
    a pull request's CI alerts only through its shepherd, so without one a red pull request waits unseen. A beat
    whose checkout and repository are both gone is retired instead (ADR-072). Returns the numbers started."""
    now, stale, retired = now or time.time(), [], []
    def take(beats):
        for name, beat in list(beats.items()):
            if now - beat['at'] <= SHEPHERD_SILENCE:
                continue
            if orphan(beat):
                retired.append(beats.pop(name))
                continue
            beat['at'] = now  # one revival per silence
            stale.append(beat)
    update(take, 'shepherds.json')
    for beat in retired:
        update(lambda alerts: alerts.pop(f"{beat['repo_dir']}|pull request|#{beat['number']}", None))
    for beat in stale:
        detached('shepherd', beat['repo_dir'], beat['number'], session=beat.get('session'))
    return [beat['number'] for beat in stale]


def shepherd(repo_dir, number, *, session=None, every=60, limit=None, sleep=time.sleep):
    """Keep pull request `number` up to date with main until it merges, closes or loses auto-merge.
    A conflict outside CHANGELOG.md, or the first CI failure, is an alert of the pushing `session`
    for as long as it lasts (once per head). It beats in shepherds.json until the pull request is done, so revive()
    starts it again if it stops before. It has no time limit (`limit` seconds, for tests): a pull request can wait
    hours in the queue, and 6 hours stopped every shepherd of a queue overnight on 2026-10-07. A round that fails
    STUCK_ROUNDS times in a row is an alert too: it beats while it fails, so revive() does not see it. Returns the
    pull request's last state.
    Its worktree is usually parked or deleted once the pull request merges, and `gh` run there failed with "no git
    remotes found": the shepherd never saw MERGED and ended as "shepherd stuck". So it resolves owner/repo once,
    keeps it, with the head branch it read, in its beat before it waits for the next round -- so a restart answered
    not-found still finds the push alerts of that branch -- and names it (-R) in every gh call; only a catch-up needs
    the checkout. A pull request or repository that was removed or archived ends it with 'GONE' in that round, without
    an alert of its own: its beat and that pull request's alerts go with it."""
    started, state, alerted, settled = time.monotonic(), None, None, set()
    name, view, failing = f'{repo_dir}#{number}', None, 0
    stored = read('shepherds.json').get(name, {})
    repo, branch = stored.get('repo'), stored.get('branch')  # a revive keeps the head branch it read

    def beat():  # written before it waits, so a restart answered not-found still knows the branch it read
        update(lambda beats: beats.__setitem__(name, {'repo_dir': str(repo_dir), 'number': number, 'repo': repo,
                                                       'branch': branch, 'session': session,
                                                       'at': time.time()}), 'shepherds.json')

    while limit is None or time.monotonic() - started < limit:
        beat()
        try:
            try:  # only the queries about the subject itself end it; a later failure of the round is a retry
                if not repo:
                    recorded = json.loads(gh(['repo', 'view', '--json', 'nameWithOwner,isArchived'], repo_dir))
                    repo = recorded['nameWithOwner']
                else:  # archived after the first round, or revived from a beat: still ends, nothing merges there
                    recorded = repository_view(repo)
                if recorded.get('isArchived'):
                    read_branch = None
                    try:
                        read_branch = json.loads(gh(['pr', 'view', str(number), '-R', repo, '--json', 'headRefName'],
                                                    repo_dir))['headRefName']
                    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
                        pass  # nothing left to read: its beat and the stand-in alert still go
                    retire(name, str(repo_dir), number, repo,
                           read_branch or branch or recorded_branch(repo, number))
                    return 'GONE'
                view = json.loads(gh(['pr', 'view', str(number), '-R', repo, '--json',
                                      'state,mergeStateStatus,autoMergeRequest,headRefName,headRefOid,url,title'], repo_dir))
            except (OSError, subprocess.SubprocessError, ValueError, KeyError) as error:
                if not gone(error):
                    raise  # unreachable, not gone: the round's own handler below counts it
                # The pull request or its repository was removed: nothing is left to fix, so no alert of its own,
                # and the stand-in alert of a pull request it never read goes too (SPEC-OS-SHIP-001 AC-05).
                retire(name, str(repo_dir), number, repo,
                       (view or {}).get('headRefName') or branch or recorded_branch(repo, number))
                return 'GONE'
            branch = view['headRefName']  # the beat keeps it, so a later restart's not-found still finds its pushes
            beat()  # persisted now, before the wait: a restart answered not-found still finds its pushes
            key, head, state = f"{repo}|pull request|{branch}", view['headRefOid'], view['state']
            for run in runs_for(repo_dir, head, repo) or []:  # the pushes it made had no watcher
                if run['status'] == 'completed' and attempt(run) not in settled:
                    settled.add(attempt(run))
                    settle(repo, repo_dir, run, session)
            if state != 'OPEN' or not view.get('autoMergeRequest'):
                update(lambda alerts: alerts.pop(key, None))
                update(lambda beats: beats.pop(name, None), 'shepherds.json')
                return state
            problem, failures = None, []
            if view['mergeStateStatus'] in ('BEHIND', 'DIRTY'):
                if alerted and alerted[:2] == ('conflicts with main', head):
                    problem = ('conflicts with main', alerted[2])  # main not merged in yet: no new attempt on this head
                elif not queue_ahead(repo_dir, number, repo):  # the oldest waiting pull request goes first
                    conflicts = catch_up(repo_dir, view['headRefName'])
                    problem = ('conflicts with main', conflicts) if conflicts else None
            elif view['mergeStateStatus'] in ('BLOCKED', 'UNSTABLE'):
                failures = failed_runs(repo_dir, head, repo)
                problem = ('CI failed', [f"{r['workflowName']}: {r['url']}" for r in failures]) if failures else None
            if problem and (not alerted or alerted[:2] != (problem[0], head)):
                # The failing tests themselves, as a push's alert has them: the only alert this pull request's CI gets.
                errors = problem[1] + [line for r in failures for line in excerpt(repo_dir, r['databaseId'], repo)]
                pr_alert(repo, number, view, problem[0], errors, session)
                alerted = (problem[0], head, problem[1])
            elif not problem and (alerted or key in read()):  # read(): one an earlier shepherd raised, too
                update(lambda alerts: alerts.pop(key, None))
                alerted = None
        except (OSError, subprocess.SubprocessError, ValueError, KeyError) as error:
            # GitHub unreachable for a moment, or main moved again while merging: the next round retries. Failing
            # round after round (a refused push, an expired login) the queue waits behind it: 2 hours on 2026-10-08.
            # Only a not-found answer to a query about the pull request or its repository (above) ends it as GONE: a
            # run listing or a catch-up that failed says nothing about the subject, which may still be open.
            failing += 1
            if failing == STUCK_ROUNDS:
                shown = view or {'headRefName': f'#{number}', 'title': str(repo_dir), 'url': ''}
                reason = (getattr(error, 'stderr', None) or '').strip()[-500:]
                # A pull request it never read gets the stand-in key the recovery below clears, even once repo is known.
                pr_alert(repo if view else str(repo_dir), number, shown, 'shepherd stuck',
                         [f'{error} {reason}'.strip()], session)
                alerted = ('shepherd stuck', None, None)
        else:
            if failing >= STUCK_ROUNDS:  # the stand-in key, when it never read its pull request
                update(lambda alerts: alerts.pop(f'{repo_dir}|pull request|#{number}', None))
            failing = 0
        sleep(every)
    return state


def detached(*args, session=None):
    """Start this module with `args` in a process that outlives the caller, as the pre-push hook starts the watcher;
    `session` is the Claude Code session its alerts belong to (default: the caller's).
    ponytail: a second `tare ci pr` on the same branch starts a second shepherd; both only update a branch behind."""
    # A hidden console, not none: a console-less parent on Windows gives every gh and git it runs, once a minute, a
    # console window of its own (2026-10-06). CREATE_NO_WINDOW keeps one hidden console the children inherit.
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
    env = {**os.environ, 'CLAUDE_CODE_SESSION_ID': session} if session else None
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), *map(str, args)], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags,
                     start_new_session=os.name != 'nt', env=env)


def absorb(repo_dir, branch):
    """Bring into the local branch what the shepherd pushed to it (main merged in), so the next push fast-forwards;
    a conflict leaves the branch as it was."""
    if git_in(repo_dir, 'status', '--porcelain', '--untracked-files=no'):
        raise ValueError('Commit or stash local changes before absorbing remote commits.')
    try:
        git_in(repo_dir, 'fetch', '--quiet', 'origin', branch)
    except subprocess.CalledProcessError:
        return  # not pushed yet
    if subprocess.run([host_executable('git', required=True), '-C', str(repo_dir), 'merge-base', '--is-ancestor', f'origin/{branch}', 'HEAD']).returncode:
        try:
            git_in(repo_dir, 'merge', '--no-commit', '--no-ff', '--no-edit', '--quiet', f'origin/{branch}')
            if graph_merge_losses(repo_dir, 'HEAD') or graph_merge_losses(repo_dir, f'origin/{branch}'):
                raise ValueError(f'origin/{branch} discards delivery context in {GRAPH}; reconcile it with the backlog CLI first.')
            git_in(repo_dir, 'commit', '--no-edit', '--quiet')
        except (subprocess.CalledProcessError, ValueError, KeyError, TypeError):
            subprocess.run([host_executable('git', required=True), '-C', str(repo_dir), 'merge', '--abort'], capture_output=True)
            raise ValueError(f'origin/{branch} has commits that conflict with this branch: merge them first.')


def pr_identity(value):
    if not isinstance(value, dict):
        return False
    number, url = value.get('number'), value.get('url')
    return (type(number) is int and number > 0 and isinstance(url, str)
        and re.fullmatch(r'https://[^/\s]+/[^/\s]+/[^/\s]+/pull/'+str(number), url) is not None)


def preflight(repo_dir):
    """What GitHub says about the repository and its main branch. The repository settings are required (a failure
    propagates, so nothing is pushed blind); the protection readings fall back to "unknown" or "none"."""
    def read(endpoint, default):
        try:
            value = json.loads(gh(['api', endpoint], repo_dir))
        except (ValueError, OSError, subprocess.SubprocessError):
            return default
        return value if isinstance(value, type(default)) else default
    settings = json.loads(gh(['api', 'repos/{owner}/{repo}'], repo_dir))
    if not isinstance(settings, dict):
        raise ValueError('Preflight refused: GitHub did not return the repository settings.')
    return {'settings': settings, 'branch': read('repos/{owner}/{repo}/branches/main', {}),
            'protection': read('repos/{owner}/{repo}/branches/main/protection', {}),  # 404 or 403: none we can see
            'rules': read('repos/{owner}/{repo}/rules/branches/main', [])}


def merge_plan(settings, branch, protection, rules):
    """What the repository allows, and the merge method that follows: merge commit, else squash, never rebase.
    A setting GitHub did not show (a token without push access) is unknown, which counts as allowed."""
    rules = [rule for rule in rules if isinstance(rule, dict)]
    def params(kind):
        return [rule.get('parameters') or {} for rule in rules if rule.get('type') == kind]
    def classic(key):
        return protection.get(key) or {}
    checks = set(classic('required_status_checks').get('contexts') or [])
    checks |= set(((branch.get('protection') or {}).get('required_status_checks') or {}).get('contexts') or [])
    checks |= {check['context'] for found in params('required_status_checks')
               for check in found.get('required_status_checks') or [] if isinstance(check, dict) and check.get('context')}
    reviews = max([classic('required_pull_request_reviews').get('required_approving_review_count') or 0]
                  + [found.get('required_approving_review_count') or 0 for found in params('pull_request')])
    kinds = {rule.get('type') for rule in rules}
    signed = classic('required_signatures').get('enabled') is True or 'required_signatures' in kinds
    linear = classic('required_linear_history').get('enabled') is True or 'required_linear_history' in kinds
    out = {}  # method -> why the repository rules it out
    for method, setting in (('merge', 'allow_merge_commit'), ('squash', 'allow_squash_merge')):
        if settings.get(setting) is False:
            out[method] = f'the repository setting {setting} is off'
        for found in params('pull_request'):
            allowed = found.get('allowed_merge_methods')
            if isinstance(allowed, list) and method not in allowed:
                out.setdefault(method, 'a ruleset allows only ' + ', '.join(allowed))
    if linear:
        out.setdefault('merge', 'linear history is required')
    method = next((name for name in ('merge', 'squash') if name not in out), None)
    if method is None:
        raise ValueError(f"Preflight refused: no merge method is left: merge commit ({out['merge']}), squash "
                         f"({out['squash']}); rebase is never used.")
    return {'method': method, 'auto_merge': settings.get('allow_auto_merge'), 'protected': branch.get('protected'),
            'required_checks': sorted(checks), 'required_reviews': reviews, 'signed_commits': signed,
            'linear_history': linear, 'visibility': settings.get('visibility')}


# What builds the Windows sandbox checks run in (its policy, environment and launcher) and the probe that proves it.
# CI's Windows has none, so a pull request touching one runs the probe here first (docs/guides/CI_FEEDBACK.md).
SANDBOX_PATHS = ('relay/command_protection.py', 'relay/sandbox_secrets.py', 'relay/sandbox-python/',
                 'relay/work_units.py', 'relay/windows_isolation.py', 'satellites/tare.tools.kernel',
                 'scripts/ops/sandbox_probe.py')


def sandbox_probe(repo_dir):
    """'not needed', 'passed' or 'not run: no sandbox on this host'; ValueError when the probe of this checkout fails.
    No flag skips it: someone who must can open the pull request with gh by hand."""
    git_in(repo_dir, 'fetch', '--quiet', 'origin', 'main')
    if not any(path.startswith(SANDBOX_PATHS) for path in
               git_in(repo_dir, 'diff', '--name-only', 'origin/main...HEAD').splitlines()):
        return 'not needed'
    repo_dir = Path(repo_dir)
    path = os.pathsep.join((str(repo_dir), str(repo_dir/'satellites'/'tare.tools.kernel')))
    try:
        done = subprocess.run([sys.executable, str(repo_dir/'scripts'/'ops'/'sandbox_probe.py')], cwd=repo_dir,
                              env={**os.environ, 'PYTHONPATH': path, 'PYTHONUTF8': '1'}, capture_output=True,
                              text=True, encoding='utf-8', errors='replace', timeout=900)
    except subprocess.TimeoutExpired:
        raise ValueError('Preflight refused: the sandbox probe failed: it ran for more than 15 minutes.') from None
    if done.returncode == 3:
        print('tare ci pr: this host has no sandbox, so the sandbox probe did not run: the real sandbox was not '
              'exercised.', file=sys.stderr)
        return 'not run: no sandbox on this host'
    if done.returncode:
        try:
            failed = ', '.join(json.loads(done.stdout.splitlines()[-1])['failed'])
        except (ValueError, IndexError, KeyError, TypeError):
            failed = f'exit {done.returncode}: ' + (done.stdout + done.stderr).strip()[-300:]
        raise ValueError(f'Preflight refused: the sandbox probe failed: {failed}. See why with '
                         '`python scripts/ops/sandbox_probe.py`.')
    return 'passed'


def pull_request(repo_dir, body=None):
    """main takes changes through pull requests that merge themselves once CI is green: refuse what the
    preflight refuses, push the current branch, open its pull request into main (or reuse the open one), turn on
    auto-merge with the merge method the repository allows and, when main moved, bring the branch up to date
    (main requires it). Returns {'number', 'url', 'state', 'preflight', 'sandbox_probe'}."""
    def git(*args):
        return subprocess.run([host_executable('git', required=True), '-C', str(repo_dir), *args], capture_output=True, text=True, encoding='utf-8',
                              errors='replace', check=True).stdout.strip()
    branch = git('rev-parse', '--abbrev-ref', 'HEAD')
    if branch in ('HEAD', 'main', 'dev'):
        raise ValueError('Work on a branch: main takes changes only through a pull request.')
    absorb(repo_dir, branch)
    read = preflight(repo_dir)
    plan = merge_plan(read['settings'], read['branch'], read['protection'], read['rules'])
    # This file also runs as a script (the detached shepherd), where a relative import at module level fails.
    from . import push_preflight
    executable = host_executable('git', required=True)
    refusals = []
    if read['settings'].get('private') is not True:  # anything but a private repository counts as public: fail closed
        refusals = push_preflight.identity_refusals(push_preflight.unpublished(repo_dir, 'HEAD', git=executable))
    fix = ' ' + push_preflight.FIX if refusals else ''
    if plan['signed_commits']:
        refusals += [f'commit {sha[:8]} is not signed and main requires signed commits'
                     for sha in push_preflight.unsigned(repo_dir, 'HEAD', git=executable)]
    if refusals:
        raise ValueError('Preflight refused: ' + '; '.join(refusals) + '.' + fix)
    probe = sandbox_probe(repo_dir)
    git('push', '--quiet', '--set-upstream', 'origin', branch)
    found = json.loads(gh(['pr', 'list', '--head', branch, '--base', 'main', '--state', 'open', '--json', 'number,url'],
                          repo_dir) or '[]')
    if not found:
        git('fetch', '--quiet', 'origin', 'main')
        commits = git('log', '--format=- %s', 'origin/main..HEAD')
        lines = commits.splitlines()
        title = lines[0][2:] if len(lines) == 1 else f'{branch}: {len(lines)} commits'
        created = gh(['pr', 'create', '--base', 'main', '--head', branch, '--title', title[:120],
            '--body', body or ('Commits:\n' + commits if commits else branch)], repo_dir).strip()
        found = json.loads(gh(['pr', 'list', '--head', branch, '--base', 'main', '--state', 'open', '--json',
                               'number,url'], repo_dir))
        if not found and re.fullmatch(r'https://[^/\s]+/[^/\s]+/[^/\s]+/pull/[1-9][0-9]*', created):
            found = [{'number': 0, 'url': created}]
    if not isinstance(found, list) or len(found) != 1 or not isinstance(found[0], dict):
        raise ValueError('Pull request identity is not visible yet. The created PR is preserved; retry ci pr.')
    if not pr_identity(found[0]):
        url = found[0].get('url')
        if not isinstance(url, str) or not re.fullmatch(r'https://[^/\s]+/[^/\s]+/[^/\s]+/pull/[1-9][0-9]*', url):
            raise ValueError('Invalid pull request identity; auto-merge was not requested.')
        observed = json.loads(gh(['pr', 'view', url, '--json', 'number,url,headRefName,baseRefName'], repo_dir))
        if not pr_identity(observed) or observed.get('headRefName') != branch or observed.get('baseRefName') != 'main':
            raise ValueError('Pull request identity is not confirmed for this branch; auto-merge was not requested.')
        found = [observed]
    number, url = found[0]['number'], found[0]['url']
    # gh --auto merges immediately when the base has no enforced protection.
    # Keep the reviewable PR open rather than assuming another repo's CI gate.
    if plan['protected'] is not True:
        return {'number': number, 'url': url, 'preflight': plan, 'sandbox_probe': probe,
                'state': 'UNPROTECTED_AWAITING_INTEGRATION' if plan['protected'] is False else 'PROTECTION_UNCONFIRMED'}
    if plan['auto_merge'] is False:
        return {'number': number, 'url': url, 'state': 'AUTO_MERGE_DISABLED_AWAITING_INTEGRATION', 'preflight': plan,
                'sandbox_probe': probe}
    try:
        gh(['pr', 'merge', str(number), '--auto', '--' + plan['method']], repo_dir)
    except subprocess.CalledProcessError as exc:
        if 'Auto merge is not allowed for this repository' not in str(exc.stderr or ''):
            raise
        return {'number': number, 'url': url, 'state': 'AUTO_MERGE_DISABLED_AWAITING_INTEGRATION', 'preflight': plan,
                'sandbox_probe': probe}
    state = json.loads(gh(['pr', 'view', str(number), '--json', 'mergeStateStatus'], repo_dir))['mergeStateStatus']
    if state == 'BEHIND':  # main moved: GitHub merges main into the branch, and the local branch follows
        gh(['pr', 'update-branch', str(number)], repo_dir)
        git('pull', '--quiet', '--ff-only', 'origin', branch)
    detached('shepherd', repo_dir, number)
    return {'number': number, 'url': url, 'state': state, 'preflight': plan, 'sandbox_probe': probe}


def forget_pushing_worktree():
    """Drop the worktree a git hook names from this process's environment (its children inherit it). The pre-push
    hook starts `tare ci watch` with git's GIT_DIR, GIT_INDEX_FILE and the commit's GIT_AUTHOR_DATE, and with them
    every `git -C <dir>` here and in the shepherds it revives acted on the pushing worktree: a shepherd merged main
    into whatever branch that worktree had checked out and pushed it to its pull request, refused, so no pull
    request was brought up to date (2026-10-08)."""
    try:
        names = subprocess.run([host_executable('git', required=True), 'rev-parse', '--local-env-vars'], capture_output=True, text=True,
                               timeout=30).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return  # no git: nothing here runs it
    for name in names + ['GIT_AUTHOR_DATE', 'GIT_COMMITTER_DATE']:
        os.environ.pop(name, None)


REPOSITORY_NAME = re.compile(r'[^/\s:\\]+/[^/\s:\\]+')


def repo_name(value):
    """`owner/name` when the value names a repository gh can be asked about; a local path names none."""
    return value if isinstance(value, str) and REPOSITORY_NAME.fullmatch(value) else None


def repository_view(repo):
    """`gh repo view` takes the repository as its positional argument: `-R` is not one of its flags, so a check
    that named it that way failed every round and no archived repository was ever noticed (AC-05)."""
    return json.loads(gh(['repo', 'view', repo, '--json', 'isArchived'], '.'))


def pull_request_state(repo, number):
    return json.loads(gh(['pr', 'view', str(number), '-R', repo, '--json', 'state'], '.'))


def ended(alert, beats):
    """The names of the shepherd beats an alert leaves behind once its subject ended: its pull request merged or
    closed, its repository removed or archived. None while the subject stands; raises where gh failed without a
    not-found answer, so an alert gh could not resolve is never removed."""
    found = re.search(r'#([1-9][0-9]*)', alert.get('commit') or '')
    number = int(found.group(1)) if found else None
    named = [name for name, beat in beats.items()
             if number is not None and beat.get('number') == number
             and (beat.get('repo') == alert.get('repo') or str(beat.get('repo_dir')) == alert.get('repo'))]
    repo = next((beats[name]['repo'] for name in named if beats[name].get('repo')), None) or repo_name(alert.get('repo'))
    if repo is None:
        return None  # its repository was never recorded: nothing to ask GitHub about
    every = sorted(set(named) | {name for name, beat in beats.items() if beat.get('repo') == repo})
    try:
        archived = repository_view(repo).get('isArchived')
    except subprocess.CalledProcessError as error:
        if gone(error):
            return every  # the repository itself is gone, with every beat its shepherds left
        raise
    if archived:
        return every
    if alert.get('workflow') == 'pull request':
        if number is None:
            return None  # the alert does not name its pull request: gh cannot be asked about it
        try:
            if pull_request_state(repo, number).get('state') != 'OPEN':
                return named
        except subprocess.CalledProcessError as error:
            if gone(error):
                return named  # its pull request is gone
            raise
    return None


def prune():
    """Remove the alerts whose subject is gone and the shepherd beats they left; an alert gh could not resolve
    stays (fail closed: only a not-found answer proves a subject gone). The alerts of the same repository and
    branch go with a pull request's alert: a push run's alert of that branch outlives its pull request otherwise,
    and the alert that named the branch is gone with it. Returns how many alerts it removed."""
    beats, keys, names, removed = read('shepherds.json'), [], set(), []
    for key, alert in list(read().items()):
        try:
            retired = ended(alert, beats)
        except (OSError, subprocess.SubprocessError, ValueError, KeyError):
            continue  # gh could not tell: the alert stays
        if retired is not None:
            keys.append(key)
            names.update(retired)
    if keys:
        def forget(alerts):
            for key in keys:
                alert = alerts.pop(key, None)
                if alert is None:
                    continue  # an earlier alert of the same branch already took it
                removed.append(key)
                if not alert.get('branch'):
                    continue
                for other in [k for k, a in alerts.items()
                              if a.get('repo') == alert.get('repo') and a.get('branch') == alert.get('branch')]:
                    del alerts[other]
                    removed.append(other)
        update(forget)
    if names:
        update(lambda store: [store.pop(name, None) for name in names], 'shepherds.json')
    return len(removed)


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'):  # a Windows console's code page cannot print every log character
        sys.stdout.reconfigure(errors='replace')
    forget_pushing_worktree()
    parser = argparse.ArgumentParser(prog='tare ci', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='action', required=True)
    follow = sub.add_parser('watch')
    follow.add_argument('repo_dir', type=Path)
    follow.add_argument('sha')
    full = sub.add_parser('nightly')
    full.add_argument('repo_dirs', type=Path, nargs='*')
    check = sub.add_parser('check')
    check.add_argument('--prune', action='store_true',
                       help='remove the alerts whose pull request or repository is gone, with their shepherd beats')
    dismiss = sub.add_parser('dismiss', help='drop one alert, by the key check prints')
    dismiss.add_argument('key')
    sub.add_parser('stop-hook')
    pr = sub.add_parser('pr', help='push this branch and open (or update) its pull request into main, auto-merged '
                                   'once CI is green')
    pr.add_argument('--repo-dir', type=Path, default=Path.cwd())
    pr.add_argument('--body', help='the pull request description (default: its commits)')
    keep = sub.add_parser('shepherd', help='keep a pull request up to date with main until it merges')
    keep.add_argument('repo_dir', type=Path)
    keep.add_argument('number', type=int)
    args = parser.parse_args(argv)
    if args.action in ('watch', 'nightly', 'check', 'stop-hook'):
        try:  # every turn's end, push and daily check restarts a shepherd that stopped
            revive()
        except (OSError, ValueError):
            pass  # never in the way of the check itself
    if args.action == 'shepherd':
        session = os.environ.get('CLAUDE_CODE_SESSION_ID')  # inherited from the agent that ran tare ci pr, if any
        print(json.dumps({'state': shepherd(args.repo_dir.resolve(), args.number, session=session)}))
        return 0
    if args.action == 'pr':
        try:
            print(json.dumps(pull_request(args.repo_dir.resolve(), args.body)))
            return 0
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            detail = getattr(exc, 'stderr', '') or ''
            print(f'tare ci pr: {exc} {detail}'.strip(), file=sys.stderr)
            return 1
    if args.action == 'watch':
        repo_dir = args.repo_dir.resolve()
        session = os.environ.get('CLAUDE_CODE_SESSION_ID')  # inherited from the agent that pushed, if any
        update(lambda repos: repos.__setitem__(str(repo_dir), {'sha': args.sha, 'session': session}), 'repos.json')
        opened = watch(repo_dir, args.sha, session=session)
        try:
            opened += nightly(repo_dir)
        except (OSError, subprocess.SubprocessError, ValueError):
            pass  # the scheduled check retries
        print(json.dumps({'opened': len(opened)}))
        return 0
    if args.action == 'nightly':
        opened = []
        for repo_dir in args.repo_dirs or [Path(p) for p in read('repos.json') if Path(p).is_dir()]:
            try:
                opened += nightly(repo_dir.resolve())
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                print(f'{repo_dir}: {exc}', file=sys.stderr)
        print(json.dumps({'opened': len(opened)}))
        return 0
    if args.action == 'check':
        if args.prune:
            print(f'CI: pruned {prune()} alert(s) whose subject is gone')
        entries = alert_entries()
        print(describe([alert for _, alert in entries], [key for key, _ in entries]) if entries else 'CI: no open alerts')
        return 1 if entries else 0
    if args.action == 'dismiss':
        removed = []
        def drop(alerts):
            removed.append(alerts.pop(args.key, None))
        update(drop)
        if removed[0] is None:
            print(f'tare ci dismiss: no alert {args.key}', file=sys.stderr)
            return 1
        print(describe([removed[0]]))
        return 0
    return stop_hook(sys.stdin.read())


if __name__ == '__main__':
    raise SystemExit(main())
