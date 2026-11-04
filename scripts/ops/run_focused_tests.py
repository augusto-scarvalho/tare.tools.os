"""Run explicit test files; retain logs/JUnit, remove generated pytest workspaces."""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time


@contextmanager
def workspace():
    """Remove only our owned workspace, including nested Windows long paths."""
    owner = tempfile.TemporaryDirectory(prefix='tare-pytest-')
    try:
        yield owner.name
    finally:
        for attempt in range(5):
            try:
                if os.name == 'nt' and Path(owner.name).exists():
                    target = Path(owner.name).resolve()
                    if target.parent != Path(tempfile.gettempdir()).resolve() or not target.name.startswith('tare-pytest-'):
                        raise ValueError('Unexpected focused-test workspace; cleanup refused')
                    path = str(target)
                    extended = '\\\\?\\UNC\\'+path[2:] if path.startswith('\\\\') else '\\\\?\\'+path
                    def writable(function, name, error):
                        if not isinstance(error, PermissionError) or Path(name).is_symlink():
                            raise error
                        os.chmod(name, stat.S_IWRITE)
                        function(name)
                    shutil.rmtree(extended, onexc=writable)
                owner.cleanup()
                break
            except OSError as error:
                if getattr(error, 'winerror', None) not in (32, 145) or attempt == 4:
                    raise
                time.sleep(.2*(attempt+1))


def run(tests, output):
    root = Path.cwd().resolve()
    for selector in tests:
        path = Path(selector.split('::', 1)[0]).resolve()
        if selector.startswith('-') or not path.is_relative_to(root) or not path.is_file() or path.suffix != '.py':
            raise ValueError(f'Invalid test selector {selector!r}. Choose a Python test file or node ID inside this repository.')
    if not tests:
        raise ValueError('Select at least one test file; this runner never defaults to the whole suite.')
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    result = dict(tests=tests, exit_code=None, scratch_removed=False)
    started = time.monotonic()
    scratch = None
    try:
        with workspace() as folder:
            scratch = Path(folder)
            with (output/'pytest.log').open('wb') as log:
                command = [sys.executable, '-B', '-m', 'pytest', '-q', '-p', 'no:cacheprovider',
                    '--basetemp', str(scratch/'pytest'), '--junitxml='+str(output/'junit.xml'), *tests]
                result['exit_code'] = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT).returncode
    except BaseException as exc:
        result['error'] = type(exc).__name__
        raise
    finally:
        result.update(seconds=round(time.monotonic()-started, 3),
                      scratch_removed=scratch is not None and not scratch.exists())
        (output/'result.json').write_text(json.dumps(result, indent=2)+'\n', encoding='utf8')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='New folder for logs, JUnit and the exit receipt')
    parser.add_argument('tests', nargs='+', help='Explicit test files or pytest node IDs')
    args = parser.parse_args(argv)
    result = run(args.tests, args.output)
    print(json.dumps(result))
    return result['exit_code']


if __name__ == '__main__':
    raise SystemExit(main())
