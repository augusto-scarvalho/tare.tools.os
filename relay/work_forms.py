"""Ways of working inside a session: what each needs in this installation and project, and a local record of the
one each session turn used. Report and record only; nothing here changes how a session works."""
from datetime import datetime, timezone
from itertools import takewhile
import json
import os
from pathlib import Path
import shutil
from compute_plane.managed_process import host_executable
import threading

# Test runners by ecosystem: the files that declare one, its runner (a project wrapper first), the whole suite and
# how one unit of work narrows it to its own tests, which `implement` will verify with. Detection only.
ECOSYSTEMS = (
    ('cargo', ('Cargo.toml',), ('cargo',), ['test'], 'cargo test <test name filter>'),
    ('go', ('go.mod',), ('go',), ['test', './...'], 'go test ./<package directory>'),
    ('dotnet', ('*.sln', '*.csproj'), ('dotnet',), ['test'], 'dotnet test --filter <name>'),
    ('maven', ('pom.xml',), ('mvnw', 'mvn'), ['test'], 'mvn test -Dtest=<class>'),
    ('gradle', ('build.gradle', 'build.gradle.kts'), ('gradlew', 'gradle'), ['test'], 'gradle test --tests <class>'),
)
NODE_RUNNERS = ('jest', 'vitest', 'mocha')  # they take test files after `npm test --`

# Three things are called plan; the report names them apart.
PLANS = {'plan (session tool)': "the session writes its own plan into its ledger (plan tool)",
         'delegate_plan': 'a separate read-only model returns planning advice to the coordinator',
         '/plan': 'a reviewed planner turns a demand into graph work',
         'tare serve /plan': 'a read-only planning answer in a serve chat'}


def runner(root, names):
    for name in names:
        for wrapper in (root/name, root/(name+'.cmd'), root/(name+'.bat')):
            if wrapper.is_file():
                return str(wrapper)
        found = shutil.which(name)
        if found:
            return found
    return None


def ecosystems(root):
    """The test runners this project declares, whether each is installed and how a unit would narrow it."""
    from .turn_engine import VERIFY_MAX_TEST_FILES, project_python, python_tests
    root = Path(root)
    found = []
    test_files = python_tests(root)
    if test_files is not None:
        found.append({'name': 'pytest', 'runner': project_python(root), 'suite': ['-m', 'pytest', '-q'],
                      'narrow': 'pytest <test file or file::test>',
                      'whole_suite_verifier': test_files <= VERIFY_MAX_TEST_FILES})
    try:
        script = (json.loads((root/'package.json').read_text(encoding='utf-8')).get('scripts') or {}).get('test') \
            if (root/'package.json').is_file() else None
    except (ValueError, OSError, AttributeError):
        script = None
    if isinstance(script, str) and script:
        found.append({'name': 'npm', 'runner': shutil.which('npm'), 'suite': ['test'],
                      'narrow': 'npm test -- <test file>' if any(r in script for r in NODE_RUNNERS) else None})
    for name, markers, names, suite, narrow in ECOSYSTEMS:
        if any(next(root.glob(marker), None) for marker in markers):
            found.append({'name': name, 'runner': runner(root, names), 'suite': suite, 'narrow': narrow})
    return found


