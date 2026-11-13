"""Planner delegation uses the real session/tool boundary, with offline model ports."""
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from compute_plane.native_journal import NativeJournal, save_json
from compute_plane.native_models import encode
from relay import delegation, native_agent, planning_tools, turn_engine
from relay.execution_usage_sources import execution_usage
from tare_tools_agent_runtime import ToolCall, EffectRequest
from test_native_agent import Client, deployment, setup  # noqa: F401
from test_execution_usage_sources import direct


PLAN = {'summary': 'Bounded plan.', 'steps': ['Update a.py; then verify tests/test_a.py.'],
        'criteria': ['Preserve existing callers.'], 'sources': [], 'remaining': [], 'tasks': []}


@pytest.fixture
def planner(tmp_path, monkeypatch):
    parent = tmp_path/'parent'
    parent.mkdir()
    journal = NativeJournal(parent/'journal.jsonl')
    journal.append({'kind': 'session_started', 'session_id': 'parent'})
    journal.append({'kind': 'model_requested', 'call_id': 'parent/model/1'})
    source = tmp_path/'project'; source.mkdir()
    candidate = dict(deployment(), id='fixture-planner')
    params = json.loads(candidate['native_profile']['parameters_json'])
    params['chat_template_kwargs']['reasoning_effort'] = 'high'
    candidate['native_profile']['parameters_json'] = encode(params)
    state = {'deployment': deployment(), 'planning': {'routes': {'worker': candidate}, 'other_providers': False},
        'limits': {'max_model_calls': 10}, 'deadline_utc': None,
        'config': {'repositories': {'repo': str(source)}, 'writable': {'repo': ['a.py']},
                   'verifiers': {}, 'capabilities': ['read', 'edit', 'terminal'],
                   'project_graph': str(tmp_path/'graph.json')}}
    return parent, state, candidate


def wrap(parent, state, control):
    tools = SimpleNamespace(names=('read',), tool_specs_json='{}', session_id='parent', binding=control.binding)
    return planning_tools.PlanningTools(tools, parent, state)


def request(args=None, effect='effect'):
    return EffectRequest('parent', effect, ToolCall('plan-call', 'delegate_plan', encode(args or {'instruction': 'Plan a change.'})))


def execution():
    from tare_tools_agent_runtime import BindingRef, CancellationToken, ExecutionControl
    return ExecutionControl(BindingRef('parent', 'binding', 'owner', 1, 'opaque'), CancellationToken(),
                            1000, monotonic=lambda: 0)


def test_required_planner_uses_role_routes_instead_of_coordinator(tmp_path, monkeypatch):
    parent=deployment(); candidate=dict(deployment(),id='frontier-planner')
    monkeypatch.setattr(turn_engine,'policy',lambda:{'planner':['configured-planner']})
    monkeypatch.setattr('relay.native_catalog.select',lambda name:deepcopy(candidate))
    result=planning_tools.snapshot(parent,required=True)
    assert list(result['routes'])==['configured-planner'] and result['other_providers']
    assert result['routes']['configured-planner']['id']!='session-planner'


def test_exact_planner_pin_is_the_only_offered_route(monkeypatch):
    from relay import execution_scope
    from dataclasses import replace
    candidate = dict(deployment(), native_model_choice={'connection': 'codex-cli'})
    scope = replace(execution_scope.current(), pins=(('planner', 'openai/codex-cli@high'),))
    monkeypatch.setattr(execution_scope, 'current', lambda: scope)
    monkeypatch.setattr(execution_scope, 'pinned', lambda role: deepcopy(candidate))
    monkeypatch.setattr(turn_engine, 'policy', lambda: pytest.fail('A pin must not expose the unfiltered route menu.'))
    result = planning_tools.snapshot(deployment(), required=True)
    assert result['routes'] == {'codex-cli': candidate}
    assert result['route_order'] == ['codex-cli'] and result['other_providers']


def test_delivery_diagnostics_reach_the_coordinator(planner, monkeypatch):
    parent, state, candidate = planner
    monkeypatch.setattr(turn_engine, 'available', lambda name: (candidate, None))
    details = {'code': 'INVALID_DELIVERY', 'issues': [{'code': 'native_artifact_ignored_target', 'field': 'tasks.0.target_paths.0'}]}
    monkeypatch.setattr(native_agent, 'run_session', lambda *a, **kw: {
        'status': 'BLOCKED', 'reason': 'MODEL_FAILURE', 'failure_code': 'INVALID_DELIVERY', 'delivery_validation': details})
    result = json.loads(wrap(parent, state, execution()).execute(request(), execution()).output_json)
    assert result['delivery_validation'] == details
    assert 'host rejected' in result['next']


