"""The local test runner must retain evidence, not expanded test workspaces."""
import importlib.util
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location('focused_tests',
    Path(__file__).resolve().parents[1]/'scripts/ops/run_focused_tests.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


@pytest.mark.parametrize('outcome', [0, 1, 'interrupt'])
def test_cleanup_on_pass_failure_and_interrupt_preserves_logs_and_receipt(tmp_path, monkeypatch, outcome):
    monkeypatch.chdir(tmp_path)
    (tmp_path/'test_fixture.py').write_text('# selected test\n')
    output = tmp_path/'evidence'; seen = []
    def execute(command, **kwargs):
        scratch = Path(command[command.index('--basetemp')+1])
        scratch.mkdir(); (scratch/'generated-repo').mkdir(); seen.append(scratch.parent)
        assert not scratch.is_relative_to(output)
        kwargs['stdout'].write(b'test diagnostics\n')
        (output/'junit.xml').write_text('<testsuite/>')
        if outcome == 'interrupt':
            raise KeyboardInterrupt()
        return SimpleNamespace(returncode=outcome)
    monkeypatch.setattr(runner.subprocess, 'run', execute)
    if outcome == 'interrupt':
        with pytest.raises(KeyboardInterrupt):
            runner.run(['test_fixture.py::test_selected'], output)
    else:
        result = runner.run(['test_fixture.py::test_selected'], output)
        assert result['exit_code'] == outcome
    receipt = json.loads((output/'result.json').read_text())
    assert receipt['scratch_removed'] and all(not p.exists() for p in seen)
    assert (output/'pytest.log').read_bytes() == b'test diagnostics\n'
    assert (output/'junit.xml').is_file()


def test_existing_evidence_and_unscoped_invocations_are_preserved(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path/'test_fixture.py').write_text('# selected test\n')
    output = tmp_path/'evidence'; output.mkdir(); (output/'proof').write_text('original')
    with pytest.raises(FileExistsError):
        runner.run(['test_fixture.py'], output)
    assert (output/'proof').read_text() == 'original'
    for tests in ([], ['.'], ['--basetemp=outside'], ['../test_fixture.py']):
        with pytest.raises(ValueError):
            runner.run(tests, tmp_path/'new')
    assert not (tmp_path/'new').exists()


def test_windows_late_handle_cleanup_retries_only_its_owned_workspace(monkeypatch):
    original = runner.tempfile.TemporaryDirectory
    owner = original(prefix='tare-pytest-fixture-')
    calls = []
    def cleanup():
        calls.append(owner.name)
        if len(calls) == 1:
            error = OSError('directory not empty while child handle closes')
            error.winerror = 145
            raise error
        owner.cleanup()
    monkeypatch.setattr(runner.tempfile, 'TemporaryDirectory',
                        lambda **kwargs: SimpleNamespace(name=owner.name, cleanup=cleanup))
    monkeypatch.setattr(runner.time, 'sleep', lambda _: None)
    with runner.workspace() as folder:
        assert folder == owner.name
        Path(folder, 'proof').write_text('temporary fixture')
    assert calls == [owner.name, owner.name] and not Path(owner.name).exists()


@pytest.mark.skipif(os.name != 'nt', reason='Windows extended-path cleanup')
def test_owned_workspace_cleanup_removes_nested_paths_beyond_win32_limit():
    with runner.workspace() as folder:
        nested = Path(folder)
        while len(str(nested)) < 280:
            nested /= 'nested-repository-fixture'
        extended = Path('\\\\?\\'+str(nested.resolve()))
        extended.mkdir(parents=True)
        (extended/'effect.json').write_text('retained only during test')
        os.chmod(extended/'effect.json', stat.S_IREAD)
        assert (extended/'effect.json').is_file()
    assert not Path(folder).exists()
