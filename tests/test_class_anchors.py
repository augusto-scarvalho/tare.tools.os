"""A model with no line is placed by the known lines of the same reading, its anchors (owner, 2026-11-16). All numbers
here are invented."""
import json

import pytest

from relay import model_signals as signals
from relay import native_catalog, native_routes
from relay.vendor_models import LINES
from test_cross_vendor_auditors import pool  # noqa: F401  (fixture)

OR = 'https://openrouter.ai/api/v1'


def known(line_, version, effort, score):
    return {'line': line_, 'version': version, 'effort': effort, 'intelligence': score, 'coding': None,
            'agentic': None, 'cost_per_task_usd': None}


def other(creator, name, score, effort=None, permaslug=None, cost=None):
    return {'line': None, 'creator': creator, 'name': name, 'effort': effort, 'permaslug': permaslug,
            'intelligence': score, 'coding': None, 'agentic': None, 'cost_per_task_usd': cost}


# Weakest frontier anchor: Astra 66. Weakest middling: Argon 50. Rows that must not count: Argon at a lower effort
# (10), Sol 6 beside the newer Sol 6.1 (5), and the cheap Luna.
ANCHORS = [known('gemini-carbon', '4', 'high', 70), known('gpt-astra', '6', 'max', 66),
           known('gpt-sol', '6.1', 'max', 60), known('gpt-sol', '6', 'max', 5),
           known('gemini-argon', '4', 'high', 50), known('gemini-argon', '4', 'low', 10),
           known('gpt-luna', '6', 'max', 20)]


def reading(rows, before=None, source='openrouter', version='v1'):
    def one(r):
        return {'source': source, 'version': version, 'as_of': '2026-11-16', 'rows': r}
    return {**one(rows), **({'previous': one(before)} if before is not None else {})}


def twice(*rows):
    return reading([*ANCHORS, *rows], [*ANCHORS, *rows])


def route(model, endpoint=OR):
    return {'id': 'fixture', 'native_profile': {'model': model, 'endpoint': endpoint, 'parameters_json': '{}'}}


KIMI = 'moonshotai/kimi-k3.1-pro'


def kimi(score):
    return other('moonshotai', 'Kimi K3.1 Pro', score, permaslug=KIMI+'-20261023')


def test_anchors_are_one_per_line_at_the_newest_version_and_highest_effort():
    found = signals._anchors(reading(ANCHORS))
    assert sorted(found) == sorted([
        (70, 'frontier', LINES['gemini-carbon']['label'].format(version='4')),
        (66, 'frontier', 'GPT-6 Astra'), (60, 'middling', 'GPT-6.1 Sol'), (50, 'middling', 'Gemini 4 Argon'),
        (20, 'cheap', 'GPT-6 Luna')])


def test_a_line_named_at_its_newest_version_only_as_a_variant_gives_no_anchor():
    """Measured 2026-11-16: the source named Opus 5.5 only as "(Max, Default Fallback)", so the clean Opus 5 row was
    taken as the Opus anchor and lowered the middling floor from Argon to an older, weaker model."""
    rows = [*ANCHORS, known('claude-opus', '5', 'max', 30),
            other('anthropic', 'Claude Opus 5.5 (Max, Default Fallback)', 64)]
    assert 'Claude Opus 5' not in {name for _, _, name in signals._anchors(reading(rows))}
    assert signals._place(signals._anchors(reading(rows)), 40) == ('cheap', 'Gemini 4 Argon')
    # another creator's model of a similar name says nothing about the line
    rows[-1] = other('reseller', 'Claude Opus 5.5 (Max, Default Fallback)', 64)
    assert (30, 'middling', 'Claude Opus 5') in signals._anchors(reading(rows))


@pytest.mark.parametrize('score, expected', [
    (70, ('frontier', 'GPT-6 Astra')), (66, ('frontier', 'GPT-6 Astra')),
    (65.9, ('middling', 'Gemini 4 Argon')), (50, ('middling', 'Gemini 4 Argon')),
    (49.9, ('cheap', 'Gemini 4 Argon')), (0, ('cheap', 'Gemini 4 Argon'))])
def test_a_model_is_in_the_highest_class_whose_weakest_anchor_it_reaches(score, expected):
    assert signals._place(signals._anchors(reading(ANCHORS)), score) == expected
    found = signals.klass(route(KIMI), twice(kimi(score)))
    assert (found['class'], found['anchor'], found['from']) == (*expected[:1], expected[1], 'anchors')


def test_a_class_with_no_anchor_cannot_be_entered():
    middling = [r for r in ANCHORS if r['line'] in ('gpt-sol', 'gemini-argon')]
    frontier = [r for r in ANCHORS if r['line'] in ('gpt-astra', 'gemini-carbon')]
    assert signals._place(signals._anchors(reading(middling)), 99)[0] == 'middling'  # nobody in frontier
    assert signals._place(signals._anchors(reading(frontier)), 99) is None  # no middling anchor: nothing is placed
    assert signals._place([], 99) is None
    assert signals.klass(route(KIMI), reading(frontier + [kimi(99)], frontier + [kimi(99)])) is None


