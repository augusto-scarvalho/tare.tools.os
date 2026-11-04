"""SPEC-114 supervision-panel backend: collectors, gated actions, chat bridge.

Pure backend for scripts/harness_ui.py. Three jobs:
- state_snapshot(root): one never-crash JSON dict the browser polls;
- run_action(root, action, params): the ONLY write path, every action an
  allowlisted harness subcommand run via subprocess (no second write path),
  with the same structural HITL refusal the chat REPL enforces (classify_command);
- ChatBridge: pipes to `harness.py chat`, so the chat tab inherits the REPL's
  gate ladder untouched (SPEC-111 R1/R15 no-input mode).

No sockets, no HTML here — the transport lives in harness_ui.py so this module
stays inside the harness_lib file budget.
"""
from __future__ import annotations

import collections
import datetime as dt
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

try:
    from . import chat_hud, cost_metrics, decision_inbox, model_routing, records, secret_scan
    from . import targets as targets_lib
    from .async_state import tail_lines, workflow_process_alive  # SPEC-118 tail + liveness (no bind())
    from .chat_engines import classify_command
    from .chat_setup import _load_prefs
    from .common import now, read_json, truncate_text
    from .processes import signal_process_tree
except ImportError:  # run directly for the __main__ self-check
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from harness_lib import chat_hud, cost_metrics, decision_inbox, model_routing, records, secret_scan
    from harness_lib import targets as targets_lib
    from harness_lib.async_state import tail_lines, workflow_process_alive
    from harness_lib.chat_engines import classify_command
    from harness_lib.chat_setup import _load_prefs
    from harness_lib.common import now, read_json, truncate_text
    from harness_lib.processes import signal_process_tree

_TIER_ORDER = {"critical": 0, "high": 1, "watch": 2, "info": 3}
_GATE_FAIL = {"fail", "failed", "error"}  # a last-validation status that needs the operator
QUIET_SECONDS = 90  # a running worker silent longer than this is "gone quiet" (watch rank)
# Canonical gate ladder in flow order. Hardcoded to avoid importing the heavy CLI-level
# scripts/spec_test_gate.py from harness_lib (it pulls in harness.py); keep in sync with
# spec_test_gate.GATES (SPEC-114 v12 panel gates section).
_GATE_TIERS = ["smoke", "spec-pack", "commit", "feature", "release", "product-release",
               "coverage", "graph", "workflow", "soak", "scenarios"]


# -- mtime cache for the per-tick collectors ---------------------------------
# The panel polls ~3s; worklog.json / cost-metrics.json are append-only and rarely
# change between ticks. Key each parse by the source file's (mtime_ns, size) so an
# unchanged file costs one stat instead of a full parse. A stat failure falls
# through to the loader (degrade like every collector here). Module-level, bounded
# by the handful of source paths (per root × 2).
# ponytail: unbounded dict, but keyed by absolute path — a couple entries in practice.
_STAT_CACHE: dict[str, tuple[int, int, Any]] = {}


def _cached(path: Path, loader: Any) -> Any:
    try:
        st = path.stat()
    except OSError:
        return loader()
    key = str(path)
    hit = _STAT_CACHE.get(key)
    if hit is not None and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
        return hit[2]
    val = loader()
    _STAT_CACHE[key] = (st.st_mtime_ns, st.st_size, val)
    return val


def metrics_snapshot(root: Path) -> dict[str, Any]:
    """Cost summary for /api/state costs + /api/metrics; the whole-ledger
    aggregation is cached by cost-metrics.json's mtime so a tick without a new
    delegation is a stat, not a re-parse. ALL panel caching lives here."""
    root = Path(root)
    # include_disk=False: the panel's _costs discards the disk fields, and their _dir_mb
    # walk is ~85% of the /api/state cold path (measured 2026-07-23). CLI/self_review keep
    # the default True. This kills the recon-reported 11.8s cold at the root.
    return _cached(root / cost_metrics.LEDGER_REL, lambda: cost_metrics.summarize(root, include_disk=False))


def capabilities_snapshot(root: Path) -> dict[str, Any]:
    """CAP.2 read-only GUI feed for /api/capabilities: the CAP.1 skills + MCP
    inventory (capabilities_view) in one payload, grouped client-side by scope.
    Reuses the CAP.1 snapshots verbatim; nothing re-implemented here. Cached by
    the capability manifest's mtime like metrics_snapshot — re-opening the Config
    view without an edit is a stat, not a re-scan. Degrades to empty lists, never
    a 500.
    # ponytail: cache key is the manifest only; a referencedBy edit under
    # .claude/agents or prompts won't bust it until the manifest changes — fine
    # for a read-only panel (touch the manifest or restart the panel to refresh)."""
    root = Path(root)

    def _build() -> dict[str, Any]:
        from harness_lib import capabilities_view
        try:
            return {"generatedAt": now(),
                    "skills": capabilities_view.skills_snapshot(root),
                    "mcp": capabilities_view.mcp_snapshot(root)}
        except Exception as exc:  # a broken manifest/target must not 500 the panel
            return {"generatedAt": now(), "skills": [], "mcp": [], "error": type(exc).__name__}

    return _cached(root / ".harness" / "capabilities.json", _build)


def capability_file(root: Path, skill_id: str, rel: str | None = None) -> dict[str, Any]:
    """CAP.2 read-only viewer body for ONE skill — closed to the snapshot
    inventory + path-confined by capabilities_view.skill_file (the trust
    boundary: an unknown id or a `..` escape refuses). HarnessError degrades to
    the /api/specs/file error shape, never a 500. MCP has NO file route: its
    already-masked row ships in /api/capabilities and renders client-side, so a
    declaration file's secret VALUES (target.json/services.json env) are never
    read here."""
    from harness_lib import capabilities_view
    from harness_lib.common import HarnessError
    try:
        return {"id": skill_id, "rel": rel or "SKILL.md",
                "body": capabilities_view.skill_file(root, skill_id, rel)}
    except HarnessError as exc:
        return {"error": str(exc), "id": skill_id}


# -- state snapshot (never-crash per source) ---------------------------------

def _escalations(root: Path) -> list[dict[str, Any]]:
    state = read_json(root / ".harness" / "state" / "escalations.json", {}) or {}
    resolved = set(state.get("resolvedIds") or [])
    rows: list[dict[str, Any]] = []
    for eid, rec in (state.get("raised") or {}).items():
        if eid in resolved or not isinstance(rec, dict):
            continue
        reason = str(rec.get("reason") or "").split("\n", 1)[0]
        rows.append({"id": rec.get("escalationId") or eid, "reason": reason,
                     "criticality": rec.get("criticality"),
                     "raisedAt": rec.get("raisedAt"),
                     "subject": rec.get("subject") or "self",  # esc-scoped-hitl-view
                     "suggestedProfile": rec.get("suggestedProfile")})
    rows.sort(key=lambda r: (_TIER_ORDER.get(str(r.get("criticality")), 4),
                             str(r.get("raisedAt") or "")))
    return rows


def _last_validation(root: Path) -> dict[str, Any]:
    qs = read_json(root / ".harness" / "state" / "quality-state.json", {}) or {}
    lv = qs.get("lastValidation") or {}
    if not isinstance(lv, dict):
        return {}
    return {"gate": lv.get("gate"), "status": lv.get("status"),
            "at": lv.get("validatedAt"), "counts": lv.get("counts") or {}}


