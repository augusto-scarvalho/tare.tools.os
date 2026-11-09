"""Terminal presentation of recorded native results; no model calls or new evidence."""
import os
import shlex
import json
import time
from pathlib import Path


def recorded_checks(session):
    """Read verified history for display only; never restore verifier authority."""
    from compute_plane.native_journal import NativeJournal, custody_lock
    root = Path(session)
    if not (root/'journal.jsonl').is_file():
        return {}
    try:
        with custody_lock(root):
            records = NativeJournal(root/'journal.jsonl').snapshot().records
        names, checks = {}, {}
        for record in records:
            event = record.event()
            if event['kind'] == 'effect_requested' and event['tool_call']['name'] == 'verify':
                names[event['effect_id']] = json.loads(event['tool_call']['arguments_json'])['name']
            elif event['kind'] == 'effect_returned':
                result = event['result']
                name = names.get(result['effect_id'])
                if name is not None and result['status'] in {'succeeded', 'failed'}:
                    checks[name] = json.loads(result['output_json'])
        return checks
    except (OSError, ValueError, KeyError, TypeError):
        return None  # Missing/unreadable evidence must not become an invented PASS.


def safe_print(text, file=None):
    """Write text plus newline, backslash-escaping characters the target encoding cannot represent."""
    import sys
    if file is None:
        file = sys.stdout
    buffer = getattr(file, 'buffer', None)
    if buffer is not None:
        encoding = getattr(file, 'encoding', None) or 'utf-8'
        buffer.write(text.encode(encoding, errors='backslashreplace') + b'\n')
    else:
        file.write(text + '\n')
    file.flush()


def render_failure(result):
    lines = []
    if result.get('failure_code'):
        lines.append('Failure: '+result['failure_code'])
    reason = result.get('provider_error') or {}
    if reason:
        lines.append('Provider said: '+': '.join(str(reason[k]) for k in ('type', 'message') if reason.get(k)))
    if result.get('delivery_validation'):
        lines.append('Delivery validation:')
        lines.extend('  '+item['field']+': '+item['code']
                     for item in result['delivery_validation']['issues'])
    return lines


def provider_limit_guidance(result):
    probe = result.get('qualification') or {}
    cli = (result.get('runtime_observation') or {}).get('deployment_id') or 'this CLI'
    if 'CLI_QUALIFICATION_FAILED' in (result.get('reason'), result.get('failure_code')):
        release = ' '.join(str(probe[k]) for k in ('version', 'model') if probe.get(k))
        return (f"{cli} was updated{' to ' + release if release else ''} and no longer answers in the shape tare "
                f"decodes ({probe.get('probe_code') or 'shape check'} on a one-call probe"
                + (f": {probe['detail']}" if probe.get('detail') else '') + '). Your request was not sent. '
                'Choose another model or CLI, or run tare setup --prove for this CLI to check it again.')
    if 'CLI_QUALIFICATION_INTERRUPTED' in (result.get('reason'), result.get('failure_code')):
        return (f'The check of {cli}\'s new release was cut short, so your request was not sent. Resume to '
                'check it again and continue.')
    if result.get('failure_code') in ('CLI_USAGE_LIMIT', 'PLAN_USAGE_LIMIT'):
        limit = result.get('usage_limit') or {}
        until = limit.get('until')
        when = f" until {time.strftime('%H:%M', time.localtime(until))}" if type(until) in (int, float) else ''
        return (f"The {limit.get('account') or 'provider'} account behind this model has no usage left{when}; "
                'tare auto and tare serve skip its routes until then. Choose another model, or use Model → '
                'Continue task to move this conversation to another provider with a compact brief.')
    if (result.get('status') == 'BLOCKED' and result.get('reason') == 'MODEL_FAILURE'
            and result.get('failure_code') == 'HTTP_429'):
        return ('The provider limited this request (HTTP 429). '
                'Use Model → Continue task to choose another provider and review a compact brief. '
                'History, recorded usage and existing limits are preserved.')
    if (result.get('status') == 'BLOCKED' and result.get('reason') == 'MODEL_FAILURE'
            and result.get('failure_code') == 'CLI_GENERATION_FAILED'
            and result.get('provider_rate_limit_observed') is True):
        return ('Claude reported an API rate limit during this failed request. '
                'The final cause and remote usage are unknown. Review saved changes. '
                'Use Model → Continue task to choose another provider and review a compact brief. '
                'History, recorded usage and existing limits are preserved.')
    return None


