# TUI Daily Use — Quickstart

English quickstart for everyday work in the tare terminal UI (TUI). This replaces the
daily-use parts of the outdated `FLOWS_TUI_USER_GUIDE.md`. Graphs and flows are **optional
helpers**, not prerequisites. Individual Work execution supports explicitly selected
project graphs; sequences still use the control graph.

## 1. Open a project

Run `tare` from your project folder, or pass another folder:

```powershell
cd C:/path/to/your-project
tare
tare "C:/path/to/another project"
```

Windows command setup is described in [product installation](PRODUCT_INSTALLATION.md).
The command uses the managed release and its automatic updates; reopen the TUI
after an update. `tare console --root PATH` and the desktop shortcut still work.
History stays outside projects in the installation's configured storage.

For a saved conversation with a finite allowance, an operator can explicitly raise
its totals when opening the TUI:
`tare console --root PATH --resume-coordinator-seconds 3600 --resume-coordinator-calls 90`.
These apply only when sending a follow-up to a saved assistant conversation. The
call total is the OS pool shared with workers; the runtime's effective call ceiling
follows the explicit grant while its original budget identity and journal stay fixed. Used calls and active time remain
charged, and repeating the same total does not replenish either allowance. The
agent cannot grant itself more budget. The equivalent public `agent` options are
`--active-time-budget` and `--active-call-budget`.

Completed waits for your reply are excluded from a finite conversation deadline.
An unfinished or uncertain invocation does not automatically receive that time
back. After an interruption, inspect saved effects before retrying: an interrupted
implementation is not silently replayed or accepted.

After a cancelled read-only audit, a new human request can start one bounded
review when the previous process has confirmed local cleanup. The candidate and
earlier usage remain recorded, including unknown remote charges. The new review
uses the current reviewer choice; accepted or adverse verdicts stay final.

For a saved delivery, `/recheck` repeats the candidate's checks and independent audit
without calling the coordinator or implementer. Use `/retry_delivery` when an audit
finding requires another implementation attempt, and `/repair_plan` when the saved
plan needs a scope correction. These commands retain the candidate, previous findings,
history and charged usage. They still require the existing permissions and remaining
budget; a failed check or audit keeps the delivery blocked.

The symbol beside the status animates while a model or tool is working, including
silent waits before the first token. The terminal tab follows the same activity:
`⠋ - tare - my-project`. `○` means ready, `?` waits for your input, `✓` marks a
finished turn, `■` means stopped, and `!` needs attention. A finished turn does not
by itself mean a Work is verified or settled. Cancellation keeps animating until
the runner stops. Closing tare restores the shell title where supported.

### Choose connections for one launch

Append a switch to `tare` or `tare PATH`. It lasts for this TUI and its delegated
work only. Closing it restores the usual routing; saved machine settings, model
preferences and conversation prompts are not replaced by these switches.

| Switch | Effect |
| --- | --- |
| `--subscription_only` | Use subscriptions: Codex, Claude and AGY CLIs and the OpenCode subscription. Hosted Jev stays available. |
| `--local_only` | Use local models; replace hosted Jev with the local judge. Search APIs stay available. |
| `--api_only` | Use inference APIs. Hosted Jev and search APIs stay available. |
| `--pro_planner` | Use OpenAI Sol PRO, then Astra PRO if available, for the separate planner only, with at least high reasoning. Requires an API key. |
| `--prefer_api` | Prefer available inference APIs for planner and auditors; fall back to other allowed connections. |
| `--prefer_subscription` | Prefer available subscriptions (CLIs, the OpenCode subscription) for planner and auditors; fall back to other allowed connections. |
| `--prefer_local` | Keep coordinator, explorer, implementer and researcher on local inference; use qualified external planner and auditors. Hosted Jev stays available. |
| `--disable_judge` | Disable hosted Jev and the local judge. A protected command then asks you first, except in **Edit + system commands** (YOLO), where it runs with no model review and no question. The sandbox, the fixed checks and final audits still apply, and so does native Codex Auto-review when the project's command review is Codex or both. `/permissions` shows which of these applies. |

