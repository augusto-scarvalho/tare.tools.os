"""Release export must not inherit uncommitted development code."""
import importlib.util
import subprocess
import json
import zipfile
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('native_release',
    Path(__file__).resolve().parents[1]/'scripts/ops/build_native_release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def test_export_uses_exact_git_bytes_and_excludes_dirty_untracked_and_history(tmp_path):
    repo = tmp_path/'repo'; repo.mkdir()
    def git(*args):
        return subprocess.check_output(['git', '-C', str(repo), *args])
    git('init')
    (repo/'pyproject.toml').write_bytes(b'[project]\nname="fixture"\n')
    (repo/'relay/history').mkdir(parents=True)
    (repo/'relay/native.py').write_bytes(b'COMMITTED = True\n')
    (repo/'relay/history/secret.py').write_bytes(b'not package input\n')
    git('add', '.')
    git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@invalid', '-c', 'core.hooksPath=/dev/null',
        'commit', '-m', 'fixture')
    commit = git('rev-parse', 'HEAD').decode().strip()
    (repo/'relay/native.py').write_bytes(b'DIRTY = True\r\n')
    (repo/'relay/untracked.py').write_bytes(b'UNTRACKED = True\n')
    destination = tmp_path/'snapshot'
    manifest = release.snapshot(repo, commit, ['pyproject.toml', 'relay/*.py'], destination)
    assert set(manifest) == {'pyproject.toml', 'relay/native.py'}
    assert (destination/'relay/native.py').read_bytes() == b'COMMITTED = True\n'
    assert (repo/'relay/native.py').read_bytes() == b'DIRTY = True\r\n'
    assert not (destination/'relay/history').exists()


@pytest.mark.parametrize('commit', ['HEAD', 'main', '--help', 'a'*7])
def test_mutable_or_abbreviated_revisions_rejected(tmp_path, commit):
    with pytest.raises(ValueError, match='full_commit'):
        release.snapshot(tmp_path, commit, ['pyproject.toml'], tmp_path/'out')


def test_selection_keeps_flat_packages_flat():
    assert release.selected('src/pkg/nested/code.py', ['src/**'])
    assert not release.selected('src-other/pkg/code.py', ['src/**'])
    assert not release.selected('old/relay/code.py', ['relay/*.py'])
    assert not release.selected('relay/history/code.py', ['relay/*.py'])


def test_wheel_identity_ignores_vendored_distribution_metadata(tmp_path):
    wheel = tmp_path/'fixture.whl'
    with zipfile.ZipFile(wheel, 'w') as archive:
        archive.writestr('fixture-1.0.dist-info/METADATA', 'Name: fixture\nVersion: 1.0\n')
        archive.writestr('fixture/_vendor/helper-2.0.dist-info/METADATA', 'Name: helper\nVersion: 2.0\n')
    assert release.wheel_identity(wheel) == ('fixture', '1.0')


