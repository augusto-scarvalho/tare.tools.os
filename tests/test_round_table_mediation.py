#!/usr/bin/env python3
"""
tests/test_round_table_mediation.py — Comprehensive Test Battery for Pure Deterministic Mediation
==================================================================================================
Covers:
- ATX Markdown Section Parser & Hierarchy (nested sections, bounds, duplicate detection)
- Canonical LF/CRLF Section Hash Integrity & Section Inventory
- Proposed Patch Operations (append_clause, replace_section, replace_exact)
- replace_exact with 0 and >1 occurrences (fail-closed)
- Invalid base_version, base_sha256, expected_sha256, unknown op, NUL byte, max ops bound
- Forbidden heading introductions (level <= target level)
- Conflict detection (same section different blockers, overlapping nested ranges, multiple ops same heading)
- Determinism & Idempotence (bit-for-bit repeatability)
- Legacy votes (without patch) & optional patch handling
- source_seat injection & untrusted payload isolation
- Auto-seal with single blocker without invoking text role / LLM
- Multiple blockers draft compilation without auto-seal -> AWAITING_HUMAN_REVISION
- round-table:human-review comment marker gating
- Revision artifacts (compiled.md, report.json schema 1) and Chained Ledger events
- LLM mediator opt-in only (--llm-mediator) and fallback to AWAITING_HUMAN_REVISION on failure
- Human revision submission (fail-closed zero mutation on error, success seals and increments round)
- Deterministic narrative scribe default (zero LLM calls) and --llm-scribe opt-in
- Compatibility with legacy cases lacking workflow_contract
"""
import hashlib
import json
from pathlib import Path
import pytest

from relay import round_table_engine, round_table_mediation


@pytest.fixture
def mock_rt_env(monkeypatch, tmp_path):
    rt_dir = tmp_path / "round_tables"
    rt_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(round_table_engine, "ROUND_TABLES_DIR", rt_dir)
    return rt_dir


# ==============================================================================================
# 1. ATX Parser, Section Hierarchy & Inventory
# ==============================================================================================
def test_markdown_section_parsing_and_nested_hierarchy():
    text = """# Proposal Title

Introductory overview text.

## 1. Objetivos Nucleares (In-Scope)
Core objective 1.

### 1.1 Detalhes de Implementação
Sub-detail paragraph.

## 1.2 Não-Objetivos Explícitos (Via Negativa / Fora de Escopo)
Explicit non-goals.

## 3. Matriz de Falsificação
Test matrix content.
"""
    sections = round_table_mediation.parse_markdown_sections(text)
    assert len(sections) == 5

    s_title = sections[0]
    assert s_title["level"] == 1
    assert s_title["heading"] == "# Proposal Title"
    assert s_title["heading_title"] == "Proposal Title"
    assert s_title["end_line"] == len(round_table_mediation.normalize_text(text).split("\n"))

    s_obj = sections[1]
    assert s_obj["level"] == 2
    assert s_obj["heading"] == "## 1. Objetivos Nucleares (In-Scope)"
    # Terminates at line of ## 1.2 Non-goals
    assert "Core objective 1." in s_obj["body"]
    assert "### 1.1 Detalhes de Implementação" in s_obj["body"]

    s_sub = sections[2]
    assert s_sub["level"] == 3
    assert s_sub["heading"] == "### 1.1 Detalhes de Implementação"
    assert "Sub-detail paragraph." in s_sub["body"]

    s_nongoals = sections[3]
    assert s_nongoals["level"] == 2
    assert s_nongoals["heading"].startswith("## 1.2 ")

    s_matrix = sections[4]
    assert s_matrix["level"] == 2
    assert s_matrix["heading"].startswith("## 3. ")


def test_markdown_headings_inside_fences_are_not_sections():
    text = """# Proposal

```markdown
## Example only
```

## Objetivos
Real objective.
"""
    headings = [item["heading"] for item in round_table_mediation.section_inventory(text)]
    assert headings == ["# Proposal", "## Objetivos"]

def test_lf_crlf_normalization_and_inventory_hash():
    text_lf = "## 1. Objetivos\nObjective text.\n\n## 1.2 Não-Objetivos\nNon goals.\n"
    text_crlf = "## 1. Objetivos\r\nObjective text.\r\n\r\n## 1.2 Não-Objetivos\r\nNon goals.\r\n"

    inv_lf = round_table_mediation.section_inventory(text_lf)
    inv_crlf = round_table_mediation.section_inventory(text_crlf)

    assert len(inv_lf) == 2
    assert len(inv_crlf) == 2
    assert inv_lf[0]["sha256"] == inv_crlf[0]["sha256"]
    assert inv_lf[1]["sha256"] == inv_crlf[1]["sha256"]

    prompt_table = round_table_mediation.format_section_inventory_prompt(text_lf, "v001", "a" * 64)
    assert "## 1. Objetivos" in prompt_table
    assert inv_lf[0]["sha256"] in prompt_table


def test_duplicate_and_missing_headings_fail_closed():
    text_with_dup = "## Section A\nBody 1\n\n## Section A\nBody 2\n"
    sections = round_table_mediation.parse_markdown_sections(text_with_dup)
    assert len(sections) == 2
    assert sections[0]["is_duplicate"] is True
    assert sections[1]["is_duplicate"] is True

    # Patch targeting duplicate heading must fail
    patch_dup = {
        "base_version": "v001",
        "base_sha256": round_table_mediation.sha256_text(text_with_dup),
        "operations": [{
            "op": "replace_section",
            "heading": "## Section A",
            "expected_sha256": sections[0]["sha256"],
            "content": "New content",
        }]
    }
    valid, _, err = round_table_mediation.validate_proposed_patch(
        patch_dup, text_with_dup, "v001", round_table_mediation.sha256_text(text_with_dup)
    )
    assert not valid
    assert "duplicate" in err

    # Patch targeting non-existent heading must fail
    patch_missing = {
        "base_version": "v001",
        "base_sha256": round_table_mediation.sha256_text(text_with_dup),
        "operations": [{
            "op": "replace_section",
            "heading": "## Non Existent Section",
            "expected_sha256": "0" * 64,
            "content": "New content",
        }]
    }
    valid, _, err = round_table_mediation.validate_proposed_patch(
        patch_missing, text_with_dup, "v001", round_table_mediation.sha256_text(text_with_dup)
    )
    assert not valid
    assert "not found" in err


# ==============================================================================================
# 2. Patch Operations: append_clause, replace_section, replace_exact
# ==============================================================================================
SAMPLE_DOC = """# Test Document Title

## 1. Objetivos Nucleares (In-Scope)
- Invariant 1: Bounded memory usage.
- Invariant 2: Fail closed on corrupt files.

## 1.2 Não-Objetivos Explícitos (Via Negativa / Fora de Escopo)
- Out of scope: AST rewriting.

## 3. Matriz de Falsificação & Rastreabilidade
- Test cases mapped.
"""


def test_proposed_patch_operations_append_clause():
    base_sha = round_table_mediation.sha256_text(SAMPLE_DOC)
    inv = {s["heading"]: s["sha256"] for s in round_table_mediation.section_inventory(SAMPLE_DOC)}

    patch = {
        "base_version": "v001",
        "base_sha256": base_sha,
        "operations": [{
            "op": "append_clause",
            "heading": "## 1. Objetivos Nucleares (In-Scope)",
            "expected_sha256": inv["## 1. Objetivos Nucleares (In-Scope)"],
            "content": "- Invariant 3: Added 64 KiB streaming bounded cursor.",
        }]
    }

    votes = {
        "openai": {
            "verdict": "REVISE",
            "summary": "Need streaming bounded cursor",
            "issues": [{
                "id": "ISS-OPENAI-01",
                "severity": "blocking",
                "claim": "Unbounded memory",
                "falsifier": "Large file overflow",
                "resolution": "Add bounded streaming",
                "proposed_patch": patch,
            }]
        },
        "anthropic": {"verdict": "APPROVE", "summary": "OK", "issues": []},
        "google": {"verdict": "APPROVE", "summary": "OK", "issues": []},
    }

    compiled_text, report = round_table_mediation.compile_revision(
        SAMPLE_DOC, "v001", base_sha, votes
    )
    assert report["outcome"] == "AUTO_SEAL"
    assert "- Invariant 3: Added 64 KiB streaming bounded cursor." in compiled_text
    assert "- Invariant 1: Bounded memory usage." in compiled_text
    assert "## 1.2 Não-Objetivos Explícitos" in compiled_text


