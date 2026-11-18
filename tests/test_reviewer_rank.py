"""A model may review another's work unless it is two categories or more below it, on two scales: the hosted classes,
and five bands among the consumer open-weight models of a board (owner, 2026-11-18). Offline: the boards, the general
reading and the consumer list are fixtures, and every score here is invented."""
import json
from pathlib import Path

import pytest

from relay import model_signals as signals
from relay import native_catalog, native_plan, native_routes, work_audit

CODING = signals.task_type('coding')['sources']


def hosted(name):
    return {'id': name, 'model': name, 'native_profile': {'model': 'acme/'+name, 'endpoint': 'https://api.example/v1'}}


def local(name):
    return {'id': name, 'model': name, 'native_profile': {'model': name, 'endpoint': 'http://127.0.0.1:8080/v1'}}


def row(name, score, creator='acme', line=None):
    return {'line': line, 'name': name, 'creator': creator, 'effort': None, 'permaslug': None, 'intelligence': score}


def kept(*rows, at=0):
    return {signals.source_key(CODING[at]): {'rows': list(rows)}}


TEN = [row(f'm{i}', i/10) for i in range(10)]  # ten consumer models a tenth apart: two to a band (m8 and m9 are band 4)


@pytest.fixture
def world(monkeypatch):
    """{deployment id: its general class}; the ten consumer models m0..m9; no general reading."""
    classes = {}
    monkeypatch.setattr(signals, 'klass', lambda deployment, saved=None: classes.get(deployment['id']))
    monkeypatch.setattr(signals, 'CONSUMER', {f'm{i}': f'm{i}' for i in range(10)})
    monkeypatch.setattr(signals, 'reading', lambda: None)
    return classes


def test_hosted_models_compare_their_classes_and_one_below_reviews(world):
    world.update(fable={'class': 'frontier'}, astra={'class': 'frontier'}, opus={'class': 'middling'}, haiku={'class': 'cheap'})
    may = lambda reviewer, author: signals.may_review(hosted(reviewer), hosted(author))
    assert may('opus', 'fable') and may('astra', 'fable') and may('fable', 'fable') and may('fable', 'haiku')
    assert not may('haiku', 'fable') and may('haiku', 'opus')


def test_a_hosted_reviewer_is_never_refused_for_lacking_a_board_row(monkeypatch):
    placed = {('astra', 'coding'): 'middling', ('opus', 'coding'): 'cheap'}  # what the coding board places
    general = {'fable': 'frontier', 'astra': 'frontier', 'opus': 'middling', 'kimi': 'cheap'}  # Kimi: no class at all
    monkeypatch.setattr(signals, 'judged', lambda d, kind=None, kept=None: (
        {'class': placed[d['id'], kind], 'source': 'Board'} if (d['id'], kind) in placed
        else {'class': general[d['id']], 'source': 'general'}))
    may = lambda reviewer, author: signals.may_review(hosted(reviewer), hosted(author), 'coding', {})
    assert may('fable', 'astra') and may('kimi', 'astra')  # no row: their general classes, frontier and cheap
    assert not may('kimi', 'fable')  # cheap two classes below frontier
    assert may('opus', 'fable')  # the board places the reviewer only: Fable has no place for the type


def test_local_models_take_five_bands_among_the_consumer_models_of_the_board_alone(world):
    # Closed models on the board count for nothing: with these two the ten would split 12 ways and m5 would be one band
    # below m9.
    board = kept(*TEN, row('Closed Big', 5.0, 'bigco'), row('gpt-sol', 9.0, 'OpenAI', line='gpt-sol'))
    may = lambda reviewer, author: signals.may_review(local(reviewer), local(author), 'coding', board)
    assert may('m6', 'm9') and may('m8', 'm9') and may('m9', 'm9') and may('m0', 'm3') and may('m9', 'm0')
    assert not may('m5', 'm9') and not may('m0', 'm4')
    assert not signals.may_review(local('m5'), local('m9'), ['coding', 'games'], board)  # a demand's types: every one


