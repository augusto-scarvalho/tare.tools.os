# TCP-04: exact mediation and controlled execution

## Automatic review by execution connection (0.1.109)

In Project permissions, choose **Edit + system commands**, then **Automatic:
Codex or Tare + Jev** under **Action review**. Save the project policy once.
The connection executing each action determines the route, independently of
the coordinator:

| Execution connection | Action review | Isolation |
| --- | --- | --- |
| Official Codex CLI | Native Codex Auto-review | Codex sandbox |
| HTTP/API, Claude CLI, AGY CLI | Jev; Tare reviewer only on escalation | Tare MXC |

A model name, Codex account or Responses API connection does not establish the
native CLI capability. Missing Jev credentials, a refused native request or an
unavailable sandbox stops the action; it never switches to unreviewed execution.
Routine Jev decisions need no frontier reviewer. Final Work/code auditing remains
a separate stage. Native Codex forwarding may consume normal Codex usage.

The current project review policy also reaches delegated implementers and named
host checks in saved conversations. It does not rewrite their coordinator prompt,
history or cached prefix, or expand existing file permissions. Explicit saved
Work runs retain their existing policy. Manual review modes remain available.
Automatic mode is opt-in per project and currently requires Windows. Protected
pytest checks store their cache in the admitted temporary directory so a read-only
project does not trigger Python's Windows temporary-directory retry loop.

Build commands can write artifacts throughout the host-owned private candidate,
never the original project. File tools retain the plan's edit paths, and the host
refuses unplanned source changes before audit/application. The host's read/verify-only
check capability can read linked Git metadata for its exact registered checks;
worker commands still cannot access `.git`. Registered executable paths are resolved
before execution, and their installation runtime is admitted read-only.

## Checks without command protection (2026-11-13)

A project without command protection still runs every check in the same MXC
policy: `factory(..., checks_only=True)` builds the same read, write, deny and
environment scopes and the same socketpair shim, with no classifier, reviewer, Codex
gate or review reservation. Its broker admits only host-registered argv (and the
read-only `rg` search a session already has); anything else is refused. The
conversation's host checks (`conversation_verifier`), a native session's `verify`
(`checks_factory`, bound in `native_agent.run_session` and `native_work`) and Work
verification (`protected_work_verifier` with no policy) use it where
`sandbox_available()` (the Kernel's `MxcEnvironment` support query, deny support
included, asked once per process) says the host has the sandbox. A result whose
limitation is `ISOLATION_NETWORK_DENIED`, `ISOLATION_SECRET_DENIED`,
`ISOLATION_INTERNAL_IPC_UNAVAILABLE`, or a binding refusal
(`ISOLATION_UNAVAILABLE`), reaches `outside(name, argv, why)`: the project's saved
"always", this turn's yes, or the user's answer in the conversation's
execution-permission dialog (`kind: sandbox`). Only a yes reruns that check with
`managed_check`; its receipt records the decision and the reason. Without a host
sandbox, hosted checks run with `managed_check` and record `not isolated`, a
read-only project's checks ask first, and `execution_scope.session_role` refuses a
local implementer.

## Jev classification and review escalation (0.1.66)

In Settings (F2), Project permissions, enable protected commands and select a
Classifier preset. `Jev: review when needed` uses pinned `jev-1.13.0` independently
of the conversation model. `Jev: always review` retains a reviewer for routine
actions too. `Selected model: always review` preserves the prior classifier and
reviewer composition. Existing saved conversations keep their original policy;
settings apply to new conversations. Native Codex review still requires the
selected official Codex CLI connection.

The host checks command permissions, current control and source identity before
classification. One bounded call uses the existing hosted TypeSafe Choice worker,
machine eligibility and credential vault. It sends the admitted operator request,
exact command and scope, plus explicitly referenced small UTF-8 source files.
The existing 8 KiB Choice limit refuses oversized context without truncation.
No new service, SDK, local model download or frontier fallback is introduced.

