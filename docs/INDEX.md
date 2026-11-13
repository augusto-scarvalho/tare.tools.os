# Documentation map

Every document under `docs/`, grouped by purpose, with one line on what it covers. Manuals for installing, using
and operating tare live in `guides/`; decisions in `adr/` and `decisions/`; contracts in `specs/`; dated
qualification evidence in `qualification/` and `history/`. Session reports, handoffs, dated plans and audits
stay out of Git on the machine that produced them (`docs/operations/` and `docs/evidence/` are in
`.gitignore`; the two files under `operations/` listed below are tracked exceptions).

Portuguese versions of the installation guide and the TUI quickstart are in [`pt-BR/`](pt-BR/). The English
documents are the reference.

## Start here

- [README](../README.md): what tare is (a terminal workspace where a coding agent works through separate,
  accountable roles) and how to get started.
- [Install, update and roll back a version](guides/PRODUCT_INSTALLATION.md): the offline product bundle, its
  installation, updates and rollback.
- [TUI daily use: quickstart](guides/TUI_DAILY_USE.md): everyday work in the terminal UI, from launching and
  choosing a project to reviewing changes.
- [One chat, separate execution roles](guides/CONVERSATION_ROLES.md): the coordinator that turns a plain-language
  request into execution by separate roles, with no per-message role selection.
- Portuguese: [Instalar, atualizar e voltar uma versão](pt-BR/PRODUCT_INSTALLATION.md) and
  [Uso diário do TUI](pt-BR/TUI_DAILY_USE.md).

## Using the TUI

- [Tare setup](guides/SETUP.md): the `/setup` screen; one connection (a CLI subscription login or an API key)
  is enough to start.
- [Project permissions in the TUI](guides/TUI_PROJECT_PERMISSIONS.md): read, edit and system-command
  permissions for a project, and the **Implement this plan?** dialog that shows a plan before it is implemented.
- [The Linux sandbox for checks](guides/LINUX_SANDBOX.md): bubblewrap, what a check gets and where it differs from
  Windows, the requirements, and how an administrator allows bubblewrap on Ubuntu.
- [Model and reasoning selection](guides/MODEL_PICKER.md): choosing the model and reasoning effort per
  conversation and role.
- [Guidance sent to the models](guides/GUIDANCE.md): the `/guidance` panel; tare's role guidance (editable), the
  blocks Claude Code, Codex and AGY add on their own (remove or replace where allowed) and tare's protocol texts.
- [Ways of working](guides/WAYS_OF_WORKING.md): the four answer forms of a session (direct by default, three
  opt-in experiments).
- [Delegated planning](guides/DELEGATED_PLANNING.md): the separate planner used for changes and its
  `/delegation` settings.
- [Assisted exploration inside native flows](guides/ASSISTED_EXPLORATION.md): the read-only explorer for new
  conversations and plan/code flows.
- [Challenger mode](guides/CHALLENGER_MODE.md): the optional challenger capability (off by default) and the
  historical FLOW-19 experiment results.
  Data: [ordinary X5 case, 2026-10-29](guides/CHALLENGER_ORDINARY_X5_2026-10-29.json).
- [FLOW-19 implementer profiles](guides/IMPLEMENTER_PROFILES.md): the Cheap, Intermediate and Smart
  implementer policy (v1).
- [See who performed the work](guides/EXECUTION_TEAM.md): the **Team** view (`/team`) of a conversation's saved
  executions.
- [Why a route cannot start](guides/SCHEDULING_DIAGNOSTICS.md): rejected routes under **tare auto**, with code,
  explanation and next step.
- [Automatic flow selection fallback](guides/FLOW_SELECTION_FALLBACK.md): what happens when the local flow
  evaluator is unavailable.
- [Inspecting project JSON in a conversation](guides/PROJECT_DATA_READS.md): reading minified JSON through the
  existing `read` tool.
- [Direct-conversation attachment composition](guides/NATIVE_ATTACHMENTS.md): attaching files to a direct
  conversation.
- [Web reading and the internet researcher](guides/CONVERSATION_RESEARCH.md): `search_web`, page reading and
  the researcher role.
- [Web search APIs](guides/WEB_SEARCH_APIS.md): adding a DeepSeek Search, SerpApi or Firecrawl key.
- [Storage and cleanup](guides/STORAGE_MAINTENANCE.md): the `/storage` view and safe cleanup, including while a
  task runs.
- [Evidence at completion](guides/COMPLETION_EVIDENCE.md): how a delivery recorded from the conversation is
  backed by evidence, managed landing and older records.
