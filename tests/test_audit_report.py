"""The read-only audit report over the cheap judges' shadow logs."""
import json

import pytest

from relay import audit_report


def pick(choice, p, model, ms):
    return {'choice': choice, 'model': model, 'probabilities': {choice: p, 'other': 1 - p}, 'ms': ms}


@pytest.fixture
def logs(tmp_path):
    serve = tmp_path/'storage'/'serve'
    serve.mkdir(parents=True)
    materiality = {
        'ts': '2026-01-01T00:00:00+00:00', 'kind': 'materiality', 'route': 'sol', 'changed_lines': 12,
        'items': [
            {'finding': 'cache the pages', 'jev': pick('residual', .99, 'jev-1.13.0', 100),
             'local': pick('residual', .95, 'qwen38-gsq', 200), 'demoted': True},
            {'finding': 'off by one', 'jev': pick('material', .8, 'jev-1.13.0', 300),
             'local': pick('material', .9, 'qwen38-gsq', 400), 'demoted': False},
            {'finding': 'rename', 'jev': pick('material', .7, 'jev-1.13.0', 100),
             'local': {'failure': 'BUSY', 'ms': 50}, 'demoted': False},
            {'finding': 'style', 'demoted': False},  # a row with no judge at all
        ]}
    fix = {'ts': '2026-01-02T00:00:00+00:00', 'kind': 'fix', 'route': 'sol', 'changed_lines': 80,
           'novelty': [
               {'finding': 'a', 'jev': pick('new', .9, 'jev-1.13.0', 100),
                'local': pick('original', .95, 'qwen38-gsq', 200)},
               {'finding': 'b', 'jev': pick('new', .8, 'jev-1.13.0', 300)},
               {'finding': 'c'}],
           'proportion': {'jev': pick('excessive', .7, 'jev-1.13.0', 250),
                         'local': pick('proportionate', .9, 'qwen38-gsq', 150)}}
    shadow = [
        {'ts': 't1', 'source': 'serve', 'final_tier': 0,
         'jev': pick('local', .9, 'jev-1.13.0', 10), 'local': pick('cheap', .9, 'qwen38-gsq', 20)},
        {'ts': 't2', 'source': 'serve', 'final_tier': 1,
         'jev': pick('cheap', .9, 'jev-1.13.0', 30), 'local': pick('cheap', .9, 'qwen38-gsq', 40)},
        {'ts': 't3', 'source': 'tui', 'tier': 1,  # no final_tier: not counted for tier matches
         'jev': pick('cheap', .9, 'jev-1.13.0', 50)},
        {'ts': 't4', 'source': 'serve', 'final_tier': 0}]  # no judges answered
    (serve/'audit-judge.jsonl').write_text(
        json.dumps(materiality) + '\n' + 'not json at all\n\n' + json.dumps(fix) + '\n', encoding='utf-8')
    (serve/'jev-shadow.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in shadow), encoding='utf-8')
    return tmp_path/'storage'


def test_the_report_counts_materiality_fix_and_shadow_activity(logs):
    summary = audit_report.summarize(logs/'serve'/'audit-judge.jsonl', logs/'serve'/'jev-shadow.jsonl')
    assert summary['materiality'] == {'rows': 1, 'findings': 3, 'demoted': 1, 'both': 2, 'agree': 2}  # 4th item: no judge
    assert summary['fix']['items'] == 3 and summary['fix']['new'] == 1  # local decides 'a'; only jev's 'b' is new
    assert summary['fix']['proportion'] == {'jev': {'excessive': 1}, 'local': {'proportionate': 1}}
    assert summary['shadow'] == {'rows': 4, 'answered': {'jev': 2, 'local': 2},
                                 'match': {'jev': 2, 'local': 1}}
    assert summary['ms'] == {'jev': {'total': 1240.0, 'count': 9}, 'local': {'total': 1060.0, 'count': 7}}


def test_the_report_renders_the_counts(logs, capsys):
    assert audit_report.main(['--storage', str(logs)]) == 0
    out = capsys.readouterr().out
    assert f'storage: {logs}' in out
    assert 'materiality: 1 rows, 3 findings judged, 1 demoted' in out
    assert 'jev + local agree when both answered: 2 of 2 (100%)' in out
    assert 'fix: 1 rows, 3 review items' in out
    assert 'jev proportion verdicts: excessive 1' in out
    assert 'local proportion verdicts: proportionate 1' in out
    assert 'review items judged new: 1 of 3 (33%)' in out
    assert 'jev-shadow: 4 rows' in out
    assert 'jev tier pick matches final_tier: 2 of 2 (100%)' in out
    assert 'local tier pick matches final_tier: 1 of 2 (50%)' in out
    assert 'jev: 137.8 ms (n=9)' in out
    assert 'local: 151.4 ms (n=7)' in out


def test_missing_logs_render_an_empty_report(tmp_path, capsys):
    assert audit_report.main(['--storage', str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert 'materiality: 0 rows, 0 findings judged, 0 demoted' in out
    assert 'jev + local agree when both answered: 0 of 0' in out
    assert 'fix: 0 rows, 0 review items' in out
    assert 'jev proportion verdicts: none answered' in out
    assert 'review items judged new: 0 of 0' in out
    assert 'jev-shadow: 0 rows' in out
    assert 'jev tier pick matches final_tier: 0 of 0' in out
    assert 'jev: no timing' in out and 'local: no timing' in out
