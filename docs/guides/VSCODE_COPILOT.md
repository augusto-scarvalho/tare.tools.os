# tare as a local model in VS Code Copilot

`tare serve` publishes tare as OpenAI-compatible models for Copilot Chat's Custom Endpoint provider (BYOK).
What the three kinds share (routing policy, audit, judges, vision, verifiers, snapshots) lives in
`relay/turn_engine.py`; `relay/turn_capabilities.py` records which frontend gets which capability.

## `tare (agent)`: tare runs the tools

Each Copilot chat becomes a tare conversation on the open folder (Copilot's `<workspace_info>`, or
`tare serve --root`); new messages are follow-ups and progress streams into the chat.

- **Routes** follow `<storage>/serve/routing.json` or the default policy in `relay/turn_engine.py`: local
  models first, then cheap, then intermediate API price (subscription quota counts at API price). A blocked
  or rejected turn continues on the next tier; a session that spent its per-message budget (60 calls, one
  hour) continues in a fresh session with the continuity brief (`turn_engine.successor`).
- **Modes**: `/read-only`, `/accept-edits` (default) and `/yolo` (a terminal for any command, operator
  authorised) set the chat's permissions; `/<mode> default` makes it the project's mode for new chats. The
  mode is per project and machine, shared with the TUI (read, edit, commands).
- **Audit**: every turn that changed code is reviewed, only on what changed, by an independent auditor from
  another vendor first (GPT-6.1 Sol or Opus 5.5 at high). The project's tests (a detected pytest or
  `npm test` verifier) and the commands the turn ran reach it as host evidence. Cheap judges (hosted Jev,
  the resident local LitJEV) may demote a finding to a residual; findings get one fix round.
- **Undo**: `/undo` restores what the last file-changing turn changed; editing or rewinding an earlier
  message in Copilot restores what the discarded turns changed and forks the chat. Files you changed
  yourself keep their content.
- **Other folders**: a question that needs a folder outside the open one (another repository) makes the agent
  ask in the chat; a reply whose every word agrees (sim, pode sim, ok) lets it read that folder, any other
  reply does not ("ok, mas antes..." grants nothing). A `/plan` or `/explore` that asked runs again on the
  yes. What it reads goes to the conversation's model. The grant stays for the project on this machine and is
  the TUI's (remove it from the grants file the question names to take it back). A drive, the user folder, a
  folder above it and credential folders (`.ssh`, `.aws`, `.config`, ...) are never granted or read.
- **Local models**: `/local-gpu off`, in this model or `tare-copilot`, keeps this chat on cloud and CLI routes
  (no local route, judge or explorer) until `/local-gpu on`; other chats are unaffected. A chat can start that
  way: `/local-gpu off <request>` as its first message (tare-copilot answers the command alone, so send the
  request next). `/local-gpu off default` leaves the shared local GPU to others for every chat and the TUI (the
  TUI's machine option; it survives restarts) until `/local-gpu on default`. `/local-gpu` says which holds.
  A chat's choice survives tare serve's restarts (tare-copilot keeps it in `<storage>/serve/copilot-chats.json`).
  Other clients can put the choice on each request instead: `"tare_local_models": false` in the chat
  completion body (both models; never sent upstream) applies to that request alone, atomically with its
  inference, and a single local route such as `tare-qwen38` is refused with 409. The stream answers with
  `X-Tare-Local-Models: on|off`, the policy it ran under (the machine's choice wins), and `/v1/models` marks
  each single route with `tare_local`.
- **Flows**: `/plan`, `/explore` (read-only) and `/research` (links read locally, else a free search).
  Links in any message are read as untrusted text, from public web addresses only: `localhost`, your network and
  Tailscale are refused ([rules](CONVERSATION_RESEARCH.md)).
- **Images**: for a model that cannot see, the policy's vision routes describe them, in order: GPT-6 Luna
  (Codex CLI), Gemini 3.8 Flash (AGY CLI), Claude Sonnet 5.5 (Claude CLI), GPT-5.6 Luna.

## `tare (copilot)`: VS Code runs the tools

`tare-copilot` passes Copilot's tools to the route, so VS Code applies edits with Keep/Undo, asks before
every command and keeps its checkpoints. tare picks the route per user message and audits the message's net
change from project snapshots (VS Code's command outputs are the evidence), asking one fix round on REVISE.
It reads linked pages and describes images for a route that cannot see. It escalates on clear signals, a
route error, a fix that fails its audit again, or the runtime's no-progress rule (three rounds that repeat
what was seen), raising a per-chat floor that never drops so the prompt cache holds; stuck on the strongest
route, it stops instead of looping.

## Single routes

`tare-qwen38`, `tare-swift` pass Copilot's tools straight to one route. Only OpenAI-chat profiles can carry
client tools (the local gateway, OpenRouter); vendor CLI routes are refused with 400.

## VS Code

Run **Chat: Manage Language Models** or edit `chatLanguageModels.json` in the VS Code user folder
(`%APPDATA%\Code\User` on Windows):

```json
{
  "name": "tare",
  "vendor": "customendpoint",
  "apiKey": "tare-local",
  "apiType": "chat-completions",
  "models": [
    {"id": "tare", "name": "tare (agent)", "url": "http://127.0.0.1:8790/v1/chat/completions",
     "toolCalling": true, "vision": true, "maxInputTokens": 200000, "maxOutputTokens": 32000},
    {"id": "tare-copilot", "name": "tare (copilot: VS Code runs the tools)",
     "url": "http://127.0.0.1:8790/v1/chat/completions",
     "toolCalling": true, "vision": false, "maxInputTokens": 114000, "maxOutputTokens": 16384},
    {"id": "tare-qwen38", "name": "tare · Qwen3.8 27B (local)", "url": "http://127.0.0.1:8790/v1/chat/completions",
     "toolCalling": true, "vision": false, "maxInputTokens": 114000, "maxOutputTokens": 16384},
    {"id": "tare-swift", "name": "tare · Swift 1.5 27B (local)", "url": "http://127.0.0.1:8790/v1/chat/completions",
     "toolCalling": true, "vision": false, "maxInputTokens": 114000, "maxOutputTokens": 16384}
  ]
}
```

Where tare's product is installed, this block is managed: each `Tare-Update` run adds the models the running
`tare serve` publishes and fixes their url (Code and Code - Insiders, plain JSON only, a backup first). Other
providers and your values are kept; a tare model removed from the block comes back.

Then pick the model in the Chat model picker. VS Code must be signed in to GitHub: without a session Copilot
runs its limited free tier, which unregisters BYOK providers ("unregistered providers due to enterprise
policy" in the Copilot Chat log) and answers with its own model instead. Copilot Business/Enterprise
administrators can also disable BYOK by policy.

## Running it on a Windows machine

`tare serve` runs on the Windows side so it can use the Windows logins of the Claude, Codex and AGY CLIs;
the local model gateway in WSL answers on `127.0.0.1:8080`.

- It runs from the managed product (`%LOCALAPPDATA%\tare.tools\product\tare.cmd serve`), started hidden at
  logon by the scheduled task `Tare-Serve`, with its state in `%LOCALAPPDATA%\tare.tools\serve-state`.
- Updates are automatic (see `docs/guides/PRODUCT_INSTALLATION.md`): a push of main publishes a release
  within minutes, `Tare-Update` installs it beside the running one and moves `tare serve` onto it only while
  idle, rolling back if it does not come up. `scripts/ops/install_product_tasks.ps1` sets a machine up.
- After signing in to a vendor CLI, restart the task: discovery retries CLIs that did not answer.
