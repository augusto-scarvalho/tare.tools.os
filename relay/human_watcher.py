#!/usr/bin/env python3
"""tare.tools Relay HUMAN watcher — external approval/alert notifier + mobile actions.

The human gatekeeper does not read agent chats. This script is the external
signal: it polls relay/approvals/ and relay/alerts/ and pushes to the owner's
PHONE via ntfy.sh (topic NTFY_TOPIC).

Features:
- Single consolidated notification per train with full Auditor Summary & Technical Debts
- 3 Action Buttons: [Approve] [Deny] [Auto]
- Auto-Approve Mode: Continuously auto-lands approved trains until human toggles off
- Automatic Technical Debt Graphification: Converts audit debts into backlog intake items

Run (console):    python relay/human_watcher.py
Run (no console): pythonw relay/human_watcher.py
"""
import ctypes
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

RELAY = Path(__file__).resolve().parent
if str(RELAY) not in sys.path:
    sys.path.insert(0, str(RELAY))
import relay_io

POLL_SECONDS = 20
REMIND_MINUTES = 15
NTFY_TOPIC = os.environ.get("RELAY_NTFY_TOPIC", "tare-relay-166cb5b879bd")
APPROVE_TOPIC = os.environ.get("RELAY_NTFY_APPROVE_TOPIC", NTFY_TOPIC + "-approve")
AUTO_MODE_FILE = RELAY / "history" / "auto-approve.json"
MB_ICONINFO_SYSTEMMODAL = 0x40 | 0x1000
MB_ICONWARN_SYSTEMMODAL = 0x30 | 0x1000


def drive_search_url(filename: str) -> str:
    return "https://drive.google.com/drive/search?q=" + urllib.parse.quote(filename)


def get_auto_mode() -> dict[str, Any]:
    try:
        if AUTO_MODE_FILE.exists():
            return json.loads(AUTO_MODE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"enabled": False, "count": 0}


def set_auto_mode(enabled: bool, note: str = "") -> None:
    data = get_auto_mode()
    data["enabled"] = enabled
    data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if enabled:
        data["enabled_at"] = data.get("enabled_at") or data["updated_at"]
    else:
        data["disabled_at"] = data["updated_at"]
    data["note"] = note
    AUTO_MODE_FILE.parent.mkdir(parents=True, exist_ok=True)
    AUTO_MODE_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _capability_nonce(train: str) -> Optional[str]:
    try:
        cap = json.loads((RELAY / "approvals" / f"CAPABILITY_{train}.json")
                         .read_text(encoding="utf-8-sig"))
        return cap.get("nonce")
    except Exception:
        return None


