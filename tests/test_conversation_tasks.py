"""Role and demand contracts, through real runtime effects and real candidate trees."""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from compute_plane.native_journal import save_json
from compute_plane.native_models import encode
from relay import conversation_tasks as tasks, native_agent, work_units
from tare_tools_agent_runtime import BindingRef, CancellationToken, EffectRequest, EffectResult, ExecutionControl, ToolCall
from test_native_agent import Client, deployment, setup  # noqa: F401


def control():
    return ExecutionControl(BindingRef('parent', 'binding', 'owner', 1, 'opaque'), CancellationToken(),
                            100000, monotonic=lambda: 0)


def effect(name, args, key='effect'):
    return EffectRequest('parent', key, ToolCall('tool-'+key, name, encode(args)))


@pytest.mark.parametrize('assistant', [False, True])
@pytest.mark.parametrize('role', ['implementer', 'explorer'])
def test_public_cli_keeps_explicit_native_sessions_distinct_from_assistant_chats(tmp_path, assistant, role):
    from relay.console_backend import Selection, command
    selected = Selection(str(tmp_path), mode='session', prompt='Inspect this project.', role=role,
                         write_paths=['.'] if role == 'implementer' else [])
    assert ('--conversation' in command(selected, assistant=assistant)) is assistant


@pytest.fixture
def host(tmp_path):
    root = tmp_path/'project'; root.mkdir()
    (root/'a.py').write_text('value = 1\n')
    custody = tmp_path/'session'; custody.mkdir()
    execution = control()
    seen = []
    def execute(request, _):
        seen.append(request.tool_call.name)
        return EffectResult(request.effect_id, 'succeeded', encode({'ok': True}), True)
    inner = SimpleNamespace(names=('read', 'edit', 'verify', 'terminal_run', 'apply_patch', 'delegate_plan'),
        tool_specs_json=encode({n: {} for n in ('read', 'edit', 'verify', 'terminal_run', 'apply_patch', 'delegate_plan')}),
        session_id='parent', binding=execution.binding, execute=execute)
    state = {'conversation_flow': True, 'config': {'repositories': {'repo': str(root)},
        'writable': {'repo': ['a.py']}, 'verifiers': {}, 'specgraph_edit_context': False},
        'deployment': deployment(), 'limits': {'max_model_calls': 30}, 'deadline_utc': None}
    return tasks.ConversationTools(inner, custody, state, 'turn-1'), execution, root, seen


def intake(host, intent='deliver', relation='new', **kwargs):
    tools, execution, *_ = host
    result = tools.execute(effect('demand', {'intent': intent, 'relation': relation,
        **({'objective': 'Change value to two.', 'criteria': ['value equals two']} if relation == 'new' else {}),
        **kwargs}, key='intake-'+relation), execution)
    assert result.status == 'succeeded', result.output_json
    return json.loads(result.output_json)


def test_unrecorded_followup_returns_exact_continuation_without_dispatching_planner(host):
    tools, execution, _, seen = host
    original = intake(host)['task']
    user_reply(host, 'sim')
    refused = tools.execute(effect('delegate_plan', {'instruction': 'Continue the comparison'}), execution)
    assert refused.status == 'refused' and seen == []
    output = json.loads(refused.output_json)
    action = output['required_action']
    assert action == {'tool': 'demand', 'arguments': {'intent': 'deliver', 'relation': 'continue'}}
    assert 'not an unavailable planner' in output['next']
    continued = tools.execute(effect(action['tool'], action['arguments'], key='continue'), execution)
    assert continued.status == 'succeeded'
    current = tasks.current(tasks.load(tools.root))
    assert current['id'] == original['id'] and current['criteria'] == original['criteria']
    dispatched = tools.execute(effect('delegate_plan', {'instruction': 'Continue the comparison'}, key='plan'), execution)
    assert dispatched.status == 'succeeded' and seen == ['delegate_plan']


def planned(host):
    tools, *_ = host
    task = tasks.current(tasks.load(tools.root))
    artifact = tools.root/'planning'/'plan'/'session'/'artifact.json'
    artifact.parent.mkdir(parents=True)
    payload = {'summary': 'Change a.py.', 'criteria': ['value equals two'], 'steps': ['Edit a.py.'],
        'sources': [], 'remaining': [], 'tasks': [{'key': 'change', 'title': 'Change value', 'summary': 'Set two.',
        'cluster': 'test', 'priority': 'P2', 'target_paths': ['a.py'], 'criteria': ['value equals two'],
        'depends_on': [], 'test_paths': [], 'verifiers': []}]}
    save_json(artifact, {'payload': payload})
    value = tasks.load(tools.root); task = tasks.current(value)
    tasks.task_plan(tools.root, tools.state['config'], task, {'artifact': str(artifact)})
    tasks.save(tools.root, value)
    return task


def test_planner_subtask_cannot_drop_the_users_delivery_criteria(host):
    tools = host[0]
    original = intake(host)['task']
    original['criteria'] = ['Deliver the fix and prove a stale write is refused.']
    artifact = tools.root/'planning'/'partial'/'session'/'artifact.json'
    artifact.parent.mkdir(parents=True)
    save_json(artifact, {'payload': {'summary': 'Investigate first', 'criteria': ['Write a report'],
        'tasks': [{'title': 'Document the investigation', 'target_paths': ['report.md'],
                   'criteria': ['Write a report'], 'depends_on': []}]}})
    tasks.task_plan(tools.root, tools.state['config'], original, {'artifact': str(artifact)})
    assert original['plan']['unit']['criteria'] == [
        'Deliver the fix and prove a stale write is refused.', 'Write a report']


def test_generated_scope_failure_returns_planner_repair_without_dispatch_or_lost_candidate(host,monkeypatch):
    tools,execution,root,*_=host
    intake(host);task=planned(host)
    (root/'pyproject.toml').write_text('[project]\nname="tare-tools-os"\n')
    (root/'scripts').mkdir();(root/'scripts/sync_disposition_manifest.py').touch()
    (root/'relay').mkdir();(root/'relay/a.py').write_text('value = 1\n')
    artifact=Path(task['plan']['artifact']);payload=json.loads(artifact.read_text())
    payload['payload']['tasks'][0]['target_paths']=['relay/a.py']
    save_json(artifact,payload)
    value=tasks.load(tools.root);task=tasks.current(value)
    task['plan']['sha256']=hashlib.sha256(artifact.read_bytes()).hexdigest()
    candidate={'patch':'retained-unapplied','sha256':'a'*64}
    task['delivery']={'verdict':'NOT_VERIFIED','candidate':candidate}
    original=list(task['criteria']);tasks.save(tools.root,value)
    monkeypatch.setattr(work_units,'run_unit',lambda *a,**kw:pytest.fail('Scope failure must dispatch no implementer'))
    result=tools.execute(effect('implement',{},'missing-generated-scope'),execution)
    assert result.status=='refused'
    assert json.loads(result.output_json)['required_action']['tool']=='delegate_plan'
    repaired=tasks.current(tasks.load(tools.root))
    assert repaired['criteria']==original and not repaired.get('plan')
    assert repaired['replan_candidate']['candidate']==candidate and repaired['replan_from']['artifact']==str(artifact)


def test_same_goal_planner_scope_correction_keeps_the_unapplied_candidate(host, monkeypatch):
    tools, execution, *_ = host
    intake(host); task = planned(host)
    value = tasks.load(tools.root)
    previous = {'verdict': 'NOT_REVIEWED', 'applied': False,
        'files': ['a.py'], 'candidate': {'patch': 'retained-host-candidate', 'sha256': 'a'*64}}
    tasks.current(value).update(stage='PENDING', delivery=previous)
    tasks.save(tools.root, value)
    monkeypatch.setattr(tools.tools, 'execute', lambda request, control: EffectResult(request.effect_id,
        'succeeded', encode({'status': 'PLANNED', 'artifact': task['plan']['artifact']}), True))
    result = tools.execute(effect('delegate_plan', {'instruction': 'Reconcile the executable check scope.'}), execution)
    assert result.status == 'succeeded'
    assert tasks.current(tasks.load(tools.root))['delivery'] == previous


def test_action_context_keeps_operator_constraints_while_full_plan_reaches_worker_and_audit(host,monkeypatch):
    from relay import work_forms
    tools,execution,*_=host
    intake(host);planned(host)
    value=tasks.load(tools.root);task=tasks.current(value)
    task['criteria']=['Keep reference read-only.','Only run the two focused tests.']
    artifact=Path(task['plan']['artifact'])
    payload=json.loads(artifact.read_text())
    payload['payload']['tasks'][0]['criteria']=[str(i)+': verification detail. '*70 for i in range(8)]
    save_json(artifact,payload)
    tasks.task_plan(tools.root,tools.state['config'],task,{'artifact':str(artifact)})
    tasks.save(tools.root,value)
    tools.state['config']['command_protection']={'review':'auto','backend':'mxc'}
    monkeypatch.setattr('relay.conversation_permissions.authorize',lambda root,state,*a,**kw:state['config'])
    recorded=[];original_record=work_forms.record_plan
    def record(root,config,plan):
        recorded.extend(deepcopy(plan['units']))
        return original_record(root,config,plan)
    monkeypatch.setattr(work_forms,'record_plan',record)
    observed=[]
    def stopped(root,state,*a,**kw):
        observed.append(state['config']['command_context'])
        return {'verdict':'INCONCLUSIVE','applied':False,'reason':'fixture stopped','files':[]}
    monkeypatch.setattr(work_units,'run_unit',stopped)
    result=tools.execute(effect('implement',{}),execution)
    assert result.status=='succeeded'
    context=json.loads(observed[0])
    assert context=={'title':task['objective'],'criteria':task['criteria'],'target_paths':['a.py']}
    assert len(observed[0].encode())<8192
    assert tasks.current(tasks.load(tools.root))['plan']['unit']['criteria']==task['plan']['unit']['criteria']
    assert recorded[0]['criteria']==task['plan']['unit']['criteria']
    assert 'criteria' not in json.loads(recorded[-1]['planner_advice'])['tasks'][0]
    assert json.loads(recorded[-1]['planner_advice'])['sources']==payload['payload']['sources']


@pytest.mark.parametrize('requested', ['name','paths'])
def test_verification_only_can_run_saved_plan_tests_in_private_scratch(host,monkeypatch,requested):
    tools,execution,project,seen=host
    intake(host);task=planned(host)
    (project/'tests').mkdir();(project/'tests/test_graph.py').write_text('def test_graph(): pass\n')
    value=tasks.load(tools.root)
    tasks.current(value)['plan']['unit']['test_paths']=['tests/test_graph.py']
    tasks.save(tools.root,value)
    commands=[]
    monkeypatch.setattr(work_units,'run_checks',lambda tree,argv:(commands.append((tree,argv)) or ('green','passed')))
    args={'names':['pytest-tests']}
    if requested=='paths':
        refused=tools.execute(effect('check',{'names':['tests/test_graph.py']}),execution)
        assert refused.status=='refused' and commands==[]
        output=json.loads(refused.output_json)
        assert output['available_checks']['pytest-tests']['argv'][-1]=='tests/test_graph.py'
        args=output['required_action']['arguments']
    result=tools.execute(effect('check',args),execution)
    assert result.status=='succeeded' and json.loads(result.output_json)['status']=='green'
    tree,argv=commands[0];command=argv['pytest-tests']
    assert 'tests/test_graph.py' in command and command[-2]=='--basetemp'
    assert Path(command[-1]).parent==tree.parent and not Path(command[-1]).is_relative_to(project)
    assert not seen and (project/'a.py').read_text()=='value = 1\n'


def test_check_path_retry_never_broadens_a_subset_or_arbitrary_command(host,monkeypatch):
    tools,execution,project,_=host
    intake(host);planned(host)
    (project/'tests').mkdir()
    for path in ['test_first.py','test_second.py']:
        (project/'tests'/path).write_text('def test_value(): pass\n')
    value=tasks.load(tools.root)
    tasks.current(value)['plan']['unit']['test_paths']=['tests/test_first.py','tests/test_second.py']
    tasks.save(tools.root,value)
    monkeypatch.setattr(work_units,'run_checks',lambda *a:pytest.fail('A refused check must not run'))
    for names in [['tests/test_first.py'],['python -m pytest'],[{}]]:
        result=tools.execute(effect('check',{'names':names}),execution)
        assert result.status=='refused'
        output=json.loads(result.output_json)
        assert 'pytest-tests' in output['available_checks'] and 'required_action' not in output


def user_reply(host, text):
    from compute_plane.native_journal import NativeJournal
    tools = host[0]
    path = tools.root/'journal.jsonl'
    journal = NativeJournal(path)
    if not path.exists(): journal.append({'kind': 'session_started', 'session_id': tools.session_id})
    journal.append({'kind': 'user_message', 'message_id': 'user-'+str(len(journal.snapshot().records)), 'text': text})
    tools.invocation_id = 'turn-2'


@pytest.mark.parametrize('name', ['edit', 'verify', 'terminal_run', 'apply_patch', 'unregistered_effect'])
def test_coordinator_cannot_bypass_roles_even_with_forged_effect(host, name):
    tools, execution, root, seen = host
    assert name not in tools.names
    assert tools.execute(effect(name, {}), execution).status == 'refused'
    assert not seen and (root/'a.py').read_text() == 'value = 1\n'


@pytest.mark.parametrize('topic', ['workflow', 'permissions', 'backlog'])
def test_product_contract_is_local_read_only_and_distinct_from_project(host, topic):
    tools, execution, root, seen = host
    tools.state['config']['writable'] = {}
    result = tools.execute(effect('product_help', {'topic': topic}), execution)
    assert result.status == 'succeeded'
    value = json.loads(result.output_json)
    assert value['product'] == 'tare.tools' and value['version'] and value['source']
    assert not seen and (root/'a.py').read_text() == 'value = 1\n'
    assert not (tools.root/'conversation-tasks.json').exists()  # explanations start no Work
    assert 'edit' not in tools.names and tools.state['config']['writable'] == {}
    if topic == 'backlog':
        from graph_backlog.validation import validate_work_graph
        assert validate_work_graph(value['starter'])['status'] == 'PASS'
        assert value['starter']['nodes'] == [] and value['graph'] is None
        assert 'does not automatically create' in value['plan']
        assert 'does not link it automatically' in value['binding']


