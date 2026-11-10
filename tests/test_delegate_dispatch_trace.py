"""Offline checks for compact executed-attempt records."""
from relay.native_routes import executed_attempt


def test_attempt_records_reasoning_sent_in_deployment():
    deployment = {'model': 'm1', 'protocol': 'cli', 'native_model_choice': {'connection': 'route-a'},
                  'parameters_json': {'chat_template_kwargs': {'reasoning_effort': 'xhigh'}}}
    record = executed_attempt(deployment, {'status': 'FAILED', 'reason': 'x', 'failure_code': 'E'})
    assert record['deployment'] == 'route-a'
    assert record['model'] == 'm1'
    assert record['reasoning'] == 'xhigh'
    assert record['status'] == 'FAILED'
    assert record['failure_code'] == 'E'


def test_attempt_missing_reasoning_is_unknown_and_compact():
    record = executed_attempt({'model': 'm2', 'api_key': 'secret', 'prompt': 'p'})
    assert record['reasoning'] == 'UNKNOWN'
    assert record['status'] == 'UNKNOWN'
    assert 'secret' not in repr(record) and 'prompt' not in record


import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from relay import native_agent, native_routes, turn_engine
from relay.native_routes import settle_attempt

FILES = [{'path': 'a.py', 'diff': '+x\n'}]


def auditor(name, effort):
    return {'id': name, 'model': name+'-model', 'protocol': 'vendor-cli',
            'native_profile': {'model': name+'-runtime', 'parameters_json': json.dumps({'reasoning_effort': effort})}}


def effort_applied(deployment, effort):
    return {**deployment, 'native_profile': {**deployment['native_profile'],
                                             'parameters_json': json.dumps({'reasoning_effort': effort})}}


@pytest.fixture
def audit_env(tmp_path, monkeypatch):
    monkeypatch.setattr(native_agent, 'session_root', lambda: tmp_path/'sessions')
    monkeypatch.setattr(turn_engine, 'policy', lambda: {'auditor_selection': 'cross_vendor_frontier', 'auditor_effort': 'high'})
    monkeypatch.setattr(turn_engine, 'with_effort', effort_applied)
    monkeypatch.setattr(turn_engine, 'note_usage_limit', lambda deployment, *args, **kwargs: None)
    monkeypatch.setattr('relay.native_artifacts.read_input', lambda path: {'artifact': {'payload': {
        'verdict': 'ACCEPT', 'summary': 'Fixture review', 'findings': [], 'remaining': []}}})
    return tmp_path


def test_audit_keeps_every_executed_attempt_and_known_skip(audit_env, monkeypatch):
    routes = [('limited', auditor('limited', 'low')), ('first', auditor('first', 'low')), ('second', auditor('second', 'low'))]
    monkeypatch.setattr(turn_engine, 'pick_auditors', lambda producer, names: routes)
    monkeypatch.setattr(turn_engine, 'usage_limited', lambda deployment: deployment['id'] == 'limited')
    sent = []
    def run(config, deployment, custody, instruction, **kwargs):
        sent.append(deployment['id'])
        if len(sent) == 1:
            return {'status': 'BLOCKED', 'failure_code': 'CLI_USAGE_LIMIT'}
        return {'status': 'PROPOSED', 'artifact_file': 'fixture.json'}
    monkeypatch.setattr(native_agent, 'run_session', run)
    record = turn_engine.run_audit('Change a.py', FILES, audit_env, {'id': 'author'}, lambda text: None)
    assert record['verdict'] == 'ACCEPT' and sent == ['first', 'second']
    assert [(a['deployment'], a['status'], a['reasoning']) for a in record['attempts']] == [
        ('first', 'BLOCKED', 'high'), ('second', 'PROPOSED', 'high')]
    decision = record['executed_decision']
    assert decision['mode'] == 'AUTOMATIC' and decision['propensity'] == {'first': 0, 'second': 1}
    assert decision['fallback']['first_ranked'] == 'limited'
    assert decision['fallback']['skipped'] == [{'deployment': 'limited', 'reason': 'USAGE_LIMITED'}]
    assert 'Change a.py' not in json.dumps(record['attempts'])


