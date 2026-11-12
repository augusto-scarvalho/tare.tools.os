# Project permissions in the TUI

For normal coding requests, ask in the conversation. Before anything is
implemented, tare shows the planner's whole plan in an **Implement this plan?**
dialog: its summary, steps, files, checks, what it must achieve and what the
planner left uncertain, scrollable. It offers **Implement this plan**, **Change the plan** and
**Deny**; when the plan's files still need edit access, it also offers
**Implement, always allow edits here**, so one dialog covers both questions. **Change the plan**
implements nothing: the coordinator is told you want changes, with what you
typed in the line above the buttons (or it asks you what to change), and the
revised plan is shown again before anything is implemented. An approval is remembered for that
plan: corrections of the same task under the same plan do not ask again, and a new plan asks again.

**Before implementing** in `/permissions` chooses this. **Show me the plan and
ask** shows every new plan, even in a project that already allows
edits. **Go straight on** implements without showing the plan, as tare did
before: if the plan needs edit access the project does not allow yet, an inline
question offers **Allow for this task**, **Always allow in this project** or
**Deny**, showing the affected files and checks.

The choice has two levels in the same panel. **My default, every project** is
yours, set once for every project; until you set it, it is **Show me the plan and
ask**. **Before implementing** is the selected project's: **Use my default** (it
names what your default is now, and it is what a project shows until you choose
for it), or one of the two choices above, which then holds for that project
whatever your default says. A new project follows your default. Both apply at
once, also to open conversations.

Either way, approval continues the same plan and conversation and covers that
task's corrections; a new plan or other files ask again. The worker waits without inference while you decide; a
denial starts no implementer and is not requested again in that user turn. The wait does not spend the turn's
time: its time limit stops while the dialog is open and resumes, with the same time left, when you answer.

Without an interactive frontend (`tare serve`, headless and scripted runs)
nobody can be shown the plan, so nothing changes there: a plan whose files are
already allowed is implemented, and one that needs edit access is refused.

Remembering grants project file changes and checks; it does not enable arbitrary
commands, disable sandbox policy or adopt project instructions. Existing edit or
commands/yolo policy avoids another edit prompt. The coordinator's prompt, tools,
configuration and history stay unchanged. Explicit native `/session` tools keep
their saved scope.

Open `/permissions`, **Settings → Project permissions**, or **Project permissions**
in the command palette. The panel shows the selected project's saved defaults,
the current conversation's actual permissions, and its recorded project instructions.

## Choose defaults for new conversations

| Mode | Allowed tools |
| --- | --- |
| Read only | Read files, list folders and search project text |
| Edit files | Reading plus edits within the selected paths |
| Edit + system commands | Scoped file tools plus the terminal tools |

**Before implementing** and **My default, every project** (above) are saved with
the same **Save** but are read each time a plan is about to be implemented, so
they also reach open conversations. Your default is kept beside the project
defaults, in `<storage>/console/project-permissions/defaults.json`.

Enter one project-relative file or directory per line. Use `.` for the whole
project; for example, `src` and `docs` permit edits inside those directories.
Globs, parent traversal and `.git` paths are refused. Reading remains available
across the project. Unknown projects initially use **Read only**, no edit paths,
and quarantined project instructions. Explicitly saved permissions remain available.

Click **Save for new conversations** to persist a change. Editing the form or
opening the panel does not save anything. Closing after a successful save also
updates the current conversation's controls if execution has not started.
Starting a new conversation, opening another project, or reopening the TUI loads
that project's defaults. Reopening a saved draft retains its selected settings. The panel labels those
pending settings separately from the defaults, so its next message does not
appear to have permissions it will not use.

Defaults are stored outside the project, separately for each project and control
root. Stale saves are refused until Refresh. An unreadable preferences file is
preserved and prevents a new direct session from starting; existing saved sessions
can still resume with their original settings.

## Saved conversations retain their permissions

The **Effective permissions** section reads the saved session's role, admitted
tools, edit paths, terminal setting and named checks. Changing project defaults
does not modify that session. Resume uses its saved configuration, without
repeating new-session scope arguments.

**Action review** can be set to **Automatic: Codex or Tare + Jev** when protected
commands are enabled. Codex CLI execution uses native Auto-review and its sandbox;
other connections use Jev and Tare MXC, with an additional Tare review only when
Jev requests one. Configure the Jev key in Setup, Helpers. This is action approval;
the independent Work auditor still checks the delivery separately.

For delegated implementation and named checks, the current saved project review
policy applies even in an older conversation. The coordinator's saved prompt,
tools, history and edit scope stay unchanged. Direct saved agent sessions and
explicit Work runs retain their own policy. Automatic routing currently requires
Windows; a missing service or sandbox refuses the action.