def test_product_contract_rejects_unknown_topic_without_project_access(host):
    tools, execution, _, seen = host
    assert tools.execute(effect('product_help', {'topic': 'arbitrary/files'}), execution).status == 'refused'
    assert not seen


def test_nonempty_backlog_contract_is_owner_validated_and_stable_without_project_access(host):
    from graph_backlog.validation import validate_work_graph
    tools, execution, _, seen = host
    value = json.loads(tools.execute(effect('product_help', {'topic':'backlog'}), execution).output_json)
    contract = value['authoring']
    assert contract == tasks.backlog_authoring_contract()
    example = contract['example']
    assert validate_work_graph(example)['status'] == 'PASS'
    assert example['nodes'] and example['sources'] and not seen
    assert example['nodes'][0]['completion']['status'] == 'NOT_DONE'
    assert example['nodes'][0]['staleness_state'] == 'FRESH'
    assert next(iter(example['sources'].values()))['recoverability'] == 'RECOVERABLE_REFERENCE'
    assert 'NOT_DONE' in contract['completion_status'] and 'FRESH' in contract['staleness_state']
    example['nodes'][0]['completion']['status'] = 'no'
    assert validate_work_graph(example)['status'] == 'FAIL'
    assert tasks.backlog_authoring_contract()['example']['nodes'][0]['completion']['status'] == 'NOT_DONE'


def test_legacy_failed_graph_reuses_goal_patch_and_diagnostics_but_same_contract_still_stops(host, monkeypatch):
    tools, execution, _, _ = host
    intake(host); planned(host)
    value = tasks.load(tools.root); task = tasks.current(value)
    artifact = Path(task['plan']['artifact']); data = json.loads(artifact.read_bytes())
    data['payload']['tasks'][0].update(target_paths=['work-graph.json'], verifiers=[tasks.BACKLOG_CHECK])
    save_json(artifact, data)
    tasks.task_plan(tools.root, tools.state['config'], task, {'artifact':str(artifact)})
    tools.state['config']['writable'] = {'repo':['work-graph.json']}
    job = tools.root/'units/legacy'; job.mkdir(parents=True)
    patch = job/'patch-1.diff'; patch.write_text('retained unapplied candidate changes')
    receipt = job/'receipt.json'
    failed = {'verdict':'NOT_VERIFIED','checks_after':"invalid status 'no'",'patch':str(patch)}
    save_json(receipt, {'result':failed})
    task['delivery'] = {'verdict':'NOT_VERIFIED','receipt':str(receipt)}
    tasks.save(tools.root, value)
    for _ in range(2): work_units.record(tools.root, task['plan']['unit'], failed)
    original = deepcopy(task['plan']); calls=[]
    def execute(custody, state, args, *a, **kw):
        unit = work_units.read_ledger(custody)['plan']['units'][0]
        assert unit['verification_feedback'] == "invalid status 'no'"
        assert unit['previous_candidate_changes'] == patch.read_text()
        assert unit['verification_contract']['example']['nodes']
        calls.append(unit['verification_contract_digest'])
        result = {**failed, 'receipt':str(receipt)}
        work_units.record(custody, unit, result)
        return result
    monkeypatch.setattr(work_units, 'run_unit', execute)
    for key in ('first','second'):
        result = tools.execute(effect('implement', {'note':'Correct the validation failure.'}, key), execution)
        assert result.status == 'succeeded', result.output_json
    stale=tasks.load(tools.root); tasks.current(stale)['stage']='IMPLEMENTING'
    tasks.save(tools.root,stale)
    stopped = tools.execute(effect('implement', {'note':'Try a differently worded note.'}, 'third'), execution)
    assert stopped.status == 'refused' and 'same host contract' in stopped.output_json
    assert len(calls)==2 and calls[0]==calls[1]
    current = tasks.current(tasks.load(tools.root))
    assert current['plan'] == original and current['stage'] == 'PENDING'
    from relay.work_forms import record_plan
    renamed = {**work_units.read_ledger(tools.root)['plan']['units'][0], 'key':'renamed', 'verifiers':[]}
    assert record_plan(tools.root, tools.state['config'], {'units':[renamed], 'criteria':task['criteria']})['status']=='PLANNED'
    with pytest.raises(ValueError, match='same host contract'):
        work_units.check_failures(tools.root, renamed, conversation=True)


@pytest.mark.parametrize('kind', ['checks', 'audit'])
@pytest.mark.parametrize('same_key', [False, True])
def test_exhausted_scope_blocks_without_changing_ledger(tmp_path, kind, same_key):
    unit = {'key': 'blocked' if same_key else 'fresh', 'target_paths': ['a.py'],
            'verification_contract_digest': 'current'}
    old = {**unit, 'key': 'blocked'}
    result = {'verdict': 'NOT_VERIFIED'} if kind == 'checks' else {
        'verdict': 'NOT_REVIEWED', 'reason': work_units.AUDIT_REFUSED}
    count = work_units.MAX_FAILURES if kind == 'checks' else work_units.MAX_REFUSALS
    for _ in range(count):
        work_units.record(tmp_path, old, result)
    before = (tmp_path/work_units.LEDGER).read_bytes()
    if not same_key:
        assert unit['key'] not in work_units.read_ledger(tmp_path)['units']
    with pytest.raises(ValueError) as exc:
        work_units.check_failures(tmp_path, unit, conversation=True)
    if same_key:
        assert ('same host contract' if kind == 'checks' else 'independent audit refused') in str(exc.value)
    else:
        assert all(text in str(exc.value) for text in ('blocked', 'a.py', 'plain words', 'No implementer started'))
    assert (tmp_path/work_units.LEDGER).read_bytes() == before


@pytest.mark.parametrize('kind', ['checks', 'audit'])
@pytest.mark.parametrize('case', ['disjoint', 'applied', 'below', 'contract', 'host_refusal',
                                  'interrupted', 'operator_retry', 'nonconversation_retry'])
def test_scope_guard_preserves_exclusions_and_retry_contract(tmp_path, kind, case):
    unit = {'key': 'fresh', 'target_paths': ['a.py'], 'verification_contract_digest': 'current'}
    old = {**unit, 'key': 'old', 'target_paths': ['b.py'] if case == 'disjoint' else ['a.py']}
    if case == 'contract':
        old['verification_contract_digest'] = 'previous'
    result = {'verdict': 'NOT_VERIFIED'} if kind == 'checks' else {
        'verdict': 'NOT_REVIEWED', 'reason': work_units.AUDIT_REFUSED}
    if case == 'host_refusal':
        result = {'verdict': 'NOT_REVIEWED', 'reason': 'application refused'}
    count = work_units.MAX_FAILURES if kind == 'checks' else work_units.MAX_REFUSALS
    for _ in range(count - (case == 'below')):
        work_units.record(tmp_path, old, result)
    if case == 'applied':
        ledger = work_units.read_ledger(tmp_path)
        ledger['units']['old']['state'] = 'APPLIED'
        save_json(tmp_path/work_units.LEDGER, ledger)
    if case == 'interrupted':
        work_units.record(tmp_path, old, {'verdict': 'INCONCLUSIVE'})
    before = (tmp_path/work_units.LEDGER).read_bytes()
    blocked = case == 'nonconversation_retry' or (kind == 'audit' and case == 'contract') or (
        kind == 'checks' and case == 'interrupted')
    kwargs = {'conversation': case != 'nonconversation_retry',
              'operator_retry': case in {'operator_retry', 'nonconversation_retry'}}
    if blocked:
        with pytest.raises(ValueError, match='/retry_delivery' if case == 'nonconversation_retry' else 'plain words'):
            work_units.check_failures(tmp_path, unit, **kwargs)
    else:
        work_units.check_failures(tmp_path, unit, **kwargs)
    assert (tmp_path/work_units.LEDGER).read_bytes() == before


def test_scope_guard_checks_each_history_without_aggregation(tmp_path):
    unit = {'key': 'fresh', 'target_paths': ['a.py']}
    for key in ('first', 'second'):
        work_units.record(tmp_path, {**unit, 'key': key}, {'verdict': 'NOT_VERIFIED'})
    work_units.check_failures(tmp_path, unit)
    work_units.record(tmp_path, {**unit, 'key': 'second'}, {'verdict': 'NOT_VERIFIED'})
    before = (tmp_path/work_units.LEDGER).read_bytes()
    with pytest.raises(ValueError, match='blocked unit second'):
        work_units.check_failures(tmp_path, unit)
    assert (tmp_path/work_units.LEDGER).read_bytes() == before


@pytest.mark.parametrize('kind', ['checks', 'audit'])
@pytest.mark.parametrize('overlap', [False, True])
def test_new_demand_scope_guard_precedes_authorization_and_dispatch(host, monkeypatch, kind, overlap):
    from relay import work_forms
    tools, execution, *_ = host
    first = intake(host)['task']
    old = {'key': first['id'], 'target_paths': ['a.py'] if overlap else ['b.py']}
    result = {'verdict': 'NOT_VERIFIED'} if kind == 'checks' else {
        'verdict': 'NOT_REVIEWED', 'reason': work_units.AUDIT_REFUSED}
    demand = tools.execute(effect('demand', {'intent': 'deliver', 'relation': 'new',
        'objective': 'Different words, same scope.' if overlap else first['objective'],
        'criteria': first['criteria']}, 'new-demand'), execution)
    assert demand.status == 'succeeded', demand.output_json
    fresh = json.loads(demand.output_json)['task']
    assert fresh['id'] != first['id']
    planned(host)
    # A blocked sibling can appear after this fresh plan was recorded. Rehydration
    # preserves its empty current-key entry; only the cross-key precheck protects it.
    for _ in range(work_units.MAX_FAILURES if kind == 'checks' else work_units.MAX_REFUSALS):
        work_units.record(tools.root, old, result)
    snapshots = []
    calls = []
    def authorize(root, state, *a, **kw):
        calls.append('authorize')
        return state['config']
    original = work_forms.record_plan
    def record(*a, **kw):
        calls.append('record_plan')
        recorded = original(*a, **kw)
        snapshots.append((tools.root/work_units.LEDGER).read_bytes())
        return recorded
    def run(*a, **kw):
        calls.append('run_unit')
        return {'verdict': 'INCONCLUSIVE', 'applied': False, 'reason': 'fixture stopped', 'files': []}
    monkeypatch.setattr('relay.conversation_permissions.authorize', authorize)
    monkeypatch.setattr(work_forms, 'record_plan', record)
    monkeypatch.setattr(work_units, 'run_unit', run)
    answer = tools.execute(effect('implement', {}, 'fresh-demand'), execution)
    if overlap:
        assert answer.status == 'refused', answer.output_json
        assert 'plain words' in answer.output_json and 'a.py' in answer.output_json
        # Immutable-artifact rehydration records the plan before the precheck;
        # authorization and dispatch-time record_plan must never be reached.
        assert calls == ['record_plan']
        assert (tools.root/work_units.LEDGER).read_bytes() == snapshots[0]
    else:
        assert answer.status == 'succeeded', answer.output_json
        assert calls == ['record_plan', 'authorize', 'record_plan', 'run_unit']


def test_saved_coordinator_does_not_advertise_or_request_unusable_folder_access(host):
    tools, execution, _, seen = host
    inner = tools.tools
    inner.names = (*inner.names, 'request_folder_access', *tasks.FOLDER_READS)
    inner.tool_specs_json = encode({n:{} for n in inner.names})
    old = tasks.ConversationTools(inner, tools.root, tools.state, 'old')
    assert not tasks.FOLDER_READS.intersection(old.names)
    result = old.execute(effect('request_folder_access', {'path':'C:/outside','reason':'Read schema.'}), execution)
    assert result.status == 'refused' and 'no permission request was created' in result.output_json
    assert not seen
    fresh = tasks.ConversationTools(inner, tools.root, {**tools.state,'coordinator_folder_reads':True}, 'new')
    assert tasks.FOLDER_READS <= set(fresh.names) and 'edit' not in fresh.names


def test_host_backlog_check_accepts_valid_graph_and_rejects_invalid_without_project_imports(host):
    tools, _, root, _ = host
    unit = {'repository': 'repo', 'target_paths': ['work-graph.json'], 'verifiers': [tasks.BACKLOG_CHECK]}
    argv = tasks.host_checks(tools.state['config'], unit)[tasks.BACKLOG_CHECK]['argv']
    (root/'json.py').write_text("raise AssertionError('Project code was imported')\n")
    graph = root/'work-graph.json'
    graph.write_text(json.dumps(tasks.product_help(tools.state, 'backlog')['starter']))
    valid = subprocess.run(argv, cwd=root, capture_output=True)
    assert valid.returncode == 0, valid.stderr.decode()
    graph.write_text('{"nodes": []}')
    invalid = subprocess.run(argv, cwd=root, capture_output=True)
    assert invalid.returncode != 0 and b'FAIL' in invalid.stdout
    assert tools.state['config']['verifiers'] == {}


def test_coordinator_gets_audit_failure_facts_without_unreadable_host_custody_paths(host):
    secret = str(host[0].root/'units/private/receipt.json')
    value = {'verdict':'NOT_REVIEWED','receipt':secret,'patch':secret,
        'candidate':{'tree':secret},'files':['work-graph.json'],
        'audit':{'verdict':'INCONCLUSIVE','reason':'DEADLINE','session':secret}}
    output = tasks.delivery_view(value)
    assert json.dumps(secret) not in json.dumps(output) and value['receipt'] == secret
    assert output['candidate_preserved'] and 'not project files' in output['files_scope']
    assert output['reason']=='Independent audit incomplete: DEADLINE'
    assert 'stop' in output['next']


def test_graph_file_delivery_does_not_claim_a_project_binding():
    value = {'verdict':'VERIFIED', 'applied':True, 'files':['work-graph.json']}
    unit = {'verifiers':[tasks.BACKLOG_CHECK]}
    output = tasks.delivery_view(value, config={}, unit=unit)
    assert output['applied'] and output['project_graph_binding']['changed'] is False
    assert output['project_graph_binding']['current_conversation'] is None
    assert 'No binding was configured' in output['project_graph_binding']['new_conversations']
    existing = {'repository':'project','path':'C:/project/previous.json'}
    output = tasks.delivery_view(value, config={'project_graph':existing}, unit=unit)
    assert output['project_graph_binding']['current_conversation'] == existing
    assert 'project_graph_binding' not in tasks.delivery_view(value)


