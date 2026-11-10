import asyncio
import copy
import hashlib
import json

import pytest
from graph_backlog.core import WorkGraph
from graph_backlog.jsonutil import compute_revision_hash, sha256_canonical
from graph_backlog.mutations import add_node_to_graph
from graph_backlog.validation import validate_work_graph
from relay import native_plan


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf-8')


@pytest.fixture
def proposal(tmp_path, monkeypatch):
    # Fixture providers still use automatic, independently admitted reviewers.
    monkeypatch.setattr('relay.native_routes.credential_available', lambda _: True)
    # Legacy routing scenarios use a synthetic high-capable local profile. This
    # exercises flow mechanics, not qualification of Qwen as a real planner.
    from relay import native_catalog
    select = native_catalog.select
    def fixture_profile(name=None, path=None, **kwargs):
        value = select(name, path, **kwargs)
        if path is None and name in {None, 'local-qwen', 'qwen38', 'qwen38-gsq'}:
            value = copy.deepcopy(value)
            params = json.loads(value['native_profile']['parameters_json'])
            params.setdefault('chat_template_kwargs', {})['reasoning_effort'] = 'high'
            value['native_profile']['parameters_json'] = json.dumps(params)
            value['native_profile']['source'] = 'Offline high-capable planning fixture; not a local-model qualification.'
        return value
    monkeypatch.setattr(native_catalog, 'select', fixture_profile)
    project = tmp_path/'project'; project.mkdir()
    (project/'a.py').write_text('value = 1\n')
    base = {'meta': {'schema': 'tare.tools/work-graph/0.5'}, 'sources': {}, 'nodes': [], 'edges': []}
    base = add_node_to_graph(WorkGraph(base), 'existing', 'Existing task')
    payload = {'summary': 'Bounded change', 'sources': [{'repository': 'project', 'path': 'a.py',
        'sha256': hashlib.sha256((project/'a.py').read_bytes()).hexdigest()}], 'remaining': [],
        'steps': ['Change a.py'], 'criteria': ['Value is two'], 'tasks': [
            {'key': 'change', 'title': 'Change value', 'summary': 'Set value to two', 'cluster': 'core',
             'priority': 'P1', 'criteria': ['Value equals two'], 'target_paths': ['a.py'], 'depends_on': ['existing']},
            {'key': 'test', 'title': 'Test value', 'summary': 'Protect value behavior', 'cluster': 'core',
             'priority': 'P2', 'criteria': ['Test fails for one'], 'target_paths': ['test_a.py'], 'depends_on': ['change']}]}
    directory = tmp_path/'plan'
    graph = tmp_path/'work-graph.json'; write(graph, base)
    value = {'schema': 'tare.tools/graph-plan/1', 'plan_id': 'plan-fixture', 'instruction': 'Change and test',
        'repository': 'project', 'roots': {'project': str(project)}, 'graph_path': str(graph),
        'base_revision': compute_revision_hash(base), 'observed_at': '2026-09-13T12:00:00Z', 'plan': payload,
        'patch': native_plan.compile_patch(base, payload, 'plan-fixture', 'project', '2026-09-13T12:00:00Z')}
    write(directory/'base-graph.json', base)
    write(directory/'input'/'proposal.json', value)
    write(directory/'input'/'request.json', {'instruction': value['instruction'], 'existing_work': base['nodes']})
    artifact = {'schema': 'tare.tools/native-artifact/1', 'role': 'plan-auditor', 'kind': 'review',
        'authority': 'NONE', 'session_id': 'review-session', 'call_id': 'review-call', 'profile_digest': 'a'*64,
        'payload': {'summary': 'Adequate', 'sources': [{'repository': 'proposal', 'path': 'proposal.json',
            'sha256': hashlib.sha256((directory/'input'/'proposal.json').read_bytes()).hexdigest()},
            {'repository': 'proposal', 'path': 'request.json',
             'sha256': hashlib.sha256((directory/'input'/'request.json').read_bytes()).hexdigest()}],
            'remaining': [], 'verdict': 'ACCEPT', 'findings': []}}
    write(directory/'review-artifact.json', artifact)
    write(directory/'review.json', {'proposal_sha256': sha256_canonical(value), 'review': artifact['payload']})
    return directory, graph, value, base