def _gates(root: Path, last_validation: dict[str, Any]) -> dict[str, Any]:
    """SPEC-114 v12 gates section: the canonical gate ladder in flow order + the last
    local validation + (optional) the active target's last gate. `order` is the
    spec_test_gate.GATES tier list; `last` reuses the already-collected lastValidation;
    `target` reads the active target's quality-state.json (via targets_lib.state_dir),
    degrading to None. Never-crash (wrapped like every other collector)."""
    out: dict[str, Any] = {"order": list(_GATE_TIERS), "last": last_validation or {}, "target": None}
    try:
        name = _load_prefs(root).get("target")
        if name:
            qs = read_json(targets_lib.state_dir(name) / "quality-state.json", {}) or {}
            lv = qs.get("lastValidation") or {}
            if isinstance(lv, dict) and lv.get("gate"):
                out["target"] = {"name": name,
                                 "last": {"gate": lv.get("gate"), "status": lv.get("status"),
                                          "at": lv.get("validatedAt"), "counts": lv.get("counts") or {}}}
    except Exception:
        out["target"] = None
    return out


# -- N3 attention bay (K4): flight-strip ordering + named operator actions -----
# A pure ranking/labeling layer over data state_snapshot ALREADY collected. Order
# is derived on EVERY call from the state mirror — no stored index, no cache — so
# it cannot go stale (the K4 no-cache condition). Generalizes the panel's existing
# criticality-sorted idiom (_TIER_ORDER) across sources instead of by status.

def _iso_epoch(iso: Any) -> float:
    try:
        return dt.datetime.fromisoformat(str(iso)).timestamp()
    except (ValueError, TypeError):
        return 0.0


def _age_seconds(iso: Any, now_dt: dt.datetime) -> float | None:
    try:
        t = dt.datetime.fromisoformat(str(iso))
    except (ValueError, TypeError):
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return (now_dt - t).total_seconds()


def attention_strips(escalations: Any, workers: Any, last_validation: Any,
                     now_dt: dt.datetime) -> list[dict[str, Any]]:
    """Attention-ordered supervision strips (ATC flight-strip bay). Pure function of
    the state mirror: given the already-collected escalations/workers/lastValidation,
    return strips sorted by attention tier (critical first), ties broken by recency.
    Each strip NAMES the operator's next action (the dark-board rule — a signal with
    no action is noise). Degrades to [] on any trouble. Stores nothing."""
    strips: list[dict[str, Any]] = []
    try:
        for e in escalations or []:
            crit = str(e.get("criticality") or "info")
            subject = str(e.get("subject") or "self")
            strips.append({"source": "escalation", "rank": _TIER_ORDER.get(crit, 4),
                           "label": f"escalation {e.get('id') or '?'} ({crit})"
                                    + (f" [{subject}]" if subject != "self" else ""),
                           "detail": str(e.get("reason") or "")[:200],
                           "nextAction": "resolver escalation ou editar handoff",
                           "subject": subject,
                           "at": e.get("raisedAt")})
        lv = last_validation or {}
        if str(lv.get("status") or "").lower() in _GATE_FAIL:
            fails = (lv.get("counts") or {}).get("fail", "?")
            strips.append({"source": "gate", "rank": _TIER_ORDER["high"],
                           "label": f"gate {lv.get('gate') or '?'} {lv.get('status')}",
                           "detail": f"{fails} falha(s)",
                           "nextAction": "re-rodar smoke / ver falhas",
                           "at": lv.get("at")})
        for w in workers or []:
            if str(w.get("status")) != "running":
                continue
            age = _age_seconds(w.get("lastOutputAt"), now_dt)
            if age is None or age < QUIET_SECONDS:  # no log yet / still emitting → not an alarm
                continue
            wid = str(w.get("workerId") or "?")
            strips.append({"source": "worker", "rank": _TIER_ORDER["watch"],
                           "label": f"worker {wid} quiet {int(age)}s",
                           "detail": f"no output for {int(age)}s",
                           "nextAction": "drill-in / workflow tail",
                           "at": w.get("lastOutputAt"),
                           "wf": w.get("workflow"), "worker": wid})
        # ponytail: no "drift" strip — protected-file drift is NOT in state_snapshot's
        # sources today and the K4 condition forbids a new subprocess for it. Add a
        # drift strip only once a cheap drift signal already lands in the snapshot.
        strips.sort(key=lambda s: (s["rank"], -_iso_epoch(s.get("at"))))  # tier, then recency
    except Exception:
        return []
    return strips


_TIER_NAME = {rank: name for name, rank in _TIER_ORDER.items()}  # rank -> tier name


def risk_summary(out: Any) -> dict[str, Any]:
    """UX-GA.1 risk-summary strip: a one-glance rollup rendered ABOVE the attention
    bay, derived ONLY from snapshot sections already collected (attention /
    escalations / lastValidation) — a VIEW computed on every call, no cache, no new
    reads, no state written. Tolerates every missing key: a calm/empty board is all
    zeros, never a crash."""
    s: dict[str, Any] = {"tiers": {t: 0 for t in _TIER_ORDER}, "openEscalations": 0,
                         "gateFailing": False, "topStrip": "",
                         "requiresSecurityReview": False}
    try:
        snap = out or {}
        strips = snap.get("attention") or []
        for strip in strips:
            name = _TIER_NAME.get(strip.get("rank"), "info")
            s["tiers"][name] = s["tiers"].get(name, 0) + 1
        if strips:
            # the bay is tier-sorted (critical first) — strip[0] IS the top blast radius
            s["topStrip"] = str(strips[0].get("label") or "")
        s["openEscalations"] = len(snap.get("escalations") or [])
        lv = snap.get("lastValidation") or {}
        s["gateFailing"] = str(lv.get("status") or "").lower() in _GATE_FAIL
        # ponytail: read-if-present only — _last_validation doesn't ship this flag
        # today (it lives under quality-state's qualityGates); surface it the day a
        # collector adds it to the snapshot, never add a new read here.
        s["requiresSecurityReview"] = bool(lv.get("requiresSecurityReview"))
    except Exception:
        pass  # partial rollup beats a sunk page; the shape above is always valid
    return s


def group_workers(workers: Any) -> list[dict[str, Any]]:
    """UX-GA.2 grouped fan-out worker cards: categorize the already-collected
    worker rows per workflow — `"adhoc"` when a row carries none — so N parallel
    workers render as one card-group per workflow/wave (categorize, not stream).
    A pure VIEW over the rows (never mutated, never copied), computed on every
    call: no cache, no new reads, no state written. A calm board (or any
    trouble) is []. Group order is stable most-recent first: the max
    lastOutputAt/finishedAt inside the group (ISO strings compare lexically),
    ties keeping first-appearance order; worker order INSIDE a group is the
    input order, untouched."""
    groups: dict[str, dict[str, Any]] = {}
    try:
        for w in workers or []:
            wf = str(w.get("workflow") or "") or "adhoc"
            g = groups.setdefault(wf, {"workflowId": wf, "statusCounts": {}, "workers": []})
            st = str(w.get("status") or "?")
            g["statusCounts"][st] = g["statusCounts"].get(st, 0) + 1
            g["workers"].append(w)
        # ponytail: recency = max activity ISO string in the group; a group whose
        # rows carry no timestamp sorts last — good enough until someone measures
        # a board where that misleads.
        return sorted(groups.values(), reverse=True,
                      key=lambda g: max((str(w.get("lastOutputAt") or w.get("finishedAt") or "")
                                         for w in g["workers"]), default=""))
    except Exception:
        return []


