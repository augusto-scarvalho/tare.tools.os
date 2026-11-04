"""Real tool edits reviewed against local bytes, without a model service."""
import json
from pathlib import Path
import time

import pytest

from compute_plane.native_capabilities import NativeCapabilities
from compute_plane.native_review import digest, receipt_path
from relay.native_review import summarize, render
from tare_tools_agent_runtime import BindingRef, CancellationToken, ExecutionControl, EffectRequest, ToolCall


@pytest.fixture
def session(tmp_path):
    repo, root, scratch = tmp_path/'repo', tmp_path/'session', tmp_path/'scratch'
    for p in (repo, root, scratch):
        p.mkdir()
    (repo/'a.py').write_bytes(b'# existing user change\r\nvalue = 1\r\n')
    binding = BindingRef('w', 'b', 'o', 1, 't')
    control = ExecutionControl(binding, CancellationToken(), time.monotonic()+30)
    tools = NativeCapabilities(session_id='s', binding=binding, repositories={'repo': str(repo)},
        writable={'repo': ['a.py', 'new.txt']}, verifiers={}, scratch_root=scratch,
        specgraph_edit_context=False, review_root=root/'review')
    state = {'session_id': 's', 'instruction': 'Change value', 'config': {'repositories': {'repo': str(repo)}}}
    events = []

    def edit(search='value = 1', replace='value = 2', path='a.py', sha='current'):
        source = repo/path
        before = source.read_bytes() if source.exists() else None
        args = {'repository': 'repo', 'path': path, 'sha256': digest(before) if sha == 'current' else sha,
                'search': search, 'replace': replace}
        effect_id = f's/effect/{len(events)+1}'
        request = {'kind': 'effect_requested', 'effect_id': effect_id,
                   'tool_call': {'name': 'edit', 'arguments_json': json.dumps(args)}}
        result = tools.execute(EffectRequest('s', effect_id, ToolCall('t', 'edit', json.dumps(args))), control)
        events.extend([request, {'kind': 'effect_returned', 'result': {
            'effect_id': effect_id, 'status': result.status, 'output_json': result.output_json}}])
        return result

    def check(code=0):
        eid = f's/effect/{len(events)+1}'
        events.extend([{'kind': 'effect_requested', 'effect_id': eid,
                       'tool_call': {'name': 'verify', 'arguments_json': '{"name":"unit"}'}},
                      {'kind': 'effect_returned', 'result': {'effect_id': eid,
                       'status': 'succeeded' if code == 0 else 'failed',
                       'output_json': json.dumps({'returncode': code, 'output': 'receipt'})}}])

    return repo, root, tools, events, edit, check, lambda: summarize(root, state, events)


def test_actual_edit_preserves_prior_bytes_and_has_no_model_payload_growth(session):
    repo, root, tools, events, edit, check, report = session
    result = edit()
    assert result.status == 'succeeded'
    assert 'existing user change' not in result.output_json
    assert not tools.observations()['review_errors']
    receipt = json.loads(receipt_path(root/'review', events[0]['effect_id']).read_text())
    assert (root/'review'/'blobs'/receipt['before']).read_bytes() == b'# existing user change\r\nvalue = 1\r\n'
    diff = report()['edits'][0]['diff']
    assert '-value = 1' in diff and '+value = 2' in diff
    before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
    report(); report()
    assert before == {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_external_edit_and_later_session_edit_never_become_one_diff(session):
    repo, root, tools, events, edit, check, report = session
    edit(); check()
    assert report()['checks'][0]['freshness'] == 'OBSERVED_FILES_MATCH'
    (repo/'a.py').write_bytes(b'# external edit\nvalue = 3\n')
    assert report()['checks'][0]['freshness'] == 'STALE'
    edit('value = 3', 'value = 4')
    rows = report()['edits']
    assert len(rows) == 2
    assert '+# external edit' not in rows[1]['diff']
    assert '-value = 3' in rows[1]['diff']
    check(1)
    assert [c['returncode'] for c in report()['checks']] == [0, 1]
    assert report()['checks'][0]['freshness'] == 'STALE'


@pytest.mark.parametrize('damage', ['legacy', 'corrupt'])
def test_missing_or_corrupt_evidence_does_not_invent_diff(session, damage):
    repo, root, tools, events, edit, check, report = session
    edit()
    path = receipt_path(root/'review', events[0]['effect_id'])
    if damage == 'legacy':
        path.unlink()
    else:
        receipt = json.loads(path.read_text())
        (root/'review'/'blobs'/receipt['before']).write_bytes(b'bad')
    result = report()
    assert result['edits'][0]['diff'] is None
    assert 'Diff unavailable' in render(result)


def test_refused_edit_has_no_snapshot_and_snapshot_failure_does_not_retry_edit(session):
    repo, root, tools, events, edit, check, report = session
    assert edit(sha='0'*64).status == 'refused'
    assert not (root/'review').exists()
    (root/'review').write_text('blocked storage')
    assert edit().status == 'succeeded'
    assert tools.observations()['review_errors']
    assert report()['edits'][-1]['diff'] is None


def test_new_file_and_unresolved_effect_are_visible(session):
    repo, root, tools, events, edit, check, report = session
    edit('', 'new content', 'new.txt'); check()
    assert '+new content' in report()['edits'][0]['diff']
    events.append({'kind': 'effect_requested', 'effect_id': 'pending',
                   'tool_call': {'name': 'edit', 'arguments_json': '{}'}})
    assert report()['unresolved_effects'] == ['pending']
    assert report()['checks'][0]['freshness'] == 'UNKNOWN'


def test_terminal_mutation_invalidates_recorded_check_even_if_bytes_match(session):
    repo, root, tools, events, edit, check, report = session
    edit(); check()
    events.extend([{'kind':'effect_requested','effect_id':'command',
        'tool_call':{'name':'terminal_start','arguments_json':'{}'}},
        {'kind':'effect_returned','result':{'effect_id':'command','status':'succeeded','output_json':'{}'}}])
    check()
    checks = report()['checks']
    assert checks[0]['freshness']=='STALE'
    assert checks[1]['freshness']=='UNKNOWN'


def test_turn_filter_and_interactive_diff_limit(session):
    repo, root, tools, events, edit, check, report = session
    edit(); check(); boundary=len(events)
    edit('value = 2','value = 3'); check()
    state={'session_id':'s','instruction':'Edit','config':{'repositories':{'repo':str(repo)}}}
    first=summarize(root,state,events,end=boundary)
    second=summarize(root,state,events,start=boundary,max_diff_bytes=1)
    assert len(first['edits'])==len(second['edits'])==1
    assert '+value = 2' in first['edits'][0]['diff']
    assert second['edits'][0]['diff'] is None
    assert 'preview limit' in second['edits'][0]['diff_unavailable']
    metadata=summarize(root,state,events,include_diffs=False)
    assert all(row['diff'] is None for row in metadata['edits'])