def test_exact_patch_preserves_existing_graph_and_retry_does_not_duplicate(proposal):
    directory, graph, value, base = proposal
    before = graph.read_bytes()
    view = native_plan.preview(directory)
    assert graph.read_bytes() == before
    assert view['status'] == 'REVIEWED'
    result = native_plan.apply(directory, view['proposal_sha256'])
    updated = json.loads(graph.read_bytes())
    assert result['status'] == 'APPLIED'
    assert updated['nodes'][0] == base['nodes'][0] and updated['sources'] == base['sources']
    assert validate_work_graph(updated)['status'] == 'PASS'
    assert all(n['admission_state'] != 'ADMITTED' for n in updated['nodes'][1:])
    from relay.work_run import load_node, derive_descriptor
    with pytest.raises(ValueError, match='WORK_REQUIRES_PREPARATION'):
        load_node(graph, 'plan-fixture-change')
    selected = updated['nodes'][1]
    assert selected['repository_scopes'][0]['target_paths'] == ['a.py']
    with pytest.raises(ValueError, match='VERIFICATION_CHECKS_MISSING'):
        derive_descriptor(selected, train_id='unused', repository_root=directory.parent/'project')
    assert updated['edges'][1]['from'] == 'plan-fixture-change'
    assert native_plan.apply(directory, view['proposal_sha256'])['status'] == 'ALREADY_APPLIED'
    assert len(json.loads(graph.read_bytes())['nodes']) == 3


@pytest.mark.parametrize('fault', ['source', 'graph', 'proposal', 'review', 'uncited', 'request-uncited', 'patch'])
def test_changed_inputs_or_unaccepted_review_never_mutate_graph(proposal, fault):
    directory, graph, value, base = proposal
    digest = native_plan.preview(directory)['proposal_sha256']
    if fault == 'source':
        (directory.parent/'project'/'a.py').write_text('value=9')
    elif fault == 'graph':
        base['nodes'][0]['title'] = 'Concurrent change'
        base['revision'] = compute_revision_hash(base); write(graph, base)
    elif fault in {'proposal', 'patch'}:
        if fault == 'patch':
            value['patch']['nodes'][0]['admission_state'] = 'ADMITTED'
        else:
            value['instruction'] = 'Another request'
        write(directory/'input'/'proposal.json', value)
    else:
        artifact = json.loads((directory/'review-artifact.json').read_bytes())
        if fault == 'review':
            artifact['payload']['verdict'] = 'REVISE'
        elif fault == 'request-uncited':
            artifact['payload']['sources'] = [s for s in artifact['payload']['sources'] if s['path'] != 'request.json']
            review = json.loads((directory/'review.json').read_bytes())
            review['review'] = artifact['payload']
            write(directory/'review.json', review)
        else:
            artifact['payload']['sources'] = []
        write(directory/'review-artifact.json', artifact)
    before = graph.read_bytes()
    with pytest.raises(ValueError): native_plan.apply(directory, digest)
    assert graph.read_bytes() == before


@pytest.mark.parametrize('fault', ['cycle', 'missing', 'duplicate', 'escape', 'collision'])
def test_invalid_decomposition_is_rejected_by_compiler_and_owner(proposal, fault):
    _, _, value, base = proposal
    payload = copy.deepcopy(value['plan'])
    if fault == 'cycle': payload['tasks'][0]['depends_on'] = ['test']
    if fault == 'missing': payload['tasks'][0]['depends_on'] = ['nonexistent']
    if fault == 'duplicate': payload['tasks'][1]['key'] = 'change'
    if fault == 'escape': payload['tasks'][0]['target_paths'] = ['../secret']
    if fault == 'collision': base['nodes'][0]['id'] = 'plan-fixture-change'
    with pytest.raises(ValueError):
        native_plan.compile_patch(base, payload, 'plan-fixture', 'project', value['observed_at'])


