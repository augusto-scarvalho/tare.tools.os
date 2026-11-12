"""Read-only project/model observations for the conversation header."""
import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
from compute_plane.managed_process import host_executable
import time
import unicodedata

from rich.text import Text
from textual import work
from textual.widgets import Button, Checkbox, Input, Select, Static

from .console_output import plain, show


SPINNER = '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
PRESENCE = {'ready': '○', 'working': SPINNER[0], 'waiting': '?', 'finished': '✓', 'cancelled': '■', 'error': '!'}
PRESENCE_LABELS = {'ready': 'Ready', 'working': 'Working · model or tools', 'waiting': 'Waiting for your input',
                   'finished': 'Turn finished', 'cancelled': 'Stopped', 'error': 'Execution needs attention'}
WAITING = {'WAITING_FOR_HUMAN', 'WAITING_INPUT', 'NEEDS_CLARIFICATION', 'PAUSED', 'HUMAN_DECLINED'}


def presence_state(*, busy, waiting=False, cancelled=False, failed=False, result=None):
    if busy:
        return 'waiting' if waiting else 'working'
    result = result or {}
    task = result.get('conversation_task') or {}
    status = result.get('status', '')
    if waiting or status in WAITING or task.get('stage') in WAITING:
        return 'waiting'
    if cancelled or status in {'CANCELLED', 'CANCELED', 'ABORTED'}:
        return 'cancelled'
    if failed or status in {'BLOCKED', 'FAILED', 'ERROR', 'RECONCILIATION_REQUIRED', 'UNAVAILABLE', 'TIMED_OUT', 'EXHAUSTED', 'REJECTED'}:
        return 'error'
    if status in {'PROPOSED', 'COMPLETED', 'SUCCEEDED', 'DELIVERED', 'REVIEWED', 'ACCEPTED', 'DONE', 'FINISHED', 'APPLIED', 'VERIFIED', 'SETTLED'}:
        return 'finished'
    return 'waiting' if result else 'ready'


def presence_title(symbol, root):
    # OSC data must not contain terminators, controls or surrogate code points. Keep well below Windows' limit.
    name = Path(root).name or Path(root).anchor or 'workspace'
    name = ' '.join(''.join(c for c in name if unicodedata.category(c) not in {'Cc', 'Cs', 'Zl', 'Zp'}).split())
    return f'{symbol} - tare - {name[:120] or "workspace"}'


