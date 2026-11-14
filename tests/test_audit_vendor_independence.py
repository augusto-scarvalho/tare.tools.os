"""An independent auditor is one of another vendor; a same-vendor audit runs and counts, labelled (owner, 2026-11-14)."""
import json
from pathlib import Path

import pytest

from relay import native_catalog, native_routes, turn_engine, work_audit
from relay.landing_policy import SAME_VENDOR_AUDIT, SAME_VENDOR_REVIEW, independence
from test_conversation_review_recovery import reviewed  # noqa: F401  (fixtures)
from test_conversation_tasks import host  # noqa: F401
from test_cross_vendor_auditors import install, pool  # noqa: F401
from test_native_agent import setup  # noqa: F401

FABLE = 'claude-api-fable-5-1'


def only(*keys):
    return lambda key: key in keys


def shipped():
    return {name: value for name, value, _ in native_catalog.offline_deployments(include_unconfigured=True)}


def who(producer, auditor):
    return independence(work_audit.identity(producer, 'implementer'), work_audit.identity(auditor, 'auditor'))


def test_the_rule_refuses_same_model_or_bias_and_names_the_same_vendor():
    routes = shipped()
    fable = native_catalog.select(FABLE)
    assert who(fable, routes['claude-fable-5-1-cli']) == 'AUDITOR_NOT_INDEPENDENT'  # same model id
    assert who(fable, native_catalog.select('claude-api-fable-5-2')) == 'AUDITOR_NOT_INDEPENDENT'  # same bias domain
    assert who(fable, native_catalog.select('claude-api-opus-5-5')) == 'SAME_VENDOR_NOT_INDEPENDENT'
    assert who(fable, native_catalog.select('openai-api')) == 'INDEPENDENT'
    # Still refused as today, wherever it is decided.
    (row,) = work_audit.prepare(fable, [native_catalog.select('claude-api-fable-5-2')], 'optional')['routes']
    assert (row['eligible'], row['reason']) == (False, 'AUDITOR_NOT_INDEPENDENT')


def test_a_claude_on_agy_auditing_claude_code_is_a_same_vendor_audit():
    routes = shipped()
    (row,) = work_audit.prepare(routes['claude-fable-5-1-cli'], [routes['agy-claude-opus-5-5-cli']], 'optional')['routes']
    assert (row['eligible'], row['cross_vendor'], row['reason']) == (True, False, 'SAME_VENDOR_NOT_INDEPENDENT')


def test_opus_as_the_only_auditor_of_fable_runs_labelled_everywhere(pool, monkeypatch):
    monkeypatch.setattr(native_routes, 'credential_available', only('ANTHROPIC_API_KEY'))
    fable = native_catalog.select(FABLE)
    selected, receipt = native_routes.reviewers(fable)
    assert selected[0]['id'] == 'claude-api-opus-5-5'
    assert next(c for c in receipt['candidates'] if c['deployment'] == 'claude-api-opus-5-5')['reason'] == 'SAME_VENDOR_NOT_INDEPENDENT'
    work = work_audit.prepare(fable, 'auto', 'optional')
    assert (work['status'], work['routes'][0]['reason']) == ('READY', 'SAME_VENDOR_NOT_INDEPENDENT')
    # The conversation audit (TUI and tare serve): who audited, the verdict and the fix request all say so.
    verdict = {'verdict': 'REVISE'}
    monkeypatch.setattr('relay.native_agent.run_session', lambda *a, **kw: {'status': 'PROPOSED', 'artifact_file': 'x'})
    monkeypatch.setattr('relay.native_artifacts.read_input', lambda path: {'artifact': {'payload': {
        'verdict': verdict['verdict'], 'summary': 'Off by one', 'findings': [{'claim': 'Loop ends early'}], 'remaining': []}}})
    monkeypatch.setattr(turn_engine, 'triage', lambda record, *a, **kw: record['findings'])
    shown = []
    record, fix = turn_engine.audit_turn([{'path': 'a.py', 'diff': '+x\n'}], pool.parent, 'Change a.py', fable, shown.append)
    assert record['route'] == 'claude-api-opus-5-5' and record['independence'] == SAME_VENDOR_AUDIT
    assert f'_tare audit · claude-api-opus-5-5 · {SAME_VENDOR_AUDIT} · 1 changed lines_' in ''.join(shown)
    assert f'**REVISE** ({SAME_VENDOR_AUDIT}) — Off by one' in ''.join(shown)
    assert fix.startswith(f'An auditor (claude-api-opus-5-5 · {SAME_VENDOR_AUDIT}) reviewed your change')
    verdict['verdict'] = 'ACCEPT'
    again, _ = turn_engine.audit_turn([{'path': 'a.py', 'diff': '+y\n'}], pool.parent, fix, fable, shown.append)
    assert again['recheck'] is True  # the labelled fix request is still recognized as the fix round


