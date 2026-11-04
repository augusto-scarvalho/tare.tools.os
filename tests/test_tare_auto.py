"""tare auto in the TUI: policy routes at session start, the next tier after a blocked turn."""
import json
from types import SimpleNamespace

import pytest

from relay import model_choices, native_catalog, turn_engine
from relay.console_models import ConversationModels


@pytest.fixture
def tiers(monkeypatch):
    down = set()
    monkeypatch.setattr(turn_engine, 'policy', lambda: {'implementer': [['local'], ['cheap-a', 'cheap-b'], ['middle']]})
    monkeypatch.setattr(turn_engine, 'available', lambda name: (None, 'busy') if name in down else (
        {'id': name, 'native_profile': {'protocol': 'openai-chat', 'model': name}}, None))
    return down


def test_the_picker_offers_tare_auto_and_a_new_session_resolves_it_to_a_policy_route(tiers):
    item = next(r for r in model_choices.inventory() if r['key'] == 'tare-auto')
    assert item['label'] == 'tare auto' and item['group'] == 'tare'
    chosen = model_choices.realization(model_choices.inventory(),
                                       {'key': 'tare-auto', 'connection': 'tare-auto', 'reasoning': 'default'})
    tiers.add('local')
    resolved = turn_engine.resolve_auto(chosen)
    assert resolved['id'] == 'cheap-a' and 'native_auto_routes' not in resolved
    assert resolved['native_auto_selection'] == {'tier': 1, 'route': 'cheap-a',
                                                 'skipped': [{'route': 'local', 'reason': 'busy'}]}
    assert resolved['native_model_choice']['label'] == 'tare auto'
    plain = native_catalog.select('qwen38-gsq')
    assert turn_engine.resolve_auto(plain) is plain


def test_a_blocked_session_moves_to_the_next_tier_until_none_is_left(tiers):
    first = turn_engine.resolve_auto({'native_auto_routes': 'implementer'})
    second = turn_engine.next_auto(first)
    assert (first['id'], second['id'], second['native_auto_selection']['tier']) == ('local', 'cheap-a', 1)
    assert turn_engine.next_auto(turn_engine.next_auto(second)) is None
    assert turn_engine.next_auto({'id': 'qwen38-gsq'}) is None  # not a tare auto session


def test_an_account_out_of_usage_moves_within_its_tier_first(tiers, monkeypatch):
    monkeypatch.setattr(turn_engine, 'usage_limited', lambda deployment: 1e12 if deployment['id'] == 'cheap-a' else None)
    tiers.add('cheap-a')  # available() skips the spent account's routes
    spent = turn_engine.resolve_auto({'native_auto_routes': 'implementer'}, 1)
    assert spent['id'] == 'cheap-b'
    assert turn_engine.next_auto({**spent, 'id': 'cheap-a', 'native_auto_selection': {'tier': 1, 'route': 'cheap-a'}})['id'] == 'cheap-b'
    # Any other stop still goes one tier up.
    assert turn_engine.next_auto({**spent, 'native_auto_selection': {'tier': 1, 'route': 'cheap-b'}})['id'] == 'middle'


def test_usage_limits_are_kept_per_account_until_they_reset(tmp_path, monkeypatch):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path))
    claude = {'id': 'claude-opus-5-5-cli', 'native_profile': {'protocol': 'vendor-cli', 'endpoint': 'cli://claude'}}
    sonnet = {'id': 'claude-sonnet-5-5-cli', 'native_profile': {'protocol': 'vendor-cli', 'endpoint': 'cli://claude'}}
    native = {'id': 'codex-native-gpt-6-luna', 'native_profile': {'protocol': 'openai-codex-responses', 'endpoint': 'x'}}
    local = {'id': 'qwen38-gsq', 'native_profile': {'protocol': 'openai-chat', 'endpoint': 'http://127.0.0.1:8080/v1'}}
    assert [turn_engine.usage_account(d) for d in (claude, native, local)] == ['claude', 'codex', None]
    assert turn_engine.note_usage_limit(claude, 2000, now=1000) == 2000
    assert turn_engine.usage_limited(sonnet, now=1500) == 2000 and turn_engine.usage_limited(sonnet, now=2001) is None
    assert turn_engine.note_usage_limit(native, None, now=1000) == 1000 + turn_engine.USAGE_LIMIT_DEFAULT_SECONDS
    assert turn_engine.note_usage_limit(native, 1000 + 30*86400, now=1000) == 1000 + turn_engine.USAGE_LIMIT_DEFAULT_SECONDS
    assert turn_engine.note_usage_limit(local, 2000) is None and turn_engine.usage_limited(local) is None