@pytest.mark.parametrize('failure', [None, 'build', 'different-wheel', 'interrupt'])
def test_build_retains_receipts_without_expanded_sources(tmp_path, monkeypatch, failure):
    """A real Git export is temporary even when pip or reproducibility fails."""
    repo = tmp_path/'repo'; repo.mkdir()
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    (repo/'pyproject.toml').write_bytes(b'[project]\nname="fixture"\nversion="1.0"\n')
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Fixture', '-c',
        'user.email=fixture@invalid', '-c', 'core.hooksPath=', 'commit', '-qm', 'fixture'], check=True)
    commit = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD']).decode().strip()
    lock = dict(schema='tare.tools/native-release-inputs/1', python='.'.join(map(str, release.sys.version_info[:2])),
        build_tools={}, source_date_epoch=0, sources=[dict(id='fixture', commit=commit,
        include=['pyproject.toml'], package='fixture', version='1.0')])
    output = tmp_path/'release'; builds = []
    original_run = release.subprocess.run
    def pip(argv, **kwargs):
        if argv[:4] != [release.sys.executable, '-m', 'pip', 'wheel']:
            return original_run(argv, **kwargs)
        source = Path(argv[-1]); builds.append(source)
        assert (source/'pyproject.toml').read_bytes() == (repo/'pyproject.toml').read_bytes()
        kwargs['stdout'].write(b'fixture build log\n')
        if failure == 'build':
            raise subprocess.CalledProcessError(1, argv)
        if failure == 'interrupt':
            raise KeyboardInterrupt()
        dest = Path(argv[argv.index('--wheel-dir')+1])
        with zipfile.ZipFile(dest/'fixture-1.0-py3-none-any.whl', 'w') as wheel:
            wheel.writestr(zipfile.ZipInfo('fixture-1.0.dist-info/METADATA'), 'Name: fixture\nVersion: 1.0\n')
            wheel.writestr(zipfile.ZipInfo('fixture.py'),
                'CHANGED' if failure == 'different-wheel' and len(builds) == 2 else 'EXACT')
    monkeypatch.setattr(release.subprocess, 'run', pip)
    if failure:
        with pytest.raises((subprocess.CalledProcessError, ValueError, KeyboardInterrupt)):
            release.build(lock, {'fixture':repo}, output)
    else:
        manifest = release.build(lock, {'fixture':repo}, output)
        assert len(builds) == 2
        assert manifest['packages'][0]['build_sha256'] == [manifest['packages'][0]['sha256']]*2
        assert not list((output/'wheels-2').glob('*.whl'))
    assert all(not path.exists() for path in builds)
    assert not list(output.glob('build-*'))
    assert (output/'fixture-1.log').read_bytes() == b'fixture build log\n'
    receipt = json.loads((output/'manifest.json').read_text())
    assert receipt['status'] == ('INTERRUPTED' if failure == 'interrupt' else 'FAILED' if failure else 'REPRODUCIBLE_WHEELS')
    assert json.loads((output/'inputs.json').read_text())['sources'][0]['commit'] == commit


def test_actual_offline_wheels_stay_reproducible_after_temporary_source_cleanup(tmp_path):
    pytest.importorskip('setuptools')
    pytest.importorskip('wheel')
    repo = tmp_path/'real-package'; repo.mkdir()
    (repo/'pyproject.toml').write_bytes(b'[build-system]\nrequires=["setuptools","wheel"]\n'
        b'build-backend="setuptools.build_meta"\n[project]\nname="tare-storage-fixture"\nversion="1.0"\n'
        b'[tool.setuptools]\npy-modules=["fixture"]\n')
    (repo/'fixture.py').write_bytes(b'EXACT = True\n')
    subprocess.run(['git','init','-q',str(repo)],check=True)
    subprocess.run(['git','-C',str(repo),'add','.'],check=True)
    subprocess.run(['git','-C',str(repo),'-c','user.name=Fixture','-c','user.email=fixture@invalid',
        '-c','core.hooksPath=','commit','-qm','fixture'],check=True)
    commit = subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD']).decode().strip()
    lock = dict(schema='tare.tools/native-release-inputs/1',python='.'.join(map(str,release.sys.version_info[:2])),
        build_tools={},source_date_epoch=0,sources=[dict(id='fixture',commit=commit,include=['pyproject.toml','fixture.py'],
        package='tare-storage-fixture',version='1.0')])
    output = tmp_path/'release'
    try:
        receipt = release.build(lock,{'fixture':repo},output)
    except subprocess.CalledProcessError:
        pytest.fail('\n'.join(p.read_text(errors='replace') for p in output.glob('*.log')))
    package = receipt['packages'][0]
    wheel = output/'wheels-1'/package['wheel']
    assert package['build_sha256'] == [release.sha(wheel.read_bytes())]*2
    with zipfile.ZipFile(wheel) as archive:
        assert archive.read('fixture.py') == b'EXACT = True\n'
    assert not list(output.glob('build-*')) and not list((output/'wheels-2').glob('*.whl'))
