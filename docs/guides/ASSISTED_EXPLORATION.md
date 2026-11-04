# Assisted exploration inside native flows

`/delegation` configures a read-only explorer for new conversations, plan/code
reviews and managed implementations. The parent can call `explore` for a bounded
investigation, then continue its own work. Existing sessions keep their saved
settings. Installation defaults to disabled; saving the toggle authorizes the
configured auxiliary for new sessions. This does not enable native delegation
inside provider CLIs: OS composes the existing Runtime sessions over any admitted
native profile.

## Configuration

Choose an explicit registered profile. A plain name is enough. For experiments,
the same variant format as challenger mode accepts reasoning, parameters,
task_types, serving identity and resource_pool. LoRA/template identity is checked
through the existing fleet binding; a label never proves an adapter is loaded.

Example settings, saved through the panel or `python -m relay.delegation
--configure settings.json`:

```json
{
  "enabled": true,
  "profile": {"profile": "codex-native-gpt-6-luna", "task_types": ["exploration"],
              "parameters": {"parallel_tool_calls": true, "reasoning": {"effort": "high"}}},
  "overflow": null,
  "calls": 10,
  "requests": 4,
  "seconds": 300,
  "max_usd": "0",
  "challenger": null,
  "sample_percent": 0
}
```

The explorer is always the configured one, with its own model, effort and
parameters, never the session's. A local explorer has one slot per endpoint in
the process; `overflow` names the hosted explorer that takes the investigations
beyond it, and all of them when the local one cannot run (the conversation
leaves the local GPU, or it would swap a local session's model). Without an
overflow explorer such a session does not delegate. A metered overflow needs a
positive `max_usd`.

`calls`, `seconds` and `requests` are ceilings. The session chooses how many
investigations to send and the breadth of each: `quick` (3 calls, 60 s),
`medium` (6, 180 s, the default) or `thorough` (10, 300 s), cut to these
ceilings and to the session's remaining calls. Measured on 2026-10-03, ten
questions with 6 calls each: gpt-6-luna at its profile settings answered 4
fully; with batching, high effort and the explorer's search rules, 8;
gpt-6-sol, 9.

Profiles must exist in the active installation's profile directory. All settings
apply to that operational storage root. `TARE_DELEGATION_POLICY=off` disables new
delegation bindings; an absolute JSON path selects an explicit policy instead.
Resuming a bound session keeps its original policy and provider realization.

The experiment sample assigns one alternative explorer to a fraction of parent
sessions. This is sampled routing, with no duplicate implementation or automatic
preference promotion. Use `task_types: ["exploration"]` when restricting a variant.
Measurements retain the exact variant key and parent session, including failures.

## Context, evidence and limits

- Each request supplies a repository, relative directory and concrete question.
  The child receives this question and applicable project instructions, not the
  parent transcript. Its file tools are restricted to the selected directory.
  Tool and evidence paths remain relative to the original repository root, so
  `relay/file.py` keeps the same meaning for both agents.
- The child may read/list/search, and returns a typed summary, sources and
  gaps. It cannot edit, execute commands, change the graph or spawn another child.
  The host fills each source's hash from the child's own read; a cited file the
  child never read becomes a gap.
- Reports are capped at 8 KiB. File references are mapped back to the parent's
  repository, and source hashes are rechecked before reuse. A report without
  sources returns its summary and gaps marked unverified; a child out of calls
  returns its trail (ranges read, search hits). An invalid/oversized report
  returns a gap to the parent.
- Each completed report carries `excerpts`: for every `path:line` or
  `path:start-end` its summary or gaps mention in a cited source, tare reads the
  lines back itself (two lines of context, at most 40 per reference, 3000
  characters per report) while the file still matches the recorded hash. Other
  parents need not read those lines again.
- The parent auditor must still read the decisive sources. Auxiliary references
  and excerpts do not populate the auditor's independent-read evidence.
- One `explore` call carries 1 to 8 investigations, run in parallel (a local
  explorer takes them one at a time). Each child is told the other questions of
  its call and asked to stay on its own. The same question and scope twice in a
  call runs once; asked again later in the session, the earlier report returns
  while its sources are unchanged. Other tasks retain their own allowances; this
  is not a global monthly budget.
- `serve/explore-overlap.jsonl` measures waste: per call, the investigations
  dispatched, reused and refused as duplicates, and the files read by more than
  one child (also returned to the parent as `overlap`); and each parent read of
  a file a child already read, marked `cited` when it checks a cited source.
  `serve/explore-judge.jsonl` records, in shadow, whether the judges find each
  report answers its question, and `serve/explore-tier.jsonl` which explorer
  (cheap or stronger) they would have sent it to, next to how it went. All keep
  hashes and sizes, never texts; nothing routes on them yet.
- Auxiliary generations and summaries consume the parent's call ceiling. A
  reservation persists through interruption. One parent call and ten seconds
  are left available when admitting a child; remaining-call notices encourage
  both agents to deliver findings and gaps before exhaustion. These notices do
  not guarantee completion by a model.
- Auxiliary time cannot extend the parent deadline; live cancellation and
  binding revocation propagate to the child.
- `max_usd` is the total auxiliary API allowance per parent session. When the
  parent also has a spend policy, auxiliary reservations reduce that allowance
  as well. Metered profiles need a usable tariff; unknown charges retain their
  reservation. Existing spend prediction limits still apply.
- Local compute and subscription quota are not converted to fictitious USD.
  A selected subscription profile permits bounded calls when its vendor balance
  is unknown; a known resource/quota admission failure returns to the parent.

## Code map and splits

Every explorer's instruction carries a map of its scope: classes and functions
with signatures and line ranges, the files most relevant to its question first,
about 6000 characters. Outlines come from Python's ast (other code files appear
with their size) and are cached under `explore/outlines` by size and mtime.