```powershell
tare --subscription_only
tare "C:/path/to/your-project" --prefer_local
tare --prefer_subscription --pro_planner
tare --local_only --disable_judge
```

Choose at most one `*_only` mode and one `--prefer_*` mode. Conflicting combinations
are rejected before opening the TUI. PRO cannot be combined with local-only or
subscription-only. Hyphen spellings such as `--local-only` also work.
The TUI displays the active launch switches above the conversation. Existing
exclusions, budgets, audit independence and GPU leases still apply. If no allowed
connection is available, tare reports the block rather than starting another kind
of inference. A saved chat pinned to an excluded connection must be continued on an
allowed connection; its original history and cache prefix stay intact.

The local judge is used only while already loaded and idle; tare never loads it, waits for it
or loads another model over an active GPU lease. When a protected command needs it and it is
not available, no model can review that command, so tare asks you first. In **Edit + system
commands** (YOLO) it asks once per turn: **Run commands without review this turn?** shows the
command that raised it, and **Run without review this turn** runs that command and the rest of
the turn's commands the judge cannot review, without asking again. Each of them is marked as
not reviewed in its activity row, and the next turn asks again.
**Deny** refuses only that command; the next one asks again. In the other modes tare asks
about each command. `/permissions` says which applies. `--disable_judge` is different: there
YOLO runs protected commands without asking, because you turned the judges off. Bulk
hosted-Jev optimizations remain off in local-only mode; their existing
deterministic/explorer fallback handles the work.

**Run this command?** waits for your answer: it does not expire, and while it is open the turn's
time stops (the implementer's and the conversation's) and so does the elapsed time shown. Only your
answer or cancelling the turn closes it. After **Allow this command**, which covers that one command only (tare may ask
again for the next one), tare checks that the command, its folder, inputs and context are still exactly what you were shown and runs it within a fresh short
review window (60 seconds by default); anything changed, and it does not run.

From a source install, run `tare PATH` in the activated environment; its history
goes to `.reconciliation` next to the clone unless `TARE_STORAGE_ROOT` is set.

For roadmap/backlog investigations, see [project-data reads](PROJECT_DATA_READS.md).
Use `/backlog` to select the project's graph explicitly and `/new` for the updated
JSON read tools. Existing conversations keep their original tool contract.

## Optional SpecGraph lookup

In workspace settings (F2 on small terminals), **SpecGraph advisory lookup** is
off by default. Enable it before starting a new conversation. The `context` tool
queries existing published snapshots only; it does not index, run another LLM,
or enable the edit gate. Saved conversations retain their capabilities, model
parameters and limits independently of the default for new conversations; no
settings change is needed to resume. The CLI still rejects explicit startup
flags such as `--specgraph-context` or `--no-context` with `--resume`.

Publish an index explicitly with the installed SpecGraph CLI:

```powershell
Set-Location -LiteralPath "C:\path\to\your-project"
python -m specgraph.cli index
tare agent --root "C:\path\to\your-project" --specgraph-context --instruction "Find callers of parse"
```

The legacy CLI default and `--no-context` retain their behavior. The new
`--specgraph-context` flag enables consultative lookup independently of editing.
An index hit is snapshot metadata, not proof of current source contents or full
coverage; stale, absent or partial indexes require ordinary `read`/`search`
fallback. Read current sources before editing or asserting evidence. Queries
are bounded to the selected repository/index and their response limits; they do
not expand permissions, admit a Work, update its graph, or prove its criteria.
Normal conversations do not acquire a Work binding through lookup. No sustained
savings are claimed; the small paired TUI qualification remains separate.

## 2. Select another project

Use `/project` (or F5) to open the project/workspace selector and pick another project
folder. "Reload workspace" changes the folder and starts a separate conversation for it;
saved conversations remain available for their own folder. You can also reload with Ctrl+R.

## 3. Execution permission

