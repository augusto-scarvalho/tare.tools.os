"""Read-only Work selection and terminal views over the existing owners."""
def inspect_work(*, work_id=None, profile='operational', limit=10, graph_path=None):
    from graph_backlog.core import WorkGraph
    from graph_backlog.algorithms import ranked_next, readiness
    from graph_backlog.validation import validate_work_graph
    from tare_ops import read_graph
    from . import relay_mesh

    if graph_path:
        from .project_graph import graph_path as validate_graph_path
        path = validate_graph_path(graph_path)
    else:
        path = relay_mesh.WORK_GRAPH_FILE
    raw = read_graph(path)
    if validate_work_graph(raw)['status'] != 'PASS':
        raise ValueError('INVALID_WORK_GRAPH: use backlog validate for details')
    graph = WorkGraph(raw)
    result = {'authority': 'NONE', 'graph': str(path), 'profile': profile}
    if work_id is None:
        if not 1 <= limit <= 100:
            raise ValueError('limit must be between 1 and 100')
        rows = ranked_next(graph, profile, limit)
        result['suggestions'] = [{**row,
            'admission_state': graph.by_id[row['id']].get('admission_state', 'UNKNOWN')}
            for row in rows]
    else:
        node = graph.get_node(work_id)
        if node is None:
            raise ValueError('WORK_NOT_FOUND:' + work_id)
        result['work'] = {key: node.get(key) for key in (
            'id', 'title', 'summary', 'priority', 'admission_state', 'completion',
            'exit_criteria', 'repository_scopes', 'target_paths')}
        result['verification_checks'] = (node.get('metrics') or {}).get('verification_checks', [])
        result['readiness'] = readiness(graph, node, profile)
        from .workflow_compiler import compile_recipe, recipe, owner_capabilities, owner_effects
        result['workflow'] = compile_recipe(recipe(), capabilities=owner_capabilities(),
                                            allowed_effects=owner_effects())
    return result


def control_copy(view):
    return {'PAUSE_REQUESTED': 'Pause requested. Finishing the current round.',
            'PAUSED': 'Work paused. You can close this TUI.',
            'RESUME_REQUESTED': 'Resume saved. Select Resume to continue Work.'}.get((view or {}).get('status'), '')


