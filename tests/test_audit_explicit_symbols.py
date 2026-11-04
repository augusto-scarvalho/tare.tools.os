"""Real-file falsifiers for explicit partial review, not an alternative audit store."""
from copy import deepcopy
from pathlib import Path
import json

import pytest

from relay.audit_packet import prepare, verify, seal, recheck, budget, correction, validate_review
from relay.relay_io import write_json_atomic
from test_audit_packet import selection, candidate, cases, request_data, review


@pytest.fixture
def large_selection(selection):
    path = Path(selection['repositories'][0]['root']) / 'fix.py'
    path.write_text("VALUE = 1\n\nclass Service:\n    label = 'retained class context'\n"
                    "    def answer(self):\n        return VALUE\n\n"
                    "    def unrelated(self):\n" + "        # unselected body padding\n" * 1000 +
                    "        return 'untouched'\n", encoding='utf-8')
    selection['repositories'][0]['changes'][0]['symbols'] = ['Service.answer']
    return selection


def after(packet):
    return next(s for s in packet['input']['sources'] if s['id'] == 'C1.after')


def test_large_file_uses_complete_method_and_scaffolding_without_losing_custody(large_selection):
    packet = prepare(large_selection)
    source = after(packet)
    text = ''.join(s['text'] for s in source['segments'])
    assert 'class Service:' in text and 'retained class context' in text
    assert 'def answer(self):\n        return VALUE' in text.replace('\r\n', '\n')
    assert 'unselected body padding' not in text
    assert len(packet['source_archive']['C1.after']) > 24000
    assert budget(packet)['initial_context_bytes'] < 24000
    assert packet['input']['changes'][0]['omitted_changed_lines']['after'] >= 1000
    diff = next(s['text'] for s in packet['input']['sources'] if s['kind'] == 'diff')
    assert 'unselected body padding' not in diff
    value = review(packet)
    value['findings'][0]['location'].update(start_line=6, end_line=6)
    validate_review(packet, value)
    value['findings'][0]['location'].update(start_line=9, end_line=9)
    with pytest.raises(ValueError, match='outside_source'):
        validate_review(packet, value)
    import jsonschema
    schema = json.loads((Path(__file__).resolve().parents[1] / 'schemas/audit-input-v1.schema.json').read_bytes())
    jsonschema.validate(packet['input'], schema)
    path = Path(large_selection['repositories'][0]['root']) / 'fix.py'
    path.write_text(path.read_text().replace("'untouched'", "'outside edit'"))
    with pytest.raises(ValueError, match='source_changed'):
        recheck(packet)


@pytest.mark.parametrize('names', [[], ['absent'], ['Service.answer', 'Service.answer'], [1]])
def test_invalid_or_missing_selection_is_refused(large_selection, names):
    large_selection['repositories'][0]['changes'][0]['symbols'] = names
    with pytest.raises(ValueError, match='symbol_selection|symbol_missing'):
        prepare(large_selection)


@pytest.mark.parametrize('mutation', ['segment', 'archive'])
def test_resealed_forged_projection_is_rejected(large_selection, mutation):
    packet = prepare(large_selection)
    if mutation == 'segment':
        after(packet)['segments'][0]['text'] = 'forged\n'
    else:
        packet['source_archive']['C1.after'] += '# forged\n'
    with pytest.raises(ValueError, match='symbol_projection|symbol_archive'):
        verify(seal(packet))


def test_fixed_requires_delta_inside_selected_projection(large_selection):
    first = prepare(large_selection)
    value = review(first)
    value['findings'][0]['location'].update(start_line=6, end_line=6)
    reply = {'schema': 'tare.tools/audit-correction/1', 'packet_id': first['input']['packet_id'],
             'dispositions': [{'finding_id': 'F1', 'disposition': 'FIXED', 'change_refs': ['C1'],
                               'test_receipt_refs': ['T1'], 'reason': ''}]}
    path = Path(large_selection['repositories'][0]['root']) / 'fix.py'
    path.write_text(path.read_text().replace("'untouched'", "'outside edit'"))
    large_selection['packet_id'] = 'fix-r2'
    with pytest.raises(ValueError, match='empty_delta'):
        correction(prepare(large_selection), first, value, reply)
    path.write_text(path.read_text().replace('return VALUE', 'return 2'))
    delta = correction(prepare(large_selection), first, value, reply)
    diff = next(s['text'] for s in delta['input']['sources'] if s['id'] == 'C1.diff')
    assert '-        return VALUE' in diff and '+        return 2' in diff
    assert 'outside edit' not in diff and budget(delta)['initial_context_bytes'] < 24000
    assert delta['input']['changes'][0]['diff_basis'] == first['input']['packet_id']


def test_resume_freezes_selection_even_without_findings(large_selection, cases, request_data):
    packet = prepare(large_selection)
    cases.begin_packet(packet, request_data)
    state = cases.load(request_data['case_id'])
    state['status'] = 'EXECUTION_FAILED'
    state['attempts'][-1]['result'] = {'review': None}
    write_json_atomic(cases.path(request_data['case_id']), state)
    large_selection['packet_id'] = 'fix-r2'
    large_selection['repositories'][0]['changes'][0]['symbols'] = ['@module']
    with pytest.raises(ValueError, match='symbol_review_scope_changed'):
        cases.begin_packet(prepare(large_selection), {'case_id': request_data['case_id'],
                           'continuation': {'reason': 'Try dropping reviewed method'}}, resume=True)


def test_nested_closure_is_not_cut_from_its_enclosing_function(selection):
    path = Path(selection['repositories'][0]['root']) / 'fix.py'
    path.write_text('def outer():\n    captured = 2\n    def inner():\n        return captured\n    return inner\n')
    selection['repositories'][0]['changes'][0]['symbols'] = ['outer.inner']
    packet = prepare(selection)
    assert ''.join(s['text'] for s in after(packet)['segments']).encode() == path.read_bytes()


def test_new_file_references_exact_selected_source_instead_of_duplicate_diff(large_selection):
    root = Path(large_selection['repositories'][0]['root'])
    (root / 'new.py').write_bytes((root / 'fix.py').read_bytes())
    (root / 'fix.py').write_bytes(b'pass\n')
    large_selection['repositories'][0]['changes'][0]['path'] = 'new.py'
    packet = prepare(large_selection)
    diff = next(s['text'] for s in packet['input']['sources'] if s['kind'] == 'diff')
    assert 'Added file' in diff and 'source C1.after' in diff
    assert 'def answer' not in diff and 'def answer' in ''.join(s['text'] for s in after(packet)['segments'])


def test_missing_context_symbol_and_indivisible_symbol_fail_closed(large_selection):
    repo = large_selection['repositories'][0]
    repo['context'] = [{'id': 'CTX', 'path': 'fix.py', 'symbols': ['unknown'],
                        'why': 'Required dependency', 'obligation_ids': ['O1']}]
    with pytest.raises(ValueError, match='context_symbol_missing'):
        prepare(large_selection)
    repo['context'] = []
    repo['changes'][0]['symbols'] = ['Service']
    with pytest.raises(ValueError, match='prompt_over_budget'):
        prepare(large_selection)
