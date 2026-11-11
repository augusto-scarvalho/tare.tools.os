"""Publish a product release when the OS-qualified sources move. CI runs it (.github/workflows/release.yml) after
tare.tools CI passed on main; elsewhere it publishes nothing unless told `--here`.

Mirrors of the five repositories live under the publisher's state, never the development checkouts, so work in
progress cannot enter. The newest PRODUCT_RELEASE_INPUTS_* lock on OS main is the template: OS uses that main,
while satellites use its gitlink pins, never their independent mains. Versions and timestamps come only from
these selected commits; missing or unavailable pins refuse publication. The build (twice, reproducible) runs in a venv
pinned to the lock's tools, is packed with its dependencies, smoke-installed into a throwaway home and published
as the GitHub release product-<bundle id prefix>, which `tare.cmd auto-update` installs on every machine. The release
carries published.json (the heads it was built from), which the next run reads to tell whether anything packaged
changed.

  python scripts/ops/publish_product.py [--state DIR] [--force] [--commit SHA] [--state-from-release]
                                        [--bundle-out PATH] [--draft] [--here]

With --draft the release stays a draft until the workflow's `sign` job has signed it (ADR-075).
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import tomllib
import venv

OWNER = 'augusto-scarvalho'
REPOS = {'runtime': 'tare.tools.agent-runtime', 'kernel': 'tare.tools.kernel', 'specgraph': 'tare.tools.specgraph',
         'backlog': 'tare.tools.backlog-graph', 'os': 'tare.tools.os'}
TEMPLATE = 'scripts/ops/release_inputs/PRODUCT_RELEASE_INPUTS_WIN312_'
HERE = Path(__file__).resolve().parent
# The ripgrep every bundle carries for the agents' search tool. Same pin as the kernel's
# tools/install_test_ripgrep.py; tests/test_publish_product.py fails when the two differ.
RIPGREP_VERSION = '14.1.1'
MINGIT_VERSION = '2.56.0.4'
MINGIT_SHA256 = 'c4f27d9356ef471dd9fea1f89e5c176cfff7c85b29912a00f3cc7fb59197a39f'
RIPGREP = {'nt': ('ripgrep-14.1.1-x86_64-pc-windows-msvc.zip',
                  'd0f534024c42afd6cb4d38907c25cd2b249b79bbe6cc1dbee8e3e37c2b6e25a1', 'rg.exe'),
           'posix': ('ripgrep-14.1.1-x86_64-unknown-linux-musl.tar.gz',
                     '4cf9f2741e6c465ffdb7c26f38056a59e2a2544b51f7cc128ef28337eeae4d8e', 'rg')}


def fetch_ripgrep(destination, platform=None, opener=None):
    # Download the pinned archive, refuse it unless its SHA-256 matches, and extract only the executable.
    import io
    import tarfile
    import urllib.request
    import zipfile
    asset, digest, executable = RIPGREP[platform or os.name]
    url = f'https://github.com/BurntSushi/ripgrep/releases/download/{RIPGREP_VERSION}/{asset}'
    with (opener or urllib.request.urlopen)(url, timeout=60) as response:
        raw = response.read(8 * 1024 * 1024 + 1)
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError('ripgrep_archive_digest_mismatch: ' + asset)
    folder = asset.removesuffix('.zip').removesuffix('.tar.gz')
    if asset.endswith('.zip'):
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            binary = archive.read(f'{folder}/{executable}')
    else:
        with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
            binary = archive.extractfile(f'{folder}/{executable}').read()
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / executable
    target.write_bytes(binary)
    target.chmod(0o755)
    return target


def fetch_mingit(destination, opener=None):
    """Official native BusyBox MinGit; no MSYS shell or machine-wide install."""
    import io
    import stat
    import urllib.request
    import zipfile
    url = ('https://github.com/git-for-windows/git/releases/download/v2.56.0.windows.4/'
           f'MinGit-{MINGIT_VERSION}-busybox-64-bit.zip')
    with (opener or urllib.request.urlopen)(url, timeout=60) as response:
        raw = response.read(48 * 1024 * 1024 + 1)
    if hashlib.sha256(raw).hexdigest() != MINGIT_SHA256:
        raise ValueError('mingit_archive_digest_mismatch')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        for item in archive.infolist():
            path = Path(item.filename)
            if (path.is_absolute() or '..' in path.parts or ':' in item.filename
                    or '\\' in item.filename or stat.S_ISLNK(item.external_attr >> 16)):
                raise ValueError('mingit_archive_unsafe_path')
        destination.mkdir(parents=True, exist_ok=False)
        archive.extractall(destination)
    # Git locates `sh` on PATH. The official native applet stub ships as ash;
    # give that same stub its sh name instead of invoking busybox without an applet.
    shutil.copyfile(destination/'ucrt64/bin/ash.exe', destination/'ucrt64/bin/sh.exe')
    (destination/'tare-source.json').write_text(json.dumps({
        'archive':url, 'sha256':MINGIT_SHA256,
        'source':'https://github.com/git-for-windows/git/tree/v2.56.0.windows.4',
        'native_shell':'https://github.com/git-for-windows/busybox-w32',
        'adaptation':'ucrt64/bin/sh.exe is a copy of the supplied native ash.exe applet stub'},
        indent=2)+'\n', encoding='utf-8')
    return destination


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args])


def mirror(state, source):
    """A bare mirror of the source's GitHub repository, fetched now: (path, main commit)."""
    path = state/'mirrors'/f'{source}.git'
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(['git', 'clone', '--quiet', '--mirror', f'https://github.com/{OWNER}/{REPOS[source]}.git',
                        str(path)], check=True)
    else:
        git(path, 'fetch', '--quiet', '--prune', 'origin')
    return path, git(path, 'rev-parse', 'refs/heads/main').decode().strip()


