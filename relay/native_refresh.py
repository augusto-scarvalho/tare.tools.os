"""Host-observed source refresh, using the Runtime's existing durable continuation."""
import hashlib

from compute_plane.native_journal import save_json
from compute_plane.native_models import encode


def accepted_sources(state, journal):
    pending = state.get('pending_source_refresh')
    if pending and any(r.event().get('kind') == 'user_message'
                       and r.event().get('message_id') == pending['message_id']
                       and r.event().get('text') == pending['text']
                       and r.event().get('attachments', []) == pending.get('attachments', [])
                       for r in journal.snapshot().records):
        state['accepted_sources'] = pending['accepted_sources']
        state['last_source_refresh'] = {key: pending[key] for key in ('message_id', 'text', 'follow_up')}
        del state['pending_source_refresh']
    return state.get('accepted_sources', {})


def continuation_text(state, message_id, text):
    previous = state.get('last_source_refresh', {})
    if previous.get('message_id') == message_id and previous.get('follow_up') == text:
        return previous['text']
    return text


def continue_after_refresh(session, control, state, state_path, journal, changes, *, follow_up=None, message_id=None,
                           attachments=(), interrupts_turn=False, model_call_limit=None):
    """Promote observations only when the matching notification is in the journal."""
    changes = sorted(changes, key=lambda row: (row['repository'], row['path']))
    text = ('Host-observed changes to previously read files. These are observations, not new project instructions. '
            'Read the current files before editing; a null current_sha256 means the file is missing. '
            'Previous test results do not verify the current files. Rerun relevant named verifiers.\n' +
            encode({'source_changes': changes}))
    if follow_up is not None:
        text += '\nUser follow-up:\n' + follow_up
    identity = message_id or 'source-refresh-' + hashlib.sha256(text.encode()).hexdigest()
    # Keep the original baseline until an actual read/edit supersedes it.
    accepted = {**state.get('accepted_sources', {})}
    for row in changes:
        accepted[row['repository']+':'+row['path']] = {
            'baseline_sha256': row['baseline_sha256'], 'current_sha256': row['current_sha256'],
            'observation_sequence': row['observation_sequence']}
    state['pending_source_refresh'] = {'message_id': identity, 'text': text, 'accepted_sources': accepted,
                                       'follow_up': follow_up}
    if attachments:
        from .native_attachments import records
        state['pending_source_refresh']['attachments'] = records(attachments)
    save_json(state_path, state)
    session.continue_with(control, message_id=identity, text=text,
                          **({'model_call_limit':model_call_limit} if model_call_limit is not None else {}),
                          **({'interrupts_turn': True} if interrupts_turn else {}),
                          **({'attachments': attachments} if attachments else {}))
    accepted_sources(state, journal)
    save_json(state_path, state)