def test_a_model_scoped_limit_leaves_other_models_on_the_account(tmp_path, monkeypatch):
    # AGY 1.4.12 "Individual quota reached" refused Gemini 4 while Gemini 3.8 answered the same request (kind 'model').
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path))
    def agy(model):
        return {'id': model, 'native_profile': {'protocol': 'vendor-cli', 'endpoint': 'cli://agy', 'model': model}}
    argon, flash = agy('gemini-4-argon-high'), agy('gemini-3.8-flash-high')
    assert turn_engine.note_usage_limit(argon, 5000, now=1000, kind='model') == 5000
    assert turn_engine.usage_limited(argon, now=1500) == 5000 and turn_engine.usage_limited(flash, now=1500) is None
    # An account-wide limit still leaves every model, and the later of both limits wins.
    assert turn_engine.note_usage_limit(flash, 3000, now=1000) == 3000
    assert turn_engine.usage_limited(flash, now=1500) == 3000 and turn_engine.usage_limited(argon, now=1500) == 5000
    assert turn_engine.usage_limited(argon, now=5001) is None


def test_the_console_continues_a_blocked_auto_session_without_a_review_panel(tiers, tmp_path, monkeypatch):
    source = tmp_path/'session'
    source.mkdir()
    saved = turn_engine.resolve_auto({'native_auto_routes': 'implementer'})
    (source/'native-agent.json').write_text(json.dumps({'deployment': saved}), encoding='utf-8')
    monkeypatch.setattr('relay.native_continuity.preview', lambda *a, **kw: {'text': 'BRIEF'})
    calls, said = [], []
    console = SimpleNamespace(busy=False, workspace_root=tmp_path, say=said.append, notify=said.append,
                              backend=SimpleNamespace(continuity_terminal=lambda source: None),
                              selection=lambda: SimpleNamespace(mode='assistant', streaming=True, local_models=True),
                              execute=lambda resume, **kw: calls.append(kw))
    ConversationModels.auto_escalate(console, str(source))
    value, text, deployment = calls[0]['continuation']
    assert calls[0]['assistant'] is True and text.startswith('BRIEF') and deployment['id'] == 'cheap-a'
    assert deployment['native_streaming'] is True and 'continuing on cheap-a' in said[0]
    (source/'native-agent.json').write_text(json.dumps({'deployment': {'id': 'qwen38-gsq'}}), encoding='utf-8')
    ConversationModels.auto_escalate(console, str(source))
    assert len(calls) == 1  # an ordinary session is left alone
    (source/'native-agent.json').write_text(json.dumps({'deployment': saved}), encoding='utf-8')
    ConversationModels.auto_escalate(console, str(source), 'EXHAUSTED')
    assert len(calls) == 1 and 'spent the budget you set' in said[-1]  # the operator's budget is the cap


def test_the_console_audits_a_turn_that_changed_code_and_sends_one_fix(tmp_path, monkeypatch):
    source = tmp_path/'session'
    source.mkdir()
    (source/'native-agent.json').write_text(json.dumps({
        'config': {'role': 'implementer'}, 'deployment': {'id': 'qwen38-gsq'}}), encoding='utf-8')
    changed = [{'path': 'a.py', 'diff': '+x'}]
    audits = []

    def audit(request, files, root, implementer, emit, checks=None):
        audits.append((request, implementer['id']))
        emit('**REVISE** — off by one\n')
        return {'route': 'sol', 'verdict': 'REVISE', 'findings': ['range skips the last item'],
                'remaining': [], 'changed_lines': 1, 'recheck': request.startswith(turn_engine.FIX_PREFIX)}
    monkeypatch.setattr(turn_engine, 'run_audit', audit)
    monkeypatch.setattr(turn_engine, 'judges', lambda request: {})  # no judge: every finding stands
    sent, said = [], []
    console = SimpleNamespace(call_from_thread=lambda fn, *a, **kw: fn(*a, **kw), notify=said.append,
                              audit_say=lambda text, source: said.append(text),
                              send_audit_fix=lambda text, source: sent.append(text))
    ConversationModels._audit_changes(console, str(source), [], tmp_path, 'add paging')
    assert audits == [] and sent == []  # nothing changed, no audit
    ConversationModels._audit_changes(console, str(source), changed, tmp_path, 'add paging')
    assert audits == [('add paging', 'qwen38-gsq')] and said == ['**REVISE** — off by one'] and len(sent) == 1
    assert 'range skips the last item' in sent[0] and 'smallest change' in sent[0]
    ConversationModels._audit_changes(console, str(source), changed, tmp_path, sent[0])
    assert len(audits) == 2 and len(sent) == 1  # the fix is audited, but gets no second automatic fix


