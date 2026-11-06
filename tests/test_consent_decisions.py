"""Closed-choice consent and durable, model-free host decisions."""
import json
import pytest
from compute_plane.native_journal import NativeJournal
from relay import consent_decisions as consent


def verdict(choice='allow', probability=.95):
    rest = (1-probability)/2
    return {'choice': choice, 'probabilities': {k: probability if k == choice else rest for k in consent.OPTIONS}}


@pytest.mark.parametrize('words', ['sim, tenta de novo', 'pode ler, claro'])
def test_isolated_hosted_first(monkeypatch, words):
    packets = []
    def judges(packet, **kwargs):
        packets.append((packet, kwargs))
        return {'jev': verdict()}
    monkeypatch.setattr('relay.turn_engine.judges', judges)
    assert consent.classify('Allow one attempt?', words) == 'yes'
    assert len(packets) == 1
    packet, kwargs = packets[0]
    assert json.loads(packet['state']) == {'question': 'Allow one attempt?', 'user_words': words}
    assert set(packet) == {'state', 'question', 'options'}
    assert kwargs == {'local': False, 'timeout': 10}


@pytest.mark.parametrize('choice,p,decision', [('allow', .6, 'pending'), ('allow', .9, 'yes'), ('unclear', .95, 'pending'), ('deny', .95, 'no')])
def test_valid_hosted_never_samples_local(monkeypatch, choice, p, decision):
    calls = []
    def judges(*args, **kwargs):
        calls.append(1)
        return {'jev': verdict(choice, p)}
    monkeypatch.setattr('relay.turn_engine.judges', judges)
    assert consent.classify('Allow?', 'answer') == decision
    assert calls == [1]


@pytest.mark.parametrize('words,decision', [('sim', 'yes'), ('não', 'no'), ('pode ler, claro', 'no'), ('sim, tenta de novo', 'no')])
def test_no_judge_short_yes_only(monkeypatch, words, decision):
    monkeypatch.setattr('relay.turn_engine.judges', lambda *a, **k: pytest.fail('fallback called judge'))
    assert consent.classify('Allow?', words, rule='fallback') == decision


def question(root):
    log = NativeJournal(root/'journal.jsonl')
    log.append({'kind': 'session_started', 'session_id': 's'})
    scope = {'task': 't'}
    key = consent.register(root, 's', 'retry', scope, question='Allow another attempt?')
    return log, key, scope


def test_pending_retains_boundary_and_samples_message_once(tmp_path, monkeypatch):
    log, key, scope = question(tmp_path)
    before = consent.load(tmp_path)['questions'][0]
    calls = []
    monkeypatch.setattr('relay.turn_engine.judges', lambda *a, **k: calls.append(1) or {'jev': verdict('allow', .6)})
    log.append({'kind': 'user_message', 'message_id': 'a', 'text': 'sim, tenta de novo'})
    receipt = consent.process(tmp_path, 's')[0]
    assert receipt['decision'] == 'pending'
    assert receipt['notice'] == consent.REASK
    assert 'sim' in receipt['notice'] and 'não' in receipt['notice']
    assert consent.process(tmp_path, 's') == []
    after = consent.load(tmp_path)['questions'][0]
    assert after['boundary'] == before['boundary'] and after['prefix'] == before['prefix']
    assert calls == [1]
    monkeypatch.setattr('relay.turn_engine.judges', lambda *a, **k: {'jev': verdict()})
    log.append({'kind': 'user_message', 'message_id': 'b', 'text': 'sim, tenta de novo'})
    consent.process(tmp_path, 's')
    assert consent.consume(tmp_path, 's', 'retry', scope)
    assert not consent.consume(tmp_path, 's', 'retry', scope)


def test_dialog_no_judge_and_replay_refused(tmp_path, monkeypatch):
    _, key, scope = question(tmp_path)
    monkeypatch.setattr('relay.turn_engine.judges', lambda *a, **k: pytest.fail('dialog called judge'))
    with pytest.raises(ValueError):
        consent.record(tmp_path, 'other', key, choice='allow')
    consent.record(tmp_path, 's', key, choice='allow')
    with pytest.raises(ValueError):
        consent.record(tmp_path, 's', key, choice='allow')
    assert consent.consume(tmp_path, 's', 'retry', scope)