def label(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise ValueError('invalid_header_label')
    cleaned = ' '.join(plain(value).split())
    if not cleaned:
        raise ValueError('empty_header_label')
    return cleaned


@dataclass(frozen=True)
class HeaderDetails:
    branch: str
    model: str
    source: str
    identity: str | None = None
    reasoning: str = ''
    display_model: str = ''


def run_git(root, *args, timeout=2, text=True):
    """Read-only git in `root`: no optional locks (never waits on an agent's index lock), no prompt, no inherited
    repository variables, the bundled git."""
    env = dict(os.environ, GIT_OPTIONAL_LOCKS='0', GIT_TERMINAL_PROMPT='0')
    for key in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR'):
        env.pop(key, None)
    # log.showSignature would run gpg (or ssh-keygen) for every signed commit a log or show prints.
    return subprocess.run([host_executable('git', required=True), '-c', 'core.fsmonitor=false', '-c', 'core.quotepath=false',
                           '-c', 'log.showSignature=false', '-C', str(root), *args], capture_output=True, timeout=timeout, env=env,
                          **({'encoding': 'utf-8', 'errors': 'replace'} if text else {}))


def git_branch(root):
    """Observe symbolic/unborn and detached HEAD without changing Git state."""
    def git(*args):
        return run_git(root, *args)
    try:
        result = git('symbolic-ref', '--quiet', '--short', 'HEAD')
        if result.returncode == 0:
            return label(result.stdout.strip())
        if result.returncode == 1:
            result = git('rev-parse', '--short', 'HEAD')
            if result.returncode == 0:
                return 'detached '+label(result.stdout.strip())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return 'No Git branch'


def agents_edit(root):
    """The SHA-256 (or 'absent') of a root AGENTS.md that differs from HEAD; None when it matches or outside Git."""
    from .project_admission import AdmissionRejected, local_edit, preview
    try:
        return (preview(root)['root_sha256'] or 'absent') if local_edit(root) else None
    except (AdmissionRejected, OSError, ValueError):
        return None


def agents_change(root):
    """The root AGENTS.md's recent committed change (project_admission.recent_change), or None."""
    from .project_admission import recent_change
    try:
        return recent_change(root)
    except Exception:
        return None


def read_details(key):
    root, session, profile, deployment, automatic = key
    branch = git_branch(root)
    try:
        from .console_connection import deployment_for, deployment_identity
        chosen = deployment_for(key)
        if chosen is None:
            return HeaderDetails(branch, 'Auto-select on start', 'Next execution')
        if 'native_auto_routes' in chosen:
            return HeaderDetails(branch,'tare auto','Next execution',deployment_identity(key,chosen),'','tare auto')
        from .model_choices import LABELS
        from .execution_team import reasoning
        effort = reasoning(chosen) or ''
        metadata = chosen.get('native_model_choice', {})
        model = label(chosen['native_profile']['model'])
        return HeaderDetails(branch, model, 'Saved model' if session else 'Next execution',
                             deployment_identity(key, chosen), effort,
                             metadata.get('label', LABELS.get(model, model)))
    except Exception:
        # A label is optional observation, never authority to replace a profile,
        # leak descriptor/credential content, or prevent interaction with chat.
        return HeaderDetails(branch, 'Saved model unavailable' if session else 'Model profile unavailable',
                             'Saved model' if session else 'Next execution')


class ConversationHeader:
    def reset_presence(self):
        self.presence_failed = self.presence_cancelled = False

    def refresh_presence(self):
        """Animate a fixed cell even before the first token; never poll a model, file or Git here."""
        if self.connection_closing or not self.is_mounted:
            return
        symbol = self.query_one_optional('#activity-symbol', Static)
        if symbol is None:
            return
        waiting = bool(self.folder_request) or any(
            panel is not None and panel.is_mounted
            for panel in (getattr(self, '_command_permission_panel', None),
                          getattr(self, '_execution_permission_panel', None)))
        kind = presence_state(busy=self.busy, waiting=waiting, cancelled=self.presence_cancelled,
                              failed=self.presence_failed, result=self.last_result)
        frame = SPINNER[self.presence_step % len(SPINNER)] if kind == 'working' else PRESENCE[kind]
        self.presence_step = (self.presence_step + 1) % len(SPINNER) if kind == 'working' else 0
        show(symbol, frame, layout=False)
        # The same frame and the same status text as the top row (role, model, state, time: set_status), above the
        # input box where the owner looks. Nothing is shown while idle, so no stale model or role stays behind.
        near = self.query_one_optional('#activity-near-composer', Static)
        if near is not None:
            show(near, Text(f'{frame} {getattr(self, "status_text", "")}', no_wrap=True, overflow='ellipsis')
                 if self.busy else '', layout=False)
        if kind != self.presence_kind:
            self.presence_kind = kind
            symbol.tooltip = PRESENCE_LABELS[kind]
        # The Textual title is static (its hidden Header need not recompose every frame).
        title = presence_title(PRESENCE[kind], self.workspace_root)
        if self.title != title:
            self.title = title
        self.write_terminal_title(presence_title(frame, self.workspace_root))
        if self.presence_timer is not None:
            self.presence_timer.resume() if self.busy else self.presence_timer.pause()

    def write_terminal_title(self, title):
        # Textual's console writes to a null file; use its terminal driver, as OSC 52 does.
        if self._driver is None or self.is_headless or self.is_web or title == self.terminal_title:
            return
        try:
            if not self.terminal_title_saved:
                self._driver.write('\x1b[22;2t')  # Save the shell's window title when supported.
                self.terminal_title_saved = True
            self._driver.write('\x1b]2;'+title+'\x1b\\')
            self._driver.flush()
            self.terminal_title = title
        except (OSError, ValueError):
            pass  # An unavailable terminal must never interrupt an agent operation.

    def restore_terminal_title(self):
        if self.presence_timer is not None:
            self.presence_timer.stop()
        if not self.terminal_title_saved or self._driver is None:
            return
        try:
            # A terminal without title-stack support still must not retain an animated working title.
            self._driver.write('\x1b]2;'+presence_title(PRESENCE['ready'], self.workspace_root)+'\x1b\\\x1b[23;2t')
            self._driver.flush()
        except (OSError, ValueError):
            pass
        self.terminal_title_saved = False

    def header_key(self):
        if self.assistant_session:
            return (self.workspace_root, self.assistant_session, '', '', False)
        if getattr(self, 'conversation_coordinator', ''):
            # A new conversation runs the pinned coordinator (--coordinator), not the saved model preference.
            return (self.workspace_root, '', '', self.conversation_coordinator, False)
        return (self.workspace_root, self.assistant_session or '',
                self.query_one('#profile', Input).value.strip(),
                str(self.query_one('#model', Select).value),
                self.query_one('#auto-execution', Checkbox).value)

    def sync_header(self, *, force=False):
        if self.connection_closing or not self.is_mounted or not self.query_one_optional('#session-context'):
            return
        if self.query_one('#mode', Select).value != 'assistant':
            return
        key = self.header_key()
        changed = key != self.header_identity
        if changed:
            self.cancel_connection_check()
            self.connection_observation = None
            self.header_identity, self.header_details = key, None
        self.render_header()
        if force and self.header_loading:
            self.header_refresh_pending = True
        if (changed or force or time.monotonic()-self.header_observed >= 5) and not self.header_loading:
            self.header_loading = True
            self.observe_header(key)

    def render_header(self):
        key, details = self.header_identity, self.header_details
        root, session, profile, deployment, automatic = key
        heading = Text(no_wrap=True, overflow='ellipsis')
        heading.append(' '.join(plain(Path(root).name or root).split()) or 'Workspace', style='bold')
        heading.append('  ·  '+(details.branch if details else 'Reading branch…'), style='dim')
        widget = self.query_one('#flow-heading', Static)
        show(widget, heading, layout=False)  # one fixed line
        widget.tooltip = Text(plain(root)+'\n'+(details.branch if details else 'Reading Git metadata'))
        model = details.model if details else 'Reading saved model…' if session else 'Reading model…'
        self.render_model_controls(details)
        source = details.source if details else 'Saved model' if session else 'Next execution'
        context = self.query_one('#session-context', Static)
        observation = self.connection_observation
        health = ('Checking connection…' if self.connection_loading else
                  observation.summary() if observation else 'Connection unchecked')
        # A project's AGENTS.md reaches agents only once adopted in /permissions; unsaid, a planner broke its rules.
        # The welcome of a new conversation says it, where adoption applies and the sentence fits (this line cut it).
        policy = getattr(self, 'project_admission', None) or {}
        edit = getattr(self, '_agents_edit', None)
        unread = ('This project\'s AGENTS.md is not loaded, so agents do not follow its rules. Review it in '
                  '/permissions to trust this repository\'s AGENTS.md in new conversations.'
                  if policy.get('mode') not in ('adopted', 'trusted') and (Path(root)/'AGENTS.md').is_file() else
                  'AGENTS.md has uncommitted changes that are not loaded (the committed version is). Review them in '
                  '/permissions to load them.' if policy.get('mode') == 'trusted' and edit
                  and edit != (policy.get('root_sha256') or 'absent') else '')
        welcome = self.query_one_optional('#welcome', Static)
        change = getattr(self, '_agents_change', None)
        # Committed rules load unreviewed, and agents commit and merge here too: every new conversation's welcome
        # says a recent change, so the operator sees it even when an agent drove the TUI first.
        changed = (f"AGENTS.md changed {change['when']} in {change['commit'][:8]} \"{change['subject']}\" "
                   f"(+{change['added']} -{change['removed']} lines), and new conversations follow it; "
                   'git log -p -1 -- AGENTS.md shows the change.'
                   if policy.get('mode') == 'trusted' and change else '')
        unread = '\n\n'.join(n for n in (unread, changed) if n)
        if welcome is not None and getattr(self, '_unread_note', None) != unread:
            from .console_output import welcome_text
            self._unread_note = unread
            welcome.update(welcome_text(unread))
        disk = getattr(self, '_storage_warning', '')
        suffix = '  ·  '+health+('  ·  '+disk if disk else '')
        value = Text(model, no_wrap=True, overflow='ellipsis')
        value.truncate(max(8, context.size.width-len(suffix)), overflow='ellipsis')
        value.append('  ·  '+health)
        if disk:
            value.append('  ·  '+disk, style='bold '+(self.current_theme.warning or 'yellow'))
        show(context, value, layout=False)  # one fixed line
        detail = ('No connection check yet. Use Check to make one read-only request.' if not observation else
                  observation.detail+'\nObserved '+observation.observed_at+' · '+str(observation.duration_ms)+' ms')
        context.tooltip = Text(model+'\n'+source+'\n'+detail)
        if self.query_one_optional('#connection-details'):
            show(self.query_one('#connection-details', Static), plain(model+'  ·  '+source+'\n'+health+'\n'+detail))
        button = self.query_one('#connection-check', Button)
        button.label = 'Cancel check' if self.connection_loading else 'Check'
        button.styles.min_width = 15 if self.connection_loading else 8
        button.disabled = False

    @work(group='header')
    async def observe_header(self, key):
        try:
            while True:
                self.header_refresh_pending = False
                # A new execution reserves its path before the runtime writes the descriptor.
                # Keep the existing loading state; never substitute a future model preference.
                pending = False
                for _ in range(10):
                    pending = bool(key[1] and self.busy and key == self.header_key()
                                   and not await asyncio.to_thread((Path(key[1])/'native-agent.json').exists))
                    if not pending:
                        break
                    if self.connection_closing or not self.is_mounted:
                        return
                    await asyncio.sleep(.1)
                details = await asyncio.to_thread(read_details, key)
                self._agents_edit = await asyncio.to_thread(agents_edit, key[0])
                self._agents_change = await asyncio.to_thread(agents_change, key[0])
                if pending and self.busy and details.identity is None:
                    details = HeaderDetails(details.branch, 'Reading saved model…', 'Saved model')
                if (self.connection_closing or not self.is_mounted or not self.query_one_optional('#session-context')
                        or self.query_one('#mode', Select).value != 'assistant'):
                    return
                current = self.header_key()
                if self.header_refresh_pending or key != current:
                    key = current
                    continue
                self.header_details = details
                if (self.connection_observation and self.connection_observation.identity is not None
                        and self.connection_observation.identity != details.identity):
                    self.connection_observation = None
                self.render_header()
                return
        finally:
            self.header_loading = False
            self.header_observed = time.monotonic()
            self.header_refresh_pending = False

    def cancel_connection_check(self):
        if self.connection_cancellation is not None:
            self.connection_cancellation.cancel()

    def action_check_connection(self):
        if (self.connection_closing or not self.query_one_optional('#mode')
                or self.query_one('#mode', Select).value != 'assistant'):
            return
        if self.connection_loading:
            self.cancel_connection_check()
            return
        from tare_tools_agent_runtime import CancellationToken
        self.connection_observation = None
        self.sync_header(force=True)
        self.connection_loading = True
        self.connection_cancellation = CancellationToken()
        self.render_header()
        self.observe_connection(self.header_key(), self.connection_cancellation)

    @work(group='connection')
    async def observe_connection(self, key, cancellation):
        from .console_connection import probe, target_for
        task = asyncio.create_task(asyncio.to_thread(probe, key, cancellation))
        self.connection_task = task
        try:
            observation = await asyncio.shield(task)
            if not self.connection_closing and self.is_mounted and key == self.header_key():
                try:
                    current = await asyncio.to_thread(target_for, key)
                    identity = current.identity if current else None
                except Exception:
                    identity = None
                if key == self.header_key() and identity == observation.identity:
                    self.connection_observation = observation
        except asyncio.CancelledError:
            cancellation.cancel()
            await asyncio.shield(task)  # The transport owner must finish cleanup.
            raise
        finally:
            if self.connection_task is task:
                self.connection_task = None
                self.connection_loading = False
                self.connection_cancellation = None
            if (not self.connection_closing and self.is_mounted and self.query_one_optional('#session-context')
                    and self.query_one('#mode', Select).value == 'assistant'):
                self.sync_header(force=True)