def test_across_the_scales_a_local_model_counts_as_basic(world):
    world.update(sol={'class': 'middling'}, fable={'class': 'frontier'}, luna={'class': 'cheap'})
    board = kept(*TEN)
    assert signals.may_review(local('m0'), hosted('sol'), 'coding', board)
    assert not signals.may_review(local('m9'), hosted('fable'), 'coding', board)
    assert signals.may_review(hosted('luna'), local('m9'), 'coding', board)  # a hosted reviewer of local work is above


def test_an_author_the_board_places_is_never_reviewed_by_a_model_it_does_not(world):
    board = kept(*TEN)
    assert not signals.may_review(local('generic-gemma'), local('m0'), 'coding', board)  # a generic local model
    assert signals.may_review(local('generic-gemma'), local('unplaced'), 'coding', board)  # neither: no restriction
    assert signals.may_review(local('m0'), local('unplaced'), 'coding', board)
    # Compared on the first of the type's boards that places both.
    two = {**kept(row('m9', .9)), **kept(*TEN, at=1)}
    assert signals.may_review(local('m8'), local('m9'), 'coding', two)
    assert not signals.may_review(local('m4'), local('m9'), 'coding', two)


def test_the_dimension_is_the_units_type_and_the_general_reading_only_for_a_type_with_no_board(world, monkeypatch):
    monkeypatch.setattr(signals, 'reading', lambda: {'rows': [row('m0', 9.0), row('m9', .05), *TEN[1:9]]})
    board = kept(*TEN)  # m0 two bands below m9 at coding, above it in general
    assert not signals.may_review(local('m0'), local('m9'), 'coding', board)
    assert signals.may_review(local('m0'), local('m9')) and signals.may_review(local('m0'), local('m9'), 'games', board)
    assert signals.may_review(local('m0'), local('m9'), 'coding', kept(row('m1', .1)))  # a board that places neither


def qwen():
    """A coding board in the boards' own spellings: ten models of the shipped list (Qwen3.8 27B twice, counted once at
    its best) beside a closed model. Ascending, two to a band: Qwen3.6 35B-A3B is band 1, Qwen3.6 27B 2, Qwen3.8 27B 3
    and Flash-Next 4. The scores are invented."""
    return kept(row('qwen3.5-0.8b', .05), row('qwen3-8b', .15), row('Llama-3.3-70B-Instruct', .20, 'Meta'),
                row('qwen3.6-35b-a3b_none', .30), row('Gemma 4 26B', .35, 'Google'), row('qwen3.6-27b', .40),
                row('Qwen 3.8 27B', .45), row('Qwen3.8-27B', .50), row('gpt-oss-120b', .52, 'OpenAI'),
                row('Qwen3.8-Flash-Next', .55), row('qwen3.5-122b-a10b_none', .60), row('Claude Opus', .9, 'Anthropic'))


def test_the_shipped_local_models_are_ranked_by_their_base_models_among_the_listed_ones(monkeypatch):
    monkeypatch.setattr(signals, 'reading', lambda: None)
    moe, fable, gsq, swift = (native_catalog.select(name) for name in ('qwen36-moe', 'fable-tc', 'qwen38-gsq', 'swift-next'))
    assert signals.measured_as(fable) == ('qwen3.6-27b', 'fine-tune')
    assert [signals._band(qwen()[signals.source_key(CODING[0])]['rows'], d) for d in (moe, fable, gsq, swift)] == [1, 2, 3, 4]
    may = lambda reviewer, author: signals.may_review(reviewer, author, 'coding', qwen())
    assert not may(moe, gsq) and not may(moe, swift) and not may(fable, swift)
    # "as long as it audits something below it": Qwen3.6 MoE audits the models one band above it, or below
    assert may(moe, fable) and may(fable, gsq) and may(gsq, swift) and may(swift, moe)
    # A generic local model is placed only by a list entry the board holds (its own id here).
    assert may(local('llama-3.3-70b-instruct'), moe) and not may(local('gemma-4-31b-it'), moe)


