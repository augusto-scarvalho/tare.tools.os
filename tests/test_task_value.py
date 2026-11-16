"""Pay-per-token routes are ordered by value, the score per dollar of a task on the LMArena agent board (owner,
2026-11-16). Every number here is invented."""
import json

import pytest

from relay import model_signals as signals
from relay import native_catalog, native_routes
from test_class_anchors import KIMI, known, other, route
from test_cross_vendor_auditors import pool  # noqa: F401  (fixture)
from test_tiers_by_class import everything, ranked_names  # noqa: F401  (fixture)

NOW = 1794628000.0  # 2026-11-14 UTC
DAY = 86400
OPENROUTER = {'meta': {'as_of': '2026-11-15T00:00:00Z', 'version': 'v1'}, 'data': [
    {'model_permaslug': 'openai/gpt-6.1-sol-20260929', 'display_name': 'GPT-6.1 Sol (Max)', 'intelligence_index': 60}]}


def board_body(day, *items):
    """The Hugging Face answer's shape, from (name, organization, score, cost)."""
    return {'rows': [{'row': {'model_name': n, 'organization': o, 'score': s, 'cost_per_task_p50': c,
                              'output_tokens_per_task_p50': 1000.0, 'leaderboard_publish_date': day}}
                     for n, o, s, c in items]}


FIRST = board_body('2026-11-14', ('GPT-6.1 Sol (Max)', 'openai', 0.1, 1.0), ('Kimi K3.1 Pro (Max)', 'moonshot', 0.05, 2.0))
SECOND = board_body('2026-11-15', ('GPT-6.1 Sol (Max)', 'openai', 0.12, 1.5))


def served(openrouter, arena, calls):
    def fetch(source, key):
        if source in ('openrouter', 'arena'):  # the task-type boards are read after these and are not this test's
            calls.append((source, key))
        body = {'openrouter': openrouter, 'arena': arena}[source]
        if isinstance(body, Exception):
            raise body
        return body
    return fetch


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setattr('relay.provider_quota.secret', lambda name: 'key')


def test_the_board_is_read_beside_the_class_reading_once_a_day_and_a_failed_read_keeps_the_last(keyed):
    calls = []
    first = signals.refresh_reading(NOW, fetch=served(OPENROUTER, FIRST, calls))
    assert first['source'] == 'openrouter' and calls == [('openrouter', 'key'), ('arena', None)]
    assert first['board']['as_of'] == '2026-11-14' and len(first['board']['rows']) == 2
    signals.refresh_reading(NOW + 7200, fetch=served(OPENROUTER, SECOND, calls))
    assert len(calls) == 2  # once a day, the two reads together
    # the class source answers, the board does not: the last board stays; the reading before keeps no board of its own
    later = signals.refresh_reading(NOW + 2*DAY, fetch=served(OPENROUTER, OSError('offline'), calls))
    assert later['board'] == first['board'] and 'board' not in later['previous']
    # and an arena reshaped as nobody foresaw is no board either
    again = signals.refresh_reading(NOW + 4*DAY, fetch=served(OPENROUTER, {'rows': 'x'}, calls))
    assert again['board'] == first['board']
    last = signals.refresh_reading(NOW + 6*DAY, fetch=served(OPENROUTER, SECOND, calls))
    assert last['board']['as_of'] == '2026-11-15' and len(last['board']['rows']) == 1


def test_with_the_arena_as_the_class_source_it_is_read_once_and_is_the_board(monkeypatch):
    monkeypatch.setattr('relay.provider_quota.secret', lambda name: None)
    calls = []
    saved = signals.refresh_reading(NOW, fetch=served(OPENROUTER, FIRST, calls))
    assert (saved['source'], calls) == ('arena', [('arena', None)]) and 'board' not in saved
    assert signals._board(saved) == {'as_of': '2026-11-14', 'rows': saved['rows']}
    # a reading saved before this change, or with a damaged board, has none
    assert signals._board({'source': 'openrouter', 'rows': []}) is None
    assert signals._board({'source': 'openrouter', 'board': {'rows': 'x'}}) is None and signals._board(None) is None


