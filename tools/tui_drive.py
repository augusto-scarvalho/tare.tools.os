"""Drive the real TUI as a user for e2e journeys, with diff-only output.

    python tools/tui_drive.py run JOURNEY.json [--backend pilot|pty] [--lot DIR] [--serve] [-v]
    python tools/tui_drive.py step --lot DIR '{"click": "Send", "until": "Olá"}'

Backends: `pilot` (Textual headless, real widgets/bindings/subprocesses, fast) and
`pty` (Windows ConPTY + pyte, real terminal escape/mouse/resize parsing).
Journey: {"root": dir, "size": [w, h], "fixture": ["reply", ...], "steps": [...]}.
Optional "argv" launches another entry (pty only), e.g. the installed product:
["cmd.exe", "/d", "/c", "%LOCALAPPDATA%/tare.tools/product/tare.cmd", "console", "--root", "$ROOT"];
$ROOT, $LOT and %VARS% expand, and the development PYTHONPATH is not inherited.
Optional "storage" reuses an existing state folder (default LOT/state), e.g. after a rollback.
"cli_discovery": true lets the pty console find and ack installed CLIs (real calls).
`fixture` serves scripted chat replies over loopback HTTP; $FIXTURE_PROFILE in a
step expands to its profile path. Without `fixture`, whatever profile the journey
selects is real inference and may be paid.

Steps (each may add "until": text, "timeout": seconds, "quiet": seconds still, "note": text):
  {"paste": text}  {"press": "ctrl+enter" | [keys]}  {"click": label | "#id", "nth": 1}
  {"expect": text} {"absent": text}  {"resize": [w, h]}  {"screen": true}
  {"set": {"#id": value}}  -- pilot only; recorded as setup, not a user action
  {"wait": seconds}  {"raw": bytes}  -- raw is pty only; exact terminal input (escape sequences)
  {"quit": true}
A passing step prints one line (plus changed rows with -v or in serve mode); a
failing step prints the trimmed screen and app state, then stops. Every step's
full screen is kept in LOT/screens/ and LOT/journal.jsonl, not in the output.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time

PROJECTS = Path(__file__).resolve().parents[2]
CHECKOUTS = [PROJECTS/'tare.tools.os', PROJECTS/'tare.tools.kernel', PROJECTS/'tare.tools.agent-runtime'/'src']
PTY_KEYS = {'enter': '\r', 'escape': '\x1b', 'tab': '\t', 'backspace': '\x7f', 'space': ' ',
            'up': '\x1b[A', 'down': '\x1b[B', 'right': '\x1b[C', 'left': '\x1b[D',
            'f1': '\x1bOP', 'f2': '\x1bOQ', 'f3': '\x1bOR', 'f4': '\x1bOS', 'f5': '\x1b[15~',
            **{f'ctrl+{c}': chr(ord(c) - 96) for c in 'abcdefghijklmnopqrstuvwxyz'},
            # Textual turns on the kitty keyboard protocol; Windows Terminal answers with CSI-u.
            'ctrl+enter': '\x1b[13;5u'}


class StepFailed(Exception):
    pass


def trimmed(text):
    return '\n'.join(line.rstrip() for line in text.splitlines() if line.strip())


def changed_rows(before, after, limit=20):
    old, new = before.splitlines(), after.splitlines()
    rows = [f'  {y + 1:>2}| {line.rstrip()}' for y, line in enumerate(new)
            if (old[y] if y < len(old) else '') != line and line.strip()]
    return rows[:limit] + ([f'  ... +{len(rows) - limit} rows'] if len(rows) > limit else [])


def locate(text, label, nth):
    hits = [(line.index(label), y) for y, line in enumerate(text.splitlines()) if label in line]
    if len(hits) > 1 and nth is None:
        raise StepFailed(f'{label!r} is ambiguous at rows {[y + 1 for _, y in hits]}; add "nth"')
    if len(hits) < (nth or 1):
        raise StepFailed(f'{label!r} not on screen')
    x, y = hits[(nth or 1) - 1]
    return x + len(label) // 2, y  # 0-based cell inside the label


def fixture(replies, lot):
    """Loopback provider: one scripted reply per model call, streamed when asked."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    sys.path.insert(0, str(CHECKOUTS[0]/'tests'))
    from native_stream_fixture import sse
    from test_native_agent import Client, deployment
    replies, counters = iter(replies), Client()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *unused):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if not self.path.endswith('/chat/completions'):
                raw = json.dumps(counters.post(self.path, body, None)).encode()
                self.send_response(200); self.end_headers(); self.wfile.write(raw); return
            text = next(replies, 'fixture exhausted')
            usage = {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}
            self.send_response(200)
            if body.get('stream'):
                self.send_header('Content-Type', 'text/event-stream'); self.end_headers()
                for part in ({'role': 'assistant', 'content': text}, {}):
                    self.wfile.write(sse({'model': 'fixture', 'choices': [
                        {'index': 0, 'delta': part, 'finish_reason': None if part else 'stop'}]}))
                self.wfile.write(sse({'model': 'fixture', 'choices': [], 'usage': usage}) + b'data: [DONE]\n\n')
            else:
                raw = json.dumps({'model': 'fixture', 'usage': usage, 'choices': [{'finish_reason': 'stop',
                    'message': {'role': 'assistant', 'content': text}}]}).encode()
                self.end_headers(); self.wfile.write(raw)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    Thread(target=server.serve_forever, daemon=True).start()
    profile = lot/'fixture-profile.json'
    profile.write_text(json.dumps(deployment(endpoint=f'http://127.0.0.1:{server.server_port}')))
    return str(profile)


