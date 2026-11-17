"""Model signals: matching a source's names to model lines, the saved reading, and the class of a route."""
import json
from pathlib import Path

import pytest

from relay import model_signals as signals
from relay.model_choices import EFFORTS
from relay.vendor_models import LINES, line

FIXTURES = Path(__file__).resolve().parent/'fixtures'
# Trimmed excerpts of one real read of each source on 2026-11-14 (no key or account data in them).
OPENROUTER = json.loads((FIXTURES/'model_signals_openrouter_20261114.json').read_text(encoding='utf-8'))
ARENA = json.loads((FIXTURES/'model_signals_arena_20261114.json').read_text(encoding='utf-8'))
NOW = 1794628000.0  # 2026-11-14 UTC
DAY = 86400


def route(model, **parameters):
    return {'id': 'fixture', 'native_profile': {'model': model, 'parameters_json': json.dumps(parameters)}}


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setattr('relay.provider_quota.secret', lambda name: 'key')


def serve(body):
    return lambda source, key: body[source]


def test_every_known_line_matches_once_at_every_effort():
    for name, row in LINES.items():
        for effort in EFFORTS:
            found = signals.normalise(row['label'].format(version='6.1')+f' ({effort.capitalize()})', row['vendor'])
            assert found == (name, '6.1', effort)
    # the bracket may be lower case, as Arena writes xhigh
    assert signals.normalise('GPT-6.1 Sol (xhigh)', 'openai') == ('gpt-sol', '6.1', 'xhigh')


@pytest.mark.parametrize('name, creator', [
    ('GPT-6 Sol (Daybreak Blue, max)', 'openai'),         # a variant, though it names an effort
    ('Claude Fable 5.2 (Max, Default Fallback)', 'anthropic'),
    ('GPT-6.1 Sol', 'openai'),                            # no effort
    ('GPT-6.1 Sol (Non-reasoning)', 'openai'),
    ('GPT-6.1 Sol Pro (Max)', 'openai'),                  # another line
    ('GPT-6.1 Sol (Max)', 'google'),                      # not its vendor
    ('GPT-7 Nova (Max)', 'openai'), ('Hy4', None), (None, 'openai')])
def test_a_name_that_is_not_exactly_one_known_line_version_and_effort_is_refused(name, creator):
    assert signals.normalise(name, creator) is None


def test_two_rows_for_one_line_version_and_effort_are_both_refused():
    twice = {'meta': OPENROUTER['meta'], 'data': [
        {'model_permaslug': 'openai/gpt-6.1-sol-20260929', 'display_name': 'GPT-6.1 Sol (Max)', 'intelligence_index': 54.6},
        {'model_permaslug': 'openai/gpt-6.1-sol-20261101', 'display_name': 'GPT-6.1 Sol (Max)', 'intelligence_index': 40},
        {'model_permaslug': 'openai/gpt-6.1-sol-20261101', 'display_name': 'GPT-6.1 Sol (High)', 'intelligence_index': 50}]}
    assert [r['effort'] for r in signals.parse_openrouter(twice)[0]] == ['high']


def test_the_real_excerpts_parse_to_rows_with_the_sources_date_and_numbers():
    rows, as_of, version = signals.parse_openrouter(OPENROUTER)
    assert (as_of, version) == ('2026-11-14', 'v1')
    argon = next(r for r in rows if (r['line'], r['effort']) == ('gemini-argon', 'high'))
    assert argon == {'line': 'gemini-argon', 'version': '4', 'effort': 'high', 'intelligence': 50.4, 'coding': None,
                     'agentic': None, 'cost_per_task_usd': None}
    assert not any(r['line'] == 'claude-fable' for r in rows)  # "(Max, Default Fallback)" is not one effort
    rows, as_of, version = signals.parse_arena(ARENA)
    assert (as_of, version) == ('2026-11-11', None)
    fable = next(r for r in rows if (r['line'], r['version']) == ('claude-fable', '5.2'))
    assert fable['effort'] == 'max' and fable['cost_per_task_usd'] == pytest.approx(7.43, abs=0.01)
    assert signals.parse_arena({'rows': 'x'}) is None and signals.parse_openrouter([]) is None


def test_openrouter_is_read_first_with_the_key_else_the_keyless_dataset(monkeypatch):
    calls = []
    body = {'openrouter': OPENROUTER, 'arena': ARENA}

    def fetch(source, key):
        if source in body:  # the task-type boards are read after these (tests/test_task_types.py)
            calls.append((source, key))
        return body[source]
    monkeypatch.setattr('relay.provider_quota.secret', lambda name: None)
    assert signals.refresh_reading(NOW, fetch=fetch)['source'] == 'arena' and calls == [('arena', None)]
    monkeypatch.setattr('relay.provider_quota.secret', lambda name: 'key')
    calls.clear()
    value = signals.refresh_reading(NOW + 2*DAY, fetch=fetch)
    assert (value['source'], calls) == ('openrouter', [('openrouter', 'key'), ('arena', None)])  # and the board
    assert value['previous']['source'] == 'arena' and signals.reading()['as_of'] == '2026-11-14'


