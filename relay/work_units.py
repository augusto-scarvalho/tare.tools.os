"""Ways of working, phase 2: the session hands one bounded unit to a child implementer working in a worktree of the
session's own tree (uncommitted bytes included). tare runs the unit's checks before and after, decides the verdict
and applies only a VERIFIED change, file by file, when each file still holds the bytes the child started from.
Off by default (experiment implement_child); the model never writes a check command. Phase 3 (D8): the session may
hand several units of its plan at once, run in parallel when it asks or the operator turned it on."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import contextvars
from datetime import datetime, timezone
import hashlib
from contextvars import ContextVar
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import uuid

from .work_forms import LEDGER, MAX_UNITS, lint, literal

VERIFY_SECONDS = 300            # default per-check cap; explicit --unlimited time removes it
MAX_RUNGS = 3                   # routes tried for one unit, cheapest first
MAX_FAILURES = 2                # NOT_VERIFIED runs of the same unit before tare refuses a third (R3)
# NOT_REVIEWED runs in a row before tare refuses another: an audit finding the implementer cannot fix (a file its
# tools cannot delete) otherwise loops; dogfooding the TUI, a local implementer and the auditor ran 7 rounds, 65 min.
MAX_REFUSALS = 3                # at most the three runs the ledger keeps (runs recorded before `repeated`)
MAX_PROGRESSING_REFUSALS = 6    # a cost backstop: an audit repeating a finding stops the unit at once
AUDIT_REFUSED = 'The independent audit did not accept this candidate.'  # only these count toward MAX_REFUSALS
AUDIT_INCOMPLETE = 'The independent audit reached no verdict.'  # a coverage gap or failed review: no defect to count
CHILD_LIMITS = {'max_model_calls': 30, 'max_seconds': 900}
# ponytail: from the phase 3 comparison (2026-10-06), children that finished a unit used 12 and 16 calls, and one
# given 7 ran out without changing anything; below this the session edits directly. Revisit with more units.
MIN_CHILD_CALLS = 10
# Calls the session keeps for itself after its children: verify, a read or two and deliver. With 2 it ran out
# before delivering a green result in the phase 3 comparison.
SESSION_RESERVE = 4
OUTPUT_BYTES = 4096
PARALLEL_CHILDREN = 2           # children at once with parallel on (D8); TARE_PARALLEL_CHILDREN changes it, 1 to 4
LEDGER_LOCK = threading.Lock()  # units handed at once record their state in the same ledger
CHECK_PORT = ContextVar('conversation_check_port', default=None)
# A check the sandbox stopped (command_protection.verification_limitation), or one that could not run in it at all.
SANDBOX_LIMITS = ('ISOLATION_INTERNAL_IPC_UNAVAILABLE', 'ISOLATION_NETWORK_DENIED', 'ISOLATION_SECRET_DENIED',
                  'ISOLATION_UNAVAILABLE')
EFFORTS = ('low', 'medium', 'high', 'xhigh')
STRINGS = {'type': 'array', 'items': {'type': 'string'}}
UNIT_SPEC = {'implement': {
    'description': 'Hand one bounded unit of a change to a separate implementer that works on a copy of this '
        'project, and get it back verified: tare runs the unit\'s checks before and after and applies the change '
        'here only when they pass (VERIFIED); otherwise you get the verdict, the reason and the retained patch. '
        'Name a unit of your plan, or give it inline: the paths it may change, its done criteria and the session '
        'verifiers or existing tests that check it. It costs a separate session: for a small edit, edit directly.',
    'parameters': {'type': 'object', 'properties': {
        'unit': {'type': 'string', 'description': 'Key of a unit in your plan (plan tool).'},
        'title': {'type': 'string'},
        'target_paths': {**STRINGS, 'description': 'Relative literal paths the implementer may change.'},
        'criteria': {**STRINGS, 'description': 'How to tell the unit is done.'},
        'verifiers': {**STRINGS, 'description': "Names of this session's verifiers that check it."},
        'test_paths': {**STRINGS, 'description': 'Existing test files that check it; with pytest, file::test '
                       'names one test.'},
        'note': {'type': 'string', 'description': 'What the implementer should know; at most 2000 characters.'},
        'route': {'type': 'string', 'description': "One of tare's implementer routes; empty: tare climbs from the "
                  'cheapest one this installation has.'},
        'effort': {'type': 'string', 'enum': list(EFFORTS)},
        'units': {'type': 'array', 'minItems': 1, 'maxItems': MAX_UNITS, 'description': 'Several units of your plan '
                  'at once, instead of unit: each by key, with its own route, effort and note if you want. Hand a '
                  'unit only after the units it depends on are applied.', 'items': {'type': 'object', 'properties': {
                      'unit': {'type': 'string'}, 'route': {'type': 'string'},
                      'effort': {'type': 'string', 'enum': list(EFFORTS)}, 'note': {'type': 'string'}},
                      'required': ['unit'], 'additionalProperties': False}},
        'parallel': {'type': 'boolean', 'description': 'Run those units at the same time, each on its own copy '
                     '(2 at most); their changes are still applied one after another, each checked on the tree the '
                     'earlier ones left. Same calls, less waiting: for units that share no files, whose '
                     'interfaces your plan fixes and whose tests do not run another unit\'s new code (such a '
                     'unit fails alone and is run again after the others).'}},
        'additionalProperties': False}}}


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def imported_uid_baseline(tree, base, missing):
    """Keep only host-generated Godot UID metadata in the private check baseline."""
    from .challenger import git
    from .challenger_workspace import safe
    generated = {}
    for name in missing:
        path = safe(tree, name)
        if path.is_file() and path.stat().st_size <= 128:
            raw = path.read_bytes()
            if re.fullmatch(rb'uid://[a-z0-9]{1,32}\r?\n?', raw):
                generated[name] = hashlib.sha256(raw).hexdigest()
    if generated:
        git(tree, 'add', '--', *generated)
        git(tree, '-c', 'user.name=tare verifier', '-c', 'user.email=verifier@localhost',
            '-c', 'core.hooksPath=', '-c', 'commit.gpgsign=false', 'commit', '--only',
            '-m', 'Generated verification metadata', '--', *generated)
        base = git(tree, 'rev-parse', 'HEAD').decode().strip()
    return base, generated


def clip(text, limit=OUTPUT_BYTES):
    raw = text.encode('utf-8', 'replace')
    return text if len(raw) <= limit else raw[-limit:].decode('utf-8', 'replace')


def check_output(text, limit=1500):
    """Keep the first failure context and final summary inside the existing bound."""
    raw=text.encode('utf-8','replace')
    if len(raw)<=limit:return text
    marker=b'\n... output omitted ...\n'
    head=(limit-len(marker))*2//3
    return raw[:head].decode('utf-8','ignore')+marker.decode()+raw[-(limit-len(marker)-head):].decode('utf-8','ignore')


def read_ledger(custody):
    path = Path(custody)/LEDGER
    try:
        return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
    except (OSError, ValueError):
        return {}


def resolve_unit(custody, config, args):
    """The unit to run: one of the plan's, or the one given inline, checked with the plan's lint."""
    if args.get('unit'):
        units = (read_ledger(custody).get('plan') or {}).get('units') or []
        unit = next((u for u in units if u.get('key') == args['unit']), None)
        if unit is None:
            raise ValueError(f"No unit {args['unit']} in this session's plan.")
    else:
        unit = {k: args[k] for k in ('title', 'target_paths', 'criteria', 'verifiers', 'test_paths') if k in args}
        unit['key'] = 'unit-' + hashlib.sha256(json.dumps(unit, sort_keys=True).encode()).hexdigest()[:10]
    # The lint sees this unit alone: its depends_on names units of the plan, not of this list (the walkthrough of
    # bd6ee62, 2026-10-07, refused every unit with a dependency as "unknown"). Dependencies are checked below.
    problems = lint(config, {'units': [{**unit, 'depends_on': []}], 'criteria': unit.get('criteria') or []})
    if problems:
        raise ValueError(' '.join(problems))
    if args.get('unit'):
        applied = {k for k, v in (read_ledger(custody).get('units') or {}).items() if v.get('state') == 'APPLIED'}
        waiting = [d for d in unit.get('depends_on') or [] if d not in applied]
        if waiting:
            raise ValueError('Hand this unit after the units it depends on are applied: ' + ', '.join(waiting))
    if not config.get('conversation_flow') and all(Path(p).suffix.lower() in PROSE for p in unit['target_paths']):
        # The phase 3 comparison (545b5dc): a documentation unit checked by the code's tests could never turn them
        # green, and its children spent 23 calls on it.
        raise ValueError('A unit that only changes documentation has nothing its checks can tell: write it directly (R0).')
    return unit


PROSE = ('.md', '.rst', '.txt', '.adoc')


SUFFIXES = {'pytest': ('.py',), 'npm': ('.js', '.mjs', '.cjs', '.jsx', '.ts', '.tsx'), 'cargo': ('.rs',),
            'go': ('.go',), 'maven': ('.java', '.kt'), 'gradle': ('.java', '.kt', '.groovy'), 'dotnet': ('.cs', '.fs')}


def test_file(test):
    """The file of a test path, without the pytest node (file::test)."""
    return test.partition('::')[0]


def narrowed(name, runner, tests):
    """The argv that runs only `tests` with an ecosystem's runner (the restricted targets of D6), or None."""
    stems = [Path(test_file(t)).stem for t in tests]
    if name == 'pytest':
        return [runner, '-B', '-m', 'pytest', '-q', '-p', 'no:cacheprovider', *tests]
    if name == 'npm':
        return [runner, 'test', '--', *tests]
    if name == 'cargo':
        return [runner, 'test', *(a for s in stems for a in ('--test', s))]
    if name == 'go':
        return [runner, 'test', *sorted({'./' + (Path(test_file(t)).parent.as_posix() or '.') for t in tests})]
    if name == 'maven':
        return [runner, '-q', 'test', '-Dtest=' + ','.join(stems)]
    if name == 'gradle':
        return [runner, 'test', *(a for s in stems for a in ('--tests', s))]
    if name == 'dotnet':
        return [runner, 'test', '--filter', '|'.join('FullyQualifiedName~' + s for s in stems)]
    return None


def pytest_scratch(argv, python, tree, *, previous_tree=None):
    """Own a conversation check's temporary root without changing an explicit one."""
    module = argv[1:]
    if module[:1] == ['-B']:
        module = module[1:]
    if previous_tree is not None and argv and argv[0] == python and module[:2] == ['-m', 'pytest']:
        before = str(Path(previous_tree).parent/('.'+Path(previous_tree).name+'-pytest'))
        after = str(Path(tree).parent/('.'+Path(tree).name+'-pytest'))
        rebound = list(argv)
        for i, arg in enumerate(argv):
            if arg == '--':
                break
            if arg == '--basetemp='+before:
                rebound[i] = '--basetemp='+after
            elif arg == '--basetemp' and i+1 < len(argv) and argv[i+1] == before:
                rebound[i+1] = after
        return rebound  # Explicit user paths and non-pytest commands keep their exact argv.
    if (argv and argv[0] == python and module[:2] == ['-m', 'pytest']
            and not any(arg == '--basetemp' or arg.startswith('--basetemp=') for arg in argv)):
        return [*argv, '--basetemp', str(Path(tree).parent/('.'+Path(tree).name+'-pytest'))]
    return argv


