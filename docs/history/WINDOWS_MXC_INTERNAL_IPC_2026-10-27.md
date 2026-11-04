# Windows MXC internal IPC qualification (2026-10-27)

An ordinary coordinated-chat delivery selected existing Usage TUI tests. Under the current MXC deny-network profile, Python 3.12 on Windows stopped while creating `asyncio`'s internal TCP socket pair. The pytest timeout was reported as failing checks, which invited another implementation even though test assertions had not been established. The public run was stopped for diagnosis; no candidate was accepted or applied.

Small owned native probes used the same Windows process factory, bounded execution, filesystem scopes and verified cleanup. A host listener was reachable before each contained check. No inference, model download, global firewall modification or production policy change was involved.

| Probe policy | Internal socket pair / asyncio | Host listener |
|---|---|---|
| Current explicit deny-egress profile | Timeout | Denied |
| Omit the optional network field | Timeout | Denied |
| Add `networkLoopback` capability | Works | **Reachable: rejected as a solution** |
| Add only private-network capability, retain deny egress | Timeout | Denied |

The installed Python did not expose `socket.AF_UNIX`, so no Unix-socket compatibility layer was installed. These observations are specific to the tested Windows/Python build. They do not qualify a general networking policy or justify a broader grant. Small probe receipts remain in the operator archive under `backlog-top3-20261027/mxc-*-result.json`.

The shipped diagnostic now recognizes the observed MXC asyncio/socketpair timeout signature as inconclusive execution compatibility, rather than evidence of failing application assertions. It retains the ordinary candidate/check evidence and existing network policy. A matching trace can pause qualification; it cannot approve a command, omit a required check, or enable networking. Normal assertion failures, other backends and successful checks keep their original statuses.

For the current report/data-only change, the planner must reconcile the required checks with the actual changed scope and existing public TUI evidence. Required verification remains required. General support for network-denied Windows internal IPC needs a separately qualified substrate/runtime solution; opening access to host services is not a substitute. Microsoft's [PSEC schema](https://github.com/microsoft/mxc/blob/ad54c69452147bd4ca15d027388ad6074903dbff/external/windows-sdk/ProcessSecurityEnvironment.fbs) and [ProcessContainer network contract](https://github.com/microsoft/mxc/blob/main/docs/backends/process-container/networking.md) describe the relevant native policy boundary.
