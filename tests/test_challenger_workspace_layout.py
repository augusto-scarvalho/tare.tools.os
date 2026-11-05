"""Retained receipts stay nested; disposable comparison roots stay shallow on Windows."""
from pathlib import Path
import json
import pytest
from relay import challenger_workspace as workspace


def test_new_comparison_roots_are_short_distinct_and_inside_the_accounted_storage(tmp_path):
    area=tmp_path/'challengers';job=area/('p'*24)/'runs'/('r'*32)
    job.mkdir(parents=True)
    (job/'run.json').write_text(json.dumps({'execution_contract':{'workspace_layout':1}}))
    roots=[workspace.tree(job,name) for name in ('candidate','reference')]
    assert len(set(roots))==2
    for root in roots:
        assert root.parent==area/'wt'
        assert len(root.name)==16 and all(c in '0123456789abcdef' for c in root.name)
        assert len(str(root))<len(str(job/'candidate'))-40
    other=area/('p'*24)/'runs'/('s'*32);other.mkdir()
    (other/'run.json').write_bytes((job/'run.json').read_bytes())
    assert workspace.tree(other,'candidate') not in roots
    assert workspace.tree(job,'candidate')==roots[0]


def test_legacy_comparison_roots_and_unknown_names_keep_their_boundary(tmp_path):
    job=tmp_path/'job';job.mkdir()
    assert workspace.tree(job,'candidate')==job/'candidate'
    (job/'run.json').write_text('{"execution_contract":{"variant_inbox":1}}')
    assert workspace.tree(job,'reference')==job/'reference'
    with pytest.raises(ValueError):workspace.tree(job,'../outside')


def test_the_workspace_rides_out_a_windows_reader_denied_during_a_replace(tmp_path, monkeypatch):
    from relay import challenger, challenger_workspace
    job = tmp_path/'runs'/'job'; job.mkdir(parents=True); (job/'run.json').write_text('{}')
    real, calls = challenger.read_json, []
    def replacing(path, limit):  # the worker's atomic replace briefly denies a reader on Windows
        calls.append(path)
        if len(calls) == 1:
            raise PermissionError('replace in progress')
        return real(path, limit)
    monkeypatch.setattr(challenger, 'read_json', replacing)
    monkeypatch.setattr(challenger.time, 'sleep', lambda seconds: None)
    assert challenger_workspace.tree(job, 'candidate') == job/'candidate' and len(calls) == 2
