"""Native multi-language syntax highlighting (tree-sitter) -> ANSI, on demand.

Rust/C-backed via ``tree-sitter`` + ``tree-sitter-language-pack`` (the language pack's
detection/queries run through a PyO3 Rust core). These are an **optional** dependency:
if the engines are absent, the output is not a TTY, or ``NO_COLOR`` is set, the text is
returned UNCHANGED — the harness core stays zero-dep and pipe-safe. Highlighting is
applied only to what is actually printed (on demand); redirected / JSON-consumed output
is byte-identical to the legacy path.

Design:
- ``color_enabled(stream)`` gates on ``HARNESS_COLOR`` (always/never/auto) -> ``NO_COLOR``
  -> ``stream.isatty()``, enabling Windows VT once so ANSI renders on conhost.
- ``detect_language(text, lang=, filename=)``: explicit hint -> path/extension -> content.
- ``highlight(text, ...)``: parse -> run the language's highlights query -> paint each
  byte with a 256-color style (overlap resolved: most-specific/highest-priority capture
  wins) -> emit SGR runs. Diff-shaped text gets structural +/- coloring instead.

Self-check: ``python scripts/harness_lib/highlight.py`` (runs with the engines if present,
else asserts the graceful fallback).
"""
from __future__ import annotations

import os
import sys
from typing import Any

# capture-name -> xterm-256 color. Dotted names fall back to their prefix
# (keyword.control -> keyword). A dark, VS Code-ish palette.
_PALETTE: dict[str, int] = {
    "keyword": 176, "operator": 250, "string": 173, "string.special": 173,
    "escape": 179, "comment": 245, "function": 186, "function.method": 186,
    "function.builtin": 186, "constructor": 79, "type": 79, "type.builtin": 79,
    "number": 151, "float": 151, "boolean": 75, "constant": 75,
    "constant.builtin": 75, "variable.parameter": 216, "variable.builtin": 75,
    "property": 117, "attribute": 186, "tag": 75, "label": 117, "punctuation": 245,
    "punctuation.bracket": 250, "punctuation.delimiter": 245, "punctuation.special": 179,
}
# tie-break when two captures cover the same span (higher wins).
_PRIORITY: dict[str, int] = {
    "comment": 9, "string": 8, "keyword": 7, "type": 6, "constructor": 6,
    "function": 5, "number": 5, "boolean": 5, "constant": 5, "attribute": 4,
    "property": 4, "variable.parameter": 4, "escape": 4, "tag": 4, "operator": 2,
    "punctuation": 1, "variable": 1, "label": 3,
}
_ALIASES = {"py": "python", "js": "javascript", "ts": "typescript", "tsx": "tsx",
            "rs": "rust", "rb": "ruby", "yml": "yaml", "sh": "bash", "shell": "bash",
            "c++": "cpp", "h": "c", "hpp": "cpp", "md": "markdown", "kt": "kotlin"}
_MAX_HIGHLIGHT_BYTES = 200_000  # on demand: skip oversized buffers (return plain)
_RESET = "\x1b[0m"

_engine_cache: Any = ...  # ... = not yet probed; None = unavailable; else the module
_vt_enabled = False


def _engine():
    global _engine_cache
    if _engine_cache is ...:
        try:
            try:
                from harness_lib.common import import_native_retry
            except ImportError:  # bare-script self-check mode (python harness_lib/highlight.py)
                from common import import_native_retry
            # [-1] = tree_sitter_language_pack; tree_sitter is also cached in sys.modules.
            # The retry absorbs a Windows SAC cold-cache block on a fresh .pyd (once/proc).
            _engine_cache = import_native_retry("tree_sitter", "tree_sitter_language_pack")[-1]
        except Exception:
            _engine_cache = None
    return _engine_cache


def _enable_windows_vt() -> None:
    global _vt_enabled
    if _vt_enabled or os.name != "nt":
        return
    _vt_enabled = True
    try:  # flip ENABLE_VIRTUAL_TERMINAL_PROCESSING so conhost renders SGR
        import ctypes
        k = ctypes.windll.kernel32  # type: ignore[attr-defined]
        h = k.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if k.GetConsoleMode(h, ctypes.byref(mode)):
            k.SetConsoleMode(h, mode.value | 0x0004)
    except Exception:
        pass


def color_enabled(stream: Any = None) -> bool:
    """True only when it is safe to emit ANSI: HARNESS_COLOR override, else a real TTY
    with NO_COLOR unset. Any redirect / pipe / capture returns False (byte-identical)."""
    mode = os.environ.get("HARNESS_COLOR", "").strip().lower()
    if mode == "always":
        _enable_windows_vt()
        return True
    if mode == "never":
        return False
    if os.environ.get("NO_COLOR"):
        return False
    stream = stream if stream is not None else sys.stdout
    try:
        if not stream.isatty():
            return False
    except Exception:
        return False
    _enable_windows_vt()
    return True


