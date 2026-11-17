"""The catalogue of task types, the boards read for it, and a model's class per type. Every model and number here is
invented (no real benchmark reading is in the repository)."""
import csv
import io
import json
import zipfile

import pytest

from relay import model_choices
from relay import model_signals as signals

NOW = 1794628000.0  # 2026-11-14 UTC
DAY = 86400


def source(board, reader='epoch', column='Score', **more):
    return {'reader': reader, 'board': board, 'column': column, 'higher_is_better': True, 'kind': 'outcome',
            'name': 'Board '+board, 'citation': 'Source: '+board, **more}


ONE, TWO = source('one.csv'), source('two.csv')
PAGE = {'reader': 'page', 'board': 'https://example.test/leaderboard', 'model': 'Model', 'column': 'Score', 'higher_is_better': True,
        'kind': 'outcome', 'name': 'Board page', 'citation': 'Source: page'}
PAGE_ROWS = [('Gemini 4 Carbon (High)', 'Google', .71), ('GPT-6 Astra (Max)', 'OpenAI', .66), ('GPT-6.1 Sol (Max)', 'OpenAI', .60),
             ('GPT-6.1 Terra (High)', 'OpenAI', .58), ('Claude Opus 5.5 (High)', 'Anthropic', .55),
             ('Gemini 4 Argon (High)', 'Google', .50), ('Gemini 3.8 Flash (High)', 'Google', .30),
             ('Claude Haiku 5.5 (High)', 'Anthropic', .20)]  # the lines of LINES_ROWS
KIND = {'id': 't', 'label': 'testing', 'description': 'a type of the tests', 'sources': [ONE, TWO]}
# Eight known lines (the least a board places with), ranked: frontier is the top quarter (the second best, .66),
# middling the top three quarters (the sixth best, .50). Invented.
LINES_ROWS = [('gemini-4-carbon_high', 'Google DeepMind', .71), ('gpt-6-astra_max', 'OpenAI', .66),
              ('gpt-6.1-sol_max', 'OpenAI', .60), ('gpt-6.1-terra_high', 'OpenAI', .58),
              ('claude-opus-5-5_high', 'Anthropic', .55), ('gemini-4-argon_high', 'Google DeepMind', .50),
              ('gemini-3.8-flash_high', 'Google DeepMind', .30), ('claude-haiku-5-5_high', 'Anthropic', .20)]


def csv_text(column, rows):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(['Model version', 'Organization', 'Release date', column])
    writer.writerows((version, org, '2026-11-10', score) for version, org, score in rows)
    return out.getvalue()