def test_audit_findings_go_only_to_the_audited_conversation(tmp_path):
    audited, other = tmp_path/'a', tmp_path/'b'
    typed, sent, notes, said = [], [], [], []
    prompt = SimpleNamespace(text='', load_text=typed.append)
    console = SimpleNamespace(busy=False, assistant_session=str(other), query_one=lambda *a: prompt,
                              selection=lambda: SimpleNamespace(mode='assistant'), say=said.append,
                              notify=lambda text, **kw: notes.append(text), execute=lambda resume: sent.append(resume))
    console.audited_open = lambda source: ConversationModels.audited_open(console, source)
    ConversationModels.send_audit_fix(console, 'fix it', str(audited))  # the operator moved to conversation b
    ConversationModels.audit_say(console, '**REVISE**', str(audited))
    assert typed == [] and sent == [] and said == [] and 'another conversation' in notes[0] and len(notes) == 2
    console.assistant_session = str(audited)
    ConversationModels.send_audit_fix(console, 'fix it', str(audited))
    assert typed == ['fix it'] and sent == [False]


def test_jev_shadow_records_its_pick_next_to_what_tare_did_and_never_raises(tmp_path, monkeypatch):
    import compute_plane.jev_choice_worker as worker
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path))
    monkeypatch.delenv('TYPESAFE_API_KEY', raising=False)
    # Hosted Jev only: on the GPU node (the CI runner) a resident Qwen would otherwise answer as the local judge.
    monkeypatch.setattr(turn_engine, 'local_judge', lambda: None)
    assert turn_engine.jev_shadow('fix a typo', {'source': 'serve'}) is None  # off without a key
    monkeypatch.setenv('TYPESAFE_API_KEY', 'k')
    asked = []
    def assess(request, *, key, timeout, model):
        assert model == turn_engine.HOSTED_JEV_MODEL
        asked.append(request)
        return {'choice': 'local', 'probabilities': {'local': 0.9, 'cheap': 0.08, 'intermediate': 0.02},
                'usage': {'input_tokens': 120, 'output_tokens': 0}, 'model_identity': {'model': turn_engine.HOSTED_JEV_MODEL}}
    monkeypatch.setattr(worker, 'assess', assess)
    row = turn_engine.jev_shadow('fix a typo', {'source': 'serve', 'final_tier': 0})
    assert json.loads(asked[0]['state']) == {'user_request':'fix a typo'} and set(asked[0]['options']) == {'local', 'cheap', 'intermediate'}
    assert row['jev']['choice'] == 'local' and row['final_tier'] == 0 and 'fix a typo' not in json.dumps(row)
    monkeypatch.setattr(worker, 'assess', lambda *a, **kw: (_ for _ in ()).throw(ValueError('ASSESSMENT_RESOURCE_BUSY')))
    assert turn_engine.jev_shadow('x', {'source': 'tui'})['jev']['failure'] == 'ASSESSMENT_RESOURCE_BUSY'
    assert 'jev' not in turn_engine.jev_shadow('', {'event': 'escalated'})  # events cost nothing
    rows = (tmp_path/'serve'/'jev-shadow.jsonl').read_text(encoding='utf-8').splitlines()
    assert len(rows) == 3
    monkeypatch.setenv('TARE_JEV_SHADOW', 'off')
    assert turn_engine.jev_shadow('x', {}) is None