def test_legacy_coordinator_fallback_is_repaired_without_rewriting_frozen_tools(planner,monkeypatch):
    parent,state,candidate=planner
    state.update(conversation_flow=True,conversation_tool_specs='original cached tool schema')
    state['planning']={'routes':{'session-planner':deployment()},'other_providers':False}
    monkeypatch.setattr(planning_tools,'snapshot',lambda *a,**kw:
        {'routes':{'frontier':candidate},'route_order':['frontier'],'other_providers':True})
    monkeypatch.setattr(turn_engine,'available',lambda name:(deepcopy(candidate),None))
    tool=wrap(parent,state,execution())
    assert tool.choose({'route':'session-planner'})[0]=='frontier'
    assert state['conversation_tool_specs']=='original cached tool schema'
    assert list(state['planning']['routes'])==['session-planner']


def test_below_floor_local_planner_is_refused_even_when_default_is_omitted(planner,monkeypatch):
    parent,state,candidate=planner
    candidate['native_profile']['parameters_json']=encode({'chat_template_kwargs':
        {'enable_thinking':True,'reasoning_effort':'low'},'cache_prompt':True})
    calls=[];stub_child(monkeypatch,candidate,calls)
    tool=wrap(parent,state,execution())
    refused=tool.execute(request({'instruction':'Plan.','effort':'medium'}),execution())
    assert refused.status=='refused' and 'minimum reasoning is high' in refused.output_json
    assert not calls and not (parent/'planning').exists()
    default=tool.execute(request({'instruction':'Plan.','effort':'default'},'corrected'),execution())
    assert default.status=='refused' and 'at least high' in default.output_json
    assert not calls and not (parent/'planning').exists()


def test_local_only_plans_on_the_local_model_or_says_no_local_planner_is_available(planner, monkeypatch):
    from relay import execution_scope as scope, native_catalog
    from relay.execution_team import reasoning
    parent, state, candidate = planner
    tool = wrap(parent, state, execution())
    with scope.activated(scope.Scope(only='local')):
        monkeypatch.setattr(turn_engine, 'available', lambda name, **kw: (native_catalog.select(name), None))
        name, value = tool.choose({})
        assert name == 'local-qwen' and reasoning(value) == 'xhigh'  # its lowest level at or above high
        monkeypatch.setattr(turn_engine, 'available', lambda name, **kw: (None, 'local GPU busy'))
        with pytest.raises(ValueError, match=r'^No local planner is available in local-only mode \(local-qwen: '):
            tool.choose({})
    monkeypatch.setattr(turn_engine, 'available', lambda name, **kw: (deepcopy(candidate), None))
    assert tool.choose({})[0] == 'worker'  # other modes keep the configured planner


def stub_child(monkeypatch, candidate, calls):
    monkeypatch.setattr(turn_engine, 'available', lambda name: (deepcopy(candidate), None))
    def run(config, dep, root, instruction, **kwargs):
        calls.append((config, dep, root, instruction, kwargs))
        root.mkdir()
        journal = NativeJournal(root/'journal.jsonl')
        journal.append({'kind': 'session_started', 'session_id': 'planner'})
        for n in (1, 2):
            journal.append({'kind': 'model_requested', 'call_id': f'planner/model/{n}'})
        save_json(root/'artifact.json', {'payload': PLAN})
        save_json(root/'native-agent.json', {'session_id': 'planner'})
        return {'status': 'PROPOSED', 'model_calls': 2}
    monkeypatch.setattr(native_agent, 'run_session', run)


def test_child_is_read_only_bounded_and_reuses_a_completed_effect(planner, monkeypatch):
    parent, state, candidate = planner
    calls = []
    stub_child(monkeypatch, candidate, calls)
    control = execution(); tool = wrap(parent, state, control)
    result = tool.execute(request(), control)
    value = json.loads(result.output_json)
    assert value['status'] == 'PLANNED' and value['plan'] == PLAN and value['route'] == 'worker'
    config, dep, root, instruction, opts = calls[0]
    assert config['writable'] == {} and config['capabilities'] == ['read', 'list', 'search']
    assert 'project_graph' not in config and opts['delegated_leaf'] is True
    assert opts['cancellation'] is control.cancellation and opts['limits']['max_model_calls'] == 6
    assert opts['limits']['max_seconds'] == 600
    assert tool.calls_committed() == 2
    assert tool.execute(request(), control).output_json == result.output_json and len(calls) == 1
    changed = tool.execute(request({'instruction': 'Different mission.'}), control)
    assert changed.status == 'refused' and len(calls) == 1


