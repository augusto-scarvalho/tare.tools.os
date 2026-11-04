"""Offline contract tests; no scheduling owners or runtime execution required."""
import copy
import json
from pathlib import Path

import jsonschema
import pytest

from relay.scheduling_diagnostics import (
    Unavailable, UnschedulableError, diagnostic, render_report,
)


POLICY = 'MACHINE_POLICY LOCAL_GPU_DISABLED NOT_USED_ON_THIS_MACHINE OLDER_MODEL AUDITOR_NOT_INDEPENDENT'.split()
RESOURCE = 'QUOTA_EXHAUSTED GPU_BUSY GPU_RESERVED GPU_IMAGE_WAITING GPU_LEASE_HELD'.split()
DEPENDENCY = 'CLI_UNAVAILABLE CREDENTIAL_UNAVAILABLE GATEWAY_UNREACHABLE GATEWAY_ERROR PROFILE_NOT_INSTALLED CLI_EXECUTABLE_UNAVAILABLE'.split()
CONFIGURATION = 'PROFILE_UNAVAILABLE GPU_MODEL_NOT_ADVERTISED ROLE_NOT_CONFIGURED RUNNER_NOT_SUPPORTED_BY_NATIVE_WORK RUNNER_NOT_SUPPORTED_BY_NATIVE_FLOW COST_OR_PREFERENCE_UNKNOWN PROFILE_OR_IDENTITY_INVALID DUPLICATE_REALIZATION'.split()
UNKNOWN = 'RESOURCE_OBSERVATION_INVALID GPU_STATUS_UNKNOWN GPU_STATUS_EXPIRED UNKNOWN'.split()
CODES = POLICY + RESOURCE + DEPENDENCY + CONFIGURATION + UNKNOWN
TRANSIENT = set(RESOURCE + ['GATEWAY_UNREACHABLE'])


@pytest.mark.parametrize('field', ['code', 'message', 'action'])
def test_schema_rejects_an_empty_code_or_recovery_instruction(field, validator):
    value = diagnostic('GPU_BUSY')
    value[field] = ''
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(value)


@pytest.fixture(scope='module')
def validator():
    path = Path(__file__).resolve().parents[1] / 'contracts/v1/unschedulable-reason.schema.json'
    schema = json.loads(path.read_text(encoding='utf-8'))
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


@pytest.mark.parametrize('category,codes', [
    ('policy', POLICY), ('resource', RESOURCE), ('dependency', DEPENDENCY),
    ('configuration', CONFIGURATION), ('unknown', UNKNOWN),
])
def test_catalog_classification(category, codes):
    for code in codes:
        result = diagnostic(code)
        assert result['code'] == code
        assert result['category'] == category
        assert result['retryable'] is (code in TRANSIENT)
        assert result['message'] and result['action']
        assert 'retry_after_seconds' not in result
        if category == 'policy':
            assert not result['retryable']
            assert 'override' not in result['action'].lower()


@pytest.mark.parametrize('code', CODES)
def test_catalog_json_schema_and_freshness(code, validator):
    first = diagnostic(code, retry_after_seconds=86400)
    validator.validate(first)
    assert json.loads(json.dumps(first, allow_nan=False)) == first
    first['message'] = 'caller mutation'
    assert diagnostic(code)['message'] != first['message']


@pytest.mark.parametrize('delay', [0, 1, 86400])
def test_delay_boundaries(delay, validator):
    result = diagnostic('GPU_BUSY', retry_after_seconds=delay)
    assert result['retry_after_seconds'] == delay
    validator.validate(result)


@pytest.mark.parametrize('delay', [True, False, -1, 86401, 1.0, float('nan'), float('inf'), '3', [], {}])
def test_invalid_delay_is_not_coerced(delay):
    with pytest.raises(ValueError, match='retry_after_seconds must be an integer'):
        diagnostic('GPU_BUSY', retry_after_seconds=delay)


@pytest.mark.parametrize('code', [None, [], {}, 12, 'gpu_busy', 'GPU_BUSY: token=secret', 'credential missing', 'new_upstream_code'])
def test_unknown_codes_never_echo_arbitrary_input(code):
    assert diagnostic(code) == diagnostic('UNKNOWN')


def test_legacy_string_compatibility_and_explicit_identity():
    legacy = 'credential missing: secret-token; GPU_BUSY'
    reason = Unavailable(legacy, 'MACHINE_POLICY')
    assert isinstance(reason, str)
    assert reason == legacy
    assert reason.startswith('credential missing')
    assert str(reason) == legacy
    assert json.loads(json.dumps({'reason': reason})) == {'reason': legacy}
    assert reason.diagnostic == diagnostic('MACHINE_POLICY')
    other = Unavailable(legacy, 'GPU_BUSY', retry_after_seconds=7)
    assert other == reason  # string identity does not imply diagnostic identity
    assert other.diagnostic['retry_after_seconds'] == 7
    assert other.diagnostic != reason.diagnostic


