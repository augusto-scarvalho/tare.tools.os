"""An ordinary worker's single call stays inside its existing bounded phase."""
from copy import deepcopy
from relay import work_units


def test_worker_request_inherits_phase_without_changing_profile_or_call_cap(tmp_path):
    deployment={'native_profile':{'endpoint':'cli://claude'},
                'native_execution_limits':{'max_model_calls':16,'max_seconds':300,'request_seconds':120}}
    original=deepcopy(deployment)
    limits=work_units.child_limits(tmp_path,{'limits':{'max_model_calls':None}},deployment=deployment)
    assert limits=={'max_model_calls':30,'max_seconds':900,'request_seconds':900}
    assert deployment==original


def test_request_timeout_is_cut_to_parent_remaining_phase(tmp_path,monkeypatch):
    monkeypatch.setattr('time.time',lambda:1000)
    limits=work_units.child_limits(tmp_path,{'limits':{'max_model_calls':None},'deadline_utc':1300})
    assert limits['request_seconds']==limits['max_seconds']==240
    assert limits['max_model_calls']==30
