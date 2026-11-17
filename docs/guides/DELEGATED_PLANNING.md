# Delegated planning

New TUI chats always use a separate planner for changes. Open `/delegation`,
choose **Choose planner model…**, and save. This selects a planner for new native conversations
without changing their coordinator model. The planner, like the implementers a
conversation delegates, may run on any provider or allowance, whichever account
the coordinator is on: `--subscription_only`, `--api_only`, `--local_only`, the
routing profile (`tare setup --auto`, the "Models per role" matrix) and pins
limit it. Existing conversations keep their saved tools/routes.
Without a planner list of your own, the planner is chosen from the role table, the
routes switched on for it in "Models per role": the ones that reach the class the planner
asks for first (frontier unless you choose another in `/routes` > Order, "What class of model each
role asks for"; a route above it goes after the exact one, one below it last), then a lapsing allowance, then subscription before metered API. A `/routes` planner
list you save replaces that choice and keeps its order; the model picker uses the
same configuration for new planning flows.

Open **Review settings…** here, or `/routes`, to choose **Automatic · another
provider, frontier models**. Formal PLAN reviews, automatic Work audits and code
turn audits share the same role admission and independence checks. The planner
is the producer of a plan; the implementer is the producer of a code change.
Changing the planner does not make that model eligible to independently audit its own work.
The preview names the current plan reviewer; live eligibility is checked at
dispatch. Delegated planning advice continues to be assessed by the coordinator;
this setting chooses the auditor for existing reviews and adds no review stage.

The automatic pool admits Sol 6.1, Astra 6, Opus 5.5, Fable 5.1 and Gemini 4 Argon,
using the installed Codex/Claude/AGY connections or enabled native HTTP APIs.
Another model vendor comes first, then a route that reaches the class the role wants
(the planner wants a frontier model; a reviewer wants the class below it, Sol, Opus or Argon, which checks code and plans as well, and takes a frontier model after those on the same allowance; the class of a route is
the one `tare models` shows, from `relay/model_signals.py`), then subscription allowance
before metered API. No order of models is written by hand.
A model with no line (Kimi, Grok, GLM, DeepSeek, Qwen...) is placed by the lines tare knows, in the same public
benchmark reading: for each known line the newest version listed, at the highest effort listed, is an anchor with the
line's own class, and the model is in the highest class whose weakest anchor it reaches (below the weakest middling
anchor, cheap). It is placed only when the route's model matches exactly one row (an OpenRouter route by the row's
permaslug, any other by name and vendor) and the two latest readings of one source agree; otherwise it has no class and
counts as cheap, as before. `tare models` says it as "middling: reaches Gemini 4 Argon on openrouter, 2026-11-16".
Where a person reads the lowest class (`tare models`, the routes matrix, the plan shown for approval) it is shown as "basic", because the name `cheap`
read as a judgement of price or quality: the class is the one enough for simple work, and a model in it may be dear. Only the shown word changes: the plan's `needs: cheap`, receipts, `--json` and what a model is told keep `cheap`.
A model that is the best in general may know nothing of CAD or 3D, so tare also keeps a catalogue of task types
(`relay/task_types.json`: software engineering, web and front-end, mathematics, physics and natural sciences, legal,
consulting and finance work, cybersecurity, engineering, CAD, and types with no source yet, among them art and audio made by code, which no public board measures) and, for each, an ordered
list of public boards: LMArena configs, CSV files of Epoch AI's benchmark archive, and four leaderboards that exist
only as a public page (CWE-bench, Vals AI LegalBench after APEX-Agents, BenchCAD, and Mercor's Physical-Engineering
leaderboard). Each board is read again once a day with the same rule as the
general reading (no key, no model call, nothing on a machine with `tare setup --no-use api`), kept in
`serve/task-boards.json` with its own read date, and a board that fails keeps its last reading. A model's class for a
type comes from the first board that places it. On such a board the known lines are a ranking, not their general
labels: a line that leads in general may be weak at one type of work, and as its class's weakest anchor it would drop
that class's floor. The lines split 3 : 6 : 3 in general, so frontier is the top quarter of the board's lines,
middling the top three quarters, the rest cheap. A line counts only at the newest version any board holds, and a board
places a model only with at least 8 of the known lines: an aged board, or a page that lists few of them, places
nobody and says so; no board that places it gives the general class, marked general. A board near its ceiling, where
a cheap line outranks frontier ones, is no source (Epoch's OTIS Mock AIME and GPQA Diamond were removed for that). `tare models --types` shows each source's read date
and how many known lines it holds; `tare models --type math` is `tare models` with a class for that type, for
example "middling for mathematics (Epoch FrontierMath tiers 1-3, read 16 Nov)" or "frontier (general: no board places
it for CAD)". `tare models` and the routes matrix also show each route's own record as implementer on this machine
per type of work its units named ("audio work: 3 of 4 passed, 2 of 2 audits accepted"; a unit of several types counts
in each; a unit that names a type and no `needs` is counted too, though such a type ranks no route): raw counts that
steer nothing, and the only measure of a type no board covers. An Epoch row is matched like any other source's: a model of a known line by its slug and its effort, any
other by name and creator. A page is read from the one URL the catalogue names, once a day like the rest: reader
`page` takes the first HTML table whose header row holds the source's `model` and `column` headers (and its
`organisation` one, when it has it), reader `script` takes the rows of a JSON `<script id=...>` at the catalogue's
dotted `rows` path, with the `model`, `column` and `organisation` paths in a row (Mercor's page has no table), and
reader `json` takes the same paths from a data file that is JSON itself, with the file's own date at `as_of`. A
page that states no organisation takes a known line's name as its vendor's model, and a name with no effort bracket is
a row with no stated effort; a page whose table or columns moved is a failed reading, so the saved board stays. Adding
a type, or a source of a known reader (`arena`, `epoch`, `page`, `script`, `json`), is an edit of the catalogue. A
source says what its numbers are (`kind`): `outcome` for measured results, `preference` for votes, `reported` for
scores each lab reported of its own model (medicine's only source, HealthBench Professional, is such a collection:
`tare models --type medicine` says "as each lab reported it").
The planner may name the type of a task (`kind`, one id of the catalogue, beside `needs`; left out when the task is none of them or mixes several). A unit's implementer routes are then ranked by each model's class for that type where a board places it, else by its general class, exactly as with no `kind`; a machine with no saved boards ranks as before, and a pinned route or one the coordinator chose is never reordered. The unit takes the type of its hardest task (the one whose `needs` decides), and only a unit with a `needs` is ranked. Its own `needs` and `kind` stay that way for every reader that knows only them, and a unit whose tasks state different hints also keeps each task's own `{needs, kind, critical}` as `parts` (each once, only the keys a task states; none when the tasks state one set of hints at most): see below. The outcome keeps `kind` and `class_source` (the board's name, or `general`) beside `needs` and `route_class`, and a delivery that fell short of its need says the type in its class note. The auditor of a unit's change is judged the same way, by its class for the unit's `kind` against the class a reviewer wants, and only falling short moves it (an auditor better at the type than a reviewer needs keeps its place: "the exact class before one above it" stays a matter of the general class), and only among the auditors the automatic choice admits: an auditor of the implementer's model or bias domain is still never chosen, another vendor still comes first, and a pin or the owner's auditor list keeps its order; each candidate of that selection says `kind` and `class_source` beside `classes_short`.
Before any plan exists, the coordinator may name the demand's types of work (`kinds` of the `demand` tool: up to 6 different ids of the catalogue, for a new or revised demand, kept with it as its objective is; a continued or corrected demand keeps what was recorded, and one recorded before the field has none). The conversation's planner is then judged by its weakest type: a route that falls short of frontier at any of them goes after the routes that reach it at all of them, and, as for the auditor of a change, only falling short moves it (the other terms keep the general class). Only tare's automatic choice is ordered, when `delegate_plan` starts: the planner routes the session froze, in the order `native_routes.ranked('planner', work_type=[...])` gives them for the types then (a route it no longer ranks keeps its place after them); a pin (`--planner`), a route the coordinator chose, a temporary policy (`--prefer_*`, `--*_only`, `--pro_planner`) and a planner list the owner saved are untouched and read no board, and a demand with no types chooses as before. `native_routes.reviewers(planner, 'plan-auditor', work_type=[...])` ranks a plan's reviewers the same way, with independence, another vendor first, a pin and the owner's auditor list as before; a conversation's plan with tasks has a reviewer (below), and that review passes it the demand's types.
The planner may also mark a task `critical` (a wrong result would be costly, or hard to notice or undo: security-sensitive code, money, a data migration or deletion, a public interface others depend on, a result nobody will check; left out for ordinary work). The unit is critical when any of its tasks is, and its implementer routes then go best first instead of cheapest sufficient first: by each model's class for the unit's `kind` (the general class with no `kind`), then a route a board places before one it does not, then the higher score on that board among routes one board places; the existing order (allowance, then cost) only breaks ties, and `needs` no longer reorders it. A route that cannot run now is refused as before and the next best takes the unit; a pin and a route the coordinator chose are never reordered. The outcome and the delivery keep `critical` beside `needs`.
Every conversation plan that has tasks is also read once by an independent, read-only "frugal critic" (measured on 2026-11-17: a plan whose sort key contradicted the request passed every test and the delivery audit, because tests and auditor both follow the plan; the critic found it, and three planted defects, and accepted the three sound plans it saw without a finding). It runs inside `delegate_plan`, right after the planner returns, and only then: a plan with no tasks, and every other flow, makes no extra model call. Nothing the planner states about its tasks (`needs`, `critical`) decides whether the plan is reviewed, because a planner that underrates its own task must not excuse its review (a weak local planner rated every task `cheap` in 2 of 15 plans for one request, and both plans carried the defect). The reviewer is chosen as `/plan` chooses its plan reviewer for that planner (`native_plan.select_route(..., 'plan-auditor', ..., producer=planner)` with the demand's `kinds`: the role table, independence, another vendor first, pins, the owner's auditor list, `--only`/`--prefer` and the routing profile's switch); the planner never reviews itself, so with no other eligible reviewer there is no review. One `plan-auditor` session (shown as "Plan review"), typed `Review` verdict, capabilities read, list and search only, sees the project and a read-only folder `proposal` with `plan.json` and `request.json`; its instruction is the measured text `CRITIC_BRIEF` in `relay/planning_tools.py`. It has its own ceiling of 12 calls and 600 s inside the shared budgets, charged to the planner's receipt like the planner's own calls and money; if the shared budget cannot fund it, there is none. It never blocks: whatever happens (no reviewer, switched off, budget, a failed or cancelled session, an invalid verdict) the plan returns as before with `review.status` `SKIPPED` or `FAILED` and a short `reason`, and the review is kept in `receipt.json` (`planning/<job>/review/` holds its session), so a restart or a second read of the same plan never pays for another one; a replanned demand may be reviewed again. The coordinator reads `review` (`status`, `verdict`, `findings` with `claim` and `paths`, `route`, `model_calls`, `independent`); with findings, `next` tells it that they are advice, to tell the user, and that it may ask the planner for a revised plan. When the verdict is REVISE with findings, the same planner route revises the plan once, automatically (measured on 2026-11-17: the findings removed the defect only when the planner revised its plan with them): a new planner child in `planning/<job>/revision/`, handed the first plan as advice (as a replan is) and the findings' claims in `critic_findings` under the instruction `CRITIC_REVISION`, funded like the first planner call (the shared call, time and money ceilings, `local_slot`, cancellation, the planner's own 6 calls and 600 s) and charged to the same receipt. The revised plan, and its artifact, are then what `delegate_plan` returns and what the plan is compiled from; `review` keeps the first plan's verdict and findings and gains `revised: true`, `revision_route` and `revision_model_calls`, and `next` says the planner revised the plan once with them. The revised plan is never reviewed again, so nothing loops. If the budget cannot fund the revision, or it fails, is cancelled or returns no tasks, the first plan returns with its findings (`revised: false`, `revision_reason`). The user's "Implement this plan?" dialog shows the verdict and each finding first, above the plan (for a revised plan: "Independent review of the first plan: REVISE", the findings, and that the planner revised the plan once with them, the plan below being the revised one); an accepting review adds one line saying a reviewer of another vendor accepted it (or that it was the same vendor, not independent), and no review adds nothing.
A unit with `parts` goes to the weakest link, still one implementer: the number of classes a route falls short is the largest over the parts that state `needs`, each judged for its own `kind`, so a route that reaches every part goes before one that is short on any, whatever the cost order (the cheapest sufficient first, as before), then those one class short on their worst part, and so on. A critical unit is ranked on the critical task's own `kind`, not the hardest task's, and with several critical tasks a route is as good as its worst one (ties by the weakest part); the effort raised toward a board's is the highest over the parts' types, and a stated `effort` still replaces it. The outcome records the lowest class the route has over the parts as `route_class`, with that part's `kind` and `class_source`, and the unit's types as `kinds`. A unit without `parts`, a pin and a route the coordinator chose are ordered as before. Splitting a plan across several implementers is not done.
A task may also state its `effort` (low, medium, high or xhigh; never max, which neither tare nor the planner chooses: the user does, with an implementer pin at `@max` or by telling the coordinator; left out unless it clearly needs more thought than usual, or is mechanical). The unit runs every automatic route at the highest effort any of its tasks states, even lower than the route's own (a level a route does not list becomes the highest level it lists below it, from the same list the model picker shows; a route that lists none at or below it, or lists no levels, runs at its own; an AGY route's level is its model id, so it changes level by taking the id AGY lists for that level, low, medium or high, and keeps its own for a level it has no id for). With no stated effort and a `kind` that a board placed the model for, the route is raised toward the effort that board read the model at, but no higher than `high` for an ordinary unit and `xhigh` for a critical one (boards read models at `max`, too dear to run every unit of a type at; owner, 2026-11-16), two ceilings you may move in `/routes` > Order, clamped to the levels the route lists, and only when that is higher than the route's own, never lower. A stated effort has no such ceiling. A pin, and a route and effort the coordinator fixed, are untouched, and so are the conversation's own effort, the auditors and the planner. The plan shows it beside `needs`, `kind` and `critical` (`Model: ...; effort high`); the outcome and the delivery keep `effort` and `effort_from` (`plan`, or the board's name) when the unit ran at that effort (the level actually applied).
Among pay-per-token routes (not subscriptions, free credit or the planner) that tie up to the kind of billing, an implementer, a conversation or an auditor takes the best value first: the score of the model above the lowest score on the LMArena agent board, per dollar of a task, both from one row of the board (a model of a line at the route's own effort when the board lists it, else at the highest it lists). The board is read beside the class reading, at most once a day, and kept even when OpenRouter gives the class; a route it has no single row for, or no cost, keeps its place after those that have one. `tare models` and the matrix say the figure: "about US$ 0.14 per task on the LMArena agent board, 2026-11-15".
Codex Direct precedes its CLI alias to preserve the native cache route. Pro API
modes, small helpers and older models are not automatic frontier reviewers.
Unavailable credentials, invalid local Codex login, disabled connections and known
quota exhaustion exclude a route. An API key does not prove remote authentication
or a known balance; missing cost remains unknown. The selection itself uses no LLM.

**Use my auditor routes** retains an explicit list and the independence gate.
Previously saved auditor lists stay manual until their owner chooses automatic
review. An independent auditor is one of another vendor. Another bias domain of
the same vendor (Opus for Fable, or AGY's Claude for Claude Code) is chosen only
when no auditor of another vendor is eligible, whatever the class, tier or a
subscription/API preference; in an owner's list another vendor also goes first.
Such an audit runs and its verdict counts as before, but it is never called
independent: its selection reason is `SAME_VENDOR_NOT_INDEPENDENT`, and the
conversation audit lines, the review result, a Work's result and the preview say
"audited by the same vendor: not independent" (a plan review: "reviewed by the
same vendor: not independent"). A pinned or explicitly named auditor of the same
vendor is your choice: it runs, with that label (`landing_policy.independence`).
Another vendor is a diversity policy, not proof of unbiased judgment.

You can turn "another vendor first" off: untick **Put auditors of another vendor
first** in `/routes` (saved as `"any_vendor_auditor": true` in the routing policy,
absent while ticked), or launch with `--any_vendor_auditor` for one TUI and its
children. Either one turns it off, in both reviewer modes and for plan reviews.
Then the automatic order is the class the route falls short of, a lapsing allowance,
cost tier, primary before reserve, CLI backup and table order (selection reason
`AUTO_COST_AND_CONFIGURED_ORDER`); your own list keeps its order
(`OPERATOR_AUDITOR_ROUTES_IN_ORDER`) and is replaced by the automatic choice only
when a temporary preference sets all of it aside. Nothing else changes: the same
model or bias domain is still refused (`AUDITOR_NOT_INDEPENDENT`), and a same-vendor
audit is still `SAME_VENDOR_NOT_INDEPENDENT` and labelled as above.
Saved formal-plan reviews and their retries retain the original exact profile;
changing settings never resamples an accepted or rejected verdict.

The default is a preference when an independent reviewer is available. If none
is eligible, PLAN retains its existing separate-session planner review and marks
it as **not independently attested**. It never promotes that fallback to an
independent audit. Choose **Require an independent plan reviewer** (CLI:
`--reviewer-deployment auto`) to block before spending when no eligible route
exists. Canonical Work admission gates remain unchanged; a code-turn review with
no independent candidate remains explicitly unaudited.
New [coordinated chats](CONVERSATION_ROLES.md) require an independent auditor
before applying an implementation candidate; they do not use that legacy fallback.

The coordinator can call `delegate_plan` with a specific `instruction`, optional
bounded `context` containing findings already known, a configured `route`, and
optional `effort`. A small change uses a small separate plan in the coordinated
chat; the legacy optional-delegation switch applies to direct sessions. The child uses the
existing planner role, native session and observed-source artifact validator.
It may read/list/search existing project roots; it cannot edit, run commands,
delegate, admit Works or apply the returned advice. The coordinator reviews the
handoff and continues in its original conversation. Its prompt/history is not
rewritten to pass the planning mission.
Effort overrides use the model picker's existing connection mappings; an
unsupported level is refused before inference. New planning leaves distinguish
files actually read from searched or proposed targets in their evidence list.

Each request allows up to six model calls and 180 seconds, cut to the parent's
remaining budget while reserving two coordinator calls. A session can request
planning at most three times (per demand in coordinated chats). Child calls also constrain later exploration and
implementation. An explicit parent spend cap requires matching child tariff
evidence; unfinished or unpriced calls keep their spend owner's reservation.
Local planners use the existing local slot and GPU availability checks.

Custody is `planning/<effect digest>/`: dispatch intent precedes inference,
and the child retains its normal journal, raw usage and artifact. Replaying a
completed effect returns that handoff; interruption never silently repeats a
potentially charged call. The returned artifact is advice with `authority: NONE`.
`/usage` includes coordinator, exploration, planning and implementer unit
sessions, deduplicated by their existing session/call identities. Missing prices
and counters stay unknown.
The TUI includes planning-only and implementer children even without an explorer.
Incomplete provider replies retain their stop reason; a missing older reason is
unknown and never permission to repeat a possibly charged request automatically.

Compare equivalent planning demands using the same source revision, coordinator
model/effort, tools and limits. Count the entire journey including child calls,
cache reads/writes, failed attempts and coordinator synthesis. Rotate execution
order, retain warm-cache follow-ups, and assess source accuracy, concrete steps,
dependencies, checks and open risks. A bounded pilot describes these tasks; it
does not establish a universal quality or cost ranking. Paid trials are separate
operator-authorized runs; ordinary repository tests stay offline.
Keep dated results and raw receipts in a bounded private lot with a review date
and a concise report as their destination. When cache detail is unreported,
present a token-price interval rather than filling in zero. The price of the
same tokens without cache discounts can be calculated without making cold calls.