def archive(members):
    """An Epoch-like zip from {member: csv text}."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, text in members.items():
            z.writestr(name, text)
    return buffer.getvalue()


def board(src, *rows, anchors=LINES_ROWS):
    """The saved form of a board read from an archive of `rows` beside the anchors."""
    rows, as_of = signals.parse_epoch(archive({src['board']: csv_text(src['column'], [*anchors, *rows])}), src)
    return {'read_at': NOW, 'as_of': as_of, 'rows': rows}


def route(model, endpoint='https://api.example/v1', **parameters):
    return {'id': 'fixture', 'native_profile': {'model': model, 'endpoint': endpoint, 'parameters_json': json.dumps(parameters)}}


KIMI, GLM = route('moonshotai/kimi-k3.1-pro'), route('z-ai/glm-9')


def kept(first=(), second=()):
    return {signals.source_key(ONE): board(ONE, *first), signals.source_key(TWO): board(TWO, *second)}


def test_the_catalogue_is_well_formed_and_lists_the_types_with_no_source_yet():
    ids = [t['id'] for t in signals.TYPES]
    assert len(ids) == len(set(ids))
    for t in signals.TYPES:
        assert all(isinstance(t[k], str) and t[k] for k in ('id', 'label', 'description')) and isinstance(t['sources'], list)
        for s in t['sources']:
            assert s['reader'] in signals.READERS and s['kind'] in ('outcome', 'preference')
            assert isinstance(s['higher_is_better'], bool) and all(s[k] for k in ('board', 'column', 'name', 'citation'))
            assert s['reader'] != 'epoch' or s['board'].endswith('.csv')
            assert s['reader'] not in ('page', 'script') or s['board'].startswith('https://') and s['model']
            assert s['reader'] != 'script' or all(s[k] for k in ('script', 'rows'))
            assert s['reader'] not in ('page', 'script') or 'licen' not in s['citation'].lower()  # none is claimed
    keys = [signals.source_key(s) for t in signals.TYPES for s in t['sources']]
    assert len(keys) == len(set(keys))
    assert {t['id'] for t in signals.TYPES if not t['sources']} == {'3d', 'games', 'art', 'audio', 'medicine', 'data', 'writing', 'philosophy'}
    assert {t['id'] for t in signals.TYPES if t['sources']} == {
        'coding', 'web', 'math', 'science', 'professional', 'security', 'engineering', 'cad'}
    # legal work reads APEX-Agents first and the public LegalBench page after it
    assert [s['reader'] for s in signals.task_type('professional')['sources']] == ['epoch', 'page']


def test_a_model_is_placed_by_the_first_source_that_places_it():
    saved = kept(first=[('kimi-k3.1-pro_max', 'Moonshot', .80)], second=[('kimi-k3.1-pro_max', 'Moonshot', .10)])
    found = signals.type_class(KIMI, KIND, saved)
    assert (found['class'], found['general'], found['source'], found['board'], found['kind'], found['read_at']) \
        == ('frontier', False, 'Board one.csv', 'one.csv', 'outcome', NOW)
    assert signals.describe_type(found) == 'frontier for testing (Board one.csv, read 14 Nov)'


def test_a_model_only_the_second_source_places_gets_that_sources_class():
    saved = kept(second=[('glm-9_max', 'Z.ai', .55)])  # the house is spelled z-ai by the route and Z.ai by the board
    found = signals.type_class(GLM, KIND, saved)
    assert (found['class'], found['general'], found['source']) == ('middling', False, 'Board two.csv')
    # votes are said as votes
    vote = {**TWO, 'kind': 'preference'}
    assert signals.describe_type(signals.type_class(GLM, {**KIND, 'sources': [vote]}, {signals.source_key(vote): saved[signals.source_key(TWO)]})) \
        == 'middling for testing (Board two.csv, votes, read 14 Nov)'


def test_a_model_no_board_places_gets_the_general_class_marked_general():
    saved = kept()
    # a line model keeps its line's class; a model with no line and no row has none
    found = signals.type_class(route('gemini-4-hydrogen', reasoning_effort='high'), KIND, saved)
    assert found['general'] and found['class'] == 'middling'
    assert signals.describe_type(found) == 'middling (general: no board places it for testing)'
    nobody = signals.type_class(route('unknownco/unseen-1'), KIND, saved)
    assert nobody['general'] and nobody['class'] is None
    assert signals.describe_type(nobody).startswith('none (no general class either: no board places it for testing')
    # a type with no source at all
    assert signals.type_class(KIMI, {**KIND, 'sources': []}, saved)['general']


def test_a_known_line_is_placed_at_the_highest_effort_the_board_lists_whatever_the_routes():
    """Like with like: the lines that rank a board are read at their highest effort, so a route's model is too. Read
    at its own effort, a route at medium (not listed) outranked the same model's route at high (listed)."""
    saved = kept(first=[('gemini-3.8-flash_low', 'Google DeepMind', .90)])  # a low-effort row that would be frontier
    for effort in ('high', 'medium', 'low'):
        found = signals.type_class(route('gemini-3.8-flash', reasoning_effort=effort), KIND, saved)
        assert (found['class'], found['effort']) == ('cheap', 'high')


