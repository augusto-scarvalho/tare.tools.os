#!/usr/bin/env python3
"""relay/consensus_engine.py — multi-agent dogfooding & consensus
(RELAY-INTAKE-DOGFOODING-CONSENSUS-01, TRAIN-10).

Agents in the field (planner, implementer, auditor, bookkeeper) log protocol
friction, broadcast improvement proposals to peer inboxes, and cast typed votes
(`AGREE` / `OBJECTION` / `AMENDMENT`). A proposal is synthesized into a canonical
backlog task ONLY on real consensus — a quorum of AGREE with zero open
objections/amendments. Minority or disputed proposals are refused (fail closed):
no single agent, and no disputed vote, can manufacture backlog authority.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
import relay_io  # noqa: E402
import relay_mesh  # noqa: E402
import intake_processor  # noqa: E402  — shared graph-ops + id helpers

VOTE_TYPES = ("AGREE", "OBJECTION", "AMENDMENT")
# The swarm roster whose agreement counts. A vote from outside is ignored.
SWARM_AGENTS = ("codex_planner", "claude_opus_acer", "claude_fable_acer", "claude_fable", "claude_opus", "antigravity", "bookkeeper")
DEFAULT_QUORUM = 2  # minimum AGREE votes; OBJECTION/AMENDMENT still block


def _dogfood_dir() -> Path:
    return relay_mesh.RELAY_DIR / "intake" / "dogfooding"


def _friction_dir() -> Path:
    return _dogfood_dir() / "friction"


def _proposal_path(proposal_id: str) -> Path:
    return _dogfood_dir() / "proposals" / f"{intake_processor._slug(proposal_id)}.json"


def ensure_dirs() -> None:
    _friction_dir().mkdir(parents=True, exist_ok=True)
    (_dogfood_dir() / "proposals").mkdir(parents=True, exist_ok=True)


def log_friction(agent: str, observation: str, *, severity: str = "minor",
                 tags: Optional[list] = None) -> Path:
    """Append-only field friction record. Cheap; never mutates authority."""
    ensure_dirs()
    rid = f"FRICTION-{int(time.time() * 1000)}-{intake_processor._slug(agent, 16)}"
    path = _friction_dir() / f"{rid}.json"
    relay_io.write_json_atomic(path, {
        "id": rid, "agent": agent, "observation": observation,
        "severity": severity, "tags": tags or [], "at": relay_mesh.now_iso()})
    return path


def create_proposal(proposal_id: str, author: str, title: str, body: str, *,
                    quorum: int = DEFAULT_QUORUM, broadcast: bool = True) -> Dict[str, Any]:
    """Open a proposal and broadcast it to peer role inboxes for async voting.
    The author's own stance is recorded as an implicit AGREE."""
    ensure_dirs()
    path = _proposal_path(proposal_id)
    proposal = {
        "id": proposal_id, "author": author, "title": title, "body": body,
        "quorum": int(quorum), "status": "OPEN", "createdAt": relay_mesh.now_iso(),
        "votes": {author: {"vote": "AGREE", "comment": "author", "at": relay_mesh.now_iso()}},
    }
    relay_io.write_json_atomic(path, proposal)
    if broadcast:
        for role in ("planner", "plan-auditor", "implementer", "auditor"):
            relay_mesh.send_inbox_msg(role, f"Consensus proposal open: {proposal_id} — {title}", {
                "proposal_id": proposal_id, "author": author, "title": title,
                "action": "VOTE_PROPOSAL"}, required_action="notify")
    return proposal