class PilotUI:
    """Textual headless: real app, widgets, bindings and subprocesses; no terminal parsing."""
    quiet = .3

    def __init__(self, app, pilot):
        self.app, self.pilot = app, pilot

    def screen(self):
        import io
        from rich.console import Console
        width, height = self.app.size
        console = Console(width=width, height=height, file=io.StringIO(), record=True,
                          force_terminal=True, legacy_windows=False, safe_box=False)
        # ponytail: private compositor, same call as App.export_screenshot; breaks on Textual refactor.
        console.print(self.app.screen._compositor.render_update(full=True, screen_stack=self.app._background_screens))
        return console.export_text(styles=False)

    def state(self):
        app, result = self.app, getattr(self.app, 'last_result', None) or {}
        focused = app.focused
        return {'screen': type(app.screen).__name__, 'busy': getattr(app, 'busy', None),
                'focus': focused and (f'#{focused.id}' if focused.id else type(focused).__name__),
                'result': {k: str(result[k])[:200] for k in ('status', 'reason', 'model_calls', 'output') if k in result}}

    async def wait(self, seconds):
        await self.pilot.pause(seconds)

    async def paste(self, text):
        from textual import events
        # Enter at the App like the terminal driver does; the App forwards it to focus once.
        self.app.post_message(events.Paste(text))

    async def press(self, keys):
        await self.pilot.press(*keys)

    async def click(self, label, nth):
        if label.startswith('#'):
            await self.pilot.click(label)
        else:
            await self.pilot.click(offset=locate(self.screen(), label, nth))

    async def resize(self, size):
        await self.pilot.resize_terminal(*size)

    async def raw(self, text):
        raise StepFailed('"raw" terminal input needs the pty backend')

    async def quit(self):
        await self.pilot.press('ctrl+q')

    async def set(self, values):
        for selector, value in values.items():
            widget = self.app.query_one(selector)
            widget.load_text(value) if hasattr(widget, 'load_text') else setattr(widget, 'value', value)