def test_tui_requires_review_and_explicit_apply_then_selects_created_work(proposal):
    from textual.widgets import Input, Select, Button
    from relay.native_console import WorkConsole
    from relay.console_backend import Backend
    directory, graph, value, base = proposal
    class LocalBackend(Backend):
        def catalog(self, root):
            return {'models': [{'deployment': 'local-qwen', 'model': 'Qwen'}],
                'sessions': [], 'works': [], 'errors': []}
    async def scenario():
        app = WorkConsole(directory.parent/'project', backend=LocalBackend())
        async with app.run_test(size=(140,46)) as pilot:
            await pilot.pause()
            app.query_one('#mode', Select).value = 'plan'
            app.query_one('#plan', Input).value = str(directory)
            await pilot.pause()
            assert app.query_one('#apply-plan', Button).disabled
            before = graph.read_bytes()
            await app.review_selection().wait()
            assert graph.read_bytes() == before
            assert not app.query_one('#apply-plan', Button).disabled
            app.apply_reviewed_plan()
            while app.busy: await pilot.pause(0.05)
            assert app.last_result['status'] == 'APPLIED'
            assert app.query_one('#mode', Select).value == 'work'
            assert app.query_one('#work-id', Input).value == 'plan-fixture-change'
    asyncio.run(scenario())


def test_plan_review_keeps_literal_windows_graph_path_and_rows(proposal):
    from relay.console_output import SelectableMessage
    from relay.native_console import WorkConsole
    from relay.console_backend import Backend
    from textual.selection import Selection
    directory, _, _, _ = proposal
    result=native_plan.preview(directory)
    # A presentation-only view; this literal is never passed to the graph owner.
    path=r'C:\projects\_archives\[flow15]\work-graph.json'
    result['proposal']['graph_path']=path
    class LocalBackend(Backend):
        def catalog(self,root):
            return {'models':[{'deployment':'local-qwen','model':'fixture'}],
                    'sessions':[],'works':[],'errors':[]}
    async def scenario():
        app=WorkConsole(directory.parent/'project',backend=LocalBackend())
        async with app.run_test(size=(140,46)) as pilot:
            await app.workers.wait_for_complete()
            app.show_plan(result);await pilot.pause()
            message=app.query_one('.plan-review',SelectableMessage)
            shown=message.get_selection(Selection(None,None))[0]
            assert '\nGraph: '+path+'\n' in shown
            assert '\nReviewer: Adequate\n' in shown
            assert 'Apply adds proposed tasks.' in shown
    asyncio.run(scenario())


def pending_review(proposal):
    directory, _, value, _ = proposal
    (directory/'review.json').unlink()
    config = {'role': 'plan-auditor', 'context_policy': {'max_compactions': None}}
    native_plan.save_review_settings(directory, value, config,
        {'native_profile': {'model': 'saved-model'}}, {'max_calls': None})
    return directory


def test_retry_only_reviews_preserves_settings_history_and_graph(proposal, monkeypatch):
    directory = pending_review(proposal)
    frozen = {p: p.read_bytes() for p in [directory/'input'/'proposal.json',
        directory/'input'/'request.json', directory/'base-graph.json', proposal[1]]}
    calls = []
    def run(config, deployment, custody, instruction, **kwargs):
        calls.append(custody)
        assert config['role'] == 'plan-auditor' and config['writable'] == {}
        assert config['context_policy']['max_compactions'] is None
        assert deployment['native_profile']['model'] == 'saved-model'
        assert kwargs['limits'] == {'max_calls': None}
        assert native_plan.preview(directory)['status'] == 'REVIEW_REQUIRED'
        with pytest.raises(ValueError, match='active_writer'):
            native_plan.retry_review(directory)
        with pytest.raises(ValueError, match='active_writer'):
            native_plan.apply(directory, native_plan.preview(directory)['proposal_sha256'])
        write(custody/'journal.jsonl', {'attempt': len(calls)})
        return ({'status': 'CANCELLED', 'reason': 'OPERATOR_CANCELLED'} if len(calls) == 1 else
            {'status': 'PROPOSED', 'artifact_file': str(directory/'review-artifact.json')})
    monkeypatch.setattr('relay.native_agent.run_session', run)
    failed = native_plan.retry_review(directory)
    assert failed['review_attempts'][0]['status'] == 'CANCELLED'
    first = {p: p.read_bytes() for p in calls[0].iterdir()}
    final = native_plan.retry_review(directory)
    assert final['status'] == 'REVIEWED' and len(final['review_attempts']) == 2
    assert calls[0] != calls[1] and all(p.read_bytes() == b for p, b in first.items())
    assert all(p.read_bytes() == b for p, b in frozen.items())
    assert native_plan.retry_review(directory)['status'] == 'REVIEWED' and len(calls) == 2