def cast_vote(proposal_id: str, agent: str, vote: str, comment: str = "") -> Dict[str, Any]:
    """Record one agent's typed vote (last vote per agent wins). A vote outside
    VOTE_TYPES or from a non-swarm agent is refused, fail closed."""
    vote = str(vote).upper()
    if vote not in VOTE_TYPES:
        return {"ok": False, "reason": f"invalid vote {vote!r}; expected {VOTE_TYPES}"}
    if agent not in SWARM_AGENTS:
        return {"ok": False, "reason": f"non-swarm agent {agent!r} cannot vote"}
    path = _proposal_path(proposal_id)
    proposal = relay_io.read_json(path)
    if not isinstance(proposal, dict):
        return {"ok": False, "reason": f"proposal {proposal_id!r} not found"}
    if proposal.get("status") != "OPEN":
        return {"ok": False, "reason": f"proposal is {proposal.get('status')}, voting closed"}
    proposal.setdefault("votes", {})[agent] = {
        "vote": vote, "comment": comment, "at": relay_mesh.now_iso()}
    relay_io.write_json_atomic(path, proposal)
    return {"ok": True, "proposal_id": proposal_id, "agent": agent, "vote": vote}


def tally(proposal_id: str) -> Dict[str, Any]:
    """Count votes and decide consensus. Consensus requires a quorum of AGREE
    AND zero OBJECTION AND zero open AMENDMENT (any dispute blocks)."""
    proposal = relay_io.read_json(_proposal_path(proposal_id))
    if not isinstance(proposal, dict):
        return {"consensus": False, "reason": "proposal not found"}
    votes = proposal.get("votes", {})
    agree = sum(1 for v in votes.values() if v.get("vote") == "AGREE")
    objection = sum(1 for v in votes.values() if v.get("vote") == "OBJECTION")
    amendment = sum(1 for v in votes.values() if v.get("vote") == "AMENDMENT")
    quorum = int(proposal.get("quorum", DEFAULT_QUORUM))
    if objection or amendment:
        consensus, reason = False, f"disputed: {objection} objection(s), {amendment} amendment(s)"
    elif agree < quorum:
        consensus, reason = False, f"minority: {agree} AGREE < quorum {quorum}"
    else:
        consensus, reason = True, f"consensus: {agree} AGREE >= quorum {quorum}, no disputes"
    return {"consensus": consensus, "reason": reason, "agree": agree,
            "objection": objection, "amendment": amendment, "quorum": quorum,
            "voters": sorted(votes)}


def synthesize(proposal_id: str, *, cluster: str = "08 Relay / Work convergence",
               priority: str = "P2") -> Dict[str, Any]:
    """Turn a CONSENSUS proposal into a canonical backlog task. Refuses (changes
    nothing) on minority or disputed votes. Idempotent by derived task id."""
    result = tally(proposal_id)
    if not result["consensus"]:
        return {"ok": False, "outcome": "CONSENSUS_NOT_REACHED", "reason": result["reason"]}
    proposal = relay_io.read_json(_proposal_path(proposal_id))
    task_id = intake_processor._slug("CONSENSUS-" + proposal_id, 48)
    if task_id in intake_processor._node_ids():  # idempotent
        return {"ok": True, "outcome": "ALREADY_SYNTHESIZED", "taskId": task_id}
    admit = intake_processor._run_graph_ops([
        "add-node", "--id", task_id, "--title", proposal.get("title", proposal_id),
        "--cluster", cluster, "--priority", priority,
        "--summary", proposal.get("body", "")[:1000],
        "--tag", "consensus", "--save"])
    if admit.returncode != 0 or task_id not in intake_processor._node_ids():
        return {"ok": False, "outcome": "ADMISSION_FAILED",
                "reason": admit.stderr.strip()[:300]}
    proposal["status"] = "SYNTHESIZED"
    proposal["synthesizedTask"] = task_id
    relay_io.write_json_atomic(_proposal_path(proposal_id), proposal)
    relay_mesh.send_inbox_msg("planner", f"Consensus synthesized to backlog: {task_id}", {
        "proposal_id": proposal_id, "task_id": task_id, "action": "PLAN_NEW_INTAKE"},
        required_action="notify")
    return {"ok": True, "outcome": "SYNTHESIZED", "taskId": task_id}


if __name__ == "__main__":  # pragma: no cover
    relay_io.utf8_console()
    print(json.dumps(tally(sys.argv[1]) if len(sys.argv) > 1 else {"usage": "tally <proposal_id>"},
                     indent=2, ensure_ascii=False))