def test_planner_graph_unit_compiles_a_host_owned_check_without_configuring_project_commands(host):
    tools, _, _, _ = host
    intake(host); planned(host)
    task = tasks.current(tasks.load(tools.root))
    artifact = Path(task['plan']['artifact'])
    value = json.loads(artifact.read_bytes())
    value['payload']['tasks'][0].update(target_paths=['work-graph.json'], verifiers=[tasks.BACKLOG_CHECK])
    save_json(artifact, value)
    tasks.task_plan(tools.root, tools.state['config'], task, {'artifact': str(artifact)})
    assert task['plan']['unit']['verifiers'] == [tasks.BACKLOG_CHECK]
    assert tools.state['config']['verifiers'] == {}  # coordinator stays frozen/read-only


def test_goal_is_durable_and_intake_replays_without_another_demand(host):
    first = intake(host)
    assert intake(host)['reused']
    assert len(tasks.load(host[0].root)['tasks']) == 1
    resumed = tasks.ConversationTools(host[0].tools, host[0].root, host[0].state, 'turn-2')
    host = (resumed, *host[1:])
    continued = intake(host, relation='continue')
    assert continued['task']['id'] == first['task']['id']
    assert continued['task']['criteria'] == ['value equals two']


def test_progress_question_preserves_delivery_and_blocks_implementation_this_turn(host):
    task = intake(host)['task']; planned(host)
    tools, execution, *_ = host
    answer = intake(host, intent='answer', relation='continue')
    assert answer['current']['id'] == task['id'] and answer['current']['intent'] == 'deliver'
    assert tools.execute(effect('implement', {}), execution).status == 'refused'
    assert tasks.current(tasks.load(tools.root))['plan']
    assert tools.execute(effect('demand', {'intent': 'deliver', 'relation': 'continue'}, 'bypass-answer'), execution).status == 'refused'


def test_plan_then_user_implementation_reuses_the_same_planner_artifact(host):
    intake(host, intent='plan'); task = planned(host)
    before = deepcopy(task['plan'])
    assert host[0].execute(effect('implement', {}), host[1]).status == 'refused'
    user_reply(host, 'Now implement the saved plan.')
    continued = intake(host, intent='deliver', relation='continue', objective='Implement the previously planned change.')['task']
    assert continued['plan'] == before


def test_coordinator_cannot_promote_plan_only_intent_without_another_user_input(host):
    intake(host, intent='plan'); planned(host)
    result = host[0].execute(effect('demand', {'intent': 'deliver', 'relation': 'continue'}, 'promote'), host[1])
    assert result.status == 'refused'
    assert tasks.current(tasks.load(host[0].root))['intent'] == 'plan'
    result = host[0].execute(effect('demand', {'intent': 'deliver', 'relation': 'new',
        'objective': 'The same change under another name.'}, 'bypass'), host[1])
    assert result.status == 'refused' and len(tasks.load(host[0].root)['tasks']) == 1


def test_resuming_without_a_new_user_message_cannot_expand_plan_only_authority(host):
    intake(host, intent='plan'); planned(host)
    host[0].invocation_id = 'resume-same-user-input'
    result = host[0].execute(effect('demand', {'intent': 'deliver', 'relation': 'continue'}, 'resumed'), host[1])
    assert result.status == 'refused'


def test_pending_question_cannot_be_silently_cleared_without_a_user_reply(host):
    intake(host); planned(host)
    intake(host, relation='continue', question='Current page or all results?')
    result = host[0].execute(effect('demand', {'intent': 'deliver', 'relation': 'continue'}, 'not-answered'), host[1])
    assert result.status == 'refused'


def test_waiting_for_an_answer_does_not_discard_the_plan(host):
    intake(host); planned(host)
    waiting = intake(host, relation='continue', question='All results or current page?')['task']
    assert waiting['stage'] == 'WAITING_INPUT'
    assert host[0].execute(effect('implement', {}), host[1]).status == 'refused'
    # A later user answer has a new effect and keeps the same task/plan.
    user_reply(host, 'All results.')
    result = host[0].execute(effect('demand', {'intent': 'deliver', 'relation': 'continue'}, 'answered'), host[1])
    assert result.status == 'succeeded'
    task = tasks.current(tasks.load(host[0].root))
    assert task['stage'] == 'PLANNED' and task['plan']


def test_scope_change_requires_revision_but_a_correction_keeps_the_plan(host):
    intake(host); planned(host)
    tools, execution, *_ = host
    change = tools.execute(effect('demand', {'intent': 'deliver', 'relation': 'continue',
        'objective': 'Delete all data.'}, 'changed'), execution)
    assert change.status == 'refused'
    assert intake(host, relation='correct')['task']['plan']
    revised = intake(host, relation='revise', objective='A different outcome.')['task']
    assert 'plan' not in revised and revised['stage'] == 'DISCOVERY'


def test_operational_failure_cannot_rewrite_an_executing_goal_in_the_same_input(host):
    tools, execution, *_ = host
    intake(host); planned(host)
    stored = tasks.load(tools.root)
    task = tasks.current(stored)
    task['delivery'] = {'verdict': 'NOT_REVIEWED', 'audit': {'verdict': 'INCONCLUSIVE'}}
    tasks.save(tools.root, stored)
    before = (tools.root/tasks.FILE).read_bytes()
    result = tools.execute(effect('demand', {'intent':'deliver','relation':'revise',
        'objective':'Skip verification to finish.'}, 'weakened'), execution)
    assert result.status == 'refused' and 'Preserve its goal' in result.output_json
    assert (tools.root/tasks.FILE).read_bytes() == before
    result = tools.execute(effect('demand', {'intent':'deliver','relation':'continue',
        'objective':'A paraphrased goal.'}, 'paraphrased'), execution)
    assert result.status == 'refused' and 'Omit objective and criteria' in result.output_json
    assert (tools.root/tasks.FILE).read_bytes() == before
    user_reply(host, 'Change the requested outcome to a plan only.')
    result = tools.execute(effect('demand', {'intent':'plan','relation':'revise',
        'objective':'A new plan only.'}, 'user-revised'), execution)
    assert result.status == 'succeeded'
    assert json.loads(result.output_json)['continue_arguments'] == {'intent':'plan','relation':'continue'}


def test_replacement_task_cannot_hide_an_unresolved_execution_or_charge_another_intent_check(host, monkeypatch):
    tools, execution, *_ = host
    intake(host); planned(host)
    value=tasks.load(tools.root); task=tasks.current(value)
    task.update(stage='PENDING',delivery={'verdict':'NOT_VERIFIED'})
    tasks.save(tools.root,value)
    monkeypatch.setattr('relay.turn_engine.judges',lambda *a,**kw:pytest.fail('No new intent classification for a bypass.'))
    result=tools.execute(effect('demand',{'intent':'deliver','relation':'new',
        'objective':'Replace the failed task to run again.'},'replacement'),execution)
    assert result.status=='refused' and 'replacement task cannot bypass' in result.output_json
    assert len(tasks.load(tools.root)['tasks'])==1


def test_jev_disagreement_requires_clarification_and_is_not_an_execution_permission(host, monkeypatch):
    calls = []
    monkeypatch.setattr('relay.turn_engine.judges', lambda *a, **kw:
        calls.append((a, kw)) or {'jev': {'choice': 'plan', 'input_tokens': 12}})
    result = intake(host)
    assert result['status'] == 'NEEDS_CLARIFICATION'
    assert tasks.current(tasks.load(host[0].root)) is None
    assert intake(host)['status'] == 'NEEDS_CLARIFICATION' and len(calls) == 1
    assert calls[0][1] == {'local': False, 'timeout': 2, 'model': 'jev-1.14.0'}
    assert host[0].execute(effect('implement', {}), host[1]).status == 'refused'


def test_existing_cached_jev_disagreement_is_preserved_without_resampling(host, monkeypatch):
    tools, execution, _, seen = host
    value = tasks.load(tools.root)
    value['assessment'] = {'boundary': tools.input_id(), 'result': {
        'status': 'NEEDS_CLARIFICATION', 'assessment': {'choice': 'investigate',
            'probabilities': {'answer': .05, 'deliver': .42, 'investigate': .49,
                              'plan': .04, 'uncertain': 0.0}}}}
    tasks.save(tools.root, value)
    monkeypatch.setattr('relay.turn_engine.judges', lambda *a, **kw: pytest.fail('Reuse the recorded scores.'))
    result = intake(host)
    assert result['status'] == 'NEEDS_CLARIFICATION'
    assert tasks.current(tasks.load(tools.root)) is None
    assert result['assessment']['probabilities']['investigate'] == .49
    assert tools.execute(effect('implement', {}), execution).status == 'refused'
    assert seen == [] and not (tools.root/'units').exists()


def test_decisive_jev_plan_only_disagreement_still_requires_clarification(host, monkeypatch):
    monkeypatch.setattr('relay.turn_engine.judges', lambda *a, **kw: {'jev': {
        'choice': 'plan', 'probabilities': {'answer': .01, 'deliver': .02, 'investigate': .03,
                                         'plan': .93, 'uncertain': .01}}})
    assert intake(host)['status'] == 'NEEDS_CLARIFICATION'
    assert tasks.current(tasks.load(host[0].root)) is None
    assert host[0].execute(effect('implement', {}), host[1]).status == 'refused'


@pytest.mark.parametrize('intent,choice', [('plan','investigate'),('investigate','plan'),
                                        ('plan','answer'),('investigate','answer')])
def test_jev_read_only_ambiguity_does_not_block_advice_or_authorize_edits(host, monkeypatch, intent, choice):
    calls = []
    monkeypatch.setattr('relay.turn_engine.judges', lambda *a, **kw:
        calls.append((a, kw)) or {'jev': {'choice': choice, 'input_tokens': 12}})
    intake(host, intent=intent)
    tools, execution, *_ = host
    task = tasks.current(tasks.load(tools.root))
    assert task['intent'] == intent and task['interpretation']['status'] == 'UNDERSTOOD'
    assert task['interpretation']['assessment']['choice'] == choice
    assert tools.execute(effect('implement', {}), execution).status == 'refused'
    result = tools.execute(effect('demand', {'intent':'deliver','relation':'new','objective':'Escalate now.'},
                                  'escalate'), execution)
    assert result.status == 'refused' and len(calls) == 1


def test_cached_read_only_disagreement_can_continue_without_paying_for_another_classification(host, monkeypatch):
    tools, *_ = host
    value=tasks.load(tools.root)
    value['assessment']={'boundary':tools.input_id(),'result':{'status':'NEEDS_CLARIFICATION',
        'assessment':{'choice':'investigate'},'next':'Old clarification hint'}}
    tasks.save(tools.root,value)
    monkeypatch.setattr('relay.turn_engine.judges',lambda *a,**kw:pytest.fail('Reuse the recorded assessment.'))
    intake(host,intent='plan')
    task=tasks.current(tasks.load(tools.root))
    assert task['intent']=='plan' and 'next' not in task['interpretation']


def test_intent_check_receives_bounded_user_context_for_a_short_reply(host, monkeypatch):
    tools, *_ = host
    tools.state['instruction']='Compare small alternatives; only a plan, no edits.'
    user_reply(host,'Continue the comparison.')
    user_reply(host,'Both currencies.')
    requests=[]
    monkeypatch.setattr('relay.turn_engine.judges',lambda request,**kw:
        requests.append(request) or {'jev':{'choice':'plan'}})
    intake(host,intent='plan')
    assert len(requests)==1 and len(requests[0]['state'])<=6000
    assert all(text in requests[0]['state'] for text in [
        'only a plan, no edits','Continue the comparison.','Both currencies.'])
    assert tools.execute(effect('implement',{}),host[1]).status=='refused'


def test_intent_packet_separates_user_request_and_folder_consent_without_losing_text(host, monkeypatch):
    from relay.console_entry import ASSISTANT_CONTEXT
    tools, *_ = host
    original = 'Implemente um relatório versionado com os recibos existentes. Preserve pendências e fontes.'
    tools.state['instruction'] = ASSISTANT_CONTEXT+original
    note = '[tare] The user granted read access to C:\\projects\\_archives (saved for this project).'
    user_reply(host, 'sim\n\n'+note)
    captured=[]
    monkeypatch.setattr('relay.turn_engine.judges', lambda request, **kw:
        captured.append((request,kw)) or {'jev': {'choice':'deliver','model':'jev-1.14.0'}})
    assert intake(host)['task']['intent']=='deliver'
    request,options=captured[0]
    context=json.loads(request['state'])
    assert context['original_user_request']==original and context['recorded_goal'] is None
    assert context['recent_user_messages']==[{'text':'sim','runtime_note':note}]
    assert 'workflow stage' in request['question'] and 'reports' in request['options']['deliver']
    assert options=={'local':False,'timeout':2,'model':'jev-1.14.0'}
    assert tools.execute(effect('implement',{}),host[1]).status=='refused'


def test_oversized_intent_context_is_not_tail_truncated_or_sent(host, monkeypatch):
    tools, *_ = host
    tools.state['instruction']='Only a plan, no edits. '+('context '*2000)
    monkeypatch.setattr('relay.turn_engine.judges',lambda *a,**kw:pytest.fail('Do not send partial or oversized context.'))
    result=intake(host,intent='plan')
    assert result['task']['intent']=='plan'
    assert result['task']['interpretation']['assessment']=={
        'source':'coordinator','failure':'ASSESSMENT_INPUT_OVERFLOW'}
    assert tools.execute(effect('implement',{}),host[1]).status=='refused'


def test_continuing_an_unrecorded_demand_returns_an_actionable_error_before_spending_quota(host, monkeypatch):
    monkeypatch.setattr('relay.turn_engine.judges',lambda *a,**kw:pytest.fail('Invalid intake cannot spend quota.'))
    result=host[0].execute(effect('demand',{'intent':'plan','relation':'continue'}),host[1])
    assert result.status=='refused' and 'relation=new' in result.output_json