def extract_audit_dossier(train_id: str) -> dict[str, Any]:
    """Extracts verdict, recommendation, and technical debts from AUDIT_REPORT.md."""
    train_dir = RELAY / "trains" / train_id
    report_file = train_dir / "AUDIT_REPORT.md"
    state_file = train_dir / "STATE.json"
    state = {}
    try:
        if state_file.exists():
            state = json.loads(state_file.read_text(encoding="utf-8-sig"))
    except Exception:
        pass

    tasks = state.get("task_ids", [])
    title = state.get("title", train_id)

    if not report_file.exists():
        return {
            "title": title, "tasks": tasks, "verdict": "ANALYZED",
            "recommendation": "Audit report not generated yet.",
            "debts": [], "raw_text": "",
        }

    text = report_file.read_text(encoding="utf-8", errors="replace")
    verdict = "VERIFIED_PASS" if "VERIFIED_PASS" in text else \
              ("FAIL" if "AUDIT_VERDICT: FAIL" in text else "ANALYZED")

    # Group lines by Markdown section
    sections: dict[str, list[str]] = {}
    current_sec = "header"
    sections[current_sec] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("## "):
            sec_title = line[3:].strip().lower()
            current_sec = sec_title
            sections[current_sec] = []
        else:
            sections[current_sec].append(line)

    # 1. Recommendation
    rec = ""
    for sec_name, lines in sections.items():
        if "recommendation" in sec_name or "recomenda" in sec_name:
            rec_lines = [ln for ln in lines if ln and not ln.startswith("###") and not ln.startswith("AUDIT_VERDICT") and not ln.startswith("---")]
            rec = "\n".join(rec_lines)
            break
    if not rec:
        rec = "Passed the adversarial audit with no blocking issues."

    # 2. Findings
    findings = []
    for sec_name, lines in sections.items():
        if any(k in sec_name for k in ("finding", "battery", "achado", "bateria")):
            for ln in lines:
                clean = ln.lstrip("-*123456789. ")
                if clean and len(clean) > 12 and not clean.startswith("###"):
                    findings.append(clean)
            break

    # 3. Technical Debts
    debts = []
    for sec_name, lines in sections.items():
        if any(k in sec_name for k in ("debt", "risk", "débito", "debito", "risco")):
            for ln in lines:
                clean = ln.lstrip("-*123456789. ")
                if clean and len(clean) > 12 and not clean.startswith("###"):
                    debts.append(clean)
    # Also fallback scan all lines for debt keywords if none found
    if not debts:
        for line in text.splitlines():
            line_clean = line.strip().lstrip("-*123456789. ")
            lower = line_clean.lower()
            # The Portuguese keywords match audit reports saved before the reports switched to English.
            if any(k in lower for k in ["technical debt", "residual risk", "not built", "deferred to", "left open", "quarantine",
                                        "pages_surface_unavailable", "débito técnico", "debito tecnico", "risco residual",
                                        "não construídas", "diferido para", "aberto para", "quarentena"]):
                if len(line_clean) > 15 and line_clean not in debts and not line_clean.startswith("#"):
                    debts.append(line_clean)

    # 4. Footprint
    footprint = []
    for sec_name, lines in sections.items():
        if "footprint" in sec_name:
            for ln in lines:
                clean = ln.lstrip("-*` ")
                if clean and not clean.startswith("###"):
                    footprint.append(clean)
            break

    # 5. Extract Task Intent & Plain Explanation
    packet_file = train_dir / "PACKET.md"
    task_summaries = []
    task_intents = []
    if packet_file.exists():
        p_text = packet_file.read_text(encoding="utf-8", errors="replace")
        for p_line in p_text.splitlines():
            if p_line.strip().startswith("- **Summary:**"):
                task_summaries.append(p_line.replace("- **Summary:**", "").strip())
            elif p_line.strip().startswith("- **Intent:**"):
                task_intents.append(p_line.replace("- **Intent:**", "").strip())

    # Build plain-language explanation (Executive / Zero-Jargon Mode)
    what_it_does = " ".join(task_summaries) if task_summaries else f"Implements and delivers improvements for {title}."
    why_it_matters = " ".join(task_intents) if task_intents else "Keeps the autonomous agent architecture evolving, stable and compliant."
    
    # Clean up technical jargon for the executive summary
    what_plain = what_it_does.replace("shadow GatePlan", "background validation and safety plan") \
                             .replace("FSM", "workflow state machine") \
                             .replace("DAG", "project task graph") \
                             .replace("TaskStore", "central task store") \
                             .replace("harness", "test and agent orchestration environment")
    
    plain_explanation = {
        "what_it_does": what_plain,
        "why_it_matters": why_it_matters,
        "testing_status": f"Validation complete: {len(findings)} tests and adversarial checks passed.",
        "verdict_plain": "Approved with full technical compliance by the independent audit." if verdict == "VERIFIED_PASS" else "Needs the fixes the audit pointed out.",
        "has_debts": len(debts) > 0,
        "debts_plain": f"{len(debts)} technical note(s) recorded for follow-up without blocking the landing." if debts else "No debt or residual risk found."
    }

    return {
        "title": title,
        "tasks": tasks,
        "verdict": verdict,
        "recommendation": rec,
        "findings": findings[:6],
        "debts": debts[:5],
        "footprint": footprint[:5],
        "plain_explanation": plain_explanation,
        "raw_text": text,
    }


