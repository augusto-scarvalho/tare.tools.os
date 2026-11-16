"""Find installed vendor CLIs and offer their qualified profiles, without spending a call.

A CLI is 'found' when installed and 'ready' once it answered a proof the owner asked for (tare setup --prove,
/setup): one real, tiny generation through the normal inference path, cached per executable file.
Codex models come from Codex's own per-account cache (~/.codex/models_cache.json) when it
exists, otherwise from the shipped templates. Claude Code has no listing: a model id that one of its family aliases
resolves to and no template names yet is offered too (alias_models), read once per executable file. AGY lists its
models (`agy models`, per account): a newer version of a line its templates have is offered too (agy_profiles), read
again when the file changes or the reading is a day old.
Discovery keeps the profiles it wrote current (the
cache records each written file's hash); a profile the user wrote or edited is never replaced.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from compute_plane.managed_process import host_executable
import subprocess
import tempfile

TEMPLATES = Path(__file__).with_name('native_deployments')/'vendor_cli_templates.json'
# Name on PATH, then the vendors' default Windows install folders.
LOCATIONS = {
    'cli://claude': ('claude', ['%USERPROFILE%/.local/bin/claude.exe',
                                '%APPDATA%/npm/node_modules/@anthropic-ai/claude-code/bin/claude.exe']),
    'cli://codex': ('codex', ['%LOCALAPPDATA%/Programs/OpenAI/Codex/bin/codex.exe']),
    'cli://agy': ('agy', ['%LOCALAPPDATA%/agy/bin/agy.exe']),
}
ACK_PROFILE = {'cli://claude': 'claude-opus-5-5-cli', 'cli://codex': 'codex-cli', 'cli://agy': 'agy-cli'}
LABEL = {'cli://claude': 'Claude CLI', 'cli://codex': 'Codex CLI', 'cli://agy': 'AGY CLI'}


def locate(endpoint):
    name, known = LOCATIONS[endpoint]
    found = host_executable(name)
    for path in [found, *map(os.path.expandvars, known)]:
        # The Kernel refuses .cmd/.bat shims: arguments would pass through cmd.exe.
        if path and Path(path).is_file() and Path(path).suffix.lower() not in {'.cmd', '.bat'}:
            # Keep the launcher path as found: resolving it follows Codex's link into a versioned
            # release folder, and the profile then keeps running that release after updates.
            return os.path.abspath(path)
    return None


def deployment(template, executable):
    value = json.loads(json.dumps(template))
    value['native_cli'] = {'executable': executable, **value['native_cli']}
    return value


def codex_catalog():
    """The models Codex lists for the signed-in account and installed version, or None."""
    home = Path(os.environ.get('CODEX_HOME') or Path.home()/'.codex')
    try:
        models = json.loads((home/'models_cache.json').read_text(encoding='utf-8'))['models']
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return models if isinstance(models, list) and models else None


def claude_aliases(executable):
    """Which model id each Claude Code family alias resolves to, read with zero tokens (four CLI starts)."""
    from compute_plane.vendor_guidance import claude_models
    from .vendor_models import LINES
    return claude_models(executable, [r['claude_code_alias'] for r in LINES.values() if 'claude_code_alias' in r])


def alias_models(cache, executable):
    """The alias reading of this executable file: taken once, then kept in the cache until the file changes. A
    reading that fails is kept as empty (the templates alone are used) so a broken CLI is not started on every
    discovery; the next Claude Code update reads again."""
    held = cache.get('claude_aliases')
    if not (isinstance(held, dict) and held.get('fingerprint') == fingerprint(executable)):
        try:
            reading = claude_aliases(executable)
            held = {'cli_version': reading['cli_version'], 'models': reading['models']}
        except Exception:
            held = {'cli_version': '', 'models': {}}
        held['fingerprint'] = fingerprint(executable)
        cache['claude_aliases'] = held
    return held


def alias_profiles(wanted, reading):
    """Profiles for the models Claude Code's aliases resolve to that are a newer version of a line the templates
    have (claude-fable-5-3 beside claude-fable-5-2-cli), copied from that line's newest template."""
    from .vendor_models import line, parse
    found = {}
    models = reading.get('models') if isinstance(reading.get('models'), dict) else {}
    for alias, model in sorted(models.items()):
        # The id becomes a file name: only claude-<line>-<one or two numbers>.
        if not isinstance(model, str) or not re.fullmatch(r'claude-[a-z]+-\d+(-\d+)?', model) or not line(model):
            continue
        vendor, name, version = parse(model)
        same = {n: parse(t['native_profile']['model'])[2] for n, t in wanted.items()
                if parse(t['native_profile']['model'])[:2] == (vendor, name)}
        if not same or version <= max(same.values()):
            continue
        base = max(same, key=lambda n: (same[n], n))  # claude-opus-5-5-cli before the claude-cli base
        value = deepcopy(wanted[base])
        value['id'] = model+'-cli'
        value['model'] = value['native_profile']['model'] = model
        value['native_profile'].pop('expected_response_model', None)
        value['native_profile']['source'] = (f"Claude Code {reading.get('cli_version')} alias reading ({alias} -> {model}), "
                                             f"copied from {base}; account availability unchecked")
        found[value['id']] = value
    return found


