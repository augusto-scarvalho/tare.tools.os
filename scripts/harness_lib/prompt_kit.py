"""Interactive prompt primitives for the harness CLI (SPEC-111).

Three interaction modes, resolved per prompt by `detect_mode` (R1):
- "tui": arrow-key menus rendered with ANSI on stderr.
- "numbered": plain-ASCII numbered lists read via input().
- "no-input": never prompts; defaults or HarnessError (R2).

All rendering goes to stderr (R4); stdout is never touched, so any harness
subcommand can adopt these primitives without breaking its JSON contract.
This module is side-effect free: it never writes files or env vars.
"""
from __future__ import annotations

import contextlib
import ctypes
import getpass
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

try:
    from .common import HarnessError
except ImportError:  # run directly for the __main__ self-check
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from harness_lib.common import HarnessError


@dataclass
class Option:
    value: str
    label: str = ""
    description: str = ""
    disabled: str = ""  # non-empty = reason this option cannot be picked (R7)

    def __post_init__(self) -> None:
        self.label = self.label or self.value


_VT_ENABLED: bool | None = None


def _enable_vt() -> bool:
    """Enable ANSI (VT) processing on the stderr console; False means no tui.

    ctypes SetConsoleMode instead of the os.system('') trick because only the
    former reports failure — and that report is what drives the R1 fallback.
    """
    global _VT_ENABLED
    if _VT_ENABLED is None:
        if os.name != "nt":
            _VT_ENABLED = True
        else:
            try:
                k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
                handle = k32.GetStdHandle(-12)  # STD_ERROR_HANDLE
                mode = ctypes.c_uint32()
                ok = k32.GetConsoleMode(handle, ctypes.byref(mode))
                _VT_ENABLED = bool(ok and k32.SetConsoleMode(handle, mode.value | 0x0004))
            except Exception:
                _VT_ENABLED = False
    return _VT_ENABLED


def _raw_keys_available() -> bool:
    try:
        if os.name == "nt":
            import msvcrt  # noqa: F401
        else:
            import termios  # noqa: F401
            import tty  # noqa: F401
        return True
    except ImportError:
        return False


def detect_mode(no_input: bool = False) -> str:
    """R1 mode ladder: no-input -> numbered -> tui."""
    if no_input or os.environ.get("HARNESS_NO_INPUT") == "1" or not sys.stdin.isatty():
        return "no-input"
    if (not sys.stderr.isatty() or os.environ.get("NO_COLOR")
            or os.environ.get("TERM") == "dumb"
            or not _raw_keys_available() or not _enable_vt()):
        return "numbered"
    return "tui"


def _glyphs() -> dict[str, str]:
    """Unicode set when stderr can actually encode it, else ASCII (R6)."""
    enc = getattr(sys.stderr, "encoding", None) or "ascii"
    try:
        "?❯◉◯·↑↓".encode(enc)
        return {"mark": "?", "cursor": "❯", "on": "◉", "off": "◯",
                "sep": " · ", "updown": "↑/↓"}
    except (UnicodeEncodeError, LookupError):
        return {"mark": "?", "cursor": ">", "on": "[x]", "off": "[ ]",
                "sep" : " | ", "updown": "up/down"}


def _err_write(text: str) -> None:
    sys.stderr.write(text)
    sys.stderr.flush()


def _err_line(text: str = "") -> None:
    _err_write(text + "\n")


def _input_on_stderr(prompt: str) -> str:
    """input() writes its prompt to stdout; keep prompts on stderr (R4)."""
    _err_write(prompt)
    return input()


# CSI sequences (what follows ESC) shared by POSIX raw mode and Windows
# VT-input mode; legacy Windows console prefixes live in _NT_LEGACY.
CSI_MAP = {"[A": "up", "[B": "down", "[C": "right", "[D": "left",
           "[Z": "shift-tab", "[H": "home", "[F": "end",
           "[1~": "home", "[7~": "home", "[4~": "end", "[8~": "end",
           "[3~": "delete"}
