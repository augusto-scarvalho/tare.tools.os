"""Advisory model-response recovery from recorded evidence; never tool recovery."""
import time


RECOVERABLE_ERRORS = frozenset({
    'HTTP_CANCELLED', 'HTTP_DEADLINE', 'HTTP_INCOMPLETE_EXCHANGE',
    'HTTP_INCOMPLETE_STREAM', 'HTTP_OUTPUT_LIMIT', 'CLI_INCOMPLETE_EXCHANGE',
})

CLI_PREFLIGHT_ERRORS = frozenset({'CLI_VERSION_CHANGED', 'CLI_VERSION_UNAVAILABLE',
                                 'CLI_INCOMPATIBLE_VERSION', 'CLI_CAPABILITY_UNAVAILABLE',
                                 'CLI_USER_INSTRUCTIONS_PRESENT'})


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


def inspect(journal, observations, session_id, *, max_model_calls, deadline_utc, owner_interrupted=False):
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
    eligible = (crashed or cleanup and observed.get('error') in RECOVERABLE_ERRORS
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