def test_planning_time_never_extends_the_parent_deadline(planner, monkeypatch):
    parent, state, candidate = planner
    calls = []
    stub_child(monkeypatch, candidate, calls)
    monkeypatch.setattr(planning_tools.time, 'time', lambda: 1000)
    state['deadline_utc'] = 1090
    control = execution()
    result = wrap(parent, state, control).execute(request(), control)
    assert result.status == 'succeeded', result.output_json
    assert calls[0][-1]['limits'] == {'max_model_calls': 6, 'max_seconds': 85, 'request_seconds':85}
    assert state['deadline_utc'] == 1090 and state['limits']['max_model_calls'] == 10


def test_unlimited_planning_keeps_the_normal_request_timeout_instead_of_short_profile_default(planner, monkeypatch):
    from relay import execution_scope
    parent, state, candidate = planner
    candidate['native_execution_limits'] = {'max_model_calls': 6, 'max_seconds': 600, 'request_seconds': 120}
    original = deepcopy(candidate)
    calls = []
    stub_child(monkeypatch, candidate, calls)
    scope = replace(execution_scope.current(), unlimited=('time',))
    monkeypatch.setattr(execution_scope, 'current', lambda: scope)
    control = execution()
    control = type(control)(control.binding, control.cancellation, float('inf'), monotonic=lambda: 0)
    result = wrap(parent, state, control).execute(request(), control)
    assert result.status == 'succeeded', result.output_json
    child_limits = calls[0][-1]['limits']
    assert child_limits['max_seconds'] is None
    assert child_limits['request_seconds'] == 600
    assert child_limits['max_model_calls'] == 6
    assert state['deadline_utc'] is None and candidate == original


@pytest.mark.parametrize('extend,reword',[(False,False),(True,False),(True,True)])
def test_genuine_later_scope_revision_keeps_candidate_and_reuses_prior_plan_with_shared_budget(planner,monkeypatch,extend,reword):
    import hashlib
    from relay import conversation_tasks, native_artifacts
    parent,state,candidate=planner;state['conversation_flow']=True
    source=Path(state['config']['repositories']['repo']);(source/'a.py').write_text('value = 1\n')
    control=execution();tool=wrap(parent,state,control)
    owner=conversation_tasks.ConversationTools(tool,parent,state,'turn-1')
    owner.understand({'intent':'deliver','relation':'new','objective':'Fix the value.','criteria':['Preserve callers.']})
    payload={**PLAN,'tasks':[{'key':'fix','title':'Fix value','summary':'Fix value.',
        'cluster':'test','priority':'P2','target_paths':['a.py'],'criteria':['Preserve callers.','Old regression remains green.'],
        'depends_on':[],'test_paths':[],'verifiers':[]}]}
    artifact=parent/'planning'/'old'/'session'/'artifact.json';artifact.parent.mkdir(parents=True)
    save_json(artifact,{'schema':'tare.tools/native-artifact/1','role':'planner','kind':native_artifacts.KINDS['planner'],
        'authority':'NONE','session_id':'old','call_id':'old/1','profile_digest':'a'*64,'payload':payload})
    saved=conversation_tasks.load(parent);task=conversation_tasks.current(saved)
    conversation_tasks.task_plan(parent,state['config'],task,{'artifact':str(artifact)})
    old_sha=hashlib.sha256(artifact.read_bytes()).hexdigest()
    delivery={'candidate':{'patch':'retained-private-patch'},'verdict':'NOT_REVIEWED','applied':False}
    task['delivery']=delivery;conversation_tasks.save(parent,saved)
    for i in range(3):
        path=parent/'planning'/str(i);path.mkdir()
        save_json(path/'receipt.json',{'demand_id':task['id'],'calls_reserved':6,'calls_used':1,'status':'FINISHED'})
    NativeJournal(parent/'journal.jsonl').append({'kind':'user_message','message_id':'new-scope',
        'text':'Include the newly identified writer in the acceptance scope.'})
    owner.invocation_id='turn-2'
    criteria=['Preserve callers.']+(['Cover the newly identified writer.'] if extend else [])
    owner.understand({'intent':'deliver','relation':'revise','criteria':criteria,
        **({'objective':'Repair the value and its real writer.'} if reword else {})})
    revised=conversation_tasks.current(conversation_tasks.load(parent))
    assert revised['prior_delivery']==delivery and revised['replan_from']['sha256']==old_sha
    assert (revised.get('replan_candidate')==delivery) is not reword
    assert revised.get('planning_revision',0)==int(extend)
    calls=[];actual=native_agent.run_session
    monkeypatch.setattr(turn_engine,'available',lambda name:(deepcopy(candidate),None))
    def child(config,dep,root,instruction,**kw):
        calls.append(kw)
        assert config['input_artifacts'][0]['artifact']['payload']==payload
        assert config['input_artifacts'][0]['authority']=='NONE'
        assert kw['limits']['max_model_calls']==4  # old charged calls still reduce the original allowance
        revised_payload=deepcopy(payload);revised_payload['tasks'][0]['criteria']=['Cover the newly identified writer.']
        client=Client(steps=[('read',{'repository':'repo','path':'a.py'}),('tare_deliver',revised_payload)])
        return actual(config,dep,root,instruction,client=client,local_counter=client,**kw)
    monkeypatch.setattr(native_agent,'run_session',child)
    result=owner.execute(request(effect='new-scope-plan'),control)
    if not extend:
        assert result.status=='refused' and not calls
        return
    assert result.status=='succeeded',result.output_json
    current=conversation_tasks.current(conversation_tasks.load(parent))
    assert current['delivery']==(None if reword else delivery) and current['criteria']==criteria
    assert current['prior_delivery']==delivery
    assert current['plan']['artifact']!=str(artifact) and artifact.is_file()
    conversation_tasks.task_plan(parent,state['config'],current,{'artifact':current['plan']['artifact']})
    assert ('Old regression remains green.' in current['plan']['unit']['criteria']) is not reword
    assert tool.calls_committed()==5 and state['limits']['max_model_calls']==10
    with pytest.raises(ValueError,match='already revised|Execution already started'):
        owner.understand({'intent':'deliver','relation':'revise','criteria':criteria+['Another automatic renewal.']})