def test_proposed_patch_operations_replace_section():
    base_sha = round_table_mediation.sha256_text(SAMPLE_DOC)
    inv = {s["heading"]: s["sha256"] for s in round_table_mediation.section_inventory(SAMPLE_DOC)}

    patch = {
        "base_version": "v001",
        "base_sha256": base_sha,
        "operations": [{
            "op": "replace_section",
            "heading": "## 1. Objetivos Nucleares (In-Scope)",
            "expected_sha256": inv["## 1. Objetivos Nucleares (In-Scope)"],
            "content": "- Total redesign: Fully bounded chunked pipelines only.",
        }]
    }

    votes = {
        "openai": {
            "verdict": "REVISE",
            "summary": "Section overhaul",
            "issues": [{
                "id": "ISS-OPENAI-01",
                "severity": "blocking",
                "claim": "Flawed design",
                "falsifier": "Deadlock",
                "resolution": "Redesign section",
                "proposed_patch": patch,
            }]
        },
        "anthropic": {"verdict": "APPROVE", "summary": "OK", "issues": []},
        "google": {"verdict": "APPROVE", "summary": "OK", "issues": []},
    }

    compiled_text, report = round_table_mediation.compile_revision(
        SAMPLE_DOC, "v001", base_sha, votes
    )
    assert report["outcome"] == "AUTO_SEAL"
    assert "## 1. Objetivos Nucleares (In-Scope)" in compiled_text
    assert "- Total redesign: Fully bounded chunked pipelines only." in compiled_text
    assert "- Invariant 1: Bounded memory usage." not in compiled_text


def test_proposed_patch_operations_replace_exact_success():
    base_sha = round_table_mediation.sha256_text(SAMPLE_DOC)
    inv = {s["heading"]: s["sha256"] for s in round_table_mediation.section_inventory(SAMPLE_DOC)}

    patch = {
        "base_version": "v001",
        "base_sha256": base_sha,
        "operations": [{
            "op": "replace_exact",
            "heading": "## 1. Objetivos Nucleares (In-Scope)",
            "expected_sha256": inv["## 1. Objetivos Nucleares (In-Scope)"],
            "old": "- Invariant 1: Bounded memory usage.",
            "new": "- Invariant 1: Strictly bounded heap footprint <= 16 MB.",
        }]
    }

    votes = {
        "google": {
            "verdict": "REVISE",
            "summary": "Clarify memory bounds",
            "issues": [{
                "id": "ISS-GOOGLE-01",
                "severity": "blocking",
                "claim": "Vague memory metric",
                "falsifier": "OOM",
                "resolution": "State bound in MB",
                "proposed_patch": patch,
            }]
        },
        "anthropic": {"verdict": "APPROVE", "summary": "OK", "issues": []},
        "openai": {"verdict": "APPROVE", "summary": "OK", "issues": []},
    }

    compiled_text, report = round_table_mediation.compile_revision(
        SAMPLE_DOC, "v001", base_sha, votes
    )
    assert report["outcome"] == "AUTO_SEAL"
    assert "- Invariant 1: Strictly bounded heap footprint <= 16 MB." in compiled_text
    assert "- Invariant 2: Fail closed on corrupt files." in compiled_text


def test_proposed_patch_operations_replace_exact_zero_or_multiple_occurrences():
    base_sha = round_table_mediation.sha256_text(SAMPLE_DOC)
    inv = {s["heading"]: s["sha256"] for s in round_table_mediation.section_inventory(SAMPLE_DOC)}

    # 1. Zero occurrences (old not found)
    patch_zero = {
        "base_version": "v001",
        "base_sha256": base_sha,
        "operations": [{
            "op": "replace_exact",
            "heading": "## 1. Objetivos Nucleares (In-Scope)",
            "expected_sha256": inv["## 1. Objetivos Nucleares (In-Scope)"],
            "old": "Text that does not exist in section",
            "new": "New text",
        }]
    }
    valid, _, err = round_table_mediation.validate_proposed_patch(
        patch_zero, SAMPLE_DOC, "v001", base_sha
    )
    assert not valid
    assert "not found" in err

    # 2. Multiple occurrences of string in section
    multi_doc = "## 1. Objetivos\nDuplicate target string.\nSome text.\nDuplicate target string.\n"
    multi_sha = round_table_mediation.sha256_text(multi_doc)
    multi_inv = round_table_mediation.section_inventory(multi_doc)

    patch_multi = {
        "base_version": "v001",
        "base_sha256": multi_sha,
        "operations": [{
            "op": "replace_exact",
            "heading": "## 1. Objetivos",
            "expected_sha256": multi_inv[0]["sha256"],
            "old": "Duplicate target string.",
            "new": "Single string.",
        }]
    }
    valid, _, err = round_table_mediation.validate_proposed_patch(
        patch_multi, multi_doc, "v001", multi_sha
    )
    assert not valid
    assert "not unique" in err


# ==============================================================================================
# 3. Fail-Closed Validation (Bad Hash, Base Mismatch, NUL byte, Forbidden Headings, Max Ops)
# ==============================================================================================
def test_invalid_base_and_operations_fail_closed():
    base_sha = round_table_mediation.sha256_text(SAMPLE_DOC)
    inv = {s["heading"]: s["sha256"] for s in round_table_mediation.section_inventory(SAMPLE_DOC)}

    # Wrong base_version
    p_bad_ver = {
        "base_version": "v999",
        "base_sha256": base_sha,
        "operations": [{"op": "append_clause", "heading": "## 1. Objetivos Nucleares (In-Scope)", "expected_sha256": inv["## 1. Objetivos Nucleares (In-Scope)"], "content": "x"}]
    }
    valid, _, err = round_table_mediation.validate_proposed_patch(p_bad_ver, SAMPLE_DOC, "v001", base_sha)
    assert not valid
    assert "base_version mismatch" in err

    # Wrong base_sha256
    p_bad_sha = {
        "base_version": "v001",
        "base_sha256": "f" * 64,
        "operations": [{"op": "append_clause", "heading": "## 1. Objetivos Nucleares (In-Scope)", "expected_sha256": inv["## 1. Objetivos Nucleares (In-Scope)"], "content": "x"}]
    }
    valid, _, err = round_table_mediation.validate_proposed_patch(p_bad_sha, SAMPLE_DOC, "v001", base_sha)
    assert not valid
    assert "base_sha256 mismatch" in err

    # Wrong expected_sha256 for section
    p_bad_sec_sha = {
        "base_version": "v001",
        "base_sha256": base_sha,
        "operations": [{"op": "append_clause", "heading": "## 1. Objetivos Nucleares (In-Scope)", "expected_sha256": "0" * 64, "content": "x"}]
    }
    valid, _, err = round_table_mediation.validate_proposed_patch(p_bad_sec_sha, SAMPLE_DOC, "v001", base_sha)
    assert not valid
    assert "hash mismatch" in err

    # Unknown operation (e.g. insert_after)
    p_unk_op = {
        "base_version": "v001",
        "base_sha256": base_sha,
        "operations": [{"op": "insert_after", "heading": "## 1. Objetivos Nucleares (In-Scope)", "expected_sha256": inv["## 1. Objetivos Nucleares (In-Scope)"], "content": "x"}]
    }
    valid, _, err = round_table_mediation.validate_proposed_patch(p_unk_op, SAMPLE_DOC, "v001", base_sha)
    assert not valid
    assert "Unknown operation" in err

    # NUL byte in payload
    p_nul = {
        "base_version": "v001",
        "base_sha256": base_sha,
        "operations": [{"op": "append_clause", "heading": "## 1. Objetivos Nucleares (In-Scope)", "expected_sha256": inv["## 1. Objetivos Nucleares (In-Scope)"], "content": "text with \x00 byte"}]
    }
    valid, _, err = round_table_mediation.validate_proposed_patch(p_nul, SAMPLE_DOC, "v001", base_sha)
    assert not valid
    assert "NUL" in err

    # Exceeding 8 ops per blocker
    p_too_many = {
        "base_version": "v001",
        "base_sha256": base_sha,
        "operations": [{"op": "append_clause", "heading": "## 1. Objetivos Nucleares (In-Scope)", "expected_sha256": inv["## 1. Objetivos Nucleares (In-Scope)"], "content": f"Clause {i}"} for i in range(9)]
    }
    valid, _, err = round_table_mediation.validate_proposed_patch(p_too_many, SAMPLE_DOC, "v001", base_sha)
    assert not valid
    assert "maximum allowed operations" in err