def test_no_inline_scope_or_replaced_planner_artifact_can_execute(host, monkeypatch):
    intake(host)
    calls = []
    monkeypatch.setattr(work_units, 'run_unit', lambda *a, **kw: calls.append(a))
    tools, execution, *_ = host
    assert tools.execute(effect('implement', {}), execution).status == 'refused'
    task = planned(host)
    assert tools.execute(effect('implement', {'target_paths': ['a.py']}), execution).status == 'refused'
    Path(task['plan']['artifact']).write_text('{}')
    assert tools.execute(effect('implement', {}), execution).status == 'refused'
    assert not calls


@pytest.mark.parametrize('explicit', [False, True])
def test_runtime_conversation_offers_only_coordinator_capabilities(setup, monkeypatch, explicit):
    config, custody, root = setup
    if explicit:
        # The actual TUI CLI passes this when SpecGraph context and named
        # verifiers are disabled; do not manufacture an unavailable context tool.
        config['capabilities'] = ['read', 'list', 'search', 'edit']
    client = Client(steps=[('tare_deliver', {'summary': 'A read-only response.'})])
    result = native_agent.run_session(config, deployment(), custody, 'Explain a.py.',
        conversation=True, client=client, local_counter=client)
    assert result['status'] == 'PROPOSED'
    state = json.loads((custody/'native-agent.json').read_text())
    assert state['conversation_flow'] and 'session-planner' not in state['planning']['routes']
    assert state['planning']['other_providers']
    body = next(body for path, body in client.calls if path == '/chat/completions')
    names = {t['function']['name'] for t in body['tools']}
    assert {'demand', 'delegate_plan', 'implement'} <= names
    assert not {'edit', 'verify', 'command', 'terminal_run', 'plan'} & names
    assert (root/'a.py').read_text() == 'value = 1\n'


def test_resume_freezes_tools_and_guidance_instead_of_invalidating_provider_history(setup, monkeypatch):
    config, custody, root = setup
    client = Client(steps=[('tare_deliver', {'summary': 'First answer.'})])
    first = native_agent.run_session(config, deployment(), custody, 'Explain a.py.',
        conversation=True, client=client, local_counter=client)
    changed = deepcopy(tasks.SPEC)
    changed['demand']['description'] += ' Changed in a newer product build.'
    monkeypatch.setattr(tasks, 'SPEC', changed)
    monkeypatch.setattr(tasks, 'GUIDANCE', 'Different future guidance.')
    follow = Client(steps=[('tare_deliver', {'summary': 'The same conversation continues.'})])
    second = native_agent.run_session(None, None, custody, resume=True,
        follow_up='And the current value?', message_id='next', client=follow, local_counter=follow)
    assert second['status'] == 'PROPOSED' and second['profile_digest'] == first['profile_digest']
    before = next(b for p, b in client.calls if p == '/chat/completions')
    after = next(b for p, b in follow.calls if p == '/chat/completions')
    assert before['tools'] == after['tools']
    assert before['messages'][0] == after['messages'][0]
    assert 'Same conversation' not in (root/'a.py').read_text()


@pytest.mark.parametrize('kind', ['retry', 'folder'])
@pytest.mark.parametrize('choice,probability', [('unclear', .95), ('allow', .6), (None, None)])
def test_resumed_pending_consent_warns_coordinator_without_judging_host_notice(
        setup, monkeypatch, kind, choice, probability):
    from relay import consent_decisions as consent
    from test_consent_decisions import verdict
    config, custody, root = setup
    client = Client(steps=[('tare_deliver', {'summary': 'May I proceed?'})])
    first = native_agent.run_session(config, deployment(), custody, 'Explain a.py.',
        conversation=True, client=client, local_counter=client)
    question = 'Allow another attempt?' if kind == 'retry' else 'Allow reading this folder?'
    scope = ({'path': str(root.parent/'reference'), 'store': str(root.parent/'grants.json')}
             if kind == 'folder' else {'task': 'example'})
    key = consent.register(custody, first['session_id'], kind, scope, question=question)
    judge_question = ('Allow read access to the entire folder for this project? The grant is saved for this project on this machine. Path: ' + json.dumps(scope['path'])
                      if kind == 'folder' else question)
    before = consent.load(custody)['questions'][0]
    packets = []
    def judges(packet, **kwargs):
        packets.append(packet)
        return {'jev': verdict(choice, probability)} if choice else {}
    monkeypatch.setattr('relay.turn_engine.judges', judges)
    follow = Client(steps=[('tare_deliver', {'summary': 'Please clarify your choice.'})])
    second = native_agent.run_session(None, None, custody, resume=True,
        follow_up='sim, tenta de novo', message_id='ambiguous-reply', client=follow, local_counter=follow)
    assert second['status'] == 'PROPOSED'
    body = next(b for p, b in follow.calls if p == '/chat/completions')
    assert consent.REASK in json.dumps(body['messages'])
    after = next(q for q in consent.load(custody)['questions'] if q['id'] == key)
    assert after['status'] == 'WAITING'
    assert (after['boundary'], after['prefix']) == (before['boundary'], before['prefix'])
    assert after['used_messages'] == ['ambiguous-reply']
    count = len(packets)
    assert count > 0
    assert all(json.loads(p['state']) == {
        'question': judge_question, 'user_words': 'sim, tenta de novo'} for p in packets)
    assert consent.process(custody, first['session_id']) == []
    assert len(packets) == count


@pytest.mark.parametrize('verdict', ['ACCEPT', 'REVISE', 'INCONCLUSIVE', 'OUTSIDE', 'PROTECTED'])
def test_real_candidate_is_verified_then_audited_before_application(tmp_path, monkeypatch, verdict):
    protected=verdict=='PROTECTED'
    if protected:verdict='ACCEPT'
    root = tmp_path/'project'; root.mkdir()
    def git(*args):
        return subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True)
    git('init'); git('config', 'user.name', 'Fixture'); git('config', 'user.email', 'fixture@example.invalid')
    (root/'a.py').write_text('value = 1\n')
    git('add', 'a.py'); git('commit', '-m', 'fixture')
    custody = tmp_path/'session'; custody.mkdir()
    config = {'repositories': {'repo': str(root)}, 'writable': {'repo': ['a.py']},
        'verifiers': {'value': {'repository': 'repo', 'argv': [sys.executable, '-c',
            "from pathlib import Path; assert Path('a.py').read_text() == 'value = 2\\n'"]}}}
    state = {'config': config, 'deployment': deployment(), 'limits': {'max_model_calls': 30}, 'deadline_utc': None,
             'conversation_flow': True}
    if protected:
        from relay import command_protection
        config.update(conversation_flow=True,command_protection={'review':'auto'})
        state['conversation_flow']=True
        config['verifiers']['conversation-diff']={'repository':'repo','argv':['git','diff','--check','HEAD','--','a.py']}
        def check_port(control,custody,config,deployment,tree,unit,commands):
            assert all(Path(a[0]).is_absolute() for a in commands.values())
            def run(argv,**kwargs):
                assert argv in commands.values(), 'The suite and unit must use the same admitted paths.'
                return subprocess.run(argv,**kwargs)
            return run
        monkeypatch.setattr(command_protection,'conversation_verifier',check_port)
    if verdict=='OUTSIDE':
        config.update(writable={'repo':['.']},conversation_flow=True)
        state['conversation_flow']=True
    from relay.work_forms import record_plan
    record_plan(custody, config, {'units': [{'key': 'change', 'title': 'Change value', 'target_paths': ['a.py'],
        'criteria': ['value equals two'], 'verifiers': ['value']}], 'criteria': ['value equals two']})
    monkeypatch.setattr(work_units, 'routes', lambda *a, **kw: iter([('fixture', deployment())]))
    order = []
    def child(child_config, dep, session, instruction, **kwargs):
        order.append('implement')
        assert kwargs['delegated_leaf'] and child_config['role'] == 'implementer'
        if protected:assert 'conversation-diff' not in child_config['verifiers']
        Path(child_config['repositories']['repo'], 'a.py').write_text('value = 2\n')
        if verdict=='OUTSIDE':Path(child_config['repositories']['repo'],'unplanned.py').write_text('surprise=1\n')
        return {'status': 'PROPOSED', 'model_calls': 1}
    monkeypatch.setattr(native_agent, 'run_session', child)
    def review(tree, dep, unit, changed, checks):
        order.append('audit')
        assert (root/'a.py').read_text() == 'value = 1\n'
        assert (tree/'a.py').read_text() == 'value = 2\n' and 'a.py' in changed
        assert checks['after'] is not None
        return {'verdict': verdict}
    result = work_units.run_unit(custody, state, {'unit': 'change'}, lambda: False, review=review,
                                 control=control() if protected else None)
    if verdict=='OUTSIDE':
        assert order==['implement'] and result['verdict']=='NOT_VERIFIED'
        assert 'outside the saved plan' in result['reason'] and not result.get('applied')
        assert (root/'a.py').read_text()=='value = 1\n' and not (root/'unplanned.py').exists()
        return
    assert order == ['implement', 'audit']
    assert bool(result.get('applied')) is (verdict == 'ACCEPT')
    if verdict == 'ACCEPT':  # the summary follows this English result
        assert 'in the language of their request' in result['next']
    assert (root/'a.py').read_text() == ('value = 2\n' if verdict == 'ACCEPT' else 'value = 1\n')
    if verdict != 'ACCEPT': assert Path(result['patch']).is_file()
    if verdict == 'INCONCLUSIVE':
        def accepted(tree, *args):
            order.append('audit-only')
            assert (root/'a.py').read_text() == 'value = 1\n'
            return {'verdict': 'ACCEPT'}
        reused = work_units.run_unit(custody, state, {'unit': 'change'}, lambda: False, review=accepted,
                                     candidate={**result['candidate'], 'review_only': True})
        assert reused['applied'] and order == ['implement', 'audit', 'audit-only']
        assert work_units.unit_calls(custody) == 1
        assert (root/'a.py').read_text() == 'value = 2\n'
    if protected:assert config['verifiers']['conversation-diff']['argv'][0]=='git'


def test_failed_candidate_is_preserved_for_a_narrow_correction_and_audited_before_application(tmp_path, monkeypatch):
    from relay.work_forms import record_plan
    root=tmp_path/'project'; root.mkdir()
    (root/'a.py').write_text('value = 1\n')
    custody=tmp_path/'session'; custody.mkdir()
    config={'repositories':{'repo':str(root)},'writable':{'repo':['a.py']},'conversation_flow':True,
        'verifiers':{'value':{'repository':'repo','argv':[sys.executable,'-I','-c',
            "from pathlib import Path; assert Path('a.py').read_text() == 'value = 3\\n'"]}}}
    unit={'key':'repair','title':'Set three','target_paths':['a.py'],'criteria':['value equals three'],
        'verifiers':['value']}
    record_plan(custody,config,{'units':[unit],'criteria':unit['criteria']})
    state={'config':config,'conversation_flow':True,'deployment':deployment(),'limits':{'max_model_calls':40},'deadline_utc':None}
    monkeypatch.setattr(work_units,'routes',lambda *a,**kw:iter([('fixture',deployment())]))
    calls=[]; audits=[]
    def child(config,dep,session,instruction,**kw):
        path=Path(config['repositories']['repo'])/'a.py'
        assert path.read_text()==('value = 1\n' if not calls else 'value = 2\n')
        calls.append(True)
        path.write_text('value = 2\n' if len(calls)==1 else 'value = 3\n')
        return {'status':'PROPOSED','model_calls':1}
    monkeypatch.setattr(native_agent,'run_session',child)
    def audit(tree,*a):
        audits.append(True)
        assert (root/'a.py').read_text()=='value = 1\n'
        assert (tree/'a.py').read_text()=='value = 3\n'
        return {'verdict':'ACCEPT'}
    first=work_units.run_unit(custody,state,{'unit':'repair'},lambda:False,review=audit)
    assert first['verdict']=='NOT_VERIFIED' and first['candidate'] and not audits
    assert (root/'a.py').read_text()=='value = 1\n'
    second=work_units.run_unit(custody,state,{'unit':'repair'},lambda:False,review=audit,candidate=first['candidate'])
    assert second['applied'] and second['audit']['verdict']=='ACCEPT'
    assert len(calls)==2 and len(audits)==1 and (root/'a.py').read_text()=='value = 3\n'


def test_plain_folder_snapshot_has_no_partial_copy_on_oversize_and_never_creates_source_git(tmp_path, monkeypatch):
    from relay import conversation_workspace
    source, target = tmp_path/'plain', tmp_path/'candidate'
    source.mkdir(); (source/'note.txt').write_text('existing note')
    monkeypatch.setattr(conversation_workspace, 'MAX_BYTES', 2)
    with pytest.raises(ValueError, match='bounded candidate'):
        conversation_workspace.snapshot(source, target)
    assert not target.exists() and not (source/'.git').exists()
    monkeypatch.setattr(conversation_workspace, 'MAX_BYTES', 32000)
    base = conversation_workspace.snapshot(source, target)
    assert len(base) == 40 and (target/'note.txt').read_text() == 'existing note'
    assert not (source/'.git').exists()


@pytest.mark.parametrize('listed', [False, True])
def test_executable_plan_accepts_new_files_and_tests_but_not_whole_directories(host, listed):
    intake(host)
    tools, *_ = host
    artifact = tools.root/'planning'/'new'/'artifact.json'
    artifact.parent.mkdir(parents=True)
    plan = {'summary': 'Add a module and its tests.', 'steps': ['Create both files.'], 'criteria': ['New behavior works'],
        'sources': [], 'remaining': [], 'tasks': [{'key': 'new', 'title': 'New module', 'summary': 'Add it.',
            'cluster': 'test', 'priority': 'P2', 'target_paths': ['pkg/new.py', 'tests/test_new.py'],
            'criteria': ['New behavior works'], 'depends_on': [], 'test_paths': ['tests/test_new.py'] if listed else []}]}
    save_json(artifact, {'payload': plan})
    value = tasks.load(tools.root)
    tasks.task_plan(tools.root, tools.state['config'], tasks.current(value), {'artifact': str(artifact)})
    assert tasks.current(value)['plan']['unit']['test_paths'] == ['tests/test_new.py']
    commands = work_units.checks({**tools.state['config'], 'conversation_flow': True},
        tasks.current(value)['plan']['unit'], 'repo', Path(tools.state['config']['repositories']['repo']))
    assert 'pytest-tests' in commands and '-B' in commands['pytest-tests']
    assert 'tests/test_new.py' in commands['pytest-tests']
    plan['tasks'][0]['target_paths'] = ['.']; plan['tasks'][0]['test_paths'] = []
    save_json(artifact, {'payload': plan})
    with pytest.raises(ValueError, match='literal target files'):
        tasks.task_plan(tools.root, tools.state['config'], tasks.current(value), {'artifact': str(artifact)})