def test_a_board_that_stopped_at_older_versions_places_nobody():
    """An aged source: it holds Sol 5.6 where another board already holds Sol 6.1, so it is one line short of the
    least a board places with."""
    old = [('gpt-5.6-sol_max', 'OpenAI', .60) if row[0] == 'gpt-6.1-sol_max' else row for row in LINES_ROWS]
    aged = board(ONE, ('kimi-k3.1-pro_max', 'Moonshot', .65), anchors=old)
    alone = {signals.source_key(ONE): aged}
    # on its own it is the newest it knows: Sol 5.6 is one of its eight lines, and Kimi is middling
    assert signals.type_class(KIMI, {**KIND, 'sources': [ONE]}, alone)['class'] == 'middling'
    beside = {**alone, signals.source_key(TWO): board(TWO)}  # another board holds Sol 6.1
    found = signals.type_class(KIMI, {**KIND, 'sources': [ONE]}, beside)
    assert found['general'] and found['class'] is None
    assert 'GPT-5.6 Sol' not in [a[2] for a in signals._anchors(aged, signals._current(beside))]
    # a board with no known line at all has no anchor
    bare = {'read_at': NOW, 'as_of': None, 'rows': [{'line': None, 'creator': 'Moonshot', 'name': 'kimi-k3.1-pro', 'effort': 'max',
                                                     'permaslug': None, 'intelligence': .9}]}
    assert signals._anchors(bare) == [] and signals.type_class(KIMI, {**KIND, 'sources': [ONE]}, {signals.source_key(ONE): bare})['general']


def nested(path, value):
    for key in reversed(path.split('.')):
        value = {key: value}
    return value


def table(head, *rows):
    return '<table><thead><tr>' + ''.join(f'<th>{h}</th>' for h in head) + '</tr></thead><tbody>'         + ''.join('<tr>' + ''.join(cells) + '</tr>' for cells in rows) + '</tbody></table>'


def page_html(src, rows=PAGE_ROWS):
    """An invented page in the structure of a shipped source: a table after a decoy one (reader 'page'), or a JSON
    script (reader 'script', the rows at the catalogue's path)."""
    if src['reader'] == 'script':
        found = [{**nested(src['model'], n), **nested(src['organisation'], o), **nested(src['column'], v)} for n, o, v in rows]
        body = f'<script id="{src["script"]}" type="application/json">{json.dumps(nested(src["rows"], found))}</script>'
    else:
        body = table(['Check', 'Result'], ['<td>a</td>', '<td>1</td>']) + table(
            ['Rank', src['model'], src['column'] + ' &#9660;'],
            *(['<td>1</td>', f'<th><a href="/m">{n}</a></th>', f'<td>{v*100:.1f}%</td>'] for n, o, v in rows))
    return f'<html><body>{body}</body></html>'.encode()


PAGES = {s['board']: s for t in signals.TYPES for s in t['sources'] if s['reader'] in ('page', 'script')}


def served(zip_bytes, web_rows, calls, down=()):
    def fetch(src, key):
        calls.append(src)
        if src in down:
            raise OSError('offline')
        if src == 'epoch':
            return zip_bytes
        if src.startswith('page:'):
            return page_html(PAGES[src[5:]])
        return {'rows': [{'row': r} for r in web_rows]}
    return fetch


def everything():
    """The shipped catalogue answered by invented data: an archive of every Epoch board it names, and Arena rows."""
    epoch = [s for t in signals.TYPES for s in t['sources'] if s['reader'] == 'epoch']
    web = [{'model_name': n, 'organization': o, 'category': 'overall', 'score': v, 'rating': v, 'leaderboard_publish_date': '2026-11-15'}
           for n, o, v in (('Gemini 4 Carbon (High)', 'google', .71), ('GPT-6 Astra (Max)', 'openai', .66),
                           ('GPT-6.1 Sol (Max)', 'openai', .60), ('Gemini 4 Argon (High)', 'google', .50),
                           ('Gemini 3.8 Flash (High)', 'google', .30))]
    return archive({s['board']: csv_text(s['column'], LINES_ROWS) for s in epoch}), web