def test_report_sources_redaction_and_native_candidates(validator):
    secret = 'https://user:password@example.test/?token=secret GPU_BUSY credential missing'
    rows = [
        {'route': 'legacy', 'reason': Unavailable(secret, 'MACHINE_POLICY')},
        {'deployment': 'native', 'eligible': False, 'reason': 'GPU_BUSY'},
        {'route': 'explicit', 'reason': secret, 'diagnostic': {
            'code': 'GATEWAY_ERROR', 'message': secret, 'action': secret,
            'category': 'resource', 'retryable': False, 'retry_after_seconds': 10,
            'secret': secret,
        }},
        {'route': 'unknown', 'reason': secret},
        {'route': 'future', 'diagnostic': {'code': secret, 'retry_after_seconds': True}},
        {'deployment': 'eligible', 'eligible': True, 'reason': 'CONFIGURED_NATIVE_ROLE'},
    ]
    error = UnschedulableError('tare_auto_no_route_available', rows)
    report = error.report
    assert isinstance(error, ValueError)
    assert report['schema'] == 'tare.tools/unschedulable/1'
    assert [r['code'] for r in report['reasons']] == [
        'MACHINE_POLICY', 'GPU_BUSY', 'GATEWAY_ERROR', 'UNKNOWN', 'UNKNOWN',
    ]
    assert report['reasons'][1]['route'] == 'native'
    assert report['reasons'][2]['retry_after_seconds'] == 10
    assert 'retry_after_seconds' not in report['reasons'][4]
    assert secret not in json.dumps(report)
    assert secret not in str(error)
    for reason in report['reasons']:
        validator.validate({key: value for key, value in reason.items() if key != 'route'})
    assert str(error) == render_report(report)


def test_unavailable_diagnostic_takes_precedence():
    row = {'route': 'a', 'reason': Unavailable('arbitrary', 'GPU_BUSY'),
           'diagnostic': diagnostic('MACHINE_POLICY')}
    assert UnschedulableError('blocked', [row]).report['reasons'][0]['code'] == 'GPU_BUSY'


def test_reports_are_bounded_without_silent_text_slicing():
    rows = [{'route': f'route-{index}', 'reason': 'CLI_UNAVAILABLE'} for index in range(70)]
    error = UnschedulableError('tare_auto_no_route_available', rows)
    report = error.report
    assert len(report['reasons']) == 64
    assert report['omitted'] == 6
    text = str(error)
    assert len(text) > 300
    assert 'route-63 [CLI_UNAVAILABLE]' in text
    assert 'route-64 [' not in text
    assert text.endswith('6 additional route(s) omitted.')
    assert text.count('[CLI_UNAVAILABLE]') == 64
    assert text.startswith('tare_auto_no_route_available: No route can start.')
    assert json.loads(json.dumps(report)) == report


@pytest.mark.parametrize('count', [0, 1, 64, 65])
def test_exact_boundaries_and_iterables(count):
    error = UnschedulableError('blocked', ({'deployment': 'a', 'reason': 'GPU_BUSY'} for _ in range(count)))
    assert len(error.report['reasons']) == min(count, 64)
    assert error.report['omitted'] == max(0, count - 64)


def test_route_names_are_visible_bounded_and_not_stringified():
    rows = [{'route': '\x1b\n\t' + 'x' * 100, 'reason': 'GPU_BUSY'},
            {'route': {'secret': 'never stringify'}, 'reason': 'GPU_BUSY'}]
    report = UnschedulableError('blocked', rows).report
    name = report['reasons'][0]['route']
    assert len(name) == 64 and name.isprintable()
    assert name.endswith('…')
    assert report['reasons'][1]['route'] == '(unnamed route)'
    assert 'never stringify' not in json.dumps(report)


def test_input_and_output_isolation():
    rows = [{'deployment': 'a', 'reason': 'GPU_BUSY',
             'diagnostic': {**diagnostic('GPU_RESERVED'), 'unrelated': ['private']}}]
    before = copy.deepcopy(rows)
    error = UnschedulableError('blocked', rows)
    assert rows == before
    snapshot = error.report
    snapshot['reasons'][0]['message'] = 'mutated'
    snapshot['reasons'].clear()
    rows[0]['diagnostic']['code'] = 'MACHINE_POLICY'
    assert error.report['reasons'][0]['code'] == 'GPU_RESERVED'
    assert 'mutated' not in str(error)


def test_transport_error_does_not_claim_specific_resource_or_execution_outcome():
    text = str(UnschedulableError('blocked', [{'route': 'remote', 'reason': 'GATEWAY_ERROR'}]))
    assert '[GATEWAY_ERROR]' in text
    assert 'GPU' not in text and 'credential' not in text.lower()
    assert 'no model started' not in text.lower()
    assert 'command succeeded' not in text.lower() and 'command failed' not in text.lower()


def test_schema_required_fields_unknown_properties_and_invalid_values(validator):
    valid = diagnostic('UNKNOWN')
    for key in valid:
        incomplete = dict(valid)
        del incomplete[key]
        assert not validator.is_valid(incomplete)
    assert not validator.is_valid({**valid, 'extra': 'not allowed'})
    for changes in [
        {'category': 'transport'}, {'retryable': 1}, {'code': 42},
        {'message': None}, {'action': []}, {'retry_after_seconds': True},
        {'retry_after_seconds': -1}, {'retry_after_seconds': 86401},
        {'retry_after_seconds': 0.5},
    ]:
        assert not validator.is_valid({**valid, **changes})
