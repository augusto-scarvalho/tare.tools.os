import hashlib
import json

import pytest
from tare_tools_agent_runtime import CancellationToken
from relay import native_agent, research_flow, research_synthesis as flow
from test_native_agent import setup, Client, deployment
from test_research_flow import study


@pytest.fixture
def synthesis(study, monkeypatch):
    directory, root, calls, fetched, collect = study
    research_flow.execute(directory, fetch=collect)
    second = directory.parent/'second'
    research_flow.create(second, root, 'Which alternative?', ['https://docs.example.com/guide'], deployment())
    research_flow.execute(second, fetch=collect)
    selected = [{'kind': 'research', 'directory': str(p)} for p in (directory, second)]
    target = directory.parent/'synthesis'
    view = flow.preview(root, 'What agrees and what remains disputed?', selected)
    original = native_agent.run_session
    # Bypass the participant fixture wrapper, but use the actual native runner and observations.
    from test_research_flow import NATIVE_RUN
    synthesis_calls = []
    def run(config, selected_model, session, instruction, **kwargs):
        path = target/'findings'/'positions.json'
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        client = Client(steps=[('read', {'repository': 'findings', 'path': 'positions.json'}),
            ('tare_deliver', {'summary': 'Both describe TTL; the alternative remains disputed. No independent verification.',
             'remaining': ['Alternative needs evidence'], 'sources': [
                 {'repository': 'findings', 'path': 'positions.json', 'sha256': digest}]})])
        synthesis_calls.append((client, config, selected_model, str(session), instruction, kwargs['resume']))
        return NATIVE_RUN(config, selected_model, session, instruction, client=client, local_counter=client, **kwargs)
    monkeypatch.setattr(native_agent, 'run_session', run)
    return target, root, selected, view, synthesis_calls


def make(data):
    target, root, selected, view, calls = data
    return flow.create(target, root, view['question'], selected, view['sha256'], deployment())


def test_real_delivery_frozen_sources_and_idempotency(synthesis):
    target, root, selected, view, calls = synthesis
    originals = [( __import__('pathlib').Path(s['directory'])/'analyst'/'artifact.json').read_bytes() for s in selected]
    make(synthesis)
    before = (target/'findings'/'positions.json').read_bytes()
    assert len(list((target/'findings'/'sources').glob('*.txt'))) == 1
    result = flow.execute(target)
    assert result['status'] == 'COMPLETED', result
    assert result['report']['sources'][0]['path'] == 'positions.json'
    assert 'disputed' in result['report']['summary']
    assert len(result['inputs']) == 2
    assert result['inputs'][0]['packet']['reports'][0]['artifact']['payload']['summary'] == 'TTL is five minutes.'
    artifact = (target/'analyst'/'artifact.json').read_bytes()
    # Subsequent source change must not refresh the reviewed snapshots.
    page = next((__import__('pathlib').Path(selected[0]['directory'])/'sources'/'corpus').glob('*.md'))
    page.write_text('changed', encoding='utf-8')
    assert flow.execute(target)['report'] == result['report']
    assert len(calls) == 1 and calls[0][-1] is False
    assert (target/'analyst'/'artifact.json').read_bytes() == artifact
    assert (target/'findings'/'positions.json').read_bytes() == before
    for selection, raw in zip(selected, originals):
        assert (__import__('pathlib').Path(selection['directory'])/'analyst'/'artifact.json').read_bytes() == raw
    assert all(v is not None for v in flow.read(target)[0]['limits'].values())


def test_project_changed_preview_and_tamper_reject_before_model(synthesis):
    target, root, selected, view, calls = synthesis
    with pytest.raises(ValueError, match='another project'):
        flow.preview(root.parent, view['question'], selected)
    with pytest.raises(ValueError, match='changed since preview'):
        flow.create(target, root, 'Different question', selected, view['sha256'], deployment())
    make(synthesis)
    next((target/'findings'/'sources').glob('*.txt')).write_text('tampered', encoding='utf-8')
    with pytest.raises(ValueError, match='source changed'):
        flow.execute(target)
    assert calls == []


def test_cancel_then_retry_same_identity(synthesis):
    target, root, selected, view, calls = synthesis
    make(synthesis)
    identity = flow.read(target)[0]['identity']
    token = CancellationToken(); token.cancel()
    assert flow.execute(target, cancellation=token)['status'] == 'CANCELLED'
    assert not calls
    assert flow.execute(target)['status'] == 'COMPLETED'
    assert flow.read(target)[0]['identity'] == identity


def test_invalid_selection_and_missing_evidence(synthesis):
    target, root, selected, view, calls = synthesis
    for rows in ([selected[0]], [selected[0], selected[0]], selected*5):
        with pytest.raises(ValueError): flow.preview(root, view['question'], rows)
    path = __import__('pathlib').Path(selected[0]['directory'])/'analyst'/'artifact.json'
    path.unlink()
    with pytest.raises((ValueError, OSError)): flow.preview(root, view['question'], selected)
    assert not calls