def lock_for(mirrors, heads):
    """The OS main's newest release template, resolved to OS main and its qualified satellite pins."""
    os_mirror = mirrors['os']
    names = [n for n in git(os_mirror, 'ls-tree', '--name-only', heads['os'], 'scripts/ops/release_inputs/').decode().split('\n')
             if n.startswith(TEMPLATE) and n.endswith('.json')]
    template = json.loads(git(os_mirror, 'show', f"{heads['os']}:{max(names)}"))
    sources = []
    for source in template['sources']:
        source_id = source['id']
        commit = heads['os']
        if source_id != 'os':
            pin_path = f'satellites/{REPOS[source_id]}'
            entry = git(os_mirror, 'ls-tree', heads['os'], '--', pin_path).decode().split()
            if not entry:
                raise ValueError(f'missing_satellite_pin: {source_id} ({pin_path})')
            if (len(entry) != 4 or entry[:2] != ['160000', 'commit']
                    or not re.fullmatch(r'[0-9a-f]{40}', entry[2]) or entry[3] != pin_path):
                raise ValueError(f'invalid_satellite_pin: {source_id} ({pin_path}); expected 160000 commit fullsha40')
            commit = entry[2]
            try:
                object_type = git(mirrors[source_id], 'cat-file', '-t', commit).decode().strip()
            except subprocess.CalledProcessError as exc:
                raise ValueError(f'unavailable_satellite_pin: {source_id} {commit}') from exc
            if object_type != 'commit':
                raise ValueError(f'invalid_satellite_pin_object: {source_id} {commit}; expected commit')
        project = tomllib.loads(git(mirrors[source_id], 'show', f'{commit}:pyproject.toml').decode())['project']
        sources.append({**source, 'commit': commit, 'version': project['version']})
    epoch = max(int(git(mirrors[s['id']], 'show', '-s', '--format=%ct', s['commit']).decode()) for s in sources)
    return {**template, 'sources': sources, 'source_date_epoch': epoch}


def packaged_changes(mirrors, previous, lock):
    """Changed package inputs, including rules that select the exported source."""
    import importlib.util
    spec = importlib.util.spec_from_file_location('build_native_release', HERE/'build_native_release.py')
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    changed = []
    for source in lock['sources']:
        before = previous.get(source['id'])
        if before == source['commit']:
            continue
        names = git(mirrors[source['id']], 'diff', '--name-only', before, source['commit']).decode().split() \
            if before else ['pyproject.toml']
        changed += [f"{source['id']}:{n}" for n in names
                    if builder.selected(n, source['include']) or
                    (source['id'] == 'os' and n.startswith(TEMPLATE) and n.endswith('.json'))]
    return changed


