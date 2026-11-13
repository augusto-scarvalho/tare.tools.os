# Exploration and research delegation policy — 2026-09-07

Status: steps 1–3 of section 8 are implemented; step 4 is text. Complements
[INFERENCE_TELEMETRY.md](INFERENCE_TELEMETRY.md).

## 1. Principle

Paid models decide, edit and verify. Local models read, collect and
summarize. The frontier model never receives a page, a whole file or a corpus:
it receives a report of up to 350 words with a `source_ref` per claim, and asks for
`read_evidence` when it needs the literal text.

`explore` returns the Kernel explorer's report (`report`, ≤ 350 words,
with `files_read_count` and the custody of the receipts); `research` returns the
`summary / evidence / remaining` envelope. Both with `authority: NONE`. The caller
is responsible for how the result is used; there is no conversion from one format to the other.

## 2. Tools

The interface is the CLI. Claude Code and Codex already run shell;
a command that prints the JSON envelope to stdout is the tool. There is no
server, port or state beyond the disk.

| Command | Status | Scope | Local cost |
|---|---|---|---|
| `tare-ops explore --root R --question … [--focus path:start-end …]` | native read-only exploration (`relay/native_exploration.py`) | repository, question ≤ 8 KB, focus ≤ 48 KB | 1 native session, ≤ 240 s |
| `tare-ops evidence --root R --path P --start N --end M [--revision SHA]` | wraps the Kernel's `read_evidence()` | literal excerpt, ≤ 200 lines, `X-` ref | none |
| `tare-ops research --question … --seed URL… --domain D… --max-pages N --max-depth D [--collect-only]` | `relay/research.py` | bulk, recursive collection, with limits | parallel fetch + N Qwen calls |
| `tare-ops research --result JOB [--ref W-…]` | same | the envelope of a job, or up to 200 lines of a collected page | none |

`research` runs in the foreground and prints the envelope at the end; a caller that wants
it asynchronous runs it in the background through its own CLI and queries `--result`.
The default custody is tare's storage folder, `<storage>` (`explore/`, `research/`), which
`tare-ops telemetry` already reads; `--custody` changes it. `<storage>` is `TARE_STORAGE_ROOT`
when set, `.reconciliation` next to the clone for a source install, and
`%LOCALAPPDATA%\tare.tools\product\state` for an installed product. The `explorer.lock`
lock in the custody serializes exploration and research with each other.

The `audit_local` MCP server that already exists in
`tare.tools.kernel/compute_plane/audit_exploration.py` stays as it is,
with `explore` and `read_evidence`, only for the restricted audit mode in which the
driver denies shell to Claude Code. It exists because `--restricted` plus
`--allowedTools` by tool name is the strongest and shortest guarantee
that the auditor has exactly two typed capabilities; replacing it with an
unrestricted `Bash` would require a command-line parser in the hook, which is more
code and a fragile trust boundary. It is ~55 lines with no SDK, no network
and no state; there is nothing to cut. It stays frozen at two tools. If the
restricted mode needs bulk research, the decision is made at that time, with
typed arguments, not with a shell parser. Codex has no restricted mode today
and uses only the CLI; if it gets one, it reuses the same `serve()`.

## 3. Routing rule

Apply in order; stop at the first one that fits.

1. **I know the file or symbol.** I read it myself. Do not delegate.
2. **A "where is / what does it say / what exists" question inside the repo.** `explore`.
3. **A literal excerpt of something already cited.** `read_evidence`.
4. **More than five external sources, or any link recursion.** `research`.
5. **A result with a non-empty `remaining`.** At most one second, refined
   question to the same tool. After that, the caller reads on its own.

Do not delegate: scope decisions, editing, running tests, verifying
results, widening domains or crawl depth.

## 3b. Cost per role: the cheapest route that clears the floor

Four tiers of marginal cost, written in `cost_tier` in
`relay/role_deployments.json` and projected by `python relay/round_table_roster.py --roles`:

| Tier | What it is | Deployments |
|---|---|---|
| `free` | local GPU, no money or quota | `local-qwen` |
| `flat` | subscription plan, zero marginal cost until the quota runs out | `claude-cli`, `codex-cli`, `gemini-cli`, `kimi-cli`, `opencode-sub-*` (reserves) |
| `free-credit` | AWS credit, reserve | `bedrock-*-explorer` |
| `metered` | paid per token | `openrouter-glm-auditor` |

Quality floor per role, on the Artificial Analysis scale of 2026-09-07
(local Qwen 34, GLM 5.3 44, Fable 5.1 and GPT-6 Astra 51):

| Role | Floor | Ladder, from the cheapest that clears the floor upward |
|---|---|---|
| explorer | correct reading with citation | `local-qwen` → `bedrock-*-explorer` in parallel for large slices. Never `flat` or `metered`: spending CLI quota on reading is the most common waste. |
| research | cited facts | `local-qwen`. Bedrock reserve (DeepSeek-V3.2, Nova Pro) once the transport exists. |
| planner | routine: 34; changes architecture or contract: 51 | routine on `local-qwen`; the rest on `claude-cli` or `codex-cli`. `metered` only when both plans are exhausted. |
| implementer | bounded edit with a configured verifier: 34; the rest: 51 | small edits with `--allow-exec` and verifiers on `local-qwen`; the rest on `claude-cli` or `codex-cli`. `kimi-cli` and `metered` only when the quota is exhausted. |
| auditor, plan-auditor | 44, and a bias domain different from the author's | a `flat` CLI from a vendor other than the implementer's (Claude audits Codex, Codex audits Claude, Gemini when both are authors). `openrouter-glm-auditor` when the eligible CLIs are out of quota or all are authors. `local-qwen` never audits a complex abstraction alone; it serves as the auditor's explorer. |

Rules that come with it:

- **Plan quota is the scarce resource, not money.** It resets every cycle;
  what is spent on exploration is missing for judgment at the end of the cycle. That is why
  the `free-credit` reserve ranks above `flat` for exploring, even though it is an
  older-generation model. The OpenCode subscription reserves are the exception: that plan serves
  only implementers and explorers, so using it takes nothing from judgment, and automatic
  selection reaches them before the credit when the local model is unavailable.
- **Independence matters more than the ladder.** If the cheapest route that clears the
  floor is in the same bias domain as the author, go up one step. Never down.
- **Moving up a tier requires a reason recorded in the receipt:** quota exhausted,
  an authorship conflict, or a floor the previous step did not reach. "Just to be
  safe" is not a reason.
- **Qwen becomes a routine auditor only with an executable verifier.** Without a
  verifier, the audit floor is 44.
- **`--local_only` is the owner's exception, not a new ladder.** In that mode,
  and only there, `local-qwen` also takes the planner and auditor roles
  (`local_only_roles` in `relay/role_deployments.json`): an admission by the
  owner's choice of the mode, not a claim that it clears the floors above. Its
  audit of a Qwen-family implementer shares the author's bias domain, so it is
  labelled **same-family audit, not independent**, and the change is applied
  only after the user confirms it in the TUI: after an ACCEPT, or, when the audit
  left only open items (no findings) and the checks passed, as the user's own
  decision that no audit accepted. Without a TUI (`tare serve`, headless, Work
  runs) nobody can confirm, and the candidate stays unapplied.

## 4. The `research` pipeline

Three layers, always in this order, with the whole collection before inference.

**Collection, no model.** `relay/web_reader.py`, which already existed, fetches and
extracts to markdown: Trafilatura first, Firecrawl only if `FIRECRAWL_API_KEY`
is set, urllib plus Trafilatura as a last resort. Recursion is a
breadth-first search over the markdown links, restricted to the domain allowlist,
with parallel fetch. Output: `corpus/<sha256 of the content>.md` and `manifest.jsonl`
with URL, hash, depth, method, bytes and the citable `W-` ref.

**Map, local Qwen.** One call per batch of pages up to `batch_chars`
(default 24,000 characters; adjust after checking the backend window in doctor):
"given the question, extract up to 5 facts with a literal quote or `facts: []`".
A page larger than the batch goes in alone, truncated at `batch_chars`. Facts with
a ref that does not belong to the batch are discarded. Serial, under the lock.