def test_forbidden_heading_introduction_fails_closed():
    base_sha = round_table_mediation.sha256_text(SAMPLE_DOC)
    inv = {s["heading"]: s["sha256"] for s in round_table_mediation.section_inventory(SAMPLE_DOC)}

    # Introducing ## (level 2 <= target level 2) inside a level 2 section
    p_bad_heading = {
        "base_version": "v001",
        "base_sha256": base_sha,
        "operations": [{
            "op": "append_clause",
            "heading": "## 1. Objetivos Nucleares (In-Scope)",
            "expected_sha256": inv["## 1. Objetivos Nucleares (In-Scope)"],
            "content": "## Malicious Hijacked Heading\nSome text.",
        }]
    }
    valid, _, err = round_table_mediation.validate_proposed_patch(p_bad_heading, SAMPLE_DOC, "v001", base_sha)
    assert not valid
    assert "breaks hierarchy" in err

    # Introducing ### (level 3 > target level 2) is allowed
    p_good_subheading = {
        "base_version": "v001",
        "base_sha256": base_sha,
        "operations": [{
            "op": "append_clause",
            "heading": "## 1. Objetivos Nucleares (In-Scope)",
            "expected_sha256": inv["## 1. Objetivos Nucleares (In-Scope)"],
            "content": "### 1.3 Valid Subsection\nSome text.",
        }]
    }
    valid, _, err = round_table_mediation.validate_proposed_patch(p_good_subheading, SAMPLE_DOC, "v001", base_sha)
    assert valid


# ==============================================================================================
# 4. Conflict Detection
# ==============================================================================================
def test_conflict_detection_same_section_different_blockers():
    base_sha = round_table_mediation.sha256_text(SAMPLE_DOC)
    inv = {s["heading"]: s["sha256"] for s in round_table_mediation.section_inventory(SAMPLE_DOC)}

    patch1 = {
        "base_version": "v001",
        "base_sha256": base_sha,
        "operations": [{
            "op": "append_clause",
            "heading": "## 1. Objetivos Nucleares (In-Scope)",
            "expected_sha256": inv["## 1. Objetivos Nucleares (In-Scope)"],
            "content": "- Patch from OpenAI",
        }]
    }
    patch2 = {
        "base_version": "v001",
        "base_sha256": base_sha,
        "operations": [{
            "op": "replace_section",
            "heading": "## 1. Objetivos Nucleares (In-Scope)",
            "expected_sha256": inv["## 1. Objetivos Nucleares (In-Scope)"],
            "content": "- Patch from Anthropic",
        }]
    }

    votes = {
        "openai": {
            "verdict": "REVISE",
            "summary": "Blocker 1",
            "issues": [{"id": "ISS-OAI", "severity": "blocking", "claim": "c1", "falsifier": "f1", "resolution": "r1", "proposed_patch": patch1}]
        },
        "anthropic": {
            "verdict": "REVISE",
            "summary": "Blocker 2",
            "issues": [{"id": "ISS-ANT", "severity": "blocking", "claim": "c2", "falsifier": "f2", "resolution": "r2", "proposed_patch": patch2}]
        },
        "google": {"verdict": "APPROVE", "summary": "OK", "issues": []},
    }

    _, report = round_table_mediation.compile_revision(SAMPLE_DOC, "v001", base_sha, votes)
    assert report["outcome"] == "AWAITING_HUMAN_REVISION"
    assert len(report["conflicts"]) > 0
    assert report["conflicts"][0]["type"] == "SECTION_COLLISION"
    assert any("Conflict detected" in r for r in report["human_review_reasons"])


def test_conflict_detection_multiple_ops_same_heading_same_blocker():
    base_sha = round_table_mediation.sha256_text(SAMPLE_DOC)
    inv = {s["heading"]: s["sha256"] for s in round_table_mediation.section_inventory(SAMPLE_DOC)}

    patch_double = {
        "base_version": "v001",
        "base_sha256": base_sha,
        "operations": [
            {
                "op": "append_clause",
                "heading": "## 1. Objetivos Nucleares (In-Scope)",
                "expected_sha256": inv["## 1. Objetivos Nucleares (In-Scope)"],
                "content": "- First op",
            },
            {
                "op": "append_clause",
                "heading": "## 1. Objetivos Nucleares (In-Scope)",
                "expected_sha256": inv["## 1. Objetivos Nucleares (In-Scope)"],
                "content": "- Second op on same heading",
            }
        ]
    }
    valid, _, err = round_table_mediation.validate_proposed_patch(patch_double, SAMPLE_DOC, "v001", base_sha)
    assert not valid
    assert "Multiple operations targeting same heading" in err


def test_conflict_detection_overlapping_ranges():
    doc_nested = """# Title

## 1. Parent Section
Parent text.

### 1.1 Child Section
Child text.

## 2. Non-Goals
Non goals text.
"""
    base_sha = round_table_mediation.sha256_text(doc_nested)
    inv = {s["heading"]: s["sha256"] for s in round_table_mediation.section_inventory(doc_nested)}

    patch_parent = {
        "base_version": "v001",
        "base_sha256": base_sha,
        "operations": [{
            "op": "replace_section",
            "heading": "## 1. Parent Section",
            "expected_sha256": inv["## 1. Parent Section"],
            "content": "Replaced parent entirely",
        }]
    }
    patch_child = {
        "base_version": "v001",
        "base_sha256": base_sha,
        "operations": [{
            "op": "append_clause",
            "heading": "### 1.1 Child Section",
            "expected_sha256": inv["### 1.1 Child Section"],
            "content": "Appended to child",
        }]
    }

    votes = {
        "openai": {
            "verdict": "REVISE",
            "summary": "Blocker parent",
            "issues": [{"id": "ISS-PARENT", "severity": "blocking", "claim": "p", "falsifier": "p", "resolution": "p", "proposed_patch": patch_parent}]
        },
        "anthropic": {
            "verdict": "REVISE",
            "summary": "Blocker child",
            "issues": [{"id": "ISS-CHILD", "severity": "blocking", "claim": "c", "falsifier": "c", "resolution": "c", "proposed_patch": patch_child}]
        },
        "google": {"verdict": "APPROVE", "summary": "OK", "issues": []},
    }

    _, report = round_table_mediation.compile_revision(doc_nested, "v001", base_sha, votes)
    assert report["outcome"] == "AWAITING_HUMAN_REVISION"
    assert any(c["type"] == "RANGE_OVERLAP" for c in report["conflicts"])