@pytest.mark.parametrize('changed,fresh', [(True, True), (False, True), (True, False)])
def test_stale_plan_facts_allow_one_human_continued_requalification_without_resetting_budget(planner, monkeypatch, changed, fresh):
    import hashlib
    from relay import conversation_tasks, native_artifacts
    parent, state, candidate = planner
    state['conversation_flow'] = True
    source = Path(state['config']['repositories']['repo'])
    (source/'facts.txt').write_text('before\n')
    fact_hash = hashlib.sha256((source/'facts.txt').read_bytes()).hexdigest()
    payload = {**PLAN, 'sources': [{'repository': 'repo', 'path': 'facts.txt', 'sha256': fact_hash}]}
    artifact = parent/'planning/old/session/artifact.json'; artifact.parent.mkdir(parents=True)
    save_json(artifact, {'schema': 'tare.tools/native-artifact/1', 'role': 'planner',
        'kind': native_artifacts.KINDS['planner'], 'authority': 'NONE', 'session_id': 'old',
        'call_id': 'old/1', 'profile_digest': 'a'*64, 'payload': payload})
    task = {'id': 'task-fix', 'objective': 'Deliver the original report.', 'criteria': ['Keep all evidence.'],
        'intent': 'deliver', 'intent_input': 'external-fix', 'stage': 'PLANNED',
        'plan': {'artifact': str(artifact), 'sha256': hashlib.sha256(artifact.read_bytes()).hexdigest(),
            'sources': payload['sources'], 'unit': {'repository': 'repo', 'target_paths': ['report.md']}}}
    conversation_tasks.save(parent, {'schema': 'tare.tools/conversation-tasks/1',
        'current': task['id'], 'tasks': [task]})
    journal = NativeJournal(parent/'journal.jsonl')
    if not fresh:
        journal.append({'kind': 'user_message', 'message_id': 'external-fix', 'text': 'Continue.'})
    for index in range(3):
        effect_id = 'old-plan-'+str(index)
        journal.append({'kind': 'effect_requested', 'effect_id': effect_id, 'tool_call': {'name': 'delegate_plan'}})
        folder = parent/'planning'/hashlib.sha256(effect_id.encode()).hexdigest()[:24]; folder.mkdir()
        save_json(folder/'receipt.json', {'demand_id': task['id'], 'calls_reserved': 6, 'calls_used': 1, 'status': 'FINISHED'})
    if fresh:
        journal.append({'kind': 'user_message', 'message_id': 'external-fix', 'text': 'The source was fixed; continue.'})
    if changed:
        (source/'facts.txt').write_text('after\n')
    calls = []; stub_child(monkeypatch, candidate, calls)
    tool = wrap(parent, state, execution())
    owner = conversation_tasks.ConversationTools(tool, parent, state, 'continued-turn')
    intake = owner.understand({'intent': 'deliver', 'relation': 'continue'})
    assert bool(intake.get('planning_requalification')) is (changed and fresh)
    if changed and fresh:
        assert intake['planning_requalification']['status'] == 'SOURCE_CHANGED'
        assert 'remain charged' in intake['next'] and intake['task']['criteria'] == task['criteria']
    result = tool.execute(request(effect='repair-plan'), execution())
    if not (changed and fresh):
        assert result.status == 'refused' and calls == []
        return
    assert result.status == 'succeeded' and len(calls) == 1
    config, _, _, instruction, options = calls[0]
    assert config['input_artifacts'][0]['artifact']['payload'] == payload
    assert options['limits']['max_model_calls'] == 4  # prior charged calls remain charged
    assert 'changed_source_facts' in instruction and 'not an applied project change' in instruction
    rows = planning_tools.records(parent)
    assert len(rows) == 4 and sum(bool(r.get('source_requalification')) for r in rows) == 1
    assert tool.calls_committed() == 5 and state['limits']['max_model_calls'] == 10
    repeated = tool.execute(request(effect='repeat-repair-plan'), execution())
    assert repeated.status == 'refused' and len(calls) == 1


