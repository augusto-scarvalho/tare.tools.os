"""Lean stdio frontend for bounded native sessions; the Runtime owns execution."""
import argparse
from contextlib import asynccontextmanager
import hashlib
import json
from pathlib import Path
import re
import sys
import threading

from compute_plane.native_journal import custody_lock, regular, save_json
from . import native_agent, native_limits
from .native_catalog import select
from .native_profiles import describe


def identity(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', value):
        raise ValueError('Use a stable request/message ID of 1-64 letters, digits, dot, dash or underscore.')
    return value


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class Service:
    def __init__(self, root, profiles, *, write_paths=(), verifiers=None, context=True,
                 max_model_calls=120, max_seconds=3600):
        root = regular(root).resolve()
        if not root.is_dir() or not profiles or 'auto' in profiles:
            raise ValueError('Select an existing project and at least one explicit deployment.')
        native_limits.validate({'max_model_calls': max_model_calls, 'max_seconds': max_seconds})
        if max_model_calls is None or max_seconds is None:
            raise ValueError('MCP requires finite call and time ceilings.')
        self.profiles = {name: select(name) for name in profiles}
        self.ceilings = {'max_model_calls': max_model_calls, 'max_seconds': max_seconds}
        caps = ['read', 'list', 'search'] + (['context'] if context else [])
        if write_paths:
            caps.append('edit')
        if verifiers:
            caps.append('verify')
        self.config = native_agent.configuration({
            'schema': 'tare.tools/native-agent-config/1',
            'role': 'implementer' if write_paths else 'auditor' if verifiers else 'explorer',
            'repositories': {root.name: str(root)}, 'capabilities': caps,
            'writable': {root.name: list(write_paths)} if write_paths else {},
            'verifiers': {name: {'repository': root.name, 'argv': argv}
                          for name, argv in (verifiers or {}).items()},
            'specgraph_edit_context': context})
        self.root = root
        self.custody = regular(native_agent.session_root()).resolve()
        if self.custody.is_relative_to(root):
            raise ValueError('MCP session storage must be outside the project; set TARE_STORAGE_ROOT.')
        self.active = {}  # Event-loop owned; no detached daemon or alternate runtime.

    def path(self, request_id):
        identity(request_id)
        return regular(self.custody/('mcp-'+digest([str(self.root), request_id])[:32]))

    def limits(self, profile, calls=None, seconds=None):
        requested = {key: value for key, value in
                     (('max_model_calls', calls), ('max_seconds', seconds)) if value is not None}
        native_limits.validate(requested)
        if any(value > self.ceilings[key] for key, value in requested.items()):
            raise ValueError('Requested limit exceeds the server ceiling.')
        values = native_limits.resolve(self.profiles[profile], requested)
        for key, ceiling in self.ceilings.items():
            values[key] = min(values[key], ceiling) if values[key] is not None else ceiling
        for key in native_limits.TIME_FIELDS[1:]:
            values[key] = min(values[key], values['max_seconds']) if values[key] is not None else values['max_seconds']
        # One retry at most, never a deployment's unlimited default: a retryable failure (429, 502 to 504) is retried
        # once, as everywhere since the kernel pin 78c2089; the request id already keeps a retry from doubling work.
        values['max_model_retries'] = min(values['max_model_retries'] if values['max_model_retries'] is not None else 1, 1)
        values['max_stagnant_turns'] = min(values['max_stagnant_turns'] or 3, 3)
        return values

    def catalog(self):
        return {'profiles': [dict(describe(name, value), mcp_limits=self.limits(name))
                             for name, value in self.profiles.items()],
                'root': str(self.root), 'role': self.config['role'],
                'writable': self.config['writable'], 'verifiers': list(self.config['verifiers']),
                'ceilings': self.ceilings, 'authority': 'NONE'}

    def descriptor(self, request_id):
        data = native_agent.read_json(self.path(request_id)/'mcp-request.json')
        name = data['profile']
        if (data['request_id'] != request_id or data['config'] != self.config
                or name not in self.profiles or data['deployment_digest'] != digest(self.profiles[name])
                or any(data['limits'][key] > self.ceilings[key] for key in self.ceilings)):
            raise ValueError('Session scope/profile changed; restart with its original server settings.')
        return data

    def inspect(self, request_id):
        data = self.descriptor(request_id)
        path = self.path(request_id)
        result = {'request_id': request_id, 'session': str(path), 'profile': data['profile'],
                  'effective_limits': data['limits'], 'authority': 'NONE',
                  'status': 'RUNNING' if request_id in self.active else 'NO_RECORDED_OUTCOME'}
        state_path = path/'native-agent.json'
        if state_path.exists():
            state = native_agent.read_json(state_path, 1024*1024)
            result['deadline_utc'] = state['deadline_utc']
        outcome = path/'outcome.json'
        if outcome.exists():
            saved = native_agent.read_json(outcome, 4*1024*1024)
            for key in ('reason', 'model_calls', 'tokens', 'cost', 'usage_coverage', 'failure_code', 'invocation_id'):
                if key in saved:
                    result[key] = saved[key]
            result['last_outcome_status'] = saved['status']
            output = saved.get('output') or ''
            result['output'] = output[:8192]
            result['output_truncated'] = len(output) > 8192
            if request_id not in self.active:
                invocations = state.get('invocations', []) if state_path.exists() else []
                latest = invocations[-1] if invocations else {}
                result['status'] = saved['status'] if latest.get('id') == saved.get('invocation_id') else 'INTERRUPTED_OR_UNCERTAIN'
        return result

    async def execute(self, request_id, operation):
        import anyio
        from tare_tools_agent_runtime import CancellationToken
        if self.active:
            raise ValueError('This server is busy. Inspect or cancel the active request before starting another.')
        cancel, done, errors = CancellationToken(), threading.Event(), []
        self.active[request_id] = (cancel, done)

        def worker():
            try:
                operation(cancel)
            except BaseException as exc:
                errors.append(exc)
            finally:
                done.set()

        thread = threading.Thread(target=worker, name='tare-mcp-session', daemon=False)
        try:
            thread.start()
            while not done.is_set():
                await anyio.sleep(.05)
        finally:
            cancel.cancel()
            # MCP cancellation/disconnect must not release custody with a live worker.
            with anyio.CancelScope(shield=True):
                while thread.is_alive():
                    await anyio.sleep(.05)
            self.active.pop(request_id, None)
        if errors:
            raise errors[0]
        return self.inspect(request_id)

    async def start(self, request_id, instruction, profile, calls=None, seconds=None):
        path = self.path(request_id)
        if profile not in self.profiles:
            raise ValueError('Deployment is not enabled on this server; call tare_profiles.')
        if not isinstance(instruction, str) or not instruction.strip() or len(instruction.encode()) > 16384:
            raise ValueError('Instruction must contain 1-16384 UTF-8 bytes.')
        limits = self.limits(profile, calls, seconds)
        data = {'request_id': request_id, 'instruction_digest': digest(instruction),
                'profile': profile, 'deployment_digest': digest(self.profiles[profile]),
                'config': self.config, 'limits': limits}

        def run(cancel):
            control = regular(path/'mcp-control')
            control.mkdir(parents=True, exist_ok=True)
            with custody_lock(control):
                file = path/'mcp-request.json'
                if file.exists():
                    if native_agent.read_json(file) != data:
                        raise ValueError('Request ID already belongs to different input/settings.')
                    return  # Retrying start never duplicates model calls or effects.
                save_json(file, data)
                native_agent.run_session(self.config, self.profiles[profile], path, instruction,
                                         limits=limits, cancellation=cancel)
        return await self.execute(request_id, run)

    async def resume(self, request_id, message_id, follow_up=None, refresh=False):
        identity(message_id)
        data = self.descriptor(request_id)
        if follow_up is not None and (not follow_up.strip() or len(follow_up.encode()) > 16384):
            raise ValueError('Follow-up must contain 1-16384 UTF-8 bytes.')
        path = self.path(request_id)

        def run(cancel):
            with custody_lock(path/'mcp-control'):
                receipt = regular(path/'mcp-control'/(message_id+'.json'))
                input_digest = digest([follow_up, refresh])
                if receipt.exists():
                    if native_agent.read_json(receipt)['input_digest'] != input_digest:
                        raise ValueError('Message ID already belongs to different input.')
                    return
                save_json(receipt, {'input_digest': input_digest})
                native_agent.run_session(self.config, self.profiles[data['profile']], path,
                    resume=True, limits=data['limits'], cancellation=cancel, refresh=refresh,
                    follow_up=follow_up, message_id=message_id if follow_up is not None else None)
        return await self.execute(request_id, run)

    def cancel(self, request_id):
        self.path(request_id)
        active = self.active.get(request_id)
        if active:
            active[0].cancel()
        return {'request_id': request_id, 'status': 'CANCELLATION_REQUESTED' if active else 'NOT_ACTIVE',
                'cleanup_verified': False}

    def evidence_path(self, request_id):
        self.descriptor(request_id)
        if request_id in self.active:
            raise ValueError('native_custody_has_active_writer')
        return self.path(request_id)

    def evidence_response(self, request_id, report):
        result = {'request_id': request_id, 'session': str(self.path(request_id)),
                  'authority': 'NONE', 'acceptance': 'unknown', 'report': report}
        # Match the SDK's text serialization, including indentation and escaping.
        if len(json.dumps(result, indent=2).encode('utf-8')) > 32768:
            result.pop('report')
            result.update(error='response_too_large', max_bytes=32768,
                          hint='Narrow review by invocation_id or effect_id; full evidence remains in the saved session.')
        return result

    def review(self, request_id, invocation_id=None, effect_id=None):
        from .native_review import review
        path = self.evidence_path(request_id)
        report = review(path, project=self.root, invocation_id=invocation_id,
                        include_diffs=effect_id is not None, diff_effect_id=effect_id,
                        max_diff_bytes=65536)
        if effect_id is not None:
            report['edits'] = [row for row in report['edits'] if row['effect_id'] == effect_id]
            if not report['edits']:
                raise ValueError('No recorded edit matches this effect and invocation.')
        return self.evidence_response(request_id, report)

    def usage(self, request_id):
        from .execution_usage_sources import execution_usage
        path = self.evidence_path(request_id)
        return self.evidence_response(request_id, execution_usage(sessions=[path]))


def server(service):
    from mcp.server.fastmcp import FastMCP

    @asynccontextmanager
    async def lifespan(_):
        try:
            yield {}
        finally:
            import anyio
            active = tuple(service.active.values())
            for cancel, _ in active:
                cancel.cancel()
            with anyio.CancelScope(shield=True):
                while any(not done.is_set() for _, done in active):
                    await anyio.sleep(.05)

    app = FastMCP('tare.tools', lifespan=lifespan,
                  instructions='Tare owns the inner agent runtime and tools. This server is scoped to one project. '
                  'Use stable request/message IDs; inspect before retrying. Results are proposals, not Work admission. '
                  'Use tare_review for recorded edits/checks and tare_usage for invocation accounting without inference. '
                  'Read-only by default. Call/time ceilings span the saved session, including continuation.')

    @app.tool()
    def tare_profiles() -> dict:
        """Inspect allowed models, effective limits, permissions and usage coverage. No model call."""
        return service.catalog()

    @app.tool()
    async def tare_run(request_id: str, instruction: str, profile: str,
                       max_model_calls: int | None = None, max_seconds: float | None = None) -> dict:
        """Run a bounded task in Tare's runtime. Reuse the request ID only for identical retries."""
        return await service.start(request_id, instruction, profile, max_model_calls, max_seconds)

    @app.tool()
    def tare_inspect(request_id: str) -> dict:
        """Read the saved result, measured usage and limits for a known request ID. No model call."""
        return service.inspect(request_id)

    @app.tool()
    def tare_review(request_id: str, invocation_id: str | None = None,
                    effect_id: str | None = None) -> dict:
        """Review saved edits and check freshness without inference or rerunning tests.

        Default: metadata only. Select an edit effect_id for its bounded diff.
        Optional invocation_id narrows the review. Recorded PASS is not acceptance.
        Responses over 32 KiB return an explicit error and saved-session reference.
        """
        return service.review(request_id, invocation_id, effect_id)

    @app.tool()
    def tare_usage(request_id: str) -> dict:
        """Read deduplicated session/invocation usage without inference; unknown cost stays unknown.

        Responses over 32 KiB return an explicit error and saved-session reference.
        """
        return service.usage(request_id)

    @app.tool()
    async def tare_continue(request_id: str, message_id: str, follow_up: str | None = None,
                            refresh: bool = False) -> dict:
        """Continue a saved session; retain profile, permissions, total calls and original deadline.

        A new message ID explicitly authorizes another invocation. Repeating an ID only inspects.
        Set refresh only to accept changed source files; uncertain side effects remain blocked.
        """
        return await service.resume(request_id, message_id, follow_up, refresh)

    @app.tool()
    def tare_cancel(request_id: str) -> dict:
        """Request cancellation of this server's active session. This acknowledgement is not cleanup proof."""
        return service.cancel(request_id)

    return app


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--deployment', action='append', required=True, help='Allowed profile name; repeat to add models')
    parser.add_argument('--write-path', action='append', default=[], help='Explicit relative edit scope; default read-only')
    parser.add_argument('--verify', action='append', default=[], help='Named verifier: name=["executable","arg",...]')
    parser.add_argument('--no-context', action='store_true', help='Use file/search tools without SpecGraph')
    # Wide by design (2026-10-07): a session that hands units to children shares its ceiling with them, and 16 calls
    # starved every child; the operator narrows them when the numbers say so.
    parser.add_argument('--max-model-calls', type=int, default=120)
    parser.add_argument('--max-seconds', type=float, default=3600)
    args = parser.parse_args(argv)
    try:
        verifiers = {}
        for spec in args.verify:
            name, raw = spec.split('=', 1)
            if name in verifiers:
                raise ValueError('Duplicate verifier name.')
            verifiers[name] = json.loads(raw)
        service = Service(args.root, args.deployment, write_paths=args.write_path,
                          verifiers=verifiers, context=not args.no_context,
                          max_model_calls=args.max_model_calls, max_seconds=args.max_seconds)
        server(service).run(transport='stdio')
        return 0
    except ImportError:
        print('Install the MCP extra: python -m pip install "tare-tools-os[mcp]"', file=sys.stderr)
        return 2
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print('MCP startup failed: '+type(exc).__name__+'. Check profiles, project, storage and limits.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