def availability(root, mode=None):
    """Each way of working, whether it is available here and what is missing; no model, network or test run."""
    from .delegation import policy as exploration
    from .flow_assessment import policy_path
    from .turn_engine import project_permission_mode
    root = Path(root).resolve()
    mode = mode or project_permission_mode(root)
    found = ecosystems(root)
    try:
        settings = exploration()
        explore = {'available': settings['enabled'], 'explorer': settings['profile'] if isinstance(
            settings['profile'], str) else 'a variant', 'missing': [] if settings['enabled'] else ['exploration on in /delegation']}
    except (ValueError, OSError) as exc:
        explore = {'available': False, 'missing': ['readable exploration settings: ' + str(exc)[:120]]}
    implement = [why for ok, why in (
        (host_executable('git'), 'git on PATH'),
        ((root/'.git').exists(), 'a git repository'),
        (mode != 'read', 'edit permission (the project is read-only)'),
        (any(e['runner'] and e['narrow'] for e in found), "a test runner that can run one unit's tests"),
    ) if not ok]
    try:
        flows = policy_path()
        flow_policy = str(flows) if flows else 'absent: no flow is suggested; choose /session, /explore, /plan or /research'
    except ValueError as exc:
        flow_policy = 'unreadable: ' + str(exc)[:120]
    from .delegation import experiments
    from .environment import summary
    trials = experiments()
    plan_on = trials['plan_tool']
    implement += [] if trials['implement_child'] else ['the implement_child experiment on']
    return {'root': str(root), 'permission_mode': mode or 'not chosen', 'forms': {
                'direct': {'available': True, 'missing': []}, 'explore': explore,
                'plan': {'available': plan_on, 'missing': [] if plan_on else ['the plan_tool experiment on']},
                'delegate_plan': {'available': trials['planner_child'],
                                 'missing': [] if trials['planner_child'] else ['delegated planning on in /delegation']},
                'implement': {'available': not implement, 'missing': implement}},
            'ecosystems': found, 'flow_policy': flow_policy, 'plans': PLANS, 'machine': summary()}


def availability_text(report):
    lines = [f"Ways of working in {report['root']} · permission {report['permission_mode']}"]
    for name, form in report['forms'].items():
        state = 'available' if form['available'] else 'unavailable'
        lines.append(f'{name}: {state}' + ('; needs ' + ', '.join(form['missing']) if form['missing'] else ''))
    for e in report['ecosystems']:
        lines.append(f"tests: {e['name']} via {e['runner'] or 'a runner that is not installed'}; one unit: "
                     f"{e['narrow'] or 'no narrow form'}" + ('' if e.get('whole_suite_verifier', True) else
                                                            '; the suite is too large to verify each turn whole'))
    if not report['ecosystems']:
        lines.append('tests: no test runner detected')
    lines.append('flow suggestions: ' + report['flow_policy'])
    machine = report.get('machine') or {}
    if 'error' in machine:
        lines.append('this machine: setup unreadable, every route refused: ' + machine['error'])
    elif not machine.get('saved'):
        lines.append('this machine: not set up yet; every route tare finds may be used')
    else:
        lines.append('this machine: ' + '; '.join(
            f"{c['label']} {c['state']}" + (' except ' + ', '.join(c['declined']) if c['declined'] else '')
            for c in machine['categories'].values()))
    return '\n'.join(lines)


# What hosted Jev is asked about each request, for decision D7: whether a judge would choose the way of working
# better than the session does. Shadow only: the answer is recorded beside what the session did, never followed.
FORM_QUESTION = 'Which way of working fits this software request best?'
FORM_OPTIONS = {
    'direct': 'Answer or change it in this session: read, edit and verify directly.',
    'explore': 'Investigate the code read-only first, with explorers, before deciding what to do.',
    'plan': 'Write a plan of separate units, each with its tests, before changing code.',
    'implement': 'Hand a bounded change that existing tests can check to a separate implementer, then verify it.',
}


def judge_form(text):
    """Ask hosted Jev, while the turn runs, which way of working fits `text`; returns a function that gives its
    verdict (waiting at most 5 s more) or None. Needs TYPESAFE_API_KEY, and the request text goes to TypeSafe;
    never the local judge, which would take the local GPU."""
    from .environment import decides
    from .turn_engine import jev_key
    if os.environ.get('TARE_WORK_FORM_SHADOW', '').lower() == 'off' or not (text or '').strip() \
            or not jev_key() or not decides('jev'):
        return lambda: None
    from .turn_engine import judges
    answer = {}
    def ask():
        answer.update(judges({'state': json.dumps({'user_request':text},ensure_ascii=False), 'options': FORM_OPTIONS, 'question': FORM_QUESTION},
                             local=False))
    thread = threading.Thread(target=ask, daemon=True)
    thread.start()
    def verdict():
        thread.join(5)
        return answer.get('jev')
    return verdict


