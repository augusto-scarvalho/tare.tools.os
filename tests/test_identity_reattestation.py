"""relay_mesh reattest: an explicit operator path for a strict train refused at land only because the roster no longer
matches a saved identity (IDENTITY_ROSTER_STALE / IDENTITY_ROSTER_MISMATCH). It re-attests the implementer, archives
the verdict and audit (never reused) and sends the same candidate back to a new audit, which a native Work runs on
`tare run <work> --resume --reattest`. Offline fixtures, no model."""
import hashlib
import json
from types import SimpleNamespace

import pytest

from relay import landing_policy, relay_io, relay_mesh, work_run
from relay.work_audit import audit_custody
from relay.landing_policy import (IDENTITY_SCHEMA, LandingPolicy, _identity_from_payload, attest_identity,
                                  evaluate_admission, read_identity, verify_identity_against_roster, write_evidence,
                                  write_identity, write_verdict)
from test_landing_policy import ROSTER, evidence
from test_work_audit import reviewers  # noqa: F401  (fixtures)
from test_work_resume import host, run  # noqa: F401
from test_work_run import os_root  # noqa: F401

TRAIN = 'TRAIN-SMART-ONE'  # the train test_landing_policy.evidence binds


@pytest.fixture
def mesh(tmp_path, monkeypatch):
    original = relay_mesh.RELAY_ROOT
    relay_mesh.rebind_root(tmp_path)
    relay_mesh.ensure_dirs()
    (tmp_path/'relay'/'round_table_roster.json').write_bytes(ROSTER.read_bytes())
    # The candidate's Git checks are not under test: the descriptor and candidate the evidence binds.
    monkeypatch.setattr(relay_mesh, '_strict_candidate_context',
                        lambda d, s: (SimpleNamespace(sha256='1'*64), SimpleNamespace(sha256='2'*64), '', {}))
    monkeypatch.setattr(relay_mesh, 'audit_valid', lambda *a: (True, 'fixture'))
    yield tmp_path/'relay'
    relay_mesh.rebind_root(original)


def attested(mesh, role, model, key, runtime):
    return attest_identity(roster_path=mesh/'round_table_roster.json', agent_id=role+'-one', role=role,
                           model_id=model, runtime_key=key, runtime_model=runtime)


def local_before_262(mesh):
    """What #258 saved for a local qwen38-ninfer: vendor Local, its own id as bias domain. #262 names it Qwen."""
    return _identity_from_payload({
        'schema': IDENTITY_SCHEMA, 'agent_id': 'tare-run:local', 'role': 'implementer', 'model_id': 'qwen38-ninfer',
        'runtime_key': 'native', 'runtime_model': 'qwen38-ninfer', 'vendor': 'Local', 'bias_domain': 'qwen38-ninfer',
        'roster_sha256': hashlib.sha256((mesh/'round_table_roster.json').read_bytes()).hexdigest(), 'line': 'local'})


def strict_train(mesh, implementer=None, status='READY_FOR_LANDING', strict=True):
    """A strict train admitted with an independent audit, as the Work saved it."""
    implementer = implementer or attested(mesh, 'implementer', 'gpt-5.6-sol', 'codex', 'gpt-5.6-sol')
    auditor = attested(mesh, 'auditor', 'gemini-3.7', 'google-cli', 'gemini-3.7-flash-high')
    train = mesh/'trains'/TRAIN; train.mkdir(parents=True)
    (train/'CANDIDATE_CHANGE.json').write_text('{"candidate": "fixture"}', encoding='utf-8')
    (train/'AUDIT_REPORT.md').write_text('PASS\n', encoding='utf-8')
    verdict = evaluate_admission(evidence(), implementer, policy=LandingPolicy('required', True), auditor=auditor,
                                 audit_passed=True,
                                 audit_sha256=hashlib.sha256((train/'AUDIT_REPORT.md').read_bytes()).hexdigest())
    assert verdict.verdict == 'ADMITTED'
    write_identity(train/'IMPLEMENTER_IDENTITY.json', implementer)
    write_identity(train/'AUDITOR_IDENTITY.json', auditor)
    write_evidence(train/'LANDING_EVIDENCE.json', evidence())
    write_verdict(train/'ADMISSION_VERDICT.json', verdict)
    relay_io.write_json_atomic(train/'STATE.json', {
        'train_id': TRAIN, 'task_ids': ['work-one'], 'status': status, 'revision': 7, 'history': [],
        **({'execution_descriptor': {'sha256': '1'*64}} if strict else {})})
    return train


