"""Cross-repository audit lifecycle: independent evidence, correction and authority."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from compute_plane.audit_driver import ClaudeAuditDriver
from relay.native_audit_access import AuditAccess
from test_native_audit_access import prefix
from relay.audit_cases import checksum, json_bytes, validate_limits, attempt_limit_reached
from relay.audit_execution import execute_packet
from relay.audit_packet import prepare, validate_review
from test_audit_cases import cases, request_data
from test_audit_execution import candidate
from test_audit_packet import selection, PacketProvider


class IndependentProvider(PacketProvider):
    skip_exploration = False

    def run(self, command, *, prompt, **kwargs):
        launcher = Path(prefix(command[command.index('--settings') + 1])[-1])
        service = AuditAccess(launcher.with_name('manifest.json'))
        source = service.invoke('read', repository='os', path='unmentioned.py', start_line=1, end_line=1)
        def evidence(value):
            if value['findings']:
                value['findings'][0]['location']['source_ref'] = source['id']
            value['coverage'][0]['evidence_refs'] = [source['id']]
        self.mutation = evidence
        data = json.loads(prompt.split(b'IMPLEMENTER RECOMMENDATION AND CURRENT OBJECTIVE:\n')[1])
        assert all('text' not in source and 'segments' not in source for source in data['sources'])
        return super().run(command, prompt=b'fixture\n' + json_bytes(data), **kwargs)


@pytest.fixture
def independent(selection, request_data, monkeypatch):
    request_data.pop('exploration')
    root = Path(selection['repositories'][0]['root'])
    (root / 'unmentioned.py').write_text('answer = 1\n', encoding='utf-8')
    selection['repositories'][0]['excluded'] = [{'path': 'unmentioned.py', 'reason': 'Implementer considers unrelated'}]
    return selection


def test_new_case_explores_excluded_file_and_hands_off_final_evidence(independent, request_data, cases):
    packet = prepare(independent)
    cases.begin_packet(packet, request_data)
    state = cases.load(request_data['case_id'])
    assert state['anchor']['exploration']['transport'] == 'native-cli'
    assert state['anchor']['limits']['attempts'] is None
    assert state['anchor']['limits']['context_tokens'] is None
    status = execute_packet(cases, request_data['case_id'], 1, driver=IndependentProvider(), effort='xhigh')
    assert status['status'] == 'REJECTED'
    result = cases.load(request_data['case_id'])['attempts'][-1]['result']
    assert result['review_status'] == 'VALID'
    assert result['evidence_sources'][0]['path'] == 'unmentioned.py'
    assert result['metrics']['exploration']['verified_sources'] == 1
    assert result['metrics']['exploration']['generation_calls'] == 0
    assert result['metrics']['exploration']['access'] == 'native_cli'
    run = Path(cases.load(request_data['case_id'])['attempts'][-1]['execution']['run'])
    assert not (run / 'exploration' / 'snapshot').exists()
    assert 'chat.history' not in json.dumps(result) and 'provider-events' not in json.dumps(result)
    validate_review(packet, result['review'], result['evidence_sources'])
    with pytest.raises(ValueError, match='finding_source_missing'):
        validate_review(packet, result['review'])


@pytest.mark.parametrize('gateway', [False, True, 'glm'])
def test_correction_of_independently_discovered_file_resumes_session(independent, request_data, cases, gateway):
    provider = IndependentProvider()
    if gateway == 'glm':
        request_data['auditor'].update(model_id='glm-5.3', runtime_key='aider-openrouter',
                                      runtime_model='openrouter/z-ai/glm-5.3')
        provider.gateway = {'provider': 'OpenRouter', 'model': 'openrouter/z-ai/glm-5.3',
            'wire_model': 'z-ai/glm-5.3', 'base_url': 'https://openrouter.ai/api',
            'deployment_id': 'chair-glm-openrouter', 'quota_domain': 'openrouter-credits'}
    packet = prepare(independent)
    cases.begin_packet(packet, request_data)
    execute_packet(cases, request_data['case_id'], 1, driver=provider, effort='xhigh')
    session = cases.load(request_data['case_id'])['session_id']
    # This case-scoped lock survives a killed MCP server in the old path.
    previous_run = Path(cases.load(request_data['case_id'])['attempts'][0]['execution']['run'])
    manifest = json.loads((previous_run / 'exploration/manifest.json').read_text())
    Path(manifest['lock_path']).write_text('orphaned after process termination')
    root = Path(independent['repositories'][0]['root'])
    (root / 'unmentioned.py').write_text('answer = 2\n', encoding='utf-8')
    next_selection = deepcopy(independent)
    next_selection['packet_id'] = 'fix-r2'
    next_selection['repositories'][0]['excluded'] = []
    next_selection['repositories'][0]['changes'].append({'id': 'C2', 'path': 'unmentioned.py', 'why': 'Fix F1'})
    next_selection['obligations'][0]['change_ids'].append('C2')
    reply = {'schema': 'tare.tools/audit-correction/1', 'packet_id': 'fix-r1', 'dispositions': [{
        'finding_id': 'F1', 'disposition': 'FIXED', 'change_refs': ['C2'],
        'test_receipt_refs': ['T1'], 'reason': 'Corrected independently discovered dependency'}]}
    cases.begin_packet(prepare(next_selection), {'case_id': request_data['case_id'], 'correction': reply,
        'continuation': {'reason': 'F1 corrected'}}, resume=True)
    provider.verdict = 'ACCEPTED_BOUNDED'
    if gateway is True:
        provider.gateway = {'provider': 'OpenRouter', 'model': 'claude-fable-5',
            'wire_model': 'anthropic/claude-fable-5', 'base_url': 'https://openrouter.ai/api',
            'deployment_id': 'chair-anthropic-openrouter', 'quota_domain': 'openrouter-credits'}
    outcome = execute_packet(cases, request_data['case_id'], 2, driver=provider, effort='xhigh')
    assert outcome['status'] == 'CLOSED'
    assert provider.calls[0][-1] == provider.calls[1][-1] == session
    assert '--resume' in provider.calls[1]
    if gateway:
        result = cases.load(request_data['case_id'])['attempts'][-1]['result']
        assert result['primary_model_requested'] == provider.gateway['model']
        assert result['primary_model_reported'] == provider.gateway['wire_model']
        assert result['metrics']['transport'] == provider.gateway


def test_direct_independent_read_can_close_without_auxiliary_model(independent, request_data, cases):
    cases.begin_packet(prepare(independent), request_data)
    provider = IndependentProvider('ACCEPTED_BOUNDED')
    provider.skip_exploration = True
    outcome = execute_packet(cases, request_data['case_id'], 1, driver=provider, effort='xhigh')
    assert outcome['status'] == 'CLOSED'
    result = cases.load(request_data['case_id'])['attempts'][-1]['result']
    assert result['review_status'] == 'VALID'
    assert result['metrics']['exploration']['generation_calls'] == 0


@pytest.mark.parametrize('failure,expected', [('quota', 'provider_http_429'),
                                            ('timeout', 'audit_deadline_exceeded')])
def test_provider_failure_is_not_masked_by_absent_exploration(
        independent, request_data, cases, failure, expected):
    class Unavailable(IndependentProvider):
        def run(self, command, *, on_output, **kwargs):
            if failure == 'timeout':
                raise TimeoutError('no provider turn')
            on_output(json_bytes({'type': 'result', 'session_id': command[-1],
                'subtype': 'success', 'is_error': True, 'api_error_status': 429,
                'modelUsage': {}, 'total_cost_usd': 0}) + b'\n')
            return 1, b''
    cases.begin_packet(prepare(independent), request_data)
    outcome = execute_packet(cases, request_data['case_id'], 1, driver=Unavailable(), effort='xhigh')
    result = cases.load(request_data['case_id'])['attempts'][-1]['result']
    assert outcome['status'] == 'EXECUTION_FAILED'
    assert result['reason'] == expected
    assert result['review_status'] == 'MISSING'
    assert result['metrics']['exploration']['completed'] == 0


def test_changed_live_evidence_is_reported_without_acceptance(independent, request_data, cases):
    class Changed(IndependentProvider):
        def run(self, *args, **kwargs):
            result = super().run(*args, **kwargs)
            root = Path(independent['repositories'][0]['root'])
            (root / 'unmentioned.py').write_text('answer = 99\n', encoding='utf-8')
            return result
    cases.begin_packet(prepare(independent), request_data)
    outcome = execute_packet(cases, request_data['case_id'], 1,
                             driver=Changed('ACCEPTED_BOUNDED'), effort='xhigh')
    result = cases.load(request_data['case_id'])['attempts'][-1]['result']
    assert outcome['status'] == 'EXECUTION_FAILED'
    assert result['review_status'] == 'INVALID'
    assert result['reason'].startswith('exploration_invalid:exploration_evidence_changed')


def test_timeout_after_primary_tool_event_preserves_session(independent, request_data, cases):
    class Interrupted(IndependentProvider):
        def run(self, command, *, on_output, **kwargs):
            session = command[-1]
            on_output(json_bytes({'type': 'assistant', 'session_id': session,
                'message': {'id': 'message-before-timeout', 'model': command[command.index('--model') + 1],
                            'content': [{'type': 'tool_use', 'name': 'Bash',
                                         'input': {}}]}}) + b'\n')
            raise TimeoutError('fixture deadline')
    cases.begin_packet(prepare(independent), request_data)
    execute_packet(cases, request_data['case_id'], 1, driver=Interrupted(), effort='xhigh')
    state = cases.load(request_data['case_id'])
    assert state['status'] == 'EXECUTION_FAILED'
    assert state['session_id'] == state['attempts'][0]['execution']['session_id']
    assert state['attempts'][0]['result']['review_status'] == 'MISSING'


def test_user_can_end_disagreement_without_rewriting_auditor_verdict(independent, request_data, cases):
    cases.begin_packet(prepare(independent), request_data)
    execute_packet(cases, request_data['case_id'], 1, driver=IndependentProvider(), effort='xhigh')
    result = cases.load(request_data['case_id'])['attempts'][-1]['result']
    decision = {'decision': 'ACCEPT_RISK', 'statement': 'Deliver the original objective with this known risk',
                'operator_label': 'User', 'expected_result_sha256': checksum(json_bytes(result))}
    with pytest.raises(ValueError, match='result_changed'):
        cases.user_decision(request_data['case_id'], 1, {**decision, 'expected_result_sha256': '0' * 64})
    outcome = cases.user_decision(request_data['case_id'], 1, decision)
    assert outcome['status'] == 'CLOSED_BY_USER'
    assert outcome['user_decision']['authority'] == 'USER_EXPLICIT'
    assert cases.load(request_data['case_id'])['attempts'][-1]['result'] == result


@pytest.mark.parametrize('ceiling,expected', [(None, 'REJECTED'), (100, 'INCONCLUSIVE_WITH_REVISIT_TRIGGER')])
def test_context_ceiling_is_optional_but_observation_is_preserved(independent, request_data, cases, ceiling, expected):
    class HighContext(IndependentProvider):
        def run(self, *args, on_output, **kwargs):
            def measured(raw):
                event = json.loads(raw)
                if event.get('type') == 'assistant':
                    event['message']['usage'] = {'input_tokens': 212803,
                        'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 0}
                on_output(json_bytes(event) + b'\n')
            return super().run(*args, on_output=measured, **kwargs)
    request_data['limits'] = {'attempts': None, 'context_tokens': ceiling}
    cases.begin_packet(prepare(independent), request_data)
    outcome = execute_packet(cases, request_data['case_id'], 1, driver=HighContext(), effort='xhigh')
    assert outcome['status'] == expected
    assert cases.load(request_data['case_id'])['attempts'][-1]['result']['metrics']['peak_context_tokens'] == 212803


@pytest.mark.parametrize('attempts', [None, 1, 4, 5, 100])
def test_attempt_limit_is_optional_without_arbitrary_four_attempt_ceiling(attempts):
    limits = {'attempts': attempts, 'context_tokens': 120000}
    validate_limits(limits)
    assert attempt_limit_reached(limits, 100) == (attempts is not None)


@pytest.mark.parametrize('attempts', [0, -1, True, 1.2, '4'])
def test_invalid_explicit_limit_is_rejected(attempts):
    with pytest.raises(ValueError, match='invalid_bounded_limits'):
        validate_limits({'attempts': attempts, 'context_tokens': 120000})
