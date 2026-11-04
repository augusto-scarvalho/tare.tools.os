from textual import events
from textual._xterm_parser import XTermParser
from relay.console_windows_input import ConsoleInputText,unicode_windows_driver
import pytest


@pytest.mark.parametrize('modifiers,char,key', [(0,'\r','enter'),(16,'\r','shift+enter'),
    (8,'\n','ctrl+enter'),(2,'\r','alt+enter'),(24,'\n','ctrl+shift+enter')])
def test_windows_return_keeps_modifiers_for_composer(modifiers,char,key):
    parser=XTermParser()
    events_read=list(parser.feed(ConsoleInputText().feed([(True,char,13,modifiers)])))
    assert [event.key for event in events_read]==[key]


def test_windows_paste_return_is_not_a_modified_key():
    assert ConsoleInputText().feed([(True,'\r',0,0)])=='\r'


def test_conpty_alt_release_surrogates_survive_modifiers_and_batch_boundary():
    text=ConsoleInputText()
    assert text.feed([(True,'é',0,0),(False,'é',0,0),
        (True,'\0',18,2),(True,'\0',102,2),(False,'\ud83d',18,0)])=='é'
    assert text.feed([(True,'\0',18,2),(True,'\0',99,2)])==''
    assert text.feed([(False,'\ude42',18,0),(True,'F',0,0),(False,'F',70,16)])=='🙂F'


def test_unicode_packet_key_up_does_not_duplicate_surrogate_units():
    text=ConsoleInputText()
    assert text.feed([(True,'\ud83d',231,0),(False,'\ud83d',231,0)])==''
    assert text.feed([(True,'\ude42',231,0),(False,'\ude42',231,0)])=='🙂'
    assert text.feed([(False,'é',18,0)])=='é'


def test_unicode_composition_survives_bracketed_paste_and_control_keys():
    text=ConsoleInputText();parser=XTermParser()
    prefix=[(True,c,0,0) for c in '\x1b[200~x']
    first=list(parser.feed(text.feed(prefix+[(False,'\ud83d',18,0)])))
    last=[(False,'\ude42',18,0)]+[(True,c,0,0) for c in 'y\x1b[201~']
    tokens=first+list(parser.feed(text.feed(last)))
    assert [e.text for e in tokens if isinstance(e,events.Paste)]==['x🙂y']
    assert text.feed([(True,'\0',32,8),(True,'\x03',67,8)])=='\0\x03'


def test_invalid_surrogate_does_not_crash_or_erase_following_text():
    text=ConsoleInputText()
    assert text.feed([(False,'\ud83d',18,0)])==''
    assert text.feed([(True,'X',0,0)])=='\ufffdX'


def test_explicit_or_non_windows_driver_is_preserved():
    class CustomDriver:pass
    assert unicode_windows_driver(CustomDriver) is CustomDriver


def test_queued_split_sequences_are_consumed_before_escape_timeout(monkeypatch):
    """A busy input batch must not turn paste, mouse or Ctrl+Enter into text."""
    import ctypes
    import sys
    from types import SimpleNamespace
    from textual import _parser, drivers

    class Character(ctypes.Structure):
        _fields_ = [('UnicodeChar', ctypes.c_wchar)]
    class Key(ctypes.Structure):
        _fields_ = [('bKeyDown', ctypes.c_int), ('uChar', Character),
                    ('wVirtualKeyCode', ctypes.c_uint), ('dwControlKeyState', ctypes.c_uint)]
    class Event(ctypes.Structure):
        _fields_ = [('KeyEvent', Key)]
    class Record(ctypes.Structure):
        _fields_ = [('EventType', ctypes.c_uint), ('Event', Event)]

    pasted = 'Queued text, preserved literally.\n' * 20
    batches = ['\x1b', '[200~'+pasted, '\x1b', '[201~', '\x1b', '[13;5u',
               '\x1b', '[<0;4;5M', '\x1b']
    clock, idle, output, errors = [0.0], [False], [], []
    monkeypatch.setattr(_parser, 'get_time', lambda: clock[0])
    class Stop:
        def is_set(self):
            clock[0] += 1.0  # Simulated scheduling delay; the next batch is already queued.
            return not batches and idle[0]
    class Monitor:
        def __init__(self):
            self.exit_event = Stop()
            self.app = SimpleNamespace(log=SimpleNamespace(error=errors.append))
            self.process_event = output.append
        def on_size_change(self, *size):
            raise AssertionError('No resize in this fixture')
    def wait(handles, timeout):
        if batches:return 0
        idle[0] = True
        return None
    def read(handle, records, limit, count):
        batch = batches.pop(0)
        for index, character in enumerate(batch):
            record = records._obj[index]
            record.EventType = 1
            record.Event.KeyEvent.bKeyDown = 1
            record.Event.KeyEvent.uChar.UnicodeChar = character
        count._obj.value = len(batch)
        return True
    win32 = SimpleNamespace(EventMonitor=Monitor, INPUT_RECORD=Record,
        wintypes=SimpleNamespace(DWORD=ctypes.c_uint), STD_INPUT_HANDLE=-10,
        GetStdHandle=lambda _: 0, wait_for_handles=wait,
        KERNEL32=SimpleNamespace(ReadConsoleInputW=read))
    monkeypatch.setattr(drivers, 'win32', win32, raising=False)
    monkeypatch.setitem(sys.modules, 'textual.drivers.win32', win32)
    class Driver:
        def start_application_mode(self):
            win32.EventMonitor().run()
    Driver.__module__ = 'textual.drivers.windows_driver'
    with monkeypatch.context() as platform:
        platform.setattr(sys, 'platform', 'win32')
        Wrapped = unicode_windows_driver(Driver)
    Wrapped().start_application_mode()
    assert not errors
    assert [e.text for e in output if isinstance(e, events.Paste)] == [pasted]
    assert [e.key for e in output if isinstance(e, events.Key)] == ['ctrl+enter', 'escape']
    assert len([e for e in output if isinstance(e, events.MouseDown)]) == 1
