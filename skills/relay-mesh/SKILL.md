---
name: relay-mesh
description: "Coordinate, claim, implement, audit, and land release trains across multi-machine agent swarms using the tare.tools Relay Mesh (MARM)."
---

# Relay Mesh Coordination Skill (MARM)

Use this skill to interact with the distributed Multi-Agent Relay Mesh (`relay/`) synchronized over Google Drive and Git repositories.

---

## 🎯 When to Use This Skill
- To inspect active execution trains, leases, and pending approvals across all machines.
- To claim a task packet (`claim`) and assume the role of **Implementer** or **Auditor**.
- To dispatch a completed implementation to the **Adversarial Auditor**.
- To raise high-priority human alerts (`alert`) upon detecting unresolvable invariant breaks or deadlocks.
- To execute 1-click landing (`land`) of audited release trains into `work-graph.json`.

---

## 🛠️ CLI Quick Reference

```bash
# 1. View global status across all machines
python relay/relay_mesh.py board

# 2. Claim an execution train as Implementer
python relay/relay_mesh.py claim --train <TRAIN_ID> --agent "antigravity" --role "implementer"

# 3. Renew an active lease (heartbeat)
python relay/relay_mesh.py renew-lease --train <TRAIN_ID> --agent "antigravity"

# 4. Dispatch code to Adversarial Auditor after tests pass
python relay/relay_mesh.py dispatch --train <TRAIN_ID> --next-role "auditor" --agent "antigravity" --notes "Implemented code and verified 100% tests pass."

# 5. Claim and Audit as Adversarial Auditor
python relay/relay_mesh.py claim --train <TRAIN_ID> --agent "gemini_auditor" --role "auditor"
python relay/relay_mesh.py audit --train <TRAIN_ID> --status PASS --agent "gemini_auditor"

# 6. Raise Human Alert (Only on critical blockers / deadlocks)
python relay/relay_mesh.py alert --title "Deadlock Detected" --details "Conflict on seam 1" --agent "antigravity"

# 7. Land an audited train (Marks tasks DONE in work-graph.json)
python relay/relay_mesh.py land --train <TRAIN_ID>

# 8. Unhold a stalled train (resets retry backoff and moves back to PENDING)
python relay/relay_mesh.py unhold --train <TRAIN_ID>
```

---

## 📁 Key File Locations
- **Live Board:** `relay/CURRENT_BOARD.md`
- **Machine Board (JSON):** `relay/BOARD.json`
- **Active Trains:** `relay/trains/<TRAIN_ID>/`
- **Machine Leases:** `relay/leases/`
- **Mailboxes:** `relay/inbox/<role>/`
- **Human Alerts:** `relay/alerts/`
- **Human Approvals:** `relay/approvals/`

---

## 🛡️ Universal Adversarial Auditor Red-Teaming Playbook (U-7D)

As an **Auditor (Plan or Code Auditor)** in the `tare.tools` mesh, your goal is **NOT** to be agreeable or write superficial praise. You are the **Principal Security & Systems Gatekeeper** protecting the system against catastrophic failures, hidden deadlocks, race conditions, memory leaks, and boundary violations across **ANY software domain** (kernels, web apps, databases, distributed systems, ML pipelines, CLI tools).

### 🔍 The Universal 7-Dimension Attack Matrix (U-7D):

1. **Logic, Invariants & Semantic Integrity:**
   - Are mathematical preconditions, postconditions, and domain invariants formally preserved?
   - What happens on empty inputs, nulls, negative numbers, extreme scale, clock skew, and boundary edges?
   - Is every state transition explicit? Does the system make impossible states unrepresentable?
   - Is idempotency guaranteed ($f(x) \equiv f(f(x))$) across retries, crashes, and duplicate events?
2. **Concurrency, Asynchrony & Distributed Systems:**
   - What happens if 2+ instances, threads, hosts, or agents execute concurrently?
   - Identify all TOCTOU (Time-of-Check to Time-of-Use), check-then-write hazards, and race windows.
   - Look for lock order inversions (deadlocks), starvations, lost updates, and memory visibility issues.
   - If distributed/replicated: evaluate network partitions, out-of-order delivery, and split-brain risks.
3. **Resilience, Failure Modes & Crash Survival:**
   - Does the design FAIL CLOSED (stop safely, alert, hold) or fail open (silently corrupt data/state)?
   - Blast Radius: Does a secondary module failure crash the primary core (bulkhead isolation)?
   - Retries: Are retries bounded with exponential backoff and jitter to prevent thundering herds?
   - Crash Consistency: If power is cut / `SIGKILL` issued mid-write, can the system recover cleanly on reboot?
4. **Security, Threat Model & Injection Surfaces:**
   - Are untrusted inputs sanitized at boundaries? Check for command injection, SQL/NoSQL injection, prompt injection, and path traversal (`../`).
   - Are credentials, secret keys, or PII exposed to logs, telemetry, error messages, or client payloads?
   - Evaluate authentication, authorization, and confused deputy vulnerabilities.
5. **Physical Resource Bounds & Algorithmic Complexity:**
   - Is algorithmic complexity bounded? Flag hidden $O(N^2)$ / $O(2^N)$ nested loops and ReDoS regex patterns.
   - Are memory and I/O strictly bounded? (Streaming vs loading entire files/tables in RAM, connection pools, backpressure).
   - Are file descriptors, sockets, threads, and subprocesses guaranteed to close/cleanup via RAII / try-finally?
6. **Compatibility, Migration & Rollback Safety:**
   - Does this introduce breaking API, schema, or binary changes for existing consumers?
   - If a rollback is performed 1 hour after deployment, will persisted state remain readable and uncorrupted?
7. **Forensic Observability & Actionability:**
   - Are failure modes clearly identifiable with structured contextual errors (who, what payload, why)?
   - Can an operator reconstruct the exact sequence of events from logs/metrics alone without guesswork?

---

### 🌐 Domain-Adaptive Attack Lenses (Detect and Apply):

| Domain | What the Auditor Attacks |
|---|---|
| **Web Apps & Frontend** | Re-render loops, async race conditions, responsive layout breakage, XSS, event listener leaks, state synchronization. |
| **APIs & Microservices** | OpenAPI contract violations, non-semantic HTTP codes, auth bypass, rate limit exhaustion, upstream timeouts. |
| **Systems & CLI** | Exit codes (0 vs !=0), stdio piping, SIGINT/SIGTERM traps, cross-platform path resolution (Windows/Linux). |
| **Databases & Storage** | ACID transaction atomicity, write-ahead logs, serializable isolation, schema migration rollback, index lock contention. |
| **Multi-Agent & AI** | State machine loops, token budget overruns, agent hallucination cascades, non-deterministic drift. |

---

### 📋 Mandatory Report Structure for `AUDIT_REPORT.md`:
1. Header naming all task IDs and binding the current `PACKET.md` SHA-256 digest.
2. `1. Executive Summary & Seam Verification`.
3. `2. Adversarial Battery & Falsifier Assessment` (systematic assessment against the U-7D dimensions).
4. `3. Production Footprint Verification` (line-by-line diff review).
5. `4. Audit Recommendation: Approve vs Do Not Approve` (explicit trade-off balancing with technical liabilities and residual risks).
6. Terminal Verdict Marker (`VERIFIED_PASS` or `AUDIT_VERDICT: FAIL <detailed actionable reason>`).