def test_the_resident_local_judge_is_asked_only_while_loaded_and_idle(tmp_path, monkeypatch):
    import compute_plane.ninfer_litjev as litjev
    import urllib.request
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path))
    monkeypatch.setenv('TARE_LOCAL_GATEWAY', 'http://gateway.test:8080')
    monkeypatch.delenv('TYPESAFE_API_KEY', raising=False)
    status = {'current_model': 'qwen38-gsq', 'backend_load': {'admitted': 0, 'running': 0, 'waiting': 0}}

    class Reply:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self, *a):
            return json.dumps(status).encode()
    monkeypatch.setattr(urllib.request, 'urlopen', lambda url, timeout: Reply())
    assert turn_engine.local_judge() == 'http://gateway.test:8080/v1/chat/completions'
    asked = []
    monkeypatch.setattr(litjev, 'assess', lambda request, **kw: asked.append(kw) or {
        'choice': 'local', 'probabilities': {'local': 0.8, 'cheap': 0.2, 'intermediate': 0.0},
        'usage': {'input_tokens': 90, 'output_tokens': 1}, 'model_identity': {'model': 'qwen38-gsq'}})
    row = turn_engine.jev_shadow('fix a typo', {'source': 'serve'})  # no Jev key: the local judge alone
    assert row['local']['choice'] == 'local' and 'jev' not in row and asked[0]['model'] == 'qwen38-gsq'
    for busy in ({**status, 'backend_load': {'admitted': 1}}, {**status, 'current_model': 'swift27b'},
                 {**status, 'backend_load': None}):
        status.clear(); status.update(busy)
        assert turn_engine.local_judge() is None  # generating, another model, or not NInfer: never wait or load
    assert turn_engine.jev_shadow('fix a typo', {'source': 'serve'}) is None


def test_a_tui_session_with_commands_is_audited_on_project_snapshots(tmp_path):
    from types import SimpleNamespace
    from relay.native_console import session_has_terminal
    session = tmp_path/'session'
    session.mkdir()
    assert session_has_terminal(SimpleNamespace(terminal=True, session=str(session)))
    assert not session_has_terminal(SimpleNamespace(terminal=False, session=str(session)))  # no saved config yet
    (session/'native-agent.json').write_text(json.dumps({'config': {'terminal': True}}), encoding='utf-8')
    assert session_has_terminal(SimpleNamespace(terminal=False, session=str(session)))  # a resumed conversation


LUNA = 'Vou rastrear a regra que define IsDeleted e o fluxo usado para propostas antigas...'


def test_unanswered_escalates_only_a_confident_announcement_and_fails_open(tmp_path, monkeypatch):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path))
    odds = {LUNA: ('announced', .95), 'Sim, é 42.': ('answered', .97),
            'Você quer o fluxo das propostas novas ou das antigas?': ('answered', .9),
            'Não posso apagar o banco de produção: o pedido não dá esse acesso.': ('answered', .93),
            'Deixa eu ver o código.': ('announced', .7)}  # below the bar: the reply stands
    asked = []

    def judges(request):
        asked.append(request)
        choice, p = odds[json.loads(request['state'])['assistant_final_reply']]
        other = 'answered' if choice == 'announced' else 'announced'
        return {'local': {'choice': choice, 'model': 'qwen38-gsq', 'probabilities': {choice: p, other: 1 - p}}}
    monkeypatch.setattr(turn_engine, 'judges', judges)
    verdict = turn_engine.unanswered('Como IsDeleted é definido?', LUNA)
    assert verdict['model'] == 'qwen38-gsq' and verdict['probabilities']['announced'] == .95
    assert asked[0]['question'] == turn_engine.ANSWER_QUESTION and set(asked[0]['options']) == {'answered', 'announced'}
    for reply in list(odds)[1:]:  # a short answer, a clarifying question, a justified refusal
        assert turn_engine.unanswered('x', reply) is None, reply
    assert turn_engine.unanswered('x', 'a' * (turn_engine.ANSWER_JUDGE_MAX_CHARS + 1)) is None
    assert turn_engine.unanswered('x', '  ') is None and len(asked) == 5  # long or empty: no judge is asked
    jev = {'choice': 'answered', 'model': 'jev-1.13.0', 'probabilities': {'answered': .8, 'announced': .2}}
    monkeypatch.setattr(turn_engine, 'judges', lambda request: {**judges(request), 'jev': jev})
    assert turn_engine.unanswered('x', LUNA) is None  # the judges disagree: the reply stands
    monkeypatch.setattr(turn_engine, 'judges', lambda request: {})
    assert turn_engine.unanswered('x', LUNA) is None  # no judge available: fail open
    rows = [json.loads(line) for line in (tmp_path/'serve'/'answer-judge.jsonl').read_text(encoding='utf-8').splitlines()]
    assert rows[0]['escalate'] is True and rows[-1]['no_judge'] is True and not rows[-1]['escalate']
    assert 'IsDeleted' not in json.dumps(rows)  # hashes and sizes, never the texts


