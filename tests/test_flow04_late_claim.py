"""A delayed proposal cannot borrow the authority of a live successor.

Use the native Work path, real Git candidates and the real coordinator. The
three races distinguish the host check from the database's atomic claim check.
"""
import json

import pytest

from test_native_settlement import prepared
from test_work_run import _git, os_root


@pytest.mark.parametrize('arrival', ['before_candidate', 'after_candidate', 'during_claim'])
def test_late_native_proposal_after_handoff_cannot_accept_or_land(
        os_root, tmp_path, monkeypatch, arrival):
    from compute_plane import native_journal
    from relay import candidate_change

    runner, args = prepared(os_root, tmp_path)
    gate = runner.authority_gate
    old = runner.settings['control'].binding
    successor = []

    def transfer():
        successor.append(gate.handoff(old, 'successor', ttl_s=120))
        assert gate.is_current(successor[0]) and not gate.is_current(old)

    if arrival == 'during_claim':
        submit = gate.submit_terminal_claim

        def delayed_claim(claim):
            # Transfer after the host's last check, before the atomic DB check.
            transfer()
            assert not submit(claim)
            assert gate.accepted_claim(claim) is None
            return False

        monkeypatch.setattr(gate, 'submit_terminal_claim', delayed_claim)
    else:
        materialize = candidate_change.materialize_verify_and_record_candidate

        def delayed_candidate(*a, **kw):
            if arrival == 'before_candidate':
                transfer()
            candidate = materialize(*a, **kw)
            if arrival == 'after_candidate':
                transfer()
            return candidate

        monkeypatch.setattr(candidate_change, 'materialize_verify_and_record_candidate', delayed_candidate)

    reason = 'CLAIM_REFUSED' if arrival == 'during_claim' else 'CONTROL_STOPPED'
    with pytest.raises(ValueError, match=reason):
        runner.run(**args)

    custody = runner.settings['custody']
    before = json.loads((custody/'attempt.json').read_bytes())
    journal_before = (custody/'journal.jsonl').read_bytes()
    events = [r.event() for r in native_journal.NativeJournal(custody/'journal.jsonl').snapshot().records]
    assert len(runner.settings['client'].generations) == 5
    assert sum(e['kind'] == 'effect_requested' and e['tool_call']['name'] == 'edit' for e in events) == 1
    assert json.loads((args['train_dir']/'NATIVE_PROPOSAL.json').read_bytes())['binding']['binding_id'] == old.binding_id
    assert not (args['train_dir']/'NATIVE_ACCEPTANCE.json').exists()
    assert _git('rev-parse', 'HEAD', cwd=args['lease'].repository_root) == args['descriptor'].base_commit

    # Recovery of the predecessor cannot create another call, edit or admission.
    with pytest.raises(ValueError, match='CONTROL_STOPPED'):
        runner.run(**args, resume=True)
    assert len(runner.settings['client'].generations) == 5
    after = json.loads((custody/'attempt.json').read_bytes())
    for name in ('identity', 'deadline_utc', 'proposal'):
        assert after[name] == before[name]
    assert (custody/'journal.jsonl').read_bytes() == journal_before
    assert len(after['attempts']) == len(before['attempts']) + 1
    assert gate.is_current(successor[0])
