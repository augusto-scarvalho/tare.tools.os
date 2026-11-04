"""Phase 0 of the ways of working: per-class experiments, the flow policy path, the availability report and the
shadow of session turns. Nothing here changes how a session works."""
import json
import os
from pathlib import Path

import pytest
from tare_tools_agent_runtime import CancellationToken

from relay import console_entry, delegation, flow_assessment, native_agent, native_diagnostics, turn_engine, work_forms
from relay.console_backend import Selection
from test_delegation import enabled, helper, ask  # noqa: F401 (fixture)
from test_native_agent import Client, deployment, setup  # noqa: F401 (fixture)


@pytest.fixture
def storage(tmp_path, monkeypatch):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'storage'))
    monkeypatch.delenv('TARE_DELEGATION_POLICY')
    return tmp_path/'storage'


def test_an_experiment_takes_its_class_value_over_the_global_one(storage):
    # Jev first is not measured with a local session: off for local, on elsewhere, with no code exception.
    assert delegation.experiments()['jev_first'] and delegation.experiments('quota')['jev_first']
    assert not delegation.experiments('local')['jev_first']
    delegation.save_experiments('local', jev_first=True)
    delegation.save_experiments(adaptive_k=True)  # a global save keeps the class values
    assert delegation.experiments('local')['jev_first'] and delegation.experiments('local')['adaptive_k']
    assert delegation.class_values() == {'local': {'jev_first': True}}
    assert delegation.experiments() == dict(delegation.EXPERIMENTS, adaptive_k=True)  # what the panel's switches show
    with pytest.raises(ValueError):
        delegation.save_experiments('gpu', jev_first=True)
    assert delegation.main(['--experiment', 'usd.jev_first=off']) == 0
    assert not delegation.experiments('usd')['jev_first'] and delegation.experiments('quota')['jev_first']
    assert 'Experiments for usd sessions: jev_first=off' in delegation.digest_text(delegation.digest())


def test_the_flow_policy_is_only_the_installations(storage, tmp_path, monkeypatch):
    monkeypatch.delenv('TARE_FLOW_POLICY')
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path/'appdata'))
    assert flow_assessment.policy_path() is None and flow_assessment.policy() is None
    development = tmp_path/'appdata'/'tare.tools'/'tui-development'/'assessment-policies'/'flows.json'
    development.parent.mkdir(parents=True)
    development.write_text('{}')
    assert flow_assessment.policy_path() is None  # one development machine's folder is no product location
    installed = storage/'assessment-policies'/'flows.json'
    installed.parent.mkdir(parents=True)
    installed.write_text('{}')
    assert flow_assessment.policy_path() == installed
    monkeypatch.setenv('TARE_FLOW_POLICY', 'off')
    assert flow_assessment.policy_path() is None


def test_findings_without_a_flow_policy_say_so(storage):
    baseline = {'action': 'REVIEW_FINDINGS', 'mode': 'research', 'owner': 'x', 'owner_field': 'research'}
    result = flow_assessment.after_findings({}, baseline, CancellationToken())
    assert result == {**baseline, 'flow_assessment': {'status': 'ABSTAINED', 'reason': 'FLOW_POLICY_ABSENT'}}


def test_classification_without_a_policy_leaves_a_route_this_installation_lacks(tmp_path, monkeypatch):
    project = tmp_path/'project'; project.mkdir()
    monkeypatch.setattr(turn_engine, 'available', lambda name, probe=True: (None, 'CLI not installed'))
    monkeypatch.setattr(turn_engine, 'choose', lambda tiers, start=0, probe=True: (1, 'bedrock-glm', {}, []))
    used = []
    def generate(deployment, *a, **kw):
        used.append(deployment['id'])
        return True, json.dumps({'flow': 'session', 'reason': 'Small edit.', 'question': ''}), \
            {'generation_calls': 1, 'usage': None}
    result = console_entry.propose(Selection(str(project), deployment='codex-cli', prompt='Fix the typo.'),
                                   CancellationToken(), directory=tmp_path/'entry', generate=generate)
    assert result['status'] == 'PROPOSED' and used == ['bedrock-glm']


