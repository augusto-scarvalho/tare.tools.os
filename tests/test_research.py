"""Mass research with deterministic fetch and a stub local model; no network, no inference."""
import json
from pathlib import Path

import pytest

from relay import research
from relay.web_reader import WebDocument


SITE = {
    'https://docs.example.com/': '# Home\n\nSee [guide](/guide) and [api](https://docs.example.com/api) '
                                 'and [outside](https://evil.example.org/x).',
    'https://docs.example.com/guide': '# Guide\n\nThe cache TTL defaults to 5 minutes. [deep](/deep)',
    'https://docs.example.com/api': '# API\n\nUnrelated text.',
    'https://docs.example.com/deep': '# Deep\n\nShould not be crawled at depth 2.',
}


def fetch(url):
    text = SITE.get(url)
    if text is None:
        return WebDocument(url=url, title='', markdown='', raw_size_bytes=0, clean_size_bytes=0,
                           reduction_pct=0.0, method='failed', status='ERROR', error='missing')
    return WebDocument(url=url, title=url, markdown=text, raw_size_bytes=len(text) * 2,
                       clean_size_bytes=len(text), reduction_pct=50.0, method='trafilatura_local', status='OK')


def model(calls):
    def complete(messages):
        calls.append(messages)
        body = messages[1]['content']
        if messages[0]['content'] is research.MAP_PROMPT:
            sources = json.loads(body.split('SOURCES: ', 1)[1])
            facts = [{'ref': s['ref'], 'quote': 'The cache TTL defaults to 5 minutes.', 'fact': 'TTL is 5 min'}
                     for s in sources if 'TTL' in s['text']]
            return 'noise before ' + json.dumps({'facts': facts}) + ' noise after'
        facts = json.loads(body.split('FACTS: ', 1)[1])
        return json.dumps({'summary': 'TTL is five minutes.', 'evidence': [
            {'ref': facts[0]['ref'], 'claim': 'TTL is 5 min'}, {'ref': 'W-forged', 'claim': 'invented'}],
            'remaining': ['Is TTL configurable?']})
    return complete


def test_research_respects_limits_and_returns_cited_envelope(tmp_path):
    calls = []
    result = research.research(tmp_path, 'What is the cache TTL?', ['https://docs.example.com/'],
                               domains=['docs.example.com'], max_pages=10, max_depth=1,
                               fetch=fetch, complete=model(calls))
    assert result['status'] == 'COMPLETED', result['error']
    job = tmp_path / result['job_id']
    entries = [json.loads(line) for line in (job / 'manifest.jsonl').read_text().splitlines()]
    urls = {e['url'] for e in entries}
    assert urls == {'https://docs.example.com/', 'https://docs.example.com/guide', 'https://docs.example.com/api'}
    assert 'https://evil.example.org/x' not in urls and 'https://docs.example.com/deep' not in urls
    assert result['pages'] == 3 and result['pages_by_method'] == {'trafilatura_local': 3}
    assert {p.name for p in (job / 'corpus').iterdir()} == {e['sha256'] + '.md' for e in entries}
    guide = next(e for e in entries if e['url'].endswith('/guide'))
    envelope = result['envelope']
    assert envelope['evidence'] == [{'ref': guide['ref'], 'claim': 'TTL is 5 min'}]  # forged ref dropped
    assert envelope['remaining'] == ['Is TTL configurable?']
    assert result['facts'] == 1 and result['model_observations'] == [] and result['authority'] == 'NONE'
    assert len(calls) == 2  # one map batch (all pages fit), one reduce
    view = research.view(result)
    assert 'model_observations' not in view and 'PRIVATE' not in json.dumps(view)
    assert json.loads((job / 'result.json').read_text(encoding='utf-8'))['job_id'] == result['job_id']
    assert not (tmp_path / 'explorer.lock').exists()
    cited = research.read_ref(tmp_path, result['job_id'], guide['ref'], 1, 3)
    assert 'TTL defaults to 5 minutes' in cited['text'] and cited['sha256'] == guide['sha256']