@pytest.mark.parametrize('fault', ['request', 'proposal', 'graph', 'source', 'patch'])
def test_retry_refuses_changed_inputs_before_model_call(proposal, monkeypatch, fault):
    directory = pending_review(proposal)
    def forbidden(*args, **kwargs):
        pytest.fail('Model must not be called for changed planning inputs')
    monkeypatch.setattr('relay.native_agent.run_session', forbidden)
    if fault == 'request':
        write(directory/'input'/'request.json', {'instruction': 'Different'})
    elif fault == 'source':
        (directory.parent/'project'/'a.py').write_text('value=9')
    elif fault == 'graph':
        graph = copy.deepcopy(proposal[3]); graph['nodes'][0]['title'] = 'Changed'
        write(proposal[1], graph)
    else:
        value = proposal[2]
        if fault == 'patch': value['patch']['nodes'][0]['admission_state'] = 'ADMITTED'
        else: value['instruction'] = 'Changed'
        write(directory/'input'/'proposal.json', value)
    with pytest.raises(ValueError): native_plan.retry_review(directory)
    assert not (directory/'review-attempts.json').exists()


def test_crash_does_not_reuse_old_acceptance_and_keeps_attempt(proposal, monkeypatch):
    directory = proposal[0]
    # Simulate durable RUNNING state after an older acceptance and a process death.
    native_plan.save_review_settings(directory, proposal[2], {}, {}, {})
    write(directory/'review-attempts.json', {'attempts': [{'directory': 'review-interrupted', 'status': 'RUNNING'}]})
    assert native_plan.preview(directory)['status'] == 'REVIEW_REQUIRED'
    with pytest.raises(ValueError, match='review accepting'):
        native_plan.apply(directory, native_plan.preview(directory)['proposal_sha256'])
    def crash(*args, **kwargs): raise OSError('Injected process boundary')
    monkeypatch.setattr('relay.native_agent.run_session', crash)
    with pytest.raises(OSError): native_plan.retry_review(directory)
    attempts = native_plan.preview(directory)['review_attempts']
    assert len(attempts) == 2 and all(a['status'] == 'INTERRUPTED' for a in attempts)
    assert native_plan.preview(directory)['status'] == 'REVIEW_REQUIRED'
    monkeypatch.setattr('relay.native_agent.run_session', lambda *a, **kw:
        {'status': 'PROPOSED', 'artifact_file': str(directory/'review-artifact.json')})
    assert native_plan.retry_review(directory)['status'] == 'REVIEWED'


def test_substantive_rejection_requires_revised_proposal(proposal, monkeypatch):
    directory = pending_review(proposal)
    artifact = json.loads((directory/'review-artifact.json').read_bytes())
    artifact['payload'].update(verdict='REVISE', remaining=['Clarify the task'])
    write(directory/'review-artifact.json', artifact)
    calls = []
    def run(*args, **kwargs):
        calls.append(True)
        return {'status': 'PROPOSED', 'artifact_file': str(directory/'review-artifact.json')}
    monkeypatch.setattr('relay.native_agent.run_session', run)
    assert native_plan.retry_review(directory)['status'] == 'REVIEW_REQUIRED'
    with pytest.raises(ValueError, match='proposal corrections'): native_plan.retry_review(directory)
    assert len(calls) == 1