def test_tare_auto_in_the_tui_goes_up_a_tier_when_a_reply_only_announced_the_work(tiers, tmp_path, monkeypatch):
    source = tmp_path/'session'
    source.mkdir()
    saved = turn_engine.resolve_auto({'native_auto_routes': 'implementer'})
    (source/'native-agent.json').write_text(json.dumps({'deployment': saved}), encoding='utf-8')
    monkeypatch.setattr('relay.native_continuity.preview', lambda *a, **kw: {'text': 'BRIEF'})
    judged = []
    monkeypatch.setattr(turn_engine, 'unanswered', lambda request, reply, observed=None: judged.append(reply) or {
        'model': 'qwen38-gsq', 'probabilities': {'announced': .95}})
    calls, said = [], []
    console = SimpleNamespace(busy=False, workspace_root=tmp_path, say=said.append,
                              notify=lambda text, **kw: said.append(text), assistant_session=str(source),
                              backend=SimpleNamespace(continuity_terminal=lambda source: None),
                              selection=lambda: SimpleNamespace(mode='assistant', streaming=True, session=None, local_models=True),
                              execute=lambda resume, **kw: calls.append(kw),
                              call_from_thread=lambda fn, *a, **kw: fn(*a, **kw))
    console.auto_escalate = lambda *a, **kw: ConversationModels.auto_escalate(console, *a, **kw)
    console.escalate_unanswered = lambda *a: ConversationModels.escalate_unanswered(console, *a)
    ConversationModels._judge_answer(console, str(source), 'Como IsDeleted é definido?', LUNA)
    value, text, deployment = calls[0]['continuation']
    assert deployment['id'] == 'cheap-a' and text.startswith('BRIEF') and text.endswith(turn_engine.UNANSWERED_CONTINUE)
    assert judged == [LUNA] and 'only announced the work' in said[0] and 'continuing on cheap-a' in said[0]
    console.assistant_session = str(tmp_path/'other')  # the operator moved to another conversation
    ConversationModels._judge_answer(console, str(source), 'q', LUNA)
    assert len(calls) == 1 and 'another conversation' in said[-1]
    top = turn_engine.resolve_auto({'native_auto_routes': 'implementer'}, 2)
    (source/'native-agent.json').write_text(json.dumps({'deployment': top}), encoding='utf-8')
    ConversationModels._judge_answer(console, str(source), 'q', LUNA)
    (source/'native-agent.json').write_text(json.dumps({'deployment': {'id': 'qwen38-gsq'}}), encoding='utf-8')
    ConversationModels._judge_answer(console, str(source), 'q', LUNA)
    assert len(judged) == 2  # no stronger tier, or not tare auto: the judge is not asked