def test_promised_new_test_is_missing_before_red_when_broken_and_green_only_after_fix(host):
    tools, _, project, _ = host
    intake(host); task = planned(host)
    artifact = Path(task['plan']['artifact'])
    plan = json.loads(artifact.read_text(encoding='utf8'))
    plan['payload']['tasks'][0]['target_paths'].append('tests/test_new.py')
    save_json(artifact, plan)
    tasks.task_plan(tools.root, tools.state['config'], task, {'artifact': str(artifact)})
    commands = work_units.checks({**tools.state['config'], 'conversation_flow': True},
        task['plan']['unit'], 'repo', project)
    before, _ = work_units.run_checks(project, commands)
    assert before == 'red'  # The promised test does not yet exist.
    (project/'tests').mkdir()
    (project/'tests/test_new.py').write_text('def test_new(): assert False\n')
    after, _ = work_units.run_checks(project, commands)
    assert after == 'red'
    assert work_units.verdict(before, after, {'tests/test_new.py': b'broken'}, [], lambda: after)[0] == 'NOT_VERIFIED'
    (project/'tests/test_new.py').write_text('def test_new(): assert True\n')
    assert work_units.run_checks(project, commands)[0] == 'green'


def test_git_ignored_outputs_are_rejected_at_plan_admission_without_force_tracking(host):
    from relay.challenger import git
    from relay.conversation_workspace import ignored_targets
    tools, _, project, _ = host
    (project/'.gitignore').write_text('/private/\n')
    (project/'private').mkdir()
    (project/'private/tracked.md').write_text('Already versioned.\n')
    git(project, 'init')
    git(project, 'add', '.gitignore', 'a.py')
    git(project, 'add', '-f', 'private/tracked.md')
    git(project, '-c', 'user.name=T', '-c', 'user.email=t@localhost',
        '-c', 'commit.gpgsign=false', 'commit', '-m', 'Fixture')
    assert ignored_targets(project, ['private/new.json', 'private/tracked.md', 'report.md']) == ['private/new.json']
    intake(host); task = planned(host)
    original_plan = deepcopy(task['plan'])
    artifact = Path(task['plan']['artifact'])
    plan = json.loads(artifact.read_text(encoding='utf8'))
    plan['payload']['tasks'][0]['target_paths'].append('private/new.json')
    save_json(artifact, plan)
    with pytest.raises(ValueError, match='Git ignores these new outputs'):
        tasks.task_plan(tools.root, tools.state['config'], task, {'artifact': str(artifact)})
    assert task['plan'] == original_plan
    assert not (project/'private/new.json').exists()
    assert git(project, 'status', '--porcelain') == b''


@pytest.mark.parametrize('adopted,metered', [(False, False), (True, False), (True, True)])
def test_native_conversation_runs_all_roles_with_real_effects_and_typed_artifacts(setup, monkeypatch, adopted, metered):
    import hashlib
    from relay import planning_tools, conversation_review, turn_engine
    config, custody, root = setup
    profile = deployment()
    if metered:
        from dataclasses import asdict
        from test_native_spend import tariff
        profile.update(native_usage_tariff=asdict(tariff()), native_spend_policy={'max_amount': '0.02', 'currency': 'USD'})
    if adopted:
        from relay.project_admission import configure, preview
        config['project_admission'] = configure(root, preview(root), nested=True)
    before, after = b'value = 1\n', b'value = 2\n'
    source = {'repository': 'repo', 'path': 'a.py', 'sha256': hashlib.sha256(before).hexdigest()}
    plan = {'summary': 'Change the value.', 'steps': ['Set two and verify.'], 'criteria': ['value equals two'],
        'sources': [source], 'remaining': [], 'tasks': [{'key': 'change', 'title': 'Change value',
            'summary': 'Set two.', 'cluster': 'test', 'priority': 'P2', 'target_paths': ['a.py'],
            'criteria': ['value equals two'], 'depends_on': [], 'test_paths': [], 'verifiers': ['unit']}]}
    parent = Client(steps=[('demand', {'intent': 'deliver', 'relation': 'new', 'objective': 'Change value to two.',
        'criteria': ['value equals two']}), ('delegate_plan', {'instruction': 'Plan the requested change.'}),
        ('implement', {}), ('tare_deliver', {'summary': 'Changed, verified and independently reviewed.'})])
    clients = {
        'planner': Client(steps=[('read', {'repository': 'repo', 'path': 'a.py'}), ('tare_deliver', plan)]),
        'implementer': Client(steps=[('read', {'repository': 'repo', 'path': 'a.py'}),
            ('edit', {**source, 'search': 'value = 1', 'replace': 'value = 2'}),
            ('verify', {'name': 'unit'}), ('tare_deliver', {'summary': 'Changed a.py and verified unit.'})]),
        'auditor': Client(steps=[('read', {'repository': 'candidate', 'path': 'a.py'}),
            ('tare_deliver', {'summary': 'The value is two and the host check passed.', 'verdict': 'ACCEPT',
                'findings': [], 'remaining': [], 'sources': [{'repository': 'candidate', 'path': 'a.py',
                    'sha256': hashlib.sha256(after).hexdigest()}]})])}
    planner_profile = deepcopy(profile)
    params = json.loads(planner_profile['native_profile']['parameters_json'])
    params['chat_template_kwargs']['reasoning_effort'] = 'high'
    planner_profile['native_profile']['parameters_json'] = json.dumps(params)
    monkeypatch.setattr(planning_tools, 'snapshot', lambda *a, **kw:
        {'routes': {'planner': deepcopy(planner_profile)}, 'other_providers': False})
    monkeypatch.setattr(turn_engine, 'available', lambda name, *a:
        (deepcopy(planner_profile if name == 'planner' else profile), None))
    monkeypatch.setattr(work_units, 'routes', lambda *a, **kw: iter([('worker', deepcopy(profile))]))
    monkeypatch.setattr(turn_engine, 'pick_auditors', lambda *a, **kw: [('independent', deepcopy(profile))])
    actual = native_agent.run_session
    roles = []
    def child(child_config, dep, session, instruction, **kwargs):
        role = child_config['role']; roles.append(role)
        assert (root/'a.py').read_bytes() == before
        assert role != 'planner' or 'Change value to two.' in instruction
        client = clients[role]
        return actual(child_config, dep, session, instruction, client=client, local_counter=client, **kwargs)
    monkeypatch.setattr(native_agent, 'run_session', child)
    events = []
    result = actual(config, profile, custody, 'Change value to two.', conversation=True,
        limits={'max_model_calls': 40, 'max_seconds': 900}, client=parent, local_counter=parent, on_activity=events.append)
    assert result['status'] == 'PROPOSED', result
    task = tasks.current(tasks.load(custody))
    assert task['stage'] == 'DELIVERED', task
    assert roles == ['planner', 'implementer', 'auditor']
    started = [e['activity'] for e in events if e['activity']['operation'] == 'agent_run' and e['activity']['state'] == 'running']
    assert [row['actor']['role'] for row in started] == ['coordinator', 'planner', 'implementer', 'auditor']
    assert started[1]['actor']['reasoning'] == 'high' and all(row['target'] == 'Change value to two.' for row in started)
    assert (root/'a.py').read_bytes() == after and not (root/'.git').exists()
    assert conversation_review.committed(custody) == 2 and result['reviews']['model_calls'] == 2
    assert len(list((custody/'planning').glob('*/receipt.json'))) == 1
    if metered:
        from decimal import Decimal
        assert Decimal('0') < work_units.unit_spend(custody) < Decimal(result['spend']['auxiliary_committed_amount'])
        assert Decimal(result['spend']['committed_amount']) <= Decimal('0.02')
    from relay.native_review import review
    from relay.native_review_actions import undo_edit
    recorded = review(custody, project=root)
    assert len(recorded['edits']) == 1 and '+value = 2' in recorded['edits'][0]['diff']
    assert recorded['edits'][0]['origin'] == 'implementer'
    from relay.native_turn_review import review_turn
    turn = review_turn(custody, project=root, invocation_id=result['invocation_id'])
    assert turn['undo']['status'] == 'READY' and len(turn['files']) == 1, turn
    restored = undo_edit(custody, project=root, effect_id=recorded['edits'][0]['effect_id'])
    assert restored['status'] == 'UNDONE' and (root/'a.py').read_bytes() == before


def test_executable_projection_keeps_task_criteria_and_drops_historical_phase_limits(host):
    intake(host, intent='plan'); task = planned(host)
    artifact = Path(task['plan']['artifact'])
    value = json.loads(artifact.read_bytes())
    value['payload']['summary'] = 'Only plan; do not implement during this phase.'
    value['payload']['criteria'].append('No file edits in this plan-only response.')
    save_json(artifact, value)
    stored = tasks.load(host[0].root); task = tasks.current(stored)
    tasks.task_plan(host[0].root, host[0].state['config'], task, {'artifact': str(artifact)})
    assert task['plan']['unit']['criteria'] == ['value equals two']
    assert task['plan']['unit']['title'] == 'Change value'
    assert 'No file edits' in artifact.read_text()


def test_scope_preflight_avoids_spending_on_unwritable_plan(host, monkeypatch):
    intake(host); planned(host)
    host[0].state['config']['writable'] = {'repo': ['another.py']}
    calls = []
    monkeypatch.setattr(work_units, 'run_unit', lambda *a, **kw: calls.append(1))
    result = host[0].execute(effect('implement', {}), host[1])
    assert result.status == 'refused' and 'edit scope' in result.output_json and not calls


def test_correction_keeps_rejected_candidate_but_not_an_already_applied_patch(host):
    intake(host); planned(host)
    value = tasks.load(host[0].root)
    tasks.current(value)['delivery'] = {'applied': False, 'candidate': {'patch': 'retained'}}
    tasks.save(host[0].root, value)
    corrected = intake(host, relation='correct')['task']
    assert corrected['delivery']['candidate']['patch'] == 'retained'
    value = tasks.load(host[0].root); tasks.current(value)['delivery']['applied'] = True
    tasks.save(host[0].root, value)
    # Distinct tool effect, same still-valid plan.
    result = host[0].execute(effect('demand', {'intent': 'deliver', 'relation': 'correct'}, 'later-correction'), host[1])
    assert json.loads(result.output_json)['task']['delivery'] is None


@pytest.mark.parametrize('relation', ['correct', 'revise'])
@pytest.mark.parametrize('applied_candidate', [False, True])
def test_applied_delivery_blocks_older_candidates_after_transition(host, monkeypatch, relation, applied_candidate):
    tools, execution, *_ = host
    intake(host); original = planned(host)
    value = tasks.load(tools.root); task = tasks.current(value)
    stale = {'applied': False, 'files': ['obsolete.py'], 'candidate': {'patch': 'old'}}
    applied = {'applied': True, 'files': ['a.py']}
    if applied_candidate:
        applied['candidate'] = {'patch': 'already-applied'}
    task.update(prior_delivery=stale, replan_candidate=stale, delivery=applied)
    tasks.save(tools.root, value)
    user_reply(host, 'Repair the delivered change.')
    transitioned = intake(host, relation=relation)['task']
    stored = tasks.current(tasks.load(tools.root))
    assert stored['prior_delivery'] == applied
    assert 'replan_candidate' not in stored
    assert tasks.retained_candidate(stored) is None
    assert stored.get('delivery') is None
    assert transitioned['stage'] == ('PLANNED' if relation == 'correct' else 'DISCOVERY')
    assert stored.get('planning_revision', 0) == (1 if relation == 'correct' else 0)
    if relation == 'revise':
        value = tasks.load(tools.root)
        tasks.task_plan(tools.root, tools.state['config'], tasks.current(value),
                        {'artifact': original['plan']['artifact']})
        tasks.save(tools.root, value)
    calls = []
    def run(*args, **kwargs):
        calls.append(kwargs)
        return {'verdict': 'INCONCLUSIVE', 'applied': False, 'files': [], 'reason': 'fixture stopped'}
    monkeypatch.setattr(work_units, 'run_unit', run)
    result = tools.execute(effect('implement', {}, 'fresh-implementation'), execution)
    assert result.status == 'succeeded', result.output_json
    assert len(calls) == 1
    assert calls[0].get('candidate') is None


@pytest.mark.parametrize('extending', [False, True])
def test_revision_preserves_and_reuses_unapplied_candidate(host, monkeypatch, extending):
    tools, execution, *_ = host
    intake(host); original = planned(host)
    targets = {'repo': ['a.py']}
    candidate = {'patch': 'retained', 'sha256': 'a'*64, 'targets': targets,
                 'basis': tasks.source_state(tools.state['config'], targets)}
    previous = {'applied': False, 'verdict': 'NOT_VERIFIED', 'files': ['a.py'], 'candidate': candidate}
    value = tasks.load(tools.root); tasks.current(value)['delivery'] = previous
    tasks.save(tools.root, value)
    user_reply(host, 'Revise the requested outcome.')
    intake(host, relation='revise', **({} if extending else {'objective': 'A different outcome.'}))
    value = tasks.load(tools.root); task = tasks.current(value)
    assert task['prior_delivery'] == previous
    assert task.get('replan_candidate') == (previous if extending else None)
    assert tasks.retained_candidate(task) == previous
    assert task['stage'] == 'DISCOVERY'
    assert task.get('planning_revision', 0) == (0 if extending else 1)
    tasks.task_plan(tools.root, tools.state['config'], task, {'artifact': original['plan']['artifact']})
    tasks.save(tools.root, value)
    calls = []
    def run(*args, **kwargs):
        calls.append(kwargs)
        return {'verdict': 'INCONCLUSIVE', 'applied': False, 'files': [], 'reason': 'fixture stopped'}
    monkeypatch.setattr(work_units, 'run_unit', run)
    result = tools.execute(effect('implement', {}, 'reuse-implementation'), execution)
    assert result.status == 'succeeded', result.output_json
    assert len(calls) == 1
    assert calls[0]['candidate'] == {**candidate, 'review_only': False}


