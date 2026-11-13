"""Prove the Windows sandbox that checks run in, case by case, on this machine.

CI cannot: its Windows image has no process security environment. `tare ci pr` runs this file on the machine that
opens a pull request whose commits touch the sandbox files (relay/ci_watch.py SANDBOX_PATHS) and refuses to open it
when a case fails. By hand, from the repository root, with an interpreter that has the Kernel's dependencies
(FlatBuffers; the installed product's has them):

    python scripts/ops/sandbox_probe.py

It runs the code of the checkout it lives in. It builds throwaway projects in a temporary folder it owns, runs their
checks the way tare runs the checks of a project without command protection (command_protection.conversation_verifier
with work_units.run_checks), answers no to every question (nothing runs outside the sandbox) and removes everything it
created. One line per case, then a JSON line; exit 0 when every case that ran passed, 1 when one failed, 3 when this
host has no sandbox. Standard library only; no network, no installs, no model.
"""
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
# A fake value in the project's .env and in a file outside it: a receipt that holds it means the sandbox let it be read.
SECRET = 'probe-fake-value-' + os.urandom(6).hex()


def write(root, files):
    for name, text in files.items():
        path = root/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')


def remove(folder):
    """Remove `folder`, its links first: the link itself, never its target."""
    for path, directories, _ in os.walk(folder):
        for name in list(directories):
            link = os.path.join(path, name)
            if os.path.islink(link) or getattr(os.lstat(link), 'st_reparse_tag', 0):
                (os.rmdir if os.name == 'nt' else os.unlink)(link)
                directories.remove(name)

    def unlock(function, path, _):
        os.chmod(path, stat.S_IWRITE)
        function(path)
    shutil.rmtree(folder, onerror=unlock)


