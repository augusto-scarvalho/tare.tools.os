"""tare-node-update: keep the GPU node on the latest CI-verified tare.tools.os main.

tare-node-update.timer runs it as the unprivileged service user every 30 minutes (never as root). It fetches main
into ~/src/tare.tools.os, and when main changed what the service runs (node/tare_node/, node/pyproject.toml; anything
else only moves the recorded commit), the commit passed the required GitHub CI check (`node (Linux flock)`), and the
node is quiet (no text request in flight, no reservation), it builds the wheel in a clean directory, installs it in
~/.venvs/tare-node and ~/.venvs/comfyui, restarts the gateway via a scoped sudoers rule, and waits for it to answer;
a gateway that does not come back gets the previous wheel again. ComfyUI restarts only when its hook changed, and
only with no image job (else at a later run). State, wheels and the last outcome live in ~/.local/state/tare-node/.
`--install-units` (root) writes the unprivileged systemd unit, timer, and scoped sudoers drop-in.
"""
import argparse
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

GATEWAY = 'http://127.0.0.1:8080'
DEFAULT_REPO = 'augusto-scarvalho/tare.tools.os'
REQUIRED_CHECK = 'node (Linux flock)'
FAILED_CONCLUSIONS = frozenset({'failure', 'cancelled', 'timed_out', 'action_required'})
ALLOWED_UNITS = frozenset({'llm-inference.service', 'comfyui.service'})
USER_RE = re.compile(r'^[a-z_][a-z0-9_-]{0,31}$')
HOOK_FILES = ('node/tare_node/comfy_gpu.py', 'node/tare_node/coordinated_comfy.py', 'node/tare_node/gpu_lease.py')
SERVICE_FILES = ('node/tare_node', 'node/pyproject.toml')  # what the installed wheel carries; nothing else restarts
SERVICE = """[Unit]
Description=Keep the node on the latest tare.tools.os main (tare-node-update)
After=network-online.target llm-inference.service
Wants=network-online.target

[Service]
Type=oneshot
User={user}
ExecStart={home}/.venvs/tare-node/bin/tare-node-update --home {home} --user {user}
"""
SUDOERS = (
    '{user} ALL=(root) NOPASSWD: '
    '/bin/systemctl restart llm-inference.service, '
    '/bin/systemctl restart comfyui.service, '
    '/usr/bin/systemctl restart llm-inference.service, '
    '/usr/bin/systemctl restart comfyui.service\n'
)
TIMER = """[Unit]
Description=Run tare-node-update every 30 minutes

[Timer]
OnBootSec=10min
OnUnitActiveSec=30min
RandomizedDelaySec=2min
Persistent=true

[Install]
WantedBy=timers.target
"""


def _validate_user(user):
    if not isinstance(user, str) or not USER_RE.fullmatch(user):
        raise ValueError(f'invalid service user name: {user!r}')
    return user


def _reject_symlink(path, *, stop_at=None):
    target = Path(path)
    stop = Path(stop_at).resolve() if stop_at is not None else None
    cur = target
    while True:
        if cur.is_symlink():
            raise ValueError(f'refusing symlink path: {cur}')
        if stop is not None and cur.parent == cur:
            break
        if stop is not None and cur == stop:
            break
        if cur.parent == cur:
            break
        cur = cur.parent


def _write_text_atomic(path, content, *, mode=None):
    path = Path(path)
    _reject_symlink(path, stop_at=path.parent)
    if path.parent.is_symlink():
        raise ValueError(f'refusing symlink parent directory: {path.parent}')
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix='.' + path.name + '.', dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        if mode is not None and hasattr(os, 'fchmod'):
            os.fchmod(fd, mode)
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def quiet(status):
    """(True, None) when restarting the gateway breaks nothing in flight, else (False, why)."""
    coordination = status.get('gpu_coordination') or {}
    if coordination.get('reservation'):
        return False, 'a GPU reservation is active'
    queue = status.get('text_queue') or {}
    if queue.get('running') or queue.get('waiting'):
        return False, 'text requests are in flight'
    return True, None


def image_busy(status):
    """An image or training job holds or waits for the GPU: ComfyUI must not restart now."""
    coordination = status.get('gpu_coordination') or {}
    owner = coordination.get('owner') or {}
    return owner.get('kind') == 'image' or any(
        isinstance(t, dict) and t.get('kind') == 'image' for t in coordination.get('queue') or [])


