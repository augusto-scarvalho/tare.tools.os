import json
from pathlib import Path

import pytest

from scripts.round_table.report_round_table_telemetry import collect, extract, main, render_html, row, summarize


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding='utf-8')


def test_duplicate_receipts_and_parent_delegation_are_counted_once(tmp_path):
    call = {'model': 'qwen38', 'usage': {'prompt_tokens': 100, 'completion_tokens': 10,
            'prompt_tokens_details': {'cached_tokens': 80}}}
    explorer = {'schema': 'tare.tools/audit-exploration-result/2', 'request_id': 'explore-1',
                'model_observations': [call], 'report': 'PRIVATE EXPLORATION'}
    save(tmp_path / 'explore-1/result.json', explorer)
    save(tmp_path / 'session.json', {'schema': 'tare.tools/aider-session/1', 'id': 'parent',
        'history': {'messages': ['PRIVATE PROMPT']}, 'model_observations': [call],
        'delegation_observations': [explorer]})
    seat = {'seat': 'auditor', 'execution_nonce': 'seat-1', 'ok': True,
            'deployment': {'runtime_model': 'fable', 'attempt_id': 'attempt-1'},
            'raw_output': 'PRIVATE VOTE', 'vote': {'text': 'PRIVATE VOTE'}}
    save(tmp_path / 'rounds/r001/seat_auditor.json', seat)
    save(tmp_path / 'rounds/r001/seat_auditor_fable.json', seat)
    report = collect([tmp_path, tmp_path / 'session.json'])
    assert report['summary']['records'] == 3
    assert report['summary']['calls'] == 2
    assert report['summary']['aggregated_executions'] == 1
    assert report['summary']['input_tokens'] == 200
    assert report['summary']['cache_reuse_ratio'] == .8
    assert next(r for r in report['rows'] if r['origin'] == 'explorer')['group'] == 'parent'
    assert 'PRIVATE' not in json.dumps(report)
    assert not report['warnings']


def test_unknowns_cost_provenance_weighted_cache_and_latency():
    rows = [row('1', 'g', 'managed', 'a', input_tokens=100, cache_read_tokens=80,
                reported_cost_usd='0.1', estimated_cost_usd='0.9', duration_ms=10),
            row('2', 'g', 'managed', 'a', input_tokens=900, cache_read_tokens=0,
                estimated_cost_usd='0.2', duration_ms=30),
            row('3', 'g', 'audit', 'b', unit='attempt', input_tokens=10000, duration_ms=100000),
            row('4', 'g', 'managed', 'a', status='ERROR')]
    summary = summarize(rows)
    assert summary['cache_reuse_ratio'] == .08
    assert summary['reported_cost_usd'] == '0.1'
    assert summary['estimated_cost_usd'] == '0.2'
    assert summary['cost_records'] == 2
    assert summary['cache_hits'] == summary['cache_misses'] == 1
    assert summary['latency_p50_ms'] == 20 and summary['latency_p95_ms'] == 30
    assert summary['errors'] == 1
    assert rows[2]['cache_status'] == 'UNKNOWN'
    assert summarize([rows[3]])['reported_cost_usd'] is None


def test_cli_aggregate_includes_all_input_components_without_attributing_multiple_models():
    usage = {'inputTokens': 20, 'outputTokens': 5, 'cacheReadInputTokens': 80,
             'cacheCreationInputTokens': 10}
    receipt = {'schema': 'tare.tools/audit-result/1', 'case_id': 'case', 'run_id': 'r1',
               'metrics': {'provider_token_usage': {'a': usage, 'b': usage},
                           'cost_estimate_usd': {'value': .5}}}
    result, = extract(receipt, Path('result.json'))
    assert result['input_tokens'] == 220 and result['cache_read_tokens'] == 160
    assert result['unit'] == 'attempt'
    assert result['model'] == 'Several models (aggregated execution)'
    assert result['reported_cost_usd'] is None and result['estimated_cost_usd'] == '0.5'
    del usage['cacheCreationInputTokens']
    partial, = extract(receipt, Path('result.json'))
    assert partial['input_tokens'] is None


def test_exact_explorer_supersedes_legacy_and_missing_sources_are_visible(tmp_path):
    legacy = {'schema': 'tare.tools/audit-exploration-result/2', 'request_id': 'explore-1',
              'tokens': {'input': 0, 'output': 0}, 'token_source': None}
    result, = extract(legacy, Path('result.json'))
    assert result['input_tokens'] is None
    save(tmp_path / 'old/result.json', legacy)
    save(tmp_path / 'new/result.json', {**legacy, 'model_observations': [{'model': 'qwen38', 'error': 'TimeoutError'}]})
    report = collect([tmp_path, tmp_path / 'missing'])
    assert len(report['rows']) == 1 and report['rows'][0]['status'] == 'ERROR'
    assert report['warnings'] == [{'source': 'missing', 'reason': 'source_not_found'}]


def test_html_embeds_untrusted_labels_as_data_and_cli_writes_offline_outputs(tmp_path, capsys):
    payload = '</script><script>alert("unsafe")</script>'
    save(tmp_path / 'session.json', {'schema': 'tare.tools/aider-session/1', 'id': 'session',
         'model_observations': [{'model': payload, 'error': 'TimeoutError'}]})
    assert main(['--source', str(tmp_path), '--html', str(tmp_path / 'index.html'),
                 '--json', str(tmp_path / 'telemetry.json')]) == 0
    html = (tmp_path / 'index.html').read_text(encoding='utf-8')
    assert payload not in html and '\\u003c/script\\u003e' in html
    assert '__TELEMETRY_DATA__' not in html
    assert json.loads((tmp_path / 'telemetry.json').read_text(encoding='utf-8'))['summary']['calls'] == 1
    assert '1 calls' in capsys.readouterr().out


def test_round_table_retries_and_roles_count_actual_attempts_only(tmp_path):
    save(tmp_path / 'rounds/r001/seat_a.json', {'seat': 'a', 'execution_nonce': 'n',
         'deployment': {'semantic_attempts': [{'runtime_model': 'one'}, {'runtime_model': 'two'}]}})
    save(tmp_path / 'rounds/r001/mediation.json', {'attempts': [
        {'deployment': 'skipped', 'ok': False},
        {'receipt': {'attempt_id': 'med-1', 'runtime_model': 'three'}, 'ok': True}]})
    save(tmp_path / 'decision/scribe.json', {'attempts': [{'deployment': 'deterministic', 'ok': True}]})
    report = collect([tmp_path])
    assert report['summary']['aggregated_executions'] == 3
    assert {r['model'] for r in report['rows']} == {'one', 'two', 'three'}


@pytest.mark.parametrize('invalid', [-1, float('nan'), float('inf'), True])
def test_invalid_counters_are_unknown(invalid):
    result = row('1', 'g', 'managed', 'm', input_tokens=invalid, cache_read_tokens=invalid,
                 reported_cost_usd=invalid)
    assert result['input_tokens'] is None and result['reported_cost_usd'] is None
    assert result['cache_status'] == 'UNKNOWN'