def record_turn(config, deployment, result, conversation=False, jev=None):
    """One local row per session turn, never sent anywhere: the way of working it used, where it ran, on what class
    of route, its verifier runs and how it ended. The shadow that phase 0 of the work forms plan measures.
    TARE_WORK_FORM_SHADOW=off turns it off."""
    if os.environ.get('TARE_WORK_FORM_SHADOW', '').lower() == 'off':
        return
    try:
        from .challenger_variants import accounting
        from .turn_engine import judge_log
        activity = result.get('tool_activity') or {}
        operations = [item['operation'] for item in activity.get('items', [])]
        checks = [item['state'] for item in activity.get('items', []) if item['operation'] == 'verify']
        failed_in_a_row = sum(1 for _ in takewhile(lambda state: state == 'failed', reversed(checks)))
        limits = result.get('effective_limits') or {}
        judge_log('work-form', {
            'ts': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'form': next((f for f in ('implement', 'plan', 'explore') if f in operations), 'direct'),
            'flow': os.environ.get('TARE_FLOW'), 'client': os.environ.get('TARE_CLIENT'),
            'role': config['role'], 'conversation': bool(conversation),
            'class': accounting(deployment) if (deployment or {}).get('native_profile') else None,
            'model': (deployment or {}).get('native_profile', {}).get('model'),
            'verifiers': sorted(config.get('verifiers') or {}), 'verify_runs': len(checks),
            'verify_failed': checks.count('failed'), 'verify_failed_in_a_row': failed_in_a_row,
            'last_verify': checks[-1] if checks else None, 'edits': operations.count('edit'),
            'tools': len(operations), 'tools_omitted': activity.get('omitted', 0),
            'status': result['status'], 'reason': result.get('reason'), 'model_calls': result.get('model_calls'),
            'max_model_calls': limits.get('max_model_calls'), 'max_seconds': limits.get('max_seconds'),
            'seconds': round(result.get('elapsed_from_preparation_ms', 0)/1000, 1),
            'tokens': result.get('tokens'), 'session': result.get('session'), **({'jev': jev} if jev else {}),
            'units': units_of(result.get('session'))})
    except Exception:  # ponytail: a shadow never fails the turn it records
        pass


def units_of(session):
    """{state: count} of the session's units in its ledger (work.json), or {} without one."""
    try:
        ledger = json.loads((Path(session)/LEDGER).read_text(encoding='utf-8')) if session else {}
    except (OSError, ValueError):
        return {}
    counts = {}
    for unit in (ledger.get('units') or {}).values():
        counts[unit.get('state')] = counts.get(unit.get('state'), 0) + 1
    return counts


def digest(rows):
    """The shadow at a glance: turns by class and form, and how verifiers went."""
    if not rows:
        return {}
    count = lambda key: {k: sum(str(r.get(key)) == k for r in rows) for k in sorted({str(r.get(key)) for r in rows})}
    return {'turns': len(rows), 'classes': count('class'), 'forms': count('form'), 'flows': count('flow'),
            'roles': count('role'),
            'with_verifier': sum(bool(r.get('verifiers')) for r in rows),
            'verify_runs': sum(r.get('verify_runs', 0) for r in rows),
            'verify_failed': sum(r.get('verify_failed', 0) for r in rows),
            'turns_ending_on_two_failures': sum(r.get('verify_failed_in_a_row', 0) >= 2 for r in rows),
            'jev': {k: sum((r.get('jev') or {}).get('choice') == k for r in rows) for k in FORM_OPTIONS},
            'jev_agreed': sum((r.get('jev') or {}).get('choice') == r.get('form') for r in rows),
            'units': {s: sum((r.get('units') or {}).get(s, 0) for r in rows)
                      for s in sorted({s for r in rows for s in (r.get('units') or {})})}}


MAX_UNITS = 8
LEDGER = 'work.json'
LEDGER_BYTES = 65536
UNIT = {'type': 'object', 'properties': {
    'key': {'type': 'string', 'description': 'Short id: lowercase letters, digits and hyphens.'},
    'title': {'type': 'string'},
    'target_paths': {'type': 'array', 'items': {'type': 'string'}, 'description':
        'Relative literal paths this unit changes (files, or folders); no two units may share one.'},
    'criteria': {'type': 'array', 'items': {'type': 'string'}, 'description': 'How to tell the unit is done.'},
    'depends_on': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Keys of units that come first.'},
    'verifiers': {'type': 'array', 'items': {'type': 'string'}, 'description': "Names of this session's verifiers."},
    'test_paths': {'type': 'array', 'items': {'type': 'string'}, 'description':
        'Existing test files that check it; with pytest, file::test (or file::Class::test) names one test.'},
    'repository': {'type': 'string', 'description': 'Only when the session has several repositories.'}},
    'required': ['key', 'title', 'target_paths', 'criteria']}