def test_audit_pin_records_pinned_reasoning_and_interrupted_attempts(audit_env, monkeypatch):
    monkeypatch.setattr('relay.execution_scope.current',
                        lambda: SimpleNamespace(pin=lambda role: role == 'auditor', changes=lambda role: False,
                                                unlimited=()))
    monkeypatch.setattr('relay.execution_scope.require', lambda deployment, role: None)
    monkeypatch.setattr(turn_engine, 'pick_auditors',
                        lambda producer, names: [('pinned', auditor('pinned', 'xhigh')), ('next', auditor('next', 'low'))])
    monkeypatch.setattr(turn_engine, 'usage_limited', lambda deployment: False)
    monkeypatch.setattr(native_agent, 'run_session', lambda *a, **kw: {'status': 'INTERRUPTED', 'reason': 'cancelled'})
    record = turn_engine.run_audit('Change a.py', FILES, audit_env, {'id': 'author'}, lambda text: None)
    assert 'verdict' not in record
    assert [(a['deployment'], a['status'], a['reasoning']) for a in record['attempts']] == [
        ('pinned', 'INTERRUPTED', 'xhigh'), ('next', 'INTERRUPTED', 'low')]
    assert record['executed_decision']['mode'] == 'EXACT_TEMPORARY_PIN'


def test_audit_without_candidates_records_no_decision(audit_env, monkeypatch):
    monkeypatch.setattr(turn_engine, 'pick_auditors', lambda producer, names: [])
    monkeypatch.setattr(native_agent, 'run_session', lambda *a, **kw: pytest.fail('dispatch without candidates'))
    assert turn_engine.run_audit('Change a.py', FILES, audit_env, {'id': 'author'}, lambda text: None) is None


def helper_cli(monkeypatch, answer):
    dep = {'id': 'cli', 'model': 'm', 'native_cli': {'executable': 'codex'},
           'native_profile': {'model': 'm-runtime', 'parameters_json': json.dumps({'reasoning_effort': 'medium'})}}
    monkeypatch.setattr(turn_engine, 'search_links', lambda text: [])
    monkeypatch.setattr(turn_engine, 'HELPER_ROUTES', ('gone', 'cli'))
    monkeypatch.setattr(turn_engine, 'available', lambda name: (None, 'offline') if name == 'gone' else (dep, None))
    monkeypatch.setattr(turn_engine, 'codex_isolation', lambda effort: [])
    monkeypatch.setattr(turn_engine, 'route_effort', lambda deployment: 'medium')
    def run(argv, **kwargs):
        if answer:
            Path(argv[argv.index('-o') + 1]).write_text(answer, encoding='utf-8')
    monkeypatch.setattr(turn_engine.subprocess, 'run', run)


def test_research_cli_records_visited_routes_without_prompt_or_answer(monkeypatch):
    helper_cli(monkeypatch, 'Answer text.')
    record, answer = turn_engine.run_research('What changed?', lambda text: None)
    assert answer == 'Answer text.' and record['route'] == 'cli' and record['mode'] == 'web-search'
    decision = record['executed_decision']
    assert decision['propensity'] == {'cli': 1}
    assert decision['fallback']['skipped'] == [{'deployment': 'gone', 'reason': 'UNAVAILABLE'}]
    assert [(a['status'], a['reasoning']) for a in record['attempts']] == [('COMPLETED', 'medium')]
    assert 'What changed?' not in json.dumps(record) and 'Answer text.' not in json.dumps(record)


def test_research_cli_failure_keeps_the_attempt(monkeypatch):
    helper_cli(monkeypatch, '')
    record, answer = turn_engine.run_research('What changed?', lambda text: None)
    assert 'not available' in answer and record['status'] == 'FAILED'
    assert [(a['status'], a['failure_code']) for a in record['attempts']] == [('FAILED', 'RuntimeError')]
    assert 'What changed?' not in json.dumps(record)


def test_research_legacy_helper_without_trace_keeps_answer_and_unknown_provenance(monkeypatch):
    monkeypatch.setattr(turn_engine, 'search_links', lambda text: [])
    calls = []
    def legacy(prompt, **kwargs):
        calls.append((prompt, kwargs))
        return 'Unchanged legacy answer.', 'legacy-cli'
    monkeypatch.setattr(turn_engine, 'ask_cli', legacy)
    record, answer = turn_engine.run_research('What changed?', lambda text: None)
    assert answer == 'Unchanged legacy answer.'
    assert record == {'flow': 'research', 'route': 'legacy-cli', 'mode': 'web-search'}
    assert 'executed_decision' not in record and 'attempts' not in record
    assert len(calls) == 1 and calls[0][1]['search'] is True
    assert calls[0][1]['trace'] == {}