@pytest.mark.parametrize('path', ['automatic', 'prefer_subscription', 'owner_list', 'owner_list_remapped'])
def test_another_vendor_on_a_costlier_tier_is_chosen_before_the_same_vendor(pool, monkeypatch, path):
    install(pool, 'claude-opus-5-5-cli', 'cli://claude', 'claude-opus-5-5')  # flat, priority 0
    monkeypatch.setattr(native_routes, 'credential_available', only('OPENAI_API_KEY'))  # openai-api: metered
    fable = native_catalog.select(FABLE)
    if path == 'prefer_subscription':
        monkeypatch.setenv('TARE_SESSION_SCOPE', json.dumps({'prefer': 'subscription'}))
    if path.startswith('owner_list'):
        monkeypatch.setattr(turn_engine, 'available', lambda name, **kw: (native_catalog.select(name), None))
        if path == 'owner_list_remapped':  # the subscription preference sets the owner's API route aside
            monkeypatch.setenv('TARE_SESSION_SCOPE', json.dumps({'prefer': 'subscription'}))
        names = ['openai-api', 'claude-opus-5-5-cli'] if path == 'owner_list_remapped' else ['claude-opus-5-5-cli', 'openai-api']
        chosen = [name for name, _ in turn_engine.pick_auditors(fable, names)]
    else:
        chosen = [d['id'] for d in native_routes.reviewers(fable)[0]]
    assert chosen[0] == 'openai-api' and 'claude-opus-5-5-cli' in chosen


def test_the_owner_list_names_a_same_vendor_auditor_in_its_receipt(pool, monkeypatch):
    monkeypatch.setattr(turn_engine, 'available', lambda name, **kw: (native_catalog.select(name), None))
    turn_engine.policy_path().parent.mkdir(parents=True, exist_ok=True)
    turn_engine.policy_path().write_text(json.dumps({'auditor': ['claude-api-opus-5-5', 'openai-api']}), encoding='utf-8')
    selected, receipt = native_routes.reviewers(native_catalog.select(FABLE))
    assert [d['id'] for d in selected] == ['openai-api', 'claude-api-opus-5-5']
    assert [c['reason'] for c in receipt['candidates']] == ['OPERATOR_SELECTED_INDEPENDENT', 'SAME_VENDOR_NOT_INDEPENDENT']


