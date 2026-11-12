# Guidance sent to the models (`/guidance`)

Type `/guidance` in a conversation to open the panel. It lists the texts that guide the models tare calls, shows
where each one goes and who wrote it, and lets you change the ones that can be changed. Any changed text shows
before × after, and one button takes it back to its default.

Your choices are kept in `<storage>/serve/guidance.json`, written by `relay/guidance.py`. If you have saved nothing,
there is no file, and tare sends what the CLIs write, except the blocks tare leaves out by default (below).

## What tare leaves out by default

| CLI | Block | Why |
| --- | --- | --- |
| Codex | `skills_instructions`, the skills list | It lists every skill installed on the machine, about 16,000 characters on the owner's. tare turns Codex's tools off, so the model cannot open those skills. tare will have its own skills and its own way of calling them (decided 2026-11-12). |

tare leaves a block out only on a CLI version where it has checked that the CLI really does. The check happens when
the TUI opens: one more reading without tokens, with the block asked out. An installed copy of tare that updates by
itself may meet a Codex that ignores the request. There the block is still sent, and the panel says so; leaving it
out unchecked would make every Codex call fail. Before the first reading, nothing is left out.

In the panel the block is marked `T`. The view below it shows what is left out, and **Vendor's default** sends it
again as the CLI writes it. Then it is marked `M`, and **tare's default** leaves it out again. **Reset everything**
also goes back to tare's defaults. A choice of your own for that block always wins over tare's.

## The panel

The list on the left is in groups, in this order:

1. tare · role guidance
2. tare · protocol texts (read-only)
3. Claude Code
4. Codex
5. AGY

A heading cannot be selected. Under each CLI's heading are the version its texts were read from, how and when they
were read, and its blocks in the order the CLI sends them.

A text you changed is marked as the Files pane (F8) marks a changed file: its name in colour and one bold letter
after it.

| Mark | Meaning |
| --- | --- |
| `M` | your choice: your text, or a block tare leaves out that you send again |
| `T` | left out by tare's default |
| `D` | removed |
| `C` | the default changed under your edit |
| `U` | a block the CLI started sending that you have not reviewed |
| grey | not in the latest reading |

The panel's first line repeats this key.

When you open the panel right after starting the TUI, tare may still be reading the CLIs. Each CLI's group then
shows its heading and the line "Being read now", and fills in by itself when the reading is done. tare's own
groups can be used at once, and nothing you have typed or selected is lost when the list fills in.

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

Each time the TUI opens, tare reads in the background what each CLI adds on its own right now. The reading never
holds up the TUI. The result is kept in `<storage>/serve/vendor-guidance.json`. The panel's first line says which
CLI version each text was read from, how, and when.

There are two kinds of reading:

- **Without tokens, at every start.** For Claude Code and Codex.
- **With one small real call, once per CLI version.** For Claude Code and AGY, when the installed version differs
  from that of the last real reading. The call goes to the CLI's cheapest ready route (Claude Haiku; for AGY, the
  low tier of its Gemini Flash route) at low effort, and costs a few hundred tokens. If it fails, for example
  because a quota is spent, the panel keeps the last reading and says the real call could not be made. tare tries
  again at the next start, never more than once per start.

What each reading does for each CLI:

- **Claude Code, without tokens.** tare starts the CLI with its usual settings, but points it at a recorder on
  this machine instead of Anthropic's servers.
  - The CLI's login token goes only to that recorder, so it never leaves the computer, and nothing reaches the
    provider.
  - The recorder keeps the request's body, not its headers, and refuses the request, so the CLI stops at once. It
    takes about two seconds.
- **Claude Code, real call.** The real call makes one small request, and tare's add-on records every block as it
  is sent.
  - Some blocks Claude Code adds only to a real call. On 2.1.322 that was the reminder of how many tokens are
    left.
  - The panel marks those blocks "Seen only in a real call". Like other blocks of their kind, they can be removed
    or replaced.
- **Codex, without tokens.** tare asks Codex to show what it would send (`codex debug prompt-input`); nothing is
  sent.
  - It does this as tare's real call would: with the skills installed on this machine, but without your login or
    your Codex settings file.
  - On Codex 0.166.0 every block matched what a real call sent. It takes about a second.
- **AGY, real call.** AGY offers no way to see what it adds without a call. tare makes one small real call and
  reads only the record AGY keeps of that call.
  - It finds the frames around tare's text, a block with your local time, a block about the model selection, and
    the tools AGY adds.
  - None of them can be changed.
  - The same record holds your saved AGY permission rules. Nothing shows whether they reach the model, so tare
    does not list them.
- **AGY, until the first real call works.** The panel shows what AGY 1.5.7 sent on 2026-11-12, marked as such.

Values that belong to your machine or to the day, such as your email, folders, dates, times, token counts and model
names, are shown as placeholders like `<account email>`. That way two readings of the same CLI version match.

When you edit a Claude Code block, keep the placeholders where you want the real values: your text is sent
with the values Claude Code wrote in that same block, so "Today is `<date>`." reaches the model with the real
date. A placeholder whose value is not in that block is sent as written.

When a block's text changes between readings, the panel keeps the earlier text and shows the difference. Only the
previous text is kept; there is no archive of older ones. If a CLI cannot be read, the panel keeps the last reading
and says so.

Some blocks come and go between calls. Claude Code's reminder of the tokens left, for example, was in five real
readings and missing from a sixth. So a block missing from the newest reading stays in the list, in grey, with when
and on which CLI version it was last seen. You can still remove or replace it as before. Only one entry per
distinct block is kept, so the list stays short.

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
  can be removed. None can be replaced. tare leaves the skills list out by default (above). Before the first call with a removal on a new Codex version, tare checks
  with Codex's own `debug prompt-input` that the block really goes away. If it does not, the call is refused.
- **AGY.** Its blocks can only be read.

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
- The Claude reading without tokens misses blocks that only a real call carries; the real call, once per version,
  finds them. Neither shows the system lines Claude Code puts before tare's text to the add-on, so those come from
  the reading without tokens.
- AGY is read from its own record of a real call, by the tags in its text. An AGY version that changes those tags
  cannot be read until tare learns them; what AGY sends but does not keep in that record cannot be seen.
- A real reading is made on a subscription route; it is not added to the spend ledger, which counts paid API keys.
  The tokens it used are kept with the reading.
- If `guidance.json` holds a choice this version of tare cannot apply, calls to that CLI stop with "Open
  /guidance and choose "Reset everything"". That button deletes the file.
- Whatever a provider adds on its own servers cannot be seen.

## Out of scope

None of these are built:

- a check before dispatch that inspects Claude's final request;
- an archive of every old vendor text;
- a "read again" button (a reading happens when the TUI opens);
- a real reading more often than once per CLI version, or for Codex (its reading without tokens matched a real
  call);
- blocking a new block by default;
- editing tare's transport protocol texts, or the fixed sentences `run_session` adds around the role guidance;
- other prompts in tare: the heads-up writer, vision, research, flow selection;
- a `tare guidance` command line;
- freezing vendor choices per session (the client reads them at the start of each turn);
- changing any default.
