"""Point the node's services at this installed version instead of a tare.tools.local-labs checkout.

The systemd overrides on the node carry arguments that grew over time (--gpu-lock, --comfy-url, --route-timeout):
only the program changes. The gateway runs ~/.venvs/tare-node/bin/tare-node-gateway with the packaged fleet and
image-route configs; ComfyUI runs ~/.venvs/comfyui/bin/tare-node-comfy (the hook lives in ComfyUI's own Python).
Without --apply it prints the overrides it would write. With --apply (as root, on the node, with the owner) it keeps
each previous override as .bak, links ~/.local/bin/gpu and gpu-run, reloads systemd and restarts the gateway;
ComfyUI is restarted by hand once no image job runs. Rollback: put the .bak files back, daemon-reload, restart.
"""
import argparse
import os
import secrets
import shlex
import subprocess
from pathlib import Path

from tare_node.update import (
    _default_service_home,
    _default_service_user,
    _validate_home,
    _validate_user,
    install_units,
)

try:
    import pwd
except ImportError:  # pragma: no cover - non-POSIX
    pwd = None

OVERRIDES = {'llm-inference.service': 'zzz-shared-gpu.conf', 'comfyui.service': 'zzz-shared-gpu.conf'}
PACKAGED = {'--config': 'config/qualified_model_fleet.json', '--image-routes': 'config/comfy_image_routes.json'}


def rewrite(text, home):
    """Render @TARE_NODE_HOME@ and move known ExecStart commands to installed entry points.
    Other lines and already rewritten or unknown commands keep their arguments."""
    out = []
    for line in text.splitlines():
        if line.startswith('ExecStart=') and line != 'ExecStart=':
            argv = [part.replace('@TARE_NODE_HOME@', home) for part in shlex.split(line[len('ExecStart='):])]
            line = 'ExecStart=' + ' '.join(map(shlex.quote, entry(argv, home)))
        out.append(line)
    return '\n'.join(out) + '\n'


def entry(argv, home):
    if len(argv) >= 2 and argv[1].endswith('tools/serving/qualified_model_gateway.py'):
        rest, kept = argv[2:], []
        while rest:
            flag = rest.pop(0)
            if flag in PACKAGED and rest and rest[0].endswith(PACKAGED[flag]):
                rest.pop(0)  # the packaged copy is the default
            else:
                kept.append(flag)
        return [f'{home}/.venvs/tare-node/bin/tare-node-gateway', *kept]
    if len(argv) >= 2 and argv[1].endswith('tools/serving/coordinated_comfy.py'):
        return [f'{home}/.venvs/comfyui/bin/tare-node-comfy', *argv[2:]]
    return argv


def _reject_symlink(path):
    target = Path(path)
    if target.is_symlink():
        raise ValueError(f'refusing symlink path: {target}')


def _write_no_symlink(path, content):
    path = Path(path)
    _reject_symlink(path)
    _reject_symlink(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path, flags, 0o644)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        handle.write(content)


def ensure_token(path):
    """The node's API token for GPU reservations (contract gpu-lease/2): created once, readable only by the owner
    of the folder it lives in (the service user, also under sudo); never printed. True when it was created."""
    path = Path(path)
    _reject_symlink(path)
    _reject_symlink(path.parent)
    if path.is_file():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        handle.write(secrets.token_urlsafe(32) + '\n')
    owner = path.parent.stat()
    if hasattr(os, 'chown') and (owner.st_uid, owner.st_gid) != (os.getuid(), os.getgid()):
        os.chown(path, owner.st_uid, owner.st_gid)
    return True


def _resolve_user_and_home(explicit_user, explicit_home):
    if explicit_user:
        user = explicit_user
    else:
        user = _default_service_user()
        if user == 'root' and explicit_home and pwd is not None:
            home_path = Path(explicit_home)
            if home_path.exists() and not home_path.is_symlink():
                st_uid = home_path.stat().st_uid
                if st_uid != 0:
                    try:
                        user = pwd.getpwuid(st_uid).pw_name
                    except KeyError:
                        pass
    home = explicit_home or _default_service_home(user)
    return user, home


def main(argv=None):
    parser = argparse.ArgumentParser(prog='tare-node-install', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--home', default=None, help="the service user's home (auto-detected under sudo)")
    parser.add_argument('--user', default=None, help='the unprivileged service user (auto-detected under sudo)')
    parser.add_argument('--systemd', type=Path, default=Path('/etc/systemd/system'))
    parser.add_argument('--sudoers', type=Path, default=Path('/etc/sudoers.d'))
    parser.add_argument('--no-auto-update', action='store_true',
                        help='do not install or enable the unprivileged tare-node-update.timer')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    user, home = _resolve_user_and_home(args.user, args.home)
    if args.apply and not args.no_auto_update:
        _validate_user(user)
        home = _validate_home(home)
    for unit, name in OVERRIDES.items():
        path = args.systemd/(unit + '.d')/name
        if not path.is_file() or path.is_symlink() or path.parent.is_symlink():
            print(f'{unit}: no regular override at {path}; nothing to change')
            continue
        old = path.read_text(encoding='utf-8')
        new = rewrite(old, home)
        print(f'== {path}\n{new}' if new != old else f'{unit}: already on the installed version')
        if args.apply and new != old:
            _write_no_symlink(path.with_suffix(path.suffix + '.bak'), old)
            _write_no_symlink(path, new)
    if not args.apply:
        if args.no_auto_update:
            print('tare-node-update.timer: skipped (--no-auto-update)')
        else:
            print(f'tare-node-update.timer: would install for {user} (home {home}) with scoped sudoers')
    if args.apply:
        token = Path(home)/'.local/state/tare-qualified-models/api-token'
        print(f'API token for GPU reservations: {token} ' + ('created' if ensure_token(token) else 'kept'))
        bin_dir = Path(home)/'.local/bin'
        _reject_symlink(bin_dir)
        _reject_symlink(bin_dir.parent)
        bin_dir.mkdir(parents=True, exist_ok=True)
        for command in ('gpu', 'gpu-run'):
            link = bin_dir/command
            bak = link.with_suffix('.bak')
            _reject_symlink(bak)
            if link.exists() and not link.is_symlink():
                link.rename(bak)
            elif link.is_symlink():
                link.unlink()
            os.symlink(Path(home)/'.venvs/tare-node/bin'/command, link)
        if not args.no_auto_update:
            install_units(home, user, systemd=args.systemd, sudoers=args.sudoers)
            print(f'Auto-updates enabled for {user} (tare-node-update.timer, unprivileged).')
        else:
            subprocess.run(['systemctl', 'daemon-reload'], check=True)
        subprocess.run(['systemctl', 'restart', 'llm-inference.service'], check=True)
        print('Gateway restarted. Restart ComfyUI (systemctl restart comfyui) once `gpu status` shows no image job.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