def test_legacy_settings_recovery_preserves_original_model_and_evidence(proposal, monkeypatch):
    directory, _, value, base = proposal
    (directory/'review.json').unlink()
    write(directory/'input'/'request.json', {'instruction': value['instruction'], 'existing_work': [
        {'id': n['id'], 'title': n['title'], 'status': n['work_status'], 'criteria': n['exit_criteria']}
        for n in base['nodes']]})
    saved = {'config': {'context_policy': {'max_compactions': None}}, 'deployment': {'model': 'original'},
             'limits': {'max_calls': None}}
    write(directory/'auditor'/'native-agent.json', saved)
    write(directory/'auditor-result.json', {'status': 'BLOCKED', 'reason': 'INVALID_PROVIDER_RESPONSE'})
    frozen = (directory/'auditor'/'native-agent.json').read_bytes()
    def run(config, deployment, custody, instruction, **kwargs):
        assert deployment == saved['deployment'] and kwargs['limits'] == saved['limits']
        return {'status': 'CANCELLED'}
    monkeypatch.setattr('relay.native_agent.run_session', run)
    result = native_plan.retry_review(directory)
    assert result['review_attempts'][0]['status'] == 'BLOCKED'
    assert (directory/'auditor'/'native-agent.json').read_bytes() == frozen


def test_tui_plan_retry_uses_saved_options_and_preserves_path_on_child_failure(proposal):
    from relay.console_backend import Selection, command
    from relay.native_console import WorkConsole
    from test_native_console import FakeBackend
    from textual.widgets import Input, Select, Button
    directory = pending_review(proposal)
    selection = Selection(str(directory.parent/'project'), mode='plan', plan=str(directory),
        deployment='other-model', unlimited=True, profile='other-profile', prompt='Changed demand')
    args = command(selection, True)
    assert '--retry-review' in args
    assert not any(x in args for x in ('--deployment', '--native-deployment', '--unlimited', '--instruction'))
    selection.root = str(directory)
    with pytest.raises(ValueError, match='another project'): command(selection, True)
    async def scenario():
        backend = FakeBackend()
        async def broken():
            backend.process.returncode = 2
            return b'', b'Injected child failure'
        backend.process.communicate = broken
        app = WorkConsole(directory.parent/'project', backend=backend)
        async with app.run_test(size=(140,46)) as pilot:
            await app.action_reload().wait()
            await pilot.pause()
            app.query_one('#mode', Select).value = 'plan'
            app.query_one('#plan', Input).value = str(directory)
            await pilot.pause()
            assert str(app.query_one('#resume', Button).label) == 'Retry review'
            app.execute(True)
            while app.busy: await pilot.pause(0.05)
            assert '--retry-review' in backend.launches[0]
            assert app.query_one('#plan', Input).value == str(directory)
            assert not app.reviewed_plan
    asyncio.run(scenario())


