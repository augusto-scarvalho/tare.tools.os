"""Read-only next-step selection for a demand's existing PLAN and linked Works."""
from pathlib import Path

from graph_backlog.algorithms import readiness
from graph_backlog.core import WorkGraph
from graph_backlog.jsonutil import compute_revision_hash
from graph_backlog.validation import validate_work_graph
from . import console_recovery, relay_mesh, work_run
from .native_agent import read_json


def linkage(value):
    """Return explicitly linked Works and PLANs, following confirmed replacements."""
    ids, applied, plans = [], set(), []
    replacements = {}
    for index, event in enumerate(value['events']):
        kind, choice = event['kind'], event.get('selection') or {}
        if choice and Path(choice['root']).resolve() != Path(value['root']).resolve():
            raise ValueError('A linked stage belongs to another project.')
        detail = event.get('detail') or {}
        result = detail.get('result') or detail
        if (kind in {'graph_applied', 'work_prepared', 'work_revised', 'work_order_applied', 'work_replacement_applied'}
                or kind == 'execution_requested' and choice.get('mode') in {'work', 'sequence'}
                or kind == 'mutation_reconciled' and result.get('work_ids')):
            from .console_backend import Selection, require_control_graph
            require_control_graph(Selection(value['root'], graph_path=result.get('graph_path') or choice.get('graph_path', '')))
        if kind == 'mutation_reconciled':
            requested = detail.get('request_event')
            if not isinstance(requested, int) or not 0 <= requested < index:
                raise ValueError('Recovered mutation has no preceding request binding.')
            request = value['events'][requested]
            if request['kind'] != detail.get('operation'):
                raise ValueError('Recovered mutation operation differs from its request.')
            choice = request['selection']
            if Path(choice['root']).resolve() != Path(value['root']).resolve():
                raise ValueError('Recovered mutation belongs to another project.')
        if kind == 'work_replacement_applied' or (kind == 'mutation_reconciled' and detail.get('operation') == 'work_replacement_requested'):
            replacements.update(result.get('replacements', {}))
        if kind == 'work_order_applied' or (kind == 'mutation_reconciled' and detail.get('operation') == 'work_order_requested'):
            ids.extend(result.get('work_ids', []))
        if kind == 'graph_applied' or (kind == 'mutation_reconciled' and detail.get('operation') == 'graph_apply_requested'):
            ids.extend(result.get('work_ids', [])); applied.add(choice.get('plan'))
        if kind == 'execution_requested' and choice.get('mode') == 'plan':
            plans.append(choice['plan'])
            applied.discard(choice['plan'])
        if kind == 'execution_requested' and choice.get('mode') == 'work':
            ids.append(choice['work'])
        if kind in {'work_prepared', 'work_revised'}:
            ids.append(result['work_id'])
    def successor(key):
        seen = set()
        while key in replacements:
            if key in seen: raise ValueError('Cyclic replacement history.')
            seen.add(key); key = replacements[key]
        return key
    ids = list(dict.fromkeys(successor(key) for key in ids))
    return ids, applied, plans


def sequence_request_refused(value, index):
    """An explicit pre-custody rejection has no sequence execution to reconcile."""
    from .work_sequence import FILE
    event = value['events'][index]
    if event['kind'] != 'execution_requested' or event['selection']['mode'] != 'sequence':
        return False
    directory = event['selection']['sequence']
    if (Path(directory)/FILE).exists():
        return False
    outcome = next((e for e in value['events'][index+1:]
                    if e['kind'] in {'execution_requested', 'execution_observed'}), None)
    return bool(outcome and outcome['kind'] == 'execution_observed'
                and (outcome.get('detail') or {}).get('status') == 'REFUSED'
                and (outcome.get('selection') or {}).get('sequence') == directory)