def _ledger_head(root: Path) -> str:
    # LEDGER_HEAD.md first (class-B split); CONTEXT.md fallback for pre-split trees.
    for rel in (records.LEDGER_HEAD_REL, records.CONTEXT_REL):
        path = root / rel
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if records.HEAD_START in text and records.HEAD_END in text:
            return text.split(records.HEAD_START, 1)[1].split(records.HEAD_END, 1)[0].strip()
    return ""


def _costs(root: Path) -> dict[str, Any]:
    # ponytail: /api/state's COLD dominator (~85%, measured 2026-07-23) is inside
    # metrics_snapshot -> cost_metrics.summarize._dir_mb (os.walk stats the whole .harness +
    # graphify-out tree for a machine-disk field this collector DISCARDS). Root-cause fix is
    # out of this file's footprint (cost_metrics.py); this only reads the cheap ledger-derived
    # sliver below, already mtime-cached warm. See result-api-state-cold.md planDeviations.
    s = metrics_snapshot(root)
    ct = s.get("chatTurns") or {}
    dg = s.get("delegations") or {}
    return {"chatTurns": {"count": ct.get("count", 0),
                          "costUsd": (ct.get("costUsd") or {}).get("total")},
            "delegations": {"count": dg.get("count", 0),
                            "estTokens": (dg.get("estTokens") or {}).get("total")},
            "topExpensive": (s.get("topExpensive") or [])[:3]}


def _targets_with_roots(root: Path) -> list[dict[str, str]]:
    """R33: governed targets as {name, root} so the onboarding select can show full
    paths and disambiguate same-named repos. Never-crash per entry (resolve raises
    for a disabled/missing target; fall back to its declared root so it still shows)."""
    out: list[dict[str, str]] = []
    for name, entry in sorted(targets_lib.load_targets().items()):
        try:
            r = str(targets_lib.target_root(targets_lib.resolve(name)))
        except Exception:
            r = str(targets_lib.target_root(entry))
        out.append({"name": name, "root": r})
    return out


UPLOAD_DIR = Path(".harness") / "runtime" / "chat-uploads"
UPLOAD_CAP = 8 * 1024 * 1024  # bytes per attachment; the panel is a loopback tool, not a file host


def save_upload(root: Path, body: dict[str, Any]) -> dict[str, Any]:
    """Persist one browser-attached file (base64) under chat-uploads and return
    its repo-relative posix path for an @mention. Name is sanitized to a basename
    of [\\w.-] (no traversal, no spaces — keeps the @mention unquoted)."""
    import base64
    name = re.sub(r"[^\w.-]", "_", Path(str(body.get("name") or "file")).name).strip("._") or "file"
    try:
        data = base64.b64decode(str(body.get("data") or ""), validate=True)
    except (ValueError, TypeError):
        return {"error": "invalid base64 payload"}
    if not data:
        return {"error": "empty file"}
    if len(data) > UPLOAD_CAP:
        return {"error": f"file too large (cap {UPLOAD_CAP // (1024 * 1024)}MB)"}
    updir = root / UPLOAD_DIR
    updir.mkdir(parents=True, exist_ok=True)
    target = updir / f"{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{name}"
    for i in range(2, 100):  # same-second collision: numeric suffix
        if not target.exists():
            break
        target = updir / f"{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{i}-{name}"
    target.write_bytes(data)
    return {"ok": True, "path": (UPLOAD_DIR / target.name).as_posix(), "bytes": len(data)}