_NT_LEGACY = {"H": "up", "P": "down", "K": "left", "M": "right",
              "G": "home", "O": "end", "S": "delete"}
_CTRL = {"\r": "enter", "\n": "enter", " ": "space", "\x03": "ctrl-c",
         "\x04": "ctrl-d", "\x08": "backspace", "\x7f": "backspace", "\t": "tab"}


def _read_key() -> str:
    """One keypress: a logical key name, a printable char, or ''."""
    if os.name == "nt":
        import msvcrt
        ch = msvcrt.getwch()
        if ch in ("\xe0", "\x00"):
            return _NT_LEGACY.get(msvcrt.getwch(), "")
        if ch == "\x1b":
            # VT-input mode delivers CSI as chars; bare Esc has nothing pending
            if not msvcrt.kbhit():
                return "esc"
            seq = ""
            while msvcrt.kbhit() and len(seq) < 8:
                seq += msvcrt.getwch()
                if seq[-1:].isalpha() or seq[-1:] == "~":
                    break
            return CSI_MAP.get(seq, "")
        return _CTRL.get(ch, ch)
    import termios
    import tty
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        # raw (not cbreak) on purpose: '\x03' arrives as a char we handle at a
        # known point instead of KeyboardInterrupt firing mid-render (R5)
        tty.setraw(fd)
        ch = os.read(fd, 1).decode(errors="replace")
        if ch == "\x1b":
            import select
            if not select.select([fd], [], [], 0.03)[0]:
                return "esc"  # bare Esc, no CSI sequence following
            seq = ""
            while len(seq) < 8:
                seq += os.read(fd, 1).decode(errors="replace")
                if seq[-1] in "@ABCDEFGHIJKLMNOPQRSTUVWXYZ~abcdefghijklmnopqrstuvwxyz":
                    break
            return CSI_MAP.get(seq, "")
        return _CTRL.get(ch, ch)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


@contextlib.contextmanager
def _vt_input():
    """nt: enable ENABLE_VIRTUAL_TERMINAL_INPUT (0x0200) on stdin so keys arrive
    as CSI sequences (the only way Shift+Tab is distinguishable). Yields whether
    it took effect; always restores the original mode."""
    if os.name != "nt":
        yield True
        return
    handle = old_mode = None
    ok = False
    try:
        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = k32.GetStdHandle(-10)  # STD_INPUT_HANDLE
        mode = ctypes.c_uint32()
        if k32.GetConsoleMode(handle, ctypes.byref(mode)):
            old_mode = mode.value
            ok = bool(k32.SetConsoleMode(handle, old_mode | 0x0200))
    except Exception:
        ok = False
    try:
        yield ok
    finally:
        if handle is not None and old_mode is not None:
            try:
                ctypes.windll.kernel32.SetConsoleMode(handle, old_mode)  # type: ignore[attr-defined]
            except Exception:
                pass


def _apply_key(buf: str, pos: int, key: str) -> tuple[str, int, str]:
    """Pure line-editor reducer. action: '' | 'submit' | 'shift-tab' | 'ctrl-c' | 'eof'."""
    if key == "enter":
        return buf, pos, "submit"
    if key == "shift-tab":
        return buf, pos, "shift-tab"
    if key == "ctrl-c":
        return buf, pos, "ctrl-c"
    if key == "ctrl-d":
        return buf, pos, "eof" if not buf else ""
    if key == "backspace":
        return (buf[:pos - 1] + buf[pos:], pos - 1, "") if pos else (buf, pos, "")
    if key == "delete":
        return buf[:pos] + buf[pos + 1:], pos, ""
    if key == "left":
        return buf, max(pos - 1, 0), ""
    if key == "right":
        return buf, min(pos + 1, len(buf)), ""
    if key == "home":
        return buf, 0, ""
    if key == "end":
        return buf, len(buf), ""
    if key == "space":
        key = " "
    if len(key) == 1 and (key.isprintable() or key == " "):
        return buf[:pos] + key + buf[pos:], pos + 1, ""
    return buf, pos, ""  # up/down/esc/tab/unknown: ignored in the editor