def edit_roster(mesh, change):
    path = mesh/'round_table_roster.json'
    value = json.loads(path.read_bytes())
    change(value)
    path.write_text(json.dumps(value, indent=1), encoding='utf-8')


def files(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()}


def reattested(mesh, train, code, capsys):
    before = files(train)
    assert relay_mesh.admission_valid(TRAIN) == (False, 'smart admission invalid: ' + code)
    assert relay_mesh.reattest_train(TRAIN) is True
    out = capsys.readouterr().out
    assert f'[REATTESTED] {TRAIN}: {code};' in out and 'READY_FOR_LANDING -> AUDIT_PENDING' in out
    state = json.loads((train/'STATE.json').read_bytes())
    assert state['status'] == 'AUDIT_PENDING' and state['revision'] == 8
    assert state['history'][-1]['role'] == 'operator' and code in state['history'][-1]['note']
    [folder] = [p for p in train.iterdir() if p.is_dir()]
    record = json.loads((folder/'REATTESTATION.json').read_bytes())
    archived = ['ADMISSION_VERDICT.json', 'AUDITOR_IDENTITY.json', 'AUDIT_REPORT.md', 'IMPLEMENTER_IDENTITY.json']
    assert sorted(record['archived']) == archived and record['refusal'] == code
    assert record['candidate_sha256'] == hashlib.sha256(before['CANDIDATE_CHANGE.json']).hexdigest()
    for name in archived:  # moved, byte for byte, never rewritten or deleted
        assert (folder/name).read_bytes() == before[name]
        assert record['archived'][name] == hashlib.sha256(before[name]).hexdigest()
    assert not any((train/name).exists() for name in archived[:3])
    # The candidate's evidence is unchanged; the implementer is attested again from today's roster.
    assert (train/'LANDING_EVIDENCE.json').read_bytes() == before['LANDING_EVIDENCE.json']
    new = read_identity(train/'IMPLEMENTER_IDENTITY.json')
    old = read_identity(folder/'IMPLEMENTER_IDENTITY.json')
    assert record['identities'] == {'implementer': {'old': old.sha256, 'new': new.sha256}} and new != old
    verify_identity_against_roster(new, mesh/'round_table_roster.json')
    assert {k: v for k, v in new.payload.items() if k not in ('vendor', 'bias_domain', 'roster_sha256')} == {
        k: v for k, v in old.payload.items() if k not in ('vendor', 'bias_domain', 'roster_sha256')}
    # Nothing lands until a new audit and verdict exist; the archived verdict is never read as the train's.
    assert relay_mesh.land_train(TRAIN, signoff='SMART_POLICY') is False
    assert relay_mesh.admission_valid(TRAIN) == (False, 'smart admission invalid: VERDICT_UNAVAILABLE')
    assert relay_mesh.reattest_train(TRAIN) is False  # no longer at READY_FOR_LANDING
    return new, old


def test_a_roster_edit_is_reattested_and_only_a_new_audit_and_verdict_admit_it(mesh, capsys):
    train = strict_train(mesh)
    edit_roster(mesh, lambda value: value.setdefault('notes', 'edited in a release'))
    new, old = reattested(mesh, train, 'IDENTITY_ROSTER_STALE', capsys)
    assert (new.vendor, new.bias_domain) == (old.vendor, old.bias_domain)
    assert new.payload['roster_sha256'] != old.payload['roster_sha256']
    # The existing audit owner writes a new audit; the existing admission computes a fresh verdict over it.
    write_identity(train/'AUDITOR_IDENTITY.json',
                   attested(mesh, 'auditor', 'gemini-3.7', 'google-cli', 'gemini-3.7-flash-high'))
    (train/'AUDIT_REPORT.md').write_text('PASS again\n', encoding='utf-8')
    verdict = relay_mesh.prepare_admission_verdict(TRAIN, audit_mode='required')
    assert verdict.verdict == 'ADMITTED' and verdict.payload['implementer_identity_sha256'] == new.sha256
    assert relay_mesh.admission_valid(TRAIN)[0] is True
    # The record binds exactly this pair, for this train and candidate.
    assert relay_mesh.reattested(train, old.sha256, new.sha256)
    assert not relay_mesh.reattested(train, new.sha256, old.sha256)
    (train/'CANDIDATE_CHANGE.json').write_text('{"candidate": "another"}', encoding='utf-8')
    assert not relay_mesh.reattested(train, old.sha256, new.sha256)


