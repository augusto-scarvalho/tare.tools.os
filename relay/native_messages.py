"""Durable operator inbox, separate from the single-writer execution journal."""
import argparse
import json
from pathlib import Path
from uuid import uuid4

from compute_plane.native_journal import NativeJournal, custody_lock, regular, save_json
from .native_agent import read_json


def state_for(directory, project=None):
    directory = regular(Path(directory).resolve())
    state = read_json(directory/'native-agent.json', 1024*1024)
    if state.get('messages_enabled') is not True:
        raise ValueError('This session does not accept queued messages. Use a new direct session.')
    if project is not None and Path(project).resolve() not in [
            Path(p).resolve() for p in state['config']['repositories'].values()]:
        raise ValueError('The session belongs to another project.')
    return directory, state


def read_queue(directory, session_id):
    path = regular(directory/'messages'/'queue.json')
    queue = read_json(path, 4*1024*1024) if path.exists() else {'session_id': session_id, 'items': []}
    if queue.get('session_id') != session_id or not isinstance(queue.get('items'), list):
        raise ValueError('Message queue identity changed.')
    for message in queue['items']:
        if isinstance(message, dict) and 'attachments' in message:
            from .native_attachments import references
            if not references(message['attachments']):
                raise ValueError('Empty attachment metadata is not a canonical queued message.')
    return queue


def enqueue(directory, text, *, message_id=None, project=None, attachments=(), attachment_root=None):
    directory, state = state_for(directory, project)
    message_id = uuid4().hex if message_id is None else message_id
    if (not isinstance(text, str) or not text.strip() and not attachments or len(text.encode()) > 16384
            or not isinstance(message_id, str) or not message_id.strip() or len(message_id.encode()) > 128):
        raise ValueError('Supply a nonempty message (at most 16384 UTF-8 bytes) and a valid message ID.')
    if not isinstance(attachments, tuple):
        raise ValueError('Attachments must be an immutable tuple.')
    incoming = {'id': message_id, 'text': text}
    if attachments:
        if not state.get('attachments_enabled'):
            raise ValueError('This session does not accept attachments. Start a new conversation.')
        if any(directory.is_relative_to(Path(p).resolve()) for p in state['config']['repositories'].values()):
            raise ValueError('Attachment custody must be outside the project.')
        from tare_tools_agent_runtime import NativeMessage
        from .native_attachments import records
        NativeMessage(message_id, text, attachments)
        incoming['attachments'] = records(attachments)
    inbox = regular(directory/'messages'); inbox.mkdir(exist_ok=True)
    with custody_lock(inbox):
        if read_json(directory/'native-agent.json', 1024*1024).get('continued_to'):
            raise ValueError('This conversation changed provider. Send the message to its successor.')
        for record in journal_records(directory):
            event = record.event()
            if (event['kind'] == 'user_message' and event['message_id'] == message_id
                    and _content(event) != _content(incoming)):
                raise ValueError('Message ID already has different text or attachments.')
        queue = read_queue(directory, state['session_id'])
        existing = next((m for m in queue['items'] if m['id'] == message_id), None)
        if existing is not None:
            if _content(existing) != _content(incoming):
                raise ValueError('Message ID already has different text or attachments.')
        else:
            queue['items'].append(incoming)
            if len(json.dumps(queue, ensure_ascii=False).encode()) > 4*1024*1024:
                raise ValueError('Session inbox is full; no message was saved.')
            if attachments:
                from .native_attachments import prepare
                prepare(directory, attachments, source_root=attachment_root)
            save_json(inbox/'queue.json', queue)
    return {'message_id': message_id, 'status': 'SAVED',
            'delivery': 'Next complete round while running, or on Resume. Not yet a processing acknowledgement.'}


def receiver(directory, session_id):
    def receive(consumed):
        queue = read_queue(directory, session_id)
        for message in queue['items']:
            if message['id'] not in consumed:
                if message.get('attachments'):
                    from tare_tools_agent_runtime import NativeMessage
                    from .native_attachments import references
                    return NativeMessage(message['id'], message['text'], references(message['attachments']))
                return message['id'], message['text']
        return None
    return receive


def journal_records(directory):
    journal = regular(directory/'journal.jsonl')
    # NativeJournal requires whole records; an incomplete read is unavailable,
    # never an acknowledgement inferred from process liveness or stdout.
    return NativeJournal(journal).snapshot().records if journal.exists() else ()


def _content(message):
    return message['text'], message.get('attachments', [])


