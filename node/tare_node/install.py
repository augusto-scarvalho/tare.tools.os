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


def ensure_token(path):
    """The node's API token for GPU reservations (contract gpu-lease/2): created once, readable only by the owner
    of the folder it lives in (the service user, also under sudo); never printed. True when it was created."""
    path = Path(path)
    if path.is_file():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        handle.write(secrets.token_urlsafe(32) + '\n')
    owner = path.parent.stat()
    if hasattr(os, 'chown') and (owner.st_uid, owner.st_gid) != (os.getuid(), os.getgid()):
        os.chown(path, owner.st_uid, owner.st_gid)
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(prog='tare-node-install', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--home', default=str(Path.home()), help="the service user's home (with sudo: /home/<user>)")
    parser.add_argument('--systemd', type=Path, default=Path('/etc/systemd/system'))
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    for unit, name in OVERRIDES.items():
        path = args.systemd/(unit + '.d')/name
        if not path.is_file() or path.is_symlink():
            print(f'{unit}: no regular override at {path}; nothing to change')
            continue
        old = path.read_text(encoding='utf-8')
        new = rewrite(old, args.home)
        print(f'== {path}\n{new}' if new != old else f'{unit}: already on the installed version')
        if args.apply and new != old:
            path.with_suffix(path.suffix + '.bak').write_text(old, encoding='utf-8')
            path.write_text(new, encoding='utf-8')
    if args.apply:
        token = Path(args.home)/'.local/state/tare-qualified-models/api-token'
        print(f'API token for GPU reservations: {token} ' + ('created' if ensure_token(token) else 'kept'))
        bin_dir = Path(args.home)/'.local/bin'
        for command in ('gpu', 'gpu-run'):
            link = bin_dir/command
            if link.exists() and not link.is_symlink():
                link.rename(link.with_suffix('.bak'))
            elif link.is_symlink():
                link.unlink()
            os.symlink(Path(args.home)/'.venvs/tare-node/bin'/command, link)
        subprocess.run(['systemctl', 'daemon-reload'], check=True)
        subprocess.run(['systemctl', 'restart', 'llm-inference.service'], check=True)
        print('Gateway restarted. Restart ComfyUI (systemctl restart comfyui) once `gpu status` shows no image job.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
