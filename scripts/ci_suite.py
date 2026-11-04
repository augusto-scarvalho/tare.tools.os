"""CI selection and timed pytest phases, using only the standard library.

Pushes and pull requests run the tests that ran the changed code: `scripts/test_map.py` records, per test
file, the repository files whose functions it ran, and CI keeps that map in the Actions cache. The
nightly schedule (and a manual dispatch) runs everything and refreshes the map; without a fresh map, or
when a change touches a gate or shared setup, the whole suite runs.

CI splits each platform's run into shards on separate runners: `slice` gives each shard a share of the plan's
test files balanced by their measured seconds, and `merge` folds the shards' records into the map, the seconds
and one verdict.
"""
import argparse
import ast
import json
import os
from pathlib import Path, PurePosixPath
import random
import re
import subprocess
import sys
import time
import warnings
import xml.etree.ElementTree as ET


QUICK = [
    'tests/test_ci_suite.py',
    'tests/test_disposition_manifest_compliance.py',
    'tests/test_native_agent.py',
    'tests/test_native_display.py',
    'tests/test_native_mcp.py',
    'tests/test_native_model_listing.py',
    'tests/test_native_release_package_data.py',
    'tests/test_native_simple_start.py',
    'tests/test_project_admission.py',
    'tests/test_project_admission_ui.py',
    'tests/test_route_strategy.py',
    'tests/test_snapshot_preparation.py',
]
# Whole-repository scans: no import or execution ties them to one module, so any code change runs them.
SCANS = ['tests/test_turn_capabilities.py']
# Gates, dependencies and shared setup: any change runs everything.
FULL = ('.github/', '.gitmodules', 'scripts/ci_suite.py', 'scripts/test_map.py')
# A satellite pin (satellites/<name>) is expanded into the files its new commit changed inside the satellite,
# which then follow the rules of the OS's own code; an added or removed satellite, one the map never recorded,
# or a change that cannot be read runs everything.
SATELLITES = 'satellites'
# The federation test checks the pins against both READMEs and .gitmodules: a changed pin runs it even in a narrow
# plan. A pin bump without the READMEs passed a scoped run and left the full suite red (2026-10-06).
FEDERATION = 'tests/test_federation_submodules.py'
MAP_MAX_AGE = 7 * 86400
SKIP_DIRS = {'satellites', 'side_projects', 'scratch', 'node_modules'}
PATHLIKE = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_-]*(?:[./][A-Za-z0-9_][A-Za-z0-9_-]*)+')
# Console scripts a test may spawn by name.
SCRIPTS = {'tare': 'native_installation', 'tare-ops': 'native_installation'}


def documentation(path):
    p = PurePosixPath(path)
    return (path in {'README.md', 'README.pt-BR.md', 'CHANGELOG.md'} or
            p.parent in (PurePosixPath('docs/operations'), PurePosixPath('docs/guides')) and p.suffix == '.md')


def python_files(root):
    for path in root.rglob('*.py'):
        parts = path.relative_to(root).parts
        if not (parts[0].startswith('.') or SKIP_DIRS & set(parts) or parts[:2] == ('relay', 'history')):
            yield path.relative_to(root).as_posix(), path


def references(tree):
    """Names one file can reach another by: import parts and names, module/file paths in string literals.
    Names from literals are also kept with a leading '"': only they can reach code another process runs."""
    found, literal = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            found.update((node.module or '').split('.'))
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            found.update(part for alias in node.names for part in alias.name.split('.'))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value
            literal.update([value, value.removeprefix('tests.').removeprefix('tests/').removesuffix('.py')])
            for match in PATHLIKE.findall(value):
                literal.update(re.split('[./]', match))
            if value in SCRIPTS:
                literal.add(SCRIPTS[value])
    found |= literal
    found.update('"'+name for name in literal)
    found -= {'', '"'}
    return found


def satellite_files(root, satellite):
    """A checked-out satellite's own code (its tests and conftest left out: they never run in the OS's CI)."""
    base = root/satellite
    if not base.is_dir():
        return
    for path in base.rglob('*.py'):
        parts = path.relative_to(base).parts
        if (parts[0].startswith('.') or {'tests', 'test', 'node_modules'} & set(parts[:-1])
                or parts[-1].startswith('test_') or parts[-1] == 'conftest.py'):
            continue
        yield path.relative_to(root).as_posix(), path


def consumers_of(root, satellites=()):
    """Reference -> the repository's Python files using it (tests and code), plus the code of the given
    satellites (satellites/<name>), so importers inside a changed satellite show up by name too."""
    consumers = {}
    files = [*python_files(root), *(item for satellite in satellites for item in satellite_files(root, satellite))]
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', SyntaxWarning)  # invalid escapes in old sources are not our signal
        for relative, path in files:
            for reference in references(ast.parse(path.read_text(encoding='utf-8-sig'))):
                consumers.setdefault(reference, set()).add(relative)
    return consumers


def is_test(path):
    p = PurePosixPath(path)
    return p.parts[0] == 'tests' and p.name.startswith('test_') and p.suffix == '.py'