def next_step(value):
    selected = value['snapshot']['selection']
    if (Path(value['control_root']).resolve() != Path(relay_mesh.RELAY_ROOT).resolve()
            or Path(value['root']).resolve() != Path(selected['root']).resolve()):
        raise ValueError('Demand project/control root changed.')
    def step(action, reason, **fields):
        return {'action': action, 'reason': reason, **fields}
    if console_recovery.pending(value):
        return step('CHECK_RECOVERY', 'An operation is still unconfirmed. Inspect its receipts before continuing.')
    closure = next((i for i in range(len(value['events'])-1, -1, -1)
                    if value['events'][i]['kind'] == 'demand_result_confirmed'), None)
    if closure is not None:
        report = value['events'][closure]['detail']
        browsing = {'draft_saved', 'message_saved', 'stage_opened', 'next_step_opened'}
        if report['disposition'] == 'FULFILLED' and all(e['kind'] in browsing for e in value['events'][closure+1:]):
            from .demand_outcome import inspect
            try:
                unchanged = inspect(value, resolutions=report.get('resolutions')) == report['evidence']
            except Exception:
                # Strict native JSON has its own rejection type. Any failed
                # comparison requires review; it can never establish completion.
                unchanged = False
            if unchanged:
                return step('DEMAND_COMPLETE', 'Demand result was reviewed as fulfilled. Its recorded evidence is unchanged.',
                            summary=report['summary'], result_sha256=report['sha256'])
            return step('REVIEW_DEMAND_RESULT', 'Evidence changed after the reviewed result. Review the demand again.')
    ids, applied, plans = linkage(value)
    # A newly reviewed successor remains a review step; it cannot silently replace prior Works.
    if plans and plans[-1] not in applied:
        return step('REVIEW_PLAN', 'Review the saved PLAN; Apply still checks its current proposal and graph.', plan=plans[-1])
    if not ids:
        if selected['mode'] == 'plan' and selected.get('plan'):
            return step('REVIEW_PLAN', 'Review the selected PLAN before applying it.', plan=selected['plan'])
        if selected['mode'] == 'plan' and selected.get('findings'):
            from .planning_findings import load
            load(selected['findings'], selected['findings_sha256'], value['root'])
            return step('START_PLAN', 'Reviewed findings are attached. Review the planning request and Start PLAN.')
        if selected['mode'] == 'work' and selected.get('work'):
            ids = [selected['work']]
        else:
            knowledge = next((e for e in reversed(value['events']) if e['kind'] == 'execution_requested'
                              and e['selection']['mode'] in {'research', 'parallel', 'session'}), None)
            if knowledge:
                choice = knowledge['selection']; mode = choice['mode']
                field = 'batch' if mode == 'parallel' else mode
                if mode in {'research', 'parallel'}:
                    from . import research_flow, parallel_explore
                    view = (research_flow if mode == 'research' else parallel_explore).inspect(choice[field])
                    if Path(view['root']).resolve() != Path(value['root']).resolve():
                        raise ValueError('Linked knowledge flow belongs to another project.')
                    if view['status'] == 'COMPLETED':
                        return step('REVIEW_FINDINGS', 'Review the completed findings for PLAN, or review the demand result.',
                                    mode=mode, owner_field=field, owner=choice[field])
                else:
                    index = value['events'].index(knowledge)
                    observation = console_recovery.observe({**value, 'events': value['events'][:index+1]})
                    if observation['state'] == 'RECOVERED' and observation['result']['status'] == 'PROPOSED':
                        return step('REVIEW_DEMAND_RESULT', 'Review the delivered session result against your original request.')
                return step('RESUME_FLOW', 'Inspect the saved flow and its next action before resuming.',
                            mode=mode, owner_field=field, owner=choice[field])
            return step('NO_RECIPE', 'No linked PLAN/Work recipe. Continue the selected flow through its existing controls.')
    from .console_backend import Selection, require_control_graph
    require_control_graph(Selection(value['root'], graph_path=selected.get('graph_path', '')))
    raw = read_json(relay_mesh.WORK_GRAPH_FILE, 32*1024*1024)
    if validate_work_graph(raw)['status'] != 'PASS':
        raise ValueError('Work graph is invalid; next step cannot be selected.')
    wg = WorkGraph(raw)
    missing = [key for key in ids if key not in wg.by_id]
    if missing:
        return step('BLOCKED', 'Linked Works are missing from the current graph.', missing=missing)
    # A demand-started sequence owns recovery; do not replace it with individual runs.
    for index in range(len(value['events']) - 1, -1, -1):
        event = value['events'][index]
        if event['kind'] != 'execution_requested' or not (event.get('detail') or {}).get('demand_sequence'):
            continue
        from .work_sequence import inspect
        directory = event['selection']['sequence']
        # A rejected CLI request can finish before sequence custody is created.
        # That explicit rejection is not an interrupted sequence to resume.
        if sequence_request_refused(value, index):
            continue
        view = inspect(directory)
        if Path(view['root']).resolve() != Path(value['root']).resolve():
            raise ValueError('The linked sequence belongs to another project.')
        if view['status'] != 'COMPLETED':
            return step('RESUME_SEQUENCE', 'Inspect and resume the saved sequence; completed Works will not run again.',
                        sequence=directory)
        break
    done = [key for key in ids if wg.status_of(wg.by_id[key]) == 'DONE']
    common = {'work_ids': ids, 'done': done, 'graph_revision': compute_revision_hash(raw)}
    pending = [key for key in ids if key not in done]
    if not pending:
        return step('LINKED_WORKS_DONE', 'All linked Works are DONE in the current graph. This does not declare the whole demand complete.', **common)
    blocked = []
    pending.sort(key=lambda key: wg.by_id[key].get('priority', 'P3'))
    for key in pending:
        node = wg.by_id[key]
        if wg.status_of(node) == 'SUPERSEDED':
            blocked.append({'work_id': key, 'reason': 'Review the successor of this superseded Work.'}); continue
        ready = readiness(wg, node, 'operational')
        if ready['unresolved_prerequisites']:
            blocked.append({'work_id': key, 'prerequisites': ready['unresolved_prerequisites']}); continue
        fields = {**common, 'work_id': key}
        if node.get('admission_state') == 'PROPOSED_FROM_PLAN_NOT_ADMITTED':
            return step('PREPARE_WORK', 'Review criteria and verification for '+key+' before confirming preparation.', **fields)
        if not ready['ready']:
            blocked.append({'work_id': key, 'reasons': ready['reasons']}); continue
        state = work_run.status(key)
        if state.get('receipt_errors'):
            return step('INSPECT_WORK', 'Some execution receipts are unavailable. Inspect before deciding Start or Resume.', **fields)
        receipt = state.get('last_run')
        if receipt and Path(receipt['repository_root']).resolve() != Path(value['root']).resolve():
            return step('BLOCKED', 'The linked execution belongs to another project.', **fields)
        control = state.get('operational_control') or {}
        if control.get('status') in {'PAUSE_REQUESTED', 'PAUSED', 'RESUME_REQUESTED'}:
            from .work_console import control_copy
            return step('INSPECT_WORK', control_copy(control)+' Inspect controls; Resume must be explicit.', **fields)
        wait = state.get('human_wait')
        if wait and Path(wait['request']['repository_root']).resolve() != Path(value['root']).resolve():
            return step('BLOCKED', 'The pending decision belongs to another project.', **fields)
        if wait:
            return step('HUMAN_DECISION', 'Open the saved decision; its current state determines whether Resume is allowed.', **fields)
        if state.get('train') or receipt:
            return step('RESUME_WORK', 'Inspect the existing attempt for '+key+' and use Resume; no new execution was started.', **fields)
        return step('START_WORK', key+' is prepared. Start performs the current execution checks.', **fields)
    return step('BLOCKED', 'Linked Works have unresolved prerequisites or need review; no outside Work was selected.', blocked=blocked, **common)


def preview_sequence(value):
    """Offer only this demand's linked, prepared Works to the existing sequence owner."""
    from .work_sequence import preview
    step = next_step(value)
    if step['action'] != 'START_WORK':
        raise ValueError(step['reason'])
    pending = [key for key in step['work_ids'] if key not in step['done']]
    raw = read_json(relay_mesh.WORK_GRAPH_FILE, 32*1024*1024)
    wg = WorkGraph(raw)
    pending.sort(key=lambda key: wg.by_id[key].get('priority', 'P3'))
    # Preparation is never inferred from approval of the PLAN. Every selected Work
    # must already have its own admitted contract and verifier (checked by preview).
    view = preview(pending, value['root'])
    for key in pending:
        state = work_run.status(key)
        if state.get('receipt_errors') or state.get('train') or state.get('last_run') or state.get('human_wait'):
            raise ValueError('Inspect the existing execution of '+key+' before grouping Works.')
        external = [r['id'] for r in readiness(wg, wg.by_id[key], 'operational')['unresolved_prerequisites']
                    if r['id'] not in pending]
        if external:
            raise ValueError(key+' requires outside Works: '+', '.join(external))
    return view
