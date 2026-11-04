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
worktrees and unknown directories are preserved. A folder's age or name is never proof that it is safe
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