def _drain_pending_chars() -> str:
    """Raw drain of input already buffered (a paste burst); '' when none."""
    out: list[str] = []
    try:
        if os.name == "nt":
            import msvcrt
            while msvcrt.kbhit():
                ch = msvcrt.getwch()
                out.append("\n" if ch == "\r" else ch)
        else:
            import select
            fd = sys.stdin.fileno()
            while select.select([fd], [], [], 0)[0]:
                data = os.read(fd, 4096).decode(errors="replace")
                if not data:
                    break
                out.append(data.replace("\r", "\n"))
    except Exception:
        return ""
    return "".join(c for c in "".join(out) if c == "\n" or c.isprintable())


def read_line(prompt: str, render: Callable[[str, int], None] | None = None,
              on_shift_tab: Callable[[], None] | None = None) -> tuple[str, str]:
    """Raw-mode line editor (SPEC-111 R22): returns ('line', text) or, when a
    paste burst containing newlines arrives, ('block', text). Raises
    KeyboardInterrupt on Ctrl-C and EOFError on Ctrl-D with an empty buffer.
    render(buf, pos) lets a HUD own the drawing."""

    def default_render(b: str, p: int) -> None:
        _err_write("\r\x1b[2K" + prompt + b)
        if len(b) - p:
            _err_write(f"\x1b[{len(b) - p}D")

    draw = render or default_render
    buf, pos = "", 0
    with _vt_input():
        draw(buf, pos)
        while True:
            key = _read_key()
            buf, pos, action = _apply_key(buf, pos, key)
            if action == "ctrl-c":
                _err_line()
                raise KeyboardInterrupt
            if action == "eof":
                _err_line()
                raise EOFError
            if action == "shift-tab":
                if on_shift_tab is not None:
                    on_shift_tab()
                draw(buf, pos)
                continue
            pending = _drain_pending_chars()
            if action == "submit":
                _err_line()
                if pending.strip():  # enter arrived inside a paste burst
                    return "block", (buf + "\n" + pending).rstrip("\n")
                return "line", buf
            if pending:
                buf = buf[:pos] + pending + buf[pos:]
                pos += len(pending)
                if "\n" in pending:  # multi-line paste => block (R19)
                    _err_line()
                    return "block", buf.rstrip("\n")
            draw(buf, pos)


def _tui_menu(title: str, options: list[Option], default: int,
              multi: bool, preselected: set[int]) -> int | set[int]:
    g = _glyphs()
    enabled = [i for i, o in enumerate(options) if not o.disabled]
    idx = default if default in enabled else enabled[0]
    checked = set(preselected)
    hints = [f"{g['updown']} move"]
    if multi:
        hints.append("space toggle")
    hints += ["enter select", "ctrl-c cancel"]
    total_lines = len(options) + 2  # title + rows + hint footer
    _err_write("\x1b[?25l")  # hide cursor
    try:
        drawn = False
        while True:
            if drawn:
                _err_write(f"\x1b[{total_lines}A")
            drawn = True
            _err_line(f"\x1b[K{g['mark']} {title}")
            for i, opt in enumerate(options):
                cursor = g["cursor"] if i == idx else " "
                mark = (g["on"] if i in checked else g["off"]) + " " if multi else ""
                desc = f" — {opt.description}" if opt.description else ""
                note = f"  [{opt.disabled}]" if opt.disabled else ""
                row = f"{cursor} {mark}{opt.label}{desc}{note}"
                if opt.disabled:
                    row = f"\x1b[2m{row}\x1b[0m"  # dim
                elif i == idx:
                    row = f"\x1b[36m{row}\x1b[0m"  # cyan highlight
                _err_line(f"\x1b[K{row}")
            _err_line(f"\x1b[K\x1b[2m{g['sep'].join(hints)}\x1b[0m")
            key = _read_key()
            if key == "ctrl-c":
                raise KeyboardInterrupt
            if key == "up":
                idx = enabled[(enabled.index(idx) - 1) % len(enabled)]
            elif key == "down":
                idx = enabled[(enabled.index(idx) + 1) % len(enabled)]
            elif key == "space" and multi:
                checked.symmetric_difference_update({idx})
            elif key == "enter":
                return checked if multi else idx
    finally:
        _err_write("\x1b[?25h")  # show cursor
        _err_line()