PLAN_SPEC = {'plan': {
    'description': 'Write your plan for a change that spans several files or steps: up to 8 units, each with the '
        'paths it changes, its done criteria and the tests that check it. tare checks the plan without a model '
        '(paths exist, no two units share a path, named verifiers and tests exist, no dependency cycle) and keeps '
        'it in this session\'s ledger, so you can work through it unit by unit. A one-file change needs no plan.',
    'parameters': {'type': 'object', 'properties': {
        'units': {'type': 'array', 'items': UNIT, 'minItems': 1, 'maxItems': MAX_UNITS},
        'criteria': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Done criteria of the whole change.'},
        'remaining': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Open questions, if any.'}},
        'required': ['units', 'criteria'], 'additionalProperties': False}}}


def literal(path):
    """A relative literal path (the rules /plan applies to graph task scopes)."""
    from pathlib import PurePosixPath
    parts = PurePosixPath(path).parts
    return bool(parts) and '\\' not in path and ':' not in path and not path.startswith('/') and '..' not in parts \
        and '.git' not in parts and not any(c in path for c in '*?[]')


def lint(config, plan):
    """What is wrong with a session's plan, checked without a model: [] when nothing is."""
    import re
    from .work_parallel import overlap
    units, problems = plan.get('units'), []
    if not isinstance(units, list) or not 1 <= len(units) <= MAX_UNITS or not all(isinstance(u, dict) for u in units):
        return [f'Give 1 to {MAX_UNITS} units.']
    if not isinstance(plan.get('criteria'), list) or not plan['criteria']:
        problems.append('Give the done criteria of the whole change.')
    repositories, keys = config['repositories'], [u.get('key') for u in units]
    if len(set(map(str, keys))) != len(keys):
        problems.append('Unit keys must differ.')
    for unit in units:
        key = unit.get('key')
        if not isinstance(key, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,47}', key):
            problems.append(f'{key}: the key uses lowercase letters, digits and hyphens.')
            continue
        name = unit.get('repository') or (next(iter(repositories)) if len(repositories) == 1 else None)
        if name not in repositories:
            problems.append(f'{key}: name the repository ({", ".join(repositories)}).')
            continue
        root = Path(repositories[name])
        paths = unit.get('target_paths')
        if not isinstance(paths, list) or not paths or not all(isinstance(p, str) for p in paths):
            problems.append(f'{key}: list the paths it changes.')
            paths = []
        for path in paths:
            if not literal(path):
                problems.append(f'{key}: {path} is not a relative literal path.')
            elif not config.get('conversation_flow') and not (root/path).exists() and not (root/path).parent.is_dir():
                problems.append(f'{key}: neither {path} nor its folder exists.')
        if not isinstance(unit.get('criteria'), list) or not unit['criteria']:
            problems.append(f'{key}: give its done criteria.')
        for verifier in unit.get('verifiers') or []:
            if verifier not in (config.get('verifiers') or {}):
                problems.append(f'{key}: this session has no verifier named {verifier}.')
        for test in unit.get('test_paths') or []:
            name, _, node = test.partition('::') if isinstance(test, str) else ('', '', '')
            if not literal(name) or not ((root/name).is_file() or config.get('conversation_flow') and name in paths) or \
                    node and not re.fullmatch(r'[A-Za-z_]\w*(::[A-Za-z_]\w*)?', node):
                problems.append(f'{key}: the test {test} does not exist.')
        for other in unit.get('depends_on') or []:
            if other not in keys or other == key:
                problems.append(f'{key}: depends on an unknown unit {other}.')
    for index, unit in enumerate(units):
        for other in units[index+1:]:
            if overlap(unit.get('target_paths') or [], other.get('target_paths') or []):
                problems.append(f"{unit.get('key')} and {other.get('key')} change the same paths: merge or split them.")
    order, pending = [], {u.get('key'): set(u.get('depends_on') or []) for u in units}
    while pending:
        ready = [k for k, deps in pending.items() if deps <= set(order)]
        if not ready:
            problems.append('The units depend on each other in a cycle: ' + ', '.join(map(str, pending)) + '.')
            break
        order += ready
        for k in ready:
            pending.pop(k)
    return problems


def record_plan(custody, config, plan):
    """Check the plan and keep it in the session's ledger, work.json (at most 64 KiB), each new unit PENDING. A
    plan written again keeps what its units already went through, and the units it drops stay on record."""
    from compute_plane.native_journal import save_json
    problems = lint(config, plan)
    path = Path(custody)/LEDGER
    try:
        before = (json.loads(path.read_text(encoding='utf-8')).get('units') or {}) if path.is_file() else {}
    except (OSError, ValueError, AttributeError):
        before = {}
    planned = {u['key']: u for u in plan.get('units') or [] if isinstance(u, dict) and isinstance(u.get('key'), str)}
    before = {k: v for k, v in before.items() if isinstance(v, dict)} if isinstance(before, dict) else {}

    def entry(key):
        # A unit is what it changes, not its name: a unit planned again under another key, over the same paths,
        # keeps what the first went through, so R3 still counts its failures (the TUI walkthrough of 2026-10-07
        # re-planned a twice-failed unit under a new key and paid for two more children).
        paths = sorted(planned[key].get('target_paths') or [])
        if key in before:
            return {**before[key], 'paths': paths}
        for old_key, old in before.items():
            if old_key not in planned and old.get('state') != 'APPLIED' and set(old.get('paths') or []) & set(paths):
                return {**old, 'paths': paths}
        return {'state': 'PENDING', 'paths': paths}
    units = before if problems else {**{k: v for k, v in before.items() if v.get('state') != 'PENDING'},
                                     **{k: entry(k) for k in planned}}
    keys = list(planned)
    ledger = {'schema': 'tare.tools/work-ledger/1', 'ts': datetime.now(timezone.utc).isoformat(timespec='seconds'),
              'plan': plan, 'problems': problems, 'units': units}
    if len(json.dumps(ledger).encode('utf-8')) > LEDGER_BYTES:
        raise ValueError('The plan is over 64 KiB: fewer, shorter units.')
    save_json(path, ledger)
    if problems:
        return {'status': 'NEEDS_FIXES', 'problems': problems, 'next': 'Fix these and call plan again.'}
    return {'status': 'PLANNED', 'units': keys, 'ledger': str(path),
            'next': 'Work through the units in order, verifying each. If this plan should become graph Works with '
                    'a review by another model, suggest the operator take it to /plan as reviewed findings '
                    f'(python -m relay.planning_findings --kind session --source {Path(custody).resolve()}); '
                    'you never start that.'}


class PlanTools:
    """The session's `plan` tool over its other tools (phase 1 of the ways of working; experiment plan_tool)."""

    def __init__(self, tools, custody, state):
        from compute_plane.native_models import encode, object_json
        self.tools, self.custody, self.state = tools, Path(custody), state
        self.names = (*tools.names, 'plan')
        self.tool_specs_json = encode({**object_json(tools.tool_specs_json), **PLAN_SPEC})

    def __getattr__(self, name):
        return getattr(self.tools, name)

    def execute(self, request, control):
        from compute_plane.native_models import encode, object_json
        from tare_tools_agent_runtime import EffectResult
        if request.tool_call.name != 'plan':
            return self.tools.execute(request, control)
        if request.session_id != self.session_id or control.binding != self.binding or control.should_stop():
            return EffectResult(request.effect_id, 'refused', encode({'reason': 'CONTROL_OR_SESSION_MISMATCH'}))
        try:
            output = record_plan(self.custody, self.state['config'], object_json(request.tool_call.arguments_json))
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
        if request is None or request.tool_call.name != 'plan':
            return self.tools.recover_effect(journal)
        # Writing the ledger again is harmless: the plan is checked and saved whole.
        return request, EffectResult(request.effect_id, 'refused', encode({
            'reason': 'Interrupted before a result was recorded. Call plan again.'}))


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description='What each way of working needs in a project; no model calls.')
    parser.add_argument('root', type=Path, nargs='?', default=Path.cwd())
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    report = availability(args.root)
    print(json.dumps(report, indent=2) if args.json else availability_text(report))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
