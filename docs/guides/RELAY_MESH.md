# Relay mesh (MARM): roles, commands and the round table

The multi-agent relay mesh (trains, leases, the round table) and its operating rules, as `AGENTS.md` held
them until 2026-10-08. They bind the agents running the relay (`relay/relay_mesh.py`,
`relay/autonomous_planner.py`, `relay/round_table_engine.py`), not every agent in the repository: those read
[`AGENTS.md`](../../AGENTS.md), and each tare role gets its own prompt capsule (`relay/prompt_capsules.json`).
Fetching web pages for any agent: [`skills/web-ingestion/SKILL.md`](../../skills/web-ingestion/SKILL.md).

## 👥 Swarm Roles & Division of Responsibilities

| Role | Primary Agent | Key Responsibilities | Instruction File |
|---|---|---|---|
| **Planner** | `codex_planner` / `chatgpt_architect` (OpenAI GPT-5.6 Sol High) | Selects frontier tasks, initializes trains (`init-train`), writes `PACKET.md`, dispatches to Plan Auditor. | [`docs/guides/RELAY_PLANNER_PLAYBOOK.md`](RELAY_PLANNER_PLAYBOOK.md) |
| **Plan Auditor** | `antigravity` (Google Gemini 3.7 High Thinking) | Adversarially reviews Codex's `PACKET.md`, verifies DAG acyclicity, bounds footprints, signs `PLAN_AUDIT_REPORT.md`. | `skills/relay-mesh/SKILL.md` |
| **Implementer** | `antigravity` (Google Gemini 3.7 Medium Thinking) | Claims trains (`claim`), writes code & test falsifiers, passes `pytest`, dispatches to Code Auditor. | [`AGENTS.md`](../../AGENTS.md) |
| **Code Auditor** | `codex_auditor` (OpenAI GPT-5.6 Sol High / Codex) | Adversarially audits Antigravity's code, runs adversarial test battery, verifies anti-tamper invariants, signs `AUDIT_REPORT.md`. | [`docs/guides/RELAY_PLANNER_PLAYBOOK.md`](RELAY_PLANNER_PLAYBOOK.md) |
| **Smart Admission Policy** | Deterministic OS gate | Recomputes Work/SpecGraph/Git evidence and lands exact admitted candidates by CAS; optional independent audit excludes the implementer `bias_domain` and prefers cross-vendor. | [`relay/landing_policy.py`](../../relay/landing_policy.py) |
| **Human Gatekeeper** | Operator | Exception/legacy path for explicit intervention; not the routine automation bottleneck. | `relay/approvals/` |
| **Bookkeeper / Curator** | `antigravity` / Gemini 3.7 (Medium Thinking) | Graphifies landed evidence, validates DAG hygiene, re-exports HTML viewer, logs journal. | `relay/bookkeeper.py` |

## Local operator trust boundary

The current contract is one trusted local operator, not mutually distrustful participants.
A GitHub account or a `senderRole` label is not proof of authority: transport and labels
are not the host's role binding. The host fixes role, session/deployment configuration,
and capabilities ([`configuration` / `run_session`](../../relay/native_agent.py));
artifacts carry the session and profile digest, not authority.
The native code auditor cannot edit: [`work_audit.run`](../../relay/work_audit.py)
gives it read/list/search access, no writable paths, commands or admission power.
Code audit is optional by default ([`default_policy` / `evaluate_admission`](../../relay/landing_policy.py)).
When counted as independent, it requires a different registered `model_id` and `bias_domain`
([`attest_identity`](../../relay/landing_policy.py), [`prepare`](../../relay/work_audit.py));
its review references the exact candidate and recorded evidence (`work_audit.run`).
A separate `plan-auditor` session alone does not attest model independence.
Saved and imported artifacts remain proposals with `authority: NONE`
([`save_output` / `read_input`](../../relay/native_artifacts.py)); importing them does not verify their sources.
Hashes bind bytes; they do not authenticate a person.
Settlement remains with the OS/policy or explicit operator intervention, not agent labels or reviews.
File-tool scopes and the selected [command sandbox](../../relay/command_protection.py) already confine effects.
Sharing an OS account does not bypass those boundaries; role keys do not replace isolation
or authenticate an external controller.
This use case needs no added PKI, per-role accounts or new gates.
A different trust boundary—untrusted participants or different controllers—must be analyzed
when it exists; it is not unfinished work for the trusted local installation.

---