def test_the_boards_are_read_once_a_day_and_a_failed_source_keeps_its_saved_board_and_stops_no_other():
    zipped, web = everything()
    calls = []
    signals.refresh_boards(NOW, served(zipped, web, calls), {})
    saved = signals.boards()
    every = [signals.source_key(s) for t in signals.TYPES for s in t['sources']]
    assert sorted(saved) == sorted(every) and all(b['read_at'] == NOW and b['rows'] for b in saved.values())
    assert calls.count('epoch') == 1 and sorted(c for c in calls if c != 'epoch' and not c.startswith('page:')) == ['arena:agent', 'arena:webdev']
    assert sorted(c for c in calls if c.startswith('page:')) == sorted('page:' + url for url in PAGES)  # one GET of each named URL
    assert saved[signals.source_key(signals.task_type('web')['sources'][0])]['as_of'] == '2026-11-15'
    calls.clear()
    signals.refresh_boards(NOW + 7200, served(zipped, web, calls), {})
    assert calls == []  # once a day
    # a day later the archive is down and the arena answers: the Epoch boards stay as they were, the arena's are new
    signals.refresh_boards(NOW + 2*DAY, served(zipped, web, calls, down=('epoch',)), {})
    after = signals.boards()
    epoch_key = signals.source_key(signals.task_type('math')['sources'][0])
    assert after[epoch_key]['rows'] == saved[epoch_key]['rows'] and after[epoch_key]['read_at'] == NOW
    assert after[epoch_key]['tried_at'] == NOW + 2*DAY
    web_key = signals.source_key(signals.task_type('web')['sources'][0])
    assert after[web_key]['read_at'] == NOW + 2*DAY and 'tried_at' not in after[web_key]
    calls.clear()
    signals.refresh_boards(NOW + 2*DAY + 600, served(zipped, web, calls), {})
    assert calls == []  # after a failed try, an hour passes first
    signals.refresh_boards(NOW + 2*DAY + 3700, served(zipped, web, calls), {})
    assert calls.count('epoch') == 1 and signals.boards()[epoch_key]['read_at'] == NOW + 2*DAY + 3700


def test_a_board_missing_from_the_archive_or_empty_fails_alone():
    zipped, web = everything()
    math = signals.task_type('math')['sources'][0]
    broken = archive({s['board']: csv_text(s['column'], LINES_ROWS) for t in signals.TYPES for s in t['sources']
                      if s['reader'] == 'epoch' and s['board'] != math['board']})
    signals.refresh_boards(NOW, served(broken, web, []), {})
    saved = signals.boards()
    assert signals.source_key(math) in saved and 'rows' not in saved[signals.source_key(math)]  # tried, nothing to keep
    assert saved[signals.source_key(signals.task_type('science')['sources'][0])]['rows']
    # an archive that is not a zip, and a reshaped arena answer, read nothing and raise nothing
    signals.refresh_boards(NOW + 2*DAY, lambda s, k: b'not a zip' if s == 'epoch' else {'rows': 'x'}, {})
    assert signals.boards()[signals.source_key(math)].get('tried_at') == NOW + 2*DAY


def test_a_machine_that_declined_api_use_reads_nothing():
    calls = []
    zipped, web = everything()
    signals.refresh_boards(NOW, served(zipped, web, calls), {'api_providers': {'use': False}})
    assert calls == [] and signals.boards() == {}


def test_refresh_reading_reads_the_boards_after_the_general_reading_and_neither_stops_the_other(monkeypatch):
    monkeypatch.setattr('relay.provider_quota.secret', lambda name: None)
    zipped, web = everything()
    calls = []
    served_by = served(zipped, web, calls, down=('arena:webdev', 'epoch'))
    value = signals.refresh_reading(NOW, fetch=served_by, setup={})
    assert value['source'] == 'arena' and 'arena:agent' in calls and 'epoch' in calls  # the agent board is read for both
    agent_key = signals.source_key(signals.task_type('coding')['sources'][0])
    assert signals.boards()[agent_key]['rows'] and 'rows' not in signals.boards()[signals.source_key(signals.task_type('web')['sources'][0])]
    # the general source down, the boards fine
    down = served(zipped, web, [], down=('arena',))
    assert signals.refresh_reading(NOW + 2*DAY, fetch=down, setup={}) == value
    assert signals.boards()[signals.source_key(signals.task_type('math')['sources'][0])]['read_at'] == NOW + 2*DAY