def agy_listing(executable):
    """The model ids `agy models` prints for the signed-in account (no model call): one 'id<TAB>Display' per line."""
    done = subprocess.run([executable, 'models'], capture_output=True, text=True, timeout=60,
                          stdin=subprocess.DEVNULL, cwd=str(Path(executable).absolute().parent))
    if done.returncode:
        raise OSError(f'agy models exited {done.returncode}')
    # The id becomes a profile name: lowercase letters, digits, dots and hyphens only.
    return [i for i in (l.split('	')[0].strip() for l in done.stdout.splitlines() if '	' in l)
            if re.fullmatch(r'[a-z0-9][a-z0-9.-]*', i)]


def agy_models(cache, executable):
    """The `agy models` reading, kept in the cache and taken again when the executable file changes or the last
    attempt is a day old (the list depends on the account too). An attempt that fails, or lists no id, keeps the
    last ids; it counts as the attempt, so a CLI that cannot list is not started on every discovery.
    `read_at` moves only when the ids change, so a profile's source does not change every day."""
    held = cache.get('agy_models')
    held = held if isinstance(held, dict) else {}
    now = datetime.now(timezone.utc)
    try:
        fresh = held.get('fingerprint') == fingerprint(executable) and now - datetime.fromisoformat(held['checked_at']) < timedelta(days=1)
    except (KeyError, TypeError, ValueError):
        fresh = False
    if not fresh:
        try:
            ids = agy_listing(executable)
        except Exception:
            ids = None
        held = {'ids': held.get('ids') or [], 'read_at': held.get('read_at', ''), 'fingerprint': fingerprint(executable),
                'checked_at': now.isoformat()}
        if ids:
            if sorted(ids) != sorted(held['ids']):
                held['read_at'] = now.isoformat()
            held['ids'] = ids
        cache['agy_models'] = held
    return held


def agy_profiles(wanted, reading):
    """Profiles for the AGY models that are a newer version of a line the templates have (gemini-4.1-argon beside
    agy-gemini-4-argon-cli), copied from that line's newest template. AGY lists one id per effort: the map is rebuilt
    from the listed ids for the efforts the template maps and never gains one (the Kernel takes only those)."""
    from .vendor_models import AGY_EFFORT, line, parse
    efforts = {}
    for i in reading.get('ids') or []:
        if AGY_EFFORT.search(i):
            efforts.setdefault(AGY_EFFORT.sub('', i), set()).add(AGY_EFFORT.search(i)[1])
    found = {}
    for base, listed in sorted(efforts.items()):
        if not line(base):
            continue
        vendor, name, version = parse(base)
        same = {n: parse(AGY_EFFORT.sub('', t['native_profile']['model']))[2] for n, t in wanted.items()
                if parse(AGY_EFFORT.sub('', t['native_profile']['model']))[:2] == (vendor, name)}
        if not same or version <= max(same.values()):
            continue
        template = max(same, key=lambda n: (same[n], n))
        value = deepcopy(wanted[template])
        mapped = [e for e in value['native_reasoning_models'] if e in listed]
        params = json.loads(value['native_profile'].get('parameters_json') or '{}')
        if not mapped:
            continue
        effort = params.get('effort')
        rule = f"the template's effort {effort}"
        if effort not in mapped:  # nearest listed effort, the lower one on a tie
            order = ('low', 'medium', 'high', 'xhigh', 'max')
            effort = min(mapped, key=lambda e: (abs(order.index(e)-order.index(params.get('effort', 'high'))), order.index(e)))
            rule = f"nearest listed effort {effort}, the template's {params.get('effort')} is not listed"
        params['effort'] = effort
        value['id'] = f'agy-{base.replace(".", "-")}-cli'
        value['model'] = value['native_profile']['model'] = f'{base}-{effort}'
        value['native_profile']['parameters_json'] = json.dumps(params, separators=(',', ':'))
        value['native_reasoning_models'] = {e: f'{base}-{e}' for e in mapped}
        value['native_profile'].pop('expected_response_model', None)
        value['native_profile']['source'] = (f"Exact model IDs from agy models {reading.get('read_at', '')[:10]}, copied from "
                                             f"{template}, {rule}; account availability unchecked")
        found[value['id']] = value
    return found


