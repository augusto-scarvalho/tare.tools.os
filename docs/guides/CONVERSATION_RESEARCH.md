# Web reading and the internet researcher

For requests to use configured search services, the coordinator calls `search_web`
with `provider=deepseek`, `serpapi` or `firecrawl`. No API client, code project or
planner is needed to use these existing tools. `product_help` with `topic=web`
reports which services have a key and are allowed, without exposing credentials.

Questions about Tare itself use the local `product_help` contract for workflow,
permissions and backlog setup. Project file tools inspect the selected project;
internet research cannot establish the behavior of a private installed product.
The coordinator delegates execution even with no direct write scope. The host
asks for any missing worker permission in the conversation, after a saved plan.
`/plan` produces a plan; creating a graph and binding it are separate outcomes.
Named verification can reuse configured checks and the saved plan's host graph
validator without opening a demand, replanning or admitting edits.

When no explicit checks are configured, conversation execution reuses Tare's
bounded project-test detector. For a Godot project with `tests/smoke.gd` and an
installed engine, it imports resources in the private candidate and runs the
headless smoke test before independent review. New script UID metadata generated
by the host import stays in the private verification baseline, outside the applied
source patch. Existing metadata and source changes remain visible to review.
Configured scoped checks take
precedence. `product_help` with `topic=workflow` lists available check names.
Saved coordinator tool definitions and the immutable planner artifact stay intact.
If later checks fail, the earlier applied patch remains historical evidence;
its old green result cannot replace the new diagnostic. A verification-only
message does not authorize edits. A later correction request can reuse the plan.
An unavailable route that never started execution cannot turn an older failure
receipt into the current result or prevent trying the configured default route.

Backlog help includes a nonempty example produced by the installed owner adapter
and checked by its validator, with the real status values and reference shape.
The host also supplies that contract directly to a graph implementer, so missing
coordinator notes never require reading the installation or asking the user for enums.
Example tasks and timestamps are placeholders, not observations about the project.

A failed candidate keeps its captured application basis and exact check feedback
for a narrow correction. Older patches without a basis are advice, never silently
applied. Two failures under the same host contract stop further execution; a real
owner-contract change admits bounded requalification. Renaming tasks, changing
notes or selecting another model does not reset the guard. Saved coordinator
toolsets stay frozen; new chats expose `folder_list`, `folder_read` and
`folder_search` for folders explicitly granted by the user.

An independent review has at most eight model calls and five minutes, within
the enclosing conversation's remaining budget. It uses the selected model's
context window, output reserve and compaction fractions, and can use one summary
within those same limits. Window/output sizes are not copied from the coordinator.
An optional deployment field `native_context_policy` declares `trigger`, `target`
and a required `source`; fractions describe the window, capped by the actual input
space after reserving output. Without a declaration, Tare uses trigger 0.8 and
target 0.5, labelled as Tare defaults, not a provider recommendation.
`tare profiles show <name>` reports these values and their source without secrets.
New sessions freeze the model policy; saved sessions retain their original policy
and prompt history. Explicit session policy overrides remain authoritative.
Context-admission failures include configured limits and separate measured and
estimated counts. They do not establish provider quota exhaustion. One explicit
later recovery can retry a completed operational failure with the same auditor
and unchanged candidate. Context failures additionally require an actual change
to the host admission/counting contract; another message, route or note cannot
renew the same failed review. Accepted/revised/rejected decisions remain final
for that same candidate across host-policy changes. Remote auditors reserve the
last finite call for an honest verdict and remaining obligations.
New remote auditor sessions request their final call when half of their original
time budget remains. The deadline and call ceiling
stay fixed; this is an opportunity to conclude, not a guarantee of a verdict.

Demand continuation and tool refusals report current host recovery facts alongside
the historical failure. An unchanged goal keeps its saved plan and candidate;
correcting context admission does not require splitting the delivery or starting
another planner. These observations append through ordinary tool results, without
rewriting frozen role instructions or granting execution authority.

A valid CLI response can still request the wrong action when delivery is required.
That failure is `CLI_REQUIRED_ACTION`, with the actual auditor model and retained
usage, rather than a claim of malformed JSON or exhausted context. A budget or
closing-hint change cannot repeat that candidate's failed final choice. Legacy
`NO_PROGRESS` is reclassified only when its saved transport proves that exact
failure. Status questions receive these current diagnostics without running a
planner, implementer or another review.

