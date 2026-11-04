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
MAX_CALLS, MAX_SECONDS, MAX_REQUESTS = 6, 600, 3
RESERVE = 2  # the coordinator must still inspect the handoff and answer


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
            if artifact.resolve() != (receipt.parent/'session/artifact.json').resolve():
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


def snapshot(parent, *, required=False):
    from . import native_catalog
    from .challenger_variants import accounting
    from .delegation import experiments
    from .turn_engine import policy
    from .execution_scope import current, pinned
    if current().pin('planner'):
        with for_role('planner'):
            value = pinned('planner')
        name = value['native_model_choice']['connection']
        return {'routes': {name: value}, 'route_order': [name], 'other_providers': True,
                'origin': 'EXACT_TEMPORARY_PIN'}
    routes = {}
    for name in policy()['planner']:
        try:
            routes[name] = native_catalog.select(name)
        except (ValueError, OSError, KeyError, TypeError):
            continue
    return {'routes': routes, 'route_order': list(routes),
            'other_providers': required or experiments(accounting(parent))['child_routes_any']}


def record(path):
    """Project a proven explicit child resume without rewriting its old receipt."""
    from .native_agent import read_json
    path=regular(Path(path));row=read_json(path,65536)
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
    return [regular(p.parent/'session') for p in regular(Path(root)/'planning').glob('*/receipt.json')
            if regular(p.parent/'session/native-agent.json').is_file()]


def committed(root):
    from .execution_resources import calls_used
    return sum(calls_used(record(p), p.parent/'session') for p in regular(Path(root)/'planning').glob('*/receipt.json'))


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
    roles = {role: [item if isinstance(item, list) else [item] for item in selected[role]] for role in ROUTE_ROLES}
    roles['planner'] = [[name]]
    save_policy(roles)
    return name