def affected_tests(consumers, changed):
    """Changed test modules and helpers plus the test modules importing them, transitively."""
    affected, pending = set(changed), list(changed)
    while pending:
        path = pending.pop()
        users = {c for c in consumers.get(PurePosixPath(path).stem, set()) if c.startswith('tests/')}
        for consumer in users - affected:
            affected.add(consumer)
            pending.append(consumer)
    if any(PurePosixPath(path).name == 'conftest.py' for path in affected):
        raise ValueError('shared_conftest_dependency')
    return {path for path in affected if is_test(path)}


def shape(source):
    """A module besides its functions: module and class statements (constants, class headers, module code;
    docstrings left out), and its module- and class-level imports."""
    statements, imports = [], []
    def walk(body, prefix):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                imports.append(prefix+ast.dump(node))
            elif isinstance(node, ast.ClassDef):
                statements.append(prefix+ast.dump(ast.ClassDef(
                    name=node.name, bases=node.bases, keywords=node.keywords, body=[],
                    decorator_list=node.decorator_list, type_params=getattr(node, 'type_params', []))))
                walk(node.body, prefix+node.name+'.')
            elif not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                      and isinstance(node.value.value, str)):
                statements.append(prefix+ast.dump(node))
    walk(ast.parse(source).body, '')
    return statements, sorted(imports)