def scoped_verifiers(config, unit, repository, tree):
    """Compile the automatic conversation pytest default for this unit, preserving custom checks."""
    if not config.get('conversation_flow'):
        return config
    from .turn_engine import project_python
    python = project_python(Path(config['repositories'][repository]))
    tests = [t for t in unit.get('test_paths') or [] if test_file(t).endswith('.py')]
    verifiers = {}
    for name, row in config['verifiers'].items():
        argv = list(row['argv'])
        if row['repository'] == repository:
            if name == 'tests' and tests and argv == [python, '-m', 'pytest', '-q']:
                # Compile through the same per-file runner as the unit. Removing
                # the default first prevents recursion and preserves its origin.
                without_default = {**config, 'verifiers': {
                    n: v for n, v in config['verifiers'].items() if n != name}}
                automatic = checks(without_default, {**unit, 'verifiers': [],
                                                    'test_paths': tests}, repository, tree)
                for base, command in automatic.items():
                    key = base
                    suffix = 2
                    while key in config['verifiers'] or key in verifiers:
                        key = f'{base}#{suffix}'
                        suffix += 1
                    verifiers[key] = {**row, 'argv': command}
                continue
            if not config.get('command_protection'):
                argv = pytest_scratch(argv, python, tree)
        verifiers[name] = {**row, 'argv': argv}
    return {**config, 'verifiers': verifiers}


def planned_backlog(unit, repository, tree):
    """Bind the existing graph port to one valid, explicitly planned private target."""
    from .conversation_tasks import BACKLOG_CHECK
    from .project_graph import binding
    from graph_backlog.validation import validate_work_graph
    from tare_ops import read_graph
    paths=[p for p in unit['target_paths'] if p.endswith('.json')]
    if BACKLOG_CHECK not in unit['verifiers'] or len(paths)!=1:return None
    try:
        path=Path(tree)/paths[0]
        if not path.resolve().is_relative_to(Path(tree).resolve()):return None
        graph=binding({'repository':repository,'path':str(path)},{repository:str(tree)})
        return graph if validate_work_graph(read_graph(Path(graph['path'])))['status']=='PASS' else None
    except (ValueError,OSError,TypeError,KeyError):
        return None  # New/invalid/ambiguous graphs retain file tools and the normal validator.


def pytest_scope(argv, python):
    """Recognize only unfiltered pytest targets, not arbitrary command equivalence."""
    parts=list(argv)
    if not parts or parts.pop(0)!=python:return None
    if parts[:1]==['-B']:parts.pop(0)
    if parts[:2]!=['-m','pytest']:return None
    parts=parts[2:];targets=[]
    while parts:
        part=parts.pop(0)
        if part in ('-q','--quiet'):continue
        if part=='-p':
            if not parts or parts.pop(0)!='no:cacheprovider':return None
            continue
        if part=='--basetemp':
            if not parts or parts[0].startswith('-'):return None
            parts.pop(0);continue
        if part.startswith('--basetemp=') and part!='--basetemp=':continue
        if part.startswith('-'):return None
        targets.append(part)
    return tuple(targets) if targets else None


def checks(config, unit, repository, tree):
    """{name: argv} that tare runs in the child's tree: the unit's named verifiers, and each ecosystem's runner on
    the unit's test files (pytest, npm with jest/vitest/mocha, cargo, go, maven, gradle, dotnet)."""
    from .work_forms import ecosystems
    from .turn_engine import project_python
    automatic_default = False
    if config.get('conversation_flow'):
        source_python = project_python(Path(config['repositories'][repository]))
        row = config['verifiers'].get('tests', {})
        automatic_default = ('tests' in (unit.get('verifiers') or [])
                             and row.get('repository') == repository
                             and row.get('argv') == [source_python, '-m', 'pytest', '-q']
                             and any(test_file(t).endswith('.py') for t in unit.get('test_paths') or []))
    config = scoped_verifiers(config, unit, repository, tree)
    # Candidates omit environments. Keep the source project's interpreter instead of
    # accidentally selecting a different global Python from the disposable worktree.
    python = project_python(Path(config['repositories'][repository]))
    found = {name: config['verifiers'][name]['argv'] for name in unit.get('verifiers') or []
             if not (automatic_default and name == 'tests')
             and config['verifiers'][name]['repository'] == repository}
    automatic = {}

    def add_pytest(targets):
        groups = {}
        for target in targets:
            groups.setdefault(test_file(target), []).append(target)
        for path, selectors in groups.items():
            base = 'pytest-tests:' + path
            name = base
            suffix = 2
            while name in found:
                name = f'{base}#{suffix}'
                suffix += 1
            found[name] = narrowed('pytest', python, selectors)
            automatic[name] = selectors

    tests = unit.get('test_paths') or []
    if config.get('conversation_flow') and any(test_file(t).endswith('.py') for t in tests) \
            and not any(e['name'] == 'pytest' for e in ecosystems(Path(tree))):
        if 'pytest-tests' not in found:
            add_pytest([t for t in tests if test_file(t).endswith('.py')])
        tests = [t for t in tests if not test_file(t).endswith('.py')]
    for eco in ecosystems(Path(tree)) if tests else ():
        mine = [t for t in tests if test_file(t).endswith(SUFFIXES.get(eco['name'], ()))]
        runner = python if eco['name'] == 'pytest' else eco['runner']
        argv = narrowed(eco['name'], runner, mine) if mine and runner and eco['narrow'] else None
        if argv:
            if eco['name'] == 'pytest':
                if 'pytest-tests' not in found:
                    add_pytest(mine)
            else:
                found.setdefault(f"{eco['name']}-tests", argv)
            tests = [t for t in tests if t not in mine]
    if config.get('conversation_flow'):
        for name, selectors in automatic.items():
            scope = pytest_scope(found[name], python)
            if any(pytest_scope(argv, python) == scope for other, argv in found.items()
                   if other not in automatic):
                del found[name]
                continue
            from .candidate_checks import command as candidate_command
            owned = candidate_command(tree, unit, python, selectors)
            if owned is not None:
                found[name] = owned
    if config.get('conversation_flow') and not config.get('command_protection'):
        for name, argv in found.items():
            found[name] = pytest_scratch(argv, python, tree)
    return found


class CheckCleanupUnknown(RuntimeError):
    """Keep the candidate until the check process tree is known to be stopped."""


def managed_check(argv, *, cwd, timeout, env=None, cancelled=lambda:False, **ignored):
    from compute_plane.managed_process import run_pipe_process
    from compute_plane.workspace import isolated_child_environment
    # Keep Windows nested pytest/artifact paths no longer than the ordinary
    # TEMP path. An extra directory below AppData/Local/Temp crosses MAX_PATH.
    temp_base=(Path.home()/'.tare'/'tmp' if os.name=='nt' else Path(tempfile.gettempdir())).resolve()
    temp_base.mkdir(parents=True,exist_ok=True)
    scratch=Path(tempfile.mkdtemp(prefix='' if os.name=='nt' else 'tc-',dir=temp_base)).resolve()
    process=None
    try:
        child_env=isolated_child_environment({'PYTHONUTF8':'1','PYTHONDONTWRITEBYTECODE':'1'}) if env is None else dict(env)
        child_env.update(TEMP=str(scratch),TMP=str(scratch),TMPDIR=str(scratch))
        process,out,err=run_pipe_process(argv,cwd=cwd,env=child_env,
            timeout=float('inf') if timeout is None else timeout,
            cancelled=cancelled,max_output=1024*1024)
        if not process['cleanup_verified']:
            raise CheckCleanupUnknown('Check cleanup is unconfirmed; candidate and scratch retained: '+str(scratch))
        result=subprocess.CompletedProcess(argv,process['exit_code'] if process['exit_code'] is not None else -1,
            out.decode('utf8',errors='replace'),err.decode('utf8',errors='replace'))
        result.process=process
        for key in ('timeout','cancelled','output_limit'):
            if process.get(key):
                result.execution_limitation='Check '+key+'; process tree cleanup verified.'
                break
        return result
    finally:
        if process is not None and process.get('cleanup_verified'):
            if scratch.resolve()!=scratch or scratch.parent!=temp_base:
                raise CheckCleanupUnknown('Check scratch path changed; cleanup held.')
            from compute_plane.native_journal import io_path
            def remove_readonly(operation,path,error):
                if isinstance(error,FileNotFoundError):return
                if not isinstance(error,PermissionError):raise error
                os.chmod(path,stat.S_IREAD|stat.S_IWRITE)
                operation(path)
            # onerror also supports the product's minimum Python 3.11.
            shutil.rmtree(io_path(scratch),onerror=lambda operation,path,info:
                          remove_readonly(operation,path,info[1]))
            if scratch.exists():raise OSError('Check scratch cleanup did not finish: '+str(scratch))


def run_checks(tree, commands):
    """('green' | 'red' | 'inconclusive', output): every command must pass; a timeout or a command that cannot
    start settles nothing."""
    outputs = []
    statuses = []

    def receipt():
        # Clip only logs; exceptional outcomes and pending commands remain complete afterward.
        output = clip('\n'.join(outputs))
        pending = [f'{name}: not executed' for name in commands if name not in completed]
        exceptional = '\n'.join([*statuses, *pending])
        return output + ('\n' if output and exceptional else '') + exceptional

    completed = set()
    state = 'green'
    from .execution_resources import selected
    timeout = None if selected('time') else VERIFY_SECONDS
    for name, argv in commands.items():
        try:
            port = CHECK_PORT.get()
            run = port or managed_check
            options = {}
            if port is None:
                from compute_plane.workspace import isolated_child_environment
                options['env'] = isolated_child_environment({'PYTHONUTF8':'1', 'PYTHONDONTWRITEBYTECODE':'1'})
            done = run(argv, cwd=tree, capture_output=True, text=True, errors='replace',
                                  timeout=timeout, stdin=subprocess.DEVNULL, **options)
        except subprocess.TimeoutExpired as exc:
            completed.add(name)
            statuses.append(f'{name}: more than {exc.timeout} s (timeout)')
            return 'inconclusive', receipt()
        except OSError as exc:
            completed.add(name)
            statuses.append(f'{name}: could not start: {exc}')
            return 'inconclusive', receipt()
        completed.add(name)
        outputs.append(f'$ {name} (exit {done.returncode})\n{check_output(done.stdout + done.stderr)}')
        if getattr(done, 'isolation', None):  # not sandboxed: no sandbox here, or run outside it by the user's choice
            statuses.append(f'{name}: {done.isolation}')
        if getattr(done, 'execution_limitation', None):
            statuses.append(f'{name}: {done.execution_limitation}')
            return 'inconclusive', receipt()
        if done.returncode:
            state = 'red'
    return state, receipt()


def verdict(before, after, changed, protected, retry):
    """VERIFIED, NOT_VERIFIED or INCONCLUSIVE, and why; `retry` reruns the checks once (a flaky check)."""
    if not changed:
        return 'INCONCLUSIVE', 'the implementer changed nothing'
    touched = sorted(set(changed) & set(protected))
    if touched:
        return 'INCONCLUSIVE', 'the implementer changed the checks themselves: ' + ', '.join(touched)
    if 'inconclusive' in (before, after):
        return 'INCONCLUSIVE', 'a check did not finish'
    if after == 'green':
        return 'VERIFIED', 'red before, green after' if before == 'red' else 'green before and after (the change keeps them green)'
    if before == 'green' and retry() == 'green':
        return 'VERIFIED', 'green after a second run (the check is flaky)'
    return 'NOT_VERIFIED', 'the checks fail after the change'


