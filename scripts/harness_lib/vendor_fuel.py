"""Vendor fuel gauge — N-VENDORCREDIT v1: canonical state + probes + verb.

Spec: specs/40-features/vendor-fuel-gauge.md. The state written here
(.harness/state/vendor-fuel.json) is the CANONICAL machine-readable feed; the GUI
strip is only a reader and the future worker load-balancer is the load-bearing
consumer (D017/U-scarcity). Nothing here fabricates a number.

Per-vendor surfaces (2026-07-22 truth-gates, claude half CORRECTED 2026-07-23):

  claude -> `claude -p "/usage" --model <cheap> --output-format json`: the CLI
    renders the real subscription panel (session / week % USED + resets); parsed
    to pct = 100 - week-all-models-used. The earlier "interactive-TUI only, pct
    null" verdict was WRONG — a Git-Bash MSYS mangle turned `/usage` into a
    Windows path; via subprocess LIST args the panel renders fine, and pinned to
    the cheapest model (_PROBE_MODEL) the turn bills ~$0. Falls back to
    `auth status --json` liveness (pct null, "(no usage panel)" detail).
  codex  -> `codex login status` (FREE, auth) + the SESSION ROLLOUT gauge
    (owner-corrected 2026-07-23, third codex claim overturned today): every
    `codex exec` run writes `~/.codex/sessions/Y/M/D/rollout-*.jsonl` whose
    token_count events carry REAL `rate_limits` — primary.used_percent over
    window_minutes (10080 = week) + resets_at. Reading the newest rollout is
    a file read, zero model cost: pct = 100 - used_percent. No rollout on
    disk -> pct stays null with the honest detail (the cheap refresh — one
    minimal luna-low exec turn to mint a fresh rollout — is the documented
    escalation, not the default).
  antigravity -> the agy `/usage` TUI panel (Models & Quota), driven HEADLESS in a
    pseudo-console (pywinpty/ConPTY): agy has no usage CLI subcommand and `/usage`
    runs only in the interactive TUI (print mode hallucinates it and spends a turn).
    The driver accepts the folder-trust prompt for a throwaway cwd, runs /usage, and
    scrapes the panel. The PROBE seat is the cheapest model (gemini-3.6-flash-low) —
    /usage renders regardless of model — but the binding window is the whole GEMINI
    MODELS group (Flash + Pro share one quota), which is also what the production R9
    seat (gemini-3.6-flash-high) draws from: its 5h + weekly REMAINING% ride the
    standard slots (codex shape), pct = the smaller of the two. When the panel can't
    be read (pywinpty absent / not signed in / timeout / layout drift) the row is
    UNAVAILABLE with an honest detail — never a fabricated reading, never invented
    5h/wk gaps. TUI-scrape is version-fragile, so the parser is unit-pinned on a
    captured fixture.

This module NEVER invents a scale: no real percent -> pct is null and the
honest text detail is shown instead.

Privacy: `claude auth status` also returns email/orgId/orgName. Those are PII and
are NEVER stored or surfaced — only the fields in _CLAUDE_KEEP are whitelisted
into the canonical value.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import shutil
import threading
from subprocess import TimeoutExpired

import sys
import time
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # direct run for the __main__ self-check
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness_lib import processes
from harness_lib.common import ROOT, now, parse_iso_datetime, read_json, write_json

SCHEMA_VERSION = 1
STATE_REL = ".harness/state/vendor-fuel.json"
VENDORS = ("claude", "codex", "kimi", "antigravity")
PROBE_TIMEOUT_S = 60           # per-vendor cap; a hung CLI becomes 'unavailable', not a hang
STALE_SECONDS = 2 * 3600       # a probe older than this reads 'stale' (GUI/show)

# claude value whitelist — keep PII (email/orgId/orgName) OUT of the canonical state.
_CLAUDE_KEEP = ("loggedIn", "authMethod", "subscriptionType", "apiProvider")

# The /usage panel is CLI-rendered — the model does no work, so pin the CHEAPEST tier
# (owner 2026-07-23: the default Fable turn billed ~$0.37; haiku bills ~$0). Measured:
# haiku returns the identical panel at cost_usd 0. Update the id when a cheaper tier ships.
_PROBE_MODEL = "claude-haiku-4-5-20251001"


# -- probe plumbing ----------------------------------------------------------

def _run(name: str, args: list[str], path: str | None,
         timeout: int = PROBE_TIMEOUT_S, cwd: str | None = None) -> tuple[int, str, str, int] | None:
    """Run a vendor CLI, resolving the executable via PATH (path overrides it for
    the mock shim in tests; None uses the live environment). `cwd` pins the child's
    working directory (the mint turns run from an EMPTY temp dir on purpose). Returns
    (rc, stdout, stderr, elapsed_ms), or None when the CLI is not on PATH."""
    exe = shutil.which(name, path=path)
    if not exe:
        return None
    env = None if path is None else {**os.environ, "PATH": path}
    t0 = time.monotonic()
    # raw-subprocess-ratchet: every spawn routes through the processes helpers
    # (stdin hygiene + creationflags + the encoding defaults live there).
    proc = processes.run_quiet([exe, *args], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=timeout,
                               env=env, cwd=cwd)
    return proc.returncode, proc.stdout or "", proc.stderr or "", int((time.monotonic() - t0) * 1000)


def _fresh(value: dict[str, Any], label: str, pct: float | None,
           detail: str, probe_ms: int) -> dict[str, Any]:
    return {"kind": "cli-usage", "value": value,
            "summary": {"label": label, "pct": pct, "detail": detail},
            "capturedAt": now(), "probeMs": probe_ms, "status": "fresh"}


def _unavailable(label: str, detail: str, probe_ms: int, error: str) -> dict[str, Any]:
    return {"kind": "cli-usage", "value": {},
            "summary": {"label": label, "pct": None, "detail": detail},
            "capturedAt": now(), "probeMs": probe_ms, "status": "unavailable", "error": error}


# -- per-vendor probes (pinned truth-gates) ----------------------------------

# Match "Current session: 4% used" / "Current week (all models): 77% used · resets ..."
# Separator before "resets" is a middle-dot in practice; \W+ tolerates any glyph/spacing.
_USAGE_LINE = re.compile(r"(?im)^\s*Current\s+(session|week[^:]*):\s*(\d+)%\s*used(?:\W+resets\s+([^\n]+))?")


def _parse_usage_panel(text: str) -> dict[str, Any]:
    """Parse `/usage`'s rendered panel into {session, weekAll, perModel, resets}.
    The panel gives % USED per window ('Current week (all models): 77% used · resets
    Jul 26'); pct-remaining = 100-used. The binding constraint is the week-all-models
    window, so that drives the gauge's pct. Missing windows -> absent, never faked."""
    out: dict[str, Any] = {"windows": []}
    for m in _USAGE_LINE.finditer(text):
        win, used, resets = m.group(1).strip().lower(), int(m.group(2)), (m.group(3) or "").strip()
        row = {"window": win, "usedPct": used, "resets": resets}
        out["windows"].append(row)
        if win == "session":
            out["session"] = row
        elif "all models" in win:
            out["weekAll"] = row
    return out


def probe_claude(path: str | None = None) -> dict[str, Any]:
    """`claude -p "/usage" --output-format json` -> the REAL subscription gauge. The
    panel renders session/week/per-model % USED + reset times in the `result` text
    (owner-corrected 2026-07-23: the earlier auth-status/`pct=null` gate was defeated
    by Git-Bash MSYS mangling `/usage`->a path; via subprocess LIST args there is no
    shell, so /usage passes intact). pct = 100 - week(all-models) used (the binding
    window). COST ~$0.37/probe -> the caller uses a SPARSE cadence (hourly), never
    per-heartbeat; the heartbeat reads the cached state. Falls back to auth-status
    liveness (pct=null) when /usage yields no parseable panel."""
    label = "CLAUDE"
    try:
        res = _run("claude", ["-p", "/usage", "--model", _PROBE_MODEL, "--output-format", "json"], path)
    except TimeoutExpired:
        return _unavailable(label, "probe timed out", PROBE_TIMEOUT_S * 1000, "timeout")
    except OSError as exc:
        return _unavailable(label, "probe failed", 0, f"{type(exc).__name__}: {exc}")
    if res is None:
        return _unavailable(label, "claude CLI not found", 0, "cli-not-found")
    rc, out, err, ms = res
    try:
        panel = json.loads(out).get("result", "")
    except (ValueError, json.JSONDecodeError):
        panel = ""
    parsed = _parse_usage_panel(panel)
    week = parsed.get("weekAll")
    if week and isinstance(week.get("usedPct"), int):
        pct = 100 - week["usedPct"]  # gas REMAINING this week (the binding window)
        sess = parsed.get("session") or {}
        detail = f"week {pct}% left" + (f" · session {100 - sess['usedPct']}%" if sess.get("usedPct") is not None else "")
        if week.get("resets"):
            detail += f" · resets {week['resets']}"
        return _fresh(parsed, label, float(pct), detail, ms)
    # /usage gave nothing parseable -> cheap liveness fallback (auth status, pct=null).
    try:
        alive = _run("claude", ["auth", "status", "--json"], path)
        data = json.loads(alive[1]) if alive else {}
    except Exception:  # noqa: BLE001
        data = {}
    if data.get("loggedIn"):
        value = {k: data.get(k) for k in _CLAUDE_KEEP if data.get(k) is not None}
        det = " · ".join(x for x in (value.get("subscriptionType"), value.get("authMethod")) if x) or "logged in"
        return _fresh(value, label, None, det + " (no usage panel)", ms)
    return _unavailable(label, "no usage panel; not logged in", ms, (err or panel or "no output").strip()[:200])


_CODEX_HOME = Path.home() / ".codex"


def _window_label(minutes: Any) -> str:
    """One naming for a quota window across every codex site (the owner's
    padronizacao demand): 300 -> `5h`, 10080 -> `week`, anything else states its
    own minutes. Unknown minutes are printed, never guessed into a bucket; an
    ABSENT one is `?` (the GUI's own token for the same gap), never `Nonemin`."""
    if minutes is None:
        return "?"
    return {300: "5h", 10080: "week"}.get(minutes) or f"{minutes}min"


def _codex_rollout_limits(home: Path | None = None) -> dict[str, Any] | None:
    """Newest session rollout's LAST rate_limits event, or None. Pure file read
    (~/.codex/sessions/Y/M/D/rollout-*.jsonl); malformed lines are skipped, a
    missing tree returns None — never raises."""
    env_home = os.environ.get("HARNESS_CODEX_HOME")  # hermetic override (scenarios)
    base = (home or (Path(env_home) if env_home else _CODEX_HOME)) / "sessions"
    try:
        files = sorted(base.glob("*/*/*/rollout-*.jsonl"),
                       key=lambda p: p.stat().st_mtime, reverse=True)[:5]
    except OSError:
        return None
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for line in reversed(text.splitlines()):
            if '"rate_limits"' not in line:
                continue
            try:
                evt = json.loads(line)
            except ValueError:
                continue
            # rate_limits nests at varying depths across codex versions — walk for it.
            rl = _find_key(evt, "rate_limits")
            primary = (rl or {}).get("primary") if isinstance(rl, dict) else None
            if isinstance(primary, dict) and isinstance(primary.get("used_percent"), (int, float)):
                out = {"usedPercent": float(primary["used_percent"]),
                       "windowMinutes": primary.get("window_minutes"),
                       "resetsAt": primary.get("resets_at"),
                       "rolloutMtime": f.stat().st_mtime,
                       "rollout": f.name}
                # `secondary` is a COMPATIBILITY path, not a window observed here.
                # CORRECTED 2026-07-31 (the earlier comment claimed the pair was
                # "MEASURED locally" — it was not): across the 6 newest rollouts,
                # 199 rate_limits events, EVERY `secondary` is null and the only
                # `window_minutes` ever present is 10080 (plan_type "plus"). Older
                # codex versions did emit a pair, and there the slots FLIP, so
                # window_minutes — never the slot — is the label (300 = 5h,
                # 10080 = week). The top-level keys stay the primary window
                # byte-identical; `windows` publishes whatever pair the payload
                # actually states, the moment it states one.
                windows = [{"usedPercent": out["usedPercent"],
                            "windowMinutes": out["windowMinutes"],
                            "resetsAt": out["resetsAt"]}]
                secondary = rl.get("secondary")
                if isinstance(secondary, dict) and secondary.get("window_minutes") is not None:
                    sec_used = secondary.get("used_percent")
                    windows.append({"usedPercent": float(sec_used) if isinstance(sec_used, (int, float)) else None,
                                    "windowMinutes": secondary.get("window_minutes"),
                                    "resetsAt": secondary.get("resets_at")})
                out["windows"] = windows
                # Vendor STATEMENTS we were dropping. Copied verbatim, never
                # interpreted: `balance: "0"` is not "exhausted subscription", it is
                # what the vendor said about credits, which are a separate pot.
                if isinstance(rl.get("plan_type"), str) and rl["plan_type"].strip():
                    out["planType"] = rl["plan_type"].strip()
                credits = rl.get("credits")
                if isinstance(credits, dict):
                    kept = {k: credits[k] for k in ("has_credits", "unlimited", "balance")
                            if k in credits}
                    if kept:
                        out["credits"] = kept
                return out
    return None


# Rolling windows the ACTIVITY reader reports, in minutes (5h / 24h / week).
ACTIVITY_WINDOWS = (300, 1440, 10080)


def _codex_rollout_activity(home: Path | None = None,
                            now_ts: float | None = None) -> list[dict[str, Any]]:
    """Tokens the local codex rollouts show CONSUMED in each rolling window.

    This is ACTIVITY, never quota: it counts what these files recorded on this
    machine, which is neither the account's usage nor anything the vendor
    declared. It exists because codex (plan `plus`) publishes only the weekly
    quota window, so the gauge had nothing true to show for the short ones —
    and the alternative, inferring a budget from `used_percent` and dividing,
    would print a percentage the vendor never stated.

    `last_token_usage` per event, NEVER `total_token_usage`: the latter is
    cumulative per session and summing it double-counts every turn. Events are
    deduplicated by timestamp (the same event can be re-emitted).

    Cost: only files whose mtime is at/after the OLDEST cutoff are opened, and
    each is streamed ONCE feeding every accumulator. Measured on the local
    corpus: 179 rollouts, 74 eligible for 7d; ~99ms median vs ~8ms for the
    single-file quota read. `fuel probe` runs at the top of every loop round,
    so this bound is the feature, not an optimization.
    """
    env_home = os.environ.get("HARNESS_CODEX_HOME")
    base = (home or (Path(env_home) if env_home else _CODEX_HOME)) / "sessions"
    now = now_ts if now_ts is not None else dt.datetime.now(dt.timezone.utc).timestamp()
    oldest = now - max(ACTIVITY_WINDOWS) * 60
    try:
        candidates = [p for p in base.glob("*/*/*/rollout-*.jsonl")
                      if p.stat().st_mtime >= oldest]
    except OSError:
        return []
    seen: dict[str, int] = {}
    for f in candidates:
        try:
            with f.open("r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    if '"last_token_usage"' not in line:
                        continue
                    try:
                        evt = json.loads(line)
                    except ValueError:
                        continue
                    ts = evt.get("timestamp")
                    usage = _find_key(evt, "last_token_usage")
                    total = usage.get("total_tokens") if isinstance(usage, dict) else None
                    if not isinstance(ts, str) or not isinstance(total, int) or total < 0:
                        continue
                    when = parse_iso_datetime(ts)
                    if when is None:
                        continue
                    age = now - when.timestamp()
                    if age < 0 or age > max(ACTIVITY_WINDOWS) * 60:
                        continue  # future stamp or out of range: not evidence
                    seen[ts] = total
        except OSError:
            continue
    if not seen:
        return []
    rows: list[dict[str, Any]] = []
    for minutes in ACTIVITY_WINDOWS:
        cutoff = now - minutes * 60
        hits = [n for ts, n in seen.items()
                if (w := parse_iso_datetime(ts)) is not None and w.timestamp() >= cutoff]
        rows.append({"windowMinutes": minutes, "totalTokens": sum(hits), "turns": len(hits)})
    return rows


def _find_key(node: Any, key: str) -> Any:
    """Depth-first search for `key` in nested dicts/lists (bounded by event size)."""
    if isinstance(node, dict):
        if key in node:
            return node[key]
        for v in node.values():
            hit = _find_key(v, key)
            if hit is not None:
                return hit
    elif isinstance(node, list):
        for v in node:
            hit = _find_key(v, key)
            if hit is not None:
                return hit
    return None


def probe_codex(path: str | None = None, home: Path | None = None) -> dict[str, Any]:
    """`codex login status` (auth) + the newest session-rollout rate_limits
    (the REAL gauge, zero-cost file read). No rollout -> pct null, honest detail."""
    label = "CODEX"
    try:
        res = _run("codex", ["login", "status"], path)
    except TimeoutExpired:
        return _unavailable(label, "probe timed out", PROBE_TIMEOUT_S * 1000, "timeout")
    except OSError as exc:
        return _unavailable(label, "probe failed", 0, f"{type(exc).__name__}: {exc}")
    if res is None:
        return _unavailable(label, "codex CLI not found", 0, "cli-not-found")
    rc, out, err, ms = res
    # codex prints "Logged in using X" to STDERR (stdout empty); prefer stdout, fall back.
    combined = (out or "").strip() or (err or "").strip()
    lines = [ln.strip() for ln in combined.splitlines() if ln.strip()]
    line = lines[0] if lines else ""
    if rc != 0 or not line.lower().startswith("logged in"):
        return _unavailable(label, (line or "not logged in")[:120], ms, (line or err or "logged-out").strip()[:200])
    idx = line.lower().find("using ")
    method = line[idx + 6:].strip().rstrip(".") if idx >= 0 else "unknown"
    value = {"loggedIn": True, "method": method, "raw": line}
    # Local ACTIVITY rides beside the quota, never inside it. Its scan time joins
    # probeMs so the gauge's own cost stays visible in the reading it produces.
    started = time.monotonic()
    activity = _codex_rollout_activity(home)
    if activity:
        value["activity"] = activity
        ms += (time.monotonic() - started) * 1000
    limits = _codex_rollout_limits(home)
    if limits is None:
        return _fresh(value, label, None, f"{method or 'logged in'} (no session rollout yet)", ms)
    pct = round(100.0 - limits["usedPercent"], 1)
    value["rateLimits"] = limits
    win = _window_label(limits.get("windowMinutes"))
    now_ts = dt.datetime.now().timestamp()
    age_s = max(0.0, now_ts - limits["rolloutMtime"])
    age_min = int(age_s / 60)
    # The rollout is the MEASUREMENT; re-reading it is not a new one. Stamping the
    # real measurement time keeps `capturedAt` (when the file was read) from passing
    # for freshness it does not have.
    value["measuredAt"] = dt.datetime.fromtimestamp(limits["rolloutMtime"]).isoformat(timespec="seconds")
    reset = ""
    if isinstance(limits.get("resetsAt"), (int, float)):
        reset = " · resets " + dt.datetime.fromtimestamp(limits["resetsAt"]).strftime("%b %d, %I:%M%p")
    if age_s > STALE_SECONDS:
        # MEASURED 2026-07-27: a 54h-old rollout reported "week 1% left, resets Jul 28"
        # as `fresh`; the window had already rolled over and the true value was 100%.
        # An expired window's percentage is not a stale number, it is a WRONG one, and
        # every machine consumer reads `pct` — the delegation-fuel rule routes lanes by
        # it. That reading cost a day of deferred work before it was caught. Unknown
        # routes correctly (the reader checks); a confident wrong number does not.
        # `resets_at` cannot rescue it: here `now` was still BEFORE the dead window's
        # advertised reset, so only the rollout's AGE detects this.
        return _fresh(value, label, None,
                      f"no current reading · rollout {age_min}m old{reset} (that window may have "
                      f"expired) · one `codex exec` turn mints a fresh one · {method}", ms)
    detail = f"{win} {pct:g}% left{reset} · rollout {age_min}m old · {method}"
    # The second window of the pair (when the payload carried one) joins the detail
    # verbatim — same used->left convention as pct, labelled by ITS window_minutes.
    extra = limits.get("windows", [])[1:]
    if extra and isinstance(extra[0].get("usedPercent"), (int, float)):
        detail += f" · {_window_label(extra[0].get('windowMinutes'))} {100 - extra[0]['usedPercent']:g}% left"
    return _fresh(value, label, pct, detail, ms)


_KIMI_HOME = Path.home() / ".kimi-code"
_KIMI_USAGES = "https://api.kimi.com/coding/v1/usages"


def _kimi_cred(home: Path | None = None) -> dict[str, Any] | None:
    """The CLI's newest usable credential record (HARNESS_KIMI_HOME overrides for
    hermetic scenarios; an explicit `home` wins over both). Read in-process ONLY:
    no field of it is ever stored, logged, or written to the canonical state —
    callers take the one value they need and drop the rest.

    We deliberately do NOT refresh the token ourselves, though the record carries
    a `refresh_token` (INVESTIGATED 2026-07-30, owner asked). The kimi CLI's
    refresh ROTATES it (kimi-code's own coreProcessService test pins the rotated
    pair coming back), so a refresh we performed and did not persist would burn
    the stored credential and log the OWNER out of their CLI — and persisting it
    would make this read-only gauge a writer of someone else's credential store,
    racing any live kimi process. The turn-based mint costs ~1 request unit of
    100; that is the cheaper hazard by a wide margin."""
    env_home = os.environ.get("HARNESS_KIMI_HOME")
    base = (home or (Path(env_home) if env_home else _KIMI_HOME)) / "credentials"
    try:
        files = sorted((p for p in base.rglob("*") if p.is_file()),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        tok = data.get("access_token") if isinstance(data, dict) else None
        if isinstance(tok, str) and len(tok) > 20:
            return data
    return None


def _kimi_expired_for(cred: dict[str, Any] | None) -> int | None:
    """Seconds SINCE the access token's own `expires_at` (None when absent,
    unparseable, or still valid). MEASURED 2026-07-30: the CLI mints a 1h token
    (`expires_in` 3600), which is why this gauge reads stale so often — the
    reason is a clock, not a fault, and the operator deserves to see which."""
    exp = (cred or {}).get("expires_at")
    if not isinstance(exp, (int, float)) or isinstance(exp, bool):
        return None
    # The credential file is UNTRUSTED input (json.loads accepts NaN/Infinity by
    # default, and a corrupted store can carry an absurd int). Without this guard
    # the arithmetic below raises ValueError/OverflowError — and probe_all's
    # per-vendor loop has no try/except, so ONE bad byte in kimi's credential
    # would take down the whole pass, every vendor with it (reckon 2026-07-30,
    # reproduced end-to-end). The module's law is that a broken vendor becomes a
    # row, never an exception.
    try:
        if not math.isfinite(exp):
            return None
        # the store writes epoch SECONDS (measured); tolerate a ms stamp too.
        secs = exp / 1000 if exp > 1e11 else exp
        delta = int(time.time() - secs)
    except (ValueError, OverflowError):
        return None
    return delta if delta > 0 else None


def _kimi_pct(win: Any) -> float | None:
    """remaining/limit as gas-left %, falling back to (limit-used)/limit — the
    API DROPS `remaining` on an exhausted window (measured 2026-07-29: the
    capped 5h detail carried only limit/used, so the binding signal vanished
    from the gauge exactly when it mattered). None only when neither pair is
    present — never a fabricated number."""
    try:
        lim = float(win["limit"])
    except (KeyError, TypeError, ValueError):
        return None
    if lim <= 0:
        return None
    try:
        rem = float(win["remaining"])
    except (KeyError, TypeError, ValueError):
        try:
            rem = lim - float(win["used"])
        except (KeyError, TypeError, ValueError):
            return None
    return round(100.0 * max(rem, 0.0) / lim, 1)


def probe_kimi(path: str | None = None, getter=None, home: Path | None = None) -> dict[str, Any]:
    """`GET /coding/v1/usages` with the CLI's OAuth token -> the REAL subscription
    gauge (endpoint surfaced by the community tracker Golden0Voyager/kimi-code-usage;
    MEASURED 2026-07-28: weekly + 5h windows with limit/used/remaining/resetTime,
    plus a parallel cap). pct = the BINDING window: min(weekly, short-window)
    remaining% (owner 2026-07-29 — the 5h cap 403'd a live lane while weekly
    read 99%; the gauge must match the TUI /usage panel, which shows BOTH bars). A 401 means the token went stale since the last CLI
    turn (any real kimi call refreshes it), so the row stays FRESH with pct=null
    and an honest detail — the codex no-current-reading rule, not 'unavailable'.
    No token at all -> `kimi --version` liveness. userId/businessId never reach
    the stored value (the claude PII-strip rule)."""
    label = "KIMI"
    cred = _kimi_cred(home)
    token = cred.get("access_token") if cred else None
    if token is None:
        try:
            res = _run("kimi", ["--version"], path)
        except TimeoutExpired:
            return _unavailable(label, "probe timed out", PROBE_TIMEOUT_S * 1000, "timeout")
        except OSError as exc:
            return _unavailable(label, "probe failed", 0, f"{type(exc).__name__}: {exc}")
        if res is None:
            return _unavailable(label, "kimi CLI not found", 0, "cli-not-found")
        rc, out, err, ms = res
        ver = (out or "").strip().splitlines()[0].strip() if (out or "").strip() else ""
        if rc == 0 and ver:
            return _fresh({"version": ver}, label, None, f"v{ver} · not logged in (no oauth token)", ms)
        return _unavailable(label, "kimi CLI errored", ms, (err or out or "no output").strip()[:200])
    t0 = time.monotonic()
    try:
        status, body = (getter or _http_get)(_KIMI_USAGES, token, PROBE_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 — transport failure is 'unavailable', never a crash
        ms = int((time.monotonic() - t0) * 1000)
        return _unavailable(label, "unreachable", ms, f"{type(exc).__name__}: {exc}"[:200])
    ms = int((time.monotonic() - t0) * 1000)
    if status == 401:
        # STRUCTURED, not prose: `_needs_mint` used to sniff this branch by
        # substring (reckon 2026-07-30 nit — two independently-written literals
        # that a reword would silently divorce). The flag IS the signal now; the
        # detail is free to say whatever reads best to a human.
        aged = _kimi_expired_for(cred)
        why = f"token expired {aged // 60}m ago" if aged is not None else "token stale"
        return _fresh({"tokenStale": True}, label, None,
                      f"{why} · no current reading · one kimi turn mints a fresh one", ms)
    try:
        doc = json.loads(body) if body else {}
    except ValueError:
        doc = {}
    week = doc.get("usage") if isinstance(doc, dict) else None
    pct = _kimi_pct(week)
    if pct is None:
        return _unavailable(label, f"usages endpoint gave no parseable panel (http {status})",
                            ms, f"http-{status}")
    detail = f"week {pct:g}% left"
    for row in (doc.get("limits") or []):
        d5 = (row or {}).get("detail")
        p5 = _kimi_pct(d5)
        if p5 is not None:
            win = (row or {}).get("window") or {}
            dur = win.get("duration")
            unit = ("h" if isinstance(dur, int) and win.get("timeUnit") == "TIME_UNIT_MINUTE"
                    and dur % 60 == 0 else "m")
            span = f"{dur // 60}{unit}" if unit == "h" else f"{dur}{unit}"
            detail += f" · {span} {p5:g}%"
            pct = min(pct, p5)  # the short window binds dispatch just as hard
            if p5 <= 0:
                detail += " EXHAUSTED"
                r5 = str((d5 or {}).get("resetTime") or "")
                if r5:
                    try:
                        ts5 = dt.datetime.fromisoformat(r5.replace("Z", "+00:00"))
                        detail += " · window resets " + ts5.astimezone().strftime("%H:%M")
                    except ValueError:
                        pass
            break
    reset = str((week or {}).get("resetTime") or "")
    if reset:
        try:
            ts = dt.datetime.fromisoformat(reset.replace("Z", "+00:00"))
            detail += " · resets " + ts.astimezone().strftime("%b %d")
        except ValueError:
            pass
    level = ((doc.get("user") or {}).get("membership") or {}).get("level")
    value: dict[str, Any] = {"usage": week, "limits": doc.get("limits"),
                             "parallel": doc.get("parallel")}
    if isinstance(level, str) and level.startswith("LEVEL_"):
        value["membership"] = level
        detail += f" · {level[6:].lower()}"
    return _fresh(value, label, pct, detail, ms)


# -- antigravity (agy): the `/usage` TUI panel, driven headless via ConPTY ----
#
# agy exposes NO usage CLI subcommand and its `/usage` slash command runs ONLY in the
# interactive TUI — print mode (`agy -p "/usage"`) treats it as a PROMPT and the model
# hallucinates an answer, spending a real turn. So the real Models&Quota panel is read
# the way a human reads it: drive the TUI headless in a pseudo-console (pywinpty/
# ConPTY), accept the folder-trust prompt for the throwaway cwd, run `/usage`, scrape
# the panel. The seat spawns gemini-3.6-flash, so the GEMINI MODELS group is the binding
# window; its 5h + weekly REMAINING% ride the standard slots exactly like codex.
# ponytail: TUI-scrape is inherently version-fragile (trust wording / panel layout can
# shift with the agy CLI) — replace with a real endpoint the day agy or Google Code
# Assist exposes one (agy's own quota_manager fetches it over OAuth internally). The
# PARSER is unit-pinned on a captured fixture so a layout drift fails LOUD, not silent;
# every failure path degrades to `agy models` reachability, never a crash.
_AGY_MODEL = "gemini-3.6-flash-low"   # cheapest probe seat; /usage renders regardless of model
_AGY_BOOT_S = 30                      # sign-in can lag; the driver watches for signals, not clocks
_AGY_HARD_CAP_S = 90                  # ABSOLUTE wall-clock cap: a watchdog kills the child past this
_AGY_GROUP = re.compile(r"^\s*([A-Z][A-Z /]*MODELS)\s*$", re.M)


def _strip_ansi(s: str) -> str:
    s = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", s)          # CSI (colours, cursor moves)
    s = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", s)  # OSC (title etc.)
    s = re.sub(r"\x1b[()][AB0-2]", "", s)                    # charset selects
    # 2-byte escapes (ESC 7/8/c/=/> M/D/E …) BEFORE the C0 pass: else the lone ESC is
    # dropped and its partner is orphaned — `\x1b7` -> `7` would corrupt an adjacent
    # quota digit (audit F1). ESC + any printable is drained as a pair here.
    s = re.sub(r"\x1b[ -~]", "", s)
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", s)    # remaining C0 controls (incl lone ESC)


def _drive_agy_usage(path: str | None = None, model: str = _AGY_MODEL) -> str | None:
    """Drive the agy TUI headless in a ConPTY and return the stripped `/usage` panel
    text, or None when the seat is unreachable / pywinpty is absent / it times out.
    Signal-driven (not fixed sleeps for correctness): accept the trust prompt, run
    /usage, wait for the 'Models & Quota' panel. cwd is an EMPTY temp dir so no project
    capsule rides along (the mint-turn rule). Bounded THREE ways so it never hangs the
    round-open probe: per-wait timeouts, an ABSOLUTE-deadline watchdog thread that force
    kills the child even if the main flow stalls, and a force terminate in finally.
    Never raises."""
    try:
        from winpty import PtyProcess
    except Exception:  # noqa: BLE001 — no ConPTY backend is a valid 'no reading', not a crash
        return None
    import tempfile
    exe = shutil.which("agy", path=path)
    if not exe:
        return None
    cwd = tempfile.mkdtemp(prefix="agy-fuel-")
    buf: list[str] = []
    stop = threading.Event()
    try:
        p = PtyProcess.spawn([exe, "--model", model], cwd=cwd, dimensions=(60, 200))
    except Exception:  # noqa: BLE001
        shutil.rmtree(cwd, ignore_errors=True)
        return None

    def _reader() -> None:
        while not stop.is_set():
            try:
                d = p.read(4096)
            except (EOFError, OSError):
                break
            buf.append(d) if d else time.sleep(0.03)

    def _wait(pat: str, timeout: float) -> bool:
        rx, end = re.compile(pat, re.I), time.time() + timeout
        while time.time() < end and not stop.is_set():
            if rx.search(_strip_ansi("".join(buf))):
                return True
            time.sleep(0.2)
        return False

    def _watchdog() -> None:
        # ABSOLUTE cap: kill the child past the hard deadline even if the main flow (or
        # PtyProcess itself) stalls, so a hung sign-in/TTY can never wedge `fuel probe`.
        if not stop.wait(_AGY_HARD_CAP_S):
            try:
                if p.isalive():
                    p.terminate(force=True)
            except Exception:  # noqa: BLE001
                pass
            stop.set()   # unblock the main _wait loops immediately, don't idle to their own timeouts

    threading.Thread(target=_reader, daemon=True).start()
    threading.Thread(target=_watchdog, daemon=True).start()
    text = ""
    try:
        if _wait(r"trust this folder|do you trust", _AGY_BOOT_S):
            p.write("\r")                       # accept the pre-selected "Yes, I trust"
        _wait(r"for shortcuts|Antigravity CLI \d", 15)   # REPL ready
        p.write("/usage")                       # autocomplete shows "/usage View model quota usage"
        time.sleep(0.8)
        p.write("\r")                           # select + run
        if not _wait(r"Models & Quota|Limit Remaining", 20):
            p.write("\r")                       # a 2nd Enter if the first only selected
            _wait(r"Models & Quota|Limit Remaining", 8)
        text = _strip_ansi("".join(buf))
    except Exception:  # noqa: BLE001 — a mid-drive write/read failure returns what was scraped
        text = _strip_ansi("".join(buf))        # (so "never raises" holds; probe treats "" as unread)
    finally:
        try:
            if p.isalive():
                p.terminate(force=True)
        except Exception:  # noqa: BLE001
            pass
        try:
            p.close()                           # release the ConPTY pseudo-console handle (Windows
        except Exception:  # noqa: BLE001       # leaks it on kill-only, wedging the next probe)
            pass
        stop.set()                              # releases the reader + watchdog waits
        time.sleep(0.1)                         # let the child drop its cwd handle before rmtree
        for _ in range(3):                      # empty temp dir; a locked-handle race just retries
            shutil.rmtree(cwd, ignore_errors=True)
            if not os.path.isdir(cwd):
                break
            time.sleep(0.2)
    return text if "Limit Remaining" in text else None


def _agy_window(block: str, label: str) -> dict[str, Any] | None:
    """After `label`, the next 'NN.NN%' is the REMAINING pct and the next 'Refreshes in
    <t>' is the reset; 'Quota available' with no percent means a full 100%."""
    i = block.find(label)
    if i < 0:
        return None
    tail = block[i + len(label): i + len(label) + 160]
    pm = re.search(r"(\d+(?:\.\d+)?)\s*%", tail)
    if not pm:
        return {"remaining": 100.0, "refresh": None} if "Quota available" in tail else None
    rm = re.search(r"Refreshes in\s*([0-9hmd ]+?)(?:\n|·|$)", tail)
    return {"remaining": float(pm.group(1)), "refresh": rm.group(1).strip() if rm else None}


def _parse_agy_usage(text: str, group_key: str = "GEMINI") -> dict[str, Any] | None:
    """Parse the agy `/usage` panel into {weekly, fiveHour} REMAINING for the model GROUP
    the seat runs on (we spawn gemini-3.6-flash -> GEMINI MODELS). None when the group or
    both windows are absent (not signed in, panel layout drifted)."""
    heads = list(_AGY_GROUP.finditer(text))
    block = None
    for i, m in enumerate(heads):
        if group_key in m.group(1):
            end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
            block = text[m.start():end]
            break
    if block is None:
        return None
    wk, fh = _agy_window(block, "Weekly Limit Remaining"), _agy_window(block, "Five Hour Limit Remaining")
    return {"weekly": wk, "fiveHour": fh} if (wk or fh) else None


def probe_antigravity(path: str | None = None, driver=None) -> dict[str, Any]:
    """agy quota via the `/usage` TUI panel driven headless (ConPTY). The seat spawns
    gemini-3.6-flash, so the GEMINI MODELS group is the binding window: 5h + weekly
    REMAINING% ride the standard slots (codex shape) and pct = the SMALLER of the two
    (the binding window, the kimi rule). When the drive can't read the panel (pywinpty
    absent / not signed in / timeout / layout drift) the row is UNAVAILABLE with an
    honest detail — never a fabricated reading and never invented 5h/wk gaps. `driver`
    is injected by the tests; live it is `_drive_agy_usage`."""
    label = "ANTIGRAVITY"
    t0 = time.monotonic()
    try:
        panel = (driver or _drive_agy_usage)(path)
    except Exception:  # noqa: BLE001 — a TUI-drive failure is 'no reading', never a crash
        panel = None
    ms = int((time.monotonic() - t0) * 1000)
    parsed = _parse_agy_usage(panel) if panel else None
    if not (parsed and (parsed.get("weekly") or parsed.get("fiveHour"))):
        return _unavailable(label, "agy /usage panel unreadable (agy signed out / pywinpty "
                            "absent / TUI timeout)", ms, "usage-unreadable")
    wk, fh = parsed.get("weekly"), parsed.get("fiveHour")
    windows = []
    if fh:
        windows.append({"windowMinutes": 300, "usedPercent": 100.0 - fh["remaining"],
                        "refresh": fh.get("refresh")})
    if wk:
        windows.append({"windowMinutes": 10080, "usedPercent": 100.0 - wk["remaining"],
                        "refresh": wk.get("refresh")})
    pct = min(w["remaining"] for w in (fh, wk) if w)   # binding window (the kimi rule)
    parts = []
    if wk:
        parts.append(f"week {wk['remaining']:g}% left"
                     + (f" · resets {wk['refresh']}" if wk.get("refresh") else ""))
    if fh:
        parts.append(f"5h {fh['remaining']:g}%"
                     + (f" · resets {fh['refresh']}" if fh.get("refresh") else ""))
    return _fresh({"group": "GEMINI", "rateLimits": {"windows": windows}},
                  label, float(pct), " · ".join(parts), ms)


_PROBERS = {"claude": probe_claude, "codex": probe_codex, "kimi": probe_kimi,
            "antigravity": probe_antigravity}


# -- openai-compatible HTTP vendors (SPEC-168 v1.1) ---------------------------

_LOOPBACK = {"localhost", "127.0.0.1", "::1"}
_ARG_KEYS = {"--base-url": "baseUrl", "--api-key-env": "keyEnv",
             "--allow-hosts": "allowHosts", "--keyless-hosts": "keylessHosts"}


def http_vendor_configs(root: Path = ROOT) -> dict[str, dict[str, str]]:
    """Every `type: http` executor, read from the SAME commandTemplate the worker
    spawn uses (`tools/openai_worker.py --base-url X --api-key-env Y ...`). Parsing
    the template rather than a second config block is what keeps the gauge probing
    the endpoint the lane would actually get: one source, no drift."""
    data = read_json(Path(root) / ".harness/routing/executors.json", None)
    execs = (data or {}).get("executors") if isinstance(data, dict) else None
    out: dict[str, dict[str, str]] = {}
    for name, ex in (execs or {}).items():
        if not isinstance(ex, dict) or ex.get("type") != "http":
            continue
        tmpl = ex.get("commandTemplate") or []
        cfg = {key: str(tmpl[i + 1]) for i, tok in enumerate(tmpl)
               if (key := _ARG_KEYS.get(str(tok))) and i + 1 < len(tmpl)}
        out[str(name)] = cfg
    return out


def _api_key(key_env: str) -> str | None:
    """Vault first, env second — the same order the worker dispatch uses. NEVER
    returned to a caller or logged; only its presence reaches the state."""
    try:
        from harness_lib import keys_vault
        if key := keys_vault.vault_get(key_env):
            return key
    except Exception:  # noqa: BLE001 — no vault backend is a valid state, not an error
        pass
    return os.environ.get(key_env) or None


def _http_get(url: str, key: str | None, timeout: int) -> tuple[int, str]:
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"} if key else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — host allowlisted above
            # 1 MiB, not 8 KiB: NVIDIA's model list truncated at 8 KiB, the JSON failed
            # to parse, and the gauge printed "0 models" — a number nobody counted
            # (measured 2026-07-27). Still bounded; a hostile body cannot exhaust memory.
            return resp.status, resp.read(1 << 20).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""


def probe_http(name: str, cfg: dict[str, str], getter=None,
               timeout: int = PROBE_TIMEOUT_S) -> dict[str, Any]:
    """`GET {base-url}/models` — the only quota-adjacent surface an openai-compatible
    provider exposes. NVIDIA Build and Gemini publish no balance endpoint, so a
    reachable provider reports `pct: null` with an honest detail; the ONE real number
    is the provider's own 402 (credits exhausted), which is vendor truth and lands as
    `pct: 0.0`. That is the spec's declared v1.1 backstop, not an estimate.

    The egress allowlist and the keyless rule are re-applied here from the executor's
    own flags: a gauge that reaches a host the worker may not reach would be measuring
    a lane that cannot run (SPEC-165 R6)."""
    label = name.upper()
    base, key_env = cfg.get("baseUrl"), cfg.get("keyEnv") or "OPENAI_API_KEY"

    def gone(detail: str, probe_ms: int, error: str) -> dict[str, Any]:
        """An unavailable HTTP row that still says WHICH KIND of provider it is.
        The bare `_unavailable` value is `{}`, so a reader (the GUI fuel carousel)
        could not tell an unreachable api provider from a missing CLI vendor and
        dropped it — the operator lost the row exactly when it mattered. baseUrl is
        the discriminator every http row carries (may be "" when none is configured);
        CLI vendors keep the empty value."""
        row = _unavailable(label, detail, probe_ms, error)
        row["value"] = {"baseUrl": base or ""}
        return row

    if not base:
        return gone("no base URL in the executor commandTemplate", 0, "no-base-url")
    from urllib.parse import urlparse
    host = (urlparse(base).hostname or "").lower()
    allow = {h.strip().lower() for h in (cfg.get("allowHosts") or "").split(",") if h.strip()}
    if allow and host not in allow:
        return gone(f"egress denied: {host} not allowlisted", 0, "egress-denied")
    keyless_ok = host in _LOOPBACK | {h.strip().lower()
                                      for h in (cfg.get("keylessHosts") or "").split(",") if h.strip()}
    key = _api_key(key_env)
    if not key and not keyless_ok:
        return gone(f"no API key ({key_env})", 0, "no-api-key")
    t0 = time.monotonic()
    try:
        status, body = (getter or _http_get)(base.rstrip("/") + "/models", key, timeout)
    except Exception as exc:  # noqa: BLE001 — every transport failure is 'unavailable', never a crash
        ms = int((time.monotonic() - t0) * 1000)
        return gone("unreachable", ms, f"{type(exc).__name__}: {exc}"[:200])
    ms = int((time.monotonic() - t0) * 1000)
    value = {"baseUrl": base, "keyEnv": key_env, "keyPresent": bool(key), "httpStatus": status}
    if status == 402:  # the provider itself says the tank is empty — a REAL zero
        return _fresh(value, label, 0.0, "credits exhausted (402)", ms)
    if status == 429:
        return _fresh(value, label, None, "rate-limited (429) · quota not exposed", ms)
    if status in (401, 403):
        return gone(f"key rejected ({status})", ms, f"http-{status}")
    if status != 200:
        return gone(f"unexpected HTTP {status}", ms, f"http-{status}")
    try:  # an unparseable body still proves reachability — it must NOT become "0 models"
        models = len(json.loads(body).get("data") or [])
    except (ValueError, AttributeError, TypeError):
        models = None
    if models is not None:
        value["models"] = models
    count = f" · {models} models" if models is not None else ""
    return _fresh(value, label, None, f"reachable{count} · quota not exposed", ms)


def all_vendors(root: Path = ROOT) -> tuple[str, ...]:
    """The CLI vendors plus every http executor. Config-derived on purpose: a new
    executor enters the gauge by being configured, not by editing a tuple here
    (owner 2026-07-27 — the gauge has to pull every vendor, and the load-balance
    door SPEC-168 opened reads THIS list). An executor with neither a prober nor an
    http config (the `generic` placeholder) is not a vendor and never appears."""
    return (*VENDORS, *sorted(http_vendor_configs(root)))


# -- mint (owner grant 2026-07-30): buy a fresh reading with the cheapest turn --
#
# The codex/kimi gauges are PASSIVE reads (rollout file / oauth'd endpoint), so
# between CLI turns they honestly decay to "no current reading" — the remedy the
# details name ("one turn mints a fresh one") is exactly what `probe --mint`
# runs. Three owner lashings, each pinned by scenario teeth (vf-16): CHEAPEST
# model, LOWEST reasoning effort, and an EMPTY temp cwd so no project
# capsule/AGENTS.md rides along (a capsule-loaded codex turn measured 21,840
# tokens on 2026-07-30; the bare turn is the point).
#
# DEFAULT-PATH POLICY (owner 2026-08-07, revising the 2026-07-30 "always mint-free"
# call for kimi ONLY): kimi's oauth token lives 1h (_kimi_expired_for), so left
# passive its gauge reads stale almost always and the lane is never routed to —
# the "economy" of not minting actually costs the whole kimi lane. A kimi mint
# costs ~1/100 request units and SELF-THROTTLES to ~1x/h (the minted token stays
# valid 1h, so probes inside that window read it and skip the mint), so every
# probe — round-open AND the GUI fuel-probe — refreshes a stale kimi before
# reporting. codex mint burns a ChatGPT turn with no such throttle (its staleness
# is rollout age, not a clock), so it stays --mint opt-in, never automatic.
#
# The 1x/h self-throttle holds on mint SUCCESS (a fresh token skips the next hour).
# ponytail: no cooldown on FAILED mints — a present-but-wedged kimi CLI is retried
# every probe (each attempt hard-capped at MINT_TIMEOUT_S; the common dead-token
# case rc!=0-fails fast). Add a lastMintAttempt cooldown in the kimi row if a
# broken CLI's retries measurably hurt round-open cadence.
MINT_TIMEOUT_S = 180  # a real (tiny) turn, not a status read; still hard-bounded
_MINT_PROMPT = "ok"   # the one-word turn; no project prompt/capsule is ever passed
# codex: gpt-5.4-mini is the cheapest slug in the local models_cache; effort is
# `low` because `minimal` is structurally refused by exec ("cannot be used with
# reasoning.effort 'minimal': web_search", and the tool survives both
# `-c tools.web_search=false` and `--disable web_search` — all three measured
# 2026-07-30). low + empty cwd measured 14,319 tokens vs 21,840 capsule-loaded.
_MINT_CODEX_ARGS = ["exec", "--sandbox", "read-only", "--skip-git-repo-check",
                    "-m", "gpt-5.4-mini", "-c", "model_reasoning_effort=low",
                    _MINT_PROMPT]
# kimi: kimi-for-coding (K2.7 base) over the k3-256k flagship default; the CLI
# exposes no effort knob (`kimi --help` 2026-07-30) — cheapest model is the lever.
_MINT_KIMI_ARGS = ["-p", _MINT_PROMPT, "-m", "kimi-code/kimi-for-coding"]


def _mint_turn(name: str, args: list[str], path: str | None) -> bool:
    """ONE bare vendor turn from an EMPTY temp cwd (removed afterwards — reckon
    2026-07-30 flagged the leak). True on rc==0; never raises."""
    import tempfile
    cwd = tempfile.mkdtemp()
    try:
        res = _run(name, args, path, timeout=MINT_TIMEOUT_S, cwd=cwd)
    except TimeoutExpired:
        return False
    except OSError:
        return False
    finally:
        shutil.rmtree(cwd, ignore_errors=True)
    return res is not None and res[0] == 0


def _mint_codex(path: str | None = None) -> bool:
    return _mint_turn("codex", list(_MINT_CODEX_ARGS), path)


def _mint_kimi(path: str | None = None) -> bool:
    import tempfile
    # --skills-dir <empty>: skill discovery is project context — the mint carries none.
    skills = tempfile.mkdtemp()
    try:
        return _mint_turn("kimi", [*_MINT_KIMI_ARGS, "--skills-dir", skills], path)
    finally:
        shutil.rmtree(skills, ignore_errors=True)


def _needs_mint(state: dict[str, Any]) -> list[str]:
    """Vendors whose gauge a mint turn can actually refresh: codex logged-in with
    no current rollout reading (absent or STALE_SECONDS-old rollout — both are the
    fresh+pct-null shape), and kimi with a 401-stale oauth token. NEVER claude
    (its /usage probe is already a live turn), never http rows (nothing to mint),
    never `unavailable` (a missing/logged-out CLI would hang on a login prompt,
    not refresh a gauge)."""
    vendors = state.get("vendors") or {}
    need: list[str] = []
    cx = vendors.get("codex") or {}
    if cx.get("status") == "fresh" and (cx.get("summary") or {}).get("pct") is None:
        need.append("codex")
    km = vendors.get("kimi") or {}
    if (km.get("status") == "fresh" and (km.get("summary") or {}).get("pct") is None
            and (km.get("value") or {}).get("tokenStale") is True):
        need.append("kimi")
    return need


def _mint_targets(stale: list[str], mint: bool, auto: bool) -> list[str]:
    """Which _needs_mint vendors to actually spend a turn on. `--mint` does all;
    the default path auto-mints kimi ONLY (cheap, 1h-self-throttled) and never
    codex (a ChatGPT turn). `auto` is False on the re-pass so it never re-mints."""
    if mint:
        return stale
    return ["kimi"] if auto and "kimi" in stale else []


def probe_all(root: Path = ROOT, path: str | None = None, getter=None,
              mint: bool = False, _auto: bool = True) -> dict[str, Any]:
    """Run EVERY configured vendor probe SEQUENTIALLY, build the canonical state,
    write it ATOMICALLY (common.write_json = temp+rename), and return it. `path`
    overrides PATH for the mock shim in tests; `getter` injects the HTTP fetch the
    same way. Never raises: a broken/absent vendor becomes an 'unavailable' row.

    All-or-nothing ON PURPOSE (owner 2026-07-28): the old per-vendor filter both
    CLOBBERED unlisted rows (fresh dict written whole) and let vendors age apart,
    so the R13 gas balancer would compare readings hours out of sync. One pass,
    one write, one updatedAt — cross-vendor skew is bounded by the pass itself.

    `mint=True` (owner 2026-07-30): after the pass, run one bare cheap turn per
    _needs_mint vendor, then ONE clean re-pass (the recursive call carries
    mint=False implicitly — no loop). A failed mint keeps the honest stale row.
    The DEFAULT path (mint=False) still auto-mints kimi ONLY when its 1h token is
    stale — see the mint policy block above; `_auto=False` on the re-pass so the
    refreshed kimi is never minted twice."""
    out: dict[str, Any] = {"schemaVersion": SCHEMA_VERSION, "vendors": {}, "updatedAt": now()}
    http_cfgs = http_vendor_configs(root)
    for v in all_vendors(root):
        if fn := _PROBERS.get(v):
            out["vendors"][v] = fn(path)
        elif v in http_cfgs:
            out["vendors"][v] = probe_http(v, http_cfgs[v], getter)
        else:
            out["vendors"][v] = _unavailable(v.upper(), "unknown vendor", 0, "unknown-vendor")
    out["updatedAt"] = now()
    write_json(Path(root) / STATE_REL, out)
    to_mint = _mint_targets(_needs_mint(out), mint, _auto)
    if to_mint:
        minted = False
        for v in to_mint:
            ok = {"codex": _mint_codex, "kimi": _mint_kimi}[v](path)
            # stderr, NOT stdout: on the default path this fires ~1x/h, and stdout
            # carries the machine-readable payload (`fuel probe --json`, the compact
            # TSV, the audit-leg re-probe's JSON) — the TSV/JSON boundary contract.
            print(f"mint {v}: {'ok' if ok else 'failed (rc!=0 / timeout / cli gone)'}",
                  file=sys.stderr)
            minted = minted or ok
        if minted:
            return probe_all(root, path, getter, _auto=False)
    return out


# -- read side (GUI/CLI never probe here) ------------------------------------

def load_state(root: Path = ROOT) -> dict[str, Any]:
    """Raw canonical state, or an empty scaffold when the file is absent."""
    data = read_json(Path(root) / STATE_REL, None)
    if not isinstance(data, dict):
        return {"schemaVersion": SCHEMA_VERSION, "vendors": {}, "updatedAt": None}
    return data


def _age_seconds(captured_at: Any, ref: dt.datetime) -> float | None:
    t = parse_iso_datetime(captured_at if isinstance(captured_at, str) else None)
    if t is None:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return (ref - t).total_seconds()


def _local_stamp(captured_at: Any) -> str:
    """'26/07 19:59' local, for the heartbeat glance. A bare age in minutes makes
    the reader do arithmetic to notice a row is rotten (owner 2026-07-26, after a
    2134m-old gauge was quoted as current). Naive reads as UTC — same convention
    as _age_seconds right below, so the two can never disagree by a timezone."""
    t = parse_iso_datetime(captured_at if isinstance(captured_at, str) else None)
    if t is None:
        return ""
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t.astimezone().strftime("%d/%m %H:%M")


def effective_status(vendor_state: dict[str, Any], ref: dt.datetime) -> str:
    """'stale' when a fresh probe is older than STALE_SECONDS, else the stored status."""
    status = str(vendor_state.get("status") or "unavailable")
    if status != "fresh":
        return status
    age = _age_seconds(vendor_state.get("capturedAt"), ref)
    return "stale" if (age is not None and age > STALE_SECONDS) else status


def show(root: Path = ROOT) -> dict[str, Any]:
    """Read the state and annotate each vendor with ageSeconds + effectiveStatus
    (the CLI convenience view; the GUI re-derives staleness client-side)."""
    state = load_state(root)
    ref = dt.datetime.now(dt.timezone.utc)
    for vs in (state.get("vendors") or {}).values():
        if isinstance(vs, dict):
            vs["ageSeconds"] = _age_seconds(vs.get("capturedAt"), ref)
            vs["effectiveStatus"] = effective_status(vs, ref)
    return state


# -- standardized slots (owner 2026-07-31: "it has to be standardized across every vendor") --
# Every vendor renders the SAME durations in the SAME order, so the eye compares
# position by position instead of hunting. A duration a vendor does not publish is
# DRAWN as a gap — never omitted (which hides the difference) and never 0% (which
# reads as exhausted). The four states are told apart by SHAPE, not colour, so the
# ASCII and the DOM say the same thing and neither depends on a palette.
SLOT_ORDER = (("5h", 300), ("wk", 10080))
SLOT_CELLS = 10
_SLOT_FILL = {"published": ("#", "-"), "derived": ("o", "-"),
              "not-published": (".", "."), "stale": ("x", "x")}


def slot_bar(state: str, pct: float | None) -> str:
    """A fixed-width ASCII cell bar. Shape carries the state: `#` published,
    `o` derived (hollow — a number we computed), `.` not published, `x` stale.
    A published 0% is `[----------]`, structurally different from a dotted gap."""
    fill, empty = _SLOT_FILL.get(state, (".", "."))
    if state in ("not-published", "stale"):
        return "[" + fill * SLOT_CELLS + "]"
    if not isinstance(pct, (int, float)):
        # A number that is not there renders as the UNKNOWN shape, never as a full
        # bar. The old fallback filled every cell — a `published` slot whose reading
        # went missing would have drawn 100% (mutation oracle, 2026-07-31).
        return "[" + _SLOT_FILL["not-published"][0] * SLOT_CELLS + "]"
    n = max(0, min(SLOT_CELLS, int(round(pct / 100 * SLOT_CELLS))))
    return "[" + fill * n + empty * (SLOT_CELLS - n) + "]"


def derive_codex_pace(value: dict[str, Any], published: dict[int, float]) -> dict[str, Any] | None:
    """Codex publishes only the weekly quota window, so the 5h slot would stay a
    gap forever. What CAN be honestly computed from what it does publish: the share
    of the IMPLIED WEEKLY BUDGET burned in the last 5h — a PACE reading, not a 5h
    quota (the vendor never stated one). Refuses unless every input is real; a
    published 5h window makes this unreachable (the caller checks first).

    Assumption, carried in the explanation and never hidden: it only holds if all
    account consumption flows through this machine's rollouts.
    """
    if 300 in published or 10080 not in published:
        return None
    used_pct = 100.0 - published[10080]
    act = {r.get("windowMinutes"): r for r in (value.get("activity") or [])
           if isinstance(r, dict)}
    week, five = act.get(10080, {}).get("totalTokens"), act.get(300, {}).get("totalTokens")
    if used_pct <= 0 or not isinstance(week, int) or not isinstance(five, int) or week <= 0:
        return None
    budget = week / (used_pct / 100.0)
    if budget <= 0:
        return None
    pace = round(100.0 * five / budget, 1)
    return {"pct": pace, "reading": f"pace ~{pace:g}% of wk",
            "explanation": (f"Derived pace: {week / 1e6:.1f}M tokens measured in 7d / "
                            f"{used_pct:g}% published weekly use = {budget / 1e6:.1f}M implied "
                            f"weekly budget; {five / 1e6:.1f}M measured in 5h / "
                            f"{budget / 1e6:.1f}M = {pace:g}% of that weekly budget. "
                            f"Assumes all account consumption flows through this "
                            f"machine's rollouts.")}


def is_api_vendor(vs: dict[str, Any]) -> bool:
    """True for an INFERENCE ENDPOINT (every `type: http` executor — gemini/nvidia
    -compat and the local one), false for a metered agent subscription. Same
    discriminator the GUI uses: `probe_http` stamps `baseUrl` on every row it writes,
    fresh or unavailable (possibly ""); a CLI vendor never carries the key. Transport,
    not name convention — a provider renamed tomorrow still classifies correctly."""
    return "baseUrl" in (vs.get("value") or {})


def vendor_slots(vs: dict[str, Any]) -> list[dict[str, Any]]:
    """The standardized slot row for one vendor: 5h, wk, then any extra window the
    vendor really publishes. Pure read over the cached state — never probes.

    EMPTY for an inference API. Standardization covers the vendors that meter a
    subscription; an openai-compatible endpoint publishes no window and never will
    (spec v1.1 "reachability, not a tank"), so drawing 5h/wk gaps for it invents an
    absence — a dotted slot says "the vendor has not said yet" when the truth is
    "there is nothing there to say" (owner 2026-07-31: "they are inference APIs,
    not vendor agents we integrate")."""
    if is_api_vendor(vs):
        return []
    value = vs.get("value") or {}
    summ = vs.get("summary") or {}
    eff = vs.get("effectiveStatus") or vs.get("status") or ""
    stale_note = None
    if eff == "stale" or value.get("tokenStale") is True:
        stale_note = summ.get("detail") or "stale reading"
    published: dict[int, float] = {}
    for w in (value.get("rateLimits") or {}).get("windows") or []:
        used, minutes = w.get("usedPercent"), w.get("windowMinutes")
        if isinstance(used, (int, float)) and isinstance(minutes, int):
            published[minutes] = max(0.0, min(100.0, 100.0 - used))
    for row in value.get("windows") or []:  # claude's own shape
        used, name = row.get("usedPct"), row.get("window")
        if not isinstance(used, (int, float)) or not isinstance(name, str):
            continue
        minutes = 300 if name == "session" else 10080 if name == "week (all models)" else None
        if minutes:
            published[minutes] = max(0.0, min(100.0, 100.0 - used))
    for row in value.get("limits") or []:  # kimi's own shape
        detail = row.get("detail") if isinstance(row, dict) else None
        minutes = ((row.get("window") or {}).get("duration") if isinstance(row, dict) else None)
        pct = _kimi_pct(detail) if isinstance(detail, dict) else None
        if isinstance(minutes, int) and pct is not None:
            published[minutes] = pct
    if isinstance(_kimi_pct(value.get("usage")), float):
        published.setdefault(10080, _kimi_pct(value.get("usage")))
    out: list[dict[str, Any]] = []
    for label, minutes in SLOT_ORDER:
        if stale_note:
            out.append({"label": label, "state": "stale", "pct": None,
                        "reading": "stale", "explanation": stale_note})
            continue
        if minutes in published:
            pct = published[minutes]
            out.append({"label": label, "state": "published", "pct": pct,
                        "reading": f"{pct:g}% left", "explanation": None})
            continue
        pace = derive_codex_pace(value, published) if minutes == 300 else None
        if pace:
            out.append({"label": label, "state": "derived", **pace})
        else:
            out.append({"label": label, "state": "not-published", "pct": None,
                        "reading": "not published", "explanation": None})
    return out


def fuel_summary(root: Path = ROOT) -> str:
    """A compact per-vendor gas block for heartbeat surfaces (checkpoint --render):
    the overseer SEES remaining gas and balances worker routing without running a
    verb. Reads the cached state only (never probes). `unavailable` vendors drop out
    (honest absence); a >2h-stale row is flagged. Returns '' when no state exists.
    Never raises — a bad read must not break the reinjection render."""
    try:
        vendors = (show(root).get("vendors") or {})
    except Exception:  # noqa: BLE001
        return ""
    rows = []
    for v, vs in vendors.items():
        if not isinstance(vs, dict):
            continue
        eff = vs.get("effectiveStatus") or vs.get("status") or "?"
        if eff == "unavailable":
            continue
        summ = vs.get("summary") or {}
        pct = summ.get("pct")
        gauge = (f"{round(pct)}% gas left" if isinstance(pct, (int, float))
                 else (summ.get("detail") or "no numeric surface"))
        age = vs.get("ageSeconds")
        bits = [b for b in (_local_stamp(vs.get("capturedAt")),
                            f"{int(age // 60)}m ago" if isinstance(age, (int, float)) else "") if b]
        aged = f", probed {' · '.join(bits)}" if bits else ""
        flag = " [STALE — reprobe]" if eff == "stale" else ""
        rows.append(f"- {summ.get('label') or v}: {gauge}{flag}{aged}")
    if not rows:
        return ""
    return ("## Vendor fuel — gas remaining (balance worker routing by this; "
            "refresh ~$0 via `python scripts/harness.py fuel probe`)\n" + "\n".join(rows))


# -- CLI verb (registered thin in harness_lib.cli_registry) ------------------

def cmd_fuel(args: Any) -> None:
    from harness_lib.common import agent_output_compact
    action = getattr(args, "action", "show") or "show"
    mint = bool(getattr(args, "mint", False))
    if mint and action != "probe":
        print("fuel: --mint requires the `probe` action", file=sys.stderr)
        raise SystemExit(2)
    if action == "probe":
        state = probe_all(ROOT, mint=True) if mint else probe_all(ROOT)
    else:
        state = show(ROOT)
    if getattr(args, "json", False):
        print(json.dumps(state, indent=2, ensure_ascii=False))
        return
    if not agent_output_compact():
        # HUMAN: the standardized gauge, same slots in the same order for every
        # vendor, state carried by SHAPE so a pipe or a colourless terminal loses
        # nothing. The derived reading's arithmetic rides a FOOTNOTE, never inline
        # beside the number (owner 2026-07-31: no wall of text next to the data).
        notes: list[str] = []
        for v, vs in (state.get("vendors") or {}).items():
            if not isinstance(vs, dict):
                continue
            label = (vs.get("summary") or {}).get("label") or v
            if is_api_vendor(vs):
                # Inference endpoints have no tank to draw: one reading line, its own
                # `api` register, and the probe's own sentence verbatim.
                eff = vs.get("effectiveStatus") or vs.get("status") or ""
                head = f"{eff} · " if eff and eff != "fresh" else ""
                print(f"{label:<15} api  {head}"
                      f"{(vs.get('summary') or {}).get('detail') or 'no reading'}")
                continue
            slots = vendor_slots(vs)
            cells = []
            for s in slots:
                mark = " *" if s["state"] == "derived" else ""
                cells.append(f"{s['label']} {slot_bar(s['state'], s['pct'])} {s['reading']}{mark}")
                if s["state"] == "derived" and s.get("explanation"):
                    notes.append(f"  * {label} {s['label']}: {s['explanation']}")
            print(f"{label:<15} " + "   ".join(cells))
        for note in notes:
            print(note)
        return
    # compact (agent consumer): one honest TSV row per vendor SLOT — the same four
    # states the human view shows, machine-readable.
    print("vendor\tslot\tstate\tbar\treading")
    for v, vs in (state.get("vendors") or {}).items():
        if not isinstance(vs, dict):
            continue
        label = (vs.get("summary") or {}).get("label") or v
        if is_api_vendor(vs):  # one `api` row, bar `n/a` — there is no window to bar
            print(f"{label}\tapi\t{vs.get('effectiveStatus') or vs.get('status') or '?'}\tn/a\t"
                  f"{(vs.get('summary') or {}).get('detail') or 'no reading'}")
            continue
        for s in vendor_slots(vs):
            print(f"{label}\t{s['label']}\t{s['state']}\t{slot_bar(s['state'], s['pct'])}\t{s['reading']}")


# -- deterministic self-check (no real vendor calls) -------------------------

def _self_check() -> None:
    import tempfile

    # hermetic kimi-credential override: an EMPTY home so no real oauth token is
    # found and the probe NEVER dials api.kimi.com from a check.
    os.environ["HARNESS_KIMI_HOME"] = tempfile.mkdtemp()
    root = Path(tempfile.mkdtemp())
    orig = globals()["_run"]

    def fake(name, args, path, timeout=PROBE_TIMEOUT_S):
        if name == "claude":  # PII (email/orgId) MUST be dropped from value
            return 0, json.dumps({"loggedIn": True, "authMethod": "claude.ai",
                                  "subscriptionType": "max", "apiProvider": "firstParty",
                                  "email": "x@y.z", "orgId": "SECRET"}), "", 7
        if name == "kimi":  # version on stdout (real shape)
            return 0, "0.29.2\n", "", 2
        return 0, "", "Logged in using ChatGPT\n", 3  # codex writes to STDERR (real shape)

    # agy /usage panel fixture (minimal shape of the real Models & Quota render).
    agy_panel = ("Models & Quota\nGEMINI MODELS\n Weekly Limit Remaining\n [##] 88.00%\n"
                 " 88% remaining · Refreshes in 5h 0m\n Five Hour Limit Remaining\n [##] 90.00%\n"
                 " 90% remaining · Refreshes in 2h 3m\nCLAUDE AND GPT MODELS\n"
                 " Weekly Limit Remaining\n 100.00%\n Quota available\n")
    orig_drive = globals()["_drive_agy_usage"]
    globals()["_run"] = fake
    globals()["_drive_agy_usage"] = lambda path=None, model=_AGY_MODEL: agy_panel
    try:
        st = probe_all(root)
    finally:
        globals()["_run"] = orig
        globals()["_drive_agy_usage"] = orig_drive
    assert st["schemaVersion"] == SCHEMA_VERSION and set(st["vendors"]) == set(VENDORS), st
    cl = st["vendors"]["claude"]
    assert cl["status"] == "fresh" and cl["summary"]["pct"] is None, cl
    # the fake returns an auth-status shape (no /usage panel) -> the fallback liveness
    # path, whose detail names the missing panel. (This case still proves PII-strip.)
    assert cl["summary"]["detail"] == "max · claude.ai (no usage panel)", cl["summary"]
    assert "email" not in cl["value"] and "orgId" not in cl["value"], cl["value"]  # PII stripped
    assert cl["value"] == {"loggedIn": True, "authMethod": "claude.ai",
                           "subscriptionType": "max", "apiProvider": "firstParty"}, cl["value"]
    cx = st["vendors"]["codex"]
    assert cx["status"] == "fresh" and cx["value"]["method"] == "ChatGPT", cx
    km = st["vendors"]["kimi"]
    assert km["status"] == "fresh" and km["summary"]["pct"] is None, km
    assert "0.29.2" in km["summary"]["detail"] and "not logged in" in km["summary"]["detail"], km
    ag = st["vendors"]["antigravity"]  # /usage TUI panel -> GEMINI group 5h+weekly remaining
    assert ag["status"] == "fresh" and ag["summary"]["pct"] == 88.0, ag  # min(wk 88, 5h 90)
    assert "week 88% left" in ag["summary"]["detail"] and "5h 90%" in ag["summary"]["detail"], ag["summary"]
    assert not is_api_vendor(ag) and len(vendor_slots(ag)) == 2, ag  # real 5h/wk bars (metered)
    slot5h = next(s for s in vendor_slots(ag) if s["label"] == "5h")
    assert slot5h["state"] == "published" and abs(slot5h["pct"] - 90.0) < 0.01, slot5h
    # unreadable panel (driver None) -> UNAVAILABLE, honest detail, never a crash / fabrication
    fb = probe_antigravity(driver=lambda p=None: None)
    assert fb["status"] == "unavailable" and fb["error"] == "usage-unreadable", fb
    assert fb["value"] == {} and fb["summary"]["pct"] is None, fb  # no fabricated reading

    # kimi quota parse: fake cred + injected getter -> real weekly pct, 5h window,
    # membership tag; userId NEVER lands in the stored value (PII-strip rule).
    kh = Path(tempfile.mkdtemp())
    (kh / "credentials").mkdir()
    (kh / "credentials" / "kimi-code.json").write_text(
        json.dumps({"access_token": "x" * 32}), encoding="utf-8")
    fake_usages = json.dumps({
        "user": {"userId": "SECRET-ID", "businessId": "B", "membership": {"level": "LEVEL_BASIC"}},
        "usage": {"limit": "100", "used": "1", "remaining": "99",
                  "resetTime": "2026-08-03T10:25:38Z"},
        "limits": [{"window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
                    "detail": {"limit": "100", "used": "2", "remaining": "98",
                               "resetTime": "2026-07-29T02:25:38Z"}}],
        "parallel": {"limit": "10"}})
    kq = probe_kimi(getter=lambda url, key, t: (200, fake_usages), home=kh)
    # pct is the BINDING window: min(weekly 99, 5h 98) = 98 (owner 2026-07-29).
    # This pin lagged at 99.0 until 2026-07-30 — the scenario's vf-14 had the truth.
    assert kq["status"] == "fresh" and kq["summary"]["pct"] == 98.0, kq
    assert "week 99% left" in kq["summary"]["detail"] and "5h 98%" in kq["summary"]["detail"], kq
    assert "basic" in kq["summary"]["detail"], kq
    assert "SECRET-ID" not in json.dumps(kq) and '"businessId"' not in json.dumps(kq), kq
    stale = probe_kimi(getter=lambda url, key, t: (401, ""), home=kh)
    assert stale["status"] == "fresh" and stale["summary"]["pct"] is None, stale
    # the 401 row's marker is STRUCTURED (what _needs_mint reads); with no
    # expires_at in this fixture the prose falls back to the generic wording.
    assert stale["value"] == {"tokenStale": True}, stale
    assert "token stale" in stale["summary"]["detail"], stale
    assert _kimi_expired_for({"expires_at": time.time() - 600}) >= 600
    assert _kimi_expired_for({"expires_at": time.time() + 600}) is None
    assert _kimi_expired_for({}) is None and _kimi_expired_for(None) is None
    # untrusted store: every junk shape reads as "no expiry known", and NONE of
    # them raises (reckon 2026-07-30 — the exception aborted the WHOLE pass, not
    # just kimi's row). json.loads accepts NaN/Infinity, so these are reachable.
    for junk in (float("nan"), float("inf"), float("-inf"), 10**400, -(10**400),
                 "1785425968", None, True, [], {}):
        assert _kimi_expired_for({"expires_at": junk}) is None, junk
    # atomic write landed, no .tmp leftover
    p = root / STATE_REL
    assert p.exists() and not list(p.parent.glob("*.tmp")), list(p.parent.iterdir())
    assert json.loads(p.read_text(encoding="utf-8"))["vendors"]["codex"]["summary"]["label"] == "CODEX"

    # fuel_summary: the heartbeat block reads the fresh state and names each available
    # vendor; an empty root yields no block (never a fabricated line).
    block = fuel_summary(root)
    assert "Vendor fuel" in block and "CLAUDE" in block and "CODEX" in block and "KIMI" in block, block
    assert fuel_summary(Path(tempfile.mkdtemp())) == "", "empty state must yield no block"

    # cli-not-found -> unavailable (empty PATH dir resolves nothing)
    empty = Path(tempfile.mkdtemp())
    un = probe_claude(path=str(empty))
    assert un["status"] == "unavailable" and un["error"] == "cli-not-found", un

    # stale derivation: a fresh row older than STALE_SECONDS reads 'stale'
    ref = dt.datetime.now(dt.timezone.utc)
    old = (ref - dt.timedelta(seconds=STALE_SECONDS + 60)).isoformat()
    assert effective_status({"status": "fresh", "capturedAt": old}, ref) == "stale"
    assert effective_status({"status": "fresh", "capturedAt": ref.isoformat()}, ref) == "fresh"
    assert effective_status({"status": "unavailable"}, ref) == "unavailable"

    # absent file -> empty scaffold, never a crash
    assert load_state(Path(tempfile.mkdtemp()))["vendors"] == {}

    # mint gating is a PURE read of the pass result: codex fresh+pct-null and kimi
    # 401-token-stale qualify; a numeric gauge, a logged-out kimi, claude, http
    # rows and `unavailable` NEVER do (a logged-out CLI would hang on login).
    def _frow(pct, detail=""):
        return {"status": "fresh", "summary": {"pct": pct, "detail": detail}}

    _km_stale = dict(_frow(None, "token expired 70m ago · …"), value={"tokenStale": True})
    assert _needs_mint({"vendors": {"codex": _frow(None, "no current reading"),
                                    "kimi": _km_stale}}) == ["codex", "kimi"]
    # prose alone never mints — only the structured flag does (reckon nit)
    assert _needs_mint({"vendors": {"kimi": _frow(None, "token stale · …")}}) == []
    assert _needs_mint({"vendors": {"codex": _frow(86.0), "kimi": _frow(None, "not logged in"),
                                    "claude": _frow(None), "gemini": _frow(None)}}) == []
    assert _needs_mint({"vendors": {"codex": {"status": "unavailable",
                                              "summary": {"pct": None, "detail": ""}}}}) == []
    assert _needs_mint({}) == []

    # default path auto-mints kimi ONLY (cheap, 1h-throttled); --mint does all;
    # codex never mints without --mint (a ChatGPT turn); the re-pass never re-mints.
    assert _mint_targets(["codex", "kimi"], mint=False, auto=True) == ["kimi"]
    assert _mint_targets(["codex", "kimi"], mint=True, auto=True) == ["codex", "kimi"]
    assert _mint_targets(["codex"], mint=False, auto=True) == []
    assert _mint_targets(["kimi"], mint=False, auto=False) == []
    assert _mint_targets([], mint=False, auto=True) == []

    # the mint turns carry the owner's three lashings in their argv (cheap model,
    # low effort, bare prompt), run from an EMPTY temp cwd — never the repo — and
    # remove their temp dirs afterwards (reckon 2026-07-30: the leak finding).
    rec: list[tuple] = []

    def _rec_run(name, args, path, timeout=PROBE_TIMEOUT_S, cwd=None):
        empty = bool(cwd) and not any(Path(cwd).iterdir())  # judged AT CALL TIME
        rec.append((name, list(args), timeout, cwd, empty))
        return 0, "", "", 1

    globals()["_run"] = _rec_run
    try:
        assert _mint_codex() and _mint_kimi()
    finally:
        globals()["_run"] = orig
    cname, cargs, cto, ccwd, cempty = rec[0]
    assert cname == "codex" and cargs == _MINT_CODEX_ARGS and cto == MINT_TIMEOUT_S, rec[0]
    assert ccwd and Path(ccwd) != ROOT and cempty, rec[0]
    assert not Path(ccwd).exists(), f"mint cwd must be cleaned up: {ccwd}"
    kname, kargs, _, kcwd, kempty = rec[1]
    assert kname == "kimi" and kargs[:len(_MINT_KIMI_ARGS)] == _MINT_KIMI_ARGS, rec[1]
    assert kargs[-2] == "--skills-dir" and not Path(kargs[-1]).exists(), rec[1]
    assert kcwd and Path(kcwd) != ROOT and kempty and not Path(kcwd).exists(), rec[1]

    # a failed/hung mint degrades to False — never raises, never blocks the pass
    def _boom(name, args, path, timeout=PROBE_TIMEOUT_S, cwd=None):
        raise TimeoutExpired(cmd=name, timeout=timeout)

    globals()["_run"] = _boom
    try:
        assert _mint_codex() is False and _mint_kimi() is False
    finally:
        globals()["_run"] = orig
    print("vendor_fuel self-check ok")


if __name__ == "__main__":
    _self_check()
