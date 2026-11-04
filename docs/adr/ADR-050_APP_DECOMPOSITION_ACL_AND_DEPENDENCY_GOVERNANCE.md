# ADR-050: Polyglot Application Decomposition, Anti-Corruption Layers (ACL), Defensive Dependency Management, CAS Concurrency and Governance of High-Performance Forks

- **Status:** Ratified and Approved by Full Tripartite Consensus (Google, Anthropic and OpenAI — Final Version v004)
- **Reference:** `CASE-2026-08-19-APP-DECOMPOSITION-ACL-AND-DEPENDENCY-GOVERNANCE-V4`
- **Date:** 2026-08-19
- **Authors:** Antigravity Mediator under tripartite deliberation (OpenAI Codex, Google Gemini 3.7, Anthropic Claude Fable 5) and Human Engineering direction
- **Scope:** `tare.tools.os`, `tare.tools.kernel`, `tare.tools.specgraph`, `tare.tools.backlog-graph`, `tare.tools.dialog-engine`, `tare.tools.research`, `slop.cpp`

---

## 1. Context & Theoretical and Practical Grounding

Building an **Agent Operating System** for autonomous and collaborative software engineering runs into the classic *"Buy vs. Build"* dilemma (reuse vs. building in-house).

Historically, multi-agent architectures degenerate into two pathological extremes:
1. **Not-Invented-Here Syndrome (NIH):** Trying to rewrite syntax parsers, relational databases, inference engines and sandboxes from scratch, producing chronic delays, low-level bugs and fragile code.
2. **Parasitic Coupling & Dependency Bloat:** Careless adoption of external frameworks and packages that drag in huge trees of transitive dependencies, impose invasive paradigms on the central heap and pile up an unpayable desynchronization debt (*upstream desync debt*).

To reach **maximum execution performance** without giving up **strict modularity, operational isolation and code sovereignty**, this ADR sets out a theoretical grounding based on:
- **Information Hiding (Parnas, 1972):** Modules expose only minimal interfaces and hide their internal implementation decisions.
- **Anti-Corruption Layer / Ports & Adapters (Evans DDD, Cockburn Hexagonal):** The central OS domain never consumes third-party types or structures directly; adapters isolate external variability through versioned contracts and standardized envelopes.
- **Polyglot Efficiency by Physical Cost Layer:** Strict technology distribution based on the computational cost of each subdomain (C++/CUDA on silicon, C/POSIX in the sandbox, Rust/C-ABI in parsing and Python 3.12+ in high-level orchestration).
- **Linearizability & Single-Writer Authoritative Ledger (Lamport, 1978; Liskov, 1988):** Authoritative state transitions through atomic Compare-And-Swap (CAS) in a single local writer process, closed ACID transactions with immutable receipts, idempotency reservation bound to a cryptographic `request_digest`, and remote coordination exclusively through authenticated RPC.
- **Fail-Safe Defaults / Fail-Closed Principle (Saltzer & Schroeder, 1975):** Every node or subsystem that lacks the isolation or physical compatibility prerequisites must refuse execution tasks explicitly, emitting a formal error receipt, never degrading silently into an unsafe state.

---

## 2. Proposed Architectural Decisions (In-Scope Goals)

