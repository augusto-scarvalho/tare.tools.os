"""Replay the two measured candidates without models, operator custody or network."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize('arm', ['A','B'])
@pytest.mark.verifies('SPEC-OS-WORKFLOW-IR-COMPILER-001', 'AC-09')
def test_published_pair_retains_each_work_obligation(tmp_path, arm):
    report = Path(__file__).resolve().parents[1]/'docs/guides/FLOW14_COMPARISON.json'
    raw = report.read_bytes()
    assert len(raw) < 32768
    data = json.loads(raw)
    files = data['frozen_workload']['files']
    for name,body in files.items():
        path = tmp_path/name
        assert path.resolve().is_relative_to(tmp_path.resolve())
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(body.encode('utf-8'))
        assert hashlib.sha256(path.read_bytes()).hexdigest()==data['frozen_workload']['sha256'][name]
    subprocess.run(['git','init','-q'],cwd=tmp_path,check=True,capture_output=True,timeout=10)
    def check(*modules):
        return subprocess.run([sys.executable,'-B','-m','unittest','-q',*modules],cwd=tmp_path,
                              capture_output=True,timeout=10)
    assert check('tests.test_normalize','tests.test_report').returncode != 0
    def apply(work):
        patch = data['candidate_patches'][arm][work].encode('utf-8')
        subprocess.run(['git','apply','--check','-'],input=patch,cwd=tmp_path,check=True,capture_output=True,timeout=10)
        subprocess.run(['git','apply','-'],input=patch,cwd=tmp_path,check=True,capture_output=True,timeout=10)
    apply('ledger-normalize')
    assert check('tests.test_normalize').returncode == 0
    assert check('tests.test_report').returncode != 0
    apply('ledger-report')
    result = check('tests.test_normalize','tests.test_report')
    assert result.returncode == 0, result.stderr.decode(errors='replace')
    for name,body in files.items():
        if name not in ('flowledger/normalize.py','flowledger/report.py'):
            assert (tmp_path/name).read_bytes() == body.encode('utf-8')
