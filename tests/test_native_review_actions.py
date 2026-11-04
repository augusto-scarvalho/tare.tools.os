"""Operator actions against real saved native sessions, without a model service."""
from contextlib import ExitStack
import json

import pytest

from compute_plane.native_journal import custody_lock
from compute_plane.native_review import digest, receipt_path
from relay import native_agent, native_review_actions as actions
from relay.native_review import review
from test_native_agent import setup, Client, run, steps


@pytest.fixture
def edited(setup):
    config, custody, root = setup
    before = b'# user note\r\nvalue = 1\r\n'
    (root/'a.py').write_bytes(before)
    calls = list(steps())
    calls[1][1]['sha256'] = digest(before)
    # Fixture verifier checks exact text without the user note; this slice does
    # not need to run it. A separate test below exercises actual verifier state.
    client = Client(steps=[*calls[:2], calls[-1]])
    run(config, custody, client)
    edit = review(custody, project=root)['edits'][0]
    return config, custody, root, edit['effect_id'], before, client


def test_view_and_undo_keep_original_bytes_and_journal(edited):
    _, custody, root, effect, before, client = edited
    journal = (custody/'journal.jsonl').read_bytes()
    calls = len(client.calls)
    viewed = actions.read_edit_file(custody, project=root, effect_id=effect)
    assert viewed['content'] == '# user note\r\nvalue = 2\r\n'
    result = actions.undo_edit(custody, project=root, effect_id=effect)
    assert result['status'] == 'UNDONE'
    assert (root/'a.py').read_bytes() == before
    assert (custody/'journal.jsonl').read_bytes() == journal
    assert len(client.calls) == calls
    assert review(custody, project=root)['edits'][0]['undo_status'] == 'UNDONE'
    with pytest.raises(ValueError, match='already requested'):
        actions.undo_edit(custody, project=root, effect_id=effect)
    assert (root/'a.py').read_bytes() == before


@pytest.mark.parametrize('raw', [b'newer user changes\r\n', None])
def test_newer_change_or_deletion_refuses_without_intent(edited, raw):
    _, custody, root, effect, _, _ = edited
    if raw is None:
        (root/'a.py').unlink()
    else:
        (root/'a.py').write_bytes(raw)
    with pytest.raises(ValueError, match='changed'):
        actions.undo_edit(custody, project=root, effect_id=effect)
    assert ((root/'a.py').read_bytes() if (root/'a.py').exists() else None) == raw
    assert not actions.undo_path(custody, effect).exists()


@pytest.mark.parametrize('case', ['project', 'scope', 'capability', 'role', 'turn', 'snapshot', 'receipt', 'journal', 'session'])
def test_invalid_authority_or_evidence_never_changes_file(edited, case):
    _, custody, root, effect, _, _ = edited
    original = (root/'a.py').read_bytes()
    kwargs = {'project': root, 'effect_id': effect}
    state = json.loads((custody/'native-agent.json').read_text())
    if case == 'project': kwargs['project'] = root.parent
    if case == 'session': state['session_id'] = 'another-conversation'
    if case == 'turn': kwargs['invocation_id'] = 'not-the-recorded-turn'
    if case == 'scope': state['config']['writable'] = {'repo': ['elsewhere']}
    if case == 'capability': state['config']['capabilities'] = ['read']
    if case == 'role':
        state['config'].update(role='explorer', writable={}, verifiers={})
    (custody/'native-agent.json').write_text(json.dumps(state))
    receipt = receipt_path(custody/'review', effect)
    if case == 'receipt': receipt.write_text('{}')
    if case == 'snapshot':
        sha = json.loads(receipt.read_text())['before']
        (custody/'review'/'blobs'/sha).write_bytes(b'corrupt')
    if case == 'journal':
        with (custody/'journal.jsonl').open('ab') as stream: stream.write(b'{broken')
    with pytest.raises((ValueError, KeyError)):
        actions.undo_edit(custody, **kwargs)
    assert (root/'a.py').read_bytes() == original
    assert not actions.undo_path(custody, effect).exists()