- [TUI and flows user guide](guides/FLOWS_TUI_USER_GUIDE.md): superseded prototype guide that points to the
  daily-use quickstart and keeps a research synthesis.

## Models, providers and inference

- [Vendor CLI inference, with Tare owning the agent](guides/VENDOR_CLI_INFERENCE.md): using AGY, Codex or
  Claude CLIs as inference backends for native sessions and Work.
- [Codex inference with the Tare agent loop](guides/NATIVE_CODEX_INFERENCE.md): Codex as a model behind the
  native agent loop, and its qualification.
- [HTTP providers](guides/HTTP_API_PROVIDERS.md): models in the picker, keys and connections, sessions, usage
  and cache for HTTP API providers.
- [OpenCode: subscription and pay-per-token API](guides/OPENCODE.md): the two OpenCode providers, the operator's model
  list, key setup, what tare sends and records, and how the profiles are refreshed.
- [Provider quota and credit](guides/PROVIDER_QUOTA.md): `tare quota`, what each subscription and API account
  reports as left, where each reading comes from and what no provider reports.
- [OpenRouter models and routes per role](guides/OPENROUTER_MODELS.md): the operator-approved OpenRouter
  connection contracts.
- [Internal Amazon Bedrock catalog](guides/BEDROCK_CATALOG.md): why each Bedrock model was selected; the live
  table comes from `relay/round_table_roster.json`.
- [Temporary inference policy](guides/COORDINATOR_POLICY.md): per-instance overrides of roles, reasoning and
  model pins, with offline evidence.
- [Exploration and research delegation policy](guides/DELEGATION_POLICY.md): when exploration and research are
  delegated and to which roles.
- [CLI and lean MCP](guides/CLI_MCP.md): the same runtime from the TUI, a terminal or an MCP client.
- [tare as a local model in VS Code Copilot](guides/VSCODE_COPILOT.md): `tare serve` as an OpenAI-compatible
  Custom Endpoint for Copilot Chat.
- [Agent commands on Windows](guides/WINDOWS_AGENT_COMMANDS.md): the host facts (OS, shell, PATH executables)
  the terminal gives the agent on Windows.
- [The shared local GPU](guides/SHARED_GPU.md): who gets the GPU, the `gpu` command on the GPU node, what tare
  does when it is busy and `tare gpu status`.
- [Inference telemetry](guides/INFERENCE_TELEMETRY.md): what is measured per run and model call, data sources
  and how to reproduce the report.
- [EXP-3: locality and loading cost](guides/LOCALITY_COST.md): pointer to the bounded Local Labs measurement.

## Work runs, workflows and planning

- [`tare-ops run <WorkId>`: run and settle an explicit Work](guides/WORK_RUN.md): the managed Work run, its
  command controls and the reuse → gap → owner map.
- [tare-ops: local composition and frugality cuts](guides/TARE_OPS_LOCAL_INTEGRATION.md): the `tare_ops.py`
  facade over existing functions, what was kept and what was pruned.
- [Inspecting the known Work recipe](guides/WORKFLOW_COMPILER.md): `tare workflow analyze` and the static Work
  recipe (FLOW-09).
- [Ready Work dispatch and recovery](guides/WORKFLOW_DISPATCH.md): running a sequence of selected prepared
  Works and recovering it (FLOW-10).
- [Parallel Work sequence: operator guide](guides/PARALLEL_WORK_GUIDE.md): overlapping implementation
  preparation for two prepared, distinct Works.
- [Individual Works and explicit composition](guides/WORK_COMPOSITION_COMPARISON.md): FLOW-14, two or three
  compatible Works kept distinct. Data: [paired comparison](guides/FLOW14_COMPARISON.json).
- [Reusing reviewed exploration](guides/WORK_CONTEXT_REUSE.md): sharing one reviewed exploration across related
  Works. Data: [qualification](guides/WORK_CONTEXT_REUSE_QUALIFICATION.json).
- [Work pause and human reconsideration](guides/WORK_PAUSE.md): **Pause Work** during execution (FLOW-17).
- [FLOW-04: cancellation, limits and recovery](guides/FLOW04_RECOVERY.md): how the native Work path stops,
  enforces limits and recovers.
- [FLOW-03: settlement, landing and backlog reconciliation](guides/FLOW03_SETTLEMENT.md): which guard and which
  test hold each settlement claim, and what stays uncovered.
- [Review a bounded planning horizon](guides/HORIZON_REVIEW.md): explicit scheduling review or bounded PLAN
  proposal, validated by the Backlog owner.
