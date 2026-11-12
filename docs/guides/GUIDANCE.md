# Guidance sent to the models (`/guidance`)

Type `/guidance` in a conversation to open the panel. It lists the texts that guide the models tare calls, shows
where each one goes and who wrote it, and lets you change the ones that can be changed. One button takes any changed
text back to its default.

Your choices are kept in `<storage>/serve/guidance.json`, written by `relay/guidance.py`. If you have saved nothing,
there is no file, and tare sends exactly what it sent before this panel existed.

## The three layers

| Layer | Written by | In the panel | Default | When a change applies |
| --- | --- | --- | --- | --- |
| Role guidance: the prompt capsules of `relay/prompt_capsules.json` (each role's text, and the texts added to the role text or the first message) | tare | edit, Save | tare's default | new sessions; a session already running keeps the text it started with |
| Blocks a vendor CLI adds on its own, as measured (Claude Code 2.1.322, Codex 0.166.0) | the vendor | Remove, and Save a replacement where the CLI allows it | the vendor's default | the next turn |
| tare's transport protocol texts for each CLI | tare | read-only | n/a | n/a |

An edited role text changes the version and the SHA-256 that the session records for its role guidance: the
version gets `-edited`, for example `2-edited`. Saving an empty text, or the default text unchanged, means the
default. **Reset everything** deletes the file.

## What each CLI allows

- **Claude Code.** The account email block can be removed. The environment, model and date blocks can be removed or
  replaced. The identity line ("You are a Claude agent..."), the billing header and the tool descriptions can only
  be read. tare applies a Claude choice through a small add-on that only works while Claude Code's hooks are on.
  For some calls tare normally keeps them off: summaries, and calls that offer the model no tools, such as the
  heads-up notes. **While a Claude choice is saved, tare turns the hooks on for those calls too.** That also runs
  the hooks of the plugins built into Claude Code (on 2.1.322: agents-md, telemetry, privacy, plugin-authoring),
  which already run on every Claude call that offers tools. A Claude Code older than 2.1.322 refuses the choice.
- **Codex.** The skills list, the sandbox and escalation rules, the collaboration mode and the environment block can
  be removed. None can be replaced. Before the first call with a removal on a new Codex version, tare checks with
  Codex's own `debug prompt-input` (which sends nothing) that the block really goes away; if it does not, the call
  is refused.
- **AGY.** tare cannot see what AGY adds, so it lists nothing and changes nothing.

A choice that a CLI cannot honour is refused before anything is sent (`CLI_GUIDANCE_UNSUPPORTED`). It is never
silently ignored. How the Kernel applies each control, and how to re-measure after a CLI update, is in the Kernel's
`docs/VENDOR_GUIDANCE.md`.

## Limits

- Claude Code applies a choice through a small mod. If the mod fails, Claude Code sends its own text anyway, so a
  removal on Claude is not proved for each request.
- While a Claude choice is saved, Claude Code's built-in plugins run their hooks on every Claude call (see above).
- If `guidance.json` holds a choice this version of tare no longer lists, calls to that CLI stop with "Open
  /guidance and choose "Reset everything"". That button deletes the file.
- The vendor texts are shown as measured on one machine, with placeholders such as `<account email>` and
  `<working directory>` for that machine's values. A newer CLI version may add other blocks.
- Whatever a provider adds on its own servers cannot be seen.

## Out of scope

None of these are built:

- a check before dispatch that inspects Claude's final request;
- editing tare's transport protocol texts, or the fixed sentences `run_session` adds around the role guidance;
- other prompts in tare: the heads-up writer, vision, research, flow selection;
- AGY;
- a "capture now" button in the product;
- a `tare guidance` command line;
- freezing vendor choices per session (the client reads them at the start of each turn);
- changing any default.