def test_research_legacy_helper_failure_before_dispatch_has_no_provenance(monkeypatch):
    monkeypatch.setattr(turn_engine, 'search_links', lambda text: [])
    def legacy(prompt, **kwargs):
        raise RuntimeError('legacy unavailable')
    monkeypatch.setattr(turn_engine, 'ask_cli', legacy)
    assert turn_engine.run_research('What changed?', lambda text: None) == (
        None, 'tare: web search is not available: legacy unavailable')


@pytest.mark.parametrize('trace', [None, [], {}, {'route': 'legacy'},
    {'visited': [], 'skipped': [], 'deployment': {}},
    {'route': 'legacy', 'skipped': [], 'deployment': {}},
    {'route': 'legacy', 'visited': [], 'deployment': {}},
    {'route': 'legacy', 'visited': [], 'skipped': []}])
def test_helper_dispatch_incomplete_trace_does_not_invent_provenance(trace):
    assert turn_engine.helper_dispatch(trace, {'status': 'COMPLETED'}) == {}


def test_research_without_any_route_records_no_decision(monkeypatch):
    helper_cli(monkeypatch, 'unused')
    monkeypatch.setattr(turn_engine, 'available', lambda name: (None, 'offline'))
    record, answer = turn_engine.run_research('What changed?', lambda text: None)
    assert record is None and 'not available' in answer


def test_implementer_selection_records_only_visited_tiers():
    dep = {'id': 'cheap-a', 'model': 'm', 'native_auto_selection': {
        'tier': 1, 'route': 'cheap-a', 'skipped': [{'route': 'local-a', 'reason': 'offline', 'diagnostic': {'message': 'x'}}]}}
    decision = native_routes.selected_decision(dep)
    assert decision['mode'] == 'AUTOMATIC' and decision['propensity'] == {'cheap-a': 1}
    assert decision['fallback']['skipped'] == [{'deployment': 'local-a', 'reason': 'offline'}]
    pin = native_routes.selected_decision({'id': 'pinned'}, pinned=True)
    assert pin['mode'] == 'EXACT_TEMPORARY_PIN' and pin['propensity'] == {'pinned': 1}
    assert native_routes.selected_decision({'id': 'fixed'})['mode'] == 'CONFIGURED'


def test_settled_attempt_never_rewrites_earlier_attempts():
    attempts = [executed_attempt({'id': 'a'}, {'status': 'FAILED'}), executed_attempt({'id': 'b'}, {'status': 'DISPATCHING'})]
    settle_attempt(attempts[-1], {'status': 'PROPOSED', 'reason': 'ok', 'prompt': 'secret'})
    assert [a['status'] for a in attempts] == ['FAILED', 'PROPOSED'] and 'secret' not in repr(attempts)


from contextlib import nullcontext
from copy import deepcopy

from relay import conversation_review, work_units
from relay import execution_scope as scope
from test_implement_unit import repository
from test_native_agent import setup, deployment  # noqa: F401
from test_role_pin_dispatch import pin_catalog  # noqa: F401
from test_role_pins import parse
from test_conversation_tasks import host, intake, planned  # noqa: F401


