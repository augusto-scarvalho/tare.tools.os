"""Supervised background execution of active conversation turns inside the TUI."""
import asyncio
from dataclasses import dataclass, field, replace
import json
from pathlib import Path
import time
from uuid import uuid4

from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, OptionList, Static

from . import relay_mesh
from .console_output import plain
from .console_panels import SupportPanel

MAX_BUFFERED_EVENTS = 200
ACTIVE_STATES = {'running', 'needs_answer'}


@dataclass
class BackgroundRun:
    """One detached native conversation subprocess owned and supervised by the TUI."""
    session: str
    demand_id: str | None
    root: str
    title: str
    selection: object
    process: object
    completion: asyncio.Task
    clock: object
    started: float
    invocation_id: str | None
    sink: list
    command_approval_root: str = ''
    status: str = 'running'          # 'running' | 'needs_answer' | 'done' | 'failed' | 'cancelled'
    detail: str = ''
    result: dict | None = None
    exit_code: int | None = None
    stderr_text: str = ''
    events: list = field(default_factory=list)
    delivered: bool = False
    pending_kind: str | None = None  # 'execution' | 'command'
    pending_request_id: str | None = None
    notified_request_id: str | None = None
    cancel_requested: bool = False
    watch_task: asyncio.Task | None = None
    ended_at: float | None = None

    @property
    def is_active(self):
        return self.status in ACTIVE_STATES

    def elapsed_seconds(self):
        end = self.ended_at if self.ended_at is not None else self.clock()
        return max(0.0, end - self.started)


class BackgroundPanel(SupportPanel):
    """Inspect, answer, open, or stop turns running in the background."""
    BINDINGS = [Binding('escape', 'close', 'Close', priority=True)]
    DEFAULT_CSS = '''
    BackgroundPanel .support-body { height: auto; max-height: 85%; max-width: 96; }
    BackgroundPanel #bg-list { height: auto; max-height: 16; margin: 1 0; }
    BackgroundPanel #bg-detail { height: auto; max-height: 5; color: $text-muted; margin-bottom: 1; }
    BackgroundPanel #bg-actions { height: auto; }
    BackgroundPanel #bg-actions Button { width: 1fr; height: auto; min-height: 3; }
    '''

    def __init__(self, runs):
        super().__init__()
        self._runs = list(runs)

    def compose(self):
        with Vertical(classes='support-body'):
            yield Static('Background runs', classes='panel-title')
            yield Static('Turns moved to background keep their process supervision and stall alerts.', markup=False)
            yield OptionList(id='bg-list')
            yield Static('', id='bg-detail', markup=False)
            with Horizontal(id='bg-actions'):
                yield Button('Answer', id='bg-answer', classes='panel-primary')
                yield Button('Open', id='bg-open')
                yield Button('Stop', id='bg-stop')
                yield Button('Clear finished', id='bg-clear')
                yield Button('Close', id='bg-close')

    def on_mount(self):
        self.refresh_runs(self._runs)
        self.query_one('#bg-list', OptionList).focus()

    def _selected_run(self):
        menu = self.query_one('#bg-list', OptionList)
        idx = menu.highlighted
        if idx is None or not (0 <= idx < len(self._runs)):
            return None
        return self._runs[idx]

    def refresh_runs(self, runs):
        self._runs = list(runs)
        menu = self.query_one('#bg-list', OptionList)
        prev = menu.highlighted if menu.highlighted is not None else 0
        labels = []
        for run in self._runs:
            elapsed = f'{run.elapsed_seconds():.0f}s'
            badge = run.status.replace('_', ' ')
            title = plain(run.title).replace('\n', ' ').replace('\t', ' ')[:64]
            row = Text(f'[{badge} · {elapsed}] {title}')
            if run.detail:
                row.append('\n  ' + plain(run.detail)[:84], style='dim')
            labels.append(row)
        menu.set_options(labels or [Text('No background runs.')])
        if self._runs:
            menu.highlighted = min(prev, len(self._runs) - 1)
        self._sync_buttons()

    def _sync_buttons(self):
        run = self._selected_run()
        self.query_one('#bg-answer', Button).disabled = run is None or run.status != 'needs_answer'
        self.query_one('#bg-open', Button).disabled = run is None
        self.query_one('#bg-stop', Button).disabled = run is None or not run.is_active
        self.query_one('#bg-clear', Button).disabled = not any(not r.is_active for r in self._runs)
        detail = ''
        if run is not None:
            detail = f'Project: {run.root} · Session: {Path(run.session).name}'
            if run.detail:
                detail += f'\n{run.detail}'
        self.query_one('#bg-detail', Static).update(detail)

    def on_option_list_option_highlighted(self, event):
        if event.option_list.id == 'bg-list':
            self._sync_buttons()

    def on_option_list_option_selected(self, event):
        if event.option_list.id != 'bg-list':
            return
        run = self._selected_run()
        if run is None:
            return
        action = 'answer' if run.status == 'needs_answer' else 'open'
        self.dismiss((action, run.session))

    def on_button_pressed(self, event):
        bid = event.button.id
        if bid not in {'bg-answer', 'bg-open', 'bg-stop', 'bg-clear', 'bg-close'}:
            return
        event.stop()
        if bid == 'bg-close':
            self.dismiss(None)
            return
        if bid == 'bg-clear':
            self.dismiss(('clear', None))
            return
        run = self._selected_run()
        if run is None:
            return
        self.dismiss((bid.removeprefix('bg-'), run.session))

    def action_close(self):
        self.dismiss(None)