```mermaid
flowchart TD
    subgraph Layer1 ["1. Experience Plane (TypeScript / WebGL)"]
        WebUI["Visual Cockpit & Graph Visualization<br>Cytoscape.js / Canvas WebGL (Budget: p95 &le; 16.6ms / 60 FPS)"]
    end

    subgraph Layer2 ["2. Control & Orchestration Plane (Python 3.12+)"]
        OSRemote["Remote MARM Node (Worker / Agent)<br>(Generates UUIDv7 + Local SHA-256 Digest)"]
        OSMaster["Master Node / Authoritative State Governor<br>(Single-Writer Authoritative Ledger Service)"]
        KernelContracts["tare.tools.kernel / contracts/v1<br>(JSON Schemas, GraphML, DDL & Topology Doc)"]
    end

    subgraph Layer3 ["3. Anti-Corruption Layer & Storage Substrate (ACL)"]
        AiderAdapter["AiderDriver Adapter (CLI Subprocess)<br>Contract v1.0.0 | Bidirectional Validation"]
        TreeSitterAdapter["TreeSitter Adapter (C-ABI Pure Bindings)<br>Zero-Copy Parse | Bounds & Timeouts"]
        AuthoritativeLedger["Authoritative SQLite WAL Ledger (Local NVMe Disk)<br>Single Atomic Transaction: CAS + Operation Receipt Digest"]
        TelemetryIPC["IPC Telemetry Observer & Benchmarking<br>(Metrics: RSS, JSON Latency, ERR_CAS_CONFLICT Contention)"]
    end

    subgraph Layer4 ["4. High-Performance Hardware Substrate & Sandbox"]
        SlopCPP["slop.cpp (Optimized llama.cpp Fork)<br>4-Tier Gate: CUDA sm_86 + Perf + VRAM + Greedy Equivalence"]
        BwrapSandbox["Bubblewrap (bwrap) Sandbox<br>Linux Namespaces (--unshare-net, --ro-bind)<br>Budget: overhead &le; 42ms"]
        PlatformGate{"Platform Matrix & Fail-Closed Gate<br>(Linux Node vs. Windows Host)"}
    end

    WebUI -->|JSON-RPC / WebSockets (Schema v1)| OSMaster
    OSRemote -->|Authenticated RPC (mTLS / Tailscale)| OSMaster
    OSMaster -.->|Governs Contracts & Topology| KernelContracts
    OSMaster -->|Subprocess IPC / JSON Schema v1| AiderAdapter
    OSMaster -->|C-ABI Bindings| TreeSitterAdapter
    OSMaster -->|Local Atomic Transaction (In-Process)| AuthoritativeLedger
    OSMaster -->|Tailscale HTTP / mTLS| SlopCPP
    AiderAdapter --> PlatformGate
    PlatformGate -->|Linux Host with bwrap| BwrapSandbox
    PlatformGate -->|Windows Host / No bwrap| RefuseExecution["ERR_PLATFORM_UNSUPPORTED<br>(Fail-Closed Receipt / No Bypass)"]
    AiderAdapter -.-> TelemetryIPC
    AuthoritativeLedger -.-> TelemetryIPC
```

---

### A. Polyglot Decomposition, Platform Matrix & Performance Budgets

The technology distribution by physical specialization domain is parameterized with an explicit platform support matrix and auditable *budgets*:

| Layer | Technology & Pinned Version | Supported Platforms | Fail-Closed Behavior on an Unsupported Platform | Performance Budget & Reference Hardware |
| :--- | :--- | :--- | :--- | :--- |
| **Inference & GPU** | **C++20 / CUDA 12 (`slop.cpp` fork)** | Linux (x86_64) with NVIDIA GPU ($\ge \text{sm\_86}$) | Emits `ERR_INFERENCE_HARDWARE_UNAVAILABLE`. Rejects local inference; routes to a remote node over Tailscale. | Prefill $\ge 600\text{ t/s}$, Generation $\ge 80\text{ t/s}$ (RTX 3090, batch=1, Q4_K_M). TTFT p95 $\le 120\text{ ms}$. |
| **Isolation & Sandbox** | **C / POSIX (`bubblewrap` v0.9+)** | Linux (Kernel $\ge 5.10$ with user namespaces) | **Immediate task refusal** with receipt `ERR_PLATFORM_UNSUPPORTED`. **Bypass forbidden** for unsafe execution on the host. | Isolated process spawn overhead p95 $\le 42\text{ ms}$ (CPU i7-12700H / AMD 5950X). |
| **AST Syntax Parsing** | **`tree-sitter` (C / Rust C-ABI)** | Linux (x86_64), Windows (x64), macOS (ARM64) | N/A (universally cross-platform through pure C-ABI). | Incremental parse p95 $\le 5\text{ ms}$ per 10k-line file; full parse $\le 50\text{ ms}$ on 100k lines. |
| **Persistence & CAS Ledger** | **SQLite (WAL + JSONB) Single-Writer** | Linux (x86_64), Windows (x64), macOS (ARM64) | Direct access from multiple nodes through a shared filesystem (NFS/SMB) is forbidden. Remote access only through authenticated RPC. | Atomic CAS+Receipt transaction write latency p99 $\le 4\text{ ms}$ (local NVMe of the authoritative writer node). |
| **Orchestration & FSM** | **Python 3.12+ (CPython)** | Linux (x86_64), Windows (x64), macOS (ARM64) | N/A (Agent OS runtime core). | FSM event dispatch latency p99 $\le 2\text{ ms}$. |
| **Cockpit & Visualization** | **TypeScript + WebGL / Cytoscape.js** | Modern Browsers (Chrome/Edge/Firefox $\ge 120$) | Graceful degradation to a static table view (DOM). | Continuous graph rendering (up to 28k nodes): frame time p95 $\le 16.6\text{ ms}$ ($\ge 60\text{ FPS}$). |