An investigation with `split: N` is covered by N explorers. tare shares the
scope's code out deterministically: files, and large files cut at top-level
definitions or methods, in path order and balanced by lines, at most 4000 lines
per explorer (a larger scope is refused). Each explorer gets its part, a map
that says who covers what, and the pieces of other parts its code calls most;
reading outside its part is allowed and measured. The result reports, per part,
the pieces its explorer read less than half of. Splits find what one explorer
misses at several times the tokens: use them when completeness matters.

An investigation with `candidates` covers its scope more cheaply when a pattern
narrows it: `{"search": "<ripgrep regex>"}` lists each matching code line,
`{"definitions": "<regex>"}` each function or class whose name matches. tare
shares the candidates among `split` explorers (default 1, at most 40 each), in
path order with each file whole; the explorers judge them and return
`locations` (repository, path, line, claim). tare keeps the locations whose line
exists, merges them across explorers, and reports the candidates no explorer
located or read. A search that leaves a few dozen lines to judge is where this
pays (all 6 blocked endings of a 3000-line package for 3 calls); when every
function body must be read, one thorough explorer reading whole files costs less.

### Candidate decider and exploration experiments

Each setting can be switched in `/delegation` or with
`python -m relay.delegation --experiment NAME=VALUE` (`--experiments` lists
them). They are saved next to the policy in `experiments.json`, so an older
build keeps accepting the policy. `TARE_EXPLORE_DECIDER` overrides the decider
for one process. On by default:

- `decider=jev` chooses which candidates explorers judge when there are more
  than 40 per explorer: hosted Jev's probability that the candidate, with its
  enclosing definition and two lines around it, answers the question. `rule`
  ranks by the question's words in the path (3), the enclosing definition's
  name (2) and the line (1); it also breaks Jev's ties and takes over when Jev
  does not answer. The rest are reported as `unjudged` in the coverage and told
  to the explorer, never dropped silently. `shadow` logs both and changes
  nothing; `off` restores the refusal.