- [Graph audit: reconciliation, trace and complete differences](guides/GRAPH_SHADOW_RECONCILIATION.md):
  comparing the Backlog Graph owner with the OS graph.
- [Promote reviewed findings into PLAN](guides/RESEARCH_PROMOTION.md): turning research or session findings
  into a bounded owner requirement.
- [Durable state: current scope](guides/DURABLE_STATE.md): owners and sources of truth for tare state, and why
  no outbox or new backend was added.
- [Package audit: local advisory profile](guides/AUDIT_PACKET_LOCAL.md): opt-in audit with independent
  exploration, preparation, reservation and recovery.
- [Test selection: use and limits](guides/TEST_SELECTION.md): SpecGraph test recommendation from a receipt
  (C5 proof) and explicit checks in the TUI.
- [CI feedback and regression scope](guides/CI_FEEDBACK.md): which tests CI runs per change and every night.

## Experiments and flow qualifications

- [Admission and backpressure](guides/ADMISSION_BACKPRESSURE.md): bounded EXP-2 measurement of the native
  exploration batch.
- [EXP-5: bounded evidence-scheduling replay](guides/EVIDENCE_SCHEDULING.md): offline comparison of scheduling
  orders; current scheduling retained.
  - [Expanded retrospective sample, 2026-10-21](guides/EVIDENCE_SCHEDULING_SAMPLE_20261021.md)
  - Data: [CI sample](operations/EVIDENCE_SCHEDULING_CI_SAMPLE_20261021.json) and
    [result](operations/EVIDENCE_SCHEDULING_RESULT_20261021.json).
- [FLOW-09 qualification, 2026-10-17](guides/FLOW09_QUALIFICATION_2026-10-17.md): the static Work recipe as an
  inspectable, compiled structure.
- [FLOW-10 bounded Work DAG qualification, 2026-10-17](guides/FLOW10_QUALIFICATION_2026-10-17.md): prepared
  Works as a versioned dependency DAG.
- [Behavioral profiles from Work evidence and existing audits](guides/FLOW18_BEHAVIORAL_PROFILES.md): the
  optional `/usage` profiles view (FLOW-18). Data:
  [observation](guides/FLOW18_OBSERVATION_2026-10-18.json),
  [existing audits](guides/FLOW18_EXISTING_AUDITS_2026-10-18.json),
  [attribution observation](guides/FLOW18_ATTRIBUTION_OBSERVATION_2026-10-21.json).
- [FLOW-20: one small guidance intervention](guides/FLOW20_GUIDANCE.md): the AB8 experiment on implementer
  guidance. Data: [AB8 result](guides/FLOW20_AB8_2026-10-18.json).
- [FLOW-23 conditional backend decision](guides/FLOW23_BACKEND_DECISION.md): native state preserved; no
  external durable backend adopted.
  - [FLOW-23 native review observation, 2026-10-22](guides/FLOW23_NATIVE_REVIEW_2026-10-22.md)
- [I2I-06: bounded managed-product qualification](guides/PRODUCT_TRANSITION_MODEL.md): finite qualification of
  installation-local product transitions.
- [Saved Sonnet delivery recovery, 2026-10-30](guides/SONNET_MEDIUM_DELIVERY_QUALIFICATION.md): recovery of a
  saved delivery to `DELIVERED`.
  - [Route-selection dogfood, 2026-10-30](guides/SONNET_MEDIUM_DOGFOOD_2026-10-30.json): deterministic
    route-selection propensities for a FLOW-19 routing canary (paused, unqualified).

## Qualification (`qualification/`)

- [One ordinary delivery: product-export checks run early](qualification/NEXT_DEEPSEEK_KIMI_OPUS_2026-11-02.md):
  a `SPECGRAPH-COMPLETION-X5` delivery observation, cited as that Work's evidence.

## History (`history/`)

Dated records of individual deliveries and qualifications.

- [Jev consent qualification, 2026-11-07](history/JEV_CONSENT_QUALIFICATION_2026-11-07.md): supplied measurements,
  multilingual plain-language consent judgment, and asymmetric allow >= 0.8 / deny >= 0.9 thresholds.

- [Jev intent input qualification, 2026-10-27](history/JEV_INTENT_QUALIFICATION_2026-10-27.md): what the
  intent classifier received when an implementation request was classified as investigation.
- [Hosted Jev mode qualification, 2026-10-28](history/JEV_MODE_QUALIFICATION_2026-10-28.md): `jev-1.14.0` for
  command triage and initial routing; bounded comparison and saved-policy compatibility.
