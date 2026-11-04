# The shared local GPU

One GPU on the GPU node (`<gpu-node>`, reached over Tailscale) serves three kinds of work: the local text models,
through the node's model gateway; image jobs (ComfyUI); and heavy jobs such as training, image batches and
benchmarks. They take turns. The node keeps the lock and its queue; tare reads it, decides whether to wait or to go
to another model, and records what happened.

## Who goes first

- **Image and heavy jobs first.** A text request gives the GPU up within 5 s to an image job that holds it or waits
  for it; the gateway then refuses text with `503 gpu_busy`. A running training step is never interrupted.
- **Text keeps the GPU for 20 s after each request** (idle grace), so an agent's next call does not reload the model;
  an image job waiting longer than 5 minutes ends that grace.
- **Text runs one request at a time**, smallest waiting request first.

## Heavy work on the GPU: the `gpu` command on the node

Never run heavy GPU work directly: it would take the GPU from text and image jobs without the queue knowing.
On the node (from another machine prefix `ssh <gpu-node> "wsl -e bash -lc '...'"`; `gpu hold` needs `ssh -t`):

- `gpu status`: who holds the GPU, what runs, who waits, and a late marker.
- `gpu run --task <name> -- <command>`: waits for its turn, runs, releases. The task name learns its usual duration
  (median of its last ten runs); `status` marks a run past 1.5 times that as late. The only cutoff is
  `--kill-after 2h`.
- `gpu hold --reason "<why>" 30m`: an interactive shell holding the GPU, the time is mandatory; leaving the shell
  returns it.

## What tare does when the GPU is taken

- **Nothing waits for a busy GPU.** A local route whose GPU an image job holds is skipped and the next route of tare's
  tiers takes the work at once: a conversation, its explorers and the implementers it delegates (a Work's units;
  a local session's unit then leaves the local bill, as the session itself would). A call refused mid-session
  (`HTTP_503` with `busy_reason` `gpu_busy`, `gpu_lease_timeout` or `gpu_lease_cancelled` in its receipt) costs no
  pause before the next route.
- **You decide the fallback.** Explorers fall back to the overflow explorer of `/delegation overflow` (the judge
  picks the tier). A conversation and its implementers follow the routing policy, which `/routes` in the TUI
  edits per role (one tier per line; saved to `<storage>/serve/routing.json`, "tare's defaults" goes back);
  a unit tries the coordinator's chosen route first and, when its GPU is busy, the policy's next routes.
- **Leaving the GPU to others:** `/local-gpu off` in a chat (or `/local-gpu off default` for the machine) keeps tare
  off the local models; `--no-local-models` does the same for one run.
- **Looking:** `tare gpu status` shows the holder, the queue and, when the node sends it, the estimated wait
  (`--json` for the raw status). When a `tare serve` turn leaves the local model for this, the chat says so
  ("local GPU busy (...), back in about 4 min; moving to the next route"), and `tare gpu report [--days 7]`
  sums the sessions that went to another route because of it, per route, with their tokens and known cost.

## Initial tare auto choice

New **tare auto** native conversations and the first ordinary `tare serve` chat turn share one initial
choice helper. It first checks the owner's configured policy routes for machine/chat opt-outs, installed
profiles and credentials, account quota blocks, and current shared GPU leases/reservations. A resident model
is not GPU ownership: image/heavy work holding or waiting, and another client's reservation, exclude local
routes. Availability is checked again after assessment, before dispatch; the gateway still makes the final
lease admission decision.

When hosted JEV is configured (`TYPESAFE_API_KEY`, including the existing credential vault) and permitted by
machine decision-model settings, tare asks the existing cheap hosted choice worker once, with a 3-second
transport timeout and no routing retry. It sends the **full user request** and eligible tiers described by
configured model identity, transport, context/output limits and profile source evidence. No local judge is
loaded, and no frontier model is called to classify. The serialized UTF-8 assessment is bounded to 6,000 bytes,
including descriptions; oversized requests bypass JEV rather than losing requirements to truncation.
The request text is sent to TypeSafe when JEV is used.

JEV may suggest only an eligible policy tier, not an arbitrary model. Custom tiers keep their actual profile
identities: a tier number is not a universal model-quality score. Routes within a tier and the subsequent
fallback ladder retain owner order. A single eligible tier skips the paid assessment. If the suggested
ladder disappears during assessment, tare retries the full configured policy deterministically, including
earlier available tiers, and records the fallback reason. Malformed/nonfinite or extreme future quota reset
timestamps retain a bounded quota exclusion rather than crashing or blocking forever.
Missing/disabled JEV, timeout/failure, abstention, or invalid/incomplete
probabilities keep the deterministic first-available policy choice. `TARE_JEV_SHADOW=off`, the existing
all-judges-off switch, also disables this assessment. Probabilities are advice, **not calibrated correctness**,
execution permission, or a guarantee of task success. There is no scheduler, persistent ranking service,
per-tool classification, or electricity-price optimization.

The saved deployment's `native_auto_selection.decision` records the source (`policy` or `hosted_jev`), selected
tier, reason, JEV model/input-token usage when reported, and availability exclusions; serve also saves the
selection with its chat session entry. The receipt contains neither raw request text nor credentials.
The initial decision does not pay for an additional shadow classification. Existing observational shadows
elsewhere are unchanged. Explicit deployments, resume, provider continuations, successor sessions and follow-up
turns do not invoke initial routing; their existing saved profile, history and cache identity remain in use.

Offline regression coverage is in `tests/test_jev_initial_routing.py`, `tests/test_tare_auto.py`, and
`tests/test_serve_agent.py`; it uses fixture transports/profiles/lease observations, not providers or a live GPU.

## Reserving the GPU for a bench or a long run

From this machine, without ssh (contract `gpu-lease/2`; the node needs its API token, see
[`node/docs/SHARED_GPU.md`](../../node/docs/SHARED_GPU.md)):

- Save the node's token once: `tare setup --credential TARE_GPU_TOKEN` (the value from the node's
  `~/.local/state/tare-qualified-models/api-token`, read from stdin, kept in the vault).