def definitions(source, header=False):
    """Qualified name (as code objects name it: Class.method, f.<locals>.g) -> its definitions' AST
    (only name, arguments and decorators when `header`)."""
    found = {}
    def visit(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                dump = ast.dump(child)
                if header:
                    body, child.body = child.body, []
                    dump, child.body = ast.dump(child), body
                found.setdefault(prefix+child.name, []).append(dump)
                visit(child, prefix+child.name+'.<locals>.')
            elif isinstance(child, ast.ClassDef):
                visit(child, prefix+child.name+'.')
            else:
                visit(child, prefix)
    visit(ast.parse(source), '')
    return found


def changed_functions(previous, path, root):
    """(functions whose definitions changed, appeared or disappeared; those whose name, arguments or decorators
    did; whether imports changed), or None when module or class statements changed (constants, class headers,
    module code), the old version is unknown or either side does not parse."""
    old = previous(path) if previous else None
    try:
        new = (root/path).read_bytes()
        if old is None:
            return None
        (old_statements, old_imports), (new_statements, new_imports) = shape(old), shape(new)
        if old_statements != new_statements:
            return None
        changed = lambda a, b: {name for name in a.keys() | b.keys() if a.get(name) != b.get(name)}
        return (changed(definitions(old), definitions(new)),
                changed(definitions(old, True), definitions(new, True)), old_imports != new_imports)
    except (OSError, SyntaxError, ValueError):
        return None


def version_only(previous, path, root):
    old = previous(path) if previous else None
    strip = lambda raw: re.sub(rb'(?m)^version = .*$', b'', raw.replace(b'\r\n', b'\n'))  # CRLF checkouts
    try:
        return old is not None and strip(old) == strip((root/path).read_bytes())
    except OSError:
        return False


def load_map(path, now=None):
    """The runner's map, or None when missing, unreadable or older than MAP_MAX_AGE."""
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8'))
        fresh = (now or time.time()) - value['full_at'] <= MAP_MAX_AGE
        return value if fresh and isinstance(value['tests'], dict) else None
    except (OSError, TypeError, ValueError, KeyError):
        return None


def ran_by(test_map):
    """Repository file, and `file::function`, -> the test modules whose tests ran it."""
    users = {}
    for test, entries in test_map['tests'].items():
        for entry in entries:
            for key in {entry, entry.split('::', 1)[0]}:
                users.setdefault(key, set()).add(test)
    return users


def satellite_pin(path):
    p = PurePosixPath(path)
    return p.parts[0] == SATELLITES and len(p.parts) == 2


def pin_reason(path, status, record, runs):
    """Why a changed pin runs everything, as far as its record and the map tell, or None."""
    if status != 'M':
        return 'satellite_added_or_removed'
    if 'reason' in record or 'changed_files' not in record:
        return record.get('reason', 'satellite_change_unavailable')
    if runs is None:
        return 'test_map_unavailable'
    if not any(key.startswith(path+'/') for key in runs):
        return 'satellite_not_in_test_map'
    files = record['changed_files']
    if any(kind not in {'A', 'M'} for kind in files.values()):
        return 'satellite_file_deleted_or_renamed'
    if any(PurePosixPath(name).name == 'pyproject.toml' for name in files):
        return 'satellite_packaging_change'
    return None


def users_of(consumers, path):
    """The repository's Python files reaching a file: a data file is read by name or, inside its own folder,
    by the folder's name."""
    p = PurePosixPath(path)
    names = [p.stem] if p.suffix == '.py' or consumers.get(p.stem) or len(p.parts) < 3 else [p.parent.name]
    return {c for name in names for c in consumers.get(name, set())} - {path}


def satellite_internal(path, users):
    """A satellite's prose, or its own tests that no OS code reaches: no OS test runs them."""
    inner = PurePosixPath(*PurePosixPath(path).parts[2:])
    own_test = ({'tests', 'test'} & set(inner.parts[:-1]) or inner.name.startswith('test_')
                or inner.name == 'conftest.py')
    return inner.suffix == '.md' or (not users and bool(own_test))


def plan(root, changes, *, force_full=False, test_map=None, previous=None, satellites=None):
    # satellites: satellites/<name> -> {'old_pin', 'new_pin', 'changed_files': {file: status}}, or a record
    # with a 'reason' when the change inside it cannot be read (satellite_changes).
    quick = list(QUICK)
    if any(satellite_pin(path) for path in changes or {}):
        quick.append(FEDERATION)
    runs = ran_by(test_map) if test_map else None
    # Every changed pin is reported (its pins, changed files and, when it runs everything, why) before anything
    # returns: a plan that runs everything for one reason still shows each satellite's own.
    report = {}
    for path, status in (changes or {}).items():
        if satellite_pin(path):
            record = report[path] = dict((satellites or {}).get(path) or {})
            reason = pin_reason(path, status, record, runs)
            if reason:
                record['reason'] = reason
    result = {'scope': 'full', 'reason': 'unknown_or_shared_impact', 'changed_paths': sorted(changes or {}),
              'quick': quick, 'selected': [], 'satellites': report}
    if force_full or changes is None or not changes:
        result['reason'] = 'explicit_full_or_unavailable_diff'
        return result
    try:
        # The changed satellites' own code is indexed too, so importers inside a satellite show up by name,
        # as the OS's own importers do.
        consumers = consumers_of(root, sorted(path for path, record in report.items() if 'reason' not in record))
    except (OSError, SyntaxError, UnicodeError):
        # Invalid syntax anywhere must not produce a narrow plan.
        return {**result, 'reason': 'test_dependencies_unavailable'}
    for path, record in report.items():
        if 'reason' in record:
            continue
        for name in record['changed_files']:
            file = path+'/'+name
            users = users_of(consumers, file)
            if PurePosixPath(name).suffix != '.py' and not users and not satellite_internal(file, users):
                record['reason'] = 'unreferenced_file'
                break
    failed = [report[path]['reason'] for path in sorted(report) if 'reason' in report[path]]
    if failed:
        return {**result, 'reason': failed[0]}
    selected, changed_tests, code, items = set(), set(), bool(report), []
    for path, status in changes.items():
        # A satellite pin becomes the files its new commit changed inside the satellite, as
        # (satellites/<name>/<file>, status, satellites/<name>); the OS's own files are (path, status, None).
        # They follow the rules of the OS's own code.
        if PurePosixPath(path).parts[0] != SATELLITES:
            items.append((path, status, None))
        elif path in report:
            items += [(path+'/'+name, kind, path) for name, kind in report[path]['changed_files'].items()]
        else:
            return result  # not a pin: the OS keeps no files of its own under satellites/
    for path, status, satellite in items:
        p = PurePosixPath(path)
        if p.parts[0] == 'node':
            continue  # the node service imports nothing of the OS and runs its own tests (node.yml)
        if status not in {'A', 'M'}:
            return {**result, 'reason': 'deletion_rename_or_type_change'}
        if documentation(path):
            continue
        code = True
        if path == 'pyproject.toml' and not version_only(previous, path, root):
            return {**result, 'reason': 'dependency_or_packaging_change'}
        if not satellite and (path.startswith(FULL) or p.name == 'conftest.py' or p.suffix == '.md'):
            return result
        if p.parts[0] == 'tests' and p.suffix == '.py':
            changed_tests.add(path)
            selected |= runs.get(path, set()) if runs else set()
            continue
        if runs is None:
            return {**result, 'reason': 'test_map_unavailable'}
        users = users_of(consumers, path)
        if satellite and satellite_internal(path, users):
            continue
        if p.suffix != '.py' and not users:
            return {**result, 'reason': 'unreferenced_file'}
        functions = changed_functions(previous, path, root) if p.suffix == '.py' and status == 'M' else None
        if p.suffix == '.py' and status == 'A':
            # A new module: whatever uses it changed too and is selected as a change of its own. Inside a
            # satellite the tests that ran its importers are added: the satellite's own tests do not run here.
            reached = {c for c in users if is_test(c)}
            if satellite:
                reached |= {test for user in users for test in runs.get(user, set())}
        elif functions is not None and test_map.get('functions'):
            bodies, headers, imports = functions
            # Only function definitions changed (bodies, signatures, added or removed functions): a caller ran
            # the function it calls, so the tests that ran them see it. So do tests naming this module in a
            # string (`python -m`, a path), run by another process the map cannot see.
            reached = {c for c in consumers.get('"'+p.stem, set()) if is_test(c)}
            reached |= {test for name in bodies for test in runs.get(path+'::'+name, set())}
            for name in headers:
                # A changed name or signature also reaches what names the function without running it: an
                # import of a module-level function (and the tests running the importing module), a
                # monkeypatch target in a string.
                named = consumers.get(name if '.' not in name else '"'+name.rsplit('.', 1)[-1], set()) - {path}
                if any(PurePosixPath(c).name == 'conftest.py' for c in named):
                    return {**result, 'reason': 'shared_conftest_dependency'}
                reached |= {c for c in named if is_test(c)}
                reached |= {test for c in named if not c.startswith('tests/') for test in runs.get(c, set())}
            if imports:  # an import-time failure reaches every test importing the module
                reached |= runs.get(path, set())
        else:
            reached = {c for c in users if is_test(c)} | runs.get(path, set())
            if functions is None:
                # Importers (or readers of a data file) see this change, so the tests running them do too.
                for user in users:
                    reached |= runs.get(user, set())
        selected |= reached
        if p.suffix == '.py':
            selected.update(SCANS)
    if changed_tests:
        try:
            for path in changed_tests:
                ast.parse((root/path).read_text(encoding='utf-8-sig'))
            selected |= affected_tests(consumers, changed_tests)
        except (OSError, SyntaxError, UnicodeError, ValueError):
            return {**result, 'reason': 'test_dependencies_unavailable_or_global'}
    if not code:
        return {**result, 'scope': 'docs', 'reason': 'operator_documentation_only', 'quick': []}
    return {**result, 'scope': 'focused', 'reason': 'tests_that_ran_or_reference_the_change',
            'selected': sorted(path for path in selected - set(quick) if (root/path).is_file())}


def changed_paths(base, head='HEAD'):
    if not base or set(base) == {'0'}:
        return None
    try:
        raw = subprocess.check_output(['git', 'diff', '--name-status', '-z', '--no-renames',
                                       base, head, '--'], stderr=subprocess.DEVNULL)
        fields = raw.decode('utf-8').split('\0')
        return dict(zip(fields[1::2], fields[0:-1:2]))
    except (subprocess.CalledProcessError, UnicodeError):
        return None


def base_bytes(base, path):
    try:
        return subprocess.check_output(['git', 'show', base+':'+path], stderr=subprocess.DEVNULL)
    except (subprocess.CalledProcessError, OSError):
        return None


def verified_path(map_path):
    return Path(map_path).with_name(Path(map_path).stem+'-verified.json')


def last_verified(map_path, branch):
    """This branch's newest commit whose regression passed on this runner, when HEAD descends from it."""
    try:
        sha = json.loads(verified_path(map_path).read_text(encoding='utf-8'))[branch]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    ancestor = subprocess.run(['git', 'merge-base', '--is-ancestor', sha, 'HEAD'], stderr=subprocess.DEVNULL)
    return sha if ancestor.returncode == 0 else None


def mark_verified(map_path, head):
    branch = os.environ.get('GITHUB_REF_NAME')
    if not branch or os.environ.get('GITHUB_EVENT_NAME') == 'pull_request':
        return
    path = verified_path(map_path)
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        value = {}
    value = value if isinstance(value, dict) else {}
    value[branch] = head
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True)+'\n', encoding='utf-8')