def recovery_guidance(result):
    """Describe recorded recovery eligibility; execution always rechecks custody."""
    if (result.get('status') != 'RECONCILIATION_REQUIRED'
            or result.get('reason') != 'UNFINISHED_INVOCATION'):
        return None
    recovery = result.get('model_recovery', {})
    if recovery.get('transport_eligible') is True:
        prefix = ('The model response was interrupted; local cleanup is confirmed. '
                  'Remote completion and usage remain unknown. ')
        if recovery.get('owner_interrupted'):
            prefix = ('The session process ended before saving a model response. '
                      'Remote completion and usage remain unknown. ')
        deadline = recovery.get('deadline_utc')
        if recovery.get('remaining_model_calls') == 0:
            return prefix + 'The saved model-call limit is exhausted. Review saved changes before starting a new session.'
        if (recovery.get('reason') == 'DEADLINE_EXHAUSTED'
                or deadline is not None and time.time() >= deadline):
            return prefix + 'The saved session deadline has expired. Review saved changes before starting a new session.'
        if recovery.get('available') is True:
            return prefix + ('Resume discards the incomplete response and makes a new model call under the saved limits. '
                             'Completed tool actions are preserved; the interrupted call may still be charged.')
    return ('This operation has no confirmed outcome. Resume checks saved tool results and edit snapshots '
            'before continuing. An external command without proof of completion is not repeated; '
            'inspect its process and output in the terminal.')


def render_result(result, *, interactive=False):
    lines = ['Status: ' + result['status']]
    if result.get('unschedulable'):
        from .scheduling_diagnostics import render_report
        lines.extend(['', render_report(result['unschedulable'])])
    elif result.get('output'):
        lines.extend(['', result['output']])
    if result.get('reason') and result['status'] != 'PROPOSED':
        lines.extend(['', 'Reason: ' + result['reason']])
    guidance = recovery_guidance(result)
    if guidance:
        lines.extend(['', 'Recovery guidance: ' + guidance])
    lines.extend(render_failure(result))
    limit_guidance = provider_limit_guidance(result)
    if limit_guidance:
        lines.extend(['', 'Provider guidance: ' + limit_guidance])
    if result.get('source_recovery'):
        recovery = result['source_recovery']
        lines.extend(['', 'Source recovery: '+recovery['state']+' ('+recovery['reason']+')'])
    if result.get('source_changes'):
        lines.extend(['', 'Changed since last observation (previously read files only):'])
        for change in result['source_changes']:
            status = 'missing' if change['current_sha256'] is None else 'changed'
            lines.append(f"  {change['repository']}:{change['path']} ({status})")
        lines.append('Previous checks do not verify these changes.')

    observations = result.get('capability_observations', {})
    checks = observations.get('verifications', {})
    history = recorded_checks(result['session']) if result.get('session') else {}
    previous = {k: v for k, v in (history or {}).items() if k not in checks}
    lines.extend(['', 'Recorded named checks:'])
    if not checks and not previous:
        lines.append('  None recorded this invocation.')
    for name, check in sorted(checks.items()):
        code = check.get('returncode')
        status = 'PASS' if code == 0 else 'FAIL' if code is not None else 'UNKNOWN'
        if (check.get('effects') is None or observations.get('effects') is None
                or check['effects'] != observations['effects']):
            status += ' (before later edits or freshness unknown)'
        lines.append(f'  {name}: {status}')
    for name, check in sorted(previous.items()):
        code = check.get('returncode')
        status = 'PASS' if code == 0 else 'FAIL' if code is not None else 'UNKNOWN'
        lines.append(f'  {name}: previous {status} (not rerun this invocation)')
    if history is None:
        lines.append('  Earlier check history unavailable.')
    if observations.get('terminal'):
        lines.extend(['', 'Terminal activity was recorded separately. Inspect command output for test results; '
                      'terminal commands are not named checks.'])

    if 'model_calls' in result:
        lines.extend(['', f"Model calls (session): {result['model_calls']}"])
    tokens = result.get('tokens', {}).get('total_tokens')
    if tokens is not None:
        partial = ' (partial)' if tokens.get('unknown_calls') else ''
        lines.append(f"Tokens (session): {tokens['known_sum']}{partial}")
    if 'elapsed_from_preparation_ms' in result:
        lines.append(f"Time (this invocation): {result['elapsed_from_preparation_ms']/1000:.1f}s")

    session = result.get('session')
    if session:
        lines.extend(['', 'Session: ' + session])
        if result['status'] == 'PROPOSED' and not interactive:
            argv = ['tare-ops', 'agent', '--native', '--resume', '--interactive',
                    '--format', 'text', '--session', session]
            if os.name == 'nt':
                command = ' '.join(argv[:-1]) + " '" + session.replace("'", "''") + "'"
                label = 'Continue (PowerShell):'
            else:
                command, label = shlex.join(argv), 'Continue:'
            lines.extend(['', label, '  ' + command])
    return '\n'.join(lines)


