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
            assert service.read(handle)['exit_code'] == 0
            panel.action_close(); await pilot.pause()
    try: asyncio.run(scenario())
    finally: terminals.close()


@pytest.mark.skipif(os.name != 'nt',reason='Windows PowerShell')
def test_powershell_read_host_with_live_display(tmp_path):
    project = tmp_path/'project'; project.mkdir()
    terminals = ConsoleTerminals()
    try:
        terminals.connection(tmp_path/'custody',{'project':str(project)})
        service = SessionTerminal(terminals,tmp_path/'custody')
        started = service.client.request('start',repository='project',request_id='shell',mode='pty',
            argv=['powershell.exe','-NoLogo','-NoProfile','-Command',
                  "Write-Host 'SHELL_READY'; $answer = Read-Host 'Value'; Write-Output ('ACK:'+$answer)"])
        handle = started['handle']
        deadline = time.monotonic()+10
        while time.monotonic()<deadline:
            view = service.view(handle)['text']
            if view is not None and 'Value:' in view.plain: break
            time.sleep(.05)
        assert view is not None and 'Value:' in view.plain
        service.send(handle,'rendered\r')
        while time.monotonic()<deadline:
            view = service.view(handle)['text']
            if view is not None and 'ACK:rendered' in view.plain: break
            time.sleep(.05)
        assert 'ACK:rendered' in view.plain
    finally:
        terminals.close()