def inspect(directory, project=None):
    directory, state = state_for(directory, project)
    session_id = state['session_id']
    queue = read_queue(directory, session_id)
    records = journal_records(directory)

    if records:
        first = records[0].event()
        if first.get('kind') != 'session_started' or first.get('session_id') != session_id:
            raise ValueError('Journal session identity mismatch.')

    queue_items = queue['items']
    queue_by_id = {}
    for m in queue_items:
        if not isinstance(m, dict) or 'id' not in m:
            continue
        mid = m['id']
        if mid in queue_by_id:
            raise ValueError('Duplicate message ID in queue.')
        queue_by_id[mid] = m

    delivered = {}  # mid -> (content, sequence)
    deliveries = {}  # mid -> delivery dict
    unanswered_mids = []  # received mids awaiting completion
    model_calls = 0
    pending_request = None  # (req_seq, call_id)
    pending_return = None   # (req_seq, ret_seq, call_id, expected_output)

    for i, record in enumerate(records):
        event = record.event()
        kind = event.get('kind')
        seq = record.sequence

        if kind == 'user_message':
            if i == 0:
                raise ValueError('user_message cannot be first journal record.')
            pred_seq = records[i - 1].sequence
            after_seq = event.get('after_sequence')
            if after_seq is None or after_seq != pred_seq:
                raise ValueError('Invalid user message after_sequence.')

            mid = event.get('message_id')
            if not isinstance(mid, str) or not mid.strip():
                raise ValueError('Invalid user message ID.')
            if mid in delivered:
                raise ValueError(f'Duplicate user message ID in journal: {mid}')

            content = _content(event)
            if mid in queue_by_id and _content(queue_by_id[mid]) != content:
                raise ValueError('Message ID already has different text or attachments; delivery is not confirmed.')

            delivered[mid] = (content, seq)
            deliveries[mid] = {
                'status': 'RECEIVED',
                'receipt_sequence': seq,
                'call_id': None,
                'request_sequence': None,
                'response_sequence': None,
                'terminal_sequence': None,
            }
            unanswered_mids.append(mid)
            pending_request = None
            pending_return = None

        elif kind == 'model_requested':
            call_id = event.get('call_id')
            expected_call_id = f'{session_id}/model/{model_calls + 1}'
            if pending_request is not None:
                pending_request = None
            elif call_id == expected_call_id:
                model_calls += 1
                pending_request = (seq, call_id)
            else:
                pending_request = None
            pending_return = None

        elif kind in {'model_failed', 'invocation_interrupted', 'invocation_not_started', 'model_abandoned'}:
            pending_request = None
            pending_return = None

        elif kind == 'model_returned':
            call_id = event.get('call_id')
            reply = event.get('reply')
            if (pending_request is not None and call_id == pending_request[1]
                    and seq == pending_request[0] + 1):
                req_seq, _ = pending_request
                pending_request = None
                if (isinstance(reply, dict)
                        and 'complete' in reply and type(reply['complete']) is bool and reply['complete'] is True
                        and 'proposes_delivery' in reply and type(reply['proposes_delivery']) is bool and reply['proposes_delivery'] is True
                        and 'tool_calls' in reply and isinstance(reply['tool_calls'], (tuple, list)) and len(reply['tool_calls']) == 0
                        and isinstance(reply.get('text'), str) and bool(reply['text'].strip())):
                    pending_return = (req_seq, seq, call_id, reply['text'])
                else:
                    pending_return = None
            else:
                pending_request = None
                pending_return = None

        elif kind == 'terminal':
            status = event.get('status')
            output = event.get('output')
            if pending_return is not None:
                req_seq, ret_seq, call_id, expected_output = pending_return
                pending_return = None
                if status == 'proposed' and output == expected_output and seq == ret_seq + 1:
                    term_seq = seq
                    for mid in unanswered_mids:
                        deliveries[mid] = {
                            'status': 'RESPONSE_READY',
                            'receipt_sequence': delivered[mid][1],
                            'call_id': call_id,
                            'request_sequence': req_seq,
                            'response_sequence': ret_seq,
                            'terminal_sequence': term_seq,
                        }
                    unanswered_mids.clear()
            pending_request = None

        else:
            if kind not in {'context_counted', 'context_activated', 'context_summary_requested',
                            'context_summary_returned', 'context_summary_failed', 'context_summary_interrupted'}:
                pending_return = None

    if any(m['id'] in delivered and delivered[m['id']][0] != _content(m) for m in queue_items if isinstance(m, dict) and 'id' in m):
        raise ValueError('Message ID already has different text or attachments; delivery is not confirmed.')

    items = []
    for m in queue_items:
        mid = m['id']
        if mid in deliveries:
            delivery = deliveries[mid]
            status = 'IN_CONTEXT'
            sequence = delivered[mid][1]
        else:
            delivery = {
                'status': 'SAVED',
                'receipt_sequence': None,
                'call_id': None,
                'request_sequence': None,
                'response_sequence': None,
                'terminal_sequence': None,
            }
            status = 'QUEUED'
            sequence = None
        item = {
            'message_id': mid,
            'text': m['text'],
            **({'attachments': m['attachments']} if m.get('attachments') else {}),
            'status': status,
            'sequence': sequence,
            'delivery': delivery,
        }
        items.append(item)
    return {'items': items}



def main(argv=None):
    from tare_tools_agent_runtime import ModelFailure
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session', required=True, type=Path)
    parser.add_argument('--root', type=Path)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--text'); action.add_argument('--show', action='store_true')
    parser.add_argument('--message-id')
    parser.add_argument('--attachment-input', type=Path, help='Read immutable snapshot references for this message')
    args = parser.parse_args(argv)
    try:
        refs, source_root = (), None
        if args.attachment_input:
            if args.show:
                raise ValueError('Attachment input requires --text, which may be empty.')
            from .native_attachments import read_input
            refs, source_root = read_input(args.attachment_input)
        value = inspect(args.session, args.root) if args.show else enqueue(
            args.session, args.text, message_id=args.message_id, project=args.root,
            attachments=refs, attachment_root=source_root)
        print(json.dumps(value, ensure_ascii=True)); return 0
    except (ValueError, OSError, KeyError, TypeError, ModelFailure) as exc:
        print(json.dumps({'status': 'REFUSED', 'reason': str(exc)})); return 2
