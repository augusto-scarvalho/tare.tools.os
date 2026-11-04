# SPEC-OS-PROJECT-ADMISSION-001 — project admission contract

Adopting instructions and authorizing effects are independent decisions. Reading a project, a preview or a skill does not authorize commands, checks, scripts or dependencies.

This contract bounds the intake of the native TUI and of new CLI sessions with `--root`. Explicitly provided legacy host configurations keep their previous contract; they are not an intake path for external material. No admission configuration is loaded automatically from the repository. In new TUI sessions, including advanced mode, verifiers require an explicitly chosen argv: discovering the project's tests or scripts does not enable execution.

Git observations, read-only search and automatic CLI discovery use native executables found in absolute directories of the host PATH. The current folder and relative PATH entries do not implicitly provide executables; on Windows, these routes use `.exe`. Absolute PATH entries and launchers of explicitly saved connections remain operator configuration, distinct from project adoption. The header, the code map and the diff/audit reads disable the project's `core.fsmonitor`; observing a CLI's login uses its launcher's folder on the host.

## PA-01 — Quarantine and permissions

An unknown project starts with `mode=read`, `write_paths=[]` and `project_admission={mode: quarantine, root_sha256: null, nested: false}`. Root and nested remain data, not adopted guidance. In the TUI, editing in quarantine requires an explicit choice of role and paths. In the CLI, choosing `--role implementer` keeps the existing default scope `.`; `--write-path` allows restricting it. Explicitly saved preferences are preserved; opening or closing the panel does not adopt instructions.

## PA-02 — Explicit review and CAS

The panel shows the absolute project, the AGENTS.md path, the SHA-256 of its bytes or its absence, and the reviewed text. Root and nested discovery have separate choices; nested requires an adopted root. Save checks the preview the screen presented, never silently replaces it with a new read, and uses the revision/CAS of the existing preferences outside the project. A stale approval remains inspectable: Refresh presents the new content for a new decision on the same screen. An invalid preview blocks adoption, not saving in quarantine. Saving updates the entire pending policy of a conversation not yet started; it never reconfigures a saved session.

## PA-03 — Transport, resume and children

TUI sessions with admission do not start the legacy background auditor after editing: choosing project, instructions and effects does not authorize another model, budget or instruction scope. Independent review requires an explicit session/flow. Legacy conversations keep their previous policy.

`project_admission={mode, root_sha256, nested}` accompanies the selection, the new configuration and the draft. Switching project loads that project's preferences. An adopted root pins the hash or the absence; save and start/resume check the pin before inference, including follow-ups. Quarantine forces `scoped_instructions=False`. Nested instructions are only discovered with a separate opt-in, through the existing per-session observation and pinning mechanisms, with no recursive scan and no attestation of the whole tree. Child explorers are read-only, without checks, and can reduce, never widen, the parent's instruction consent. Exploration experiments cannot promote root/nested in quarantine.

## PA-04 — Skills as snapshots

Inventory and preview show path, hash and size. Add revalidates the hash and puts JSON with project, path, SHA-256, size and full text into the draft. After Add, mutating the file does not change the snapshot in the draft. Auxiliary files, scripts and dependencies remain data and require the existing permissions. The combined draft limit is 16 KiB UTF-8, including JSON and previous text: an error or excess is refused atomically, with no truncation or LLM summary. An individual skill read is limited to 32 KiB; the inventory examines at most 128 entries per location. Selection does not execute code.

## PA-05 — Legacy and recovery

A missing field in an old configuration/session means the legacy contract, labeled as such, not retroactive consent. Old, explicitly saved preferences keep their permissions; new conversations use quarantine until explicit adoption. Saved sessions preserve configuration and snapshots. A change to an adopted instruction requires restoring the original to resume, or reviewing and starting a new conversation. A new CLI session without config/resume uses explorer/quarantine; `--role implementer` and paths authorize editing separately. Admission flags do not override config/resume.

## PA-06 — Qualification and limits

Qualification requires offline backend/UI fixtures on Windows and Linux, plus a real TUI script: preview/absence, root/nested choices, stale-preview/CAS, invalid preview, new/resume/draft/project switch, child in quarantine, skill snapshot and atomic rejection. Admission backend and UI join QUICK without duplicating the regression phase. Written tests are not execution evidence; CI and real replay must record their own results. This contract does not declare executed checks, Work admission or graph closure.

## Interfaces and scope

`relay.project_admission`: `preview`, `validate`, `configure`, `check`. `configure` compares the reviewed preview; `check` verifies pin/absence, without granting effects. `relay.project_permissions` keeps preferences/CAS and separates role/paths from the instruction policy.

CLI: `--project-admission quarantine|adopted`, `--root-instruction-pin SHA256|absent`, `--nested-instructions`. Adoption requires a reviewed pin. There is no override across multiple repositories.

Out of scope: composite projects/federation, installation, registry/framework/daemon, Kernel/Aider/harness changes, a general security guarantee, and Work/graph closure. Pinning is a point-in-time observation, not a permanent lock against external editors. Commands/checks can access paths outside the mediated effects and gain no authorization from instructions or skills.
