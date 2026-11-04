# Packet audit: local advisory profile

Status: implemented, opt-in and advisory. It is not an independent audit of this implementation, nor an
admission, ratification or rollout. The OS composes packets and cases (`relay/audit_*.py`); the Kernel provides
Git reads and the Claude CLI driver (`compute_plane/audit_driver.py`). Standard library and existing primitives,
with no additional database, daemon, registry or agent loop.

Every command below runs in an activated source install (see "Install from source" in the README) as
`tare-ops audit <action> --store <cases-folder> ...`. Keep the store outside the candidate repositories.

## Independent exploration

`audit open --packet ... --request ...` uses independent exploration by default in new cases. The default is
`{"exploration": {"transport": "native-cli"}}`: the auditor reads the live repositories through the deterministic
evidence CLI (`relay/native_audit_access.py`), with no auxiliary model and no local model server. The Claude CLI
gets only its Bash tool, and a `PreToolUse` hook (`relay/native_audit_cli.py`) allows nothing but that CLI; there is
no MCP server.

- `read <repository> <path> <start_line> <end_line> [--revision WORKTREE|INDEX|<commit>]` returns a citable range
  with an `X` ID, at most 24000 bytes per range. It reads the current file by default and a Git revision without
  checking it out. Commit references do not mean the working tree is identical to them.
- `query <repository> <query>` looks the project up through SpecGraph (a published index when the exploration's
  `index_states` names one). It is navigation, not proof of current source or of an absence.

Each attempt must read at least one source itself; otherwise it fails with `independent_source_not_read`. Evidence
locations from earlier attempts are navigation only: read the source again to cite it. The verifier checks the
hashes of the files actually read; a source or HEAD change during the round is recorded as changed evidence, not
as a product defect or as acceptance.

There is no copy, no `git init/add` and no upfront hashing of the whole repository. The prompt carries repository
and revision references and the implementer's recommendation; full sources and diffs are not resent to the frontier
model. A dependency in another repository requires including that repository in the demand.

`exploration: null` explicitly selects the packet-only transport without tools, for compatibility and fixtures.
Existing cases keep their sealed profile. The retired route through a local model gateway
(`"exploration": {"api_base": ...}`, the Aider explorer) fails with `RETIRED_AIDER_AUDIT_ACCESS`; historical cases
need their archived host.

### Implementer and auditor

- The user's original objective governs the cycle. The implementer delivers the identity of the change, what it
  claims to have done, tests and a recommended path through the change to save reading. The recommendation does
  not restrict access or define the audit's obligations on its own.
- The auditor reads the live checkout of the demand's repositories, including the local changes actually
  submitted. It selects evidence independently and can consult dependencies outside the diff. The scope limits the
  behavior to evaluate, not the files it may consult.
- The final artifact identifies candidate and session, verified obligations, stable findings, location, evidence,
  impact on the objective and falsifier. It distinguishes an executed reproduction, a static demonstration and an
  unconfirmed hypothesis; it never presents a proposed test as an executed one.
- The implementer consumes the final artifacts, not the auditor's chat, stream, reasoning or drafts. An interrupted
  run delivers status and partial evidence, explicitly identified as such, with no approval.
- When correcting, the implementer returns the delta, a response per finding ID and updated evidence. It resumes the
  same auditor session; a reset is explicit and preserves the earlier artifacts. The new round checks the
  corrections and the delta's impact without routinely restarting the whole investigation.

### Closure, optional limits and authority

The monetary ceiling and the attempt and context ceilings are optional and absent by default. When selected, they
are recorded; reaching a limit ends that run with the available artifacts, with no automatic retry or reset to get
around it.

Each round must bring the delivery closer to the original objective: verify a correction, demonstrate a relevant
defect or resolve a concrete gap. Do not create obligations from style preferences, opportunistic refactoring or
unsupported hypotheses. Do not reopen a resolved finding without new evidence or impact from the change.

If a disagreement persists without new evidence, deliver a concise statement of the divergence, with impact and
alternatives, for the user to decide. The user's explicit instruction is sovereign: it can accept the risk, require
changes or stop, without agent unanimity. The queryable state includes `result_sha256`, which binds the decision to
the result the user saw:

```json
{"decision": "ACCEPT_RISK", "statement": "Explicit user decision",
 "operator_label": "User", "expected_result_sha256": "RESULT_SHA256"}
```

Record it with `audit user-decision --case ... --attempt ... --request decision.json`. The alternatives are
`REQUIRE_CHANGES` and `STOP`; an optional `limits` member replaces the case's limits. The entry is an operator
action under the same process trust model as the CLI. The review stays intact, and closure by human decision uses
`CLOSED_BY_USER`; it is not a fabricated auditor PASS.