class PtyUI:
    """Windows ConPTY: the real console entry point behind a real pseudo-terminal."""
    quiet = .8  # panel transitions keep laying out past 0.3s in a real terminal

    def __init__(self, terminals, handle, size):
        import pyte
        self.terminals, self.handle, self.cursors, self.sent = terminals, handle, {}, 0
        self.term = pyte.Screen(*size)
        self.stream = pyte.Stream(self.term)

    def pump(self):
        view = self.terminals.read(owner='drive', handle=self.handle, cursors=self.cursors, limit=65536)
        for name, chunk in view['output'].items():
            self.cursors[name] = chunk['cursor']
            self.stream.feed(chunk['text'])
        self.status = {k: view.get(k) for k in ('status', 'exit_code', 'cleanup_verified')}

    def screen(self):
        return '\n'.join(self.term.display)

    def state(self):
        return self.status

    async def wait(self, seconds):
        await asyncio.sleep(seconds)
        self.pump()

    def send(self, text):
        # A unique id per send: TerminalSessions deduplicates repeated request ids.
        self.sent += 1
        self.terminals.send(owner='drive', handle=self.handle, request_id=f'k{self.sent}', text=text)

    async def paste(self, text):
        self.send('\x1b[200~' + text + '\x1b[201~')

    async def press(self, keys):
        for key in keys:
            if key not in PTY_KEYS and len(key) != 1:
                raise StepFailed(f'no terminal sequence for {key!r}')
            self.send(PTY_KEYS.get(key, key))

    async def click(self, label, nth):
        if label.startswith('#'):
            raise StepFailed('pty clicks by visible label, not selector')
        x, y = locate(self.screen(), label, nth)
        self.send(f'\x1b[<0;{x + 1};{y + 1}M\x1b[<0;{x + 1};{y + 1}m')

    async def resize(self, size):
        self.terminals.resize(owner='drive', handle=self.handle, columns=size[0], rows=size[1])
        self.term.resize(size[1], size[0])

    async def raw(self, text):
        self.send(text)

    async def quit(self):
        self.send('')  # Ctrl+Q, as a user leaves
        for _ in range(100):
            await self.wait(.1)
            if self.status['status'] != 'running':
                return
        raise StepFailed('still running 10s after Ctrl+Q')

    async def set(self, values):
        raise StepFailed('"set" bypasses the UI; pty journeys must click and type')


async def settle(ui, until, timeout, quiet=.3):
    """Wait for `until` (if given), then for the screen to hold still for `quiet` seconds.

    `until` must survive the settling, so a transient frame cannot pass a step."""
    deadline = time.monotonic() + timeout
    await ui.wait(.1)
    while until is not None and until not in ui.screen():
        if time.monotonic() > deadline:
            raise StepFailed(f'{until!r} not on screen after {timeout}s')
        await ui.wait(.1)
    last, still_since = ui.screen(), time.monotonic()
    # ponytail: a spinner that never stops just ends the wait at the deadline, without failing.
    while time.monotonic() - still_since < quiet and time.monotonic() < deadline + quiet:
        await ui.wait(.1)
        if ui.screen() != last:
            last, still_since = ui.screen(), time.monotonic()
    if until is not None and until not in last:
        raise StepFailed(f'{until!r} appeared, then left the screen before it settled')


async def perform(ui, step, variables):
    step = json.loads(json.dumps(step).replace('$FIXTURE_PROFILE', json.dumps(variables.get('FIXTURE_PROFILE', ''))[1:-1]))
    await ui.wait(step.get('wait', 0))  # also refreshes the frame before locating anything
    if 'paste' in step:
        await ui.paste(step['paste'])
    if 'press' in step:
        await ui.press([step['press']] if isinstance(step['press'], str) else step['press'])
    if 'click' in step:
        await ui.click(step['click'], step.get('nth'))
    if 'raw' in step:
        await ui.raw(step['raw'])
    if 'resize' in step:
        await ui.resize(step['resize'])
    if 'set' in step:
        await ui.set(step['set'])
    await settle(ui, step.get('until'), step.get('timeout', 5 if 'until' not in step else 60), step.get('quiet', ui.quiet))
    text = ui.screen()
    if 'expect' in step and step['expect'] not in text:
        raise StepFailed(f'expected {step["expect"]!r}')
    if 'absent' in step and step['absent'] in text:
        raise StepFailed(f'unexpected {step["absent"]!r}')
    return text


