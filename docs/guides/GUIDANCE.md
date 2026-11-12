# Guidance sent to the models (`/guidance`)

Type `/guidance` in a conversation to open the panel. It lists the texts that guide the models tare calls, shows
where each one goes and who wrote it, and lets you change the ones that can be changed. Any changed text shows
before × after, and one button takes it back to its default.

Your choices are kept in `<storage>/serve/guidance.json`, written by `relay/guidance.py`. If you have saved nothing,
there is no file, and tare sends exactly what it sent before this panel existed.

## The three layers

| Layer | Written by | In the panel | Default | When a change applies |
| --- | --- | --- | --- | --- |
| Role guidance: the prompt capsules of `relay/prompt_capsules.json` (each role's text, and the texts added to the role text or the first message) | tare | edit, Save | tare's default | new sessions; a session already running keeps the text it started with |
| Blocks a vendor CLI adds on its own, read from the installed CLI | the vendor | Remove, and Save a replacement where the CLI allows it | the vendor's default | the next turn |
| tare's transport protocol texts for each CLI | tare | read-only | n/a | n/a |

An edited role text changes the version and the SHA-256 that the session records for its role guidance: the
version gets `-edited`, for example `2-edited`. Saving an empty text, or the default text unchanged, means the
default. **Reset everything** deletes the file.

## Reading what each CLI adds

Each time the TUI opens, tare reads in the background what Claude Code and Codex add on their own right now. It
uses no tokens and never holds up the TUI. The result is kept in `<storage>/serve/vendor-guidance.json`, and the
panel's first line says which CLI version each text was read from and when.

- **Claude Code.** tare starts the CLI with its usual settings, but points it at a recorder on this machine
  instead of Anthropic's servers. The CLI's login token goes only to that recorder, so it never leaves the
  computer, and nothing reaches the provider. The recorder keeps the request's body, not its headers, and refuses
  the request, so the CLI stops at once. It takes about two seconds.
- **Codex.** tare asks Codex to show what it would send (`codex debug prompt-input`); nothing is sent. It takes
  under a second.
- **AGY.** tare cannot see what AGY adds, so it lists nothing and changes nothing.

Values that belong to your machine or to the day, such as your email, folders, dates and model names, are shown as
placeholders like `<account email>`. That way two readings of the same CLI version match.

When a block's text changes between readings, the panel keeps the earlier text and shows the difference. Only the
previous text is kept; there is no archive of older ones. If a CLI cannot be read, the panel keeps the last reading
and says so.

## What each CLI allows

- **Claude Code.** Context blocks (`context:…`, such as the account email) can be removed. Attachments
  (`attachment:…`, such as the environment, model and date blocks, or the frame around the context) can be
  removed or replaced. System blocks, the text around the blocks and tool descriptions can only be read. A block
  type a newer Claude Code adds appears by itself, with the controls of its kind.
- **Claude Code's hooks.** tare applies a Claude choice through a small add-on that only works while Claude Code's
  hooks are on. For some calls tare normally keeps them off: summaries, and calls that offer the model no tools,
  such as the heads-up notes. **While a Claude choice is saved, tare turns the hooks on for those calls too.** That
  also runs the hooks of the plugins built into Claude Code (on 2.1.322: agents-md, telemetry, privacy,
  plugin-authoring), which already run on every Claude call that offers tools.
- **Claude Code versions.** A Claude Code older than 2.1.322 refuses a choice.
- **Codex.** The skills list, the sandbox and escalation rules, the collaboration mode and the environment block
  can be removed. None can be replaced. Before the first call with a removal on a new Codex version, tare checks
  with Codex's own `debug prompt-input` that the block really goes away. If it does not, the call is refused.

A choice that a CLI cannot honour is refused before anything is sent (`CLI_GUIDANCE_UNSUPPORTED`). It is never
silently ignored. How the Kernel reads each CLI and applies each control is in the Kernel's
`docs/VENDOR_GUIDANCE.md`.

## When a default changes under your choice

A saved choice records the default it was made against, with its SHA-256. Defaults can change under it: a CLI
update can rewrite a vendor block, and a tare update can rewrite a role text. In that case the item shows as
**changed underneath**, the panel shows the difference between the default you approved and the new one, and you
decide:

- **Keep mine.** Your version stays, now approved against the new default.
- **Use the new default.** Your choice goes.
- **Edit and Save.** Your new text is approved against the new default.

Until you decide, tare keeps sending your version, so no vendor text you did not approve goes out in its place.

A block a CLI starts sending that you have never seen shows as **new**. It goes out as the CLI writes it until you
choose otherwise. When a reading finds a changed or a new text, the TUI says so once in its status line and points
to `/guidance`.

## Limits

- Claude Code applies a choice through a small mod. If the mod fails, Claude Code sends its own text anyway, so a
  removal on Claude is not proved for each request.
- While a Claude choice is saved, Claude Code's built-in plugins run their hooks on every Claude call (see above).
- The Claude reading points the CLI at a local address; whether a CLI behaves differently for that address than
  for Anthropic's cannot be seen without a real call.
- If `guidance.json` holds a choice this version of tare cannot apply, calls to that CLI stop with "Open
  /guidance and choose "Reset everything"". That button deletes the file.
- Whatever a provider adds on its own servers cannot be seen.

## Out of scope

None of these are built:

- a check before dispatch that inspects Claude's final request;
- an archive of every old vendor text;
- a "read again" button (a reading happens when the TUI opens);
- reading AGY;
- blocking a new block by default;
- editing tare's transport protocol texts, or the fixed sentences `run_session` adds around the role guidance;
- other prompts in tare: the heads-up writer, vision, research, flow selection;
- a `tare guidance` command line;
- freezing vendor choices per session (the client reads them at the start of each turn);
- changing any default.
