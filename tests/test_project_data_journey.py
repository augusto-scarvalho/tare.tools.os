"""A minified project graph can be inspected, saved and resumed without replay."""
import hashlib
import json

from relay import native_agent
from relay.project_graph import observe
from test_native_agent import Client, deployment, setup
from test_work_console import graph_root


def test_minified_investigation_save_and_resume(setup):
    config, custody, root = setup
    raw = json.dumps({'nodes': [{'id': str(i), 'completion': {'status': 'NOT_DONE'},
                                'notes': 'x'*6000} for i in range(200)]}).encode()
    (root/'work-graph.json').write_bytes(raw)
    config.update(capabilities=['read', 'edit'], writable={'repo': ['.']}, verifiers={})
    first = Client(steps=[
        ('read', {'repository': 'repo', 'path': 'work-graph.json', 'json_pointer': '/nodes',
                  'json_fields': '/id,/completion/status', 'limit': 100}),
        ('read', {'repository': 'repo', 'path': 'work-graph.json', 'json_pointer': '/nodes',
                  'json_fields': '/id,/completion/status', 'offset': 100, 'limit': 100,
                  'sha256': hashlib.sha256(raw).hexdigest()}),
        ('tare_deliver', {'summary': 'The graph records 200 NOT_DONE nodes; implementation not independently checked.'})])
    native_agent.run_session(config, deployment(), custody, 'Investigate the selected project graph.',
                             client=first, local_counter=first)
    report = '# Investigation\n\nGraph records 200 NOT_DONE nodes. Implementation is not independently checked.\n'
    save = Client(steps=[('edit', {'repository': 'repo', 'path': 'REPORT.md', 'sha256': None,
                                  'search': '', 'replace': report}),
                         ('tare_deliver', {'summary': 'Saved REPORT.md.'})])
    native_agent.run_session(None, None, custody, resume=True, follow_up='Save the investigation.',
                             message_id='save-report', client=save, local_counter=save)
    before = (custody/'journal.jsonl').read_bytes()
    reopened = Client(steps=[('tare_deliver', {'summary': 'REPORT.md remains saved.'})])
    native_agent.run_session(None, None, custody, resume=True, client=reopened, local_counter=reopened)
    assert (root/'REPORT.md').read_text() == report
    assert (root/'work-graph.json').read_bytes() == raw
    assert (custody/'journal.jsonl').read_bytes() == before
    assert first.generations == 3 and save.generations == 2 and reopened.generations == 0
    assert native_agent.read_json(custody/'native-agent.json')['structured_reads'] is True


def test_graph_summary_exposes_recorded_counts_without_claiming_admission(graph_root):
    graph = graph_root/'work-graph.json'
    raw = json.loads(graph.read_text())
    result = observe(str(graph))
    assert result['node_count'] == len(raw['nodes'])
    assert sum(result['completion_counts'].values()) == len(raw['nodes'])
    assert 'admission labels' in result['summary'] and result['authority'] == 'NONE'
    assert result['omitted_inventory_nodes'] == 0
    assert result['completion_inventory'] == [
        {'id': n['id'], 'status': n.get('completion', {}).get('status', 'UNRECORDED')} for n in raw['nodes']]
    assert len(json.dumps(result, ensure_ascii=False, separators=(',', ':')).encode()) <= 32768


def test_graph_inventory_reports_omitted_nodes_when_snapshot_budget_is_full(graph_root):
    from copy import deepcopy
    path = graph_root/'work-graph.json'
    graph = json.loads(path.read_text())
    sample = graph['nodes'][0]
    for i in range(600):
        row = deepcopy(sample)
        row['id'] = 'additional-'+str(i)+'-'+('x'*100)
        graph['nodes'].append(row)
    path.write_text(json.dumps(graph))
    result = observe(str(path))
    assert result['omitted_inventory_nodes'] > 0
    assert len(result['completion_inventory']) + result['omitted_inventory_nodes'] == len(graph['nodes'])
    assert len(json.dumps(result, ensure_ascii=False, separators=(',', ':')).encode()) <= 32768


def test_legacy_session_keeps_its_original_read_schema(setup, monkeypatch):
    from compute_plane import native_capabilities
    config, custody, root = setup
    config.update(capabilities=['read'], role='explorer', writable={}, verifiers={})
    original = native_capabilities.NativeCapabilities
    def legacy(**kwargs):
        kwargs.pop('structured_reads', None)
        return original(**kwargs)
    monkeypatch.setattr(native_capabilities, 'NativeCapabilities', legacy)
    client = Client(steps=[('tare_deliver', {'summary': 'Original conversation.'})])
    native_agent.run_session(config, deployment(), custody, 'Inspect the project.', client=client, local_counter=client)
    state = native_agent.read_json(custody/'native-agent.json')
    state.pop('structured_reads')
    native_agent.save_json(custody/'native-agent.json', state)
    monkeypatch.setattr(native_capabilities, 'NativeCapabilities', original)
    resumed = Client(steps=[('tare_deliver', {'summary': 'Continued with original tools.'})])
    native_agent.run_session(None, None, custody, resume=True, follow_up='Continue the investigation.',
                             message_id='legacy-follow-up', client=resumed, local_counter=resumed)
    assert resumed.generations == 1
    assert 'json_pointer' not in json.dumps(resumed.calls)