def test_verification_only_uses_named_host_checks_and_leaves_source_untouched(host):
    intake(host, intent='investigate')
    tools, execution, root, _ = host
    tools.state['config']['verifiers'] = {'value': {'repository': 'repo', 'argv': [sys.executable, '-B', '-c',
        "from pathlib import Path; assert Path('a.py').read_text() == 'value = 1\\n'"]}}
    refused = tools.execute(effect('check', {'names': ['arbitrary command']}, 'bad-check'), execution)
    assert refused.status == 'refused'
    result = tools.execute(effect('check', {'names': ['value']}, 'check'), execution)
    assert result.status == 'succeeded' and json.loads(result.output_json)['status'] == 'green'
    assert (root/'a.py').read_text() == 'value = 1\n' and not (root/'.git').exists()


def test_closed_check_needs_no_demand_and_does_not_admit_later_edits(host):
    tools, execution, root, _ = host
    tools.state['config']['verifiers'] = {'value': {'repository':'repo', 'argv':[sys.executable,'-I','-c','pass']}}
    result = tools.execute(effect('check', {'names':['value']}, 'standalone-check'), execution)
    assert result.status == 'succeeded' and json.loads(result.output_json)['status'] == 'green'
    assert tasks.load(tools.root)['tasks'] == []
    assert (root/'a.py').read_text() == 'value = 1\n'
    change = tools.execute(effect('demand', {'intent':'deliver','relation':'new',
        'objective':'Change value'}, 'after-verification'), execution)
    assert change.status == 'refused' and 'cannot expand' in change.output_json


def test_check_reuses_saved_graph_validator_without_replanning_or_binding(host):
    from graph_backlog.adapters import MarkdownAdapter
    tools, execution, root, _ = host
    intake(host); planned(host)
    value = tasks.load(tools.root); task = tasks.current(value)
    task['plan']['unit'].update(target_paths=['work-graph.json'], verifiers=[tasks.BACKLOG_CHECK])
    tasks.save(tools.root, value)
    before = deepcopy(task)
    (root/'work-graph.json').write_text(json.dumps(MarkdownAdapter.from_markdown('')), encoding='utf-8')
    user_reply(host, 'Confirm the saved backlog delivery without repeating implementation.')
    result = tools.execute(effect('check', {'names':[tasks.BACKLOG_CHECK]}, 'check-saved-graph'), execution)
    output = json.loads(result.output_json)
    assert result.status == 'succeeded' and output['status'] == 'green'
    assert output['project_graph_binding']['current_conversation'] is None
    assert output['project_graph_binding']['changed'] is False
    after = tasks.current(tasks.load(tools.root))
    assert all(after[k] == before[k] for k in ('intent','objective','criteria','plan'))
    assert tools.state['config']['verifiers'] == {}
    assert not list(tools.root.glob('units/*/receipt.json'))


def test_host_checks_keep_selected_command_protection_and_never_fall_back(host, monkeypatch):
    import sys
    from relay import command_protection
    tools, execution, root, _ = host
    configured = []
    class Gate:
        def execute(self, *a, **kw):
            raise AssertionError('Use the mediated command launcher, not an unbound port.')
    # Observe the actual NativeCapabilities verify effect without launching a
    # command: a denial must propagate, with no raw subprocess fallback.
    from compute_plane.native_capabilities import NativeCapabilities
    def denied(self, request, control):
        configured.append(json.loads(request.tool_call.arguments_json))
        return EffectResult(request.effect_id, 'refused', encode({'reason': 'policy denied'}))
    monkeypatch.setattr(NativeCapabilities, 'execute', denied)
    monkeypatch.setattr(command_protection, 'factory', lambda *a, **kw: lambda **kwargs: Gate())
    config = {**tools.state['config'], 'command_protection': {'backend': 'codex', 'review': 'codex'}}
    run = command_protection.conversation_verifier(execution, tools.root, config, deployment(), root,
        {'repository': 'repo', 'title': 'Check value', 'criteria': [], 'target_paths': ['a.py']}, {'one': [sys.executable]})
    with pytest.raises(ValueError, match='policy denied'):
        run([sys.executable])
    assert configured == [{'name': 'one'}]
    with pytest.raises(ValueError, match='not declared'):
        run(['arbitrary'])


@pytest.mark.parametrize('failure', ['format', 'legacy_context'])
@pytest.mark.parametrize('verdict', ['ACCEPT','REVISE','INCONCLUSIVE'])
def test_one_later_review_retry_keeps_the_auditor_and_never_resamples_a_verdict(host, monkeypatch, failure, verdict):
    import hashlib
    from compute_plane.native_journal import NativeJournal
    from relay import conversation_review, turn_engine
    tools, execution, root, _ = host
    intake(host); task = planned(host)
    evidence=b'The requested value is two.\n'
    (root/'facts.md').write_bytes(evidence)
    source={'repository':'repo','path':'facts.md','sha256':hashlib.sha256(evidence).hexdigest()}
    task['plan']['sources']=[source]
    for args in [('init',), ('add', '.'), ('-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost',
                                         '-c', 'commit.gpgsign=false', 'commit', '-m', 'input')]:
        subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True)
    (root/'a.py').write_bytes(b'value = 2\n')
    journal = NativeJournal(tools.root/'journal.jsonl')
    journal.append({'kind': 'session_started', 'session_id': 'parent'})
    journal.append({'kind': 'user_message', 'message_id': 'user-1', 'text': 'Change value to two.'})
    picks, dispatched = [], []
    monkeypatch.setattr(turn_engine, 'pick_auditors', lambda *a, **kw:
        picks.append(1) or [('first' if len(picks) == 1 else 'different', deployment())])
    actual = native_agent.run_session
    def child(config, dep, custody, instruction, **kwargs):
        assert config['context_policy'] == {'max_compactions':1, 'trigger':0.8, 'target':0.5}
        assert kwargs['limits']['max_model_calls'] == 8 and kwargs['limits']['max_seconds'] <= 1200
        assert kwargs['limits']['request_seconds'] <= 600
        packet=json.loads((custody.parent/'input/request.json').read_bytes())
        assert {**source,'repository':'candidate'} in packet['source_references_to_read']
        assert 'facts.md' in instruction and 'do not inventory unrelated files' in instruction
        dispatched.append(dep)
        if len(dispatched) == 1:
            custody.mkdir()
            log = NativeJournal(custody/'journal.jsonl')
            log.append({'kind': 'session_started', 'session_id': 'auditor'})
            log.append({'kind': 'model_requested', 'call_id': 'auditor/model/1'})
            return ({'status':'BLOCKED','reason':'COMPACTION_BUDGET'} if failure=='legacy_context' else
                    {'status':'FAILED','reason':'MODEL_FAILURE','failure_code':'INVALID_DELIVERY'})
        client = Client(steps=[('read', {'repository': 'candidate', 'path': 'a.py'}),
            ('tare_deliver', {'summary': 'Observed value and recorded obligations.', 'verdict': verdict, 'findings': [],
                'remaining': [] if verdict=='ACCEPT' else ['Additional external evidence is unavailable.'],
                'sources': [{'repository': 'candidate', 'path': 'a.py',
                             'sha256': hashlib.sha256(b'value = 2\n').hexdigest()}]})])
        return actual(config, dep, custody, instruction, client=client, local_counter=client, **kwargs)
    monkeypatch.setattr(native_agent, 'run_session', child)
    args = (tools, task, root, deployment(), task['plan']['unit'], {'a.py': b'value = 2\n'}, {'after': 'passed'}, execution)
    first = conversation_review.review(*args)
    assert first['verdict'] == 'INCONCLUSIVE'
    assert first['failure_code'] == ('COMPACTION_BUDGET' if failure=='legacy_context' else 'INVALID_DELIVERY')
    assert first['actor'] == {'role':'auditor','model':'fixture','route':'first'}
    if failure=='legacy_context':
        receipt=next((tools.root/'reviews').glob('*/receipt.json'))
        old=json.loads(receipt.read_bytes());old['result']['failure_code']=None;old.pop('context_contract')
        save_json(receipt,old)  # the completed receipt shape written by 0.1.104
    assert conversation_review.review(*args) == first and len(dispatched) == 1
    tools.invocation_id = 'turn-2'
    accepted = conversation_review.review(*args)
    assert accepted['verdict'] == verdict and accepted['route'] == 'first' and len(picks) == 1
    tools.invocation_id = 'turn-3'
    if failure=='legacy_context':
        monkeypatch.setattr(conversation_review,'context_contract',lambda dep:{'policy':{'max_compactions':1,'trigger':0.9}})
    assert conversation_review.review(*args) == accepted and len(dispatched) == 2


@pytest.mark.parametrize('failure_reason', ['COMPACTION_BUDGET', 'DEADLINE'])
def test_context_failures_require_an_actual_host_contract_change_not_another_message(host, monkeypatch, failure_reason):
    from compute_plane.native_journal import NativeJournal
    from relay import conversation_review,turn_engine
    tools,execution,root,_=host
    intake(host);task=planned(host)
    for args in [('init',),('add','.'),('-c','user.name=Fixture','-c','user.email=fixture@localhost',
        '-c','commit.gpgsign=false','commit','-m','input')]:
        subprocess.run(['git','-C',str(root),*args],check=True,capture_output=True)
    (root/'a.py').write_text('value = 2\n')
    log=NativeJournal(tools.root/'journal.jsonl');log.append({'kind':'session_started','session_id':'parent'})
    monkeypatch.setattr(turn_engine,'pick_auditors',lambda *a,**kw:[('first',deployment())])
    original=conversation_review.context_contract(deployment());old={**original,'agy_cached_input':None}
    monkeypatch.setattr(conversation_review,'context_contract',lambda dep:old)
    calls=[]
    def blocked(config,dep,custody,*a,**kw):
        calls.append(str(custody))
        custody.mkdir();journal=NativeJournal(custody/'journal.jsonl')
        journal.append({'kind':'session_started','session_id':'review'})
        return {'status':'BLOCKED','reason':failure_reason}
    monkeypatch.setattr(native_agent,'run_session',blocked)
    args=(tools,task,root,deployment(),task['plan']['unit'],{'a.py':b'value = 2\n'},{'after':'passed'},execution)
    first=conversation_review.review(*args)
    tools.invocation_id='turn-2'
    assert conversation_review.review(*args)==first and len(calls)==1
    monkeypatch.setattr(conversation_review,'context_contract',lambda dep:original)
    assert conversation_review.review(*args)['verdict']=='INCONCLUSIVE' and len(calls)==2
    tools.invocation_id='turn-3'
    conversation_review.review(*args)
    assert len(calls)==2  # same candidate/contract: no renewed budget or verdict resampling


def test_host_contract_recovery_is_exposed_without_changing_the_plan_or_dispatching_a_planner(host):
    from relay import conversation_review
    tools, execution, root, seen = host
    original_file = (root/'a.py').read_bytes()
    intake(host); planned(host)
    value = tasks.load(tools.root); task = tasks.current(value)
    original_plan = deepcopy(task['plan'])
    identity = 'a'*64
    job = tools.root/'reviews'/identity[:24]; job.mkdir(parents=True)
    result = {'verdict': 'INCONCLUSIVE', 'reason': 'COMPACTION_BUDGET', 'failure_code': None,
              'session': str(job/'session')}
    row = {'status': 'FINISHED', 'route':'first', 'calls_reserved':8,
           'deployment': deployment(), 'request_sha256': identity, 'result': result}
    save_json(job/'receipt.json', row)
    task.update(stage='PENDING', delivery={'verdict': 'NOT_REVIEWED', 'audit': result,
                                          'candidate': {'patch': 'private-host-path'}, 'files': ['a.py']})
    tasks.save(tools.root, value)
    tools.invocation_id = 'turn-2'
    blocked = tools.execute(effect('delegate_plan', {}, key='wrong-stage'), execution)
    output = json.loads(blocked.output_json)
    assert blocked.status == 'refused' and seen == []
    assert output['saved_plan'] is True
    assert output['delivery']['context_requalification_available'] is True
    assert 'private-host-path' not in blocked.output_json and str(job) not in blocked.output_json
    continuation = intake(host, relation='continue')
    assert continuation['delivery']['context_requalification_available'] is True
    assert 'saved plan' in continuation['next'] and continuation['task']['plan'] == original_plan
    assert (root/'a.py').read_bytes() == original_file and seen == []
    row['context_contract'] = conversation_review.context_contract(row['deployment'])
    save_json(job/'receipt.json', row)
    assert conversation_review.context_recovery_available(tools.root, result) is False
    answer=tools.execute(effect('demand',{'intent':'answer','relation':'continue'},key='status-question'),execution)
    current=json.loads(answer.output_json)
    assert answer.status=='succeeded' and current['stage']=='ANSWER'
    assert current['delivery']['audit']['failure_code']=='COMPACTION_BUDGET'
    assert current['delivery']['audit']['actor']['model']=='fixture'
    assert current['current']['plan']==original_plan and seen==[]


@pytest.mark.parametrize('stage,remaining,known', [('tool_choice',1,True),('tool_choice',2,False),('schema',1,False)])
def test_legacy_no_progress_requires_observed_final_choice_rejection(tmp_path, stage, remaining, known):
    from compute_plane.native_journal import NativeObservationJournal
    from relay.conversation_review import context_failure
    job=tmp_path/'review';(job/'session').mkdir(parents=True)
    NativeObservationJournal(job/'session/observations.jsonl').append({'purpose':'model',
        'remaining_model_calls':remaining, 'transport_calls':[{'proposal_rejection':{'stage':stage}}]})
    old={'verdict':'INCONCLUSIVE','reason':'NO_PROGRESS','failure_code':None}
    result=context_failure(job,{'deployment':deployment(),'route':'first','calls_reserved':8},old)
    assert (result.get('failure_code')=='CLI_REQUIRED_ACTION') is known
    assert old=={'verdict':'INCONCLUSIVE','reason':'NO_PROGRESS','failure_code':None}
    if not known:assert result is old