def satellite_changes(base, changes):
    """satellites/<name> whose pin changed -> {'old_pin', 'new_pin', 'changed_files': {file: status}} from the
    diff inside the checked-out satellite, or with the 'reason' that change cannot be read."""
    found = {}
    for path, status in (changes or {}).items():
        p = PurePosixPath(path)
        if not satellite_pin(path):
            continue
        record = found[path] = {}
        # An added satellite has only its new pin, a removed one only its old pin; both run everything
        # (plan), but the plan still reports the pins that exist.
        revisions = ([('old_pin', base)] if status != 'A' else []) + ([('new_pin', 'HEAD')] if status != 'D' else [])
        try:
            for key, revision in revisions:
                record[key] = subprocess.check_output(['git', 'rev-parse', revision+':'+path], text=True,
                                                      stderr=subprocess.DEVNULL).strip()
        except (subprocess.CalledProcessError, OSError, TypeError):
            if status == 'M':
                record['reason'] = 'satellite_pin_unavailable'
            continue
        if status != 'M':
            continue
        # An uninitialized submodule is an empty folder: git -C there would answer for the OS itself.
        if not (Path(path)/'.git').exists():
            record['reason'] = 'satellite_not_checked_out'
            continue
        try:
            checked = subprocess.check_output(['git', '-C', path, 'rev-parse', 'HEAD'], text=True,
                                              stderr=subprocess.DEVNULL).strip()
            if checked != record['new_pin']:
                record['reason'] = 'satellite_checkout_differs_from_pin'
                continue
            raw = subprocess.check_output(['git', '-C', path, 'diff', '--name-status', '-z', '--no-renames',
                                           record['old_pin'], record['new_pin'], '--'], stderr=subprocess.DEVNULL)
            fields = raw.decode('utf-8').split('\0')
            record['changed_files'] = dict(zip(fields[1::2], fields[0:-1:2]))
        except (subprocess.CalledProcessError, OSError, UnicodeError):
            record['reason'] = 'satellite_commit_or_diff_unavailable'
    return found