def test_a_same_vendor_plan_review_carries_the_review_wording(pool, monkeypatch, tmp_path):
    from test_native_plan import write
    from graph_backlog.core import WorkGraph
    from graph_backlog.mutations import add_node_to_graph
    from relay import native_plan
    monkeypatch.setattr(native_routes, 'credential_available', only('ANTHROPIC_API_KEY'))
    project = tmp_path/'project'; project.mkdir(); (project/'a.py').write_text('value = 1\n')
    graph = tmp_path/'work-graph.json'
    write(graph, add_node_to_graph(WorkGraph({'meta': {'schema': 'tare.tools/work-graph/0.5'}, 'sources': {},
                                              'nodes': [], 'edges': []}), 'existing', 'Existing task'))
    target = tmp_path/'plan'

    def run(config, deployment, custody, instruction, **kwargs):
        import hashlib
        if config['role'] == 'planner':
            payload = {'summary': 'Bounded change', 'remaining': [], 'steps': ['Change a.py'], 'criteria': ['Two'],
                'sources': [{'repository': 'project', 'path': 'a.py',
                             'sha256': hashlib.sha256((project/'a.py').read_bytes()).hexdigest()}],
                'tasks': [{'key': 'change', 'title': 'Change value', 'summary': 'Set value to two', 'cluster': 'core',
                           'priority': 'P1', 'criteria': ['Value equals two'], 'target_paths': ['a.py'],
                           'depends_on': ['existing']}]}
        else:
            payload = {'summary': 'Adequate', 'remaining': [], 'verdict': 'ACCEPT', 'findings': [], 'sources': [
                {'repository': 'proposal', 'path': name, 'sha256': hashlib.sha256((target/'input'/name).read_bytes()).hexdigest()}
                for name in ('proposal.json', 'request.json')]}
        write(custody/'artifact.json', {'schema': 'tare.tools/native-artifact/1', 'role': config['role'],
            'kind': 'plan' if config['role'] == 'planner' else 'review', 'authority': 'NONE', 'session_id': 'fixture',
            'call_id': 'call', 'profile_digest': 'a'*64, 'payload': payload})
        return {'status': 'PROPOSED', 'artifact_file': str(custody/'artifact.json')}
    monkeypatch.setattr('relay.native_agent.run_session', run)
    result = native_plan.generate(project, graph, target, 'Change it', native_catalog.select(FABLE))
    route = result['routes']['plan-auditor']
    assert route['deployment'] == 'claude-api-opus-5-5' and route['independence'] == SAME_VENDOR_REVIEW
    assert result['review']['independence'] == f'Separate session; {SAME_VENDOR_REVIEW}.'
    assert f'claude-api-opus-5-5 · {route["model"]} · {route["reason"]} · {SAME_VENDOR_REVIEW}' in native_plan.render(result)
    assert f'Reviewer ({SAME_VENDOR_REVIEW}): Adequate' in native_plan.render_summary(result)


def admission_train(tmp_path, cross=False, saved=None, mode='required'):
    """A train with a passed audit's receipts and its saved verdict; `saved` edits the saved payload."""
    import hashlib
    from test_landing_policy import evidence, identity as attested
    from relay.landing_policy import LandingPolicy, evaluate_admission, write_evidence, write_identity
    from relay.train_execution import canonical_json_bytes
    train = tmp_path/'TRAIN-SMART-ONE'; train.mkdir()
    impl, auditor = ((attested('implementer', 'gpt-5.6-sol', 'codex', 'gpt-5.6-sol'),
                      attested('auditor', 'gemini-3.7', 'google-cli', 'gemini-3.7-flash-high')) if cross else
                     (attested('implementer', 'claude-opus-4.8', 'claude', 'claude-opus-4-8'),
                      attested('auditor', 'fable-5', 'claude', 'claude-fable-5')))
    report = train/'AUDIT_REPORT.md'; report.write_text('PASS\n', encoding='utf-8')
    verdict = evaluate_admission(evidence(), impl, policy=LandingPolicy(mode, True), auditor=auditor,
                                 audit_passed=True, audit_sha256=hashlib.sha256(report.read_bytes()).hexdigest())
    write_identity(train/'IMPLEMENTER_IDENTITY.json', impl); write_identity(train/'AUDITOR_IDENTITY.json', auditor)
    write_evidence(train/'LANDING_EVIDENCE.json', evidence())
    payload = json.loads(verdict.canonical_bytes)
    if saved:
        saved(payload)
    (train/'ADMISSION_VERDICT.json').write_bytes(canonical_json_bytes(payload))
    return train, verdict