def test_availability_names_what_each_way_of_working_lacks(storage, tmp_path, monkeypatch):
    project = tmp_path/'project'
    (project/'tests').mkdir(parents=True)
    (project/'tests'/'test_a.py').write_text('def test_a(): pass\n')
    (project/'package.json').write_text(json.dumps({'scripts': {'test': 'vitest run'}}))
    (project/'Cargo.toml').write_text('[package]\n')
    report = work_forms.availability(project, mode='read')
    assert report['forms']['direct']['available'] and not report['forms']['explore']['available']
    implement = report['forms']['implement']
    assert not implement['available'] and 'a git repository' in implement['missing']
    assert 'the implement_child experiment on' in implement['missing']
    assert 'edit permission (the project is read-only)' in implement['missing']
    assert [e['name'] for e in report['ecosystems']] == ['pytest', 'npm', 'cargo']
    assert report['ecosystems'][1]['narrow'] == 'npm test -- <test file>'
    assert report['flow_policy'].startswith('absent') and '/plan' in report['plans']
    text = work_forms.availability_text(report)
    assert 'implement: unavailable; needs ' in text and 'tests: pytest via ' in text


def test_a_clean_installation_reports_without_a_traceback(tmp_path, monkeypatch):
    # No gateway, no keys, no development policy: the doctor still reports every way of working.
    monkeypatch.setenv('TARE_MANAGED_HOME', str(tmp_path/'home'))
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'storage'))
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path/'appdata'))
    monkeypatch.delenv('TARE_FLOW_POLICY')
    monkeypatch.delenv('TARE_DELEGATION_POLICY')
    project = tmp_path/'empty'; project.mkdir()
    report = native_diagnostics.doctor(root=project)['work_forms']
    assert set(report['forms']) == {'direct', 'explore', 'plan', 'delegate_plan', 'implement'} and report['ecosystems'] == []
    assert work_forms.availability_text(report).endswith(
        'absent: no flow is suggested; choose /session, /explore, /plan or /research\n'
        'this machine: not set up yet; every route tare finds may be used')


def test_each_session_turn_leaves_one_local_row(setup, enabled, monkeypatch):
    monkeypatch.delenv('TARE_WORK_FORM_SHADOW')
    monkeypatch.setenv('TARE_FLOW', 'agent')
    monkeypatch.setenv('TYPESAFE_API_KEY', 'fixture-key')
    asked = []
    def judges(request, local=True, timeout=10):  # hosted Jev only: the local judge would take the GPU
        if request['question'] != work_forms.FORM_QUESTION:
            return {}
        asked.append((request['state'], local))
        return {'jev': {'choice': 'implement', 'probabilities': {'implement': .7}, 'ms': 5}}
    monkeypatch.setattr(turn_engine, 'judges', judges)
    config, custody, root = setup
    helper(monkeypatch, root)
    parent = Client(steps=[ask(), ('tare_deliver', {'summary': 'Investigation complete.'})])
    native_agent.run_session(config, deployment(), custody, 'Find it.', client=parent, local_counter=parent,
                             limits={'max_model_calls': 4})
    log = Path(os.environ['TARE_STORAGE_ROOT'])/'serve'/'work-form.jsonl'
    row, = [json.loads(line) for line in log.read_text(encoding='utf-8').splitlines()]  # the explorer adds none
    assert (row['form'], row['flow'], row['class'], row['role']) == ('explore', 'agent', 'usd', 'implementer')
    assert row['verifiers'] == ['unit'] and row['status'] == 'PROPOSED'
    assert row['jev']['choice'] == 'implement' and [(json.loads(state),local) for state,local in asked] == [({'user_request':'Find it.'},False)]
    summary = delegation.digest()['work_forms']
    assert summary['forms'] == {'explore': 1} and summary['jev']['implement'] == 1 and summary['jev_agreed'] == 0
    assert 'Jev would choose implement 1' in delegation.digest_text(delegation.digest())


def test_verifier_failures_in_a_row_are_counted(storage, monkeypatch):
    monkeypatch.delenv('TARE_WORK_FORM_SHADOW')
    items = [{'operation': op, 'state': state} for op, state in
             [('verify', 'failed'), ('edit', 'succeeded'), ('verify', 'succeeded'), ('verify', 'failed'), ('verify', 'failed')]]
    work_forms.record_turn({'role': 'implementer', 'verifiers': {'tests': {}}}, None,
                           {'status': 'BLOCKED', 'tool_activity': {'items': items, 'omitted': 0}})
    row = json.loads((storage/'serve'/'work-form.jsonl').read_text(encoding='utf-8'))
    assert (row['form'], row['verify_runs'], row['verify_failed'], row['verify_failed_in_a_row'], row['edits']) == \
        ('direct', 4, 3, 2, 1)
    monkeypatch.setenv('TARE_WORK_FORM_SHADOW', 'off')
    work_forms.record_turn({'role': 'implementer'}, None, {'status': 'BLOCKED'})
    assert len((storage/'serve'/'work-form.jsonl').read_text(encoding='utf-8').splitlines()) == 1
