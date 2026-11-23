# Managed skills and inert bundle snapshots

Skills in tare stay separate from both permanent `/guidance` and effect authorization (`SPEC-OS-PROJECT-ADMISSION-001` PA-04). Reading, inspecting, capturing, editing, or activating a skill never runs scripts, fetches remote packages, or expands file/command permissions.

## 1. Inert local bundle snapshots (`tare skills`)

Inspect and capture any explicitly chosen local skill directory containing `SKILL.md`:

```powershell
tare skills inspect C:\path\to\skill
tare skills capture C:\path\to\skill [--name slug] [--include reference.md --include scripts]
tare skills list
tare skills show <bundle> [snapshot] [--file SKILL.md]
tare skills diff <bundle> [old] [new]
```

- Every visited component is opened through pinned, non-following handles (`console_resources._pinned`); symlinks, Windows reparse points/junctions, `..` traversal, network roots, reserved names, and special files are refused.
- Exact captured bytes and a verifiable manifest (`tare.tools/skill-bundle-snapshot/1`) are stored under `<storage>/serve/skills/snapshots/<bundle_id>/<snapshot_id>/`, outside `.agents/skills/` and `skills/`.
- Coverage stays explicit: excluded directories (`.git`, `node_modules`, `dist`, `out`, etc.), skipped/oversized entries, opaque binary files, and `--include` subsets mark `complete_directory=false`, classify static markdown references (`resolved`, `missing`, `outside_scope`, `not_inspected`), and list passive script/package cues as unauthorized `external_bindings`.
- Reading or diffing a snapshot verifies stored bytes against the manifest and its immutable directory ID without rereading the mutable source directory.

## 2. Managed configurable skills (`SKL-01`)

Create a managed skill from a verified inert snapshot, configure typed parameters or an overlay, inspect diffs, and explicitly adopt upstream updates:

```powershell
tare skills catalog [--vendor claude|codex|agy] [--class CLASS] [--all]
tare skills import <slug-or-id> [--vendor claude|codex|agy] [--as skill-id] [--roles r1,r2] [--min-model-class cheap|middling|frontier] [--skip-plan-review] [--all]
tare skills adopt <bundle> [snapshot] [--as skill-id] [--roles r1,r2] [--min-model-class cheap|middling|frontier] [--skip-plan-review]
tare skills managed [--project PATH] [--role ROLE]
tare skills param <skill-id> <name> <value> [--type string|int|float|bool|choice] [--choices a,b] [--project PATH]
tare skills edit <skill-id> (--text "..." | --file PATH | --clear) [--project PATH]
tare skills roles <skill-id> [ROLE ...] [--none | --reset] [--project PATH]
tare skills role-policy [ROLE] [--enable | --disable | --reset] [--project PATH]
tare skills model-class <skill-id> [cheap|middling|frontier] [--clear | --reset] [--project PATH]
tare skills plan-review <skill-id> [--skip | --require | --reset] [--project PATH]
tare skills triggers <skill-id> [PHRASE ...] [--clear | --reset] [--project PATH]
tare skills select "<prompt>" [--role ROLE] [--project PATH] [--max-shortlist 5]
tare skills effective <skill-id> [--project PATH] [--role ROLE] [--text-only]
tare skills update <skill-id> [snapshot] [--from-origin] [--keep-overlay]
tare skills remove <skill-id>
tare skills bindings
```

- Configuration lives in `<storage>/serve/skills/managed.json` (`tare.tools/managed-skills/1`), separate from `<storage>/serve/guidance.json`.
- Parameters use `<name>` placeholders rendered in a single pass (`guidance.fill`). Per-project parameter, role, and overlay overrides never mutate the global base.
- When a newer inert snapshot of the same bundle is captured—or when a managed skill's local `origin_path` changes on disk—`tare skills managed`, `effective`, and `/skills` flag the update/drift with a reviewable diff (`base -> upstream` or `base -> origin (live)`); tare never silently replaces the managed skill.

## 2b. Local vendor catalog discovery (`SKL-01c`)

`tare skills catalog` and `/skills` read local Claude Code (`~/.claude/skills` and installed plugins), Codex (`~/.codex/skills`, `.system`, `~/.agents/skills`), and AGY (`~/.gemini/antigravity-cli/skills`, `~/.gemini/skills`, `builtin/skills`) directories strictly read-only, deduplicating identical `SKILL.md` SHA-256 entries across vendors:

- **Deterministic zero-token classification:**
  - `portable_ready`: portable markdown guidance ready for 1-click import across all models.
  - `local_tool_hybrid`: references local CLIs/scripts (`pxl`, `tune`, `playwright`, etc.) that stay inert unless authorized separately.
  - `needs_sanitization`: exceeds 64 KiB or mixes portable guidance with foreign harness cues.
  - `blocked_capability` *(auto-hidden)*: requires blocked external APIs/credentials (e.g. `OPENAI_API_KEY` image generation).
  - `cli_internal` *(auto-hidden)*: vendor-specific CLI/harness skills (`agy-customizations`, `native_swarm`, `skill-installer`, `openai-docs`, etc.).