def test_max_pages_and_collect_only_stop_before_inference(tmp_path):
    calls = []
    result = research.research(tmp_path, 'q', ['https://docs.example.com/'], domains=['docs.example.com'],
                               max_pages=1, max_depth=3, fetch=fetch, complete=model(calls), collect_only=True)
    assert result['status'] == 'COLLECTED' and result['pages'] == 1 and calls == []


def test_off_domain_seeds_or_bad_bounds_fail_closed(tmp_path):
    result = research.research(tmp_path, 'q', ['https://evil.example.org/'], domains=['docs.example.com'],
                               max_pages=5, max_depth=0, fetch=fetch, complete=model([]))
    assert result['status'] == 'FAILED' and 'no_readable_pages' in result['error']
    with pytest.raises(ValueError):
        research.research(tmp_path, 'q', ['https://docs.example.com/'], domains=[], max_pages=5, max_depth=0)
    with pytest.raises(ValueError):
        research.research(tmp_path, 'q', ['https://docs.example.com/'], domains=['docs.example.com'],
                          max_pages=0, max_depth=0)
    with pytest.raises(ValueError, match='ref_missing'):
        research.read_ref(tmp_path, result['job_id'], 'W-nope')


def test_active_lock_blocks_a_second_job(tmp_path):
    (tmp_path / 'explorer.lock').write_bytes(b'')
    result = research.research(tmp_path, 'q', ['https://docs.example.com/'], domains=['docs.example.com'],
                               max_pages=5, max_depth=0, fetch=fetch, complete=model([]))
    assert result['status'] == 'FAILED' and result['error'].startswith('FileExistsError')
    assert (tmp_path / 'explorer.lock').exists()  # not ours to remove


def test_shared_lock_serializes_research_and_explore(tmp_path):
    custody = tmp_path / 'custody'
    custody.mkdir()
    lock = custody / 'explorer.lock'
    lock.write_bytes(b'')
    result = research.research(custody / 'research', 'q', ['https://docs.example.com/'],
                               domains=['docs.example.com'], max_pages=5, max_depth=0,
                               fetch=fetch, complete=model([]), lock_path=lock)
    assert result['status'] == 'FAILED' and result['error'].startswith('FileExistsError')
    assert lock.exists()  # not ours to remove


def test_research_fail_closed_on_native_provider_error(tmp_path):
    from relay.native_generation import NativeGenerationError

    def failing_complete(messages):
        raise NativeGenerationError('provider exploded')

    result = research.research(tmp_path, 'What is the cache TTL?', ['https://docs.example.com/'],
                               domains=['docs.example.com'], max_pages=10, max_depth=1,
                               fetch=fetch, complete=failing_complete)
    assert result['status'] == 'FAILED'
    assert result['error'] == 'NativeGenerationError:provider exploded'
    assert not (tmp_path / 'explorer.lock').exists()


def test_research_fail_closed_releases_lock_when_result_write_fails(tmp_path, monkeypatch):
    import relay.research as mod

    original_encoded = mod.encoded

    def failing_encoded(value):
        raise OSError('disk full')

    monkeypatch.setattr(mod, 'encoded', failing_encoded)

    result = research.research(tmp_path, 'What is the cache TTL?', ['https://docs.example.com/'],
                               domains=['docs.example.com'], max_pages=10, max_depth=1,
                               fetch=fetch, complete=model([]))
    assert result['status'] == 'FAILED'
    assert result['error'] == 'OSError:disk full'
    assert not (tmp_path / 'explorer.lock').exists()


def test_programming_errors_propagate_and_release_lock(tmp_path):
    def broken_complete(messages):
        raise RuntimeError('bug in caller code')

    with pytest.raises(RuntimeError):  # no blanket except: bugs surface, the lock still goes
        research.research(tmp_path, 'What is the cache TTL?', ['https://docs.example.com/'],
                          domains=['docs.example.com'], max_pages=10, max_depth=1, fetch=fetch, complete=broken_complete)
    assert not (tmp_path / 'explorer.lock').exists()


