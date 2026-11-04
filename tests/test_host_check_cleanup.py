import json
from pathlib import Path
import sys,time
import pytest
from relay import work_units,native_agent
from test_native_agent import setup,deployment  # noqa:F401
from test_implement_unit import repository


def test_timeout_stops_a_real_grandchild_before_workspace_can_be_removed(tmp_path):
    started=tmp_path/'started';escaped=tmp_path/'escaped'
    grandchild="from pathlib import Path;import time;Path(%r).write_text('started');time.sleep(2);Path(%r).write_text('escaped')" % (str(started),str(escaped))
    parent="import subprocess,sys,time;subprocess.Popen([sys.executable,'-c',%r]);time.sleep(30)" % grandchild
    result=work_units.managed_check([sys.executable,'-c',parent],cwd=tmp_path,timeout=1)
    assert started.exists(), 'The real grandchild must have started before the timeout.'
    assert result.process['cleanup_verified'] and result.process['timeout']
    assert result.execution_limitation=='Check timeout; process tree cleanup verified.'
    time.sleep(2.1)
    assert not escaped.exists()


def test_success_removes_nested_check_scratch(tmp_path):
    code="import tempfile;from pathlib import Path;p=Path(tempfile.gettempdir());(p/'generated').write_text('scratch');print(p)"
    result=work_units.managed_check([sys.executable,'-c',code],cwd=tmp_path,timeout=10)
    assert result.returncode==0 and result.process['cleanup_verified']
    assert not Path(result.stdout.strip()).exists()


def test_cleanup_handles_deep_windows_artifact_paths(tmp_path):
    code="import tempfile,os,stat;from pathlib import Path;from compute_plane.native_journal import io_path;p=Path(tempfile.gettempdir());d=p/('a'*60)/('b'*60)/('c'*60)/('.git')/'objects';io_path(d).mkdir(parents=True);f=io_path(d/('f'*64));f.write_text('readonly Git object');os.chmod(f,stat.S_IREAD);print(p)"
    result=work_units.managed_check([sys.executable,'-c',code],cwd=tmp_path,timeout=10)
    assert result.returncode==0 and result.process['cleanup_verified']
    assert not Path(result.stdout.strip()).exists()


def test_nested_pytest_owner_can_clean_a_realistic_artifact_tree(tmp_path):
    code="import tempfile;from pathlib import Path;from compute_plane.native_journal import io_path;root=Path(tempfile.gettempdir());t=tempfile.TemporaryDirectory(prefix='tare-pytest-');d=Path(t.name)/'pytest'/'test_auto_implementer_composes0'/'.reconciliation'/'work-runs'/('a'*32)/'code-audit'/'0'/'scratch'/'effect-receipts';io_path(d).mkdir(parents=True);io_path(d/('b'*64+'.json')).write_text('{}');t.cleanup();print(root)"
    result=work_units.managed_check([sys.executable,'-c',code],cwd=tmp_path,timeout=10)
    assert result.returncode==0, result.stderr
    assert result.process['cleanup_verified'] and not Path(result.stdout.strip()).exists()


def test_unknown_cleanup_keeps_candidate_and_starts_no_implementer(setup,monkeypatch):
    config,custody,root=setup;repository(root)
    monkeypatch.setattr(work_units,'routes',lambda *a,**kw:iter([('worker',deployment())]))
    monkeypatch.setattr(native_agent,'run_session',lambda *a,**kw:pytest.fail('No inference before confirmed cleanup.'))
    def unknown(*args,**kwargs):raise work_units.CheckCleanupUnknown('Injected unconfirmed cleanup')
    monkeypatch.setattr(work_units,'run_checks',unknown)
    result=work_units.run_unit(custody,{'config':config,'deployment':deployment(),
        'limits':{'max_model_calls':30},'deadline_utc':None},
        {'title':'Set two','target_paths':['a.py'],'criteria':['value equals two'],'verifiers':['unit']},lambda:False)
    assert result['verdict']=='INCONCLUSIVE' and Path(result['retained_tree']).is_dir()
    assert (root/'a.py').read_text()=='value = 1\n'