@pytest.mark.parametrize('stage', ['input', 'diff.patch', 'views', 'request.json', 'config'])
def test_audit_preparation_persists_terminal_intent_without_dispatch(host, monkeypatch, stage):
    tools, control, root, _ = host
    intake(host); task = planned(host)
    repository(root)
    changed = b'value = 2\n'; (root/'a.py').write_bytes(changed)
    producer = deployment()
    monkeypatch.setattr(turn_engine, 'pick_auditors', lambda *a, **kw: [('independent', deployment())])
    monkeypatch.setattr(native_agent, 'run_session', lambda *a, **kw: pytest.fail('preparation dispatched'))
    from relay import review_diff, folder_access
    original_prepare = review_diff.prepare
    def prepare(*args):
        diff, projections, views, raw_hash, failure = original_prepare(*args)
        return diff, projections, {**views, 'view.json': b'{}'}, raw_hash, failure
    monkeypatch.setattr(review_diff, 'prepare', prepare)
    original_mkdir, original_bytes, original_text = Path.mkdir, Path.write_bytes, Path.write_text
    intents = []
    def fail():
        receipt = next((tools.root/'reviews').glob('*/receipt.json'))
        row = json.loads(receipt.read_text(encoding='utf-8'))
        assert row['status'] == 'RUNNING' and len(row['request_sha256']) == 64
        assert row['invocation'] == tools.invocation_id and 'calls_reserved' in row
        assert 'amount_committed' in row
        assert not row.get('attempts') and not row.get('executed_decision')
        intents.append(row)
        raise OSError('offline preparation failure: '+stage)
    def mkdir(path, *args, **kwargs):
        if stage == 'input' and path.name == 'input' and 'reviews' in path.parts:
            fail()
        return original_mkdir(path, *args, **kwargs)
    def write_bytes(path, *args, **kwargs):
        if 'reviews' in path.parts and path.name == {'diff.patch': 'diff.patch', 'views': 'view.json'}.get(stage):
            fail()
        return original_bytes(path, *args, **kwargs)
    def write_text(path, *args, **kwargs):
        if stage == 'request.json' and path.name == 'request.json' and 'reviews' in path.parts:
            fail()
        return original_text(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'mkdir', mkdir)
    monkeypatch.setattr(Path, 'write_bytes', write_bytes)
    monkeypatch.setattr(Path, 'write_text', write_text)
    if stage == 'config':
        tools.state['config']['folder_access'] = {'grants': str(root/'unused-grants.json')}
        monkeypatch.setattr(folder_access, 'granted', lambda *a: fail())
    args = (tools, task, root, producer, task['plan']['unit'], {'a.py': changed}, {'after': 'green'}, control)
    with pytest.raises(OSError, match='offline preparation failure'):
        conversation_review.review(*args)
    receipt = next((tools.root/'reviews').glob('*/receipt.json'))
    row = json.loads(receipt.read_text(encoding='utf-8'))
    assert len(intents) == 1 and row['status'] == 'FINISHED'
    assert row['request_sha256'] == intents[0]['request_sha256']
    assert row['result']['verdict'] == 'INCONCLUSIVE'
    assert row['result']['failure_code'] == 'PREPARATION_FAILED'
    assert row['calls_used'] == 0 and row['amount_committed'] == '0'
    assert not row.get('attempts') and not row.get('executed_decision')
    assert not (receipt.parent/'session').exists()
    assert conversation_review.review(*args) == row['result']
    assert len(intents) == 1 and len(list((tools.root/'reviews').glob('*/receipt.json'))) == 1


def unit_env(setup, tmp_path, monkeypatch, actor):
    config, custody, root = setup
    repository(root)
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'storage'))
    monkeypatch.setattr(turn_engine, 'policy', lambda: {'implementer': [['child-route']]})
    monkeypatch.setattr(turn_engine, 'available', lambda name: (deployment(role='planner'), None))
    monkeypatch.setattr(work_units, 'child_limits', lambda *a, **kw: {'max_model_calls': 4, 'max_seconds': 30})
    monkeypatch.setattr(work_units, 'run_checks', lambda *a: ('green', 'Observed baseline.'))
    monkeypatch.setattr(work_units, 'local_slot', lambda value: nullcontext(), raising=False)
    monkeypatch.setattr(native_agent, 'run_session', actor)
    monkeypatch.setattr(work_units, 'run_session', actor, raising=False)
    state = {'config': config, 'conversation_flow': True, 'deployment': deployment(),
             'limits': {'max_model_calls': 30}, 'deadline_utc': None}
    args = {'key': 'trace', 'title': 'Change value', 'repository': 'repo', 'target_paths': ['a.py'],
            'criteria': ['value is two'], 'test_paths': [], 'verifiers': ['unit'], 'route': 'child-route'}
    return custody, state, args


def test_implementer_interrupted_rung_keeps_unknown_attempt(setup, tmp_path, monkeypatch):
    sent = []
    def actor(config, value, *args, **kwargs):
        sent.append(deepcopy(value))
        raise OSError('worker interrupted')
    custody, state, args = unit_env(setup, tmp_path, monkeypatch, actor)
    result = work_units.run_unit(custody, state, args, lambda: False)
    receipt = json.loads(Path(result['receipt']).read_text(encoding='utf-8'))
    assert len(sent) == 1 and len(receipt['rungs']) == 1
    row = receipt['rungs'][0]
    assert row['route'] == 'child-route' and 'worker interrupted' in row['reason']
    assert row['executed_decision']['mode'] == 'CONFIGURED'
    assert row['executed_decision']['propensity'] == {'child-route': 1}
    assert row['executed_decision']['ope_supported'] is False
    assert [(a['status'], a['reasoning']) for a in row['attempts']] == [('DISPATCHING', 'high')]
    assert 'Change value' not in json.dumps(row['attempts'])