- `tare gpu run <name> 2h -- <command>`: reserves the GPU for text up to the time given (at most 4 h), runs the
  command and releases it when the command ends. Only the tare sessions the command starts use the reserved GPU:
  they carry the reservation's secret (`TARE_GPU_RESERVATION`, sent only to local routes). Your other conversations,
  the Copilot and every other client get "reserved" and take their next route at once, like for a busy GPU.
- `tare gpu hold <name> 2h`: the same without a command, until the time ends or Ctrl-C.
- `--wait 30m` keeps trying while the GPU is taken (a training job is never cut for a reservation); without it, one
  try. The reservation renews itself every ttl/3; if the node ends it, tare says so and the command goes on with a
  shared GPU.

## Lease qualification: `res-ip-d`

This bounded experiment uses the actual shared GPU lease protocol (`gpu-lease/2`) in disposable Linux state,
with real flock, processes and loopback HTTP. It runs no GPU workload, downloads no models and changes no
installed service. Process death/restart is exercised; the operator's machine is not rebooted.

| Boundary | Required behavior |
|---|---|
| Concurrent admission | Recheck under the existing admission lock; preserve the first grant and refuse the contender. |
| Storage | Commit the restart fence before acquiring the GPU; unavailable/corrupt state refuses without holding it. |
| TTL and maximum duration | Missing renewal expires the reservation; renewal cannot extend its original maximum. |
| Revocation | Old IDs/secrets/fences cannot change a successor; admitted work keeps exclusion until completion. |
| Owner death/restart | Kernel lock is released, stale metadata grants nothing, and the same state yields a larger fence. |
| Cancellation | A local cancelled/dead waiter leaves the queue. A disconnected remote holder loses its reservation through TTL; local cancellation is not remote cleanup acknowledgement. |
| Kernel client | One loss notification; leaving a lost reservation cannot release its successor. |

Node owns this resource's lock and reservation; the pinned Kernel client consumes the protocol and OS chooses
when to reserve or reroute. Kernel's `WorkspaceLease` is a pinned Git workspace descriptor, not a TTL resource
reservation. This reconciles `res-ip-d` with TCP-06 without claiming Genesis activation, global deadlock freedom
or a replacement for its multi-agent authority protocol. TCP-06 keeps its own recorded obligations.

## Which models the node serves

The fleet is yours. The node ships tare.tools' catalog: the models we qualified, with what each does well. Your fleet
picks from it, adds your own models (only the weights' path on the node; the backend's arguments if you want) and
sets the default, which tare auto runs locally (the `local-default` route of `/routes`; tare's measured profile
when the default is a catalog model, a generic one when it is yours). The catalog's models are listed first.

- `/fleet` in the TUI: check catalog models, one line per own model (`name /path/to/weights.gguf -c 32768`), the
  default; Save changes the node at once.
- `tare fleet` shows the catalog and the fleet; `tare fleet add|remove <id>...`, `tare fleet default <id>`,
  `tare fleet own <name> <path> [-- <args>]`. Both use the node token of `tare gpu` and refresh this machine's view.
- On the node it is `~/.config/tare-node/fleet.json`, which updates never touch
  ([`node/docs/QUALIFIED_MODEL_FLEET.md`](../../node/docs/QUALIFIED_MODEL_FLEET.md)).

## Where the pieces live

- The contract between the node and tare, `gpu-lease/1` and `/2`: the kernel's `docs/GPU_LEASE_CONTRACT.md`.
- The node's service (gateway, lock and queue, `gpu` command, ComfyUI hook, reservations, its installer):
  [`node/`](../../node/README.md) (`docs/SHARED_GPU.md` there). The node runs an installed version of it
  (`~/.venvs/tare-node`), not a checkout; tare.tools.local-labs keeps the experiments and the qualification evidence
  the fleet registry cites.
