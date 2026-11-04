"""Preserve Windows Unicode composition in Textual's existing console driver."""
import codecs
import sys


class ConsoleInputText:
    """Join UTF-16 units across input batches, including Alt-release composition."""
    def __init__(self):
        self.decoder=codecs.getincrementaldecoder('utf-16-le')('replace')

    def feed(self,keys):
        text=[]
        for down,char,virtual_key,modifiers in keys:
            # ConPTY emits supplementary characters on VK_MENU key-up; ordinary
            # key-up duplicates and modifier/numpad placeholders are not text.
            if not down and not (virtual_key==18 and char!='\0'):
                continue
            if down and modifiers and virtual_key==0:
                continue
            if char=='\0' and not modifiers & 12:  # Keep explicit Ctrl+Space/NUL.
                continue
            if down and virtual_key==13:
                # The Windows monitor otherwise drops Return's modifiers. Use
                # the parser's existing CSI-u support; pasted text (VK=0) stays literal.
                modifier=1+bool(modifiers & 16)+2*bool(modifiers & 3)+4*bool(modifiers & 12)
                char=f'\x1b[13;{modifier}u' if modifier!=1 else '\r'
            text.append(char)
        return self.decoder.decode(''.join(text).encode('utf-16-le','surrogatepass'))


def unicode_windows_driver(base):
    """Use the platform driver unchanged except for its Windows input monitor."""
    if sys.platform!='win32' or base.__module__!='textual.drivers.windows_driver':
        return base
    import ctypes
    from textual import constants
    from textual._xterm_parser import XTermParser
    from textual.drivers import win32

    class UnicodeMonitor(win32.EventMonitor):
        def run(self):
            parser=XTermParser(debug=constants.DEBUG)
            decoder=ConsoleInputText()
            records=(win32.INPUT_RECORD*1024)()
            count=win32.wintypes.DWORD()
            handle=win32.GetStdHandle(win32.STD_INPUT_HANDLE)
            try:
                while not self.exit_event.is_set():
                    # An escape may be split at the console's record boundary.
                    # Consume queued input before applying an idle timeout;
                    # scheduling delay is not a user's standalone Escape key.
                    if win32.wait_for_handles([handle],100) is None:
                        for event in parser.tick():self.process_event(event)
                        continue
                    if not win32.KERNEL32.ReadConsoleInputW(handle,ctypes.byref(records),1024,ctypes.byref(count)):
                        raise ctypes.WinError(ctypes.get_last_error())
                    keys=[];size=None
                    for record in records[:count.value]:
                        if record.EventType==1:
                            key=record.Event.KeyEvent
                            keys.append((bool(key.bKeyDown),key.uChar.UnicodeChar,key.wVirtualKeyCode,key.dwControlKeyState))
                        elif record.EventType==4:
                            value=record.Event.WindowBufferSizeEvent.dwSize
                            size=(value.X,value.Y)
                    text=decoder.feed(keys)
                    if text:
                        for event in parser.feed(text):self.process_event(event)
                    if size is not None:self.on_size_change(*size)
            except Exception as error:
                self.app.log.error('Windows console input failed',error)

    class UnicodeWindowsDriver(base):
        def start_application_mode(self):
            # Textual exposes a driver hook but no monitor factory. Substitute
            # only during this synchronous construction, then restore it; the
            # existing driver still owns rendering, console modes and cleanup.
            original=win32.EventMonitor
            try:
                win32.EventMonitor=UnicodeMonitor
                super().start_application_mode()
            finally:
                win32.EventMonitor=original
    return UnicodeWindowsDriver