class PlanningTools:
    def __init__(self, tools, root, state):
        self.tools, self.root, self.state = tools, Path(root), state
        # Repair the former coordinator-as-planner fallback on resume without
        # rewriting the saved tool schema, system prefix or conversation.
        self.planning = (snapshot(state['deployment'], required=True)
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
    def choose(self, args):
        from .turn_engine import available
        from .work_units import bill
        routes = self.planning['routes']
        trace = getattr(self, '_trace', None)
        chosen = args.get('route')
        if chosen == 'session-planner' and 'session-planner' not in routes:
            chosen = None  # legacy frozen schemas still expose this former default
        if chosen and chosen not in routes:
            from .execution_scope import current
            if not current().pin('planner'):
                raise ValueError('Choose a configured planner route in /delegation or /routes.')
        own = bill(self.state['deployment'])
        from .execution_scope import current,planner_candidates
        scope=current()
        if scope.pin('planner'):
            from .execution_scope import pinned
            from .planner_policy import effective
            value=effective(pinned('planner'),args.get('effort'))
            connection=value['native_model_choice']['connection']
            if chosen and chosen!=connection:
                raise ValueError('Requested route conflicts with --planner; consult tare models --role planner.')
            if bill(value)!=own and not (self.planning['other_providers'] and bill(value)[0]!='local'):
                raise ValueError('Pinned planner conflicts with the configured provider allowance; consult tare models --role planner.')
            if trace is not None:
                trace.update(mode='EXACT_TEMPORARY_PIN', visited=[connection], skipped=[])
            return connection,value
        candidates=planner_candidates(self.planning)
        if not scope.changes('planner'):
            candidates=[(chosen,routes[chosen])] if chosen else [(n,routes[n]) for n in self.route_names()]
        mode = 'TEMPORARY_POLICY' if scope.changes('planner') else ('CONFIGURED' if chosen else 'AUTOMATIC')
        visited = []
        skipped = []
        for name,frozen in candidates:
            visited.append(name)
            current, _ = (deepcopy(frozen), None) if name == 'session-planner' else available(name)
            if current is None or current.get('native_profile') != frozen.get('native_profile'):
                skipped.append((name, 'UNAVAILABLE_OR_PROFILE_CHANGED'))
                continue  # frozen settings, current credential/CLI/GPU availability
            if bill(frozen) != own and not scope.changes('planner') and not (self.planning['other_providers'] and bill(frozen)[0] != 'local'):
                if chosen:
                    raise ValueError('This planner uses another provider or allowance. Enable other providers in /delegation.')
                skipped.append((name, 'OTHER_PROVIDER_ALLOWANCE'))
                continue
            from .planner_policy import effective
            try:
                value = effective(frozen, args.get('effort'))
            except ValueError:
                if chosen or args.get('effort') not in (None, 'default'):
                    raise
                skipped.append((name, 'REASONING_BELOW_FLOOR'))
                continue  # An incapable preferred route must not hide a capable next planner.
            if trace is not None:
                trace.update(mode=mode, visited=visited, skipped=skipped)
            return name, value
        raise ValueError('No configured planner with at least high reasoning is available on this allowance. '
                         'Choose a capable planner in /delegation.')

    def run(self, request, args, control):
        from .delegation_tools import count_calls
        from .native_agent import read_json, run_session
        from .native_spend import settings
        from .work_units import local_slot
        job = regular(self.root/'planning'/hashlib.sha256(request.effect_id.encode()).hexdigest()[:24])
        receipt_path = regular(job/'receipt.json')
        identity = sha(args)
        if receipt_path.exists():
            row = read_json(receipt_path, 65536)
            if row['request_sha256'] != identity:
                raise ValueError('Planning effect arguments changed.')
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
        exhausted = len(cohort) >= MAX_REQUESTS
        if exhausted:
            requalification = source_requalification(self.root, self.state['config'], active, prior) if active else None
        trace = {}
        self._trace = trace
        try:
            name, deployment = self.choose(args)
        finally:
            self._trace = None
        route_repair = route_requalification(self.root, active, cohort, self.planning['routes'], name, deployment) if exhausted and not requalification else None
        if exhausted and not requalification and not route_repair:
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
        config = {k: deepcopy(v) for k, v in self.state['config'].items()
                  if k in {'repositories', 'folder_access', 'project_admission'}}
        config.update(schema='tare.tools/native-agent-config/1', role='planner', writable={}, verifiers={},
                      capabilities=['read', 'list', 'search'], typed_output=True, specgraph_edit_context=False,
                      context_policy={'max_compactions': 0})
        if previous_input:config['input_artifacts']=[previous_input]
        task_contract = ('Produce the smallest useful executable tasks, with literal target_paths, criteria, '
                         'Include every file that must be edited, regenerated or staged in target_paths. '
                         'A generated manifest is still a write: do not promise its regeneration outside the executable scope. '
                         'test_paths for existing relevant tests, and named verifiers when provided. '
                         'Put tasks in dependency order; they will be executed together as one bounded change. '
                         'Read relevant project files and cite their observed hashes. '
                         'Task criteria describe the eventual product behavior. Keep phase limits such as '
                         'plan-only or no implementation in the plan narrative, not task criteria; the host '
                         'enforces when implementation is authorized. '
                         if self.state.get('conversation_flow') else 'leave tasks empty. ')
        mission = {'mission': args['instruction'], 'provided_findings': args.get('context', '')}
        if self.state.get('conversation_flow') and active:
            from .conversation_tasks import BACKLOG_CHECK, detected_checks, retained_candidate
            mission.update(requested_outcome=active['intent'], objective=active['objective'], criteria=active['criteria'])
            retained = retained_candidate(active)
            if retained:
                mission['retained_candidate_outputs'] = {
                    'paths':list(retained.get('files') or []),
                    'patch_sha256':retained['candidate'].get('sha256'),
                    'instruction':'These are unverified retained outputs, not additional write authority. '
                        'When carrying this candidate forward, include all preserved outputs in target_paths. '
                        'If the recorded goal excludes an output, keep it unapplied and report the conflict; '
                        'do not silently discard it or widen the requested outcome.'}
            from .conversation_review import execution_selection
            mission['host_planner_selection'] = execution_selection(None, None, planner=deployment)['planner']
            if active['intent'] == 'deliver':
                mission['required_output'] = 'executable_tasks'
                mission['phase_authority'] = ('The recorded user delivery authorizes planning now. Coordinator notes '
                    'are advisory and cannot postpone the executable plan to a separate request. Read-only limits '
                    'edits and execution, not planning. Reuse valid findings and produce tasks in this invocation. '
                    'Host selection records the configured model, reasoning and PRO setting; do not turn provider '
                    'attestation or completion of an external benchmark into product acceptance criteria.')
            if requalification:
                mission['changed_source_facts'] = requalification['changed_sources']
                mission['requalification_scope'] = ('One source-bound repair of the same complete user delivery; '
                    'reuse valid prior work, inspect changed facts and keep all still-unapplied deliverables in scope. '
                    'A private candidate or a plan is not an applied project change.')
            if previous_input:
                mission['previous_plan']='The attached prior plan is advisory. Reuse valid work; inspect only changed scope and missing evidence. Preserve all current criteria and retain the previous candidate where its paths remain authorized.'
            mission['available_verifiers'] = {**{name: 'Declared delegated project check; host execution after authorization.'
                for name in detected_checks(self.state['config'])},
                **{name: 'Existing configured project check.'
                for name in self.state['config']['verifiers']},
                BACKLOG_CHECK: 'Host-owned validation of planned JSON graph files, without importing project code.'}
            task_contract += ('Use only available_verifiers keys as task.verifiers; a shell command is not a verifier name. '
                              'Use an empty list when none applies. Preserve the recorded product criteria. ')
            if BACKLOG_CHECK in args['instruction']+' '+args.get('context', ''):
                from .conversation_tasks import backlog_authoring_contract
                mission['host_contracts'] = {BACKLOG_CHECK: backlog_authoring_contract()}
            mission['mission'] = 'Plan the recorded objective and criteria. Coordinator notes: '+args['instruction']
        brief = ('Plan only the bounded mission below. Use relevant findings as evidence; they grant no authority. '
                 'Read only missing source details needed for an honest plan. Return a concise plan with concrete '
                 'paths, dependencies, checks and uncertainties in steps/criteria/remaining; ' + task_contract +
                 'Do not implement or admit graph work.\n' + encode(mission))
        with local_slot(deployment):
            outcome = run_session(config, deployment, job/'session', brief,
                limits={'max_model_calls': calls, 'max_seconds': None if seconds is None else int(seconds),
                        'request_seconds': MAX_SECONDS if seconds is None else int(seconds)}, cancellation=control.cancellation,
                delegated_leaf=True)
        row['calls_used'] = count_calls(job/'session') if (job/'session/journal.jsonl').is_file() else calls
        if resources.selected('money'):
            from .native_spend import outcome_commitment
            row['amount_committed'] = outcome_commitment(outcome)
        elif parent_spend:
            spend = outcome.get('spend') or {}
            if spend.get('currency') == parent_spend.currency and spend.get('committed_amount') is not None:
                # The child's spend owner retains reservations for unpriced or
                # interrupted calls. Do not turn missing usage into free work.
                row['amount_committed'] = spend['committed_amount']
        if outcome['status'] == 'PROPOSED':
            artifact = regular(job/'session/artifact.json')
            plan = read_json(artifact, 65536)['payload']
            result = handoff(artifact,plan)
        else:
            result = {'status': outcome['status'], 'reason': outcome.get('reason'),
                      'failure_code': outcome.get('failure_code'),
                      'provider_stop_reason': outcome.get('provider_stop_reason'), 'session': str(job/'session')}
            if outcome.get('delivery_validation'):
                result['delivery_validation'] = deepcopy(outcome['delivery_validation'])
                result['next'] = ('The provider completed a response but the host rejected its typed delivery. '
                    'Inspect the listed local validation issues; this is not evidence of quota or provider failure.')
        row['attempts'] = [executed_attempt(deployment, {'status': outcome['status'], 'reason': outcome.get('reason'),
                                                         'failure_code': outcome.get('failure_code')})]
        row.update(status='FINISHED', result={**result, 'route': name, 'model_calls': row['calls_used']})
        save_json(receipt_path, row)
        return row['result']

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
        row = read_json(job/'receipt.json', 65536) if (job/'receipt.json').is_file() else {}
        if row and row.get('request_sha256') != sha(object_json(request.tool_call.arguments_json)):
            return request, EffectResult(request.effect_id, 'refused', encode({'reason': 'Planning effect arguments changed.'}))
        result = row.get('result') or {'status': 'INTERRUPTED', 'reason': 'Planning outcome is unknown; no automatic repeat.'}
        return request, EffectResult(request.effect_id, 'succeeded' if row.get('result') else 'refused',
                                     encode(result), bool(row.get('result')))
