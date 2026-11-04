"""Resolve once and preserve all wheel bytes for offline product installs."""
import argparse
from email.parser import BytesParser
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile


def pack(release, output, installer, tools=()):
    manifest = json.loads((release/'manifest.json').read_text(encoding='utf-8'))
    if manifest['status'] not in {'REPRODUCIBLE_WHEELS', 'SINGLE_BUILD_WHEELS'}:
        raise ValueError('Build reproducible project wheels first')
    output.mkdir(parents=True, exist_ok=False)
    wheels = output/'wheels'; wheels.mkdir()
    projects = []
    for package in manifest['packages']:
        wheel = release/'wheels-1'/package['wheel']
        if hashlib.sha256(wheel.read_bytes()).hexdigest() != package['sha256']:
            raise ValueError('Project wheel changed: '+wheel.name)
        projects.append(str(wheel))
    subprocess.run([sys.executable, '-m', 'pip', 'download', '--only-binary=:all:',
                    '--dest', str(wheels), *projects], check=True)
    lines = []
    for wheel in sorted(wheels.glob('*.whl')):
        with zipfile.ZipFile(wheel) as archive:
            metadata = BytesParser().parsebytes(archive.read(next(n for n in archive.namelist()
                                                            if n.count('/') == 1 and n.endswith('.dist-info/METADATA'))))
        lines.append(metadata['Name']+'=='+metadata['Version']+' --hash=sha256:'+hashlib.sha256(wheel.read_bytes()).hexdigest())
    (output/'requirements.lock').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    shutil.copyfile(release/'manifest.json', output/'release.json')
    shutil.copyfile(installer, output/'install.py')
    for tool in tools:  # release-owned executable or runtime tree; every byte enters bundle.json
        target = output/'tools'/Path(tool).name
        target.parent.mkdir(exist_ok=True)
        if Path(tool).is_dir():
            shutil.copytree(tool, target)
        else:
            shutil.copyfile(tool, target)
            target.chmod(0o755)
    files = {p.relative_to(output).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(output.rglob('*')) if p.is_file()}
    (output/'bundle.json').write_text(json.dumps({'schema': 'tare.tools/product-bundle/1',
        'python': '.'.join(map(str, sys.version_info[:2])), 'platform': sys.platform, 'files': files},
        indent=2, sort_keys=True)+'\n', encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--installer', type=Path, default=Path(__file__).resolve().parents[2]/'relay/native_product.py')
    parser.add_argument('--tool', type=Path, action='append', default=[],
                        help='executable placed in bundle/tools, which the launcher puts on PATH; repeat')
    args = parser.parse_args()
    pack(args.release.resolve(), args.output.resolve(), args.installer.resolve(), [t.resolve() for t in args.tool])