@pytest.mark.parametrize('reviewer_name',[None,'bedrock-opus'])
@pytest.mark.parametrize('unlimited',[False,True])
@pytest.mark.parametrize('independent_available',[False,True])
def test_new_plan_uses_recoverable_review_stage(proposal, monkeypatch, reviewer_name, unlimited, independent_available):
    directory, graph, value, _ = proposal
    target = directory.parent/'new-plan'
    roles = []
    def run(config, deployment, custody, instruction, **kwargs):
        roles.append(config['role'])
        selected = json.loads((target/'routes.json').read_bytes())[config['role']]
        assert selected['deployment_sha256']==sha256_canonical(deployment)
        assert kwargs['limits']['max_model_calls']==(None if unlimited else 7 if config['role']=='plan-auditor' and reviewer_name else 12)
        if config['role'] == 'planner':
            payload = value['plan']
        else:
            assert (target/'review-settings.json').exists()
            payload = {'summary': 'Adequate', 'sources': [{'repository': 'proposal', 'path': name,
                'sha256': hashlib.sha256((target/'input'/name).read_bytes()).hexdigest()}
                for name in ('proposal.json', 'request.json')], 'remaining': [], 'verdict': 'ACCEPT', 'findings': []}
        artifact = {'schema': 'tare.tools/native-artifact/1', 'role': config['role'],
            'kind': 'plan' if config['role'] == 'planner' else 'review', 'authority': 'NONE',
            'session_id': 'fixture', 'call_id': 'call', 'profile_digest': 'a'*64, 'payload': payload}
        write(custody/'artifact.json', artifact)
        return {'status': 'PROPOSED', 'artifact_file': str(custody/'artifact.json')}
    monkeypatch.setattr('relay.native_agent.run_session', run)
    from relay.native_catalog import select
    reviewer = select(reviewer_name) if reviewer_name else None
    monkeypatch.setattr('relay.native_routes.credential_available', lambda _: independent_available)
    if reviewer: reviewer['native_execution_limits']={'max_model_calls':7}
    result = native_plan.generate(directory.parent/'project', graph, target, 'Change and test', select('local-qwen'),
                                  unlimited=unlimited, reviewer_deployment=reviewer)
    if reviewer:
        assert result['routes']['plan-auditor']['reason']=='OPERATOR_SELECTED_REVIEWER'
    elif independent_available:
        assert result['routes']['plan-auditor']['selection']['role']=='plan-auditor'
        assert result['routes']['plan-auditor']['deployment']=='opencode-sub-gemini-4-argon'  # subscription before paid
    else:
        assert result['routes']['plan-auditor']['reason']=='SEPARATE_SESSION_NO_INDEPENDENT_REVIEWER'
        assert result['routes']['plan-auditor']['independent'] is False
        assert 'selection' not in result['routes']['plan-auditor']
        assert 'not attested' in result['review']['independence']
        persisted = json.loads((target/'routes.json').read_bytes())['plan-auditor']
        decision = persisted['executed_decision']
        planner_id = select('local-qwen')['id']
        assert decision['executed_deployment'] == planner_id
        assert decision['propensity'] == {planner_id: 1} and type(decision['propensity'][planner_id]) is int
        assert decision['ope_supported'] is False
        assert decision['fallback']['executed'] == planner_id
        assert decision['fallback']['skipped'][0]['reason'] == persisted['selection_attempt']['reason']
        assert persisted['independent'] is False
    assert result['routes']['planner']['model']==select('local-qwen')['native_profile']['model']
    assert (target/'review-settings.json').exists()
    assert result['routes']['plan-auditor']['deployment'] in native_plan.render(result)
    assert result['status'] == 'REVIEWED'
    assert roles == ['planner', 'plan-auditor'] and len(result['review_attempts']) == 1


@pytest.mark.parametrize('override', [['--deployment', 'local-qwen'], ['--unlimited'], ['--instruction', 'Changed'], ['--reviewer-deployment','bedrock-opus'],['--reviewer-profile','other.json']])
def test_cli_retry_rejects_setting_overrides(proposal, monkeypatch, override):
    monkeypatch.setattr(native_plan, 'retry_review', lambda *a, **kw: pytest.fail('No retry with overrides'))
    assert native_plan.main(['--plan', str(proposal[0]), '--retry-review', *override]) == 2