System commands and configured checks run programs. They can access files outside
the mediated edit paths and use the network; these settings are **not an operating
system sandbox**. Configured checks remain a separate new-session field. Flows
retain their own scopes and do not take permissions from these direct-conversation
defaults.

## Project instructions

The panel shows the absolute project and root `AGENTS.md` path, SHA-256 (or
observed absence), reviewed text, and the saved conversation snapshot when present.
Opening or closing it adopts nothing. Instruction adoption and edit/command
permissions are separate choices; editing in quarantine is allowed with explicit
write scope.

Choose root adoption explicitly; nested discovery is a separate checkbox, off by
default. Save revalidates the preview shown on screen and the preferences revision.
If the file changed, Refresh and review it again in the same panel before saving.
An invalid preview disables adoption but still allows saving while ignoring
instructions.

The checkbox trusts the repository's `AGENTS.md`, not one copy of it. In a Git
checkout the trust covers every worktree of that repository (they share its common
Git directory), and a session loads the root `AGENTS.md` as committed: a merge or
pull that changes it needs no new review. An uncommitted change to it (modified,
staged, deleted or untracked, as an agent's edit would be) is not loaded; the
committed version is, and a new conversation's welcome says so. Saving the panel
with the box checked reviews that edit and loads it while it stays as reviewed.
Agents commit and merge here too, so a committed change
is no review: while the last commit that changed `AGENTS.md` is under seven days old,
every new conversation's welcome names it (commit, subject, lines added and removed).
Unchecking it in any worktree untrusts the repository in all of them. Outside Git
the reviewed bytes are pinned as before (an absent file is pinned as absent), so a
change needs a new review. Policies saved as `adopted` keep their pin until saved again.

New conversations remain quarantined until explicit adoption, including when
older saved preferences supply edit permissions. Legacy configurations/sessions
without an admission field are labeled legacy and retain their original contract.
Defaults saved from a resumed conversation never modify that session. New pending
conversations and drafts transport the entire selected admission policy; switching
projects loads that project's defaults.

With nested discovery explicitly enabled, mediated file access observes ancestor
`AGENTS.md` files only along the accessed subtree. Deeper rules apply there; no
recursive scan or whole-tree security attestation is performed. Observations and
absences are pinned in saved sessions. Changed adopted root or nested observations
can block resume: restore the original files or review and start a new conversation.
Refresh does not rewrite a saved session's instruction snapshots. Child explorers
remain read-only and cannot expand the parent's instruction consent in quarantine.

Root preview is limited to 16 KiB. Nested discovery is bounded by the existing
16 KiB/file, 24,000 encoded bytes, 256 locations and 64 nested folders/path limits.
Invalid, unreadable, linked or oversized sources are refused, not silently adopted.
Commands/checks can access arbitrary paths outside mediated discovery and require
separate authorization; instructions do not grant tools or override the operator.

## Explicit skill snapshots

Open Skills, review the path, SHA-256, byte size and full text, then Add to draft.
Add revalidates the hash. The draft contains JSON with the complete selected text,
project, path, hash and size, not an instruction to reread mutable `SKILL.md` later.
Changing the file after Add does not change that draft snapshot. Auxiliary files,
scripts and dependencies remain data and need the existing permissions.

Skill reads allow up to 32 KiB and inspect at most 128 entries per location; the
combined draft, including JSON overhead, is limited to **16 KiB UTF-8**. Add refuses
errors or oversize atomically, preserving the prior draft, without truncation or
LLM summarization. A skill never authorizes commands or checks.

## CLI equivalent

Command review, classifier and isolation selections also apply to newly started
managed Work runs. The Work's declared target paths define its write scope;
project defaults do not broaden that descriptor. Saved conversations and Work
runs keep their original policy on resume. Protected Work currently supports
one local Windows execution repository and named noninteractive checks.

Specify writable paths when starting an implementer session:

```bash
tare agent --root /path/to/project --role implementer \
  --write-path src --write-path docs --instruction "Update the API documentation"
```

`--write-path` is repeatable and only applies to a new implementer session. It
cannot be combined with `--config` or repeated on resume. The CLI does not load
TUI project defaults implicitly; use explicit CLI options or a configuration.
CLI sessions default to read-only. Explicitly choosing `--role implementer`
retains the CLI's existing whole-project `.` write scope when no `--write-path`
is supplied; use the path options to narrow it. Instruction adoption remains separate.

Use the same controls in the installed TUI or, from a source install, through
`tare` in the activated environment. The installed version follows the
normal product release and update process.
