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
tare skills adopt <bundle> [snapshot] [--as skill-id] [--roles r1,r2]
tare skills managed [--project PATH] [--role ROLE]
tare skills param <skill-id> <name> <value> [--type string|int|float|bool|choice] [--choices a,b] [--project PATH]
tare skills edit <skill-id> (--text "..." | --file PATH | --clear) [--project PATH]
tare skills roles <skill-id> [ROLE ...] [--none | --reset] [--project PATH]
tare skills role-policy [ROLE] [--enable | --disable | --reset] [--project PATH]
tare skills effective <skill-id> [--project PATH] [--role ROLE] [--text-only]
tare skills update <skill-id> [snapshot] [--keep-overlay]
tare skills remove <skill-id>
tare skills bindings
```

- Configuration lives in `<storage>/serve/skills/managed.json` (`tare.tools/managed-skills/1`), separate from `<storage>/serve/guidance.json`.
- Parameters use `<name>` placeholders rendered in a single pass (`guidance.fill`). Per-project parameter, role, and overlay overrides never mutate the global base.
- When a newer inert snapshot of the same bundle is captured, `tare skills managed`, `effective`, and `/skills` flag the update with a reviewable `base -> upstream` diff; tare never silently replaces the managed skill.

## 3. Role-scoped availability and per-role policy (`SKL-01b`)

Managed skills use a two-gate intersection across the 9 OS roles (`coordinator`, `implementer`, `planner`, `plan-auditor`, `auditor`, `explorer`, `researcher`, `vision`, `heads_up`):

1. **Per-role policy (`tare skills role-policy`):** disables all managed skills for `explorer`, `researcher`, `vision`, and `heads_up` by default, while allowing `coordinator`, `implementer`, `planner`, `plan-auditor`, and `auditor`. Both global and per-project overrides are supported.
2. **Per-skill allowed roles (`tare skills roles`):** restricts each skill to the exact roles that may receive it (for example, `implementer planner auditor` for implementation/review skills vs `coordinator` only for coordination playbooks).

A role receives a managed skill only when **both** the role policy is enabled **and** the role is listed on that skill.

- **Frozen per-role propagation:** when a new conversation starts with selected `--skill` IDs, tare verifies the backing inert snapshots once and freezes the effective role policy and rendered skills into `native-agent.json` (`managed_skills_pool`).
- Each role receives only its own frozen slice: the coordinator receives only coordinator-allowed skills (plus a 1-line summary of delegated child-role skills without loading their body), delegated `implementer`, `planner`, `plan-auditor`, `auditor`, and `explorer` workers inherit only their allowed slice from the parent's frozen pool, and disabled roles always receive zero skills.
- Editing `managed.json`, changing role policies, or deleting a skill mid-conversation never mutates running conversations, delegated child workers, `--resume`, or provider continuations.

## 4. Session activation and runtime bindings

- **CLI:** pass `--skill <skill-id>` (repeatable) when starting a new `tare agent` session. `--skill` is refused on `--resume`, `--config`, or `--continue-provider`.
- **TUI (`/skills`):** inspect project skills (**Add to draft** still appends an atomic hash-checked snapshot up to 16 KiB), capture local folders into inert snapshots (**Capture**), promote snapshots to managed skills (**Manage**), edit overlays (**Edit** / **Save**), configure per-skill roles and global/project role policies (**Roles**), inspect diffs and per-runtime bindings (**View**), apply upstream updates (**Update**), and **Activate / Deactivate** managed skills for new conversations.
- **Frozen per session:** when a new session starts, tare verifies each selected skill's backing inert snapshot and freezes its rendered text, SHA-256, parameters, effective roles, and origin into `native-agent.json`. Later edits, parameter changes, role edits, or upstream updates never reach already-started or resumed sessions.
- **Runtime support (`tare skills bindings`):** portable skill text is frozen into the session system prompt across Claude CLI, Codex CLI (where Codex's own `skills_instructions` are left out by default via `/guidance`), AGY CLI, Direct HTTP API, and Local Gateway. Auxiliary scripts and external bindings remain inert data unless authorized separately through host permissions.
