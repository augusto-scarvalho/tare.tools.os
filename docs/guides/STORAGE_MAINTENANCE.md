# Storage and cleanup

Open `/storage` in the TUI, including while a task is running. The command palette
also offers **Storage and cleanup**. No model call is needed.

1. Scan the operational folder shown at the top. The default is the console's
   configured storage folder. The product installation is included separately.
2. Review consumption by origin and the explanation beside every measured path.
3. Select candidates, check **Delete the selected items**, then choose
   **Clean selected**. Nothing is selected automatically.
4. Scan again to refresh usage. Changing the folder or selection clears confirmation.

The view includes sessions, conversations, managed Work, experiments, evidence,
builds, temporary files and installed versions. On Windows it also observes the
standard uv cache, system temporary folder and Docker virtual disks; their cleanup
remains with their owning tools.

## What can be removed

- **Old product installations:** the current installation and its recorded previous
  version are always kept. An older installation must have matching bundle/ready
  identities and unchanged installed package records, no unrecognized user files,
  no Git tree or links, and no observed process using its directory.
- **Duplicate build wheels:** only `t/<build>/wheels-2/*.whl` matching a completed
  reproducible build manifest and the retained `wheels-1` copy. Sources, manifests,
  logs and the first wheel remain. Builds inside Git workspaces or in use are kept.

## tare's own leftovers

tare deletes its scratch when the work that made it ends. What an interrupted run
leaves (a killed process, a closed laptop, a file another process still held) is
swept when the TUI opens and when `tare serve` starts: in the background, without delaying startup. It looks
for at most a minute; a removal it has begun finishes (Git's own removal of a
registered tree has two minutes). Nothing to configure. An item goes only
when no record of tare can still need it **and** its newest file is more than a
day old (a usage marker goes as soon as its process is dead, whatever its age);
anything uncertain is kept. Only finished or interrupted work is swept: while a
conversation's turn is still in progress (it can wait days for an answer with
nothing open in its tree), every candidate tree, command folder and check
scratch last written after that turn began is kept. A turn counts as in progress
while its record has no end and its process, on this machine, is not provably
gone.

| Where | Made by | Deleted at a normal end by | Still needed after its process is gone by |
|---|---|---|---|
| `<storage>/wt/<8 hex>`, `wt/.<8 hex>-pytest` | `run_unit` and the conversation's `check` (a candidate tree; pytest's root beside it) | `git worktree remove` and `worker_wait.discard` | a waiting worker (its conversation's `conversation-tasks.json` names the tree); a tree Git still registers (removed the way `run_unit` does, with `git worktree remove`, then deleted); a pytest root while its tree is there |
| `<storage>/agent-sessions/…/command-tmp` (a session's, its checks', a unit's, a worker's) | the command binding (`command_protection`) | the binding's close | a waiting worker (named by its conversation's record); a session waiting for the user's folder answer |
| temporary folder: `tare-audit-`, `tare-blind-images-`, `tare-bwrap-`, `tare-cli-ack-`, `tare-cli-profile-`, `tare-command-`, `tare-inference-`, `tare-native-review-`, `tare-pr-`, `tare-reading-`, `tare-round-table-seat-`, `tare-round-table-stream-`, `tare-serve-cli-`, `tare-serve-images-`, `tare-snapshot-`, `tare-test-`, `tare-vision-` + 8 random characters | relay and the Kernel (`TemporaryDirectory`, `mkdtemp`) | the same code, on leaving | nobody (a `tare-pr-` tree Git still registers is removed through Git first) |
| `~/.tare/tmp/<8 random>` (Windows), `tc-<8 random>` in the temporary folder (elsewhere) | a check (`work_units.managed_check`) | the check, when its process cleanup is verified | a process of the check that may still run (an open file or a working folder keeps it) |
| `<product>/releases/.use.<release>.<pid>` | each launch, under `usage.lock` | nobody (`prune` drops dead ones, not for the current and previous release) | its process while it runs, or while that cannot be told; swept for every release, under `usage.lock` |
| `<product>/download-*`, `<product>/catalog-trial-*` | an update, under `install.lock` | the update, on leaving | the update holding `install.lock`; swept only while the sweep holds it |