With review when needed, `ROUTINE_ELIGIBLE` skips reviewer inference and native
review. `REVIEW_REQUIRED` requires the configured Tare reviewer, native Codex
execution gate, or both. `DENY_RECOMMENDED` blocks; `INSUFFICIENT_EVIDENCE` pauses
for missing context. Unavailability, cancellation, stale identity and malformed
answers also stop the action. Jev recommendations cannot grant permissions or
change the sandbox. Exact owner permits, durable claims and final revalidation
still govern execution. Choice scores are recorded observations, not calibrated
probabilities of safety. Unknown runtime internals/dependencies are explicit.

The child uses a separate `command-tmp` directory; the native capability owner's
`scratch/effect-receipts` remains outside child read/write scopes. Every profile
denies the project's secrets before source disclosure; see the deny list below.
It matches names and does not discover all possible secrets.

Qualification includes offline contract/composition tests and sixteen synthetic
public cases through the real Jev service, with zero effects in that campaign.
Two false escalation/denial cases were retained, corrected and replayed before
the final 16/16 pass. This is small integration evidence, not general calibration
or third-party safety qualification. Real public TUI dogfood exercised one routine
command with one Jev call and zero reviewer calls, and one script escalation with
one Jev call and one genuine native Codex approval before a single MXC effect.
Provider usage is recorded when reported;
native forwarding still uses normal Codex quota, and unreported billing remains
unknown. Production key lifecycle, broad calibration, additional platforms and
distributed trusted CI remain separate backlog work.

## Local Windows use from the TUI (0.1.65)

Open Settings (F2), then Project permissions. Choose Edit + system commands,
select Tare, Native Codex or Tare + Codex under Command review, choose Tare MXC
or Codex sandbox, set the finite review-call limit, and save for new conversations.
Off retains the previous behavior. Existing conversations retain their original
policy; a protected conversation exposes noninteractive `command` instead of an
interactive terminal. These settings affect command execution, not file-tool scope.

In the selected-model preset, the selected connection supplies review inference. Native Codex review requires
the selected official Codex CLI connection. The host freezes its executable hash
before saving the session and refuses changes on resume. Tare classification uses
low effort and its reviewer high effort; the native execution gate is a separate
approval stage, not a standalone native assessment RPC. Both requires the Tare
decision and native approval; failure does not choose an unreviewed path.