def test_a_local_model_saved_as_vendor_local_is_reattested_as_its_family(mesh, capsys):
    train = strict_train(mesh, implementer=local_before_262(mesh))
    new, old = reattested(mesh, train, 'IDENTITY_ROSTER_MISMATCH', capsys)
    assert (old.vendor, old.bias_domain) == ('Local', 'qwen38-ninfer')
    assert (new.vendor, new.bias_domain, new.payload['line']) == ('Qwen', 'qwen/qwen-family', 'local')
    assert new.payload['roster_sha256'] == old.payload['roster_sha256']  # the roster is the same; the derivation is not


@pytest.mark.parametrize('case', ['admitted', 'other_code', 'status', 'not_strict', 'roster_cannot_attest'])
def test_every_other_train_is_refused_and_unchanged(mesh, capsys, monkeypatch, case):
    train = strict_train(mesh, status='AUDIT_PENDING' if case == 'status' else 'READY_FOR_LANDING',
                         strict=case != 'not_strict')
    if case == 'other_code':
        (train/'AUDIT_REPORT.md').write_text('another report\n', encoding='utf-8')
    elif case == 'roster_cannot_attest':  # the roster changed, and no longer runs the model by that alias
        edit_roster(mesh, lambda value: value['models']['gpt-5.6-sol']['runtime'].update(codex='gpt-5.6-renamed'))
    before = files(train)
    monkeypatch.setattr('sys.argv', ['relay_mesh.py', 'reattest', '--train', TRAIN])
    with pytest.raises(SystemExit) as refused:
        relay_mesh.main()
    assert refused.value.code == 1 and files(train) == before
    assert {
        'admitted': 'admission of \'TRAIN-SMART-ONE\' is ADMITTED, not IDENTITY_ROSTER_MISMATCH or IDENTITY_ROSTER_STALE',
        'other_code': 'is VERDICT_RECOMPUTE_MISMATCH, not',
        'status': "is 'AUDIT_PENDING', not READY_FOR_LANDING",
        'not_strict': 'is not a strict train',
        'roster_cannot_attest': 'cannot attest the implementer of \'TRAIN-SMART-ONE\': IDENTITY_RUNTIME_ALIAS_MISMATCH',
    }[case] in capsys.readouterr().out


def test_a_refused_transition_leaves_the_train_and_its_audit_custody_exactly_as_they_were(mesh, capsys, monkeypatch):
    train = strict_train(mesh, implementer=local_before_262(mesh))
    run_custody = mesh.parent/'run'
    relay_io.write_json_atomic(train/'WORK_RUN.json', {'implementation_custody': str(run_custody/'implement'),
                                                       'audit': None})
    (run_custody/'code-audit'/'0').mkdir(parents=True)
    (run_custody/'code-audit'/'0'/'audit-result.json').write_text('{"status": "PROPOSED"}', encoding='utf-8')
    before, custody = files(train), files(run_custody)
    monkeypatch.setattr(relay_mesh, 'transition_train', lambda *a, **k: False)  # a concurrent change won
    assert relay_mesh.reattest_train(TRAIN) is False
    assert "changed meanwhile; nothing was changed" in capsys.readouterr().out
    assert files(train) == before and files(run_custody) == custody
    assert not [p for p in train.iterdir() if p.is_dir()]


def local_implementer(host):
    """The fixture Work's implementer as a local qwen38-ninfer the roster does not know (a line-derived identity)."""
    profile = json.loads(host[2].read_bytes())
    profile['model'] = profile['native_profile']['model'] = 'qwen38-ninfer'
    profile['native_profile']['endpoint'] = profile['native_counter_endpoint'] = host[3].endpoint = 'http://127.0.0.1:9/v1'
    host[2].write_text(json.dumps(profile), encoding='utf-8')


@pytest.mark.parametrize('refusal,record', [('MISMATCH', 'kept'), ('MISMATCH', 'missing'),
                                            ('MISMATCH', 'other_digest'), ('STALE', 'kept')])