@pytest.mark.parametrize('graph_contract', [False, True])
def test_conversation_planner_receives_actual_check_names_separately_from_coordinator_notes(planner, monkeypatch, graph_contract):
    from relay import conversation_tasks
    parent, state, candidate = planner
    state['conversation_flow'] = True
    state['config']['verifiers'] = {'project-tests': {'repository': 'repo', 'argv': ['python', '-m', 'pytest']}}
    calls = []; stub_child(monkeypatch, candidate, calls)
    control = execution(); tool = wrap(parent, state, control)
    coordinator = conversation_tasks.ConversationTools(tool, parent, state, 'turn')
    coordinator.understand({'intent': 'deliver', 'relation': 'new', 'objective': 'Create graph.',
                            'criteria': ['Installed graph validation passes.']})
    context = 'Example command: python -m graph_backlog.cli validate'
    if graph_contract: context += ' Host check: '+conversation_tasks.BACKLOG_CHECK
    result = tool.execute(request({'instruction': 'Create graph.', 'context': context}), control)
    assert result.status == 'succeeded', result.output_json
    brief = calls[0][3]
    mission = json.loads(brief.rsplit('\n', 1)[-1])
    assert set(mission['available_verifiers']) == {'project-tests', conversation_tasks.BACKLOG_CHECK}
    assert mission['criteria'] == ['Installed graph validation passes.']
    assert 'a shell command is not a verifier name' in brief
    if graph_contract:
        assert mission['host_contracts'][conversation_tasks.BACKLOG_CHECK] == conversation_tasks.backlog_authoring_contract()
    else:
        assert 'host_contracts' not in mission


@pytest.mark.parametrize('cause', ['budget', 'cancelled', 'binding', 'arguments', 'provider'])
def test_refusal_never_starts_a_child(planner, monkeypatch, cause):
    parent, state, candidate = planner
    calls = []
    stub_child(monkeypatch, candidate, calls)
    control = execution()
    args = {'instruction': 'Plan.'}
    req = request(args)
    if cause == 'budget': state['limits']['max_model_calls'] = 3
    elif cause == 'cancelled': control.cancellation.cancel()
    elif cause == 'binding': req = replace(req, session_id='other')
    elif cause == 'arguments': req = request({'instruction': 'x'*9000})
    else: state['deployment']['id'] = 'another-provider'
    assert wrap(parent, state, control).execute(req, control).status == 'refused'
    assert calls == [] and not (parent/'planning').exists()


def test_interruption_reserves_calls_and_does_not_repeat_paid_planning(planner, monkeypatch):
    parent, state, candidate = planner
    monkeypatch.setattr(turn_engine, 'available', lambda name: (deepcopy(candidate), None))
    calls = []
    def interrupted(*args, **kwargs):
        calls.append(1)
        raise OSError('worker interrupted')
    monkeypatch.setattr(native_agent, 'run_session', interrupted)
    control = execution(); tool = wrap(parent, state, control)
    assert tool.execute(request(), control).status == 'refused'
    assert tool.calls_committed() == 6
    again = json.loads(tool.execute(request(), control).output_json)
    assert again['status'] == 'INTERRUPTED' and calls == [1]