def test_ignored_final_choice_is_not_repeated_for_an_unrelated_budget_or_hint_change(host, monkeypatch):
    from compute_plane.native_journal import NativeJournal
    from relay import conversation_review, turn_engine
    tools, execution, root, _ = host
    intake(host);task=planned(host)
    for args in [('init',),('add','.'),('-c','user.name=Fixture','-c','user.email=fixture@localhost',
        '-c','commit.gpgsign=false','commit','-m','input')]:
        subprocess.run(['git','-C',str(root),*args],check=True,capture_output=True)
    (root/'a.py').write_text('value = 2\n')
    log=NativeJournal(tools.root/'journal.jsonl');log.append({'kind':'session_started','session_id':'parent'})
    monkeypatch.setattr(turn_engine,'pick_auditors',lambda *a,**kw:[('first',deployment())])
    calls=[]
    def blocked(config,dep,custody,*a,**kw):
        calls.append(str(custody));custody.mkdir()
        NativeJournal(custody/'journal.jsonl').append({'kind':'session_started','session_id':'review'})
        return {'status':'FAILED','reason':'MODEL_FAILURE','failure_code':'CLI_REQUIRED_ACTION'}
    monkeypatch.setattr(native_agent,'run_session',blocked)
    args=(tools,task,root,deployment(),task['plan']['unit'],{'a.py':b'value = 2\n'},{'after':'passed'},execution)
    first=conversation_review.review(*args)
    assert first['failure_code']=='CLI_REQUIRED_ACTION'
    tools.invocation_id='turn-2'
    old=conversation_review.context_contract(deployment())
    monkeypatch.setattr(conversation_review,'context_contract',lambda dep:{**old,'extra_hint':'new text'})
    assert conversation_review.review(*args)==first and len(calls)==1
    monkeypatch.setattr(conversation_review,'context_contract',lambda dep:{**old,'cli_final_choice_enforced':True})
    second=conversation_review.review(*args)
    assert second['failure_code']=='CLI_REQUIRED_ACTION' and len(calls)==2
    tools.invocation_id='turn-3'
    assert conversation_review.review(*args)==second and len(calls)==2


def test_context_failure_reports_limits_and_estimates_without_rewriting_legacy_evidence(tmp_path):
    from compute_plane.native_journal import NativeJournal
    from relay.conversation_review import context_failure
    job=tmp_path/'review'; child=job/'session';child.mkdir(parents=True)
    save_json(child/'native-agent.json', {'deployment':{'native_profile':{'context_window':200000,'output_tokens':16384}},
        'config':{'context_policy':{'max_compactions':0}},'limits':{'max_seconds':300}})
    log=NativeJournal(child/'journal.jsonl')
    log.append({'kind':'context_counted','count':{'request_purpose':'model','input_tokens':None,
        'estimated_input_tokens':173466,'method':'utf8-envelope-estimate-v1'}})
    old={'verdict':'INCONCLUSIVE','reason':'COMPACTION_BUDGET','failure_code':None}
    result=context_failure(job,{'calls_reserved':8,'calls_used':3},old)
    assert old['failure_code'] is None and 'budget' not in old
    assert result['failure_code']=='COMPACTION_BUDGET'
    assert result['budget']=={'context_tokens':200000,'input_limit_tokens':183488,
        'compaction_trigger_tokens':160000,'output_tokens':16384,'max_compactions':0,
        'calls_limit':8,'calls_used':3,'seconds_limit':300,'input_tokens':None,
        'estimated_input_tokens':173466,'count_method':'utf8-envelope-estimate-v1'}
    view=tasks.delivery_view({'audit':result,'candidate':{'patch':'host-only'}})
    assert view['audit']['budget']==result['budget'] and view['candidate_preserved']
    assert 'does not itself change or renew' in view['next'] and 'estimated from measured' in view['next']
    unknown={'verdict':'INCONCLUSIVE','reason':'SUMMARY_UNFINISHED'}
    assert context_failure(job,{},unknown) is unknown  # unknown dispatch remains unrepeated


@pytest.mark.parametrize('failure', ['quota', 'partial_quota', 'unknown'])
def test_auditor_fallback_only_after_explicit_quota_before_any_review(host, monkeypatch, failure):
    import hashlib
    from compute_plane.native_journal import NativeJournal
    from relay import conversation_review, turn_engine
    tools, execution, root, _ = host
    intake(host); task = planned(host)
    for args in [('init',), ('add', '.'), ('-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost',
                                         '-c', 'commit.gpgsign=false', 'commit', '-m', 'input')]:
        subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True)
    (root/'a.py').write_bytes(b'value = 2\n')
    log=NativeJournal(tools.root/'journal.jsonl')
    log.append({'kind':'session_started','session_id':'parent'})
    dispatched=[]
    monkeypatch.setattr(turn_engine,'pick_auditors',lambda *a,**kw:[('first',deployment()),('second',deployment())])
    actual=native_agent.run_session
    def child(config, dep, custody, instruction, **kwargs):
        dispatched.append(str(custody))
        if len(dispatched)==1:
            custody.mkdir()
            journal=NativeJournal(custody/'journal.jsonl')
            journal.append({'kind':'session_started','session_id':'auditor'})
            journal.append({'kind':'model_requested','call_id':'a/1'})
            if failure=='partial_quota': journal.append({'kind':'model_returned','call_id':'a/1','reply':{}})
            if failure!='unknown': journal.append({'kind':'model_failed','call_id':'a/1','code':'CLI_USAGE_LIMIT'})
            return {'status':'FAILED','reason':'MODEL_FAILURE','failure_code':'CLI_USAGE_LIMIT'}
        client=Client(steps=[('read',{'repository':'candidate','path':'a.py'}),
            ('tare_deliver',{'summary':'Value is two.','verdict':'ACCEPT','findings':[],'remaining':[],
                'sources':[{'repository':'candidate','path':'a.py','sha256':hashlib.sha256(b'value = 2\n').hexdigest()}]})])
        return actual(config,dep,custody,instruction,client=client,local_counter=client,**kwargs)
    monkeypatch.setattr(native_agent,'run_session',child)
    args=(tools,task,root,deployment(),task['plan']['unit'],{'a.py':b'value = 2\n'},{'after':'passed'},execution)
    result=conversation_review.review(*args)
    assert result['verdict']==('ACCEPT' if failure=='quota' else 'INCONCLUSIVE')
    assert len(dispatched)==(2 if failure=='quota' else 1)
    assert conversation_review.review(*args)==result  # no resampling or repeated dispatch
    assert len(dispatched)==(2 if failure=='quota' else 1)
    assert (root/'a.py').read_bytes()==b'value = 2\n'
    assert conversation_review.committed(tools.root)==(3 if failure=='quota' else 1)


@pytest.mark.parametrize('verdict', ['INCONCLUSIVE', 'NOT_REVIEWED'])
def test_same_turn_inconclusive_audit_does_not_reask_permission_or_repeat_work(host, monkeypatch, verdict):
    tools, execution, *_ = host
    intake(host); planned(host)
    calls=[]
    monkeypatch.setattr(work_units,'run_unit',lambda *a,**kw:
        calls.append(1) or {'verdict':verdict,'audit':{'verdict':'INCONCLUSIVE'},'applied':False})
    assert tools.execute(effect('implement',{},'first'),execution).status=='succeeded'
    second=tools.execute(effect('implement',{},'second'),execution)
    assert second.status=='succeeded' and 'Stop and explain' in second.output_json
    assert json.loads(second.output_json)['dispatch']=={'implementer':'NOT_STARTED','auditor':'NOT_STARTED'}
    assert 'returned unchanged' in second.output_json
    assert calls==[1]


def test_guidance_matches_coordinator_direct_conversation_and_inline_approval():
    text = native_agent.prompt_capsule(deployment(), 'coordinator', 'conversation')['system']
    policy = ("Answer simple questions, explanations, quick comparisons and informal advice such as 'como você faria?' "
              'directly in the conversation, reading the project if needed, without demand, plan or delegate_plan. '
              'Require the separate planner only for a requested change or an explicit formal plan. '
              'For these requests record demand with intent=deliver or intent=plan, then call delegate_plan; '
              'a formal plan alone does not authorize implementation.')
    assert policy in text and policy in tasks.GUIDANCE
    for clause in ('Summarize a returned plan in a few lines and ask at most two decision questions',
                   'without another confirmation request in chat',
                   'On failure cite the recorded reason and the next step, never invent a cause'):
        assert clause in text and clause in tasks.GUIDANCE
    assert 'For substantive design comparisons or recommendations' not in tasks.GUIDANCE


def test_repeated_audit_refusals_stop_before_another_implementer(tmp_path):
    unit = {'key': 'task-x', 'target_paths': ['a.py']}
    refused = {'verdict': 'NOT_REVIEWED', 'reason': work_units.AUDIT_REFUSED}
    for reason in ('An observed audit dependency changed before application.', 'The candidate changed during review.'):
        work_units.record(tmp_path, unit, {'verdict': 'NOT_REVIEWED', 'reason': reason})  # accepted, refused by the host
    work_units.check_failures(tmp_path, unit, conversation=True)  # the host's refusals are not the auditor's
    for _ in range(work_units.MAX_REFUSALS - 1):
        work_units.record(tmp_path, unit, refused)
    work_units.check_failures(tmp_path, unit, conversation=True)  # still the implementer's to fix
    work_units.record(tmp_path, unit, refused)
    with pytest.raises(ValueError, match='audit refused the last 3 candidates'):
        work_units.check_failures(tmp_path, unit, conversation=True)
    work_units.check_failures(tmp_path, unit, conversation=True, operator_retry=True)  # the user asked for one more


@pytest.mark.parametrize('kind', ['checks', 'audit', 'overlap'])
@pytest.mark.parametrize('reply', ['sim', 'pode sim', 'ok!', 'claro que pode'])
def test_journal_retry_passes_both_guards_once(host, monkeypatch, kind, reply):
    from relay.conversation_recheck import retry_decision
    tools, execution, _, _ = host
    intake(host); task = planned(host)
    unit = task['plan']['unit']
    blocked = {**unit, 'key': 'older-name'} if kind == 'overlap' else unit
    failure = {'verdict': 'NOT_REVIEWED', 'reason': work_units.AUDIT_REFUSED} if kind == 'audit' else {'verdict': 'NOT_VERIFIED'}
    for _ in range(work_units.MAX_REFUSALS if kind == 'audit' else work_units.MAX_FAILURES):
        work_units.record(tools.root, blocked, failure)
    calls = []
    def run(custody, state, *args, **kwargs):
        worker_unit = work_units.read_ledger(custody)['plan']['units'][0]
        work_units.check_failures(custody, worker_unit, conversation=True,
            operator_retry=state.get('operator_delivery_retry', False))
        calls.append(state.get('operator_delivery_retry'))
        return {'verdict': 'NOT_VERIFIED'}
    monkeypatch.setattr(work_units, 'run_unit', run)
    def attempt(key, **args):
        return tools.execute(effect('implement', args, key), execution)
    refused = attempt('blocked', note='The user said yes')
    assert refused.status == 'refused' and 'plain words' in refused.output_json
    assert '/retry_delivery' not in refused.output_json
    request = tools.root/'conversation-retry/request.json'
    before = request.read_bytes()
    assert attempt('no-message').status == 'refused'
    assert request.read_bytes() == before and calls == []
    user_reply(host, reply)
    from relay import consent_decisions as consent
    q = consent.pending(tools.root, tools.session_id)[0]
    consent.record(tools.root, tools.session_id, q['question_id'], choice='allow')
    intake(host, relation='continue')
    released = attempt('released')
    assert released.status == 'succeeded', released.output_json
    assert calls == [True]
    tools = tasks.ConversationTools(tools.tools, tools.root, tools.state, 'resumed')
    assert attempt('used').status == 'refused'
    assert calls == [True]
    assert retry_decision(tools.root, task, unit) is False


def test_non_yes_first_journal_reply_cannot_be_revived_by_later_yes(host, monkeypatch):
    from test_consent_decisions import verdict
    monkeypatch.setattr('relay.turn_engine.judges', lambda packet, **k:
        {'jev': verdict('deny')} if set(packet['options']) == {'allow', 'deny', 'unclear'} else {})
    tools, execution, _, _ = host
    intake(host); task = planned(host)
    for _ in range(work_units.MAX_FAILURES):
        work_units.record(tools.root, task['plan']['unit'], {'verdict':'NOT_VERIFIED'})
    monkeypatch.setattr(work_units, 'run_unit', lambda *a, **kw: pytest.fail('No consent'))
    assert tools.execute(effect('implement', {}, 'initial-refusal'), execution).status == 'refused'
    user_reply(host, 'not now')
    user_reply(host, 'yes')  # no intervening refusal/question
    intake(host, relation='continue')
    result = tools.execute(effect('implement', {'note':'User approved'}, 'declined'), execution)
    assert result.status == 'refused'


@pytest.mark.parametrize('stop', ['worker', 'budget', 'permission'])
def test_released_retry_is_burned_when_attempt_stops(host, monkeypatch, stop):
    from relay import conversation_permissions
    tools, execution, _, _ = host
    intake(host); task = planned(host)
    for _ in range(work_units.MAX_FAILURES):
        work_units.record(tools.root, task['plan']['unit'], {'verdict': 'NOT_VERIFIED'})
    assert tools.execute(effect('implement', {}, 'ask'), execution).status == 'refused'
    user_reply(host, 'sim')
    from relay import consent_decisions as consent
    q = consent.pending(tools.root, tools.session_id)[0]
    consent.record(tools.root, tools.session_id, q['question_id'], choice='allow')
    intake(host, relation='continue')
    reached = []
    def fail(*args, **kwargs):
        reached.append(stop)
        if stop == 'permission': raise PermissionError('permission denied')
        raise ValueError('attempt interrupted')
    with monkeypatch.context() as patch:
        if stop == 'worker': patch.setattr(work_units, 'run_unit', fail)
        elif stop == 'budget': patch.setattr(work_units, 'child_limits', fail)
        else: patch.setattr(conversation_permissions, 'authorize', fail)
        result = tools.execute(effect('implement', {}, 'interrupted'), execution)
        assert result.status == 'refused'
    assert reached == [stop]
    tools = tasks.ConversationTools(tools.tools, tools.root, tools.state, 'resumed-after-stop')
    monkeypatch.setattr(work_units, 'run_unit', lambda *a, **kw: pytest.fail('Consent replayed'))
    result = tools.execute(effect('implement', {}, 'replay'), execution)
    assert result.status == 'refused'