Ask for the change in the conversation. Before implementing, tare shows the whole
plan in an **Implement this plan?** dialog (steps, files, checks and what the
planner left uncertain) with **Implement this plan**, **Change the plan** and
**Deny**; when the files still need edit access it also offers **Implement, always
allow edits here**. **Change the plan** implements nothing: type what should change
in the line above the buttons, and the revised plan is shown again before anything
is implemented.

With **Go straight on** in `/permissions` (for one project under **Before implementing**,
or for every project under **My default, every project**), tare does not show
the plan. When the saved plan needs edit access, it offers **Allow for this task**,
**Always allow in this project**, and **Deny**, showing the project, files and checks;
remembered edit access, including an existing commands/yolo policy, needs no extra
edit confirmation.

Either way, an approval continues that plan in the same chat and covers its
corrections; a new plan or other files ask again. The coordinator stays read-only.
Waiting for your decision makes no model calls and does not spend the turn's time: its time limit stops
until you answer. Plan-only requests still do not execute.
Runs without the TUI (`tare serve`, headless runs) never show the plan.

For advanced defaults, open `/permissions`:

- **My default, every project** — **Show me the plan and ask** (until you choose) or **Go straight on**;
  a new project follows it.
- **Before implementing** — this project's choice: **Use my default** (until you choose), **Show me the plan
  and ask** or **Go straight on**.
- **Read only** — no file edits or terminal.
- **Edit files** — write within the chosen scope; terminal disabled.
- **Edit + system commands** — file edits plus terminal commands.

Defaults are saved separately for each project. A coordinator's own tools, model,
instructions and history remain fixed; worker authorization is checked separately
when executing a plan. Explicit `/session` conversations retain their original
capabilities. Commands can access the host and network: file edit scope alone is
not a system sandbox.

## 4. Send a normal coding request

Type your request and press **Enter** to send. **Shift+Enter** or **Ctrl+J** inserts
a newline; **Ctrl+Enter** also sends. Completion menus use Enter to accept the
selected command or file first. The coordinator delegates planning to the configured
planner routes, then implementation, host verification and independent review.
The tool calls of each delegated session appear in the conversation as collapsed
rows named by their stage, for example `Implement · Edit relay/x.py · Done` or
`Explore · Read a.py · Done`, with **Full output** for each; the coordinator's own
rows have no prefix.

Text pasted into the composer remains an editable draft. The field grows up to
eight rows. Drafts of at least 1 KiB or more than eight lines show a byte/line
count; drafts above the 16 KiB direct-prompt limit show a warning. Shorten the
draft or attach supporting material before sending. Pasting does not submit
the message or start inference.

## 5. Slash completion

Typing `/` opens the completion list. Use the arrow keys to choose, Tab or Enter to
complete the command, and Esc to close the list. Accepted commands include `/new`,
`/resume`, `/model`, `/diff`, `/sessions`, `/project`, `/permissions`, `/backlog`,
`/terminal`, `/attach`, `/status`, and `/help`.

The command palette also includes **Artifacts and feedback**, **Tasks and agents**,
**Available tools**, and **Project skills**. Their slash commands are `/artifacts`,
`/tasks`, `/tools`, and `/skills`.

## 6. Corrections

If the result is wrong, just send another ordinary message describing the correction —
the session is persistent, so context carries over. During execution, Send becomes Queue;
corrections are saved for a subsequent model round. A delivery receipt is not proof that
the model followed the correction. For a clean start use `/new`; for a
different model preference for the next conversation use `/model`.

### Comment on a response or diff

Open `/artifacts` to choose a retained response, or **F3 → Comment** for the
selected recorded file diff. Select a passage in the preview, write a comment,
and choose **Add to draft**. A short response can be quoted whole without a
selection. The existing draft is preserved; review it and send normally.

Feedback carries a hash of the captured text and, for diffs, its session and edit
reference. This identifies the reviewed version, not the current file or a full
untruncated artifact. Excerpts are limited to 2 KiB and comments to 4 KiB. Adding
feedback makes no model call. Changing projects/conversations, a running task,
or a failed draft save prevents adding it to the wrong destination. Closing the
panel before **Add to draft** discards the unsubmitted comment.

