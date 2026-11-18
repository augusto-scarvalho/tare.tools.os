"""A conversation plan that is not routine gets one independent read-only review, and a REVISE with findings makes the
planner revise the plan once. Offline: the children are stubbed at run_session, the reviewer choice at the /plan flow's
own selector."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from compute_plane.native_journal import NativeJournal, save_json
from relay import conversation_permissions as permissions
from relay import conversation_tasks, native_agent, native_artifacts, native_plan, native_routes, planning_tools, turn_engine, work_audit
from test_planning_tools import execution, planner, request, wrap  # noqa: F401  (planner is a fixture)

TASK = {'key': 'a', 'title': 'Change a.py', 'summary': 'Edit it.', 'cluster': 'c', 'priority': 'P1',
        'criteria': ['It works.'], 'target_paths': ['a.py'], 'test_paths': [], 'depends_on': [], 'verifiers': []}
PLAN = {'summary': 'Plan.', 'steps': ['Edit a.py.'], 'criteria': ['It works.'], 'sources': [], 'remaining': [],
        'tasks': [dict(TASK, critical=True)]}
REVISED = {**PLAN, 'summary': 'Revised plan.', 'tasks': [dict(TASK, critical=True, title='Change a.py, descending')]}
FINDING = {'claim': 'BLIND SPOT: the request asks for descending order; the plan sorts ascending.', 'paths': ['a.py']}


def plan_of(**hints):
    return {**PLAN, 'tasks': [dict(TASK, **hints)]}


class Stub:
    """Every child, the reviewer choice and what the reviewer answers."""
    def __init__(self, monkeypatch, candidate, plan=PLAN):
        self.plan, self.revised, self.sessions, self.picked = plan, REVISED, [], []
        self.reviewer = dict(candidate, id='fixture-critic')
        self.pick = (self.reviewer, {})
        self.verdict = {'verdict': 'REVISE', 'summary': 'Found.', 'findings': [FINDING], 'remaining': [], 'sources': []}
        self.outcome = {'status': 'PROPOSED'}   # the reviewer's
        self.revision = {'status': 'PROPOSED'}  # the revising planner's
        self.kind = 'INDEPENDENT'
        monkeypatch.setattr(turn_engine, 'available', lambda name: (deepcopy(candidate), None))
        monkeypatch.setattr(native_agent, 'run_session', self.run)
        monkeypatch.setattr(native_plan, 'select_route', self.select)
        monkeypatch.setattr(work_audit, 'independence', lambda a, b: self.kind)
        # the stubbed artifacts are bare payloads; the real reader wants a whole signed-off artifact
        monkeypatch.setattr(native_artifacts, 'read_input', lambda path: {
            'sha256': hashlib.sha256(Path(path).read_bytes()).hexdigest(),
            'artifact': {'role': 'planner', 'payload': json.loads(Path(path).read_text(encoding='utf-8'))['payload']}})

    def select(self, choice, role, reason, producer=None, work_type=None):
        self.picked.append((choice, role, producer['id'], work_type))
        if isinstance(self.pick, Exception):
            raise self.pick
        return self.pick

    def run(self, config, deployment, root, instruction, **kwargs):
        self.sessions.append((config, deployment, root, instruction, kwargs))
        root.mkdir()
        role = 'revision' if root.name == 'revision' else config['role']
        journal = NativeJournal(root/'journal.jsonl')
        journal.append({'kind': 'session_started', 'session_id': role})
        for n in range(3 if role == 'plan-auditor' else 2):
            journal.append({'kind': 'model_requested', 'call_id': f'{role}/model/{n}'})
        save_json(root/'native-agent.json', {'session_id': role})
        outcome = {'planner': {'status': 'PROPOSED'}, 'plan-auditor': self.outcome, 'revision': self.revision}[role]
        if role == 'plan-auditor':
            assert (root.parent/'proposal/plan.json').is_file()  # the reviewer reads it while it runs
        if outcome['status'] == 'PROPOSED':
            save_json(root/'artifact.json', {'payload': {'planner': self.plan, 'revision': self.revised,
                                                          'plan-auditor': self.verdict}[role]})
        if isinstance(outcome.get('raises'), BaseException):
            raise outcome['raises']
        return outcome

    def roles(self):
        return [s[2].name if s[2].name == 'revision' else s[0]['role'] for s in self.sessions]


@pytest.fixture
def conversation(planner, monkeypatch):
    parent, state, candidate = planner
    state['conversation_flow'] = True
    stub = Stub(monkeypatch, candidate)
    control = execution(); tool = wrap(parent, state, control)
    conversation_tasks.ConversationTools(tool, parent, state, 'turn').understand({
        'intent': 'deliver', 'relation': 'new', 'objective': 'Sort descending.', 'criteria': ['Descending.'],
        'kinds': ['coding']})
    return tool, control, stub, parent, state


def delegate(conversation, effect='effect'):
    tool, control, *_ = conversation
    result = tool.execute(request(effect=effect), control)
    assert result.status == 'succeeded', result.output_json
    return json.loads(result.output_json)


def receipt(parent):
    return json.loads(next((parent/'planning').glob('*/receipt.json')).read_text(encoding='utf-8'))


def dialog(parent, name='session'):
    artifact = next((parent/'planning').glob(f'*/{name}/artifact.json'))
    return permissions.plan_text(parent, {'artifact': str(artifact), 'plan': hashlib.sha256(artifact.read_bytes()).hexdigest()})


@pytest.mark.parametrize('flow,plan,reviewed', [
    (True, plan_of(needs='cheap'), True),                     # all cheap: the planner's own rating excuses nothing
    (True, plan_of(), True),                                  # no needs stated
    (True, plan_of(needs='middling'), True),
    (True, plan_of(needs='cheap', critical=True), True),
    (True, {**PLAN, 'tasks': []}, False),                     # no tasks: nothing to review
    (False, plan_of(critical=True), False)])                  # not a conversation
def test_every_conversation_plan_with_tasks_is_reviewed_whatever_its_tasks_state(planner, monkeypatch, flow, plan, reviewed):
    parent, state, candidate = planner
    state['conversation_flow'] = flow
    stub = Stub(monkeypatch, candidate, plan)
    stub.verdict = {'verdict': 'ACCEPT', 'summary': 'Sound.', 'findings': [], 'remaining': [], 'sources': []}
    control = execution(); tool = wrap(parent, state, control)
    if flow:
        conversation_tasks.ConversationTools(tool, parent, state, 'turn').understand({
            'intent': 'deliver', 'relation': 'new', 'objective': 'Sort.', 'criteria': ['Sorted.']})
    value = json.loads(tool.execute(request(), control).output_json)
    assert value['status'] == 'PLANNED'
    if reviewed:
        assert value['review']['status'] == 'REVIEWED' and stub.roles() == ['planner', 'plan-auditor']
    else:
        assert 'review' not in value and stub.roles() == ['planner'] and stub.picked == []
        assert tool.calls_committed() == 2 and not any((parent/'planning').glob('*/review'))


def test_a_review_the_budget_cannot_follow_with_a_revision_reaches_everywhere_as_findings(conversation):
    tool, control, stub, parent, state = conversation
    state['limits']['max_model_calls'] = 9  # 1 parent + 2 planner + 3 review leave 1 after the reserve: no revision
    value = delegate(conversation)
    assert value['status'] == 'PLANNED' and value['plan'] == PLAN and value['model_calls'] == 2
    assert value['review'] == {'status': 'REVIEWED', 'verdict': 'REVISE', 'findings': [FINDING], 'route': 'fixture-critic',
                               'model_calls': 3, 'independent': True, 'revised': False, 'revision_reason': 'NOT_FUNDED'}
    assert value['next'].endswith(planning_tools.CRITIC_NEXT) and 'delegate_plan' in value['next']
    # the reviewer is chosen as /plan chooses it, for this planner and the demand's types
    assert stub.picked == [('auto', 'plan-auditor', 'fixture-planner', ['coding'])]
    config, deployment, root, instruction, opts = stub.sessions[1]
    assert deployment['id'] == 'fixture-critic' and instruction == planning_tools.CRITIC_BRIEF
    assert (config['role'], config['typed_output'], config['writable'], config['verifiers'], config['capabilities']) == (
        'plan-auditor', True, {}, {}, ['read', 'list', 'search'])
    assert set(config['repositories']) == {'repo', 'proposal'} and root == next((parent/'planning').glob('*/review/session'))
    proposal = root.parent/'proposal'
    assert json.loads((proposal/'plan.json').read_text(encoding='utf-8')) == PLAN
    assert json.loads((proposal/'request.json').read_text(encoding='utf-8')) == {
        'objective': 'Sort descending.', 'criteria': ['Descending.'], 'kinds': ['coding']}
    # 1 parent call, 2 planner, RESERVE 2 of the 9: at most 4 for the review, within its own 12 calls and 600 s
    assert opts['limits']['max_model_calls'] == 4 and opts['limits']['max_seconds'] == 600 and opts['delegated_leaf'] is True
    # recorded beside the planner's, charged to the shared budget, and its session is a child like the planner's
    assert receipt(parent)['result']['review'] == value['review']
    assert tool.calls_committed() == 5 and receipt(parent)['calls_used'] == 5
    assert len(planning_tools.children(parent)) == 2
    # a second read of the same plan does not pay again
    assert delegate(conversation) == value and stub.roles() == ['planner', 'plan-auditor']
    # the user's dialog shows the verdict and the findings, above the plan
    shown = dialog(parent)
    assert shown.startswith('Independent review of this plan: REVISE\n- '+FINDING['claim']+' (Files: a.py)\n')
    assert 'only advises' in shown and shown.index('Plan.') > shown.index(FINDING['claim'])


def test_a_revise_with_findings_makes_the_planner_revise_once_and_the_revised_plan_is_what_returns(conversation):
    tool, control, stub, parent, state = conversation
    value = delegate(conversation)
    assert stub.roles() == ['planner', 'plan-auditor', 'revision']  # one revision, and no second review
    assert value['status'] == 'PLANNED' and value['plan'] == REVISED and value['model_calls'] == 2
    assert value['artifact'].endswith('revision\\artifact.json') or value['artifact'].endswith('revision/artifact.json')
    assert value['review'] == {'status': 'REVIEWED', 'verdict': 'REVISE', 'findings': [FINDING], 'route': 'fixture-critic',
        'model_calls': 3, 'independent': True, 'revised': True, 'revision_route': 'worker', 'revision_model_calls': 2}
    assert value['next'].endswith(planning_tools.CRITIC_REVISED_NEXT) and planning_tools.CRITIC_NEXT not in value['next']
    # the revision is the planner's route, with the first plan attached as advice and the findings in the mission
    config, deployment, root, brief, opts = stub.sessions[2]
    first = stub.sessions[0]
    assert deployment['id'] == first[1]['id'] and root == next((parent/'planning').glob('*/revision'))
    assert config['role'] == 'planner' and config['writable'] == {} and config['capabilities'] == ['read', 'list', 'search']
    assert len(config['input_artifacts']) == 1 and config['input_artifacts'][0]['artifact']['role'] == 'planner'
    mission = json.loads(brief.rsplit('\n', 1)[-1])
    assert mission['critic_findings'] == [FINDING['claim']] and mission['critic_note'] == planning_tools.CRITIC_REVISION
    assert mission['previous_plan'] == planning_tools.PREVIOUS_PLAN
    assert opts['delegated_leaf'] is True and opts['limits']['max_model_calls'] == 2 and opts['limits']['max_seconds'] == 600
    # charged like the planner's own calls (2 + 3 + 2), recorded in the receipt, and a child like the others
    assert tool.calls_committed() == 7 and receipt(parent)['calls_used'] == 7
    assert receipt(parent)['result']['plan'] == REVISED and receipt(parent)['result']['review'] == value['review']
    assert len(planning_tools.children(parent)) == 3
    # a second read of the same effect pays for nothing
    assert delegate(conversation) == value and len(stub.sessions) == 3
    # the dialog and the saved plan carry the revised plan, under the review of the first
    shown = dialog(parent, 'revision')
    assert shown.startswith('Independent review of the first plan: REVISE\n- '+FINDING['claim']+' (Files: a.py)\n'
        'The planner revised the plan once with these findings; the plan below is the revised one.\n\nRevised plan.')


@pytest.mark.parametrize('verdict', [
    {'verdict': 'ACCEPT', 'summary': 'Sound.', 'findings': [], 'remaining': [], 'sources': []},
    {'verdict': 'INCONCLUSIVE', 'summary': 'Unsure.', 'findings': [FINDING], 'remaining': ['x'], 'sources': []}])
def test_a_review_that_is_not_a_revise_triggers_no_revision(conversation, verdict):
    tool, control, stub, parent, state = conversation
    stub.verdict = verdict
    value = delegate(conversation)
    assert stub.roles() == ['planner', 'plan-auditor'] and value['plan'] == PLAN and 'revised' not in value['review']
    assert (planning_tools.CRITIC_NEXT in value['next']) == bool(verdict['findings'])


@pytest.mark.parametrize('how', ['exhausted', 'raises', 'no-tasks'])
def test_a_revision_that_fails_returns_the_first_plan_with_the_findings_and_is_charged(conversation, how):
    tool, control, stub, parent, state = conversation
    if how == 'exhausted':
        stub.revision = {'status': 'EXHAUSTED', 'reason': 'Out of calls.', 'failure_code': 'CALL_LIMIT'}
    elif how == 'raises':
        stub.revision = {'status': 'PROPOSED', 'raises': OSError('disk')}
    else:
        stub.revised = {**REVISED, 'tasks': []}
    value = delegate(conversation)
    assert stub.roles() == ['planner', 'plan-auditor', 'revision']
    assert value['plan'] == PLAN and value['next'].endswith(planning_tools.CRITIC_NEXT)
    assert value['review']['findings'] == [FINDING] and value['review']['revised'] is False
    assert value['review']['revision_reason'] == {'exhausted': 'CALL_LIMIT', 'raises': 'REVISION_ERROR', 'no-tasks': 'NO_TASKS'}[how]
    # a crash cannot tell what it spent, so the reserved 2 are counted; otherwise its own 2
    assert tool.calls_committed() == 7 and receipt(parent)['status'] == 'FINISHED'
    assert delegate(conversation) == value and len(stub.sessions) == 3  # never repeated
    assert dialog(parent).startswith('Independent review of this plan: REVISE')


@pytest.mark.parametrize('how', ['unavailable', 'none'])
def test_a_skipped_review_triggers_no_revision(conversation, how):
    tool, control, stub, parent, state = conversation
    stub.pick = ValueError('Pinned plan-auditor unavailable.') if how == 'unavailable' else (None, {})
    value = delegate(conversation)
    assert value['review']['status'] == 'SKIPPED' and stub.roles() == ['planner'] and value['plan'] == PLAN


def test_a_failed_review_triggers_no_revision(conversation):
    tool, control, stub, parent, state = conversation
    stub.outcome = {'status': 'EXHAUSTED', 'reason': 'Out of calls.', 'failure_code': 'CALL_LIMIT'}
    value = delegate(conversation)
    assert value['review']['status'] == 'FAILED' and stub.roles() == ['planner', 'plan-auditor'] and value['plan'] == PLAN


@pytest.mark.parametrize('kind,line', [
    ('INDEPENDENT', 'A reviewer of another vendor accepted this plan.'),
    ('SAME_VENDOR_NOT_INDEPENDENT', 'A reviewer accepted this plan (reviewed by the same vendor: not independent).')])
def test_an_accepting_review_says_so_in_one_line_and_adds_nothing_for_the_coordinator(conversation, kind, line):
    tool, control, stub, parent, state = conversation
    stub.kind = kind
    stub.verdict = {'verdict': 'ACCEPT', 'summary': 'Sound.', 'findings': [], 'remaining': [], 'sources': []}
    value = delegate(conversation)
    assert value['review']['status'] == 'REVIEWED' and value['review']['verdict'] == 'ACCEPT' and value['review']['findings'] == []
    assert 'review.findings' not in value['next'] and stub.roles() == ['planner', 'plan-auditor']
    assert dialog(parent).startswith(line+'\n\nPlan.')


def test_without_a_review_the_dialog_shows_the_plan_alone(conversation):
    tool, control, stub, parent, state = conversation
    stub.pick = (None, {})
    value = delegate(conversation)
    assert value['review']['status'] == 'SKIPPED' and dialog(parent).startswith('Plan.\n\nSteps:')


@pytest.mark.parametrize('how,reason', [
    ('none', 'NO_INDEPENDENT_REVIEWER'), ('off', 'SWITCHED_OFF_FOR_ROLE'), ('self', 'NO_INDEPENDENT_REVIEWER'),
    ('same-family', 'NO_INDEPENDENT_REVIEWER'), ('unavailable', 'REVIEWER_UNAVAILABLE')])
def test_no_eligible_reviewer_means_no_review_and_the_plan_returns_as_it_is(conversation, how, reason):
    tool, control, stub, parent, state = conversation
    if how == 'none':
        stub.pick = (None, {'selection': {'candidates': [{'reason': 'ROLE_NOT_CONFIGURED'}]}})
    elif how == 'off':
        stub.pick = (None, {'selection': {'candidates': [{'reason': 'QUOTA_EXHAUSTED'}, {'reason': 'SWITCHED_OFF_FOR_ROLE'}]}})
    elif how == 'self':
        stub.pick = (dict(stub.reviewer, id='fixture-planner'), {})  # the planner's own model, whatever listed it
    elif how == 'same-family':
        stub.kind = 'AUDITOR_NOT_INDEPENDENT'
    else:
        stub.pick = ValueError('Pinned plan-auditor unavailable.')
    value = delegate(conversation)
    assert value['status'] == 'PLANNED' and value['plan'] == PLAN
    assert value['review']['status'] == 'SKIPPED' and value['review']['reason'] == reason and value['review']['model_calls'] == 0
    assert 'findings' not in value['review'] and stub.roles() == ['planner'] and tool.calls_committed() == 2


@pytest.mark.parametrize('how', ['none', 'self', 'same-family'])
def test_with_no_other_reviewer_the_planners_own_local_model_reviews_only_where_the_user_ticked_it(conversation, monkeypatch, how):
    from relay import environment
    tool, control, stub, parent, state = conversation
    stub.pick = {'none': (None, {}), 'self': (dict(stub.reviewer, id='fixture-planner'), {}),
                 'same-family': stub.pick}[how]
    stub.kind = 'AUDITOR_NOT_INDEPENDENT' if how == 'same-family' else stub.kind
    asked = []
    monkeypatch.setattr(environment, 'granted', lambda role, deployment, value=None: asked.append(
        (role, deployment['id'])) or True)
    value = delegate(conversation)
    assert asked == [('plan-auditor', 'fixture-planner')]
    config, deployment, root, instruction, opts = stub.sessions[1]
    assert deployment['id'] == stub.sessions[0][1]['id'] and instruction == planning_tools.CRITIC_BRIEF
    assert config['role'] == 'plan-auditor' and config['capabilities'] == ['read', 'list', 'search']
    assert value['review']['own_model'] is True and value['review']['independent'] is False
    assert stub.roles() == ['planner', 'plan-auditor', 'revision']  # its REVISE makes the planner revise once too
    assert dialog(parent, 'revision').startswith(
        "Review by the planner's own model as a critic (not independent) of the first plan: REVISE\n")
    assert receipt(parent)['critic']['own_model'] is True


def test_a_planner_whose_model_never_audits_does_not_review_its_own_plan(conversation, monkeypatch):
    from relay import environment, landing_policy
    tool, control, stub, parent, state = conversation
    stub.pick = (None, {})
    monkeypatch.setattr(environment, 'granted', lambda role, deployment, value=None: True)
    def identity(deployment, role):
        raise landing_policy.LandingPolicyRejected('IDENTITY_MODEL_DOES_NOT_AUDIT')
    monkeypatch.setattr(work_audit, 'identity', identity)
    value = delegate(conversation)
    assert value['review']['status'] == 'SKIPPED' and value['review']['reason'] == 'NO_INDEPENDENT_REVIEWER'
    assert stub.roles() == ['planner'] and value['plan'] == PLAN


def test_an_accepting_self_review_and_a_same_vendor_review_never_read_as_independent(conversation, monkeypatch):
    from relay import environment
    tool, control, stub, parent, state = conversation
    stub.pick = (None, {})
    stub.verdict = {'verdict': 'ACCEPT', 'summary': 'Sound.', 'findings': [], 'remaining': [], 'sources': []}
    monkeypatch.setattr(environment, 'granted', lambda role, deployment, value=None: True)
    delegate(conversation)
    assert dialog(parent).startswith("The planner's own model, asked again as a critic, accepted this plan "
                                     "(not independent).\n\nPlan.")


def test_a_same_vendor_revise_is_not_called_independent(conversation):
    tool, control, stub, parent, state = conversation
    stub.kind = 'SAME_VENDOR_NOT_INDEPENDENT'
    stub.revision = {'status': 'EXHAUSTED', 'reason': 'Out of calls.', 'failure_code': 'CALL_LIMIT'}
    delegate(conversation)
    assert dialog(parent).startswith('Review by the same vendor (not independent) of this plan: REVISE\n')


def test_the_shared_call_budget_that_cannot_fund_a_review_skips_it(conversation):
    tool, control, stub, parent, state = conversation
    state['limits']['max_model_calls'] = 6  # 1 parent + 2 planner + 2 reserved leaves 1
    value = delegate(conversation)
    assert value['status'] == 'PLANNED' and value['review']['status'] == 'SKIPPED' and value['review']['reason'] == 'SHARED_BUDGET'
    assert stub.roles() == ['planner'] and tool.calls_committed() == 2


@pytest.mark.parametrize('how', ['exhausted', 'raises', 'invalid'])
def test_a_review_that_fails_never_blocks_the_plan_and_is_still_charged(conversation, how):
    tool, control, stub, parent, state = conversation
    if how == 'exhausted':
        stub.outcome = {'status': 'EXHAUSTED', 'reason': 'Out of calls.', 'failure_code': 'CALL_LIMIT'}
    elif how == 'raises':
        stub.outcome = {'status': 'PROPOSED', 'raises': OSError('disk')}
    else:
        stub.verdict = dict(stub.verdict, verdict='MAYBE')
    value = delegate(conversation)
    assert value['status'] == 'PLANNED' and value['plan'] == PLAN and 'findings' not in value['review']
    assert value['review']['status'] == 'FAILED' and 'review.findings' not in value['next']
    assert value['review']['reason'] == {'exhausted': 'CALL_LIMIT', 'raises': 'REVIEW_ERROR', 'invalid': 'INVALID_VERDICT'}[how]
    # a crash cannot tell what it spent, so the whole reserved allowance (5) is counted; otherwise its 3 calls
    assert tool.calls_committed() == (7 if how == 'raises' else 5) and receipt(parent)['status'] == 'FINISHED'
    assert delegate(conversation) == value and len(stub.sessions) == 2  # never repeated


class Killed(BaseException):
    """The process ends here: nothing in the planner's tool catches it, as nothing catches a killed process."""