def bill(deployment):
    """Who pays for a route: its class (local, quota, usd) and, for a hosted one, the vendor named first in its id
    (codex, claude, bedrock, openrouter), since each is another account or bill."""
    from .challenger_variants import accounting
    kind = accounting(deployment)
    return (kind,) if kind == 'local' else (kind, str(deployment.get('id', '')).split('-')[0])


def routes(parent, chosen=None, effort=None, *, allow_parent=False, refused=None):
    """The implementer routes to try, cheapest first: tare's tiers, on the session's own bill (D4): a local session
    takes local routes, a hosted one the routes of its class and vendor, so a Codex quota session never climbs into
    Bedrock dollars or another CLI's quota (the TUI walkthrough of 2026-10-07 did, unasked). The coordinator may fix
    one of them and its effort; the child_routes_any experiment opens every hosted route. Nothing waits for a busy GPU
    (owner, 2026-10-08): a local route whose GPU an image job holds is skipped, the coordinator's chosen one included,
    and the unit takes the next routes of the policy (the owner's <storage>/serve/routing.json, else tare's), leaving a
    local session's bill as the session itself would. A route the routing profile switches off for the implementer is
    never run, the coordinator's chosen one and the fallback to the session's own model included (only a pin is the
    user's own choice); `refused` collects why."""
    from .delegation import experiments
    from .turn_engine import available, gpu_taken, policy, with_effort
    from .execution_scope import current, execution_tiers, is_pro, kind, refusal
    temporary=current()
    if temporary.pin('implementer'):
        from .execution_scope import pin_selection
        deployment=pin_selection('implementer',chosen,effort)
        yield deployment['native_model_choice']['connection'],deployment
        return  # Never reconstruct from a name under the parent's coordinator role.
    tiers = execution_tiers(policy()['implementer'])
    allowed = [name for tier in tiers for name in tier]
    parent_name = parent.get('id')
    parent_allowed = (allow_parent and parent.get('native_profile') and isinstance(parent_name, str)
        and not is_pro(parent) and temporary.allows(parent, 'implementer')[0])
    if parent_allowed and parent_name not in allowed:
        allowed.append(parent_name)  # The selected connection, still a separate role on the same bill.
    if chosen and chosen not in allowed:
        raise ValueError("Choose one of tare's implementer routes: " + ', '.join(allowed))
    own = bill(parent) if parent.get('native_profile') else None
    any_hosted = own and own[0] != 'local' and experiments(own[0]).get('child_routes_any')
    gpu_busy = False
    # An explicit CLI preference admits configured subscription workers even
    # with a local coordinator. It never opens an unrelated inference API bill.
    fits = lambda d: temporary.changes('implementer') or (temporary.prefer == 'subscription' and kind(d) == 'subscription') or own is None or bill(d) == own or (any_hosted and bill(d)[0] != 'local') or (
        gpu_busy and own[0] == 'local')
    def names():
        yield from [chosen] if chosen else allowed
        if chosen and gpu_busy:
            yield from (n for n in allowed if n != chosen)
    found = []
    for name in names():
        deployment, why = available(name)
        gpu_busy = gpu_busy or gpu_taken(why)
        if deployment is None:
            continue
        if off := refusal('implementer', deployment, name):
            if name == chosen and refused is not None:
                refused.append('SWITCHED_OFF_FOR_ROLE: ' + off)
            continue  # as an unavailable route: the chosen one is not replaced unless its GPU was busy
        if not fits(deployment):
            if name == chosen:
                raise ValueError(f'{chosen} is not on this session\'s bill ({" ".join(own)}): choose a route of the '
                                 'same class and vendor, or turn the child_routes_any experiment on.')
            continue
        found.append(name)
        yield name, with_effort(deployment, effort) if effort else deployment
    if parent_allowed and not found and not chosen:
        # Separate implementer role, same explicitly selected model/allowance.
        # Explicit route choices never silently fall back to this profile.
        # The conversation's model was picked for the conversation, not as implementer: the profile still decides.
        if off := refusal('implementer', parent, parent['id']):
            if refused is not None:
                refused.append('SWITCHED_OFF_FOR_ROLE: ' + off)
            return
        yield parent['id'], with_effort(parent, effort) if effort else parent


def changed_files(tree, base):
    from .challenger_workspace import changes
    return changes(tree, base)


def base_bytes(tree, base, name):
    from .challenger import git
    try:
        return git(tree, 'show', f'{base}:{name}')
    except ValueError:
        return None


def session_suite(config, repository):
    return {n: v['argv'] for n, v in config['verifiers'].items() if v['repository'] == repository}


def suite_states(tree, suite):
    """{name: (state, output)} of each of the session's checks in a tree."""
    return {name: run_checks(tree, {name: argv}) for name, argv in suite.items()}


def blocked_by_sandbox(receipt):
    """Whether a check receipt (run_checks' output) names a sandbox limitation. Printed by a test, it only holds."""
    return any(f': {code}:' in receipt for code in SANDBOX_LIMITS)


def suite_allows(config, unit, repository, tree, changed, before=None):
    """D9b, before a VERIFIED change is applied: no check of the session that was not red before the change
    (`before`, {name: state}) fails with it, and when they all pass it is applied. A check red before as well (a
    later unit of the plan makes it pass) or one longer than VERIFY_SECONDS settles nothing: then only a change
    the unit's tests cover is applied.
    ponytail: "cover" means a unit test file names the changed module, a coverage run would be exact; a check red
    before and after can hide a new failure in it."""
    after = suite_states(tree, session_suite(config, repository))
    broken = [n for n, (state, _) in after.items() if state == 'red' and (before or {}).get(n) != 'red']
    if broken:
        return False, 'the session\'s checks fail with the change:\n' + '\n'.join(after[n][1] for n in broken)
    if after and all(state == 'green' for state, _ in after.values()):
        return True, 'the session\'s checks pass with the change'
    # The sandbox, not a timeout, stopped a check, and it did not run outside (the user said no, or no one could be
    # asked): that settles nothing silently. The change is held like any unverified one.
    blocked = [n for n, (state, output) in after.items() if state == 'inconclusive' and blocked_by_sandbox(output)]
    if blocked:
        return False, ('the session\'s checks could not be judged in the sandbox and did not run outside it; the '
                       'patch is kept:\n' + '\n'.join(after[n][1] for n in blocked))
    texts =' '.join((Path(tree)/t).read_text(encoding='utf-8', errors='replace')
                     for t in {test_file(t) for t in unit.get('test_paths') or []} if (Path(tree)/t).is_file())
    # Prose needs no test: a unit's documentation held a verified change (task bd6ee62, 2026-10-07).
    uncovered = [n for n in changed if Path(n).stem not in texts and Path(n).suffix.lower() not in PROSE]
    if uncovered:
        return False, 'no check that fits 300 s covers ' + ', '.join(uncovered) + '; the patch is kept'
    return True, 'the unit\'s tests cover every changed file'


def apply(root, tree, base, changed, writable):
    """Write each changed file into the session's tree, but only if every one still holds the bytes the child
    started from (else nothing is written: CONFLICT). The session already holds its workspace lock."""
    from pathlib import PurePosixPath
    from .challenger_workspace import safe
    outside = [n for n in changed if not any(p in ('.', '') or n == p or PurePosixPath(n).is_relative_to(p)
                                             for p in writable)]
    if outside:
        return 'OUTSIDE', outside
    def same(here, base_raw):
        # The base comes from git's objects (LF); a Windows checkout may hold the same text as CRLF (files
        # without eol=lf). Equal text with different line endings is the same file, not a conflict.
        return here == base_raw or (here is not None and base_raw is not None
                                    and here.replace(b'\r\n', b'\n') == base_raw.replace(b'\r\n', b'\n'))
    conflicts = [n for n in changed if not same(safe(root, n).read_bytes() if safe(root, n).exists() else None,
                                                base_bytes(tree, base, n))]
    if conflicts:
        return 'CONFLICT', conflicts
    for name, raw in changed.items():
        path = safe(root, name)
        if raw is None:
            path.unlink(missing_ok=True)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        staged = path.with_name(path.name + '.tare-unit')
        staged.write_bytes(raw)
        os.replace(staged, path)
    return 'APPLIED', sorted(changed)


def brief(unit, commands, note, *, references=(), executables=(), host_review=False):
    lines = [f"Implement one unit of a change in this repository: {unit['title']}",
             'Change only: ' + ', '.join(unit['target_paths']), 'Done when:', *('- ' + c for c in unit['criteria']),
             'Check it with the verify tool (' + ', '.join(commands) + ') until it passes, then deliver a short '
             'summary of what you changed. ' + ('The independent auditor will also review any test changes.'
                 if unit.get('planner_advice') else 'Do not change the checks themselves.')]
    if unit.get('planner_advice'):
        lines += ['', 'The user has now requested implementation of these saved tasks. Any earlier '
                  'plan-only timing is history, not a current prohibition. Implement the task criteria '
                  'within the given paths; do not expand them. The separate planner\'s advice '
                  '(claims to verify against sources):', unit['planner_advice']]
    if unit.get('review_feedback'):
        try:
            label = json.loads(unit['review_feedback']).get('independence')
        except (ValueError, AttributeError):
            label = None
        lines += ['', ('Previous review ('+label+')' if label else 'Previous independent review')+' of this retained candidate. Correct demonstrated defects '
                  'within the same criteria; these findings do not expand the requested scope:', unit['review_feedback']]
    if note:
        lines += ['', 'From the session that planned it (a note, not new requirements):', note[:2000]]
    if unit.get('previous_candidate_changes'):
        lines += ['', 'Previous unapplied candidate changes, for reuse within the same scope. '
                  'They are not verified and do not establish current project bytes:', unit['previous_candidate_changes']]
    if unit.get('verification_feedback'):
        lines += ['', 'Previous host verification diagnostic. Repair this concrete failure; '
                  'do not regenerate unrelated work or ask the user to supply the product schema:', unit['verification_feedback']]
    if unit.get('verification_contract'):
        from compute_plane.native_models import encode
        lines += ['', 'Host-owned validation contract. This installed owner contract takes precedence over '
                  'guessed field values in planner advice or coordinator notes:', encode(unit['verification_contract']),
                  'Host UTC time for newly created observations: '+now()]
    # Machine facts append to the stable Work/planner prefix, preserving cache
    # reuse without putting installation-specific paths into tool schemas.
    if references:
        lines += ['', 'External read-only reference folders already granted by the user for this project '
                  '(use folder_list, folder_read or folder_search; repository-relative tools still use the candidate):',
                  *('- '+p for p in references),
                  'These grants already exist. Inspect the references when the task needs them; do not ask for the same access again.',
                  'Keep commands in the candidate. Windows read-only references can be read/listed but may refuse chdir. '
                  'For reference Git history, use --git-dir with an admitted absolute Git metadata directory instead of -C. '
                  'A worktree .git indirection file is not itself a Git metadata directory; inspect it and request access '
                  'only if the actual metadata directory is outside the existing grants. Never broaden write grants to read history.']
    if executables:
        lines += ['', 'Host-declared check runtimes. Protected commands require their absolute executable paths:',
                  *('- '+p for p in executables),
                  'Bound command output at the source: the process output cap is 1 MiB and the model view is 16 KiB. '
                  'Prefer compact CLI projections; capture/filter large JSON before printing the needed fields. '
                  'An unfiltered full diff can exceed the cap. After a stopped command, inspect candidate state '
                  'before a correction; never automatically repeat the same unbounded operation.',
                  'File tools protect .git metadata. Use the admitted Git executable for status, diff and history; '
                  'a refusal to read .git with the file reader is not a lost project permission.',
                  'A pinned submodule may have no local checkout. List its parent before descending; '
                  'a missing checkout does not prove an installed dependency is absent.',
                  'Named check interpreters: '+json.dumps({n:a[0] for n,a in commands.items()},ensure_ascii=False,sort_keys=True)+'. '
                  'For a manual baseline comparison reuse that project check interpreter and its existing dependencies. '
                  'The product installation Python need not contain project test packages; use verify for registered checks.']
    if host_review:
        lines += ['', 'This repository is your disposable candidate, not the original project. '
                  'Finish all planned source and data changes here before delivering. For backlog changes, '
                  'run the required CLI preview/apply against the graph inside this candidate; that stages '
                  'the proposed graph bytes and does not apply the patch to the original project. '
                  'Do not defer candidate changes while waiting for an audit: the host runs independent '
                  'verification and audit after you return, then applies only an accepted patch. '
                  'There is no audit action in your catalog by design. Do not spawn an auditor or claim '
                  'that review has happened. External reference folders remain read-only.',
                  'The candidate HEAD and generated custody commits can be private experiment snapshots. '
                  'Committed tests must not depend on those object IDs. Use an immutable published baseline or a '
                  'reproducible fixture available in a fresh CI checkout for before/after controls.']
    return '\n'.join(lines)


