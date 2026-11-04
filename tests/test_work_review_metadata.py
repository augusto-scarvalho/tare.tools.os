"""Offline FLOW-06 declarations and FLOW-18 controller-attempt observations."""
import json
from types import SimpleNamespace

import pytest

from relay import work_run, work_resume


@pytest.fixture(autouse=True)
def runtime_version(monkeypatch):
    def version(name):
        assert name == 'tare-tools-agent-runtime'
        return '1.2.3'
    monkeypatch.setattr(work_run.metadata, 'version', version)


def deployment():
    return {'id': 'frozen', 'model': 'catalog-alias', 'token': 'SECRET',
            'native_profile': {'model': 'requested-wire', 'protocol': 'chat',
                               'endpoint': 'https://private.invalid',
                               'api_key': 'SECRET', 'temperature': 0.8}}


@pytest.mark.parametrize('value', [None, '', ' ', 7, {}, [], 'bad\nclass', 'x' * 1025])
def test_invalid_or_absent_declarations_stay_unknown(value):
    assert work_run._review_metadata({'metrics': {'task_class': value}, 'title': 'coding'}, None) == {}
    assert work_run._review_metadata({'task_class': 'unsupported-top-level'}, {}) == {}
    native = deployment()
    native['native_profile']['model'] = value
    assert work_run._review_metadata(None, native) == {}


def test_only_small_allowlisted_runtime_declarations_are_exported():
    result = work_run._review_metadata({'metrics': {'task_class': 'bounded-edit'}}, deployment())
    assert result['task_class'] == 'bounded-edit'
    assert json.loads(result['runtime_identity']) == {
        'deployment_id': 'frozen', 'requested_model': 'requested-wire', 'protocol': 'chat'}
    assert set(result) == {'task_class', 'runtime_identity', 'runtime_observation'}
    assert result['runtime_observation'] == {
        'schema': 'tare.tools/work-runtime-observation/1',
        'runtime_version': '1.2.3', 'deployment_id': 'frozen',
        'requested_model': 'requested-wire', 'protocol': 'chat', 'effort': None}
    assert all(isinstance(v, str) and v.isprintable() and len(v) <= 1024
               for k, v in result.items() if k != 'runtime_observation')
    assert all(v is None or (isinstance(v, str) and v.isprintable() and len(v) <= 1024)
               for v in result['runtime_observation'].values())
    assert not any(word in str(result) for word in
                   ('SECRET', 'private.invalid', 'temperature', 'catalog-alias', 'served'))


@pytest.mark.parametrize('protocol,parameters', [
    ('openai-responses', {'reasoning': {'effort': 'high'}, 'api_key': 'SECRET'}),
    ('vendor-cli', {'effort': 'high', 'api_key': 'SECRET'}),
])
@pytest.mark.parametrize('encoded', [False, True])
def test_explicit_effort_only(protocol, parameters, encoded):
    native = deployment()
    native['native_profile'].update(protocol=protocol, parameters_json=(
        json.dumps(parameters) if encoded else parameters))
    observation = work_run._review_metadata(None, native)['runtime_observation']
    assert observation == {
        'schema': 'tare.tools/work-runtime-observation/1',
        'runtime_version': '1.2.3', 'requested_model': 'requested-wire',
        'protocol': protocol, 'deployment_id': 'frozen', 'effort': 'high'}
    assert 'SECRET' not in str(observation)


@pytest.mark.parametrize('protocol', ['openai-responses', 'vendor-cli'])
@pytest.mark.parametrize('parameters', [
    None, '', '{bad', 'null', '[]', '7', [], 7, {},
    {'reasoning': None}, {'reasoning': []}, {'reasoning': 'high'},
    {'reasoning': {'effort': None}, 'effort': None},
    *[{'reasoning': {'effort': v}, 'effort': v}
      for v in ['', ' ', 7, True, {}, [], 'bad\neffort', 'x' * 1025]],
])
def test_missing_or_malformed_optional_effort_is_unknown(protocol, parameters):
    native = deployment()
    native['native_profile'].update(protocol=protocol, parameters_json=parameters)
    result = work_run._review_metadata(None, native)
    assert result['runtime_observation']['effort'] is None
    assert json.loads(result['runtime_identity'])['deployment_id'] == 'frozen'


