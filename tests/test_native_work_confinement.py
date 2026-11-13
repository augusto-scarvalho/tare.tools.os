"""Offline Work composition: existing review/isolation ports, no child process."""
from types import SimpleNamespace
import json

import pytest

from test_command_protection import host
from relay import command_protection as protection
from relay.native_work import implement_native


class Prepared(Exception):
    pass


@pytest.fixture
def work(host, monkeypatch):
    import compute_plane.native_capabilities as capabilities
    import compute_plane.native_models as models
    instances = []
    generation = []

    class Tools:
        names = ('verify',)
        tool_specs_json = '[]'

        def __init__(self, **kwargs):
            self.options = kwargs
            self.mediation = None
            self.claim_action = host.claim
            instances.append(self)

        def bind_command_mediation(self, value):
            self.mediation = value

        def verify(self, criterion):
            selected = self.options['verifiers'][criterion]
            assert selected['argv'] == [str(host.executable), '-c', 'print(1)']
            if self.mediation is None:
                return host.effect()
            return self.mediation.execute(host.facts(), host.effect)

    def adapter(*args, **kwargs):
        generation.append('generation')
        instances[-1].verify('unit')
        raise Prepared()

    monkeypatch.setattr(capabilities, 'NativeCapabilities', Tools)
    monkeypatch.setattr(models, 'NativeModelAdapter', adapter)
    descriptor = SimpleNamespace(work_id='work', sha256='a'*64, execution_repository='project',
        tracked_paths=('check.py',), verification_checks=[SimpleNamespace(
            criterion='unit', command=(str(host.executable), '-c', 'print(1)'))])
    settings = dict(worktree=host.root, descriptor=descriptor,
        node={'id': 'work', 'exit_criteria': ['unit']}, session_id='session',
        control=host.control, journal=SimpleNamespace(snapshot=lambda: SimpleNamespace(records=())),
        profile=SimpleNamespace(context_window=8192, output_tokens=256), client=None,
        context_policy=SimpleNamespace(window=8192, output_reserve=256),
        custody=host.custody, budget=SimpleNamespace(max_model_calls=1))
    return SimpleNamespace(host=host, settings=settings, instances=instances, generation=generation)


def test_named_verifier_uses_production_factory_once(work):
    with pytest.raises(Prepared):
        implement_native(**work.settings, command_protection=work.host.config['command_protection'],
                         command_protection_deployment=work.host.deployment)
    assert len(work.host.native) == 1
    assert work.host.effects == ['effect']
    assert work.instances[0].options['writable'] == {'project': ['check.py']}
    assert work.instances[0].names == ('verify',)


@pytest.mark.parametrize('scoped', [True, False])
def test_review_activity_retains_work_source(work, scoped):
    from relay.native_activity import activity_view

    source = {'session': str(work.host.custody.resolve()), 'kind': 'attempt',
              'mode': 'work', 'owner': 'work', 'project': str(work.host.root.resolve()),
              'control_root': str(work.host.custody.parent.resolve()),
              'stage': 'Work work · Implement', 'work': 'work'} if scoped else None
    events = []
    with pytest.raises(Prepared):
        implement_native(**work.settings, command_protection=work.host.config['command_protection'],
                         command_protection_deployment=work.host.deployment,
                         on_activity=events.append, activity_source=source)
    assert len(events) == 1
    event = events[0]
    if scoped:
        assert event['channel'] == 'activity'
        record = event['activity']
    else:
        record = event
    assert record['operation'] == 'command' and record['state'] == 'running'
    assert record['detail'] == 'Reviewing command with codex.'
    view = activity_view(record)
    if scoped:
        assert record['source'] is source
        assert view['source'] == source

        import asyncio
        from relay.console_output import observe_process

        displayed, diagnostics = [], []

        async def parse_envelope():
            stdout, stderr = asyncio.StreamReader(), asyncio.StreamReader()
            stdout.feed_data(b'{}')
            stdout.feed_eof()
            stderr.feed_data((json.dumps(event) + '\n').encode('utf-8'))
            stderr.feed_eof()

            async def wait():
                return 0

            process = SimpleNamespace(stdout=stdout, stderr=stderr, wait=wait, returncode=0)
            await observe_process(process, displayed.append, diagnostics.append)

        asyncio.run(parse_envelope())
        assert displayed == [{'channel': 'activity', 'activity': view}]
        assert displayed[0]['activity']['source'] == source
        assert diagnostics == []
    else:
        assert 'source' not in record and 'source' not in view
    assert len(work.host.native) == 1 and work.host.effects == ['effect']