def test_implementer_preflight_refusal_records_no_attempt(setup, tmp_path, monkeypatch):
    custody, state, args = unit_env(setup, tmp_path, monkeypatch, lambda *a, **kw: pytest.fail('dispatched'))
    result = work_units.run_unit(custody, state, args, lambda: False,
                                 review=SimpleNamespace(preflight=lambda value: False))
    assert result['verdict'] == 'NOT_REVIEWED'
    receipt = json.loads(Path(result['receipt']).read_text(encoding='utf-8'))
    assert receipt['rungs'] == [] and 'executed_decision' not in json.dumps(receipt)


def test_conversation_review_records_pinned_auditor_attempt(host, pin_catalog, monkeypatch):
    tools, control, root, _ = host
    intake(host); task = planned(host)
    repository(root)
    changed = b'value = 2\n'; (root/'a.py').write_bytes(changed)
    producer = deepcopy(pin_catalog['producer-cli'])
    tools.state['deployment'] = producer
    seen = []
    def run(config, value, session, instruction, **kwargs):
        seen.append(deepcopy(value))
        saved = json.loads((session.parent/'receipt.json').read_text(encoding='utf-8'))
        assert len(saved['attempts']) == 1 and saved['attempts'][0]['status'] == 'DISPATCHING'
        assert saved['executed_decision']['mode'] == 'EXACT_TEMPORARY_PIN'
        assert saved['request_sha256'] and (Path(config['repositories']['review'])/'request.json').is_file()
        session.mkdir(parents=True)
        (session/'native-agent.json').write_text('{}')
        return {'status': 'BLOCKED', 'reason': 'Offline boundary', 'failure_code': 'FIXTURE_FAILURE'}
    monkeypatch.setattr(native_agent, 'run_session', run)
    with scope.activated(parse(['--auditor', 'anthropic/test-cli@max'])):
        conversation_review.review(tools, task, root, producer, task['plan']['unit'],
                                   {'a.py': changed}, {'after': 'green'}, control)
    row = json.loads(next((tools.root/'reviews').glob('*/receipt.json')).read_text(encoding='utf-8'))
    decision = row['executed_decision']
    assert len(seen) == 1 and decision['mode'] == 'EXACT_TEMPORARY_PIN'
    assert decision['propensity'] == {row['route']: 1} and decision['ope_supported'] is False
    sent = native_routes._reasoning_of(seen[0]['native_profile'].get('parameters_json'))
    assert [(a['status'], a['reasoning'], a.get('failure_code')) for a in row['attempts']] == [
        ('BLOCKED', sent, 'FIXTURE_FAILURE')]
    assert 'Independently review' not in json.dumps(row['attempts'])


@pytest.mark.parametrize('interrupted', [False, True])
def test_explorer_recovery_persists_each_dispatch(tmp_path, monkeypatch, interrupted):
    from relay import delegation, delegation_tools
    tools = object.__new__(delegation_tools.ExplorationTools)
    tools.settings = {'seconds': 30}
    tools.state = {'deadline_utc': None, 'config': {}}
    tools.route_class = None
    tools._dispatch_modes = {'explorer': 'CONFIGURED'}
    monkeypatch.setattr(delegation, 'experiments', lambda route: {'leaf_project_instructions': False})
    folder = tmp_path/'explorer'; folder.mkdir()
    row = {'repository': 'repo', 'repository_root': str(tmp_path), 'source_path': '.',
           'question': 'PRIVATE QUESTION', 'calls_reserved': 4, 'status': 'RUNNING'}
    value = deployment()
    value['native_profile']['parameters_json'] = json.dumps({'reasoning_effort': 'xhigh'})
    original = deepcopy(value)
    seen = []
    first = {'status': 'BLOCKED', 'reason': 'Lost response', 'failure_code': 'OFFLINE_LOSS',
             'model_recovery': {'available': True}}
    def run(config, sent, session, *args, **kwargs):
        saved = json.loads((folder/'receipt.json').read_text(encoding='utf-8'))
        seen.append((deepcopy(sent), kwargs))
        assert saved['attempts'][-1]['status'] == 'DISPATCHING'
        assert saved['executed_decision']['mode'] == 'CONFIGURED'
        if len(seen) == 1:
            assert sent == original and len(saved['attempts']) == 1
            session.mkdir()
            (session/'native-agent.json').write_text('{}')
            (session/'outcome.json').write_text(json.dumps(first))
            return first
        assert config is None and sent is None and kwargs['resume'] and kwargs['resume_interrupted_model']
        assert len(saved['attempts']) == 2 and saved['attempts'][1]['resume'] is True
        assert saved['attempts'][0]['status'] == 'BLOCKED'
        assert saved['attempts'][0]['failure_code'] == 'OFFLINE_LOSS'
        if interrupted:
            raise OSError('resume interrupted')
        outcome = {'status': 'CANCELLED', 'reason': 'Offline cancellation'}
        (session/'outcome.json').write_text(json.dumps(outcome))
        return outcome
    monkeypatch.setattr(native_agent, 'run_session', run)
    tools.investigate(folder, row, value, {'identity': {}},
                      {'others': [], 'part': None, 'map': None}, SimpleNamespace(should_stop=lambda: False))
    saved = json.loads((folder/'receipt.json').read_text(encoding='utf-8'))
    assert len(seen) == 2 and value == original
    assert [a['status'] for a in saved['attempts']] == ['BLOCKED', 'DISPATCHING' if interrupted else 'CANCELLED']
    assert [a['reasoning'] for a in saved['attempts']] == ['xhigh', 'xhigh']
    assert saved['status'] == 'INCOMPLETE'
    assert 'PRIVATE QUESTION' not in json.dumps(saved['attempts'])
    assert saved['attempts'][0].get('resume') is None
    if interrupted:
        assert 'reason' not in saved['attempts'][1]


