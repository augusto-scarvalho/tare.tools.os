# ADR-062: Universal Doctrine of Frugal Engineering, Ambidextrous Innovation and Freedom of Compute (BYOC)

## Status
**RATIFIED / RATIFIED_BY_TRIPARTITE_GOVERNANCE** (Concluded and Approved through `CASE-2026-08-20-ENGINEERING-DOCTRINE-AND-BYOC`)

## Ratification Date
2026-08-21

## Deliberation Quorum
- **Quorum Mode:** `FRONTIER_UNANIMOUS`
- **Google Seat:** `Gemini 3.7 Flash · high` via Antigravity CLI (`agy.EXE`)
- **OpenAI Seat:** `GPT-5.6 Sol · high` via OpenAI Codex CLI (`codex.exe`)
- **Anthropic Seat:** `Claude Fable 5 · high` via Claude Code CLI (`claude.exe`)
- **Independent Mediator:** `z-ai/glm-5.2` (Via Negativa & Autonomous Dialectical Synthesis)

---

## 1. Context & Motivation

The `tare.tools` ecosystem needed a timeless engineering charter, immune to technology fads, hardware-agnostic and oriented to concrete results. It was mandatory to establish universal development principles that would protect the project both against **technical hypertrophy** (unnecessary factories, *YAGNI*, dependency overload and bureaucracy) and against **paralyzing minimalist stinginess** (fear of innovating, attachment to archaic primitives and missed opportunities for technological leaps).

---

## 2. The Doctrine of the 6 Fundamental Principles

The ecosystem's 6 Constitutional Engineering Principles are hereby formally established and ratified:

### 2.1 Principle I: The Primacy of the Via Negativa (Subtraction before Addition in the Core)
- The best code in production is the one that solves the problem with the smallest possible surface of complexity.
- No premature abstraction, factory, indirection layer or microservice is accepted to meet hypothetical needs (*YAGNI*).
- The standard library and the system's native primitives have top priority over external dependencies.

### 2.2 Principle II: The Balance of Fertile Frugality (Engineering Ambidexterity)
- **The Trap of Paralyzing Minimalism:** Being so austere that technological advances, intelligence leaps and new frontiers are rejected for fear of writing code. The ecosystem must be ambitious, bold and open to new ideas.
- **The Trap of Technical Hypertrophy:** Building theoretical castles of premature complexity, unnecessary frameworks and bureaucracy with no practical value.
- **The Synthesis of Ambidexterity:** Unrestricted freedom to explore, prototype and innovate (*Spikes*, research branches and laboratories), combined with surgical filtering and relentless rigor through the Via Negativa when consolidating code into the stable tree. A holistic view of the whole without losing focus on the systemic architecture.

### 2.3 Principle III: The Rule of the Empirical Falsifier Proportional to Risk
- No technical decision or architectural veto is accepted on the basis of abstract dogma or aesthetic opinion.
- Every technical challenge requires a verifiable falsifier suited to its risk class:
  1. *Bugs and Functional Regressions:* Reproducible automated tests (`reproduction_test`).
  2. *Performance and Frugality:* Comparative benchmarks and memory consumption measurements.
  3. *Security and Integrity:* Formal threat models, proofs of static invariants and concurrency/TOCTOU inspections.

### 2.4 Principle IV: Freedom of Compute & Sovereignty (BYOC — Bring Your Own Compute)
- The ecosystem natively supports 3 operational realities with contract parity and predictable degradation:
  1. *Zero-Hardware / Free-Tier Developer:* Operates at $0.00 cost using free cloud quotas (Google Gemini Free Tier, NVIDIA NIM/Build Free Evaluation Tier with 1,000 credits) and open models on a local CPU (llama.cpp).
  2. *Company / Professional Engineer:* Connects commercial API keys securely in the OS Keyring.
  3. *Sovereign User / Homelab:* Runs local models on any GPU (NVIDIA, Apple Silicon, AMD, Intel) through OpenAI-compatible servers, with 100% privacy and offline operation.
- The user is the sole sovereign of their compute power and their data.

### 2.5 Principle V: Contractual Fidelity with a Stop Clause and Safe Amendment
- **At Design Time:** Relentless debate through the Via Negativa and tradeoff analysis to design surgical plans.
- **At Implementation Time:** The executing agent's duty of fidelity to the approved plan (`DECISION.md` / `PACKET.md`), with no insubordination or silent unilateral cutting.
- **Emergency Stop Clause:** If a critical vulnerability, physical infeasibility or falsified premise is discovered during execution, the agent MUST stop execution and issue a Nonconformity Report for quick re-deliberation, removing the paradox between blind obedience and engineering common sense.

### 2.6 Principle VI: Unix Ergonomics & $O(1)$ Frugality Interfaces
- The Unix terminal and standard processes are the priority interaction channel for autonomous agents (zero token overhead).
- Any MCP integration is admissible as long as it proves in a reproducible benchmark an $O(1)$ context cost with a schema-bounded token ceiling, on-demand pagination and failure isolation.

---

## 3. Consequences & Practical Application

1. **Self-Consistency:** Nominal or dogmatic vetoes are formally forbidden; every technical demand must come with a metric and a falsifier.
2. **Unblocked Innovation Cycle:** *Spike* branches and local laboratories (`tare.tools.local-labs`) operate with full creative freedom to test GPU/LLM limits before purging and consolidation into the core.
3. **User Sovereignty:** No feature may force the user to subscribe to closed cloud providers if a viable open and local alternative exists.