def test_replan_uses_current_graph_sources_and_prior_findings_without_overwriting(proposal, monkeypatch):
    directory, graph, value, base = proposal
    # The prior plan is rejected; its original source then changes legitimately.
    artifact = json.loads((directory/'review-artifact.json').read_bytes())
    artifact['payload'].update(verdict='REVISE', remaining=['Only one implementation task is needed'])
    write(directory/'review-artifact.json', artifact)
    write(directory/'review.json', {'proposal_sha256': sha256_canonical(value), 'review': artifact['payload']})
    (directory.parent/'project'/'a.py').write_text('value = 3\n')
    current = copy.deepcopy(base)
    current['nodes'][0]['completion']['status'] = 'DONE'
    current['nodes'][0]['completion']['dod_satisfied'] = True
    current['revision'] = compute_revision_hash(current); write(graph, current)
    frozen = {p: p.read_bytes() for p in directory.rglob('*') if p.is_file()}
    target = directory.parent/'successor'
    roles = []
    def run(config, deployment, custody, instruction, **kwargs):
        roles.append(config['role'])
        request = json.loads((target/'input'/'request.json').read_bytes())
        assert request['previous_plan']['review']['review']['verdict'] == 'REVISE'
        assert request['existing_work'][0]['completion']['status'] == 'DONE'
        if config['role'] == 'planner':
            payload = copy.deepcopy(value['plan'])
            payload['tasks'] = payload['tasks'][:1]
            payload['sources'] = [{'repository': repo, 'path': name,
                'sha256': hashlib.sha256(path.read_bytes()).hexdigest()} for repo, name, path in (
                    ('project', 'a.py', directory.parent/'project'/'a.py'),
                    ('request', 'request.json', target/'input'/'request.json'))]
        else:
            # Parent acceptance/rejection is never the new review.
            assert native_plan.preview(target)['status'] == 'REVIEW_REQUIRED'
            payload = {'summary': 'Adequate correction', 'verdict': 'ACCEPT', 'findings': [], 'remaining': [],
                'sources': [{'repository': 'proposal', 'path': name,
                    'sha256': hashlib.sha256((target/'input'/name).read_bytes()).hexdigest()}
                    for name in ('proposal.json', 'request.json')]}
        record = {'schema': 'tare.tools/native-artifact/1', 'role': config['role'],
            'kind': 'plan' if config['role'] == 'planner' else 'review', 'authority': 'NONE',
            'session_id': 'fixture', 'call_id': 'call', 'profile_digest': 'a'*64, 'payload': payload}
        write(custody/'artifact.json', record)
        return {'status': 'PROPOSED', 'artifact_file': str(custody/'artifact.json')}
    monkeypatch.setattr('relay.native_agent.run_session', run)
    from relay.native_catalog import select
    view = native_plan.replan(directory, target, 'Use one task and preserve completed Work.', select(), unlimited=True)
    assert view['status'] == 'REVIEWED' and roles == ['planner', 'plan-auditor']
    assert view['proposal']['predecessor']['proposal_sha256'] == sha256_canonical(value)
    assert view['proposal']['base_revision'] == current['revision']
    assert all(p.read_bytes() == raw for p, raw in frozen.items())
    assert json.loads(graph.read_bytes()) == current
    native_plan.apply(target, view['proposal_sha256'])
    updated = json.loads(graph.read_bytes())
    assert updated['nodes'][0] == current['nodes'][0] and len(updated['nodes']) == 2
    assert updated['edges'][0]['from'] == 'existing'
    assert all(p.read_bytes() == raw for p, raw in frozen.items())


@pytest.mark.parametrize('fault', ['empty-feedback', 'same-custody', 'nested-custody', 'tampered-parent', 'busy-parent'])
def test_replan_invalid_or_busy_parent_does_not_call_model(proposal, monkeypatch, fault):
    from contextlib import nullcontext
    from compute_plane.native_journal import custody_lock
    directory, _, value, _ = proposal
    target = directory.parent/'successor'; feedback = 'Fix the review findings'
    if fault == 'empty-feedback': feedback = '  '
    if fault == 'same-custody': target = directory
    if fault == 'nested-custody': target = directory/'child'
    if fault == 'tampered-parent':
        value['patch']['nodes'][0]['title'] = 'Tampered'; write(directory/'input'/'proposal.json', value)
    monkeypatch.setattr('relay.native_agent.run_session', lambda *a, **kw: pytest.fail('No model call allowed'))
    with custody_lock(directory) if fault == 'busy-parent' else nullcontext():
        with pytest.raises(ValueError): native_plan.replan(directory, target, feedback, {})
    if target != directory: assert not target.exists()