class BackgroundExecution:
    """Mixin for WorkConsole: detach active session turns to background and supervise them."""

    def has_active_background_runs(self):
        return any(run.is_active for run in self.background_runs.values())

    def background_run_for_demand(self, demand_id):
        if not demand_id:
            return None
        return next((run for run in self.background_runs.values() if run.demand_id == demand_id), None)

    def background_run_for_session(self, session):
        if not session:
            return None
        return self.background_runs.get(session)

    def _background_title(self, selection):
        if self.demand_state and self.demand_state.get('title'):
            return str(self.demand_state['title'])[:64]
        prompt = (getattr(selection, 'prompt', '') or '').strip().splitlines()
        if prompt:
            return prompt[0][:64]
        return Path(selection.session).name if selection.session else 'Conversation'

    def _refresh_background_panel(self):
        if isinstance(getattr(self, 'screen', None), BackgroundPanel):
            try:
                self.screen.refresh_runs(self.background_runs.values())
            except Exception:
                pass
        if getattr(self, 'panel', None) == 'navigation':
            try:
                self.render_conversations()
            except Exception:
                pass

    def detach_active_to_background(self):
        """Move the currently running session turn to a supervised background slot and unlock the TUI."""
        state = getattr(self, '_active_run_state', None)
        if (not self.busy or state is None or self.cancel_requested
                or self.process is None or self.process.returncode is not None):
            return False
        selection = state['selection']
        if selection.mode != 'session' or not selection.session:
            return False
        if not state.get('conversation_flow'):
            self.set_status('Only conversation-mode turns can move to background; direct sessions run their post-turn audit on completion.')
            return False
        session = selection.session
        run = BackgroundRun(
            session=session,
            demand_id=self.demand_id,
            root=selection.root,
            title=self._background_title(selection),
            selection=selection,
            process=state['process'],
            completion=state['completion'],
            clock=state['clock'],
            started=state['started'],
            invocation_id=state['invocation_id'],
            sink=state['sink'],
            command_approval_root=state.get('command_approval_root') or '',
        )

        def bg_activity(event, target=run):
            if self.assistant_session == target.session and not self.busy:
                self.assistant_activity(event)
            else:
                target.events.append(('activity', event))
                if len(target.events) > MAX_BUFFERED_EVENTS:
                    del target.events[:len(target.events) - MAX_BUFFERED_EVENTS]

        def bg_say(text, target=run):
            if self.assistant_session == target.session and not self.busy:
                self.say(text)
            else:
                target.events.append(('say', text))
                if len(target.events) > MAX_BUFFERED_EVENTS:
                    del target.events[:len(target.events) - MAX_BUFFERED_EVENTS]

        run.sink[0] = bg_activity
        run.sink[1] = bg_say
        self.background_runs[session] = run
        self._detached_session = session
        self._active_run_state = None
        self._execution_permission_seen = None
        state['detach_event'].set()
        run.watch_task = asyncio.create_task(self._watch_background_run(run))
        self.set_status(f'Running in background: {run.title} · /new or F1 to work elsewhere · /bg to manage')
        self.notify(f'Moved to background: {run.title} · use /bg to inspect, answer, or stop.')
        self._refresh_background_panel()
        return True

    async def _watch_background_run(self, run):
        from . import command_permission
        from .conversation_permissions import pending as permission_pending
        try:
            while True:
                while not run.completion.done():
                    await asyncio.wait({run.completion}, timeout=1)
                    if run.completion.done():
                        break
                    req = kind = None
                    try:
                        req = permission_pending(run.session)
                        if req:
                            kind = 'execution'
                    except (OSError, ValueError):
                        req = None
                    if req is None and run.command_approval_root:
                        try:
                            req = command_permission.pending(run.command_approval_root)
                            if req:
                                kind = 'command'
                        except (OSError, ValueError):
                            req = None
                    if req:
                        run.status = 'needs_answer'
                        run.pending_kind = kind
                        run.pending_request_id = req['id']
                        run.detail = (req.get('objective') or 'Waiting for command approval'
                                      if kind == 'command' else req.get('objective') or 'Waiting for your answer')[:140]
                        if run.notified_request_id != req['id']:
                            run.notified_request_id = req['id']
                            self.notify(f'Background run needs your answer: {run.title} · open /bg to respond.',
                                        severity='warning')
                            self._refresh_background_panel()
                    elif run.status == 'needs_answer':
                        run.status = 'running'
                        run.pending_kind = None
                        run.pending_request_id = None
                        run.detail = ''
                        self._refresh_background_panel()
                stdout, stderr = await run.completion
                run.clock.resume()
                run.exit_code = run.process.returncode
                run.stderr_text = stderr.decode('utf-8', errors='replace') if stderr else ''
                try:
                    run.result = json.loads(stdout)
                except (ValueError, UnicodeError):
                    run.result = None
                run.pending_kind = None
                run.pending_request_id = None
                if (not run.cancel_requested and not self.close_after_run
                        and await self._try_background_auto_escalate(run)):
                    continue
                run.ended_at = run.clock()
                if run.cancel_requested:
                    run.status = 'cancelled'
                    run.detail = 'Cancelled'
                elif (run.result is not None and run.exit_code == 0
                      and run.result.get('status') in {'PROPOSED', 'REVIEWED', 'COMPLETED', 'SETTLED'}):
                    run.status = 'done'
                    run.detail = str(run.result.get('status'))
                else:
                    run.status = 'failed'
                    run.detail = str((run.result or {}).get('status') or f'exit {run.exit_code}')
                self._deliver_background_completion(run)
                break
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            run.status = 'failed'
            run.detail = str(exc)[:140]
            run.ended_at = run.clock()
        finally:
            self._refresh_background_panel()
            if self.close_after_run and not self.busy and not self.has_active_background_runs():
                self.exit()

    async def _try_background_auto_escalate(self, run):
        """Mirror foreground tare auto route escalation when a detached background turn stops recoverably."""
        from .native_agent import read_json, session_root
        from .native_continuity import preview, save_request
        from .turn_engine import STOPPED, local_models, next_auto, successor
        status = (run.result or {}).get('status')
        if run.selection.mode != 'session' or not run.session or status not in STOPPED:
            return False
        try:
            state = read_json(Path(run.session) / 'native-agent.json', 1024 * 1024)
            saved = state['deployment']
            if not saved.get('native_auto_selection'):
                return False
            role = 'coordinator' if state.get('conversation_flow') else state.get('config', {}).get('role', 'implementer')
            if successor(status, per_message_limits=False) == 'stop':
                run.sink[1](
                    f"tare auto: this conversation spent the budget you set, so it stays on "
                    f"{saved['native_auto_selection']['route']}. Raise the limit, or start a new conversation, "
                    f"to continue on a stronger route."
                )
                return False
            with local_models(run.selection.local_models):
                deployment = next_auto(saved, role=role)
            if deployment is None:
                run.sink[1]('tare auto: no further route is available.')
                return False
            terminal_conn = getattr(self.backend, 'continuity_terminal', lambda _s: None)(run.session)
            value = await asyncio.to_thread(preview, run.session, run.root, terminal_connection=terminal_conn)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.notify('tare auto: ' + str(exc), severity='error')
            return False
        if run.cancel_requested or self.close_after_run:
            return False
        if deployment['native_profile']['protocol'] != 'vendor-cli':
            deployment = {**deployment, 'native_streaming': run.selection.streaming}
        route = deployment['native_auto_selection']['route']
        brief = value['text'] + '\nContinue the task from the brief and history.'
        new_session = str(session_root() / uuid4().hex)
        save_request(new_session, value, brief, deployment, chosen=False)
        invocation_id = uuid4().hex
        args = ['agent', '--invocation-id', invocation_id, '--continue-provider', '--session', new_session,
                '--format', 'json', '--activity-events']
        if run.stderr_text:
            run.sink[1](run.stderr_text)
        run.sink[1](f"tare auto: {saved['native_auto_selection']['route']} stopped; continuing on {route}.")
        from .execution_scope import for_coordinator
        prev_approval = getattr(self.backend, 'action_approval_root', None)
        coordinator = (getattr(run.selection, 'execution_policy', None) or {}).get('coordinator', '')
        try:
            with for_coordinator(coordinator):
                process = await self.backend.launch(args, run.root)
            launched_approval = getattr(process, 'action_approval_root', None)
            if launched_approval is None:
                current_approval = getattr(self.backend, 'action_approval_root', '')
                launched_approval = current_approval if current_approval != prev_approval else ''
        finally:
            if self.busy and prev_approval is not None:
                self.backend.action_approval_root = prev_approval
        if run.cancel_requested or self.close_after_run:
            try:
                self.backend.cancel(process)
            except (OSError, ProcessLookupError):
                pass
            try:
                await process.communicate()
            except Exception:
                pass
            return False
        old_session = run.session
        run.session = new_session
        run.selection = replace(run.selection, session=new_session, prompt='')
        run.process = process
        run.invocation_id = invocation_id
        run.command_approval_root = launched_approval
        run.status = 'running'
        run.detail = f'Escalated to {route}'
        run.result = None
        run.exit_code = None
        run.stderr_text = ''
        run.ended_at = None
        run.notified_request_id = None
        self.background_runs.pop(old_session, None)
        self.background_runs[new_session] = run
        self._record_background_escalation_demand(run, old_session, new_session, invocation_id, len(brief.encode()))
        from .console_output import observe_process
        run.completion = asyncio.create_task(observe_process(
            process, lambda ev: run.sink[0](ev), lambda text: run.sink[1](text)))
        self.notify(f'Background run escalated to {route}: {run.title}.')
        self._refresh_background_panel()
        return True

    def _record_background_escalation_demand(self, run, old_session, new_session, invocation_id, brief_bytes):
        """Point the saved conversation demand (and open view if still on it) at the escalated continuity session."""
        notice = f'Provider changed explicitly. Continuity brief: {brief_bytes} bytes. Previous history kept locally.'
        if self.assistant_session == old_session and not self.busy:
            self.assistant_session = new_session
            self.remember_message(notice)
        if not run.demand_id:
            return
        from . import console_demand
        try:
            saved_demand = console_demand.read(run.demand_id, run.root, relay_mesh.RELAY_ROOT)
            snapshot = dict(saved_demand['snapshot'])
            snapshot['assistant_session'] = new_session
            snapshot['selection'] = {**(snapshot.get('selection') or {}), 'mode': 'session', 'session': new_session, 'prompt': ''}
            history = list(snapshot.get('assistant_history') or [])
            if not history or history[-1] != notice:
                history.append(notice)
            snapshot['assistant_history'] = history
            updated = console_demand.save(
                run.demand_id, saved_demand['revision'], run.root, relay_mesh.RELAY_ROOT,
                'execution_requested', snapshot,
                {'resume': False, 'replan': False, 'operator': '', 'invocation_id': invocation_id,
                 'context_root': '', 'context_paths': [], 'attachments': [], 'demand_sequence': False},
            )
            if self.demand_id == run.demand_id:
                self.demand_revision = updated['revision']
                self.demand_state = updated
        except Exception:
            pass

    def _flush_background_run_to_view(self, run):
        """Render buffered events and final result when the run's conversation is open."""
        from .console_output import Transcript
        if run.events:
            buffered = list(run.events)
            run.events.clear()
            for kind, payload in buffered:
                if kind == 'activity':
                    self.assistant_activity(payload)
                else:
                    self.say(payload)
        if run.is_active or run.delivered:
            return
        run.delivered = True
        try:
            self.query_one('#output', Transcript).finish_stream()
        except Exception:
            pass
        self.finish_activity()
        if run.stderr_text:
            self.say(run.stderr_text)
        if run.result is not None:
            self.last_result = run.result
            for row in run.result.get('tool_activity', {}).get('items', []):
                self.assistant_activity({'channel': 'activity', 'activity': row})
            if 'status' in run.result:
                from .native_display import render_result
                if self.selection().mode == 'assistant':
                    self.assistant_result(run.result, exit_code=run.exit_code)
                else:
                    self.say(render_result(run.result), literal=True)
                self.present_session_consent(run.result)
            from .console_demand import result_summary
            self.checkpoint('execution_observed', result_summary(run.result), required=False)
        self.set_status(f'Background run {run.status} ({run.detail}) · {run.elapsed_seconds():.1f}s')

    def _deliver_background_completion(self, run):
        if self.assistant_session == run.session and not self.busy:
            self._flush_background_run_to_view(run)
            self.notify(f'Background run finished ({run.detail}): {run.title}.')
        else:
            self.notify(f'Background run finished ({run.detail}): {run.title} · open /bg to view.')

    def answer_background_permission(self, run):
        from . import command_permission
        from .conversation_permissions import answer, pending, plan_text
        from .execution_permission_panel import ExecutionPermissionPanel
        request = kind = None
        try:
            request = pending(run.session)
            if request:
                kind = 'execution'
        except (OSError, ValueError) as exc:
            self.notify(str(exc), severity='error')
            return
        if request is None and run.command_approval_root:
            try:
                request = command_permission.pending(run.command_approval_root)
                if request:
                    kind = 'command'
            except (OSError, ValueError) as exc:
                self.notify(str(exc), severity='error')
                return
        if not request:
            run.status = 'running' if run.is_active else run.status
            run.pending_kind = None
            run.pending_request_id = None
            self.notify('This background run no longer has a pending question.')
            self._refresh_background_panel()
            return
        if kind == 'execution' and 'artifact' in request:
            try:
                request = {**request, 'plan_text': plan_text(run.session, request)}
            except (OSError, ValueError, KeyError, TypeError) as exc:
                request = {**request, 'plan_text': 'The saved plan cannot be shown: ' + str(exc)}
        panel = ExecutionPermissionPanel(request)

        def decided(choice):
            try:
                if kind == 'command':
                    chosen = choice or 'deny'
                    command_permission.answer(run.command_approval_root, request['id'], chosen)
                else:
                    chosen = choice or ('once' if request.get('kind') == 'stall' else 'deny')
                    answer(run.session, request['id'], chosen, project=run.root,
                           directory=self.permissions_directory(run.root), control_root=relay_mesh.RELAY_ROOT,
                           changes=panel.changes)
                run.pending_kind = None
                run.pending_request_id = None
                if run.is_active:
                    run.status = 'running'
                    run.detail = ''
                self.notify(f'Answered background run ({chosen}): {run.title}.')
            except (OSError, ValueError) as exc:
                self.notify(str(exc), severity='error')
            self._refresh_background_panel()

        self.push_screen(panel, decided)

    def open_background_run(self, run):
        if self.busy or self.sending_message:
            self.notify('Move or finish the current foreground turn before switching conversations.')
            return
        if run.session == self.assistant_session:
            self._flush_background_run_to_view(run)
            return
        if not run.demand_id:
            self.notify('This background run has no saved conversation record yet.')
            return
        if Path(run.root).resolve() != Path(self.workspace_root).resolve():
            self.notify(f'Switch workspace to {run.root} to open this background conversation.')
            return
        from . import console_demand
        try:
            # Validate destination and preserve any unsent draft/context before switching.
            console_demand.read(run.demand_id, self.workspace_root, relay_mesh.RELAY_ROOT)
            if (self.demand_id or self.selection().prompt.strip() or self.context_paths
                    or self.attachments or self.assistant_history):
                self.checkpoint('draft_saved')
        except Exception as exc:
            self.notify('Cannot switch conversations: ' + str(exc), severity='error')
            return
        from textual.widgets import Select
        widget = self.query_one('#saved-demand', Select)
        widget.set_options([(Text(plain(run.title)), run.demand_id)])
        widget.value = run.demand_id
        self.open_demand()
        if self.assistant_session == run.session:
            self._flush_background_run_to_view(run)

    def stop_background_run(self, run):
        if not run.is_active:
            return
        run.cancel_requested = True
        run.detail = 'Cancellation requested'
        if run.process is not None and run.process.returncode is None:
            try:
                self.backend.cancel(run.process)
                self.notify(f'Stopping background run: {run.title}.')
            except (OSError, ProcessLookupError) as exc:
                self.notify(f'Could not stop background run: {exc}', severity='error')
        self._refresh_background_panel()

    def cancel_background_runs(self):
        for run in list(self.background_runs.values()):
            if run.is_active:
                run.cancel_requested = True
                if run.process is not None and run.process.returncode is None:
                    try:
                        self.backend.cancel(run.process)
                    except (OSError, ProcessLookupError):
                        pass

    def open_background_panel(self):
        if isinstance(self.screen, BackgroundPanel):
            self.screen.action_close()
            return
        if not self.background_runs:
            self.set_status('No background runs. Press Ctrl+B or type /bg during a turn to move it to background.')
            return

        def handled(outcome):
            if not outcome:
                return
            action, session = outcome
            if action == 'clear':
                for sid in [k for k, v in self.background_runs.items() if not v.is_active]:
                    del self.background_runs[sid]
                self.set_status('Cleared finished background runs.')
                return
            run = self.background_runs.get(session)
            if run is None:
                return
            if action == 'answer':
                self.answer_background_permission(run)
            elif action == 'open':
                self.open_background_run(run)
            elif action == 'stop':
                self.stop_background_run(run)

        self.push_screen(BackgroundPanel(self.background_runs.values()), handled)

    def action_background(self):
        """Ctrl+B / /bg: detach active session turn to background, or open the Background runs panel."""
        if isinstance(self.screen, BackgroundPanel):
            self.screen.action_close()
            return
        if self.busy and getattr(self, '_active_run_state', None) is not None:
            if self.detach_active_to_background():
                return
        self.open_background_panel()