## What the implementer receives

`audit handoff` delivers `tare.tools/audit-result/1`: run status, review status, findings by ID/obligation/location,
proposed falsifier, minimal fix and next action. It includes no transcript or base64 snapshots. `authority` is
always `NONE`. `audit status` is the compact projection, which the tests keep under 2 KiB. Querying never starts or
repeats the model.

A budget or timeout failure after a complete StructuredOutput keeps the draft as `PARTIAL`. That can provide
actionable findings, but never approval. Invalid JSON, incomplete coverage or unproven authorship do not close the
case. Auxiliary uses reported by the CLI are kept separate from the review's author.

## Preparing a packet without spending quota

```powershell
tare-ops audit prepare --store C:/audit-state/cases --request C:/audit-input/selection.json --output C:/audit-input/packet-r1.json
```

The output must be a new file outside the candidate repositories. `selection.json` contains `packet_id`,
`mode: advisory`, `intent`, `scope`, `non_goals`, `repositories`, `obligations` and, when available, `evidence`.
Illustrative structure (replace path and base with real ones):

```json
{
  "packet_id": "feature-r1",
  "mode": "advisory",
  "intent": "Fix the feature's behavior",
  "scope": "Input contract and persistence of this feature",
  "non_goals": ["General refactoring", "Global activation"],
  "repositories": [{
    "id": "os",
    "root": "C:/path/to/your-project",
    "base": "FULL_40_CHARACTER_GIT_SHA",
    "target": "WORKTREE",
    "changes": [{"id": "C1", "path": "relay/session_hooks.py", "why": "Fix persistence"}],
    "excluded": [],
    "context": []
  }],
  "obligations": [{
    "id": "O1", "claim": "The recorded receipt can be read back",
    "change_ids": ["C1"], "falsify": "Write near the limit and read back"
  }],
  "evidence": [{
    "id": "T1", "path": "C:/audit-input/test-receipt.json",
    "why": "Byte-limit regression", "obligation_ids": ["O1"]
  }]
}
```

Each repository is independent: `base` is a full SHA; `target` is a full SHA, `INDEX` (staged only) or `WORKTREE`
(tracked plus untracked non-ignored files). Every detected change must be in `changes` or in `excluded` with a
reason. Renames appear as removal plus addition. Context files use `id`, `path`, `why`, `obligation_ids`.

By default the compiler includes the diff and the full bytes of the selected files, with refs such as `C1.before`,
`C1.after`, `C1.diff`, and generates the seal; the auditor does not copy hashes or inventories. A later change of
bytes or absences invalidates the packet before spending. The initial context (instructions, input, response schema
and system prompt) is limited to 24000 bytes by default; `max_initial_context_bytes` can select up to 64000.
Exceeding the selected limit, binaries, symlinks, submodules and mode-only changes are refused, not truncated.
Ignored files are not discovered; supply needed dependencies explicitly as context.

Receipts are bytes of evidence declared by the implementer, not proof produced by the auditor: include command,
output/status, environment/executor and the link to the candidate. The compiler seals them but does not certify
execution or merit. The model's falsifiers are `PROPOSED`; nothing is run automatically.

### Automatic context and frugal delta

`context_strategy: specgraph` in a `repositories` member selects governing contracts, linked implementations,
direct static dependencies and tests automatically, with SpecGraph's parser and without writing a cache or running
candidate code. The graph is built from the current bytes and its digest is revalidated before and after the call.
The automatic profile requires `WORKTREE`. Missing links, parse errors and unresolved external dependencies ask
for explicit context instead of declaring sufficiency; static links do not prove coverage of dynamic behavior.

Relevant functions, classes and changed lines are kept whole, with the module context. `segments` contains text and
absolute lines; a finding on an omitted line is refused. Full bytes are preserved in the sealed `source_archive`,
outside the prompt; a full source is limited to 256 KiB and the custody packet to 2 MiB.

In a correction, the diff is against the previous round's candidate (`diff_basis`), not against the feature's
original base. The current context and all obligations remain; session context is never inherited approval.

### Explicit partitions by symbol

When a full file exceeds the ceiling and automatic links are not available, `changes` and `context` accept
`symbols: ["function", "Class.method"]` with `context_strategy: full`. These are exact names from the SpecGraph
parser, not arbitrary ranges; `@module` selects the lexical context of the module and classes (imports, constants,
decorators, headers, class fields). The diff of a modified file refers to the selected projection, so its line
numbers are not those of the original file. `omitted_changed_lines` makes changes outside the partition explicit. An
acceptance covers only the declared obligations. On resume, `FIXED` requires a difference in the reviewed
projection; editing only an omitted method is not enough.