def test_off_domain_redirect_is_rejected_and_not_crawled(tmp_path):
    def redirect_fetch(url):
        if url == 'https://docs.example.com/redirect':
            return WebDocument(url=url, title='Redirect', markdown='# Redirected' + chr(10) * 2 + 'Content from another host. [leak](/leaked)',
                               raw_size_bytes=100, clean_size_bytes=50, reduction_pct=50.0,
                               method='trafilatura_local', status='OK',
                               final_url='https://evil.example.org/landed')
        return fetch(url)

    result = research.research(tmp_path, 'q', ['https://docs.example.com/redirect', 'https://docs.example.com/'],
                               domains=['docs.example.com'], max_pages=5, max_depth=1,
                               fetch=redirect_fetch, complete=model([]), collect_only=True)
    assert result['status'] == 'COLLECTED' and result['pages'] == 3  # home, guide, api; the redirect is not a page
    job = tmp_path / result['job_id']
    entries = [json.loads(line) for line in (job / 'manifest.jsonl').read_text().splitlines()]
    redirect_entry = next(e for e in entries if e['url'] == 'https://docs.example.com/redirect')
    assert redirect_entry['status'] == 'OFF_DOMAIN_REDIRECT'
    assert redirect_entry['final_url'] == 'https://evil.example.org/landed'
    assert redirect_entry['bytes'] == 0
    assert redirect_entry['sha256'] is None
    assert redirect_entry['ref'] is None
    # The redirected page's text never reaches the corpus, and its links are not crawled (depth 1 was available)
    corpus = b''.join(f.read_bytes() for f in (job / 'corpus').iterdir())
    assert b'another host' not in corpus
    assert {e['url'] for e in entries} == {'https://docs.example.com/redirect', 'https://docs.example.com/',
                                           'https://docs.example.com/guide', 'https://docs.example.com/api'}
    assert all(e['final_url'] is None for e in entries if e['status'] == 'OK')  # the stub fetch reports none


def test_tree_reduce_and_telemetry_rows(tmp_path):
    from scripts.round_table.report_round_table_telemetry import extract
    facts = [{'ref': f'W-{i}', 'quote': 'x' * 50, 'fact': 'f'} for i in range(6)]
    seen = []
    def complete(messages):
        seen.append(json.loads(messages[1]['content'].split('FACTS: ', 1)[1]))
        return json.dumps({'summary': 's', 'evidence': [{'ref': seen[-1][0]['ref'], 'claim': 'c'}], 'remaining': []})
    envelope = research.reduce_facts('q', facts, complete, 250, [])
    assert len(seen) == 4 and len(seen[-1]) == 3  # 3 partials of 2 facts, then the final over their claims
    assert envelope['evidence'] == [{'ref': 'W-0', 'claim': 'c'}]
    receipt = {'schema': research.SCHEMA, 'job_id': 'research-1', 'model_observations': [
        {'model': 'qwen38', 'started_at': '2026-09-07T00:00:00+00:00', 'duration_ms': 5,
         'usage': {'prompt_tokens': 10, 'completion_tokens': 2}}]}
    rows = list(extract(receipt, Path('research-1/result.json')))
    assert [(r['origin'], r['group'], r['input_tokens']) for r in rows] == [('research', 'research-1', 10)]


def _reducer(calls, claim='c', forge=False):
    """Stub reduce model: cites the first ref of what it received, optionally a forged one; records payload sizes."""
    def complete(messages):
        body = messages[1]['content'].split('FACTS: ', 1)[1]
        calls.append(len(body.encode()))
        first = json.loads(body)[0]['ref']
        evidence = [{'ref': first, 'claim': claim}] + ([{'ref': 'W-forged', 'claim': 'invented'}] if forge else [])
        return json.dumps({'summary': 's', 'evidence': evidence, 'remaining': []})
    return complete


