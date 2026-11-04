import ast
from pathlib import Path

from relay.turn_capabilities import CAPABILITIES, FRONTENDS, SHARED

RELAY = Path(__file__).resolve().parents[1]/'relay'
# What one VS Code adapter may take from the other: the chat protocol, not turn logic.
VSCODE_PROTOCOL = {'chat_key', 'text_of', 'user_requests', 'workspace_root', 'run_turn'}


def test_every_capability_is_decided_for_every_frontend():
    for capability, decisions in CAPABILITIES.items():
        assert set(decisions) == set(FRONTENDS), capability
        for frontend, decision in decisions.items():
            assert decision == SHARED or (decision.split(': ', 1)[0] in {'n/a', 'separate', 'gap'}
                                          and len(decision.split(': ', 1)[1]) > 10), (capability, frontend)


def test_frontends_take_shared_turn_logic_from_the_engine_not_from_another_adapter():
    borrowed = {}
    for module in RELAY.glob('*.py'):
        if module.name == 'serve_agent.py':
            continue
        for node in ast.walk(ast.parse(module.read_text(encoding='utf-8'))):
            if isinstance(node, ast.ImportFrom) and node.module == 'serve_agent':
                extra = {alias.name for alias in node.names} - VSCODE_PROTOCOL
                if extra:
                    borrowed[module.name] = sorted(extra)
            if isinstance(node, ast.ImportFrom) and node.level and not node.module:
                if any(alias.name == 'serve_agent' for alias in node.names):
                    borrowed[module.name] = ['serve_agent (whole module)']
    assert borrowed == {}, 'move shared turn logic into relay/turn_engine.py: ' + str(borrowed)


def test_a_permission_mode_chosen_in_one_frontend_is_the_mode_in_the_other(tmp_path):
    from relay import serve_agent
    from relay.project_permissions import read_defaults, save_defaults
    project, control, prefs = tmp_path/'project', tmp_path/'control', tmp_path/'tui-state'
    project.mkdir()
    control.mkdir()
    assert serve_agent.project_mode(project) == 'accept-edits'  # never chosen anywhere
    save_defaults(prefs, project, control, {'mode': 'commands', 'write_paths': ['src']}, 'missing')  # the TUI
    assert serve_agent.project_mode(project) == 'yolo'
    serve_agent.save_project_mode(project, 'read-only')  # /read-only default in VS Code
    assert read_defaults(prefs, project, control)[0] == {'mode': 'read', 'write_paths': ['src']}
    serve_agent.save_project_mode(project, 'accept-edits')
    assert read_defaults(prefs, project, control)[0] == {'mode': 'edit', 'write_paths': ['src']}