def _print_option_rows(options: list[Option], checked: set[int],
                       multi: bool) -> None:
    for i, opt in enumerate(options, 1):
        mark = ("[x] " if (i - 1) in checked else "[ ] ") if multi else ""
        desc = f" — {opt.description}" if opt.description else ""
        note = f"  [{opt.disabled}]" if opt.disabled else ""
        _err_line(f"  {i}) {mark}{opt.label}{desc}{note}")


def _numbered_select(title: str, options: list[Option], default: int) -> int:
    _err_line(title)
    _print_option_rows(options, set(), multi=False)
    while True:
        raw = _input_on_stderr(f"choice [{default + 1}]: ").strip() or str(default + 1)
        pick = None
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            pick = int(raw) - 1
        else:
            pick = next((i for i, o in enumerate(options)
                         if raw in (o.value, o.label)), None)
        if pick is None:
            _err_line("  pick a number from the list")
            continue
        if options[pick].disabled:
            _err_line(f"  {options[pick].label}: {options[pick].disabled}")
            continue
        return pick


def _numbered_multi(title: str, options: list[Option],
                    preselected: set[int]) -> set[int]:
    _err_line(title)
    _print_option_rows(options, preselected, multi=True)
    _err_line("  numbers comma-separated; 'a' = all; Enter = keep marked")
    while True:
        raw = _input_on_stderr("choice: ").strip().lower()
        if not raw:
            return set(preselected)
        if raw == "a":
            return {i for i, o in enumerate(options) if not o.disabled}
        picks: set[int] = set()
        bad = None
        for part in raw.replace(",", " ").split():
            if part.isdigit() and 1 <= int(part) <= len(options):
                i = int(part) - 1
                if options[i].disabled:
                    bad = f"{options[i].label}: {options[i].disabled}"
                    break
                picks.add(i)
            else:
                bad = f"'{part}' is not an option number"
                break
        if bad:
            _err_line(f"  {bad}")
            continue
        return picks


def _coerce(options: Iterable[Option | str]) -> list[Option]:
    coerced = [o if isinstance(o, Option) else Option(o) for o in options]
    if not coerced:
        raise HarnessError("select called with no options")
    return coerced


def select(title: str, options: Iterable[Option | str], default: int = 0,
           mode: str | None = None, flag_hint: str = "") -> str:
    opts = _coerce(options)
    mode = mode or detect_mode()
    if all(o.disabled for o in opts):
        reasons = "; ".join(f"{o.label}: {o.disabled}" for o in opts)
        raise HarnessError(f"no selectable option for '{title}'\ncause: {reasons}")
    if mode == "no-input":
        if not (0 <= default < len(opts)) or opts[default].disabled:
            raise HarnessError(
                f"cannot prompt for '{title}' in no-input mode and the default is unavailable"
                + (f"\nfix: pass {flag_hint}" if flag_hint else ""))
        return opts[default].value
    if mode == "tui":
        try:
            return opts[_tui_menu(title, opts, default, False, set())].value  # type: ignore[index]
        except (OSError, ValueError):  # console vanished mid-prompt; degrade
            pass
    return opts[_numbered_select(title, opts, default)].value


def multi_select(title: str, options: Iterable[Option | str],
                 preselected: Iterable[str] = (), mode: str | None = None) -> list[str]:
    opts = _coerce(options)
    mode = mode or detect_mode()
    pre = {i for i, o in enumerate(opts) if o.value in set(preselected) and not o.disabled}
    if mode == "no-input":
        return [opts[i].value for i in sorted(pre)]
    if mode == "tui":
        try:
            picked = _tui_menu(title, opts, 0, True, pre)
            return [opts[i].value for i in sorted(picked)]  # type: ignore[arg-type]
        except (OSError, ValueError):
            pass
    return [opts[i].value for i in sorted(_numbered_multi(title, opts, pre))]


