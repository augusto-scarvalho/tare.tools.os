"""The auditor of a unit's change is ranked by its class for the unit's type of work (`kind`) where a board places the
model, else by its general class. Only the order among the auditors the automatic choice admits moves: independence,
another vendor first, a pin and the owner's own list are as before. Every model and number here is invented."""
import json
import subprocess
from copy import deepcopy

import pytest

from relay import conversation_review, native_catalog, native_routes, turn_engine
from relay import execution_scope as scope
from relay import model_signals as signals
from test_conversation_tasks import host, intake, planned  # noqa: F401  (host is a fixture)
from test_cross_vendor_auditors import install, pool  # noqa: F401  (pool is a fixture)
from test_role_pin_dispatch import pin_catalog  # noqa: F401  (a fixture)
from test_role_pins import parse
from test_task_types import ONE, board
from test_unit_kind import TYPE

FABLE = 'claude-api-fable-5-1'
LINES = {'sol': ('gpt-6.1-sol_max', 'OpenAI'), 'terra': ('gpt-6.1-terra_high', 'OpenAI'),
         'astra': ('gpt-6-astra_max', 'OpenAI'), 'opus': ('claude-opus-5-5_high', 'Anthropic'),
         'haiku': ('claude-haiku-5-5_high', 'Anthropic'), 'argon': ('gemini-4-argon_high', 'Google DeepMind'),
         'flash': ('gemini-3.8-flash_high', 'Google DeepMind'), 'carbon': ('gemini-4-carbon_high', 'Google DeepMind')}


def saved(monkeypatch, *order):
    """The machine's saved boards: one board of the type 'coding' ranking the eight lines in `order`, best first (the
    first two frontier for the type, the next four middling, the last two cheap); none with no order. Returns the
    list of reads."""
    reads = []
    kept = {signals.source_key(ONE): board(ONE, anchors=[(*LINES[n], .9 - .1*i) for i, n in enumerate(order)])} \
        if order else {}
    monkeypatch.setattr(signals, 'TYPES', [TYPE])
    monkeypatch.setattr(signals, 'boards', lambda: reads.append(1) or kept)
    return reads


def ranking(producer, work_type=None):
    """([route ids in order], {route id: its candidate row}) of the automatic choice of a change's auditor."""
    chosen, receipt = native_routes.reviewers(native_catalog.select(producer), work_type=work_type)
    return [d['id'] for d in chosen], {c['deployment']: c for c in receipt['candidates']}


def not_read():
    pytest.fail('the boards were read')


def test_a_route_is_ranked_by_its_class_for_the_type(pool, monkeypatch):
    install(pool, 'codex-cli', 'cli://codex', 'gpt-6-astra')
    install(pool, 'codex-cli-gpt-6-1-sol', 'cli://codex', 'gpt-6.1-sol')
    # In general Sol and Argon are middling (the class an auditor wants) and Astra frontier. For this type Sol is
    # frontier, Astra middling and Argon cheap.
    reads = saved(monkeypatch, 'sol', 'terra', 'opus', 'astra', 'flash', 'haiku', 'argon', 'carbon')
    today, before = ranking(FABLE)
    assert today[:3] == ['codex-cli-gpt-6-1-sol', 'opencode-sub-gemini-4-argon', 'codex-cli']
    reads.clear()
    typed, rows = ranking(FABLE, 'coding')
    assert reads == [1]  # once for the whole ranking
    # The type decides only who falls short: Argon, cheap for this type, goes after every route that reaches the
    # class, a route paid per token included. Sol, better at the type than an auditor needs, is not put after Astra
    # for it: "the exact class before one above it" stays a matter of the general class.
    assert typed[0] == 'codex-cli-gpt-6-1-sol' and typed.index('codex-cli-gpt-6-1-sol') < typed.index('codex-cli')
    assert typed.index('openai-api') < typed.index('opencode-sub-gemini-4-argon') < typed.index('gemini-api')
    assert set(typed) == set(today)  # the same auditors, in another order
    argon = rows['opencode-sub-gemini-4-argon']
    assert (argon['kind'], argon['class_source'], argon['classes_short']) == ('coding', 'Board one.csv', 1)
    assert 'kind' not in before['opencode-sub-gemini-4-argon']  # with no type the row is today's
    # A model no board places keeps its general class: Kimi has none, so it counts as cheap, as without a type.
    kimi = rows['openrouter-kimi-k3-1-pro']
    assert (kimi['class_source'], kimi['classes_short']) == ('general', before['openrouter-kimi-k3-1-pro']['classes_short'])


def test_an_auditor_of_the_implementers_model_or_bias_domain_is_never_picked_whatever_its_class(pool, monkeypatch):
    install(pool, 'codex-cli-gpt-6-1-sol', 'cli://codex', 'gpt-6.1-sol')  # the implementer's own model
    install(pool, 'codex-cli', 'cli://codex', 'gpt-6-astra')  # its bias domain
    install(pool, 'claude-opus-5-5-cli', 'cli://claude', 'claude-opus-5-5')
    # Sol, the implementer's model, is the one auditor that reaches the wanted class exactly for this type, beside
    # Argon; Opus, first today, is cheap for it.
    saved(monkeypatch, 'terra', 'carbon', 'sol', 'argon', 'flash', 'haiku', 'opus', 'astra')
    assert ranking('openai-api')[0][0] == 'claude-opus-5-5-cli'
    typed, rows = ranking('openai-api', 'coding')
    assert typed[0] == 'opencode-sub-gemini-4-argon'  # the next independent one
    for own in ('openai-api', 'codex-cli-gpt-6-1-sol', 'codex-cli', 'openai-api-gpt-6-astra'):
        assert own not in typed and rows[own]['reason'] == 'AUDITOR_NOT_INDEPENDENT'
    # The unit's own entry (the review of a change) sees the same.
    picked = conversation_review.available(native_catalog.select('openai-api'), None, 'coding')
    assert [name for name, _ in picked] == typed