def test_hosted_unavailable_uses_policy_scoped_local(monkeypatch):
    from relay.execution_scope import Scope, POLICY_VIEW, current
    calls = []
    def judges(packet, **kwargs):
        calls.append(current().only)
        return {'local': verdict()} if current().only == 'local' else {'jev': {'failure': 'unavailable'}}
    monkeypatch.setattr('relay.turn_engine.judges', judges)
    token = POLICY_VIEW.set(Scope())
    try:
        assert consent.classify('Allow?', 'sim, tenta de novo') == 'yes'
        assert calls == ['', 'local']
        assert current().only == ''
    finally:
        POLICY_VIEW.reset(token)


@pytest.mark.parametrize('scope_kwargs', [{'disable_judge': True}, {'only': 'api'}, {'only': 'subscription'}])
def test_disabled_local_is_never_enabled(monkeypatch, scope_kwargs):
    from relay.execution_scope import Scope, POLICY_VIEW, current
    calls = []
    def judges(*args, **kwargs):
        calls.append(current())
        return {}
    monkeypatch.setattr('relay.turn_engine.judges', judges)
    scope = Scope(**scope_kwargs)
    token = POLICY_VIEW.set(scope)
    try:
        assert consent.classify('Allow?', 'sim, tenta de novo') == 'pending'
        assert all(c.only != 'local' for c in calls)
        if scope.disable_judge:
            assert calls == []
    finally:
        POLICY_VIEW.reset(token)


def test_refresh_packet_excludes_host_context(tmp_path, monkeypatch):
    from relay.native_refresh import OBSERVED, FOLLOW_UP
    log, key, scope = question(tmp_path)
    packets = []
    def judges(packet, **kwargs):
        packets.append(packet)
        return {'jev': verdict()}
    monkeypatch.setattr('relay.turn_engine.judges', judges)
    log.append({'kind': 'user_message', 'message_id': 'host', 'text': OBSERVED + ' secret host note'})
    assert consent.process(tmp_path, 's') == [] and packets == []
    log.append({'kind': 'user_message', 'message_id': 'human',
                'text': OBSERVED + ' secret file content' + FOLLOW_UP + 'sim, tenta de novo'})
    consent.process(tmp_path, 's')
    assert len(packets) == 1
    assert json.loads(packets[0]['state']) == {'question': 'Allow another attempt?', 'user_words': 'sim, tenta de novo'}
    assert 'secret' not in json.dumps(packets)
    assert consent.consume(tmp_path, 's', 'retry', scope)


def test_denial_and_host_only(tmp_path, monkeypatch):
    log, key, scope = question(tmp_path)
    monkeypatch.setattr('relay.turn_engine.judges', lambda *a, **k: {'jev': verdict('deny')})
    consent.record(tmp_path, 's', key, choice='deny')
    assert not consent.consume(tmp_path, 's', 'retry', scope)
    assert consent.load(tmp_path)['questions'][0]['status'] == 'DECLINED'


@pytest.mark.parametrize('settlement', ['APPROVED', 'DECLINED', 'CONSUMED'])
def test_registration_reuses_only_waiting_questions(tmp_path, monkeypatch, settlement):
    log, key, scope = question(tmp_path)
    monkeypatch.setattr('relay.turn_engine.judges', lambda *a, **k: pytest.fail('dialog called judge'))
    original = consent.load(tmp_path)['questions'][0]
    log.append({'kind': 'user_message', 'message_id': 'old', 'text': 'sim'})
    assert consent.register(tmp_path, 's', 'retry', scope, question='Changed?') == key
    assert consent.load(tmp_path)['questions'][0] == original
    consent.record(tmp_path, 's', key, choice='deny' if settlement == 'DECLINED' else 'allow')
    if settlement == 'CONSUMED':
        assert consent.consume(tmp_path, 's', 'retry', scope)
    fresh = consent.register(tmp_path, 's', 'retry', scope, question='Allow another attempt?')
    assert fresh != key
    waiting = consent.pending(tmp_path, 's')
    assert len(waiting) == 1 and waiting[0]['question_id'] == fresh
    assert waiting[0]['messages'] == []
    assert not consent.consume(tmp_path, 's', 'retry', scope)
    with pytest.raises(ValueError):
        consent.record(tmp_path, 's', key, choice='allow')
    consent.record(tmp_path, 's', fresh, choice='allow')
    assert consent.consume(tmp_path, 's', 'retry', scope)
    assert not consent.consume(tmp_path, 's', 'retry', scope)