def test_tui_replan_creates_new_custody_and_passes_explicit_feedback(proposal, monkeypatch):
    from relay.console_backend import Selection, command
    from relay.native_console import WorkConsole
    from test_native_console import FakeBackend
    from textual.widgets import Input, Select, TextArea
    directory = proposal[0]
    monkeypatch.setattr(native_plan, 'plan_root', lambda: directory.parent/'successors')
    chosen = Selection(str(directory.parent/'project'), mode='plan', plan=str(directory), prompt='One task, preserve DONE',
        deployment='fable-tc', unlimited=True)
    args = command(chosen, replan=True)
    assert args[args.index('--replan')+1] == str(directory)
    assert args[args.index('--instruction')+1] == chosen.prompt
    assert args[args.index('--deployment')+1] == 'fable-tc' and '--unlimited' in args
    assert chosen.plan != str(directory)
    async def scenario():
        backend = FakeBackend(); app = WorkConsole(directory.parent/'project', backend=backend)
        async with app.run_test(size=(140,48)) as pilot:
            await pilot.pause()
            app.query_one('#mode', Select).value = 'plan'
            app.query_one('#plan', Input).value = str(directory)
            app.query_one('#prompt', TextArea).load_text('One task, preserve DONE')
            await pilot.pause(); await pilot.click('#replan'); await pilot.pause()
            assert '--replan' in backend.launches[0]
            assert app.query_one('#plan', Input).value != str(directory)
            app.action_cancel()
            while app.busy: await pilot.pause(0.05)
            assert app.last_result['status'] == 'CANCELLED'
    asyncio.run(scenario())


def test_retry_refuses_reviewer_route_drift_before_inference(proposal,monkeypatch):
    directory=pending_review(proposal)
    settings=json.loads((directory/'review-settings.json').read_bytes())
    write(directory/'routes.json',{'plan-auditor':{'deployment_sha256':sha256_canonical(settings['deployment'])}})
    settings['deployment']['native_profile']['model']='changed'
    write(directory/'review-settings.json',settings)
    monkeypatch.setattr('relay.native_agent.run_session',lambda *a,**kw:pytest.fail('Route drift must not invoke model'))
    with pytest.raises(ValueError,match='review route changed'):native_plan.retry_review(directory)


def test_tui_new_plan_reviewer_selection_and_resume(proposal):
    from relay.console_backend import Selection,command
    directory=proposal[0]
    selection=Selection(root=str(directory.parent/'project'),mode='plan',prompt='A new demand',
                        plan_reviewer='bedrock-opus')
    args=command(selection)
    assert args[args.index('--reviewer-deployment')+1]=='bedrock-opus'
    selection.plan=str(directory)
    assert '--reviewer-deployment' not in command(selection,resume=True)
    selection.plan_reviewer_profile='custom.json'
    with pytest.raises(ValueError,match='not both'):command(selection)
    selection.plan_reviewer=''
    assert '--reviewer-profile' in command(selection)


def test_a_tasks_checks_become_the_evidence_its_work_requires(proposal):
    # Ways of working (D6): a task may name the session's verifiers and the tests that check it; the graph keeps
    # them as the Work's required evidence (its node schema has no field of its own).
    directory, graph, value, base = proposal
    payload = copy.deepcopy(value['plan'])
    payload['tasks'][0].update(verifiers=['unit'], test_paths=['test_a.py::test_two'])
    patch = native_plan.compile_patch(base, payload, 'plan-checks', 'project', value['observed_at'])
    node = next(n for n in patch['nodes'] if n['id'] == 'plan-checks-change')
    assert node['evidence_required'][-2:] == ['verifier: unit', 'test: test_a.py::test_two']
    payload['tasks'][0]['test_paths'] = ['../outside.py']
    with pytest.raises(ValueError, match='literal'):
        native_plan.compile_patch(base, payload, 'plan-bad', 'project', value['observed_at'])