def test_a_declined_key_is_not_used_and_no_api_use_reads_nothing(monkeypatch):
    monkeypatch.setattr('relay.provider_quota.secret', lambda name: 'key')
    calls = []
    signals.refresh_reading(NOW, fetch=lambda s, k: calls.append(s) or ARENA if s in ('openrouter', 'arena') else None,
                            setup={'api_providers': {'items': {'OPENROUTER_API_KEY': {'use': False}}}})
    assert calls == ['arena']
    later = signals.refresh_reading(NOW + 2*DAY, fetch=lambda s, k: calls.append(s), setup={'api_providers': {'use': False}})
    assert later['source'] == 'arena' and calls == ['arena']


def test_a_failed_empty_or_reshaped_source_keeps_the_last_reading_and_the_next_source_is_tried(keyed):
    good = signals.refresh_reading(NOW, fetch=serve({'openrouter': OPENROUTER, 'arena': ARENA}))
    assert good['source'] == 'openrouter' and signals.refresh_reading(NOW + 7200, fetch=None) == good  # once a day

    def down(source, key):
        raise OSError('offline')
    later = NOW + 2*DAY
    for i, body in enumerate((down, lambda s, k: {}, lambda s, k: {'data': [], 'meta': {'as_of': 'x'}, 'rows': []},
                              lambda s, k: {'meta': 3, 'data': [{'display_name': 'GPT-6.1 Sol (Max)'}], 'rows': [1]})):
        assert signals.refresh_reading(later + i*7200, fetch=body) == good  # two hours apart: past the hourly limit
        assert signals.reading()['read_at'] == NOW
    # OpenRouter reshaped, the dataset fine: the dataset's reading replaces it
    assert signals.refresh_reading(later + 9*7200, fetch=serve({'openrouter': {}, 'arena': ARENA}))['source'] == 'arena'


def test_a_failed_attempt_waits_an_hour(keyed):
    calls = []

    def down(source, key):
        calls.append(source)
        raise OSError('offline')
    assert signals.refresh_reading(NOW, fetch=down) is None
    n = len(calls)
    signals.refresh_reading(NOW + 600, fetch=down)
    assert len(calls) == n and signals.reading() is None
    signals.refresh_reading(NOW + 3700, fetch=down)
    assert len(calls) > n


def reading(source, version, score_before, score_after=None, effort='high', line_='gemini-argon', v='4'):
    def one(score):
        return {'source': source, 'version': version, 'as_of': '2026-11-14', 'rows': [
            {'line': line_, 'version': v, 'effort': effort, 'intelligence': score, 'coding': None, 'agentic': None,
             'cost_per_task_usd': 0.73}]}
    return {**one(score_after if score_after is not None else score_before), 'previous': one(score_before)}


@pytest.mark.parametrize('source, scores, expected', [
    ('openrouter', (56, 55.9, 40, 39.9, 0), ('frontier', 'middling', 'middling', 'cheap', 'cheap')),
    ('arena', (0.3, 0.14, 0.139, 0.05, 0.049, -0.1), ('frontier', 'frontier', 'middling', 'middling', 'cheap', 'cheap'))])
def test_the_class_follows_absolute_cut_points_of_each_source(source, scores, expected):
    for score, name in zip(scores, expected):
        assert signals.band(source, score) == name
        found = signals.klass(route('gemini-4-argon', reasoning_effort='high'), reading(source, 'v', score))
        assert found['class'] == name and found['from'] == 'reading'
    # a new leader changes no one's class: the cuts are numbers, not a band under the best score
    assert signals.band('openrouter', 56) == 'frontier'


def test_a_reading_replaces_the_line_class_only_when_two_readings_of_one_source_agree():
    argon = route('gemini-4-argon', reasoning_effort='high')
    assert LINES['gemini-argon']['class'] == 'middling'
    # agree: the reading's class (frontier at 60 twice) replaces the hand class
    assert signals.klass(argon, reading('openrouter', 'v1', 60, 60))['from'] == 'reading'
    # disagree, another source or another index version, or only one reading: the hand class stands, numbers shown
    for saved in (reading('openrouter', 'v1', 45, 60),
                  {**reading('openrouter', 'v1', 60), 'previous': reading('arena', 'v1', 60)['previous']},
                  {**reading('openrouter', 'v2', 60), 'previous': reading('openrouter', 'v1', 60)['previous']},
                  {k: v for k, v in reading('openrouter', 'v1', 60).items() if k != 'previous'}):
        found = signals.klass(argon, saved)
        assert (found['class'], found['from'], found['intelligence']) == ('middling', 'line', saved['rows'][0]['intelligence'])