async def drive(ui, steps, lot, variables, verbose, serve):
    journal, before, number = (lot/'journal.jsonl').open('a', encoding='utf-8'), ui.screen(), 0
    (lot/'screens').mkdir(exist_ok=True)

    def inbox():
        yield from ((step, None) for step in steps)
        if not serve:
            return
        controls, done = lot/'controls', set()
        controls.mkdir(exist_ok=True)
        print(f'serving: {controls}', file=sys.__stdout__, flush=True)
        while True:
            fresh = sorted(p for p in controls.glob('*.json') if p.name not in done)
            if not fresh:
                yield None, None
                continue
            done.add(fresh[0].name)
            yield json.loads(fresh[0].read_text(encoding='utf-8')), lot/'out'/(fresh[0].stem + '.txt')

    for step, out in inbox():
        if step is None:
            await ui.wait(.1)
            continue
        number += 1
        label = ' '.join(f'{k}={json.dumps(v, ensure_ascii=False)[:60]}' for k, v in step.items())
        started, failure = time.monotonic(), None
        try:
            if step.get('quit'):
                await ui.quit()
                after = ui.screen()
            else:
                after = await perform(ui, step, variables)
        except StepFailed as error:
            failure, after = str(error), ui.screen()
        # An input that changed nothing is the usual sign of a lost key or click.
        inert = not failure and after == before and any(k in step for k in ('click', 'press', 'paste', 'raw'))
        verdict = 'FAIL' if failure else 'ok (no visible change)' if inert else 'ok'
        lines = [f'{number:02} {verdict} {time.monotonic() - started:.1f}s {label}']
        if failure:
            lines += [f'  -> {failure}', trimmed(after), f'  state: {json.dumps(ui.state(), ensure_ascii=False)}']
        elif step.get('screen'):
            lines.append(trimmed(after))
        elif verbose or out:
            lines += changed_rows(before, after)
        (lot/'screens'/f'{number:03}.txt').write_text(after, encoding='utf-8')
        journal.write(json.dumps({'n': number, 'step': step, 'ok': not failure, 'failure': failure, 'inert': inert,
                                  'state': ui.state(), 'at': time.time()}, ensure_ascii=False) + '\n')
        journal.flush()
        print('\n'.join(lines), flush=True)
        if out:
            out.parent.mkdir(exist_ok=True)
            out.with_suffix('.tmp').write_text('\n'.join(lines), encoding='utf-8')
            out.with_suffix('.tmp').replace(out)
        before = after
        if step.get('quit') or (failure and not out):
            return not failure
    return True


def head(repo):
    run = subprocess.run(['git', '-C', str(repo), 'rev-parse', '--short', 'HEAD'], capture_output=True, text=True)
    return run.stdout.strip()