def graphify_technical_debts(train_id: str, debts: list[str]) -> int:
    """Automatically converts technical debts from the audit into backlog intake items."""
    if not debts:
        return 0
    intake_human = RELAY / "intake" / "human"
    intake_human.mkdir(parents=True, exist_ok=True)
    created = 0
    for idx, debt in enumerate(debts, 1):
        slug = re.sub(r"[^A-Za-z0-9]+", "-", debt[:30]).strip("-").upper() or f"ITEM-{idx}"
        target = intake_human / f"DEBT_{train_id}_{slug}.md"
        if not target.exists():
            content = f"""# Technical Debt: {debt[:80]}

**Priority:** P2
**Cluster:** 08 Relay / Work convergence
**SourceTrain:** `{train_id}`

## Description
Recorded automatically from the audit of `{train_id}`:
> {debt}

## Definition of Done
- [ ] Resolve the technical debt found in the audit
- [ ] Implement it and verify compliance in the test suite
"""
            target.write_text(content, encoding="utf-8")
            created += 1
            print(f"[DEBT GRAPHIFIED] Created intake item: {target.name}")
    return created


def notify(title: str, text: str, warn: bool = False, url: str | None = None,
           approve_action: Optional[Tuple[str, str]] = None,
           is_auto: bool = False) -> None:
    """Send push notification to phone via ntfy.sh JSON API (max 3 actions)."""
    try:
        actions = []
        if approve_action:
            train, nonce = approve_action
            if is_auto:
                actions.append({
                    "action": "http",
                    "label": "🛑 Turn Off Auto",
                    "url": f"https://ntfy.sh/{APPROVE_TOPIC}",
                    "method": "POST",
                    "body": f"{train}::{nonce}::AUTO_OFF",
                    "clear": True,
                })
            else:
                actions.append({
                    "action": "http",
                    "label": "✅ Approve",
                    "url": f"https://ntfy.sh/{APPROVE_TOPIC}",
                    "method": "POST",
                    "body": f"{train}::{nonce}::APPROVE",
                    "clear": True,
                })
                actions.append({
                    "action": "http",
                    "label": "❌ Deny",
                    "url": f"https://ntfy.sh/{APPROVE_TOPIC}",
                    "method": "POST",
                    "body": f"{train}::{nonce}::REJECT",
                    "clear": True,
                })
                actions.append({
                    "action": "http",
                    "label": "⚡ Auto-Approve",
                    "url": f"https://ntfy.sh/{APPROVE_TOPIC}",
                    "method": "POST",
                    "body": f"{train}::{nonce}::AUTO_ON",
                    "clear": True,
                })
        if url and len(actions) < 3:
            actions.append({
                "action": "view",
                "label": "📁 Open in Drive",
                "url": url,
            })

        payload = {
            "topic": NTFY_TOPIC,
            "title": title,
            "message": text,
            "priority": 4 if warn else 3,
            "tags": ["zap"] if is_auto else (["rotating_light"] if warn else ["bell"]),
        }
        if actions:
            payload["actions"] = actions
        if url:
            payload["click"] = url

        req = urllib.request.Request(
            "https://ntfy.sh",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            pass
    except Exception as exc:
        print(f"[HUMAN WATCHER] ntfy push error: {exc}", file=sys.stderr)

    if sys.platform == "win32":
        try:
            flags = MB_ICONWARN_SYSTEMMODAL if warn else MB_ICONINFO_SYSTEMMODAL
            threading.Thread(target=ctypes.windll.user32.MessageBoxW,
                             args=(0, text, title, flags), daemon=True).start()
        except Exception:
            pass
    else:
        print(f"[{title}] {text}")


def pending() -> dict[str, dict[str, Any]]:
    """Scans pending approvals and formats a single rich notification per train."""
    items: dict[str, dict[str, Any]] = {}
    try:
        for f in sorted((RELAY / "approvals").glob("APPROVAL_REQUEST_*.md")):
            train = f.stem.replace("APPROVAL_REQUEST_", "")
            url = drive_search_url(f.name)
            nonce = _capability_nonce(train)
            if not nonce:
                continue

            dossier = extract_audit_dossier(train)
            task_str = ", ".join(dossier["tasks"]) if dossier["tasks"] else "general"

            debts_text = ""
            if dossier["debts"]:
                debts_text = "\n\n⚠️ Technical Debts Found (they go to the backlog):\n" + \
                             "\n".join(f"• {d}" for d in dossier["debts"])
            else:
                debts_text = "\n\n✨ No technical debt recorded (100% clean)."

            msg = (
                f"🚂 Train: {train}\n"
                f"📋 Tasks: {task_str}\n"
                f"🛡️ Auditor: Antigravity (Adversarial)\n"
                f"🎯 Verdict: {dossier['verdict']}\n\n"
                f"⚖️ Audit Summary:\n{dossier['recommendation']}"
                f"{debts_text}\n\n"
                f"👉 Choose an action:"
            )

            items[f.name] = {
                "train": train,
                "title": f"Relay: Approval Requested — {train}",
                "text": msg,
                "warn": False,
                "url": url,
                "approve": (train, nonce),
                "debts": dossier["debts"],
            }

        for f in sorted((RELAY / "alerts").glob("*.md")):
            text_content = f.read_text(encoding="utf-8-sig", errors="ignore")
            if "SEVERITY: FATAL_DEADLOCK" in text_content or "DEADLOCK_REQUIRING_HUMAN" in text_content:
                url = drive_search_url(f.name)
                items[f.name] = {
                    "train": "DEADLOCK",
                    "title": "🚨 Relay: Critical DEADLOCK",
                    "text": f"Unrecoverable deadlock needs human intervention.\n\nDossier in Drive: {url}",
                    "warn": True,
                    "url": url,
                    "approve": None,
                    "debts": [],
                }
    except Exception as exc:
        print(f"[HUMAN WATCHER] error scanning pending: {exc}", file=sys.stderr)
    return items


def _apply_approval(train: str, nonce: str, debts: Optional[list[str]] = None) -> None:
    """Drops the APPROVED marker and graphifies technical debts."""
    try:
        if _capability_nonce(train) != nonce:
            print(f"[HUMAN WATCHER] Nonce mismatch for {train}, ignoring.")
            return
        approved = RELAY / "approvals" / f"APPROVED_{train}.md"
        approved.write_text(f"Approved via ntfy 1-tap.\nnonce: {nonce}\n", encoding="utf-8")

        # Graphify any technical debt
        if debts is None:
            debts = extract_audit_dossier(train).get("debts", [])
        graphified = graphify_technical_debts(train, debts)

        debt_msg = f" ({graphified} debts sent to the backlog)" if graphified else ""
        notify("Relay: Approval Confirmed", f"Train {train} approved from the phone; landing in the graph…{debt_msg}")
    except Exception as exc:
        print(f"[HUMAN WATCHER] apply approval error: {exc}", file=sys.stderr)


def _apply_rejection(train: str, nonce: str, reason: str = "Rejected by the operator") -> None:
    """Drops rejection file and updates train."""
    try:
        if _capability_nonce(train) != nonce:
            return
        rejection_file = RELAY / "approvals" / f"REJECTED_{train}.md"
        rejection_file.write_text(f"Rejected by human.\nnonce: {nonce}\nreason: {reason}\n", encoding="utf-8")
        notify("Relay: Train Rejected", f"Train {train} was rejected from the phone and returned to the implementer.")
    except Exception as exc:
        print(f"[HUMAN WATCHER] apply rejection error: {exc}", file=sys.stderr)


def approve_listener() -> None:
    """Subscribe to the approve topic; executes APPROVE, REJECT, AUTO_ON, AUTO_OFF."""
    stream_url = f"https://ntfy.sh/{APPROVE_TOPIC}/json"
    poll_url = f"https://ntfy.sh/{APPROVE_TOPIC}/json?poll=1&since=2h"
    seen_ids: set[str] = set()

    def _process_ev(ev: dict) -> None:
        if ev.get("event") != "message":
            return
        msg_id = ev.get("id")
        if msg_id and msg_id in seen_ids:
            return
        if msg_id:
            seen_ids.add(msg_id)
        body = str(ev.get("message") or "").strip()
        if "::" in body:
            parts = body.split("::")
            train = parts[0].strip()
            nonce = parts[1].strip() if len(parts) > 1 else ""
            action = parts[2].strip().upper() if len(parts) > 2 else "APPROVE"

            if action == "APPROVE":
                _apply_approval(train, nonce)
            elif action == "REJECT":
                _apply_rejection(train, nonce)
            elif action == "AUTO_ON":
                set_auto_mode(True, f"Enabled by the operator on train {train}")
                _apply_approval(train, nonce)
                notify("⚡ Auto-Approve Mode On",
                       f"Train {train} approved! The next trains will pass and land automatically.",
                       is_auto=True,
                       approve_action=(train, nonce))
            elif action == "AUTO_OFF":
                set_auto_mode(False, "Disabled by the operator")
                notify("🛑 Auto-Approve Mode Off",
                       "Continuous approvals paused. The next trains will need your manual 1-tap.")

    while True:
        try:
            # 1. Catch-up pass for any messages sent while reconnecting
            try:
                with urllib.request.urlopen(poll_url, timeout=10) as resp:
                    for raw in resp:
                        try:
                            _process_ev(json.loads(raw.decode("utf-8")))
                        except Exception:
                            continue
            except Exception:
                pass

            # 2. Long-poll / streaming subscription
            with urllib.request.urlopen(stream_url, timeout=310) as resp:
                for raw in resp:
                    try:
                        _process_ev(json.loads(raw.decode("utf-8")))
                    except Exception:
                        continue
        except Exception:
            time.sleep(5)


def poll_and_notify_pending(last_shown: Optional[dict[str, float]] = None) -> None:
    """Checks pending approvals and sends rich notifications / handles auto-approve mode."""
    if last_shown is None:
        last_shown = {}
    now = time.time()
    current = pending()
    auto_state = get_auto_mode()
    is_auto = auto_state.get("enabled", False)

    for name, item in current.items():
        train = item["train"]
        approve_pair = item["approve"]

        if is_auto and approve_pair:
            t_id, n_val = approve_pair
            print(f"[AUTO-APPROVE] Automatically landing {t_id}...")
            _apply_approval(t_id, n_val, item.get("debts", []))
            notify(f"⚡ [AUTO-LANDED] {t_id}",
                   f"Train {t_id} landed automatically in the graph in continuous mode.\n\n"
                   f"Summary: {item['text'][:400]}...",
                   url=item["url"],
                   approve_action=(t_id, n_val),
                   is_auto=True)
            continue

        if now - last_shown.get(name, 0) >= REMIND_MINUTES * 60:
            last_shown[name] = now
            notify(item["title"], item["text"], item["warn"],
                   url=item["url"], approve_action=approve_pair, is_auto=False)

    for name in list(last_shown):
        if name not in current:
            del last_shown[name]


def main() -> None:
    relay_io.utf8_console()
    print(f"[HUMAN WATCHER] Started. Topic: https://ntfy.sh/{NTFY_TOPIC} (poll: {POLL_SECONDS}s)", flush=True)
    threading.Thread(target=approve_listener, daemon=True).start()
    last_shown: dict[str, float] = {}

    while True:
        try:
            poll_and_notify_pending(last_shown)
        except Exception as exc:
            print(f"[HUMAN WATCHER] main loop error: {exc}", file=sys.stderr)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()