def test_a_native_work_refused_at_land_is_reattested_and_resumed_to_a_new_audit_and_verdict(
        host, reviewers, monkeypatch, capsys, refusal, record):
    import tare_ops
    profiles, calls, _, _ = reviewers
    if refusal == 'MISMATCH':
        local_implementer(host)
    # MISMATCH: saved before #262 (vendor Local). Either way the host stopped before landing.
    with monkeypatch.context() as before:
        if refusal == 'MISMATCH':
            before.setattr(landing_policy, '_local_row',
                           lambda model_id, models: {'vendor': 'Local', 'bias_domain': model_id, 'line': 'local'})
        def die(*a, **kw): raise SystemExit('host interrupted before landing')
        before.setattr(relay_mesh, 'land_train', die)
        with pytest.raises(SystemExit):
            run(host, '--auditor-profile', str(profiles[0]))
    capsys.readouterr()
    train, train_id = work_run.train_dir_for('work-one'), work_run.train_id_for('work-one')
    origin = json.loads((train/'WORK_RUN.json').read_bytes())
    old = read_identity(train/'IMPLEMENTER_IDENTITY.json')
    assert calls == ['one']
    if refusal == 'STALE':  # a release edits the roster: the implementer's and the audit route's receipts are stale
        roster = relay_mesh.RELAY_DIR/'round_table_roster.json'
        roster.write_bytes(roster.read_bytes() + b'\n')
    # Today the resume refuses, reusing the saved audit, and so does land.
    assert tare_ops.main(['run', 'work-one', '--resume']) == 1
    refused = json.loads(capsys.readouterr().out)
    assert (refused['status'] == 'LANDING_REFUSED' if refusal == 'MISMATCH' else
            'AUDITOR_IDENTITY_CHANGED' in refused['error']) and calls == ['one']
    assert relay_mesh.admission_valid(train_id) == (False, 'smart admission invalid: IDENTITY_ROSTER_' + refusal)
    assert relay_mesh.reattestable(train_id) == ('IDENTITY_ROSTER_' + refusal, '')  # another family's auditor: worth a new audit

    custody = audit_custody(origin)
    saved_result = files(custody)
    assert any(name.endswith('audit-result.json') for name in saved_result)
    aside = custody.with_name(custody.name + '.r' + str(json.loads((train/'STATE.json').read_bytes())['revision']))
    candidate = json.loads((train/'CANDIDATE_CHANGE.json').read_bytes())['candidate_commit']

    if record != 'kept':  # the relay-mesh command re-attests; a record that does not bind the pair is refused as before
        assert relay_mesh.reattest_train(train_id) is True
        [folder] = train.glob('REATTESTED_*')
        if record == 'missing':
            (folder/'REATTESTATION.json').unlink()
        else:
            value = json.loads((folder/'REATTESTATION.json').read_bytes())
            value['identities']['implementer']['new'] = '0'*64
            relay_io.write_json_atomic(folder/'REATTESTATION.json', value)
        capsys.readouterr()
        assert tare_ops.main(['run', 'work-one', '--resume']) == 1
        result = json.loads(capsys.readouterr().out)
        assert 'NATIVE_CANDIDATE_PROPOSAL_CHANGED' in result['error'] and calls == ['one']  # before any new audit
        assert not (train/'ADMISSION_VERDICT.json').exists() and not custody.exists()
        return

    assert tare_ops.main(['run', 'work-one', '--resume', '--reattest']) == 0, capsys.readouterr()
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'SETTLED' and calls == ['one', 'one']  # a new audit of the same candidate
    assert result['candidate_commit'] == candidate and result['admission']['verdict'] == 'ADMITTED'
    assert json.loads((train/'STATE.json').read_bytes())['status'] == 'LANDED'
    [landed] = relay_mesh.HISTORY_DIR.glob(train_id + '_LANDED_*')  # the train as it landed
    [folder] = landed.glob('REATTESTED_*')
    record = json.loads((folder/'REATTESTATION.json').read_bytes())
    new = read_identity(landed/'IMPLEMENTER_IDENTITY.json')
    assert record['identities']['implementer'] == {'old': old.sha256, 'new': new.sha256}
    assert read_identity(folder/'IMPLEMENTER_IDENTITY.json') == old
    if refusal == 'MISMATCH':
        assert (old.vendor, old.bias_domain, new.vendor, new.bias_domain) == (
            'Local', 'qwen38-ninfer', 'Qwen', 'qwen/qwen-family')
        assert set(record['identities']) == {'implementer'}  # the auditor's receipt still matches
    else:
        assert (new.vendor, new.bias_domain) == (old.vendor, old.bias_domain) and new != old
        assert set(record['identities']) == {'implementer', 'auditor:one'}
        assert record['identities']['auditor:one']['old'] == origin['audit']['routes'][0]['identity_sha256']
    verdict = json.loads((landed/'ADMISSION_VERDICT.json').read_bytes())
    assert verdict['implementer_identity_sha256'] == new.sha256
    assert verdict != json.loads((folder/'ADMISSION_VERDICT.json').read_bytes())
    # The old audit sessions are set aside, never reused or deleted; the new audit has its own.
    assert record['audit_custody_archived'] == str(aside) and files(aside) == saved_result
    assert any(name.endswith('audit-result.json') for name in files(custody))