def test_the_archive_reader_opens_only_the_named_member_in_memory_and_refuses_an_oversized_one(monkeypatch):
    raw = archive({'one.csv': csv_text('Score', LINES_ROWS), 'other.csv': csv_text('Score', LINES_ROWS)})
    opened = []
    original = zipfile.ZipFile.read
    monkeypatch.setattr(zipfile.ZipFile, 'read', lambda self, name, *a: opened.append(getattr(name, 'filename', name)) or original(self, name, *a))
    for forbidden in ('extract', 'extractall'):
        monkeypatch.setattr(zipfile.ZipFile, forbidden, lambda *a, **k: pytest.fail('a member was extracted to disk'))
    rows, as_of = signals.parse_epoch(raw, ONE)
    assert opened == ['one.csv'] and as_of == '2026-11-10' and len(rows) == len(LINES_ROWS)
    with pytest.raises(KeyError):
        signals.parse_epoch(raw, source('missing.csv'))  # a name the archive does not hold is never guessed
    monkeypatch.setattr(signals, 'CAP', 50)
    opened.clear()
    with pytest.raises(ValueError):
        signals.parse_epoch(raw, ONE)
    assert opened == []  # refused by its declared size, before it is read


def test_an_epoch_row_is_a_model_and_its_effort_or_a_variant_or_no_row():
    text = csv_text('Score', [('gpt-6.1-sol_xhigh', 'OpenAI', .1), ('gpt-6.1-sol_unknown', 'OpenAI', .2),
                              ('gpt-6.1-sol_none', 'OpenAI', .3), ('gpt-6-astra-fallback_max', 'OpenAI', .4),
                              ('gpt-6-luna_max', 'Reseller', .5), ('kimi-k3.1-pro-together_max', 'Moonshot', .6),
                              ('gemini-3.8-flash', 'Google DeepMind', .7), ('gpt-6-terra_max', 'OpenAI', 'n/a')])
    rows = signals.parse_epoch(archive({'one.csv': text}), ONE)[0]
    lines = {(r['line'], r['version'], r['effort']) for r in rows if r['line']}
    assert lines == {('gpt-sol', '6.1', 'xhigh'), ('gpt-sol', '6.1', None), ('gemini-flash', '3.8', None)}
    others = {(r['creator'], r['name']) for r in rows if not r['line']}
    # a mode with no effort of ours and a variant are not the model; another creator's row of a line's name is no line
    assert others == {('OpenAI', 'gpt-6.1-sol_none'), ('OpenAI', 'gpt-6-astra-fallback'), ('Reseller', 'gpt-6-luna'),
                      ('Moonshot', 'kimi-k3.1-pro-together')}  # the unscored row is no row


def test_a_source_that_is_not_higher_is_better_is_negated_so_that_a_higher_number_is_better(monkeypatch):
    lower = source('lower.csv', higher_is_better=False)
    monkeypatch.setattr(signals, 'TYPES', [{**KIND, 'sources': [lower]}])
    raw = archive({'lower.csv': csv_text('Score', [('gpt-6.1-sol_max', 'OpenAI', .25), ('gemini-4-argon_high', 'Google DeepMind', .5)])})
    signals.refresh_boards(NOW, lambda s, k: raw, {})
    assert sorted(r['intelligence'] for r in signals.boards()[signals.source_key(lower)]['rows']) == [-.5, -.25]


def saved_boards():
    zipped, web = everything()
    signals.refresh_boards(NOW, served(zipped, web, []), {})


def flash(capsys, *argv):
    """The JSON row of the Gemini Flash API route (a known line, cheap) for `argv`."""
    model_choices.main(['--include-unconfigured', '--vendor', 'Google', '--json', *argv])
    return {r['deployment']: r for r in json.loads(capsys.readouterr().out)['models']}['gemini-api-3-8-flash']


def test_tare_models_type_shows_each_models_class_for_the_type_and_where_it_came_from(capsys):
    saved_boards()
    model_choices.main(['--include-unconfigured', '--vendor', 'Google', '--type', 'math'])
    out = capsys.readouterr().out
    assert 'Class for mathematics: cheap for mathematics (Epoch FrontierMath tiers 1-3, read 14 Nov)' in out
    assert 'Source: Epoch AI, Capabilities & benchmarking, CC BY 4.0 (epoch.ai/benchmarks); FrontierMath, as of 2026-11-10.' in out
    model_choices.main(['--include-unconfigured', '--vendor', 'Google', '--type', '3d'])
    out = capsys.readouterr().out
    assert 'Class for 3D modelling: cheap (general: no board places it for 3D modelling)' in out and 'Source: Epoch' not in out


