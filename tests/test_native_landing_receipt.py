"""Native landing must evaluate recorded verification, never assume success."""
import pytest

from relay import executor_observation
from test_landing_evidence import _materialize


@pytest.mark.parametrize('observed', [False, True])
def test_native_landing_uses_verification_receipt(tmp_path, monkeypatch, observed):
    calls = []
    def check(train_dir, descriptor, receipt):
        calls.append((train_dir, descriptor.sha256, receipt.sha256))
        return observed
    monkeypatch.setattr(executor_observation, 'verification_observed', check)
    result = _materialize(tmp_path, monkeypatch, 'changed\n')
    assert len(calls) == 1
    assert result.payload['checks']['verification_complete'] is observed
