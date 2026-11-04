"""Codex app-server transport (M3, 2026-07-17) — JSON-RPC over stdio.

`codex app-server` is the interface the official IDE extension uses. Against the
exec --json surface it adds exactly what the chat panel wants: NATIVE per-file
diffs (v2 FileUpdateChange carries `diff`), the aggregated turn diff
(`turn/diff/updated`), live command output (`item/commandExecution/outputDelta`),
plan updates (`turn/plan/updated`), and real token usage incl. the model context
window (`thread/tokenUsage/updated`). Approval server-requests are auto-answered
per chat mode and SURFACED as text notes (interactive mid-turn HITL waits for the
harness sandbox spec — intake d7be61ebd09a).

Engine contract: identical to ClaudeEngine/CodexEngine — send(msg) -> reply,
getattr-guarded callbacks (on_activity/on_tool_result/on_plan/on_text, plus the
additive on_tool_delta/on_turn_diff), last_usage. Opt-in via
HARNESS_CODEX_TRANSPORT=app-server; construction failure falls back to the exec
transport in chat_engines._construct (one notify, no chain change).

Wire shapes pinned against the INSTALLED CLI's generated schema
(`codex app-server generate-ts`, codex-cli 0.144.4). Stdlib only.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

try:
    from . import stream_json
    from .chat_engines import INTERRUPT_NOTE, TURN_TIMEOUT, find_codex
    from .common import HarnessError
    from .processes import popen_interactive
except ImportError:  # run directly
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from harness_lib import stream_json
    from harness_lib.chat_engines import INTERRUPT_NOTE, TURN_TIMEOUT, find_codex
    from harness_lib.common import HarnessError
    from harness_lib.processes import popen_interactive

HANDSHAKE_TIMEOUT = 30.0
PROMPT_REL = ".harness/prompts/harness-operator.md"


def _sandbox_policy(chat_mode: str, room_tools: list[str], room_role: str) -> dict[str, Any] | None:
    """Per-turn SandboxPolicy object — the same capability mapping the exec
    transport uses (rooms → directory sandbox; ui-overseer read-only; plan wins).
    None = no override (exec parity: auto mode passes no -s, the user config
    default rules — forcing readOnly here broke the porteiro's route --task,
    live GUI run 2026-07-17)."""
    write = {"type": "workspaceWrite", "writableRoots": [], "networkAccess": False,
             "excludeTmpdirEnvVar": False, "excludeSlashTmp": False}
    read = {"type": "readOnly", "networkAccess": False}
    if chat_mode == "plan":
        return read
    if room_tools:
        return read if room_role == "ui-overseer" else write
    if chat_mode == "accept-edits":
        return write
    return None


def map_notification(msg: dict[str, Any], state: dict[str, Any],
                     on_tool: Any = None, on_tool_result: Any = None,
                     on_plan: Any = None, on_text: Any = None,
                     on_tool_delta: Any = None, on_turn_diff: Any = None) -> None:
    """Fold ONE app-server notification into the vendor-neutral callback
    contract. Pure given `state` (mutated in place: text/errors/usage/turn
    bookkeeping) — unit-testable without a process."""
    method, params = msg.get("method"), msg.get("params") or {}
    if method == "item/started" or method == "item/completed":
        item = params.get("item") or {}
        itype, iid = item.get("type"), str(item.get("id") or "")
        if itype == "agentMessage":
            if method == "item/completed" and item.get("text"):
                state.setdefault("texts", []).append(str(item["text"]))
                if on_text:
                    on_text(str(item["text"]))
            return
        if itype == "error":
            state.setdefault("errors", []).append(f"[codex: {item.get('message')}]")
            return
        desc = _item_descriptor(item)
        if desc is None:
            return
        if iid not in state.setdefault("seen", set()):
            state["seen"].add(iid)
            if on_tool:
                on_tool(desc)
        if method == "item/completed" and on_tool_result:
            res: dict[str, Any] = {"id": iid, "digest": _item_result_digest(item)}
            if stream_json.item_failed(item):  # chip-error-color
                res["isError"] = True
            if itype == "fileChange":
                diff = _file_change_native_diff(item)
                if diff:  # NATIVE per-file payload — no derivation, no race
                    res["diff"] = stream_json._digest(diff)
            on_tool_result(res)
    elif method == "item/commandExecution/outputDelta":
        if on_tool_delta and params.get("delta"):
            on_tool_delta({"id": str(params.get("itemId") or ""),
                           "chunk": str(params["delta"])})
    elif method == "turn/plan/updated":
        if on_plan:
            steps = [{"text": str(s.get("step") or s.get("text") or "").strip(),
                      "status": str(s.get("status") or "pending").lower().replace("inprogress", "in_progress")}
                     for s in params.get("plan") or [] if isinstance(s, dict)]
            steps = [s for s in steps if s["text"]]
            if steps:
                on_plan(steps)
    elif method == "turn/diff/updated":
        state["turn_diff"] = str(params.get("diff") or "")
        if on_turn_diff and state["turn_diff"]:
            on_turn_diff(state["turn_diff"])
    elif method == "thread/tokenUsage/updated":
        state["token_usage"] = params.get("tokenUsage") or {}
    elif method == "turn/completed":
        state["done"] = True
        turn = params.get("turn") or {}
        if turn.get("status") == "failed" and turn.get("error"):
            state.setdefault("errors", []).append(
                f"[codex turn failed: {(turn['error'] or {}).get('message') or turn['error']}]")
    elif method == "error":
        state.setdefault("errors", []).append(f"[codex: {params.get('message')}]")


def _kind_of(change: dict[str, Any]) -> str:
    """Live wire (0.144.4): `kind` is a serde-tagged OBJECT ({"type":"add"}),
    the generated TS says string — accept both."""
    kind = change.get("kind")
    return str(kind.get("type") if isinstance(kind, dict) else kind or "")


def _file_change_native_diff(item: dict[str, Any]) -> str:
    """Per-file diff text from a completed v2 fileChange. Live wire: `diff` is a
    real unified diff for updates but the RAW CONTENT for add/delete — synthesize
    the unified shape for those so the chip colorizer has +/- lines to paint."""
    import difflib
    parts: list[str] = []
    for c in item.get("changes") or []:
        if not isinstance(c, dict):
            continue
        path, kind, payload = str(c.get("path") or ""), _kind_of(c), str(c.get("diff") or "")
        if not payload:
            continue
        if kind == "add":
            parts.append("\n".join(difflib.unified_diff(
                [], payload.splitlines(), fromfile="/dev/null", tofile=f"b/{path}", lineterm="")))
        elif kind == "delete":
            parts.append("\n".join(difflib.unified_diff(
                payload.splitlines(), [], fromfile=f"a/{path}", tofile="/dev/null", lineterm="")))
        else:
            parts.append(payload)
    return "\n".join(p for p in parts if p).strip()


def _item_descriptor(item: dict[str, Any]) -> dict[str, Any] | None:
    """v2 ThreadItem -> the SAME chip descriptor shape the other parsers emit."""
    itype, iid = item.get("type"), str(item.get("id") or "")
    if itype == "commandExecution":
        cmd = str(item.get("command") or "")
        return {"name": "Bash", "arg": cmd, "id": iid, "input_digest": stream_json._digest(cmd)}
    if itype == "fileChange":
        changes = [c for c in item.get("changes") or [] if isinstance(c, dict)]
        if not changes:
            return None
        first = str(changes[0].get("path") or "")
        arg = first + (f" (+{len(changes) - 1} more)" if len(changes) > 1 else "")
        name = "Write" if _kind_of(changes[0]) == "add" else "Edit"
        digest = "\n".join(f"{_kind_of(c)} {c.get('path')}" for c in changes)
        desc = {"name": name, "arg": arg, "id": iid, "input_digest": stream_json._digest(digest)}
        lang = stream_json._lang_of(first)
        if lang:
            desc["language"] = lang
        return desc
    if itype == "webSearch":
        q = str(item.get("query") or "")
        return {"name": "WebSearch", "arg": q, "id": iid, "input_digest": stream_json._digest(q)}
    if itype == "mcpToolCall":
        name = ".".join(str(item.get(k)) for k in ("server", "tool") if item.get(k)) or "mcp"
        return {"name": name, "arg": "", "id": iid, "input_digest": ""}
    return None  # reasoning/plan/userMessage/etc. handled elsewhere or dropped


def _item_result_digest(item: dict[str, Any]) -> str:
    if item.get("type") == "commandExecution":
        out = str(item.get("aggregatedOutput") or "")
        code = item.get("exitCode")
        if code not in (None, 0):
            out = (out + f"\n[exit code {code}]").strip()
        return stream_json._digest(out or f"({item.get('status') or 'completed'})")
    if item.get("type") == "fileChange":
        lines = [f"{_kind_of(c)} {c.get('path')}" for c in item.get("changes") or []
                 if isinstance(c, dict)]
        return stream_json._digest("\n".join([*lines, str(item.get("status") or "")]).strip())
    return stream_json._digest(str(item.get("status") or "completed"))


class CodexAppServerEngine:
    """One persistent `codex app-server` process; one thread; one turn per send."""

    name = "codex"

    def __init__(self, root: Path, model: str | None, effort: str | None,
                 prompt_rel: str = PROMPT_REL, diet: dict[str, Any] | None = None) -> None:
        binary = find_codex()
        if not binary:
            raise HarnessError("codex CLI not found on PATH")
        self.root = root
        self.model = model
        self.effort = effort
        self.prompt_rel = prompt_rel
        self.chat_mode = "auto"
        self.last_usage: dict[str, Any] | None = None
        self.thread_id: str | None = None
        self._id = 0
        self._lock = threading.Lock()
        self._wlock = threading.Lock()  # interrupt() writes from another thread
        self._interrupted = threading.Event()
        try:
            self.proc = popen_interactive(
                [binary, "app-server"], cwd=root,
                env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
        except OSError as exc:
            raise HarnessError(f"codex app-server spawn failed: {exc}") from exc
        try:
            self._request("initialize", {
                "clientInfo": {"name": "universal_agent_harness", "title": "Universal Agent Harness",
                               "version": "0"},
                "capabilities": {"optOutNotificationMethods": [
                    "item/agentMessage/delta", "item/reasoning/summaryTextDelta",
                    "item/reasoning/textDelta", "item/reasoning/summaryPartAdded",
                    "rawResponseItem/completed"]},
            }, timeout=HANDSHAKE_TIMEOUT)
            self._notify("initialized", {})
        except Exception as exc:
            self.close()
            raise HarnessError(f"codex app-server handshake failed: {exc}") from exc

    # -- JSON-RPC plumbing -----------------------------------------------------
    def _send_msg(self, msg: dict[str, Any]) -> None:
        with self._wlock:
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()

    def interrupt(self) -> None:
        """Stop the CURRENT turn via the native turn/interrupt notification;
        the read loop drains until the server closes the turn (grace-capped)."""
        self._interrupted.set()
        if self.thread_id:
            try:
                self._notify("turn/interrupt", {"threadId": self.thread_id})
            except Exception:
                pass

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        self._send_msg({"method": method, "params": params})

    def _read_msg(self, deadline: float) -> dict[str, Any] | None:
        """One JSONL message, or None at EOF/deadline. ponytail: blocking readline
        + deadline check per line — a watchdog kill (send()) breaks a hung read."""
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                return None
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if isinstance(msg, dict):
                return msg
        return None

    def _request(self, method: str, params: dict[str, Any], timeout: float,
                 on_other: Any = None) -> dict[str, Any]:
        """Blocking request; non-matching messages go to `on_other` (or drop)."""
        with self._lock:
            self._id += 1
            rid = self._id
        self._send_msg({"id": rid, "method": method, "params": params})
        deadline = time.time() + timeout
        while True:
            msg = self._read_msg(deadline)
            if msg is None:
                raise HarnessError(f"{method}: no response (timeout/EOF)")
            if msg.get("id") == rid and ("result" in msg or "error" in msg):
                if msg.get("error"):
                    raise HarnessError(f"{method}: {msg['error'].get('message') or msg['error']}")
                return msg.get("result") or {}
            if on_other:
                on_other(msg)

    # -- turn ------------------------------------------------------------------
    def _answer_server_request(self, msg: dict[str, Any], on_text: Any) -> None:
        """Auto-answer an approval server-request per chat mode and SURFACE the
        decision — visibility now, interactive HITL with the sandbox spec later."""
        method = str(msg.get("method") or "")
        approve = self.chat_mode != "plan"
        decision = "approved" if approve else "denied"
        params = msg.get("params") or {}
        subject = (params.get("command") or params.get("reason")
                   or ",".join((params.get("changes") or {}).keys()) or method)
        self._send_msg({"id": msg.get("id"), "result": {"decision": decision}})
        if on_text:
            on_text(f"[aprovação automática: {decision} — {str(subject)[:160]} (modo {self.chat_mode})]")

    def send(self, msg: str) -> str:
        on_tool = getattr(self, "on_activity", None)
        on_tool_result = getattr(self, "on_tool_result", None)
        on_plan = getattr(self, "on_plan", None)
        on_text = getattr(self, "on_text", None)
        on_tool_delta = getattr(self, "on_tool_delta", None)
        on_turn_diff = getattr(self, "on_turn_diff", None)
        started = time.monotonic()
        state: dict[str, Any] = {}

        def other(m: dict[str, Any]) -> None:
            if m.get("method") and m.get("id") is not None:  # server -> client request
                self._answer_server_request(m, on_text)
            elif m.get("method"):
                map_notification(m, state, on_tool, on_tool_result, on_plan,
                                 on_text, on_tool_delta, on_turn_diff)

        try:
            if not self.thread_id:
                res = self._request("thread/start", {
                    "cwd": str(self.root),
                    "approvalPolicy": "never",
                    "developerInstructions": (self.root / self.prompt_rel).read_text(encoding="utf-8"),
                    **({"model": self.model} if self.model else {}),
                }, timeout=HANDSHAKE_TIMEOUT, on_other=other)
                self.thread_id = str(((res.get("thread") or {}).get("id")) or res.get("threadId") or "")
                if not self.thread_id:
                    raise HarnessError(f"thread/start returned no id: {res}")
            params: dict[str, Any] = {
                "threadId": self.thread_id,
                "input": [{"type": "text", "text": msg, "text_elements": []}],
                "approvalPolicy": "never",
            }
            sandbox = _sandbox_policy(self.chat_mode,
                                      getattr(self, "room_tools", []) or [],
                                      getattr(self, "room_role", ""))
            if sandbox is not None:
                params["sandboxPolicy"] = sandbox
            if self.model:
                params["model"] = self.model
            if self.effort:
                params["effort"] = self.effort
            self._request("turn/start", params, timeout=HANDSHAKE_TIMEOUT, on_other=other)
        except HarnessError as exc:
            return f"[codex app-server error] {exc}"
        self._interrupted.clear()
        watch_done = threading.Event()
        flag = os.environ.get("HARNESS_CHAT_STOP_FILE")
        if flag:  # panel ⏹: the stop flag arrives out-of-band (see chat_engines)
            try:  # a stale flag (⏹ clicked between turns) must not kill THIS turn
                os.unlink(flag)
            except OSError:
                pass

            def _flagwatch() -> None:
                while not watch_done.wait(0.25):
                    if os.path.exists(flag):
                        try:
                            os.unlink(flag)
                        except OSError:
                            pass
                        self.interrupt()
                        return

            threading.Thread(target=_flagwatch, daemon=True).start()
        deadline = time.time() + TURN_TIMEOUT
        try:
            while not state.get("done"):
                if self._interrupted.is_set():  # grace window for the aborted-turn close
                    deadline = min(deadline, time.time() + 8)
                m = self._read_msg(deadline)
                if m is None:
                    if self._interrupted.is_set():
                        break
                    if time.time() >= deadline:
                        try:  # best-effort interrupt; the turn result is lost either way
                            self._notify("turn/interrupt", {"threadId": self.thread_id})
                        except Exception:
                            pass
                        return (f"[codex turn timed out after {TURN_TIMEOUT:.0f}s — "
                                "raise HARNESS_CHAT_TURN_TIMEOUT if legitimate]")
                    return "[codex app-server exited mid-turn]"
                else:
                    other(m)
        finally:
            watch_done.set()
        if self._interrupted.is_set():
            partial = "\n\n".join(t for t in state.get("texts") or [] if t).strip()
            return f"{partial}\n\n{INTERRUPT_NOTE}" if partial else INTERRUPT_NOTE
        usage = ((state.get("token_usage") or {}).get("last")
                 or (state.get("token_usage") or {}))
        if usage:
            self.last_usage = {
                "in_tokens": usage.get("inputTokens"),
                "out_tokens": usage.get("outputTokens"),
                "cache_read_tokens": usage.get("cachedInputTokens"),
                "model": self.model, "cost_usd": None,
                "duration_s": time.monotonic() - started,
                "ctx_used": usage.get("inputTokens"),
                "ctx_window": (state.get("token_usage") or {}).get("modelContextWindow"),
            }
        reply = "\n\n".join(t for t in state.get("texts") or [] if t)
        errors = state.get("errors") or []
        if reply and errors:
            reply += "\n" + "\n".join(errors)
        return reply or "\n".join(errors)

    def close(self) -> None:
        try:
            self.proc.kill()
        except Exception:
            pass


def _self_check() -> None:
    """Offline: map_notification folds a canned turn into the callback contract."""
    fired: list[tuple[str, str]] = []
    results: list[dict[str, Any]] = []
    plans: list[list[dict[str, str]]] = []
    texts: list[str] = []
    deltas: list[dict[str, str]] = []
    diffs: list[str] = []
    state: dict[str, Any] = {}
    ntf = [
        {"method": "item/started", "params": {"item": {
            "id": "c1", "type": "commandExecution", "command": "echo hi",
            "aggregatedOutput": None, "exitCode": None, "status": "inProgress"}}},
        {"method": "item/commandExecution/outputDelta",
         "params": {"itemId": "c1", "delta": "hi\n"}},
        {"method": "item/completed", "params": {"item": {
            "id": "c1", "type": "commandExecution", "command": "echo hi",
            "aggregatedOutput": "hi", "exitCode": 0, "status": "completed"}}},
        {"method": "turn/plan/updated", "params": {"plan": [
            {"step": "do it", "status": "inProgress"}]}},
        {"method": "item/completed", "params": {"item": {
            "id": "f1", "type": "fileChange", "status": "completed", "changes": [
                {"path": "x.py", "kind": "update",
                 "diff": "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b"}]}}},
        {"method": "turn/diff/updated", "params": {"diff": "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b"}},
        {"method": "item/completed", "params": {"item": {
            "id": "m1", "type": "agentMessage", "text": "done"}}},
        {"method": "thread/tokenUsage/updated", "params": {"tokenUsage": {
            "last": {"inputTokens": 10, "cachedInputTokens": 4, "outputTokens": 3,
                     "totalTokens": 17, "reasoningOutputTokens": 0},
            "total": {}, "modelContextWindow": 272000}}},
        {"method": "turn/completed", "params": {"turn": {"status": "completed"}}},
    ]
    for m in ntf:
        map_notification(m, state, on_tool=lambda d: fired.append((d["name"], d["arg"])),
                         on_tool_result=results.append, on_plan=plans.append,
                         on_text=texts.append, on_tool_delta=deltas.append,
                         on_turn_diff=diffs.append)
    assert fired == [("Bash", "echo hi"), ("Edit", "x.py")], fired
    assert deltas == [{"id": "c1", "chunk": "hi\n"}], deltas
    assert results[0] == {"id": "c1", "digest": "hi"}, results
    assert results[1]["id"] == "f1" and "-a" in results[1]["diff"] and "+b" in results[1]["diff"], results
    assert plans == [[{"text": "do it", "status": "in_progress"}]], plans
    assert texts == ["done"] and state["done"] is True, (texts, state.get("done"))
    assert diffs and "+b" in diffs[0], diffs
    assert state["token_usage"]["modelContextWindow"] == 272000
    # sandbox mapping mirrors the exec transport (None = no override, config default)
    assert _sandbox_policy("plan", ["Edit(x)"], "route-overseer")["type"] == "readOnly"
    assert _sandbox_policy("auto", ["Edit(x)"], "route-overseer")["type"] == "workspaceWrite"
    assert _sandbox_policy("auto", ["Edit(x)"], "ui-overseer")["type"] == "readOnly"
    assert _sandbox_policy("auto", [], "") is None
    print("codex_appserver self-check ok")


if __name__ == "__main__":
    _self_check()
