"""Bounded VT rendering owned by the conversation host, independent of its UI."""
from collections import deque
import unicodedata

import pyte
from rich.style import Style
from rich.text import Text


class EscapeFilter:
    """Drop string controls (including clipboard/title commands) across chunks.

    CSI and short escapes reach the emulator only, never the operator terminal.
    Unterminated strings retain constant memory; numeric CSI fields are bounded.
    """
    def __init__(self):
        self.state, self.pending, self.combining = 'text', '', 0

    def feed(self, text):
        out = []
        for c in text:
            if self.state == 'string':
                if c in '\x07\x9c': self.state = 'text'
                elif c == '\x1b': self.state = 'string_escape'
            elif self.state == 'string_escape':
                self.state = 'text' if c == '\\' else 'string'
            elif self.state == 'escape':
                if c == '[': self.state, self.pending = 'csi', '\x1b['
                elif c in ']P_X^': self.state = 'string'
                elif c in '()*+#%': self.state, self.pending = 'short', '\x1b'+c
                else:
                    if c in '78DEM=>c': out.append('\x1b'+c)
                    self.state = 'text'
            elif self.state == 'short':
                out.append(self.pending+c); self.pending = ''; self.state = 'text'
            elif self.state == 'csi':
                if '@' <= c <= '~':
                    if len(self.pending) <= 128:
                        fields = self.pending[2:].lstrip('?=>').replace(':',';').split(';')
                        if all(not f.isdigit() or len(f) <= 4 for f in fields):
                            out.append(self.pending+c)
                    self.pending = ''; self.state = 'text'
                elif c == '\x1b': self.pending = ''; self.state = 'escape'
                elif len(self.pending) <= 128: self.pending += c
            elif c == '\x1b': self.state = 'escape'
            elif c in '\x90\x98\x9d\x9e\x9f': self.state = 'string'
            elif c == '\x9b': self.state, self.pending = 'csi','\x1b['
            elif c in '\r\n\t\b\x0b\x0c' or ord(c) >= 32 and not 127 <= ord(c) < 160:
                self.combining = self.combining+1 if unicodedata.combining(c) else 0
                if self.combining <= 8: out.append(c)
        return ''.join(out)


class _Screen(pyte.Screen):
    def __init__(self, columns, lines, reply):
        self.reply = reply
        super().__init__(columns,lines)

    def write_process_input(self, data):
        self.reply(data)


class _History(pyte.HistoryScreen):
    def __init__(self, columns, lines, reply):
        self.reply = reply
        super().__init__(columns,lines,history=300)

    def write_process_input(self, data):
        self.reply(data)