def build_python(state, tools):
    """A venv holding exactly the lock's build tools (reused while they stay the same)."""
    home = state/'build-venv'
    python = home/('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    marker = home/'tools.json'
    if not python.exists() or not marker.exists() or json.loads(marker.read_text()) != tools:
        shutil.rmtree(home, ignore_errors=True)
        venv.EnvBuilder(with_pip=True).create(home)
        subprocess.run([str(python), '-m', 'pip', 'install', '--quiet', *(f'{k}=={v}' for k, v in tools.items())],
                       check=True)
        marker.write_text(json.dumps(tools))
    return python


def notify(peers):
    """Request each node's own updater; True means accepted, not rotation complete."""
    try:
        started = {'local': subprocess.run(['schtasks', '/Run', '/TN', 'Tare-Update'],
                                          capture_output=True, timeout=60).returncode == 0}
    except (OSError, subprocess.TimeoutExpired):
        started = {'local': False}
    for peer in peers:
        try:
            started[peer] = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', peer,
                                            'schtasks /Run /TN Tare-Update'], capture_output=True,
                                           timeout=60).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            started[peer] = False  # an offline machine picks the release up at its next check
    return started


def refresh_tools(state, *, force=False, peers=()):
    """Run the publisher/packers from one main commit, never a dirty checkout."""
    source, commit = mirror(state, 'os')
    tools = state/'tools'; tools.mkdir(parents=True, exist_ok=True)
    for name in ('publish_product.py','build_native_release.py','pack_native_release.py'):
        temporary = tools/(name+'.new')
        temporary.write_bytes(git(source, 'show', f'{commit}:scripts/ops/{name}'))
        os.replace(temporary, tools/name)
    return subprocess.call([sys.executable,str(tools/'publish_product.py'),
        '--state',str(state),'--tooling-commit',commit,*(['--force'] if force else []),
        *[arg for peer in peers for arg in ('--peer',peer)]])


CI_PASSED = 'CI passed on main: '  # installed copies take only releases whose notes carry it (native_product.py)


def ci_passed(commit):
    """The URL of a successful tare.tools CI run of this OS commit on main; None while it runs, failed or never ran."""
    runs = json.loads(subprocess.run(['gh', 'run', 'list', '--repo', f"{OWNER}/{REPOS['os']}", '--commit', commit,
                                      '--workflow', 'ci.yml', '--event', 'push',  # a pull request's branch may be named main too
                                      '--json', 'conclusion,headBranch,url'],
                                     check=True, capture_output=True, text=True, timeout=120).stdout or '[]')
    return next((run['url'] for run in runs
                 if run.get('conclusion') == 'success' and run.get('headBranch') == 'main'), None)


def last_release(state):
    """The newest product release's published.json (the heads it was built from) into the state folder, where
    publish() compares with it; CI starts with an empty folder. A release without one leaves nothing to compare."""
    listed = json.loads(subprocess.run(['gh', 'release', 'list', '--repo', f"{OWNER}/{REPOS['os']}", '--limit', '30',
                                        '--exclude-drafts',  # an unsigned draft is not "the newest release"
                                        '--json', 'tagName,publishedAt'], check=True, capture_output=True,
                                       text=True, timeout=120).stdout or '[]')
    newest = max((row for row in listed if row['tagName'].startswith('product-')),
                 key=lambda row: row['publishedAt'], default=None)
    if newest:
        subprocess.run(['gh', 'release', 'download', newest['tagName'], '--repo', f"{OWNER}/{REPOS['os']}",
                        '--pattern', 'published.json', '--dir', str(state), '--clobber'], capture_output=True)
    return newest and newest['tagName']