def render(action, result):
    """Presentation only: projected readiness is never an execution/admission receipt."""
    if result.get('error') and action not in {'run', 'status'}:
        return 'Status: ' + result.get('status', 'UNAVAILABLE') + '\nReason: ' + result['error']
    if action == 'next':
        lines = ['Graph: ' + result['graph'], 'Profile: ' + result['profile']]
        rows = result['suggestions']
        lines.append('Suggested work (Backlog ordering; execution not checked):')
        for row in rows:
            lines.append(f"  {row['id']} | {row['priority']} | {row['title']} | {row['admission_state']}")
        if not rows:
            lines.append('  No work meets this profile. Use --profile planning to inspect planning candidates.')
        lines.append('Inspect an ID with: tare-ops explain WORK_ID')
        return '\n'.join(lines)
    if action == 'explain':
        work, ready = result['work'], result['readiness']
        lines = ['Graph: ' + result['graph'], f"Work: {work['id']} — {work['title']}",
                 'Admission recorded: ' + str(work.get('admission_state') or 'UNKNOWN'),
                 f"Backlog readiness ({result['profile']}): {'READY' if ready['ready'] else 'BLOCKED'}"]
        if work.get('summary'):
            lines.extend(['', work['summary']])
        for reason in ready['reasons']:
            lines.append('Reason: ' + reason)
        for blocker in ready['unresolved_prerequisites']:
            lines.append(f"Blocked by: {blocker['id']} ({blocker['completion']}) — {blocker['title']}")
        lines.append('Exit criteria:')
        lines.extend('  ' + item for item in work.get('exit_criteria') or ['None recorded.'])
        lines.append('Repository scopes:')
        for scope in work.get('repository_scopes') or []:
            paths = scope.get('target_paths') or work.get('target_paths') or []
            lines.append('  ' + scope['repository'] + ': ' + ', '.join(paths))
        lines.append('Verification commands (recorded argv, not executed):')
        import json
        for check in result['verification_checks']:
            lines.append('  ' + check['criterion'] + ': ' + json.dumps(check['command'], ensure_ascii=False))
        if not result['verification_checks']:
            lines.append('  None recorded; run requires commands matching the exit criteria.')
        lines.append('Execution still checks the descriptor, plan, candidate and admission.')
        if result.get('workflow'):
            from .workflow_compiler import render_compiled
            lines.extend(['', render_compiled(result['workflow'])])
        return '\n'.join(lines)

    # run and status share the existing work-run receipt, including failures.
    receipt = result if action == 'run' else result.get('last_run')
    lines = ['Work: ' + result.get('work_id', '?')]
    message = control_copy(result.get('operational_control'))
    if message:
        lines.extend([message, 'Inspect saved Work controls or select Resume explicitly.'])
    if result.get('human_wait'):
        from .human_wait import render as render_wait
        lines.append(render_wait(result['human_wait']))
    if action == 'status':
        train = result.get('train')
        lines.append('Train: ' + str(train['status']) if train else 'Train: none recorded')
    if not receipt:
        lines.append('Run: none recorded')
    else:
        lines.extend(['Run: ' + receipt['status'], 'Last recorded stage: ' + receipt['stage']])
        if receipt['status'] == 'RUNNING':
            lines.append('Last recorded observation; process liveness is not checked.')
        if receipt.get('error'):
            lines.append('Reason: ' + receipt['error'])
        if receipt['status'].startswith('IMPLEMENTATION_'):
            for stage in receipt.get('stages', []):
                if stage['stage'] == 'implement' and stage.get('reason'):
                    lines.append('Reason: ' + stage['reason'])
        from .landing_policy import SAME_VENDOR_AUDIT
        from .work_audit import route_reason

        def reason(row):  # a route saved before the vendor rule reads as the rule names it now
            named = route_reason(row)
            return named+(' · '+SAME_VENDOR_AUDIT if named == 'SAME_VENDOR_NOT_INDEPENDENT' else '')
        routing = receipt.get('audit_routing') or {}
        if routing:
            lines.append('Code audit: '+routing['status']+' · '+routing['reason'])
            for route in routing['routes']:
                lines.append('  '+route['deployment']['id']+': '+reason(route))
        if routing.get('selection'):
            # A configured-list candidate (native_routes._reviewers) saves no cross_vendor: the route of the same
            # deployment in this record says it; without one the candidate stays as saved.
            vendors = {route['deployment']['id']: route.get('cross_vendor') for route in routing.get('routes', [])}
            for candidate in routing['selection']['candidates']:
                if 'cross_vendor' not in candidate:
                    candidate = {**candidate, 'cross_vendor': vendors.get(candidate.get('selected_deployment', candidate['deployment']))}
                lines.append('  Candidate '+candidate['deployment']+': '+reason(candidate))
        if receipt.get('audit'):
            audit = receipt['audit']
            lines.append('Auditor: '+audit['deployment']+' · '+str(audit.get('verdict') or audit.get('reason') or audit['result']['status'])
                         +(' · '+SAME_VENDOR_AUDIT if audit.get('cross_vendor') is False else ''))
            from .native_display import render_failure
            lines.extend(render_failure(audit['result']))
            lines.append('Audit session: '+audit['session'])
        for event in receipt.get('stages', []):
            if event['stage'] == 'audit_fallback':
                lines.append('Audit fallback: '+event['from_deployment']+' · '+event['reason'])
        if receipt.get('candidate_commit'):
            lines.append('Candidate: ' + receipt['candidate_commit'])
        if receipt.get('recovery'):
            if receipt.get('resumed_execution'):
                lines.append('Resumed original implementation; session model calls: ' + str(receipt['resumed_execution']['model_calls_total']))
            else:
                lines.append('Recovered accepted implementation candidate without rerunning its model or verifiers.')
            lines.append('Original run: ' + receipt['resumed_from'])
        if receipt.get('admission'):
            lines.append('Admission: ' + receipt['admission']['verdict'])
            admitted = receipt['admission'].get('audit') or {}
            if admitted.get('counted') and admitted.get('cross_vendor') is False:
                lines.append('Audit counted: True · '+SAME_VENDOR_AUDIT)
            elif admitted:
                lines.append('Independent audit counted: '+str(admitted['counted']))
            lines.extend('  ' + reason for reason in receipt['admission'].get('reason_codes', []))
        landing = receipt.get('landing')
        if landing:
            lines.append('Landed: ' + str(landing['landed']) + ' → ' + landing['target_ref'])
            stale = (landing.get('checkout') or {}).get('stale', [])
            if stale:
                lines.append('Checkout needs reconciliation: ' + ', '.join(stale))
        if receipt.get('duration_ms') is not None:
            lines.append(f"Time: {receipt['duration_ms']/1000:.1f}s")
        limits = receipt.get('effective_limits') or {}
        def ceiling(value):
            return 'unlimited' if value is None else str(value)
        for name in ('plan', 'implementation'):
            if name in limits:
                values = limits[name]
                lines.append(f"{name.capitalize()} limits: calls={ceiling(values['max_model_calls'])}, "
                             f"seconds={ceiling(values['max_seconds'])}")
    for error in result.get('receipt_errors', []):
        lines.append('Receipt unavailable: ' + error['path'] + ' (' + error['reason'] + ')')
    if result.get('error') and action == 'status':
        lines.append('Reason: ' + result['error'])
    return '\n'.join(lines)