def land(train):
    """The landing gate's recompute (relay_mesh.admission_valid calls exactly this)."""
    from test_landing_policy import ROSTER
    from relay.landing_policy import verify_admission_bundle
    return verify_admission_bundle(train, roster_path=ROSTER, train_id=train.name, descriptor_sha256='1'*64,
                                   candidate_sha256='2'*64, audit_passed_validator=lambda: True)


def resume(train, monkeypatch, mode='required'):
    """A resumed Work prepares its admission verdict again over the saved one."""
    from relay import relay_mesh
    monkeypatch.setattr(relay_mesh, 'TRAINS_DIR', train.parent)
    monkeypatch.setattr(relay_mesh, 'audit_valid', lambda *a: (True, 'fixture'))
    return relay_mesh.prepare_admission_verdict(train.name, audit_mode=mode)


def test_a_same_vendor_verdict_saved_before_the_change_still_resumes_and_lands(tmp_path, monkeypatch):
    train, verdict = admission_train(tmp_path, saved=lambda p: p['audit'].update(independent=True))
    saved = (train/'ADMISSION_VERDICT.json').read_bytes()
    assert verdict.payload['audit']['independent'] is False and b'"independent":true' in saved
    assert resume(train, monkeypatch).sha256 == verdict.sha256
    assert land(train).sha256 == verdict.sha256 and (train/'ADMISSION_VERDICT.json').read_bytes() == saved


@pytest.mark.parametrize('cross,mode,saved', [
    (True, 'required', lambda p: p['audit'].update(independent=False)),  # the other direction, for another vendor
    (False, 'off', lambda p: p['audit'].update(independent=True)),  # an audit that did not count
    (False, 'required', lambda p: p['audit'].update(independent=True, cross_vendor=True)),
    (False, 'required', lambda p: p['audit'].update(independent=True, counted=False)),
    (False, 'required', lambda p: (p['audit'].update(independent=True), p.update(warnings=[]))),
    (False, 'required', lambda p: p['audit'].update(independent=True, sha256='0'*64))])
def test_no_other_saved_verdict_shape_is_accepted(tmp_path, monkeypatch, cross, mode, saved):
    from relay.landing_policy import LandingPolicyRejected
    train, _ = admission_train(tmp_path, cross=cross, saved=saved, mode=mode)
    with pytest.raises(LandingPolicyRejected, match='VERDICT_RECOMPUTE_MISMATCH'):
        land(train)
    with pytest.raises(LandingPolicyRejected, match='VERDICT_CREATE_ONLY_CONFLICT'):
        resume(train, monkeypatch, mode)


def test_a_new_same_vendor_verdict_round_trips(tmp_path, monkeypatch):
    train, verdict = admission_train(tmp_path)
    assert verdict.verdict == 'ADMITTED' and verdict.payload['warnings'] == ['CROSS_VENDOR_AUDITOR_PREFERRED']
    assert land(train).sha256 == verdict.sha256 == resume(train, monkeypatch).sha256


def test_a_work_run_shows_a_same_vendor_audit_that_counted():
    from relay.work_console import render
    shown = render('run', {'work_id': 'one', 'status': 'SETTLED', 'stage': 'landing', 'audit': {
        'deployment': 'claude-api-opus-5-5', 'session': 's', 'result': {'status': 'PROPOSED'}, 'verdict': 'ACCEPT',
        'independent': False, 'cross_vendor': False},
        'admission': {'verdict': 'ADMITTED', 'reason_codes': [], 'audit': {'counted': True, 'cross_vendor': False}}})
    assert f'Auditor: claude-api-opus-5-5 · ACCEPT · {SAME_VENDOR_AUDIT}' in shown
    assert f'Audit counted: True · {SAME_VENDOR_AUDIT}' in shown and 'Independent audit counted' not in shown