def test_the_reading_counts_only_for_the_route_that_runs_at_its_effort():
    saved = reading('openrouter', 'v1', 60)
    assert signals.klass(route('gemini-4-argon', reasoning_effort='medium'), saved) == {'class': 'middling', 'from': 'line'}
    assert signals.klass(route('gemini-4-argon', reasoning_effort='high'), saved)['class'] == 'frontier'
    assert signals.klass(route('gemini-4-argon-high'), saved)['class'] == 'frontier'  # AGY spells it in the model
    assert signals.klass(route('gemini-4-argon'), saved)['from'] == 'line'  # no effort declared: none assumed
    assert signals.klass(route('gemini-4-hydrogen', reasoning_effort='high'), saved)['from'] == 'line'
    ambiguous = {**saved, 'rows': saved['rows']*2}
    assert signals.klass(route('gemini-4-argon', reasoning_effort='high'), ambiguous)['from'] == 'line'


def test_the_hand_class_serves_when_there_is_no_reading_and_a_model_without_a_line_has_none():
    assert signals.reading() is None
    hand = {'claude-fable-5-2': 'frontier', 'claude-opus-5-5': 'middling', 'claude-sonnet-5-5': 'middling',
            'claude-haiku-5-5': 'cheap', 'gpt-6-astra': 'frontier', 'gpt-6.1-sol': 'middling', 'gpt-6.1-terra': 'middling',
            'gpt-6.1-luna': 'cheap', 'gemini-4-carbon': 'frontier', 'gemini-4-hydrogen': 'middling',
            'gemini-4-argon': 'middling', 'gemini-3.8-flash': 'cheap', 'gemini-4-argon-high': 'middling'}
    for model, name in hand.items():
        assert signals.klass(route(model)) == {'class': name, 'from': 'line'}
    assert signals.klass(route('moonshotai/kimi-k3.1-pro')) is None and signals.klass(route('gpt-realtime-2.1')) is None
    assert signals.describe(None).startswith('none') and signals.describe({'class': 'cheap', 'from': 'line'}) \
        == 'cheap (line default)'


def test_tare_models_shows_the_class_numbers_source_and_attribution(capsys, keyed):
    from relay import model_choices
    signals.refresh_reading(NOW, fetch=serve({'openrouter': OPENROUTER, 'arena': ARENA}))
    signals.refresh_reading(NOW + 2*DAY, fetch=serve({'openrouter': OPENROUTER, 'arena': ARENA}))
    model_choices.main(['--include-unconfigured', '--vendor', 'Google'])
    out = capsys.readouterr().out
    assert 'Class: cheap (reading; openrouter 2026-11-14: intelligence 30.8)' in out  # AGY Flash, high
    assert 'Class: frontier (line default)' in out  # Carbon at medium: the reading is at high
    assert 'Source: Artificial Analysis (artificialanalysis.ai) via OpenRouter' in out


def receipt(storage, session, unit, rungs, **result):
    job = storage/'agent-sessions'/session/'units'/unit
    job.mkdir(parents=True)
    (job/'receipt.json').write_text(json.dumps({'unit': {'key': unit}, 'rungs': rungs, 'result': result}), encoding='utf-8')