@pytest.mark.parametrize('version', [None, '', 7, 'bad\nversion', 'x' * 1025])
def test_invalid_runtime_version_is_unknown(monkeypatch, version):
    monkeypatch.setattr(work_run.metadata, 'version', lambda _: version)
    assert work_run._review_metadata(None, deployment())['runtime_observation']['runtime_version'] is None


def test_missing_runtime_distribution_is_unknown(monkeypatch):
    def missing(_):
        raise work_run.metadata.PackageNotFoundError('tare-tools-agent-runtime')
    monkeypatch.setattr(work_run.metadata, 'version', missing)
    result = work_run._review_metadata(None, deployment())
    assert result['runtime_observation']['runtime_version'] is None
    assert result['runtime_observation']['effort'] is None


def test_recorded_verdicts_are_not_quality_or_independence():
    receipt = {'status': 'SETTLED', 'stages': [{'stage': 'plan_audit', 'verdict': 'ACCEPT'}],
               'audit': {'verdict': 'REVISE', 'independent': True},
               'admission': {'verdict': 'ADMITTED'}}
    assert work_run._review_outcome(receipt) == {'recorded_audit_outcome':
        'plan_verdict=ACCEPT; code_verdict=REVISE; admission_verdict=ADMITTED'}
    assert work_run._review_outcome({'status': 'SETTLED', 'audit_routing': {'status': 'READY'}}) == {}
    assert work_run._review_outcome({'audit': {'result': {'status': 'FAILED'}}}) == {
        'recorded_audit_outcome': 'code_status=FAILED'}


def test_resume_observes_frozen_origin_not_current_configuration(tmp_path, monkeypatch):
    from relay import relay_mesh, native_catalog, train_execution, work_recovery

    graph = tmp_path / 'graph.json'
    origin = {'schema': 'tare.tools/work-run-origin/1', 'work_id': 'W',
              'run_id': 'old', 'descriptor_sha256': 'digest',
              'repository_root': str(tmp_path), 'node': {'metrics': {'task_class': 'declared'}},
              'native_deployment': deployment()}
    (tmp_path / 'WORK_RUN.json').write_text(json.dumps(origin), encoding='utf-8')
    (tmp_path / 'STATE.json').write_text(json.dumps({
        'train_id': 'T', 'task_ids': ['W'], 'status': 'PLAN_AUDIT_PENDING'}), encoding='utf-8')
    monkeypatch.setattr(work_run, '_resolve_graph_scope', lambda _: (False, str(graph), str(graph)))
    monkeypatch.setattr(work_run, 'train_dir_for', lambda *a, **k: tmp_path)
    monkeypatch.setattr(work_run, 'train_id_for', lambda *a, **k: 'T')
    monkeypatch.setattr(relay_mesh, 'RELAY_ROOT', tmp_path)
    monkeypatch.setattr(work_recovery, 'origin_protection_options', lambda _: {})
    monkeypatch.setattr(train_execution, 'read_execution_descriptor', lambda _: SimpleNamespace(
        work_graph=str(graph), work_id='W', train_id='T', sha256='digest'))
    monkeypatch.setattr(native_catalog, 'select', lambda *a, **k: pytest.fail('current catalog consulted'))
    receipt = {}
    work_resume.resume_settlement('W', receipt, lambda *a, **k: None,
                                  SimpleNamespace(is_cancelled=lambda: True), graph_path=graph)
    assert receipt['task_class'] == 'declared'
    assert json.loads(receipt['runtime_identity'])['deployment_id'] == 'frozen'
    assert receipt['runtime_observation'] == {
        'schema': 'tare.tools/work-runtime-observation/1',
        'runtime_version': '1.2.3', 'deployment_id': 'frozen',
        'requested_model': 'requested-wire', 'protocol': 'chat', 'effort': None}
    assert 'recorded_audit_outcome' not in receipt