def detect_language(text: str, *, lang: str | None = None, filename: str | None = None) -> str | None:
    """Resolve a tree-sitter language name: explicit hint -> path/extension -> content."""
    tlp = _engine()
    if tlp is None:
        return None
    if lang:
        cand = _ALIASES.get(lang.lower(), lang.lower())
        try:
            if tlp.has_language(cand):
                return cand
        except Exception:
            pass
        try:
            d = tlp.detect_language_from_extension(lang.lstrip("."))
            if d:
                return d
        except Exception:
            pass
    if filename:
        try:
            d = tlp.detect_language_from_path(filename)
            if d:
                return d
        except Exception:
            pass
    try:
        d = tlp.detect_language_from_content(text)
        if d:
            return d
    except Exception:
        pass
    return None


def _style_for(name: str) -> tuple[int, int]:
    """(256-color, priority) for a capture name, walking dotted prefixes."""
    parts = name.split(".")
    color = None
    for i in range(len(parts), 0, -1):
        key = ".".join(parts[:i])
        if color is None and key in _PALETTE:
            color = _PALETTE[key]
    prio = 0
    for i in range(len(parts), 0, -1):
        key = ".".join(parts[:i])
        if key in _PRIORITY:
            prio = _PRIORITY[key]
            break
    return (color if color is not None else -1, prio)


def _paint(code: bytes, spans: list[tuple[int, int, str]]) -> str:
    """Overlay ANSI SGR runs. spans = (start_byte, end_byte, capture). Most-specific
    (smallest range, then highest priority) capture wins each byte; uncaptured = plain."""
    n = len(code)
    color = [-1] * n  # -1 = no color
    # paint largest-first so smaller (more specific) spans overwrite; equal length ->
    # lower priority first so the higher-priority capture is applied last and wins.
    for start, end, name in sorted(spans, key=lambda s: (-(s[1] - s[0]), _style_for(s[2])[1])):
        c, _prio = _style_for(name)
        if c < 0:
            continue
        for i in range(start, min(end, n)):
            color[i] = c
    out: list[str] = []
    i = 0
    while i < n:
        c = color[i]
        j = i
        while j < n and color[j] == c:
            j += 1
        chunk = code[i:j].decode("utf-8", "replace")
        out.append(f"\x1b[38;5;{c}m{chunk}{_RESET}" if c >= 0 else chunk)
        i = j
    return "".join(out)


def _looks_diff(s: str) -> bool:
    return (s.startswith("diff --git ") or "\ndiff --git " in s
            or "\n@@ " in s or s.startswith("@@ ")
            or ("\n--- " in s and "\n+++ " in s))


def _highlight_diff(text: str) -> str:
    out = []
    for line in text.split("\n"):
        if line.startswith(("diff --git ", "index ", "+++ ", "--- ")):
            out.append(f"\x1b[1m{line}{_RESET}")            # bold header
        elif line.startswith("@@ "):
            out.append(f"\x1b[38;5;75m{line}{_RESET}")       # cyan hunk
        elif line.startswith("+"):
            out.append(f"\x1b[38;5;151m{line}{_RESET}")      # green add
        elif line.startswith("-"):
            out.append(f"\x1b[38;5;210m{line}{_RESET}")      # red del
        else:
            out.append(line)
    return "\n".join(out)


def highlight(text: str, *, lang: str | None = None, filename: str | None = None,
              stream: Any = None, force: bool = False) -> str:
    """Return ``text`` with ANSI syntax colors, or unchanged when color is disabled,
    the engine is absent, the language is unknown, or the buffer is oversized."""
    if text is None:
        return ""
    text = str(text)
    if not force and not color_enabled(stream):
        return text
    if _looks_diff(text):
        return _highlight_diff(text)
    tlp = _engine()
    if tlp is None:
        return text
    code = text.encode("utf-8")
    if len(code) > _MAX_HIGHLIGHT_BYTES:
        return text
    language = detect_language(text, lang=lang, filename=filename)
    if not language:
        return text
    try:
        from tree_sitter import Query, QueryCursor
        parser = tlp.get_parser(language)
        ts_lang = tlp.get_language(language)
        tree = parser.parse(code)
        query = Query(ts_lang, tlp.get_highlights_query(language))
        caps = QueryCursor(query).captures(tree.root_node)
    except Exception:
        return text
    spans = [(node.start_byte, node.end_byte, name)
             for name, nodes in caps.items() for node in nodes]
    if not spans:
        return text
    return _paint(code, spans)


if __name__ == "__main__":  # ponytail: self-check — engine present colors; absent/gated = identity
    os.environ["HARNESS_COLOR"] = "always"
    py = "def foo(x):\n    return x + 1  # c\n"
    out = highlight(py, lang="python")
    diff = highlight("diff --git a/x b/x\n@@ -1 +1 @@\n-old\n+new\n")
    if _engine() is not None:
        assert "\x1b[38;5;" in out and "foo" in out, "python should be colored when engine present"
        assert "def" in out
    else:
        assert out == py, "no engine -> identity"
    assert "\x1b[38;5;151m+new" in diff and "\x1b[1mdiff --git" in diff, "diff structural coloring"
    os.environ["HARNESS_COLOR"] = "never"
    assert highlight(py, lang="python") == py, "HARNESS_COLOR=never -> identity"
    os.environ.pop("HARNESS_COLOR", None)
    os.environ["NO_COLOR"] = "1"
    assert highlight(py, lang="python", force=False) == py, "NO_COLOR honored (non-forced)"
    os.environ.pop("NO_COLOR", None)
    print(f"highlight self-check OK (engine={'present' if _engine() else 'absent'})")
