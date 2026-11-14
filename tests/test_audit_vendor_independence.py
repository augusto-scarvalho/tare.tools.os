"""An independent auditor is one of another vendor; a same-vendor audit runs and counts, labelled (owner, 2026-11-14)."""
import json

import pytest

from relay import native_catalog, native_routes, turn_engine, work_audit
from relay.landing_policy import SAME_VENDOR_AUDIT, SAME_VENDOR_REVIEW, independence
from test_cross_vendor_auditors import install, pool  # noqa: F401  (pool is a fixture)

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


def test_a_work_run_shows_a_same_vendor_audit_that_counted():
    from relay.work_console import render
    shown = render('run', {'work_id': 'one', 'status': 'SETTLED', 'stage': 'landing', 'audit': {
        'deployment': 'claude-api-opus-5-5', 'session': 's', 'result': {'status': 'PROPOSED'}, 'verdict': 'ACCEPT',
        'independent': False, 'cross_vendor': False},
        'admission': {'verdict': 'ADMITTED', 'reason_codes': [], 'audit': {'counted': True, 'cross_vendor': False}}})
    assert f'Auditor: claude-api-opus-5-5 · ACCEPT · {SAME_VENDOR_AUDIT}' in shown
    assert f'Audit counted: True · {SAME_VENDOR_AUDIT}' in shown and 'Independent audit counted' not in shown