- `ordered_candidates` lists each explorer's candidates best first and marks
  those Jev scores 0.9 or more `[tare: likely]`, a reading order: the explorer
  still judges every one.
- `instruction_flags` asks Jev whether a comment, docstring or Markdown line
  reads as an instruction (at 0.9 or more), judging the line alone; the
  explorer sees it marked as data, and the coverage reports it.

On by default since 2026-10-05: `jev_first` (with Jev answering and at most 10 candidates beyond the lines it
scores 0.9 or more, no explorer runs for a candidate investigation: those lines come back as locations and the
others with the lines around them for the session to judge; a larger set goes to an explorer, and asking the
same question again sends one; a local session, such as the TUI's Qwen 3.8 27B, still sends its candidates to
an explorer, because only a hosted session was measured judging the lines; turn it off in the exploration
experiments). An experiment can take a value per class of session route (`local`, `quota`, `usd`) ahead of its
global one: `jev_first` is off for `local` by default, and `python -m relay.delegation --experiment
local.jev_first=on` changes one class only. Off by default: `candidate_windows` (each candidate comes with the lines around it as tare read them, best first
within 7 thousand characters, and the code map is short; a report located from those lines alone has their files
as sources at the hash tare read), `adaptive_k` (with Jev, only the candidates it scores 0.9 or
more, when at least 10 per explorer are), `contradiction_flags` (a candidate that
contradicts a premise of the question; each is judged alone, so two passages
that disagree are not flagged unless the question states one side),
`markdown_candidates`, `location_status` (supported or hypothesis per location,
and a conclusion), `previous_locations` (`{"locations": "previous"}`) and
`specgraph_candidates` (`{"references": "<symbol>"}` from the repository's
published SpecGraph index, which exploration reads and never builds).

Exploration asks Jev alone about up to 40 candidates per request: each is a
yes/no question over one shared state that states the question and the criteria
once, with one block per candidate (its enclosing definition and two lines
around it). Every candidate a search may list is judged (up to 400), all
requests at once: 60 candidates took 0.6 s, 172 took 0.64 s, 358 took 1.1 s, at
100 to 150 input tokens each in code (US$0.042 per million). A request's fixed
cost, about 300 tokens, is paid once per 40 candidates instead of once per
candidate. Comments, docstrings and documents are asked about instructions in
a request of their own, each line alone. The resident local judge
(LitJEV on `qwen38-gsq`) answers one request at a time, about 250 ms each and
4.5 per second whether 1 or 16 are sent at once, so it serves single decisions
(the turn tier, an answer that only announced work, audit triage) and not
exploration's dozens of candidates. It agreed with Jev on all 6 single
decisions tried and on 74 of 78 candidate verdicts (2026-10-04).

Packing was measured on 2026-10-04 over 8 tasks and 3 rounds against one
request per candidate: 0.30 times the tokens (142 against 474 per candidate),
the same recall at 10 and 40, and the candidates scored 0.9 or more were
answers 86 to 90 % of the time against 27 %, which is what makes
`[tare: likely]` useful. Merging a file's candidates into one window ranked
worse (recall at 40 of 0.88 to 0.92), and so did putting each candidate in its
own question instead of the shared state (likely right 56 %).

Measured on 2026-10-04 over 12 tasks (this repository, the agent runtime and a
synthetic exporter with a planted contradiction, misleading name and
adversarial comment): every search's candidates covered what the questions
needed. Jev put an answer first in 10 of 11 tasks, the rule in 3, and was never
worse at 10 or more; it put the real cancel before the misleading one and the
planted comment outside its top 3. Real explorers given each arm's top 10
located 16 of 20 answers (Jev) and 15 (rule), with 5 and 7 wrong places; all
said their coverage was partial. With adaptive K, explorers located as many
answers as with 40 (22 of 23, 5 wrong places each) for 0.86 times the input
tokens, short of the 0.85 pre-registered, so it stays off. A Jev that does not
answer is asked once per decision: after one unreachable or busy answer (3 s at
most) the rule decides the rest. No other question, context window (0, 5 or 10
lines), score fusion or several candidates per request ranked better.