def test_a_remapped_owner_list_keeps_its_same_vendor_route_without_another_vendor(pool, monkeypatch):
    install(pool, 'claude-fable-5-1-cli', 'cli://claude', 'claude-fable-5-1')
    install(pool, 'claude-opus-5-5-cli', 'cli://claude', 'claude-opus-5-5')
    monkeypatch.setattr(native_routes, 'credential_available', only())  # no route of another vendor
    monkeypatch.setattr(turn_engine, 'available', lambda name, **kw: (native_catalog.select(name), None))
    monkeypatch.setenv('TARE_SESSION_SCOPE', json.dumps({'prefer': 'subscription'}))  # sets the owner's API route aside
    sonnet = native_catalog.select('claude-api-sonnet-5-5')
    assert native_routes.reviewers(sonnet)[0][0]['id'] == 'claude-opus-5-5-cli'  # what the automatic choice would take
    assert [name for name, _ in turn_engine.pick_auditors(sonnet, ['openai-api', 'claude-fable-5-1-cli'])] == [
        'claude-fable-5-1-cli']  # as on origin/main


def same_vendor_review(job):
    """Record in a review's request, as the host does, a Fable implementer and an Opus auditor; its session path."""
    from relay.conversation_review import execution_selection
    path = Path(job)/'input'/'request.json'
    data = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    data['host_execution_selection'] = execution_selection(native_catalog.select(FABLE),
                                                           native_catalog.select('claude-api-opus-5-5'))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    return str(Path(job)/'session')


def claims_independence(value):
    """Whether any text in `value` calls something independent, other than the label's own "not independent"."""
    return 'independent' in json.dumps(value).lower().replace('not independent', '')


@pytest.mark.parametrize('reason,audit', [('AUDIT_INCOMPLETE', {'verdict': 'INCONCLUSIVE', 'reason': 'DEADLINE'}),
                                          ('AUDIT_REFUSED', {'verdict': 'REVISE', 'summary': 'Off by one'})])
def test_a_saved_same_vendor_delivery_keeps_its_label(tmp_path, reason, audit):
    from relay import conversation_tasks, work_units
    session = same_vendor_review(tmp_path/'reviews'/'job')
    result = {'verdict': 'NOT_REVIEWED', 'reason': getattr(work_units, reason),
              'audit': {**audit, 'session': session, 'independence': SAME_VENDOR_AUDIT}}
    shown = conversation_tasks.delivery_view(conversation_tasks.retained_delivery(result))
    assert shown['audit']['independence'] == SAME_VENDOR_AUDIT and SAME_VENDOR_AUDIT in shown['reason']
    assert not claims_independence(shown), shown
    assert result['reason'] == getattr(work_units, reason)  # the saved reason is still the refusal counter's key


def test_a_resumed_same_vendor_review_keeps_its_label(reviewed, monkeypatch):
    import test_conversation_review_recovery as fixtures
    from compute_plane.native_models import NativeHttpClient, NativeTransportInterrupted
    from relay import conversation_review, native_agent
    from test_conversation_tasks import user_reply
    from test_native_agent import Client
    from test_saved_audit_resume import RUN_SESSION
    owner_host, run, *_ = reviewed
    owner, control, tree, *_ = owner_host
    clients = []

    class Interrupted(Client, NativeHttpClient):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.interrupted = False
            clients.append(self)

        def post_observed(self, path, body, execution):
            if path.endswith('/chat/completions') and self.generations == 2 and not self.interrupted:
                self.interrupted = True
                raise NativeTransportInterrupted('HTTP_INCOMPLETE_STREAM', {'started': True, 'cleanup_verified': True,
                    'remote_stop': 'unknown', 'http_attempts_upper_bound': 1})
            return self.post(path, body, execution), {'started': True, 'cleanup_verified': True}
    monkeypatch.setattr(fixtures, 'Client', Interrupted)
    run.task['attempt_input'] = 'prior-worker-input'
    user_reply(owner_host, 'Review this retained candidate without another worker.')
    first = run('ACCEPT')
    assert first['reason'] == 'UNFINISHED_INVOCATION'
    job = Path(first['session']).parent
    same_vendor_review(job)  # a review interrupted before this change: its request names both identities
    previous = json.loads((job/'receipt.json').read_text())
    run.task['delivery'] = {'audit': first}
    run.task['attempt_input'] = owner.input_id()
    user_reply(owner_host, 'Continue the saved audit only.')
    owner.invocation_id = 'explicit-audit-resume'
    owner.state['limits']['max_model_calls'] = 30
    monkeypatch.setattr(native_agent, 'run_session', lambda config, dep, session, **kw: RUN_SESSION(
        config, dep, session, client=clients[0], local_counter=clients[0], **kw))
    result = conversation_review.resume_review(owner, run.task, job, previous, tree, run.task['plan']['unit'],
                                               {'a.py': b'value = 2\n'}, control)
    assert result['verdict'] == 'ACCEPT' and result['independence'] == SAME_VENDOR_AUDIT
    assert json.loads((job/'receipt.json').read_text())['result']['independence'] == SAME_VENDOR_AUDIT
    assert not claims_independence({k: result[k] for k in ('verdict', 'independence')})