def reread(conversation, stub, sessions):
    """What the same effect returns after the restart: nothing more is dispatched, and conversation recovery reads it."""
    tool, control, _, parent, state = conversation
    committed = tool.calls_committed()
    value = delegate(conversation)
    assert len(stub.sessions) == sessions and tool.calls_committed() == committed
    row = planning_tools.record(next((parent/'planning').glob('*/receipt.json')))
    assert row['status'] == 'FINISHED' and row['result'] == value
    assert receipt(parent)['status'] == 'FINISHED' and delegate(conversation) == value  # settled once, then stable
    return value


@pytest.mark.parametrize('written', [False, True])
def test_a_restart_during_the_review_keeps_the_valid_plan_and_never_repeats_the_review(conversation, written):
    tool, control, stub, parent, state = conversation
    stub.outcome = {'status': 'PROPOSED' if written else 'EXHAUSTED', 'raises': Killed()}
    with pytest.raises(Killed):
        tool.execute(request(), control)
    assert stub.roles() == ['planner', 'plan-auditor']
    value = reread(conversation, stub, 2)
    assert value['status'] == 'PLANNED' and value['plan'] == PLAN
    if written:  # the review had finished: kept, and the revision it asked for is not dispatched after a restart
        assert value['review'] == {'status': 'REVIEWED', 'verdict': 'REVISE', 'findings': [FINDING], 'route': 'fixture-critic',
            'model_calls': 3, 'independent': True, 'revised': False, 'revision_reason': 'INTERRUPTED'}
        assert value['next'].endswith(planning_tools.CRITIC_NEXT)
    else:
        assert value['review'] == {'status': 'FAILED', 'route': 'fixture-critic', 'model_calls': 0, 'reason': 'INTERRUPTED'}


