"""Expand existing local vendor connections from explicit offline catalogues.

No login, inference, credential discovery or implicit model fallback. Existing
profiles are preserved; only capability metadata is added to base connections.
"""
import argparse
import json
from pathlib import Path

from relay.native_generation import load_deployment
from relay.vendor_models import CODEX_NATIVE_BATCH_QUALIFIED, expand  # noqa: F401  (re-exported)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profiles', type=Path, required=True)
    parser.add_argument('--codex-catalog', type=Path, required=True)
    parser.add_argument('--agy-models', type=Path, help='JSON array of exact model IDs from agy models')
    parser.add_argument('--backup', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    bases = {name: load_deployment(args.profiles/(name+'.json'))
             for name in ('claude-cli', 'codex-cli', 'codex-native', 'agy-cli')
             if (args.profiles/(name+'.json')).is_file()}
    codex = json.loads(args.codex_catalog.read_text(encoding='utf-8'))['models']
    agy = json.loads(args.agy_models.read_text(encoding='utf-8')) if args.agy_models else []
    desired = expand(bases, codex, agy)
    pending = []
    for name, value in desired.items():
        target = args.profiles/(name+'.json')
        if target.exists():
            old = load_deployment(target)
            if old == value:
                continue
            if name not in bases:
                raise ValueError('Existing custom profile differs; preserved: '+name)
        pending.append((name, target, value))
    if args.apply:
        from compute_plane.native_journal import save_json
        args.backup.mkdir(parents=True, exist_ok=True)
        for name, target, value in pending:
            if target.exists():
                with (args.backup/(name+'.json')).open('xb') as stream:
                    stream.write(target.read_bytes())
            save_json(target, value)
    print(json.dumps({'applied': args.apply, 'profiles': [name for name, _, _ in pending]}))


if __name__ == '__main__':
    main()