def test_a_same_vendor_accept_saved_before_the_change_is_labelled_when_reused(reviewed):
    _, run, *_ = reviewed
    first = run('ACCEPT')
    assert 'independence' not in first  # the fixture's identities are unknown
    same_vendor_review(Path(first['session']).parent)  # as a review saved before the label: identities, no label
    again = run('ACCEPT')  # reused from its receipt: no second auditor call (run checks it)
    assert again['verdict'] == 'ACCEPT' and again['independence'] == SAME_VENDOR_AUDIT
    assert {k: v for k, v in again.items() if k != 'independence'} == first


def test_the_correction_brief_names_a_same_vendor_review():
    from compute_plane.native_models import encode
    from relay.work_units import brief
    unit = {'title': 'Fix', 'target_paths': ['a.py'], 'criteria': ['Two'],
            'review_feedback': encode({'verdict': 'REVISE', 'findings': [], 'independence': SAME_VENDOR_AUDIT})}
    shown = brief(unit, ['tests'], '')
    assert f'Previous review ({SAME_VENDOR_AUDIT}) of this retained candidate' in shown
    assert 'Previous independent review' not in shown
    unit['review_feedback'] = encode({'verdict': 'REVISE', 'findings': []})
    assert 'Previous independent review of this retained candidate' in brief(unit, ['tests'], '')


def test_a_work_saved_before_the_rule_reads_its_same_vendor_route_as_such():
    from relay.work_console import render
    routing = {'status': 'READY', 'reason': 'AUTO_CROSS_VENDOR_THEN_COST_AND_CONFIGURED_ORDER', 'routes': [
        {'deployment': {'id': 'claude-api-opus-5-5'}, 'reason': 'AUTO_SELECTED_INDEPENDENT', 'cross_vendor': False},
        {'deployment': {'id': 'openai-api'}, 'reason': 'AUTO_SELECTED_INDEPENDENT', 'cross_vendor': True}],
        'selection': {'candidates': [{'deployment': 'claude-api-opus-5-5', 'cross_vendor': False,
                                      'reason': 'CONFIGURED_INDEPENDENT_NATIVE_ROUTE'}]}}
    shown = render('run', {'work_id': 'one', 'status': 'RUNNING', 'stage': 'code_audit',
                           'audit_routing': routing}).splitlines()
    assert f'  claude-api-opus-5-5: SAME_VENDOR_NOT_INDEPENDENT · {SAME_VENDOR_AUDIT}' in shown
    assert f'  Candidate claude-api-opus-5-5: SAME_VENDOR_NOT_INDEPENDENT · {SAME_VENDOR_AUDIT}' in shown
    assert '  openai-api: AUTO_SELECTED_INDEPENDENT' in shown  # another vendor: unchanged
    assert work_audit.route_reason({'reason': 'OPERATOR_SELECTED_INDEPENDENT', 'cross_vendor': False}) == (
        'SAME_VENDOR_NOT_INDEPENDENT')  # what a resumed Work's code_audit stage re-emits
