"""The generated plan packet must use the selected Work's declared scope."""
import json
import pytest
from relay import relay_mesh


@pytest.fixture
def packet(tmp_path, monkeypatch):
    original = relay_mesh.RELAY_ROOT
    relay_mesh.rebind_root(tmp_path)
    relay_mesh.ensure_dirs()
    monkeypatch.setattr(relay_mesh, '_coord_ok', lambda action: True)
    monkeypatch.setattr(relay_mesh.planner_role, 'enforce_fence', lambda agent: (True, 'isolated packet test'))
    monkeypatch.setattr(relay_mesh, 'send_inbox_msg', lambda *a, **k: None)
    def create(nodes):
        relay_mesh.WORK_GRAPH_FILE.write_text(json.dumps({'nodes': nodes, 'edges': []}), encoding='utf-8')
        path = relay_mesh.init_train('PACKET-SCOPE', 'Declared scope', [n['id'] for n in nodes], 'planner')
        return (path/'PACKET.md').read_text(encoding='utf-8')
    try:
        yield create
    finally:
        relay_mesh.rebind_root(original)


def node(id='TASK-A', repository='alpha.project', path='src/current.py'):
    return {'id': id, 'title': 'Repair current scope', 'summary': 'Use declared source',
            'exit_criteria': ['preserve behavior'], 'source_refs': ['CURRENT-SOURCE'],
            'repository_scopes': [{'repository': repository, 'target_paths': [path],
                                   'grounding_refs': ['alpha.project:docs/current.md'], 'target_symbols': []}]}


def test_declared_scope_and_sources_replace_unrelated_frozen_harness(packet):
    text = packet([node()])
    assert 'alpha.project' in text and 'src/current.py' in text
    assert 'CURRENT-SOURCE' in text and 'preserve behavior' in text
    for wrong in ('scripts/harness_lib', 'universal-agent-harness-prototype', '186 modules',
                  'C:/projects/tare.tools.research', 'MANDATORY IMPLEMENTATION DIRECTIVE',
                  'All source-of-truth modules and archeological references exist'):
        assert wrong not in text
    assert 'declared' in text.lower()


def test_missing_scope_is_visible_without_inventing_repository_paths(packet):
    item = node(); item.pop('repository_scopes')
    text = packet([item])
    assert any(w in text.lower() for w in ('not declared', 'not configured', 'missing', 'not provided'))
    assert 'Verified Repository & Filesystem Grounding' not in text
    assert 'scripts/harness_lib' not in text and 'C:/projects/' not in text


def test_multiple_tasks_keep_both_scopes_and_task_identities(packet):
    text = packet([node(), node('TASK-B', 'beta.project', 'lib/other.py')])
    for value in ('TASK-A', 'TASK-B', 'alpha.project', 'beta.project', 'src/current.py', 'lib/other.py'):
        assert value in text