@pytest.mark.parametrize('lock', ['session', 'workspace'])
def test_active_writer_refuses(edited, lock):
    _, custody, root, effect, _, _ = edited
    original = (root/'a.py').read_bytes()
    with ExitStack() as stack:
        if lock == 'session': stack.enter_context(custody_lock(custody))
        else: native_agent.workspace_locks(stack, {'repo': str(root)})
        with pytest.raises(ValueError, match='active_writer'):
            actions.undo_edit(custody, project=root, effect_id=effect)
    assert (root/'a.py').read_bytes() == original


def test_interrupted_completion_retains_uncertain_receipt_and_never_repeats(edited, monkeypatch):
    _, custody, root, effect, before, _ = edited
    original = actions.save_json
    def fail_result(path, value):
        if value['status'] == 'UNDONE': raise OSError('completion storage failed')
        original(path, value)
    monkeypatch.setattr(actions, 'save_json', fail_result)
    with pytest.raises(OSError, match='storage failed'):
        actions.undo_edit(custody, project=root, effect_id=effect)
    assert (root/'a.py').read_bytes() == before
    assert review(custody, project=root)['edits'][0]['undo_status'] == 'UNKNOWN'
    with pytest.raises(ValueError, match='uncertain result'):
        actions.undo_edit(custody, project=root, effect_id=effect)


@pytest.mark.parametrize('original', [None, b''])
def test_new_file_is_removed_but_original_empty_file_survives(setup, original):
    config, custody, root = setup
    if original is None: (root/'a.py').unlink()
    else: (root/'a.py').write_bytes(original)
    run(config, custody, Client(steps=[
        ('edit', {'repository': 'repo', 'path': 'a.py', 'sha256': digest(original), 'search': '', 'replace': 'new\r\n'}),
        ('tare_deliver', {'summary': 'Created text'})]))
    effect = review(custody)['edits'][0]['effect_id']
    actions.undo_edit(custody, project=root, effect_id=effect)
    assert ((root/'a.py').read_bytes() if (root/'a.py').exists() else None) == original


def test_checks_become_stale_and_resume_requires_refresh(setup):
    config, custody, root = setup
    run(config, custody, Client(steps=steps()))
    saved = review(custody)
    assert saved['checks'][0]['freshness'] == 'OBSERVED_FILES_MATCH'
    actions.undo_edit(custody, project=root, effect_id=saved['edits'][0]['effect_id'], invocation_id=saved['turns'][0]['id'])
    assert review(custody)['checks'][0]['freshness'] == 'STALE'
    client = Client(steps=[])
    with pytest.raises(ValueError, match='native_resume_source_changed'):
        native_agent.run_session(None, None, custody, resume=True, client=client)
    assert client.calls == []
    fresh = Client(steps=[('read', {'repository':'repo', 'path':'a.py'}),
                         ('verify', {'name':'unit'}),
                         ('tare_deliver', {'summary':'The restored value fails the new-value verifier.'})])
    native_agent.run_session(None, None, custody, resume=True, refresh=True,
                             client=fresh, local_counter=fresh, follow_up='Check the restored file', message_id='after-undo')
    checked = review(custody)['checks']
    assert checked[0]['freshness'] == 'STALE'
    assert checked[-1]['returncode'] == 1
    assert checked[-1]['freshness'] == 'OBSERVED_FILES_MATCH'


def test_current_view_does_not_show_old_snapshot_and_bounds_size(edited):
    _, custody, root, effect, _, _ = edited
    (root/'a.py').write_bytes(b'current file\n')
    assert actions.read_edit_file(custody, project=root, effect_id=effect)['content'] == 'current file\n'
    (root/'a.py').write_bytes(b'x' * (256*1024+1))
    with pytest.raises(ValueError, match='preview limit'):
        actions.read_edit_file(custody, project=root, effect_id=effect)