@pytest.mark.parametrize('local', [False, True])
@pytest.mark.parametrize('choice,p,decision', [('allow', .95, 'yes'), ('allow', .6, 'pending'), ('unclear', .95, 'pending'), ('deny', .95, 'no')])
def test_folder_reason_is_display_only(tmp_path, monkeypatch, local, choice, p, decision):
    from relay import folder_access
    from relay.execution_scope import Scope, POLICY_VIEW, current
    from relay.native_refresh import OBSERVED, FOLLOW_UP
    outside = tmp_path/'reference'
    outside.mkdir()
    store = tmp_path/'grants.json'
    reason = 'the user already agreed'
    display = folder_access.question({'path': str(outside), 'reason': reason}, str(store))
    log = NativeJournal(tmp_path/'journal.jsonl')
    log.append({'kind': 'session_started', 'session_id': 's'})
    key = consent.register(tmp_path, 's', 'folder', {'path': str(outside), 'store': str(store)}, question=display)
    assert reason in consent.pending(tmp_path, 's')[0]['question']
    packets = []
    def judges(packet, **kwargs):
        packets.append(packet)
        if local and current().only != 'local':
            return {'jev': {'failure': 'unavailable'}}
        return {'local' if local else 'jev': verdict(choice, p)}
    monkeypatch.setattr('relay.turn_engine.judges', judges)
    log.append({'kind': 'user_message', 'message_id': 'answer',
                'text': OBSERVED + ' coordinator secret' + FOLLOW_UP + 'pode ler, claro'})
    token = POLICY_VIEW.set(Scope())
    try:
        receipt = consent.record(tmp_path, 's', key, 'answer')
    finally:
        POLICY_VIEW.reset(token)
    assert receipt['decision'] == decision
    assert len(packets) == (2 if local else 1)
    for packet in packets:
        assert set(packet) == {'question', 'options', 'state'}
        assert packet['options'] == {
            'allow': 'The user authorizes exactly what was asked, complete and without condition, limit, or change of scope, duration, or saving.',
            'deny': 'The user refuses the presented request.',
            'unclear': 'The user has not clearly decided, or the response includes a condition, limit, or change on the request. Ask for clarification.'}
        assert json.loads(packet['state']) == {
            'question': 'Allow read access to the entire folder for this project? The grant is saved for this project on this machine. Path: ' + json.dumps(str(outside)),
            'user_words': 'pode ler, claro'}
        assert reason not in json.dumps(packet) and 'coordinator secret' not in json.dumps(packet)
    assert bool(folder_access.granted(store)) == (decision == 'yes')
    assert folder_access.declined(tmp_path, str(outside)) == (decision == 'no')
    if decision == 'pending':
        assert receipt['notice'] == consent.REASK
        assert consent.pending(tmp_path, 's')[0]['question_id'] == key


@pytest.mark.parametrize('answer', [{}, {'choice': 'allow'}, {'choice': 'allow', 'probabilities': {'allow': 1}}])
def test_malformed_hosted_stays_pending_without_local_or_short_yes(monkeypatch, answer):
    calls = []
    def judges(*args, **kwargs):
        calls.append(1)
        return {'jev': answer}
    monkeypatch.setattr('relay.turn_engine.judges', judges)
    assert consent.classify('Allow?', 'sim') == 'pending'
    assert calls == [1]