def confirm(question: str, default: bool = False, danger: str | None = None,
            mode: str | None = None) -> bool:
    mode = mode or detect_mode()
    if danger:
        # R8 severe: typed word in every interactive mode, fail closed headless
        if mode == "no-input":
            raise HarnessError(
                f"'{question}' requires typing '{danger}' to confirm and cannot run with --no-input")
        _err_line(question)
        _err_line(f"  type '{danger}' to confirm; anything else cancels")
        return _input_on_stderr("confirm: ").strip() == danger
    if mode == "no-input":
        return default
    suffix = "[Y/n]" if default else "[y/N]"
    raw = _input_on_stderr(f"{question} {suffix} ").strip().lower()
    if not raw:
        return default
    return raw in {"y", "yes"}


def _fuzzy_score(query: str, cand: str) -> tuple[int, int] | None:
    """Subsequence match score (smaller = better): (span, first index).

    Every query char must appear in order; ties broken by candidate length
    at the call site. Empty query matches everything with the best score.
    """
    if not query:
        return (0, 0)
    pos = -1
    first = -1
    for ch in query:
        pos = cand.find(ch, pos + 1)
        if pos < 0:
            return None
        if first < 0:
            first = pos
    return (pos - first, first)


def _fuzzy_matches(query: str, candidates: Iterable[str], limit: int) -> list[str]:
    q = query.strip().lower()
    scored = []
    for cand in candidates:
        score = _fuzzy_score(q, cand.lower())
        if score is not None:
            scored.append((score, len(cand), cand))
    scored.sort()
    return [cand for _, _, cand in scored[:limit]]


def _tui_fuzzy(title: str, candidates: list[str], query: str, limit: int) -> str | None:
    g = _glyphs()
    total = limit + 2  # query line + fixed match rows + hint
    idx = 0
    _err_write("\x1b[?25l")
    try:
        drawn = False
        while True:
            matches = _fuzzy_matches(query, candidates, limit)
            idx = min(idx, max(len(matches) - 1, 0))
            if drawn:
                _err_write(f"\x1b[{total}A")
            drawn = True
            _err_line(f"\x1b[K{g['mark']} {title} > {query}")
            for i in range(limit):
                row = ""
                if i < len(matches):
                    row = f"{g['cursor'] if i == idx else ' '} {matches[i]}"
                    if i == idx:
                        row = f"\x1b[36m{row}\x1b[0m"
                _err_line(f"\x1b[K{row}")
            _err_line(f"\x1b[K\x1b[2mtype to filter{g['sep']}{g['updown']} move{g['sep']}"
                      f"enter select{g['sep']}esc skip\x1b[0m")
            key = _read_key()
            if key == "ctrl-c":
                raise KeyboardInterrupt
            if key == "esc":
                return None
            if key == "enter":
                return matches[idx] if matches else None
            if key == "up" and matches:
                idx = (idx - 1) % len(matches)
            elif key == "down" and matches:
                idx = (idx + 1) % len(matches)
            elif key == "backspace":
                query = query[:-1]
            elif key == "space":
                query += " "
            elif len(key) == 1 and key.isprintable():
                query += key
    finally:
        _err_write("\x1b[?25h")
        _err_line()


def fuzzy_select(title: str, candidates: Iterable[str], query: str = "",
                 mode: str | None = None, limit: int = 8) -> str | None:
    """Type-to-filter picker (SPEC-111 R21). Returns None when skipped/no match."""
    cands = list(candidates)
    mode = mode or detect_mode()
    if mode == "no-input" or not cands:
        return None
    if mode == "tui":
        try:
            return _tui_fuzzy(title, cands, query, limit)
        except (OSError, ValueError):
            pass
    q = text(f"{title} — search", default=query, mode="numbered")
    matches = _fuzzy_matches(q, cands, max(limit, 10))
    if not matches:
        _err_line("  no match")
        return None
    opts = [Option(m) for m in matches]
    opts.append(Option("", label="(skip — keep as typed)"))
    return select("pick a file", opts, default=0, mode="numbered") or None