def row(line_, version, effort, score, cost):
    return {**known(line_, version, effort, score), 'cost_per_task_usd': cost}


def saved_with(*rows, **more):
    """A reading whose class source is OpenRouter (numbers that must never be mixed in) and a board of `rows`."""
    return {'source': 'openrouter', 'as_of': '2026-11-14', 'version': 'v1',
            'rows': [row('gemini-argon', '4', 'high', 99, None), row('gemini-argon', '4', 'max', 99, None)],
            'board': {'as_of': '2026-11-15', 'rows': [row('gemini-hydrogen', '4', 'low', -0.2, 0.1), *rows]}, **more}


def argon(effort):
    return {'id': 'x', 'native_profile': {'model': 'gemini-4-argon', 'endpoint': 'https://api.example/v1',
                                          'parameters_json': json.dumps({'reasoning_effort': effort})}}


def test_the_value_comes_from_one_row_of_the_board_and_never_from_the_class_reading():
    saved = saved_with(row('gemini-argon', '4', 'high', 0.1, 0.5))
    assert signals.task(argon('high'), saved) == {
        'score': 0.1, 'cost_per_task_usd': 0.5, 'value': pytest.approx(0.3/0.5), 'source': 'arena',
        'as_of': '2026-11-15', 'effort': 'high'}  # the class reading's 99 is not the score
    assert signals.task(argon('high'), {**saved, 'board': None}) is None  # no board: no figure, whatever else is read
    for score, cost in ((None, 0.5), (0.1, None), (0.1, 0), (0.1, float('nan'))):
        assert signals.task(argon('high'), saved_with(row('gemini-argon', '4', 'high', score, cost))) is None
    twice = saved_with(row('gemini-argon', '4', 'high', 0.1, 0.5), row('gemini-argon', '4', 'high', 0.2, 0.4))
    assert signals.task(argon('high'), twice) is None  # two rows of one model: neither


def test_a_line_model_matches_at_its_own_effort_else_the_highest_listed():
    saved = saved_with(row('gemini-argon', '4', 'low', 0.0, 0.1), row('gemini-argon', '4', 'high', 0.1, 0.5),
                       row('gemini-argon', '4', 'max', 0.2, 2.0), row('gemini-argon', '3', 'xhigh', 0.3, 0.1))
    assert signals.task(argon('low'), saved)['cost_per_task_usd'] == 0.1
    assert signals.task(argon('high'), saved)['effort'] == 'high'
    found = signals.task(argon('medium'), saved)  # not listed: the highest listed, and it says which
    assert (found['effort'], found['cost_per_task_usd']) == ('max', 2.0)  # not the other version's xhigh
    assert signals.task(argon('max'), saved)['effort'] == 'max'


def test_a_model_with_no_line_goes_through_the_match_and_a_route_the_board_does_not_name_has_none():
    kimi = other('moonshotai', 'Kimi K3.1 Pro', 0.05, 'max', cost=2.0)
    saved = saved_with(kimi)
    found = signals.task(route(KIMI), saved)
    assert (found['cost_per_task_usd'], found['effort'], found['value']) == (2.0, 'max', pytest.approx(0.25/2.0))
    assert signals.task(route('moonshotai/kimi-k3.1', 'https://gateway.example/v1'), saved) is None
    assert signals.task(route(KIMI), saved_with(kimi, {**kimi, 'cost_per_task_usd': 3.0})) is None  # two rows at the top effort


def test_describe_task_says_the_figure_its_source_and_its_date_or_nothing():
    found = signals.task(argon('medium'), saved_with(row('gemini-argon', '4', 'max', 0.2, 0.144)))
    assert signals.describe_task(found) == 'about US$ 0.14 per task at max effort on the LMArena agent board, 2026-11-15'
    assert signals.describe_task({**found, 'effort': None}).startswith('about US$ 0.14 per task on the LMArena')
    assert signals.describe_task(None) == ''