class Node:
    """The node's side effects, one place to fake in tests."""

    def __init__(self, home, user, *, repo=DEFAULT_REPO):
        self.home, self.user, self.repo = Path(home), _validate_user(user), repo
        self.src, self.state_dir = self.home/'src/tare.tools.os', self.home/'.local/state/tare-node'

    def run(self, *argv):
        return subprocess.run(argv, check=True, capture_output=True, text=True, timeout=600).stdout.strip()

    def latest(self):
        self.run('git', '-C', str(self.src), 'fetch', '--quiet', 'origin', 'main')
        return self.run('git', '-C', str(self.src), 'rev-parse', 'origin/main')

    def verified(self, commit):
        """Require the GitHub CI `node (Linux flock)` check run to succeed on this exact commit (fail closed)."""
        if not re.fullmatch(r'[0-9a-f]{40}', str(commit or '')):
            return False
        headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'tare-node-update'}
        token = os.environ.get('GITHUB_TOKEN', '').strip()
        if token:
            headers['Authorization'] = 'Bearer ' + token
        url = f'https://api.github.com/repos/{self.repo}/commits/{commit}/check-runs?per_page=100'
        try:
            with urlopen(Request(url, headers=headers), timeout=15) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
        except (OSError, URLError):
            return False
        if len(raw) > 2 * 1024 * 1024:
            return False
        try:
            data = json.loads(raw.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            return False
        runs = data.get('check_runs')
        if not isinstance(runs, list):
            return False
        required_passed = False
        for run in runs:
            if not isinstance(run, dict):
                continue
            status = run.get('status')
            conclusion = run.get('conclusion')
            if status == 'completed' and conclusion in FAILED_CONCLUSIONS:
                return False
            if run.get('name') == REQUIRED_CHECK and status == 'completed' and conclusion == 'success':
                required_passed = True
        return required_passed

    def changed(self, old, new, paths):
        try:
            diff = self.run('git', '-C', str(self.src), 'diff', '--name-only', old, new, '--', *paths)
        except subprocess.CalledProcessError:  # the recorded commit is not in this checkout (moved, rewritten): rebuild
            return True
        return bool(diff)

    def build(self, commit):
        self.run('git', '-C', str(self.src), 'checkout', '--quiet', '--detach', commit)
        build_dir = self.src/'node/build'
        if build_dir.is_symlink():
            raise ValueError(f'refusing symlink build directory: {build_dir}')
        if build_dir.exists():
            shutil.rmtree(build_dir)
        wheels_root = self.state_dir/'wheels'
        _reject_symlink(wheels_root, stop_at=self.home)
        out = wheels_root/commit[:12]
        if out.is_symlink():
            raise ValueError(f'refusing symlink wheel output directory: {out}')
        if out.exists():
            shutil.rmtree(out)
        out.mkdir(parents=True, exist_ok=True)
        self.run(str(self.home/'.venvs/tare-node/bin/pip'), 'wheel', '--quiet', '--no-deps', '-w', str(out),
                 str(self.src/'node'))
        built = sorted(p for p in out.glob('*.whl') if p.is_file() and not p.is_symlink())
        if len(built) != 1:
            raise ValueError(f'expected exactly one built wheel in {out}, found {len(built)}')
        return str(built[0])

    def install(self, wheel):
        wheel_path = Path(wheel)
        _reject_symlink(wheel_path, stop_at=self.home)
        for venv in ('tare-node', 'comfyui'):
            self.run(str(self.home/'.venvs'/venv/'bin/pip'), 'install', '--quiet', '--no-deps', '--force-reinstall',
                     str(wheel_path))

    def restart(self, unit):
        if unit not in ALLOWED_UNITS:
            raise ValueError(f'refusing to restart unauthorized systemd unit: {unit!r}')
        if hasattr(os, 'geteuid') and os.geteuid() != 0:
            self.run('sudo', '-n', 'systemctl', 'restart', unit)
        else:
            self.run('systemctl', 'restart', unit)

    def _gateway_headers(self):
        token_path = self.home/'.local/state/tare-qualified-models/api-token'
        try:
            if not token_path.is_symlink() and token_path.is_file():
                token = token_path.read_text(encoding='utf-8').strip()
                if token:
                    return {'Authorization': 'Bearer ' + token}
        except OSError:
            pass
        return {}

    def status(self):
        with urlopen(Request(GATEWAY + '/v1/fleet/status', headers=self._gateway_headers()), timeout=5) as response:
            return json.load(response)

    def healthy(self, seconds=60):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                value = self.status()
                if value.get('role') == 'qualified-model-gateway' and (value.get('gpu_coordination') or {}).get('enabled'):
                    return True
            except (OSError, ValueError):
                pass
            time.sleep(1)
        return False

    def load(self):
        try:
            path = self.state_dir/'installed.json'
            _reject_symlink(path, stop_at=self.home)
            return json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}

    def save(self, value):
        _reject_symlink(self.state_dir, stop_at=self.home)
        _write_text_atomic(self.state_dir/'installed.json', json.dumps(value, indent=1))