# ==============================================================================================
# 5. Determinism, Source Seat Injection & Legacy Vote Optionality
# ==============================================================================================
def test_deterministic_compilation_idempotence():
    base_sha = round_table_mediation.sha256_text(SAMPLE_DOC)
    inv = {s["heading"]: s["sha256"] for s in round_table_mediation.section_inventory(SAMPLE_DOC)}

    patch = {
        "base_version": "v001",
        "base_sha256": base_sha,
        "operations": [{
            "op": "append_clause",
            "heading": "## 1. Objetivos Nucleares (In-Scope)",
            "expected_sha256": inv["## 1. Objetivos Nucleares (In-Scope)"],
            "content": "- Deterministic addition 123.",
        }]
    }

    votes = {
        "google": {
            "verdict": "REVISE",
            "summary": "Issue",
            "issues": [{"id": "ISS-G", "severity": "blocking", "claim": "c", "falsifier": "f", "resolution": "r", "proposed_patch": patch}]
        },
        "anthropic": {"verdict": "APPROVE", "summary": "OK", "issues": []},
        "openai": {"verdict": "APPROVE", "summary": "OK", "issues": []},
    }

    text1, rep1 = round_table_mediation.compile_revision(SAMPLE_DOC, "v001", base_sha, votes)
    text2, rep2 = round_table_mediation.compile_revision(SAMPLE_DOC, "v001", base_sha, votes)

    assert text1 == text2
    assert rep1["compiled_sha256"] == rep2["compiled_sha256"]
    assert json.dumps(rep1, sort_keys=True) == json.dumps(rep2, sort_keys=True)


def test_source_seat_injection_and_untrusted_payload():
    base_sha = round_table_mediation.sha256_text(SAMPLE_DOC)
    inv = {s["heading"]: s["sha256"] for s in round_table_mediation.section_inventory(SAMPLE_DOC)}

    # Untrusted payload tries to claim source_seat is 'god_mode'
    patch = {
        "base_version": "v001",
        "base_sha256": base_sha,
        "source_seat": "spoofed_admin",
        "operations": [{
            "op": "append_clause",
            "heading": "## 1. Objetivos Nucleares (In-Scope)",
            "expected_sha256": inv["## 1. Objetivos Nucleares (In-Scope)"],
            "content": "- Safe clause.",
        }]
    }

    votes = {
        "chair_actual_google": {
            "verdict": "REVISE",
            "summary": "Test spoof injection",
            "issues": [{"id": "ISS-01", "severity": "blocking", "claim": "c", "falsifier": "f", "resolution": "r", "proposed_patch": patch}]
        },
        "anthropic": {"verdict": "APPROVE", "summary": "OK", "issues": []},
        "openai": {"verdict": "APPROVE", "summary": "OK", "issues": []},
    }

    _, report = round_table_mediation.compile_revision(SAMPLE_DOC, "v001", base_sha, votes)
    assert report["issues"][0]["source_seat"] == "chair_actual_google"


def test_legacy_vote_without_patch_optionality():
    base_sha = round_table_mediation.sha256_text(SAMPLE_DOC)

    votes = {
        "openai": {
            "verdict": "REVISE",
            "summary": "Broad conceptual blocker without mechanical patch",
            "issues": [{
                "id": "ISS-CONCEPTUAL",
                "severity": "blocking",
                "claim": "Architecture too complex",
                "falsifier": "Mental fatigue",
                "resolution": "Simplify"
            }]
        },
        "anthropic": {"verdict": "APPROVE", "summary": "OK", "issues": []},
        "google": {"verdict": "APPROVE", "summary": "OK", "issues": []},
    }

    _, report = round_table_mediation.compile_revision(SAMPLE_DOC, "v001", base_sha, votes)
    assert report["outcome"] == "AWAITING_HUMAN_REVISION"
    assert report["issues"][0]["status"] == "NO_PATCH"
    assert any("provided no proposed_patch" in r for r in report["human_review_reasons"])


# ==============================================================================================
# 6. Auto-Seal, Multiple Blockers Draft & Human Review Comment Marker
# ==============================================================================================
def test_auto_seal_single_blocker_zero_llm_calls(mock_rt_env, monkeypatch):
    round_table_engine.init_round_table(
        case_id="RT-AUTO-SEAL-01",
        title="Auto Seal Test Proposal",
        proposal_text=SAMPLE_DOC,
        standalone_offline=True,
    )

    base_sha = round_table_mediation.sha256_text(SAMPLE_DOC)
    inv = {s["heading"]: s["sha256"] for s in round_table_mediation.section_inventory(SAMPLE_DOC)}

    patch = {
        "base_version": "v001",
        "base_sha256": base_sha,
        "operations": [{
            "op": "append_clause",
            "heading": "## 1. Objetivos Nucleares (In-Scope)",
            "expected_sha256": inv["## 1. Objetivos Nucleares (In-Scope)"],
            "content": "- Invariant 3: Clean auto-sealed invariant.",
        }]
    }

    llm_calls = {"text_role": 0}

    def mock_seats(seat_id, prompt, expected_nonce, timeout=300):
        if "ROUND 2" in prompt:
            return True, {
                "seat": seat_id,
                "execution_nonce": expected_nonce,
                "verdict": "APPROVE",
                "confidence": 0.99,
                "summary": "v002 approved.",
                "strengths": ["Clean"],
                "issues": [],
                "recommendations": []
            }, "OK"

        if seat_id == "google":
            return True, {
                "seat": seat_id,
                "execution_nonce": expected_nonce,
                "verdict": "REVISE",
                "confidence": 0.95,
                "summary": "Needs clause 3",
                "strengths": ["Strong basis"],
                "issues": [{
                    "id": "ISS-G-01",
                    "severity": "blocking",
                    "claim": "Missing invariant 3",
                    "falsifier": "Fails without clause 3",
                    "resolution": "Append clause 3",
                    "proposed_patch": patch,
                }],
                "recommendations": []
            }, "OK"
        else:
            return True, {
                "seat": seat_id,
                "execution_nonce": expected_nonce,
                "verdict": "APPROVE",
                "confidence": 0.95,
                "summary": "Looks good.",
                "strengths": ["Solid"],
                "issues": [],
                "recommendations": []
            }, "OK"

    def mock_text_role(*args, **kwargs):
        llm_calls["text_role"] += 1
        return True, "LLM Generated Text", {"deployment_id": "test"}

    monkeypatch.setattr(round_table_engine, "_execute_seat", mock_seats)
    monkeypatch.setattr(round_table_engine, "_execute_text_role", mock_text_role)

    final_res = round_table_engine.run_full_deliberation(
        "RT-AUTO-SEAL-01", max_rounds=3, standalone_offline=True
    )
    assert final_res["status"] == "APPROVED"
    # Zero calls to LLM text role!
    assert llm_calls["text_role"] == 0

    case = round_table_engine.RoundTableCase("RT-AUTO-SEAL-01")
    state = case.load_state()
    assert state["current_version"] == "v002"
    assert (case.rounds_dir / "r001" / "revision" / "compiled.md").exists()
    assert (case.rounds_dir / "r001" / "revision" / "report.json").exists()