def previous_bytes(base, satellites):
    """The old version of a changed file: at the base commit, or, inside a satellite, at its old pin."""
    def previous(path):
        p = PurePosixPath(path)
        satellite = '/'.join(p.parts[:2])
        if p.parts[0] != SATELLITES or len(p.parts) < 3:
            return base_bytes(base, path)
        try:
            return subprocess.check_output(['git', '-C', satellite, 'show',
                                            satellites[satellite]['old_pin']+':'+'/'.join(p.parts[2:])],
                                           stderr=subprocess.DEVNULL)
        except (subprocess.CalledProcessError, OSError, KeyError):
            return None
    return previous


def tested_in_pull_request(head, api=None):
    """The pull request CI run that already tested exactly this merge commit's bytes (its URL), or None.

    main takes changes only through pull requests that must be up to date with it: when the merge commit's tree
    is its second parent's tree, main's previous commit is an ancestor of that parent and tare.tools CI passed on
    that parent in the pull request, the bytes on main are the bytes CI tested, and running it again only
    doubles the runners' load. Anything else (no token, no access, another shape) runs CI as before."""
    def git(*args):
        return subprocess.run(['git', *args], capture_output=True, text=True).stdout.strip()
    parents = git('rev-list', '--parents', '-n', '1', head).split()
    if len(parents) != 3 or git('rev-parse', head + '^{tree}') != git('rev-parse', parents[2] + '^{tree}'):
        return None
    if subprocess.run(['git', 'merge-base', '--is-ancestor', parents[1], parents[2]]).returncode != 0:
        return None
    if api is None:
        token, repo = os.environ.get('GITHUB_TOKEN'), os.environ.get('GITHUB_REPOSITORY')
        if not token or not repo:
            return None
        import urllib.request

        def api(path):
            request = urllib.request.Request('https://api.github.com/repos/' + repo + path, headers={
                'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json'})
            with urllib.request.urlopen(request, timeout=30) as reply:
                return json.load(reply)
    try:
        runs = api(f'/actions/runs?head_sha={parents[2]}&event=pull_request&status=success&per_page=50')
    except (OSError, ValueError):
        return None
    workflow = os.environ.get('GITHUB_WORKFLOW', 'tare.tools CI')
    for run in runs.get('workflow_runs', []):
        if run.get('name') == workflow and run.get('conclusion') == 'success' and run.get('head_sha') == parents[2]:
            return run.get('html_url') or str(run.get('id'))  # a merged pull request is no longer listed on it
    return None


def write_plan(output, map_path=None):
    event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text(encoding='utf-8')) if os.environ.get('GITHUB_EVENT_PATH') else {}
    name = os.environ.get('GITHUB_EVENT_NAME', '')
    base = event.get('pull_request', {}).get('base', {}).get('sha') or event.get('before')
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
    # A push is compared with the last commit whose regression passed here, not only the previous push:
    # a run that stopped early (a failed quick check) never ran the tests its own changes needed.
    verified = last_verified(map_path, os.environ.get('GITHUB_REF_NAME', '')) if name == 'push' and map_path else None
    if verified and verified != head:
        base = verified
    full = name not in {'push', 'pull_request'}
    changes = changed_paths(base)
    test_map = load_map(map_path) if map_path else None
    # Before the satellites are checked out their records say so and the pin runs everything; the plan
    # rerun after the checkout (ci.yml) reads them.
    satellites = {} if full else satellite_changes(base, changes)
    value = plan(Path.cwd(), changes, force_full=full, test_map=test_map,
                 previous=previous_bytes(base, satellites), satellites=satellites)
    value['test_map'] = {'path': str(map_path) if map_path else None, 'fresh': test_map is not None,
                         'full_head': (test_map or {}).get('full_head')}
    value['head'] = head
    value['base'] = base
    value['base_source'] = 'last_verified' if base == verified else 'event'
    value['comparison_available'] = changes is not None
    tested = tested_in_pull_request(head) if name == 'push' and os.environ.get('GITHUB_REF_NAME') == 'main' else None
    if tested:  # the same bytes passed CI in their pull request: no second run on main
        value.update(scope='docs', reason='tested_in_pull_request', tested_in=tested, quick=[], selected=[])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(value, indent=2)+'\n', encoding='utf-8')
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as stream:
            stream.write('scope='+value['scope']+'\n')
    print(json.dumps(value))


def test_files(root):
    """Every file pytest collects from tests/ (`python_files = test_*.py`), repository-relative."""
    return sorted(path.relative_to(root).as_posix() for path in Path(root, 'tests').rglob('test_*.py'))


def seconds_path(map_path):
    """Measured seconds per test file, kept next to the test map."""
    return Path(map_path).with_name(Path(map_path).stem+'-seconds.json')