def test_reattest_needs_resume_and_a_work_whose_admission_passes_is_refused_unchanged(
        host, reviewers, monkeypatch, capsys):
    import tare_ops
    profiles, calls, _, _ = reviewers
    with monkeypatch.context() as before:
        def die(*a, **kw): raise SystemExit('host interrupted before landing')
        before.setattr(relay_mesh, 'land_train', die)
        with pytest.raises(SystemExit):
            run(host, '--auditor-profile', str(profiles[0]))
    capsys.readouterr()
    train = work_run.train_dir_for('work-one')
    custody = audit_custody(json.loads((train/'WORK_RUN.json').read_bytes()))
    trained, audited = files(train), files(custody)
    assert tare_ops.main(['run', 'work-one', '--reattest']) == 1
    assert 'WORK_REATTEST_REQUIRES_RESUME_WITHOUT_RETRY' in json.loads(capsys.readouterr().out)['error']
    assert tare_ops.main(['run', 'work-one', '--resume', '--reattest']) == 1
    result = json.loads(capsys.readouterr().out)
    assert result['error'] == ("ValueError:WORK_REATTEST_REFUSED: [REFUSED] reattest: admission of "
                               f"'{work_run.train_id_for('work-one')}' is ADMITTED, not IDENTITY_ROSTER_MISMATCH "
                               "or IDENTITY_ROSTER_STALE.")
    assert files(train) == trained and files(custody) == audited and calls == ['one']


def test_a_saved_auditor_of_the_implementers_own_family_is_not_re_audited(host, reviewers, monkeypatch, capsys):
    """The origin case: a local qwen38-ninfer implementer saved as vendor Local and the shipped Qwen as saved auditor,
    both Qwen after #262. A new audit would spend model calls and be refused (AUDITOR_NOT_INDEPENDENT)."""
    import tare_ops
    profiles, calls, _, _ = reviewers
    local_implementer(host)
    audit = json.loads(profiles[0].read_bytes())
    audit['model'] = 'qwen-3.8-27b'
    profiles[0].write_text(json.dumps(audit), encoding='utf-8')
    with monkeypatch.context() as before:
        before.setattr(landing_policy, '_local_row',
                       lambda model_id, models: {'vendor': 'Local', 'bias_domain': model_id, 'line': 'local'})
        def die(*a, **kw): raise SystemExit('host interrupted before landing')
        before.setattr(relay_mesh, 'land_train', die)
        with pytest.raises(SystemExit):
            run(host, '--auditor-profile', str(profiles[0]))
    capsys.readouterr()
    train, train_id = work_run.train_dir_for('work-one'), work_run.train_id_for('work-one')
    custody = audit_custody(json.loads((train/'WORK_RUN.json').read_bytes()))
    assert relay_mesh.admission_valid(train_id) == (False, 'smart admission invalid: IDENTITY_ROSTER_MISMATCH')
    trained, audited, roster = files(train), files(custody), files(relay_mesh.RELAY_DIR)
    code, why = relay_mesh.reattestable(train_id)
    assert code == 'IDENTITY_ROSTER_MISMATCH' and 'would not help' in why and 'AUDITOR_NOT_INDEPENDENT' in why
    assert 'Run the Work again to choose another auditor' in why
    assert tare_ops.main(['run', 'work-one', '--resume', '--reattest']) == 1
    error = json.loads(capsys.readouterr().out)['error']
    assert error == f"ValueError:WORK_REATTEST_REFUSED: [REFUSED] reattest: {why}"
    assert relay_mesh.reattest_train(train_id) is False and why in capsys.readouterr().out
    assert files(train) == trained and files(custody) == audited and files(relay_mesh.RELAY_DIR) == roster
    assert calls == ['one']  # no new audit call
