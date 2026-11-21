import asyncio
from pathlib import Path
import sys
import time
import os
import pytest

from textual.widgets import Input, Select, Static, Button

from relay.console_backend import Backend
from relay.console_terminal import ConsoleTerminals, SessionTerminal
from relay.native_console import WorkConsole
from relay.terminal_panel import TerminalPanel


PROGRAM = '''
import os,sys,time
if os.name == 'nt':
    import msvcrt
    get = msvcrt.getwch
else:
    import tty
    tty.setraw(0)
    get = lambda: os.read(0,1).decode()
sys.stdout.write('\\x1b[2J\\x1b[H\\x1b[6n'); sys.stdout.flush()
reply = ''
while not reply.endswith('R'): reply += get()
assert reply.startswith('\\x1b['), repr(reply)
sys.stdout.write('QUERY_OK\\r\\n'); sys.stdout.flush()
line = ''
while True:
    c = get()
    if c in '\\r\\n': break
    line += c
sys.stdout.write('ACK:'+line+'\\r\\n'); sys.stdout.flush()
time.sleep(60)
'''


def test_pty_answers_query_without_open_panel_and_releases_monitor(tmp_path):
    project = tmp_path/'project'; project.mkdir()
    terminals = ConsoleTerminals()
    try:
        terminals.connection(tmp_path/'custody',{'project':str(project)})
        service = SessionTerminal(terminals,tmp_path/'custody')
        started = service.client.request('start',repository='project',request_id='start',
                                         argv=[sys.executable,'-B','-u','-c',PROGRAM],mode='pty')
        handle = started['handle']
        deadline = time.monotonic()+8
        while time.monotonic()<deadline:
            view = service.view(handle)
            if view['text'] is not None and 'QUERY_OK' in view['text'].plain: break
            time.sleep(.05)
        assert view['text'] is not None and 'QUERY_OK' in view['text'].plain
        assert service.read(handle)['status'] == 'running'
    finally:
        terminals.close()
    assert not terminals._monitor.thread.is_alive()


@pytest.mark.parametrize('size', [(80,24),(130,44)])
def test_process_panel_inputs_closes_reopens_and_stops_owned_command(tmp_path,monkeypatch,size):
    project = tmp_path/'project'; project.mkdir()
    custody = tmp_path/'custody'
    backend = Backend()
    monkeypatch.setattr(backend,'catalog',lambda root: {'models':[], 'sessions':[], 'works':[], 'errors':[]})
    terminals = backend._terminals = ConsoleTerminals()
    terminals.connection(custody,{'project':str(project)})
    service = backend.terminals(custody)
    started = service.client.request('start',repository='project',request_id='start',
        argv=[sys.executable,'-B','-u','-c',PROGRAM],mode='pty')
    handle = started['handle']

    async def until(pilot, check):
        for _ in range(100):
            if check(): return
            await pilot.pause(.05)
        assert check()

    async def scenario():
        app = WorkConsole(project,backend=backend,initial_mode='assistant',storage_preferred=tmp_path/'state')
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            app.assistant_session = str(custody)
            app.action_processes()
            await pilot.pause()
            panel = app.screen
            assert isinstance(panel,TerminalPanel)
            await until(pilot,lambda: panel.handle == handle)
            await until(pilot,lambda: not panel.query_one('#terminal-entry',Input).disabled)
            # A conversation update while this screen is open still reaches its
            # own widgets, not the process controls.
            app.say('Background conversation update')
            app.controls(False)
            panel.query_one('#terminal-keyboard',Button).press()
            await pilot.pause()
            await pilot.press('o','k','enter')
            await until(pilot,lambda: service.view(handle)['text'] is not None and 'ACK:ok' in service.view(handle)['text'].plain)
            await pilot.press('ctrl+g')
            assert not panel.keyboard
            if (lot := os.environ.get('TARE_EXPERIMENT_RUN_DIR')):
                app.save_screenshot(filename=f'panels-real-pty-running-{sys.platform}-{size[0]}.svg',path=lot)
            panel.query_one('#terminal-fit',Button).press()
            await until(pilot,lambda: service.read(handle)['columns'] != 80)
            panel.action_close()
            await until(pilot,lambda: not isinstance(app.screen,TerminalPanel))
            assert service.read(handle)['status'] == 'running'
            app.action_processes(); await pilot.pause()
            panel = app.screen
            await until(pilot,lambda: panel.handle == handle)
            await until(pilot,lambda: not panel.query_one('#terminal-stop',Button).disabled)
            panel.query_one('#terminal-stop',Button).press()
            await until(pilot,lambda: service.read(handle)['cleanup_verified'])
            assert service.read(handle)['status'] == 'stopped'
            await until(pilot,lambda: panel.query_one('#terminal-stop',Button).disabled)
            assert not panel.query_one('#terminal-close',Button).disabled
            if (lot := os.environ.get('TARE_EXPERIMENT_RUN_DIR')):
                app.save_screenshot(filename=f'panels-real-pty-stopped-{sys.platform}-{size[0]}.svg',path=lot)
            panel.action_close()
            await pilot.pause()
    try:
        asyncio.run(scenario())
    finally:
        terminals.close()