async def run(args):
    journey = json.loads(Path(args.journey).read_text(encoding='utf-8'))
    lot = (args.lot or PROJECTS/'.reconciliation/evidence-store/reports/tui-drive'/
           f'{Path(args.journey).stem}-{args.backend}-{time.strftime("%Y%m%d-%H%M%S")}').resolve()
    lot.mkdir(parents=True, exist_ok=True)
    root = Path(journey.get('root') or lot/'workspace'); root.mkdir(parents=True, exist_ok=True)
    size, variables = tuple(journey.get('size', (100, 30))), {}
    if 'fixture' in journey:
        variables['FIXTURE_PROFILE'] = fixture(journey['fixture'], lot)
    os.environ.update(PYTHONPATH=os.pathsep.join(map(str, CHECKOUTS)), TARE_STORAGE_ROOT=str(journey.get('storage') or lot/'state'),
                      PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
    (lot/'run.json').write_text(json.dumps({
        'backend': args.backend, 'provider': 'fixture' if 'fixture' in journey else 'as selected in journey',
        'root': str(root), 'size': size, 'journey': str(Path(args.journey).resolve()),
        'argv': journey.get('argv', 'development sources'),
        'heads': {p.name if p.name != 'src' else p.parent.name: head(p) for p in CHECKOUTS}}, indent=2))
    print(f'lot: {lot}  backend={args.backend}  provider={"fixture" if "fixture" in journey else "REAL"}', flush=True)
    steps = journey.get('steps', [])
    if args.backend == 'pilot':
        sys.path[:0] = [str(p) for p in CHECKOUTS]
        from relay import relay_mesh
        from relay.console_backend import Backend
        from relay.native_console import WorkConsole
        relay_mesh.RELAY_ROOT = lot/'no-control-graph'
        app = WorkConsole(root, backend=Backend(), initial_mode='assistant', storage_preferred=lot/'state')
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            return await drive(PilotUI(app, pilot), steps, lot, variables, args.verbose, args.serve)
    sys.path.insert(0, str(CHECKOUTS[1]))
    from compute_plane.terminal_sessions import TerminalSessions
    with TerminalSessions(output_chars=8 * 1024 * 1024) as terminals:
        # Startup CLI discovery makes real ack calls; journeys opt in with "cli_discovery": true.
        env = dict(os.environ, TERM='xterm-256color', COLORTERM='truecolor',
                   TARE_CLI_DISCOVERY='1' if journey.get('cli_discovery') else '0')
        argv = [sys.executable, '-B', '-c', 'from relay.native_installation import main; raise SystemExit(main())',
                'console', '--root', str(root), '--storage-root', os.environ['TARE_STORAGE_ROOT']]
        cwd = CHECKOUTS[0]
        if 'argv' in journey:
            cwd = root
            env.pop('PYTHONPATH')  # an installed entry must not see the development sources
            argv = [os.path.expandvars(a.replace('$ROOT', str(root)).replace('$LOT', str(lot))) for a in journey['argv']]
        view = terminals.start(owner='drive', request_id='tui', mode='pty', columns=size[0], rows=size[1],
            argv=argv, cwd=str(cwd), env=env, timeout=2400)
        ui = PtyUI(terminals, view['handle'], size)
        try:
            await settle(ui, journey.get('ready', 'Ctrl+Enter'), 60, quiet=1)  # early keys get lost
            return await drive(ui, steps, lot, variables, args.verbose, args.serve)
        finally:
            final = terminals.stop(owner='drive', handle=ui.handle)
            (lot/'terminal-final.json').write_text(json.dumps(final, indent=2))
            print(f'terminal: status={final.get("status")} exit={final.get("exit_code")} '
                  f'cleanup_verified={final.get("cleanup_verified")}')


def step(args):
    controls = args.lot/'controls'
    number = max([int(p.stem) for p in controls.glob('*.json')] + [0]) + 1
    out = args.lot/'out'/f'{number:03}.txt'
    (controls/f'{number:03}.tmp').write_text(args.step, encoding='utf-8')
    (controls/f'{number:03}.tmp').replace(controls/f'{number:03}.json')
    deadline = time.monotonic() + json.loads(args.step).get('timeout', 60) + 30
    while not out.exists():
        if time.monotonic() > deadline:
            sys.exit('no answer from the serving driver')
        time.sleep(.1)
    print(out.read_text(encoding='utf-8'))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    runner = sub.add_parser('run')
    runner.add_argument('journey')
    runner.add_argument('--backend', choices=('pilot', 'pty'), default='pilot')
    runner.add_argument('--lot', type=Path)
    runner.add_argument('--serve', action='store_true', help='after the journey, take steps from LOT/controls')
    runner.add_argument('-v', '--verbose', action='store_true', help='print changed rows for passing steps')
    stepper = sub.add_parser('step')
    stepper.add_argument('--lot', type=Path, required=True)
    stepper.add_argument('step')
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding='utf-8')
    if args.command == 'step':
        return step(args)
    return 0 if asyncio.run(run(args)) else 1


if __name__ == '__main__':
    sys.exit(main())
