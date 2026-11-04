#!/usr/bin/env python3
"""
relay/round_table_mediation.py — Pure Deterministic Mediation & Frugal Fallbacks
================================================================================
Implements pure deterministic patch compilation and frugal human-revision gates
for the Multi-Chair Round Table consensus engine:
- ATX Markdown Section Parser (levels 1-6, fail-closed on duplicates/hierarchy)
- Canonical Section Inventory Generator (LF-normalized SHA-256)
- Proposed Patch Schema Validator (append_clause, replace_section, replace_exact)
- Conflict Detector (section collisions, range overlaps, multiple ops per heading)
- Deterministic Revision Compiler & Artifact Generator (compiled.md, report.json)
- Strict Auto-Seal Gating (single blocker, valid patch, 3/3 chairs, no human-review markers)
- Deterministic Narrative Scribe
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

ATX_HEADING_REGEX = re.compile(r"^(#{1,6})\s+(.*?)(?:\s+#+)?\s*$")
HUMAN_REVIEW_MARKER = "round-table:human-review"
ALLOWED_PATCH_OPS = {"append_clause", "replace_section", "replace_exact"}
MAX_OPS_PER_BLOCKER = 8


def normalize_text(text: str) -> str:
    """Canonical LF normalization across Windows CRLF and Unix LF (RT-N1)."""
    if not isinstance(text, str):
        text = str(text)
    return text.replace("\r\n", "\n").replace("\r", "\n")


def sha256_text(content: str) -> str:
    """Computes SHA-256 on canonical LF-normalized UTF-8 bytes (RT-N1)."""
    norm = normalize_text(content)
    return hashlib.sha256(norm.encode("utf-8", errors="replace")).hexdigest()


def parse_markdown_sections(text: str) -> List[Dict[str, Any]]:
    """
    Parses ATX markdown headings (levels 1 to 6) and extracts section boundaries.
    A section starts at a heading line and terminates at the next heading of
    level <= the section's level, or at EOF.
    """
    norm = normalize_text(text)
    lines = norm.split("\n")

    heading_indices: List[Tuple[int, int, str, str]] = []
    open_fence: Optional[Tuple[str, int]] = None
    for idx, line in enumerate(lines):
        stripped = line.lstrip()
        fence_match = re.match(r"^(`{3,}|~{3,})", stripped)
        if fence_match:
            marker = fence_match.group(1)
            fence = (marker[0], len(marker))
            if open_fence is None:
                open_fence = fence
            elif fence[0] == open_fence[0] and fence[1] >= open_fence[1]:
                open_fence = None
            continue
        if open_fence is not None:
            continue
        m = ATX_HEADING_REGEX.match(line)
        if m:
            level = len(m.group(1))
            title = m.group(2).strip()
            heading_indices.append((idx, level, line.rstrip(), title))

    if not heading_indices:
        sha = sha256_text(norm)
        return [{
            "level": 0,
            "heading": "",
            "heading_title": "",
            "start_line": 0,
            "end_line": len(lines),
            "body": norm,
            "full_text": norm,
            "sha256": sha,
            "has_human_review": HUMAN_REVIEW_MARKER in norm.lower(),
            "is_duplicate": False,
        }]

    heading_counts = Counter(h[2] for h in heading_indices)
    sections: List[Dict[str, Any]] = []

    for i, (start_idx, level, heading_line, title) in enumerate(heading_indices):
        end_idx = len(lines)
        for next_start, next_level, _, _ in heading_indices[i + 1:]:
            if next_level <= level:
                end_idx = next_start
                break

        body_lines = lines[start_idx + 1:end_idx]
        body_text = "\n".join(body_lines)
        full_section_lines = lines[start_idx:end_idx]
        full_text = "\n".join(full_section_lines)

        sha = sha256_text(full_text)
        has_hr = HUMAN_REVIEW_MARKER in full_text.lower()
        is_dup = heading_counts[heading_line] > 1

        sections.append({
            "level": level,
            "heading": heading_line,
            "heading_title": title,
            "start_line": start_idx,
            "end_line": end_idx,
            "body": body_text,
            "full_text": full_text,
            "sha256": sha,
            "has_human_review": has_hr,
            "is_duplicate": is_dup,
        })

    return sections


def section_inventory(text: str) -> List[Dict[str, Any]]:
    """Generates a structured inventory of all sections with headings and SHA-256 hashes."""
    sections = parse_markdown_sections(text)
    return [
        {
            "heading": sec["heading"],
            "level": sec["level"],
            "sha256": sec["sha256"],
            "has_human_review": sec["has_human_review"],
            "is_duplicate": sec["is_duplicate"],
        }
        for sec in sections
        if sec["heading"]
    ]


def format_section_inventory_prompt(text: str, base_version: str, base_sha256: str) -> str:
    """Renders a compact markdown inventory of headings and hashes for chair prompts."""
    inv = section_inventory(text)
    lines = [
        f"### CANONICAL SECTION INVENTORY OF THE PROPOSAL (Base: `{base_version}`, SHA-256: `{base_sha256}`):",
        "If you vote 'REVISE' with a blocker, you MAY OPTIONALLY include a structured `proposed_patch`.",
        "For deterministic auto-resolution, copy the exact heading and its matching `expected_sha256`:",
        "",
    ]
    if not inv:
        lines.append("- (Document without structured headings)")
    else:
        for item in inv:
            hr_flag = " ⚠️ [human-review]" if item["has_human_review"] else ""
            lines.append(f"- Heading: `{item['heading']}` | Hash: `{item['sha256']}`{hr_flag}")
    return "\n".join(lines)


def _check_forbidden_headings(content: str, target_level: int) -> Optional[str]:
    """Ensures content does not introduce ATX headings of level <= target section level."""
    for line in content.split("\n"):
        m = ATX_HEADING_REGEX.match(line)
        if m:
            lvl = len(m.group(1))
            if lvl <= target_level:
                return f"Content introduces heading of level {lvl} ('{line.strip()}'), which breaks hierarchy for target level {target_level}."
    return None


def validate_proposed_patch(
    patch: Any,
    doc_text: str,
    expected_base_version: str,
    expected_base_sha256: str,
) -> Tuple[bool, Optional[Dict[str, Any]], str]:
    """
    Strict fail-closed validation of proposed_patch against document text and base hash.
    Does NOT throw on invalid schema; returns (valid, validated_patch, error_reason).
    """
    if not isinstance(patch, dict):
        return False, None, "Patch payload is not a valid JSON dictionary."

    base_v = str(patch.get("base_version", "")).strip()
    if base_v != expected_base_version:
        return False, None, f"base_version mismatch: got '{base_v}', expected '{expected_base_version}'."

    base_sha = str(patch.get("base_sha256", "")).strip().lower()
    if base_sha != expected_base_sha256.lower():
        return False, None, f"base_sha256 mismatch: got '{base_sha}', expected '{expected_base_sha256}'."

    raw_ops = patch.get("operations")
    if not isinstance(raw_ops, list):
        return False, None, "Patch 'operations' must be a list."

    if len(raw_ops) == 0:
        return False, None, "Patch 'operations' list cannot be empty."

    if len(raw_ops) > MAX_OPS_PER_BLOCKER:
        return False, None, f"Patch exceeds maximum allowed operations per blocker ({len(raw_ops)} > {MAX_OPS_PER_BLOCKER})."

    sections = parse_markdown_sections(doc_text)
    sec_map: Dict[str, Dict[str, Any]] = {}
    dup_headings: Set[str] = set()

    for s in sections:
        h = s["heading"]
        if not h:
            continue
        if s["is_duplicate"]:
            dup_headings.add(h)
        else:
            sec_map[h] = s

    seen_headings_in_blocker: Set[str] = set()
    validated_ops: List[Dict[str, Any]] = []

    for idx, op_data in enumerate(raw_ops):
        if not isinstance(op_data, dict):
            return False, None, f"Operation #{idx+1} is not a valid dictionary object."

        op = str(op_data.get("op", "")).strip()
        if op not in ALLOWED_PATCH_OPS:
            return False, None, f"Unknown operation '{op}' in op #{idx+1}. Allowed: {sorted(ALLOWED_PATCH_OPS)}."

        heading = str(op_data.get("heading", "")).strip()
        if not heading:
            return False, None, f"Op #{idx+1} missing required 'heading'."

        if heading in seen_headings_in_blocker:
            return False, None, f"Multiple operations targeting same heading '{heading}' within the same blocker."
        seen_headings_in_blocker.add(heading)

        if heading in dup_headings:
            return False, None, f"Target heading '{heading}' is duplicate in document."

        if heading not in sec_map:
            return False, None, f"Target heading '{heading}' not found in document."

        target_sec = sec_map[heading]
        expected_sha = str(op_data.get("expected_sha256", "")).strip().lower()
        if not expected_sha or len(expected_sha) != 64:
            return False, None, f"Op #{idx+1} missing valid 64-character 'expected_sha256'."

        if target_sec["sha256"].lower() != expected_sha:
            return False, None, f"Heading '{heading}' hash mismatch: expected {expected_sha}, computed {target_sec['sha256']}."

        target_level = target_sec["level"]

        if op == "replace_section":
            if "content" not in op_data or not isinstance(op_data["content"], str):
                return False, None, f"Op #{idx+1} (replace_section) requires string 'content'."
            content = normalize_text(op_data["content"])
            if "\x00" in content:
                return False, None, f"Op #{idx+1} contains forbidden NUL character."
            forbidden_err = _check_forbidden_headings(content, target_level)
            if forbidden_err:
                return False, None, f"Op #{idx+1} (replace_section) violation: {forbidden_err}"
            validated_ops.append({
                "op": "replace_section",
                "heading": heading,
                "expected_sha256": expected_sha,
                "content": content,
            })

        elif op == "append_clause":
            if "content" not in op_data or not isinstance(op_data["content"], str):
                return False, None, f"Op #{idx+1} (append_clause) requires string 'content'."
            content = normalize_text(op_data["content"]).strip()
            if not content:
                return False, None, f"Op #{idx+1} (append_clause) 'content' cannot be empty."
            if "\x00" in content:
                return False, None, f"Op #{idx+1} contains forbidden NUL character."
            forbidden_err = _check_forbidden_headings(content, target_level)
            if forbidden_err:
                return False, None, f"Op #{idx+1} (append_clause) violation: {forbidden_err}"
            validated_ops.append({
                "op": "append_clause",
                "heading": heading,
                "expected_sha256": expected_sha,
                "content": content,
            })

        elif op == "replace_exact":
            if "old" not in op_data or not isinstance(op_data["old"], str):
                return False, None, f"Op #{idx+1} (replace_exact) requires string 'old'."
            if "new" not in op_data or not isinstance(op_data["new"], str):
                return False, None, f"Op #{idx+1} (replace_exact) requires string 'new'."
            old_str = normalize_text(op_data["old"])
            new_str = normalize_text(op_data["new"])
            if not old_str:
                return False, None, f"Op #{idx+1} (replace_exact) 'old' string cannot be empty."
            if "\x00" in old_str or "\x00" in new_str:
                return False, None, f"Op #{idx+1} contains forbidden NUL character."

            # Verify old_str exists exactly once in the target section full_text
            count = target_sec["full_text"].count(old_str)
            if count == 0:
                return False, None, f"Op #{idx+1} (replace_exact): 'old' string not found in section '{heading}'."
            if count > 1:
                return False, None, f"Op #{idx+1} (replace_exact): 'old' string is not unique in section '{heading}' ({count} occurrences)."

            forbidden_err = _check_forbidden_headings(new_str, target_level)
            if forbidden_err:
                return False, None, f"Op #{idx+1} (replace_exact) violation: {forbidden_err}"

            validated_ops.append({
                "op": "replace_exact",
                "heading": heading,
                "expected_sha256": expected_sha,
                "old": old_str,
                "new": new_str,
            })

    return True, {
        "base_version": base_v,
        "base_sha256": base_sha,
        "operations": validated_ops,
    }, "OK"


def _detect_conflicts(
    candidate_patches: List[Dict[str, Any]],
    sections: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Set[Tuple[str, str]]]:
    """
    Detects conflicts across candidate patches:
    1. Section collision: Multiple distinct blockers modifying the same heading.
    2. Range overlap: Operations modifying parent and child nested sections in the same round.
    Returns (conflicts_list, conflicting_issue_ids).
    """
    conflicts: List[Dict[str, Any]] = []
    conflicting_issue_keys: Set[Tuple[str, str]] = set()

    # 1. Section collision map
    heading_to_blockers: Dict[str, List[Dict[str, Any]]] = {}
    for item in candidate_patches:
        patch = item["patch"]
        for op in patch["operations"]:
            h = op["heading"]
            heading_to_blockers.setdefault(h, []).append(item)

    for h, items in heading_to_blockers.items():
        if len(items) > 1:
            seats = [it["source_seat"] for it in items]
            issue_ids = [it["issue_id"] for it in items]
            for it in items:
                conflicting_issue_keys.add((it["source_seat"], it["issue_id"]))
            conflicts.append({
                "type": "SECTION_COLLISION",
                "heading": h,
                "seats": seats,
                "issues": issue_ids,
                "reason": f"Multiple distinct blockers targeting the same section heading '{h}'."
            })

    # 2. Range overlap detection among non-disjoint sections
    sec_span_map = {s["heading"]: (s["start_line"], s["end_line"]) for s in sections if s["heading"]}
    op_items: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    for item in candidate_patches:
        for operation in item["patch"]["operations"]:
            op_items.append((item, operation))

    for i, (it1, op1) in enumerate(op_items):
        for it2, op2 in op_items[i + 1:]:
            h1 = op1["heading"]
            h2 = op2["heading"]
            span1 = sec_span_map.get(h1)
            span2 = sec_span_map.get(h2)
            if not span1 or not span2 or h1 == h2:
                continue
            start1, end1 = span1
            start2, end2 = span2
            overlap = (start1 <= start2 < end1) or (start2 <= start1 < end2)
            if overlap:
                key1 = (it1["source_seat"], it1["issue_id"])
                key2 = (it2["source_seat"], it2["issue_id"])
                conflicting_issue_keys.update((key1, key2))
                conflicts.append({
                    "type": "RANGE_OVERLAP",
                    "headings": [h1, h2],
                    "seats": [it1["source_seat"], it2["source_seat"]],
                    "issues": [it1["issue_id"], it2["issue_id"]],
                    "reason": f"Operations target overlapping section spans '{h1}' and '{h2}'."
                })

    return conflicts, conflicting_issue_keys


def compile_revision(
    base_text: str,
    base_version: str,
    base_sha256: str,
    seat_votes: Dict[str, Any],
    seat_deployments: Optional[Dict[str, Any]] = None,
    case_id: str = "CASE",
    round_num: int = 1,
    structure_validator: Optional[Callable[[str], Tuple[bool, List[str]]]] = None,
    chairs_independent: Optional[bool] = None,
) -> Tuple[str, Dict[str, Any]]:
    """
    Pure deterministic compilation of proposal revisions:
    - Extracts blockers and optional proposed_patch from all seat votes.
    - Injects source_seat into each issue record.
    - Validates patches and detects conflicts.
    - Applies non-conflicting ops from bottom to top.
    - Determines outcome: AUTO_SEAL or AWAITING_HUMAN_REVISION.
    - Emits structured report (schema_version 1) and compiled markdown text.
    """
    norm_base = normalize_text(base_text)
    sections = parse_markdown_sections(norm_base)
    lines = norm_base.split("\n")

    all_blocking_issues: List[Dict[str, Any]] = []
    for seat_id, vote in seat_votes.items():
        if not isinstance(vote, dict):
            continue
        issues = vote.get("issues") or []
        if not isinstance(issues, list):
            continue
        for iss in issues:
            if isinstance(iss, dict) and iss.get("severity") == "blocking":
                iss_copy = dict(iss)
                iss_copy["source_seat"] = seat_id
                all_blocking_issues.append(iss_copy)

    all_blocking_issues.sort(key=lambda x: (str(x.get("source_seat", "")), str(x.get("id", ""))))

    report_issues: List[Dict[str, Any]] = []
    candidate_patches: List[Dict[str, Any]] = []
    human_review_reasons: List[str] = []

    for iss in all_blocking_issues:
        seat_id = iss["source_seat"]
        iss_id = str(iss.get("id", f"ISS-{seat_id.upper()}"))
        raw_patch = iss.get("proposed_patch")

        if not raw_patch:
            report_issues.append({
                "source_seat": seat_id,
                "id": iss_id,
                "status": "NO_PATCH",
                "reason": "No proposed_patch provided in vote.",
                "headings": [],
                "ops_count": 0,
            })
            human_review_reasons.append(f"Blocker '{iss_id}' from seat '{seat_id}' provided no proposed_patch.")
            continue

        valid, val_patch, err_msg = validate_proposed_patch(
            raw_patch,
            norm_base,
            base_version,
            base_sha256,
        )

        if not valid or not val_patch:
            report_issues.append({
                "source_seat": seat_id,
                "id": iss_id,
                "status": "REJECTED",
                "reason": f"Invalid patch: {err_msg}",
                "headings": [op.get("heading") for op in raw_patch.get("operations", []) if isinstance(op, dict) and "heading" in op],
                "ops_count": len(raw_patch.get("operations", [])) if isinstance(raw_patch.get("operations"), list) else 0,
            })
            human_review_reasons.append(f"Patch for blocker '{iss_id}' rejected: {err_msg}")
        else:
            candidate_patches.append({
                "source_seat": seat_id,
                "issue_id": iss_id,
                "patch": val_patch,
            })

    conflicts, conflicting_issue_keys = _detect_conflicts(candidate_patches, sections)
    if conflicts:
        for c in conflicts:
            human_review_reasons.append(f"Conflict detected ({c['type']}): {c['reason']}")

    applicable_patches: List[Dict[str, Any]] = []
    for cand in candidate_patches:
        iss_id = cand["issue_id"]
        seat_id = cand["source_seat"]
        val_patch = cand["patch"]
        headings = [op["heading"] for op in val_patch["operations"]]
        ops_cnt = len(val_patch["operations"])

        if (seat_id, iss_id) in conflicting_issue_keys:
            report_issues.append({
                "source_seat": seat_id,
                "id": iss_id,
                "status": "CONFLICT",
                "reason": "Patch rejected due to conflict with another issue/section.",
                "headings": headings,
                "ops_count": ops_cnt,
            })
        else:
            applicable_patches.append(cand)
            report_issues.append({
                "source_seat": seat_id,
                "id": iss_id,
                "status": "APPLIED",
                "reason": "OK",
                "headings": headings,
                "ops_count": ops_cnt,
            })

    report_issues.sort(key=lambda x: (x["source_seat"], x["id"]))

    sec_by_heading = {s["heading"]: s for s in sections if s["heading"]}
    ops_to_apply: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []

    for cand in applicable_patches:
        for op in cand["patch"]["operations"]:
            target_sec = sec_by_heading.get(op["heading"])
            if target_sec:
                ops_to_apply.append((target_sec, op))

    ops_to_apply.sort(key=lambda x: x[0]["start_line"], reverse=True)

    touched_sections: Set[str] = set()
    compiled_lines = list(lines)

    for target_sec, op in ops_to_apply:
        heading = op["heading"]
        touched_sections.add(heading)
        start_idx = target_sec["start_line"]
        end_idx = target_sec["end_line"]
        orig_sec_lines = compiled_lines[start_idx:end_idx]
        heading_line = orig_sec_lines[0]
        op_type = op["op"]

        if target_sec["has_human_review"]:
            human_review_reasons.append(f"Section '{heading}' contains round-table:human-review marker.")

        if op_type == "replace_section":
            content_str = op["content"].strip()
            if content_str:
                new_sec_lines = [heading_line, content_str]
            else:
                new_sec_lines = [heading_line]
            compiled_lines[start_idx:end_idx] = new_sec_lines

        elif op_type == "append_clause":
            content_str = op["content"].strip()
            orig_text = "\n".join(orig_sec_lines)
            new_text = orig_text + "\n\n" + content_str
            compiled_lines[start_idx:end_idx] = new_text.split("\n")

        elif op_type == "replace_exact":
            orig_text = "\n".join(orig_sec_lines)
            new_text = orig_text.replace(op["old"], op["new"], 1)
            compiled_lines[start_idx:end_idx] = new_text.split("\n")

    compiled_text = normalize_text("\n".join(compiled_lines))
    compiled_sha = sha256_text(compiled_text) if compiled_text else None

    if structure_validator is None:
        struct_valid, missing_struct = True, []
    else:
        struct_valid, missing_struct = structure_validator(compiled_text)
    if not struct_valid:
        human_review_reasons.append(f"Structural validation failed: missing {', '.join(missing_struct)}.")

    total_blockers = len(all_blocking_issues)
    applied_count = sum(1 for iss in report_issues if iss["status"] == "APPLIED")
    has_hr_marker = any(sec_by_heading[h]["has_human_review"] for h in touched_sections if h in sec_by_heading)
    has_three_chairs = len(seat_votes) == 3 and all(v.get("verdict") in ("APPROVE", "REVISE", "REJECT") for v in seat_votes.values())
    chair_gate = has_three_chairs and chairs_independent is not False

    if chairs_independent is False:
        human_review_reasons.append("The effective chair composition is incomplete or does not contain three independent bias domains.")

    if total_blockers > 1:
        human_review_reasons.append(f"Multiple blockers present ({total_blockers} blockers).")

    can_auto_seal = (
        total_blockers == 1
        and applied_count == 1
        and len(conflicts) == 0
        and not has_hr_marker
        and struct_valid
        and chair_gate
        and len(human_review_reasons) == 0
    )

    outcome = "AUTO_SEAL" if can_auto_seal else "AWAITING_HUMAN_REVISION"

    report: Dict[str, Any] = {
        "schema_version": 1,
        "case_id": case_id,
        "round": round_num,
        "base_version": base_version,
        "base_sha256": base_sha256,
        "compiled_sha256": compiled_sha,
        "outcome": outcome,
        "issues": report_issues,
        "touched_sections": sorted(list(touched_sections)),
        "conflicts": conflicts,
        "human_review_reasons": sorted(list(set(human_review_reasons))),
        "chairs_independent": chairs_independent,
    }

    return compiled_text, report


def generate_deterministic_scribe_narrative(
    case_id: str,
    title: str,
    verdict: str,
    current_version: str,
    version_sha256: str,
    seat_votes: Dict[str, Any],
    last_seat_deployments: Dict[str, Any],
    outcome_summary: str,
    timestamp: Optional[str] = None,
) -> str:
    """
    Projects authoritative deliberation data into a factual markdown narrative
    without invoking any LLM or external services.
    """
    ts = timestamp or datetime.now(timezone.utc).isoformat()
    lines = [
        f"### Factual Deliberation Report (Deterministic Scribe)",
        f"",
        f"- **Case:** `{case_id}`",
        f"- **Title:** {title}",
        f"- **Verdict:** `{verdict}`",
        f"- **Ratified Version:** `{current_version}` (`{version_sha256[:16]}...`)",
        f"- **Timestamp:** {ts}",
        f"",
        f"#### Seat Opinions:",
    ]

    for seat_id, vote in sorted(seat_votes.items()):
        if not isinstance(vote, dict):
            continue
        v_str = vote.get("verdict", "UNKNOWN")
        v_sum = vote.get("summary", "")
        dep_id = last_seat_deployments.get(seat_id, "unknown")
        lines.append(f"- **{seat_id.upper()}** (`{dep_id}`): Verdict `{v_str}` — {v_sum}")

    lines.extend([
        f"",
        f"#### Closure & Traceability:",
        f"{outcome_summary}",
    ])

    return "\n".join(lines)