@pytest.mark.parametrize('written', [True, False])
def test_a_restart_during_the_revision_uses_a_finished_revision_and_otherwise_keeps_the_first_plan(conversation, written):
    tool, control, stub, parent, state = conversation
    stub.revision = {'status': 'PROPOSED' if written else 'EXHAUSTED', 'raises': Killed()}
    with pytest.raises(Killed):
        tool.execute(request(), control)
    assert stub.roles() == ['planner', 'plan-auditor', 'revision']
    value = reread(conversation, stub, 3)
    assert value['review']['findings'] == [FINDING] and value['review']['verdict'] == 'REVISE'
    if written:
        assert value['plan'] == REVISED and value['artifact'].replace('\\', '/').endswith('revision/artifact.json')
        assert value['review']['revised'] is True and value['review']['revision_model_calls'] == 2
        assert value['next'].endswith(planning_tools.CRITIC_REVISED_NEXT)
        assert dialog(parent, 'revision').startswith('Independent review of the first plan: REVISE')
    else:
        assert value['plan'] == PLAN and value['review']['revised'] is False
        assert value['review']['revision_reason'] == 'INTERRUPTED' and value['next'].endswith(planning_tools.CRITIC_NEXT)


def test_the_critic_and_the_revision_keep_the_planners_admission_and_the_proposal_is_only_evidence(conversation, tmp_path):
    from relay.project_admission import QUARANTINE
    tool, control, stub, parent, state = conversation
    state['config'].update(project_admission=dict(QUARANTINE), folder_access={'grants': str(tmp_path/'grants.json')})
    delegate(conversation)
    planner_config, critic_config, revision_config = (s[0] for s in stub.sessions)
    for config in (planner_config, critic_config, revision_config):  # the policy the parent holds, in every child
        assert config['project_admission'] == QUARANTINE and config['folder_access'] == state['config']['folder_access']
    assert revision_config['repositories'] == planner_config['repositories']  # a planner: the same three keys
    # the project is the one instruction source; the proposal folder is evidence the reviewer reads, never instructions
    assert critic_config['instruction_repositories'] == ['repo'] and set(critic_config['repositories']) == {'repo', 'proposal'}
    assert (critic_config['writable'], critic_config['verifiers']) == ({}, {})
    admitted = native_agent.configuration(critic_config)  # the runtime accepts it as it stands
    assert admitted['project_admission'] == QUARANTINE and admitted['instruction_repositories'] == ['repo']
    # the allowance is for a plan auditor and for nothing else: a planner with evidence roots is still refused
    with pytest.raises(ValueError, match='Project admission requires one project'):
        native_agent.configuration({**critic_config, 'role': 'planner'})
    with pytest.raises(ValueError, match='Project admission requires one project'):
        native_agent.configuration({k: v for k, v in critic_config.items() if k != 'instruction_repositories'})