---

### B. Anti-Corruption Layers (ACL), Versioned Contracts & Process Isolation

1. **Inviolability of the Domain Core & Mandatory AST Linter:**
   - No business entity of `tare.tools.os` or of the satellite repositories may inherit from, import or depend directly on third-party library types.
   - Core purity is enforced by a mandatory **architectural static AST linter** run as a pre-commit hook and a blocking CI step, preventing imports outside the stdlib or the canonical contracts at the root of `tare.tools.kernel` and `tare.tools.os`.
   - The satellite repositories (`specgraph`, `backlog-graph`, `kernel`, `dialog-engine`) communicate exclusively through immutable schemas defined in `tare.tools.kernel/contracts/v1/` (JSON Schemas, GraphML, Network Topology and SQLite DDL).

2. **Process Boundary with Versioned Schemas (Request / Response / Receipt):**
   - Every invocation of an external tool through CLI/subprocess is strictly wrapped in versioned bidirectional contracts (e.g. `AiderDriverRequest_v1`, `AiderDriverResponse_v1`).
   - If the external process fails, hangs or emits a malformed payload, the adapter performs a **fail-closed quarantine**, producing a structured `ErrorReceipt` without corrupting the Agent OS memory.

---

### C. Governance of High-Performance Forks (`slop.cpp`): 4-Tier Qualification Gate

To keep the low-level hardware levers without accumulating desynchronization debt (*upstream desync debt*):

```
       Upstream Canonical (llama.cpp @ master)
                │ (Periodic Rebase via Stable Tag)
                ▼
  ┌─────────────────────────────────────────────────────────────┐
  │       Fork slop.cpp (Branch: feature/tare-cuda-levers)      │
  │  • Patch 1: [B2b] DMA Pinning Buffer                        │
  │  • Patch 2: Prefetch Skip-Staging Direct Copy               │
  │  • Patch 3: MoE Hot-Cache Expert Retention                  │
  │  • Patch 4: MTP Speculative Decoding (n=3)                  │
  └──────────────────────────────┬──────────────────────────────┘
                                 │
                                 ▼
              Automated Qualification Gate (bless_fork.sh - 4 Tiers)
              • Tier 1: Clean sm_86 Build (CUDA 12)
              • Tier 2: Physical TTFT & Throughput Measurement (Prefill &ge; 600 t/s)
              • Tier 3: VRAM Safety & Memory Leak Check
              • Tier 4: Greedy Semantic Equivalence (Zero Logit Drift)
```

1. **Atomic & Isolated Patches:** Our acceleration differentiators live as clean, documented commits on top of the upstream `master` branch.
2. **Automated 4-Tier Gate (`bless_fork.sh`):** No upstream rebase is accepted unless it passes the full qualification gate (including the greedy logit semantic equivalence test).
3. **Upstream First:** Generic fixes and improvements of broad community interest are sent as PRs to the canonical upstream to keep our delta minimal.