def test_a_board_with_fewer_than_ten_consumer_models_places_none(monkeypatch):
    # The installed Arena agent board's three (2026-11-18): their fifths were 0, 1 and 3, and Qwen3.8 27B, a hair below
    # Flash-Next, could not review it.
    monkeypatch.setattr(signals, 'reading', lambda: None)
    moe, gsq, swift = (native_catalog.select(name) for name in ('qwen36-moe', 'qwen38-gsq', 'swift-next'))
    sparse = kept(row('NVIDIA-Nemotron-3.5-Super-VL-120B-A12B (NVFP4)', -0.09), row('Qwen3.8-27B', -0.056),
                  row('Qwen3.8-Flash-Next', -0.052))
    assert signals._band(sparse[signals.source_key(CODING[0])]['rows'], swift) is None
    assert signals.may_review(gsq, swift, 'coding', sparse) and signals.may_review(moe, swift, 'coding', sparse)
    # Beside it, a board of the type with ten or more still bands: Qwen3.6 MoE (1) is two below Flash-Next (4) there.
    both = {**sparse, **kept(*qwen()[signals.source_key(CODING[0])]['rows'], at=1)}
    assert signals.may_review(gsq, swift, 'coding', both) and not signals.may_review(moe, swift, 'coding', both)


def test_admission_a_pinned_reviewer_and_a_works_list_refuse_a_pair_two_categories_apart(monkeypatch):
    monkeypatch.setattr(signals, 'reading', lambda: None)
    moe, gsq = native_catalog.select('qwen36-moe'), native_catalog.select('qwen38-gsq')
    line = {'id': 'qwen36-moe', 'native_catalog': 'qwen36-moe', 'model': moe['model'], 'roles': ['auditor', 'plan-auditor'],
            'runner': 'native-http', 'kind': 'primary', 'cost_tier': 'free'}
    author = work_audit.identity(gsq, 'implementer')
    for role in ('auditor', 'plan-auditor'):
        assert native_routes.admission(line, role, author, producer=gsq, work_type='coding', kept=qwen())[2] == 'REVIEWER_BELOW_AUTHOR'
        assert native_routes.admission(line, role, author)[2] != 'REVIEWER_BELOW_AUTHOR'  # no producer, no pair
    monkeypatch.setattr(signals, 'boards', qwen)
    with pytest.raises(ValueError, match='REVIEWER_BELOW_AUTHOR'):
        native_plan.select_route(moe, 'plan-auditor', 'PINNED', gsq, work_type='coding')
    assert native_plan.select_route(gsq, 'plan-auditor', 'PINNED', moe, work_type='coding')[0] is gsq
    # A Work's explicit auditor list has no type: the general reading.
    monkeypatch.setattr(signals, 'reading', lambda: {'rows': qwen()[signals.source_key(CODING[0])]['rows']})
    (audit,) = work_audit.prepare(gsq, [moe], 'optional')['routes']
    assert (audit['eligible'], audit['reason']) == (False, 'REVIEWER_BELOW_AUTHOR')