def test_tare_models_type_in_json_marks_a_class_from_a_board_and_one_that_is_general(capsys):
    saved_boards()
    placed = flash(capsys, '--type', 'math')['type_class']
    assert (placed['general'], placed['type'], placed['class'], placed['source'], placed['read_at'])         == (False, 'math', 'cheap', 'Epoch FrontierMath tiers 1-3', NOW)
    assert flash(capsys, '--type', '3d')['type_class'] == {'class': 'cheap', 'type': '3d', 'label': '3D modelling', 'general': True}
    assert 'type_class' not in flash(capsys)  # without --type nothing changes


def test_tare_models_types_shows_the_catalogue_with_each_sources_read_date_and_anchors(capsys):
    saved_boards()
    model_choices.main(['--types'])
    out = capsys.readouterr().out
    assert 'math: mathematics - ' in out and 'cad: CAD - ' in out and 'no source yet: the general class' in out
    assert 'Epoch CritPt (outcome): read 14 Nov, 8 of 12 known lines' in out and 'LMArena webdev (preference): read 14 Nov' in out
    assert 'CritPt, as collected by Epoch AI (epoch.ai/benchmarks)' in out
    model_choices.main(['--types', '--json'])
    catalogue = {t['id']: t for t in json.loads(capsys.readouterr().out)['types']}
    assert catalogue['3d']['sources'] == [] and len(catalogue) == len(signals.TYPES)
    gpqa = next(s for s in catalogue['science']['sources'] if s['board'] == 'critpt_external.csv')
    assert (gpqa['anchors'], gpqa['known_lines'], gpqa['places'], gpqa['read_at']) == (8, 12, True, NOW)


def test_an_unread_or_aged_source_is_said_to_place_no_model(capsys):
    model_choices.main(['--types'])
    assert 'Epoch CritPt (outcome): not read yet, 0 of 12 known lines; fewer than 8: places no model' in capsys.readouterr().out
    assert signals.describe_source({'name': 'B', 'kind': 'outcome', 'read_at': NOW, 'anchors': 2, 'known_lines': 12, 'places': False})         == 'B (outcome): read 14 Nov, 2 of 12 known lines; fewer than 8: places no model'


def test_an_unknown_type_id_is_an_error_that_lists_the_ids(capsys):
    with pytest.raises(SystemExit) as stop:
        model_choices.main(['--type', 'nope'])
    assert stop.value.code == 2
    err = capsys.readouterr().err
    assert 'Unknown task type: nope' in err and all(t['id'] in err for t in signals.TYPES)


def html(*tables):
    return ('<html><body>' + ''.join(tables) + '</body></html>').encode()


LEADERBOARD = table(
    ['#Rank', 'Model', 'Maker', 'Score<span>&#9660;</span>'],
    ['<td>1</td>', '<td><b>GPT-6 Astra (Max)</b><small>note, v2</small></td>', '<td>OpenAI</td>', '<td>75.0%</td>'],
    ['<td>2</td>', '<td>Gemini&nbsp;4&nbsp;Argon</td>', '<td>Google</td>', '<td><span>0.5</span></td>'],
    ['<td>3</td>', '<th>GPT-6 Luna</th>', '<td>Reseller</td>', '<td>40</td>'],
    ['<td>4</td>', '<td>Kimi K3.1 Pro</td>', '<td>Moonshot</td>', '<td>coming soon</td>'],
    ['<td colspan="4">a bar under the row above</td>'],
    ['<td>5</td>', '<td>Kimi K3.1 Pro (Max, Fallback)</td>', '<td>Moonshot</td>', '<td>&#8776;0.3</td>'],
    ['<td>6</td>', '<td>Gemini 3.8 Flash (07/26)</td>', '<td>Google</td>', '<td>10</td>'])
OTHER = table(['Rank', 'Model', 'Pass'], ['<td>1</td>', '<td>Invented One</td>', '<td>.9</td>'])  # a model column, no score