The bounded real Argon CLI review on 2026-10-25 established cache reuse and context
admission, but did not establish a valid final audit. The candidate remains
unapplied. Closing guidance alone was insufficient; a separate strict-grammar
diagnostic reached its 120-second deadline with usage unknown. Neither result is
an accepted/revised review or a successful end-to-end recovery qualification.

Give the assistant a URL in an ordinary conversation. The coordinator can use
`read_url` to read it through the existing Trafilatura reader, without another
model or a delegated researcher. Returned text includes the source URL, title,
content hash, fetch time and actual access errors. Long pages have line ranges.

`read_url`, the host's reading of the sources a researcher cites, links in a `tare serve` or Copilot request,
`/research` in `tare serve` (its links, the links on those pages and free-search results) and the sources of a
research flow reach only public web addresses, even when you typed the URL yourself.
`localhost`, private and link-local addresses (including `169.254.169.254`), `100.64.0.0/10` (Tailscale),
unique-local IPv6, multicast and reserved ranges, and names ending in `.local`, `.internal`, `.localhost` or
`.ts.net` are refused, in any notation. The reader checks every connection it opens, so a redirect or a name
that resolves to such an address is refused too. A refused read says so and is not cached. Your own dev server
(`http://localhost:3000`) cannot be read this way; system proxies are not used for these reads. Text in a URL
still reaches the public host it names.

Ask a question that needs source discovery or comparison and the coordinator can
use `research`. This is a separate internet role. The repository explorer still
investigates project files; neither role replaces the planner or delivery audit.

## Connections

The product declares and qualifies its research report validator during installation.
A missing tool dependency is returned as a tool failure; it does not interrupt
the conversation or authorize automatically repeating paid research.

The Researcher entry in `/routes` sets the connections and fallback order. Defaults
are GPT-6.1 Luna through Codex, Haiku 5.5 through Claude Code, then Gemini 3.8 Flash
through AGY, at medium effort. Only configured, enabled CLI connections are used.
The CLI's login is reused; ambient API keys are removed from its environment.
This does not establish that every account has access to every named model.

An authoritative model or quota rejection before generation can select the next
connection. A failed, interrupted or uncertain generation is returned as a failure,
without automatically spending on another provider. An explicitly requested
connection is never silently replaced. GPT-6 Luna is not a substitute for 6.1.

The researcher has web tools, without project files, shell, editing, browser
actuation or recursive delegation. Execution uses the Kernel's managed process
and the parent's cancellation/deadline. The CLI deadline is at most 150 seconds;
bounded child call reservations share the coordinator's budget. Unknown CLI call
counts keep their reservation rather than claiming zero. Vendor-internal retries
and cache behavior remain vendor-controlled.

## Sources and limitations

Reports contain concise findings, source URLs, short exact excerpts, inference
labels and remaining questions. The researcher supplies short literal locators;
Tare fetches the URLs, locates those anchors and reads the surrounding source
lines itself, as it does for repository exploration. Models do not compose the
quotations. This establishes where an excerpt came from;
it does not independently audit the meaning of every claim. Unavailable sources
or unmatched anchors produce a partial result, not invented evidence.

The coordinator distinguishes observations, document claims and suggestions.
A README saying a feature is implemented is not evidence that its tests passed.
Missing tools, a rejected model and an HTTP error are different diagnoses; none
alone establishes that the computer is offline.

## Reuse and storage

Repeated questions reuse recent reports. Related questions resume the same
researcher connection, with stable role instructions and tools. Session reuse is
recorded separately from actual cache hits: only reported usage establishes hits.
Missing usage remains unknown. Research receipts identify their own usage scope;
the coordinator usage file alone is not the total cost of delegated research.

Each conversation keeps at most eight cleaned pages (64 KiB each) and eight full
reports, reusable for one hour. Older compact receipts retain accounting. Full
CLI output and a second website corpus are not retained. The ordinary conversation
journal remains the destination of the findings; the CLI owns its native session
history. No source downloads or model downloads are scheduled in the background.

New conversations receive these tools. Saved conversations preserve their frozen
role, tool definitions, history and model connection on resume; a software update
does not rewrite their prompt prefix or pretend they received new capabilities.