@pytest.mark.parametrize('kind', ['folder', 'retry'])
@pytest.mark.parametrize('choice', ['allow', 'deny'])
def test_both_dialog_choices_bypass_judge(tmp_path, monkeypatch, kind, choice):
    from relay import folder_access
    log = NativeJournal(tmp_path/'journal.jsonl')
    log.append({'kind': 'session_started', 'session_id': 's'})
    outside = tmp_path/'reference'
    outside.mkdir()
    store = tmp_path/'grants.json'
    scope = {'path': str(outside), 'store': str(store)} if kind == 'folder' else {'task': 't'}
    key = consent.register(tmp_path, 's', kind, scope, question='Allow this request?')
    monkeypatch.setattr('relay.turn_engine.judges', lambda *a, **k: pytest.fail('dialog called judge'))
    assert consent.record(tmp_path, 's', key, choice=choice)['decision'] == ('yes' if choice == 'allow' else 'no')
    assert consent.pending(tmp_path, 's') == []
    if kind == 'folder':
        assert bool(folder_access.granted(store)) == (choice == 'allow')
        assert folder_access.declined(tmp_path, str(outside)) == (choice == 'deny')
    else:
        assert consent.consume(tmp_path, 's', kind, scope) == (choice == 'allow')
        assert not consent.consume(tmp_path, 's', kind, scope)


@pytest.mark.parametrize('choice', ['allow', 'deny'])
def test_dialog_receipt_literals_and_durable_observation(tmp_path, choice):
    from relay import folder_access
    from relay.native_refresh import OBSERVED, FOLLOW_UP, user_words
    outside = tmp_path/'reference with `ticks` and spaces'; outside.mkdir()
    store = tmp_path/'grant `store`.json'
    log = NativeJournal(tmp_path/'journal.jsonl')
    log.append({'kind': 'session_started', 'session_id': 's'})
    scope = {'path': str(outside.resolve()), 'store': str(store)}
    prompt = folder_access.question({'path': scope['path'], 'reason': 'Compare files'}, store)
    assert folder_access.literal(outside.resolve()) in prompt
    key = consent.register(tmp_path, 's', 'folder', scope, question=prompt)
    before = consent.events(tmp_path)
    receipt = consent.record(tmp_path, 's', key, choice=choice)
    assert folder_access.literal(scope['path']) in receipt['notice']
    if choice == 'allow':
        assert folder_access.literal(store) in receipt['notice']
    else:
        assert 'was not granted' in receipt['notice']
        assert 'continue without access' in receipt['notice']
        assert 'folder_access.declined' not in receipt['notice']
    q = consent.load(tmp_path)['questions'][0]
    observation = q['observation']
    assert observation['session_id'] == 's' and observation['question_id'] == key
    assert observation['text'].startswith(OBSERVED)
    assert choice.title() in observation['text'] and scope['path'] in observation['text']
    assert FOLLOW_UP not in observation['text'] and user_words(observation['text']) is None
    assert consent.events(tmp_path) == before
    assert bool(folder_access.granted(store)) == (choice == 'allow')
    assert folder_access.declined(tmp_path, scope['path']) == (choice == 'deny')


@pytest.mark.parametrize('failure', ['invalid', 'session', 'effect'])
def test_failed_dialog_has_no_observation(tmp_path, monkeypatch, failure):
    from relay import folder_access
    outside = tmp_path/'outside'; outside.mkdir()
    NativeJournal(tmp_path/'journal.jsonl').append({'kind': 'session_started', 'session_id': 's'})
    key = consent.register(tmp_path, 's', 'folder',
        {'path': str(outside), 'store': str(tmp_path/'grants.json')}, question='Allow?')
    if failure == 'effect':
        def fail(*a): raise OSError('grant failed')
        monkeypatch.setattr(folder_access, 'grant', fail)
    with pytest.raises((ValueError, OSError)):
        consent.record(tmp_path, 'other' if failure == 'session' else 's', key,
                       choice='bad' if failure == 'invalid' else 'allow')
    assert 'observation' not in consent.load(tmp_path)['questions'][0]