def pick_folder() -> str:
    """R32: OS-native folder picker. The browser cannot obtain an OS path, but this
    server is loopback-local, so the SERVER opens the dialog. Tk must not run in the
    handler thread — spawn it as a short-lived subprocess. Returns the chosen path or
    "" (cancel / headless / no display). Never raises: this is a UI affordance only,
    the actual registration is the allowlisted `targets add` subcommand."""
    code = ("import tkinter as tk, tkinter.filedialog as fd; "
            "r=tk.Tk(); r.withdraw(); r.attributes('-topmost', True); "
            "print(fd.askdirectory() or '')")
    try:
        res = subprocess.run([sys.executable, "-c", code],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
        return (res.stdout or "").strip()
    except Exception:
        return ""


def config_keys_snapshot(root: Path) -> list[dict[str, Any]]:
    """SPEC-115 Config "Keys" section feed (config-keys-gui): one MASKED row per expected
    env key -- presence + where it resolves from (env|vault|absent) + the vault backend
    and lastRotated. Reuses keys_vault.rows() (already masked; degrades to no-vault when
    keyring is absent/headless) and maps its backend to a source label. NO raw-value field
    ever leaves here (S1): only name/present/source/backend/lastRotated.
    keys-keyring v2 dropped the `.env` source — a key is env, vault, or absent."""
    try:
        from . import keys_vault
    except ImportError:
        from harness_lib import keys_vault
    src = {"env": "env", "keyring": "vault", "": "absent"}
    return [{"name": r["name"], "present": bool(r["set"]),
             "source": src.get(r["backend"], "absent"),
             "backend": r["backend"], "lastRotated": r["lastRotated"]}
            for r in keys_vault.rows(root)]


def routing_snapshot(root: Path) -> dict[str, Any]:
    """SPEC-115 Config + SPEC-114 onboarding feed: model cards + resolved routing +
    governed target names + the chat prefs' saved target (activeTarget, for the
    onboarding preselect) + the masked config-keys presence table. Each section degrades
    to []/{}/None so the page never crashes."""
    root = Path(root)
    out: dict[str, Any] = {}
    for key, fn, empty in (
        ("cards", model_routing.cards_list, []),
        ("routing", model_routing.routing_show, {}),
        ("catalog", lambda r: model_routing.catalog_list(), []),  # KNOWN_MODELS: GUI dropdown feed
        ("targets", _targets_with_roots, []),  # R33: [{name, root}] full paths
        ("activeTarget", lambda r: _load_prefs(r).get("target"), None),  # R29: onboarding preselect
        ("cardsRaw", model_routing._load_cards, {}),      # advanced-mode textarea source
        ("routingRaw", model_routing._load_routing, {}),  # advanced-mode textarea source
        ("configKeys", config_keys_snapshot, []),  # config-keys-gui: masked env-key presence
    ):
        try:
            out[key] = fn(root)
        except Exception:
            out[key] = empty
    return out


def experiments_snapshot(root: Path) -> dict[str, Any]:
    """SPEC-116 experiment-lifecycle board feed: every card grouped-ready with an
    overdueDays flag on active entries past reviewBy, plus a status count rollup.
    Read-only, degrades to an empty board so the page never crashes. CLI parity:
    `harness.py experiment list`."""
    root = Path(root)
    try:
        from . import experiment_registry as exp
    except ImportError:
        from harness_lib import experiment_registry as exp
    try:
        rows = exp.experiments(root)
    except Exception:
        rows = []
    try:
        overdue = {i: d for i, _, d in exp.stale_active(root)}
    except Exception:
        overdue = {}
    for e in rows:
        e["overdueDays"] = overdue.get(e.get("id"), 0)
    counts = collections.Counter(e.get("status") for e in rows)
    return {"experiments": rows, "counts": dict(counts), "order": list(exp.STATUSES)}


def experiment_methods(root: Path) -> dict[str, Any]:
    """Tier-1 method cards from docs/EXPERIMENT_METHODS.md -> {anchor: {title,
    sections:[{label,text}]}} for the GUI experiment-paper method expand. The anchor slug
    matches ui/src/api/experiments.detectMethods (lowercase, non-alnum -> hyphen).
    Read-only; degrades to {} on a missing/short doc. The `Source` section (internal doc
    line-refs, noise in the GUI) is dropped; the rest (Like I'm five / Technical / When to
    use HERE / Recipe) is served verbatim."""
    root = Path(root)
    try:
        # errors="ignore" mirrors line ~320 (reckon 2026-07-21): a stray non-UTF-8 byte
        # must degrade, not raise UnicodeDecodeError through the request.
        md = (root / "docs" / "EXPERIMENT_METHODS.md").read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return {}
    body = md.split("# Tier 1", 1)[-1].split("# Tier 2", 1)[0]
    out: dict[str, Any] = {}
    for part in re.split(r"\n##\s+", body)[1:]:  # [0] = pre-heading preamble
        title, _, rest = part.partition("\n")
        title = title.strip()
        anchor = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        sections: list[dict[str, str]] = []
        for m in re.finditer(r"\*\*(.+?)\.\*\*\s*(.*?)(?=\n\*\*|\Z)", rest, re.DOTALL):
            label = m.group(1).strip()
            if label.lower() == "source":
                continue
            text = m.group(2).strip()[:1400]
            if text:
                sections.append({"label": label, "text": text})
        if anchor and sections:
            out[anchor] = {"title": title, "sections": sections}
    return out


def _fuel(root: Path) -> dict[str, Any]:
    """N-VENDORCREDIT v1 vendor fuel gauge for /api/state: the CANONICAL
    vendor-fuel.json read-only (the GUI NEVER probes — only the `fuel` verb / loop
    / schedule writes it). mtime-cached like every other collector; between probes
    the file is static so this costs one stat. Absent file -> empty scaffold, never
    a crash. The strip re-derives staleness client-side from capturedAt."""
    from harness_lib import vendor_fuel
    return _cached(root / vendor_fuel.STATE_REL, lambda: vendor_fuel.load_state(root))


def state_snapshot(root: Path) -> dict[str, Any]:
    """Compact supervision snapshot; each section degrades to {}/[] on trouble."""
    root = Path(root)
    out: dict[str, Any] = {"generatedAt": now()}
    for key, fn, empty in (
        ("escalations", _escalations, []),
        ("decisions", decision_inbox.pending_decisions, []),  # SPEC-145 read-only collector
        ("lastValidation", _last_validation, {}),
        ("ledgerHead", _ledger_head, ""),
        ("ledger", lambda r: _cached(r / records.WORKLOG_REL, lambda: records.tail(r)), []),
        ("costs", _costs, {}),
        ("fuel", _fuel, {}),  # N-VENDORCREDIT v1: vendor fuel gauge (read-only)
        ("branch", chat_hud._git_branch, ""),  # v12 header branch chip; "" when not a git repo
        ("workers", lambda r: chat_hud.read_workers(r, dt.datetime.now(dt.timezone.utc),
                                                     include_done=True), []),
    ):
        try:
            out[key] = fn(root)
        except Exception:  # one bad source must never sink the page
            out[key] = empty
    # N3 attention bay: a ranking/labeling layer over the sources ALREADY collected
    # above — no new reads. Derived here every poll (no cache), degrades to [].
    try:
        out["attention"] = attention_strips(out.get("escalations"), out.get("workers"),
                                             out.get("lastValidation"),
                                             dt.datetime.now(dt.timezone.utc))
    except Exception:
        out["attention"] = []
    # UX-GA.1 risk-summary strip: a one-glance rollup DERIVED from the sections
    # stamped above — an ADDITIVE key, a view, never cached (risk_summary is pure
    # and never raises; a calm board is all zeros).
    out["riskSummary"] = risk_summary(out)
    # UX-GA.2 grouped fan-out worker cards: the workers ALREADY collected above,
    # categorized per workflow — an ADDITIVE key, a pure view (group_workers never
    # raises; out["workers"] rows stay untouched, a calm board is []).
    out["workerGroups"] = group_workers(out.get("workers"))
    # v12 gates section: the canonical ladder + the last validation already collected
    # above + the active target's gate (degrades to None). Derived here (no new page read).
    try:
        out["gates"] = _gates(root, out.get("lastValidation") or {})
    except Exception:
        out["gates"] = {"order": list(_GATE_TIERS), "last": {}, "target": None}
    return out


# -- worker drill-in (SPEC-118 read-only collector) --------------------------

def _safe_log(root: Path, base: Path, task_path: Any, default_rel: str) -> Path | None:
    """Resolve a worker log path, refusing anything outside the WF directory."""
    p = Path(str(task_path)) if task_path else (base / default_rel)
    if not p.is_absolute():
        p = root / p
    try:
        p.resolve().relative_to(base.resolve())
    except (ValueError, OSError):
        return None
    return p


_PANEL_LANGS: dict[str, Any] = {}  # cached {mtime, packLangToStem} per manifest path


def _detect_panel_language(root: Path, text: str) -> str | None:
    """Native language detection mapped to a vendored grammar stem the PANEL can render,
    or None (engine absent, diff-shaped, or a language with no vendored grammar). Reuses
    the CLI's tree-sitter detector; the grammar set is read from the vendor manifest."""
    try:
        from harness_lib import highlight
        if not text or highlight._looks_diff(text):
            return None
        detected = highlight.detect_language(text)
        if not detected:
            return None
        mpath = root / "vendor" / "tree-sitter" / "manifest.json"
        try:
            mtime = mpath.stat().st_mtime
        except OSError:
            return None
        cached = _PANEL_LANGS.get(str(mpath))
        if not cached or cached[0] != mtime:
            langs = (read_json(mpath, {}) or {}).get("queries", {}).get("langs", {})
            pack_to_stem = {meta.get("packLang", stem): stem for stem, meta in langs.items()}
            pack_to_stem.update({stem: stem for stem in langs})  # stem itself is valid too
            _PANEL_LANGS[str(mpath)] = (mtime, pack_to_stem)
            cached = _PANEL_LANGS[str(mpath)]
        return cached[1].get(detected)
    except Exception:
        return None


def worker_detail(root: Path, wfid: Any, worker_id: Any, lines: int = 200) -> dict[str, Any]:
    """On-demand live output for one worker (SPEC-114 v6). Validates ids by regex
    + existence in workflow.json BEFORE building any path; degrades the whole body
    to {"error": …} (the /api/records pattern) — never 500, never probes outside
    .harness/workflows/active/<WF>/. Read-only."""
    root = Path(root)
    try:
        wfid, worker_id = str(wfid), str(worker_id)
        if not (_WORKER_ID_RE.match(wfid) and wfid.startswith("WF-") and _WORKER_ID_RE.match(worker_id)):
            return {"error": "invalid workflow or worker id"}
        base = root / ".harness" / "workflows" / "active" / wfid
        wf = read_json(base / "workflow.json", {}) or {}
        worker = next((w for w in wf.get("workers", []) if str(w.get("workerId")) == worker_id), None)
        if worker is None:
            return {"error": f"unknown workflow/worker: {wfid}/{worker_id}"}
        task = {}
        tdir = base / "async" / "tasks"
        if tdir.is_dir():
            for tp in sorted(tdir.glob("*.json")):
                t = read_json(tp, {}) or {}
                if str(t.get("workerId")) == worker_id:
                    task = t
                    break
        so = _safe_log(root, base, task.get("stdoutPath"), f"run-logs/{worker_id}.stdout.log")
        se = _safe_log(root, base, task.get("stderrPath"), f"run-logs/{worker_id}.stderr.log")
        events: list[dict[str, Any]] = []
        ev_path = base / "async" / "events.jsonl"
        # events.jsonl is append-only — only the tail matters; read a bounded window
        # instead of the whole file (tail_lines handles absence + byte-torn head).
        for line in tail_lines(ev_path, lines=400).get("lines") or []:
            try:
                e = json.loads(line)
            except (ValueError, json.JSONDecodeError):
                continue
            if str(e.get("workerId")) == worker_id:
                events.append(e)
        cmd = task.get("command")

        def _redacted(block: dict[str, Any]) -> dict[str, Any]:
            # rec-gui-1 (spec-recovery 2026-07-13): worker logs are the ONE panel
            # surface that skipped the server-side redaction seam every other
            # viewer applies — a worker that echoes env/keys would leak here.
            if isinstance(block.get("lines"), list):
                block["lines"] = [secret_scan.redact_text(str(l)) for l in block["lines"]]
            return block

        stdout = _redacted(tail_lines(so, lines))
        language = _detect_panel_language(root, "\n".join(stdout.get("lines") or []))
        return {
            "workflowId": wfid, "workerId": worker_id,
            "status": str(worker.get("status") or ""),
            "worker": {"unit": worker.get("unitId"), "profile": worker.get("taskProfile"),
                       "attempts": worker.get("attempts")},
            "task": {"pid": task.get("pid"), "status": task.get("status"),
                     "executor": task.get("executor"), "returnCode": task.get("returnCode"),
                     "startedAt": task.get("startedAt"), "settledAt": task.get("settledAt"),
                     "commandDisplay": " ".join(str(x) for x in cmd) if isinstance(cmd, list) else None},
            "language": language,
            "stdout": stdout,
            "stderr": _redacted(tail_lines(se, lines)),
            "events": events[-30:],
            "generatedAt": now(),
        }
    except Exception as exc:  # degrade whole body, never 500 (the /api/records pattern)
        return {"error": f"{type(exc).__name__}: {exc}"}



# -- MF split (2026-07-13): composer / actions / chat-bridge domains live in
# sibling modules; every public + test-referenced name re-exports here so
# harness_ui and the scenarios keep their ui_panel.* references unchanged.
from harness_lib.ui_actions import (ACTIONS, ACTION_TIMEOUT, GATE_ALLOW, OUTPUT_CAP,  # noqa: F401,E402
                                    _WORKER_ID_RE, _active_wfid, _card_flags,
                                    _discard_reason, _fallback_flags, _pid_from,
                                    _recovery_reason, _refuse, _wf_running, run_action)
from harness_lib.ui_chat_bridge import ChatBridge, ChatSessions  # noqa: F401,E402
from harness_lib.ui_composer import (_compile_reject, _dag_for, _import_harness,  # noqa: F401,E402
                                     _normalize_profile, compile_candidate,
                                     composer_snapshot)

# -- deterministic self-check (no LLM) ---------------------------------------

def _self_check() -> None:
    import json as _json
    import tempfile
    import time

    # state_snapshot shapes + criticality sort on synthetic state
    tmp = Path(tempfile.mkdtemp())
    (tmp / ".harness" / "state").mkdir(parents=True)
    (tmp / ".harness" / "context").mkdir(parents=True)
    (tmp / ".harness" / "state" / "escalations.json").write_text(_json.dumps({
        "raised": {
            "e-info": {"escalationId": "e-info", "reason": "low\nignore", "criticality": "info",
                       "raisedAt": "2026-07-10T01:00:00+00:00"},
            "e-crit": {"escalationId": "e-crit", "reason": "boom\nline2", "criticality": "critical",
                       "raisedAt": "2026-07-10T02:00:00+00:00"},
            "e-high": {"escalationId": "e-high", "reason": "warn", "criticality": "high",
                       "raisedAt": "2026-07-10T03:00:00+00:00"},
            "e-done": {"escalationId": "e-done", "reason": "resolved", "criticality": "critical"},
        },
        "resolvedIds": ["e-done"]}), encoding="utf-8")
    (tmp / ".harness" / "state" / "quality-state.json").write_text(_json.dumps({
        "lastValidation": {"gate": "spec-pack", "status": "pass",
                           "validatedAt": "2026-07-10T00:00:00+00:00",
                           "counts": {"pass": 10, "fail": 0, "skip": 1}}}), encoding="utf-8")
    # a synthetic worklog entry so the structured ledger collector has a card;
    # add_entry rewrites CONTEXT's head, so the manual write below wins for ledgerHead.
    records.add_entry(tmp, "milestone", "hello ledger", refs=["SPEC-114"], tags=["ui"])
    (tmp / ".harness" / "context" / "CONTEXT.md").write_text(
        f"# Ctx\n\n{records.HEAD_START}\n## Ledger head\n- [note] hello\n{records.HEAD_END}\n",
        encoding="utf-8")
    snap = state_snapshot(tmp)
    assert set(snap) >= {"escalations", "lastValidation", "ledgerHead", "ledger",
                         "costs", "workers", "generatedAt", "branch", "gates", "fuel"}, snap.keys()
    assert isinstance(snap["fuel"], dict) and snap["fuel"].get("vendors") == {}, snap["fuel"]  # empty scaffold on tmp root
    assert snap["ledger"] and snap["ledger"][0]["kind"] == "milestone", snap["ledger"]
    # v12: branch is a string ("" off a non-git tmp root); gates carries the canonical
    # ladder + the last validation, no active target (no prefs.target on this tmp root).
    assert isinstance(snap["branch"], str), snap["branch"]
    assert snap["gates"]["order"] == _GATE_TIERS and snap["gates"]["order"][0] == "smoke", snap["gates"]
    assert snap["gates"]["last"].get("gate") == "spec-pack" and snap["gates"]["target"] is None, snap["gates"]
    tiers = [e["criticality"] for e in snap["escalations"]]
    assert tiers == ["critical", "high", "info"], tiers  # sorted, resolved dropped
    assert snap["escalations"][0]["reason"] == "boom", snap["escalations"][0]
    assert snap["lastValidation"]["gate"] == "spec-pack"
    assert "hello" in snap["ledgerHead"] and records.HEAD_START not in snap["ledgerHead"]
    assert isinstance(snap["costs"], dict) and isinstance(snap["workers"], list)
    assert isinstance(snap.get("attention"), list), snap.keys()  # N3 bay flows through /api/state
    # a bare root never crashes the collector
    assert state_snapshot(Path(tempfile.mkdtemp()))["escalations"] == []

    # N3 attention bay (K4): attention-ordered, every strip names an action, pure.
    _now = dt.datetime.now(dt.timezone.utc)
    _old = (_now - dt.timedelta(seconds=600)).isoformat()
    _state = ([{"id": "e1", "criticality": "critical", "reason": "boom", "raisedAt": _old},
               {"id": "e2", "criticality": "info", "reason": "fyi", "raisedAt": _old}],
              [{"workerId": "wq", "status": "running", "workflow": "WF", "lastOutputAt": _old}],
              {"gate": "smoke", "status": "fail", "at": _old, "counts": {"fail": 1}})
    _a = attention_strips(*_state, _now)
    assert [s["rank"] for s in _a] == sorted(s["rank"] for s in _a), _a  # attention-ordered
    assert _a[0]["source"] == "escalation" and _a[0]["rank"] == 0, _a    # critical leads
    # critical-esc (0) → failed-gate (1) → quiet-worker (2) → info-esc (3)
    assert [s["source"] for s in _a] == ["escalation", "gate", "worker", "escalation"], \
        [s["source"] for s in _a]
    assert all(s.get("nextAction") for s in _a), _a                      # dark-board rule
    assert _a == attention_strips(*_state, _now), "pure: same state → same order (no cache)"
    assert attention_strips([], [], {}, _now) == []                      # calm board, nothing cached
    assert attention_strips(_state[0][1:], *_state[1:], _now)[0]["rank"] != 0, "re-derived, not stale"

    # UX-GA.1 risk-summary strip: a pure rollup over the SAME already-collected state.
    _rsnap = {"escalations": _state[0], "lastValidation": _state[2],
              "attention": attention_strips(*_state, _now)}
    _rs = risk_summary(_rsnap)
    assert _rs["tiers"] == {"critical": 1, "high": 1, "watch": 1, "info": 1}, _rs
    assert _rs["openEscalations"] == 2 and _rs["gateFailing"] is True, _rs
    assert _rs["topStrip"] == "escalation e1 (critical)", _rs  # bay-sorted: [0] = top blast radius
    assert _rs == risk_summary(_rsnap), "pure: same input → same output"
    _calm = risk_summary({})                                             # calm board = all zeros
    assert not any(_calm["tiers"].values()) and _calm["openEscalations"] == 0 \
        and _calm["gateFailing"] is False and _calm["topStrip"] == "" \
        and _calm["requiresSecurityReview"] is False, _calm
    assert risk_summary(None) == _calm, "missing snapshot tolerated, never crashes"
    # DERIVED, not cached: dropping the critical strip changes the very next call
    _rs2 = risk_summary({"escalations": _state[0][1:], "lastValidation": _state[2],
                         "attention": _rsnap["attention"][1:]})
    assert _rs2["tiers"]["critical"] == 0 and _rs2["openEscalations"] == 1, _rs2
    assert snap["riskSummary"] == risk_summary(snap), "snapshot stamps the derived view"

    # UX-GA.2 grouped worker cards: pure per-workflow grouping over the SAME rows.
    _gw = [{"workerId": "a1", "status": "running", "workflow": "WF-A",
            "lastOutputAt": "2026-07-12T01:00:00+00:00"},
           {"workerId": "a2", "status": "failed", "workflow": "WF-A"},
           {"workerId": "b1", "status": "running", "workflow": "WF-B",
            "lastOutputAt": "2026-07-12T02:00:00+00:00"},
           {"workerId": "x1", "status": "queued"}]  # no workflow -> "adhoc"
    _wg = group_workers(_gw)
    assert [g["workflowId"] for g in _wg] == ["WF-B", "WF-A", "adhoc"], _wg  # most-recent first
    assert _wg[1]["statusCounts"] == {"running": 1, "failed": 1}, _wg[1]
    assert _wg[1]["workers"] == [_gw[0], _gw[1]], "worker order inside a group = input order"
    assert _wg == group_workers(_gw), "pure: same input -> same output (no cache)"
    assert group_workers([]) == [] and group_workers(None) == [], "calm board -> []"
    _one = group_workers([_gw[1], _gw[0]])  # a single group preserves the given order
    assert len(_one) == 1 and _one[0]["workers"] == [_gw[1], _gw[0]], _one
    assert _gw[0] == {"workerId": "a1", "status": "running", "workflow": "WF-A",
                      "lastOutputAt": "2026-07-12T01:00:00+00:00"}, "rows never mutated"
    assert snap["workerGroups"] == group_workers(snap["workers"]), "snapshot stamps the view"

    # run_action refusals
    assert run_action(tmp, "no-such-action", {})["refused"] == "unknown action"
    hitl = run_action(tmp, "records-search", {"terms": ["x", "--approval-token", "X"]})
    assert hitl["ok"] is False and "human-only" in hitl["refused"], hitl
    unconf = run_action(tmp, "resolve-escalation", {"id": "e-crit"})
    assert unconf["ok"] is False and "confirmation" in unconf["refused"], unconf
    badgate = run_action(tmp, "rerun-gate", {"gate": "release", "confirm": True})
    assert badgate["ok"] is False and "not allowed" in badgate["refused"], badgate

    # SPEC-115 config actions: every mutating routing/model action needs a confirm,
    # and the --send/--approval-token backstop still classifies human-only.
    for act, params in (("models-add", {"id": "x", "engine": "claude"}),
                        ("routing-profile-save", {"name": "p"}),
                        ("routing-restore-canonical", {})):
        r = run_action(tmp, act, params)
        assert r["ok"] is False and "confirmation" in r["refused"], (act, r)
    sneaky = run_action(tmp, "routing-set-role",
                        {"profile": "p", "role": "chat", "primary": "--send", "confirm": True})
    assert sneaky["ok"] is False and "human-only" in sneaky["refused"], sneaky
    # the argv builders shape the expected subcommand tails
    assert ACTIONS["models-add"]["build"]({"id": "opus", "engine": "claude", "reasoning": ["high"],
                                           "default": True}) == \
        ["models", "add", "opus", "--engine", "claude", "--reasoning", "high", "--default"]
    assert ACTIONS["routing-set-role"]["build"]({"profile": "econ", "role": "plan",
        "primary": "opus:high", "fallbacks": ["gpt-5.5:high"]}) == \
        ["routing", "set-role", "--profile", "econ", "--role", "plan",
         "--primary", "opus:high", "--fallback", "gpt-5.5:high"]
    # routing_snapshot degrades on a bare root (no cards/routing/targets present)
    snap115 = routing_snapshot(Path(tempfile.mkdtemp()))
    assert {"cards", "routing", "catalog", "targets", "activeTarget",
            "cardsRaw", "routingRaw"} <= set(snap115), snap115
    assert snap115["cards"] == [] and snap115["cardsRaw"] == {"schemaVersion": 1, "engines": {}}, snap115
    assert snap115["activeTarget"] is None, snap115  # R29: never-crash → None on bare root
    # catalog is static KNOWN_MODELS, so it is populated even on a bare root
    assert any(m["id"] == "gpt-5.5" for m in snap115["catalog"]), snap115["catalog"]
    # R29: activeTarget mirrors the chat prefs' saved target (onboarding preselect)
    from harness_lib.chat_setup import _save_prefs  # test-only; production uses _load_prefs
    ptmp = Path(tempfile.mkdtemp())
    _save_prefs(ptmp, "claude", None, None, None, target="app-x")
    assert routing_snapshot(ptmp)["activeTarget"] == "app-x", routing_snapshot(ptmp)

    # R32: targets-add — allowlisted build shape + mutating→confirm, then a real
    # registration round-trip on a tmp root via the single write path (register()).
    assert ACTIONS["targets-add"]["build"]({"name": "app", "path": "/p"}) == \
        ["targets", "add", "app", "/p"]
    ta = run_action(tmp, "targets-add", {"name": "app", "path": "/p"})  # no confirm
    assert ta["ok"] is False and "confirmation" in ta["refused"], ta
    gtmp = Path(tempfile.mkdtemp()); (gtmp / ".git").mkdir()
    targets_lib.register(tmp, "probe", gtmp)
    assert (tmp / ".harness" / "targets" / "probe" / "target.json").exists()
    try:
        targets_lib.register(tmp, "probe", gtmp); assert False, "duplicate must refuse"
    except Exception as exc:
        assert "already registered" in str(exc), exc
    # R33: _targets_with_roots pairs each governed target with its full root path
    # (load_targets is fixed to the real harness dir, so inject a fake for the shape).
    _ol = targets_lib.load_targets
    targets_lib.load_targets = lambda: {"z": {"name": "z", "path": str(gtmp)}}
    try:
        assert _targets_with_roots(tmp) == [{"name": "z", "root": str(gtmp)}], _targets_with_roots(tmp)
    finally:
        targets_lib.load_targets = _ol

    # R32: pick_folder — fake the Tk subprocess (never pop a real dialog); assert it
    # returns the chosen path and the argv drives tkinter.filedialog.askdirectory.
    _orig_run, _seen = subprocess.run, {}
    subprocess.run = lambda argv, **kw: (_seen.__setitem__("argv", argv)
        or type("R", (), {"stdout": "/picked\n", "stderr": "", "returncode": 0})())
    try:
        assert pick_folder() == "/picked"
    finally:
        subprocess.run = _orig_run
    joined = " ".join(_seen["argv"])
    assert "tkinter" in joined and "askdirectory" in joined, _seen

    # N4 recovery console (K5): the four recovery verbs are mutating+recovery ACTIONS
    # with the exact real argv tails; scrub + every --force* variant are CLI-only; a
    # wfid is validated against active/ before any subcommand; unconfirmed → refused.
    # NOTHING destructive is executed — assertions are on refusal paths + argv shape.
    assert "scrub" not in ACTIONS and not any("scrub" in a for a in ACTIONS), list(ACTIONS)
    _rec = ("workflow-retry", "workflow-cancel", "workflow-mark", "workflow-recover")
    rec_root = Path(tempfile.mkdtemp())
    (rec_root / ".harness" / "workflows" / "active" / "WF-OK-1").mkdir(parents=True)
    assert ACTIONS["workflow-retry"]["build"]({"wfid": "WF-OK-1", "workerId": "w1"}) == \
        ["workflow", "retry", "WF-OK-1", "w1"]
    assert ACTIONS["workflow-cancel"]["build"]({"wfid": "WF-OK-1"}) == ["workflow", "cancel", "WF-OK-1"]
    assert ACTIONS["workflow-mark"]["build"]({"wfid": "WF-OK-1", "workerId": "w1", "status": "failed"}) == \
        ["workflow", "mark", "WF-OK-1", "w1", "failed"]
    assert ACTIONS["workflow-recover"]["build"]({"wfid": "WF-OK-1"}) == ["workflow", "async-recover", "WF-OK-1"]
    for _act in _rec:  # every recovery action is mutating+recovery and emits no --force* token
        assert ACTIONS[_act]["mutating"] and ACTIONS[_act].get("recovery"), _act
        _argv = ACTIONS[_act]["build"]({"wfid": "WF-OK-1", "workerId": "w1", "status": "done"})
        assert not any(t.startswith("--force") for t in _argv), (_act, _argv)
    _ran = {"n": 0}
    _orig_sr = subprocess.run
    subprocess.run = lambda *a, **k: (_ran.__setitem__("n", _ran["n"] + 1), _orig_sr(*a, **k))[1]
    try:
        _foreign = run_action(rec_root, "workflow-cancel", {"wfid": "WF-FOREIGN-9", "confirm": True})
        assert _foreign["ok"] is False and "workflow id" in _foreign["refused"], _foreign
        _injw = run_action(rec_root, "workflow-retry",
                           {"wfid": "WF-OK-1", "workerId": "--force-round", "confirm": True})
        assert _injw["ok"] is False and "worker id" in _injw["refused"], _injw
        _injs = run_action(rec_root, "workflow-mark",
                           {"wfid": "WF-OK-1", "workerId": "w1", "status": "--force", "confirm": True})
        assert _injs["ok"] is False and "status" in _injs["refused"], _injs
        _unconf = run_action(rec_root, "workflow-recover", {"wfid": "WF-OK-1"})  # valid id, no confirm
        assert _unconf["ok"] is False and "confirmation" in _unconf["refused"], _unconf
        assert _ran["n"] == 0, "no recovery subprocess may run (all refused before subprocess.run)"
    finally:
        subprocess.run = _orig_sr

    # Panel discard (SPEC-114 amendment): workflow-discard = scrub restricted to NOT-running
    # workflows. mutating+discard, argv is the plain `workflow scrub <wfid>` tail (NO --force),
    # a STOPPED WF (the WF-OK-1 dir, no lock) passes the pre-check, and a RUNNING WF (a
    # workflow.lock naming THIS live process) or a foreign id is refused BEFORE any subprocess.
    dc = ACTIONS["workflow-discard"]
    assert dc["mutating"] and dc.get("discard"), dc
    _dargv = dc["build"]({"wfid": "WF-OK-1"})
    assert _dargv == ["workflow", "scrub", "WF-OK-1"] and "--force" not in _dargv, _dargv
    assert _discard_reason(rec_root, {"wfid": "WF-OK-1"}) is None, "a stopped WF must be discardable"
    _run_wf = rec_root / ".harness" / "workflows" / "active" / "WF-RUN-1"
    _run_wf.mkdir(parents=True)
    (_run_wf / "workflow.lock").write_text(_json.dumps({"pid": os.getpid()}), encoding="utf-8")
    _dran = {"n": 0}
    _odr = subprocess.run
    subprocess.run = lambda *a, **k: (_dran.__setitem__("n", _dran["n"] + 1), _odr(*a, **k))[1]
    try:
        _drun = run_action(rec_root, "workflow-discard", {"wfid": "WF-RUN-1", "confirm": True})
        _dforeign = run_action(rec_root, "workflow-discard", {"wfid": "WF-NOPE-9", "confirm": True})
        _dunconf = run_action(rec_root, "workflow-discard", {"wfid": "WF-OK-1"})  # stopped, no confirm
    finally:
        subprocess.run = _odr
    assert _drun["ok"] is False and "running" in _drun["refused"], _drun  # EN string (SPEC-122)
    assert _dforeign["ok"] is False and "workflow id" in _dforeign["refused"], _dforeign
    assert _dunconf["ok"] is False and "confirmation" in _dunconf["refused"], _dunconf
    assert _dran["n"] == 0, "a running/foreign/unconfirmed discard must not spawn scrub"

    # ChatBridge round-trip against the REAL repo in manual mode (no LLM).
    # Events are on by default now (SPEC-114 v2): assert a ready evt during the
    # round-trip and an exit evt after the child winds down.
    root = Path(__file__).resolve().parents[2]

    # N2 composer: closed-vocabulary snapshot + derived DAG + in-process validate-only
    # compile (K1). Runs against the real repo profiles; read-only, materializes nothing.
    active_dir = root / ".harness" / "workflows" / "active"
    dirs_before = {p.name for p in active_dir.iterdir()} if active_dir.exists() else set()
    csnap = composer_snapshot(root)
    assert {"profiles", "executors", "taskProfiles"} <= set(csnap), csnap.keys()
    assert "research-divergence" in csnap["profiles"], list(csnap["profiles"])
    rd = csnap["profiles"]["research-divergence"]
    assert rd["type"] == "fork-join" and len(rd["branches"]) == 5, rd
    assert rd["dag"]["nodes"][0]["kind"] == "source" and rd["dag"]["nodes"][-1]["kind"] == "reduce"
    assert any(e["kind"] == "seed" for e in rd["dag"]["edges"]), rd["dag"]  # sharedContextDigest → seed edges
    assert "plan" in csnap["taskProfiles"] and "claude" in csnap["executors"], csnap
    good = compile_candidate(root, "research-divergence", "compose selfcheck", rd["branches"])
    assert good.get("valid") is True and good.get("validateOnly") is True, good
    bad = compile_candidate(root, "no-such-profile-xyz", "x", None)
    assert bad.get("valid") is False and any("no-such-profile" in e for e in bad.get("errors", [])), bad
    badtp = compile_candidate(root, "research-divergence", "x", [{"title": "t", "taskProfile": "nope"}])
    assert badtp.get("valid") is False and any("nope" in e for e in badtp.get("errors", [])), badtp
    dirs_after = {p.name for p in active_dir.iterdir()} if active_dir.exists() else set()
    assert dirs_after == dirs_before, (dirs_before, dirs_after)  # compile writes NO active/WF-* dir
    assert "compile" not in ACTIONS and "compile-candidate" not in ACTIONS, "compile is not an allowlisted action"

    # N2b composer-create (SPEC-120 v2): create-only materialize action. The build argv is
    # `workflow plan --profile … --branch-json …` and carries NO start/async verb (create
    # never starts). The compile-must-be-valid pre-check refuses an unknown profile BEFORE any
    # subprocess runs (monkeypatch proves none spawns) — the load-bearing security guard.
    cc = ACTIONS["composer-create"]
    assert cc["mutating"] and cc.get("composerCreate"), cc
    cc_argv = cc["build"]({"profile": "research-divergence",
                           "branches": [{"title": "t", "taskProfile": "plan"}], "task": "x"})
    assert cc_argv[:2] == ["workflow", "plan"] and "--branch-json" in cc_argv, cc_argv
    assert not any(v in cc_argv for v in ("start", "run", "async-run", "await", "async-recover", "execute")), cc_argv
    _cc_ran = {"n": 0}
    _cc_orig = subprocess.run
    subprocess.run = lambda *a, **k: (_cc_ran.__setitem__("n", _cc_ran["n"] + 1), _cc_orig(*a, **k))[1]
    try:
        _cc_bad = run_action(root, "composer-create",
                             {"profile": "no-such-profile-xyz", "branches": [], "confirm": True})
    finally:
        subprocess.run = _cc_orig
    assert _cc_bad["ok"] is False and _cc_ran["n"] == 0, (_cc_bad, _cc_ran)  # invalid compile → no subprocess
    assert dirs_after == ({p.name for p in active_dir.iterdir()} if active_dir.exists() else set())  # still no WF dir

    bridge = ChatBridge()
    bridge.start(root, engine="manual")
    assert bridge.send("status")
    deadline = time.monotonic() + 15
    seen, events = "", []
    while time.monotonic() < deadline and '"root"' not in seen:
        for item in bridge.read(timeout=0.5):
            if item.get("stream") == "evt":
                events.append(item["data"])
            else:
                seen += item.get("line", "") + "\n"
    assert '"root"' in seen, f"no status output in 15s; got:\n{seen[-500:]}"
    assert any(e.get("event") == "ready" for e in events), events
    bridge.send("/exit")
    deadline = time.monotonic() + 15  # let stdout EOF produce the exit evt before stop()
    while time.monotonic() < deadline and not any(e.get("event") == "exit" for e in events):
        for item in bridge.read(timeout=0.5):
            if item.get("stream") == "evt":
                events.append(item["data"])
    assert any(e.get("event") == "exit" for e in events), events
    # R24: send() logs the user's line and the queued events also land in history,
    # so a reconnecting stream can replay the whole transcript (incl. the ready evt).
    assert any(it.get("stream") == "you" and it.get("line") == "status"
               for it in bridge.history), "send() must log a you item to history"
    assert any(it.get("stream") == "evt" and (it.get("data") or {}).get("event") == "ready"
               for it in bridge.history), "history must retain ready for replay"
    bridge.stop()
    time.sleep(0.3)
    assert bridge.proc is None
    bridge.stop()  # idempotent

    # SPEC-114 v11 multi-session registry (manual engine, no LLM). new() creates a
    # live session; list() shows it; two sessions are INDEPENDENT bridges (a send to
    # each lands in that bridge's history only); close() stops+removes; stop_all()
    # clears everything (no leaked subprocess).
    sessions = ChatSessions()
    try:
        sa = sessions.new(root, engine="manual")
        sb = sessions.new(root, engine="manual")
        assert sa != sb, (sa, sb)
        ba, bb = sessions.get(sa), sessions.get(sb)
        assert ba is not None and bb is not None and ba is not bb
        listed = {s["id"]: s for s in sessions.list()}
        assert sa in listed and sb in listed and listed[sa]["alive"], sessions.list()
        assert ba.send("alpha-marker") and bb.send("beta-marker")
        ha = [it.get("line") for it in list(ba.history) if it.get("stream") == "you"]
        hb = [it.get("line") for it in list(bb.history) if it.get("stream") == "you"]
        assert "alpha-marker" in ha and "alpha-marker" not in hb, (ha, hb)
        assert "beta-marker" in hb and "beta-marker" not in ha, (ha, hb)
        # ⏹ stop_turn: live bridge writes its per-session flag file; dead one refuses
        assert ba.stop_turn() and ba.stop_file is not None and ba.stop_file.exists()
        ba.stop_file.unlink()
        assert sessions.close(sa) and sessions.get(sa) is None
        assert not ba.alive, "close() must stop the bridge"
        assert ba.stop_turn() is False, "dead bridge must refuse stop_turn"
        assert sessions.close(sa) is False  # idempotent: already gone
    finally:
        sessions.stop_all()
    assert sessions.list() == [] and sessions.get(sb) is None, "stop_all clears the registry"

    # chat-uploads: sanitized basename (traversal-proof), cap, base64 validation
    import base64 as _b64
    up = save_upload(tmp, {"name": "../../e vil.txt", "data": _b64.b64encode(b"hi").decode()})
    assert up.get("ok") and up["path"].startswith(".harness/runtime/chat-uploads/"), up
    assert up["path"].endswith("e_vil.txt") and ".." not in up["path"], up
    saved = tmp / up["path"]
    assert saved.read_bytes() == b"hi" and saved.resolve().is_relative_to((tmp / UPLOAD_DIR).resolve())
    assert "error" in save_upload(tmp, {"name": "x", "data": "not base64!!"})
    assert "error" in save_upload(tmp, {"name": "x", "data": ""})
    two = save_upload(tmp, {"name": "e vil.txt", "data": _b64.b64encode(b"h2").decode()})
    assert two.get("ok") and two["path"] != up["path"], (up, two)  # same-second collision

    print("ui_panel self-check ok")


if __name__ == "__main__":
    _self_check()