def test_forged_ref_in_partial_reduce_never_reaches_the_final_call():
    facts = [{'ref': f'W-{i}', 'quote': 'x' * 50, 'fact': 'f'} for i in range(6)]  # 3 partial groups at limit 250
    seen = []

    def complete(messages):
        seen.append(json.loads(messages[1]['content'].split('FACTS: ', 1)[1]))
        return _reducer([], forge=True)(messages)

    envelope = research.reduce_facts('q', facts, complete, 250, [])
    assert len(seen) == 4  # 3 partials + final: the tree path is exercised, not only the final filter
    assert all(f['ref'] != 'W-forged' for f in seen[-1])  # a forged partial ref is dropped before the final call
    assert envelope['evidence'] == [{'ref': 'W-0', 'claim': 'c'}]


def test_partials_over_limit_keep_reducing_until_they_fit_overflow():
    facts = [{'ref': f'W-{i}', 'quote': 'x' * 70, 'fact': 'f'} for i in range(12)]  # 2 per group at limit 250
    sizes = []
    envelope = research.reduce_facts('q', facts, _reducer(sizes, claim='c' * 60), 250, [])
    assert envelope['evidence'] == [{'ref': 'W-0', 'claim': 'c' * 60}]
    assert max(sizes) <= 250 and len(sizes) == 12  # 6 + 3 + 2 partial calls + final: every prompt fits one batch


def test_reduce_that_cannot_shrink_fails_closed_instead_of_looping():
    facts = [{'ref': f'W-{i}', 'quote': 'x' * 100, 'fact': 'f'} for i in range(4)]
    with pytest.raises(ValueError, match='reduce_did_not_converge'):  # claims as long as the limit never fit two per group
        research.reduce_facts('q', facts, _reducer([], claim='c' * 240), 250, [])


def test_max_bytes_bounds_utf8_bytes_and_never_splits_a_character(tmp_path):
    accented = chr(233) * 4  # four two-byte characters: 8 bytes

    def fetch_accented(url):
        return WebDocument(url=url, title='t', markdown=accented, raw_size_bytes=8, clean_size_bytes=8,
                           reduction_pct=0.0, method='trafilatura_local', status='OK')

    result = research.research(tmp_path, 'q', ['https://docs.example.com/'], domains=['docs.example.com'],
                               max_pages=1, max_depth=0, max_bytes=5, fetch=fetch_accented, complete=model([]), collect_only=True)
    job = tmp_path / result['job_id']
    entry = json.loads((job / 'manifest.jsonl').read_text().splitlines()[0])
    stored = (job / 'corpus' / (entry['sha256'] + '.md')).read_bytes()
    assert entry['bytes'] == 4 and stored == (chr(233) * 2).encode('utf-8')  # 5 bytes requested: 2 whole characters, not 2.5


def test_domain_allowlist_is_case_and_dot_insensitive(tmp_path):
    result = research.research(tmp_path, 'q', ['https://docs.example.com/'], domains=['DOCS.Example.COM', '.docs.example.com'],
                               max_pages=5, max_depth=0, fetch=fetch, complete=model([]), collect_only=True)
    assert result['status'] == 'COLLECTED' and result['pages'] == 1
    assert result['limits']['domains'] == ['docs.example.com']


def test_missing_usage_counters_stay_unknown_not_zero(tmp_path):
    def complete_without_usage(messages):
        return model([])(messages)

    result = research.research(tmp_path, 'What is the cache TTL?', ['https://docs.example.com/'],
                               domains=['docs.example.com'], max_pages=10, max_depth=1, fetch=fetch, complete=complete_without_usage)
    assert result['status'] == 'COMPLETED'
    assert result['tokens'] == {'input': None, 'output': None} and result['token_source'] is None  # no observed usage
    collected = research.research(tmp_path, 'q', ['https://docs.example.com/'], domains=['docs.example.com'],
                                  max_pages=1, max_depth=0, fetch=fetch, complete=model([]), collect_only=True)
    assert collected['tokens'] == {'input': 0, 'output': 0} and collected['token_source'] == 'no_model_calls'