def test_a_page_table_is_found_by_its_headers_and_read_like_any_board():
    page = html(table(['Check', 'Result'], ['<td>a</td>', '<td>1</td>']), OTHER, LEADERBOARD)
    rows, as_of = signals.parse_page(page, {**PAGE, 'organisation': 'Maker'})
    assert as_of is None  # the page states no date
    by = {(r['line'] or r['name']): r for r in rows}
    assert set(by) == {'gpt-astra', 'gemini-argon', 'GPT-6 Luna', 'Gemini 3.8 Flash (07/26)'}  # no unscored or short row
    assert (by['gpt-astra']['version'], by['gpt-astra']['effort'], by['gpt-astra']['intelligence']) == ('6', 'max', 75.0)
    assert (by['gemini-argon']['version'], by['gemini-argon']['effort'], by['gemini-argon']['intelligence']) == ('4', None, .5)
    # a creator that is not the line's vendor, and a bracket that is a date, are models with no line
    assert (by['GPT-6 Luna']['creator'], by['GPT-6 Luna']['intelligence']) == ('Reseller', 40.0)
    assert (by['Gemini 3.8 Flash (07/26)']['creator'], by['Gemini 3.8 Flash (07/26)']['effort']) == ('Google', None)
    # a page with no organisation column: a known line's name stands for its vendor's model, any other row has no creator
    rows = signals.parse_page(page, PAGE)[0]
    assert {r['line'] for r in rows if r['line']} == {'gpt-astra', 'gemini-argon', 'gpt-luna'}
    assert {r['name'] for r in rows if not r['line']} == {'Gemini 3.8 Flash (07/26)'} and {r['creator'] for r in rows if not r['line']} == {''}


def test_a_page_without_the_table_or_with_a_column_missing_is_a_failed_reading():
    assert signals.parse_page(html(OTHER), PAGE) is None  # the model column but not the score one
    assert signals.parse_page(html(LEADERBOARD), {**PAGE, 'column': 'Pass@1'}) is None
    assert signals.parse_page(html(LEADERBOARD), {**PAGE, 'organisation': 'Country'}) is None
    assert signals.parse_page(b'<html>no table</html>', PAGE) is None
    assert signals.parse_page(html(table(['Model', 'Score'], ['<td>a</td>', '<td>n/a</td>'])), PAGE) is None  # nothing scored


def test_a_failed_page_reading_keeps_the_saved_board_and_the_page_is_read_once_a_day(monkeypatch):
    monkeypatch.setattr(signals, 'TYPES', [{**KIND, 'sources': [PAGE]}])
    calls = []

    def serve(page):
        return lambda s, k: calls.append(s) or page
    signals.refresh_boards(NOW, serve(html(LEADERBOARD)), {})
    saved = signals.boards()[signals.source_key(PAGE)]
    assert calls == ['page:https://example.test/leaderboard'] and saved['read_at'] == NOW and saved['as_of'] == '2026-11-14'
    signals.refresh_boards(NOW + 3600, serve(html(LEADERBOARD)), {})
    assert len(calls) == 1  # once a day
    signals.refresh_boards(NOW + 2*DAY, serve(html(OTHER)), {})  # the columns moved
    after = signals.boards()[signals.source_key(PAGE)]
    assert after['rows'] == saved['rows'] and after['read_at'] == NOW and after['tried_at'] == NOW + 2*DAY
    # a page that fails is asked again a day later, never an hour later (an LMArena or Epoch board: an hour)
    signals.refresh_boards(NOW + 2*DAY + 3601, serve(html(OTHER)), {})
    assert len(calls) == 2
    signals.refresh_boards(NOW + 3*DAY + 1, serve(html(OTHER)), {})
    assert len(calls) == 3


