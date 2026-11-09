"""Advisory model-response recovery from recorded evidence; never tool recovery."""
import time
import socket
import math


def _gone(pid, born_before):
    """No process holds this PID, or the one holding it was created after born_before: the recorded process
    ended and its PID was reused."""
    import psutil
    try:
        return psutil.Process(pid).create_time() > born_before
    except psutil.NoSuchProcess:
        return True
    except (psutil.Error, OSError, ValueError):
        return False


def _moment(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def _pending_owner_dead(records, call_id, invocations, custody_lock_held=False, transports=()):
    """Only absence of the local processes of this exact request is proof: its owner, or, while the owner lives
    on (the same TUI), every HTTP worker the kernel recorded for it.

    Journal intervals are half-open. A later attempt may close an unfinished
    interval, but its identity can never stand in for the original owner.
    Legacy evidence requires a completed invocation and explicit lock custody;
    overlapping or otherwise ambiguous custody fails closed.
    """
    requests = [i for i, r in enumerate(records)
                if r.event().get('kind') == 'model_requested' and r.event().get('call_id') == call_id]
    if len(requests) != 1 or not invocations:
        return False
    owners = []
    for i, row in enumerate(invocations):
        start = row.get('journal_start')
        following = invocations[i + 1].get('journal_start') if i + 1 < len(invocations) else len(records)
        end = row.get('journal_end', following)
        if (type(start) is not int or type(end) is not int or type(following) is not int
                or not 0 <= start <= end <= following <= len(records)):
            return False
        if start <= requests[0] < end:
            owners.append(row)
    if len(owners) != 1:
        return False
    owner = owners[0]
    if 'owner_pid' not in owner:
        ended = owner.get('ended_at')
        return (custody_lock_held is True and type(ended) in (int, float)
                and math.isfinite(ended) and ended > 0)
    pid = owner.get('owner_pid')
    if (owner.get('owner_host') != socket.gethostname()
            or type(pid) is not int or pid <= 0):
        return False
    # The owner existed when its invocation started; a holder of its PID created later is another process.
    if _gone(pid, owner['started_at'] if _moment(owner.get('started_at')) else math.inf):
        return True
    # Each worker was created before its invocation ended; a worker the kernel left without a PID stays unproven.
    return (_moment(owner.get('ended_at')) and bool(transports)
            and all(type(t.get('pid')) is int and t['pid'] > 0 and _gone(t['pid'], owner['ended_at'])
                    for t in transports))


HTTP_TRANSPORT = 'isolated_stdlib_http'  # the kernel's HTTP transport receipt (CLI receipts say official_vendor_cli)


def _http_transport(transports):
    """Require positive HTTP evidence and reject CLI or mixed evidence."""
    return bool(transports) and all(
        type(t.get('http_attempts_upper_bound')) is int and t['http_attempts_upper_bound'] > 0
        and 'vendor' not in t and t.get('transport') == HTTP_TRANSPORT
        for t in transports)


RECOVERABLE_ERRORS = frozenset({
    'HTTP_CANCELLED', 'HTTP_DEADLINE', 'HTTP_INCOMPLETE_EXCHANGE',
    'HTTP_INCOMPLETE_STREAM', 'HTTP_OUTPUT_LIMIT', 'CLI_INCOMPLETE_EXCHANGE',
})

CLI_PREFLIGHT_ERRORS = frozenset({'CLI_VERSION_CHANGED', 'CLI_VERSION_UNAVAILABLE',
                                 'CLI_INCOMPATIBLE_VERSION', 'CLI_CAPABILITY_UNAVAILABLE',
                                 'CLI_USER_INSTRUCTIONS_PRESENT', 'CLI_QUALIFICATION_FAILED',
                                 'CLI_QUALIFICATION_INTERRUPTED'})


def model_call_ceiling(journal, initial):
    """Replay explicit Runtime call grants; never infer a grant from spent calls."""
    if initial is None:return None
    maximum=initial;previous=0
    for record in journal.snapshot().records:
        event=record.event()
        if event.get('kind')=='user_message' and event.get('model_call_limit') is not None:
            value=event['model_call_limit']
            if type(value) is not int or value<max(1,previous):raise ValueError('Invalid saved model-call grant.')
            previous=value;maximum=max(maximum,value)
    return maximum


def agy_terminal_process_stall(observations):
    """Diagnostics justify host requalification, never acceptance of an old proposal."""
    rows=[r.event() for r in observations.snapshot().records if r.event().get('purpose')=='model']
    row=rows[-1] if rows else {}
    calls=row.get('transport_calls',[])
    if row.get('error')!='CLI_INCOMPLETE_EXCHANGE' or len(calls)!=1:return False
    call=calls[0]
    return (call.get('vendor')=='agy' and call.get('timeout') is True
        and call.get('cleanup_verified') is True and call.get('cancelled') is False
        and call.get('output_limit') is False and call.get('incomplete_stream_lines')==0
        and call.get('hook_phases') in (['pre','post','stop'],['pre','tool','post','stop'])
        and set(call.get('step_tool_names',[]))<={'finish'}
        and 'result' in call.get('stream_event_types',[])
        and call.get('inference_invocations_observed')==1)


def completed_delivery_refusal(events, observations, session_id):
    """Prove a completed, locally rejected response, never an uncertain dispatch."""
    from compute_plane.native_models import REPAIRABLE_DELIVERY_ISSUES
    failed = events[-1] if events else {}
    call_id = failed.get('call_id', '')
    if (failed.get('kind') != 'model_failed' or failed.get('code') != 'INVALID_DELIVERY'
            or not call_id.startswith(session_id + '/model/')
            or not any(e.get('kind') == 'model_requested' and e.get('call_id') == call_id for e in events)):
        return None
    rows = [e for e in observations if e.get('purpose') == 'model' and e.get('call_id') == call_id]
    observed = rows[-1] if rows else {}
    validation = observed.get('delivery_validation') or {}
    issues = validation.get('issues') or []
    if (observed.get('status') != 'invalid_delivery' or observed.get('error') != 'INVALID_DELIVERY'
            or not observed.get('response_sha256') or not issues
            or any(i.get('code') not in REPAIRABLE_DELIVERY_ISSUES for i in issues)
            or any(t.get('response_received') is not True or t.get('cleanup_verified') is not True
                   for t in observed.get('transport_calls', []))):
        return None
    return validation


def inspect(journal, observations, session_id, *, max_model_calls, deadline_utc, owner_interrupted=False,
            invocations=None, custody_lock_held=False):
    records = journal.snapshot().records
    last = records[-1].event() if records else {}
    crashed = owner_interrupted and last.get('kind') == 'model_requested'
    not_started = (last.get('kind') == 'model_failed' and last.get('not_started') is True
                   and last.get('code') in CLI_PREFLIGHT_ERRORS)
    call_id = last.get('call_id', '') if crashed or not_started else last.get('invocation_id', '')
    if (not crashed and not not_started and last.get('kind') != 'invocation_interrupted'
            or not call_id.startswith(session_id + '/model/')):
        return None
    rows = [r.event() for r in observations.snapshot().records
            if r.event().get('call_id') == call_id and r.event().get('purpose') == 'model']
    observed = rows[-1] if rows else {}
    transports = observed.get('transport_calls', [])
    cleanup = bool(transports) and all(t.get('cleanup_verified') is True for t in transports)
    dead_http_owner = (not crashed and not not_started
                       and observed.get('error') == 'HTTP_CLEANUP_UNCONFIRMED'
                       and _http_transport(transports)
                       and _pending_owner_dead(records, call_id, invocations, custody_lock_held, transports))
    cleanup = cleanup or dead_http_owner
    eligible = (crashed or cleanup and observed.get('error') in RECOVERABLE_ERRORS
                or dead_http_owner
                or not_started and observed.get('status') == 'not_started')
    calls = sum(r.event()['kind'] in {'model_requested', 'context_summary_requested'} for r in records)
    remaining = None if max_model_calls is None else max(0, max_model_calls - calls)
    reason = ('LOCAL_CLEANUP_UNCONFIRMED' if not cleanup and not crashed and not not_started else
              'UNSUPPORTED_INTERRUPTION' if not eligible else
              'MODEL_CALL_LIMIT' if remaining == 0 else
              'DEADLINE_EXHAUSTED' if deadline_utc is not None and time.time() >= deadline_utc else
              'EXPLICIT_RESUME_AVAILABLE')
    return {'call_id': call_id, 'transport_eligible': eligible,
            'not_started': not_started,
            'owner_interrupted': bool(crashed),
            'local_cleanup_verified': cleanup, 'available': reason == 'EXPLICIT_RESUME_AVAILABLE',
            'reason': reason, 'remaining_model_calls': remaining, 'deadline_utc': deadline_utc,
            'remote_stop': 'not_dispatched' if not_started else 'unknown',
            'usage': 'none' if not_started else 'unknown'}