Never touched: saved conversations and sessions, patches, the shadow repository,
release folders, anything in use, anything under a link or junction, a folder that
cannot be listed (some are created by sandbox processes: shown as "could not be
inspected"), and other `tare-*` names. Each item is renamed before it is deleted, which
Windows refuses while another process holds a file in it: such an item is kept and
recorded, never retried in a loop or forced. Linux allows that rename, so there the
day of age and the process list (working folder, program, arguments) are the only
signs of use: a process that kept a file open in an item for a day without writing
loses the folder's name, not its open file. Paths over 260 characters are removed
through the long-path form. `/storage` lists what remains with the reason it is
kept, and the last sweep's totals; each sweep that found something leaves
`<storage>/storage-maintenance/last-own-sweep.json` (what was removed with its bytes,
what was kept and why). A removal cut off half-way leaves a
`.storage-retiring-<id>` folder, which the next sweep finishes.

Before the folders, on Linux, the sweep ends tare's orphaned processes. Every
check or command the Kernel starts without a sandbox carries
`TARE_OWNER=<pid>:<start time>` naming the tare that started it, and its
descendants inherit it; the direct child already dies with tare
(`PR_SET_PDEATHSIG`), but a grandchild survives a tare killed with SIGKILL. The
sweep kills (SIGKILL) a process of the current user, in tare's own PID namespace,
whose own environment names an owner the Kernel's `owner_alive` proves dead (its
pid gone, a zombie, or reused by a process that started at another time). A
process that cannot be read (another user's, or one that hid its environment),
whose owner is alive or cannot be told, that carries this tare's own inherited
stamp (a shell tare was started from), or that carries no stamp, is left alone.
Each killed process is recorded in the receipt (`killed`: pid, program name,
first 200 characters of its command line, never its environment), and `/storage`
shows the count. Windows needs none of this: a check's Job Object ends its tree
when tare dies, and the sweep never looks at processes there. Only what tare
itself would have ended is killed: the process must still be in the process group
tare made for the command (`TARE_GROUP`, set by the Kernel's exec shim). A daemon
the command detached on purpose (a build daemon other programs share) left that
group and is spared, though it carries the stamp.

## Leftovers of other tools

Other programs' files are candidates by age or by their own cleanup command, after
**days kept** (7 unless changed beside the automatic option):

- **CLI session logs:** Codex CLI (`~/.codex/sessions`) and AGY CLI
  (`~/.gemini/antigravity-cli/log`) files untouched for longer. Claude Code
  transcripts are kept, since they back its `/resume`.
- **Old CLI versions:** Codex versions other than the one its `current` link points to.
- **Old test folders:** `pytest-of-*` in the temporary folder.
- **Package caches:** npm and pip, cleared by `npm cache clean --force` and
  `pip cache purge`, never by deleting their folders.

Each item is checked again against the same rule right before removal: a log
written since the preview, or a version now in use, is kept.

**Clean old logs, versions and caches of other tools automatically** (off until
checked) removes them without asking: `tare storage auto`, which the product
updater runs daily, or hourly while the disk is low. Only these leftovers, never
tare's own files. The choice is saved once per machine (`storage-auto.json` beside
the installation descriptor), and each run leaves
`<storage>/storage-maintenance/last-auto-cleanup.json`. `<storage>` is tare's storage
folder: `TARE_STORAGE_ROOT` when set, `.reconciliation` next to the clone for a source
install, `%LOCALAPPDATA%\tare.tools\product\state` for an installed product.

Of tare's own files, sessions, evidence archives, reports, challenger results,
worktrees and unknown directories are preserved (the sweep above handles only the
listed scratch). A folder's age or name is never proof that it is safe
to remove. This screen does not discard evidence or move it to the cloud.

## Rechecks and interruptions

Cleanup shares the install/rollback lock and rechecks the exact preview fingerprint,
active version pointers, process metadata and content checks before each removal.
Targets are checked as absolute paths, without following links or junctions, then
renamed beside the original and checked again before deletion. Changed or busy
items are kept. No shell is involved in removal.

`<storage>/storage-maintenance/last-cleanup.json` records the last operation,
including any retirement path left by an interruption. Such a directory is
preserved on subsequent scans; cleanup never blindly replays an interrupted
removal. Errors may leave a partial directory and are reported as such.

Process checks observe cwd, executable and absolute command arguments. They are
not a system-wide file-handle lock against arbitrary external programs. Package
manager caches and system temporary files therefore remain observation only.

## Prevention and measurement limits

The TUI checks free space at startup and every minute. Below **10 GiB or 10% free**,
the conversation header shows the free space and `/storage` while the condition
lasts. Below **5 GiB**, where running work may fail, it also notifies once per
episode. This does not stop normal tasks or automatically delete files.

Scanning runs outside the UI thread and shows progress.
You can leave a running scan with **Esc**; it stops without removing files.
Each directory has a
15-second/200,000-file inventory bound and the overall size walk has a 90-second
budget; installed-package verification follows separately. An unreadable or
incomplete path is kept, with `+` marking a lower-bound size. Symlink/junction
targets are excluded. Sizes are logical file bytes, not physical SSD allocation;
sparse virtual disks and filesystem compression can differ. Cleanup reports
removed file bytes, not a promised increase in filesystem free space.
For an incomplete operational origin, select its specific folder at the top and
scan again to concentrate the inventory budget there.

Qualification and live journey receipts are recorded on the single
`storage-maintenance-20260924` backlog node; the earlier evidence file
`docs/operations/evidence/STORAGE_MAINTENANCE_2026-09-24.json` was removed (Git
history keeps it).

## Installed delivery on 2026-09-24

Version **0.1.56**, source `b3587c15fc1546fcea36b0e510eb2652bd54f623`.
Five project wheels matched across two clean builds. All 240 OS source entries
match Git; 193 installed OS files match the release's source hashes. Dependencies
were reused offline from the previous bundle, without resolving new versions.

The installed production TUI passed the 80x24 journey, with current and rollback
versions protected and explicit selection/confirmation required. Actual rollback
to **0.1.55** and return to **0.1.56** preserved all ten checked evidence receipts.
The real cleanup before installation removed ten unused installations totaling
**1,808,090,407 logical bytes (1.68 GiB)**. Builds and evidence were not selected.
After installation, the installed panel removed two additional unused/intermediate
versions (362,924,037 bytes), leaving exactly the current release and its 0.1.55
rollback. All ten evidence hashes still matched. Total removed across both
operations: **2,171,014,444 logical bytes (2.02 GiB)**.

Bundle `tare-tools-0.1.56-win-py312.zip`: **36,147,841 bytes**, SHA256
`1d6e26bec83a929627264f01d5cee621ccca379394af824e4a9ea74a53a63bea`.
The raw evidence of this run (cleanup previews and receipts, installed and release
checks, exported TUI screens) is not published.

Final source CI passed on Linux (2,644 passed, 7 skipped)
and Windows (2,635 passed, 16 skipped), plus 105
quick contract checks on each platform: [run 36054724084](https://github.com/augusto-scarvalho/tare.tools.os/actions/runs/36054724084).

[Published release v0.1.56](https://github.com/augusto-scarvalho/tare.tools.os/releases/tag/v0.1.56).