def test_the_class_needs_two_agreeing_readings_of_one_source_and_index_version():
    now, before = [*ANCHORS, kimi(55)], [*ANCHORS, kimi(55)]
    assert signals.klass(route(KIMI), reading(now)) is None  # one reading alone
    found = signals.klass(route(KIMI), reading(now, before))
    assert found == {'class': 'middling', 'from': 'anchors', 'source': 'openrouter', 'as_of': '2026-11-16',
                     'intelligence': 55, 'cost_per_task_usd': None, 'anchor': 'Gemini 4 Argon'}
    assert signals.klass(route(KIMI), reading(now, [*ANCHORS, kimi(40)])) is None  # they disagree
    assert signals.klass(route(KIMI), reading(now, [*ANCHORS, kimi(70)])) is None
    assert signals.klass(route(KIMI), {**reading(now, before), 'previous': reading(before, source='arena')}) is None
    assert signals.klass(route(KIMI), {**reading(now, before), 'previous': reading(before, version='v2')}) is None


def test_describe_says_it_in_one_phrase():
    middling = signals.klass(route(KIMI), twice(kimi(55)))
    assert signals.describe(middling) == 'middling: reaches Gemini 4 Argon on openrouter, 2026-11-16'
    assert signals.describe(signals.klass(route(KIMI), twice(kimi(5)))) == \
        'cheap: falls below Gemini 4 Argon on openrouter, 2026-11-16'


@pytest.mark.parametrize('rows', [
    [kimi(55), other('moonshotai', 'Kimi K3.1 Pro', 30, permaslug=KIMI+'-20261101')],  # two dated permaslugs
    [kimi(55), kimi(30)],                                                             # two rows at the top effort
    [other('moonshotai', 'Kimi K3.1 Pro (0902)', 55, permaslug=KIMI+'-20261023')],     # a date the id does not carry
    [other('moonshotai', 'Kimi K3.1 Pro (Reasoning)', 55, permaslug=KIMI+'-20261023')],  # a variant
    [other('moonshotai', 'Kimi K3.1 Pro', 55, permaslug=KIMI+'-0127')],                # a permaslug dated otherwise
    [other('moonshotai', 'Kimi K3.1 Pro Code', 55, permaslug=KIMI+'-code-20261023')]])  # another model
def test_an_ambiguous_dated_or_variant_row_matches_nothing(rows):
    assert signals._match(reading(rows), route(KIMI)) is None
    assert signals.klass(route(KIMI), twice(*rows)) is None


def test_a_variant_row_of_the_same_model_makes_the_plain_row_ambiguous():
    qwen = 'qwen/qwen3.8-max'  # the plain row and its "(0902)" variant, two permaslug dates of one id
    rows = [other('qwen', 'Qwen3.8 Max', 53, permaslug=qwen+'-20260803'),
            other('qwen', 'Qwen3.8 Max (0902)', 33, permaslug=qwen+'-20260902')]
    assert signals._match(reading(rows), route(qwen)) is None
    assert signals._match(reading(rows[:1]), route(qwen))['intelligence'] == 53


def test_an_openrouter_route_matches_by_permaslug_with_or_without_its_date():
    assert signals._match(reading([kimi(55)]), route(KIMI))['intelligence'] == 55
    undated = other('moonshotai', 'Kimi K3.1 Pro', 44, permaslug=KIMI)
    assert signals._match(reading([undated]), route(KIMI))['intelligence'] == 44
    # the name is no way in for an OpenRouter route: another model's permaslug never matches
    assert signals._match(reading([other('x', 'Whatever', 55, permaslug='y/other-model-20261023')]), route(KIMI)) is None


def test_another_route_matches_by_the_rows_name_and_its_vendor_when_it_has_one():
    row = other('moonshotai', 'Kimi K3.1 Pro', 55)  # a source with no permaslug
    saved = reading([row])
    assert signals._match(saved, route(KIMI))['name'] == 'Kimi K3.1 Pro'  # OpenRouter route, source without permaslug
    assert signals._match(saved, route('kimi-k3.1-pro', 'https://gateway.example/v1'))['name'] == 'Kimi K3.1 Pro'
    assert signals._match(saved, route('other-vendor/kimi-k3.1-pro', 'https://gateway.example/v1')) is None
    assert signals._match(saved, route('kimi-k3.1', 'https://gateway.example/v1')) is None
    assert signals._match(saved, route('kimi-k3.1-pro-thinking', 'https://gateway.example/v1')) is None