- [Scoped conversation checks, 2026-10-27](history/SCOPED_CONVERSATION_CHECKS_2026-10-27.md): an extra,
  automatically discovered verifier that blocked an otherwise passing implementation.
- [Windows MXC internal IPC qualification, 2026-10-27](history/WINDOWS_MXC_INTERNAL_IPC_2026-10-27.md): Python's
  internal `asyncio` socket pair under the deny-network sandbox profile.
- [Useful delivery pilot: X5 capture diagnostics](history/PAIRED_DELIVERY_PILOT_2026-10-28.md): the first
  bounded paired delivery case.
- [X5 incomplete-capture diagnostics: ordinary TUI delivery](history/X5_DIAGNOSTICS_DELIVERY_2026-10-28.md): an
  ordinary demand that reached `DELIVERED`.
- [Linked challenger stop and preserved-candidate recovery](history/LINKED_CHALLENGER_RECOVERY_2026-10-29.md):
  a correction delivered through the public TUI with separate roles.

## Specifications (`specs/`)

- [SPEC-OS-WORK-CONSOLE-001](specs/SPEC-OS-WORK-CONSOLE-001.md): run and settle an explicit Work through the
  tare facade (PROPOSED).
- [SPEC-OS-EXECUTION-SETTLEMENT-001](specs/SPEC-OS-EXECUTION-SETTLEMENT-001.md): settlement, recovery and
  observation of an executed Work (PROPOSED).
- [SPEC-OS-WORKFLOW-IR-COMPILER-001](specs/SPEC-OS-WORKFLOW-IR-COMPILER-001.md): logical workflow IR, compiler,
  physicalization and durable reducer (PROPOSED, with bounded FLOW-09 and FLOW-10 realizations).
- [SPEC-OS-ADAPTIVE-OBSERVATION-001](specs/SPEC-OS-ADAPTIVE-OBSERVATION-001.md): read-only behavioral profile,
  experimental scaffolding and WATCH items (PROPOSED / RESEARCH).
- [SPEC-OS-PROJECT-ADMISSION-001](specs/SPEC-OS-PROJECT-ADMISSION-001.md): project admission contract
  (quarantine, explicit instruction adoption, skills as snapshots).
- [SPEC-OS-SPECGRAPH-HOOKS-001](specs/SPEC-OS-SPECGRAPH-HOOKS-001.md): event-driven project context and doctor
  within a budget (ACCEPTED).
- [SPEC-OS-SPECGRAPH-COVERAGE-001](specs/SPEC-OS-SPECGRAPH-COVERAGE-001.md): operational OS code coverage in
  SpecGraph (ACCEPTED).
- [SPEC-OS-SHIP-001](specs/SPEC-OS-SHIP-001.md): Ship, delegated delivery through remote CI/CD: the generalized
  `tare ci`, preflight, triage, the fix loop and after-merge watching (ACCEPTED, not implemented).

## Decisions (`adr/` and `decisions/`)