def desired_profiles(templates, endpoint, executable, reading=None):
    wanted = {name: deployment(template, executable) for name, template in templates.items()
              if template['native_profile']['endpoint'] == endpoint}
    models = codex_catalog() if endpoint == 'cli://codex' else None
    if models:
        from .native_profiles import local_path
        from .native_generation import load_deployment
        from .vendor_models import expand
        # ponytail: the shipped Codex templates are only the fallback when Codex has no cache.
        wanted = {n: v for n, v in wanted.items() if not n.startswith('codex-cli')}
        bases = {'codex-cli': deployment(templates['codex-cli'], executable)}
        if local_path('codex-native').is_file():  # the owner's direct ChatGPT route: its models follow Codex too
            bases['codex-native'] = load_deployment(local_path('codex-native'))
        wanted.update(expand(bases, models))
    if endpoint == 'cli://claude' and reading:
        wanted.update(alias_profiles(wanted, reading))
    if endpoint == 'cli://agy' and reading:
        wanted.update(agy_profiles(wanted, reading))
    return wanted


def write_profile(name, value, managed):
    """Create, or replace a profile discovery wrote itself; returns 'added', 'updated' or None."""
    from .native_generation import load_deployment, MAX_DEPLOYMENT_BYTES
    from .native_profiles import local_path
    target = local_path(name)
    raw = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n').encode('utf-8')
    if len(raw) > MAX_DEPLOYMENT_BYTES:
        raise ValueError('native_deployment_file_over_budget')
    digest = hashlib.sha256(raw).hexdigest()
    if target.exists():
        current = hashlib.sha256(target.read_bytes()).hexdigest()
        if current == digest:
            managed[name] = digest  # exactly what discovery writes: adopt it
            return None
        if managed.get(name) != current:
            return None  # written or edited by the user
    with tempfile.TemporaryDirectory(prefix='tare-cli-profile-') as folder:
        source = Path(folder)/'profile.json'
        source.write_bytes(raw)
        load_deployment(source)  # validate before it can replace anything
        target.parent.mkdir(parents=True, exist_ok=True)
        state = 'updated' if target.exists() else 'added'
        staged = target.with_suffix('.json.tmp')
        staged.write_bytes(raw)
        os.replace(staged, target)
    managed[name] = digest
    return state


def retire_profile(name, endpoint, managed):
    """Delete a profile discovery wrote for this CLI and no longer offers; user-edited files stay."""
    from .native_profiles import local_path
    target = local_path(name)
    try:
        raw = target.read_bytes()
    except FileNotFoundError:
        managed.pop(name, None)
        return False
    if (hashlib.sha256(raw).hexdigest() != managed[name]
            or json.loads(raw)['native_profile']['endpoint'] != endpoint):
        return False
    target.unlink()
    managed.pop(name)
    return True


