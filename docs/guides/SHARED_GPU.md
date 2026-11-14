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

## Which routes automatic routing may use: the routing profile

`/routes` sets the order of each role's routes. The routing profile sets which routes automatic routing may use
for each role. It does not change the order. tare ships a default, and the profile keeps only your differences
from it (key `routing_profile` in this machine's `environment.json`, which the TUI, the CLI and `tare serve`
share). A model that appears later follows the default, and an update of tare's default keeps your entries. For
now the default is the routes each role uses today, so the profile can only switch routes off; an "on" entry
undoes a broader "off". The **Models per role** tab of `/routes` edits it ([below](#the-models-per-role-tab-of-routes)).

- **Only your own choices pass it.** These still start a route that is off:
  - the model you pick (the picker's concrete model for a conversation or a flow, or a route you name with
    `--deployment` or `--native-deployment`), for the role it runs in;
  - a provider switch you review in the TUI;
  - a role pin (`--implementer`, `--auditor`, ...) of that route, for that role (an auditor pin also covers plan
    reviews, and an implementer pin a conversation on tare auto); an automatic path takes a pinned route as the
    backstop does, it does not skip it;
  - `--coordinator`, for the conversation;
  - the explorer and overflow explorer you set in `/delegation`, as routes: a launch restriction that leaves them in
    place keeps them yours.

  A Work that pauses before its implementer starts (`--wait-before-implement`) remembers which implementer you
  named, so its resume in a later process starts that route, and only that one, as yours. A flow or plan review retried in a later
  process (`--retry-review`) does not: pin the route to run it.

  Everything else is automatic and skips the route: tare auto's tiers and the escalation to a next tier, the role
  tables, the planner, auditor, vision, researcher and heads-up lists, a saved `routing.json` (the order file does
  not switch a route back on), a challenger sampled by `/delegation`, a route the coordinator names for a unit's
  implementer, a plan or a search (the coordinator is a model, so its choice is not yours), the conversation's own
  model when a unit falls back to it (it was picked for the conversation, not as implementer), and a planner
  reviewing its own plan when no independent reviewer is left (that is plan-auditor work). A choice is for one
  route in one role: picking a model for the conversation or pinning it as planner does not make it your auditor.
  The relay mesh's chairs have no choice of yours: the profile decides for its implementer, planner, auditor and
  plan-auditor chairs (a chair that is off is skipped like an exhausted one); its bookkeeper and overseer are not
  roles of the profile. The mesh's planner starts its CLI without naming a model, so the CLI runs whatever it is set
  to: any entry that switches a model off for `planner` on that CLI (or on every source) refuses it, because tare
  cannot tell which model would run. A command you set yourself in `RELAY_PLAN_AUDIT_CMD` or `RELAY_CODE_AUDIT_CMD`
  is your command, not a route: the profile does not read it.
- **Two layers.** Each automatic path skips a route that is off and takes the next one, and says why (below).
  Behind them, every new start of a role checks again where it starts (a session, a Work's implementer, or a
  vision, heads-up, research, `tare explore` or `tare-copilot` call, an edited last message included): a start the
  user did not choose is refused with
  `SWITCHED_OFF_FOR_ROLE`, so a path that forgot to skip refuses loudly instead of running the route. A session
  that already started goes on: a follow-up turn or a resumed session keeps its route. A fresh session is a new
  start and is checked: after an edited earlier message, a mode change, a spent budget or an escalation,
  `tare serve` moves the chat to the next route that is on.
- **Rules come first.** A provider declined in setup, a route not yet proven here (`NOT_QUALIFIED_PIN_ONLY`), an
  older model (`OLDER_MODEL`), an auditor of the same family, a missing credential or a spent quota keep their own
  reasons. An "on" entry never makes such a route usable.
- **A skipped route says why:** `SWITCHED_OFF_FOR_ROLE` and the entry, in tare auto's skipped routes, in a role
  table's candidates and in "No route can start", with how to switch it on (`tare setup --auto ROLE:ENTRY=on`) or
  pin it, so the coordinator can tell you. A role left with no route behaves as when none is available: no auditor
  means the change is not audited, no implementer means `tare_auto_no_route_available`, and a plan whose planner
  is off for `plan-auditor`, with no independent reviewer left, is blocked before any model call
  (`ROUTE_BLOCKED`, "No eligible plan-auditor").

An entry is `MODEL@SOURCE` or a route id. tare reads a route by what it is, never by what its model's name
resembles:

| Entry | Matches |
|---|---|
| `claude/fable@api` | Claude Fable on Anthropic's API, every version, a new one included |
| `claude/fable-5.2@api` | only Claude Fable 5.2 there |
| `claude/*@claude-code` | every Claude line on Claude Code |
| `*@openrouter` | every route on OpenRouter |
| `openrouter-muse-wonder` | that one route, a route with no line, by the id `tare models` shows |

- A route has a line only where its endpoint is the vendor's own: the vendor's CLI or API, Codex Direct for
  OpenAI, or AGY serving Google's or Anthropic's model (`agy-claude-opus-5-5-cli` is `claude/opus-5.5@agy`, at
  every effort). A reseller, a custom endpoint or a local model has no line, even when its model is called
  `claude-fable-5-2`: name it by its route id or by `*@SOURCE`.
- `MODEL` is `*`, a vendor (`claude/*`), a line as `relay/model_lines.json` names it (`claude/opus`, `gpt/sol`,
  `gemini/argon`) or a line and version (`gpt/sol-6.1`; at most three parts of at most four digits).
- A route with a line is named only by `MODEL@SOURCE`, so one entry covers every route id of it on a source
  (`claude-cli` and `claude-opus-5-5-cli` both run Claude Opus 5.5 on Claude Code): `tare setup --auto` refuses a
  route id for such a route and prints the entries to use. A route id saved for one anyway is read as its version
  on its source, and `tare setup` says so.
- A route id names exactly one route, the one tare resolves it to (`local-qwen` and `qwen38-gsq` name the same
  route), and never another route that runs a model of the same name: two gateways that both serve `default` are
  two routes, and `default-high` is a third. `auto`, `tare-auto` and `local-default` are names of automatic
  routing, not routes: `tare setup` refuses them and ignores a saved one. A name that becomes a route when it
  runs is checked as the route it became: `local-default` as the fleet's default route, `local-auto` as Swift
  Flash-Next when the host is idle and as its own route otherwise, `auto` and tare auto as each candidate they
  take.
- `SOURCE` is `*` or one of `claude-code`, `codex` (the Codex CLI and Codex Direct: one ChatGPT plan), `agy`,
  `opencode-sub`, `opencode-api`, `api` (only a vendor's own API: Anthropic, OpenAI, Google), `openrouter`,
  `bedrock`, `local`, `other-api` (any other endpoint paid per token).
- The most specific entry wins: a route id, then a version, a line, a vendor, `*`; at each of these a named
  source beats `*`. When "on" and "off" tie, "off" wins. Two entries tare reads the same
  (`claude/fable-05.2@api` and `claude/fable-5.2@api`) are one: setting one replaces the other.
- Roles: `implementer` (tare auto's tiers, the conversation tare auto starts, Work units), `explorer`, `planner`,
  `plan-auditor`, `auditor`, `researcher`, `vision`, `heads_up`.
- An entry tare cannot read is ignored and named by `tare setup`; it never stops routing.

```
tare setup --auto auditor:claude/*@*=off --auto auditor:claude/opus@api=on   # audits: of Claude, only Opus on the API
tare setup --auto auditor:claude/opus@api=default                           # back to tare's default
tare setup --auto-reset auditor                                             # forget one role's entries
tare setup --auto-reset                                                     # forget the whole profile
tare setup --auto-show auditor                                              # what the role considers, and why not now
```

`--auto-show` prints one row per route the role considers, checked without a live probe:

```
auditor: the routes automatic routing considers (tare setup --auto auditor:PATTERN=on|off|default)
  route                  line@source                   profile                        now
  codex-cli-gpt-6-1-sol  gpt/sol-6.1@codex             on                             usable
  claude-opus-5-5-cli    claude/opus-5.5@claude-code   off (claude/opus@claude-code)  usable
  claude-api-opus-5-5    claude/opus-5.5@api           on                             CREDENTIAL_UNAVAILABLE
  claude-fable-5-2-cli   claude/fable-5.2@claude-code  on                             NOT_QUALIFIED_PIN_ONLY
```

`tare models --role ROLE` shows the same state in its "Automatic for ROLE" column.

### The Models per role tab of `/routes`

The second tab of `/routes` edits the same profile. Pick a role; each row is a model line (grouped by vendor, a
route with no line is a row of its own under Other, by its id) and each column a source that has a route for that
role here. Space, Enter or a click on the selected cell ticks it. Nothing is written until Save, and Esc asks once
before it discards unsaved changes. The file Save writes is the one the equivalent `tare setup --auto` commands
write.

| Cell | Means |
|---|---|
| `[x]` | on |
| `[ ]` | off by you |
| `!` after it | not usable now: a rule above the profile (not set up, `NOT_QUALIFIED_PIN_ONLY`, `OLDER_MODEL`, a missing credential, a spent quota) decides, the status line says which, and a tick cannot change it |
| `~` after it | a version entry differs from the line's |
| `.` | not carried by that source |
| `n/a` | that source carries it, but not for this role |

- Unticking writes `off` for the line on that source; ticking it back removes the entry. An `on` is written only to
  undo a broader `off`. Ticking a cell also removes the version entries of that line on that source for that role,
  and the status line names them before you save.
- Bulk actions for the role: a vendor or a source on or off (`claude/*@*`, `*@openrouter`), subscriptions only
  (`opencode-api`, `api`, `openrouter`, `bedrock` and `other-api` off), copy another role's entries, and tare's
  defaults for the role.
- The status line always says when the edited profile leaves a role with no eligible route and what then happens,
  and when an implementer has no independent auditor left on.
- Entries no cell can tick (`*@*`, a vendor, a version, a line on every source, a cell a rule decides, an entry tare
  cannot read) are listed under the table; Enter or a click removes one.

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