def projects(storage, outside):
    """{root: [(case, argv, expected state, expects the question)]}; argv[0] names a tool that may be missing."""
    flat, pnpm = storage/'wt'/'0000f1a7', storage/'wt'/'00009b9e'  # where tare puts a candidate (staged effects)
    write(outside, {'private.txt': SECRET + '\n'})
    test = "const test = require('node:test');\nconst assert = require('node:assert');\n"
    write(flat, {
        'test_ok.py': 'import unittest\n\n\nclass Ok(unittest.TestCase):\n    def test_adds(self):\n'
                      '        self.assertEqual(1 + 1, 2)\n',
        'test_fail.py': 'import unittest\n\n\nclass Fail(unittest.TestCase):\n    def test_regression(self):\n'
                        '        self.assertEqual(1 + 1, 3)\n',
        '.env': f'KEY={SECRET}\n',
        'package.json': json.dumps({'name': 'flat', 'version': '1.0.0', 'private': True,
                                    'scripts': {'test': 'node --test test/ok.test.js test/esm.test.mjs'}}),
        'lib.js': "module.exports = { name: 'lib' };\n",
        'main.js': "const lib = require('./lib');\nconst dep = require('dep');\nconsole.log('cjs ok', lib.name, dep.name);\n",
        'main.mjs': "import lib from './lib.js';\nimport dep from 'dep';\nimport { esm } from 'dep-esm';\n"
                    "console.log('esm ok', lib.name, dep.name, esm);\n",
        'node_modules/dep/package.json': json.dumps({'name': 'dep', 'version': '1.0.0', 'main': 'index.js'}),
        'node_modules/dep/index.js': "module.exports = { name: 'dep' };\n",
        'node_modules/dep-esm/package.json': json.dumps({'name': 'dep-esm', 'version': '1.0.0', 'type': 'module',
                                                         'exports': './index.js'}),
        'node_modules/dep-esm/index.js': "export const esm = 'dep-esm';\n",
        'test/ok.test.js': test + "const dep = require('dep');\n"
                           "test('cjs', () => { assert.strictEqual(require('../lib').name + dep.name, 'libdep'); });\n",
        'test/esm.test.mjs': "import test from 'node:test';\nimport assert from 'node:assert';\n"
                             "import { esm } from 'dep-esm';\nimport lib from '../lib.js';\n"
                             "test('esm', () => { assert.strictEqual(esm + lib.name, 'dep-esmlib'); });\n",
        'fail/fail.test.mjs': "import test from 'node:test';\nimport assert from 'node:assert';\n"
                              "test('regression', () => { assert.strictEqual(1 + 1, 3); });\n",
        'Cargo.toml': '[package]\nname = "probe"\nversion = "0.1.0"\nedition = "2021"\n\n[dependencies]\n',
        'src/lib.rs': 'pub fn two() -> i32 { 2 }\n\n#[test]\nfn adds() {\n    assert_eq!(two() + 1, 3);\n}\n',
    })
    # pnpm's layout: node_modules/pkg is a link into node_modules/.pnpm (a junction on Windows, as pnpm makes there),
    # where pkg finds its peer through another.
    store = pnpm/'node_modules'/'.pnpm'
    write(pnpm, {
        'package.json': json.dumps({'name': 'pnpm-like', 'version': '1.0.0', 'private': True}),
        'main.js': "const pkg = require('pkg');\nconsole.log('pnpm ok', pkg.name, pkg.peer);\n",
        'node_modules/.pnpm/pkg@1.0.0/node_modules/pkg/package.json': json.dumps({'name': 'pkg', 'version': '1.0.0'}),
        'node_modules/.pnpm/pkg@1.0.0/node_modules/pkg/index.js':
            "module.exports = { name: 'pkg', peer: require('peer').name };\n",
        'node_modules/.pnpm/peer@1.0.0/node_modules/peer/package.json': json.dumps({'name': 'peer', 'version': '1.0.0'}),
        'node_modules/.pnpm/peer@1.0.0/node_modules/peer/index.js': "module.exports = { name: 'peer' };\n",
    })
    if os.name == 'nt':
        from _winapi import CreateJunction as link
    else:
        link = os.symlink
    link(str(store/'pkg@1.0.0'/'node_modules'/'pkg'), str(pnpm/'node_modules'/'pkg'))
    link(str(store/'peer@1.0.0'/'node_modules'/'peer'), str(store/'pkg@1.0.0'/'node_modules'/'peer'))
    drive = os.environ.get('SystemDrive', 'C:') + '\\'
    return {flat: [
        ('python-test-passes', ['python', '-m', 'unittest', 'test_ok'], 'green', False),
        ('python-assertion-fails', ['python', '-m', 'unittest', 'test_fail'], 'red', False),
        ('python-reads-outside', ['python', '-c', 'import sys; print(open(sys.argv[1]).read())',
                                  str(outside/'private.txt')], 'red', True),
        ('python-reads-dotenv', ['python', '-c', "print(open('.env').read())"], 'red', True),
        ('python-connects-loopback', ['python', '-c', 'import socket, sys; socket.create_connection(("127.0.0.1", '
                                      'int(sys.argv[1])), timeout=5).close(); print("connected")', '{port}'], 'red', True),
        ('python-lists-drive', ['python', '-c', 'import os, sys; print(os.listdir(sys.argv[1]))', drive], 'red', True),
        ('python-lists-users', ['python', '-c', 'import os, sys; print(os.listdir(sys.argv[1]))',
                                os.path.join(drive, 'Users')], 'red', True),
        # The socketpair shim (relay/sandbox-python); without it asyncio waits forever, so the timer ends it.
        ('python-asyncio-runs', ['python', '-c', 'import asyncio, os, threading; t = threading.Timer(30, os._exit, (9,)); '
                                 't.daemon = True; t.start(); asyncio.run(asyncio.sleep(0.01)); print("asyncio ok")'],
         'green', False),
        ('node-commonjs', ['node', 'main.js'], 'green', False),
        ('node-es-module', ['node', 'main.mjs'], 'green', False),
        ('node-test', ['node', '--test', 'test/ok.test.js', 'test/esm.test.mjs'], 'green', False),
        ('npm-test', ['npm', 'test'], 'green', False),
        ('node-assertion-fails', ['node', '--test', 'fail/fail.test.mjs'], 'red', False),
        ('cargo-test', ['cargo', 'test', '--offline'], 'green', False),
    ], pnpm: [
        ('node-pnpm-junction', ['node', 'main.js'], 'red', True),
    ]}


