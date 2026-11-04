"""Native operator entry. Historical Aider sessions require their archived host."""
import argparse
from pathlib import Path
import sys


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    selector = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    selector.add_argument('--native', action='store_true')
    selector.add_argument('--native-deployment')
    selector.add_argument('--session', type=Path)
    selector.add_argument('--resume', action='store_true')
    boundary = argv.index('--verify-command') if '--verify-command' in argv else len(argv)
    selected, _ = selector.parse_known_args(argv[:boundary])
    if (selected.resume and selected.session is not None
            and not (selected.session / 'native-agent.json').exists()
            and (selected.session / 'session.json').exists()):
        print('LEGACY_SESSION_REQUIRES_ARCHIVED_HOST: preserve custody; start a new native session.', file=sys.stderr)
        return 2
    from .native_agent import main as run
    return run(argv)


if __name__ == '__main__':
    raise SystemExit(main())