def partition(files, shards, seconds):
    """Longest first onto the least loaded shard: `shards` sorted lists of similar measured time. A file never
    measured counts as the mean of the measured ones."""
    known = [seconds[name] for name in files if name in seconds]
    default = sum(known)/len(known) if known else 1.0
    loads, parts = [0.0]*shards, [[] for _ in range(shards)]
    for name in sorted(files, key=lambda name: (-seconds.get(name, default), name)):
        lightest = loads.index(min(loads))
        loads[lightest] += seconds.get(name, default)
        parts[lightest].append(name)
    return [sorted(part) for part in parts]


def shard_plan(value, shard, shards, root, map_path=None):
    """The quick and regression files of shard `shard` (1-based) of `shards`, from the plan."""
    try:
        seconds = json.loads(seconds_path(map_path).read_text(encoding='utf-8')) if map_path else {}
    except (OSError, ValueError):
        seconds = {}
    quick = [] if value['scope'] == 'docs' else list(value['quick'])
    regression = ([] if value['scope'] == 'docs' else
                  [name for name in test_files(root) if name not in quick] if value['scope'] == 'full' else
                  list(value['selected']))
    return {'shard': shard, 'shards': shards, 'quick': partition(quick, shards, seconds)[shard-1],
            'regression': partition(regression, shards, seconds)[shard-1]}


def pytest_args(value, phase, output, basetemp):
    if value['scope'] == 'docs':
        return []
    if (output/'shard.json').exists():  # a CI shard runs only its share (`slice`)
        targets = json.loads((output/'shard.json').read_text(encoding='utf-8'))[phase]
    elif phase == 'quick':
        targets = value['quick']
    elif value['scope'] == 'full':
        targets = ['tests/', *['--ignore='+path for path in value['quick']]]
    else:
        targets = value['selected']
    if not targets:
        return []
    return [sys.executable, '-B', '-m', 'pytest', *targets, '-q', '--durations=30',
            '--durations-min=0.1', '--junitxml='+str(output/(phase+'.xml')),
            '-p', 'no:cacheprovider', '-p', 'scripts.test_map', '--basetemp', str(basetemp)]