def delete_draft_products():
    """Drafts left by a run whose signing failed would hold the tag of the next one; published releases stay."""
    repo = f"{OWNER}/{REPOS['os']}"
    listed = json.loads(subprocess.run(['gh', 'release', 'list', '--repo', repo, '--limit', '30',
                                        '--json', 'tagName,isDraft'], check=True, capture_output=True,
                                       text=True, timeout=120).stdout or '[]')
    for row in listed:
        if row['isDraft'] and row['tagName'].startswith('product-'):
            subprocess.run(['gh', 'release', 'delete', row['tagName'], '--repo', repo, '--yes'], check=True,
                           capture_output=True, timeout=120)


def publish(state, *, force=False, peers=(), tooling_commit=None, commit=None, bundle_out=None, draft=False):
    started = time.monotonic()
    if draft:
        delete_draft_products()
    mirrors, remote_heads = {}, {}
    for source in REPOS:
        mirrors[source], remote_heads[source] = mirror(state, source)
    if commit:  # CI publishes the commit it tested, which main may have moved past since
        if subprocess.run(['git', '-C', str(mirrors['os']), 'merge-base', '--is-ancestor', commit,
                           'refs/heads/main']).returncode:
            raise ValueError(f'{commit} is not on main; only a commit of main is published.')
        remote_heads['os'] = commit
    lock = lock_for(mirrors, remote_heads)
    heads = {source['id']: source['commit'] for source in lock['sources']}
    if tooling_commit is not None and heads['os'] != tooling_commit:
        raise ValueError('Publisher source moved while selecting tools; retry on the next scheduled check.')
    published = state/'published.json'
    last = json.loads(published.read_text(encoding='utf-8')) if published.exists() else {}
    newest = last.get('heads', {}).get('os')
    if commit and newest and newest != commit and subprocess.run(
            ['git', '-C', str(mirrors['os']), 'merge-base', '--is-ancestor', newest, commit]).returncode:
        # CI runs on main can finish out of order: never publish a commit older than the newest release's.
        return {'status': 'older than the newest release', 'commit': commit, 'tag': last.get('tag')}
    if last.get('heads') == heads and not force:
        return {'status': 'unchanged', 'tag': last.get('tag')}
    if last.get('heads') and not force and not packaged_changes(mirrors, last['heads'], lock):
        # Hooks, docs and tests are not in the product: note the heads, keep the release and the machines as they are.
        published.write_text(json.dumps({**last, 'heads': heads}, indent=2), encoding='utf-8')
        return {'status': 'no packaged change', 'tag': last.get('tag')}
    passed = ci_passed(heads['os'])
    if not passed:
        # A release goes out only for a commit CI passed on main, so installed copies need not ask GitHub about CI
        # (unauthenticated, they may make 60 API calls an hour). The heads stay unrecorded: the next run checks again.
        return {'status': 'waiting for CI', 'commit': heads['os'], 'tag': last.get('tag')}
    work = state/'work'
    shutil.rmtree(work, ignore_errors=True)  # one build at a time; nothing of an earlier one is kept
    work.mkdir(parents=True)
    (work/'inputs.json').write_text(json.dumps(lock, indent=2), encoding='utf-8')
    (work/'roots.json').write_text(json.dumps({s: str(p) for s, p in mirrors.items()}), encoding='utf-8')
    python = build_python(state, lock['build_tools'])
    subprocess.run([str(python), str(HERE/'build_native_release.py'), '--lock', str(work/'inputs.json'),
                    '--roots', str(work/'roots.json'), '--output', str(work/'release')], check=True)
    # ponytail: two builds prove reproducibility; one saved only ~4 s of ~110 (packing and the trial install dominate)
    installer = work/'install.py'  # the release's own installer, from the commit being published
    installer.write_bytes(git(mirrors['os'], 'show', f"{heads['os']}:relay/native_product.py"))
    ripgrep = fetch_ripgrep(work/'ripgrep')  # pinned and hash-checked; the bundle carries it to every machine
    tools = [ripgrep]
    if os.name == 'nt': tools.append(fetch_mingit(work/'git'))
    subprocess.run([str(python), str(HERE/'pack_native_release.py'), '--release', str(work/'release'),
                    '--output', str(work/'bundle'), '--installer', str(installer),
                    *[arg for tool in tools for arg in ('--tool',str(tool))]], check=True)
    trial = work/'trial-home'  # the bundle must install exactly as a user machine would install it
    subprocess.run([sys.executable, str(work/'bundle/install.py'), '--home', str(trial), 'install', str(work/'bundle')],
                   check=True, stdout=subprocess.DEVNULL)
    subprocess.run([str(work/'bundle'/'tools'/ripgrep.name), '--version'], check=True, stdout=subprocess.DEVNULL)
    shutil.rmtree(trial, ignore_errors=True)
    identifier = hashlib.sha256((work/'bundle/bundle.json').read_bytes()).hexdigest()
    archive = shutil.make_archive(str(work/'bundle'), 'zip', work/'bundle')
    tag = 'product-' + identifier[:16]
    if bundle_out:
        Path(bundle_out).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(archive, bundle_out)
    heads_file = work/'published.json'  # read back by the next run (last_release)
    heads_file.write_text(json.dumps({'heads': heads, 'tag': tag, 'bundle': identifier}, indent=2), encoding='utf-8')
    versions = ', '.join(f"{s['package']} {s['version']} ({s['commit'][:7]})" for s in lock['sources'])
    subprocess.run(['gh', 'release', 'create', tag, archive, str(heads_file), '--repo', f"{OWNER}/{REPOS['os']}",
                    '--title', f"tare.tools {next(s['version'] for s in lock['sources'] if s['id'] == 'os')}",
                    '--notes', 'Product bundle for Python 3.12 on Windows: ' + versions + '\n\n' + CI_PASSED + passed,
                    *(['--draft'] if draft else [])],
                   check=True)
    record = {'heads': heads, 'tag': tag, 'bundle': identifier,
              'published': datetime.now(timezone.utc).isoformat(timespec='seconds'),
              'seconds': round(time.monotonic() - started), **({'draft': True} if draft else {})}
    published.write_text(json.dumps(record, indent=2), encoding='utf-8')
    shutil.rmtree(work, ignore_errors=True)
    return {'status': 'published', **record, 'updates_started': notify(peers)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--state', type=Path,
                        default=Path(os.environ.get('LOCALAPPDATA', Path.home()))/'tare.tools'/'publisher')
    parser.add_argument('--force', action='store_true', help='publish even when no qualified source moved')
    parser.add_argument('--latest-tools', action='store_true', help='run the committed main publisher and packers')
    parser.add_argument('--tooling-commit', help=argparse.SUPPRESS)
    parser.add_argument('--peer', action='append', default=[], help='user@host whose Tare-Update starts over ssh '
                        'right after a release is published; repeat')
    parser.add_argument('--commit', help='publish this OS commit of main (the one CI tested) instead of main itself')
    parser.add_argument('--state-from-release', action='store_true',
                        help="first read the newest release's published.json (CI starts with an empty state)")
    parser.add_argument('--bundle-out', type=Path, help='also copy the published bundle.zip here (provenance)')
    parser.add_argument('--draft', action='store_true',
                        help='create the release as a draft, and first delete leftover product-* drafts')
    parser.add_argument('--here', action='store_true', help='publish from this machine; CI normally does')
    args = parser.parse_args(argv)
    if not os.environ.get('GITHUB_ACTIONS') and not args.here:
        # The maintainers' scheduled task runs main's publisher: it stops publishing once CI does.
        print(json.dumps({'status': 'skipped: CI publishes releases (.github/workflows/release.yml)'}), flush=True)
        return 0
    if args.state_from_release:
        args.state.mkdir(parents=True, exist_ok=True)
        last_release(args.state.resolve())
    if args.latest_tools:
        return refresh_tools(args.state.resolve(),force=args.force,peers=args.peer)
    result = publish(args.state.resolve(), force=args.force, peers=args.peer, tooling_commit=args.tooling_commit,
                     commit=args.commit, bundle_out=args.bundle_out, draft=args.draft)
    print(json.dumps(result, indent=2), flush=True)
    if os.environ.get('GITHUB_OUTPUT'):  # the workflow's `sign` job runs only for a release this run created
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as outputs:
            outputs.write(f"published=true\ntag={result['tag']}\n" if result['status'] == 'published'
                          else 'published=false\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