def test_multiple_blockers_draft_compiled_awaiting_human_revision(mock_rt_env, monkeypatch):
    round_table_engine.init_round_table(
        case_id="RT-MULTI-BLOCK",
        title="Multi Blocker Test",
        proposal_text=SAMPLE_DOC,
        standalone_offline=True,
    )

    base_sha = round_table_mediation.sha256_text(SAMPLE_DOC)
    inv = {s["heading"]: s["sha256"] for s in round_table_mediation.section_inventory(SAMPLE_DOC)}

    patch1 = {
        "base_version": "v001",
        "base_sha256": base_sha,
        "operations": [{
            "op": "append_clause",
            "heading": "## 1. Objetivos Nucleares (In-Scope)",
            "expected_sha256": inv["## 1. Objetivos Nucleares (In-Scope)"],
            "content": "- Addition 1",
        }]
    }
    patch2 = {
        "base_version": "v001",
        "base_sha256": base_sha,
        "operations": [{
            "op": "append_clause",
            "heading": "## 1.2 Não-Objetivos Explícitos (Via Negativa / Fora de Escopo)",
            "expected_sha256": inv["## 1.2 Não-Objetivos Explícitos (Via Negativa / Fora de Escopo)"],
            "content": "- Non-goal 2",
        }]
    }

    def mock_seats(seat_id, prompt, expected_nonce, timeout=300):
        if seat_id == "google":
            return True, {
                "seat": seat_id, "execution_nonce": expected_nonce, "verdict": "REVISE", "confidence": 0.9, "summary": "s1",
                "strengths": [], "issues": [{"id": "ISS-1", "severity": "blocking", "claim": "c1", "falsifier": "f1", "resolution": "r1", "proposed_patch": patch1}], "recommendations": []
            }, "OK"
        elif seat_id == "openai":
            return True, {
                "seat": seat_id, "execution_nonce": expected_nonce, "verdict": "REVISE", "confidence": 0.9, "summary": "s2",
                "strengths": [], "issues": [{"id": "ISS-2", "severity": "blocking", "claim": "c2", "falsifier": "f2", "resolution": "r2", "proposed_patch": patch2}], "recommendations": []
            }, "OK"
        else:
            return True, {"seat": seat_id, "execution_nonce": expected_nonce, "verdict": "APPROVE", "confidence": 0.9, "summary": "ok", "strengths": [], "issues": [], "recommendations": []}, "OK"

    monkeypatch.setattr(round_table_engine, "_execute_seat", mock_seats)

    res = round_table_engine.run_full_deliberation("RT-MULTI-BLOCK", max_rounds=3, standalone_offline=True)
    assert res["status"] == "AWAITING_HUMAN_REVISION"

    case = round_table_engine.RoundTableCase("RT-MULTI-BLOCK")
    state = case.load_state()
    assert state["status"] == "AWAITING_HUMAN_REVISION"
    assert state["current_version"] == "v001"  # Does NOT create v002
    assert (case.rounds_dir / "r001" / "revision" / "compiled.md").exists()
    assert (case.rounds_dir / "r001" / "revision" / "report.json").exists()


def test_human_review_comment_marker_blocks_auto_seal():
    doc_with_marker = """# Document With Human Review Marker

## 1. Objetivos Nucleares (In-Scope)
<!-- round-table:human-review -->
Critical security section requiring mandatory human sign-off.

## 1.2 Não-Objetivos Explícitos (Via Negativa / Fora de Escopo)
No scope creep.

## 3. Matriz de Falsificação
Tests mapped.
"""
    base_sha = round_table_mediation.sha256_text(doc_with_marker)
    inv = {s["heading"]: s["sha256"] for s in round_table_mediation.section_inventory(doc_with_marker)}

    patch = {
        "base_version": "v001",
        "base_sha256": base_sha,
        "operations": [{
            "op": "append_clause",
            "heading": "## 1. Objetivos Nucleares (In-Scope)",
            "expected_sha256": inv["## 1. Objetivos Nucleares (In-Scope)"],
            "content": "- Patch attempting auto seal.",
        }]
    }

    votes = {
        "google": {
            "verdict": "REVISE",
            "summary": "Blocker",
            "issues": [{"id": "ISS-01", "severity": "blocking", "claim": "c", "falsifier": "f", "resolution": "r", "proposed_patch": patch}]
        },
        "anthropic": {"verdict": "APPROVE", "summary": "OK", "issues": []},
        "openai": {"verdict": "APPROVE", "summary": "OK", "issues": []},
    }

    _, report = round_table_mediation.compile_revision(doc_with_marker, "v001", base_sha, votes)
    assert report["outcome"] == "AWAITING_HUMAN_REVISION"
    assert any("round-table:human-review marker" in r for r in report["human_review_reasons"])


# ==============================================================================================
# 7. LLM Mediator Opt-in & Fallbacks
# ==============================================================================================
def test_llm_mediator_opt_in_and_fallback_to_awaiting(mock_rt_env, monkeypatch):
    round_table_engine.init_round_table(
        case_id="RT-LLM-OPTIN",
        title="LLM Mediator Opt-In Test",
        proposal_text=SAMPLE_DOC,
        standalone_offline=True,
    )

    def mock_seats(seat_id, prompt, expected_nonce, timeout=300):
        return True, {
            "seat": seat_id, "execution_nonce": expected_nonce, "verdict": "REVISE", "confidence": 0.9, "summary": "Need human",
            "strengths": [], "issues": [{"id": "ISS-NO-PATCH", "severity": "blocking", "claim": "c", "falsifier": "f", "resolution": "r"}], "recommendations": []
        }, "OK"

    monkeypatch.setattr(round_table_engine, "_execute_seat", mock_seats)

    # 1. Without --llm-mediator -> AWAITING_HUMAN_REVISION
    res1 = round_table_engine.run_full_deliberation("RT-LLM-OPTIN", max_rounds=3, standalone_offline=True, llm_mediator=False)
    assert res1["status"] == "AWAITING_HUMAN_REVISION"

    # 2. With --llm-mediator but LLM raises error -> stays AWAITING_HUMAN_REVISION
    def mock_failing_llm(*args, **kwargs):
        raise RuntimeError("LLM inference service unreachable")

    monkeypatch.setattr(round_table_engine, "_execute_text_role", mock_failing_llm)
    res2 = round_table_engine.run_full_deliberation("RT-LLM-OPTIN", max_rounds=3, standalone_offline=True, resume=True, llm_mediator=True)
    assert res2["status"] == "AWAITING_HUMAN_REVISION"


# ==============================================================================================
# 8. Human Revision Submission (CLI submit-revision)
# ==============================================================================================
def test_human_revision_submission_wrong_params_zero_mutation(mock_rt_env):
    round_table_engine.init_round_table(
        case_id="RT-HUMAN-ERR",
        title="Human Submission Error Test",
        proposal_text=SAMPLE_DOC,
        standalone_offline=True,
    )
    case = round_table_engine.RoundTableCase("RT-HUMAN-ERR")
    state = case.load_state()
    state["status"] = "AWAITING_HUMAN_REVISION"
    case.save_state(state)

    valid_rev_text = SAMPLE_DOC + "\n- Added human resolution.\n"
    rev_file = mock_rt_env / "rev.md"
    rev_file.write_text(valid_rev_text, encoding="utf-8")
    actual_sha = round_table_mediation.sha256_text(valid_rev_text)
    base_sha = state["versions"]["v001"]["sha256"]

    # 1. Wrong expected_base_version -> fails closed
    with pytest.raises(ValueError, match="Base version mismatch"):
        round_table_engine.submit_human_revision(
            case_id="RT-HUMAN-ERR",
            file_path=rev_file,
            expected_base_version="v999",
            expected_base_sha256=base_sha,
            authorize_result_sha256=actual_sha,
            operator_label="alice",
            reason="fixed",
            standalone_offline=True,
        )

    # 2. Wrong authorize_result_sha256 -> fails closed
    with pytest.raises(ValueError, match="authorization mismatch"):
        round_table_engine.submit_human_revision(
            case_id="RT-HUMAN-ERR",
            file_path=rev_file,
            expected_base_version="v001",
            expected_base_sha256=base_sha,
            authorize_result_sha256="0" * 64,
            operator_label="alice",
            reason="fixed",
            standalone_offline=True,
        )

    # Zero mutation verification
    st = case.load_state()
    assert st["status"] == "AWAITING_HUMAN_REVISION"
    assert st["current_version"] == "v001"
    assert "v002" not in st["versions"]
    assert not (case.proposals_dir / "v002.md").exists()