def test_a_routes_own_record_as_implementer_is_counted_from_the_unit_receipts_and_shown(tmp_path, monkeypatch, capsys):
    receipt(tmp_path, 's1', 'a', [{'route': 'gemini-api-3-8-flash', 'verdict': 'VERIFIED', 'needs': 'middling',
                                   'route_class': 'cheap', 'audit': {'verdict': 'ACCEPT'}}],
            verdict='VERIFIED', route='gemini-api-3-8-flash', applied=True)
    receipt(tmp_path, 's1', 'b', [{'route': 'gemini-api-3-8-flash', 'verdict': 'NOT_VERIFIED', 'needs': 'middling'}],
            verdict='NOT_VERIFIED', route='gemini-api-3-8-flash')
    receipt(tmp_path, 's2', 'c', [{'route': 'gemini-api-3-8-flash', 'verdict': 'VERIFIED', 'audit': {'verdict': 'REJECT'}}],
            verdict='NOT_REVIEWED', route='gemini-api-3-8-flash')
    (tmp_path/'agent-sessions'/'s2'/'units'/'torn').mkdir()
    (tmp_path/'agent-sessions'/'s2'/'units'/'torn'/'receipt.json').write_text('{"rungs": [', encoding='utf-8')
    record = signals.own_record(tmp_path)
    line_ = '3 runs; 2 passed the checks; 1 of 2 audits accepted; 1 applied; needed middling: 1 of 2 passed'
    assert list(record) == ['gemini-api-3-8-flash'] and signals.describe_record(record['gemini-api-3-8-flash']) == line_
    assert signals.describe_record(record.get('never-ran')) is None
    receipt(tmp_path, 's3', 'd', [{'route': 'once', 'verdict': 'VERIFIED'}], verdict='VERIFIED', route='once')
    once = signals.describe_record(signals.own_record(tmp_path)['once'])
    assert once == '1 runs; 1 passed the checks; 0 of 0 audits accepted; 0 applied'
    from relay import model_choices
    monkeypatch.setattr('relay.native_installation.storage_root', lambda: tmp_path)
    model_choices.main(['--include-unconfigured', '--vendor', 'Google'])
    out = capsys.readouterr().out
    assert out.count('As implementer on this machine: ' + line_) == 1 and 'does not compare two models' in out
    model_choices.main(['--include-unconfigured', '--vendor', 'Google', '--json'])
    rows = {row['deployment']: row for row in json.loads(capsys.readouterr().out)['models']}
    assert rows['gemini-api-3-8-flash']['implementer_record']['needs.middling.ran'] == 2


def test_the_own_record_counts_each_type_of_work_a_unit_named(tmp_path):
    """The only measure of a type no board covers (owner, 2026-11-16: games, art, audio). A unit of several types
    counts in each; an id the catalogue does not hold and a receipt saved before `kind` count in none."""
    receipt(tmp_path, 's', 'a', [{'route': 'r', 'verdict': 'VERIFIED', 'kind': 'audio', 'audit': {'verdict': 'ACCEPT'}}])
    receipt(tmp_path, 's', 'b', [{'route': 'r', 'verdict': 'NOT_VERIFIED', 'kind': 'art', 'kinds': ['art', 'audio']}])
    receipt(tmp_path, 's', 'c', [{'route': 'r', 'verdict': 'VERIFIED', 'kind': 'no-such-type'}])
    receipt(tmp_path, 's', 'd', [{'route': 'r', 'verdict': 'VERIFIED'}])
    tally = signals.own_record(tmp_path)['r']
    assert [tally['kind', 'audio', k] for k in ('ran', 'passed', 'audited', 'accepted')] == [2, 1, 1, 1]
    assert (tally['kind', 'art', 'ran'], tally['kind', 'art', 'passed'], tally['ran']) == (1, 0, 4)
    assert not any(k[1] == 'no-such-type' for k in tally if isinstance(k, tuple))
    assert signals.describe_record(tally).endswith(
        '; art work: 0 of 1 passed, 0 of 0 audits accepted; audio work: 1 of 2 passed, 1 of 1 audits accepted')


def test_an_older_version_of_a_line_is_one_class_lower_and_a_bedrock_id_is_read_as_its_model():
    """The version is part of what a model is: Fable 5.1 is not Fable 5.2, and the Opus 4.6 that Bedrock's free
    credit runs is not the Opus 5.5 of the CLI and the API (owner, 2026-11-15)."""
    from relay import environment
    # Fable 5.2 is listed but has not answered on this machine: it lowers nobody, and 5.1 keeps the line's class.
    assert signals.klass(route('claude-fable-5-1')) == {'class': 'frontier', 'from': 'line'}
    setup = environment.read()
    setup.setdefault('proofs', {})['claude-api-fable-5-2'] = {'ok': True}
    environment.save(setup)  # the proof tare setup --prove (or the TUI) records
    assert signals.klass(route('claude-fable-5-2')) == {'class': 'frontier', 'from': 'line'}
    assert signals.klass(route('claude-fable-5-1')) == {'class': 'middling', 'from': 'line', 'older_than': '5.2'}
    assert signals.klass(route('gpt-6-sol')) == {'class': 'cheap', 'from': 'line', 'older_than': '6.1'}
    assert signals.klass(route('gpt-5.6-luna'))['class'] == 'cheap'  # nothing is below cheap
    bedrock = {'id': 'bedrock-opus', 'model': 'claude-opus-4.6',
               'native_profile': {'model': 'us.anthropic.claude-opus-4-6-v1', 'parameters_json': '{}'}}
    found = signals.klass(bedrock)
    assert found == {'class': 'cheap', 'from': 'line', 'older_than': '5.5'}
    assert signals.describe(found) == 'cheap (line default, one class lower: an older version than 5.5)'