@pytest.mark.parametrize('active_pin', [False, True])
@pytest.mark.parametrize('refusal', ['calls', 'time', None])
def test_waiting_worker_funding_precedes_executed_record(setup, tmp_path, monkeypatch, refusal, active_pin):
    from relay.folder_access import grant
    from test_native_agent import Client
    actual_run = native_agent.run_session
    actual_child_limits = work_units.child_limits
    custody, state, args = unit_env(setup, tmp_path, monkeypatch, actual_run)
    if active_pin:
        monkeypatch.setattr(turn_engine, 'available',
                            lambda name: (deployment('bedrock-converse', role='planner'), None))
    from relay.work_forms import record_plan
    custody.mkdir(exist_ok=True)
    unit = {k: v for k, v in args.items() if k != 'route'}
    record_plan(custody, state['config'], {'units': [unit], 'criteria': unit['criteria']})
    args = {'unit': unit['key'], 'route': args['route']}
    external = tmp_path/'reference'; external.mkdir()
    store = tmp_path/'grants.json'
    state['config']['folder_access'] = {'grants': str(store)}
    client = Client(protocol='bedrock-converse' if active_pin else 'openai-chat', steps=[
        ('request_folder_access', {'path': str(external), 'reason': 'Read reference.'}),
        ('tare_deliver', {'summary': 'Waiting for permission.'})])
    monkeypatch.setattr(native_agent, 'inference_client', lambda *a, **kw: client)
    monkeypatch.setattr('compute_plane.native_models.NativeHttpClient', lambda *a, **kw: client)
    audit = lambda *a: {'verdict': 'ACCEPT'}
    first = work_units.run_unit(custody, state, args, lambda: False, review=audit)
    worker = first['worker']
    original_receipt = Path(first['receipt']).read_bytes()
    saved_session = Path(worker['session'])/'native-agent.json'
    original_session = saved_session.read_bytes()
    grant(store, str(external))
    if active_pin:
        from relay.inference_allowlist import identities
        producer = work_units.candidate_producer(custody, worker['producer'])
        connection = (producer.get('native_model_choice') or {}).get('connection', producer['id'])
        provider, _, route = identities(connection, producer)
        raw_pin = provider+'/'+connection+'@'+route['default_effort']
        selected_scope = scope.Scope(pins=(('implementer', raw_pin),)).validate()
        assert scope.pin_matches(raw_pin, producer)
        monkeypatch.setattr(scope, 'current', lambda: selected_scope)
    limits = {'max_model_calls': 1 if refusal == 'calls' else 4,
              'max_seconds': 1 if refusal == 'time' else 30}
    if refusal:
        monkeypatch.setattr(work_units, 'child_limits', actual_child_limits)
        state['limits']['max_model_calls'] = work_units.SESSION_RESERVE + 1 if refusal == 'calls' else None
        state['deadline_utc'] = __import__('time').time() + 61 if refusal == 'time' else None
    else:
        monkeypatch.setattr(work_units, 'child_limits', lambda *a, **kw: limits)
    dispatched = []
    def resume(config, value, session, **kwargs):
        dispatched.append(kwargs)
        assert config is None and value is None and kwargs['resume']
        assert kwargs['invocation_limits'] == limits
        raise OSError('offline resume interrupted')
    monkeypatch.setattr(native_agent, 'run_session', resume)
    result = work_units.run_unit(custody, state, args, lambda: False, review=audit, worker=worker)
    receipt = json.loads(Path(result['receipt']).read_text(encoding='utf-8'))
    assert Path(first['receipt']).read_bytes() == original_receipt
    assert saved_session.read_bytes() == original_session and client.generations == 2
    assert len(receipt['rungs']) == 1
    row = receipt['rungs'][0]
    if refusal:
        assert not dispatched
        assert 'executed_decision' not in row and 'attempts' not in row
        assert 'Insufficient implementer funding' in result['reason']
        assert 'required minimum' in result['reason']
        assert result['worker'] and Path(worker['tree']).is_dir()
    else:
        assert len(dispatched) == 1
        assert row['executed_decision']['mode'] == 'RESUME_SAVED_ROUTE'
        assert len(row['attempts']) == 1 and row['attempts'][0]['resume'] is True
        assert row['attempts'][0]['status'] == 'DISPATCHING'


