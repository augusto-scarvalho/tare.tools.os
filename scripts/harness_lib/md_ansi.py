"""Minimal Markdown -> ANSI renderer for the chat CLI (panel-chat QoL P6).

Only what a chat reply needs: fenced code (routed through the shipped
tree-sitter highlighter), ATX headings, bullet lists, inline `code`, and
**bold**. Pipe-safe by contract: when color is disabled (the piped panel
bridge, NO_COLOR, or any redirect) the text is returned BYTE-IDENTICAL, so the
transcript the browser reads never sees an escape byte.

Stdlib only. `python scripts/harness_lib/md_ansi.py` runs the self-check.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

try:
    from . import highlight
except ImportError:  # run directly for the __main__ self-check
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from harness_lib import highlight

_BOLD = "\x1b[1m"
_DIM = "\x1b[2m"
_CYAN = "\x1b[36m"
_RESET = "\x1b[0m"

_HEADING = re.compile(r"^#{1,6}\s+(.*)$")
_LIST = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_FENCE = re.compile(r"^\s*(?:`{3,}|~{3,})\s*([\w+.#-]*)\s*$")
_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_BOLD_SPAN = re.compile(r"\*\*(.+?)\*\*")


def _bold_only(text: str) -> str:
    return _BOLD_SPAN.sub(lambda m: f"{_BOLD}{m.group(1)}{_RESET}", text)


def _inline(text: str) -> str:
    """`code` -> cyan, **bold** -> bold. Code spans win first so ** inside a
    span stays literal."""
    out: list[str] = []
    pos = 0
    for m in _CODE_SPAN.finditer(text):
        out.append(_bold_only(text[pos:m.start()]))
        out.append(f"{_CYAN}{m.group(1)}{_RESET}")
        pos = m.end()
    out.append(_bold_only(text[pos:]))
    return "".join(out)


def _render_code(lang: str, code_lines: list[str]) -> str:
    body = highlight.highlight("\n".join(code_lines), lang=lang or None, force=True)
    return "\n".join(f"{_DIM}│{_RESET} {ln}" for ln in body.split("\n"))


def render(text: str, stream: Any = None) -> str:
    """Markdown chat reply -> ANSI, or the text UNCHANGED when color is disabled."""
    if text is None:
        return ""
    text = str(text)
    if not highlight.color_enabled(stream):
        return text  # piped panel bridge must never see ANSI
    out: list[str] = []
    code: list[str] | None = None  # None = not in a fence; list = collecting code
    lang = ""
    for line in text.split("\n"):
        fence = _FENCE.match(line)
        if code is not None:
            if fence and not fence.group(1):  # a bare fence closes the block
                out.append(_render_code(lang, code))
                code, lang = None, ""
            else:
                code.append(line)
            continue
        if fence:
            code, lang = [], fence.group(1)
            continue
        head = _HEADING.match(line)
        if head:
            out.append(f"{_BOLD}{head.group(1)}{_RESET}")
            continue
        item = _LIST.match(line)
        if item:
            out.append(f"{item.group(1)}• {_inline(item.group(2))}")
            continue
        out.append(_inline(line))
    if code is not None:  # unterminated fence: flush what we captured
        out.append(_render_code(lang, code))
    return "\n".join(out)


if __name__ == "__main__":  # ponytail: self-check — forced color renders, disabled is identity
    import os
    os.environ["HARNESS_COLOR"] = "always"
    h = render("# Title")
    assert h.startswith(_BOLD) and "Title" in h, h                       # heading bold
    assert _CYAN in render("use `x` now"), "inline code -> cyan"
    assert _BOLD in render("this **matters** ok"), "**bold** -> bold"
    assert "• item" in render("- item"), "list marker -> bullet"
    fenced = render("```python\ndef f():\n    return 1\n```")
    # "def"/"return" survive highlighting as whole runs; "def f" would be split by SGR
    assert "│" in fenced and "def" in fenced and "return" in fenced, fenced  # dim gutter + body
    if highlight._engine() is not None:
        assert "\x1b[38;5;" in fenced, "engine present must color the fence body"
    os.environ["HARNESS_COLOR"] = "never"
    src = "# Title\n- a\n`code` **b**\n```py\nx = 1\n```"
    assert render(src) == src, "disabled must be byte-identical passthrough"
    os.environ.pop("HARNESS_COLOR", None)
    print("md_ansi self-check ok")