**Reduce, local Qwen.** Relevant facts, grouped by source, become the
final envelope. If it exceeds the window, it reduces in a two-level tree.

Mandatory input limits: `--domain` and `--seed` with no default,
`--max-pages` ≤ 5000, `--max-depth` ≤ 5, 200 KB per page, `--timeout`.
Each job has its own corpus; to reuse it, call `--result JOB`, do not recrawl.

## 4b. Parallel exploration: local plus reserve

Large exploration does not need to be serial. The `explorer.lock` lock serializes only
the local backend, which is one GPU; the Bedrock reserve shares neither lock nor
backend. Two routes run at the same time, in separate processes:

```bash
# slice 1, local Qwen, audit_local route
python tare_ops.py explore --root ../repo --custody <custody> --focus src/a.py:1-200 --question "..." > a.json &
# slice 2, Opus 4.6 on Bedrock, native explorer role
python tare_ops.py agent --role explorer --deployment bedrock-opus-explorer --root ../repo --session <custody>/opus-1 --instruction "..." > b.json &
wait
```

Measured on 2026-09-07 on the Kernel: Qwen 25 s, Opus 19 s, wall clock 25 s. The total
time is that of the slowest slice, not the sum.

Rules:

- **Slice by scope, not by question.** Each route gets disjoint files or
  ranges and the same question. A question split in two produces
  two half-answers that nobody can put together.
- **The caller merges.** The frontier model reads both envelopes and cites both
  receipts: `explore-<id>` from the local route and `A-n` from the managed session. Neither route
  sees the other.
- **Give the local route a focus.** `explore` only sees the repository map; without
  `--focus` it asks for the files instead of answering. The explorer session reads
  on its own with `read`, so it takes the slice that requires following dependencies.
- **Reserves in parallel with each other.** Three reserve explorers exist:
  `bedrock-opus-explorer`, `bedrock-sonnet-explorer` and
  `bedrock-glm-explorer`. Measured on 2026-09-07 with the same slice on all three at the same
  time: Opus 43 s, GLM 5 48 s, Sonnet 54 s, wall clock 54 s, zero errors, all three
  artifacts complete. The Bedrock quota is **per model and per burst** (tokens
  per minute): two `RateLimitError`s on Opus came from Opus sessions chained
  within a few minutes, not from parallelism with other models. Rule: large slices
  go to different models at the same time; the same slice repeated
  on the same model waits one minute. The Kernel does not back off yet and turns
  the empty response into `INVALID_ACTION`; use an explicit `--session` to resume.
- **Web research stays local.** `research` speaks OpenAI-compatible to the
  local backend; Bedrock is not OpenAI-compatible, so map and reduce on the
  reserve (DeepSeek-V3.2, Nova Pro) do not exist as a route yet. This is pending work,
  not a capability.
- **Two local sessions do not parallelize.** There is only one GPU. Parallelism is
  local plus reserve, or reserve plus reserve if the quota allows.

## 5. Provenance and telemetry

- A research `source_ref` is `W-` plus a hash of the URL and the extracted content;
  `tare-ops research --result JOB --ref W-…` returns the literal excerpt.
- Each Qwen call in map and reduce records the UTC start, duration and tokens
  in private custody, like today's delegated observations. The
  `tare-ops telemetry` panel shows the volume absorbed by the local model.
- The job receipt is in `result.json`, schema `tare.tools/research-result/1`:
  question, limits, pages per method, facts, `model_observations`, duration
  and the envelope. The telemetry collector reads this receipt as the `research` origin.
  stdout carries only the envelope and the counts.

## 6. Use in the CLIs

No registration. The calling CLI runs `py -B -m tare_ops explore …` or
`… research …` like any shell command and reads the JSON from stdout.
Claude Code can get `Bash(py -B -m tare_ops explore*)` and
`Bash(py -B -m tare_ops research*)` in the project allowlist so that it does not ask for
permission on every call; Codex, the equivalent in its sandbox policy.

The section 3 rule goes into the system prompt of each role, with the exact
syntax of both commands. In restricted audit mode, the driver keeps the existing
MCP and the hook that denies everything else.