def test_human_revision_submission_correct_seals_and_resumes(mock_rt_env, monkeypatch):
    round_table_engine.init_round_table(
        case_id="RT-HUMAN-OK",
        title="Human Submission OK Test",
        proposal_text=SAMPLE_DOC,
        standalone_offline=True,
    )
    case = round_table_engine.RoundTableCase("RT-HUMAN-OK")
    state = case.load_state()
    state["status"] = "AWAITING_HUMAN_REVISION"
    case.save_state(state)

    rev_text = SAMPLE_DOC + "\n- Resolved by operator: added formal bounded stream.\n"
    rev_file = mock_rt_env / "rev_valid.md"
    rev_file.write_text(rev_text, encoding="utf-8")
    actual_sha = round_table_mediation.sha256_text(rev_text)
    base_sha = state["versions"]["v001"]["sha256"]

    res = round_table_engine.submit_human_revision(
        case_id="RT-HUMAN-OK",
        file_path=rev_file,
        expected_base_version="v001",
        expected_base_sha256=base_sha,
        authorize_result_sha256=actual_sha,
        operator_label="security_lead",
        reason="Resolved concurrency blocker manually after audit",
        standalone_offline=True,
    )
    assert res["status"] == "SEALED"
    assert res["version"] == "v002"

    st = case.load_state()
    assert st["status"] == "SEALED"
    assert st["current_version"] == "v002"
    assert st["current_round"] == 2
    assert "v002" in st["versions"]
    assert st["versions"]["v002"]["sha256"] == actual_sha
    assert (case.proposals_dir / "v002.md").exists()

    # Verify ledger has HUMAN_REVISION_SEALED entry
    ok, _, entries = case.journal.verify_integrity()
    assert ok
    sealed_events = [e for e in entries if e["event"] == "HUMAN_REVISION_SEALED"]
    assert len(sealed_events) == 1
    assert sealed_events[0]["payload"]["operator_label"] == "security_lead"

    # Subsequent deliberation round can proceed on v002
    def mock_approve(seat_id, prompt, expected_nonce, timeout=300):
        return True, {
            "seat": seat_id, "execution_nonce": expected_nonce, "verdict": "APPROVE", "confidence": 0.99, "summary": "OK",
            "strengths": ["Solid"], "issues": [], "recommendations": []
        }, "OK"

    monkeypatch.setattr(round_table_engine, "_execute_seat", mock_approve)
    delib_res = round_table_engine.conduct_round("RT-HUMAN-OK", standalone_offline=True, resume=True)
    assert delib_res["status"] == "APPROVED"


# ==============================================================================================
# 9. Operator Adjudication After Exhausted Deliberation
# ==============================================================================================
def _prepare_operator_adjudication_case(
    case_id,
    *,
    current_round=3,
    self_referential=False,
    status="AWAITING_HUMAN_REVISION",
):
    round_table_engine.init_round_table(
        case_id=case_id,
        title="Operator Adjudication Test",
        proposal_text=SAMPLE_DOC,
        max_rounds=3,
        self_referential=self_referential,
        standalone_offline=True,
    )
    case = round_table_engine.RoundTableCase(case_id)
    state = case.load_state()
    state["status"] = status
    state["current_round"] = current_round
    state["last_seat_votes"] = {
        "openai": {
            "verdict": "APPROVE",
            "confidence": 0.95,
            "summary": "Approve",
            "issues": [],
        },
        "anthropic": {
            "verdict": "APPROVE",
            "confidence": 0.91,
            "summary": "Approve",
            "issues": [],
        },
        "google": {
            "verdict": "REVISE",
            "confidence": 0.88,
            "summary": "Two blockers remain",
            "issues": [
                {
                    "id": "R3-01",
                    "severity": "blocking",
                    "claim": "Dispatcher wording is ambiguous",
                    "falsifier": "An implementation treats it as optional",
                    "resolution": "Correct the wording",
                },
                {
                    "id": "R3-02",
                    "severity": "blocking",
                    "claim": "Ambient authority proof is underspecified",
                    "falsifier": "Probe inherits ambient authority",
                    "resolution": "Make it an exit criterion",
                },
            ],
        },
    }
    case.save_state(state)
    return case, state


def _operator_adjudication_text(include_r3_02=True):
    second_disposition = "- R3-02: reclassified as a mandatory Probe A exit criterion.\n" if include_r3_02 else ""
    return f"""# Operator adjudication

## Operator ruling
Authorize a bounded exception without rewriting the 2-1 vote or claiming consensus.

## Blocker dispositions
- R3-01: accepted; dispatcher closure is mandatory and isolation is optional containment.
{second_disposition}
## Authorized scope
Phase 0 read-only work and Probe A only.

## Accepted risk
The operator accepts the documented residual risk and lack of tripartite consensus.

## Explicit exclusions
Probe B, production rollout, and ADR promotion remain prohibited.
"""


def test_operator_adjudication_is_terminal_auditable_and_not_consensus(mock_rt_env):
    case, state = _prepare_operator_adjudication_case("RT-OPERATOR-OK")
    adjudication_text = _operator_adjudication_text()
    adjudication_file = mock_rt_env / "operator-adjudication.md"
    adjudication_file.write_text(adjudication_text, encoding="utf-8")
    content_sha = round_table_engine._sha256(adjudication_text)
    proposal_sha = state["versions"]["v001"]["sha256"]

    result = round_table_engine.adjudicate_case(
        case_id="RT-OPERATOR-OK",
        file_path=adjudication_file,
        expected_version="v001",
        expected_sha256=proposal_sha,
        authorize_adjudication_sha256=content_sha,
        operator_label="human-gatekeeper",
        statement="Authorize Phase 0 and Probe A only; keep Probe B blocked.",
        standalone_offline=True,
    )

    assert result["status"] == "AUTHORIZED_BY_OPERATOR_EXCEPTION"
    assert result["integrity"]["valid"] is True
    final_state = case.load_state()
    assert final_state["status"] == "AUTHORIZED_BY_OPERATOR_EXCEPTION"
    assert final_state["operator_adjudication"]["blocking_issue_ids"] == ["R3-01", "R3-02"]
    assert final_state["operator_adjudication"]["vote_summary"] == {
        "APPROVE": 2,
        "REJECT": 0,
        "REVISE": 1,
    }
    assert final_state["operator_adjudication"]["source_status"] == "AWAITING_HUMAN_REVISION"
    decision_text = (case.decision_dir / "DECISION.md").read_text(encoding="utf-8")
    assert "OPERATOR EXCEPTION DECISION AFTER A ROUND TABLE WITHOUT CONSENSUS" in decision_text
    assert "not a Round Table consensus" in decision_text
    assert "Text Ratified by Consensus" not in decision_text
    assert "Ratified Version" not in decision_text
    assert (case.decision_dir / "OPERATOR_ADJUDICATION.md").exists()

    ok, _, entries = case.journal.verify_integrity()
    assert ok
    assert len([entry for entry in entries if entry["event"] == "OPERATOR_ADJUDICATED"]) == 1
    assert len([entry for entry in entries if entry["event"] == "DECISION_FINALIZED"]) == 1

    repeated = round_table_engine.adjudicate_case(
        case_id="RT-OPERATOR-OK",
        file_path=adjudication_file,
        expected_version="v001",
        expected_sha256=proposal_sha,
        authorize_adjudication_sha256=content_sha,
        operator_label="human-gatekeeper",
        statement="Authorize Phase 0 and Probe A only; keep Probe B blocked.",
        standalone_offline=True,
    )
    assert repeated["idempotent"] is True