def test_read_ref_rejects_paths_as_job_ids_and_tampered_manifests(tmp_path):
    result = research.research(tmp_path, 'q', ['https://docs.example.com/'], domains=['docs.example.com'],
                               max_pages=1, max_depth=0, fetch=fetch, complete=model([]), collect_only=True)
    job = tmp_path / result['job_id']
    entry = json.loads((job / 'manifest.jsonl').read_text().splitlines()[0])
    assert research.read_ref(tmp_path, result['job_id'], entry['ref'], 1, 1)['text'].startswith('# Home')
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'result.json').write_text('{}')
    for bad in (str(outside), '../outside', 'research-not-hex', result['job_id'] + '/..'):
        with pytest.raises(ValueError, match='research_job_id_required'):
            research.read_ref(tmp_path, bad, entry['ref'], 1, 1)
        with pytest.raises(ValueError, match='research_job_id_required'):
            research.job_dir(tmp_path, bad)
    # A manifest whose sha256 is a path, or whose corpus bytes changed, never yields text
    (job / 'other.md').write_text('planted')
    tampered = dict(entry, sha256='../other')
    (job / 'manifest.jsonl').write_text(json.dumps(tampered) + chr(10))
    with pytest.raises(ValueError, match='sha256_invalid'):
        research.read_ref(tmp_path, result['job_id'], entry['ref'], 1, 1)
    (job / 'manifest.jsonl').write_text(json.dumps(entry) + chr(10))
    (job / 'corpus' / (entry['sha256'] + '.md')).write_bytes(b'# Changed')
    with pytest.raises(ValueError, match='corpus_hash_mismatch'):
        research.read_ref(tmp_path, result['job_id'], entry['ref'], 1, 1)


def test_unparseable_final_url_is_off_domain_not_a_pass(tmp_path):
    def fetch_ftp(url):
        return WebDocument(url=url, title='t', markdown='# Moved' + chr(10) * 2 + '[child](/guide)', raw_size_bytes=1,
                           clean_size_bytes=1, reduction_pct=0.0, method='trafilatura_local', status='OK',
                           final_url='ftp://evil.example.org/page')

    result = research.research(tmp_path, 'q', ['https://docs.example.com/'], domains=['docs.example.com'],
                               max_pages=5, max_depth=1, fetch=fetch_ftp, complete=model([]), collect_only=True)
    assert result['status'] == 'FAILED' and 'no_readable_pages' in result['error']  # nothing collected, nothing crawled
    entries = [json.loads(line) for line in (tmp_path / result['job_id'] / 'manifest.jsonl').read_text().splitlines()]
    assert [(e['status'], e['ref']) for e in entries] == [('OFF_DOMAIN_REDIRECT', None)]
    assert not list((tmp_path / result['job_id'] / 'corpus').iterdir())


def test_reduce_budget_counts_the_serialized_list_and_caps_partial_claims():
    facts = [{'ref': f'W-{i}', 'quote': 'x' * 70, 'fact': 'f'} for i in range(12)]
    sizes = []
    envelope = research.reduce_facts('q', facts, _reducer(sizes, claim='c' * 1000), 250, [])
    assert max(sizes) <= 250 and len(envelope['evidence'][0]['claim']) == 1000  # prompts bounded; the final answer is not cut
    for group in research.batches(facts, lambda f: len(research.encoded(f)), 250):
        assert len(research.encoded(group)) <= 250  # brackets and separators are part of the budget
    with pytest.raises(ValueError, match='fact_exceeds_batch_limit'):  # a single fact larger than the budget cannot be honoured
        research.reduce_facts('q', [{'ref': 'W-0', 'quote': 'x' * 1000, 'fact': 'f'}], _reducer([]), 250, [])


