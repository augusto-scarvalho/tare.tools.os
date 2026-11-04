"""Optional Claude host adapter over the native evidence CLI, with no MCP server."""
import argparse
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import shlex
import sys

from relay.native_audit_access import SYSTEM_PROMPT, main, parser
from relay.relay_io import write_json_atomic


def hook(payload, prefix):
    allowed = isinstance(payload, dict) and payload.get('tool_name') == 'StructuredOutput'
    if isinstance(payload, dict) and payload.get('tool_name') == 'Bash':
        data = payload.get('tool_input', {})
        try:
            command = data['command']
            if (not isinstance(command, str) or len(command.encode()) > 8192
                    or set(data) - {'command', 'description', 'timeout', 'run_in_background'}
                    or data.get('run_in_background', False) is not False):
                raise ValueError('closed_cli_only')
            args = shlex.split(command, posix=True)
            # Exact canonical POSIX quoting makes metacharacters literal arguments.
            # No shell expansion, chaining, environment overrides or redirection.
            if command != shlex.join(args) or args[:len(prefix)] != prefix:
                raise ValueError('closed_cli_only')
            with contextlib.redirect_stderr(io.StringIO()):
                parser().parse_args(args[len(prefix):])
            allowed = True
        except (ValueError, KeyError, TypeError, SystemExit, argparse.ArgumentError):
            allowed = False
    return {'hookSpecificOutput': {'hookEventName': 'PreToolUse',
        'permissionDecision': 'allow' if allowed else 'deny',
        'permissionDecisionReason': 'Use only the pinned audit-evidence read/query CLI with canonical shell quoting.'}}


def entry(manifest, prefix):
    if sys.argv[1:] == ['--hook']:
        try:
            payload = json.loads(sys.stdin.buffer.read(65537))
        except (ValueError, UnicodeError):
            payload = None
        print(json.dumps(hook(payload, prefix)))
        return 0
    return main(manifest=manifest)


def command_prefix(manifest):
    launcher = Path(manifest).resolve().parent / 'evidence_cli.py'
    return [Path(sys.executable).as_posix(), '-I', '-B', launcher.as_posix()]


def vendor_files(manifest):
    path = Path(manifest).resolve()
    roots = [str(Path(__file__).resolve().parents[1])]
    for package in ('compute_plane', 'tare_tools_agent_runtime', 'specgraph'):
        spec = importlib.util.find_spec(package)
        if spec is None or spec.origin is None:
            raise ValueError('native_audit_dependency_missing:' + package)
        roots.append(str(Path(spec.origin).resolve().parent.parent))
    launcher = path.parent / 'evidence_cli.py'
    prefix = command_prefix(path)
    # Only host-selected code roots; the child never inherits candidate PYTHONPATH.
    launcher.write_text('import sys\nsys.path[:0] = ' + repr(roots)
        + '\nfrom relay.native_audit_cli import entry\nraise SystemExit(entry('
        + repr(str(path)) + ', ' + repr(prefix) + '))\n', encoding='utf-8')
    settings = path.parent / 'settings.json'
    write_json_atomic(settings, {'hooks': {'PreToolUse': [{'matcher': '.*', 'hooks': [
        {'type': 'command', 'command': shlex.join(prefix + ['--hook']), 'timeout': 10}]}]}})
    prompt = SYSTEM_PROMPT + '\nUse the CURRENT EVIDENCE CLI prefix in the current user message. '
    prompt += 'It replaces any previous attempt prefix. Use POSIX shell quoting for arguments.'
    prompt += '\nCommands: read <repository> <path> <start_line> <end_line> [--revision WORKTREE|INDEX|commit]; query <repository> <query>.\n'
    return settings, prompt


def build_command(driver, manifest, **kwargs):
    settings, prompt = vendor_files(manifest)
    command = driver.build_packet_command(**kwargs)
    command[command.index('--safe-mode')] = '--restricted'
    command[command.index('--tools') + 1] = 'Bash'
    # No blanket Bash allow rule. The hook grants the closed CLI operation;
    # the installed host's permission composition still requires qualification.
    command[command.index('--allowedTools') + 1] = ''
    command[command.index('--system-prompt') + 1] = prompt
    return command[:-2] + ['--setting-sources', '', '--settings', str(settings),
                          '--mcp-config', '{"mcpServers":{}}'] + command[-2:]