def text(prompt: str, default: str | None = "", secret: bool = False,
         validate: Callable[[str], str | None] | None = None,
         mode: str | None = None, flag_hint: str = "") -> str:
    mode = mode or detect_mode()
    if mode == "no-input":
        if default is None:
            raise HarnessError(
                f"'{prompt}' is required and cannot be prompted in no-input mode"
                + (f"\nfix: pass {flag_hint}" if flag_hint else ""))
        return default
    shown = prompt + (f" [{default}]" if default else "") + ": "
    while True:
        if secret:
            val = getpass.getpass(shown).strip()  # never echoed (R13)
        else:
            val = _input_on_stderr(shown).strip()
        val = val or (default or "")
        if not val and default is None:
            _err_line("  a value is required")
            continue
        if validate is not None:
            problem = validate(val)
            if problem:
                _err_line(f"  {problem}")
                continue
        return val


def _self_check() -> None:  # ponytail: headless checks; raw-key/render is the manual checklist
    import builtins
    import contextlib

    class FakeStream:
        def __init__(self, tty: bool, encoding: str = "utf-8") -> None:
            self._tty = tty
            self.encoding = encoding
            self.buffer: list[str] = []

        def isatty(self) -> bool:
            return self._tty

        def write(self, text: str) -> int:
            self.buffer.append(text)
            return len(text)

        def flush(self) -> None:
            pass

    @contextlib.contextmanager
    def faked(stdin_tty: bool, stderr_tty: bool, env: dict[str, str]):
        global _VT_ENABLED
        old = sys.stdin, sys.stderr, dict(os.environ), _VT_ENABLED
        sys.stdin, sys.stderr = FakeStream(stdin_tty), FakeStream(stderr_tty)
        for k in ("NO_COLOR", "TERM", "HARNESS_NO_INPUT"):
            os.environ.pop(k, None)
        os.environ.update(env)
        _VT_ENABLED = True
        try:
            yield sys.stderr
        finally:
            sys.stdin, sys.stderr = old[0], old[1]
            os.environ.clear()
            os.environ.update(old[2])
            _VT_ENABLED = old[3]

    # R1 mode ladder
    with faked(False, True, {}):
        assert detect_mode() == "no-input"
    with faked(True, True, {"HARNESS_NO_INPUT": "1"}):
        assert detect_mode() == "no-input"
    with faked(True, False, {}):
        assert detect_mode() == "numbered"
    with faked(True, True, {"NO_COLOR": "1"}):
        assert detect_mode() == "numbered"
    with faked(True, True, {"TERM": "dumb"}):
        assert detect_mode() == "numbered"
    with faked(True, True, {}):
        assert detect_mode(no_input=True) == "no-input"
        assert detect_mode() == ("tui" if _raw_keys_available() else "numbered")

    # R6 glyph fallback off a real encode() probe
    with faked(True, True, {}) as err:
        err.encoding = "cp1252"
        assert _glyphs()["cursor"] == ">"
        err.encoding = "utf-8"
        assert _glyphs()["cursor"] == "❯"

    opts = [Option("a", description="first"), Option("b", disabled="missing"), Option("c")]

    def feed(answers: list[str]):
        it = iter(answers)
        builtins.input = lambda: next(it)

    real_input = builtins.input
    real_err = sys.stderr
    sys.stderr = FakeStream(False)
    try:
        # numbered select: default via Enter; disabled reprompts; value match
        feed([""])
        assert select("t", opts, mode="numbered") == "a"
        feed(["2", "3"])
        assert select("t", opts, mode="numbered") == "c"
        feed(["c"])
        assert select("t", opts, mode="numbered") == "c"
        # numbered multi: Enter keeps preselected; 'a' = all enabled; list parse
        feed([""])
        assert multi_select("t", opts, preselected=["c"], mode="numbered") == ["c"]
        feed(["a"])
        assert multi_select("t", opts, mode="numbered") == ["a", "c"]
        feed(["1,3"])
        assert multi_select("t", opts, mode="numbered") == ["a", "c"]
        feed(["2", ""])
        assert multi_select("t", opts, mode="numbered") == []
        # confirm ladder
        feed([""])
        assert confirm("q", default=True, mode="numbered") is True
        feed(["n"])
        assert confirm("q", default=True, mode="numbered") is False
        feed([".env"])
        assert confirm("q", danger=".env", mode="numbered") is True
        feed(["wrong"])
        assert confirm("q", danger=".env", mode="numbered") is False
        # text: default, required-reprompt, validate-reprompt
        feed([""])
        assert text("p", default="d", mode="numbered") == "d"
        feed(["", "v"])
        assert text("p", default=None, mode="numbered") == "v"
        feed(["bad", "ok"])
        assert text("p", validate=lambda v: None if v == "ok" else "nope",
                    mode="numbered") == "ok"
        # fuzzy matching + numbered fuzzy_select (R21)
        cands = ["scripts/harness.py", "scripts/harness_lib/chat_operator.py",
                 "README.md", "docs/HARNESS_ARCHITECTURE.md"]
        assert _fuzzy_matches("readme", cands, 5) == ["README.md"]
        assert _fuzzy_matches("zzzq", cands, 5) == []
        assert _fuzzy_matches("", cands, 2) == ["README.md", "scripts/harness.py"]  # shortest first
        assert fuzzy_select("f", cands, mode="no-input") is None
        assert fuzzy_select("f", [], mode="numbered") is None
        feed(["chatop", "1"])
        assert fuzzy_select("f", cands, mode="numbered") == "scripts/harness_lib/chat_operator.py"
        feed(["zzzq"])
        assert fuzzy_select("f", cands, mode="numbered") is None  # no match
        feed(["readme", "2"])
        assert fuzzy_select("f", cands, mode="numbered") is None  # (skip) row
        # R22 line-editor reducer table
        assert _apply_key("ab", 1, "x") == ("axb", 2, "")
        assert _apply_key("ab", 2, "backspace") == ("a", 1, "")
        assert _apply_key("ab", 0, "backspace") == ("ab", 0, "")
        assert _apply_key("ab", 0, "delete") == ("b", 0, "")
        assert _apply_key("ab", 2, "home") == ("ab", 0, "")
        assert _apply_key("ab", 0, "end") == ("ab", 2, "")
        assert _apply_key("ab", 1, "left") == ("ab", 0, "")
        assert _apply_key("ab", 1, "right") == ("ab", 2, "")
        assert _apply_key("ab", 1, "space") == ("a b", 2, "")
        assert _apply_key("ab", 1, "enter") == ("ab", 1, "submit")
        assert _apply_key("ab", 1, "shift-tab") == ("ab", 1, "shift-tab")
        assert _apply_key("", 0, "ctrl-d") == ("", 0, "eof")
        assert _apply_key("x", 1, "ctrl-d") == ("x", 1, "")
        assert _apply_key("ab", 1, "up") == ("ab", 1, "")
        # CSI table sanity
        assert CSI_MAP["[Z"] == "shift-tab" and CSI_MAP["[3~"] == "delete"
        assert CSI_MAP["[C"] == "right" and CSI_MAP["[1~"] == "home"
    finally:
        builtins.input = real_input
        sys.stderr = real_err

    # R2 no-input semantics
    assert select("t", opts, mode="no-input") == "a"
    assert multi_select("t", opts, preselected=["c"], mode="no-input") == ["c"]
    assert confirm("q", default=True, mode="no-input") is True
    assert text("p", default="d", mode="no-input") == "d"
    for call in (lambda: text("p", default=None, mode="no-input", flag_hint="--model"),
                 lambda: confirm("q", danger=".env", mode="no-input"),
                 lambda: select("t", [Option("x", disabled="gone")], mode="no-input")):
        try:
            call()
            raise AssertionError("expected HarnessError")
        except HarnessError:
            pass
    print("prompt_kit self-check ok")


if __name__ == "__main__":
    _self_check()