def worker_brief(unit, commands, note, **options):
    """Use the existing bounded artifact channel when inline plan advice is too large."""
    from .native_agent import MAX_INITIAL_INSTRUCTION_BYTES
    instruction = brief(unit, commands, note, **options)
    inputs = []
    if len(instruction.encode('utf8')) > MAX_INITIAL_INSTRUCTION_BYTES and unit.get('planner_input'):
        from .native_artifacts import read_input
        reference = unit['planner_input']
        artifact = read_input(reference['path'])
        if artifact['sha256'] != reference['sha256'] or artifact['artifact']['role'] != 'planner':
            raise ValueError('The saved planner artifact changed; no worker was started (R0).')
        if len(json.dumps({'inputs': [artifact]}, ensure_ascii=False).encode('utf8')) > 32768:
            raise ValueError('Planner input exceeds the existing artifact bound (R0); no worker was started.')
        compact = {k: v for k, v in unit.items() if k != 'planner_advice'}
        # Preserve the advice marker so tests may be edited and the phase
        # guidance stays the same. All complete plan bytes follow as input.
        compact['planner_advice'] = ('The complete saved plan is in input_artifacts, SHA256 ' +
            artifact['sha256'] + '. Preserve every task and criterion; its source claims remain unverified here.')
        instruction = brief(compact, commands, note, **options)
        inputs = [artifact]
    if len(instruction.encode('utf8')) > MAX_INITIAL_INSTRUCTION_BYTES:
        raise ValueError('Worker briefing exceeds the bounded instruction limit (R0); no checks or model call started. '
            'Remove duplicated context or split the plan without dropping criteria. Another model uses the same limit.')
    return instruction, inputs


def unit_calls(custody):
    """Model calls the session's children have spent, from their receipts: they count against its ceiling."""
    total = 0
    for receipt in Path(custody).glob('units/*/receipt.json'):
        try:
            from .delegation_tools import count_calls
            for index, row in enumerate(json.loads(receipt.read_text(encoding='utf-8'))['rungs'], 1):
                journal = receipt.parent/f'session-{index}'/'journal.jsonl'
                total += (row['model_calls'] if row.get('model_calls') is not None else
                          count_calls(journal.parent) if journal.exists() else row.get('calls_reserved') or 0)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return total