def update(node):
    """One run: (exit code, what happened)."""
    state = node.load()
    latest = node.latest()
    if latest == state.get('commit'):
        if state.get('comfy_pending') and not image_busy(node.status()):
            node.restart('comfyui.service')
            node.save({**state, 'comfy_pending': False})
            return 0, f'up to date at {latest[:7]}; ComfyUI restarted'
        return 0, f'up to date at {latest[:7]}'
    if state.get('commit') and not node.changed(state['commit'], latest, SERVICE_FILES):
        node.save({**state, 'commit': latest})
        return 0, f'at {latest[:7]}; nothing the service runs changed'
    if not node.verified(latest):
        return 0, f'{latest[:7]} waits: CI check not yet verified'
    ok, why = quiet(node.status())
    if not ok:
        return 0, f'{latest[:7]} waits: {why}'
    wheel = node.build(latest)
    node.install(wheel)
    node.restart('llm-inference.service')
    if not node.healthy():
        if state.get('wheel'):
            node.install(state['wheel'])
            node.restart('llm-inference.service')
        node.save({**state, 'failed': latest, 'failed_at': time.time()})
        return 1, f'{latest[:7]} did not come up; back on {str(state.get("commit"))[:7]}'
    comfy = not state.get('commit') or node.changed(state['commit'], latest, HOOK_FILES)
    restarted = comfy and not image_busy(node.status())
    if restarted:
        node.restart('comfyui.service')
    node.save({'commit': latest, 'wheel': wheel, 'installed_at': time.time(), 'comfy_pending': comfy and not restarted})
    return 0, f'installed {latest[:7]}' + ('; ComfyUI restarted' if restarted else '; ComfyUI later' if comfy else '')


def install_units(home, user, systemd=Path('/etc/systemd/system'), sudoers=Path('/etc/sudoers.d')):
    user = _validate_user(user)
    home = str(Path(home))
    _write_text_atomic(Path(systemd)/'tare-node-update.service', SERVICE.format(home=home, user=user))
    _write_text_atomic(Path(systemd)/'tare-node-update.timer', TIMER)
    if sudoers is not None:
        _write_text_atomic(Path(sudoers)/'tare-node-update', SUDOERS.format(user=user), mode=0o440)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', 'enable', '--now', 'tare-node-update.timer'], check=True)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='tare-node-update', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--home', default=str(Path.home()))
    parser.add_argument('--user', default=os.environ.get('SUDO_USER') or os.environ.get('USER') or getpass.getuser())
    parser.add_argument('--repo', default=DEFAULT_REPO, help='GitHub owner/repo checked for CI verification')
    parser.add_argument('--install-units', action='store_true',
                        help='write and enable unprivileged tare-node-update.timer and scoped sudoers rule (root)')
    args = parser.parse_args(argv)
    if args.install_units:
        install_units(args.home, args.user)
        print('tare-node-update.timer enabled: every 30 minutes (unprivileged service user)')
        return 0
    if hasattr(os, 'geteuid') and os.geteuid() == 0:
        print('failed: refusing to run update as root; re-run with --install-units to install the unprivileged unit')
        return 1
    try:
        code, outcome = update(Node(args.home, args.user, repo=args.repo))
    except (OSError, subprocess.SubprocessError, ValueError, StopIteration) as exc:
        code, outcome = 1, f'failed: {type(exc).__name__}: {getattr(exc, "stderr", None) or exc}'
    print(outcome)
    return code


if __name__ == '__main__':
    sys.exit(main())
