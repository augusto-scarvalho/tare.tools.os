"""Declared package data must be inside the Git export the release builds from."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tomllib

import pytest

spec = importlib.util.spec_from_file_location('native_release_package_data',
    Path(__file__).resolve().parents[1]/'scripts/ops/build_native_release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def declared_package_data(setuptools, tracked):
    """Tracked files the setuptools package-data declarations select."""
    directories = setuptools.get('package-dir', {})
    declared = set()
    for package, patterns in setuptools['package-data'].items():
        directory = directories.get(package, package.replace('.', '/'))
        declared.update(path for path in tracked
                        if release.selected(path, [directory+'/'+pattern for pattern in patterns]))
    return declared


def omitted_from_export(declared, include):
    """Declared resources the release include does not export, by path."""
    return sorted(path for path in declared if not release.selected(path, include))


def assert_exported(declared, include):
    missing = omitted_from_export(declared, include)
    assert not missing, 'Release export omits declared package data: '+', '.join(missing)


def test_product_export_includes_all_declared_committed_package_data(tmp_path):
    """Setuptools cannot package a declared resource absent from the Git export."""
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root/'pyproject.toml').read_text(encoding='utf-8'))
    template = max((root/'scripts/ops/release_inputs').glob('PRODUCT_RELEASE_INPUTS_WIN312_*.json'))
    lock = json.loads(template.read_text(encoding='utf-8'))
    include = next(source['include'] for source in lock['sources'] if source['id'] == 'os')
    tracked = subprocess.check_output(['git', '-C', str(root), 'ls-files', '-z']).decode().split('\0')
    declared = declared_package_data(project['tool']['setuptools'], tracked)
    assert_exported(declared, include)
    commit = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD']).decode().strip()
    exported = release.snapshot(root, commit, include, tmp_path/'product-source')
    assert declared <= exported.keys()
    assert (tmp_path/'product-source/relay/behavioral_evidence.json').is_file()


def test_a_declared_tracked_resource_missing_from_the_include_fails_with_its_path():
    """The negative case: a declared, tracked resource the include omits is named, not silently accepted."""
    setuptools = {'package-data': {'pkg': ['*.json'], 'schemas': ['*.json']},
                  'package-dir': {'schemas': 'schemas'}}
    tracked = ['pkg/code.py', 'pkg/data.json', 'pkg/notes.txt', 'schemas/contract.json']
    declared = declared_package_data(setuptools, tracked)
    assert declared == {'pkg/data.json', 'schemas/contract.json'}
    partial = ['pyproject.toml', 'pkg/*.py', 'schemas/*.json']
    assert omitted_from_export(declared, partial) == ['pkg/data.json']
    with pytest.raises(AssertionError, match='pkg/data.json'):
        assert_exported(declared, partial)
    assert_exported(declared, ['pyproject.toml', 'pkg/*.py', 'pkg/*.json', 'schemas/*.json'])
