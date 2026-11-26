"""A bounded read-only planner child. Existing sessions, artifacts and usage own its execution."""
from copy import deepcopy
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import time

from compute_plane.native_journal import regular, save_json
from compute_plane.native_models import encode, object_json, sha
from .execution_scope import for_role

# High-effort planning can spend minutes inspecting a multi-file change. Keep
# calls and shared spend bounded independently of its wall-clock allowance.
MAX_CALLS, MAX_SECONDS, MAX_REQUESTS, MAX_TOTAL_ATTEMPTS = 6, 600, 3, 6
RESERVE = 2  # the coordinator must still inspect the handoff and answer


def counts_toward_quota(row):
    """Cancelled planning runs and pre-artifact delivery-validation rejections do not burn MAX_REQUESTS.
    MAX_TOTAL_ATTEMPTS still enforces a bounded ceiling per demand revision."""
    if row.get('status') != 'FINISHED':
        return True
    result = row.get('result') or {}
    if result.get('status') == 'CANCELLED':
        return False
    if (result.get('status') == 'BLOCKED'
            and (result.get('failure_code') == 'INVALID_DELIVERY'
                 or bool(result.get('delivery_validation')))):
        return False
    return True


def _planner_compaction_loop(session_dir, outcome):
    """True when a planner stopped due to context compaction budget or compaction-loop NO_PROGRESS."""
    if not isinstance(outcome, dict) or outcome.get('status') != 'BLOCKED':
        return False
    reason = outcome.get('reason')
    if reason == 'COMPACTION_BUDGET':
        return True
    if reason != 'NO_PROGRESS':
        return False
    journal_path = Path(session_dir) / 'journal.jsonl'
    if not journal_path.is_file():
        return False
    try:
        from compute_plane.native_journal import NativeJournal
        return any(r.event().get('kind') == 'context_activated'
                   for r in NativeJournal(journal_path).snapshot().records)
    except (OSError, ValueError, KeyError, TypeError):
        return False
CRITIC_CALLS, CRITIC_SECONDS = 12, 600  # the critic's own ceiling, inside the shared budget
# The text measured on 2026-11-17 (a planted-defect experiment: it found them and accepted the sound plans): keep it as is.
CRITIC_BRIEF = '''You are the frugal critic of the plan in proposal/plan.json. Read the request in proposal/request.json and the project's files, then judge the plan only against what the request asks and what this project needs to keep working. You look for two things, and nothing else.

BLIND SPOT: something the request asks for, or that the existing code or tests require, that the plan does not cover or gets wrong. Cite the request sentence, or the file and lines.

EXCESS: an item of the plan (a step, a criterion, a test, a validation, a helper, an abstraction, a file) that the request does not ask for and that this change does not need in order to be correct and safe. Name the item, and say what would be lost if it were dropped. If something the request cares about would be lost, it is not excess. Thoroughness that protects a requirement of the request is not excess; work that only protects against cases nobody asked about is.

Rules. Do not rewrite the plan and do not propose a different design of the same size. Do not add wishes of your own: a blind spot must trace to the request or to the code. A plan that is already the right size gets verdict ACCEPT with no findings; finding nothing is a valid and common result, and inventing a finding to look useful is a failure. Report at most the five findings that change the most work, most valuable first. Start each finding's claim with "BLIND SPOT:" or "EXCESS:", then the plan item or the missing piece, the evidence, and the smallest change to the plan (what to add or what to delete). Verdict REVISE only when at least one finding should change the plan before anyone implements it.'''
CRITIC_NEXT = (' A reviewer read this plan and listed problems in review.findings. They are advice, not '
               'orders. Tell the user what they say, and you may ask the planner for a revised plan with delegate_plan.')
CRITIC_REVISED_NEXT = (' A reviewer listed problems in review.findings and the planner revised the plan once '
                       'with them; this is the revised plan. Tell the user what the reviewer said.')
# Measured on 2026-11-17: the findings removed the defect only when the planner revised its plan with them.
CRITIC_REVISION = ('A frugal critic reviewed the previous plan against the request and the project. Its findings are in '
                   'critic_findings: advice, not orders. For each one, decide: apply it, or keep the plan as it is and say '
                   'why in `remaining`. Apply a BLIND SPOT only if the request or the code supports it. Apply an EXCESS by '
                   'deleting the item, not by replacing it with something else. Change nothing the findings do not touch. '
                   'Deliver the complete revised plan.')
PREVIOUS_PLAN = ('The attached prior plan is advisory. Reuse valid work; inspect only changed scope and missing evidence. '
                 'Preserve all current criteria and retain the previous candidate where its paths remain authorized.')
# Creative/asset work types (task_types.json) whose verification is the produced artifact itself, not a code plan audit.
CREATIVE_PLAN_KINDS = frozenset({'art', 'audio', '3d', 'writing'})
# Code/infrastructure work types that always keep independent plan review even when a creative skill is also active.
CODE_REVIEW_KINDS = frozenset({'coding', 'web', 'security', 'engineering', 'games'})


def plan_review_skip_reason(active, plan, deployment, skills=()):
    """Return a skip reason when a conversation plan needs no independent critic, else None.
    Local planners, critical tasks, and code/infrastructure work are always reviewed (owner, 2026-11-17 / 2026-11-23)."""
    from .challenger import local
    from .managed_skills import skills_skip_plan_review
    tasks = plan.get('tasks') or []
    if not tasks or local(deployment) or any(t.get('critical') for t in tasks):
        return None
    demand_kinds = set((active or {}).get('kinds') or ())
    task_kinds = {t['kind'] for t in tasks if t.get('kind')}
    skill_opt_out = skills_skip_plan_review(skills)
    if task_kinds & CODE_REVIEW_KINDS:
        return None
    # When every planned task is explicitly creative (art/audio/3d/writing), honor the plan even if the
    # pre-plan demand also included 'coding' (e.g. pxl/tune "art/audio made by code"), provided the demand
    # itself named a creative kind or an opt-out skill is active and no security/web/engineering/games kind applies.
    all_tasks_creative = all(t.get('kind') in CREATIVE_PLAN_KINDS for t in tasks)
    if all_tasks_creative and not (demand_kinds & (CODE_REVIEW_KINDS - {'coding'})):
        if demand_kinds & CREATIVE_PLAN_KINDS:
            return 'CREATIVE_WORK'
        if skill_opt_out:
            return 'SKILL_OPT_OUT'
    all_kinds = demand_kinds | task_kinds
    if all_kinds & CODE_REVIEW_KINDS:
        return None
    if all_kinds and all_kinds <= CREATIVE_PLAN_KINDS:
        return 'CREATIVE_WORK'
    if skill_opt_out:
        return 'SKILL_OPT_OUT'
    return None