def test_under_an_unlimited_parent_the_critic_and_the_revision_keep_their_own_ceilings(conversation, monkeypatch):
    from dataclasses import replace
    from relay import execution_scope
    tool, control, stub, parent, state = conversation
    state['limits']['max_model_calls'] = None
    scope = replace(execution_scope.current(), unlimited=('calls', 'time'))
    monkeypatch.setattr(execution_scope, 'current', lambda: scope)
    control = type(control)(control.binding, control.cancellation, 1200, monotonic=lambda: 0)
    value = json.loads(tool.execute(request(), control).output_json)
    assert value['plan'] == REVISED and stub.roles() == ['planner', 'plan-auditor', 'revision']
    assert stub.sessions[0][4]['limits']['max_model_calls'] is None  # the planner itself is lifted, as before
    assert stub.sessions[1][4]['limits'] == {'max_model_calls': 12, 'max_seconds': 600, 'request_seconds': 600}
    assert stub.sessions[2][4]['limits'] == {'max_model_calls': 6, 'max_seconds': 600, 'request_seconds': 600}


def test_the_flows_selector_takes_the_demands_types_for_the_plan_reviewer(monkeypatch):
    seen = []
    monkeypatch.setattr(native_routes, 'reviewers', lambda producer, role, work_type=None: seen.append((role, work_type)) or ([], {}))
    chosen, route = native_plan.select_route('auto', 'plan-auditor', 'WHY', {'id': 'p'}, work_type=['coding'])
    assert chosen is None and route['reason'] == 'NO_ELIGIBLE_NATIVE_ROLE' and seen == [('plan-auditor', ['coding'])]
    native_plan.select_route('auto', 'plan-auditor', 'WHY', {'id': 'p'})
    assert seen[-1] == ('plan-auditor', None)  # /plan passes none: unchanged
