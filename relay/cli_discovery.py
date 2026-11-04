"""Find installed vendor CLIs and offer their qualified profiles, without spending a call.

A CLI is 'found' when installed and 'ready' once it answered a proof the owner asked for (tare setup --prove,
/setup): one real, tiny generation through the normal inference path, cached per executable file.
Codex models come from Codex's own per-account cache (~/.codex/models_cache.json) when it
exists, otherwise from the shipped templates. Discovery keeps the profiles it wrote current (the
cache records each written file's hash); a profile the user wrote or edited is never replaced.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
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


def desired_profiles(templates, endpoint, executable):
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
        wanted = desired_profiles(templates, endpoint, executable)
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