def test_operator_adjudication_accepts_exhausted_held_case_without_mutating_authorized_artifact(mock_rt_env):
    case, state = _prepare_operator_adjudication_case(
        "RT-OPERATOR-HELD",
        status="HELD_NO_CONVERGENCE",
    )
    adjudication_text = _operator_adjudication_text()
    adjudication_file = mock_rt_env / "held-operator-adjudication.md"
    adjudication_file.write_text(adjudication_text, encoding="utf-8")
    authorized_bytes = round_table_engine._normalize_text(adjudication_text).encode("utf-8")
    content_sha = round_table_engine._sha256(adjudication_text)
    proposal_sha = state["versions"]["v001"]["sha256"]
    held_decision = round_table_engine.finalize_decision(
        "RT-OPERATOR-HELD",
        "HELD_NO_CONVERGENCE",
        "The round budget ended without consensus.",
        standalone_offline=True,
    )
    held_decision_bytes = held_decision.read_bytes()

    result = round_table_engine.adjudicate_case(
        case_id="RT-OPERATOR-HELD",
        file_path=adjudication_file,
        expected_version="v001",
        expected_sha256=proposal_sha,
        authorize_adjudication_sha256=content_sha,
        operator_label="human-gatekeeper",
        statement="Authorize the bounded exception after terminal non-convergence.",
        standalone_offline=True,
    )

    assert result["status"] == "AUTHORIZED_BY_OPERATOR_EXCEPTION"
    assert result["integrity"]["valid"] is True
    final_state = case.load_state()
    assert final_state["operator_adjudication"]["source_status"] == "HELD_NO_CONVERGENCE"
    assert (case.decision_dir / "OPERATOR_ADJUDICATION.md").read_bytes() == authorized_bytes
    assert held_decision.read_bytes() == held_decision_bytes
    assert final_state["operator_adjudication"]["decision_sha256"] == hashlib.sha256(held_decision_bytes).hexdigest()

    ok, _, entries = case.journal.verify_integrity()
    assert ok
    events = [entry for entry in entries if entry["event"] == "OPERATOR_ADJUDICATED"]
    assert len(events) == 1
    assert events[0]["payload"]["source_status"] == "HELD_NO_CONVERGENCE"
    decision_events = [entry for entry in entries if entry["event"] == "DECISION_FINALIZED"]
    assert len(decision_events) == 1
    assert decision_events[0]["payload"]["verdict"] == "HELD_NO_CONVERGENCE"

    repeated = round_table_engine.adjudicate_case(
        case_id="RT-OPERATOR-HELD",
        file_path=adjudication_file,
        expected_version="v001",
        expected_sha256=proposal_sha,
        authorize_adjudication_sha256=content_sha,
        operator_label="human-gatekeeper",
        statement="Authorize the bounded exception after terminal non-convergence.",
        standalone_offline=True,
    )
    assert repeated["idempotent"] is True


def test_operator_adjudication_held_case_still_requires_exhausted_round_budget(mock_rt_env):
    case, state = _prepare_operator_adjudication_case(
        "RT-OPERATOR-HELD-PREMATURE",
        current_round=2,
        status="HELD_NO_CONVERGENCE",
    )
    adjudication_text = _operator_adjudication_text()
    adjudication_file = mock_rt_env / "held-premature-adjudication.md"
    adjudication_file.write_text(adjudication_text, encoding="utf-8")

    with pytest.raises(ValueError, match="premature"):
        round_table_engine.adjudicate_case(
            case_id="RT-OPERATOR-HELD-PREMATURE",
            file_path=adjudication_file,
            expected_version="v001",
            expected_sha256=state["versions"]["v001"]["sha256"],
            authorize_adjudication_sha256=round_table_engine._sha256(adjudication_text),
            operator_label="human-gatekeeper",
            statement="This terminal exception is still premature.",
            standalone_offline=True,
        )

    assert case.load_state()["status"] == "HELD_NO_CONVERGENCE"
    assert not (case.decision_dir / "OPERATOR_ADJUDICATION.md").exists()


@pytest.mark.parametrize("status", ["APPROVED", "REJECTED", "HELD_UNAVAILABLE", "HELD_ERROR"])
def test_operator_adjudication_rejects_other_terminal_or_held_statuses(mock_rt_env, status):
    case_id = f"RT-OPERATOR-STATUS-{status}"
    case, state = _prepare_operator_adjudication_case(case_id, status=status)
    adjudication_text = _operator_adjudication_text()
    adjudication_file = mock_rt_env / f"{case_id}.md"
    adjudication_file.write_text(adjudication_text, encoding="utf-8")

    with pytest.raises(ValueError, match="expected one of"):
        round_table_engine.adjudicate_case(
            case_id=case_id,
            file_path=adjudication_file,
            expected_version="v001",
            expected_sha256=state["versions"]["v001"]["sha256"],
            authorize_adjudication_sha256=round_table_engine._sha256(adjudication_text),
            operator_label="human-gatekeeper",
            statement="This status must remain non-adjudicable.",
            standalone_offline=True,
        )

    assert case.load_state()["status"] == status
    assert not (case.decision_dir / "OPERATOR_ADJUDICATION.md").exists()


@pytest.mark.parametrize(
    ("current_round", "include_r3_02", "authorized_sha", "error_match"),
    [
        (2, True, None, "premature"),
        (3, False, None, "R3-02"),
        (3, True, "0" * 64, "authorization mismatch"),
    ],
)
def test_operator_adjudication_fail_closed_zero_mutation(
    mock_rt_env,
    current_round,
    include_r3_02,
    authorized_sha,
    error_match,
):
    case_id = f"RT-OPERATOR-FAIL-{current_round}-{int(include_r3_02)}-{error_match[:3]}"
    case, state = _prepare_operator_adjudication_case(case_id, current_round=current_round)
    adjudication_text = _operator_adjudication_text(include_r3_02=include_r3_02)
    adjudication_file = mock_rt_env / f"{case_id}.md"
    adjudication_file.write_text(adjudication_text, encoding="utf-8")
    content_sha = authorized_sha or round_table_engine._sha256(adjudication_text)

    with pytest.raises(ValueError, match=error_match):
        round_table_engine.adjudicate_case(
            case_id=case_id,
            file_path=adjudication_file,
            expected_version="v001",
            expected_sha256=state["versions"]["v001"]["sha256"],
            authorize_adjudication_sha256=content_sha,
            operator_label="human-gatekeeper",
            statement="Bounded operator exception.",
            standalone_offline=True,
        )

    unchanged = case.load_state()
    assert unchanged["status"] == "AWAITING_HUMAN_REVISION"
    assert "operator_adjudication" not in unchanged
    assert not (case.decision_dir / "OPERATOR_ADJUDICATION.md").exists()
    assert not (case.decision_dir / "DECISION.md").exists()


@pytest.mark.parametrize("status", ["AWAITING_HUMAN_REVISION", "HELD_NO_CONVERGENCE"])
def test_operator_adjudication_rejects_self_referential_case_before_mutation(mock_rt_env, status):
    case, state = _prepare_operator_adjudication_case(
        f"RT-OPERATOR-SELF-REF-{status}",
        self_referential=True,
        status=status,
    )
    adjudication_text = _operator_adjudication_text()
    adjudication_file = mock_rt_env / "self-referential-adjudication.md"
    adjudication_file.write_text(adjudication_text, encoding="utf-8")
    state_before = case.state_file.read_bytes()
    ledger_before = case.ledger_file.read_bytes()

    with pytest.raises(ValueError, match="Self-referential cases cannot use operator adjudication"):
        round_table_engine.adjudicate_case(
            case_id=f"RT-OPERATOR-SELF-REF-{status}",
            file_path=adjudication_file,
            expected_version="v001",
            expected_sha256=state["versions"]["v001"]["sha256"],
            authorize_adjudication_sha256=round_table_engine._sha256(adjudication_text),
            operator_label="human-gatekeeper",
            statement="This self-referential exception must be rejected.",
            standalone_offline=True,
        )

    assert case.state_file.read_bytes() == state_before
    assert case.ledger_file.read_bytes() == ledger_before
    assert not (case.decision_dir / "OPERATOR_ADJUDICATION.md").exists()
    assert not (case.decision_dir / "DECISION.md").exists()


def test_operator_adjudication_requires_blocker_inside_disposition_section(mock_rt_env):
    case, state = _prepare_operator_adjudication_case("RT-OPERATOR-WRONG-SECTION")
    adjudication_text = _operator_adjudication_text(include_r3_02=False).replace(
        "The operator accepts the documented residual risk and lack of tripartite consensus.",
        "The operator accepts R3-02 as documented residual risk and lack of tripartite consensus.",
    )
    adjudication_file = mock_rt_env / "wrong-section-adjudication.md"
    adjudication_file.write_text(adjudication_text, encoding="utf-8")

    with pytest.raises(ValueError, match="R3-02"):
        round_table_engine.adjudicate_case(
            case_id="RT-OPERATOR-WRONG-SECTION",
            file_path=adjudication_file,
            expected_version="v001",
            expected_sha256=state["versions"]["v001"]["sha256"],
            authorize_adjudication_sha256=round_table_engine._sha256(adjudication_text),
            operator_label="human-gatekeeper",
            statement="Reject disposition outside its canonical section.",
            standalone_offline=True,
        )

    assert case.load_state()["status"] == "AWAITING_HUMAN_REVISION"
    assert not (case.decision_dir / "OPERATOR_ADJUDICATION.md").exists()
    assert not (case.decision_dir / "DECISION.md").exists()