| Decision | Status | Scope |
| :--- | :--- | :--- |
| [ADR-049](adr/ADR-049_REPO_FEDERATION_AND_ANTI_DRIFT_GOVERNANCE.md) | Ratified | Repository federation through Git submodules, agent anti-drift rules, backlog separation and node attestation. |
| [ADR-050](adr/ADR-050_APP_DECOMPOSITION_ACL_AND_DEPENDENCY_GOVERNANCE.md) | Ratified | Application decomposition, anti-corruption layers, defensive dependency management, CAS concurrency and forks. |
| [ADR-053](adr/ADR-053_ROUND_TABLE_OPENROUTER_FALLBACK_GOVERNANCE.md) | Ratified | Round Table OpenRouter fallbacks: one-to-one, pinned, with no artificial independence. |
| [ADR-054](adr/ADR-054_ROUND_TABLE_ROSTER_AND_GLM_5_3_GOVERNANCE.md) | Ratified | Official Round Table roster by `bias_domain`, deterministic mediation and GLM 5.3. |
| [ADR-055](adr/ADR-055_ROUND_TABLE_DECISION_INTEGRITY.md) | Ratified | Round Table decision integrity: hash chain through ratification and create-only promotion. |
| [ADR-056](adr/ADR-056_ROUND_TABLE_HUMAN_SOVEREIGN_GOVERNANCE.md) | Ratified | Successor amendment: three independent seats, an advisory table and final human authority. |
| [ADR-057](adr/ADR-057_AGENT_RUNTIME_BOUNDED_CONTEXT_AND_REPOSITORY.md) | Ratified | The agent loop as its own bounded context and repository (`tare.tools.agent-runtime`); the legacy harness stays frozen. |
| [ADR-058](adr/ADR-058_PROPORTIONAL_DELIVERY_EVIDENCE_AND_BEHAVIOR_RELEVANT_COVERAGE.md) | Ratified | Delivery evidence and test coverage proportional to behavior and risk. |
| [ADR-062](adr/ADR-062_UNIVERSAL_FRUGAL_ENGINEERING_DOCTRINE_AND_SOVEREIGN_BYOC.md) | Ratified | Universal frugal engineering doctrine, ambidextrous innovation and compute freedom (BYOC). |
| [ADR-068](adr/ADR-068_UNIVERSAL_BRANCH_STANDARDIZATION_AND_CI_PIPELINES.md) | Superseded by ADR-073 | Universal repository branching, CI/CD pipeline standard and legacy alias deprecation. |
| [ADR-069](adr/ADR-069_SPECGRAPH_OPERATIONAL_CODE_SCOPE.md) | Accepted | Separate SpecGraph's operational code retrieval scope from its governance scope. |
| [ADR-071](adr/ADR-071_NODE_SERVICE_IN_THE_OS.md) | Accepted | The GPU node service lives in tare.tools.os as `node/`, a package of its own. |
| [ADR-072](adr/ADR-072_SHIP_DELEGATED_DELIVERY.md) | Accepted | Ship: delegated delivery through remote CI/CD as a deterministic flow; models only diagnose and fix. |
| [ADR-073](adr/ADR-073_BRANCHES_OF_THE_TARE_TOOLS_REPOSITORIES.md) | Accepted | tare.tools repositories change `main` through short-lived branches and pull requests; no `dev` required. |
| [ADR-074](adr/ADR-074_PRODUCT_RELEASES_FROM_CI.md) | Accepted | CI builds, attests and publishes the product releases after tare.tools CI passes on `main`. |
| [ADR-075](adr/ADR-075_SIGNED_PRODUCT_RELEASES.md) | Accepted | Product releases are signed with a project Ed25519 key; installed copies verify the signature. |
| [ADR-OS-SPECGRAPH-001](decisions/ADR-OS-SPECGRAPH-001.md) | Accepted | Economical composition of project intelligence in the existing session hooks. |

- [Antigravity prompt-hook feasibility, 2026-08-24](decisions/ANTIGRAVITY_PROMPT_HOOK_FEASIBILITY_2026-08-24.json):
  case record holding the Antigravity hook adapter as an unsupported vendor surface.

## Architecture (`architecture/`)

- [TCP-04: classifier, auto-review, confinement and authenticity](architecture/TCP04_ACTION_CONTROL_ARCHITECTURE.md):
  proposal for tare's own action classifier and auto-review, an optional Codex backend, confinement and
  authenticity.
- [TCP-04: exact mediation and controlled execution](architecture/TCP04_ACTION_CONTROL_IMPLEMENTATION.md): opt-in
  mediation of the exact action over the existing OS and Kernel, and its remaining limits.
- [TCP-04 action-control protocol](architecture/TCP04_ACTION_CONTROL_PROTOCOL.json): machine-readable design
  record (`PROPOSED`, no runtime activation).

## Satellites, relay mesh and governance

- [Satellite federation runbook](guides/SATELLITE_FEDERATION_RUNBOOK.md): auditing, bumping and checking
  compatibility of the federated satellites.
- [Satellite repository contents](guides/SATELLITE_REPOSITORY_PREPARATION.md): what a satellite tree holds,
  and where its retired records are listed.
- [Relay mesh (MARM): roles, commands and the round table](guides/RELAY_MESH.md): the multi-agent relay mesh
  and its operating rules.
- [Relay planner playbook](guides/RELAY_PLANNER_PLAYBOOK.md): instructions for the relay planner agent
  (formerly `CODEX.md`).
- [Governance and Round Table lifecycle](GOVERNANCE_AND_ROUND_TABLE_LIFECYCLE.md): authority model, decision
  flow, mediation, integrity seal, ratification, promotion and verification commands.
- [Changelog policy and guard](CHANGELOG_POLICY.md): the append-only `CHANGELOG.md` and the check that enforces
  it.

## Elsewhere in the repository

- [`../AGENTS.md`](../AGENTS.md): instructions for every agent working in this repository.
- [`../scripts/README.md`](../scripts/README.md): catalog of operator, CI and research scripts.
- [`../ontology/domain_ontology.yaml`](../ontology/domain_ontology.yaml): the vocabulary governed by the OS;
  external catalogs keep pointers, not copies.