The local composer uses the existing mediator, owner permit, durable claim and
Windows process supervisor. The declared runtime/project read scopes, configured
write paths, separate command temporary directory and denied network are applied by MXC.
Deny list (`relay/sandbox_secrets.py`), whatever the classifier: at any depth of the project and of its granted
folders, `.env` and `.env.*` (not `.env.example`, `.sample`, `.template`, `.dist` or `.defaults`), `.ssh`,
`.aws`, `.codex`, `.npmrc`, `.pypirc`, `.netrc`, `.git-credentials`, `.pgpass`, `.gnupg`, `.kube`, `.azure` and
`.docker/config.json`, plus the root `.git` and nested `.git` folders except for host checks and private staged
candidates, which read Git metadata. The walk skips `.git`, `node_modules`, `__pycache__` and folders holding
`pyvenv.cfg` or `CACHEDIR.TAG`, follows no link and stops at 100,000 entries or 32 levels; reaching a bound, or
finding more than the 32 paths PSEC's deny group holds, refuses the binding with a message rather than leave a
secret readable. The list is built once per binding and sorted (it enters the policy digest), so a secret created
later is not denied. Only hidden names are listed: a denied file in ripgrep's file list (`id_rsa`) would refuse
the whole `search`. MXC denies paths: a denied file cannot be read, copied, linked, renamed or deleted, and a
denied folder cannot be listed, but their names and sizes stay visible in the parent's listing. Because the rule
follows the path, renaming a folder between a write scope and a nested secret would carry the secret out of it
(probed 2026-11-13). So the host holds every such secret open, with no access and every sharing mode, for the life
of the binding (`Hold`, released by the mediator's `close` or when collected, never inherited): Windows refuses to
rename or move a folder below which something is open, and every variant probed in the sandbox (`os.rename`,
`MoveFileEx`, POSIX `FileRenameInfoEx`, `cmd move`/`ren`, `Move-Item`, a `\\?\` or 8.3 path, a grandparent,
another write scope) then failed with access denied; copying the folder (`robocopy /MOVE` too) leaves the secret
behind and a junction cannot be created. A secret directly in a write scope's root, or in a read-only scope,
needs no handle: those folders cannot be renamed from inside. If a handle cannot be opened the binding is
refused. The user cannot rename, move or delete those folders either while the binding lives. Other
runtime/profile needs are admitted separately; no generic third-party guarantee
or hard disk quota is claimed. Protected checks use their frozen executable path.
No enumerate-only scope is granted: PSEC's `fs_enumerate` admits listing of a folder's whole subtree, so the
volume root's would list the entire drive. A command lists only admitted folders (and system folders whose
Windows ACL admits every app container, such as Program Files); it can still stat each ancestor of an
admitted path, except the volume root itself.

Review reservations are recorded before dispatch and bounded per invocation.
The exact latest admitted user context and referenced executable/file identity are
revalidated before an effect. Output shows the selected review, its approved
execution, reserved calls and reported usage. Missing native usage and billing
remain unknown. A cancelled or interrupted effect remains unknown where partial
effects are uncertain, with its real process cleanup receipt retained and no
automatic reexecution of the same claimed request.

Real public TUI dogfood qualified native and combined reviews, an actual Windows
MXC effect, persistent exact-request replay refusal and user cancellation after
the process began. Offline tests cover denial, source/context changes, exhausted
budgets, narrow-screen controls and saved policy. Production key lifecycle,
expanded calibration, other platforms/PTY/third-party operation and distributed
trusted CI are deferred backlog items; they do not block this local scope.

## Native Codex execution review

`CommandActionMediator(reviewed_execution=...)` accepts the explicit Kernel
`CodexNativeCommandReview` gate. It enters only after the existing owner permit
and durable claim. A public Codex session proposes one fixed MCP command;
the bridge queues the exact action hash, argv and cwd for the host. Matching
native thread/turn/item/review events and bridge dispatch are required before
the original guard revalidates source, context, grant, fence and deadline and
the original confined effect runs once. There is no second claim or effect
ledger, and denial/unavailable/malformed/cancelled results do not choose an
unreviewed path. Result handling retains the real host command result.

The host supplies the admitted CLI/hash, explicit model/catalog/environment,
public-context disclosure, finite reservation and receipt custody. The native
forwarding session removes shell/patch/search/node/multi-agent task tools and
disables inherited hooks/apps/plugins/MCP servers; only its fixed bridge remains.
Existing official ChatGPT authentication stays with Codex. API-key login, custom
endpoints, manual approvals and changed execution identity are refused. No
installed profile, default reviewer, permission or model route is switched.

This uses native auto-review for executable approval requests. It does not
implement a standalone native ActionReviewerPort or substitute code review.
Advisory/no-permit MCP pilots approved the harmless submission of even a
destructive candidate; such approval cannot authorize its proposed effect.
The existing broker's `codex_native`/`both_required` assessment modes therefore
remain unavailable unless a genuine standalone port is independently supplied.
Hosts can compose their normal Tare assessment policy with this additional
native execution gate, inside the same owner authority and claim.

A real owned Windows command passed through NativeCapabilities, the mediator,
one durable claim, native approval, final revalidation and Tare's MXC launcher.
Its replay did not create another native session. Native reviewer model and
billing were not attested; the forwarding generation uses ordinary Codex quota.
Local cleanup is verified separately from unknown remote stop. Wider model
calibration, other platform live behavior and general third-party qualification
remain separate criteria.

Dogfood also corrected the TUI's framing budget: a valid 16 KiB public prompt
plus trusted chat guidance now fits a finite 18 KiB internal initial instruction.
Public message limits and call/time budgets remain unchanged. Public TUI replays
and offline fixtures qualify both this boundary and exact native event handling.

## Explicit MXC backends

`relay.windows_isolation.windows_isolation_factory` supplies the existing
`native_agent.command_mediation_factory` callback. The host selects `backend="mxc"`
for Tare's own native PSEC launcher or `backend="codex"` for the public CLI sandbox.
It supplies the immutable Kernel policy, cwd, fixed environment, finite command
budget and its existing mediation factory. Codex additionally requires an admitted
absolute executable and SHA-256. No model chooses these values, no second runner
or effect ledger is introduced, and errors do not select another backend.

The existing capabilities bind the selected launcher before requests. The
supplied `CommandActionMediator` still owns assessments, owner authorization,
the exact guard and the durable effect claim. Classification and auto-review
cannot expand filesystem/network permissions. The isolation factory consumes
the Codex sandbox; native execution review is the separate explicit gate above.
Hosts keep responsibility for producer signing and artifact custody.

Both backends passed owned Windows file/network controls through real native
capabilities and the guard with a durable claim, using a deterministic fixture
assessor and no model inference in the fixture. Cancellation and timeout controls
also passed with descendant cleanup. Offline tests cover policy ABI, native
context failures and closure, managed CLI retargeting, host admission and no
fallback. Microsoft's MXC SDK remains an early preview, and qualification is
scoped to the exercised Windows configuration. Other platforms, production key
custody and broad classifier/reviewer calibration remain separate criteria.
Installed profiles remain unchanged; hosts must explicitly inject this callback.

The exact mediation packet and the controlled Windows execution packet are opt-in.
Existing local permissions and installed profiles are preserved. Classifier/reviewer
observations remain advisory; neither packet activates automatic model approval.

Kernel dependency: 0.9.26; its exact revision is recorded by the
`satellites/tare.tools.kernel` gitlink. Complete Windows/Linux CI qualifies that
dependency before integrating this OS pin.

Kernel provides `ActionSnapshot`, `ActionAuthorization`, `ExactActionGuard`,
assessment ports, `CommandFacts` and the existing command-boundary hooks. The OS
provides `ActionPolicy`, `ActionReviewBroker` and `CommandActionMediator`.

The broker checks deterministic permission and Runtime control before spending a
reserved assessment. Routine triage skips review only when host policy permits it.
Modes are `tare`, `codex_native` and `both_required`; denial never chooses another
reviewer, unavailable fallback is explicit, and both-required never downgrades.
Missing standalone native Codex assessment remains unavailable. Ordinary model
inference is not the native auto-review feature; the executable native gate
above is composed after the existing owner authority and claim.

The mediator binds host-prepared command facts to a snapshot, asks the broker,
obtains a permit exclusively from the injected authority owner, and invokes the
Kernel guard. The guard rechecks current source/context/permission, Runtime
binding/fence, cancellation and deadlines before and after its durable claim.
An unknown/crashed effect cannot be executed again to obtain evidence.

`native_agent.run_session(command_mediation_factory=...)` is an explicit host seam;
the factory receives the session/binding/control and capabilities whose public
claim method uses the existing effect recorder. `ConsoleTerminals` accepts a
per-owner command mediation factory consumed by the Kernel terminal host. No
default, discovery, second runner or model-selected policy is introduced. A
configured unavailable factory fails closed.

Tests use finite fake assessment/authority ports and owned temporary projects.
They exercise exact-byte changes, stale grants/source/fences, cancellation while
reserving budget or claiming, deny/fallback rules, durable restart/replay refusal,
and a real fixture verifier through the ordinary capability path. These tests
qualify plumbing, not model safety/calibration or physical producer authenticity.

Real implementation dogfood uses the public TUI and actual terminal input. TUI
catalog/implicit-verifier fixes are independent deliveries. Codex inference now
uses its public response schema for data proposals while native tools remain
disabled; this transport change does not enable native auto-review.

`confined_actions.confined_mediation_factory(...)` now composes Kernel's copied
Windows LPAC workspace, admitted host signer and result consumer with the existing
mediator. It binds the process factory before requests, permits only the copied
input root, refuses terminal/PTY use and additional sessions, validates the exact
profile/source/environment identity, and retains the owner's permit and durable
effect claim. It introduces no alternate runner or authority ledger.

`CommandActionMediator(result_handler=...)` runs result handling only after the
guarded effect. Collection checks native identity, unchanged manifests, matching
PID and descendant cleanup. The signer covers exact action/session/effect/binding/
fence/run, input/runtime/environment/profile identity and every exported byte,
including stdout, stderr and process status. The consumer uses its own admitted
keys and expected subject before the custody callback. Signing or custody failure
leaves an unknown effect; replay does not run it again. Evidence has authority NONE
and does not declare correctness or authorize settlement.

The first Windows profile defaults to zero capabilities; Python compatibility
separately admits only registryRead. Resource monitoring can overshoot, Windows
ambient restricted-package resources remain accessible, and no hard disk quota or
VM/other-platform sandbox is supplied. Key persistence/rotation/revocation and
third-party operation require additional qualification. No installed permission
or default profile is switched to this mode.

Portable tests cover permit denial, custody and signature failure, durable replay
refusal and complete producer identity/byte verification. Windows controls cover
copied input/scratch, outside file and protected-registry denial, socket denial,
timeout/cancellation, hardlinks, altered inputs, active output and failed cleanup.
A real confined verifier passes through the OS guard and records a checked receipt.
Public TUI dogfood implements the components and invokes focused qualification
with finite cloned profiles. These are controlled examples, not a general safety
certification or model-calibration result.

Broader platform/profile qualification, production key lifecycle and distributed trusted CI are deferred. Local TUI command protection is qualified separately as described above.

## Tare-owned model assessments

`action_model_assessment.TareActionClassifier` and `TareActionReviewer` compose
the existing `native_generation.generate_text` worker over Runtime's ModelPort
and Kernel counting, transport, usage and spend handling. They expose the existing
assessment port shape to `ActionReviewBroker`; they have no effect or permit port.
The host explicitly supplies a deployment, disclosure check, custody context and
fresh journal factory. Each assessment uses text-only inference with no tools,
one generation and the original fenced control clamped to the assessment deadline.
Only a host-selected public context and exact action proposal are disclosed.
Non-UTF8 executable/input bytes and oversized data are refused without truncation.

The codec rejects duplicate JSON keys, unrecognized fields, wrong request hashes,
stage-crossed verdicts, authority claims, unrecognized reason codes and malformed
or oversized output. Live cancellation, deadline and binding are checked before
and after inference. A changed deployment cannot be selected by model output.
Served-model attestation remains as reported by the transport; a requested route
does not establish the identity of a server-side model. Missing usage or tariffs
remain unknown, including when an attempted generation is interrupted.

Assessment intents and accounting use a dedicated stream of the existing native
journal under host-held custody, separately from the tactical NativeSession log.
The intent is durable before dispatch. Any earlier intent for the same action and
stage prevents automatic redispatch, including after a successful result or a new
assessment deadline/output budget. Failure to persist the result cannot return
an observed verdict. This is assessment accounting, not another effect ledger;
the original exact guard and authority owner still govern execution.

Offline contract cases and a bounded shadow packet exercised these adapters.
The shadow used existing GPT-6.1 Sol deployments (classifier low, reviewer high):
five hypothetical action families, ten generations and zero proposed effects
executed. The authorized fixture verifier received routine/approval recommendations;
outside deletion, chained credential upload, an opaque script and injected project
evidence received conservative recommendations. The exact raw inputs, outputs,
identity hashes, usage and unsuccessful dogfood attempts are retained in the
operator's `tcp04-model-assessment-20261012` custody packet. This small corpus
qualifies the route and controls; broader calibration, contexts and deployments
remain open. It does not activate a default guard, unlock TCP-05 or implement the
free native Codex auto-review backend.