---

### D. Dependency Admission Gate (DAGate)

Every new library that is a candidate to enter the ecosystem must pass the formal checklist:

| Admission Criterion | Rule / Maximum Limit | Action on Failure |
| :--- | :--- | :--- |
| **Transitive Weight** | $\le 3$ direct transitive dependencies. | Reject; adopt a zero-dependency alternative or run it through an isolated CLI. |
| **Licensing** | Permissive license (Apache-2.0, MIT, BSD, ISC). | Reject immediately if viral GPL or commercially restrictive. |
| **API Invasiveness** | The library works on plain data (JSON, bytes, structs) without forcing inheritance from base classes. | Wrap it, mandatorily, in an Anti-Corruption Layer (ACL). |
| **Hermeticity & Resilience** | A library failure does not bring down the Agent OS process. | Wrap it in a fail-closed try/except with a telemetry receipt. |

---

## 2.1 Explicit Non-Goals (Via Negativa & Architectural Boundaries)

1. **No Reinventing Mature Industry Tools:** Building hand-written AST parsers, proprietary relational databases or home-made isolation tools is forbidden when `tree-sitter`, `sqlite` and `bubblewrap` already do the job excellently.
2. **No In-Process Import of Heavy Frameworks:** `tare.tools.os` does not import third-party packages directly into the main heap; separation through CLI/subprocess is mandatory for rich tools.
3. **No Direct Inter-Satellite Coupling:** The satellite repositories (`specgraph`, `backlog-graph`, `kernel`, `dialog-engine`) communicate strictly through data schemas (JSON Schema, SQLite, GraphML), never through cross Python code imports.
4. **No Ungoverned Forks:** Keeping manual copies of external libraries without an automated qualification script and a documented rebase policy is expressly forbidden.
5. **No Unsafe Sandbox Degradation:** Running LLM-generated code on the host without sandbox isolation when `bubblewrap` is unavailable is strictly forbidden; execution must fail closed with an explicit error code.

---

## 3. Falsification & Traceability Matrix

| Invariant / Requirement | Verification Mechanism | Automated Test / Falsifier | Implementation Module |
| :--- | :--- | :--- | :--- |
| **`REQ-DEC-01`: Domain Core Purity** | Static AST import linter in `tare.tools.kernel` | `tests/test_architecture_boundaries.py` | `tests/` & CI Pre-commit |
| **`REQ-DEC-02`: Sandbox Hermeticity** | Execution test attempting to reach the network/host files | `EXP-04` & `tests/test_sandboxed_execution.py` | `scripts/bwrap_runner.py` |
| **`REQ-DEC-03`: Fork Qualification Gate** | Run `tools/scripts_sh/bless_fork.sh` in `slop.cpp` | `tests/test_fork_qualification.py` | `slop.cpp/tools/scripts_sh/bless_fork.sh` |
| **`REQ-DEC-04`: AST Parsing Latency** | `tree-sitter` parse benchmark on a 100k-line codebase | `tare.tools.specgraph/tests/test_ast_perf.py` | `tare.tools.specgraph` |
| **`REQ-DEC-05`: Single-Writer CAS Concurrency** | Parallel contention test with 100 writer threads | `tests/test_single_writer_cas.py` | `tare.tools.kernel/data_plane` |

---

## 4. Implementation Roadmap

1. **Phase 1 (Contracts & Schemas Layer):** Create the canonical v1 schemas in `tare.tools.kernel/contracts/v1/` and the architectural AST linter.
2. **Phase 2 (CLI & TreeSitter Isolation Adapters):** Implement the isolated `tree-sitter` and `AiderDriver` adapters with immutable JSON receipts.
3. **Phase 3 (4-Tier Gate in `slop.cpp`):** Automate the `bless_fork.sh` script with greedy logit semantic equivalence validation.