The rule does not depend on the prompt alone. The Kernel's canonical hook (`tare-hook
claude` / `tare-hook codex`, gate `LocalModelsFirstGate`) denies `Agent`/`Task`
in Claude Code and `spawn_agent` in Codex while the local server answers on
`/health`, with a reason that points to the three commands. If the server is down, it
allows them; `TARE_LOCAL_MODELS_BYPASS=1` is an explicit user decision.
Registration: matcher in `~/.claude/settings.json` and the project's `.codex/hooks.json`.

## 7. Preserved restrictions

- Exploration is read-only, with no execution, one job at a time.
- The backend must be slop.cpp/Qwen on the GPU workstation; check doctor before
  fixing the map batch size.
- No paid inference in the implementation or the tests: deterministic HTTP
  and Qwen replaced by a stub, following the current explorer tests.
- A quantized Qwen does not replace an auditor of complex abstractions; research
  produces cited facts, not judgment.

## 9. Audit cycle: threat model, plausibility and stopping

A cyclic audit between implementer and auditor does not converge on its own: an
obligation written as universal ("never overwrite anything local") has no
fixed point against git's state space, and each round costs a session.
The concrete case is in the delegation handoff, section 3e: two high-value
rounds, then nine rounds stuck on a single obligation. Three duties, each
checkable by a different party, close the cycle.

**Implementer (packet).** Cycle packets use `review_schema_version: 2`
in the selection. In this version `prepare()` refuses any obligation without
a `threat_model` (`actor`, `window`, `access`: who can cause the damage, in what
window and with what access) with `threat_model_required`, and the response contract
becomes `audit-review/2` (`schemas/audit-review-v2.schema.json`). Version 1
stays frozen for the historical cases. Before resubmitting a round, the implementer
runs the ledger and attaches the JSON to the supplement:

```bash
python -m relay.audit_loop <audit-folder> [--obligations obligations.json]
```

Exit 0 is CONTINUE or ACCEPTED; exit 3 is STOP_STALLED or STOP_BUDGET. With
exit 3 the only admissible round is one that rewrites the obligation with the threat
model or one that declares the residual; resubmitting the same obligation is a violation.

**Auditor (review).** In `audit-review/2` every finding carries a `scenario` with
`actor`, `window`, `access` and `plausibility` in `operational`,
`adversarial_operator` or `laboratory`; `validate_review()` refuses a finding without
a scenario (`invalid_finding_fields`), a malformed scenario (`invalid_scenario`) and a
scenario outside the obligation's `threat_model` (`finding_outside_threat_model`):
that case belongs in `residuals`. In v1 reviews read by the ledger, a finding without
`scenario` counts as `undeclared`, which never keeps the cycle alive.

**System (ledger).** `relay/audit_loop.py` reads `review_round<N>.json` in
order and decides by the trend, never by the last verdict: `STOP_STALLED`
when the last `stall_rounds` (default 2) rounds fail exactly the same
obligation, alone, with no `operational` finding; `STOP_BUDGET` when it reaches
`max_rounds` (default 6); `ACCEPTED` only with `ACCEPTED_BOUNDED`. Replay of the ten
rounds of 2026-09-08: the ledger would have stopped at round 4.

Rules of thumb that the ledger encodes: severity without probability is noise;
the stopping criterion is decided before the first round; whoever decides to continue
is not whoever fixes; a falsifier with simulated git measures the function, not the
system, and counts as `laboratory`; changing the design decision (only reporting instead
of overwriting) is a legitimate answer to a finding.

## 8. Implementation order

1. Done: `tare-ops explore` and `tare-ops evidence` in `tare_ops.py`.
2. Done: `tare-ops research --collect-only` in `relay/research.py`.
3. Done: map and reduce with the OpenAI client directly on the local backend;
   `--result` and `--ref`; `research` origin in telemetry.
4. Pending and optional: include the section 3 rule in the Kernel's role prompts
   when there is another reason to move the satellite forward.

Tests: `tests/test_research.py` and `tests/test_tare_ops.py`, with fetch and
model replaced; no inference. Real collection verified once against
`docs.python.org`, without map or reduce.