class TerminalDisplay:
    """Primary/history and alternate screens, with one parser per process."""
    def __init__(self, columns=80, rows=24):
        self.replies = deque(maxlen=32)
        self.query_overflow = False
        self.primary = _History(columns,rows,self.reply)
        self.alternate = _Screen(columns,rows,self.reply)
        self.active = self.primary
        self.application_cursor = False
        self.bracketed_paste = False
        self.filter = EscapeFilter()
        self.stream = pyte.Stream(self)
        self.lost_output = False

    def reply(self, data):
        if len(self.replies) == self.replies.maxlen: self.query_overflow = True
        else: self.replies.append(data)

    def __getattr__(self, name):
        if name in pyte.Stream.events:
            return lambda *args, **kwargs: getattr(self.active,name)(*args,**kwargs)
        raise AttributeError(name)

    def set_mode(self, *modes, **kwargs):
        if kwargs.get('private'):
            if 1 in modes: self.application_cursor = True
            if 2004 in modes: self.bracketed_paste = True
            if any(m in modes for m in (47,1047,1049)) and self.active is self.primary:
                self.primary.save_cursor()
                self.alternate.reset()
                self.active = self.alternate
        self.active.set_mode(*(m for m in modes if m not in (47,1047,1049,2004)),**kwargs)

    def reset_mode(self, *modes, **kwargs):
        self.active.reset_mode(*(m for m in modes if m not in (47,1047,1049,2004)),**kwargs)
        if kwargs.get('private'):
            if 1 in modes: self.application_cursor = False
            if 2004 in modes: self.bracketed_paste = False
            if any(m in modes for m in (47,1047,1049)) and self.active is self.alternate:
                self.active = self.primary
                self.primary.restore_cursor()

    def reset(self):
        self.primary.reset(); self.alternate.reset()
        self.active = self.primary
        self.application_cursor = self.bracketed_paste = False

    def resize(self, columns, rows):
        if not 1 <= columns <= 1000 or not 1 <= rows <= 1000 or columns*rows > 65536:
            raise ValueError('Terminal too large; resize to at most 65,536 cells.')
        for screen in (self.primary,self.alternate):
            screen.resize(columns=columns,lines=rows)

    def feed(self, text, *, truncated=False):
        if truncated:
            self.reset(); self.filter = EscapeFilter(); self.lost_output = True
        self.stream.feed(self.filter.feed(text))
        replies = list(self.replies); self.replies.clear()
        return replies

    def key(self, key, character=None):
        arrows = {'up':'A','down':'B','right':'C','left':'D','home':'H','end':'F'}
        if key in arrows:
            return ('\x1bO' if self.application_cursor else '\x1b[')+arrows[key]
        if key.startswith('ctrl+') and key[5:] in arrows:
            return '\x1b[1;5'+arrows[key[5:]]
        common = {'enter':'\r','backspace':'\x7f','tab':'\t','escape':'\x1b',
                  'delete':'\x1b[3~','insert':'\x1b[2~','pageup':'\x1b[5~','pagedown':'\x1b[6~'}
        if key in common: return common[key]
        function = {'f1':'OP','f2':'OQ','f3':'OR','f4':'OS','f5':'[15~','f6':'[17~',
                    'f7':'[18~','f8':'[19~','f9':'[20~','f10':'[21~','f11':'[23~','f12':'[24~'}
        if key in function: return '\x1b'+function[key]
        if key == 'ctrl+space': return '\0'
        if key.startswith('ctrl+') and len(key) == 6 and 'a' <= key[-1] <= 'z':
            return chr(ord(key[-1])-96)
        if key.startswith('alt+') and len(key) == 5: return '\x1b'+key[-1]
        return character or ''

    def paste(self, text):
        return '\x1b[200~'+text+'\x1b[201~' if self.bracketed_paste else text

    def render(self, *, cursor=False):
        screen = self.active
        rows = list(self.primary.history.top) if screen is self.primary else []
        history = len(rows)
        rows.extend(screen.buffer[y] for y in range(screen.lines))
        result = Text(no_wrap=True,overflow='crop')
        for y, line in enumerate(rows):
            previous, run = None, []
            for x in range(screen.columns):
                char = line[x]
                if not char.data: continue  # Wide-character continuation cell.
                fg, bg = _color(char.fg), _color(char.bg)
                style = Style(color=fg,bgcolor=bg,bold=char.bold,italic=char.italics,
                              underline=char.underscore,strike=char.strikethrough,
                              reverse=char.reverse ^ bool(cursor and not screen.cursor.hidden
                                  and y == history+screen.cursor.y and x == screen.cursor.x))
                if previous is not None and style != previous:
                    result.append(''.join(run),previous); run = []
                run.append(char.data); previous = style
            if run: result.append(''.join(run),previous)
            if y != len(rows)-1: result.append('\n')
        return result


def _color(value):
    if value == 'default': return None
    if len(value) == 6 and all(c in '0123456789abcdefABCDEF' for c in value): return '#'+value
    # pyte uses brown for the ANSI yellow slot.
    return {'brown':'yellow','brightblack':'bright_black','brightred':'bright_red',
            'brightgreen':'bright_green','brightbrown':'bright_yellow','brightblue':'bright_blue',
            'brightmagenta':'bright_magenta','bfightmagenta':'bright_magenta',
            'brightcyan':'bright_cyan','brightwhite':'bright_white'}.get(value,value)