def test_partial_source_coverage_is_preserved(study, monkeypatch):
    from compute_plane.native_journal import save_json
    from pathlib import Path
    directory, root, calls, fetched, collect = study
    state = research_flow.read(directory)
    state['urls'].append('https://docs.example.com/missing')
    save_json(directory/'research.json', state)
    assert research_flow.execute(directory, fetch=collect)['status'] == 'PARTIAL'
    second = directory.parent/'other'
    research_flow.create(second, root, 'Alternative?', ['https://docs.example.com/guide'], deployment())
    research_flow.execute(second, fetch=collect)
    inputs = [{'kind': 'research', 'directory': str(p)} for p in (directory, second)]
    view = flow.preview(root, 'What coverage is missing?', inputs)
    assert any(p['status'] == 'ERROR' for p in view['inputs'][0]['reports'][0]['coverage'])
    target = directory.parent/'partial-synthesis'
    flow.create(target, root, view['question'], inputs, view['sha256'], deployment())
    from test_research_flow import NATIVE_RUN
    def run(*args, **kwargs):
        digest = hashlib.sha256((target/'findings'/'positions.json').read_bytes()).hexdigest()
        client = Client(steps=[('read', {'repository': 'findings', 'path': 'positions.json'}),
            ('tare_deliver', {'summary': 'Guide agrees; missing URL prevents complete coverage.',
                'remaining': ['Missing URL'], 'sources': [{'repository': 'findings', 'path': 'positions.json', 'sha256': digest}]})])
        return NATIVE_RUN(*args, client=client, local_counter=client, **kwargs)
    monkeypatch.setattr(native_agent, 'run_session', run)
    result = flow.execute(target)
    assert result['status'] == 'PARTIAL'
    assert result['inputs'][0]['packet']['reports'][0]['status'] == 'PARTIAL'
    assert 'ERROR' in json.dumps(result['inputs'])


def test_lost_delivery_resumes_native_session_without_regeneration(synthesis, monkeypatch):
    target, root, inputs, view, calls = synthesis
    make(synthesis)
    original = native_agent.run_session
    def lost(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError('lost result after native durable delivery')
    monkeypatch.setattr(native_agent, 'run_session', lost)
    assert flow.execute(target)['status'] == 'PAUSED'
    raw = (target/'analyst'/'artifact.json').read_bytes()
    saved = (target/'analyst'/'native-agent.json').read_bytes()
    monkeypatch.setattr(native_agent, 'run_session', original)
    assert flow.execute(target)['status'] == 'COMPLETED'
    assert calls[-1][-1] is True
    assert calls[-1][0].generations == 0
    assert calls[-1][3] == calls[0][3]
    assert (target/'analyst'/'artifact.json').read_bytes() == raw
    recovered = json.loads((target/'analyst'/'native-agent.json').read_bytes())
    prior = json.loads(saved)
    assert recovered['config'] == prior['config']
    assert recovered['limits'] == prior['limits']
    assert recovered['session_id'] == prior['session_id']
    assert recovered['deployment'] == prior['deployment']
    assert recovered.get('prompt_capsule') == prior.get('prompt_capsule')
    assert calls[-1][2] == calls[0][2]  # same saved deployment/profile
    # Native resume may append bookkeeping; artifact bytes above bind author/profile/session.


def test_synthesis_selection_does_not_record_planning_adoption(synthesis):
    from relay import planning_findings
    from relay.native_installation import storage_root
    target, root, inputs, view, calls = synthesis
    make(synthesis)
    _, frozen = flow.read(target)
    packet = frozen['inputs'][0]['packet']
    assert packet['review']['status'] == 'OPERATOR_SELECTED_FOR_SYNTHESIS'
    # Synthesis must not manufacture the separate PLAN selection record or duplicate its snapshots.
    assert not (storage_root()/'planning-findings').exists()
    manifest = target/'standalone-packet.json'
    manifest.write_text(json.dumps(packet), encoding='utf-8')
    with pytest.raises(ValueError, match='identity changed'):
        planning_findings.load(manifest, frozen['inputs'][0]['sha256'], root)


def test_aggregate_limit_and_malformed_input_stop_before_creation(synthesis, monkeypatch):
    target, root, inputs, view, calls = synthesis
    for value in ([None, inputs[1]], [{'kind': 'research', 'directory': None}, inputs[1]]):
        with pytest.raises(ValueError, match='kind and saved directory'):
            flow.preview(root, view['question'], value)
    monkeypatch.setattr(flow, 'MAX_BYTES', 16)
    with pytest.raises(ValueError, match='Aggregate source storage'):
        make(synthesis)
    assert not target.exists() and not calls


def test_snapshot_budget_rejects_large_sources_before_reading_them(synthesis, monkeypatch):
    from relay import planning_findings
    target, root, inputs, view, calls = synthesis
    def forbidden(*args, **kwargs):
        pytest.fail('Oversized source must be refused before loading its text')
    monkeypatch.setattr(planning_findings, 'text_source', forbidden)
    with pytest.raises(ValueError, match='Aggregate source storage'):
        planning_findings.preview('research', inputs[0]['directory'], max_bytes=1)
    assert not target.exists() and not calls