def test_a_name_two_creators_claim_matches_nothing_unless_the_route_names_its_vendor():
    saved = reading([other('moonshotai', 'Kimi K3.1 Pro', 55), other('someone', 'Kimi K3.1 Pro', 30)])
    assert signals._match(saved, route('kimi-k3.1-pro', 'https://gateway.example/v1')) is None
    assert signals._match(saved, route('someone/kimi-k3.1-pro', 'https://gateway.example/v1'))['intelligence'] == 30


def test_the_highest_effort_the_reading_lists_is_taken():
    rows = [other('moonshotai', 'Kimi K3.1 Pro', 30, 'low'), other('moonshotai', 'Kimi K3.1 Pro', 55, 'high'),
            other('moonshotai', 'Kimi K3.1 Pro', 40, 'medium')]
    assert signals._match(reading(rows), route('kimi-k3.1-pro', 'https://gateway.example/v1'))['intelligence'] == 55
    assert signals._match(reading([other('moonshotai', 'Kimi K3.1 Pro', 9), *rows]),
                          route('kimi-k3.1-pro', 'https://gateway.example/v1'))['intelligence'] == 55  # no effort: lowest


def test_the_sources_keep_the_rows_of_models_with_no_line():
    body = {'meta': {'as_of': '2026-11-16T00:00:00Z', 'version': 'v1'}, 'data': [
        {'model_permaslug': 'openai/gpt-6.1-sol-20260929', 'display_name': 'GPT-6.1 Sol (Max)', 'intelligence_index': 60},
        {'model_permaslug': KIMI+'-20261023', 'display_name': 'Kimi K3.1 Pro (High)', 'intelligence_index': 46.5,
         'coding_index': 61, 'agentic_index': 33},
        {'model_permaslug': 'qwen/qwen3.8-max-20260902', 'display_name': 'Qwen3.8 Max (0902)', 'intelligence_index': 33},
        {'model_permaslug': 'x/no-score', 'display_name': 'No Score', 'intelligence_index': None}]}
    rows = signals.parse_openrouter(body)[0]
    assert [r['line'] for r in rows] == ['gpt-sol', None, None]
    assert rows[1] == other('moonshotai', 'Kimi K3.1 Pro', 46.5, 'high', KIMI+'-20261023') | {'coding': 61, 'agentic': 33}
    assert (rows[2]['name'], rows[2]['effort']) == ('Qwen3.8 Max (0902)', None)  # the date stays in the name
    arena = {'rows': [{'row': {'model_name': 'Grok 4.7', 'organization': 'xai', 'score': 0.1, 'cost_per_task_p50': 2,
                               'leaderboard_publish_date': '2026-11-11'}}]}
    assert signals.parse_arena(arena)[0] == [other('xai', 'Grok 4.7', 0.1, cost=2)]


def test_a_reading_saved_before_this_change_still_loads_and_places_no_such_model():
    path = signals.reading_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    old = {'schema': signals.SCHEMA, 'source': 'openrouter', 'citation': 'c', 'as_of': '2026-11-14', 'version': 'v1',
           'read_at': 1794628000.0, 'rows': ANCHORS}
    path.write_text(json.dumps({**old, 'previous': old}), encoding='utf-8')
    saved = signals.reading()
    assert saved and signals.klass(route(KIMI)) is None
    assert signals.klass(route('gemini-4-argon'), saved)['from'] == 'line'


def test_a_model_with_a_line_keeps_its_class_whatever_the_rows_of_others():
    argon = {'id': 'x', 'native_profile': {'model': 'gemini-4-argon', 'endpoint': 'cli://agy',
                                           'parameters_json': json.dumps({'reasoning_effort': 'high'})}}
    assert signals.klass(argon, twice())['from'] == 'reading'
    assert signals.klass(argon, twice(kimi(99))) == signals.klass(argon, twice())


def test_the_shipped_table_ranks_a_placed_route_as_a_line_route_of_its_class_and_the_unplaced_as_before(pool, monkeypatch):
    producer = native_catalog.select('openai-api-gpt-6-astra')

    def decisions(saved):
        monkeypatch.setattr(signals, 'reading', lambda: saved)
        found, receipt = native_routes.reviewers(producer, 'auditor', check_resources=False)
        return {r['deployment']: r for r in receipt['candidates'] if r['eligible']}
    without = decisions(twice())
    placed = decisions(twice(kimi(55)))  # Kimi reaches Argon: middling, the class the auditor wants
    kimi_route, opus, muse = 'openrouter-kimi-k3-1-pro', 'claude-api-opus-5-5', 'openrouter-muse-wonder'
    assert without[kimi_route]['classes_short'] == 1
    assert placed[kimi_route]['classes_short'] == 0 == placed[opus]['classes_short']
    assert placed[kimi_route]['position'] < without[kimi_route]['position']
    # Muse has no row in the reading: it ranks as today, a model with no class counted as cheap
    assert placed[muse]['classes_short'] == without[muse]['classes_short'] == 1