After each explorer, Jev checks its citations in one request: for every
location, whether the passage tare reads at the cited line (two lines around
it) shows what the claim states. Each location is marked `verified`:
`supported` at 0.9 or more, `unsupported` at 0.1 or less, otherwise
`unverified`, and nothing is removed; a citation whose line reads as an
instruction stays `unverified`. The parent sees the marks and the counts
(`citations`). The same request carries the sufficiency shadow, which only
logs. Measured on 2026-10-04 on 18 live reports, each against the tree its
explorer read: all 100 citations were true, Jev marked 80 supported and 20
unverified, none unsupported; with the same citations moved to unrelated lines
it marked 96 % unsupported and none supported. One request per report, about
1400 tokens and 0.33 s. Audit findings get the same check as a measurement only:
the five archived ones scored 0.51 to 0.63, since a finding usually describes
something missing, which a cited passage cannot show.

Explorer time is mostly the model: on 20 live episodes, 85 % of the wall time
was gpt-6-luna generating (15 s per call at the median, 2 to 3 calls), the tools
and tare the rest. Candidate windows were measured on 2026-10-04 over 5 tasks
and two repetitions: at high effort they cut the time per episode by 32 % and
the input tokens by 40 % (2.3 calls instead of 3) and located 42 of 46 answers
against 39 of 39, all four misses on the documentation task, where long lines
leave few windows within the budget and the explorer stopped reading; at
medium effort time fell 63 % but 33 of 39 answers were located. They stay off.
Claude Sonnet 5.5 at low effort through the Claude Code CLI could not explore:
in 10 of 11 episodes its first reply tried to call the explorer's tools directly
instead of returning them in the structured reply, which the CLI ends.

Jev first was measured on 2026-10-04 with gpt-6-sol at medium effort as the
session: given the confirmed lines and the next 10 with their lines, it named
every answer in 8 of 8 tasks (27 of 27, 2 wrong places) in one reply of about
11 s and 4.5 thousand input tokens; allowed to read and search more, it found
the same answers with 2.75 calls, 26 s and four times the tokens. Jev alone,
taking only what it scores 0.9 or more, was always right but found 32 of 53
answers.

Measured again on 2026-10-05 in production: a gpt-6-sol session at medium
effort with the serve read-only configuration asked explore itself, 12 tasks,
3 runs each, against today's default (Jev orders, gpt-6-luna at high effort
explores). Showing at most 10 candidates whatever the set, Jev first found 125
of 158 answers against 149 of 155, and on the largest set (178 candidates) the
session asked for the rest twice and stopped without an answer. Limited to sets
it shows whole (5 of the 12 tasks; the others went to the explorer), it kept
150 of 157 answers with 17 wrong places, as the default did, with 23 explorers
instead of 40 and 28 instead of 38 thousand explorer input tokens per run.

Every decision is logged to `serve/explore-decider.jsonl` under the storage root
with the question and pattern hashed: candidates as `path:line`, each arm's
scores, the overlap of their top sets, the flags and the time it took. The
digest summarizes it with the location statuses and which experiments are on.

## Crash recovery and measurements

The parent saves a dispatch receipt before starting the child. Reopening uses
the completed child outcome/artifact, or its terminal delivery and observed
reads when artifact/outcome writes were lost. It rechecks the sources and never
automatically reruns an unfinished explorer. Recorded child call intents reclaim
unused call slots after interruption; uncertain money retains its reservation.
The parent can continue with its direct tools.

`python -m relay.delegation --digest [DAYS]` (default 7), also the first row of
the Measurements button in `/delegation`, summarizes per day, explorer, effort
and breadth: investigations, completed, calls, input and output tokens, then the
waste measurements and the judges' shadow verdicts. Watch the tokens: each call
resends the explorer's history, so input grows faster than the call count.