def remember(map_path, ran_path, value, phase, exit_code):
    """Fold one phase's record into the runner's map; a full regression that ran to the end marks it fresh."""
    try:
        ran = json.loads(ran_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return  # interrupted before the plugin wrote: nothing new is known
    try:
        current = json.loads(map_path.read_text(encoding='utf-8'))
        current['tests'].update(ran)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        current = {'tests': ran}
    if value['scope'] == 'full' and phase == 'regression' and exit_code in (0, 1):
        # The whole suite just ran: its own record (quick phase included) is the map.
        try:
            ran.update(json.loads(ran_path.with_name('quick-map.json').read_text(encoding='utf-8')))
        except (OSError, ValueError):
            pass
        current = {'tests': ran, 'full_head': value['head'], 'full_at': time.time(), 'functions': True}
    map_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = map_path.with_name(map_path.name+'.tmp')
    temporary.write_text(json.dumps(current, sort_keys=True)+'\n', encoding='utf-8')
    os.replace(temporary, map_path)


def run(value, phase, output, basetemp, map_path=None):
    args = pytest_args(value, phase, output, basetemp)
    if not args:
        print('No additional tests selected for '+phase+'; scope='+value['scope'])
        return 0
    output.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    ran = output/(phase+'-map.json')
    result = subprocess.run(args, env={**os.environ, 'TARE_TEST_MAP': str(ran)})
    (output/(phase+'.json')).write_text(json.dumps({'phase': phase,
        'elapsed_seconds': round(time.monotonic()-started, 3), 'exit_code': result.returncode,
        'scope': value['scope']}, indent=2)+'\n', encoding='utf-8')
    if map_path:
        remember(Path(map_path), ran, value, phase, result.returncode)
    return result.returncode


def junit_seconds(xml):
    """Seconds per test file from a JUnit report (`tests.test_x[.Class]` classnames)."""
    seconds = {}
    for case in ET.parse(xml).iter('testcase'):
        parts = case.get('classname', '').split('.')
        module = next((i for i, part in enumerate(parts) if part.startswith('test_')), None)
        if module is not None:
            name = '/'.join(parts[:module+1])+'.py'
            seconds[name] = seconds.get(name, 0.0)+float(case.get('time') or 0)
    return seconds


def merge(inputs, shards, output, map_path=None):
    """Fold the shards' outputs into one result: the test map (fresh only when every shard ran its whole share),
    the measured seconds per test file and a summary. Nonzero when a shard is missing, unfinished or red."""
    found, problems, value = {}, [], None
    # A folder of downloaded artifacts stands for the shard folders inside it.
    folders = [child for path in map(Path, inputs) for child in
               ([path] if (path/'shard.json').exists() or not path.is_dir() else sorted(path.iterdir()))]
    for folder in folders:
        try:
            part = json.loads((folder/'shard.json').read_text(encoding='utf-8'))
            value = value or json.loads((folder/'plan.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        found[part['shard']] = (folder, part)
    missing = sorted(set(range(1, shards+1))-set(found))
    if missing:
        problems.append('missing shard output: '+', '.join(map(str, missing)))
    lines = ['## CI shards', '', '| Shard | Phase | Files | Seconds | Exit |', '| ---: | --- | ---: | ---: | ---: |']
    records, codes, seconds, unfinished = {'quick': {}, 'regression': {}}, {'quick': [], 'regression': []}, {}, False
    for shard, (folder, part) in sorted(found.items()):
        for phase in ('quick', 'regression'):
            if not part[phase]:
                continue
            try:
                receipt = json.loads((folder/(phase+'.json')).read_text(encoding='utf-8'))
            except (OSError, ValueError):
                problems.append(f'shard {shard}: {phase} did not finish')
                unfinished = True
                lines.append(f'| {shard} | {phase} | {len(part[phase])} | Not run / interrupted | — |')
                continue
            codes[phase].append(receipt['exit_code'])
            lines.append(f"| {shard} | {phase} | {len(part[phase])} | {receipt['elapsed_seconds']} | {receipt['exit_code']} |")
            if receipt['exit_code']:
                problems.append(f"shard {shard}: {phase} exited {receipt['exit_code']}")
            try:
                records[phase].update(json.loads((folder/(phase+'-map.json')).read_text(encoding='utf-8')))
            except (OSError, ValueError):
                pass
            if (folder/(phase+'.xml')).exists():
                for name, spent in junit_seconds(folder/(phase+'.xml')).items():
                    seconds[name] = seconds.get(name, 0.0)+spent
    output.mkdir(parents=True, exist_ok=True)
    for phase in ('quick', 'regression'):  # remember() reads quick-map.json beside the regression record
        (output/(phase+'-map.json')).write_text(json.dumps(records[phase], sort_keys=True)+'\n', encoding='utf-8')
    if map_path and value and value['scope'] != 'docs':
        complete = not missing and not unfinished
        for phase in ('quick', 'regression'):
            if codes[phase] or records[phase]:
                remember(Path(map_path), output/(phase+'-map.json'), value, phase,
                         max(codes[phase], default=0) if complete else 2)
        if seconds:
            try:
                known = json.loads(seconds_path(map_path).read_text(encoding='utf-8'))
            except (OSError, ValueError):
                known = {}
            known.update({name: round(spent, 3) for name, spent in seconds.items()})
            seconds_path(map_path).parent.mkdir(parents=True, exist_ok=True)
            seconds_path(map_path).write_text(json.dumps(known, sort_keys=True)+'\n', encoding='utf-8')
        if complete and not problems:
            mark_verified(Path(map_path), value['head'])
    lines += ['', 'Scope: **'+(value['scope'] if value else 'unknown')+'**.', '']
    lines += ['Problems:', *('- '+problem for problem in problems)] if problems else ['Every shard passed.']
    text = '\n'.join(lines)+'\n'
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as stream:
            stream.write(text)
    print(text)
    return 1 if problems or value is None else 0


def failed_cases(xml):
    """Test ids that failed or errored in a JUnit report."""
    try:
        cases = list(ET.parse(xml).iter('testcase'))
    except (OSError, ET.ParseError):
        return []
    return [case.get('classname', '')+'::'+case.get('name', '') for case in cases
            if case.find('failure') is not None or case.find('error') is not None]


def isolate(output, basetemp, seed=None):
    """Find tests that pass only by luck of order or company: run each of this shard's test files alone, then all
    of them in a shuffled file order (pytest runs files in the order given). The seed is recorded so a shuffled
    failure can be replayed with `--seed`. The test map is neither read nor written."""
    part = json.loads((output/'shard.json').read_text(encoding='utf-8'))
    files = sorted({*part['quick'], *part['regression']})
    seed = random.randrange(2**32) if seed is None else seed
    folder = output/'isolation'
    folder.mkdir(parents=True, exist_ok=True)

    def pytest(targets, name):
        return subprocess.run([sys.executable, '-B', '-m', 'pytest', *targets, '-q', '-p', 'no:cacheprovider',
                               '--junitxml='+str(folder/(name+'.xml')), '--basetemp', str(Path(basetemp)/name)]
                              ).returncode
    alone = {name: pytest([name], f'alone-{index}') for index, name in enumerate(files)}
    order = random.Random(seed).sample(files, len(files))
    shuffled = pytest(order, 'shuffled') if order else 0
    passed = (0, 5)  # 5: nothing collected (every test of the file skipped on this platform)
    receipt = {'seed': seed, 'files': len(files),
               'alone_failed': {name: failed_cases(folder/f'alone-{index}.xml') or code
                                for index, (name, code) in enumerate(alone.items()) if code not in passed},
               'shuffled_exit_code': shuffled, 'shuffled_failed': failed_cases(folder/'shuffled.xml'),
               'shuffled_order': order}
    (folder/'isolation.json').write_text(json.dumps(receipt, indent=2)+'\n', encoding='utf-8')
    lines = ['## Test isolation', '', f"{len(files)} test files, each alone and then all in a shuffled order "
             f"(seed `{seed}`).", '']
    lines += [f'- alone: `{name}` failed: {cases}' for name, cases in receipt['alone_failed'].items()]
    lines += [f'- shuffled: `{case}` failed' for case in receipt['shuffled_failed']]
    if shuffled not in passed and not receipt['shuffled_failed']:
        lines.append(f'- shuffled: pytest exited {shuffled} without a failed case')
    if len(lines) == 4:
        lines.append('No test depends on order or company here.')
    text = '\n'.join(lines)+'\n'
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as stream:
            stream.write(text)
    print(text)
    return 0 if not receipt['alone_failed'] and shuffled in passed else 1


def summary(output):
    value = json.loads((output/'plan.json').read_text(encoding='utf-8'))
    lines = ['## CI validation', '', 'Scope: **'+value['scope']+'**; reason: `'+value['reason']+'`.',
             '', f"Selected test modules beyond the quick checks: {len(value.get('selected', []))}.",
             '', 'Revision: `'+value['head']+'`.', '', '| Phase | Seconds | Exit |', '| --- | ---: | ---: |']
    slow, cases = [], []
    for phase in ('quick', 'regression'):
        receipt = output/(phase+'.json')
        if receipt.exists():
            row = json.loads(receipt.read_text(encoding='utf-8'))
            lines.append(f"| {phase} | {row['elapsed_seconds']} | {row['exit_code']} |")
        else:
            lines.append(f'| {phase} | Not run / interrupted | — |')
        xml = output/(phase+'.xml')
        if xml.exists():
            for case in ET.parse(xml).iter('testcase'):
                cases.append(case)
                slow.append((float(case.get('time', '0')), case.get('classname', '')+'.'+case.get('name', '')))
    failures = sum(case.find('failure') is not None or case.find('error') is not None for case in cases)
    skipped = sum(case.find('skipped') is not None for case in cases)
    lines += ['', f'Recorded cases: {len(cases)}; failures/errors: {failures}; skipped: {skipped}.']
    lines += ['', '### Slowest tests (setup + call + teardown)', '']
    for seconds, name in sorted(slow, reverse=True)[:20]:
        lines.append(f'- {seconds:.3f}s: `{name.replace(chr(10), " ").replace("`", "")}`')
    text = '\n'.join(lines)+'\n'
    (output/'summary.md').write_text(text, encoding='utf-8')
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as stream:
            stream.write(text)
    print(text)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('plan', 'check', 'slice', 'quick', 'regression', 'summary', 'merge',
                                           'isolate'))
    parser.add_argument('--seed', type=int, help='isolate: the shuffled order to replay')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--basetemp', type=Path)
    parser.add_argument('--map', type=Path, help="the test map (CI keeps it in the Actions cache)")
    parser.add_argument('--shard', help='slice: this shard of the plan, K/N (1-based)')
    parser.add_argument('--shards', type=int, help='merge: how many shards ran')
    parser.add_argument('inputs', nargs='*', type=Path, help="merge: the shards' output folders")
    args = parser.parse_args(argv)
    if args.action == 'plan':
        write_plan(args.output/'plan.json', args.map)
    elif args.action == 'slice':
        shard, shards = (int(number) for number in (args.shard or '1/1').split('/'))
        part = shard_plan(json.loads((args.output/'plan.json').read_text(encoding='utf-8')), shard, shards,
                          Path.cwd(), args.map)
        (args.output/'shard.json').write_text(json.dumps(part, indent=2)+'\n', encoding='utf-8')
        if os.environ.get('GITHUB_OUTPUT'):
            with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as stream:
                stream.write('tests='+str(bool(part['quick'] or part['regression'])).lower()+'\n')
        print(json.dumps(part))
    elif args.action == 'merge':
        if not args.shards:
            parser.error('--shards is required for merge')
        return merge(args.inputs, args.shards, args.output, args.map)
    elif args.action == 'isolate':
        if args.basetemp is None:
            parser.error('--basetemp is required for isolate')
        return isolate(args.output, args.basetemp, args.seed)
    elif args.action == 'summary':
        summary(args.output)
    elif args.action == 'check':
        value = json.loads((args.output/'plan.json').read_text(encoding='utf-8'))
        base = value.get('base') if value.get('comparison_available') else None
        if not base or set(base) == {'0'}:
            if subprocess.run(['git', 'rev-parse', '--verify', '--quiet', 'HEAD^'],
                              stdout=subprocess.DEVNULL).returncode:
                print('HEAD has no parent (the first commit of a repository): no patch to check')
                return 0
            base = 'HEAD^'
        return subprocess.run(['git', 'diff', '--check', base, 'HEAD', '--']).returncode
    else:
        if args.basetemp is None:
            parser.error('--basetemp is required for test execution')
        value = json.loads((args.output/'plan.json').read_text(encoding='utf-8'))
        code = run(value, args.action, args.output, args.basetemp, args.map)
        if args.action == 'regression' and code == 0 and args.map:
            mark_verified(args.map, value['head'])
        return code
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
