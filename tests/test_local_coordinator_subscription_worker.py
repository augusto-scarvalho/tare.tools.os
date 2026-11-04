from copy import deepcopy
import pytest
from relay import execution_scope as scope, turn_engine, work_units
from test_native_agent import deployment


@pytest.mark.parametrize('preference,kind,admitted', [
    ('subscription', 'subscription', True),
    ('subscription', 'api', False),
    ('', 'subscription', False),
    ('api', 'subscription', False),
])
def test_cli_preference_admits_configured_subscription_workers_only(monkeypatch, preference, kind, admitted):
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
    monkeypatch.setattr(work_units, 'bill', lambda d: ('local',) if d is parent else ('quota' if kind == 'subscription' else 'usd', 'codex'))
    with scope.activated(scope.Scope(prefer=preference)):
        if admitted:
            assert list(work_units.routes(parent, chosen='codex-worker')) == [('codex-worker', child)]
        else:
            with pytest.raises(ValueError, match='not on this session'):
                list(work_units.routes(parent, chosen='codex-worker'))