@pytest.mark.parametrize('explicit_route', [False, True])
@pytest.mark.parametrize('pinned', [False, True])
def test_worker_dispatch_mode_matches_applied_selection(setup, pin_catalog, monkeypatch, explicit_route, pinned):
    config, custody, root = setup
    repository(root)
    monkeypatch.setattr(work_units, 'run_checks', lambda *a: ('green', 'Offline check.'))
    sent = []
    def worker(child, value, session, instruction, **kwargs):
        sent.append(deepcopy(value))
        raise OSError('offline worker interruption')
    monkeypatch.setattr(native_agent, 'run_session', worker)
    state = {'config': config, 'conversation_flow': True, 'deployment': pin_catalog['producer-cli'],
             'limits': {'max_model_calls': 30}, 'deadline_utc': None}
    args = {'title': 'Set two', 'target_paths': ['a.py'], 'criteria': ['two'], 'verifiers': ['unit']}
    if explicit_route:
        args['route'] = 'test-cli' if pinned else 'producer-cli'
    with scope.activated(parse(['--implementer', 'anthropic/test-cli@max'] if pinned else [])):
        with scope.for_role('coordinator'):
            result = work_units.run_unit(custody, state, args, lambda: False)
    row = json.loads(Path(result['receipt']).read_text(encoding='utf-8'))['rungs'][0]
    assert len(sent) == 1 and len(row['attempts']) == 1
    assert row['executed_decision']['mode'] == (
        'EXACT_TEMPORARY_PIN' if pinned else 'CONFIGURED' if explicit_route else 'AUTOMATIC')
    assert row['attempts'][0]['reasoning'] == native_routes._reasoning_of(sent[0]['native_profile']['parameters_json'])
    if pinned:
        assert scope.pin_matches('anthropic/test-cli@max', sent[0])
        assert row['attempts'][0]['reasoning'] == 'max'
        assert 'max' not in pin_catalog['test-cli']['native_profile']['parameters_json']


from test_plan_routing import selection  # noqa: F401
from test_native_plan import proposal  # noqa: F401
from test_work_resume import host as work_host  # noqa: F401
from test_work_run import os_root  # noqa: F401


def native_selection_record(executed='chosen'):
    return {'reason': 'AUTO_COST_AND_CONFIGURED_ORDER', 'candidates': [
        {'deployment': 'second-row', 'selected_deployment': 'second', 'eligible': True,
         'position': 2, 'facts': {'private': 'SECRET'}},
        {'deployment': 'excluded', 'eligible': False, 'reason': 'QUOTA_EXHAUSTED', 'diagnostic': 'SECRET'},
        {'deployment': 'first-row', 'selected_deployment': executed, 'eligible': True, 'position': 1}]}


def test_native_selection_uses_positions_not_catalog_visits(monkeypatch):
    original = native_routes.executed_decision
    captured = []
    def record(mode, executed, considered, skipped=()):
        captured.append((list(considered), list(skipped)))
        return original(mode, executed, considered, skipped)
    monkeypatch.setattr(native_routes, 'executed_decision', record)
    dep = {'id': 'chosen', 'native_selection': native_selection_record()}
    before = deepcopy(dep)
    result = native_routes.selected_decision(dep)
    assert result['mode'] == 'AUTOMATIC' and result['fallback'] is None
    assert result['propensity'] == {'chosen': 1, 'second': 0}
    assert captured == [(['chosen', 'second', 'excluded'], [('excluded', 'QUOTA_EXHAUSTED')])]
    assert dep == before and 'SECRET' not in json.dumps(result)


