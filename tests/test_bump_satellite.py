"""A candidate bump is not qualified by testing the previous committed pins."""
import importlib.util
from pathlib import Path
import sys

import pytest


spec=importlib.util.spec_from_file_location('bump_satellite',Path(__file__).resolve().parents[1]/'scripts/ops/bump_satellite.py')
bump=importlib.util.module_from_spec(spec)
spec.loader.exec_module(bump)


def test_native_diagnostic_encoding_does_not_hide_process_failure(tmp_path):
    code,out,err=bump.run_cmd([sys.executable,'-c',
        "import sys;sys.stdout.buffer.write(b'bad\\xea');sys.stderr.buffer.write(b'failure\\xff');sys.exit(7)"],tmp_path)
    assert code==7 and out.startswith('bad') and err.startswith('failure')


def fixture_bump(tmp_path,monkeypatch,*,gate=0,commit=0):
    name='tare.tools.backlog-graph'
    (tmp_path/name).mkdir()
    monkeypatch.setattr(bump,'SATELLITES_DIR',tmp_path)
    monkeypatch.setattr(bump,'get_current_pinned_sha',lambda _: 'a'*40)
    calls=[]; pins=[]; committed=False
    monkeypatch.setattr(bump,'update_readme_pin',lambda *args:pins.append(args))
    def run(args,cwd):
        nonlocal committed
        calls.append(args)
        if args==['git','rev-parse','HEAD']:return 0,'b'*40,''
        if args==['git','rev-parse','HEAD:satellites/'+name]:return 0,('b' if committed else 'a')*40,''
        if args[:2]==['git','commit']:
            committed=commit==0
            return commit,'','commit refused' if commit else ''
        if 'pytest' in args or '-m' in args:return gate,'','gate failed' if gate else ''
        return 0,'',''
    monkeypatch.setattr(bump,'run_cmd',run)
    return name,calls,pins


def test_uncommitted_bump_reports_pending_without_running_legacy_gate(tmp_path,monkeypatch,capsys):
    name,calls,pins=fixture_bump(tmp_path,monkeypatch)
    bump.cmd_bump(name,target_ref='b'*40)
    assert pins==[(name,'b'*7)]
    assert not any('pytest' in call or '-m' in call for call in calls)
    assert 'pending' in capsys.readouterr().out.lower()


def test_committed_bump_checks_current_federation_with_its_python(tmp_path,monkeypatch):
    name,calls,_=fixture_bump(tmp_path,monkeypatch)
    bump.cmd_bump(name,target_ref='b'*40,auto_commit=True)
    gate=[sys.executable,'-m','pytest','tests/test_federation_submodules.py']
    assert gate in calls
    assert next(i for i,call in enumerate(calls) if call[:2]==['git','commit']) < calls.index(gate)
    assert not any('tests/test_harness_adapter.py' in call for call in calls)


@pytest.mark.parametrize('failed_step',['commit','gate'])
def test_failed_committed_candidate_is_retained_for_repair(tmp_path,monkeypatch,failed_step):
    name,calls,pins=fixture_bump(tmp_path,monkeypatch,
        gate=1 if failed_step=='gate' else 0,commit=1 if failed_step=='commit' else 0)
    with pytest.raises(SystemExit) as error:
        bump.cmd_bump(name,target_ref='b'*40,auto_commit=True)
    assert error.value.code==1
    assert pins==[(name,'b'*7)]
    assert ['git','checkout','a'*40] not in calls
    if failed_step=='commit':assert not any('-m' in call and 'pytest' in call for call in calls)