def test_other_provider_requires_owner_setting(planner, monkeypatch):
    parent, state, candidate = planner
    state['deployment']['id'] = 'another-provider'
    state['planning']['other_providers'] = True
    calls = []
    stub_child(monkeypatch, candidate, calls)
    control = execution()
    assert json.loads(wrap(parent, state, control).execute(request(), control).output_json)['status'] == 'PLANNED'
    assert len(calls) == 1


def test_all_children_share_calls_and_appear_in_usage_once(tmp_path):
    parent, plan, implement = tmp_path/'parent', tmp_path/'parent/planning/p/session', tmp_path/'parent/units/u/session-1'
    direct(parent, 'parent'); direct(plan, 'planner'); direct(implement, 'implementer')
    save_json(parent/'planning/p/receipt.json', {'calls_reserved': 6, 'calls_used': 1})
    save_json(parent/'units/u/receipt.json', {'rungs': [{'model_calls': 1}]})
    (parent/'delegations/e').mkdir(parents=True)
    save_json(parent/'delegations/e/receipt.json', {'calls_reserved': 3, 'calls_used': 2})
    assert delegation.child_calls(parent) == 4
    result = execution_usage(sessions=[parent])
    assert result['generation_intents'] == 3
    assert len(result['recorded_sessions']) == 3
    assert result['unpriced_or_partial_calls'] == 3
    assert execution_usage(sessions=[parent, plan])['generation_intents'] == 3


@pytest.mark.parametrize('role', ['implementer', 'explorer'])
def test_real_nested_sessions_preserve_coordinator_history_and_count_both_models(setup, monkeypatch, role):
    from test_native_artifacts import payload
    from tare_tools_agent_runtime import CancellationToken
    from relay import native_catalog
    config, custody, source = setup
    config['role'] = role
    if role == 'explorer':
        config['writable'] = {}
    config['verifiers'] = {}
    parent_dep, child_dep = deployment(), dict(deployment(), id='fixture-planner')
    params = json.loads(child_dep['native_profile']['parameters_json'])
    params['chat_template_kwargs']['reasoning_effort'] = 'high'
    child_dep['native_profile']['parameters_json'] = encode(params)
    monkeypatch.setattr(delegation, 'experiments', lambda route_class=None: dict(delegation.EXPERIMENTS, planner_child=True))
    monkeypatch.setattr(turn_engine, 'policy', lambda: dict(turn_engine.DEFAULT_POLICY, planner=['z-worker', 'a-worker']))
    monkeypatch.setattr(native_catalog, 'select', lambda name: deepcopy(child_dep))
    monkeypatch.setattr(turn_engine, 'available', lambda name: (deepcopy(child_dep), None))
    parent = Client(steps=[('delegate_plan', {'instruction': 'Plan the change to a.py.', 'route': 'z-worker'}),
                           ('tare_deliver', {'summary': 'Planning advice received; implementation remains pending.'}),
                           ('tare_deliver', {'summary': 'Refined the existing plan without another delegation.'})])
    child = Client(steps=[('read', {'repository': 'repo', 'path': 'a.py'}),
                          ('tare_deliver', payload('planner', source))])
    real_run = native_agent.run_session
    def run_child(config, dep, directory, instruction, **options):
        assert config['role'] == 'planner' and dep['id'] == 'fixture-planner'
        return real_run(config, dep, directory, instruction, client=child, local_counter=child, **options)
    monkeypatch.setattr(native_agent, 'run_session', run_child)
    observed = []
    result = real_run(config, parent_dep, custody, 'Review a bounded plan; do not implement.',
                      client=parent, local_counter=parent, limits={'max_model_calls': 6, 'max_seconds': 90},
                      cancellation=CancellationToken(), on_activity=observed.append)
    assert result['status'] == 'PROPOSED', result
    assert result['planning']['model_calls'] == 2 and parent.generations == child.generations == 2
    child_rows = [e['activity'] for e in observed if e['activity'].get('actor', {}).get('role') == 'planner']
    assert [row['state'] for row in child_rows] == ['running', 'succeeded']
    assert all(row['actor']['reasoning'] == 'high' for row in child_rows)
    wires = [body for path, body in parent.calls if path.endswith('/chat/completions')]
    assert wires[0]['tools'] == wires[1]['tools'] and wires[0]['messages'][:2] == wires[1]['messages'][:2]
    child_wires = [body for path, body in child.calls if path.endswith('/chat/completions')]
    assert all(t['function']['name'] in {'read', 'list', 'search', 'tare_deliver'} for t in child_wires[0]['tools'])
    assert execution_usage(sessions=[custody])['generation_intents'] == 4
    repeated = real_run(None, None, custody, resume=True, client=parent, local_counter=parent)
    assert repeated['status'] == 'PROPOSED' and child.generations == parent.generations == 2
    refined = real_run(None, None, custody, resume=True, follow_up='Refine the existing plan.', message_id='refine',
                       client=parent, local_counter=parent)
    assert refined['status'] == 'PROPOSED', refined
    assert child.generations == 2 and parent.generations == 3
    wires = [body for path, body in parent.calls if path.endswith('/chat/completions')]
    assert wires[0]['tools'] == wires[-1]['tools'] and wires[0]['messages'][:2] == wires[-1]['messages'][:2]