def handoff(artifact, plan):
    """Bound the inline reply, not the already bounded canonical planning artifact."""
    result={'status':'PLANNED','artifact':str(artifact),
            'next':'Review this advice and continue here. No implementation or Work was admitted.'}
    if len(encode(plan).encode())<=16384:
        result['plan']=plan
    else:
        raw=Path(artifact).read_bytes()
        result.update(plan_reference={'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)},
                      summary=plan['summary'][:1000],summary_truncated=len(plan['summary'])>1000)
        result['next']='The complete plan is in the referenced artifact; the host compiles all tasks and criteria from it. '+result['next']
    if plan.get('signals'):
        from .agent_signals import stamp_signals
        result['agent_signals'] = stamp_signals('planner', plan['signals'], source_id=str(artifact))
    return result
SPEC = {'delegate_plan': {
    'description': 'Ask a separate read-only planner for a bounded plan, then continue here. Give the specific '
        'question and relevant findings you already have; do not send the whole conversation. Use it also for '
        'read-only design comparison before recommending substantive changes. Instructions preserve the user goal '
        'and confirmed decisions; context separates observed facts from unvalidated suggestions. The planner cannot '
        'edit, execute commands, admit Works or delegate. Its calls/time share your budget. '
        'Even a small delivery uses this separate planner. For an authorized delivery, request the executable '
        'plan in this call, including any missing read-only exploration; do not use this tool as an explorer '
        'and postpone the plan to another user request. This returns a plan, not implementation.',
    'parameters': {'type': 'object', 'properties': {
        'instruction': {'type': 'string', 'minLength': 1},
        'context': {'type': 'string', 'description': 'Observed findings, source locations and constraints already known. '
            'Label assumptions and proposed solutions as hypotheses to compare, not user decisions.'},
        'route': {'type': 'string', 'description': 'One configured planner route; omitted uses the first available one.'},
        'effort': {'type': 'string', 'enum': ['default', 'high', 'xhigh', 'max'],
                   'description': 'Minimum high. Omit or use default to keep a higher configured level. '
                       'Only use an explicit level supported by the selected connection.'}},
        'required': ['instruction'], 'additionalProperties': False}}}


def incomplete_input(root, active):
    """Retain completed same-demand findings as advice when executable planning was omitted."""
    from .native_agent import read_json
    from .native_artifacts import read_input
    found = []
    for receipt in (Path(root)/'planning').glob('*/receipt.json'):
        try:
            row = read_json(regular(receipt), 65536)
            result = row.get('result') or {}
            if (row.get('status') != 'FINISHED' or row.get('demand_id') != active['id']
                    or result.get('status') != 'PLANNED' or (result.get('plan') or {}).get('tasks')):
                continue
            artifact = regular(Path(result['artifact']))
            if artifact.resolve() not in {(receipt.parent/n/'artifact.json').resolve() for n in ('session', 'session-2', 'revision')}:
                continue
            value = read_input(artifact)
            if value['artifact']['role'] != 'planner' or value['artifact']['payload'] != result.get('plan'):
                continue
            found.append((receipt.stat().st_mtime_ns, {'artifact': str(artifact), 'sha256': value['sha256']}))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return max(found, key=lambda item:item[0])[1] if found else None


def configured():
    from .native_agent import read_json
    from .turn_engine import policy_path
    path = policy_path()
    return path.exists() and 'planner' in read_json(path, 65536)


def snapshot():
    """The planner routes a session freezes at its start. Any account's: the flags, the routing profile and pins limit
    them where the planner is chosen (owner, 2026-11-14)."""
    from . import native_catalog
    from .turn_engine import planner_routes, policy
    from .execution_scope import current, pinned
    if current().pin('planner'):
        with for_role('planner'):
            value = pinned('planner')
        name = value['native_model_choice']['connection']
        return {'routes': {name: value}, 'route_order': [name], 'origin': 'EXACT_TEMPORARY_PIN'}
    routes = {}
    for name in planner_routes():
        try:
            routes[name] = native_catalog.select(name)
        except (ValueError, OSError, KeyError, TypeError):
            continue
    # The role table's order already puts a lapsing allowance in surplus first, after the class. The owner's own list
    # gets it here: a Claude week ending today was left unused while planning went to a Codex week with 6% left.
    from .turn_engine import surplus_first
    return {'routes': routes, 'route_order': surplus_first(list(routes)) if policy().get('planner') else list(routes)}


def _operator_messages(root, *, max_bytes=32768):
    """Verbatim operator messages from the parent journal, omitting host-injected notices.

    Always preserves the initial request and newest follow-ups inside `max_bytes`.
    Returns (messages, truncated).
    """
    path = Path(root)/'journal.jsonl'
    if not path.is_file():
        return [], False
    try:
        from compute_plane.native_journal import NativeJournal
        records = NativeJournal(path).snapshot().records
    except (OSError, ValueError, KeyError, TypeError):
        return [], False
    raw = []
    marker = '\n\nOperator message:\n'
    for record in records:
        event = record.event()
        kind = event.get('kind')
        if kind == 'session_started':
            instruction = event.get('instruction') or ''
            if marker in instruction:
                text = instruction.split(marker, 1)[1].strip()
                if text:
                    raw.append(text)
        elif kind == 'user_message':
            msg_id = str(event.get('message_id') or '')
            text = (event.get('text') or '').strip()
            if (not text or msg_id.startswith(('consent-decision-', 'folder-granted-', 'correction-'))
                    or text.startswith('Host-observed changes to previously read files. Host dialog choice:')):
                continue
            raw.append(text)
    if not raw or max_bytes <= 0:
        return [], bool(raw)
    if sum(len(m.encode('utf-8')) for m in raw) <= max_bytes:
        return raw, False
    # Keep the opening request and newest follow-ups within max_bytes.
    def _clip_utf8(text, budget):
        encoded = text.encode('utf-8')
        if len(encoded) <= budget:
            return text, False
        return encoded[:budget].decode('utf-8', errors='ignore'), True

    first_budget = min(max_bytes, max(max_bytes // 2, max_bytes - sum(len(m.encode('utf-8')) for m in raw[1:])))
    first, first_cut = _clip_utf8(raw[0], first_budget)
    used = len(first.encode('utf-8'))
    tail = []
    for msg in reversed(raw[1:]):
        remaining = max_bytes - used
        if remaining <= 0:
            break
        piece, cut = _clip_utf8(msg, remaining)
        if piece:
            tail.append(piece)
            used += len(piece.encode('utf-8'))
        if cut:
            break
    kept = [first, *reversed(tail)]
    return kept, True


def _check_label(check, prefix):
    argv = check.get('argv') if isinstance(check, dict) else None
    if isinstance(argv, list) and argv:
        cmd = ' '.join(Path(argv[0]).name if i == 0 else str(a) for i, a in enumerate(argv))
        return f'{prefix} ({cmd[:160]})'
    return prefix


def review_of(out, payload, independent):
    """The critic's verdict as the coordinator reads it: at most five findings, each a claim and the files to change."""
    if payload.get('verdict') not in ('ACCEPT', 'REVISE', 'INCONCLUSIVE'):
        return {**out, 'status': 'FAILED', 'reason': 'INVALID_VERDICT'}
    result = {**out, 'status': 'REVIEWED', 'verdict': payload['verdict'], 'independent': independent,
              'findings': [{'claim': f['claim'][:1500], 'paths': list(f.get('paths') or [])[:10]}
                           for f in payload.get('findings') or []][:5]}
    if payload.get('signals'):
        from .agent_signals import stamp_signals
        result['agent_signals'] = stamp_signals('plan-auditor', payload['signals'], source_id=str(out.get('route') or 'plan-auditor'))
    return result


def settled(row, job):
    """A receipt an interruption left RUNNING after the planner delivered but before the review and revision ended
    (the planner's own result is saved before either starts): what completed stands and nothing is dispatched again.
    A review that finished is kept, a revision artifact that completed is the plan, and an unfinished review or
    revision is not repeated: the plan that is valid stands, the original with whatever findings were recorded.
    Pure: it returns the receipt as it would be finished."""
    mark = row.get('critic')
    if row.get('status') != 'RUNNING' or mark is None or not row.get('result'):
        return row
    from .delegation_tools import count_calls
    from .native_artifacts import read_input

    def done(folder):
        try:
            return read_input(Path(job)/folder/'artifact.json')['artifact']['payload']
        except (OSError, ValueError, KeyError, TypeError):
            return None
    result = deepcopy(row['result'])
    review = result.get('review')
    if review is None:  # interrupted during the review
        out = {'status': 'FAILED', 'route': mark.get('route'), 'model_calls': 0, 'reason': 'INTERRUPTED',
               **({'own_model': True} if mark.get('own_model') else {})}
        if (payload := done('review/session')) is not None:
            session = Path(job)/'review/session'
            out['model_calls'] = count_calls(session) if (session/'journal.jsonl').is_file() else 0
            review = review_of({k: v for k, v in out.items() if k != 'reason'}, payload, mark.get('independent') is True)
        result['review'] = review = review or out
        if review.get('findings'):
            result['next'] += CRITIC_NEXT
    if review.get('verdict') == 'REVISE' and review.get('findings') and 'revised' not in review:
        plan = done('revision')  # interrupted during the revision
        if plan and plan.get('tasks'):
            revision = Path(job)/'revision'
            review.update(revised=True, revision_route=row['route'], revision_model_calls=
                          count_calls(revision) if (revision/'journal.jsonl').is_file() else 0)
            result = {**handoff(revision/'artifact.json', plan), 'review': review,
                      'route': result['route'], 'model_calls': result['model_calls']}
            result['next'] += CRITIC_REVISED_NEXT
        else:
            review.update(revised=False, revision_reason='INTERRUPTED')
    return {**row, 'status': 'FINISHED', 'result': result}


def record(path):
    """Project a proven explicit child resume without rewriting its old receipt."""
    from .native_agent import read_json
    path=regular(Path(path));row=settled(read_json(path,65536),path.parent)
    if row.get('status')=='RUNNING' and row.get('continuation_of'):
        original=regular(Path(row['continuation_of']))
        if (original.parent.parent==path.parent.parent and original.parent!=path.parent
                and original.name=='receipt.json' and original.is_file()
                and hashlib.sha256(original.read_bytes()).hexdigest()==row.get('source_receipt_sha256')
                and not read_json(original,65536).get('continuation_of')):
            source=record(original)
            if (source.get('result') or {}).get('status') in {'PLANNED','HANDOFF_TOO_LARGE'}:
                return {**row,'status':'FINISHED','result':source['result'],'calls_used':0,
                    'continuation_reconciled_from':str(original)}
        return row
    if row.get('status')!='FINISHED' or (row.get('result') or {}).get('status') not in {
            'RECONCILIATION_REQUIRED','INTERRUPTED','BLOCKED'}:
        return row
    try:
        from compute_plane.native_journal import NativeJournal
        from .native_artifacts import read_input,validate
        session=path.parent/'session'
        events=[r.event() for r in NativeJournal(session/'journal.jsonl').snapshot().records]
        calls=sum(e.get('kind') in {'model_requested','context_summary_requested'} for e in events)
        # A failed or unproven resume still consumed calls. Projection of a
        # successful handoff is stricter than accounting for its actual requests.
        row={**row,'calls_used':max(row.get('calls_used',row['calls_reserved']) or 0,calls)}
        state=read_json(session/'native-agent.json',1024*1024)
        outcome=read_json(session/'outcome.json',4*1024*1024)
        invocation=state['invocations'][-1];config=state['config']
        if (len(state['invocations'])<2 or config['role']!='planner' or config.get('writable')
                or config.get('verifiers') or config.get('terminal')
                or set(config.get('capabilities',[]))-{'read','list','search','context'}
                or invocation.get('status')!='PROPOSED' or not invocation.get('ended_at')
                or outcome.get('status')!='PROPOSED' or outcome.get('invocation_id')!=invocation['id']
                or (state['deployment'].get('native_model_choice') or {}).get('connection',state['deployment']['id'])!=row['route']):
            return row
        resumed=events[invocation['journal_start']:]
        if row['result'].get('status') == 'BLOCKED':
            # An explicit continuation after a proven preflight refusal has no
            # abandoned remote request to acknowledge. Never infer no dispatch
            # merely from the top-level error label.
            prior = events[:invocation['journal_start']]
            failed = prior[-1] if prior else {}
            from .native_model_recovery import CLI_PREFLIGHT_ERRORS, completed_delivery_refusal
            observed = [r.event() for r in NativeJournal(session/'observations.jsonl').snapshot().records]
            local_refusal = (row['result'].get('failure_code') == 'INVALID_DELIVERY'
                and completed_delivery_refusal(prior, observed, state['session_id'])
                and any(e.get('kind') == 'user_message' and e.get('interrupts_turn') is True for e in resumed))
            preflight = (failed.get('kind') == 'model_failed' and failed.get('not_started') is True
                    and failed.get('code') in CLI_PREFLIGHT_ERRORS
                    and failed.get('code') == row['result'].get('reason')
                    and any(e.get('kind') == 'model_requested' and e.get('call_id') == failed.get('call_id') for e in prior)
                    and any(e.get('kind') == 'model_retry_authorized'
                            and e.get('call_id') == failed.get('call_id')
                            and e.get('resolution_id') == invocation['id'] for e in resumed))
            blocked = any(i.get('status') == 'BLOCKED' and i.get('journal_start', 0) < invocation['journal_start']
                          for i in state['invocations'][:-1])
            if (not blocked
                    or not (local_refusal or preflight)):
                return row
        elif not any(e.get('kind')=='model_abandoned' and e.get('resolution_id')==invocation['id'] for e in resumed):
            return row
        if any(cap is not None and row['calls_used']>cap for cap in (row['calls_reserved'],state['budget']['max_model_calls'])):
            return row
        artifact=regular(session/'artifact.json');value=read_input(artifact);a=value['artifact']
        immutable=regular(Path(outcome['artifact_file']))
        if (immutable.parent!=session/'artifacts' or immutable.read_bytes()!=artifact.read_bytes()
                or a['role']!='planner' or a['session_id']!=state['session_id']
                or a['session_id']!=outcome['session_id'] or a['profile_digest']!=outcome['profile_digest']):
            return row
        replies=[e for e in resumed if e.get('kind')=='model_returned' and e.get('call_id')==a['call_id']]
        if len(replies)!=1 or validate('planner',object_json(replies[0]['reply']['metadata_json'])['delivery_payload'])!=a['payload']:
            return row
        return {**row,'result':{**handoff(artifact,a['payload']),
            'route':row['route'],'model_calls':calls},'explicit_resume':{
                'invocation_id':invocation['id'],'source_receipt_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                'prior_result':row['result'],'artifact_sha256':value['sha256']}}
    except (OSError,ValueError,KeyError,TypeError,IndexError):
        return row


def records(root):
    return [record(p) for p in sorted(regular(Path(root)/'planning').glob('*/receipt.json'))]


def fresh_continuation(root, active):
    """A new human turn, consumed by demand intake, must follow the previous dispatch."""
    from compute_plane.native_journal import NativeJournal
    events = NativeJournal(Path(root)/'journal.jsonl').snapshot().records
    human = [(r.sequence, r.event().get('message_id')) for r in events if r.event()['kind']=='user_message']
    planners = [r.sequence for r in events if r.event()['kind']=='effect_requested'
        and r.event()['tool_call']['name']=='delegate_plan'
        and (Path(root)/'planning'/hashlib.sha256(r.event()['effect_id'].encode()).hexdigest()[:24]/'receipt.json').is_file()]
    return bool(human and planners and human[-1][0] > max(planners)
                and human[-1][1] == (active or {}).get('intent_input'))


def source_requalification(root, config, active, prior):
    """One bounded repair after fresh human continuation and proven stale plan facts."""
    from .conversation_tasks import source_state
    plan = (active or {}).get('plan') or {}
    if not plan or (active.get('delivery') or {}).get('applied') or not fresh_continuation(root, active):
        return None
    path = regular(Path(plan['artifact']))
    if (not path.resolve().is_relative_to(Path(root).resolve()/'planning')
            or hashlib.sha256(path.read_bytes()).hexdigest() != plan['sha256']):
        return None
    changed = []
    unit = plan.get('unit') or {}
    for source in plan.get('sources', []):
        repo, name = source['repository'], source['path']
        if repo not in config['repositories'] or repo==unit.get('repository') and name in unit.get('target_paths', []):
            continue
        observed = source_state(config, {repo: [name]})[repo+':'+name]
        if observed != source['sha256']:
            changed.append({**source, 'current_sha256': observed})
    if not changed:
        return None
    identity = sha({'plan': plan['sha256'], 'changed_sources': changed})
    if any(r.get('source_requalification')==identity for r in prior):
        return None
    return {'identity': identity, 'prior': plan, 'changed_sources': changed}


def route_requalification(root, active, prior, routes, name, deployment):
    """One failed-plan recovery after a human launch preference changes transport; no budget reset."""
    from .execution_scope import current, kind
    scope = current()
    if (not active or active.get('plan') or (active.get('delivery') or {}).get('applied')
            or scope.prefer not in {'api', 'subscription'} or kind(deployment) != scope.prefer
            or not fresh_continuation(root, active) or any(r.get('route_requalification') for r in prior)):
        return None
    for row in prior:
        old = routes.get(row.get('route'))
        if (row.get('status') != 'FINISHED' or row.get('result', {}).get('status') not in
                {'EXHAUSTED', 'BLOCKED', 'INTERRUPTED', 'INVALID_DELIVERY'} or old is None
                or row.get('route') == name or kind(old) == scope.prefer):
            return None
    return {'route': name, 'transport': scope.prefer,
            'prior_requests': [r['request_sha256'] for r in prior if r.get('request_sha256')]}


def children(root):
    """The planner sessions (including any escalated attempt) and, beside each, the critic's and the revision's."""
    return [regular(p.parent/name) for p in regular(Path(root)/'planning').glob('*/receipt.json')
            for name in ('session', 'session-2', 'review/session', 'revision') if regular(p.parent/name/'native-agent.json').is_file()]


def committed(root):
    from .delegation_tools import count_calls
    from .execution_resources import calls_used
    total = 0
    for p in regular(Path(root)/'planning').glob('*/receipt.json'):
        row = record(p)
        used = calls_used(row, p.parent/'session')
        second = p.parent/'session-2'
        if (second/'journal.jsonl').is_file() and (row.get('status') == 'RUNNING' or row.get('calls_used') is None):
            first = count_calls(p.parent/'session') if (p.parent/'session'/'journal.jsonl').is_file() else 0
            used = max(used, first + count_calls(second))
        total += used
    return total


def configure_model(profile):
    """Register an immutable picker choice using existing profile and routing stores."""
    from .native_generation import load_deployment
    from .native_profiles import install, local_path
    from .turn_engine import ROUTE_ROLES, policy, save_policy
    value = load_deployment(profile)
    from .execution_team import reasoning
    from .planner_policy import LEVELS
    if reasoning(value) not in LEVELS:
        raise ValueError('Planner minimum reasoning is high. Choose high or greater before saving.')
    name = 'planner-' + sha(value)[:24]
    target = local_path(name)
    if target.exists():
        if load_deployment(target) != value:
            raise ValueError('Saved planner profile identity changed.')
    else:
        install(name, profile)
    selected = policy()
    roles = {role: [item if isinstance(item, list) else [item] for item in selected.get(role, [])] for role in ROUTE_ROLES}
    roles['planner'] = [[name]]
    save_policy(roles)
    return name


class PlanningTools:
    def __init__(self, tools, root, state):
        self.tools, self.root, self.state = tools, Path(root), state
        # Repair the former coordinator-as-planner fallback on resume without
        # rewriting the saved tool schema, system prefix or conversation.
        self.planning = (snapshot()
            if state.get('conversation_flow') and 'session-planner' in state['planning']['routes']
            else state['planning'])
        self.parent_model = None
        self.names = (*tools.names, 'delegate_plan')
        spec = deepcopy(SPEC)
        names = self.route_names()
        if names:
            spec['delegate_plan']['parameters']['properties']['route']['enum'] = names
            from .model_choices import describe
            from .planner_policy import LEVELS
            levels = {name: [level for level in describe(name, self.planning['routes'][name])['route']['efforts']
                             if level in LEVELS] for name in names}
            spec['delegate_plan']['description'] += ' Supported effort overrides by route: '+encode(levels)+'. Omission preserves the configured settings.'
        self.tool_specs_json = encode({**object_json(tools.tool_specs_json), **spec})

    def __getattr__(self, name):
        return getattr(self.tools, name)

    def route_names(self):
        # JSON object keys are sorted in custody. Route preference and tool enum
        # order are protocol state: preserve them explicitly across resume.
        return self.planning.get('route_order', sorted(self.planning['routes']))

    def calls_committed(self):
        from .delegation import child_calls
        return child_calls(self.root)

    def spend_committed(self):
        inner = getattr(self.tools, 'spend_committed', None)
        from .native_spend import total_amounts
        return total_amounts([*(r.get('amount_committed', '0') for r in records(self.root)),
                              inner() if callable(inner) else Decimal(0)])

    @for_role('planner')
    def choose(self, args, kinds=None, *, min_trigger_limit=None, exclude=()):
        """`kinds`: the recorded demand's types of work, which order only an automatic choice (no pin, no route the
        coordinator chose, no temporary policy, no list the owner saved)."""
        from .turn_engine import available, planner_routes, policy
        from .work_units import _effective_trigger_limit
        routes = self.planning['routes']
        trace = getattr(self, '_trace', None)
        chosen = args.get('route')
        if chosen == 'session-planner' and 'session-planner' not in routes:
            chosen = None  # legacy frozen schemas still expose this former default
        if chosen and chosen not in routes:
            from .execution_scope import current
            if not current().pin('planner'):
                raise ValueError('Choose a configured planner route in /delegation or /routes.')
        from .execution_scope import current,planner_candidates
        scope=current()
        if scope.pin('planner'):
            from .execution_scope import pinned
            from .planner_policy import effective
            value=effective(pinned('planner'),args.get('effort'))
            connection=value['native_model_choice']['connection']
            if chosen and chosen!=connection:
                raise ValueError('Requested route conflicts with --planner; consult tare models --role planner.')
            if connection in exclude:
                raise ValueError('Pinned planner route cannot be escalated automatically.')
            if min_trigger_limit is not None:
                cand_trigger = _effective_trigger_limit(value, ephemeral=True)
                if cand_trigger is None or cand_trigger <= min_trigger_limit:
                    raise ValueError('Pinned planner route does not have a larger context window.')
            if trace is not None:
                trace.update(mode='EXACT_TEMPORARY_PIN', visited=[connection], skipped=[])
            return connection,value
        candidates=planner_candidates(self.planning)
        if not scope.changes('planner'):
            candidates=[(chosen,routes[chosen])] if chosen else [(n,routes[n]) for n in self.route_names()]
        elif chosen:
            matched=[(n,d) for n,d in candidates if n==chosen]
            if not matched:
                _,reason=scope.allows(routes[chosen],'planner')
                raise ValueError((reason or 'Requested route is excluded by the temporary execution policy.')
                                 +' Choose an allowed planner route or omit route.')
            candidates=matched
        mode = 'TEMPORARY_POLICY' if scope.changes('planner') else ('CONFIGURED' if chosen else 'AUTOMATIC')
        if mode == 'AUTOMATIC' and kinds and not policy().get('planner'):
            # The session's routes in the order the role table ranks them now for the demand's types: a route that
            # falls short of frontier at any of them goes after (owner, 2026-11-16: a planner that knows nothing of the
            # type is no use, however strong in general). A route no longer ranked keeps its place after them.
            order = planner_routes(kinds)
            candidates.sort(key=lambda item: order.index(item[0]) if item[0] in order else len(order))
        visited = []
        skipped = []
        refused = None  # the coordinator's chosen planner, switched off for the planner by the routing profile
        for name,frozen in candidates:
            visited.append(name)
            if name in exclude:
                skipped.append((name, 'COMPACTION_LOOP_ESCALATED'))
                continue
            current, _ = (deepcopy(frozen), None) if name == 'session-planner' else available(name)
            if current is None or current.get('native_profile') != frozen.get('native_profile'):
                skipped.append((name, 'UNAVAILABLE_OR_PROFILE_CHANGED'))
                continue  # frozen settings, current credential/CLI/GPU availability (it applies --*_only)
            from .execution_scope import refusal
            if off := refusal('planner', frozen, name):  # a pin returned above; a model's choice is not the user's
                skipped.append((name, 'SWITCHED_OFF_FOR_ROLE'))
                if name == chosen:
                    refused = off
                continue
            from .planner_policy import effective
            try:
                value = effective(frozen, args.get('effort'))
            except ValueError:
                if chosen or args.get('effort') not in (None, 'default'):
                    raise
                skipped.append((name, 'REASONING_BELOW_FLOOR'))
                continue  # An incapable preferred route must not hide a capable next planner.
            if min_trigger_limit is not None:
                cand_trigger = _effective_trigger_limit(value, ephemeral=True)
                if cand_trigger is None or cand_trigger <= min_trigger_limit:
                    skipped.append((name, 'CONTEXT_WINDOW_NOT_LARGER'))
                    continue
            if trace is not None:
                trace.update(mode=mode, visited=visited, skipped=skipped)
            return name, value
        if refused:
            raise ValueError('SWITCHED_OFF_FOR_ROLE: '+refused+'. No model call was made.')
        if scope.only == 'local':
            raise ValueError('No local planner is available in local-only mode ('+(
                ', '.join(n+': '+why for n, why in skipped) or 'no local model is admitted as planner')+
                '). No model call was made.')
        raise ValueError('No configured planner with at least high reasoning is available on this allowance. '
                         'Choose a capable planner in /delegation.')

    def run(self, request, args, control):
        from .delegation_tools import count_calls
        from .native_agent import read_json, run_session
        from .work_units import local_slot
        job = regular(self.root/'planning'/hashlib.sha256(request.effect_id.encode()).hexdigest()[:24])
        receipt_path = regular(job/'receipt.json')
        identity = sha(args)
        if receipt_path.exists():
            row = read_json(receipt_path, 65536)
            if row['request_sha256'] != identity:
                raise ValueError('Planning effect arguments changed.')
            if (finished := settled(row, job)) is not row:
                save_json(receipt_path, finished)  # what the interruption left unfinished is settled once
                row = finished
            return row.get('result') or {'status': 'INTERRUPTED', 'reason': 'Prior planning may be charged; it is not repeated.'}
        demand_id = None;revision=0;active=None
        if self.state.get('conversation_flow'):
            from .conversation_tasks import current, load
            active = current(load(self.root))
            demand_id = active['id'] if active else None
            revision=active.get('planning_revision',0) if active else 0
        prior = records(self.root)
        requalification = None
        cohort = [r for r in prior if r.get('demand_id') == demand_id and r.get('planning_revision',0)==revision]
        quota_used = sum(counts_toward_quota(r) for r in cohort)
        hard_exhausted = len(cohort) >= MAX_TOTAL_ATTEMPTS
        exhausted = quota_used >= MAX_REQUESTS or hard_exhausted
        if exhausted:
            requalification = source_requalification(self.root, self.state['config'], active, prior) if active else None
        trace = {}
        self._trace = trace
        try:
            name, deployment = self.choose(args, (active or {}).get('kinds'))
        finally:
            self._trace = None
        route_repair = route_requalification(self.root, active, cohort, self.planning['routes'], name, deployment) if exhausted and not requalification else None
        if exhausted and not requalification and not route_repair:
            if hard_exhausted and quota_used < MAX_REQUESTS:
                raise ValueError(f'This demand reached the maximum of {MAX_TOTAL_ATTEMPTS} planning attempts; use the recorded plans or clarify the unresolved scope.'
                                 if self.state.get('conversation_flow') else
                                 f'This session reached the maximum of {MAX_TOTAL_ATTEMPTS} planning attempts; use the recorded plans or plan directly.')
            raise ValueError('This demand already delegated planning three times; use the recorded plans or clarify the unresolved scope.'
                             if self.state.get('conversation_flow') else
                             'This session already delegated planning three times; use the recorded plans or plan directly.')
        from . import execution_resources as resources
        calls = resources.child_calls(MAX_CALLS)
        maximum = self.state['limits']['max_model_calls']
        if maximum is not None:
            calls = resources.ceiling('calls', calls, maximum-count_calls(self.root)-resources.known_calls(self.calls_committed())-RESERVE)
        seconds = resources.child_seconds(MAX_SECONDS, control.remaining_seconds(), reserve=5)
        if self.state.get('deadline_utc') is not None:
            seconds = resources.ceiling('time', seconds, self.state['deadline_utc']-time.time()-5)
        if calls is not None and calls < 2 or seconds is not None and seconds < 10 or control.should_stop():
            raise ValueError('Not enough shared budget for planning and the coordinator response.')
        previous_input=None
        prior_input = (active.get('replan_from') or (requalification or {}).get('prior')
                       or incomplete_input(self.root, active)) if active else None
        if prior_input:
            from .native_artifacts import read_input
            prior=prior_input;path=regular(Path(prior['artifact']))
            if not path.resolve().is_relative_to(self.root.resolve()/'planning'):
                raise ValueError('The previous plan is outside this conversation.')
            previous_input=read_input(path)
            if previous_input['sha256']!=prior['sha256']:
                raise ValueError('The previous planner artifact changed; preserve its evidence before replanning.')
        allowance, parent_spend = self.allowance(deployment)
        row = {'status': 'RUNNING', 'request_sha256': identity, 'route': name,
               'calls_reserved': calls, 'amount_committed': allowance}
        if demand_id is not None:
            row['demand_id'] = demand_id
            row['planning_revision']=revision
        if requalification:
            row['source_requalification'] = requalification['identity']
        if route_repair:
            row['route_requalification'] = route_repair
        from .native_routes import executed_attempt, executed_decision
        row['executed_decision'] = executed_decision(trace.get('mode'), name, trace.get('visited', [name]), trace.get('skipped', []))
        row['attempts'] = [executed_attempt(deployment, {'status': 'DISPATCHING'})]
        continuation = self.delivery_continuation(active, cohort, name, deployment)
        if continuation:
            old_path, session_path = continuation
            row.update(calls_reserved=0, calls_used=0, continuation_of=str(old_path),
                amount_committed='0' if allowance is not None else None,
                source_receipt_sha256=hashlib.sha256(old_path.read_bytes()).hexdigest())
            saved = read_json(session_path/'native-agent.json', 1024*1024)
            ceiling = saved['budget']['max_model_calls']
            remaining = None if ceiling is None else max(0, ceiling-count_calls(session_path))
            shared_remaining = (None if maximum is None else
                maximum-count_calls(self.root)-resources.known_calls(self.calls_committed())-RESERVE)
            if shared_remaining is not None and (remaining is None or remaining > shared_remaining):
                raise ValueError('The shared call budget cannot fund the saved planner allowance.')
        job.mkdir(parents=True)
        save_json(receipt_path, row)  # durable intent precedes any provider dispatch
        if continuation:
            with local_slot(deployment):
                run_session(None, None, session_path, resume=True,
                    follow_up='Continue the saved planning mission, retaining its observed sources and original criteria.',
                    message_id='delivery-repair-'+request.effect_id, cancellation=control.cancellation, refresh=True)
            result = record(old_path)['result']
            row.update(status='FINISHED', result=result)
            save_json(receipt_path, row)
            return result
        from . import execution_resources as resources
        from .managed_skills import frozen_for_role as frozen_skills_for_role
        planner_skills = frozen_skills_for_role(self.state, 'planner')

        def build_attempt(target_deployment):
            profile = target_deployment.get('native_profile') or {}
            agy_cli = profile.get('protocol') == 'vendor-cli' and profile.get('endpoint') == 'cli://agy'
            # Non-AGY planners keep exact history (max_compactions=0); cli://agy has a 160 KiB wire cap below its model
            # window, so enable compaction at model_context_policy's trigger_limit to avoid CLI_INPUT_BYTE_LIMIT_EXCEEDED.
            planner_context = resources.context({'max_compactions': 1}) if agy_cli else {'max_compactions': 0}
            cfg = {k: deepcopy(v) for k, v in self.state['config'].items()
                   if k in {'repositories', 'folder_access', 'project_admission'}}
            cfg.update(schema='tare.tools/native-agent-config/1', role='planner', writable={}, verifiers={},
                       capabilities=['read', 'list', 'search'], typed_output=True, specgraph_edit_context=False,
                       context_policy=planner_context)
            if previous_input:cfg['input_artifacts']=[previous_input]
            task_contract = ('Produce the smallest useful executable tasks, with literal target_paths, criteria, '
                             'Include every file that must be edited, regenerated or staged in target_paths. '
                             'A generated manifest is still a write: do not promise its regeneration outside the executable scope. '
                             'test_paths for existing relevant tests, and named verifiers when provided. '
                             'Put tasks in dependency order; they will be executed together as one bounded change. '
                             'Read relevant project files and cite their observed hashes. '
                             'When an active skill already documents its CLI or workflow, rely on the skill instructions '
                             'instead of exhaustively reading the tool\'s internal source code. '
                             'Task criteria describe the eventual product behavior. Keep phase limits such as '
                             'plan-only or no implementation in the plan narrative, not task criteria; the host '
                             'enforces when implementation is authorized. '
                             if self.state.get('conversation_flow') else 'leave tasks empty. ')
            if self.state.get('conversation_flow'):
                from .challenger import local
                if local(target_deployment):
                    from .native_artifacts import EVERY_TASK_EXAMPLES
                    task_contract += EVERY_TASK_EXAMPLES + ' '
            m = {'mission': args['instruction'], 'provided_findings': args.get('context', '')}
            if self.state.get('conversation_flow') and active:
                from .conversation_tasks import BACKLOG_CHECK, detected_checks, retained_candidate
                m.update(requested_outcome=active['intent'], objective=active['objective'], criteria=active['criteria'])
                retained = retained_candidate(active)
                if retained:
                    m['retained_candidate_outputs'] = {
                        'paths':list(retained.get('files') or []),
                        'patch_sha256':retained['candidate'].get('sha256'),
                        'instruction':'These are unverified retained outputs, not additional write authority. '
                            'When carrying this candidate forward, include all preserved outputs in target_paths. '
                            'If the recorded goal excludes an output, keep it unapplied and report the conflict; '
                            'do not silently discard it or widen the requested outcome.'}
                from .conversation_review import execution_selection
                m['host_planner_selection'] = execution_selection(None, None, planner=target_deployment)['planner']
                if active['intent'] == 'deliver':
                    m['required_output'] = 'executable_tasks'
                    m['phase_authority'] = ('The recorded user delivery authorizes planning now. Coordinator notes '
                        'are advisory and cannot postpone the executable plan to a separate request. Read-only limits '
                        'edits and execution, not planning. Reuse valid findings and produce tasks in this invocation. '
                        'Host selection records the configured model, reasoning and PRO setting; do not turn provider '
                        'attestation or completion of an external benchmark into product acceptance criteria.')
                if requalification:
                    m['changed_source_facts'] = requalification['changed_sources']
                    m['requalification_scope'] = ('One source-bound repair of the same complete user delivery; '
                        'reuse valid prior work, inspect changed facts and keep all still-unapplied deliverables in scope. '
                        'A private candidate or a plan is not an applied project change.')
                if previous_input:
                    m['previous_plan'] = PREVIOUS_PLAN
                m['available_verifiers'] = {**{n: _check_label(check, 'Declared delegated project check; host execution after authorization.')
                    for n, check in detected_checks(self.state['config']).items()},
                    **{n: _check_label(check, 'Existing configured project check.')
                    for n, check in self.state['config']['verifiers'].items()},
                    BACKLOG_CHECK: 'Host-owned validation of planned JSON graph files, without importing project code.'}
                from .turn_engine import project_checks
                from .work_units import headless_refusing_godot_scripts
                manual_checks = {}
                for repo_root in self.state['config'].get('repositories', {}).values():
                    manual_checks.update(project_checks(repo_root)['manual'])
                if manual_checks:
                    m['manual_checks'] = manual_checks
                manual_paths = {p for r_row in manual_checks.values() for p in r_row.get('paths') or ()}
                gui_scripts = []
                for repo_root in self.state['config'].get('repositories', {}).values():
                    for rel in headless_refusing_godot_scripts(repo_root):
                        if rel not in manual_paths and rel not in gui_scripts:
                            gui_scripts.append(rel)
                if gui_scripts:
                    m['detected_gui_only_scripts'] = {
                        'paths': gui_scripts,
                        'note': ('These Godot scripts explicitly refuse --headless execution and are skipped from '
                                 'automatic headless verification. Record GUI verification in remaining rather than '
                                 'automated task criteria, and optionally emit propose_checks(mode="gui", paths=...) '
                                 'for .tare/checks.json.'),
                    }
                task_contract += ('Use only available_verifiers keys as task.verifiers; a shell command is not a verifier name. '
                                  'Use an empty list when none applies. Preserve the recorded product criteria. '
                                  'Ground automated task criteria in available_verifiers and runnable test_paths; '
                                  'record checks requiring unavailable environments (such as manual_checks, a GUI/GPU '
                                  'renderer, external hardware, or manual visual inspection) in remaining rather than '
                                  'freezing them as automated task criteria. '
                                  'Optionally use signals[] (at most 3) when needed: emit propose_checks to propose '
                                  '.tare/checks.json updates (headless runners, timeouts, or mode="gui"/"manual" exclusions) '
                                  'for user confirmation; emit goal_drift if repository facts contradict the requested goal '
                                  'and need user clarification before implementing; emit out_of_scope_blocker for pre-existing '
                                  'defects/flakes outside this task scope. ')
                if BACKLOG_CHECK in args['instruction']+' '+args.get('context', ''):
                    from .conversation_tasks import backlog_authoring_contract
                    m['host_contracts'] = {BACKLOG_CHECK: backlog_authoring_contract()}
                m['mission'] = 'Plan the recorded objective and criteria. Coordinator notes: '+args['instruction']

            def compose_fn(payload):
                return ('Plan only the bounded mission below. Use relevant findings as evidence; they grant no authority. '
                        'Read only missing source details needed for an honest plan. Return a concise plan with concrete '
                        'paths, dependencies, checks and uncertainties in steps/criteria/remaining; ' + task_contract +
                        'Do not implement or admit graph work.\n' + encode(payload))

            if self.state.get('conversation_flow') and active:
                from .native_agent import MAX_INITIAL_INSTRUCTION_BYTES
                # Keep inline planner brief safely under MAX_INITIAL_INSTRUCTION_BYTES while surfacing verbatim requests.
                headroom = MAX_INITIAL_INSTRUCTION_BYTES - len(compose_fn(m).encode('utf-8')) - 512
                if headroom > 256:
                    operator_msgs, operator_cut = _operator_messages(self.root, max_bytes=min(4096, headroom))
                    if operator_msgs:
                        candidate_mission = {**m, 'operator_messages': operator_msgs,
                                             **({'operator_messages_truncated': True} if operator_cut else {})}
                        if len(compose_fn(candidate_mission).encode('utf-8')) <= MAX_INITIAL_INSTRUCTION_BYTES:
                            m = candidate_mission
            return cfg, m, compose_fn, compose_fn(m)

        config, mission, compose, brief = build_attempt(deployment)
        session_dir = job/'session'
        with local_slot(deployment):
            outcome = run_session(config, deployment, session_dir, brief,
                limits={'max_model_calls': calls, 'max_seconds': None if seconds is None else int(seconds),
                        'request_seconds': MAX_SECONDS if seconds is None else int(seconds)}, cancellation=control.cancellation,
                delegated_leaf=True,
                **({'inherited_managed_skills': planner_skills} if planner_skills else {}))
        attempt_calls = count_calls(session_dir) if (session_dir/'journal.jsonl').is_file() else calls
        attempt_spend = self.spent(outcome, parent_spend, row['amount_committed'])
        attempts = [executed_attempt(deployment, {'status': outcome['status'], 'reason': outcome.get('reason'),
                                                  'failure_code': outcome.get('failure_code')})]
        planner_calls = attempt_calls
        row['calls_used'] = planner_calls
        row['amount_committed'] = attempt_spend

        # Bounded 1-shot automatic escalation when an unpinned planner hits a compaction loop on a smaller window.
        from .execution_scope import current as current_scope
        from .work_units import _effective_trigger_limit
        if (not args.get('route') and not current_scope().pin('planner') and not control.should_stop()
                and _planner_compaction_loop(session_dir, outcome)):
            orig_trigger = _effective_trigger_limit(deployment, ephemeral=True)
            if orig_trigger is not None:
                esc_trace = {}
                self._trace = esc_trace
                try:
                    esc_name, esc_deployment = self.choose(
                        args, (active or {}).get('kinds'), min_trigger_limit=orig_trigger, exclude=(name,))
                except ValueError:
                    esc_name = esc_deployment = None
                finally:
                    self._trace = None
                if esc_deployment is not None:
                    # Save attempt 1's actual call consumption before computing remaining shared budget.
                    row['calls_reserved'] = attempt_calls
                    save_json(receipt_path, row)
                    esc_calls = resources.child_calls(MAX_CALLS)
                    if maximum is not None:
                        esc_calls = resources.ceiling(
                            'calls', esc_calls,
                            maximum - count_calls(self.root) - resources.known_calls(self.calls_committed()) - RESERVE)
                    esc_seconds = resources.child_seconds(MAX_SECONDS, control.remaining_seconds(), reserve=5)
                    if self.state.get('deadline_utc') is not None:
                        esc_seconds = resources.ceiling('time', esc_seconds, self.state['deadline_utc'] - time.time() - 5)
                    can_fund = not ((esc_calls is not None and esc_calls < 2)
                                    or (esc_seconds is not None and esc_seconds < 10)
                                    or control.should_stop())
                    esc_allowance = esc_parent_spend = None
                    if can_fund:
                        try:
                            esc_allowance, esc_parent_spend = self.allowance(esc_deployment)
                        except ValueError:
                            can_fund = False
                    if can_fund:
                        from .native_spend import total_amounts
                        def _combine_amounts(first, second):
                            combined = total_amounts([first, second])
                            return None if combined is None else str(combined)
                        esc_session = job/'session-2'
                        row.pop('calls_used', None)
                        row['calls_reserved'] = (attempt_calls + esc_calls) if esc_calls is not None else None
                        row['amount_committed'] = _combine_amounts(attempt_spend, esc_allowance)
                        row['executed_decision'] = executed_decision(
                            esc_trace.get('mode'), esc_name, esc_trace.get('visited', [esc_name]),
                            esc_trace.get('skipped', []))
                        row['attempts'] = [*attempts, executed_attempt(esc_deployment, {'status': 'DISPATCHING'})]
                        row['escalated_from'] = name
                        row['route'] = esc_name
                        save_json(receipt_path, row)
                        esc_config, esc_mission, esc_compose, esc_brief = build_attempt(esc_deployment)
                        with local_slot(esc_deployment):
                            esc_outcome = run_session(esc_config, esc_deployment, esc_session, esc_brief,
                                limits={'max_model_calls': esc_calls,
                                        'max_seconds': None if esc_seconds is None else int(esc_seconds),
                                        'request_seconds': MAX_SECONDS if esc_seconds is None else int(esc_seconds)},
                                cancellation=control.cancellation, delegated_leaf=True,
                                **({'inherited_managed_skills': planner_skills} if planner_skills else {}))
                        esc_used = count_calls(esc_session) if (esc_session/'journal.jsonl').is_file() else esc_calls
                        esc_spent = self.spent(esc_outcome, esc_parent_spend, esc_allowance)
                        planner_calls = attempt_calls + esc_used
                        row['calls_used'] = planner_calls
                        row['amount_committed'] = _combine_amounts(attempt_spend, esc_spent)
                        attempts.append(executed_attempt(esc_deployment, {
                            'status': esc_outcome['status'], 'reason': esc_outcome.get('reason'),
                            'failure_code': esc_outcome.get('failure_code')}))
                        name, deployment = esc_name, esc_deployment
                        config, mission, compose = esc_config, esc_mission, esc_compose
                        session_dir, outcome = esc_session, esc_outcome

        if outcome['status'] == 'PROPOSED':
            artifact = regular(session_dir/'artifact.json')
            plan = read_json(artifact, 65536)['payload']
            result = handoff(artifact,plan)
            # Every conversation plan with tasks is reviewed unless it is non-critical, non-code creative/asset work
            # (or an active skill opts out) planned by a non-local model: a weak local planner or any critical/code
            # plan never excuses its review (owner, 2026-11-17 / 2026-11-23).
            if self.state.get('conversation_flow') and plan.get('tasks'):
                review_skills = [*planner_skills, *frozen_skills_for_role(self.state, 'implementer')]
                skip_reason = plan_review_skip_reason(active, plan, deployment, review_skills)
                if skip_reason is not None:
                    result['review'] = {'status': 'SKIPPED', 'route': None, 'model_calls': 0, 'reason': skip_reason}
                else:
                    # The planner's own result is saved before any review is dispatched: a restart then keeps a valid
                    # plan (`settled` reconciles what finished) and never dispatches the review or revision again.
                    row['result'] = {**result, 'route': name, 'model_calls': planner_calls}
                    row['critic'] = {'stage': 'review'}
                    save_json(receipt_path, row)
                    review = self.critic(row, receipt_path, job, active, deployment, plan, control)
                    result['review'] = review
                    if review.get('findings'):
                        result['next'] += CRITIC_NEXT
                        if review['verdict'] == 'REVISE':  # once, and the revision is not reviewed again
                            row['result'] = {**row['result'], 'review': deepcopy(review), 'next': result['next']}
                            row['critic']['stage'] = 'revision'
                            save_json(receipt_path, row)
                            revised = self.revise(row, receipt_path, job, deployment, config, compose, mission, artifact, review, control)
                            if revised:
                                artifact, plan = revised
                                result = {**handoff(artifact, plan), 'review': review}
                                result['next'] += CRITIC_REVISED_NEXT
        else:
            result = {'status': outcome['status'], 'reason': outcome.get('reason'),
                      'failure_code': outcome.get('failure_code'),
                      'provider_stop_reason': outcome.get('provider_stop_reason'), 'session': str(session_dir)}
            if outcome.get('delivery_validation'):
                result['delivery_validation'] = deepcopy(outcome['delivery_validation'])
                result['next'] = ('The provider completed a response but the host rejected its typed delivery. '
                    'Inspect the listed local validation issues; this is not evidence of quota or provider failure.')
        row['attempts'] = attempts
        row.update(status='FINISHED', result={**result, 'route': name, 'model_calls': planner_calls})
        save_json(receipt_path, row)
        return row['result']

    def allowance(self, deployment):
        """(the child's spend allowance, the parent's spend policy); a shared limit caps `deployment` to what remains."""
        from . import execution_resources as resources
        from .native_spend import settings
        allowance = '0' if resources.spend_capped() else None
        parent_spend, _ = settings(self.state['deployment'])
        if parent_spend:
            _, tariff = settings(deployment)
            if tariff is None or tariff.currency != parent_spend.currency or self.parent_model is None:
                raise ValueError('A shared spend limit requires a matching planner tariff.')
            allowance = self.parent_model.report()['remaining_amount']
            if Decimal(allowance) <= 0:
                raise ValueError('No shared spend allowance remains.')
            deployment['native_spend_policy'] = {'max_amount': allowance, 'currency': parent_spend.currency,
                                                'allow_estimated_counts': parent_spend.allow_estimated_counts}
        return allowance, parent_spend

    @staticmethod
    def spent(outcome, parent_spend, default):
        """What a child's outcome commits against the shared money."""
        from . import execution_resources as resources
        if resources.selected('money'):
            from .native_spend import outcome_commitment
            return outcome_commitment(outcome)
        if parent_spend:
            spend = outcome.get('spend') or {}
            if spend.get('currency') == parent_spend.currency and spend.get('committed_amount') is not None:
                # The child's spend owner retains reservations for unpriced or
                # interrupted calls. Do not turn missing usage into free work.
                return spend['committed_amount']
        return default

    def critic(self, row, receipt_path, job, active, planner, plan, control):
        """One independent read-only review of a conversation plan that has tasks (owner, 2026-11-17). Advice
        only: it never blocks, loops or raises; whatever happens the plan returns as it is, with the reason. Its calls
        and money join the planner's receipt, so every shared budget counts them like the planner's own."""
        from .execution_scope import require
        from .native_agent import run_session
        from .native_plan import select_route
        from .work_audit import independence
        from .work_units import local_slot
        out = {'status': 'SKIPPED', 'route': None, 'model_calls': 0}
        try:
            reviewer, info = select_route('auto', 'plan-auditor', 'AUTOMATIC_INDEPENDENT_REVIEWER', planner,
                                          work_type=(active or {}).get('kinds'))
            kind = reviewer and independence(planner, reviewer)
            if reviewer is None or reviewer['id'] == planner['id'] or kind in (None, 'AUDITOR_NOT_INDEPENDENT'):
                from .environment import granted
                # The planner never reviews its own plan, unless the user ticked its local model as plan reviewer in
                # /routes: one model, the critic's brief, when no other reviewer can (owner, 2026-11-17).
                if not granted('plan-auditor', planner):
                    off = reviewer is None and any(c.get('reason') == 'SWITCHED_OFF_FOR_ROLE'
                                                   for c in (info.get('selection') or {}).get('candidates', []))
                    return {**out, 'reason': 'SWITCHED_OFF_FOR_ROLE' if off else 'NO_INDEPENDENT_REVIEWER'}
                reviewer, kind, out['own_model'] = planner, 'AUDITOR_NOT_INDEPENDENT', True
            out['route'] = reviewer['id']
            require(reviewer, 'plan-auditor')
        except (ValueError, OSError, KeyError, TypeError) as exc:
            return {**out, 'reason': 'REVIEWER_UNAVAILABLE', 'detail': str(exc)[:300]}
        from .turn_engine import streaming
        reviewer = streaming(deepcopy(reviewer))
        try:
            calls, seconds, allowance, parent_spend, before, money = self.fund(
                row, receipt_path, reviewer, CRITIC_CALLS, CRITIC_SECONDS, control)
        except (ValueError, OSError, KeyError, TypeError, ArithmeticError) as exc:
            return {**out, 'reason': 'SHARED_BUDGET', 'detail': str(exc)[:300]}
        review, session = job/'review', job/'review/session'
        proposal = review/'proposal'
        outcome = {}
        try:
            row['critic'] = {'stage': 'review', 'route': out['route'], 'independent': kind == 'INDEPENDENT',
                             **({'own_model': True} if out.get('own_model') else {})}
            save_json(receipt_path, row)  # an interruption from here on is reconciled by `settled`, not repeated
            proposal.mkdir(parents=True)
            (proposal/'plan.json').write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding='utf-8')
            request_doc = {k: (active or {}).get(k) for k in ('objective', 'criteria', 'kinds')}
            operator_msgs, operator_cut = _operator_messages(self.root)
            if operator_msgs:
                request_doc['operator_messages'] = operator_msgs
                if operator_cut:
                    request_doc['operator_messages_truncated'] = True
            (proposal/'request.json').write_text(json.dumps(request_doc, indent=2, ensure_ascii=False), encoding='utf-8')
            # The planner's admission policy and folders, and the project as the one instruction source: `proposal` is
            # evidence only (an auditor's evidence root), so no instruction the parent would not load becomes active.
            parent = self.state['config']
            config = {k: deepcopy(parent[k]) for k in ('folder_access', 'project_admission') if k in parent}
            config.update(schema='tare.tools/native-agent-config/1', role='plan-auditor', typed_output=True,
                          repositories={**deepcopy(parent['repositories']), 'proposal': str(proposal)},
                          instruction_repositories=list(parent['repositories']),
                          writable={}, verifiers={}, capabilities=['read', 'list', 'search'],
                          specgraph_edit_context=False)
            from .managed_skills import frozen_for_role as frozen_skills_for_role
            critic_skills = frozen_skills_for_role(self.state, 'plan-auditor')
            with local_slot(reviewer):
                outcome = run_session(config, reviewer, session, CRITIC_BRIEF,
                    limits={'max_model_calls': calls, 'max_seconds': None if seconds is None else int(seconds),
                            'request_seconds': CRITIC_SECONDS if seconds is None else int(seconds)},
                    cancellation=control.cancellation, delegated_leaf=True,
                    **({'inherited_managed_skills': critic_skills} if critic_skills else {}))
            out.update(model_calls=self.settle(row, session, calls, before, money, outcome, parent_spend, allowance))
            if outcome['status'] != 'PROPOSED':
                return {**out, 'status': 'FAILED', 'reason': str(outcome.get('failure_code') or outcome['status'])}
            from .native_agent import read_json
            return review_of(out, read_json(regular(session/'artifact.json'), 65536)['payload'], kind == 'INDEPENDENT')
        except (ValueError, OSError, KeyError, TypeError, ArithmeticError) as exc:
            return {**out, 'status': 'FAILED', 'reason': 'REVIEW_ERROR', 'detail': str(exc)[:300]}

    def fund(self, row, receipt_path, deployment, default_calls, default_seconds, control):
        """Reserve one more child on the planner's receipt, as the planner was: the shared call, time and money
        ceilings, then its own. Returns (calls, seconds, allowance, parent_spend, calls before, money before); raises
        ValueError when the shared budget cannot fund it."""
        from . import execution_resources as resources
        from .delegation_tools import count_calls
        before, money = row['calls_used'], row['amount_committed']
        save_json(receipt_path, row)  # the shared figures below must already count what the receipt has used
        allowance, parent_spend = self.allowance(deployment)
        calls = resources.child_calls(default_calls)
        maximum = self.state['limits']['max_model_calls']
        if maximum is not None:
            calls = resources.ceiling('calls', calls, maximum-count_calls(self.root)
                                      -resources.known_calls(self.calls_committed())-RESERVE)
        seconds = resources.child_seconds(default_seconds, control.remaining_seconds(), reserve=5)
        if self.state.get('deadline_utc') is not None:
            seconds = resources.ceiling('time', seconds, self.state['deadline_utc']-time.time()-5)
        # --unlimited lifts the child's own ceilings; these two children keep theirs whatever the parent allows.
        calls = default_calls if calls is None else min(calls, default_calls)
        seconds = default_seconds if seconds is None else min(seconds, default_seconds)
        if calls < 2 or seconds < 10 or control.should_stop():
            raise ValueError('Not enough shared budget for another child.')
        # Charge the whole allowance before dispatch: a crash then leaves the receipt over-, never under-counting.
        if calls is not None:
            row['calls_used'] = before+calls
        row['amount_committed'] = money if allowance is None else None if money is None else str(Decimal(money)+Decimal(allowance))
        save_json(receipt_path, row)
        return calls, seconds, allowance, parent_spend, before, money

    def settle(self, row, session, calls, before, money, outcome, parent_spend, allowance):
        """Replace the reservation of `fund` by what the child used; returns its model calls."""
        from .delegation_tools import count_calls
        from .native_spend import total_amounts
        used = count_calls(session) if (session/'journal.jsonl').is_file() else (calls or 0)
        row['calls_used'] = before+used
        cost = total_amounts([money, self.spent(outcome, parent_spend, allowance)])
        row['amount_committed'] = None if cost is None else str(cost)
        return used

    def revise(self, row, receipt_path, job, deployment, config, compose, mission, artifact, review, control):
        """The same planner route revises the plan once with the critic's findings (owner, 2026-11-17: they removed
        the defect only when the planner used them). Returns (artifact, plan), or None when it was not funded, failed
        or delivered no tasks: the first plan then stands with its findings. Never raises; never reviewed again.
        `review` records the outcome."""
        from .native_agent import read_json, run_session
        from .native_artifacts import read_input
        from .work_units import local_slot
        deployment, session = deepcopy(deployment), job/'revision'
        review['revised'] = False
        try:
            brief = compose({**mission, 'previous_plan': PREVIOUS_PLAN, 'critic_note': CRITIC_REVISION,
                             'critic_findings': [f['claim'] for f in review['findings']]})
            config = {**config, 'input_artifacts': [read_input(artifact)]}
            calls, seconds, allowance, parent_spend, before, money = self.fund(
                row, receipt_path, deployment, MAX_CALLS, MAX_SECONDS, control)
        except (ValueError, OSError, KeyError, TypeError, ArithmeticError):
            review['revision_reason'] = 'NOT_FUNDED'
            return None
        try:
            from .managed_skills import frozen_for_role as frozen_skills_for_role
            planner_skills = frozen_skills_for_role(self.state, 'planner')
            with local_slot(deployment):
                outcome = run_session(config, deployment, session, brief,
                    limits={'max_model_calls': calls, 'max_seconds': None if seconds is None else int(seconds),
                            'request_seconds': MAX_SECONDS if seconds is None else int(seconds)},
                    cancellation=control.cancellation, delegated_leaf=True,
                    **({'inherited_managed_skills': planner_skills} if planner_skills else {}))
            review['revision_model_calls'] = self.settle(row, session, calls, before, money, outcome, parent_spend, allowance)
            if outcome['status'] != 'PROPOSED':
                review['revision_reason'] = str(outcome.get('failure_code') or outcome['status'])
                return None
            revised = regular(session/'artifact.json')
            plan = read_json(revised, 65536)['payload']
            if not plan.get('tasks'):
                review['revision_reason'] = 'NO_TASKS'
                return None
        except (ValueError, OSError, KeyError, TypeError, ArithmeticError):
            review['revision_reason'] = 'REVISION_ERROR'
            return None
        review.update(revised=True, revision_route=row['route'])
        return revised, plan

    def delivery_continuation(self, active, cohort, name, deployment):
        """One explicit same-route repair uses the saved read-only planner and its ceilings."""
        from .native_agent import read_json
        from .native_model_recovery import completed_delivery_refusal
        from compute_plane.native_journal import NativeJournal
        if not active or active.get('plan') or not fresh_continuation(self.root, active):
            return None
        attempted = [r for r in cohort if r.get('continuation_of')]
        for path in sorted((self.root/'planning').glob('*/receipt.json'), reverse=True):
            row = read_json(path, 65536)
            if (row.get('demand_id') != active['id'] or row.get('planning_revision', 0) != active.get('planning_revision', 0)
                    or row.get('status') != 'FINISHED' or row.get('route') != name
                    or (row.get('result') or {}).get('failure_code') != 'INVALID_DELIVERY'):
                continue
            session = path.parent/'session'
            state = read_json(session/'native-agent.json', 1024*1024)
            config = state['config']
            if (state['deployment'] != deployment or config['role'] != 'planner' or config.get('writable')
                    or config.get('terminal') or config.get('verifiers')
                    or config['repositories'] != self.state['config']['repositories']
                    or set(config.get('capabilities', [])) - {'read', 'list', 'search', 'context'}):
                return None
            events = [r.event() for r in NativeJournal(session/'journal.jsonl').snapshot().records]
            observations = [r.event() for r in NativeJournal(session/'observations.jsonl').snapshot().records]
            if attempted:
                # A local source-check refusal before adding any journal event
                # is not a dispatched correction. Retain its receipt, permit
                # an explicit refreshed continuation, never repeat model work.
                last = (state.get('invocations') or [{}])[-1]
                if (any(r.get('status') == 'FINISHED' for r in attempted)
                        or not last.get('ended_at') or last.get('journal_start') != len(events)):
                    return None
            if completed_delivery_refusal(events, observations, state['session_id']):
                return path, session
        return None

    def execute(self, request, control):
        from tare_tools_agent_runtime import EffectResult
        if request.tool_call.name != 'delegate_plan':
            return self.tools.execute(request, control)
        if request.session_id != self.session_id or control.binding != self.binding or control.should_stop():
            return EffectResult(request.effect_id, 'refused', encode({'reason': 'CONTROL_OR_SESSION_MISMATCH'}))
        try:
            args = object_json(request.tool_call.arguments_json)
            if (set(args)-{'instruction', 'context', 'route', 'effort'}
                    or not isinstance(args.get('instruction'), str) or not args['instruction'].strip()
                    or any(not isinstance(args.get(k, ''), str) for k in ('context', 'route'))
                    or args.get('effort', 'default') not in {'default', 'low', 'medium', 'high', 'xhigh', 'max'}
                    or len(encode(args).encode()) > 8192):
                raise ValueError('Give a planning mission and bounded findings, within 8 KiB.')
            return EffectResult(request.effect_id, 'succeeded', encode(self.run(request, args, control)), True)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            return EffectResult(request.effect_id, 'refused', encode({'reason': str(exc)[:500]}))

    def execute_many(self, requests, control):
        if not any(r.tool_call.name == 'delegate_plan' for r in requests):
            inner = getattr(self.tools, 'execute_many', None)
            if callable(inner):
                return inner(requests, control)
        return [self.execute(r, control) for r in requests]

    def recover_effect(self, journal):
        from compute_plane.native_effect_recovery import pending
        from tare_tools_agent_runtime import EffectResult
        request = pending(journal, self.session_id)
        if request is None or request.tool_call.name != 'delegate_plan':
            return self.tools.recover_effect(journal)
        job = regular(self.root/'planning'/hashlib.sha256(request.effect_id.encode()).hexdigest()[:24])
        from .native_agent import read_json
        row = settled(read_json(job/'receipt.json', 65536), job) if (job/'receipt.json').is_file() else {}
        if row and row.get('request_sha256') != sha(object_json(request.tool_call.arguments_json)):
            return request, EffectResult(request.effect_id, 'refused', encode({'reason': 'Planning effect arguments changed.'}))
        result = row.get('result') or {'status': 'INTERRUPTED', 'reason': 'Planning outcome is unknown; no automatic repeat.'}
        return request, EffectResult(request.effect_id, 'succeeded' if row.get('result') else 'refused',
                                     encode(result), bool(row.get('result')))