def conversation_status(result, *, exit_code=None):
    """Human-readable state without promoting a proposal to accepted work."""
    status = result.get('status', 'UNAVAILABLE')
    if status == 'BLOCKED' and result.get('unschedulable'):
        return 'No route can start'
    if status == 'PROPOSED' and exit_code not in (None, 0):
        return 'Execution ended with an error'
    task = result.get('conversation_task') or {}
    if status == 'PROPOSED' and task:
        if task.get('question'):
            return 'Waiting for your answer'
        if task.get('stage') == 'DELIVERED':
            return 'Change verified and reviewed'
        if task.get('intent') == 'deliver':
            return 'Change pending · '+task.get('stage', 'discovery').replace('_', ' ').lower()
        if task.get('stage') == 'PLANNED':
            return 'Plan ready'
    return {'PROPOSED': 'Response ready', 'CANCELLED': 'Cancelled',
            'RECONCILIATION_REQUIRED': 'Recovery needs review',
            'BLOCKED': 'Unable to continue', 'FAILED': 'Execution failed'}.get(
                status, status.replace('_', ' ').capitalize())


def conversation_result(result, *, exit_code=None):
    """Bounded display snapshot; receipts remain authoritative in the session."""
    observations = result.get('capability_observations', {})
    counts = {'failed': 0, 'passed': 0, 'to recheck': 0, 'unknown': 0}
    for check in observations.get('verifications', {}).values():
        code = check.get('returncode')
        if code is None:
            key = 'unknown'
        elif code != 0:
            key = 'failed'
        elif (check.get('effects') is None
                or observations.get('effects') is None
                or check['effects'] != observations['effects']):
            key = 'to recheck'
        else:
            key = 'passed'
        counts[key] += 1
    checks = ' · '.join(f'{count} {label}' for label, count in counts.items() if count)
    notices = ['Named checks this turn: '+checks] if checks else (
        ['No named checks recorded this turn.']
        if result.get('source_changes') or observations.get('effects') or observations.get('terminal') else [])
    task = result.get('conversation_task') or {}
    if task.get('stage') == 'DELIVERED':
        notices = []  # the status line already says "Change verified and reviewed", from the same record
    if observations.get('terminal'):
        notices.append('Terminal activity recorded. Inspect command output in tool activity for test results.')
    status = result.get('status')
    unschedulable = result.get('unschedulable')
    guidance = recovery_guidance(result)
    limit_guidance = provider_limit_guidance(result)
    if unschedulable:
        from .scheduling_diagnostics import render_report
        notices = [render_report(unschedulable)]
        if result.get('execution') == 'agent_not_started':
            notices.append('No model or project tool ran. Choose an available model or review the route setup, then send again.')
    elif status == 'CANCELLED':
        notices.insert(0, 'Cancelled. Earlier changes may still be present; review them before continuing.')
    elif guidance:
        notices.insert(0, 'Recorded reason: '+result['reason']+'. '+guidance)
    elif limit_guidance:
        notices.insert(0, limit_guidance)
    elif status != 'PROPOSED':
        meanings = {'GPU_STATUS_UNKNOWN': 'the local model is unavailable',
                    'CLI_USAGE_LIMIT': 'the quota or usage limit is exhausted',
                    'PLAN_USAGE_LIMIT': "this model's plan allowance is exhausted"}
        recorded = list(dict.fromkeys(str(value) for value in (
            result.get('reason'), result.get('failure_code')) if value))
        reason = '; '.join(meanings.get(value, '('+value+')') for value in recorded)
        notices.insert(0, (conversation_status(result)+': '+reason if reason else
                           conversation_status(result))+'; open Execution details before continuing.')
    if result.get('source_changes'):
        notices.append('Source changes were observed. Earlier checks do not verify those changes.')
    if result.get('delivery_validation'):
        notices.append('The result needs attention. See the recorded failure in execution details.')
    activity = result.get('tool_activity', {})
    if activity.get('omitted'):
        notices.append(f"Tool activity: {activity['omitted']} older operations omitted from this display.")
    if activity.get('observation_failures'):
        notices.append('Some tool activity updates were unavailable. Saved tool results remain authoritative.')
    exit_failure = status == 'PROPOSED' and exit_code not in (None, 0)
    if exit_failure:
        notices.insert(0, f'Process exited with code {exit_code}. Review execution details before continuing.')

    def bounded(text, limit):
        text = str(text or '')
        return text if len(text) <= limit else text[:limit]+'\n[Display shortened; full evidence is saved in the session.]'

    details = render_result({**result, 'output': None}, interactive=True)
    if exit_code is not None:
        details += f'\n\nProcess exit: {exit_code}'
    return {'kind': 'execution-result', 'status': status,
            'body': '' if unschedulable else bounded(result.get('output'), 16000),
            'notice': bounded('\n'.join(notices), 4000),
            'attention': bool(counts['failed'] or counts['to recheck'] or counts['unknown']
                              or result.get('source_changes') or result.get('failure_code')
                              or result.get('delivery_validation') or status != 'PROPOSED' or exit_failure),
            'details': bounded(details, 16000)}