## ⚡ Core CLI Commands Reference
- **Checkout Selection:** use the pinned satellites under `satellites/` (the Library is `satellites/tare.tools.library/`). Verify the selected checkout revision; do not substitute another checkout or advance satellite gitlinks implicitly.
- **Check Status:** `python relay/relay_mesh.py board`
- **Query Next Tasks:** `backlog --graph work-graph.json next`
- **Work Context:** `python -m tare_ops context --graph work-graph.json --work <WORK_ID>` reads the Work's declared `grounding_refs` where they live.
- **Specs and ADRs:** a Work names them as `repo:path` in the repository that owns them: `tare.tools.os` documents in this tree, a satellite's in `satellites/<repo>/` at the pinned gitlink. A spec is admissible when its frontmatter declares `acceptance_criteria`. Commit it in its owner repository; no catalog update follows. A run writes the documents it uses to `SOURCES.json` in its train.
- **Search Library-Owned Knowledge:** from `satellites/tare.tools.library/`, run `python -m tools.query --search "<QUERY>"`.
- **Initialize Train:** `python relay/relay_mesh.py init-train --id <TRAIN_ID> --title "<Title>" --tasks "<TASK_IDS>" --agent "<AGENT_ID>"`
- **Claim Lease:** `python relay/relay_mesh.py claim --train <TRAIN_ID> --agent "<AGENT_ID>" --role "<ROLE>"`
- **Dispatch Handoff:** `python relay/relay_mesh.py dispatch --train <TRAIN_ID> --next-role "<NEXT_ROLE>" --agent "<AGENT_ID>" --notes "<NOTES>"`
- **Audit Sign-off:** `python relay/relay_mesh.py audit --train <TRAIN_ID> --status PASS --agent "<AGENT_ID>"`
- **Land Release:** `python relay/relay_mesh.py land --train <TRAIN_ID>`
- **Smart Admit + Land:** `python relay/relay_mesh.py admit --train <TRAIN_ID>`
- **Run Bookkeeper:** `python relay/bookkeeper.py [TRAIN_ID]` (Graphifies evidence, exports HTML/Mermaid, writes journal)
- **Autonomous Planner Worker:** `python relay/autonomous_planner.py` (Zero-touch Codex planning)
- **Tripartite Round Table:** `python relay/relay_mesh.py round-table --case <CASE_ID> --doc <DOC_FILE>`
- **Round Table Human Revision:** `python relay/round_table_engine.py submit-revision <CASE_ID> --file <FILE> --expected-base-version <V> --expected-base-sha256 <HASH> --authorize-result-sha256 <HASH> --operator-label <LABEL> --reason "<REASON>"`
- **Round Table Operator Adjudication:** `python relay/round_table_engine.py adjudicate <CASE_ID> --file <FILE> --expected-version <V> --expected-sha256 <PROPOSAL_HASH> --authorize-adjudication-sha256 <ADJUDICATION_HASH> --operator-label <LABEL> --statement "<STATEMENT>"` (never for `self_referential=true`)
- **Clean Web URL Ingestion (Trafilatura/Firecrawl):** `python scripts/fetch_clean_url.py <URL> [--output file.md] [--json]`
- **Audit Round Table Telemetry:** `python scripts/report_round_table_telemetry.py`
- **Supervisor Daemon v3:** `python relay/watcher_daemon.py`

---

## 🛑 Operator Interaction Invariant (Zero-Chit-Chat / Autonomous Execution)
- **Zero Routine Chat Queries:** Agents MUST NEVER ask routine questions in chat (e.g. "which task to pick?", "should I continue?").
- **Autonomous Swarm Governance:** Agents must decide autonomously using the DAG frontier score, MARM relay pipeline, and the multi-agent consensus engine (`consensus_engine.py`).
- **Human Gatekeeper Invocations are Restricted To:**
  1. **Explicit Exception/Override:** only when smart admission is inconclusive or the owner deliberately chooses the compatible human path.
  2. **Critical Human Alerts:** Unrecoverable deadlocks or critical failures requiring physical human intervention (`python relay/relay_mesh.py alert`).

---

## 🏛️ Tripartite Round Table Protocol & Anti-Build-Trap Governance (RT-N1 to RT-N8)
- **Mandatory 3-Block Proposal Structure:** Every proposal submitted to the Round Table (`python relay/round_table_engine.py init`) MUST contain:
  1. **`## 1. Core Objectives (In-Scope)`**: Concrete, bounded requirements.
  2. **`## 1.2 Explicit Non-Goals (Via Negativa / Out of Scope)`**: Strictly declared exclusions to prevent scope creep and academic inflation.
  3. **`## 3. Falsification & Traceability Matrix`**: Automated executable test mapping.
- **Pre-Submission Linting:** Run `python relay/round_table_engine.py lint --doc <FILE>` before initializing any deliberation case.
- **Seat Inviolable Rule:** No seat may vote `REVISE` or `REJECT` based on features declared in Non-Goals / Via Negativa. All blocking issues require concrete falsifiers against declared in-scope objectives.
- **Human-Sovereign Authority (ADR-056):** The three chairs are an independent consultative red team; the human gatekeeper is the final authority. Human exceptions preserve the real tally and use `AUTHORIZED_BY_OPERATOR_EXCEPTION`, never fabricated `APPROVED`. Self-referential governance changes require unanimous Round Table approval plus explicit human ratification and cannot use operator adjudication.
- **Agent Runtime Boundary (ADR-057):** `Agent Loop Execution` belongs to the future canonical repository `tare.tools.agent-runtime`, not to OS or Kernel internals. Keep `tare.tools.harness` frozen; do not rename, import, copy, or resume its monolith. No executable bootstrap precedes the versioned F0 boundary receipt.

---

## 🚨 Backlog Anti-Drift Guard Rails and Domain Separation

1. **Strict Separation Between Backlogs:**
   - **OS Development Backlog (Repositories / DAG):** Managed strictly through `tare.tools.backlog-graph`.
   - **Hardware / Local Model Experiment Backlog (the GPU workstation):** belongs to `tare.tools.local-labs`, a separate research bench that is private by default; its backlog, experiments and evidence live there, not in this repository. Its execution follows that owner's current handoff and gates; reviewing the OS does not authorize starting experiments.
2. **Ban on Reviving Legacy Monolithic Structures:**
   - The old `tare.tools.harness` prototype (186 modules) and the automatic legacy train generators built on it are **FULLY DISCONTINUED**.
   - Autonomous agents must NEVER create trains or packets pointing to discontinued monolithic modules.