@pytest.mark.parametrize('failure', ['missing', 'failed'])
def test_binding_failure_precedes_generation_and_effect(work, monkeypatch, failure):
    def factory(*args, **kwargs):
        def bind(**session):
            if failure == 'failed':
                raise ValueError('offline binding failed')
            return None
        return bind
    monkeypatch.setattr(protection, 'factory', factory)
    with pytest.raises(ValueError):
        implement_native(**work.settings, command_protection=work.host.config['command_protection'],
                         command_protection_deployment=work.host.deployment)
    assert work.generation == work.host.effects == work.host.native == []


def test_absence_keeps_unmediated_composition(work, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Absent policy must not compose protection')
    monkeypatch.setattr(protection, 'factory', forbidden)
    with pytest.raises(Prepared):
        implement_native(**work.settings)
    assert work.instances[0].mediation is None
    assert work.host.effects == ['effect'] and work.host.native == []


def test_changed_cli_pin_precedes_generation(work):
    work.host.cli.write_bytes(b'changed executable')
    with pytest.raises(ValueError, match='saved Codex executable changed'):
        implement_native(**work.settings, command_protection=work.host.config['command_protection'],
                         command_protection_deployment=work.host.deployment)
    assert work.generation == work.host.effects == []


@pytest.mark.parametrize('change', ['policy', 'pin', 'override'])
def test_host_resume_refuses_changed_saved_identity(host, change):
    from relay.native_work_host import create_runner
    policy = dict(host.config['command_protection'])
    state = {'schema': 'tare.tools/native-work-host/1', 'work_id': 'work',
        'deployment': host.deployment, 'command_protection': policy,
        'command_protection_deployment': host.deployment,
        'command_protection_sha256': protection.digest({'policy': policy, 'deployment': host.deployment})}
    override = None
    if change == 'policy':
        policy['max_reviews'] = 2
    elif change == 'pin':
        host.cli.write_bytes(b'changed executable')
    else:
        override = dict(policy, max_reviews=2)
    (host.custody/'host.json').write_text(json.dumps(state), encoding='utf-8')
    with pytest.raises(ValueError):
        create_runner(host.deployment, 'work', host.custody, resume=True,
                      command_protection=override)


@pytest.mark.parametrize('protected', [True, False])
def test_saved_resume_without_deployment_reaches_generation_setup(host, monkeypatch, protected):
    from relay.native_work_host import create_runner
    from relay import native_generation

    state = {'schema': 'tare.tools/native-work-host/1', 'work_id': 'work',
             'deployment': host.deployment, 'limits': {'max_seconds': 120},
             'compactions': 0}
    if protected:
        policy = host.config['command_protection']
        state.update(command_protection=policy,
                     command_protection_deployment=host.deployment,
                     command_protection_sha256=protection.digest(
                         {'policy': policy, 'deployment': host.deployment}))
    (host.custody/'host.json').write_text(json.dumps(state), encoding='utf-8')
    observed = []

    def profile(deployment, model):
        observed.append((deployment, model))
        raise Prepared()

    def forbidden(*args, **kwargs):
        raise AssertionError('Generation setup must not retrieve secrets or invoke a provider')

    monkeypatch.setattr(native_generation, 'deployment_profile', profile)
    monkeypatch.setattr(native_generation, 'vault_secret', forbidden)
    monkeypatch.setattr(native_generation, 'inference_client', forbidden)
    with pytest.raises(Prepared):
        create_runner(None, 'work', host.custody, resume=True)
    assert observed == [(host.deployment, host.deployment['native_profile']['model'])]
    assert host.effects == host.native == []


@pytest.fixture
def host_verifier(work, monkeypatch, tmp_path):
    from pathlib import Path
    from compute_plane import managed_process
    from relay.native_settlement import NativeWorkRunner

    host = work.host
    policy = dict(host.config['command_protection'], max_reviews=6)
    settings = work.settings | {
        'command_protection': protection.prepare(policy, host.deployment),
        'command_protection_deployment': host.deployment}
    train = tmp_path / 'host-verification'
    train.mkdir()
    calls, scopes = [], []
    output, errors = b'x' * 20000, b'separate stderr\x00'
    receipt = {'exit_code': 0, 'cleanup_verified': True, 'cancelled': False,
               'timeout': False, 'output_limit': False, 'isolation_violation': False}

    def launcher(*args, **kwargs):
        raise AssertionError('Offline process port must not launch a child')

    def isolation(**options):
        scopes.append(options)
        def bind(**session):
            session['capabilities'].bind_command_confinement(
                launcher, options['environment'], options['seconds'],
                **{key:options[key] for key in ('verification_seconds','unbounded_verification') if key in options})
        return bind

    def process(argv, **options):
        calls.append((argv, options))
        assert options['process_factory'] is launcher
        assert options['max_output'] == 1024 * 1024
        assert not options['cancelled']()
        Path(options['dest'], 'stdout.txt').write_bytes(output)
        Path(options['dest'], 'stderr.txt').write_bytes(errors)
        return dict(receipt)

    def forbidden(*args, **kwargs):
        raise AssertionError('Protected host verification must never use pipe fallback')

    monkeypatch.setattr(protection, 'windows_isolation_factory', isolation)
    monkeypatch.setattr(managed_process, 'run_process', process)
    monkeypatch.setattr(managed_process, 'run_pipe_process', forbidden)
    runner = NativeWorkRunner.verification_runner(host.control, train,
        descriptor=settings['descriptor'], settings=settings)
    return SimpleNamespace(run=runner, host=host, settings=settings, train=train,
        calls=calls, scopes=scopes, receipt=receipt, output=output, errors=errors,
        argv=settings['descriptor'].verification_checks[0].command)


def invoke_host_verifier(verifier, argv=None):
    return verifier.run(verifier.argv if argv is None else argv,
        cwd=verifier.host.root, timeout=20, env={'UNTRUSTED': 'ignored'})


def test_host_verifier_preserves_streams_and_candidate_file_scopes(host_verifier):
    verifier = host_verifier
    result = invoke_host_verifier(verifier)
    assert result.stdout == verifier.output and len(result.stdout) > 16384
    assert result.stderr == verifier.errors
    options = verifier.scopes[0]
    assert options['cwd'] == str(verifier.host.root.resolve())
    writes = set(options['policy'].read_write)
    assert str(verifier.host.root.resolve()) not in writes
    assert str((verifier.host.root / 'check.py').resolve()) in writes
    assert verifier.calls[0][1]['cwd'] == options['cwd']
    assert 'UNTRUSTED' not in verifier.calls[0][1]['env']
    assert len(verifier.host.native) == 1
    assert json.loads((verifier.train / 'NATIVE_VERIFIER_PROCESS.json').read_bytes())['exit_code'] == 0


@pytest.mark.parametrize('unbounded,parent_seconds',[(False,1000),(True,1000),(True,float('inf'))])
def test_work_host_verifier_honors_time_scope_and_keeps_mediation(host_verifier,monkeypatch,
                                                               unbounded,parent_seconds):
    import math
    import time
    from dataclasses import replace
    from relay import execution_scope
    scope=replace(execution_scope.current(),unlimited=('time',) if unbounded else ())
    monkeypatch.setattr(execution_scope,'current',lambda:scope)
    verifier=host_verifier;original=verifier.host.control
    control=type(original)(original.binding,original.cancellation,time.monotonic()+parent_seconds)
    run=protection.protected_work_verifier(control,verifier.train,
        descriptor=verifier.settings['descriptor'],settings=verifier.settings)
    result=run(verifier.argv,cwd=verifier.host.root,timeout=None,env={})
    assert result.returncode==0 and result.stdout==verifier.output
    wall=verifier.calls[0][1]['timeout']
    if not unbounded:
        assert wall==300
    elif math.isfinite(parent_seconds):
        assert 990<wall<=1000
    else:
        assert math.isinf(wall)
    assert verifier.scopes[0]['unbounded_verification'] is unbounded
    assert len(verifier.host.native)==1
    with pytest.raises(Exception):
        run(verifier.argv,cwd=verifier.host.root,timeout=None,env={})
    assert len(verifier.calls)==1  # the existing single-effect claim still applies


@pytest.mark.parametrize('case', ['green', 'blocked', 'always', 'always-local', 'always-unknown'])
def test_work_verification_without_protection_runs_in_the_sandbox_with_no_review(host_verifier, monkeypatch, case):
    import subprocess
    from relay.turn_engine import set_checks_outside_sandbox
    verifier = host_verifier
    monkeypatch.setattr(protection, 'sandbox_available', lambda: True)
    settings = {k: v for k, v in verifier.settings.items()
                if k not in ('command_protection', 'command_protection_deployment')}
    settings['repository_root'] = str(verifier.host.root)
    # NativeWorkRunner passes challenger.local of the implementer's deployment; unknown counts as local.
    settings.update({'always': {'local_producer': False}, 'always-local': {'local_producer': True}}.get(case, {}))
    if case != 'green':
        monkeypatch.setattr(protection, 'verification_limitation',
                            lambda value: 'ISOLATION_NETWORK_DENIED: every failing test was a refused connection.')
    if case.startswith('always'):  # the project's user chose it in the TUI: a Work run, where nobody is asked,
        set_checks_outside_sandbox(verifier.host.root, True)  # honours it, but never for a local model's work
    if case != 'always':
        case = 'blocked' if case != 'green' else case
    outside = []  # the plain runner NativeWorkRunner.verification_runner passes
    runner = protection.protected_work_verifier(verifier.host.control, verifier.train,
        descriptor=settings['descriptor'], settings=settings,
        outside=lambda argv, **options: outside.append(argv) or subprocess.CompletedProcess(argv, 0, b'outside', b''))
    if case == 'blocked':
        with pytest.raises(ValueError, match='NATIVE_CANDIDATE_VERIFIER_SANDBOX_BLOCKED: no one can be asked'):
            invoke_host_verifier(SimpleNamespace(run=runner, argv=verifier.argv, host=verifier.host))
        assert outside == []
    else:
        result = invoke_host_verifier(SimpleNamespace(run=runner, argv=verifier.argv, host=verifier.host))
        assert result.returncode == 0
        assert (outside == [verifier.argv]) is (case == 'always') and (result.stdout == b'outside') is (case == 'always')
    assert verifier.scopes[0]['backend'] == 'mxc' and verifier.host.native == []  # no review, no Codex gate


def test_host_verifier_atomic_claim_prevents_second_effect(host_verifier):
    invoke_host_verifier(host_verifier)
    with pytest.raises(Exception):
        invoke_host_verifier(host_verifier)
    assert len(host_verifier.calls) == 1


def test_host_verifier_interrupted_claim_refuses_replay(host_verifier):
    host_verifier.receipt['cleanup_verified'] = False
    with pytest.raises(ValueError, match='NATIVE_CANDIDATE_VERIFIER_INTERRUPTED'):
        invoke_host_verifier(host_verifier)
    with pytest.raises(Exception):
        invoke_host_verifier(host_verifier)
    assert len(host_verifier.calls) == 1


def test_host_verifier_missing_isolation_never_falls_back(host_verifier, monkeypatch):
    monkeypatch.setattr(protection, 'windows_isolation_factory',
        lambda **options: lambda **session: None)
    with pytest.raises(ValueError, match='NATIVE_WORK_COMMAND_MEDIATION_UNAVAILABLE'):
        invoke_host_verifier(host_verifier)
    assert host_verifier.calls == []


def test_host_verifier_cancelled_control_never_falls_back(host_verifier):
    from relay.native_settlement import NativeWorkRunner
    original = host_verifier.host.control

    class Stopped:
        binding = original.binding
        def should_stop(self):
            return True

    host_verifier.run = NativeWorkRunner.verification_runner(Stopped(), host_verifier.train,
        descriptor=host_verifier.settings['descriptor'], settings=host_verifier.settings)
    with pytest.raises(ValueError, match='NATIVE_CANDIDATE_CONTROL_STOPPED'):
        invoke_host_verifier(host_verifier)
    assert host_verifier.calls == host_verifier.scopes == []


def test_host_verifier_undeclared_command_denied_before_effect(host_verifier):
    with pytest.raises(ValueError, match='NATIVE_CANDIDATE_VERIFIER_NOT_DECLARED'):
        invoke_host_verifier(host_verifier, (*host_verifier.argv, '--undeclared'))
    assert host_verifier.calls == host_verifier.scopes == []


@pytest.mark.parametrize('resume', [False, True])
def test_work_console_policy_cli_composition(tmp_path, monkeypatch, resume):
    import tare_ops
    from relay.console_backend import Selection, command
    from relay import work_run, native_catalog, work_audit
    policy = protection.validate({'review': 'tare', 'backend': 'mxc',
        'classifier': 'jev', 'review_policy': 'on_demand',
        'max_reviews': 3, 'review_seconds': 17})
    selection = Selection(root=str(tmp_path), mode='work', work='work',
        deployment='fixture', unlimited=False, max_model_calls='5', max_seconds='45',
        command_protection=policy)
    seen = []
    monkeypatch.setattr(work_run, 'run_work',
        lambda *args, **kwargs: seen.append(kwargs) or {'status': 'WAITING_FOR_HUMAN'})
    monkeypatch.setattr(native_catalog, 'select', lambda *args, **kwargs: {'id': 'fixture'})
    monkeypatch.setattr(work_audit, 'select_deployments', lambda *args: None)
    argv = command(selection, resume=resume)
    assert tare_ops.main(argv) == 0
    if resume:
        assert 'command_protection' not in seen[0]
        assert '--deployment' not in argv and '--max-model-calls' not in argv
    else:
        assert seen[0]['command_protection'] == policy
        assert seen[0]['native_deployment'] == {'id': 'fixture'}
        assert seen[0]['limits'] == {'max_model_calls': 5, 'max_seconds': 45.0}


@pytest.mark.parametrize('flag,value', [('--command-review', 'tare'),
    ('--command-isolation', 'mxc'), ('--command-classifier', 'jev'),
    ('--command-review-policy', 'always'), ('--max-command-reviews', '3'),
    ('--command-review-seconds', '17')])
def test_work_cli_rejects_resume_policy_overrides(monkeypatch, capsys, flag, value):
    import tare_ops
    from relay import work_run
    def forbidden(*args, **kwargs):
        raise AssertionError('Rejected policy must not dispatch Work')
    monkeypatch.setattr(work_run, 'run_work', forbidden)
    assert tare_ops.main(['run', 'work', '--resume', flag, value]) == 2
    output = capsys.readouterr()
    assert 'WORK_RESUME_REUSES_ORIGINAL_EXECUTION_OPTIONS' in output.out + output.err


@pytest.mark.parametrize('flag,value', [('--command-isolation', 'mxc'),
    ('--command-classifier', 'jev'), ('--command-review-policy', 'always'),
    ('--max-command-reviews', '3'), ('--command-review-seconds', '17')])
def test_work_cli_requires_explicit_review(monkeypatch, capsys, flag, value):
    import tare_ops
    from relay import work_run
    def forbidden(*args, **kwargs):
        raise AssertionError('Rejected policy must not dispatch Work')
    monkeypatch.setattr(work_run, 'run_work', forbidden)
    assert tare_ops.main(['run', 'work', flag, value]) == 2
    output = capsys.readouterr()
    assert 'Choose --command-review' in output.out + output.err


@pytest.mark.parametrize('branch', ['implementer', 'native_runner',
    'prepared_implementation', 'preparation_source'])
def test_protected_incompatible_work_refused_before_effects(monkeypatch, branch):
    from relay import work_run
    def forbidden(*args, **kwargs):
        raise AssertionError('No graph, custody, provider or command effects allowed')
    monkeypatch.setattr(work_run, '_resolve_graph_scope', forbidden)
    with pytest.raises(ValueError, match='WORK_COMMAND_PROTECTION_REQUIRES_MANAGED_IMPLEMENTATION'):
        work_run._run_work('work', command_protection={'review': 'tare'}, **{branch: object()})


def test_protected_work_resume_override_refused_before_effects(monkeypatch):
    from relay import work_run
    monkeypatch.setattr(work_run, '_resolve_graph_scope',
        lambda *args: pytest.fail('Resume override must be rejected before effects'))
    with pytest.raises(ValueError, match='WORK_RESUME_REUSES_ORIGINAL_EXECUTION_OPTIONS'):
        work_run._run_work('work', resume=True, command_protection={})


def test_protected_work_console_refuses_interactive_terminal(tmp_path):
    from relay.console_backend import Selection, command
    with pytest.raises(ValueError, match='interactive terminal'):
        command(Selection(root=str(tmp_path), mode='work', work='work',
            terminal=True, command_protection={'review': 'tare'}))


@pytest.mark.parametrize('change', ['none', 'digest', 'pin', 'prepared', 'host'])
def test_origin_policy_validated_before_recovery_provider(host, monkeypatch, change):
    from relay.work_recovery import origin_protection_options, resume_execution
    from relay import work_run
    policy = dict(host.config['command_protection'])
    origin = {'command_protection': policy, 'command_protection_deployment': host.deployment,
        'command_protection_sha256': protection.digest({'policy': policy, 'deployment': host.deployment}),
        'implementation_custody': str(host.custody)}
    if change == 'none':
        assert origin_protection_options(origin) == {
            'command_protection': policy, 'command_protection_deployment': host.deployment}
        assert origin_protection_options({}) == {}
        return
    if change == 'digest':
        origin['command_protection_sha256'] = '0'*64
    elif change == 'pin':
        host.cli.write_bytes(b'changed CLI')
    elif change == 'prepared':
        origin['prepared_implementation'] = {'path': 'unused'}
    else:
        (host.custody/'host.json').write_text(json.dumps(dict(origin, command_protection={})))
    monkeypatch.setattr(work_run, 'load_node',
        lambda *args: pytest.fail('Identity rejection must precede provider/plan recovery'))
    with pytest.raises(ValueError):
        resume_execution(None, SimpleNamespace(work_graph='unused', work_id='work'),
            origin, {}, lambda *args: None, None, retry_plan=True)


@pytest.mark.parametrize('protected', [False, True])
@pytest.mark.parametrize('existing_host', [False, True])
def test_resume_forwards_original_policy_before_host_creation(host, monkeypatch, protected, existing_host):
    from relay import work_recovery, work_run, relay_mesh, native_work_host, human_wait
    from relay import train_execution
    train = host.custody/'train'
    train.mkdir()
    (train/'STATE.json').write_text(json.dumps({'status': 'PLAN_APPROVED'}))
    origin = {'node': {'id': 'work'}, 'native_deployment': host.deployment,
        'repository_root': str(host.root), 'implementation_custody': str(host.custody),
        'effective_limits': {'implementation': {'max_model_calls': 5},
            'max_compactions_per_stage': 1, 'candidate_verification_seconds': 60}}
    expected = {}
    if protected:
        policy = host.config['command_protection']
        expected = {'command_protection': policy,
                    'command_protection_deployment': host.deployment}
        origin.update(expected, command_protection_sha256=protection.digest(
            {'policy': policy, 'deployment': host.deployment}))
    if existing_host:
        state = {key: origin[key] for key in ('command_protection',
            'command_protection_deployment', 'command_protection_sha256') if key in origin}
        (host.custody/'host.json').write_text(json.dumps(state))
    monkeypatch.setattr(work_run, 'load_node', lambda *args: origin['node'])
    monkeypatch.setattr(work_run, 'eligibility_check', lambda *args: None)
    monkeypatch.setattr(work_run, 'git', lambda *args: 'base')
    monkeypatch.setattr(train_execution, 'validate_descriptor_for_train', lambda *args, **kwargs: None)
    monkeypatch.setattr(human_wait, 'checkpoint', lambda *args: True)
    monkeypatch.setattr(relay_mesh, 'current_lease', lambda *args: None)
    observed = []
    def create(deployment, work_id, custody, **options):
        observed.append((deployment, work_id, custody, options))
        raise Prepared()
    monkeypatch.setattr(native_work_host, 'create_runner', create)
    descriptor = SimpleNamespace(work_graph='graph.json', work_id='work',
        train_id='train', target_ref='main', base_commit='base')
    with pytest.raises(Prepared):
        work_recovery.resume_execution(train, descriptor, origin, {}, lambda *args: None, None)
    deployment, work_id, custody, options = observed[0]
    assert deployment == (None if existing_host else host.deployment)
    assert work_id == 'work' and custody == host.custody
    assert options['resume'] == existing_host
    assert options['limits'] == {'max_model_calls': 5}
    for key in ('command_protection', 'command_protection_deployment'):
        if protected:
            assert options[key] == expected[key]
        else:
            assert key not in options
