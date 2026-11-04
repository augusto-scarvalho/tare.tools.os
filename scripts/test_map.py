"""pytest plugin (`-p scripts.test_map`): which repository functions each test file runs, for CI selection.

Inert unless TARE_TEST_MAP names the JSON to write. Records `path::qualified.name` for every repository function
that runs while a test file's tests set up, run and tear down; module and class bodies (import time) are not
attributed to a test.
ponytail: child processes (`python -m relay.x`, the `tare` CLI) are not recorded; static references and the
nightly full run cover them. Record children through a site hook if a miss ever slips past both.
"""
import json
import os
import sys
from pathlib import Path

ROOT = os.path.normcase(str(Path(__file__).resolve().parents[1])) + os.sep
OUTPUT = os.environ.get('TARE_TEST_MAP')
FUNCTION = 0x1  # CO_OPTIMIZED: function code, not a module or class body
files, current, tool = {}, set(), None


def started(code, offset, disable=sys.monitoring.DISABLE):
    if code.co_flags & FUNCTION:
        current.add((code.co_filename, code.co_qualname))
    return disable  # once per code object until the next test restarts events


def pytest_configure(config):
    global tool
    if not OUTPUT:
        return
    tool = next(i for i in (3, 4) if sys.monitoring.get_tool(i) is None)
    sys.monitoring.use_tool_id(tool, 'tare-test-map')
    sys.monitoring.register_callback(tool, sys.monitoring.events.PY_START, started)
    sys.monitoring.set_events(tool, sys.monitoring.events.PY_START)


def pytest_runtest_logstart(nodeid, location):
    global current
    if OUTPUT:
        current = files.setdefault(nodeid.split('::')[0], set())
        sys.monitoring.restart_events()


def pytest_sessionfinish(session):
    if not OUTPUT:
        return
    sys.monitoring.set_events(tool, 0)
    sys.monitoring.free_tool_id(tool)
    def relative(name):
        name = os.path.normcase(name)  # absolute for real files; '<frozen os>', '<string>' are not
        return name[len(ROOT):].replace(os.sep, '/') if name.startswith(ROOT) else None
    paths = {name: relative(name) for ran in files.values() for name, _ in ran}
    value = {test: sorted({paths[name]+'::'+qualname for name, qualname in ran
                           if paths[name] and paths[name] not in {test, 'scripts/test_map.py'}})
             for test, ran in files.items()}
    Path(OUTPUT).parent.mkdir(parents=True, exist_ok=True)
    Path(OUTPUT).write_text(json.dumps(value, indent=0, sort_keys=True) + '\n', encoding='utf-8')