### Heads-up notes

After a reply that follows delivered or attempted work (an implementation, an edit or a failed check), and now and
then after other replies, tare adds a short heads-up note: one thing the reply left unclear.
**Ask the agent** only puts the note in the composer as a question; it keeps any draft
you had, and nothing is sent until you edit it and press **Enter**. A note counts as
used only when you send it and the agent's reply confirms it after checking (a reply
that just agrees, with no step run, does not count). You may rewrite the
note in your own words: keep the first line (`tare's heads-up after your last reply:`)
and tare still knows which note the message is about, and keeps your version beside
the original. **Useful** and **Not
useful** record whether the note helped (the chosen button shows a check, and you can
change it). These records stay on this machine, the log holds no note text, and they
are used to tune when notes are written. A note shows only while its conversation is
open; `/heads-up` puts the last notes in the composer from the keyboard, and the
Settings checkbox turns notes off for the machine. `/usage` shows what the heads-up
writer used in the conversation (calls, tokens, time and known cost), and quitting the
console stops a writer call still in flight.

When a reply needed a warning and tare showed none, type `/missed` followed by what it
should have told you (the words are optional). It records that for the last finished
reply, even while a turn is running, and sends nothing to the agent. The record stays on
this machine (the log holds no text) and is used with the note labels to tune when and
what notes are written.

### Limits and the no-progress pause

New conversations start with **Unlimited calls and time**. They still pause after 6 replies in a
row in which the model got nothing new from its tools. The status reads "Paused: no progress",
nothing is lost, and sending any message continues the conversation with a new count.

### Inspect usage, tasks, tools and project skills

- `/usage` opens on its first row, **Provider quota and credit**: what each
  subscription and API account has left, with the time of the reading. The
  saved reading shows at once and the panel asks every provider again in the
  background, without a model call (see
  [Provider quota and credit](PROVIDER_QUOTA.md)). The rows after it show
  recorded conversation calls, tokens, cache reads/writes, known costs by
  currency and configured limits; missing counts and unpriced calls remain
  explicit. Reopen
  the panel after a running task finishes to read its saved totals.
- `/tasks` shows the current execution and retained tool activity grouped by
  child session/stage. It refreshes while open and can cancel the **whole current
  execution**. A successful tool operation does not mean that a task was accepted.
  This is the current console's observed history, not a global background-agent
  registry. Profile and recorded call/token/cache totals appear when available;
  the subscription quota is in `/usage`.
- `/tools` shows the saved conversation's effective tools and scope, or the
  pending scope for a new conversation, plus project instructions. Use
  `/permissions` to change defaults for future conversations.
- `/skills` previews immediate `skills/<name>/SKILL.md` and
  `.agents/skills/<name>/SKILL.md` files. **Add to draft** explicitly asks the agent
  to read and use that skill when you send. It does not execute the skill or
  expand permissions. Each skill is limited to 32 KiB; only the first 128 directory
  entries in each location are inspected and the returned inventory is sorted.
  Linked paths, junctions, invalid UTF-8 and changed previews are rejected.
  The draft carries a path reference: the agent reads current contents at execution.

External MCP server management, global/user skill catalogs, conversation branching,
and independent steering of flow children remain follow-up work.

## 7. Cancellation

Press **Ctrl+X** to cancel a running task. **Ctrl+Q** quits the TUI.

After cancellation, type a correction and press **Enter** in the same conversation.
The new message takes precedence over actions that had not started. Completed changes
and prior context remain saved; cancellation does not undo them or renew budgets.
An external action with an unresolved outcome must be reconciled before continuing.

## 8. Sessions and resume

Open the saved-conversation list with **F1** or `/sessions`, then open a conversation.
Send a message to continue or correct it. **Resume** continues the saved turn and queued
messages without adding a new instruction.
`/resume` also starts a resume from the composer. Conversations are stored per project
folder, so a saved conversation remains available when you switch back to its folder.