def run_cases(base, storage):
    from tare_tools_agent_runtime import BindingRef, CancellationToken, ExecutionControl
    from relay import command_protection, work_units
    tempfile.tempdir = str(base/'tmp')  # the binding's own temporary folders go there too
    os.mkdir(tempfile.tempdir)
    tools = {'python': sys.executable, 'node': shutil.which('node'), 'cargo': shutil.which('cargo'),
             'npm': shutil.which('npm.cmd') or shutil.which('npm')}
    listener = socket.create_server(('127.0.0.1', 0))
    listener.settimeout(0.2)
    reached, stop = [], threading.Event()

    def accept():
        while not stop.is_set():
            try:
                reached.append(listener.accept()[0])
            except OSError:  # a timeout, or closed
                continue
    threading.Thread(target=accept, daemon=True).start()
    control = ExecutionControl(BindingRef('work', 'binding', 'operator', 1, 'opaque'), CancellationToken(),
                               time.monotonic() + 3600)
    rows = []
    try:
        for root, cases in projects(storage, base/'outside').items():
            commands = {name: [tools[argv[0]], *(a.replace('{port}', str(listener.getsockname()[1])) for a in argv[1:])]
                        for name, argv, _, _ in cases if tools[argv[0]]}
            asked = set()

            def outside(name, argv, why):
                asked.add(name)
                return False, 'the sandbox probe runs nothing outside the sandbox'
            run = command_protection.conversation_verifier(control, base/'custody'/root.name,
                {'repositories': {'project': str(root)}, 'writable': {'project': ['.']}}, {}, root,
                {'repository': 'project', 'title': 'Sandbox probe', 'criteria': [], 'target_paths': ['.']}, commands,
                outside=outside)
            token = work_units.CHECK_PORT.set(run)
            try:
                for name, argv, state, question in cases:
                    expected = f'{state}, ' + ('asks' if question else 'no question')
                    if name not in commands:
                        rows.append((name, expected, f'not run: {argv[0]} not found', 0.0))
                        continue
                    began, connections = time.monotonic(), len(reached)
                    try:
                        got, receipt = work_units.run_checks(root, {name: commands[name]})
                    except ValueError as exc:  # refused before it ran
                        got, receipt = 'refused', str(exc)
                    observed = f'{got}, ' + ('asks' if name in asked else 'no question')
                    if SECRET in receipt:
                        observed += ', printed the secret'
                    if len(reached) > connections:
                        observed += ', reached the probe\'s listener'
                    if name == 'cargo-test' and got == 'red' and '(os error 4551)' in receipt:
                        observed = 'not run: blocked by this host\'s Application Control policy'
                    rows.append((name, expected, observed, round(time.monotonic() - began, 1)))
                    if observed != expected and not observed.startswith('not run'):
                        print(f'--- {name}:\n{receipt[-1500:]}', file=sys.stderr)
            finally:
                work_units.CHECK_PORT.reset(token)
                if callable(getattr(run, 'close', None)):
                    run.close()
    finally:
        stop.set()
        listener.close()
    return rows


def main():
    began = time.monotonic()
    sys.path[:0] = [str(ROOT), str(ROOT/'satellites'/'tare.tools.kernel')]  # the code of this checkout
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(errors='replace')
    packages = Path(os.environ.get('LOCALAPPDATA') or tempfile.gettempdir(), 'Packages')
    containers = set(packages.glob('sandbox.*'))
    base = Path(tempfile.mkdtemp(prefix='tare-sandbox-probe-')).resolve()
    os.environ['TARE_STORAGE_ROOT'] = str(base/'storage')  # before tare is imported: never the operator's storage
    try:
        from relay import command_protection
        if not command_protection.sandbox_available():
            print(json.dumps({'ok': None, 'reason': 'no sandbox on this host'}))
            return 3
        rows = run_cases(base, base/'storage')
    finally:
        remove(base)
    left = sorted(p.name for p in set(packages.glob('sandbox.*')) - containers) + [str(base)] * base.exists()
    rows.append(('cleanup', 'nothing left', 'left: ' + ', '.join(left) if left else 'nothing left', 0.0))
    failed = [name for name, expected, observed, _ in rows if observed != expected and not observed.startswith('not run')]
    for name, expected, observed, seconds in rows:
        mark = 'skip' if observed.startswith('not run') else 'FAIL' if name in failed else 'ok'
        print(f'{mark:4}  {name:26} expected: {expected:18} observed: {observed:18} {seconds:5.1f} s')
    print(json.dumps({'ok': not failed, 'failed': failed,
                      'not_run': [name for name, _, observed, _ in rows if observed.startswith('not run')],
                      'seconds': round(time.monotonic() - began, 1)}))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