def test_picker_profile_is_immutable_and_keeps_other_role_routes(tmp_path):
    from relay.native_profiles import local_path
    from relay.native_generation import load_deployment
    before = deepcopy(turn_engine.policy())
    profile = tmp_path/'profile.json'
    candidate = deployment()
    params = json.loads(candidate['native_profile']['parameters_json'])
    params['chat_template_kwargs']['reasoning_effort'] = 'high'
    candidate['native_profile']['parameters_json'] = encode(params)
    profile.write_text(json.dumps(candidate))
    selected = planning_tools.configure_model(profile)
    assert planning_tools.configure_model(profile) == selected
    assert load_deployment(local_path(selected)) == candidate
    assert turn_engine.policy()['planner'] == [selected]
    assert all(turn_engine.policy()[role] == before[role] for role in ('implementer', 'auditor', 'vision'))


@pytest.mark.parametrize('settled', [False, True])
def test_shared_money_keeps_unknown_reservations_until_child_spend_owner_settles(planner, monkeypatch, settled):
    from dataclasses import asdict
    from test_native_spend import tariff
    parent, state, candidate = planner
    for dep in (state['deployment'], candidate):
        dep['native_usage_tariff'] = asdict(tariff())
    state['deployment']['native_spend_policy'] = {'currency': 'USD', 'max_amount': '1'}
    calls = []
    stub_child(monkeypatch, candidate, calls)
    run = native_agent.run_session
    def child(*args, **kwargs):
        outcome = run(*args, **kwargs)
        if settled:
            outcome['spend'] = {'currency': 'USD', 'committed_amount': '0.001', 'uncertain_calls': 1}
        return outcome
    monkeypatch.setattr(native_agent, 'run_session', child)
    control = execution(); tools = wrap(parent, state, control)
    tools.parent_model = SimpleNamespace(report=lambda: {'remaining_amount': '0.5'})
    assert tools.execute(request(), control).status == 'succeeded'
    assert tools.spend_committed() == Decimal('0.001' if settled else '0.5')


def planning_receipt(parent):
    return json.loads(next((parent/'planning').glob('*/receipt.json')).read_text(encoding='utf-8'))


def test_configured_planner_records_decision_and_reasoning_sent(planner, monkeypatch):
    from relay import native_routes
    parent, state, candidate = planner
    calls = []
    stub_child(monkeypatch, candidate, calls)
    control = execution(); tool = wrap(parent, state, control)
    assert tool.execute(request({'instruction': 'Plan.', 'route': 'worker'}), control).status == 'succeeded'
    receipt = planning_receipt(parent)
    decision = receipt['executed_decision']
    assert decision['mode'] == 'CONFIGURED' and decision['propensity'] == {'worker': 1}
    assert decision['fallback'] is None and decision['ope_supported'] is False
    sent = native_routes._reasoning_of(calls[0][1]['native_profile']['parameters_json'])
    assert sent != 'UNKNOWN'
    assert [(a['status'], a['reasoning']) for a in receipt['attempts']] == [('PROPOSED', sent)]
    assert receipt['route'] == 'worker' and receipt['result']['status'] == 'PLANNED'
    assert 'Plan.' not in json.dumps(receipt['attempts'])


def test_unavailable_first_planner_is_a_recorded_skip(planner, monkeypatch):
    parent, state, candidate = planner
    state['planning'] = {'routes': {'gone': deepcopy(candidate), 'worker': candidate},
                         'route_order': ['gone', 'worker'], 'other_providers': False}
    calls = []
    stub_child(monkeypatch, candidate, calls)
    monkeypatch.setattr(turn_engine, 'available',
                        lambda name: (None, 'offline') if name == 'gone' else (deepcopy(candidate), None))
    control = execution()
    assert wrap(parent, state, control).execute(request(), control).status == 'succeeded'
    decision = planning_receipt(parent)['executed_decision']
    assert decision['mode'] == 'AUTOMATIC' and decision['propensity'] == {'worker': 1}
    assert decision['fallback'] == {'first_ranked': 'gone', 'executed': 'worker',
                                    'skipped': [{'deployment': 'gone', 'reason': 'UNAVAILABLE_OR_PROFILE_CHANGED'}]}
    assert len(calls) == 1