def acknowledge(value):
    """One real generation; (ready, reason)."""
    from .native_generation import generate_text, load_deployment
    with tempfile.TemporaryDirectory(prefix='tare-cli-ack-') as folder:
        path = Path(folder)/'profile.json'
        path.write_text(json.dumps(value), encoding='utf-8')
        loaded = load_deployment(path)
        ok, content, receipt = generate_text(loaded, loaded['native_profile']['model'],
                                             'Reply with exactly: OK', 120, attempt_label='cli-discovery')
    if ok and content.strip():
        return True, ''
    return False, str(receipt.get('failure_class') or 'empty reply')[:200]


def cache_file():
    from .native_installation import storage_root
    return storage_root()/'cli-discovery.json'


def fingerprint(executable):
    stat = Path(executable).stat()
    return [executable, stat.st_size, stat.st_mtime_ns]


def proven(endpoint, executable):
    """Record a CLI that answered a proof (tare setup --prove), so discovery offers its models."""
    try:
        cache = json.loads(cache_file().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        cache = {}
    cache[endpoint] = {'fingerprint': fingerprint(executable), 'checked_at': datetime.now(timezone.utc).isoformat()}
    cache_file().parent.mkdir(parents=True, exist_ok=True)
    cache_file().write_text(json.dumps(cache, indent=1), encoding='utf-8')


LOGIN = {'cli://codex': (['login', 'status'], 'codex login'), 'cli://claude': (['auth', 'status'], 'claude auth login')}


def logged_in(endpoint, executable):
    """Whether a CLI is signed in, asked locally (no model call, no quota); None when it cannot tell (AGY)."""
    if endpoint not in LOGIN:
        return None
    try:
        done = subprocess.run([executable, *LOGIN[endpoint][0]], capture_output=True, text=True, timeout=20,
                              stdin=subprocess.DEVNULL, cwd=str(Path(executable).absolute().parent))
    except (OSError, subprocess.SubprocessError):
        return None
    if endpoint == 'cli://claude':
        try:
            return json.loads(done.stdout).get('loggedIn') is True
        except ValueError:
            return None
    return done.returncode == 0


def discover():
    """Offer the models of every installed CLI this machine does not decline, without spending a call: a proof
    (tare setup --prove, /setup) is the owner's choice, and a CLI that answered one is 'ready'."""
    templates = json.loads(TEMPLATES.read_text(encoding='utf-8'))
    cache_path = cache_file()
    try:
        cache = json.loads(cache_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        cache = {}
    report = []
    from .environment import allows
    for endpoint in ACK_PROFILE:
        row = {'endpoint': endpoint, 'label': LABEL[endpoint], 'added': [], 'updated': [], 'removed': []}
        report.append(row)
        if not allows({'native_profile': {'protocol': 'vendor-cli', 'endpoint': endpoint}})[0]:
            row['status'] = 'not used'  # declined on this machine: never located
            continue
        executable = locate(endpoint)
        if executable is None:
            row['status'] = 'missing'
            continue
        if logged_in(endpoint, executable) is False:  # its models would fail when chosen: say how to sign in
            row.update(status='failed', reason=f'not signed in (run {LOGIN[endpoint][1]})')
            continue
        row['status'] = 'ready' if cache.get(endpoint, {}).get('fingerprint') == fingerprint(executable) else 'found'
        managed = cache.setdefault('managed', {})
        reading = {'cli://claude': alias_models, 'cli://agy': agy_models}.get(endpoint, lambda *a: None)(cache, executable)
        wanted = desired_profiles(templates, endpoint, executable, reading)
        for name, value in sorted(wanted.items()):
            state = write_profile(name, value, managed)
            if state:
                row[state].append(name)
        for name in sorted(set(managed) - set(wanted)):
            if retire_profile(name, endpoint, managed):
                row['removed'].append(name)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, indent=1), encoding='utf-8')
    return report


def summary(report):
    """One line for the TUI, or '' when nothing changed and nothing failed."""
    report = list(report)
    parts = [f"{r['label']} {r['status']} · {len(r['added'])} model(s) added" for r in report if r['added']]
    parts += [f"{r['label']} · {len(r['updated'])} model(s) updated" for r in report if r.get('updated')]
    parts += [f"{r['label']} · {len(r['removed'])} retired model(s) removed" for r in report if r.get('removed')]
    parts += [f"{r['label']} found but not usable: {r['reason']}" for r in report if r['status'] == 'failed']
    return '; '.join(parts)