def test_partial_usage_counters_do_not_sum_into_a_total(tmp_path, monkeypatch):
    import relay.research as mod

    def completion_with_partial_usage(deployment, receipts, **kwargs):
        def complete(messages):
            receipts.append({'generation_calls': 1, 'usage': {'prompt_tokens': 12, 'completion_tokens': None}})
            return model([])(messages)
        return complete

    monkeypatch.setattr(mod, 'native_completion', completion_with_partial_usage)
    result = research.research(tmp_path, 'What is the cache TTL?', ['https://docs.example.com/'],
                               domains=['docs.example.com'], max_pages=10, max_depth=1, fetch=fetch)
    assert result['status'] == 'COMPLETED' and result['model_calls'] == 2
    assert result['tokens'] == {'input': None, 'output': None}


def test_custody_paths_are_checked_after_resolving_links(tmp_path):
    import os
    result = research.research(tmp_path, 'q', ['https://docs.example.com/'], domains=['docs.example.com'],
                               max_pages=1, max_depth=0, fetch=fetch, complete=model([]), collect_only=True)
    job = tmp_path / result['job_id']
    entry = json.loads((job / 'manifest.jsonl').read_text().splitlines()[0])
    outside = tmp_path.parent / (tmp_path.name + '-outside')
    outside.mkdir()
    corpus_file = job / 'corpus' / (entry['sha256'] + '.md')
    (outside / corpus_file.name).write_bytes(corpus_file.read_bytes())  # same bytes, valid hash, wrong place
    corpus_file.unlink()
    try:
        os.symlink(outside / corpus_file.name, corpus_file)
    except (OSError, NotImplementedError):
        pytest.skip('symlinks not permitted on this host')
    with pytest.raises(ValueError, match='escapes_custody'):
        research.read_ref(tmp_path, result['job_id'], entry['ref'], 1, 1)
    linked_job = tmp_path / ('research-' + 'b' * 32)
    os.symlink(outside, linked_job, target_is_directory=True)
    with pytest.raises(ValueError, match='escapes_custody'):
        research.job_dir(tmp_path, linked_job.name)


def test_dot_only_allowlist_entries_are_dropped_and_fail_closed(tmp_path):
    assert research.normalize_domains(['.', '', ' ', 'Docs.Example.com.']) == ['docs.example.com']
    assert not research.allowed('https://evil.example.org.', research.normalize_domains(['.']))
    with pytest.raises(ValueError, match='explicit_seeds_domains'):
        research.research(tmp_path, 'q', ['https://docs.example.com/'], domains=['.', '..'], max_pages=1, max_depth=0)


def test_corpus_and_manifest_must_resolve_inside_their_own_roots(tmp_path):
    import os
    result = research.research(tmp_path, 'q', ['https://docs.example.com/'], domains=['docs.example.com'],
                               max_pages=1, max_depth=0, fetch=fetch, complete=model([]), collect_only=True)
    job = tmp_path / result['job_id']
    entry = json.loads((job / 'manifest.jsonl').read_text().splitlines()[0])
    corpus_file = job / 'corpus' / (entry['sha256'] + '.md')
    (job / 'private.md').write_bytes(corpus_file.read_bytes())  # inside the job, outside corpus/, hash still valid
    corpus_file.unlink()
    try:
        os.symlink(job / 'private.md', corpus_file)
    except (OSError, NotImplementedError):
        pytest.skip('symlinks not permitted on this host')
    with pytest.raises(ValueError, match='escapes_custody'):
        research.read_ref(tmp_path, result['job_id'], entry['ref'], 1, 1)
    outside = tmp_path.parent / (tmp_path.name + '-manifest')
    outside.mkdir()
    (outside / 'manifest.jsonl').write_bytes((job / 'manifest.jsonl').read_bytes())
    (job / 'manifest.jsonl').unlink()
    os.symlink(outside / 'manifest.jsonl', job / 'manifest.jsonl')
    with pytest.raises(ValueError, match='escapes_custody'):
        research.read_ref(tmp_path, result['job_id'], entry['ref'], 1, 1)