# ==============================================================================================
# 10. Deterministic Scribe & Legacy Case Compatibility
# ==============================================================================================
def test_deterministic_scribe_default_zero_llm_and_opt_in_llm_scribe(mock_rt_env, monkeypatch):
    round_table_engine.init_round_table(
        case_id="RT-SCRIBE-TEST",
        title="Scribe Invariant Test",
        proposal_text=SAMPLE_DOC,
        standalone_offline=True,
    )

    llm_calls = {"scribe": 0}

    def mock_text_role(*args, **kwargs):
        llm_calls["scribe"] += 1
        return True, "Factual LLM narrative for Round Table ratification exceeding 40 characters.", {"deployment_id": "scribe-test"}

    monkeypatch.setattr(round_table_engine, "_execute_text_role", mock_text_role)

    # 1. Default finalize_decision (deterministic scribe) -> 0 LLM calls
    dec_path = round_table_engine.finalize_decision(
        "RT-SCRIBE-TEST", "APPROVED", "Consenso alcançado.", standalone_offline=True, use_scribe=True, llm_scribe=False
    )
    assert dec_path.exists()
    dec_content = dec_path.read_text(encoding="utf-8")
    assert "Deterministic Scribe" in dec_content
    assert llm_calls["scribe"] == 0

    # 2. Opt-in with llm_scribe=True on new decision
    case2_state = round_table_engine.init_round_table(
        case_id="RT-SCRIBE-OPTIN",
        title="Scribe Opt-In Test",
        proposal_text=SAMPLE_DOC,
        standalone_offline=True,
    )
    dec_path2 = round_table_engine.finalize_decision(
        "RT-SCRIBE-OPTIN", "APPROVED", "Consenso alcançado.", standalone_offline=True, use_scribe=True, llm_scribe=True
    )
    assert dec_path2.exists()
    assert llm_calls["scribe"] == 1


def test_legacy_case_compatibility_preserved(mock_rt_env, monkeypatch):
    # Initialize a case without workflow_contract (simulating legacy case.json)
    case = round_table_engine.RoundTableCase("RT-LEGACY-01", standalone_offline=True)
    case.ensure_structure()
    sha = case.write_proposal_text("v001", SAMPLE_DOC)
    state = {
        "case_id": "RT-LEGACY-01",
        "title": "Legacy Proposal",
        "author": "operator",
        "status": "SEALED",
        "current_version": "v001",
        "current_round": 1,
        "max_rounds": 3,
        "seats": round_table_engine.SEATS,
        "mediator": round_table_engine.MEDIATOR,
        "runtime_contract": round_table_engine._runtime_contract(),
        "versions": {"v001": {"file": "proposals/v001.md", "sha256": sha}},
    }
    case.save_state(state)
    case.append_ledger("CASE_INITIALIZED", {"case_id": "RT-LEGACY-01"})

    legacy_llm_called = {"count": 0}

    def mock_seats(seat_id, prompt, expected_nonce, timeout=300):
        if "ROUND 2" in prompt:
            return True, {"seat": seat_id, "execution_nonce": expected_nonce, "verdict": "APPROVE", "confidence": 0.95, "summary": "ok", "strengths": [], "issues": [], "recommendations": []}, "OK"
        return True, {
            "seat": seat_id, "execution_nonce": expected_nonce, "verdict": "REVISE", "confidence": 0.9, "summary": "legacy blocker",
            "strengths": [], "issues": [{"id": "ISS-LEGACY", "severity": "blocking", "claim": "c", "falsifier": "f", "resolution": "r"}], "recommendations": []
        }, "OK"

    def mock_mediator(deployment, prompt, timeout=300, **kwargs):
        legacy_llm_called["count"] += 1
        return True, SAMPLE_DOC + "\n- LLM refined legacy\n", {"deployment_id": deployment["id"]}

    monkeypatch.setattr(round_table_engine, "_execute_seat", mock_seats)
    monkeypatch.setattr(round_table_engine, "_execute_text_role", mock_mediator)

    final_res = round_table_engine.run_full_deliberation("RT-LEGACY-01", max_rounds=3, standalone_offline=True)
    assert final_res["status"] == "APPROVED"
    assert legacy_llm_called["count"] >= 1


def test_overlapping_parent_and_child_operations_in_one_blocker_require_human():
    doc = """# Proposal

## Objetivos Nucleares
Parent body.

### Detail
Child body.

## Não-objetivos
Out of scope.
"""
    base_sha = round_table_mediation.sha256_text(doc)
    inventory = {item["heading"]: item["sha256"] for item in round_table_mediation.section_inventory(doc)}
    patch = {
        "base_version": "v001",
        "base_sha256": base_sha,
        "operations": [
            {
                "op": "replace_section",
                "heading": "## Objetivos Nucleares",
                "expected_sha256": inventory["## Objetivos Nucleares"],
                "content": "Revised parent body.",
            },
            {
                "op": "replace_section",
                "heading": "### Detail",
                "expected_sha256": inventory["### Detail"],
                "content": "Revised child body.",
            },
        ],
    }
    votes = {
        "google": {
            "verdict": "REVISE",
            "issues": [{
                "id": "ISS-NESTED",
                "severity": "blocking",
                "claim": "Nested clauses need revision.",
                "falsifier": "Parent replacement can erase the child replacement.",
                "resolution": "Revise both clauses.",
                "proposed_patch": patch,
            }],
        },
        "anthropic": {"verdict": "APPROVE", "issues": []},
        "openai": {"verdict": "APPROVE", "issues": []},
    }

    _, report = round_table_mediation.compile_revision(doc, "v001", base_sha, votes)

    assert report["outcome"] == "AWAITING_HUMAN_REVISION"
    assert any(conflict["type"] == "RANGE_OVERLAP" for conflict in report["conflicts"])


def test_unverified_chair_independence_prevents_auto_seal():
    base_sha = round_table_mediation.sha256_text(SAMPLE_DOC)
    section_sha = next(
        item["sha256"]
        for item in round_table_mediation.section_inventory(SAMPLE_DOC)
        if item["heading"] == "## 1. Objetivos Nucleares (In-Scope)"
    )
    patch = {
        "base_version": "v001",
        "base_sha256": base_sha,
        "operations": [{
            "op": "append_clause",
            "heading": "## 1. Objetivos Nucleares (In-Scope)",
            "expected_sha256": section_sha,
            "content": "A bounded addition.",
        }],
    }
    votes = {
        "google": {"verdict": "REVISE", "issues": [{
            "id": "ISS-INDEPENDENCE",
            "severity": "blocking",
            "claim": "A clause is missing.",
            "falsifier": "The requirement is absent.",
            "resolution": "Append the clause.",
            "proposed_patch": patch,
        }]},
        "anthropic": {"verdict": "APPROVE", "issues": []},
        "openai": {"verdict": "APPROVE", "issues": []},
    }

    _, report = round_table_mediation.compile_revision(
        SAMPLE_DOC,
        "v001",
        base_sha,
        votes,
        chairs_independent=False,
    )

    assert report["outcome"] == "AWAITING_HUMAN_REVISION"
    assert report["chairs_independent"] is False


def test_awaiting_human_revision_cannot_rerun_round(mock_rt_env):
    round_table_engine.init_round_table("RT-AWAIT-GATE", "Await gate", SAMPLE_DOC)
    case = round_table_engine.RoundTableCase("RT-AWAIT-GATE")
    state = case.load_state()
    state["status"] = "AWAITING_HUMAN_REVISION"
    case.save_state(state)

    with pytest.raises(ValueError, match="submit-revision"):
        round_table_engine.conduct_round("RT-AWAIT-GATE", resume=True)

    result = round_table_engine.run_full_deliberation("RT-AWAIT-GATE", resume=True)
    assert result["status"] == "AWAITING_HUMAN_REVISION"