### Review map and preflight

A `review_map` per obligation declares `contract_refs`, `dependencies` (`source_ref`, `symbol`) and `tests`
(`source_ref`, `symbol`, `receipt_ref`, optional `nodeids`). Without `nodeids`, the `path::symbol` family is used.
The JSON receipt names tests with result `passed`, the command, `executor: implementer`, `exit_code: 0` and
`source_sha256` keyed `repository_id:path` for every current source of the packet. These are verifiable claims by
the implementer, not independent certification.

```json
{"review_map":{"O1":{"contract_refs":["C1.after"],
 "dependencies":[{"source_ref":"D1","symbol":"decode"}],
 "tests":[{"source_ref":"X1","symbol":"test_bad_input","receipt_ref":"T1",
           "nodeids":["tests/test_hook.py::test_bad_input"]}]}}}
```

```json
{"executor":"implementer","exit_code":0,"command":["python","-m","pytest","tests/test_hook.py"],
 "tests":{"tests/test_hook.py::test_bad_input":"passed"},
 "source_sha256":{"os:relay/hook.py":"SHA256_REAL","os:tests/test_hook.py":"SHA256_REAL"}}
```

`prepare` and the pre-spend check refuse an incomplete map, an omitted symbol, a stale receipt, a test without
success and swapped references. The map is optional, to keep earlier packets valid. Before a real call,
`audit preflight` must return `DECLARED_INPUTS_CHECKED`:

```powershell
tare-ops audit preflight --store C:/audit-state/cases --packet C:/audit-input/packet-r2.json
```

It creates no case and calls no model. It shows bytes per source, repetitions and unresolved static references.
The static list is navigation, not a complete call graph; neither the check nor the model can certify semantic
sufficiency because names exist. Raise `max_initial_context_bytes` only for needed context; do not drop helpers,
fixtures or test hashes to fit. Ask one contract question per obligation and group questions that reuse the same
sources.

In corrections of packets with a map, `previous_review_archive` keeps the full sealed review and the prompt receives
`audit-review-navigation/1`, a projection of exact fields (verdict, IDs, severity, location, claims and context
requests) with the original's hash. Changing a claim or verdict in the projection and recomputing the seal is
refused. Old packets keep the full format.

Effort and model stay explicit; there is no automatic fallback or retry when a limit is exceeded.
`provider_token_usage` keeps numeric input/output/cache/thinking counters per model when the CLI reports them. An
absent count stays unknown; table cost is separate from billing and quota.

## Open, launch, query

`binding.json` defines `case_id`, `project`, `stage` (`plan` or `code`), the `implementer` and `auditor` identities
and optional `limits`, `work_ids` and `exploration`. Each identity contains `agent_id`, `model_id`, `runtime_key`
and `runtime_model`; `runtime_model` must be the exact alias of that model and runtime in
`relay/round_table_roster.json`, and the two identities need different bias domains. There is no alias fallback.
For example, an auditor on Fable 5.1 through the Claude CLI:

```json
{
  "agent_id": "fable51-auditor",
  "model_id": "fable-5.1",
  "runtime_key": "claude",
  "runtime_model": "claude-fable-5-1"
}
```

Fable 5.1 shares the `anthropic/fable` domain with Fable 5, so the two are not independent auditors of each other.

```powershell
tare-ops audit open --store C:/audit-state/cases --request C:/audit-input/binding.json --packet C:/audit-input/packet-r1.json
# launch spends quota; run it only once the model and effort are chosen.
tare-ops audit launch --store C:/audit-state/cases --case FEATURE --attempt 1 --cli C:/PATH/claude.exe --effort xhigh --timeout 900
tare-ops audit status --store C:/audit-state/cases --case FEATURE
tare-ops audit handoff --store C:/audit-state/cases --case FEATURE
```

`launch` reserves a single durable dispatch, starts it in the background and returns the run ID and result path;
`execute` is the blocking alternative. Both run in a stable workspace per case. With `native-cli` exploration the
CLI runs in restricted mode with only the hook-gated evidence CLI; with `exploration: null` it runs in safe mode with
no tools. This is not an operating-system sandbox. `--cli` must name the native executable, not a shell shim; the
timeout is at most 900 seconds.

`--max-usd` is optional in `execute`, `launch` and `qualify`. Omitting it runs without a monetary ceiling: no
`--max-budget-usd` is sent to the provider. An explicit limit must be above 0 and at most 100, and it is a soft
limit, not a guarantee of billing or quota. Do not use zero as a synonym for no limit. `launch.json` and
`result.json` record `max_usd_soft: null` when there is no ceiling.

