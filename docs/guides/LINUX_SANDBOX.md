# The Linux sandbox for checks

On Linux, tare runs the checks of a model's work in a bubblewrap sandbox: the coordinator's `check`, an
implementer's or auditor's `verify`, a unit's checks and a Work run's verification. These are the same checks
that run in the process sandbox on Windows ([Project permissions](TUI_PROJECT_PERMISSIONS.md#checks-run-in-the-sandbox)).
The sandbox is the Kernel's profile (`compute_plane/linux_bwrap.py`). A local model is used as implementer only
where this sandbox works.

## What a check gets

- Read-only: the system folders (`/usr`, `/bin`, `/sbin`, `/lib*`, `/etc`), the check's runtimes, the project and
  the folders granted to it.
- Writable: the plan's edit paths and the check's own scratch folders (its `HOME` and temporary folders).
- Nothing else exists inside. Your home folder and other projects are absent.
- Secret files (`.env`, `.npmrc` and the like) are masked: reading one fails with "Permission denied". Renaming a
  folder above one carries the mask along.
- No network. The sandbox has its own loopback: a test's own server on `127.0.0.1` works. A service of this machine
  (a database on `127.0.0.1`) and other machines are unreachable, and name lookups fail.
- No VM sockets (`AF_VSOCK`) and no `io_uring`. In WSL2 a VM socket can start Windows programs.
- At most 1024 tasks, threads included. No core dumps.

When the sandbox blocks a check, tare asks before running it outside, as on Windows.

## Where it differs from Windows

- No memory limit. Windows caps each process at 256 MiB and the check at 512 MiB.
- Its own loopback instead of none.
- `/etc` is readable (user names, the host name), and so are the names of the folders above each bound path.
  `/proc` shows only the sandbox's processes, but also facts about this machine's kernel.
- The task limit counts threads: 1024 tasks, not 16 processes.
- A secret replaced from outside by a rename while a check runs (an editor saving `.env`) is readable by that check.
- Checks only. Protected commands (an **Action review**) stay Windows-only.

## Requirements

- Linux on x86_64 or aarch64.
- bubblewrap 0.8 or newer: the package `bubblewrap`. tare uses the first `bwrap` on `PATH`.
- `prlimit`: the package `util-linux`.
- Unprivileged user namespaces allowed for bubblewrap. Stock Ubuntu 23.10 and later (24.04 included) refuse them
  through AppArmor; so do some containers. WSL2's Ubuntu has AppArmor off and needs nothing more.

tare checks all of this by starting one sandbox. Where that fails, `/permissions` says **Checks: not isolated: no
sandbox on this system**, followed by **Why there is no sandbox:** and the reason, and a local implementer is
refused with the same reason. tare never installs packages or changes system settings.

## Allowing bubblewrap on Ubuntu (AppArmor)

This is for an administrator. Find the bubblewrap tare uses, as AppArmor sees it (the real file, links followed):

```bash
readlink -f "$(command -v bwrap)"
```

It is usually `/usr/bin/bwrap`. If it is another path, write that path in the profile instead. Save the profile as
`/etc/apparmor.d/bwrap` and load it:

```bash
sudo tee /etc/apparmor.d/bwrap > /dev/null <<'EOF'
abi <abi/4.0>,
include <tunables/global>

profile bwrap /usr/bin/bwrap flags=(unconfined) {
  userns,
  include if exists <local/bwrap>
}
EOF
sudo apparmor_parser -r /etc/apparmor.d/bwrap
```

It lets bubblewrap alone create user namespaces. Every other program stays restricted, and
`kernel.apparmor_restrict_unprivileged_userns` stays 1. AppArmor loads it again at every boot. CI proves the
sandbox on GitHub's Ubuntu 24.04 runner with this same profile (`Linux sandbox probe` in `.github/workflows/ci.yml`).

### The broader alternative, not recommended

```bash
sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
```

This lifts the restriction for every program on the machine, not only bubblewrap. Unprivileged user namespaces are
a common first step of attacks on the kernel, which is why Ubuntu restricts them. It also lasts only until the next
reboot.

## Check that it worked

```bash
bwrap --unshare-user --unshare-net --ro-bind / / true && echo "bubblewrap works"
```

Then restart tare: it asks once per process. `/permissions` then says **Checks: in the Linux sandbox**. From a
source checkout, `python scripts/ops/sandbox_probe.py` runs every sandbox case and exits 0 when they pass
([CI feedback](CI_FEEDBACK.md)).
