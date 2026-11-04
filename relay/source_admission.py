"""Owner-side, read-only source/criterion fence for smart admission.

The Work bytes must match the existing Backlog grounding. The run's sources file
selects immutable owner documents; neither a working file nor metadata READY
substitutes for the source body. Only a change to the acceptance-criteria text
invalidates a Work: prose edits to its spec do not. This module grants no
Authority and never changes Work.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

MAX_GRAPH_BYTES = 32 * 1024 * 1024


def evaluate_source_admission(*, work_graph: str | Path, work_grounding: bytes,
                              descriptor: Any, library_manifest: str | Path,
                              owner_roots: Mapping[str, str | Path],
                              max_bytes: int = 8192) -> dict[str, Any]:
    """Compare exact Work, source, criterion IDs/text and sealed checks.

    ``owner_roots`` is an explicit selection, never a sibling-checkout search.
    Unsupported legacy documents without explicit AC IDs fail closed.
    """
    from specgraph.federation import acquire_federated_documents, check_work_source_binding

    if type(max_bytes) is not int or not 1024 <= max_bytes <= 65536:
        raise ValueError("max_bytes must be between 1024 and 65536")
    result: dict[str, Any] = {
        "schema": "tare.tools/source-admission-observation/1", "status": "REFUSED",
        "authority": "NONE", "work_modified": False, "reason_codes": [],
    }

    def refused(reason: str) -> dict[str, Any]:
        result["reason_codes"] = [reason]
        return result

    try:
        if not isinstance(work_grounding, bytes) or len(work_grounding) > 65536:
            return refused("WORK_GROUNDING_INVALID")
        grounded = json.loads(work_grounding)
        if not isinstance(grounded, dict) or grounded.get("status") != "READY":
            return refused("WORK_NOT_EXECUTION_ELIGIBLE")
        from tare_tools_agent_runtime import WorkEnvelope
        WorkEnvelope(work_grounding, max_bytes=65536)
        graph_path = Path(work_graph)
        if graph_path.stat().st_size > MAX_GRAPH_BYTES:
            return refused("WORK_GRAPH_TOO_LARGE")
        with graph_path.open("rb") as stream:
            raw = stream.read(MAX_GRAPH_BYTES + 1)
        if len(raw) > MAX_GRAPH_BYTES:
            return refused("WORK_GRAPH_TOO_LARGE")
        if hashlib.sha256(raw).hexdigest() != grounded.get("graph_sha256"):
            return refused("WORK_GRAPH_CHANGED")
        graph = json.loads(raw)
        selected = [node for node in graph["nodes"] if node.get("id") == descriptor.work_id]
        if len(selected) != 1 or grounded.get("work_id") != descriptor.work_id:
            return refused("WORK_ID_MISMATCH")
        work = selected[0]
        # Use the Backlog owner's canonicalization, not an approximate hash.
        from graph_backlog.jsonutil import sha256_canonical
        if sha256_canonical(work) != grounded.get("work_item_sha256"):
            return refused("WORK_ITEM_MISMATCH")
        criteria = work.get("exit_criteria", [])
        checks = [check.criterion for check in descriptor.verification_checks]
        if not criteria or len(criteria) > 64 or set(checks) != set(criteria) or len(checks) != len(criteria):
            return refused("VERIFICATION_CRITERIA_MISMATCH")
        reference = work.get("spec_ref")
        if not isinstance(reference, str) or ":" not in reference:
            return refused("QUALIFIED_WORK_SPEC_REQUIRED")
        owner = reference.split(":", 1)[0]
        acquisition = acquire_federated_documents(
            library_manifest, owner, dict(owner_roots), include_ids=[reference], max_bytes=65536)
        if acquisition.get("status") != "READY":
            return refused("PINNED_SOURCE_" + acquisition.get("status", "UNAVAILABLE"))
        source = acquisition["documents"][0]
        expected = source["acceptance_criteria"]
        criterion_ids = []
        for criterion in criteria:
            # IDs and text are both compared. An ID-only Work silently dropping
            # the intent is not accepted as equivalent to this source contract.
            criterion_id, separator, text = criterion.partition(": ")
            if not separator or expected.get(criterion_id) != text:
                return refused("SOURCE_AC_TEXT_MISMATCH")
            criterion_ids.append(criterion_id)
        # The content SHA a Work recorded at preparation is provenance, not a pin: the
        # criterion text above is what must still match.
        binding = check_work_source_binding(work, source, criterion_ids, expected_source_sha256=source["sha256"])
        if binding["status"] != "MATCHED":
            result["reason_codes"] = binding["reason_codes"]
            return result
        # Detect a graph update during acquisition without reinterpreting it.
        with graph_path.open("rb") as stream:
            after = stream.read(MAX_GRAPH_BYTES + 1)
        if after != raw:
            return refused("WORK_GRAPH_CHANGED")
        result.update(status="MATCHED", source_ref=reference, source_sha256=source["sha256"],
                      source_revision=source["revision"], criterion_ids=criterion_ids,
                      criterion_sha256={key: source["criterion_sha256"][key] for key in criterion_ids},
                      work_item_sha256=grounded["work_item_sha256"])
    except (OSError, ValueError, TypeError, KeyError, AttributeError, ImportError):
        return refused("SOURCE_ADMISSION_UNAVAILABLE")
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(encoded) > max_bytes:
        return {"schema": result["schema"], "status": "REFUSED", "authority": "NONE",
                "work_modified": False, "reason_codes": ["SOURCE_ADMISSION_BUDGET_EXCEEDED"]}
    return result