Results record estimated table cost, tokens per model (including thinking and cache when reported), duration,
turns, context, authorship and review status. Context usage comes from `assistant.usage` when available; its
absence produces a `context_usage_unknown` warning, not an estimate. The deadline covers stdin, waiting and
cancellation of the child.

### OpenRouter transport

`execute`, `launch` and `qualify` accept `--transport openrouter` for packet cases (`relay/audit_fallback.py`). It
reuses the OpenRouter key from the environment or the vault, with the same model under its exact OpenRouter ID; the
Claude CLI keeps the hook, the evidence CLI and `--resume`. `metrics.transport` records the deployment, quota
domain, endpoint, model and the ID sent; the credential never enters the manifest. The review's author must match
the model observed in the stream. The option creates no automatic attempts and never switches models; the legacy
v1 route refuses it.

Qualified models: Fable 5 and Fable 5.1 (`runtime_key: claude`), and GLM 5.3 as `model_id: glm-5.3`,
`runtime_key: claude-gateway`, `runtime_model: openrouter/z-ai/glm-5.3`, where the observed author must be
`z-ai/glm-5.3`. Switching models creates a separate case and session with the predecessor's final artifacts.

The raw stream may reach 8 MiB to accommodate the CLI's verbose system events; the non-streaming response keeps
2 MiB. A single event is limited to 128 KiB and stderr to 64 KiB.

## Correcting and resuming the same session

Packets seal the output schema sent to the provider, which refuses line suffixes in evidence IDs and coverage
inconsistent with the findings. Relationships between verdict, findings and context requests are checked by the
local validator; do not fix the producer's review by hand.

First reproduce or fix the findings and gather every excerpt and receipt requested; only then run a review of the
whole set. Unproven suspicions are residuals or context requests, not reasons to widen the scope.

Prepare a new `packet_id` with the real changes and call `audit resume --packet ... --request ...`. The request
contains `case_id`, `continuation: {"reason": ...}` and, if there were findings, `correction`:

```json
{
  "schema": "tare.tools/audit-correction/1",
  "packet_id": "feature-r1",
  "dispositions": [{
    "finding_id": "F1", "disposition": "FIXED",
    "change_refs": ["C1"], "test_receipt_refs": ["T1"], "reason": ""
  }]
}
```

Every finding must be addressed: `FIXED` requires a real delta and a receipt; `DISPUTED` requires a reason and
evidence; `DEFERRED` requires a reason. This validates references; it does not accept the claim that the defect is
gone. New correction or test files can be linked only to the findings' obligations. Objective, obligations, stage,
identities and repository selection are fixed.

A session proven to exist is resumed with the same session ID and `--resume`; a reserved UUID without a producer
event is not treated as an existing session. The new review checks all obligations; no earlier PASS is inherited.

If the vendor session became unavailable, the resume request can contain
`session_reset: {"expected_session_id": ..., "reason": ...}`. That records the switch, keeps the same case and
attempts and includes the last review in the new session's packet. There is no automatic reset on a generic error.
A closed case requires another `case_id` with `reopen: {"case_id": ..., "reason": ...}`, preserving the predecessor
without inheriting its session or approval.

## Interruption and recovery

`audit cancel --case FEATURE --attempt 1` requests cooperative cancellation; the transport cancels its own child,
not a PID chosen by the user. `audit reconcile --case FEATURE` examines only the reservation, the process state and
the runner's artifacts; it does not look for conversations in vendor folders and does not redispatch.

If the supervisor died, a valid draft or result is recovered as partial and an invalid artifact gets a typed status.
The case stays inconclusive: a dead supervisor does not prove that an orphaned child died. Abandoned locks are not
removed automatically.

## Standalone qualification

`audit qualify --packet ... --run <new-folder> --cli <executable> --model <exact-alias> --effort xhigh --timeout 180`
runs one pilot without creating a case or changing the roster. It requires an explicit model, at most 8000 bytes of
initial context, and does not repeat a run already reserved. The result uses the same path as cases, with
`qualification_only: true`; it is not an admission route. The Kernel translates the OS's 2020-12 output schema into
the draft-07 form the CLI's validator accepts, without changing the OS schema.

v2 cases are advisory, even when they use the roster to pin identity; there is no bridge to admission. v1 cases
remain readable and keep their base64 result for compatibility.

The offline regressions are `tests/test_audit_*.py` here and `tests/test_session_and_audit_drivers.py` in the
Kernel; fixtures do not replace an independent audit.