def test_another_vendor_still_comes_first_when_the_same_vendor_has_the_better_class_for_the_type(pool, monkeypatch):
    monkeypatch.setattr(native_routes, 'credential_available', lambda key: key in ('OPENAI_API_KEY', 'ANTHROPIC_API_KEY'))
    # Opus (the implementer's vendor) is middling for the type, the class an auditor wants; Astra is above it and Sol
    # falls short.
    saved(monkeypatch, 'terra', 'astra', 'opus', 'argon', 'flash', 'haiku', 'sol', 'carbon')
    typed, rows = ranking(FABLE, 'coding')
    assert typed == ['openai-api-gpt-6-astra', 'openai-api', 'claude-api-opus-5-5']
    assert rows['claude-api-opus-5-5']['reason'] == 'SAME_VENDOR_NOT_INDEPENDENT'
    # Only with the vendor rule off (--any_vendor_auditor) does the class for the type order them all.
    monkeypatch.setattr(turn_engine, 'cross_vendor_first', lambda: False)
    assert ranking(FABLE, 'coding')[0] == ['claude-api-opus-5-5', 'openai-api-gpt-6-astra', 'openai-api']


def test_the_owners_order_list_is_never_reordered(pool, monkeypatch):
    monkeypatch.setattr(turn_engine, 'available', lambda name, **kw: (native_catalog.select(name), None))
    turn_engine.policy_path().parent.mkdir(parents=True, exist_ok=True)
    listed = ['openai-api', 'openai-api-gpt-6-astra', 'claude-api-opus-5-5']
    turn_engine.policy_path().write_text(json.dumps({'auditor': listed}), encoding='utf-8')
    saved(monkeypatch, 'terra', 'astra', 'opus', 'argon', 'flash', 'haiku', 'sol', 'carbon')
    fable = native_catalog.select(FABLE)
    today = [name for name, _ in conversation_review.available(fable)]
    assert today == listed  # another vendor first, then the owner's order
    monkeypatch.setattr(signals, 'boards', not_read)
    assert [name for name, _ in conversation_review.available(fable, None, 'coding')] == today
    assert ranking(FABLE, 'coding')[0] == today


def test_a_pinned_auditor_is_untouched(pin_catalog, monkeypatch):
    monkeypatch.setattr(signals, 'boards', not_read)
    producer = pin_catalog['producer-cli']
    with scope.activated(parse(['--auditor', 'anthropic/test-cli@max'])):
        for picked in (turn_engine.pick_auditors(producer, None, work_type='coding'),
                       conversation_review.available(producer, None, 'coding')):
            assert len(picked) == 1 and scope.pin_matches('anthropic/test-cli@max', picked[0][1])
        chosen, receipt = native_routes.reviewers(producer, work_type='coding')
        assert receipt['reason'] == 'EXACT_TEMPORARY_PIN' and scope.pin_matches('anthropic/test-cli@max', chosen[0])


@pytest.mark.parametrize('producer', [FABLE, 'claude-api-opus-5-5', 'openai-api', 'openai-api-gpt-6-astra', 'gemini-api'])
def test_with_no_saved_boards_the_selection_is_todays(pool, monkeypatch, producer):
    install(pool, 'codex-cli', 'cli://codex', 'gpt-6-astra')
    install(pool, 'claude-opus-5-5-cli', 'cli://claude', 'claude-opus-5-5')
    saved(monkeypatch)
    today, before = ranking(producer)
    typed, rows = ranking(producer, 'coding')
    assert typed == today
    assert all(rows[n]['classes_short'] == before[n]['classes_short'] and rows[n]['class_source'] == 'general'
               for n in today)


def test_the_review_of_a_unit_asks_for_auditors_of_its_type(host, monkeypatch):
    tools, execution, root, _ = host
    intake(host); task = planned(host)
    for args in [('init',), ('add', '.'), ('-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost',
                                         '-c', 'commit.gpgsign=false', 'commit', '-m', 'input')]:
        subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True)
    (root/'a.py').write_bytes(b'value = 2\n')
    asked = []
    monkeypatch.setattr(turn_engine, 'pick_auditors', lambda producer, names, **kw: asked.append(kw) or [])
    for unit in (task['plan']['unit'], {**deepcopy(task['plan']['unit']), 'kind': 'coding'}):
        result = conversation_review.review(tools, task, root, native_catalog.select('qwen38-gsq'), unit,
                                            {'a.py': b'value = 2\n'}, {'after': 'passed'}, execution)
        assert result == {'verdict': 'INCONCLUSIVE', 'reason': conversation_review.NO_AUDITOR}
    assert asked == [{}, {'work_type': 'coding'}]  # a unit with no type asks exactly as before