def test_pinned_planner_records_pinned_reasoning_not_default(planner, monkeypatch):
    parent, state, candidate = planner
    calls = []
    stub_child(monkeypatch, candidate, calls)
    pinned = deepcopy(candidate)
    params = json.loads(pinned['native_profile']['parameters_json'])
    params['chat_template_kwargs']['reasoning_effort'] = 'xhigh'
    pinned['native_profile']['parameters_json'] = encode(params)
    pinned['native_model_choice'] = {**(pinned.get('native_model_choice') or {}), 'connection': 'pinned-planner'}
    scope = SimpleNamespace(pin=lambda role: role == 'planner', changes=lambda role: False,
                            allows=lambda deployment, role: (True, None), restrictions=(), unlimited=())
    monkeypatch.setattr('relay.execution_scope.current', lambda: scope)
    monkeypatch.setattr('relay.execution_scope.pinned', lambda role, producer=None: deepcopy(pinned))
    control = execution()
    assert wrap(parent, state, control).execute(request(), control).status == 'succeeded'
    receipt = planning_receipt(parent)
    assert receipt['executed_decision']['mode'] == 'EXACT_TEMPORARY_PIN'
    assert receipt['executed_decision']['propensity'] == {'pinned-planner': 1}
    assert receipt['attempts'][0]['reasoning'] == 'xhigh'
    assert receipt['attempts'][0]['deployment'] == 'pinned-planner'
    assert 'xhigh' in calls[0][1]['native_profile']['parameters_json']


def test_interrupted_planner_attempt_stays_unknown_and_is_not_repeated(planner, monkeypatch):
    parent, state, candidate = planner
    monkeypatch.setattr(turn_engine, 'available', lambda name: (deepcopy(candidate), None))
    calls = []
    def interrupted(*args, **kwargs):
        calls.append(1)
        raise OSError('worker interrupted')
    monkeypatch.setattr(native_agent, 'run_session', interrupted)
    control = execution(); tool = wrap(parent, state, control)
    assert tool.execute(request(), control).status == 'refused'
    receipt = planning_receipt(parent)
    assert receipt['status'] == 'RUNNING' and 'result' not in receipt
    assert [a['status'] for a in receipt['attempts']] == ['DISPATCHING']
    assert receipt['executed_decision']['executed_deployment'] == 'worker'
    assert json.loads(tool.execute(request(), control).output_json)['status'] == 'INTERRUPTED'
    assert calls == [1] and planning_receipt(parent) == receipt


def test_a_users_correction_of_an_applied_change_plans_in_a_round_of_its_own(planner):
    from relay import conversation_tasks
    parent,state,_=planner;state['conversation_flow']=True
    control=execution();tool=wrap(parent,state,control)
    owner=conversation_tasks.ConversationTools(tool,parent,state,'turn-1')
    owner.understand({'intent':'deliver','relation':'new','objective':'Fix the value.','criteria':['Preserve callers.']})
    saved=conversation_tasks.load(parent);task=conversation_tasks.current(saved)
    task['delivery']={'verdict':'VERIFIED','applied':False};conversation_tasks.save(parent,saved)
    owner.understand({'intent':'deliver','relation':'correct'})  # a refused candidate: same round, the cap holds
    assert conversation_tasks.current(conversation_tasks.load(parent)).get('planning_revision',0)==0
    saved=conversation_tasks.load(parent);task=conversation_tasks.current(saved)
    task['delivery']={'verdict':'VERIFIED','applied':True};conversation_tasks.save(parent,saved)
    NativeJournal(parent/'journal.jsonl').append({'kind':'user_message','message_id':'ci-failed',
        'text':'CI failed on the applied change; fix it.'})
    owner.invocation_id='turn-2'
    owner.understand({'intent':'deliver','relation':'correct'})
    corrected=conversation_tasks.current(conversation_tasks.load(parent))
    assert corrected['planning_revision']==1 and corrected['delivery'] is None
    owner.understand({'intent':'deliver','relation':'correct'})  # the same user request opens one round only
    assert conversation_tasks.current(conversation_tasks.load(parent))['planning_revision']==1
