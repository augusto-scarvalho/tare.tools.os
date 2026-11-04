"""Observe an interrupted direct invocation; never dispatch, repair or grant approval."""
import hashlib
from pathlib import Path

from compute_plane.native_journal import custody_lock, regular
from .native_agent import read_json


def pending(value):
    from .console_mutation import OPERATIONS
    request = None
    for index, event in enumerate(value['events']):
        if event['kind'] == 'execution_requested' or event['kind'] in OPERATIONS:
            request = (index, event)
        elif event['kind'] == 'execution_observed' or event['kind'] in OPERATIONS.values():
            request = None
        elif (event['kind'] in {'session_reconciled', 'flow_reconciled', 'mutation_reconciled'} and request
                and event['detail']['request_event'] == request[0]):
            request = None
    # Older uncertain requests stay in history; they cannot replace a newer result.
    return request


def observe(value):
    request = pending(value)
    if request is None:
        return None
    index, event = request
    from .console_mutation import OPERATIONS, observe as observe_mutation
    if event['kind'] in OPERATIONS:
        return observe_mutation(value, index, event)
    selected = event['selection']
    identity = (event.get('detail') or {}).get('invocation_id')
    field = 'batch' if selected['mode'] == 'parallel' else selected['mode']
    observation = {'request_event': index, 'invocation_id': identity,
                   'state': 'UNKNOWN', 'session': selected.get('session'), 'result': None,
                   'mode': selected['mode'], 'owner': selected.get(field), 'owner_field': field}
    if selected['mode'] not in {'session', 'plan', 'work', 'parallel', 'research', 'sequence', 'synthesis'} or not identity:
        return {**observation, 'reason': 'This request has no qualified invocation binding. Review its owner receipts.'}
    try:
        if selected['mode'] != 'session':
            from .flow_recovery import observe as owner_observation
            result, receipts = owner_observation(selected, value, identity)
            return {**observation, 'state': 'RECOVERED', 'result': result, 'receipts': receipts,
                'reason': 'Recovered this '+selected['mode']+' attempt: '+result['status']+
                    '. Historical owner receipt; no new approval or whole-demand completion is inferred.'}
        root = Path(value['root']).resolve()
        directory = regular(selected['session'])
        if (not Path(selected['session']).is_absolute() or directory.resolve().is_relative_to(root)
                or Path(selected['root']).resolve() != root):
            raise ValueError('Session custody/project binding is invalid.')
        # Observation must not create custody or its writer lock.
        if not regular(directory/'writer.lock').is_file():
            raise ValueError('No session writer receipt exists yet.')
        with custody_lock(directory):
            state = read_json(directory/'native-agent.json', 1024*1024)
            if (state.get('schema') not in {'tare.tools/native-agent/1', 'tare.tools/native-agent/2'}
                    or root not in [Path(p).resolve() for p in state['config']['repositories'].values()]):
                raise ValueError('Session belongs to another project or has an unsupported schema.')
            rows = [r for r in state['invocations'] if r.get('id') == identity]
            if len(rows) != 1:
                raise ValueError('The requested invocation is not uniquely recorded.')
            invocation = rows[0]
            if invocation.get('ended_at') is None:
                from compute_plane.native_journal import NativeJournal
                from compute_plane.native_effect_recovery import pending as pending_effect
                journal = NativeJournal(directory/'journal.jsonl')
                records = [r.event() for r in journal.snapshot().records]
                completed = sum(e['kind'] == 'effect_returned' and e['result']['status'] != 'unknown'
                                for e in records)
                effect = pending_effect(journal, state['session_id'])
                detail = ('Pending operation: '+effect.tool_call.name+'. ' if effect else '')
                return {**observation, 'reason': f'Execution interrupted; writer released. {completed} tool results saved. '
                    + detail + 'Resume reconciles saved effects and continues under the original limits; '
                    'unconfirmed changes are not replayed.'}
            result = read_json(directory/'outcome.json', 4*1024*1024)
            if (result.get('invocation_id') != identity or result.get('session_id') != state['session_id']
                    or Path(result['session']).resolve() != directory.resolve()
                    or result.get('status') != invocation['status'] or invocation.get('error_type')):
                raise ValueError('The saved outcome does not attest this completed invocation. Review its journal.')
            receipts = [{'path': str(directory/name), 'sha256': hashlib.sha256((directory/name).read_bytes()).hexdigest()}
                        for name in ('native-agent.json', 'outcome.json')]
            return {**observation, 'state': 'RECOVERED', 'result': result, 'receipts': receipts,
                    'reason': 'Recovered the exact invocation outcome. This is not Work settlement or a new approval.'}
    except Exception as exc:
        if str(exc) == 'native_custody_has_active_writer':
            return {**observation, 'state': 'BUSY',
                    'reason': 'Owner has an active writer; completion remains unconfirmed. Check again after it releases custody.'}
        return {**observation, 'reason': str(exc)}