def test_pipe_panel_line_input_then_eof(tmp_path,monkeypatch):
    project = tmp_path/'project'; project.mkdir()
    backend = Backend()
    monkeypatch.setattr(backend,'catalog',lambda root: {'models':[], 'sessions':[], 'works':[], 'errors':[]})
    terminals = backend._terminals = ConsoleTerminals()
    custody = tmp_path/'custody'
    terminals.connection(custody,{'project':str(project)})
    service = backend.terminals(custody)
    started = service.client.request('start',repository='project',request_id='pipe',
        argv=[sys.executable,'-B','-u','-c',"import sys; print('READY'); print('ALL:'+sys.stdin.read())"])
    handle = started['handle']
    async def scenario():
        app = WorkConsole(project,backend=backend,initial_mode='assistant',storage_preferred=tmp_path/'state')
        async with app.run_test(size=(80,24)) as pilot:
            await app.workers.wait_for_complete()
            app.assistant_session = str(custody); app.action_processes()
            await pilot.pause()
            panel = app.screen
            for _ in range(40):
                if panel.handle == handle: break
                await pilot.pause(.05)
            assert panel.handle == handle
            panel.query_one('#terminal-entry',Input).value = 'one line'
            panel.send_line()
            panel.operate('terminal-eof')
            for _ in range(80):
                if 'ALL:one line' in panel.pipe_text: break
                await pilot.pause(.05)
            assert 'ALL:one line' in panel.pipe_text
            assert panel.query_one('#terminal-fit',Button).disabled
            for _ in range(80):  # the output can arrive before the process is reaped
                if service.read(handle)['exit_code'] is not None: break
                await pilot.pause(.05)
            assert service.read(handle)['exit_code'] == 0
            panel.action_close(); await pilot.pause()
    try: asyncio.run(scenario())
    finally: terminals.close()


@pytest.mark.skipif(os.name != 'nt',reason='Windows PowerShell')
@pytest.mark.timeout(120)  # a cold powershell.exe on a loaded runner can take well over the suite's 30 s
def test_powershell_read_host_with_live_display(tmp_path):
    def wait_for(service, handle, needle, *, timeout, nudge_after=None):
        started = time.monotonic()
        deadline = started + timeout
        nudged = False
        next_probe = started + 0.5
        view = None
        while time.monotonic() < deadline:
            snapshot = service.view(handle)
            view = snapshot['text']
            plain = view.plain if view is not None else ''
            if needle in plain:
                return True, {'view': view, 'issue': snapshot['issue'], 'sequence': snapshot['sequence']}
            now = time.monotonic()
            if now >= next_probe:
                next_probe = now + 0.5
                state = service.read(handle)
                if state['status'] != 'running':
                    # Re-check view once after exit in case the final ConPTY frame just landed.
                    time.sleep(0.15)
                    snapshot = service.view(handle)
                    view = snapshot['text']
                    if view is not None and needle in view.plain:
                        return True, {'view': view, 'issue': snapshot['issue'], 'sequence': snapshot['sequence']}
                    break
                if nudge_after is not None and not nudged and now - started >= nudge_after and not plain.strip():
                    nudged = True
                    try:
                        # Inbox Windows Server ConPTY occasionally stalls its initial VT frame until resized.
                        service.resize(handle, 81, 24)
                        service.resize(handle, 80, 24)
                    except (ValueError, OSError, RuntimeError):
                        pass
            time.sleep(.05)
        state = service.read(handle)
        snapshot = service.view(handle)
        view = snapshot['text']
        raw_stdout = state['output']['stdout']['text']
        visible = [line.rstrip() for line in (view.plain.splitlines() if view is not None else ()) if line.strip()]
        return False, {
            'status': state['status'],
            'exit_code': state['exit_code'],
            'reason': state['reason'],
            'error': state['error'],
            'elapsed_ms': state['elapsed_ms'],
            'issue': snapshot['issue'],
            'sequence': snapshot['sequence'],
            'raw_stdout_len': len(raw_stdout),
            'raw_stdout_sample': repr(raw_stdout[:200]),
            'visible_lines': visible[:10],
            'nudged': nudged,
        }

    project = tmp_path/'project'; project.mkdir()
    prompt_diagnostics = []
    for attempt in range(2):
        terminals = ConsoleTerminals()
        try:
            custody = tmp_path/f'custody-{attempt}'
            terminals.connection(custody, {'project': str(project)})
            service = SessionTerminal(terminals, custody)
            started = service.client.request('start', repository='project', request_id=f'shell-{attempt}', mode='pty',
                argv=['powershell.exe', '-NoLogo', '-NoProfile', '-Command',
                      "Write-Host 'SHELL_READY'; $answer = Read-Host 'Value'; Write-Output ('ACK:'+$answer)"])
            handle = started['handle']
            # Ceilings, not delays: each loop ends as soon as its text appears. A loaded Windows Server runner can
            # occasionally stall one cold PowerShell ConPTY startup silently; nudge ConPTY once and retry once cleanly.
            ok, info = wait_for(service, handle, 'Value:', timeout=30, nudge_after=8.0)
            if not ok:
                prompt_diagnostics.append({'attempt': attempt + 1, **info})
                continue
            service.send(handle, 'rendered\r')
            acked, ack_info = wait_for(service, handle, 'ACK:rendered', timeout=30)
            assert acked, f'PowerShell Read-Host did not echo ACK:rendered (attempt {attempt + 1}): {ack_info}'
            return
        finally:
            terminals.close()
    raise AssertionError(f'PowerShell Read-Host never rendered Value: across attempts: {prompt_diagnostics}')
