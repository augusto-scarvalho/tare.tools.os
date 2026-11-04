"""Keep one VT parser/query responder alive per PTY, even with its view closed."""
from collections import OrderedDict
from threading import Event, RLock, Thread
from uuid import uuid4

from .terminal_display import TerminalDisplay


class TerminalMonitor:
    def __init__(self, sessions):
        self.sessions = sessions
        self.lock, self.closed = RLock(), Event()
        self.owners, self.records = set(), OrderedDict()
        self.finished = set()
        self.thread = Thread(target=self._run,name='tare-terminal-display',daemon=True)
        self.thread.start()

    def add(self, owner):
        with self.lock: self.owners.add(owner)

    def close(self):
        self.closed.set()
        self.thread.join(timeout=2)
        if self.thread.is_alive(): raise RuntimeError('terminal_display_shutdown_incomplete')

    def _run(self):
        while not self.closed.wait(.08):
            with self.lock: owners = tuple(self.owners)
            for owner in owners:
                try:
                    rows = self.sessions.list(owner=owner,include_completed=True)
                except (ValueError,OSError):
                    continue  # Host closure/removal cannot retarget another owner.
                for row in rows:
                    if row['mode'] == 'pty' and (owner,row['handle']) not in self.finished:
                        try:
                            self._observe(owner,row)
                        except Exception as exc:
                            # One malformed stream must not silence all query
                            # responders. Retain a visible error without payloads.
                            with self.lock:
                                record = self.records.get((owner,row['handle']))
                                if record is not None:
                                    record['issue'] = 'Terminal preview failed: '+type(exc).__name__

    def _observe(self, owner, row):
        key = owner,row['handle']
        with self.lock:
            record = self.records.get(key)
            if record is None:
                # Running commands are capped at 16 by the Kernel host. Retain a
                # bounded set of completed screens without evicting a live parser.
                if len(self.records) >= 32:
                    old = next((k for k in self.records if k in self.finished),None)
                    if old is None: return
                    del self.records[old]
                record = self.records[key] = dict(display=None,cursor=0,issue='',sequence=0)
            output = self.sessions.read(owner=owner,handle=row['handle'],
                                       cursors={'stdout':record['cursor'],'stderr':0},limit=16384)
            columns, rows = output['columns'],output['rows']
            if columns*rows > 65536:
                record['issue'] = 'Terminal too large for preview; use Resize.'
                return
            if record['display'] is None:
                record['display'] = TerminalDisplay(columns,rows)
            display = record['display']
            if (display.active.columns,display.active.lines) != (columns,rows): record['sequence'] += 1
            display.resize(columns,rows)
            page = output['output']['stdout']
            record['cursor'] = page['cursor']
            replies = display.feed(page['text'],truncated=page['truncated'])
            if page['text']: record['sequence'] += 1
            if display.query_overflow:
                record['issue'] = 'Program queries exceeded the terminal response limit.'
            for reply in replies:
                result = self.sessions.send(owner=owner,handle=row['handle'],
                                            request_id='display:'+uuid4().hex,text=reply)
                if result['state'] in {'unknown','not_written'}:
                    record['issue'] = 'Response to the program unconfirmed; no retry.'
            if output['cleanup_verified'] and not page['more']:
                self.finished.add(key)

    def view(self, owner, handle, *, cursor=False):
        with self.lock:
            record = self.records.get((owner,handle))
            if record is None:
                return {'text':None,'issue':'Waiting for terminal; older screens may have been discarded.','sequence':0}
            display = record['display']
            issue = record['issue']
            if display is not None and display.lost_output:
                issue = 'Some output was lost; the screen restarted from the available output.'
            cache_key = record['sequence'],cursor
            if display is not None and record.get('cache_key') != cache_key:
                record['rendered'] = display.render(cursor=cursor)
                record['cache_key'] = cache_key
            return {'text':record['rendered'].copy() if display else None,
                    'issue':issue,'sequence':record['sequence']}

    def input(self, owner, handle, *, key=None, character=None, paste=None):
        with self.lock:
            record = self.records.get((owner,handle))
            if record is None or record['display'] is None:
                raise ValueError('Terminal is not ready for keyboard input yet.')
            display = record['display']
            return display.paste(paste) if paste is not None else display.key(key,character)