## 9. Review changes with /diff

Use `/diff` (or F3) to inspect confirmed file-tool changes, their diffs and recorded
checks. The panel also supports explicit undo and rerunning recorded checks. Shell-made
edits are not automatically file-tool snapshots; inspect those with Git or file tools.

## 10. See what the agents read and edit (F8)

F8, the **Files** button or `/files` opens a compact pane on the right: the files the
agents read and edited this turn (**t** switches to the whole session), whichever
model or vendor runs each role. Each row starts with what happened (`r` read, `e`
edited, `p` edited by a delegated session in its candidate tree, `x` deleted, `!`
refused or failed, `⟳` in progress) and ends with who did it when it was a delegated
session (`impl`, `expl`, `audit`). A refused row shows its reason when selected.
An edited file also shows its lines added and removed, and a delegated edit turns
from `p` to `e` once it reaches the project. Below the list, the project tree marks
what Git sees (`M` modified, `A` added, `D` deleted, `U` untracked, `!` conflict,
`•` a folder holding a change) and opens at each file an agent edits; **c** shows
only the changed files. Git is read every few seconds and only while the pane
shows, so changes made in an editor or a terminal appear too. The line under the
title counts the changes and, when there are, the commits ahead (↑) and behind (↓)
the branch's upstream, a rebase or merge in progress and its conflicts. On a file, **Enter**
opens it (a row of the list also opens it in the tree), **d** shows what changed
since the last commit, **h** lists the commits that changed it (following renames;
**Enter** on one shows what it changed in the file), **a** attaches it to your next message as an `@` reference
(the only way the pane reaches a model), and **y** copies its path.
Below 100 columns the pane takes the whole screen, like F1 and F2; from 140
columns it stays beside them. F8 or Esc returns to the message. The pane only
reads what the conversation already shows: it calls no model, and it opens closed
on a new machine and then as you left it.

## Other useful keys

- F4 / `/terminal` — inspect session processes. `/attach` adds file or image context.
- F6 — next step; Ctrl+End — jump to the latest output.
- F2 — workspace settings on small terminals.
- F8 / `/files` — the files the agents read and edit (section 10).

## Optional flows (not required for daily use)

`/session TASK`, `/explore QUESTION` (read-only), `/plan DEMAND`, and `/research QUESTION
URL` present a suggestion that you start explicitly. `/work WORK-ID` proposes implementation of a graph Work. These are optional; ordinary messages work without any graph or flow.

### Execute a Work in another project

1. Open the project with F5 and bind its graph through `/backlog`.
2. Use `/plan DEMAND`, review the proposal and Apply, or select an existing Work.
3. Use `/work WORK-ID` and start the suggested flow. For an unprepared proposal,
   open Setup → **Create source from this Work**, enter acceptance criteria and the
   verification command (one argument per line), preview and confirm preparation.
4. **Start** runs plan review, implementation in an isolated Git worktree, verification,
   admission and landing. **Review** inspects the retained evidence.
5. **Resume** continues the selected Work using its saved graph, project, model and
   limits. For an interrupted model response it authorizes another call only when
   local transport cleanup is confirmed and the original budget remains. The lost
   response's remote completion and usage stay unknown; tools are not replayed.

The project must have a Git baseline. Graph identity separates Work locks, native
coordinator state, trains and receipts even when two projects use the same Work ID.
Landing updates the selected graph. Applying a plan alone does not admit execution.

In Work mode, **Pause Work** finishes the current agent round before stopping.
Wait for “Work paused” before closing the TUI. **Inspect controls** reads the saved
pause after reopening. **Resume** explicitly continues the same attempt and budget.
To guide an unfinished agent, inspect controls while paused and **Save note**.
A declined implementation decision can be reopened with **Reconsider decision**;
it still needs a new answer. See [Work pause](WORK_PAUSE.md) for limits and recovery.
