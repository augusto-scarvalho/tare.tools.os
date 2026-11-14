from copy import deepcopy
import pytest
from relay import execution_scope as scope, turn_engine, work_units
from test_native_agent import deployment


@pytest.mark.parametrize('preference', ['', 'subscription', 'api'])
@pytest.mark.parametrize('kind', ['subscription', 'api'])
def test_a_local_coordinator_may_choose_a_worker_on_another_account_whatever_it_prefers(monkeypatch, preference, kind):
    # Before 2026-11-14 only --prefer_subscription let a local coordinator's unit leave the local bill. The account
    # no longer limits a unit; --prefer_* orders the routes and never leaves one out.
    parent = deployment()
    parent['native_profile']['endpoint'] = 'http://127.0.0.1:8080/v1'
    child = deepcopy(parent)
    child['id'] = 'codex-worker'
    if kind == 'subscription':
        child['native_profile'].update(protocol='vendor-cli', endpoint='cli://codex')
    else:
        child['native_profile'].update(protocol='openai-chat', endpoint='https://inference.example/v1')
        child['native_credential_env'] = 'EXAMPLE_KEY'
    monkeypatch.setattr(turn_engine, 'policy', lambda: {'implementer': [['codex-worker']]})
    monkeypatch.setattr(turn_engine, 'available', lambda name: (child, None))
    with scope.activated(scope.Scope(prefer=preference)):
        assert list(work_units.routes(parent, chosen='codex-worker')) == [('codex-worker', child)]
