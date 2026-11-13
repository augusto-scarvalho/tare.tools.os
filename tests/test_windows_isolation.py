from types import SimpleNamespace

import pytest
import relay.windows_isolation as module


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    calls = []
    class Launcher:
        def __init__(self, *args, **kwargs): calls.append(('launcher', args, kwargs))
    monkeypatch.setattr(module, 'WindowsMxcProfile', Launcher)
    monkeypatch.setattr(module, 'CodexSandboxProfile', Launcher)
    monkeypatch.setattr('compute_plane.linux_bwrap.LinuxBwrapProfile', Launcher)  # imported only for 'bwrap'
    mediator = SimpleNamespace(execute=lambda facts, effect: effect())
    def mediation(**session): calls.append(('mediate', session)); return mediator
    capabilities = SimpleNamespace(terminal=None,
        bind_command_confinement=lambda *args: calls.append(('bind', args)))
    session = dict(session_id='s', capabilities=capabilities,
        config={'repositories': {'fixture': str(tmp_path)}})
    args = dict(backend='mxc', policy=object(), cwd=str(tmp_path), environment={'SAFE': 'fixed'},
        seconds=10, mediation_factory=mediation)
    return args, session, mediator, calls


@pytest.mark.parametrize('backend', ['mxc', 'codex', 'bwrap'])
def test_host_composes_one_launcher_and_existing_mediator(fixture, backend):
    args, session, mediator, calls = fixture
    args['backend'] = backend
    if backend == 'codex': args.update(codex_executable='C:/admitted/codex.exe', codex_sha256='a'*64)
    bind = module.windows_isolation_factory(**args)
    args['environment']['SAFE'] = 'changed by caller'
    assert bind(**session) is mediator
    assert [item[0] for item in calls] == ['launcher', 'mediate', 'bind']
    assert calls[0][2]['environment'] == {'SAFE': 'fixed'}
    assert calls[-1][1][1:] == ({'SAFE': 'fixed'}, 10)
    if backend == 'codex': assert calls[0][2]['expected_executable_sha256'] == 'a'*64


@pytest.mark.parametrize('unbounded', [False, True])
def test_explicit_verification_scope_keeps_the_same_launcher_and_mediator(fixture, unbounded):
    args, session, mediator, calls = fixture
    observed=[]
    session['capabilities'].bind_command_confinement=lambda *a, **kw: observed.append((a,kw))
    bind=module.windows_isolation_factory(**args,verification_seconds=300,unbounded_verification=unbounded)
    assert bind(**session) is mediator
    assert [row[0] for row in calls]==['launcher','mediate']
    assert observed[0][0][1:]==({'SAFE':'fixed'},10)
    assert observed[0][1]['verification_seconds']==300
    assert observed[0][1].get('unbounded_verification',False) is unbounded


@pytest.mark.parametrize('change', [dict(backend='auto'), dict(seconds=0),
    dict(seconds=float('inf')), dict(environment={'TOKEN': 42}),
    dict(mediation_factory=None), dict(codex_executable='C:/other/codex.exe'),
    dict(backend='bwrap', codex_executable='C:/other/codex.exe'),
    dict(backend='codex'), dict(unbounded_verification=1)])
def test_invalid_host_configuration_refuses_without_binding(fixture, change):
    args, session, mediator, calls = fixture
    args.update(change)
    with pytest.raises(ValueError): module.windows_isolation_factory(**args)
    assert calls == []


@pytest.mark.parametrize('case', ['outside', 'terminal', 'missing_mediator', 'launcher_failure'])
def test_failed_admission_never_binds_or_falls_back(fixture, tmp_path, monkeypatch, case):
    args, session, mediator, calls = fixture
    bind = module.windows_isolation_factory(**args)
    if case == 'outside': session['config']['repositories']['fixture'] = str(tmp_path.parent)
    if case == 'terminal': session['capabilities'].terminal = object()
    if case == 'missing_mediator':
        # Closure deliberately observes the host's rejected mediator result.
        mediator.execute = None
    if case == 'launcher_failure':
        def fail(*a, **kw): raise OSError('MXC unavailable')
        monkeypatch.setattr(module, 'WindowsMxcProfile', fail)
    with pytest.raises((ValueError, OSError)): bind(**session)
    assert not any(call[0] == 'bind' for call in calls)
    assert len([call for call in calls if call[0] == 'launcher']) <= 1