@pytest.mark.parametrize('input_mode', ['argv', 'file', 'interactive'])
def test_public_native_start_uses_full_request_once(tiers, monkeypatch, tmp_path, input_mode):
    from relay import native_agent
    import compute_plane.jev_choice_worker as worker
    monkeypatch.setattr(turn_engine, 'jev_key', lambda: 'fixture-key')
    monkeypatch.setattr('relay.environment.decides', lambda name: True)
    monkeypatch.delenv('TARE_JEV_SHADOW', raising=False)
    asked, saved = [], []
    text = 'complex refactor; preserve every requirement é'
    def assess(request, **kw):
        assert kw == {'key': 'fixture-key', 'timeout': 3, 'model': turn_engine.HOSTED_JEV_MODEL}
        asked.append(request)
        return {'choice': 'tier_2', 'probabilities': {'tier_0': 0, 'tier_1': 0, 'tier_2': 1},
                'model_identity': {'model': turn_engine.HOSTED_JEV_MODEL}, 'usage': {'input_tokens': 90}}
    monkeypatch.setattr(worker, 'assess', assess)
    monkeypatch.setattr(turn_engine, 'jev_shadow', lambda *a: pytest.fail('duplicate initial shadow'))
    monkeypatch.setattr(native_agent, 'run_session', lambda config, deployment, custody, instruction, **kw:
                        saved.append((deployment, instruction)) or {'status': 'PROPOSED'})
    args = ['--root', str(tmp_path), '--session', str(tmp_path/'new'), '--deployment', 'tare-auto',
            '--role', 'implementer', '--no-context']
    if input_mode == 'file':
        path = tmp_path/'request.txt'
        path.write_text(text, encoding='utf-8')
        args += ['--instruction-file', str(path)]
    elif input_mode == 'interactive':
        monkeypatch.setattr(native_agent, '_interactive_line', lambda: text)
        # Stop the interactive loop after observing the initial public invocation.
        def run(config, deployment, custody, instruction, **kw):
            saved.append((deployment, instruction))
            return {'status': 'BLOCKED'}
        monkeypatch.setattr(native_agent, 'run_session', run)
        args += ['--interactive']
    else:
        args += ['--instruction', text]
    assert native_agent.main(args) == (2 if input_mode == 'interactive' else 0)
    assert len(asked) == 1 and asked[0]['state'] == text
    assert saved[0][0]['id'] == 'middle' and saved[0][1] == text
    assert saved[0][0]['native_auto_selection']['decision']['source'] == 'hosted_jev'


def test_public_explicit_resume_and_provider_continuation_keep_saved_identity(monkeypatch, tmp_path):
    from relay import native_agent, native_catalog
    monkeypatch.setattr(turn_engine, 'initial_choice', lambda *a, **kw: pytest.fail('not an initial auto chat'))
    monkeypatch.setattr(turn_engine, 'jev_shadow', lambda *a, **kw: None)
    selected = {'id': 'fixture-explicit', 'native_profile': {'protocol': 'openai-chat', 'model': 'fixed'},
                'native_cache_key': 'fixed-cache', 'native_host_modes': {'must': 'not resolve on continuation'}}
    explicit = {k: v for k, v in selected.items() if k != 'native_host_modes'}
    monkeypatch.setattr(native_catalog, 'select', lambda *a, **kw: explicit)
    source = tmp_path/'source'
    source.mkdir()
    config = {'role': 'implementer', 'repositories': {}, 'capabilities': []}
    history = [{'role': 'user', 'content': 'saved history'}]
    state = {'config': config, 'deployment': selected, 'history': history}
    path = source/'native-agent.json'
    path.write_text(json.dumps(state), encoding='utf-8')
    original = path.read_bytes()
    seen = []
    def run(config, deployment, custody, prompt, **kw):
        seen.append((deployment, prompt, kw))
        return {'status': 'PROPOSED'}
    monkeypatch.setattr(native_agent, 'run_session', run)
    assert native_agent.main(['--root', str(tmp_path), '--deployment', 'fixed', '--instruction', 'hello']) == 0
    assert seen[-1][0] is explicit
    assert native_agent.main(['--session', str(source), '--resume', '--follow-up', 'more',
                              '--message-id', 'fixture-message']) == 0
    assert seen[-1][0] is None and seen[-1][2]['follow_up'] == 'more'
    target = tmp_path/'successor'
    target.mkdir()
    (target/'continuation.json').write_text(json.dumps({'source': str(source), 'deployment': selected,
                                                       'text': 'continue with saved history'}), encoding='utf-8')
    assert native_agent.main(['--session', str(target), '--continue-provider']) == 0
    assert seen[-1][0] == selected and seen[-1][1] == 'continue with saved history'
    assert path.read_bytes() == original  # profile, cache key and history were never rewritten