def child_limits(custody, state, share=1, *, deployment=None):
    """The child's ceilings: CHILD_LIMITS, cut to the session's remaining calls and seconds (R5), the calls split
    between the `share` children running at once."""
    import time
    from .delegation_tools import count_calls
    from . import execution_resources as resources
    calls, seconds = CHILD_LIMITS['max_model_calls'], CHILD_LIMITS['max_seconds']
    profile=(deployment or {}).get('native_profile') or {}
    protection=state.get('config',{}).get('command_protection') or {}
    if profile.get('endpoint')=='cli://codex' and protection.get('review') in {'auto','codex','both'}:
        # Native execution review adds another bounded CLI round trip per command.
        # Keep the same model-call allowance; avoid spending it again in cold
        # workers merely because their safety review consumed the wall clock.
        seconds=2*seconds
    calls = resources.child_calls(calls)
    seconds = resources.child_seconds(seconds)
    maximum = (state.get('limits') or {}).get('max_model_calls')
    if maximum is not None:
        from .delegation import child_calls
        spent = resources.known_calls(child_calls(custody))
        calls = resources.ceiling('calls', calls, (maximum - count_calls(Path(custody)) - spent - SESSION_RESERVE) // share)
    if state.get('deadline_utc') is not None:
        seconds = resources.ceiling('time', seconds, int(state['deadline_utc'] - time.time()) - 60)
    if calls is not None and calls < MIN_CHILD_CALLS or seconds is not None and seconds < 120:
        available_calls = 'unlimited' if calls is None else str(max(calls, 0))
        available_seconds = 'unlimited' if seconds is None else str(max(seconds, 0))
        raise ValueError(f'Insufficient implementer funding: available {available_calls} calls and {available_seconds} s; '
                         f'required minimum {MIN_CHILD_CALLS} calls and 120 s. No model call started (R5).')
    return {'max_model_calls': calls, 'max_seconds': seconds, **({'request_seconds': seconds} if seconds is not None else {})}


def unit_spend(custody):
    from .native_spend import total_amounts
    amounts = []
    for path in Path(custody).glob('units/*/receipt.json'):
        row = json.loads(path.read_bytes())
        amounts.extend(r.get('amount_committed', '0') for r in row['rungs'])
    return total_amounts(amounts)


class Batch:
    """What units handed at once share (D8): one git operation on the session's repository at a time, changes
    applied one after another, and the bytes applied so far."""

    def __init__(self, share=1):
        self.share, self.git, self.apply, self.applied = share, threading.Lock(), threading.Lock(), {}


def local_slot(deployment):
    """At most one child on a local route at a time: the local endpoint's slot, the one local explorers take."""
    from .challenger_variants import accounting
    from .delegation_tools import slot
    if accounting(deployment) != 'local':
        return nullcontext()
    scope = (deployment.get('native_profile') or {}).get('endpoint', '') + '|'
    return slot({'accounting': 'local', 'resource_pool': hashlib.sha256(scope.encode()).hexdigest()[:24]})


def stale(tree, applied):
    """Write into a child's tree what other units applied after it started; the names whose bytes moved."""
    from .challenger_workspace import safe
    moved = []
    for name, raw in applied.items():
        path = safe(tree, name)
        if (path.read_bytes() if path.is_file() else None) == raw:
            continue
        moved.append(name)
        if raw is None:
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
    return moved


class GuardRefusal(ValueError):
    """A failure-ledger refusal, distinct from budgets and permissions."""


RETRY_QUESTION = (' Explain why execution stopped and ask the user in plain words whether to try again, '
    'then wait for their answer. The host decides consent through an isolated closed-choice judge or dialog; '
    'a clarification keeps the same question pending. Never ask the user for a command.')


def check_failures(custody, unit, *, conversation=False, operator_retry=False):
    """Keep exhausted scope blocked across renamed demands without changing its ledger."""
    entries = read_ledger(custody).get('units') or {}
    context = unit.get('verification_contract_digest') if conversation else None

    def thresholds(entry):
        history = entry.get('history') or []
        failures = sum(h['verdict'] == 'NOT_VERIFIED' and (
            not context or h.get('verification_contract_digest') == context) for h in history)
        # Only independent audit refusals count, not host application refusals. An audit finding new defects
        # is progress; one reporting a defect again (`repeated`) shows the implementer cannot fix it.
        latest = history[-1] if history else {}
        if 'repeated' not in latest:
            refused = len(history) >= MAX_REFUSALS and all(audit_refusal(h) for h in history[-MAX_REFUSALS:])
            refused = refused and f'The independent audit refused the last {MAX_REFUSALS} candidates'
        elif latest['repeated']:
            refused = 'The independent audit reported the same defect again after a correction'
        else:
            refused = latest['refusals'] >= MAX_PROGRESSING_REFUSALS and (
                f"The independent audit refused {latest['refusals']} candidates in a row, each for new defects")
        return failures >= MAX_FAILURES, refused

    failed, refused = thresholds(entries.get(unit['key']) or {})
    if refused and not (conversation and operator_retry):
        raise GuardRefusal(f"{refused} for unit {unit['key']}. "
            'No implementer starts until the user answers. Tell the user what the latest audit found and ask how to go on: what the '
            'implementer could not fix needs them. Changing notes, routes or task names does not reset this limit.'
            + (RETRY_QUESTION if conversation else ''))
    if failed and not (conversation and operator_retry):
        if conversation:
            raise GuardRefusal(f"Unit {unit['key']} failed its checks {MAX_FAILURES} times with the same host contract. "
                'No implementer starts until the user answers. Preserve the goal and candidate; diagnose the verifier or request intervention. '
                'Changing notes, routes or task names does not reset this limit.' + RETRY_QUESTION)
        raise ValueError(f"Unit {unit['key']} failed its checks {MAX_FAILURES} times: rethink it (plan) or edit directly.")
    if conversation and operator_retry:
        return
    targets = set(unit.get('target_paths') or [])
    for key, entry in entries.items():
        if key == unit['key'] or entry.get('state') == 'APPLIED':
            continue
        shared = targets.intersection(entry.get('paths') or [])
        if shared and any(thresholds(entry)):
            raise GuardRefusal(f"Unit {unit['key']} overlaps blocked unit {key} on scope: {', '.join(sorted(shared))}. "
                'No implementer starts until the user answers. Changing objective wording or demand keys does not reset this limit.'
                + (RETRY_QUESTION if conversation else ' Tell the user to use /retry_delivery to retry the blocked scope.'))


MAX_LEDGER_CLAIMS = 32  # claim digests kept per refused run; the ledger keeps three runs


def claim_digest(claim):
    """The claim as native_artifacts compares findings (case and whitespace aside); no fuzzy matching."""
    return hashlib.sha256(' '.join(str(claim).casefold().split()).encode('utf-8')).hexdigest()[:16]


def audit_refusal(entry):
    return entry['verdict'] == 'NOT_REVIEWED' and entry.get('reason') == AUDIT_REFUSED


def producer_reference(directory, deployment):
    """The frozen CLI adapter can be large. Keep it once in custody, not in the bounded task record."""
    from compute_plane.native_journal import regular, save_json
    raw=json.dumps(deployment,sort_keys=True,ensure_ascii=False).encode('utf8')
    if len(raw)>1024*1024:
        raise ValueError('The producer profile exceeds its custody bound.')
    path=regular(Path(directory)/'producer.json')
    if path.exists():
        if json.dumps(json.loads(path.read_bytes()),sort_keys=True,ensure_ascii=False).encode('utf8')!=raw:
            raise ValueError('The frozen producer snapshot changed.')
    else:
        save_json(path,deployment,max_bytes=1024*1024)
    raw=path.read_bytes()
    return {'artifact':str(path),'sha256':hashlib.sha256(raw).hexdigest(),'id':deployment['id']}


def candidate_producer(custody, value):
    """Resolve an exact host-owned snapshot, retaining compatibility with inline legacy receipts."""
    from compute_plane.native_journal import regular
    if 'artifact' not in value:return value
    path=regular(Path(value['artifact']))
    if not path.resolve().is_relative_to(Path(custody).resolve()/'units') or path.stat().st_size>1024*1024:
        raise ValueError('The producer snapshot is outside this conversation or exceeds its bound.')
    raw=path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=value['sha256']:
        raise ValueError('The frozen producer snapshot changed.')
    deployment=json.loads(raw)
    if deployment['id']!=value['id']:raise ValueError('The producer snapshot identity changed.')
    return deployment


def interruption_feedback(session, row):
    """Carry observed tool failure forward when a stopped worker could not write a final report."""
    from compute_plane.native_journal import NativeJournal
    path=Path(session)/'journal.jsonl'
    result={'child_status':row.get('child_status'),'reason':row.get('child_reason'),
            'model_calls':row.get('model_calls'),'authority':'Unverified tool observation; preserve the original goal and permissions.'}
    outcome=Path(session)/'outcome.json'
    if outcome.is_file() and outcome.stat().st_size<=1024*1024:
        saved=json.loads(outcome.read_text(encoding='utf8'))
        if result['reason'] is None:result['reason']=saved.get('reason')
        report=saved.get('output')
        if row.get('child_status')=='PROPOSED' and isinstance(report,str) and report.strip():
            result['model_report']={'basis':'Unverified implementer claim, not a host verdict.',
                'text':report[:2000],'truncated':len(report)>2000}
    if path.exists():
        events=[r.event() for r in NativeJournal(path).snapshot().records]
        reply=next((e['reply'] for e in reversed(events) if e['kind']=='model_returned'),None)
        if reply:
            try:metadata=json.loads(reply.get('metadata_json') or '{}')
            except (TypeError,ValueError):metadata={}
            stop=metadata.get('provider_stop_reason') if isinstance(metadata,dict) else None
            if stop in {'error','length','content_filter','malformed_function_call','max_tokens','other'}:
                result['provider_stop_reason']=stop
        tools={e['effect_id']:e['tool_call']['name'] for e in events if e['kind']=='effect_requested'}
        for event in reversed(events):
            if event['kind']!='effect_returned' or event['result']['status']=='succeeded':continue
            value=json.loads(event['result']['output_json'])
            text=str(value.get('detail') or value.get('output') or value.get('reason') or value.get('error_type') or '')
            result['last_tool_failure']={'tool':tools.get(event['result']['effect_id']),
                'status':event['result']['status'],'detail':text[-2048:],'truncated':len(text)>2048}
            break
    return result


def verification_evidence(session):
    """Public named-check facts, without the worker's transcript or reasoning."""
    from compute_plane.native_journal import NativeJournal
    path=Path(session)/'journal.jsonl'
    if not path.is_file():return []
    events=[r.event() for r in NativeJournal(path).snapshot().records]
    names={e['effect_id']:json.loads(e['tool_call']['arguments_json']).get('name') for e in events
        if e['kind']=='effect_requested' and e['tool_call']['name']=='verify'}
    observed=[]
    for e in events:
        if e['kind']!='effect_returned' or e['result']['effect_id'] not in names:continue
        raw=e['result']['output_json'];value=json.loads(raw)
        output=str(value.get('output') or value.get('reason') or value.get('detail') or '')
        observed.append({'name':names[e['result']['effect_id']],'status':e['result']['status'],
            'output':output[-2048:],'truncated':len(output)>2048,'result_sha256':hashlib.sha256(raw.encode()).hexdigest()})
    # Successful intermediate checks carry measured facts too. Sampling just
    # the endpoints/failures made an auditor lose genuine timing provenance.
    if len(observed)<=64:return observed
    selected=[]
    for name in dict.fromkeys(row['name'] for row in observed):
        checks=[i for i,r in enumerate(observed) if r['name']==name]
        failures=[i for i in checks if observed[i]['status']!='succeeded']
        selected.extend(sorted({checks[0],checks[-1],*failures[:1],*failures[-1:]}))
    selected=set(selected)
    # Keep ordinary runs complete and bound exceptional long histories. The
    # original hash-chained journal remains intact; omitted evidence is explicit.
    for index in reversed(range(len(observed))):
        if len(selected)>=64:break
        selected.add(index)
    indexes=sorted(selected)
    if len(indexes)>64:indexes=[*indexes[:32],*indexes[-32:]]
    rows=[observed[i] for i in indexes]
    rows[0]['projection']={'total_observations':len(observed),'included_observations':len(rows),
                           'omitted_observations':len(observed)-len(rows)}
    return rows


def run_unit(custody, state, args, stop, batch=None, *, review=None, candidate=None, worker=None, control=None, spend_model=None, review_effect=None,
             outside=None, check_control=None):
    """Run one unit up the route ladder until VERIFIED or out of rungs; apply a VERIFIED change. The receipt and
    the patch stay under the session's custody; the ledger records the unit's state. Without command protection the
    host checks run in the sandbox (command_protection.conversation_verifier) on the execution control (`control`, or
    `check_control` where the child must not share the parent's clock); `outside` decides a check the sandbox blocks."""
    from .challenger import git
    from .challenger_workspace import snapshot
    from .native_agent import run_session
    from .native_installation import storage_root
    batch = batch or Batch()
    config = state['config']
    unit = resolve_unit(custody, config, args)
    if state.get('conversation_flow') and config.get('command_protection'):
        from .command_protection import prepared_checks
        # Older saved host checks predate the CRLF-aware conversation-diff.
        # Upgrade only that exact host shape; retain custom checks unchanged.
        verifiers = {name: {**row, 'argv': list(row['argv'])}
                     for name, row in config['verifiers'].items()}
        diff = verifiers.get('conversation-diff')
        if diff and diff['argv'] == ['git', 'diff', '--check', 'HEAD', '--', *unit['target_paths']]:
            diff['argv'] = ['git', '-c',
                'core.whitespace=trailing-space,space-before-tab,cr-at-eol', *diff['argv'][1:]]
        admitted=prepared_checks({n:row['argv'] for n,row in verifiers.items()})
        config={**config,'verifiers':{n:{**row,'argv':admitted[n]} for n,row in verifiers.items()}}
    repository = unit.get('repository') or next(iter(config['repositories']))  # the lint checked it
    root = config['repositories'][repository]
    if not candidate or not candidate.get('review_only'):
        check_failures(custody, unit, conversation=config.get('conversation_flow', False),
            **({'operator_retry':True} if state.get('operator_delivery_retry') else {}))
    waiting = None
    if worker:
        worker={**worker,'producer':candidate_producer(custody,worker['producer'])}
        from .worker_wait import release, restore
        waiting = restore(worker, custody, config, unit)
    if worker and waiting is None:  # a new plan replaced the one it waited under: a fresh implementer runs it
        release(root, worker)
        worker = None
    if worker:
        from .execution_scope import current
        from .execution_team import reasoning
        permitted,why=current().allows(worker['producer'],'implementer')
        if not permitted:
            raise ValueError('The waiting worker is excluded by this execution policy: '+str(why))
        if args.get('route') and args['route'] != worker['route']:
            raise ValueError('Continue the waiting implementer on its saved route; changing provider needs a separate decision.')
        if args.get('effort') and args['effort'] != reasoning(worker['producer']):
            raise ValueError('Continue the waiting implementer with its saved reasoning level.')
    if not state.get('conversation_flow') and not (Path(root)/'.git').exists():
        raise ValueError('The repository is not a git repository: a child needs a worktree of it.')
    job = Path(custody)/'units'/(unit['key'] + '-' + uuid.uuid4().hex[:6])
    job.mkdir(parents=True)
    receipt = {'unit': unit, 'started': now(), 'rungs': [], 'effect_id':review_effect}
    if candidate:receipt['input_candidate']=candidate
    protected = [test_file(t) for t in unit.get('test_paths') or []]
    result = {'verdict': 'INCONCLUSIVE', 'reason': 'no implementer route is available'}
    producer=candidate_producer(custody,candidate['producer']) if candidate and candidate.get('review_only') else None
    refused = []  # why the routing profile refused the coordinator's chosen route or the session's own model
    choices = [(worker['route'], worker['producer'])] if worker else [(producer['id'], producer)] if producer else routes(
        state.get('deployment') or {}, args.get('route'), args.get('effort'), refused=refused,
        **({'allow_parent': True} if state.get('conversation_flow') else {}))
    visited = []  # rungs actually reached, for executed_decision only; selection is unchanged
    for rung, (name, deployment) in enumerate(choices):
        if rung >= MAX_RUNGS or stop():
            break
        if review is not None and hasattr(review, 'preflight') and not review.preflight(deployment):
            result = {'verdict': 'NOT_REVIEWED', 'reason': 'No independent auditor is available; implementation has not started.'}
            break
        visited.append(name)
        resumed_view = (review.resume_tree() if candidate and candidate.get('review_only')
                        and callable(getattr(review,'resume_tree',None)) else None)
        tree = waiting[0] if waiting else resumed_view or storage_root()/'wt'/uuid.uuid4().hex[:8]  # short: Windows paths stop at 260 characters
        row = {'route': name}
        check_token = None
        keep_tree = bool(waiting)
        try:
            # Default (no command protection, no port a caller set): the sandbox, set once the checks are known.
            default_checks = (not config.get('command_protection') and CHECK_PORT.get() is None
                              and (control or check_control) is not None)
            if not config.get('command_protection') and CHECK_PORT.get() is None and not default_checks:
                check_token=CHECK_PORT.set(lambda argv,**kwargs:managed_check(argv,cancelled=stop,**kwargs))
            selected_limits = None if candidate and candidate.get('review_only') else child_limits(custody, state, batch.share,deployment=deployment)
            proof = unit.get('proof_paths') or {repository: unit['target_paths']}
            basis = None
            if review is not None:
                from .conversation_tasks import source_state
                basis = source_state(config, proof)
            if waiting:
                base = worker['base']
            else:
                with batch.git:
                    if state.get('conversation_flow'):
                        from .conversation_workspace import snapshot as folder_snapshot
                        base = folder_snapshot(root, tree)
                    else:
                        base = snapshot(root, tree)
            if candidate and not waiting:
                from .conversation_tasks import source_state
                from compute_plane.native_journal import regular
                patch = regular(Path(candidate['patch']))
                if (not patch.resolve().is_relative_to(Path(custody).resolve()/'units')
                        or hashlib.sha256(patch.read_bytes()).hexdigest() != candidate['sha256']):
                    raise ValueError('The retained candidate patch changed.')
                from .candidate_checks import prepare_baseline,retained_baseline
                observed=source_state(config,candidate['targets'])
                requalified=None
                if observed!=candidate['basis']:
                    requalified=retained_baseline(root,unit,candidate,observed)
                    if requalified is None:
                        changed=sorted(k for k in observed if observed[k]!=candidate['basis'].get(k))
                        raise ValueError('The project changed since the retained candidate: '+', '.join(changed[:16])+
                            '. Inspect and replan the affected scope; the candidate remains preserved.')
                base,excluded,metadata=prepare_baseline(tree,unit,base,retained_patch=patch)
                if requalified and metadata:
                    metadata['retained_baseline_requalification']=requalified
                elif requalified:  # no generated baseline was prepared: the witness stays on the rung
                    row['retained_baseline_requalification']=requalified
                if metadata:row['host_baseline_metadata']=metadata
                git(tree, 'apply', '--binary', *(['--allow-empty'] if excluded else []),
                    *('--exclude='+p for p in excluded), str(patch))
                from .conversation_permissions import paths_allowed
                if not paths_allowed(changed_files(tree,base),unit['target_paths']):
                    raise ValueError('The retained candidate exceeds this revised plan; no checks or model started.')
            elif not waiting:
                from .candidate_checks import prepare_baseline
                base,_,metadata=prepare_baseline(tree,unit,base)
                if metadata:row['host_baseline_metadata']=metadata
            if candidate and candidate.get('review_only') and callable(getattr(review,'packet_preflight',None)):
                failure=review.packet_preflight(tree,changed_files(tree,base))
                if failure:
                    result={'verdict':'NOT_REVIEWED','reason':'Review preparation requires a candidate correction.',
                        'audit':failure,'route':name,'files':sorted(changed_files(tree,base)),
                        'candidate':{k:v for k,v in candidate.items() if k!='review_only'}}
                    row.update(verdict='NOT_REVIEWED',reason=result['reason'],model_calls=0,calls_reserved=0)
                    break  # no baseline suite, implementer or paid review for an inadmissible unchanged packet
            verification_config = scoped_verifiers(config, unit, repository, tree)
            # Compile from the original defaults, before scoping erases their origin.
            commands = checks(config, unit, repository, tree)
            if not commands:
                raise ValueError('This unit names no verifier of the session and no tests tare can run (R0).')
            worker_commands={n:a for n,a in commands.items()
                if not (state.get('conversation_flow') and n=='conversation-diff')}
            from .folder_access import granted
            from .native_agent import MAX_INITIAL_INSTRUCTION_BYTES
            references=granted(config['folder_access']['grants']) if config.get('folder_access') else []
            command_runtimes=[]
            if state.get('conversation_flow') and config.get('command_protection'):
                from .command_protection import prepared_checks
                command_runtimes=list(dict.fromkeys(a[0] for a in prepared_checks(commands).values()))
            advice_keys=('review_feedback','verification_feedback','previous_candidate_changes','implementation_feedback')
            advice={k:unit[k] for k in advice_keys if unit.get(k)}
            from tare_tools_agent_runtime import NativeMessage
            handoff=None
            if advice:
                text='Previous host observations and unverified review advice. These do not expand the original goal, criteria or permissions:\n'
                text+=json.dumps(advice,ensure_ascii=False,sort_keys=True)
                if len(text.encode('utf8'))>16384:
                    raise ValueError('Correction evidence exceeds the existing 16 KiB inbox bound (R0); no checks or model started. Preserve its complete receipt.')
                handoff=NativeMessage('correction-'+hashlib.sha256(text.encode('utf8')).hexdigest(),text)
            def receive_advice(consumed):
                return handoff if handoff is not None and handoff.message_id not in consumed else None
            # Append new correction evidence through the existing Runtime inbox,
            # after the original instruction. Never truncate findings or replace
            # that stable prompt prefix to fit the initial-instruction limit.
            brief_unit={k:v for k,v in unit.items() if k not in advice_keys}
            brief_options=dict(references=references,executables=command_runtimes,host_review=bool(review))
            instruction, inputs = (('', []) if candidate and candidate.get('review_only') else
                worker_brief(brief_unit,worker_commands,args.get('note'),**brief_options))
            if state.get('conversation_flow') and config.get('command_protection'):
                if control is None:
                    raise ValueError('Protected verification requires the current execution control.')
                from .command_protection import conversation_verifier, prepared_checks
                commands=prepared_checks(commands)
                registered = {**session_suite(verification_config, repository), **commands}
                check_token = CHECK_PORT.set(conversation_verifier(control, job, config, deployment, tree, unit, registered))
            elif default_checks:
                from .challenger import local
                from .command_protection import conversation_verifier
                registered = {**session_suite(verification_config, repository), **commands}
                # This rung's deployment produced the code under check (a new attempt, a recheck, a waiting worker):
                # a local model's work never runs outside the sandbox on the project's saved "always".
                checked = outside
                if outside is not None and deployment.get('native_profile') and local(deployment):
                    checked = lambda name, argv, why: outside(name, argv, why, local_producer=True)
                check_token = CHECK_PORT.set(conversation_verifier(control or check_control, job, config, deployment,
                    tree, unit, registered, outside=checked, cancelled=stop))
            # Import can create UID sidecars for existing scripts. Capture only
            # missing sidecars before the trusted host checks, never worker edits.
            missing_uids = [n+'.uid' for n in git(tree, 'ls-files', '--cached', '-z').decode().split('\0')
                if n.endswith('.gd') and not (tree/(n+'.uid')).exists()] if 'godot-import' in commands else []
            before, row['before'] = run_checks(tree, commands)
            base, generated = imported_uid_baseline(tree, base, missing_uids)
            if generated:
                row['verification_metadata'] = generated
            suite = session_suite(verification_config, repository)
            if suite == commands and (before=='green' or len(suite)==1 or candidate and candidate.get('review_only')):
                known=before if before=='green' or len(suite)==1 else 'inconclusive'
                suite_before={n:known for n in suite}
            else:
                suite_before={n:s for n,(s,_) in suite_states(tree,suite).items()}
            child = {'schema': 'tare.tools/native-agent-config/1', 'role': 'implementer',
                     'repositories': {repository: str(tree)}, 'writable': {repository: list(unit['target_paths'])},
                     'verifiers': {n: {'repository': repository, 'argv': a} for n, a in worker_commands.items()},
                     'capabilities': ['read', 'list', 'search', 'edit', 'verify'], 'specgraph_edit_context': False}
            if inputs:child['input_artifacts']=inputs
            if state.get('conversation_flow') and (graph:=planned_backlog(unit,repository,tree)):
                child['project_graph']=graph
            if config.get('folder_access'):
                child['folder_access'] = config['folder_access']
            if state.get('conversation_flow') and config.get('project_admission'):
                child['project_admission'] = config['project_admission']
            if state.get('conversation_flow') and config.get('command_protection'):
                child['command_protection'] = config['command_protection']
                from .command_protection import encode_context, prepared_checks
                child['command_context'] = config.get('command_context') or encode_context(unit)
                child['command_runtimes'] = command_runtimes
                if 'command' in config.get('capabilities', []): child['capabilities'].append('command')
            session_path=None
            if candidate and candidate.get('review_only'):
                outcome = {'status': 'PROPOSED', 'model_calls': 0}
                row['reused_candidate'] = candidate['patch']
            else:
                from .native_spend import settings
                parent_spend, _ = settings(state.get('deployment') or {})
                if waiting and parent_spend:
                    raise ValueError('A metered waiting worker needs explicit remaining-spend reconciliation; no model call started (R5).')
                if state.get('conversation_flow') and parent_spend:
                    from copy import deepcopy
                    from decimal import Decimal
                    _, tariff = settings(deployment)
                    if tariff is None or tariff.currency != parent_spend.currency or spend_model is None:
                        raise ValueError('The implementer needs a matching tariff and the remaining shared spend allowance (R5).')
                    allowance = spend_model.report()['remaining_amount']
                    if Decimal(allowance) <= 0:
                        raise ValueError('No shared spend allowance remains for the implementer (R5).')
                    deployment = deepcopy(deployment)
                    deployment['native_spend_policy'] = {'max_amount': allowance, 'currency': parent_spend.currency,
                        'allow_estimated_counts': parent_spend.allow_estimated_counts}
                    row['amount_committed'] = allowance
                if stop():
                    raise ValueError('Execution stopped before implementer dispatch; no model call started (R5).')
                selected_limits = child_limits(custody,state,batch.share,deployment=deployment)
                row['calls_reserved'] = selected_limits['max_model_calls']
                (job/'receipt.json').write_text(json.dumps({**receipt, 'rungs': receipt['rungs'] + [row]}), encoding='utf-8')
                with local_slot(deployment):
                    from .execution_resources import selected
                    if selected('money'):
                        row['amount_committed'] = None
                        (job/'receipt.json').write_text(json.dumps({**receipt, 'rungs': receipt['rungs'] + [row]}), encoding='utf-8')
                    session_path=waiting[1] if waiting else job/f'session-{rung+1}'
                    from .native_routes import executed_attempt, executed_decision
                    from .execution_scope import current, pin_matches
                    implementer_pin = current().pin('implementer')
                    trace_mode = ('RESUME_SAVED_ROUTE' if waiting else
                                  'EXACT_TEMPORARY_PIN' if implementer_pin and pin_matches(implementer_pin, deployment) else
                                  'CONFIGURED' if args.get('route') else 'AUTOMATIC')
                    row['executed_decision'] = executed_decision(trace_mode, name, visited,
                        [(earlier, 'EARLIER_RUNG_ENDED') for earlier in visited[:-1]])
                    attempt = executed_attempt(deployment, {'status': 'DISPATCHING'})
                    if waiting:
                        attempt['resume'] = True
                    row['attempts'] = [attempt]  # one row per rung; earlier rungs keep their own attempts
                    (job/'receipt.json').write_text(json.dumps({**receipt, 'rungs': receipt['rungs'] + [row]}), encoding='utf-8')
                    if waiting:
                        path=worker['folder_access_request']['path']
                        note=('[tare] Read access is now granted for '+path+'. ' if waiting[3] else
                              '[tare] The user did not grant read access to '+path+'. Continue without it; do not '
                              'request it again for this unit. ')
                        note+='Continue the saved unit using your existing observations. Keep all original criteria and checks. '
                        note+=(args.get('note') or '')
                        keep_tree=False
                        outcome=run_session(None,None,session_path,resume=True,follow_up=note,
                            message_id='folder-granted-'+uuid.uuid4().hex, invocation_limits=selected_limits,
                            **({'receive_message':receive_advice} if handoff is not None else {}),
                            **({'cancellation': control.cancellation, 'parent_clock': control.monotonic}
                               if control is not None else {}))
                        row['resumed_session']=str(session_path)
                    else:
                        outcome = run_session(child, deployment, session_path, instruction,
                                              limits=selected_limits, delegated_leaf=True,
                                              **({'receive_message':receive_advice} if handoff is not None else {}),
                                              **({'cancellation': control.cancellation, 'parent_clock': control.monotonic}
                                                 if control is not None else {}))
                    keep_tree=False
                if selected('money'):
                    from .native_spend import outcome_commitment
                    row['amount_committed'] = outcome_commitment(outcome)
                elif row.get('amount_committed') is not None:
                    spend = outcome.get('spend') or {}
                    if spend.get('currency') == parent_spend.currency and spend.get('committed_amount') is not None:
                        row['amount_committed'] = spend['committed_amount']
            row.update(child_status=outcome['status'],child_reason=outcome.get('reason'),
                model_calls=outcome.get('model_calls',0)-(worker['model_calls'] if waiting else 0))
            if row.get('attempts'):
                from .native_routes import settle_attempt
                settle_attempt(row['attempts'][-1], outcome)
            (job/'receipt.json').write_text(json.dumps({**receipt, 'rungs': receipt['rungs'] + [row]}), encoding='utf-8')
            changed = changed_files(tree, base)
            if state.get('conversation_flow') and changed and not stop():
                # Make new paths visible to diff --check before paying for an
                # independent audit. Intent-to-add keeps their content unstaged.
                added_or_modified = [name for name, raw in changed.items() if raw is not None]
                if added_or_modified:
                    git(tree, 'add', '--intent-to-add', '--', *added_or_modified)
            if state.get('conversation_flow') and stop():
                after, row['after'] = 'inconclusive', 'Not run: execution was stopped.'
            elif candidate and candidate.get('review_only'):
                # No writer ran between these two phases. Keep the named check
                # evidence; the independent audit and application gates remain.
                after,row['after']=before,row['before']
                row['host_recheck_verification_reused']=True
            else:
                after, row['after'] = run_checks(tree, commands)
            if row.get('host_baseline_metadata'):
                from .candidate_checks import MANIFEST
                checked_changes=changed_files(tree,base)
                verifier_edits={n for n in set(changed)|set(checked_changes)
                                if changed.get(n)!=checked_changes.get(n)}
                if verifier_edits-{MANIFEST}:
                    raise ValueError('Host verification changed authored candidate files; nothing was applied.')
                changed=checked_changes
            verdict_, reason = verdict(before, after, changed, [] if review else protected, lambda: run_checks(tree, commands)[0])
            if state.get('conversation_flow'):
                from .conversation_permissions import paths_allowed
                if not paths_allowed(changed,unit['target_paths']):
                    verdict_,reason='NOT_VERIFIED','Candidate changed files outside the saved plan; nothing was applied.'
            row.update(verdict=verdict_, reason=reason, before_state=before, after_state=after, files=sorted(changed))
            if changed:
                if row.get('host_baseline_metadata'):
                    patch = git(tree, 'diff', '--binary', base, '--')
                else:
                    git(tree, 'add', '--all', '--', *changed)
                    patch = git(tree, 'diff', '--cached', '--binary', base, '--')
                (job/f'patch-{rung+1}.diff').write_bytes(patch)
                row['patch'] = str(job/f'patch-{rung+1}.diff')
            result = {'verdict': verdict_, 'reason': reason, 'route': name, 'files': sorted(changed),
                      'checks_before': row['before'], 'checks_after': row['after']}
            if outcome['status']!='PROPOSED':
                result['implementation_stop']=interruption_feedback(session_path,row)
            report=outcome.get('output')
            if isinstance(report,str) and report.strip():
                result['implementer_report']={'basis':'Unverified implementer report; host checks and diff remain authoritative.',
                    'text':report[:2000],'truncated':len(report)>2000}
            if changed and review is not None:
                from .conversation_tasks import source_state
                result['candidate'] = {'patch': row['patch'],
                    'sha256': hashlib.sha256(Path(row['patch']).read_bytes()).hexdigest(),
                    'producer': producer_reference(job,deployment), 'targets': proof, 'basis': basis}
            if state.get('conversation_flow') and stop():
                result.update(verdict='INCONCLUSIVE', reason='Execution was stopped; no candidate was applied.', patch=row.get('patch'))
                break
            if (state.get('conversation_flow') and outcome.get('folder_access_request')
                    and outcome['status']=='PROPOSED' and not stop()
                    and paths_allowed(changed,unit['target_paths'])):
                from .worker_wait import retain
                result.update(verdict='INCONCLUSIVE',reason='The implementer is waiting for read access; its context and private candidate are preserved.',
                    folder_access_request=outcome['folder_access_request'],
                    worker=retain(tree,base,session_path,unit,deployment,name,proof,basis,outcome))
                row.update(verdict='INCONCLUSIVE',reason=result['reason'])
                keep_tree=True
                break  # consent cannot be replaced by checks or an audit of partial work
            if verdict_ == 'VERIFIED' and review is not None:
                observed=verification_evidence(session_path) if session_path is not None else []
                if candidate:
                    previous_job=Path(candidate['patch']).parent
                    sessions=sorted(previous_job.glob('session-*'))
                    if previous_job.resolve().is_relative_to(Path(custody).resolve()/'units') and sessions:
                        observed=[*verification_evidence(sessions[-1]),*observed]
                assessment = review(tree, deployment, unit, changed, {'before': row['before'], 'after': row['after'],
                    **({'host_baseline_metadata':row['host_baseline_metadata']} if row.get('host_baseline_metadata') else {}),
                    **({'host_recheck':{'mode':'CHECKS_AND_AUDIT_ONLY','implementer_calls_started':0,
                        'candidate_sha256':candidate['sha256']}} if candidate and candidate.get('review_only') else {}),
                    **({'observed_named_checks':observed} if observed else {})})
                result['audit'] = row['audit'] = assessment
                # Only the host sets user_decision, after the user's yes to a same-family audit that left open
                # items only (conversation_permissions.confirm_apply); the checks above already passed.
                if assessment.get('verdict') != 'ACCEPT' and assessment.get('user_decision') != 'APPLY_WITHOUT_ACCEPTED_AUDIT':
                    result.update(verdict='NOT_REVIEWED', patch=row.get('patch'), reason=AUDIT_INCOMPLETE
                                  if assessment.get('verdict') == 'INCONCLUSIVE' else AUDIT_REFUSED)
                    break
                from .conversation_review import reference_sources_current
                if not reference_sources_current(config,assessment.get('reference_sources',[])):
                    result.update(verdict='NOT_REVIEWED',reason='An observed audit reference changed or was revoked before application.',patch=row['patch'])
                    break
                if changed_files(tree, base) != changed:
                    result.update(verdict='NOT_REVIEWED', reason='The candidate changed during review.', patch=row['patch'])
                    break
                if source_state(config, proof) != basis:
                    result.update(verdict='NOT_REVIEWED', reason='The project evidence changed during execution or review.', patch=row['patch'])
                    break
                dependencies = [s for s in assessment.get('sources', []) if s['path'] not in changed]
                if any(source_state(config, {s['repository']: [s['path']]})[s['repository']+':'+s['path']] != s['sha256']
                       for s in dependencies):
                    result.update(verdict='NOT_REVIEWED', reason='An observed audit dependency changed before application.', patch=row['patch'])
                    break
                if stop():
                    result.update(verdict='NOT_REVIEWED', reason='Execution stopped after review; no candidate was applied.', patch=row['patch'])
                    break
            if verdict_ == 'VERIFIED':
                with batch.apply:  # each change is checked on the tree the earlier ones left, then applied
                    moved = stale(tree, batch.applied)
                    again = run_checks(tree, commands) if moved else ('green', '')
                    if again[0] != 'green':
                        applied, files = 'CONFLICT', sorted(changed)
                        why = 'its checks fail with the units applied before it (' + ', '.join(moved) + '):\n' + again[1]
                    else:
                        allowed, why = suite_allows(verification_config, unit, repository, tree, changed, suite_before)
                        if review is not None and changed_files(tree, base) != changed:
                            label = (result.get('audit') or {}).get('independence')
                            allowed, why = False, 'The candidate changed after '+('review ('+label+')' if label else 'independent review')+'.'
                        baseline_metadata=row.get('host_baseline_metadata')
                        if baseline_metadata:
                            from .candidate_checks import source_digest
                            if source_digest(root)!=baseline_metadata['source_sha256']:
                                allowed,why=False,'A generated-manifest source changed during verification; recheck the retained candidate.'
                        edits = []
                        if allowed and review_effect is not None:
                            from compute_plane.native_review import capture, digest, save_edit
                            for file, raw in changed.items():
                                edit_id = review_effect+':'+hashlib.sha256(file.encode()).hexdigest()[:16]
                                before_raw = capture(Path(root)/file)
                                args_ = {'repository': repository, 'path': file, 'sha256': digest(before_raw)}
                                if raw is None:
                                    args_.update(delete=True, changes=[])
                                result_ = {'sha256': digest(raw)}
                                save_edit(Path(custody)/'review', edit_id, args_, before_raw, raw, result_)
                                edits.append({'effect_id': edit_id, **args_, 'after': digest(raw), 'deleted': raw is None})
                        application_base=(row.get('host_baseline_metadata') or {}).get('base_before',base)
                        applied, files = apply(Path(root), tree, application_base, changed, config['writable'].get(repository) or []) \
                            if allowed else ('CONFLICT' if moved else 'HELD', sorted(changed))
                        if applied == 'APPLIED' and edits:
                            result['review_edits'] = edits
                    if applied == 'APPLIED':
                        batch.applied.update(changed)
                result.update(applied=applied == 'APPLIED', application=applied, application_files=files, suite=why)
                if applied != 'APPLIED':
                    result['patch'] = row.get('patch')
                break
            result['patch'] = row.get('patch')
            if state.get('conversation_flow'):
                break  # a completed failed unit needs diagnosis, not another alias or larger model
        except CheckCleanupUnknown as exc:
            keep_tree=True
            row.update(verdict='INCONCLUSIVE',reason=str(exc)[:300])
            result={'verdict':'INCONCLUSIVE','reason':row['reason'],'route':name,'retained_tree':str(tree)}
            break
        except (ValueError, OSError, KeyError, subprocess.SubprocessError) as exc:
            row.update(verdict='INCONCLUSIVE', reason=str(exc)[:300])
            result = {'verdict': 'INCONCLUSIVE', 'reason': row['reason'], 'route': name}
            if keep_tree and worker:
                result['worker']={**worker,'producer':producer_reference(Path(worker['session']).parent,worker['producer'])}
            if state.get('conversation_flow') or 'R0' in row['reason'] or 'R5' in row['reason'] or candidate is not None:
                break
        finally:
            if check_token is not None:
                check_runner=CHECK_PORT.get()
                CHECK_PORT.reset(check_token)
                if callable(getattr(check_runner,'close',None)):check_runner.close()
            receipt['rungs'].append(row)
            if not keep_tree:
                try:
                    with batch.git:
                        git(root, 'worktree', 'remove', '--force', str(tree), timeout=120)
                except (ValueError, subprocess.SubprocessError):
                    pass
                if tree.exists():
                    from .worker_wait import discard
                    try:discard(tree)
                    except OSError:row['candidate_cleanup']='deferred: files are still in use'
    if refused and not visited:
        result['reason'] = refused[0]
    result['rungs'] = len(receipt['rungs'])
    receipt.update(result=result, finished=now())
    (job/'receipt.json').write_text(json.dumps(receipt, indent=1), encoding='utf-8')
    record(custody, unit, result)
    result['receipt'] = str(job/'receipt.json')
    result['next'] = {'VERIFIED': 'Applied: read the changed files if you need to, then go on.' if result.get('applied')
                      else 'Verified but not applied (see application): merge the patch yourself or re-plan.',
                      'NOT_VERIFIED': 'Not applied. Read the checks output; fix it directly or hand the unit again.',
                      'NOT_REVIEWED': 'Not applied. Address the independent review findings; preserve this plan and candidate evidence.',
                      'INCONCLUSIVE': 'Not applied. Read the reason; the patch, if any, is kept.'}[result['verdict']]
    if state.get('conversation_flow') and not result.get('applied'):
        result['next'] = 'The candidate is not delivered. Inspect its checks, review and retained patch; continue through the implementer or ask the user about an unresolved decision.'
    elif state.get('conversation_flow'):
        # The reply follows this English result: a Portuguese request got its summary in English (2026-11-03).
        audit = result.get('audit') or {}
        label = audit.get('audit_label')
        if audit.get('user_decision') == 'APPLY_WITHOUT_ACCEPTED_AUDIT':
            result['next'] = ('Applied by the user\'s decision after its checks, although its audit (' + label + ') did '
                'not accept it (local-only mode). Its open items: ' + ' | '.join(str(i)[:300] for i in audit.get('remaining', [])[:10]) +
                '. Tell the user in plain words, in the language of their request, what changed, that no audit accepted '
                'it, and these open items; never say it was audited or accepted.')
        else:
            result['next'] = ('Applied after its checks and ' + ('a ' + label + ', which the user confirmed (local-only '
                              'mode); say plainly that the review was not independent' if label else
                              'a review ' + audit['independence'] + '; say plainly that the review was not independent'
                              if audit.get('independence') else 'an independent review') +
                              '. Tell the user in plain words, in the language of their request, what changed and how it was checked.')
    return result


def run_units(custody, state, args, stop, *, check_control=None):
    from .challenger import local
    """Several units of the plan handed at once (D8): in parallel when the session asks (`parallel`) or the operator
    turned it on (experiment implement_parallel), PARALLEL_CHILDREN at a time with one on a local route and the
    session's calls split between them; in series otherwise. A unit whose change no longer fits the tree the
    others left is run again on it, once. The session's own checks run on the result at the end."""
    items, ledger = args['units'], read_ledger(custody)
    if any(k in args for k in ('unit', 'title', 'target_paths', 'criteria', 'verifiers', 'test_paths')):
        raise ValueError('Give either units or one unit, not both.')
    plan = {u.get('key'): u for u in (ledger.get('plan') or {}).get('units') or [] if isinstance(u, dict)}
    keys = [i.get('unit') for i in items]
    if len(set(keys)) != len(keys) or not all(k in plan for k in keys):
        raise ValueError(f'Give 1 to {MAX_UNITS} different units of your plan (plan tool).')
    done = {k for k, v in (ledger.get('units') or {}).items() if v.get('state') == 'APPLIED'}
    waiting = [k for k in keys if set(plan[k].get('depends_on') or []) - done]
    if waiting:
        raise ValueError('Hand these after the units they depend on are applied: ' + ', '.join(waiting))
    try:
        cap = min(4, max(1, int(os.environ.get('TARE_PARALLEL_CHILDREN') or PARALLEL_CHILDREN)))
    except ValueError:
        cap = PARALLEL_CHILDREN
    parent = state.get('deployment') or {}
    local_session = bool(parent.get('native_profile')) and local(parent)
    asked = bool(args.get('parallel') or state.get('implement_parallel'))
    # A local session's children run on the same GPU, one request at a time: parallel would only queue them (D8).
    workers = min(len(items), cap) if asked and not local_session else 1
    batch = Batch(workers)

    def one(item, batch):
        try:
            return run_unit(custody, state, item, stop, batch,
                            **({'check_control': check_control} if check_control is not None else {}))
        except Exception as exc:  # one unit's refusal or defect never loses the others' results
            return {'verdict': 'INCONCLUSIVE', 'reason': f'{type(exc).__name__}: {exc}'[:300]}

    started = datetime.now(timezone.utc)
    with ThreadPoolExecutor(workers) as pool:
        futures = [pool.submit(contextvars.copy_context().run, one, item, batch) for item in items]
        results = [f.result() for f in futures]
    for index, item in enumerate(items):
        others = any(r.get('applied') for j, r in enumerate(results) if j != index)
        if results[index].get('application') == 'CONFLICT' and not stop():
            results[index] = {**one(item, Batch()), 'run_again': 'its first change no longer fit the tree'}
        elif workers > 1 and others and results[index]['verdict'] == 'NOT_VERIFIED' and not stop():
            # Its tests may run code another unit of the batch brings: its own copy did not have it yet (the
            # phase 3 comparison, 2026-10-06). Once, on the tree with the others in, before the session pays for it.
            results[index] = {**one(item, Batch()), 'run_again': 'it failed alone, before the units applied with it'}
    report = {'children_at_once': workers, 'seconds': round((datetime.now(timezone.utc) - started).total_seconds(), 1),
              'units': {}}
    if asked and local_session:
        report['parallel'] = 'refused: a local route serves one request at a time, so the units ran in series'
    for key, result in zip(keys, results):
        if result.get('applied'):  # applied: the checks' output is in the receipt
            result = {k: v for k, v in result.items() if k not in ('checks_before', 'checks_after')}
        report['units'][key] = result
    config, touched = state['config'], set()
    for key, result in zip(keys, results):
        if result.get('applied'):
            touched.add(plan[key].get('repository') or next(iter(config['repositories'])))
    for repository in sorted(touched):
        suite = session_suite(config, repository)
        if not suite:
            report.setdefault('checks_at_end', {})[repository] = 'this session has no verifier for it: run its suite before you deliver'
            continue
        token = None
        if check_control is not None and not config.get('command_protection') and CHECK_PORT.get() is None:
            # The project itself, not a candidate: the sandbox writes nowhere in it but its own scratch.
            from .command_protection import conversation_verifier
            token = CHECK_PORT.set(conversation_verifier(check_control, Path(custody)/'units', config, {},
                config['repositories'][repository], {'repository': repository, 'target_paths': [],
                'title': 'Run the session checks on the applied units', 'criteria': []}, suite, cancelled=stop))
        try:
            report.setdefault('checks_at_end', {})[repository] = dict(zip(
                ('state', 'output'), run_checks(config['repositories'][repository], suite)))
        finally:
            if token is not None:
                runner = CHECK_PORT.get()
                CHECK_PORT.reset(token)
                if callable(getattr(runner, 'close', None)):
                    runner.close()
    return report


def kept_patches(custody, since):
    """Units the session ran since `since` (epoch seconds) whose change was not applied but whose patch is kept:
    [(key, outcome, patch)], for the answer to point at."""
    kept = []
    for receipt in sorted(Path(custody).glob('units/*/receipt.json')):
        try:
            if receipt.stat().st_mtime < since:
                continue
            value = json.loads(receipt.read_text(encoding='utf-8'))
            result = value.get('result') or {}
            if not result.get('applied') and result.get('patch'):
                kept.append((value['unit']['key'], result.get('application') or result['verdict'], result['patch']))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return kept


def record(custody, unit, result):
    """The unit's state in the session's ledger (work.json): its last verdict and the last three runs."""
    with LEDGER_LOCK:
        write_record(custody, unit, result)


def write_record(custody, unit, result):
    from compute_plane.native_journal import save_json
    path = Path(custody)/LEDGER
    ledger = read_ledger(custody) or {'schema': 'tare.tools/work-ledger/1', 'plan': {}, 'problems': [], 'units': {}}
    entry = (ledger.setdefault('units', {}).get(unit['key'])) or {}
    state = 'APPLIED' if result.get('applied') else 'CONFLICT' if result.get('application') == 'CONFLICT' \
        else result['verdict']
    run = {'ts': now(), 'verdict': result['verdict'], 'route': result.get('route'),
        'rungs': result.get('rungs'), 'reason': result.get('reason'),
        **({'verification_contract_digest': unit['verification_contract_digest']} if unit.get('verification_contract_digest') else {})}
    if audit_refusal(run) and result.get('audit'):
        last = (entry.get('history') or [{'verdict': None}])[-1]
        findings = result['audit'].get('findings') or []
        claims = sorted({claim_digest(f['claim']) for f in findings if f.get('claim')})[:MAX_LEDGER_CLAIMS]
        before = set(last.get('claims') or ()) if audit_refusal(last) else set()
        # The auditor's mark, or a claim identical to one of the previous refused audit: never un-marked.
        run.update(claims=claims, repeated=any(f.get('repeated') is True for f in findings) or bool(before & set(claims)),
                   refusals=(last.get('refusals', 1) if audit_refusal(last) else 0) + 1)
    history = (entry.get('history') or []) + [run]
    ledger['units'][unit['key']] = {'state': state, 'history': history[-3:], 'paths': sorted(unit.get('target_paths') or [])}
    save_json(path, ledger)


class ImplementTools:
    """The session's `implement` tool over its other tools (experiment implement_child)."""

    def __init__(self, tools, custody, state):
        from compute_plane.native_models import encode, object_json
        self.tools, self.custody, self.state = tools, Path(custody), state
        self.root = self.custody  # SharedModel reads the session's journal here
        self.names = (*tools.names, 'implement')
        self.tool_specs_json = encode({**object_json(tools.tool_specs_json), **UNIT_SPEC})

    def __getattr__(self, name):
        return getattr(self.tools, name)

    def calls_committed(self):
        from .delegation import child_calls
        return child_calls(self.custody)

    def execute(self, request, control):
        from compute_plane.native_models import encode, object_json
        from tare_tools_agent_runtime import EffectResult
        if request.tool_call.name != 'implement':
            return self.tools.execute(request, control)
        if request.session_id != self.session_id or control.binding != self.binding or control.should_stop():
            return EffectResult(request.effect_id, 'refused', encode({'reason': 'CONTROL_OR_SESSION_MISMATCH'}))
        try:
            args = object_json(request.tool_call.arguments_json)
            if len(json.dumps(args).encode('utf-8')) > 16384:
                raise ValueError('Use the declared arguments, within 16 KiB.')
            if any(not isinstance(p, str) or not literal(p) for p in args.get('target_paths') or []):
                raise ValueError('Give relative literal paths.')
            output = run_units(self.custody, self.state, args, control.should_stop, check_control=control) \
                if args.get('units') else run_unit(self.custody, self.state, args, control.should_stop, check_control=control)
            return EffectResult(request.effect_id, 'succeeded', encode(output), True)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            return EffectResult(request.effect_id, 'refused', encode({'reason': str(exc)[:500]}))
        except Exception as exc:  # a defect in the tool is refused with its type, never an interrupted session
            return EffectResult(request.effect_id, 'refused', encode({'reason': f'{type(exc).__name__}: {exc}'[:500]}))

    def recover_effect(self, journal):
        from compute_plane.native_effect_recovery import pending
        from compute_plane.native_models import encode
        from tare_tools_agent_runtime import EffectResult
        request = pending(journal, self.session_id)
        if request is None or request.tool_call.name != 'implement':
            return self.tools.recover_effect(journal)
        # A change reaches the tree only after its checks passed, but file by file: an interruption while applying
        # can leave part of it written. The session reads the target files before anything else.
        return request, EffectResult(request.effect_id, 'refused', encode({
            'reason': 'Interrupted before a result was recorded: part of a verified change may have been written. '
                      'Read the target files first, then call implement again if needed.'}))
