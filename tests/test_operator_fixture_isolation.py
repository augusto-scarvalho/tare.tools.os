"""A faster temporary allocator must keep operator stores isolated per test."""
import os
from pathlib import Path

import pytest


@pytest.fixture(scope='module')
def observed_store_paths():
    return set()


@pytest.mark.parametrize('case', range(3))
def test_operator_store_state_never_reaches_the_next_test(case, observed_store_paths):
    paths = [Path(os.environ[name]).parent for name in
             ('TARE_GPU_STORE', 'TARE_STORAGE_AUTO', 'TARE_ENVIRONMENT_STORE')]
    paths.append(Path(os.environ['LOCALAPPDATA']))
    for path in paths:
        assert path.is_dir()
        assert path not in observed_store_paths
        assert not (path/'previous-test-sentinel').exists()
        (path/'previous-test-sentinel').write_text(str(case), encoding='utf-8')
    observed_store_paths.update(paths)
