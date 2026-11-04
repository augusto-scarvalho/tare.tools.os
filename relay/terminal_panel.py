"""Conversation-bound process screen; hiding it leaves its commands running."""
import asyncio

from rich.text import Text
from textual import on, work
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Input, Select, Static

from .console_output import plain
from .console_panels import SupportPanel


def _state(value):
    return 'unconfirmed' if value == 'unknown' else value


class TerminalCanvas(Static, can_focus=True):
    def on_key(self, event):
        if self.screen.keyboard and event.key not in {'ctrl+g','f4'}:
            event.stop(); event.prevent_default()
            self.screen.queue_key(event.key,event.character)

    def on_paste(self, event):
        if self.screen.keyboard:
            event.stop(); event.prevent_default()
            self.screen.queue_paste(event.text)


class TerminalPanel(SupportPanel):
    BINDINGS = [Binding('f4','close','Conversation',priority=True),
                Binding('ctrl+g','release','Release direct keyboard',priority=True),
                ('escape','close','Close panel')]
    DEFAULT_CSS = '''
    #terminal-process { margin-bottom: 1; }
    #terminal-state, #terminal-notice { height: auto; max-height: 2; color: $text-muted; }
    #terminal-notice.attention, #terminal-state.attention { color: $warning; }
    #terminal-scroll { height: 1fr; margin: 1 0; background: $panel; scrollbar-size: 1 1; }
    #terminal-canvas { width: auto; min-width: 100%; height: auto; min-height: 100%; }
    #terminal-canvas:focus { border-left: thick $accent; }
    #terminal-buttons { height: 1; grid-size: 6; grid-rows: 1; margin-top: 1; }
    #terminal-buttons Button { min-width: 0; width: 1fr; }
    TerminalPanel.narrow #terminal-buttons { height: 2; grid-size: 3; }
    #terminal-entry { margin-top: 1; }
    '''

    def __init__(self, service, *, title):
        super().__init__()
        self.service, self.title_text = service, title
        self.handle, self.rows, self.options = None, {}, ()
        self.cursors, self.pipe_text = {}, ''
        self.keyboard, self.polling, self.closing = False, False, False
        self.input_queue = asyncio.Queue(maxsize=64)
        self.pending_bytes = 0
        self.input_active = False
        self.operation_lock = asyncio.Lock()

    def compose(self):
        with Vertical(id='terminal-panel', classes='support-body'):
            with Horizontal(classes='panel-header'):
                yield Static('Terminal · '+plain(self.title_text),id='terminal-title',classes='panel-title',markup=False)
                yield Button('Back · F4',id='terminal-close')
            yield Select([],prompt='No processes in this conversation',id='terminal-process')
            yield Static('Waiting for processes…',id='terminal-state',markup=False)
            with VerticalScroll(id='terminal-scroll'):
                yield TerminalCanvas('',id='terminal-canvas',markup=False)
            yield Static('Closing this panel keeps commands running.',id='terminal-notice',markup=False)
            yield Input(placeholder='Program input · Enter sends one line',id='terminal-entry',disabled=True)
            with Grid(id='terminal-buttons'):
                yield Button('Send',id='terminal-send',classes='panel-primary',disabled=True)
                yield Button('Keyboard',id='terminal-keyboard',disabled=True)
                yield Button('Ctrl+C',id='terminal-interrupt',disabled=True)
                yield Button('EOF',id='terminal-eof',disabled=True)
                yield Button('Resize',id='terminal-fit',disabled=True)
                yield Button('Stop',id='terminal-stop',classes='panel-danger',disabled=True)

    def on_mount(self):
        self.poll()
        self.set_interval(.25,self.poll)
        self.send_queued()

    @work(group='terminal-close',exclusive=True)
    async def action_close(self):
        if self.pending_bytes or self.input_active:
            self.notice('Waiting for pending input before returning to the conversation…')
            await self.input_queue.join()
        self.closing = True
        self.dismiss()

    def action_release(self):
        self.keyboard = False
        self.query_one('#terminal-entry',Input).focus()
        self.notice('Direct keyboard released. Commands are still running.')

    def notice(self, text, *, error=False):
        if not self.closing:
            widget = self.query_one('#terminal-notice',Static)
            widget.update(plain(text))
            widget.set_class(error, 'attention')

    @on(Select.Changed,'#terminal-process')
    def select_process(self, event):
        event.stop()
        handle = event.value if isinstance(event.value,str) else None
        if handle != self.handle:
            self.handle, self.cursors, self.pipe_text, self.keyboard = handle, {}, '', False
            self.query_one('#terminal-canvas',TerminalCanvas).update('')

    @work(group='terminal-poll')
    async def poll(self):
        if self.polling or self.closing: return
        self.polling = True
        try:
            rows = await asyncio.to_thread(self.service.list)
            if self.closing: return
            self.rows = {r['handle']:r for r in rows}
            if not rows:
                self.handle, self.keyboard = None, False
                self.query_one('#terminal-state',Static).update('No processes in this conversation.')
                self.query_one('#terminal-canvas',TerminalCanvas).update('Commands started during this conversation will appear here.')
                self.query_one('#terminal-entry',Input).disabled = True
                for button in self.query('#terminal-buttons Button'): button.disabled = True
            options = tuple((plain(r['command'])[:65]+' · '+_state(r['status']),r['handle']) for r in rows)
            if options != self.options:
                self.options = options
                selected = self.query_one('#terminal-process',Select)
                selected.set_options([(Text(label,no_wrap=True,overflow='ellipsis'),handle) for label,handle in options])
                if rows: selected.value = self.handle if self.handle in self.rows else rows[-1]['handle']
            handle = self.handle
            if handle is None: return
            output = await asyncio.to_thread(self.service.read,handle,self.cursors)
            if self.closing or handle != self.handle: return
            running = output['status'] == 'running'
            pty = output['mode'] == 'pty'
            state = _state(output['status'])+' · '+str(round(output['elapsed_ms']/1000,1))+'s'
            if output['exit_code'] is not None: state += ' · exit '+str(output['exit_code'])
            if output['cleanup_verified']: state += ' · cleanup verified'
            self.query_one('#terminal-state',Static).update(state)
            self.query_one('#terminal-state').set_class(output['status'] == 'unknown' or output['exit_code'] not in (None, 0), 'attention')
            for button in self.query('#terminal-buttons Button'): button.disabled = not running
            self.query_one('#terminal-fit',Button).disabled = not running or not pty
            self.query_one('#terminal-keyboard',Button).disabled = not running or not pty
            self.query_one('#terminal-interrupt',Button).disabled = not running or not pty
            self.query_one('#terminal-eof',Button).disabled = not running or pty
            self.query_one('#terminal-entry',Input).disabled = not running
            scroll = self.query_one('#terminal-scroll',VerticalScroll)
            follow = scroll.is_vertical_scroll_end
            canvas = self.query_one('#terminal-canvas',TerminalCanvas)
            if pty:
                view = await asyncio.to_thread(self.service.view,handle,cursor=self.keyboard)
                if self.closing or handle != self.handle: return
                if view['text'] is not None: canvas.update(view['text'])
                if view['issue']: self.notice(view['issue'], error=True)
            else:
                for stream, page in output['output'].items():
                    if page['truncated']: self.pipe_text += '\n[Some earlier output was discarded.]\n'
                    if page['text']:
                        self.pipe_text += ('\n[stderr]\n' if stream == 'stderr' else '')+plain(page['text'])
                    self.cursors[stream] = page['cursor']
                self.pipe_text = self.pipe_text[-65536:]
                canvas.update(Text(self.pipe_text,no_wrap=True))
            if follow: scroll.call_after_refresh(lambda: scroll.scroll_end(animate=False))
        except (ValueError,OSError,RuntimeError) as exc:
            self.notice(str(exc), error=True)
        finally:
            self.polling = False

    @on(Input.Submitted,'#terminal-entry')
    def submit_line(self, event):
        event.stop(); self.send_line()

    def send_line(self):
        field = self.query_one('#terminal-entry',Input)
        if self.handle:
            suffix = '\r' if self.rows.get(self.handle,{}).get('mode') == 'pty' else '\n'
            if self.enqueue(field.value+suffix): field.value = ''

    def enqueue(self, text):
        size = len(text.encode('utf-8'))
        if not self.handle or not text: return False
        if self.input_queue.full() or self.pending_bytes+size > 65536:
            self.notice('Pending input is full; this input was not queued.', error=True)
            return False
        self.input_queue.put_nowait((self.handle,text))
        self.pending_bytes += size
        return True

    def queue_key(self, key, character):
        try: self.enqueue(self.service.input(self.handle,key=key,character=character))
        except ValueError as exc: self.notice(str(exc), error=True)

    def queue_paste(self, text):
        if len(text.encode('utf-8')) > 60000:
            self.notice('Paste exceeds 60 KB and was not sent.', error=True); return
        try: self.enqueue(self.service.input(self.handle,paste=text))
        except ValueError as exc: self.notice(str(exc), error=True)

    @work(group='terminal-input')
    async def send_queued(self):
        while not self.closing:
            handle, text = await self.input_queue.get()
            self.pending_bytes -= len(text.encode('utf-8'))
            self.input_active = True
            try:
                async with self.operation_lock:
                    result = await asyncio.to_thread(self.service.send,handle,text)
                self.notice('Input: '+result['state']+' · this does not confirm the program consumed it.')
            except (ValueError,OSError,RuntimeError) as exc:
                self.notice('Input unconfirmed; no retry. '+str(exc), error=True)
            finally:
                self.input_active = False
                self.input_queue.task_done()

    @on(Button.Pressed)
    def button(self, event):
        event.stop()
        action = event.button.id
        if action == 'terminal-close': self.action_close()
        elif action == 'terminal-send': self.send_line()
        elif action == 'terminal-keyboard':
            self.keyboard = True
            self.query_one('#terminal-canvas',TerminalCanvas).focus()
            self.notice('Keys go to the program. Ctrl+G releases the keyboard; F4 returns to the conversation.')
        elif action == 'terminal-interrupt': self.enqueue('\x03')
        elif action in {'terminal-stop','terminal-eof','terminal-fit'}: self.operate(action)

    @work(group='terminal-operation')
    async def operate(self, action):
        handle = self.handle
        if handle is None: return
        try:
            if action == 'terminal-eof': await self.input_queue.join()
            async with self.operation_lock:
                if action == 'terminal-stop':
                    result = await asyncio.to_thread(self.service.stop,handle)
                    self.notice('Process cleanup verified.' if result['cleanup_verified'] else 'Process cleanup unconfirmed.')
                elif action == 'terminal-eof':
                    await asyncio.to_thread(self.service.eof,handle)
                    self.notice('EOF sent after queued input.')
                else:
                    size = self.query_one('#terminal-scroll').content_size
                    await asyncio.to_thread(self.service.resize,handle,max(1,min(size.width-2,300)),max(1,min(size.height,100)))
                    self.notice('Terminal size updated.')
        except (ValueError,OSError,RuntimeError) as exc:
            self.notice(str(exc), error=True)
