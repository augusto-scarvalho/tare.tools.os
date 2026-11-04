"""Pre-spend falsifiers for missing dependencies and stale/unmapped test evidence."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from relay.audit_cases import checksum, json_bytes, main
from relay.audit_packet import prepare, verify, seal, correction, review_navigation
from relay.audit_preflight import inspect
from test_audit_packet import candidate, cases, request_data, selection, PacketProvider, run, review


@pytest.fixture
def mapped(selection):
    root = Path(selection['repositories'][0]['root'])
    (root / 'fix.py').write_text('from helper import needed\ndef answer():\n    return needed()\n')
    (root / 'helper.py').write_text('def needed():\n    return 2\n')
    (root / 'test_fix.py').write_text('from fix import answer\ndef test_answer():\n    assert answer() == 2\n')
    repo = selection['repositories'][0]
    repo['changes'][0]['symbols'] = ['answer']
    repo['excluded'] = [{'path': p, 'reason': 'Explicit dependency or test context'} for p in ['helper.py', 'test_fix.py']]
    repo['context'] = [{'id': ref, 'path': path, 'symbols': [symbol], 'why': 'Required for O1',
                        'obligation_ids': ['O1']} for ref, path, symbol in
                       [('D', 'helper.py', 'needed'), ('TEST', 'test_fix.py', 'test_answer')]]
    receipt = {'executor': 'implementer', 'exit_code': 0, 'command': ['pytest', 'test_fix.py'],
               'source_sha256': {'os:' + name: checksum((root / name).read_bytes())
                                 for name in ['fix.py', 'helper.py', 'test_fix.py']},
               'tests': {'test_fix.py::test_answer': 'passed'}}
    Path(selection['evidence'][0]['path']).write_bytes(json_bytes(receipt))
    selection['review_map'] = {'O1': {'contract_refs': ['C1.after'],
        'dependencies': [{'source_ref': 'D', 'symbol': 'needed'}],
        'tests': [{'source_ref': 'TEST', 'symbol': 'test_answer', 'receipt_ref': 'T1',
                   'nodeids': ['test_fix.py::test_answer']}]}}
    return selection


def test_mapped_packet_checks_claims_without_executing_candidate(mapped, tmp_path, capsys):
    packet = prepare(mapped)
    report = inspect(packet)
    assert report['readiness'] == 'DECLARED_INPUTS_CHECKED' and report['authority'] == 'NONE'
    assert any(row['id'] == 'TEST' for row in report['sources'])
    path = tmp_path / 'packet.json'; path.write_bytes(json_bytes(packet))
    store = tmp_path / 'never-created'
    assert main(['preflight', '--store', str(store), '--packet', str(path)]) == 0
    assert not store.exists()
    assert json.loads(capsys.readouterr().out)['readiness'] == 'DECLARED_INPUTS_CHECKED'


@pytest.mark.parametrize('mutation,error', [
    ('dependency', 'source_missing'), ('test_symbol', 'symbol_missing'),
    ('nodeid', 'test_not_passed'), ('stale', 'stale_test_sources'),
    ('skipped', 'test_not_passed'), ('counts_only', 'named_test_results'),
])
def test_preparation_refuses_missing_or_stale_evidence(mapped, mutation, error):
    mapping = mapped['review_map']['O1']
    path = Path(mapped['evidence'][0]['path']); receipt = json.loads(path.read_bytes())
    if mutation == 'dependency': mapped['repositories'][0]['context'].pop(0)
    elif mutation == 'test_symbol': mapping['tests'][0]['symbol'] = 'omitted_test'
    elif mutation == 'nodeid': mapping['tests'][0]['nodeids'] = ['test_fix.py::test_other']
    elif mutation == 'stale': receipt['source_sha256']['os:fix.py'] = '0' * 64
    elif mutation == 'skipped': receipt['tests']['test_fix.py::test_answer'] = 'skipped'
    else: receipt.pop('tests'); receipt['passed'] = 213
    path.write_bytes(json_bytes(receipt))
    with pytest.raises(ValueError, match='preflight_' + error): prepare(mapped)


def test_map_cannot_be_forged_after_sealing_and_profile_has_its_own_limit(mapped):
    packet = prepare(mapped)
    packet['input']['context']['review_map']['O1']['tests'][0]['nodeids'] = ['test_fix.py::test_missing']
    with pytest.raises(ValueError, match='preflight_test_not_passed'): verify(seal(packet))
    mapped['max_initial_context_bytes'] = 100
    with pytest.raises(ValueError, match='preflight_input_over_budget'): prepare(mapped)


def test_legacy_packet_reports_missing_map_and_unresolved_import(mapped):
    mapped.pop('review_map')
    mapped['repositories'][0]['context'].pop(0)
    report = inspect(prepare(mapped))
    assert report['readiness'] == 'NEEDS_PREPARATION'
    assert {'source_ref': 'C1.after', 'reference': 'helper.needed'} in report['unresolved_static_references']


def test_terminal_token_metrics_do_not_attest_auxiliary_author(selection, cases, request_data):
    class Metered(PacketProvider):
        def run(self, command, **kwargs):
            capture = kwargs['on_output']
            def on_output(raw):
                value = json.loads(raw)
                if value['type'] == 'result':
                    value['modelUsage'] = {'auxiliary': {'outputTokens': 7, 'thinkingTokens': 3,
                        'inputTokens': -1, 'cacheReadInputTokens': True, 'private': 'never copy'}}
                capture(json_bytes(value) + b'\n')
            return super().run(command, **{**kwargs, 'on_output': on_output})
    cases.begin_packet(prepare(selection), request_data)
    run(cases, request_data, Metered())
    result = cases.load(request_data['case_id'])['attempts'][-1]['result']
    assert result['metrics']['provider_token_usage'] == {'auxiliary': {'outputTokens': 7, 'thinkingTokens': 3}}
    assert result['primary_model_reported'] == request_data['auditor']['runtime_model']


def test_compact_correction_keeps_original_review_and_reset_binding(mapped, cases, request_data):
    for item in mapped['review_map']['O1']['tests']:
        item.pop('nodeids')  # A named family defaults to path::symbol.
    first = prepare(mapped)
    cases.begin_packet(first, request_data)
    run(cases, request_data, PacketProvider())
    state = cases.load(request_data['case_id'])
    previous_review = state['attempts'][-1]['result']['review']
    original = deepcopy(previous_review)
    path = Path(mapped['repositories'][0]['root']) / 'fix.py'
    path.write_text(path.read_text().replace('return needed()', 'return needed() + 0'))
    receipt_path = Path(mapped['evidence'][0]['path'])
    receipt = json.loads(receipt_path.read_bytes())
    receipt['source_sha256']['os:fix.py'] = checksum(path.read_bytes())
    receipt_path.write_bytes(json_bytes(receipt))
    mapped['packet_id'] = 'fix-r2'
    second = prepare(mapped)
    reply = {'schema': 'tare.tools/audit-correction/1', 'packet_id': 'fix-r1', 'dispositions': [{
        'finding_id': 'F1', 'disposition': 'FIXED', 'change_refs': ['C1'],
        'test_receipt_refs': ['T1'], 'reason': 'Test fixture delta'}]}
    projected = correction(second, first, previous_review, reply)
    assert projected['previous_review_archive'] == original == previous_review
    nav = projected['input']['previous_review']
    assert nav == review_navigation(original)
    assert nav['findings'][0]['claim'] == original['findings'][0]['claim']
    assert nav['context_requests'] == original['context_requests']
    damaged = deepcopy(projected)
    damaged['input']['previous_review']['findings'][0]['claim'] = 'rewritten'
    with pytest.raises(ValueError, match='navigation_mismatch'): verify(seal(damaged))
    cases.begin_packet(second, {'case_id': request_data['case_id'], 'continuation': {'reason': 'Correction'},
        'correction': reply, 'session_reset': {'expected_session_id': state['session_id'], 'reason': 'Explicit fixture reset'}}, resume=True)
    saved = cases.load(request_data['case_id'])
    verify(saved['attempts'][-1]['packet'])
    assert saved['attempts'][-1]['packet']['previous_review_archive'] == original
    assert saved['session_id'] is None and saved['session_history'][0]['session_id'] == state['session_id']
