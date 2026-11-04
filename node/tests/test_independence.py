"""node/ lives in tare.tools.os but stays a package of its own: standard library only, never imported by relay/."""
import ast
import sys
from pathlib import Path

import pytest

NODE = Path(__file__).resolve().parents[1]
PROVIDED = {'aiohttp'}  # the ComfyUI hook runs inside ComfyUI, whose server provides it


def imported(folder):
    for path in folder.rglob('*.py'):
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            if isinstance(node, ast.Import):
                yield path, {alias.name.split('.')[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                yield path, {node.module.split('.')[0]}


def test_the_service_imports_only_the_standard_library():
    foreign = {(path.name, name) for path, names in imported(NODE/'tare_node') for name in names
               if name not in sys.stdlib_module_names | PROVIDED | {'tare_node'}}
    assert not foreign


def test_relay_never_imports_the_service():
    relay = NODE.parent/'relay'
    if not relay.is_dir():
        pytest.skip('outside a tare.tools.os checkout')
    assert not [path.name for path, names in imported(relay) if 'tare_node' in names]