- **One-click import & role suggestions:** `tare skills import` (or **Import** in `/skills`) captures the resolved local folder inertly (allowing one explicit top-level linked vendor root while keeping full confinement inside the bundle) and adopts it with suggested roles inside the enabled core roles (never enabling `explorer`/`researcher`/`vision`/`heads_up`).
- **Native CLI loading stays off:** vendor skills imported into tare run uniformly across every runtime without enabling native vendor CLI skill injection.

## 3. Role-scoped availability and per-role policy (`SKL-01b`)

Managed skills use a two-gate intersection across the 9 OS roles (`coordinator`, `implementer`, `planner`, `plan-auditor`, `auditor`, `explorer`, `researcher`, `vision`, `heads_up`):

1. **Per-role policy (`tare skills role-policy`):** disables all managed skills for `explorer`, `researcher`, `vision`, and `heads_up` by default, while allowing `coordinator`, `implementer`, `planner`, `plan-auditor`, and `auditor`. Both global and per-project overrides are supported.
2. **Per-skill allowed roles (`tare skills roles`):** restricts each skill to the exact roles that may receive it (for example, `implementer planner auditor` for implementation/review skills vs `coordinator` only for coordination playbooks).

A role receives a managed skill only when **both** the role policy is enabled **and** the role is listed on that skill.

- **Frozen per-role propagation:** when a new conversation starts with selected `--skill` IDs, tare verifies the backing inert snapshots once and freezes the effective role policy and rendered skills into `native-agent.json` (`managed_skills_pool`).
- **Minimum model class floor (`tare skills model-class`):** skills whose output quality scales strongly with model capability (such as creative pixel art or audio synthesis DSLs) can declare an optional `min_model_class` (`cheap`, `middling`, or `frontier`, global or per-project; also configurable via `/skills` → **Roles** → **Tier**). Frozen implementer skills lift a delegated unit's effective `needs` floor along `model_signals.ORDER` (`cheap < middling < frontier`) so enabled frontier routes are preferred ahead of local/cheap routes—without ever enabling routes switched off in `/routes`, overriding `--local_only` or explicit `--implementer` pins, or granting extra spend permissions.
- **Optional plan-audit opt-out (`tare skills plan-review`):** skills for creative/asset workflows can mark `skip_plan_review` (global or per-project; also configurable via `/skills` → **Roles** → **Audit**) so non-critical, non-code conversation plans skip the independent plan critic. Independent plan review remains mandatory for local planners, critical tasks, and code/infrastructure kinds (`coding`, `web`, `security`, `engineering`, `games`).
- Each role receives only its own frozen slice: the coordinator receives only coordinator-allowed skills (plus a 1-line summary of delegated child-role skills without loading their body), delegated `implementer`, `planner`, `plan-auditor`, `auditor`, and `explorer` workers inherit only their allowed slice from the parent's frozen pool, and disabled roles always receive zero skills.
- Editing `managed.json`, changing role policies, or deleting a skill mid-conversation never mutates running conversations, delegated child workers, `--resume`, or provider continuations.

## 4. Deterministic shortlist and explicit abstention (`SKL-06` Corte A)

Opt-in automatic selection evaluates lightweight metadata (`triggers`, `skill_id`, `skill_name`, `description`, top-level headings) with zero model calls and zero token spend:

- **Hard eligibility gates first:** `role_policy` ∩ `allowed_roles` and backing snapshot integrity are checked before any skill can be selected; ineligible skills never become eligible through lexical scores.
- **Bounded shortlist & 5 explicit states:** returns at most 5 shortlisted candidates and one explicit outcome (`selected`, `no_skill`, `ambiguous`, `unavailable`, or `error` — technical errors are never masked as `no_skill`).
- **Frozen selection receipt:** pass `--auto-skills` on a new `tare agent` session (or enable `[selector] Auto-select skills` in `/skills`). The selection receipt (`tare.tools/skill-selection/1`, including `catalog_sha256` and `query_sha256`) is frozen into `native-agent.json` alongside `managed_skills_pool` and preserved unchanged on `--resume` and provider continuation. Explicit `--skill` / **Activate** selections remain respected.

## 5. Session activation and runtime bindings

- **CLI:** pass `--skill <skill-id>` (repeatable) and/or `--auto-skills` when starting a new `tare agent` session. Both flags are refused on `--resume`, `--config`, or `--continue-provider`.
- **TUI (`/skills`):** inspect project skills (**Add to draft** still appends an atomic hash-checked snapshot up to 16 KiB), capture local folders into inert snapshots (**Capture**), promote snapshots to managed skills (**Manage**), edit overlays (**Edit** / **Save**), configure per-skill roles and global/project role policies (**Roles**), inspect diffs and per-runtime bindings (**View**), apply upstream updates (**Update**), and **Activate / Deactivate** managed skills for new conversations.
- **Frozen per session:** when a new session starts, tare verifies each selected skill's backing inert snapshot and freezes its rendered text, SHA-256, parameters, effective roles, and origin into `native-agent.json`. Later edits, parameter changes, role edits, or upstream updates never reach already-started or resumed sessions.
- **Runtime support (`tare skills bindings`):** portable skill text is frozen into the session system prompt across Claude CLI, Codex CLI (where Codex's own `skills_instructions` are left out by default via `/guidance`), AGY CLI, Direct HTTP API, and Local Gateway. Auxiliary scripts and external bindings remain inert data unless authorized separately through host permissions.
