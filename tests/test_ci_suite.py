"""A missing dependency classification must widen CI, never silently skip it."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from scripts import ci_suite as ci


@pytest.fixture
def root(tmp_path):
    (tmp_path/'tests').mkdir()
    (tmp_path/'tests/test_leaf.py').write_text('def test_one(): assert True\n')
    return tmp_path


def test_prose_only_has_no_pytest_but_instruction_changes_are_full(root):
    value = ci.plan(root, {'docs/guides/CLI_MCP.md': 'M', 'CHANGELOG.md': 'M'})
    assert value['scope'] == 'docs' and value['quick'] == []
    for path in ['AGENTS.md', 'CODEX.md', 'docs/AGENTS.md', 'docs/adr/ADR-058.md']:
        assert ci.plan(root, {path: 'M'})['scope'] == 'full'


def test_the_node_service_runs_its_own_tests_not_the_os_suite(root):
    value = ci.plan(root, {'node/tare_node/gpu.py': 'M', 'node/tests/conftest.py': 'M', 'node/README.md': 'D'})
    assert value['scope'] == 'docs' and value['quick'] == []
    assert ci.plan(root, {'node/tare_node/gpu.py': 'M', 'relay/new_module.py': 'A'})['scope'] == 'full'


@pytest.mark.parametrize('path', [
    'relay/native_generation.py', 'relay/native_catalog.py', 'relay/landing_policy.py',
    'relay/new_module.py', 'satellites/tare.tools.kernel', 'pyproject.toml',
    '.github/workflows/ci.yml', 'scripts/ci_suite.py', 'tests/conftest.py',
    'tests/helper_fixture.py', 'contracts/v1/harness_disposition_manifest.json'])
def test_without_a_test_map_code_and_gate_changes_require_full(root, path):
    assert ci.plan(root, {path: 'M'})['scope'] == 'full'


@pytest.mark.parametrize('path', [
    'satellites/tare.tools.kernel', '.gitmodules', '.github/workflows/ci.yml', 'scripts/ci_suite.py',
    'scripts/test_map.py', 'tests/conftest.py', 'AGENTS.md', 'docs/adr/ADR-058.md'])
def test_gates_shared_setup_and_instructions_stay_full_with_a_map(root, path):
    assert ci.plan(root, {path: 'M'}, test_map=fresh_map({}))['scope'] == 'full'


def fresh_map(tests):
    return {'tests': tests, 'full_head': 'h', 'full_at': 0}


@pytest.fixture
def code(root):
    """relay/leaf.py is used by relay/hub.py; each has a test module that ran it."""
    (root/'relay').mkdir()
    (root/'relay/leaf.py').write_text('LIMIT = 1\n\ndef value():\n    return LIMIT\n')
    (root/'relay/hub.py').write_text('from relay.leaf import value\n\ndef total():\n    return value()\n')
    (root/'relay/settings.json').write_text('{}')
    (root/'tests/test_leaf_unit.py').write_text('from relay.leaf import value\n')
    (root/'tests/test_hub.py').write_text('from relay.hub import total\n')
    (root/'tests/test_turn_capabilities.py').write_text('')
    return fresh_map({'tests/test_leaf_unit.py': ['relay/leaf.py'],
                      'tests/test_hub.py': ['relay/hub.py', 'relay/leaf.py'],
                      'tests/test_other.py': ['relay/other.py']})


def test_body_only_change_runs_the_tests_that_ran_it(root, code):
    old = (root/'relay/leaf.py').read_bytes()
    (root/'relay/leaf.py').write_text('LIMIT = 1\n\ndef value():\n    return LIMIT + 0\n')
    value = ci.plan(root, {'relay/leaf.py': 'M'}, test_map=code, previous=lambda path: old)
    assert value['scope'] == 'focused'
    assert value['selected'] == ['tests/test_hub.py', 'tests/test_leaf_unit.py', 'tests/test_turn_capabilities.py']
    (root/'relay/hub.py').write_text('from relay.leaf import value\n\ndef total():\n    return value() + 1\n')
    old = (root/'relay/hub.py').read_bytes()
    value = ci.plan(root, {'relay/hub.py': 'M'}, test_map=code, previous=lambda path: old)
    assert value['selected'] == ['tests/test_hub.py', 'tests/test_turn_capabilities.py']


def test_with_a_function_map_a_body_change_runs_only_the_tests_that_ran_that_function(root, code):
    (root/'relay/leaf.py').write_text('LIMIT = 1\n\ndef value():\n    return LIMIT\n\ndef other():\n    return 2\n')
    old = (root/'relay/leaf.py').read_bytes()
    functions = {**code, 'functions': True, 'tests': {
        'tests/test_leaf_unit.py': ['relay/leaf.py::value'], 'tests/test_hub.py': ['relay/hub.py::total', 'relay/leaf.py::other']}}
    (root/'relay/leaf.py').write_text('LIMIT = 1\n\ndef value():\n    return LIMIT\n\ndef other():\n    return 3\n')
    value = ci.plan(root, {'relay/leaf.py': 'M'}, test_map=functions, previous=lambda path: old)
    assert value['selected'] == ['tests/test_hub.py', 'tests/test_turn_capabilities.py']
    (root/'tests/test_leaf_cli.py').write_text('COMMAND = ["python", "-m", "relay.leaf"]\n')  # another process
    value = ci.plan(root, {'relay/leaf.py': 'M'}, test_map=functions, previous=lambda path: old)
    assert value['selected'] == ['tests/test_hub.py', 'tests/test_leaf_cli.py', 'tests/test_turn_capabilities.py']
    file_level = {key: item for key, item in functions.items() if key != 'functions'}  # recorded before
    value = ci.plan(root, {'relay/leaf.py': 'M'}, test_map=file_level, previous=lambda path: old)
    assert value['selected'] == ['tests/test_hub.py', 'tests/test_leaf_cli.py', 'tests/test_leaf_unit.py',
                                 'tests/test_turn_capabilities.py']


def test_function_names_match_what_code_objects_call_themselves(tmp_path):
    source = ('class Console:\n    def sync(self):\n        def inner():\n            return 1\n        return inner()\n'
              '    async def load(self):\n        return 2\n\n'
              'def outer():\n    class Local:\n        def run(self):\n            return 3\n    return Local().run()\n'
              'if True:\n    def guarded():\n        return 4\n')
    namespace = {}
    exec(compile(source, 'probe', 'exec'), namespace)
    console, outer = namespace['Console'], namespace['outer']
    assert set(ci.definitions(source)) == {
        console.sync.__qualname__, console.sync.__code__.co_consts[1].co_qualname, console.load.__qualname__,
        outer.__qualname__, 'outer.<locals>.Local.run', namespace['guarded'].__qualname__}
    assert ci.changed_functions(lambda path: source.encode(), 'missing.py', tmp_path) is None


@pytest.mark.parametrize('after', [
    'LIMIT = 2\n\ndef value():\n    return LIMIT\n',               # a constant importers read
    'class Settings:\n    LIMIT = 1\n\ndef value():\n    return 1\n'])  # a class body
def test_module_statement_change_also_runs_the_tests_of_its_importers(root, code, after):
    old = (root/'relay/leaf.py').read_bytes()
    (root/'relay/leaf.py').write_text(after)
    hub_only = fresh_map({'tests/test_hub.py': ['relay/hub.py::total']})  # reads LIMIT, never ran leaf's code
    hub_only['functions'] = True
    value = ci.plan(root, {'relay/leaf.py': 'M'}, test_map=hub_only, previous=lambda path: old)
    assert value['scope'] == 'focused'
    assert 'tests/test_hub.py' in value['selected'] and 'tests/test_leaf_unit.py' in value['selected']
    assert ci.plan(root, {'relay/leaf.py': 'M'}, test_map=hub_only, previous=lambda path: None)['selected'] \
        == value['selected']  # no previous version: treated as a module statement change


def test_signature_and_import_changes_reach_callers_and_importing_tests_not_every_importer(root, code):
    calls = fresh_map({'tests/test_hub.py': ['relay/hub.py::total', 'relay/leaf.py::value'],
                       'tests/test_leaf_unit.py': ['relay/leaf.py::helper'], 'tests/test_far.py': ['relay/far.py::run']})
    calls['functions'] = True
    for name in ('test_far.py', 'test_patch.py'):
        (root/'tests'/name).write_text('')
    (root/'relay/far.py').write_text('from relay.leaf import LIMIT\n\ndef run():\n    return LIMIT\n')
    old = (root/'relay/leaf.py').read_bytes()
    (root/'relay/leaf.py').write_text('LIMIT = 1\n\ndef value(scale=1):\n    return LIMIT\n')  # a signature
    value = ci.plan(root, {'relay/leaf.py': 'M'}, test_map=calls, previous=lambda path: old)
    assert set(value['selected']) == {'tests/test_hub.py', 'tests/test_leaf_unit.py', 'tests/test_turn_capabilities.py'}
    (root/'tests/test_patch.py').write_text('TARGET = "relay.leaf.value"\n')  # patches it: never runs it
    value = ci.plan(root, {'relay/leaf.py': 'M'}, test_map=calls, previous=lambda path: old)
    assert 'tests/test_patch.py' in value['selected'] and 'tests/test_far.py' not in value['selected']
    (root/'relay/leaf.py').write_text('import os\nLIMIT = 1\n\ndef value():\n    return LIMIT\n')  # an import
    value = ci.plan(root, {'relay/leaf.py': 'M'}, test_map=calls, previous=lambda path: old)
    assert 'tests/test_leaf_unit.py' in value['selected'] and 'tests/test_far.py' not in value['selected']
    (root/'tests/conftest.py').write_text('TARGET = "relay.leaf.value"\n')  # every test patches it
    (root/'relay/leaf.py').write_text('LIMIT = 1\n\ndef value(scale=1):\n    return LIMIT\n')
    assert ci.plan(root, {'relay/leaf.py': 'M'}, test_map=calls, previous=lambda path: old)['reason'] \
        == 'shared_conftest_dependency'


def test_new_module_runs_tests_naming_it_its_users_count_as_their_own_change(root, code):
    (root/'relay/fresh.py').write_text('def run():\n    return 1\n')
    (root/'tests/test_fresh.py').write_text('from relay.fresh import run\n')
    old = (root/'relay/hub.py').read_bytes()
    (root/'relay/hub.py').write_text('from relay.fresh import run\nfrom relay.leaf import value\n\n'
                                     'def total():\n    return value()\n')
    changes = {'relay/fresh.py': 'A', 'tests/test_fresh.py': 'A'}
    value = ci.plan(root, changes, test_map=code)
    assert set(value['selected']) == {'tests/test_fresh.py', 'tests/test_turn_capabilities.py'}
    value = ci.plan(root, {**changes, 'relay/hub.py': 'M'}, test_map=code, previous=lambda path: old)
    assert set(value['selected']) == {'tests/test_fresh.py', 'tests/test_hub.py', 'tests/test_turn_capabilities.py'}


def test_data_files_follow_their_readers_and_unknown_ones_run_everything(root, code):
    (root/'relay/hub.py').write_text('SETTINGS = "relay/settings.json"\n')
    value = ci.plan(root, {'relay/settings.json': 'M'}, test_map=code)
    assert value['scope'] == 'focused' and value['selected'] == ['tests/test_hub.py']
    (root/'relay/profiles').mkdir()
    (root/'relay/profiles/fast.json').write_text('{}')
    (root/'relay/leaf.py').write_text('FOLDER = "profiles"\n')  # read by folder, not by file name
    value = ci.plan(root, {'relay/profiles/fast.json': 'A'}, test_map=code)
    assert set(value['selected']) == {'tests/test_hub.py', 'tests/test_leaf_unit.py'}
    (root/'relay/orphan.json').write_text('{}')
    assert ci.plan(root, {'relay/orphan.json': 'M'}, test_map=code)['reason'] == 'unreferenced_file'


def test_manifest_and_version_bumps_are_focused_but_dependencies_run_everything(root, code):
    (root/'tests/test_manifest.py').write_text('PATH = "contracts/v1/harness_disposition_manifest.json"\n')
    (root/'contracts/v1').mkdir(parents=True)
    (root/'contracts/v1/harness_disposition_manifest.json').write_text('{}')
    value = ci.plan(root, {'contracts/v1/harness_disposition_manifest.json': 'M'}, test_map=code)
    assert value['scope'] == 'focused' and value['selected'] == ['tests/test_manifest.py']
    (root/'tests/test_version.py').write_text('SOURCE = "pyproject.toml"\n')
    (root/'pyproject.toml').write_text('[project]\nversion = "0.1.63"\ndependencies = []\n')
    bump = lambda path: b'[project]\nversion = "0.1.62"\ndependencies = []\n'
    value = ci.plan(root, {'pyproject.toml': 'M'}, test_map=code, previous=bump)
    assert value['scope'] == 'focused' and value['selected'] == ['tests/test_version.py']
    dependency = lambda path: b'[project]\nversion = "0.1.63"\ndependencies = ["textual"]\n'
    assert ci.plan(root, {'pyproject.toml': 'M'}, test_map=code, previous=dependency)['reason'] \
        == 'dependency_or_packaging_change'


def test_a_missing_stale_or_unreadable_map_is_none(tmp_path):
    path = tmp_path/'map.json'
    assert ci.load_map(path) is None
    path.write_text(json.dumps(fresh_map({'tests/test_a.py': []}) | {'full_at': 1000}))
    assert ci.load_map(path, now=1000 + ci.MAP_MAX_AGE)['full_head'] == 'h'
    assert ci.load_map(path, now=1001 + ci.MAP_MAX_AGE) is None
    path.write_text('{"tests": {}}')
    assert ci.load_map(path) is None
    path.write_text('not json')
    assert ci.load_map(path) is None


def test_runs_fold_into_the_map_and_only_a_finished_full_regression_marks_it_fresh(root, monkeypatch):
    monkeypatch.chdir(root)
    store, ran = root/'cache/map.json', root/'ran.json'
    focused = {'scope': 'focused', 'head': 'new'}
    ran.write_text(json.dumps({'tests/test_leaf.py': ['relay/a.py']}))
    ci.remember(store, ran, focused, 'regression', 1)
    assert json.loads(store.read_text()) == {'tests': {'tests/test_leaf.py': ['relay/a.py']}}
    ran.write_text(json.dumps({'tests/test_other.py': ['relay/b.py::run']}))
    ci.remember(store, ran, {'scope': 'full', 'head': 'new'}, 'regression', 2)  # interrupted
    assert 'full_at' not in json.loads(store.read_text())
    ci.remember(store, ran, {'scope': 'full', 'head': 'new'}, 'quick', 0)
    assert 'full_at' not in json.loads(store.read_text())
    (root/'quick-map.json').write_text(json.dumps({'tests/test_quick.py': ['relay/q.py::check']}))
    ci.remember(store, ran, {'scope': 'full', 'head': 'new'}, 'regression', 1)
    value = json.loads(store.read_text())
    # The whole suite just ran: its own record is the map, and tests it no longer has are gone.
    assert value['full_head'] == 'new' and value['functions'] is True
    assert value['tests'] == {'tests/test_other.py': ['relay/b.py::run'], 'tests/test_quick.py': ['relay/q.py::check']}
    ci.remember(store, root/'missing.json', focused, 'regression', 0)
    assert json.loads(store.read_text()) == value


def test_plugin_records_functions_a_test_file_ran_not_import_time_bodies(tmp_path):
    from scripts import test_map
    (tmp_path/'test_probe.py').write_text(
        'from scripts import ci_suite\nfrom relay import turn_capabilities\n\n'
        'def test_probe():\n    assert ci_suite.is_test("tests/test_x.py")\n')
    output = tmp_path/'map.json'
    repo = Path(test_map.__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, '-B', '-m', 'pytest', str(tmp_path/'test_probe.py'), '-q',
                             '-p', 'scripts.test_map', '-p', 'no:cacheprovider', '--rootdir', str(tmp_path)],
                            cwd=repo, env={**os.environ, 'TARE_TEST_MAP': str(output)}, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout[-2000:]
    (recorded,) = json.loads(output.read_text()).values()
    assert 'scripts/ci_suite.py::is_test' in recorded
    assert not [entry for entry in recorded if entry.startswith('relay/turn_capabilities.py')]  # imported only


def test_isolated_test_changes_select_file_and_preserve_canaries(root):
    value = ci.plan(root, {'tests/test_leaf.py': 'M'})
    assert value['scope'] == 'focused'
    assert value['selected'] == ['tests/test_leaf.py']
    assert value['quick'] == ci.QUICK


@pytest.mark.parametrize('source', [
    'from test_leaf import fixture\n', 'import tests.test_leaf\n',
    'pytest_plugins = ["test_leaf"]\n', 'import importlib; importlib.import_module("tests.test_leaf")\n'])
def test_imported_tests_and_fixture_dependencies_include_transitive_consumers(root, source):
    (root/'tests/test_consumer.py').write_text(source)
    (root/'tests/test_outer.py').write_text('from test_consumer import fixture\n')
    value = ci.plan(root, {'tests/test_leaf.py': 'M'})
    assert value['scope'] == 'focused'
    assert set(value['selected']) == {'tests/test_leaf.py', 'tests/test_consumer.py', 'tests/test_outer.py'}


def test_helper_chain_and_global_conftest_dependency(root):
    (root/'tests/helper.py').write_text('from test_leaf import fixture\n')
    (root/'tests/test_outer.py').write_text('import helper\n')
    value = ci.plan(root, {'tests/test_leaf.py': 'M'})
    assert set(value['selected']) == {'tests/test_leaf.py', 'tests/test_outer.py'}
    (root/'tests/conftest.py').write_text('from test_leaf import fixture\n')
    assert ci.plan(root, {'tests/test_leaf.py': 'M'})['scope'] == 'full'


def test_missing_invalid_removed_and_unknown_diff_are_full(root):
    assert ci.plan(root, {'tests/missing.py': 'A'})['scope'] == 'full'
    assert ci.plan(root, {'tests/test_leaf.py': 'D'})['scope'] == 'full'
    assert ci.plan(root, None)['scope'] == 'full'
    assert ci.plan(root, {})['scope'] == 'full'
    assert ci.plan(root, {'README.md': 'M'}, force_full=True)['scope'] == 'full'
    (root/'tests/test_leaf.py').write_text('not valid python !')
    assert ci.plan(root, {'tests/test_leaf.py': 'M'})['scope'] == 'full'


CONTRACTS = ['tests/test_native_model_listing.py', 'tests/test_native_display.py',
             'tests/test_route_strategy.py']
# The package-data/export contract runs in quick; the rest of the release tests (negative cases and the real
# wheel build) stay in the regression.
PACKAGE_DATA = 'tests/test_native_release_package_data.py'
RELEASE = 'tests/test_native_release.py'


def test_phase_partition_never_duplicates_or_drops_full_suite(root):
    repository = Path(__file__).resolve().parents[1]
    assert all(path in ci.QUICK for path in CONTRACTS)  # catalog JSON/text and frontier admission run first
    assert PACKAGE_DATA in ci.QUICK and RELEASE not in ci.QUICK  # only the contract moves; the rest stays
    assert len(set(ci.QUICK)) == len(ci.QUICK)
    assert all('::' not in path and (repository/path).is_file() for path in ci.QUICK)
    value = ci.plan(root, {'pyproject.toml': 'M'})
    quick = ci.pytest_args(value, 'quick', root, root/'tmp')
    full = ci.pytest_args(value, 'regression', root, root/'tmp2')
    assert all(path in quick for path in ci.QUICK)
    assert 'tests/' in full
    assert {arg.removeprefix('--ignore=') for arg in full if arg.startswith('--ignore=')} == set(ci.QUICK)
    assert not any('::' in arg for arg in quick + full)  # whole files, never node IDs
    (root/'tests/test_native_mcp_evidence.py').write_text('')
    focused = ci.plan(root, {'relay/native_mcp.py': 'M'}, test_map=fresh_map({
        'tests/test_native_mcp_evidence.py': ['relay/native_mcp.py'], 'tests/test_native_mcp.py': ['relay/native_mcp.py']}))
    assert focused['scope'] == 'focused'
    assert focused['selected'] == ['tests/test_native_mcp_evidence.py']
    assert 'tests/test_native_mcp_evidence.py' in ci.pytest_args(focused, 'regression', root, root/'tmp')


@pytest.mark.parametrize('contract', CONTRACTS)
def test_contract_changes_run_in_quick_and_leave_the_focused_selection(root, contract):
    (root/contract).write_text('')  # the synthetic root only needs the file to exist and parse
    value = ci.plan(root, {contract: 'M', 'tests/test_leaf.py': 'M'}, test_map=fresh_map({}))
    assert value['scope'] == 'focused'
    assert contract in value['quick'] and contract in ci.pytest_args(value, 'quick', root, root/'tmp')
    assert value['selected'] == ['tests/test_leaf.py']  # quick already ran the contract: no duplicate
    regression = ci.pytest_args(value, 'regression', root, root/'tmp2')
    assert 'tests/test_leaf.py' in regression and contract not in regression  # quick ran it: no duplicate


def test_the_package_data_contract_runs_in_quick_and_the_release_tests_stay_in_regression(root):
    (root/PACKAGE_DATA).write_text('')
    value = ci.plan(root, {PACKAGE_DATA: 'M'}, test_map=fresh_map({}))
    assert value['scope'] == 'focused'
    assert value['quick'] == ci.QUICK and PACKAGE_DATA in value['quick']
    assert PACKAGE_DATA not in value['selected']  # quick already ran it: no duplicate
    quick = ci.pytest_args(value, 'quick', root, root/'tmp')
    assert PACKAGE_DATA in quick
    assert PACKAGE_DATA not in ci.pytest_args(value, 'regression', root, root/'tmp2')
    full = ci.plan(root, {'pyproject.toml': 'M'})
    regression = ci.pytest_args(full, 'regression', root, root/'tmp3')
    ignored = {arg.removeprefix('--ignore=') for arg in regression if arg.startswith('--ignore=')}
    assert ignored == set(ci.QUICK) and RELEASE not in ignored  # the expensive release tests still run
    assert 'tests/' in regression and not any('::' in arg for arg in quick + regression)


def test_git_diff_has_deletions_spaces_and_no_rename_ambiguity(root, monkeypatch):
    monkeypatch.setattr(subprocess, 'check_output', lambda *a, **k:
        b'M\0tests/test_one.py\0D\0old name.py\0A\0new name.py\0')
    assert ci.changed_paths('base') == {'tests/test_one.py': 'M', 'old name.py': 'D', 'new name.py': 'A'}
    assert ci.changed_paths('0'*40) is None
    def unavailable(*a, **k):
        raise subprocess.CalledProcessError(1, 'git')
    monkeypatch.setattr(subprocess, 'check_output', unavailable)
    assert ci.changed_paths('base') is None


def test_pytest_failure_is_not_success_and_timing_is_saved(root, monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: calls.append((a, k)) or subprocess.CompletedProcess(a, 1))
    value = ci.plan(root, {'pyproject.toml': 'M'})
    assert ci.run(value, 'quick', root, root/'tmp') == 1
    (args,), kwargs = calls[0]
    assert args[args.index('scripts.test_map')-1] == '-p' and kwargs['env']['TARE_TEST_MAP'] == str(root/'quick-map.json')
    receipt = json.loads((root/'quick.json').read_text())
    assert receipt['exit_code'] == 1 and receipt['elapsed_seconds'] >= 0


def test_summary_does_not_call_a_missing_phase_passed(root):
    (root/'plan.json').write_text(json.dumps({**ci.plan(root, None), 'head': 'abc'}))
    (root/'quick.json').write_text(json.dumps({'elapsed_seconds': 1, 'exit_code': 1}))
    (root/'quick.xml').write_text('<testsuite><testcase classname="m" name="slow" time="0.6"/></testsuite>')
    ci.summary(root)
    text = (root/'summary.md').read_text()
    assert '| quick | 1 | 1 |' in text and 'Not run / interrupted' in text
    assert '0.600s: `m.slow`' in text


@pytest.mark.parametrize('event_name,event,expected_scope,base', [
    ('push', {'before': 'prior'}, 'docs', 'prior'),
    ('pull_request', {'pull_request': {'base': {'sha': 'base-sha'}}}, 'docs', 'base-sha'),
    ('schedule', {}, 'full', None),
    ('workflow_dispatch', {}, 'full', None),
])
def test_event_selects_correct_diff_and_manual_always_runs_full(root, monkeypatch, event_name, event, expected_scope, base):
    monkeypatch.chdir(root)
    event_file = root/'event.json'; event_file.write_text(json.dumps(event))
    monkeypatch.setenv('GITHUB_EVENT_PATH', str(event_file))
    monkeypatch.setenv('GITHUB_EVENT_NAME', event_name)
    monkeypatch.setenv('GITHUB_OUTPUT', str(root/'outputs'))
    observed = []
    monkeypatch.setattr(ci, 'changed_paths', lambda value: observed.append(value) or {'README.md': 'M'})
    monkeypatch.setattr(subprocess, 'check_output', lambda *a, **k: 'head-sha\n')
    ci.write_plan(root/'plan.json')
    value = json.loads((root/'plan.json').read_text())
    assert observed == [base] and value['scope'] == expected_scope
    assert value['head'] == 'head-sha'


def test_missing_comparison_does_not_retry_unavailable_base_for_whitespace(root, monkeypatch):
    (root/'plan.json').write_text(json.dumps({'base': 'unavailable', 'comparison_available': False}))
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)
    monkeypatch.setattr(subprocess, 'run', run)
    assert ci.main(['check', '--output', str(root)]) == 0
    assert calls[-1] == ['git', 'diff', '--check', 'HEAD^', 'HEAD', '--']


def test_a_first_commit_has_no_patch_to_check_but_the_next_one_does(root, monkeypatch):
    monkeypatch.chdir(root)
    git = lambda *a: subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t', '-c', 'core.autocrlf=false',
                                     *a], check=True, capture_output=True)
    git('init', '-q')
    (root/'notes.md').write_bytes(b'trailing space \n')
    git('add', 'notes.md'); git('commit', '-q', '-m', 'root')
    (root/'plan.json').write_text(json.dumps({'base': '0'*40, 'comparison_available': False}))
    assert ci.main(['check', '--output', str(root)]) == 0  # a recreated repository's first push
    (root/'more.md').write_bytes(b'again \n')
    git('add', 'more.md'); git('commit', '-q', '-m', 'second')
    assert ci.main(['check', '--output', str(root)]) != 0


def test_a_push_is_compared_with_the_last_commit_whose_regression_passed(root, monkeypatch):
    monkeypatch.chdir(root)
    git = lambda *a: subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t', *a], check=True,
                                    capture_output=True, text=True).stdout.strip()
    git('init', '-q')
    for name in ('a', 'b', 'c'):  # a: green; b: its run stopped at the quick checks; c: this push
        (root/(name+'.md')).write_text(name)
        git('add', '-A'); git('commit', '-q', '-m', name)
    a, b, c = git('rev-list', '--reverse', 'HEAD').split()
    event_file = root/'event.json'; event_file.write_text(json.dumps({'before': b}))
    monkeypatch.setenv('GITHUB_EVENT_PATH', str(event_file))
    monkeypatch.setenv('GITHUB_EVENT_NAME', 'push')
    monkeypatch.setenv('GITHUB_REF_NAME', 'main')
    monkeypatch.delenv('GITHUB_OUTPUT', raising=False)
    test_map = root/'cache/test-map-x.json'
    ci.write_plan(root/'plan.json', test_map)  # nothing verified on this runner yet: the event's before
    assert json.loads((root/'plan.json').read_text())['base'] == b
    ci.mark_verified(test_map, a)
    ci.write_plan(root/'plan.json', test_map)
    value = json.loads((root/'plan.json').read_text())
    assert value['base'] == a and value['base_source'] == 'last_verified' and value['head'] == c
    ci.mark_verified(test_map, 'f'*40)  # not an ancestor (a force push): the event's before again
    ci.write_plan(root/'plan.json', test_map)
    assert json.loads((root/'plan.json').read_text())['base'] == b


@pytest.mark.parametrize('code,event,marked', [(0, 'push', True), (1, 'push', False), (0, 'pull_request', False)])
def test_only_a_passing_regression_of_a_branch_marks_its_head_verified(root, monkeypatch, code, event, marked):
    (root/'plan.json').write_text(json.dumps({'head': 'h1', 'scope': 'focused'}))
    monkeypatch.setenv('GITHUB_EVENT_NAME', event)
    monkeypatch.setenv('GITHUB_REF_NAME', 'main')
    monkeypatch.setattr(ci, 'run', lambda *a: code)
    test_map = root/'cache/test-map-x.json'
    assert ci.main(['regression', '--output', str(root), '--basetemp', str(root/'t'), '--map', str(test_map)]) == code
    path = ci.verified_path(test_map)
    assert (json.loads(path.read_text()) == {'main': 'h1'}) if marked else not path.exists()


SAT = 'satellites/tare.tools.kernel'


def pin(changed):
    return {SAT: {'old_pin': 'a'*40, 'new_pin': 'b'*40, 'changed_files': changed}}


@pytest.fixture
def satellite(root):
    """An OS test imports the kernel's engine module; the map recorded the kernel's functions per test."""
    (root/SAT/'src/kernel').mkdir(parents=True)
    (root/SAT/'src/kernel/engine.py').write_text('LIMIT = 1\n\ndef run():\n    return LIMIT\n\ndef stop():\n    return 0\n')
    (root/'tests/test_engine.py').write_text('from kernel.engine import run\n')
    for name in ('test_stop.py', 'test_other.py', 'test_turn_capabilities.py'):
        (root/'tests'/name).write_text('')
    test_map = fresh_map({'tests/test_engine.py': [SAT+'/src/kernel/engine.py::run'],
                          'tests/test_stop.py': [SAT+'/src/kernel/engine.py::stop'],
                          'tests/test_other.py': ['relay/other.py::go']})
    test_map['functions'] = True
    return test_map


def test_a_pin_body_change_runs_only_the_os_tests_that_ran_that_function(root, satellite):
    old = (root/SAT/'src/kernel/engine.py').read_bytes()
    (root/SAT/'src/kernel/engine.py').write_text('LIMIT = 1\n\ndef run():\n    return LIMIT\n\ndef stop():\n    return 1\n')
    records = pin({'src/kernel/engine.py': 'M'})
    value = ci.plan(root, {SAT: 'M'}, test_map=satellite, previous=lambda path: old, satellites=records)
    assert value['scope'] == 'focused'
    assert value['selected'] == ['tests/test_stop.py', 'tests/test_turn_capabilities.py']
    assert value['satellites'] == records  # pins and changed files, no reason


@pytest.mark.parametrize('after', [
    'LIMIT = 2\n\ndef run():\n    return LIMIT\n\ndef stop():\n    return 0\n',               # a constant
    'LIMIT = 1\n\ndef run():\n    return LIMIT\n\ndef stop(force=False):\n    return 0\n',  # a signature
    'import os\nLIMIT = 1\n\ndef run():\n    return LIMIT\n\ndef stop():\n    return 0\n'])  # an import
def test_a_pin_change_beyond_function_bodies_follows_the_os_rules_not_the_whole_satellite(root, satellite, after):
    kernel_module_nobody_changes(root, satellite)
    (root/SAT/'tests').mkdir()
    (root/SAT/'tests/conftest.py').write_text('from kernel.engine import stop\n')  # the kernel's own setup
    old = (root/SAT/'src/kernel/engine.py').read_bytes()
    (root/SAT/'src/kernel/engine.py').write_text(after)
    value = ci.plan(root, {SAT: 'M'}, test_map=satellite, previous=lambda path: old,
                    satellites=pin({'src/kernel/engine.py': 'M'}))
    # A signature reaches the tests that ran the function; a constant or an import, those that ran the module.
    expected = ['tests/test_stop.py'] if 'force' in after else ['tests/test_engine.py', 'tests/test_stop.py']
    assert value['scope'] == 'focused'
    assert value['selected'] == [*expected, 'tests/test_turn_capabilities.py']


def kernel_module_nobody_changes(root, test_map):
    # audit.py ran in an OS test of its own; a change elsewhere in the kernel never reaches it.
    (root/SAT/'src/kernel/audit.py').write_text('def check():\n    return True\n')
    (root/'tests/test_audit.py').write_text('')
    test_map['tests']['tests/test_audit.py'] = [SAT+'/src/kernel/audit.py::check']


def test_a_pin_constant_change_reaches_the_tests_that_ran_its_importers_inside_the_satellite(root, satellite):
    kernel_module_nobody_changes(root, satellite)
    # limits.py is imported only by the kernel's engine.py: no OS test ran or names it.
    (root/SAT/'src/kernel/limits.py').write_text('LIMIT = 1\n')
    (root/SAT/'src/kernel/engine.py').write_text(
        'from .limits import LIMIT\n\ndef run():\n    return LIMIT\n\ndef stop():\n    return 0\n')
    old = (root/SAT/'src/kernel/limits.py').read_bytes()
    (root/SAT/'src/kernel/limits.py').write_text('LIMIT = 2\n')
    value = ci.plan(root, {SAT: 'M'}, test_map=satellite, previous=lambda path: old,
                    satellites=pin({'src/kernel/limits.py': 'M'}))
    assert value['scope'] == 'focused'
    assert value['selected'] == ['tests/test_engine.py', 'tests/test_stop.py', 'tests/test_turn_capabilities.py']


def test_a_new_module_in_a_pin_runs_the_tests_that_ran_its_importers(root, satellite):
    kernel_module_nobody_changes(root, satellite)
    (root/SAT/'src/kernel/extra.py').write_text('def more():\n    return 1\n')
    records = pin({'src/kernel/extra.py': 'A'})
    value = ci.plan(root, {SAT: 'M'}, test_map=satellite, satellites=records)
    assert value['scope'] == 'focused' and value['selected'] == ['tests/test_turn_capabilities.py']  # unused
    (root/SAT/'src/kernel/engine.py').write_text(
        'from .extra import more\nLIMIT = 1\n\ndef run():\n    return LIMIT\n\ndef stop():\n    return more()\n')
    value = ci.plan(root, {SAT: 'M'}, test_map=satellite, satellites=records)
    assert value['selected'] == ['tests/test_engine.py', 'tests/test_stop.py', 'tests/test_turn_capabilities.py']


def test_every_changed_pin_is_reported_with_its_own_reason_before_the_plan_returns(root, satellite):
    agent, spec, backlog = ('satellites/tare.tools.'+name for name in ('agent-runtime', 'specgraph', 'backlog-graph'))
    records = {**pin({'src/kernel/engine.py': 'M'}),
               agent: {'old_pin': 'c'*40, 'new_pin': 'd'*40, 'changed_files': {'pyproject.toml': 'M'}},
               spec: {'new_pin': 'e'*40}, backlog: {'old_pin': 'f'*40}}
    changes = {SAT: 'M', agent: 'M', spec: 'A', backlog: 'D'}
    test_map = fresh_map({**satellite['tests'], 'tests/test_agent.py': [agent+'/src/agent/loop.py::run']})
    test_map['functions'] = True
    value = ci.plan(root, changes, test_map=test_map, previous=lambda path: None, satellites=records)
    assert value['scope'] == 'full' and value['reason'] == 'satellite_packaging_change'
    assert value['satellites'] == {
        SAT: records[SAT],  # nothing of its own runs everything
        agent: {**records[agent], 'reason': 'satellite_packaging_change'},
        spec: {**records[spec], 'reason': 'satellite_added_or_removed'},
        backlog: {**records[backlog], 'reason': 'satellite_added_or_removed'}}
    for kwargs in ({'test_map': None}, {'test_map': test_map, 'force_full': True}):
        value = ci.plan(root, changes, previous=lambda path: None, satellites=records, **kwargs)
        assert value['scope'] == 'full' and set(value['satellites']) == {SAT, agent, spec, backlog}
        assert all('old_pin' in record or 'new_pin' in record for record in value['satellites'].values())
    value = ci.plan(root, changes, previous=lambda path: None, satellites=records)
    assert value['satellites'][SAT]['reason'] == 'test_map_unavailable'
    assert value['satellites'][spec]['reason'] == 'satellite_added_or_removed'


def test_a_pin_ignores_satellite_prose_and_internal_tests_no_os_code_reaches(root, satellite):
    records = pin({'README.md': 'M', 'docs/GUIDE.md': 'A', 'tests/test_engine_internal.py': 'M',
                   'tests/conftest.py': 'M'})
    value = ci.plan(root, {SAT: 'M'}, test_map=satellite, satellites=records)
    assert value['scope'] == 'focused' and value['selected'] == []
    assert 'reason' not in value['satellites'][SAT]


def test_a_pin_runs_everything_when_its_change_cannot_be_narrowed(root, satellite):
    def reason(status='M', records=pin({'src/kernel/engine.py': 'M'}), test_map=satellite):
        value = ci.plan(root, {SAT: status}, test_map=test_map, previous=lambda path: None, satellites=records)
        assert value['scope'] == 'full' and value['satellites'][SAT]['reason'] == value['reason']
        return value['reason']
    assert reason(status='A') == 'satellite_added_or_removed'
    assert reason(status='D') == 'satellite_added_or_removed'
    assert reason(records={SAT: {'reason': 'satellite_not_checked_out'}}) == 'satellite_not_checked_out'
    assert reason(records=None) == 'satellite_change_unavailable'
    assert reason(test_map=None) == 'test_map_unavailable'
    assert reason(test_map=fresh_map({'tests/test_other.py': ['relay/other.py::go']})) == 'satellite_not_in_test_map'
    assert reason(records=pin({'src/kernel/engine.py': 'D'})) == 'satellite_file_deleted_or_renamed'
    assert reason(records=pin({'pyproject.toml': 'M'})) == 'satellite_packaging_change'
    assert reason(records=pin({'src/assets/table.json': 'M'})) == 'unreferenced_file'


def test_the_pins_and_the_diff_inside_the_checked_out_satellite_are_read_with_git(root, monkeypatch):
    monkeypatch.chdir(root)
    def git(*a, cwd=root):
        return subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t', '-c', 'advice.addEmbeddedRepo=false',
                               *a], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()
    sat = root/SAT
    (sat/'src').mkdir(parents=True)
    (sat/'src/engine.py').write_text('def run():\n    return 1\n')
    (sat/'README.md').write_text('old')
    git('init', '-q', cwd=sat); git('add', '-A', cwd=sat); git('commit', '-q', '-m', 'one', cwd=sat)
    git('init', '-q'); git('add', '-A'); git('commit', '-q', '-m', 'base')  # the satellite as a gitlink
    base = git('rev-parse', 'HEAD')
    (sat/'src/engine.py').write_text('def run():\n    return 2\n')
    (sat/'README.md').unlink()
    git('add', '-A', cwd=sat); git('commit', '-q', '-m', 'two', cwd=sat)
    git('add', '-A'); git('commit', '-q', '-m', 'bump')
    old, new = git('rev-parse', 'HEAD~1', cwd=sat), git('rev-parse', 'HEAD', cwd=sat)
    changes = ci.changed_paths(base)
    assert changes == {SAT: 'M'}
    value = ci.satellite_changes(base, changes)
    assert value == {SAT: {'old_pin': old, 'new_pin': new,
                           'changed_files': {'README.md': 'D', 'src/engine.py': 'M'}}}
    previous = ci.previous_bytes(base, value)
    assert previous(SAT+'/src/engine.py').replace(b'\r\n', b'\n') == b'def run():\n    return 1\n'
    assert previous(SAT+'/src/missing.py') is None
    assert ci.satellite_changes('f'*40, changes)[SAT] == {'reason': 'satellite_pin_unavailable'}
    assert ci.satellite_changes(base, {SAT: 'A'})[SAT] == {'new_pin': new}  # the pins that exist
    assert ci.satellite_changes(base, {SAT: 'D'})[SAT] == {'old_pin': old}
    git('checkout', '-q', old, cwd=sat)
    assert ci.satellite_changes(base, changes)[SAT]['reason'] == 'satellite_checkout_differs_from_pin'
    (sat/'.git').rename(root/'parked-git')
    assert ci.satellite_changes(base, changes)[SAT]['reason'] == 'satellite_not_checked_out'


def test_a_merge_whose_bytes_passed_in_its_pull_request_is_not_run_again(tmp_path, monkeypatch):
    def git(*args):
        return subprocess.run(['git', *args], cwd=tmp_path, capture_output=True, text=True, check=True).stdout.strip()
    git('init', '-q', '-b', 'main'); git('config', 'user.email', 't@localhost'); git('config', 'user.name', 'T')
    (tmp_path/'a.py').write_text('a = 1\n'); git('add', '.'); git('commit', '-qm', 'base')
    git('checkout', '-qb', 'work'); (tmp_path/'a.py').write_text('a = 2\n'); git('commit', '-qam', 'change')
    head = git('rev-parse', 'HEAD')
    git('checkout', '-q', 'main'); git('merge', '-q', '--no-ff', '-m', 'merge', 'work')
    merge = git('rev-parse', 'HEAD')
    monkeypatch.chdir(tmp_path)
    passed = lambda path: {'workflow_runs': [{'name': 'tare.tools CI', 'conclusion': 'success', 'head_sha': head,
                                              'html_url': 'https://ci/runs/7', 'pull_requests': []}]}
    assert ci.tested_in_pull_request(merge, api=passed) == 'https://ci/runs/7'
    assert ci.tested_in_pull_request(merge, api=lambda path: {'workflow_runs': []}) is None  # never passed there
    assert ci.tested_in_pull_request(head, api=passed) is None  # not a merge
    # main moved without the branch catching up: the merged tree is not the tree CI tested
    git('checkout', '-q', 'work'); (tmp_path/'b.py').write_text('b = 1\n'); git('add', '.'); git('commit', '-qm', 'more')
    git('checkout', '-q', 'main'); (tmp_path/'c.py').write_text('c = 1\n'); git('add', '.'); git('commit', '-qm', 'main moved')
    git('merge', '-q', '--no-ff', '-m', 'merge 2', 'work')
    assert ci.tested_in_pull_request(git('rev-parse', 'HEAD'), api=passed) is None


def test_a_changed_pin_runs_the_federation_test_even_in_a_narrow_plan(root, satellite):
    # A pin bump without the READMEs passed a scoped run and left the full suite red (2026-10-06): the test that
    # checks the READMEs against the pins now runs whenever a pin changes.
    old = (root/SAT/'src/kernel/engine.py').read_bytes()
    (root/SAT/'src/kernel/engine.py').write_text('LIMIT = 1\n\ndef run():\n    return LIMIT\n\ndef stop():\n    return 1\n')
    value = ci.plan(root, {SAT: 'M'}, test_map=satellite, previous=lambda path: old, satellites=pin({'src/kernel/engine.py': 'M'}))
    assert value['scope'] == 'focused' and ci.FEDERATION in value['quick']
    assert ci.FEDERATION not in ci.plan(root, {'README.md': 'M'})['quick']  # prose alone stays a docs plan


def test_shards_split_the_plan_into_disjoint_shares_of_similar_measured_time(root):
    for name in ('a', 'b', 'c', 'd', 'e'):
        (root/f'tests/test_{name}.py').write_text('def test(): pass\n')
    full = {'scope': 'full', 'quick': ['tests/test_leaf.py'], 'selected': []}
    store = root/'cache/test-map-linux.json'
    store.parent.mkdir()
    ci.seconds_path(store).write_text(json.dumps({'tests/test_a.py': 50, 'tests/test_b.py': 30, 'tests/test_c.py': 20}))
    shares = [ci.shard_plan(full, shard, 3, root, store) for shard in (1, 2, 3)]
    regression = [name for share in shares for name in share['regression']]
    assert sorted(regression) == ['tests/test_a.py', 'tests/test_b.py', 'tests/test_c.py', 'tests/test_d.py',
                                  'tests/test_e.py']  # every file once, the quick ones in the quick phase only
    assert shares[0]['regression'] == ['tests/test_a.py']  # the longest file gets a shard to itself
    assert [name for share in shares for name in share['quick']] == ['tests/test_leaf.py']
    assert ci.shard_plan(full, 2, 3, root, store) == shares[1]  # every shard computes the same split
    focused = {'scope': 'focused', 'quick': [], 'selected': ['tests/test_d.py']}
    assert [ci.shard_plan(focused, shard, 2, root)['regression'] for shard in (1, 2)] == [['tests/test_d.py'], []]
    assert ci.shard_plan({'scope': 'docs', 'quick': [], 'selected': []}, 1, 2, root) == {
        'shard': 1, 'shards': 2, 'quick': [], 'regression': []}


def test_a_shard_runs_only_its_share(root):
    value = {'scope': 'full', 'quick': ['tests/test_leaf.py'], 'selected': []}
    (root/'shard.json').write_text(json.dumps({'shard': 2, 'shards': 2, 'quick': [], 'regression': ['tests/test_b.py']}))
    args = ci.pytest_args(value, 'regression', root, root/'tmp')
    assert 'tests/test_b.py' in args and 'tests/' not in args
    assert ci.pytest_args(value, 'quick', root, root/'tmp') == []


def test_merge_freshens_the_map_only_when_every_shard_finished_its_share(root, monkeypatch):
    monkeypatch.setenv('GITHUB_EVENT_NAME', 'push')
    monkeypatch.setenv('GITHUB_REF_NAME', 'main')
    monkeypatch.delenv('GITHUB_STEP_SUMMARY', raising=False)
    plan = {'scope': 'full', 'head': 'abc', 'quick': ['tests/test_leaf.py'], 'selected': []}
    def shard(number, quick, regression, ran, code=0):
        folder = root/f'out-{number}'
        folder.mkdir()
        (folder/'plan.json').write_text(json.dumps(plan))
        (folder/'shard.json').write_text(json.dumps({'shard': number, 'shards': 2, 'quick': quick,
                                                     'regression': regression}))
        for phase, files in (('quick', quick), ('regression', regression)):
            if files:
                (folder/(phase+'.json')).write_text(json.dumps({'exit_code': code, 'elapsed_seconds': 1}))
                (folder/(phase+'-map.json')).write_text(json.dumps({name: ran for name in files}))
                (folder/(phase+'.xml')).write_text('<testsuite>' + ''.join(
                    f'<testcase classname="{name[:-3].replace("/", ".")}" name="t" time="2.5"/>' for name in files)
                    + '</testsuite>')
        return folder
    one = shard(1, ['tests/test_leaf.py'], ['tests/test_a.py'], ['relay/a.py::run'])
    store = root/'cache/test-map-linux.json'
    assert ci.merge([one], 2, root/'merged', store) == 1  # shard 2 never reported: no fresh map, no verdict
    assert 'full_at' not in json.loads(store.read_text()) and not ci.verified_path(store).exists()
    two = shard(2, [], ['tests/test_b.py'], ['relay/b.py::run'])
    assert ci.merge([one, two], 2, root/'merged', store) == 0
    value = json.loads(store.read_text())
    assert value['full_head'] == 'abc' and set(value['tests']) == {
        'tests/test_leaf.py', 'tests/test_a.py', 'tests/test_b.py'}
    assert json.loads(ci.verified_path(store).read_text())['main'] == 'abc'
    assert json.loads(ci.seconds_path(store).read_text())['tests/test_b.py'] == 2.5
    red = shard(3, [], ['tests/test_b.py'], [], code=1)
    (red/'shard.json').write_text(json.dumps({'shard': 2, 'shards': 2, 'quick': [], 'regression': ['tests/test_b.py']}))
    assert ci.merge([one, red], 2, root/'merged-red', store) == 1  # a red shard fails the platform


def test_isolate_finds_a_test_that_passes_only_after_another(root, monkeypatch):
    monkeypatch.chdir(root)
    monkeypatch.delenv('GITHUB_STEP_SUMMARY', raising=False)
    # In-process state, like a module imported for the first time under another test's patch: a file on disk
    # would outlive each separate run and hide the dependency.
    (root/'tests/shared_state.py').write_text('ready = False\n')
    (root/'tests/test_a.py').write_text('import shared_state\n\ndef test_sets():\n    shared_state.ready = True\n')
    (root/'tests/test_b.py').write_text('import shared_state\n\ndef test_needs_a():\n    assert shared_state.ready\n')
    output = root/'out'
    output.mkdir()
    (output/'shard.json').write_text(json.dumps({'shard': 1, 'shards': 1, 'quick': ['tests/test_leaf.py'],
                                                 'regression': ['tests/test_a.py', 'tests/test_b.py']}))
    assert ci.isolate(output, root/'not-yet/tmp', seed=7) == 1  # the runs' parent folder need not exist
    receipt = json.loads((output/'isolation/isolation.json').read_text())
    assert list(receipt['alone_failed']) == ['tests/test_b.py']  # alone it has no marker
    assert receipt['alone_failed']['tests/test_b.py'] == ['tests.test_b::test_needs_a']
    assert receipt['seed'] == 7 and sorted(receipt['shuffled_order']) == [
        'tests/test_a.py', 'tests/test_b.py', 'tests/test_leaf.py']
    (root/'tests/test_b.py').write_text('def test_alone():\n    pass\n')
    assert ci.isolate(output, root/'tmp2', seed=7) == 0
