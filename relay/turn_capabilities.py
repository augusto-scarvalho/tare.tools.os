"""Which tare frontend gets which turn capability, and why not when it does not.

`shared` means the frontend reaches the one shared implementation (relay/turn_engine.py or the runtime).
Anything else says what the frontend does instead: `n/a:` (the frontend's client owns it), `separate:` (a frontend-specific implementation still to be
unified) or `gap:` (missing, a known debt). A new capability must be decided here for every frontend; the test
in tests/test_turn_capabilities.py fails otherwise, so no frontend is forgotten by omission.
"""
FRONTENDS = ('serve', 'copilot', 'tui')  # tare serve's `tare` model, `tare-copilot`, the TUI
SHARED = 'shared'

CAPABILITIES = {
    "red CI of this machine's pushes (relay/ci_watch.py alerts, shown once each)": {
        'serve': SHARED, 'tui': SHARED,
        'copilot': "n/a: tare-copilot relays the model's own stream; the serve's `tare` model and the TUI show it"},
    'independent audit of a turn that changed code': {
        'serve': SHARED, 'copilot': SHARED, 'tui': SHARED},
    'cheap judges and one bounded fix round': {
        'serve': SHARED, 'copilot': SHARED, 'tui': SHARED},
    'host-recorded tests and commands as audit evidence': {
        'serve': SHARED, 'tui': SHARED,
        'copilot': SHARED},  # the commands and outputs VS Code reports (the client runs them, not tare)
    'audit of non-streamed requests': {
        'serve': SHARED, 'tui': SHARED,
        'copilot': "n/a: Copilot's agent turns stream; its non-streamed requests are titles and summaries"},
    'detected tests verifier': {
        'serve': SHARED, 'copilot': "n/a: the client runs its own tools", 'tui': SHARED},
    'image descriptions for models that cannot see': {
        'serve': SHARED, 'copilot': SHARED, 'tui': SHARED},
    'linked pages and /research': {
        'serve': SHARED, 'copilot': SHARED,
        'tui': "separate: the TUI's research-flow command"},
    'conversation URL reading and delegated internet researcher': {
        'tui': SHARED,
        'serve': 'separate: linked pages and /research use the turn engine research flow',
        'copilot': 'separate: linked pages and /research use the turn engine research flow'},
    'coordinator local product contract and delegated execution facts': {
        'tui': SHARED,
        'serve': 'separate: serve uses its own turn instructions and tools',
        'copilot': 'n/a: the client owns its conversation instructions and tools'},
    'permission modes (one per project and machine: turn_engine.project_permission_mode)': {
        'serve': SHARED, 'copilot': "n/a: VS Code approves each tool call", 'tui': SHARED},
    'audit of changes made by shell commands (project snapshots)': {
        'serve': SHARED, 'copilot': SHARED, 'tui': SHARED},
    'undo and rewind': {
        'serve': SHARED, 'copilot': "n/a: VS Code checkpoints and Keep/Undo",
        'tui': "separate: edit receipts (native_turn_review), which do not cover shell effects"},
    'route escalation (engine.successor decides; the adapter runs it)': {
        'serve': SHARED, 'tui': SHARED,
        'copilot': "separate: signals from the client's flow (a route error before streaming, a fix that fails "
                   "its audit again) raise a per-chat floor that never drops, to keep the prompt cache"},
    'one tier up when a completed turn only announced the work (turn_engine.unanswered, a cheap judge)': {
        'serve': SHARED, 'copilot': SHARED,
        'tui': SHARED},  # the TUI: tare auto conversations, on turns that changed no files (the audit covers those)
    'leaving a vendor account whose usage ran out until it resets (kernel CLI_USAGE_LIMIT, turn_engine.note_usage_limit)': {
        'serve': SHARED, 'tui': SHARED,
        'copilot': "n/a: tare-copilot relays only HTTP routes, never the CLIs; available() still skips the "
                   "codex-native routes of a spent ChatGPT account"},
    "stuck detection (the runtime's no-progress rule: 3 rounds that repeat what was seen)": {
        'serve': SHARED, 'tui': SHARED, 'copilot': SHARED},  # serve and TUI in the runtime; Copilot via the engine
    'machine-wide workspace locks': {
        'serve': SHARED, 'tui': SHARED, 'copilot': "n/a: VS Code edits the files, not a tare session"},
    'a conversation without local models, chosen per conversation (turn_engine.LOCAL_MODELS)': {
        'tui': SHARED,  # its Local models option; the sessions get --no-local-models
        'serve': SHARED,  # /local-gpu off|on, also as the first message with a request
        'copilot': SHARED},  # /local-gpu off|on, for that chat
    'leaving the shared local GPU to others (turn_engine.leaves_gpu_to_others, saved once per machine)': {
        'tui': SHARED,  # its option
        'serve': SHARED,  # /local-gpu off|on in a chat
        'copilot': SHARED},  # /local-gpu off|on in a chat too; its routes follow the machine's choice
    'heads-up notes after a finished turn (relay/heads_up.py: a filter over the counts tare keeps, a writer, Jev dropping repeats)': {
        'tui': SHARED,  # /heads-up puts the last notes in the composer; the option turns them off for the machine
        'serve': SHARED,  # said in the chat and kept as a note for the session's next turn
        'copilot': SHARED},  # said in the stream; VS Code's history carries them to the model's next request
    'reading a folder outside the project after asking the user (relay/folder_access.py)': {
        'tui': SHARED,  # the tools live in the native session; the TUI asks and saves the grant per project
        'serve': SHARED,  # same grants file as the TUI (folder_access.project_store); the next message answers
        'copilot': "n/a: VS Code reads the files and asks for its own tool approvals"},
}