# Shipped metered routes of the implementer table, none with a line: board rows named as each one's id names it.
GLM, MIMO, FLASH = 'openrouter-glm-5-3', 'openrouter-mimo-v2-6-pro', 'openrouter-deepseek-v4-1-flash'
BOARD = saved_with(other('z-ai', 'GLM 5.3', 0.1, cost=1.0), other('xiaomi', 'MiMo V2.6 Pro', 0.1, cost=0.2),
                   other('deepseek', 'DeepSeek V4.1 Flash', 0.05, cost=0.5))


def metered(name):
    row_ = next(r for r in native_routes.configured()['deployments'] if r.get('native_catalog', r['id']) == name)
    return row_['cost_tier'] == 'metered'


def orders(monkeypatch, role, producer=None):
    """(before, after) the board is read: the names `ranked` gives, from the same machine."""
    def names():
        chosen, selection = native_routes.ranked(role, producer)
        by_id = {c['selected_deployment']: c['catalog'] for c in selection['candidates'] if c.get('eligible')}
        return [by_id[d['id']] for d in chosen]
    monkeypatch.setattr(signals, 'reading', lambda: None)
    before = names()
    monkeypatch.setattr(signals, 'reading', lambda: BOARD)
    return before, names()


def test_among_metered_routes_the_better_value_goes_first_and_a_route_with_no_figure_keeps_its_place(everything, monkeypatch):
    before, after = orders(monkeypatch, 'implementer')
    figures = [n for n in after if n in (GLM, MIMO, FLASH)]
    assert figures == [MIMO, FLASH, GLM]  # 1.5, 0.5 and 0.3 per dollar
    rest = [n for n in after if metered(n) and n not in figures]
    assert rest == [n for n in before if metered(n) and n not in figures]  # the others keep today's order
    # They tie before the cost tier with the figured ones here (no class, a pay-per-token route): after them.
    unclassed = [n for n in rest if signals.klass(native_catalog.select(n)) is None]
    assert unclassed and all(after.index(n) > after.index(GLM) for n in unclassed)


@pytest.mark.parametrize('role', ['implementer', 'coordinator', 'planner', 'auditor', 'plan-auditor'])
def test_nothing_else_moves_free_flat_and_free_credit_routes_the_planner_and_the_candidates(everything, monkeypatch, role):
    producer = native_catalog.select('openai-api-gpt-6-astra') if role.endswith('auditor') else None
    before, after = orders(monkeypatch, role, producer)
    assert sorted(before) == sorted(after)  # the same candidates
    # Every slot of a route that is not metered holds the same route; metered routes only trade metered slots.
    assert all(after[i] == n for i, n in enumerate(before) if not metered(n))
    assert all(metered(after[i]) for i, n in enumerate(before) if metered(n))
    if role == 'planner':
        assert before == after


def test_tare_models_says_the_cost_of_a_task_with_its_source_and_date(everything, capsys, monkeypatch):
    from relay import model_choices
    monkeypatch.setattr(signals, 'reading', lambda: {**saved_with(row('claude-opus', '5.5', 'max', 0.2, 5.0)),
                                                     'citation': 'Source: fixture'})
    model_choices.main(['--vendor', 'Anthropic', '--include-unconfigured'])
    out = capsys.readouterr().out
    assert set(l for l in out.splitlines() if l.startswith('  Cost: ')) == {
        '  Cost: about US$ 5.00 per task at max effort on the LMArena agent board, 2026-11-15'}
    assert 'Source: LMArena agent leaderboard, CC BY 4.0' in out and "board's own figure, not a measurement by tare" in out


def test_the_matrix_status_line_says_it_too(pool, tmp_path, monkeypatch):
    from test_console_route_matrix import install, journey
    install(pool, 'claude-opus-5-5-cli', 'cli://claude', 'claude-opus-5-5')
    monkeypatch.setattr(signals, 'reading', lambda: saved_with(row('claude-opus', '5.5', 'max', 0.2, 5.0)))

    async def steps(app, pilot, panel, cell, status):
        await cell('claude/opus', 'claude-code')
        assert 'about US$ 5.00 per task at max effort on the LMArena agent board, 2026-11-15' in status()
    journey(tmp_path, steps, role='implementer')
