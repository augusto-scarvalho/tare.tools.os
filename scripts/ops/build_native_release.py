"""Build native wheels twice from exact Git objects, without checkout overlays.

Run in a reserved experiment lot, using a venv with the lock's build tools.
This prepares artifacts; it never installs into the operator's environment.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import importlib.metadata
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
import zipfile


def sha(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n', encoding='utf-8')


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args])


def selected(path, patterns):
    parts = PurePosixPath(path).parts
    for pattern in patterns:
        if pattern.endswith('/**'):
            if path.startswith(pattern[:-2]):
                return True
        else:
            wanted = PurePosixPath(pattern).parts
            if len(parts) == len(wanted) and all(fnmatch.fnmatchcase(a, b) for a, b in zip(parts, wanted)):
                return True
    return False


def snapshot(root, commit, patterns, destination):
    """Export only regular committed files. Dirty/untracked files cannot enter."""
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ValueError('source_requires_full_commit_id')
    resolved = git(root, 'rev-parse', '--verify', commit + '^{commit}').decode().strip()
    if resolved != commit:
        raise ValueError('source_is_not_a_commit')
    entries = []
    for row in git(root, 'ls-tree', '-rz', commit).split(b'\0'):
        if not row:
            continue
        info, raw_path = row.split(b'\t', 1)
        mode, kind, oid = info.decode().split()
        path = raw_path.decode('utf-8')
        if selected(path, patterns):
            if kind != 'blob' or mode not in {'100644', '100755'}:
                raise ValueError('source_requires_regular_file: ' + path)
            if any(x in {'..', ''} for x in path.split('/')) or '\\' in path or ':' in path:
                raise ValueError('unsafe_source_path')
            entries.append((path, oid))
    if not any(path == 'pyproject.toml' for path, _ in entries):
        raise ValueError('source_missing_pyproject')
    destination.mkdir(parents=True, exist_ok=False)
    # Batch object reads avoid one process per source file, especially on Windows.
    request = ''.join(oid + '\n' for _, oid in entries).encode()
    data = io.BytesIO(subprocess.check_output(
        ['git', '-C', str(root), 'cat-file', '--batch'], input=request))
    manifest = {}
    for path, oid in entries:
        header = data.readline().decode().split()
        if len(header) != 3 or header[:2] != [oid, 'blob']:
            raise ValueError('source_object_mismatch')
        body = data.read(int(header[2]))
        if data.read(1) != b'\n':
            raise ValueError('source_object_truncated')
        target = destination / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        manifest[path] = sha(body)
    return manifest


def wheel_identity(path):
    from email.parser import BytesParser
    with zipfile.ZipFile(path) as wheel:
        names = [n for n in wheel.namelist() if n.count('/') == 1 and n.endswith('.dist-info/METADATA')]
        if len(names) != 1:
            raise ValueError('wheel_metadata_ambiguous')
        metadata = BytesParser().parsebytes(wheel.read(names[0]))
        return metadata['Name'], metadata['Version']


def build(lock, roots, output, repetitions=2):
    if lock.get('schema') != 'tare.tools/native-release-inputs/1':
        raise ValueError('unsupported_release_lock')
    if lock['python'] != '.'.join(map(str, sys.version_info[:2])):
        raise ValueError('build_python_mismatch')
    for name, version in lock['build_tools'].items():
        if importlib.metadata.version(name) != version:
            raise ValueError('build_tool_mismatch: ' + name)
    if set(roots) != {s['id'] for s in lock['sources']}:
        raise ValueError('explicit_source_roots_required')
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / 'inputs.json', lock)
    env = dict(os.environ)
    env.pop('PYTHONPATH', None)
    env.update(SOURCE_DATE_EPOCH=str(lock['source_date_epoch']), PYTHONHASHSEED='0',
               PYTHONDONTWRITEBYTECODE='1', PIP_CONFIG_FILE=os.devnull,
               PIP_DISABLE_PIP_VERSION_CHECK='1', PIP_NO_CACHE_DIR='1')
    manifest = {'schema': 'tare.tools/native-release/1', 'status': 'BUILDING',
                'inputs_sha256': sha((output / 'inputs.json').read_bytes()),
                'python': sys.version, 'platform': sys.platform, 'packages': []}
    write_json(output / 'manifest.json', manifest)
    try:
        for source in lock['sources']:
            wheels = []
            source_manifest = None
            for repetition in range(1, repetitions + 1):
                # Exact Git objects are the source custody. Expanded copies are scratch,
                # including setuptools' build/ and egg-info, even when the build fails.
                with tempfile.TemporaryDirectory(prefix=f'build-{repetition}-', dir=output) as scratch:
                    target = Path(scratch)/'source'
                    observed = snapshot(roots[source['id']], source['commit'], source['include'], target)
                    if source_manifest is not None and source_manifest != observed:
                        raise ValueError('source_changed_between_builds')
                    source_manifest = observed
                    dest = output / f'wheels-{repetition}'
                    dest.mkdir(exist_ok=True)
                    with (output / f"{source['id']}-{repetition}.log").open('wb') as log:
                        subprocess.run([sys.executable, '-m', 'pip', 'wheel', '--no-index', '--no-deps',
                            '--no-build-isolation', '--wheel-dir', str(dest), str(target)],
                            env=env, cwd=output, stdout=log, stderr=subprocess.STDOUT, check=True)
                found = [p for p in dest.glob('*.whl') if wheel_identity(p) == (source['package'], source['version'])]
                if len(found) != 1:
                    raise ValueError('expected_wheel_missing_or_ambiguous: ' + source['id'])
                wheels.append(found[0])
            hashes = [sha(p.read_bytes()) for p in wheels]
            if len(set(hashes)) > 1 or len({w.name for w in wheels}) > 1:
                raise ValueError('wheel_not_reproducible: ' + source['id'])
            manifest['packages'].append({'id': source['id'], 'name': source['package'],
                'version': source['version'], 'commit': source['commit'], 'sources': source_manifest,
                'wheel': wheels[0].name, 'sha256': hashes[0], 'build_sha256': hashes,
                'bytes': wheels[0].stat().st_size})
            write_json(output / 'manifest.json', manifest)
            # Keep the installable first wheel and both build digests, not a duplicate binary.
            for duplicate in wheels[1:]:
                duplicate.unlink()
            print(source['id'] + (': identical wheels in two clean builds' if repetitions > 1 else ': built once'), flush=True)
    except BaseException as exc:
        manifest.update(status='INTERRUPTED' if isinstance(exc, KeyboardInterrupt) else 'FAILED',
                        failure={'type': type(exc).__name__, 'reason': str(exc)[:500]})
        write_json(output / 'manifest.json', manifest)
        raise
    # One build proves nothing about reproducibility; internal releases say so instead of claiming it.
    manifest['status'] = 'REPRODUCIBLE_WHEELS' if repetitions > 1 else 'SINGLE_BUILD_WHEELS'
    write_json(output / 'manifest.json', manifest)
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lock', required=True, type=Path)
    parser.add_argument('--roots', required=True, type=Path, help='JSON map of source ID to explicit checkout')
    parser.add_argument('--output', required=True, type=Path, help='New directory inside the reserved experiment lot')
    parser.add_argument('--repetitions', type=int, choices=(1, 2), default=2,
                        help='2 proves the wheels reproducible; 1 is for fast internal releases')
    args = parser.parse_args(argv)
    build(json.loads(args.lock.read_text(encoding='utf-8')), json.loads(args.roots.read_text(encoding='utf-8')),
          args.output.resolve(), args.repetitions)


if __name__ == '__main__':
    main()