`/usage` includes parent and auxiliary sessions with their own provider contracts
and prices. The Measurements button in `/delegation`, or
`python -m relay.delegation --report`, links recent investigations to whole-parent
usage and the observed parent outcome. Missing prices and qualification remain
explicit. The parent verdict alone does not independently qualify the explorer.
Full managed delivery comparison also includes children in each recorded stage.
Provider continuity retains prior usage and subtracts auxiliary calls from the
remaining call allowance; the successor currently uses direct tools.

Use equivalent tasks and accepted outcomes to evaluate savings. Compare the
whole implementation/review/correction journey, elapsed time and quota use.
Do not infer an economic winner from a successful helper or a price-per-token
comparison. No new metered experiment budget is enabled by this delivery.

## Verification

### Haiku 5.5 fail-fast TUI trial (2026-10-12)

Five bounded tasks used the public TUI through Windows ConPTY and keyboard input,
with real Claude Code inference: explorers requested low effort, implementers high.
Each task used a disposable Git worktree and its own history and permissions. No
operator repository material or operational backlog was part of the fixtures.
The tasks follow the style of the Luna exploration and Windows journeys above;
different fixtures and budgets prevent a direct model ranking.

| Case | Observed result | Model calls |
| --- | --- | ---: |
| Routing trace, low | First delivery omitted available contract/test bodies and proposed changing a protected test; failed task acceptance | 3 |
| Same routing trace, low | Passed after the deployment-specific evidence-reading hint, with the same request, baseline and six-call limit | 4 |
| Routing implementation, high | Only the three permitted files changed; 16 frozen tests and documented examples passed | 6 |
| Atomic-batch trace, low | Correct static diagnosis of partial mutation, ignored dry run and shared output, with contract/test evidence | 3 |
| Atomic-batch implementation, high | Atomic validation, dry-run preview, Unicode duplicates, version guards, bool rejection and independent output; 35 frozen tests passed | 5 |
| Windows process lifecycle, high | Exactly three commands: literal UTF-8 read/exit 7, successful script/exit 0, one-second timeout/exit 1; same handles and cleanup verified | 7 |

The first semantic failure paused the sequence before implementation. The only
product change was an append to the Haiku explorer's own prompt capsule: search
hits locate evidence, then the explorer reads the governing contract and relevant
test bodies before calling the behavior uncertain or proposing a fix. The base
role text, implementer guidance, provider codec and admission rules were retained.
The independent batch diagnosis also passed with this hint. Session guidance is
frozen at creation; existing sessions keep their recorded capsule.

The complete sequence used 28 of 48 authorized calls, including the failed attempt
and replay. Limits were 120 seconds per request and 420 per case; no model retries.
The controller stops on an unexpected provider/tool failure and requires source,
receipt and protected-file acceptance before advancing. Expected failing baseline
checks and explicitly requested exit/timeout outcomes are separately recognized.
Preparation and acceptance used no model calls. Journals, terminal transcripts,
fixture commits and evaluations stay in the operator's private evidence custody.

This is a small, repaired dogfood sample. It supports the measured explorer hint
and keeps high-effort implementation experimental. It does not settle general
reliability, automatic implementer admission, cost superiority or task equivalence
with Luna. See [Windows command guidance](WINDOWS_AGENT_COMMANDS.md) for host facts
and process semantics.

Tests exercise real native codecs and Runtime journals with controlled providers:
context separation, shared calls and spend, cancellation, invalid scopes, source
changes, independent auditor reads, managed implementation, child completion
before a parent crash, terminal-before-artifact recovery and shared compaction
admission. TUI tests cover
80×24 and 130×44 terminals. The raw evidence of the live journeys is not published.

Backlog: `assisted-exploration-20260924`.