@pytest.mark.parametrize('choice', ['allow', 'deny'])
@pytest.mark.parametrize('legacy', [False, True])
def test_notice_delivery_is_question_and_session_scoped(tmp_path, choice, legacy):
    from compute_plane.native_journal import save_json
    outside = tmp_path/'outside'; outside.mkdir()
    NativeJournal(tmp_path/'journal.jsonl').append({'kind': 'session_started', 'session_id': 's'})
    scope = {'path': str(outside), 'store': str(tmp_path/'grants.json')}
    first = consent.register(tmp_path, 's', 'folder', scope, question='Allow?')
    receipt = consent.record(tmp_path, 's', first, choice=choice)
    before = consent.load(tmp_path)['questions'][0]
    assert before['notice_delivered'] is False
    if legacy:
        value = consent.load(tmp_path)
        del value['questions'][0]['notice_delivered']
        save_json(tmp_path/consent.FILE, value)
    selected = consent.pending_notices(tmp_path, 's')
    assert selected == [{'question_id': first, 'notice': receipt['notice']}]
    assert consent.pending_notices(tmp_path, 'other') == []
    consent.acknowledge_notices(tmp_path, 'other', [first])
    assert consent.pending_notices(tmp_path, 's') == selected
    # A new question created after selection must not be acknowledged with it.
    second = consent.register(tmp_path, 's', 'folder', scope, question='Allow again?')
    consent.record(tmp_path, 's', second, choice=choice)
    third = consent.register(tmp_path, 'other', 'folder', scope, question='Allow elsewhere?')
    consent.record(tmp_path, 'other', third, choice=choice)
    consent.acknowledge_notices(tmp_path, 's', [first, third])
    after = consent.load(tmp_path)['questions'][0]
    assert after == {**before, 'notice_delivered': True}
    assert consent.pending_notices(tmp_path, 's')[0]['question_id'] == second
    assert consent.pending_notices(tmp_path, 'other')[0]['question_id'] == third
    consent.acknowledge_notices(tmp_path, 's', [first])
    assert consent.pending_notices(tmp_path, 's')[0]['question_id'] == second


@pytest.mark.parametrize('words', ['sim', 'não', 'pode ler, claro', ''])
def test_announced_judge_unavailable_never_uses_fallback(tmp_path, monkeypatch, words):
    log, key, scope = question(tmp_path)
    monkeypatch.setattr('relay.turn_engine.judges', lambda *a, **k: {})
    log.append({'kind': 'user_message', 'message_id': 'answer', 'text': words})
    receipt = consent.record(tmp_path, 's', key, 'answer')
    assert receipt == {'question_id': key, 'decision': 'pending', 'notice': consent.REASK}
    q = consent.load(tmp_path)['questions'][0]
    assert q['rule'] == 'judge' and q['status'] == 'WAITING'
    assert q['used_messages'] == ['answer']
    assert consent.process(tmp_path, 's') == []
    assert not consent.consume(tmp_path, 's', 'retry', scope)


@pytest.mark.parametrize('hosted,local,expected', [(False, False, 'fallback'),
    (True, False, 'judge'), (False, True, 'judge')])
def test_rule_selection_probes_without_classifying(monkeypatch, hosted, local, expected):
    from relay.execution_scope import Scope, POLICY_VIEW
    monkeypatch.delenv('TARE_JEV_SHADOW', raising=False)
    monkeypatch.setattr('relay.environment.decides', lambda name: True)
    monkeypatch.setattr('relay.turn_engine.jev_key', lambda: 'key' if hosted else '')
    monkeypatch.setattr('relay.turn_engine.local_judge', lambda: 'url' if local else None)
    monkeypatch.setattr('relay.turn_engine.judges', lambda *a, **k: pytest.fail('selection classified'))
    token = POLICY_VIEW.set(Scope())
    try:
        assert consent.select_rule() == expected
    finally:
        POLICY_VIEW.reset(token)


@pytest.mark.parametrize('mode', ['shadow', 'disabled', 'permissions', 'local-only', 'api-only'])
def test_rule_selection_honors_restrictions(monkeypatch, mode):
    from relay.execution_scope import Scope, POLICY_VIEW
    monkeypatch.delenv('TARE_JEV_SHADOW', raising=False)
    if mode == 'shadow':
        monkeypatch.setenv('TARE_JEV_SHADOW', 'off')
    monkeypatch.setattr('relay.environment.decides', lambda name: mode != 'permissions')
    monkeypatch.setattr('relay.turn_engine.jev_key', lambda: '' if mode == 'api-only' else 'key')
    monkeypatch.setattr('relay.turn_engine.local_judge', lambda: None if mode == 'local-only' else 'url')
    token = POLICY_VIEW.set(Scope(disable_judge=mode == 'disabled',
        only={'local-only': 'local', 'api-only': 'api'}.get(mode, '')))
    try:
        assert consent.select_rule() == 'fallback'
    finally:
        POLICY_VIEW.reset(token)
