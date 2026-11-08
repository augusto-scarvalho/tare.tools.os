"""A fresh user correction is steering, not permission to replay a stopped turn."""
from .native_model_recovery import inspect


def prepare_correction(session, control, root, journal, observations, state, invocation_id, *,
                       owner_interrupted=False, custody_lock_held=False):
    """Reconcile an interrupted model and insert older queued user input in order.

    Tool recovery remains with its owner before this point. Unknown effects,
    unconfirmed process cleanup (unless this HTTP request's local owner is proven
    dead, or a completed legacy invocation is protected by custody_lock) and
    exhausted limits cannot be bypassed here.
    No inference or project action is performed while admitting these messages.
    """
    recovery = inspect(journal, observations, state['session_id'],
        max_model_calls=state['budget']['max_model_calls'], deadline_utc=state['deadline_utc'],
        owner_interrupted=owner_interrupted, invocations=state.get('invocations'),
        custody_lock_held=custody_lock_held)
    if recovery and recovery['available']:
        if recovery.get('not_started'):
            session.retry_not_started_model(control, call_id=recovery['call_id'], resolution_id=invocation_id)
        else:
            session.abandon_interrupted_model(control, call_id=recovery['call_id'], resolution_id=invocation_id,
                **({'owner_interrupted': True} if recovery['owner_interrupted'] else {}))
    if state.get('messages_enabled'):
        from tare_tools_agent_runtime import NativeMessage
        from .native_messages import receiver
        receive = receiver(root, state['session_id'])
        while True:
            consumed = tuple(r.event()['message_id'] for r in journal.snapshot().records
                             if r.event()['kind'] == 'user_message')
            message = receive(consumed)
            if message is None:
                break
            if not isinstance(message, NativeMessage):
                message = NativeMessage(*message)
            session.continue_with(control, message_id=message.message_id, text=message.text,
                                  attachments=message.attachments, interrupts_turn=True)
