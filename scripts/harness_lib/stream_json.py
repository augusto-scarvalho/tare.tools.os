"""Parse a claude `--output-format stream-json` NDJSON transcript (SPEC-118 v3).

The raw NDJSON stdout log is the SINGLE source of truth; this parser derives a
COMPACT summary (final text, observed usage, activity counts) — it never stores a
second copy of the event stream. Tolerant by contract: torn/garbage lines are
skipped, a missing `result` event falls back to concatenated assistant text, and
absent usage yields None.

Stdlib only. `python stream_json.py` runs the self-check.
"""
from __future__ import annotations

import difflib
import json
import os
import sys
from typing import Any


def _first_nonempty(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def is_stream_json(text: str) -> bool:
    """Detection: first non-empty line is a JSON object carrying a "type" key."""
    line = _first_nonempty(text)
    if not line:
        return False
    try:
        obj = json.loads(line)
    except (ValueError, TypeError):
        return False
    return isinstance(obj, dict) and "type" in obj


def parse(stdout_text: str) -> dict[str, Any]:
    stdout_text = stdout_text or ""
    result: dict[str, Any] = {
        "isStreamJson": False,
        "finalText": "",
        "usage": None,
        "activity": {"toolCalls": 0, "assistantTurns": 0, "model": None, "durationMs": None},
        "errorText": "",
        "finalResultLine": "",
    }
    if not is_stream_json(stdout_text):
        return result
    result["isStreamJson"] = True

    assistant_text: list[str] = []
    error_bits: list[str] = []
    tool_calls = 0
    assistant_turns = 0
    model: str | None = None
    result_event: dict[str, Any] | None = None
    result_line = ""

    for raw in stdout_text.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except (ValueError, TypeError):
            continue  # torn/garbage line — skip, stay tolerant
        if not isinstance(ev, dict):
            continue
        etype = ev.get("type")
        if etype == "assistant":
            assistant_turns += 1
            msg = ev.get("message") or {}
            if not model and isinstance(msg.get("model"), str):
                model = msg.get("model")
            for block in msg.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    tool_calls += 1
                elif block.get("type") == "text" and block.get("text"):
                    assistant_text.append(str(block["text"]))
        elif etype == "system" and not model and isinstance(ev.get("model"), str):
            model = ev.get("model")
        elif etype == "result":
            result_event = ev
            result_line = line
        elif etype == "rate_limit_event":
            info = ev.get("rate_limit_info") or {}
            status = str(info.get("status") or "")
            if status and status != "allowed":
                error_bits.append("rate limit " + status)
        elif isinstance(etype, str) and "error" in etype:
            error_bits.append(json.dumps(ev)[:500])

    if result_event is not None:
        result["finalResultLine"] = result_line
        final = result_event.get("result")
        result["finalText"] = str(final) if isinstance(final, str) else ""
        result["activity"]["durationMs"] = result_event.get("duration_ms")
        if result_event.get("is_error"):
            error_bits.append(str(result_event.get("result") or ""))
            error_bits.append(str(result_event.get("subtype") or ""))
        usage = result_event.get("usage")
        cost = result_event.get("total_cost_usd")
        if isinstance(usage, dict):
            in_tok = (int(usage.get("input_tokens") or 0)
                      + int(usage.get("cache_creation_input_tokens") or 0)
                      + int(usage.get("cache_read_input_tokens") or 0))
            result["usage"] = {
                "inputTokens": in_tok,
                "outputTokens": int(usage.get("output_tokens") or 0),
                "costUsd": float(cost) if isinstance(cost, (int, float)) else None,
            }
    if not result["finalText"]:  # fallback: no result event / empty result field
        result["finalText"] = "\n".join(assistant_text)
    result["activity"]["toolCalls"] = tool_calls
    result["activity"]["assistantTurns"] = assistant_turns
    result["activity"]["model"] = model
    result["errorText"] = "\n".join(b for b in error_bits if b)
    return result


DIGEST_CAP = 2000  # chat-tool-chips: inspector payloads stay bounded
DIFF_CAP = 6000    # numbered-diff chips get more room than a generic digest
DIFF_ROW_CAP = 200


def _digest(value: Any, cap: int = DIGEST_CAP) -> str:
    """Compact, capped text for the chip inspector — honest [truncated] marker."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False,
                                                           separators=(",", ":"))
    return text if len(text) <= cap else text[:cap] + "…[truncated]"


def numbered_diff(old_lines: list[str], new_lines: list[str],
                  start: int = 1, context: int | None = None) -> tuple[str, int, int]:
    """claude-CLI-style numbered diff over two line lists -> (text, added,
    removed). Context rows keep the (new-)file line number, '-' rows the old
    number, '+' rows the new one — an in-place edit interleaves `11 -`/`11 +`
    exactly like the CLI. `start` is the file line the snippet anchors at.
    `context=N` collapses long equal runs to N rows around each change with a
    `⋮` separator (full-file diffs: Write-update, codex file_change); None
    keeps every context row (Edit snippets are already hunk-sized)."""
    sm = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    width = len(str(start + max(len(old_lines), len(new_lines), 1)))
    rows: list[str] = []
    added = removed = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            ks = list(range(i2 - i1))
            if context is not None and len(ks) > 2 * context + 1:
                ks = ks[:context] + [None] + ks[-context:]  # None -> `⋮` separator
            for k in ks:
                rows.append(f"{'⋮':>{width}}" if k is None
                            else f"{start + j1 + k:>{width}}   {old_lines[i1 + k]}")
            continue
        for k in range(i1, i2):
            rows.append(f"{start + k:>{width}} - {old_lines[k]}")
            removed += 1
        for k in range(j1, j2):
            rows.append(f"{start + k:>{width}} + {new_lines[k]}")
            added += 1
    if len(rows) > DIFF_ROW_CAP:
        rows = rows[:DIFF_ROW_CAP] + ["…[truncated]"]
    return "\n".join(rows), added, removed


def _edit_start(fp: Any, old: str, new: str) -> int:
    """File line where an Edit anchors: find old_string (chip renders pre-apply)
    or new_string (post-hoc replay, the edit already landed); 1 when unknown."""
    try:
        with open(fp, encoding="utf-8", errors="ignore") as fh:
            text = fh.read()
    except (OSError, TypeError):
        return 1
    for needle in (old, new):
        idx = text.find(needle) if needle else -1
        if idx >= 0:
            return text.count("\n", 0, idx) + 1
    return 1


def _lang_of(path: Any) -> str:
    """Lowercased file extension (no dot) as a highlight-language hint, or ''."""
    ext = os.path.splitext(str(path))[1] if isinstance(path, str) and path else ""
    return ext[1:].lower()


def _attach_render_fields(name: str, inp: dict[str, Any], desc: dict[str, Any]) -> None:
    """chat-chip-render payloads (stdlib only), so the front highlights instead of
    dumping raw JSON: Edit -> a unified `diff` the page colorizes; Write/NotebookEdit
    -> `code` + `language`; Read -> `language` for its tool_result digest (the front
    matches result→chip by id, so the language rides the chip). input_digest is left
    untouched for back-compat."""
    fp = inp.get("file_path") or inp.get("path") or inp.get("notebook_path")
    lang = _lang_of(fp)
    if name == "Edit" and isinstance(inp.get("old_string"), str) and isinstance(inp.get("new_string"), str):
        diff = "\n".join(difflib.unified_diff(
            inp["old_string"].splitlines(), inp["new_string"].splitlines(),
            fromfile=str(fp) if fp else "before", tofile=str(fp) if fp else "after",
            lineterm=""))
        if diff:
            desc["diff"] = _digest(diff)
        # chip-diff-v2 (owner feedback 2026-07-17): claude-CLI-style body — real
        # file line numbers + added/removed counts alongside the unified diff.
        pretty, added, removed = numbered_diff(
            inp["old_string"].splitlines(), inp["new_string"].splitlines(),
            _edit_start(fp, inp["old_string"], inp["new_string"]))
        if pretty:
            desc["diffPretty"] = _digest(pretty, DIFF_CAP)
            desc["diffAdded"], desc["diffRemoved"] = added, removed
        # SPEC-147 inv 5: the UNCAPPED pair for the workspace diff pane. The panel
        # bridge strips this into its per-session ring before SSE — chips stay capped.
        desc["diffFull"] = {"file": str(fp) if fp else "",
                            "old": inp["old_string"], "new": inp["new_string"]}
        if lang:
            desc["language"] = lang
    elif name in ("Write", "NotebookEdit"):
        content = inp.get("content") if isinstance(inp.get("content"), str) else inp.get("new_source")
        if isinstance(content, str):
            desc["code"] = _digest(content)
            if lang:
                desc["language"] = lang
            # chip-diff-v2: a Write over an EXISTING file is an update — render it
            # as a numbered diff vs the current content (a brand-new file keeps the
            # highlighted-code render, counts only).
            if name == "Write":
                try:
                    with open(fp, encoding="utf-8", errors="ignore") as fh:
                        old_text = fh.read()
                except (OSError, TypeError):
                    old_text = None
                new_lines = content.splitlines()
                if old_text is not None and old_text != content:
                    pretty, added, removed = numbered_diff(old_text.splitlines(), new_lines,
                                                           context=3)
                    desc["diffPretty"] = _digest(pretty, DIFF_CAP)
                    desc["diffAdded"], desc["diffRemoved"] = added, removed
                elif old_text is None:
                    desc["diffAdded"], desc["diffRemoved"] = len(new_lines), 0
                # SPEC-147 inv 5: uncapped pair (new file => old ""), same ring strip.
                desc["diffFull"] = {"file": str(fp) if fp else "",
                                    "old": old_text or "", "new": content}
    elif name == "Read" and lang:
        desc["language"] = lang


def tool_descriptor(block: dict[str, Any]) -> dict[str, Any]:
    """Compact {name, arg, id, input_digest} from a claude `tool_use` block —
    same primary-arg precedence the panel's fmtStreamLine uses
    (file_path/path/command/pattern/url). Shared so the chat backend and the
    worker-drawer front agree on what to show. `id`/`input_digest` power the
    chat-tool-chips inspector; {name, arg} stay untouched for older callers.

    AskUserQuestion (panel-chat QoL P8) additionally carries the raw structured
    `input` so the chat backend can derive a `question` event; every other tool
    stays compact."""
    inp = block.get("input") if isinstance(block.get("input"), dict) else {}
    name = str(block.get("name") or "?")
    arg = ""
    for k in ("file_path", "path", "command", "pattern", "url"):
        val = inp.get(k)
        if val:
            arg = str(val)
            break
    # a-task-call-carries-its-own-words: Task's input is description, prompt and
    # subagent_type; none match the precedence above, so a real Task rendered a
    # bare "Running Task" with an empty arg. `description` is the spawning agent's
    # OWN WORDS, already on the wire inside input_digest -- surface it, copy never
    # interpret (no prompt/subagent_type fallback, no derived category). Fallback
    # only: a standard key above still wins, and this touches `arg` alone
    # (input_digest, below, is unchanged).
    if not arg and name == "Task" and isinstance(inp.get("description"), str) and inp["description"]:
        arg = inp["description"]
    desc: dict[str, Any] = {"name": name, "arg": arg,
                            "id": str(block.get("id") or ""),
                            "input_digest": _digest(inp) if inp else ""}
    if name == "AskUserQuestion":
        desc["input"] = inp
    _attach_render_fields(name, inp, desc)
    return desc


def plan_steps(block: dict[str, Any]) -> list[dict[str, str]]:
    """Normalize a TodoWrite tool_use block into `[{text, status}]` (statuses
    pending/in_progress/completed) — the chat-plan-hud vocabulary. Tolerant:
    a malformed todos list yields []."""
    inp = block.get("input") if isinstance(block.get("input"), dict) else {}
    steps: list[dict[str, str]] = []
    for todo in inp.get("todos") or []:
        if not isinstance(todo, dict):
            continue
        text = str(todo.get("content") or todo.get("activeForm") or "").strip()
        if text:
            steps.append({"text": text, "status": str(todo.get("status") or "pending")})
    return steps


def tool_result_descriptor(block: dict[str, Any]) -> dict[str, Any]:
    """Compact {id, digest} from a `tool_result` block (user events). The
    content may be a plain string or a list of text blocks — both reduce to
    one capped text digest. A failed call adds `isError` (absent on success, so
    successful results stay byte-identical for old callers)."""
    content = block.get("content")
    if isinstance(content, list):
        text = "\n".join(str(b.get("text") or "") for b in content
                         if isinstance(b, dict))
    else:
        text = str(content or "")
    res = {"id": str(block.get("tool_use_id") or ""), "digest": _digest(text)}
    if block.get("is_error") is True:  # chip-error-color
        res["isError"] = True
    return res


def item_failed(item: dict[str, Any]) -> bool:
    """chip-error-color: did this codex tool item fail? Explicit `failed` status,
    or a non-zero exit code — snake `exit_code` from `codex exec --json`, camel
    `exitCode` from the app-server transport. Items with neither (fileChange,
    web_search) are never errors."""
    if str(item.get("status") or "").lower() == "failed":
        return True
    return item.get("exit_code", item.get("exitCode")) not in (None, 0)


def parse_chat_stream(lines: Any, on_tool: Any = None,
                      on_tool_result: Any = None,
                      on_plan: Any = None,
                      on_text: Any = None,
                      on_usage: Any = None) -> dict[str, Any]:
    """Streaming-friendly parse for the chat ClaudeEngine (SPEC-114 v13 verbose chat).

    Consumes NDJSON `lines` (ANY iterable — a live `Popen.stdout` or a canned list),
    firing `on_tool({name,arg})` per `tool_use` block IN ORDER so per-tool activity
    can surface MID-turn, and returns `{text, session_id, result, last_ctx_tokens}`
    (`last_ctx_tokens` = the LAST assistant message's full input) where `result` is
    the raw terminal `result` event (fed straight to `ClaudeEngine._telemetry`, so the
    rich claude telemetry is preserved). Tolerant by contract: garbage/torn/blank
    lines are skipped; finalText prefers the result event's `result`, else the
    concatenated assistant text. Never a second copy of the stream is stored."""
    text_parts: list[str] = []
    session_id: str | None = None
    result_event: dict[str, Any] | None = None
    last_ctx_tokens: int | None = None
    for raw in lines:
        line = raw.strip() if isinstance(raw, str) else ""
        if not line:
            continue
        try:
            ev = json.loads(line)
        except (ValueError, TypeError):
            continue  # torn/garbage line — skip, stay tolerant
        if not isinstance(ev, dict):
            continue
        if isinstance(ev.get("session_id"), str):
            session_id = ev["session_id"]  # carried on every event; last wins (same session)
        etype = ev.get("type")
        if etype == "assistant":
            # chat-status-line: each assistant message carries its API usage —
            # fire it so the HUD can show live turn tokens (claude-CLI parity).
            usage = (ev.get("message") or {}).get("usage")
            if isinstance(usage, dict):
                # context size = ONE API round's full input; the result event's
                # usage SUMS every round, so it overshoots the window (ctx >100%)
                # on tool-heavy turns — last assistant message wins.
                last_ctx_tokens = (int(usage.get("input_tokens") or 0)
                                   + int(usage.get("cache_read_input_tokens") or 0)
                                   + int(usage.get("cache_creation_input_tokens") or 0))
            if on_usage and isinstance(usage, dict):
                on_usage({"outputTokens": int(usage.get("output_tokens") or 0),
                          "inputTokens": int(usage.get("input_tokens") or 0),
                          "cacheReadTokens": int(usage.get("cache_read_input_tokens") or 0)})
            for block in (ev.get("message") or {}).get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    if on_tool:
                        on_tool(tool_descriptor(block))
                    # chat-plan-hud: TodoWrite IS the live plan — normalize and
                    # fire alongside the tool event (last update wins downstream).
                    if on_plan and block.get("name") == "TodoWrite":
                        steps = plan_steps(block)
                        if steps:
                            on_plan(steps)
                elif block.get("type") == "text" and block.get("text"):
                    # room-chat fix: mid-turn assistant text used to surface only
                    # as the end-of-turn fallback — a long room turn narrates for
                    # minutes between tool calls and all of it vanished.
                    text_parts.append(str(block["text"]))
                    if on_text:
                        on_text(str(block["text"]))
        elif etype == "user" and on_tool_result:
            # chat-tool-chips: tool RESULTS ride `user` events as tool_result
            # blocks — fire a capped digest per block so the chip inspector
            # can show output without a second copy of the stream.
            for block in (ev.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    on_tool_result(tool_result_descriptor(block))
        elif etype == "result":
            result_event = ev
    text = ""
    if result_event is not None and isinstance(result_event.get("result"), str):
        text = result_event["result"].strip()
    if not text:
        text = "\n".join(text_parts).strip()
    return {"text": text, "session_id": session_id, "result": result_event,
            "last_ctx_tokens": last_ctx_tokens}


def _codex_tool_descriptor(item: dict[str, Any]) -> dict[str, Any] | None:
    """Map one codex `item` (exec_events.rs ThreadItemDetails) to the SAME
    descriptor shape tool_descriptor emits for claude — the vendor-neutral chip
    contract: {name, arg, id, input_digest[, language]}. None for item types with
    no tool shape (agent_message/reasoning/todo_list/error handle elsewhere)."""
    itype, iid = item.get("type"), str(item.get("id") or "")
    if itype == "command_execution":
        cmd = str(item.get("command") or "")
        return {"name": "Bash", "arg": cmd, "id": iid, "input_digest": _digest(cmd)}
    if itype == "file_change":
        changes = [c for c in item.get("changes") or [] if isinstance(c, dict)]
        if not changes:
            return None
        first = str(changes[0].get("path") or "")
        arg = first + (f" (+{len(changes) - 1} more)" if len(changes) > 1 else "")
        # kind add -> Write, update/delete -> Edit: the panel's chip icons/previews
        # key off these names. NO diff content — the codex stream carries only
        # {path, kind} (FileChangeItem), an honest ceiling documented in PANEL_CHAT.
        name = "Write" if str(changes[0].get("kind")) == "add" else "Edit"
        digest = "\n".join(f"{c.get('kind')} {c.get('path')}" for c in changes)
        desc = {"name": name, "arg": arg, "id": iid, "input_digest": _digest(digest),
                # M1 diff derivation: the engine snapshots these paths at item.started
                # and derives a unified diff at item.completed (the codex stream itself
                # carries no diff content). Internal field — the chat event layer only
                # forwards diff/code/language, so this never reaches the panel.
                "changes": [{"path": str(c.get("path") or ""), "kind": str(c.get("kind") or "")}
                            for c in changes]}
        lang = _lang_of(first)
        if lang:
            desc["language"] = lang
        return desc
    if itype == "web_search":
        return {"name": "WebSearch", "arg": str(item.get("query") or ""), "id": iid,
                "input_digest": _digest(str(item.get("query") or ""))}
    if itype == "mcp_tool_call":
        name = ".".join(str(item.get(k)) for k in ("server", "tool") if item.get(k)) or "mcp"
        return {"name": name, "arg": "", "id": iid, "input_digest": ""}
    return None


def _codex_result_digest(item: dict[str, Any]) -> str:
    """Compact result text for a completed codex item (chip output section)."""
    if item.get("type") == "command_execution":
        out = str(item.get("aggregated_output") or "")
        code = item.get("exit_code")
        if code not in (None, 0):
            out = (out + f"\n[exit code {code}]").strip()
        return _digest(out or f"({item.get('status') or 'completed'})")
    if item.get("type") == "file_change":
        lines = [f"{c.get('kind')} {c.get('path')}" for c in item.get("changes") or []
                 if isinstance(c, dict)]
        return _digest("\n".join([*lines, str(item.get("status") or "")]).strip())
    return _digest(str(item.get("status") or "completed"))


def parse_codex_stream(lines: Any, on_tool: Any = None,
                       on_tool_result: Any = None,
                       on_plan: Any = None,
                       on_text: Any = None) -> dict[str, Any]:
    """Streaming parse for `codex exec --json` JSONL (schema: exec_events.rs) —
    the CodexEngine twin of parse_chat_stream, firing the SAME callbacks so the
    chat surface (chips, plan HUD, mid-turn text) is vendor-neutral. A future
    engine only needs a parser with this contract: map its stream onto
    on_tool/on_tool_result/on_plan/on_text and return {text, session_id, result}.

    Mapping: agent_message -> on_text (+ final text join); command_execution /
    file_change / web_search / mcp_tool_call -> on_tool at first sight (started or
    completed — some items only complete) and on_tool_result at completed;
    todo_list -> on_plan (completed|pending); reasoning dropped (parity: claude
    thinking is not surfaced either); turn.completed -> raw `result` event;
    turn.failed / error events -> formatted `errors`. Tolerant by contract:
    garbage lines skipped; nothing parsed at all -> raw stdout becomes the text
    (the old _parse fallback, kept for non-JSONL output)."""
    text_parts: list[str] = []
    errors: list[str] = []
    session_id: str | None = None
    result_event: dict[str, Any] | None = None
    seen_tools: set[str] = set()
    raw_lines: list[str] = []
    parsed_any = False
    for raw in lines:
        line = raw.strip() if isinstance(raw, str) else ""
        if not line:
            continue
        try:
            ev = json.loads(line)
        except (ValueError, TypeError):
            if len(raw_lines) < 50:
                raw_lines.append(line)  # non-JSONL fallback tail, bounded
            continue
        if not isinstance(ev, dict):
            continue
        parsed_any = True
        etype = ev.get("type")
        if etype == "thread.started":
            session_id = str(ev.get("thread_id") or "") or session_id
        elif etype in ("item.started", "item.updated", "item.completed"):
            item = ev.get("item") or {}
            itype = item.get("type")
            iid = str(item.get("id") or "")
            if itype == "agent_message":
                if etype == "item.completed" and item.get("text"):
                    text_parts.append(str(item["text"]))
                    if on_text:
                        on_text(str(item["text"]))
            elif itype == "error":
                errors.append(f"[codex: {item.get('message')}]")
            elif itype == "todo_list":
                if on_plan:
                    steps = [{"text": str(t.get("text") or "").strip(),
                              "status": "completed" if t.get("completed") else "pending"}
                             for t in item.get("items") or [] if isinstance(t, dict)]
                    steps = [s for s in steps if s["text"]]
                    if steps:
                        on_plan(steps)
            else:
                desc = _codex_tool_descriptor(item)
                if desc is not None:
                    if iid not in seen_tools:  # fire once, at first sight
                        seen_tools.add(iid)
                        if on_tool:
                            on_tool(desc)
                    if etype == "item.completed" and on_tool_result:
                        res = {"id": iid, "digest": _codex_result_digest(item)}
                        if item_failed(item):  # chip-error-color
                            res["isError"] = True
                        on_tool_result(res)
        elif etype == "turn.completed":
            result_event = ev
        elif etype == "turn.failed":
            err = (ev.get("error") or {}) if isinstance(ev.get("error"), dict) else {}
            errors.append(f"[codex turn failed: {err.get('message') or ev}]")
        elif etype == "error":
            errors.append(f"[codex: {ev.get('message')}]")
    text = "\n\n".join(t for t in text_parts if t)
    if not text and not parsed_any and raw_lines:
        text = "\n".join(raw_lines).strip()  # old _parse raw-stdout fallback
    return {"text": text, "session_id": session_id, "result": result_event,
            "errors": errors}


def _self_check() -> None:
    # A canned transcript with escaped WORKER_RESULT inside the result event's
    # text — substring-scanning the raw NDJSON cannot recover it (escaped quotes),
    # so extraction MUST route through the parsed finalText.
    wr = {"workflowId": "WF-x", "workerId": "w0", "status": "done"}
    result_text = "Analysis.\n```json\n" + json.dumps(wr) + "\n```\n"
    lines = [
        {"type": "system", "subtype": "init", "model": "claude-fixture"},
        {"type": "assistant", "message": {"model": "claude-fixture",
            "content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "README.md"}}]}},
        {"type": "assistant", "message": {"model": "claude-fixture",
            "content": [{"type": "text", "text": "done"}]}},
        {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}},
        {"type": "result", "subtype": "success", "is_error": False, "duration_ms": 1234,
            "result": result_text, "total_cost_usd": 0.0123,
            "usage": {"input_tokens": 111, "output_tokens": 22,
                      "cache_creation_input_tokens": 50, "cache_read_input_tokens": 0}},
    ]
    transcript = "\n".join(json.dumps(x) for x in lines)
    p = parse(transcript)
    assert p["isStreamJson"] is True
    assert p["usage"] == {"inputTokens": 161, "outputTokens": 22, "costUsd": 0.0123}, p["usage"]
    assert p["activity"] == {"toolCalls": 1, "assistantTurns": 2,
                             "model": "claude-fixture", "durationMs": 1234}, p["activity"]
    assert json.loads(p["finalResultLine"])["type"] == "result"
    assert wr == json.loads(p["finalText"].split("```json")[1].split("```")[0])
    # the benign allowed rate_limit_event must NOT leak into errorText
    assert p["errorText"] == "", p["errorText"]

    # torn line + garbage tolerated; detection still fires; result still found
    torn = '{"type":"system","subtype":"init"}\n{"type":"assistant","messa\ngarbage\n' + json.dumps(lines[-1])
    pt = parse(torn)
    assert pt["isStreamJson"] and pt["usage"]["outputTokens"] == 22

    # raw text (first line not JSON) → not stream-json, empty summary
    assert parse("line 000\nWORKER_RESULT {}")["isStreamJson"] is False
    assert parse("")["isStreamJson"] is False

    # error signalling reaches errorText (for rate-limit scan)
    err = json.dumps({"type": "result", "is_error": True, "subtype": "error_rate_limit",
                      "result": "rate limit exceeded"})
    pe = parse('{"type":"system"}\n' + err)
    assert "rate limit exceeded" in pe["errorText"]

    # no result event → finalText falls back to assistant text
    pf = parse('{"type":"assistant","message":{"content":[{"type":"text","text":"hi"}]}}')
    assert pf["finalText"] == "hi" and pf["usage"] is None

    # SPEC-114 v13: the chat streaming parser fires tool descriptors IN ORDER and
    # extracts final text + session_id + the raw result event (for _telemetry).
    fired: list[dict[str, str]] = []
    chat_lines = [
        '{"type":"system","subtype":"init","session_id":"S-1","model":"claude-fixture"}',
        json.dumps({"type": "assistant", "session_id": "S-1", "message": {"content": [
            {"type": "tool_use", "name": "Read", "input": {"file_path": "README.md"}}]}}),
        json.dumps({"type": "assistant", "session_id": "S-1", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls -la"}},
            {"type": "text", "text": "mid text"}]}}),
        "   ",          # blank line tolerated
        "{not json",    # torn line tolerated
        json.dumps({"type": "result", "session_id": "S-1", "result": "final answer",
                    "usage": {"input_tokens": 10, "output_tokens": 5}, "total_cost_usd": 0.01}),
    ]
    cs = parse_chat_stream(chat_lines, on_tool=fired.append)
    assert [(d["name"], d["arg"]) for d in fired] == [("Read", "README.md"), ("Bash", "ls -la")], fired
    assert cs["text"] == "final answer" and cs["session_id"] == "S-1", cs
    assert cs["result"]["type"] == "result" and cs["result"]["usage"]["output_tokens"] == 5, cs

    # chat-tool-chips: descriptors carry id + capped input_digest; tool_result
    # blocks in user events fire on_tool_result with a matching id.
    big = tool_descriptor({"type": "tool_use", "id": "tu-1", "name": "Bash",
                           "input": {"command": "x" * 5000}})
    assert big["id"] == "tu-1" and big["input_digest"].endswith("…[truncated]"), big
    assert len(big["input_digest"]) <= DIGEST_CAP + 20

    # a-task-call-carries-its-own-words: Task matches none of the primary-argument
    # keys, so its `description` — the spawning agent's own text, already inside
    # input_digest — is the FALLBACK. Copy, never interpret: no prompt/subagent_type
    # substitute, a standard key still wins, and no other tool gains the branch.
    tsk = tool_descriptor({"type": "tool_use", "id": "t1", "name": "Task",
                          "input": {"description": "audit the rail",
                                    "prompt": "body", "subagent_type": "reviewer"}})
    assert tsk["arg"] == "audit the rail" and "body" in tsk["input_digest"], tsk
    assert tool_descriptor({"type": "tool_use", "id": "t2", "name": "Task",
                            "input": {"prompt": "no description"}})["arg"] == ""
    assert tool_descriptor({"type": "tool_use", "id": "t3", "name": "Task",
                            "input": {"description": 42}})["arg"] == ""
    assert tool_descriptor({"type": "tool_use", "id": "t4", "name": "Task",
                            "input": {"command": "ls", "description": "loses"}})["arg"] == "ls"
    assert tool_descriptor({"type": "tool_use", "id": "t5", "name": "Bash",
                            "input": {"description": "not a Task"}})["arg"] == ""

    # chat-chip-render: Edit carries a unified `diff` (+lang); Write carries code+lang;
    # a plain Bash tool carries NEITHER (old chip events stay byte-identical).
    ed = tool_descriptor({"type": "tool_use", "id": "e1", "name": "Edit",
                          "input": {"file_path": "a/b.py", "old_string": "old\nkeep",
                                    "new_string": "new\nkeep"}})
    assert "-old" in ed["diff"] and "+new" in ed["diff"] and ed["language"] == "py", ed
    # chip-diff-v2: numbered pretty body + counts ride the same descriptor
    # (file absent -> anchors at line 1; context row keeps a bare number).
    assert ed["diffPretty"] == "1 - old\n1 + new\n2   keep", ed
    assert ed["diffAdded"] == 1 and ed["diffRemoved"] == 1, ed
    wr2 = tool_descriptor({"type": "tool_use", "id": "w1", "name": "Write",
                           "input": {"file_path": "x.JS", "content": "let a=1;"}})
    assert wr2["code"] == "let a=1;" and wr2["language"] == "js", wr2
    assert wr2["diffAdded"] == 1 and wr2["diffRemoved"] == 0 and "diffPretty" not in wr2, wr2
    plain = tool_descriptor({"type": "tool_use", "id": "b1", "name": "Bash",
                             "input": {"command": "ls"}})
    assert "diff" not in plain and "code" not in plain and "language" not in plain, plain
    # a-task-call-carries-its-own-words: Task carries description, prompt and
    # subagent_type; none match the primary-arg precedence, so arg fell to "" and
    # the phase line + rail read a bare "Running Task". The description is the
    # spawning agent's own words, already on the wire in input_digest -> becomes
    # arg. Copy, never interpret: no prompt/subagent_type fallback, a standard key
    # still wins, and input_digest is unchanged.
    tinp = {"description": "prepare workspace", "prompt": "do the work",
            "subagent_type": "scanner"}
    tk = tool_descriptor({"type": "tool_use", "id": "tk1", "name": "Task", "input": tinp})
    assert tk["arg"] == "prepare workspace", tk
    assert tk["input_digest"] == _digest(tinp), tk  # full input still digested
    # no description / empty / non-string description -> arg stays "" (no fallback)
    for bad in ({"prompt": "p", "subagent_type": "s"}, {"description": ""},
                {"description": 5}):
        td = tool_descriptor({"type": "tool_use", "id": "tk2", "name": "Task", "input": bad})
        assert td["arg"] == "", (bad, td)
    # a standard key still wins the precedence; description is only the fallback
    tkc = tool_descriptor({"type": "tool_use", "id": "tk3", "name": "Task",
                           "input": {"command": "run", "description": "words"}})
    assert tkc["arg"] == "run", tkc
    # the branch is Task-only: another tool with a description key is untouched
    tko = tool_descriptor({"type": "tool_use", "id": "tk4", "name": "Glob",
                           "input": {"description": "not surfaced"}})
    assert tko["arg"] == "", tko
    # chip-diff-v2: an anchored edit numbers from the real file line (5) and a
    # Write over an existing file renders as a numbered update diff.
    import tempfile as _tf
    with _tf.TemporaryDirectory() as _td:
        _p = os.path.join(_td, "f.py")
        with open(_p, "w", encoding="utf-8") as _fh:
            _fh.write("l1\nl2\nl3\nl4\nOLD\nl6\n")
        anchored = tool_descriptor({"type": "tool_use", "id": "e2", "name": "Edit",
                                    "input": {"file_path": _p, "old_string": "OLD",
                                              "new_string": "NEW"}})
        assert anchored["diffPretty"] == "5 - OLD\n5 + NEW", anchored
        wrup = tool_descriptor({"type": "tool_use", "id": "w2", "name": "Write",
                                "input": {"file_path": _p, "content": "l1\nl2\nl3\nl4\nX\nl6\n"}})
        assert "5 - OLD" in wrup["diffPretty"] and "5 + X" in wrup["diffPretty"], wrup
        assert wrup["diffAdded"] == 1 and wrup["diffRemoved"] == 1, wrup
    # chip-diff-v2 context collapse: a long equal run shrinks to N rows + `⋮`.
    ctx_text, a, r = numbered_diff([f"l{i}" for i in range(20)],
                                   [f"l{i}" for i in range(19)] + ["CHANGED"], context=2)
    assert "⋮" in ctx_text and a == 1 and r == 1, ctx_text
    assert "l0" in ctx_text and "l17" in ctx_text and "l5" not in ctx_text, ctx_text
    # chat-status-line: per-assistant-message usage fires on_usage.
    usages: list[dict[str, int]] = []
    pu = parse_chat_stream([json.dumps({"type": "assistant", "message": {
        "usage": {"output_tokens": 42, "input_tokens": 10, "cache_read_input_tokens": 5},
        "content": [{"type": "text", "text": "hi"}]}})], on_usage=usages.append)
    assert usages == [{"outputTokens": 42, "inputTokens": 10, "cacheReadTokens": 5}], usages
    # ctx fix: last assistant message's full input rides the parse result
    # (usage-less streams stay honest with None).
    assert pu["last_ctx_tokens"] == 15, pu
    assert cs["last_ctx_tokens"] is None, cs
    results_fired: list[dict[str, str]] = []
    parse_chat_stream([
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "tu-1", "name": "Read",
             "input": {"file_path": "README.md"}}]}}),
        json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tu-1",
             "content": [{"type": "text", "text": "line one"}]}]}}),
        json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tu-2", "content": "plain"}]}}),
    ], on_tool_result=results_fired.append)
    assert results_fired == [{"id": "tu-1", "digest": "line one"},
                             {"id": "tu-2", "digest": "plain"}], results_fired
    # no result event → text falls back to assistant text; no tool_use → no fires
    fired2: list[dict[str, str]] = []
    cs2 = parse_chat_stream(
        [json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}})],
        on_tool=fired2.append)
    assert cs2["text"] == "hi" and cs2["session_id"] is None and cs2["result"] is None and fired2 == [], cs2
    # room-chat fix: on_text fires per mid-turn assistant text block, in order
    texts: list[str] = []
    parse_chat_stream(chat_lines, on_text=texts.append)
    assert texts == ["mid text"], texts

    # parse_codex_stream: LIVE capture shape (codex-cli 0.144.4, 2026-07-16 —
    # command_execution started/completed, file_change, mid-turn agent_messages,
    # usage with cached_input_tokens) + todo_list. Same callback contract as claude.
    cx = [
        {"type": "thread.started", "thread_id": "T-1"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"id": "i0", "type": "error", "message": "skills budget"}},
        {"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": "Vou executar."}},
        {"type": "item.started", "item": {"id": "i2", "type": "command_execution",
            "command": "echo hi", "aggregated_output": "", "exit_code": None, "status": "in_progress"}},
        {"type": "item.completed", "item": {"id": "i2", "type": "command_execution",
            "command": "echo hi", "aggregated_output": "hi", "exit_code": 0, "status": "completed"}},
        {"type": "item.started", "item": {"id": "i3", "type": "todo_list",
            "items": [{"text": "step one", "completed": False}]}},
        {"type": "item.updated", "item": {"id": "i3", "type": "todo_list",
            "items": [{"text": "step one", "completed": True}]}},
        {"type": "item.completed", "item": {"id": "i4", "type": "file_change", "status": "completed",
            "changes": [{"path": "note.py", "kind": "add"}]}},  # no started — fires at completed
        {"type": "item.completed", "item": {"id": "i5", "type": "agent_message", "text": "Feito."}},
        {"type": "turn.completed", "usage": {"input_tokens": 89983, "cached_input_tokens": 75264,
                                             "output_tokens": 373, "reasoning_output_tokens": 109}},
    ]
    cx_tools: list[dict[str, Any]] = []
    cx_results: list[dict[str, Any]] = []
    cx_plans: list[list[dict[str, str]]] = []
    cx_texts: list[str] = []
    out = parse_codex_stream([json.dumps(e) for e in cx], on_tool=cx_tools.append,
                             on_tool_result=cx_results.append,
                             on_plan=cx_plans.append, on_text=cx_texts.append)
    assert out["session_id"] == "T-1" and out["text"] == "Vou executar.\n\nFeito.", out
    assert out["result"]["usage"]["cached_input_tokens"] == 75264, out["result"]
    assert out["errors"] == ["[codex: skills budget]"], out["errors"]
    assert cx_texts == ["Vou executar.", "Feito."], cx_texts
    assert [(t["name"], t["arg"], t["id"]) for t in cx_tools] == \
        [("Bash", "echo hi", "i2"), ("Write", "note.py", "i4")], cx_tools
    assert cx_tools[1].get("language") == "py", cx_tools[1]
    # M1: file_change descriptors carry the internal changes list for the engine's
    # snapshot→diff derivation (never emitted to the panel).
    assert cx_tools[1].get("changes") == [{"path": "note.py", "kind": "add"}], cx_tools[1]
    assert "changes" not in cx_tools[0], cx_tools[0]
    assert cx_results[0] == {"id": "i2", "digest": "hi"} \
        and cx_results[1]["id"] == "i4" and "add note.py" in cx_results[1]["digest"], cx_results
    assert cx_plans == [[{"text": "step one", "status": "pending"}],
                        [{"text": "step one", "status": "completed"}]], cx_plans
    # failed command carries the exit code in the digest; dedupe: started+completed = ONE on_tool
    fired_once: list[dict[str, Any]] = []
    res2: list[dict[str, Any]] = []
    parse_codex_stream([
        json.dumps({"type": "item.started", "item": {"id": "c1", "type": "command_execution",
                    "command": "boom", "aggregated_output": "", "status": "in_progress"}}),
        json.dumps({"type": "item.completed", "item": {"id": "c1", "type": "command_execution",
                    "command": "boom", "aggregated_output": "bad", "exit_code": 1, "status": "failed"}}),
    ], on_tool=fired_once.append, on_tool_result=res2.append)
    assert len(fired_once) == 1 and res2[0]["digest"] == "bad\n[exit code 1]", (fired_once, res2)
    # non-JSONL output falls back to raw text (old _parse contract)
    assert parse_codex_stream(["raw non-jsonl output"])["text"] == "raw non-jsonl output"
    print("stream_json self-check ok")


if __name__ == "__main__":
    _self_check()