def test_judged_retry_can_be_requested_again_after_consumption(host, monkeypatch):
    from relay import consent_decisions as consent
    from test_consent_decisions import verdict
    monkeypatch.setattr('relay.turn_engine.judges', lambda packet, **k:
        {'jev': verdict('allow', .95)} if set(packet['options']) == {'allow', 'deny', 'unclear'} else {})
    monkeypatch.setattr('relay.consent_decisions.select_rule', lambda: 'judge')
    tools, execution, _, _ = host
    intake(host); task = planned(host)
    unit = task['plan']['unit']
    for _ in range(work_units.MAX_FAILURES):
        work_units.record(tools.root, unit, {'verdict': 'NOT_VERIFIED'})
    calls = []
    def run(custody, state, *args, **kwargs):
        worker_unit = work_units.read_ledger(custody)['plan']['units'][0]
        work_units.check_failures(custody, worker_unit, conversation=True,
            operator_retry=state.get('operator_delivery_retry', False))
        calls.append(state.get('operator_delivery_retry'))
        return {'verdict': 'NOT_VERIFIED'}
    monkeypatch.setattr(work_units, 'run_unit', run)
    keys = []
    for cycle in range(2):
        refused = tools.execute(effect('implement', {}, f'blocked-{cycle}'), execution)
        assert refused.status == 'refused'
        pending = consent.pending(tools.root, tools.session_id)
        assert len(pending) == 1
        key = pending[0]['question_id']
        assert key not in keys
        keys.append(key)
        request = json.loads((tools.root/'conversation-retry/request.json').read_text())
        assert request['question_id'] == key
        assert tools.execute(effect('implement', {}, f'still-blocked-{cycle}'), execution).status == 'refused'
        assert consent.pending(tools.root, tools.session_id)[0]['question_id'] == key
        user_reply(host, 'sim, tenta de novo')
        intake(host, relation='continue')
        released = tools.execute(effect('implement', {}, f'released-{cycle}'), execution)
        assert released.status == 'succeeded', released.output_json
        assert calls == [True] * (cycle + 1)
        assert next(q for q in consent.load(tools.root)['questions'] if q['id'] == key)['status'] == 'CONSUMED'
    assert tools.execute(effect('implement', {}, 'no-third-approval'), execution).status == 'refused'
    assert calls == [True, True]


@pytest.mark.parametrize('choice', ['allow', 'deny'])
@pytest.mark.parametrize('legacy', [False, True])
def test_dialog_resume_delivers_host_observation_without_human_words(setup, monkeypatch, choice, legacy):
    from compute_plane.native_journal import NativeJournal
    from relay import consent_decisions as consent
    from relay.native_refresh import OBSERVED, FOLLOW_UP, user_words
    config, custody, root = setup
    first_client = Client(steps=[('tare_deliver', {'summary': 'May I read the reference?'})])
    first = native_agent.run_session(config, deployment(), custody, 'Compare references.',
        conversation=True, client=first_client, local_counter=first_client)
    before = consent.events(custody)
    monkeypatch.setattr(consent, 'classify', lambda *a: pytest.fail('host observation was classified'))
    observations = []
    for cycle, selected in enumerate([choice, 'deny' if choice == 'allow' else 'allow']):
        outside = root.parent/f'reference with spaces {cycle}'; outside.mkdir()
        key = consent.register(custody, first['session_id'], 'folder',
            {'path': str(outside), 'store': str(root.parent/'grants.json')}, question='Allow reading?')
        consent.record(custody, first['session_id'], key, choice=selected)
        observation = next(q['observation'] for q in consent.load(custody)['questions'] if q['id'] == key)
        observations.append(observation)
        if legacy and cycle == 0:
            journal = NativeJournal(custody/'journal.jsonl')
            predecessor = journal.snapshot().records[-1]
            journal.append({'kind': 'host_observation',
                'observation_id': observation['id'], 'text': observation['text'],
                'after_sequence': predecessor.sequence, 'after_sha256': predecessor.sha256})
        follow = Client(steps=[('tare_deliver', {'summary': 'Continuing after the choice.'})])
        result = native_agent.run_session(None, None, custody, resume=True, client=follow, local_counter=follow)
        assert result['status'] == 'PROPOSED'
        assert observation['text'].startswith(OBSERVED) and FOLLOW_UP not in observation['text']
        assert user_words(observation['text']) is None
        body = next(b for p, b in follow.calls if p == '/chat/completions')
        assert any(observation['text'] in message.get('content', '')
                   for message in body['messages'] if isinstance(message.get('content'), str))
        after = consent.events(custody)
        assert [e for e in after if e['kind'] == 'user_message' and user_words(e['text']) is not None] == [
            e for e in before if e['kind'] == 'user_message' and user_words(e['text']) is not None]
        for index, delivered in enumerate(observations):
            receipts = [e for e in after if e['kind'] == 'user_message'
                        and e.get('message_id') == 'consent-decision-' + delivered['id']]
            assert len(receipts) == (0 if legacy and index == 0 else 1)
            if receipts:
                assert receipts[0]['text'] == delivered['text']
    observed = [e for e in consent.events(custody) if e['kind'] in {'user_message', 'host_observation'}]
    again = Client(steps=[('tare_deliver', {'summary': 'Still continuing.'})])
    native_agent.run_session(None, None, custody, resume=True, client=again, local_counter=again)
    assert [e for e in consent.events(custody) if e['kind'] in {'user_message', 'host_observation'}] == observed


@pytest.mark.parametrize('delivery', ['callback', 'no_callback', 'failed_callback'])
@pytest.mark.parametrize('choice', ['allow', 'deny'])
def test_session_notice_delivery_survives_resume(setup, monkeypatch, delivery, choice):
    from relay import consent_decisions as consent
    config, custody, root = setup
    config['verifiers'] = {}
    outside = root.parent/'outside'; outside.mkdir()
    config['folder_access'] = {'grants': str(root.parent/'grants.json')}
    def run(**kwargs):
        client = Client(steps=[('tare_deliver', {'summary': 'Finished this turn.'})])
        return native_agent.run_session(client=client, local_counter=client, **kwargs)
    first = run(config=config, deployment=deployment(), custody=custody,
                instruction='Explain a.py.', conversation=True)
    session = first['session_id']
    key = consent.register(custody, session, 'folder',
        {'path': str(outside), 'store': config['folder_access']['grants']}, question='Allow?')
    receipt = consent.record(custody, session, key, choice=choice)
    notice = receipt['notice']
    before = consent.load(custody)['questions'][0]
    events = []
    def progress(event):
        assert isinstance(event, dict)
        events.append(event)
        if notice in event.get('text', '') and delivery == 'failed_callback':
            raise RuntimeError('notice transport failed')
    # A dialog settlement resumes without fabricating a new user message.
    kwargs = dict(config=None, deployment=None, custody=custody, resume=True,
                  on_progress=None if delivery == 'no_callback' else progress)
    if delivery == 'failed_callback':
        with pytest.raises(RuntimeError, match='notice transport failed'):
            run(**kwargs)
        assert consent.load(custody)['questions'][0] == before
        assert consent.pending_notices(custody, session) == [
            {'question_id': key, 'notice': notice}]
        kwargs.update(follow_up='Retry delivery.', message_id='retry-notice', on_progress=None)
    result = run(**kwargs)
    assert result['status'] == 'PROPOSED'
    assert result['output'].count(notice) == 1
    assert json.loads((custody/'outcome.json').read_text())['output'] == result['output']
    assert consent.load(custody)['questions'][0] == {**before, 'notice_delivered': True}
    if delivery != 'no_callback':
        assert [e for e in events if notice in e.get('text', '')] == [
            {'kind': 'delta', 'channel': 'text', 'text': '\n\n' + notice}]
    events.clear()
    resumed = run(config=None, deployment=None, custody=custody, resume=True,
                  follow_up='Continue again.', message_id='after-notice', on_progress=progress)
    assert resumed['status'] == 'PROPOSED'
    assert notice not in resumed['output']
    assert not any(notice in e.get('text', '') for e in events)
    assert consent.pending_notices(custody, session) == []


@pytest.mark.parametrize('mode', ['approved', 'waiting', 'permission-denied'])
def test_implement_retry_survives_revised_plan_but_not_file_permission_denial(host, monkeypatch, mode):
    from relay import consent_decisions as consent, conversation_permissions
    tools, execution, project, _ = host
    intake(host); task = planned(host)
    artifact = Path(task['plan']['artifact'])
    payload = json.loads(artifact.read_text())
    payload['payload']['tasks'][0]['verifiers'] = [tasks.BACKLOG_CHECK]
    payload['payload']['tasks'][0]['target_paths'] = ['a.py', 'work-graph.json']
    tools.state['config']['writable'] = {'repo': ['a.py', 'work-graph.json']}
    save_json(artifact, payload)
    value = tasks.load(tools.root); task = tasks.current(value)
    tasks.task_plan(tools.root, tools.state['config'], task, {'artifact': str(artifact)})
    tasks.save(tools.root, value)
    original = deepcopy(task['plan'])
    original['unit']['verification_contract_digest'] = hashlib.sha256(
        encode(tasks.backlog_authoring_contract()).encode()).hexdigest()
    for _ in range(work_units.MAX_FAILURES):
        work_units.record(tools.root, original['unit'], {'verdict': 'NOT_VERIFIED'})
    calls = []
    def run(custody, state, *args, **kwargs):
        unit = work_units.read_ledger(custody)['plan']['units'][0]
        assert unit['planner_input']['sha256'] == revised['sha256'] != original['sha256']
        assert unit['target_paths'] == ['a.py', 'b.py', 'work-graph.json'] != original['unit']['target_paths']
        assert unit['verification_contract_digest'] == revised['unit']['verification_contract_digest']
        assert unit['verification_contract_digest'] != original['unit']['verification_contract_digest']
        assert state['operator_delivery_retry'] is True
        assert state['config']['writable']['repo'] == ['a.py', 'b.py', 'work-graph.json']
        work_units.check_failures(custody, unit, conversation=True, operator_retry=True)
        calls.append(True)
        return {'verdict': 'NOT_VERIFIED'}
    monkeypatch.setattr(work_units, 'run_unit', run)
    assert tools.execute(effect('implement', {}, 'retry-before-revision'), execution).status == 'refused'
    question = deepcopy(consent.load(tools.root)['questions'][0])
    expected = {'task': task['id'], 'repository': 'repo', 'key': task['plan']['unit']['key']}
    assert question['scope'] == expected
    if mode != 'waiting':
        consent.record(tools.root, tools.session_id, question['id'], choice='allow')
    # Rehydrate from a changed saved planner artifact, not a coordinator-only unit edit.
    (project/'b.py').write_text('value = 1\n')
    payload['payload']['tasks'][0]['target_paths'] = ['a.py', 'b.py', 'work-graph.json']
    save_json(artifact, payload)
    contract = deepcopy(tasks.backlog_authoring_contract())
    contract['retry_revision_fixture'] = True
    monkeypatch.setattr(tasks, 'backlog_authoring_contract', lambda: deepcopy(contract))
    value = tasks.load(tools.root); task = tasks.current(value)
    tasks.task_plan(tools.root, tools.state['config'], task, {'artifact': str(artifact)})
    tasks.save(tools.root, value)
    revised = deepcopy(task['plan'])
    revised['unit']['verification_contract_digest'] = hashlib.sha256(encode(contract).encode()).hexdigest()
    assert revised['sha256'] != original['sha256']
    assert revised['unit']['verification_contract_digest'] != original['unit']['verification_contract_digest']
    # Saturate the revised identity too: success must not come from resetting history.
    for _ in range(work_units.MAX_FAILURES):
        work_units.record(tools.root, revised['unit'], {'verdict': 'NOT_VERIFIED'})
    with pytest.raises(work_units.GuardRefusal):
        work_units.check_failures(tools.root, revised['unit'], conversation=True)
    if mode == 'waiting':
        result = tools.execute(effect('implement', {}, 'still-waiting-after-revision'), execution)
        assert result.status == 'refused' and calls == []
        pending = consent.load(tools.root)['questions']
        assert len(pending) == 1
        assert pending[0]['id'] == question['id']
        assert pending[0]['boundary'] == question['boundary']
        assert pending[0]['scope'] == expected
        consent.record(tools.root, tools.session_id, question['id'], choice='allow')
    tools.state['config']['writable'] = {'repo': ['a.py', 'b.py', 'work-graph.json']}
    authorized = []
    real_authorize = conversation_permissions.authorize
    def authorize(*args, **kwargs):
        authorized.append(True)
        if mode == 'permission-denied': raise PermissionError('fixture refuses file authorization')
        return real_authorize(*args, **kwargs)
    monkeypatch.setattr(conversation_permissions, 'authorize', authorize)
    result = tools.execute(effect('implement', {}, 'retry-after-revision'), execution)
    assert authorized == [True]
    assert result.status == ('refused' if mode == 'permission-denied' else 'succeeded'), result.output_json
    assert calls == ([] if mode == 'permission-denied' else [True])
    assert next(q for q in consent.load(tools.root)['questions'] if q['id'] == question['id'])['status'] == 'CONSUMED'
    if mode == 'permission-denied':
        return
    result = tools.execute(effect('implement', {}, 'retry-consumed-after-revision'), execution)
    assert result.status == 'refused' and calls == [True] and authorized == [True]
    request = json.loads((tools.root/'conversation-retry/request.json').read_text())
    assert request['question_id'] != question['id'] and request['scope'] == expected