def test_an_exact_pin_and_the_owners_auditor_list_are_checked_for_the_type(monkeypatch):
    from relay import execution_scope as scope, model_choices, turn_engine
    monkeypatch.setattr(signals, 'reading', lambda: None)  # in general nothing places them: no restriction
    monkeypatch.setattr(signals, 'boards', qwen)
    moe, gsq, swift = (native_catalog.select(name) for name in ('qwen36-moe', 'qwen38-gsq', 'swift-next'))
    routes = {'qwen36-moe': moe, 'swift-next': swift}
    monkeypatch.setattr(turn_engine, 'available', lambda name, **kw: (routes[name], None))
    # The owner's list keeps its order and loses only the auditor two bands below Qwen3.8 at coding.
    listed = lambda work_type=None: [n for n, _ in turn_engine.pick_auditors(gsq, ['qwen36-moe', 'swift-next'], probe=False,
                                                                             work_type=work_type)]
    assert listed() == ['qwen36-moe', 'swift-next'] and listed('coding') == ['swift-next']
    monkeypatch.setattr(model_choices, 'select_exact', lambda *a, **kw: {**moe, 'native_model_choice': {'connection': 'qwen36-moe'}})
    monkeypatch.setattr(scope, 'pin_matches', lambda raw, value: True)
    with scope.activated(scope.Scope(pins=(('auditor', 'ninfer/qwen36-moe@xhigh'),))):
        assert scope.pinned('auditor', gsq) is moe
        with pytest.raises(ValueError, match='REVIEWER_BELOW_AUTHOR'):
            scope.pinned('auditor', gsq, 'coding')
        with pytest.raises(ValueError, match='REVIEWER_BELOW_AUTHOR'):
            turn_engine.pick_auditors(gsq, None, work_type=['coding'])


def resident(m, at='gb_64k'):
    """What must fit the 160 GB: weights at Q4_K_M and the 8-bit KV, less the tables a cited source keeps on SSD."""
    return m[at] - (m['ssd_tables_gb'] if m.get('ssd_tables_gb') and m.get('ssd_tables_source') else 0)


def test_tables_kept_on_ssd_count_out_of_the_resident_memory_only_with_a_source():
    engram = {'gb_64k': 180, 'ssd_tables_gb': 60, 'ssd_tables_source': 'https://example.test/card'}
    assert resident(engram) == 120 <= 160
    assert resident({**engram, 'ssd_tables_source': None}) == 180 > 160  # no source: everything counts


def test_every_listed_model_runs_on_a_consumer_machine_by_the_documented_formula():
    listed = json.loads(Path(signals.__file__).with_name('consumer_models.json').read_text(encoding='utf-8'))['models']
    names = [name for m in listed for name in (m['id'], *m.get('aliases', ()))]
    assert len(names) == len(set(names)) == len(signals.CONSUMER)  # one row per base model, each spelling once
    assert all(signals.CONSUMER[a] == m['id'] and a == a.lower() and ' ' not in a for m in listed for a in m.get('aliases', ()))
    for m in listed:
        assert m['vendor'] and m['generation'] and m['architecture'] in ('dense', 'moe') and m['source']
        if 'measured' in m:  # no public parameter count: Q4_K_M bounded from a run at lower precision, never that run
            seen = m['measured']
            assert m['total_b'] is None and seen['hardware'] and seen['bits_per_weight'] < 4.85
            bound = round((seen['vram_gb'] + seen['ram_gb']) * 4.85 / seen['bits_per_weight'], 1)
            assert m['gb_64k'] == m['gb_128k'] == bound and resident(m) <= 160
            continue
        weights, a = m['total_b'] * 4.85 / 8, m['attention']
        per_token = 2 * a['full_layers'] * a['kv_heads'] * a['head_dim'] / 1e9  # 8-bit K and V
        assert (m['gb_64k'], m['gb_128k']) == (round(weights + per_token*65536, 2), round(weights + per_token*131072, 2))
        assert resident(m) <= 160 and m['source'].startswith('https://huggingface.co/')


def test_a_pinned_reviewer_must_still_attest_its_identity():
    # The rank rule does not replace identity: a reviewer profile whose model and wire id disagree is refused.
    from copy import deepcopy
    from relay import landing_policy, native_catalog, native_plan
    reviewer = deepcopy(native_catalog.select('swift-next'))
    reviewer['model'] = 'qwen-3.8-27b'  # a record whose runtime is qwen38-gsq, not swift-next
    with pytest.raises(landing_policy.LandingPolicyRejected):
        native_plan.select_route(reviewer, 'plan-auditor', 'PINNED', native_catalog.select('qwen38-gsq'))
