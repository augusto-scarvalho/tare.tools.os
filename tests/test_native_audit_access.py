"""Native CLI subprocess and audit lifecycle; no model or vendor-host invocation."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

from relay.audit_cases import json_bytes
from relay.audit_execution import execute_packet
from relay.audit_independent import configuration, prepare_access
from relay.audit_packet import prepare
from relay.native_audit_access import AuditAccess, collect
from relay.native_audit_cli import hook, vendor_files
from test_audit_cases import cases, request_data
from test_audit_execution import candidate
from test_audit_packet import selection, PacketProvider


@pytest.fixture
def access(selection, tmp_path):
    run = tmp_path / 'attempt'; run.mkdir()
    manifest = prepare_access(prepare(selection), run, run,
                              configuration({'transport': 'native-cli'}))
    return AuditAccess(manifest)


def prefix(settings):
    return shlex.split(json.loads(Path(settings).read_text())['hooks']['PreToolUse'][0]
                       ['hooks'][0]['command'])[:-1]


def execute(prefix, *args):
    result = subprocess.run([*prefix, *args], capture_output=True, text=True,
                            encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def test_cli_reads_without_loading_aider_or_host_drivers(access):
    settings, _ = vendor_files(access.path)
    args = prefix(settings)
    launcher = Path(args[-1])
    code = launcher.read_text()
    blocker = '''
class NoLegacy:
    def find_spec(self, fullname, *args):
        if fullname in {'aider', 'openai', 'compute_plane.aider_driver',
                        'compute_plane.audit_driver', 'compute_plane.audit_exploration'}:
            raise AssertionError('forbidden import: ' + fullname)
sys.meta_path.insert(0, NoLegacy())
'''
    launcher.write_text(code.replace('from relay.native_audit_cli import entry',
                                     blocker + '\nfrom relay.native_audit_cli import entry'))
    source = execute(args, 'read', 'os', 'fix.py', '1', '200')
    assert source['text'] == (Path(access.repository('os')['root']) / 'fix.py').read_bytes().decode()
    assert source['id'].startswith('X-')
    sources, metrics = collect(access.path)
    assert sources == [source] and metrics['verified_sources'] == 1
    assert metrics['completed'] == metrics['generation_calls'] == 0
    assert metrics['calls'] == 1 and metrics['tokens'] is None


def test_isolated_cli_emits_utf8_for_source_text(access):
    text = 'mensagem = "ação Ω 漢"\n'
    path = Path(access.repository('os')['root']) / 'fix.py'
    path.write_bytes(text.encode('utf-8'))
    settings, _ = vendor_files(access.path)
    result = execute(prefix(settings), 'read', 'os', 'fix.py', '1', '1')
    assert result['text'] == text
    receipt = collect(access.path)[1]['reports'][0]
    assert receipt['output_bytes'] == len(json.dumps(result, ensure_ascii=False).encode('utf-8'))


def test_collect_does_not_rewrite_observed_source_receipts(access):
    source = access.invoke('read', repository='os', path='fix.py', start_line=1, end_line=1)
    assert json.loads((access.root/(source['id']+'.json')).read_bytes()) == source
    before = {p.name: p.read_bytes() for p in access.root.iterdir()}
    collect(access.path)
    assert before == {p.name: p.read_bytes() for p in access.root.iterdir()}


@pytest.mark.parametrize('arguments', [
    {'repository': 'other'}, {'path': '../outside.py'}, {'path': '.git/config'},
    {'start_line': 0}, {'end_line': 201}, {'revision': 'HEAD'}, {'path': 'missing.py'},
])
def test_read_refuses_invalid_scope_snapshot_or_range(access, arguments):
    values = dict(repository='os', path='fix.py', start_line=1, end_line=1) | arguments
    with pytest.raises(ValueError):
        access.invoke('read', **values)
    assert not list(access.root.glob('X-*.json'))


@pytest.mark.parametrize('revision', ['INDEX', 'commit'])
def test_git_read_preserves_selected_snapshot(access, revision):
    repo = access.repository('os')
    # fix.py is new; the initial committed candidate has src.txt.
    names = subprocess.check_output(['git', '-C', repo['root'], 'ls-tree', '--name-only', 'HEAD']).decode().splitlines()
    name = next(n for n in names if (Path(repo['root']) / n).is_file())
    revision = repo['head'] if revision == 'commit' else revision
    source = access.invoke('read', repository='os', path=name, start_line=1, end_line=200, revision=revision)
    assert source['revision'] == revision
    (Path(repo['root']) / name).write_text('Changed worktree\n')
    assert collect(access.path)[0] == [source]


@pytest.mark.parametrize('cancel', [True, False])
def test_cancel_or_expired_attempt_cannot_read(access, cancel):
    if cancel:
        Path(access.manifest['cancel_path']).touch()
    else:
        access.manifest['expires_at'] = 0
    with pytest.raises(InterruptedError if cancel else TimeoutError):
        access.invoke('read', repository='os', path='fix.py', start_line=1, end_line=1)
    assert not list(access.root.glob('X-*.json'))


def test_query_uses_real_index_and_never_satisfies_source_requirement(access, tmp_path):
    from specgraph.indexer import index_repository
    root = Path(access.repository('os')['root'])
    (root / 'src').mkdir(exist_ok=True)
    (root / 'src/module.py').write_text('def caller(): return 1\n')
    index = tmp_path / 'index'
    index_repository(root, state_dir=index)
    access.manifest['config']['index_states'] = {'os': str(index)}
    access.path.write_text(json.dumps(access.manifest))
    settings, _ = vendor_files(access.path)
    result = execute(prefix(settings), 'query', 'os', 'caller')
    assert result.get('role') == 'auditor' and result.get('symbols'), result
    assert not result['absence_proven']
    sources, metrics = collect(access.path)
    assert not sources and metrics['verified_sources'] == 0 and metrics['calls'] == 1


@pytest.mark.parametrize('command', [
    'python evil.py', 'PREFIX read os fix.py 1 1; echo bad',
    'PREFIX read os $(echo fix.py) 1 1', 'PREFIX read os fix.py 1 1 > receipt',
    'X=1 PREFIX read os fix.py 1 1', 'PREFIX --hook', 'PREFIX read os fix.py bad 1',
])
def test_hook_rejects_shell_and_non_cli_operations(command):
    args = ['/python', '-I', '-B', '/custody/evidence_cli.py']
    command = command.replace('PREFIX', shlex.join(args))
    assert hook({'tool_name': 'Bash', 'tool_input': {'command': command}}, args)[
        'hookSpecificOutput']['permissionDecision'] == 'deny'


def test_real_hook_accepts_quoted_literal_and_denies_other_capabilities(access):
    settings, _ = vendor_files(access.path); args = prefix(settings)
    command = shlex.join([*args, 'query', 'os', 'literal $(touch nope); x'])
    payload = {'tool_name': 'Bash', 'tool_input': {'command': command}}
    result = subprocess.run([*args, '--hook'], input=json.dumps(payload), capture_output=True,
                            text=True, timeout=30)
    assert json.loads(result.stdout)['hookSpecificOutput']['permissionDecision'] == 'allow'
    for data in ({'tool_name': 'Read'}, {**payload, 'tool_input': {
        'command': command, 'run_in_background': True}}):
        assert hook(data, args)['hookSpecificOutput']['permissionDecision'] == 'deny'


class NativeProvider(PacketProvider):
    read_source = True
    change_source = False

    def run(self, command, *, prompt, **kwargs):
        assert json.loads(command[command.index('--mcp-config') + 1]) == {'mcpServers': {}}
        assert command[command.index('--tools') + 1] == 'Bash'
        assert command[command.index('--allowedTools') + 1] == ''
        args = prefix(command[command.index('--settings') + 1])
        if self.read_source:
            source = execute(args, 'read', 'os', 'unmentioned.py', '1', '1')
            def cite(value):
                value['coverage'][0]['evidence_refs'] = [source['id']]
                if value['findings']:
                    value['findings'][0]['location']['source_ref'] = source['id']
            self.mutation = cite
        data = json.loads(prompt.split(b'IMPLEMENTER RECOMMENDATION AND CURRENT OBJECTIVE:\n')[1])
        if self.change_source:
            manifest = json.loads((Path(args[-1]).parent / 'manifest.json').read_text())
            (Path(manifest['repositories'][0]['root']) / 'unmentioned.py').write_text('Changed\n')
        return super().run(command, prompt=b'fixture\n' + json_bytes(data), **kwargs)


@pytest.mark.parametrize('mode', ['read', 'skip', 'change', 'timeout'])
def test_native_audit_closes_only_with_current_independent_evidence(selection, request_data, cases, mode):
    request_data['exploration'] = {'transport': 'native-cli'}
    root = Path(selection['repositories'][0]['root'])
    (root / 'unmentioned.py').write_text('answer = 2\n')
    selection['repositories'][0]['excluded'] = [{'path': 'unmentioned.py', 'reason': 'Unrelated'}]
    cases.begin_packet(prepare(selection), request_data)
    provider = NativeProvider('ACCEPTED_BOUNDED', failure='timeout' if mode == 'timeout' else None)
    provider.read_source = mode != 'skip'
    provider.change_source = mode == 'change'
    outcome = execute_packet(cases, request_data['case_id'], 1, driver=provider, effort='high')
    result = cases.load(request_data['case_id'])['attempts'][-1]['result']
    assert outcome['status'] == ('CLOSED' if mode == 'read' else 'EXECUTION_FAILED')
    if mode == 'read':
        assert result['metrics']['exploration']['generation_calls'] == 0
        assert result['metrics']['exploration']['completed'] == 0
        assert result['review_status'] == 'VALID'
    elif mode == 'skip':
        assert result['reason'] == 'independent_source_not_read'
    elif mode == 'change':
        assert result['reason'].startswith('exploration_invalid:')
    else:
        assert result['execution_status'] == 'TIMED_OUT'


def test_native_configuration_does_not_accept_legacy_model_overrides():
    with pytest.raises(ValueError, match='invalid_native_audit_config'):
        configuration({'transport': 'native-cli', 'api_base': 'http://localhost:1'})


def test_attempt_prefix_changes_without_changing_cached_system_prompt(selection, tmp_path):
    from relay.audit_independent import independent_prompt
    from relay.native_audit_cli import command_prefix
    packet = prepare(selection)
    manifests = []
    prompts = []
    for name in ('first', 'second'):
        run = tmp_path / name; run.mkdir()
        manifest = prepare_access(packet, run, run, configuration({'transport': 'native-cli'}))
        manifests.append(manifest)
        prompts.append(vendor_files(manifest)[1])
    assert prompts[0] == prompts[1]
    for own, other in ((manifests[0], manifests[1]), (manifests[1], manifests[0])):
        user = independent_prompt(packet, own).decode()
        assert shlex.join(command_prefix(own)) in user
        assert shlex.join(command_prefix(other)) not in user
        assert str(own.parent) not in prompts[0]


def test_facade_read_and_evidence_use_native_sources(access, tmp_path, capsys):
    import tare_ops
    assert tare_ops.main(['audit-evidence', '--manifest', str(access.path),
                          'read', 'os', 'fix.py', '1', '1']) == 0
    assert json.loads(capsys.readouterr().out)['id'].startswith('X-')
    source = tare_ops.evidence(access.repository('os')['root'], 'fix.py', 1, 1,
                               custody=tmp_path / 'direct')
    assert source['sha256'] == collect(access.path)[0][0]['sha256']


def test_native_429_without_reads_remains_provider_failure(selection, request_data, cases):
    class Unavailable(NativeProvider):
        def run(self, command, *, on_output, **kwargs):
            on_output(json_bytes({'type': 'result', 'session_id': command[-1],
                'subtype': 'success', 'is_error': True, 'api_error_status': 429,
                'modelUsage': {}}) + b'\n')
            return 1, b''
    request_data['exploration'] = {'transport': 'native-cli'}
    cases.begin_packet(prepare(selection), request_data)
    execute_packet(cases, request_data['case_id'], 1, driver=Unavailable(), effort='high')
    result = cases.load(request_data['case_id'])['attempts'][-1]['result']
    assert result['reason'] == 'provider_http_429'
    assert result['metrics']['exploration']['verified_sources'] == 0


def test_native_correction_reuses_session_with_new_source_ids(selection, request_data, cases):
    from copy import deepcopy
    request_data['exploration'] = {'transport': 'native-cli'}
    root = Path(selection['repositories'][0]['root'])
    (root / 'unmentioned.py').write_text('answer = 1\n')
    selection['repositories'][0]['excluded'] = [{'path': 'unmentioned.py', 'reason': 'Unrelated'}]
    cases.begin_packet(prepare(selection), request_data)
    provider = NativeProvider()
    execute_packet(cases, request_data['case_id'], 1, driver=provider, effort='high')
    previous = cases.load(request_data['case_id'])['attempts'][0]['result']['evidence_sources'][0]
    (root / 'unmentioned.py').write_text('answer = 2\n')
    corrected = deepcopy(selection); corrected['packet_id'] = 'fix-r2'
    corrected['repositories'][0]['excluded'] = []
    corrected['repositories'][0]['changes'].append({'id': 'C2', 'path': 'unmentioned.py', 'why': 'Fix F1'})
    corrected['obligations'][0]['change_ids'].append('C2')
    reply = {'schema': 'tare.tools/audit-correction/1', 'packet_id': 'fix-r1', 'dispositions': [{
        'finding_id': 'F1', 'disposition': 'FIXED', 'change_refs': ['C2'],
        'test_receipt_refs': ['T1'], 'reason': 'Correct dependency'}]}
    cases.begin_packet(prepare(corrected), {'case_id': request_data['case_id'],
        'correction': reply, 'continuation': {'reason': 'F1 corrected'}}, resume=True)
    provider.verdict = 'ACCEPTED_BOUNDED'
    result = execute_packet(cases, request_data['case_id'], 2, driver=provider, effort='high')
    state = cases.load(request_data['case_id'])
    assert result['status'] == 'CLOSED'
    assert provider.calls[0][-1] == provider.calls[1][-1]
    assert '--resume' in provider.calls[1]
    assert state['anchor']['exploration']['transport'] == 'native-cli'
    assert state['attempts'][1]['result']['evidence_sources'][0]['id'] != previous['id']