def test_a_page_board_places_a_known_line_and_a_model_with_no_line_like_any_board():
    scored = [(n, v*100) for n, o, v in PAGE_ROWS] + [('Kimi K3.1 Pro', 65), ('GPT-6.1 Sol', 55)]
    page = html(table(['Model', 'Score'], *(['<td>' + n + '</td>', '<td>' + str(v) + '</td>'] for n, v in scored)))
    rows = signals.parse_page(page, PAGE)[0]
    saved = {signals.source_key(PAGE): {'read_at': NOW, 'as_of': None, 'rows': rows}}
    kind = {**KIND, 'sources': [PAGE]}
    assert signals.type_class(KIMI, kind, saved)['class'] == 'middling'  # no creator on the page: matched by name
    assert signals.type_class(route('gemini-4-argon', reasoning_effort='high'), kind, saved)['class'] == 'middling'
    assert signals.type_class(route('gemini-3.8-flash', reasoning_effort='high'), kind, saved)['class'] == 'cheap'
    assert signals.type_class(GLM, kind, saved)['general']  # a model the page does not hold


SCRIPT = {**PAGE, 'reader': 'script', 'script': 'data', 'rows': 'props.rows', 'model': 'name', 'organisation': 'maker', 'column': 'score.mean'}


def test_a_json_script_is_read_at_the_catalogues_path_and_a_moved_path_is_a_failed_reading():
    data = {'props': {'rows': [{'name': 'GPT-6 Astra', 'maker': 'OpenAI', 'score': {'mean': 48.3}},
                               {'name': 'Kimi K3.1 Pro', 'maker': 'Moonshot', 'score': {'mean': 40}},
                               {'name': 'Gemini 4 Argon', 'maker': 'Google', 'score': {'mean': None}}]}}
    page = ('<script id="other">{}</script><script id="data" type="application/json">' + json.dumps(data) + '</script>').encode()
    rows, as_of = signals.parse_script(page, SCRIPT)
    assert as_of is None and {(r['line'], r['intelligence']) for r in rows} == {('gpt-astra', 48.3), (None, 40.0)}
    assert [r['creator'] for r in rows if not r['line']] == ['Moonshot']
    for moved in ({**SCRIPT, 'rows': 'props.table'}, {**SCRIPT, 'column': 'score.best'}, {**SCRIPT, 'script': 'gone'}):
        with pytest.raises(KeyError):
            signals.parse_script(page, moved)


def test_only_an_https_page_is_fetched(monkeypatch):
    monkeypatch.setattr(signals, '_get', lambda url, headers=None: pytest.fail('fetched ' + url))
    for url in ('file:///etc/hosts', 'http://example.test/'):
        with pytest.raises(ValueError):
            signals.fetch('page:' + url)
    monkeypatch.setattr(signals, '_get', lambda url, headers=None: (url, headers))
    assert signals.fetch('page:https://example.test/')[0] == 'https://example.test/'


def test_a_board_ranks_the_known_lines_instead_of_trusting_their_general_class():
    """A line that leads in general but is weak at this type of work must not set the floor of its class here: the top
    quarter of the board's lines is frontier, whatever each line is called in general."""
    # Carbon and Astra (frontier in general) are the two worst of eight on this board; Sol and Terra lead it.
    upside = [('gpt-6.1-sol_max', 'OpenAI', .90), ('gpt-6.1-terra_high', 'OpenAI', .80), ('claude-opus-5-5_high', 'Anthropic', .70),
              ('gemini-4-argon_high', 'Google DeepMind', .60), ('gemini-3.8-flash_high', 'Google DeepMind', .50),
              ('claude-haiku-5-5_high', 'Anthropic', .40), ('gpt-6-astra_max', 'OpenAI', .30),
              ('gemini-4-carbon_high', 'Google DeepMind', .20)]
    kind = {**KIND, 'sources': [ONE]}

    def at(score):
        kept = {signals.source_key(ONE): board(ONE, ('kimi-k3.1-pro_max', 'Moonshot', score), anchors=upside)}
        return signals.type_class(KIMI, kind, kept)['class']
    # by the general labels' weakest anchor .25 would be frontier (above Carbon's .20); ranked, it is cheap
    assert [at(.85), at(.79), at(.45), at(.25)] == ['frontier', 'middling', 'middling', 'cheap']
    seven = {signals.source_key(ONE): board(ONE, ('kimi-k3.1-pro_max', 'Moonshot', .85), anchors=upside[:7])}
    assert signals.type_class(KIMI, kind, seven)['general']  # a quarter of too few lines is one line: it places nobody