@pytest.mark.parametrize('fault', ['missing', 'mismatch', 'duplicate', 'pin-mismatch'])
def test_inconsistent_native_selection_keeps_only_executed(fault):
    selection = native_selection_record()
    if fault == 'missing':
        selection['candidates'][-1]['position'] = 3
    elif fault == 'mismatch':
        selection['candidates'][-1]['selected_deployment'] = 'other'
    elif fault == 'duplicate':
        selection['candidates'][0]['position'] = 1
    else:
        selection = {'reason': 'EXACT_TEMPORARY_PIN', 'candidates': [
            {'eligible': True, 'selected_deployment': 'other'}]}
    result = native_routes.selected_decision({'id': 'chosen', 'native_selection': selection})
    assert result == native_routes.executed_decision('UNKNOWN', 'chosen', ['chosen'])


def test_native_selection_pin_defaults_and_ladder_precedence():
    dep = {'id': 'chosen', 'native_selection': {'reason': 'EXACT_TEMPORARY_PIN',
        'candidates': [{'eligible': True, 'selected_deployment': 'chosen'}]}}
    assert native_routes.selected_decision(dep) == native_routes.executed_decision(
        'EXACT_TEMPORARY_PIN', 'chosen', ['chosen'])
    dep['native_auto_selection'] = {'route': 'ladder', 'skipped': [{'route': 'first', 'reason': 'LIMIT'}]}
    assert native_routes.selected_decision(dep) == native_routes.executed_decision(
        'AUTOMATIC', 'ladder', ['first', 'ladder'], [('first', 'LIMIT')])
    assert native_routes.selected_decision(dep, pinned=True)['mode'] == 'EXACT_TEMPORARY_PIN'
    assert native_routes.selected_decision({'id': 'fixed'})['mode'] == 'CONFIGURED'
    assert native_routes.selected_decision({'id': 'fixed'}, default_mode='UNKNOWN')['mode'] == 'UNKNOWN'


@pytest.mark.parametrize('role', ['implementer', 'explorer'])
def test_real_native_auto_selection_matches_shared_decision(selection, role):
    from relay import native_catalog
    chosen = native_catalog.select('auto', role=role)
    before = deepcopy(chosen)
    saved = chosen['native_selection']['candidates']
    eligible = sorted((r for r in saved if r['eligible']), key=lambda r: r['position'])
    result = native_routes.selected_decision(chosen)
    assert result['mode'] == 'AUTOMATIC' and result['fallback'] is None
    assert list(result['propensity']) == [r['selected_deployment'] for r in eligible]
    assert result['propensity'][chosen['id']] == 1 and chosen == before


@pytest.mark.parametrize('mode', ['AUTOMATIC', 'EXACT_TEMPORARY_PIN', 'CONFIGURED'])
def test_work_host_persists_shared_native_selection_without_changing_deployment(work_host, monkeypatch, mode):
    from relay import native_catalog, native_work_host
    from test_native_cli import deployment as cli_deployment
    selected = cli_deployment(work_host[0]); selected['model'] = 'qwen-3.8-27b'
    if mode != 'CONFIGURED':
        provenance = (native_selection_record(selected['id']) if mode == 'AUTOMATIC' else
            {'reason': 'EXACT_TEMPORARY_PIN', 'candidates': [
                {'eligible': True, 'selected_deployment': selected['id']}]})
        monkeypatch.setattr(native_routes, 'ranked', lambda *a, **k: ([deepcopy(selected)], provenance))
        selected = native_catalog.select('auto', role='implementer')
    before = deepcopy(selected)
    custody = work_host[0]/'selection-host'
    runner = native_work_host.create_runner(selected, 'work-one', custody, unlimited=True)
    try:
        saved = json.loads((custody/'host.json').read_text(encoding='utf-8'))
        assert saved['deployment'] == before == selected
        assert saved['executed_decision']['mode'] == mode
        assert saved['executed_decision']['executed_deployment'] == selected['id']
        assert saved['executed_decision']['fallback'] is None
    finally:
        native_work_host.finish_runner(runner)
