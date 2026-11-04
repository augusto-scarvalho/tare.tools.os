"""Demand navigation for the existing Textual console; no execution dispatcher."""
import asyncio
from dataclasses import asdict, replace
import json
import os
from pathlib import Path

from textual import work
from textual.css.query import NoMatches
from textual.widgets import Input, Select, TextArea, Checkbox

from . import console_demand, relay_mesh
from .console_backend import Selection, saved_selection

# Textual 8 names the empty selection NULL; Textual 6 uses BLANK.
EMPTY_SELECTION = getattr(Select, 'NULL', Select.BLANK)


class DemandConsole:
    def assistant_result(self, result, *, exit_code=None):
        from .native_display import conversation_result
        value = conversation_result(result, exit_code=exit_code)
        self.query_one('#output').write(value)
        self.remember_message(value)

    def remember_message(self, value):
        from .native_activity import retained_history
        self.assistant_history = retained_history([*self.assistant_history, value])

    def assistant_activity(self, event):
        if event.get('channel') == 'narration':
            self.query_one('#output').narration(event)
            self.remember_message(event['text'])  # reopening shows what the coordinator said while it worked
            return
        if event.get('channel') != 'activity':
            self.query_one('#output').stream(event)
            return
        from .native_activity import activity_key, activity_view
        value = activity_view(event['activity'])
        self.query_one('#output').tool_activity(value)
        for index, row in enumerate(self.assistant_history):
            if isinstance(row, dict) and row.get('kind') == 'tool-activity' and activity_key(row) == activity_key(value):
                self.assistant_history[index] = value
                break
        else:
            self.remember_message(value)

    def finish_activity(self):
        from .native_activity import stopped_activity
        for row in list(self.assistant_history):
            if isinstance(row, dict) and row.get('kind') == 'tool-activity' and row.get('state') == 'running':
                self.assistant_activity({'channel': 'activity', 'activity': stopped_activity(row)})

    def assistant_say(self, text):
        self.say(text)
        # Keep a bounded display transcript; native sessions own full execution evidence.
        self.remember_message(str(text)[:16000])

    def prepare_workspace(self, *, save_origin=False):
        """Commit a workspace choice before user actions; never retarget an old demand."""
        selected = self.selection()
        root = str(Path(selected.root).resolve())
        if not Path(root).is_dir():
            raise ValueError('Workspace folder is unavailable: '+root)
        chosen = None
        if self.storage_preferred is not None:
            from .native_installation import workspace_storage
            chosen = workspace_storage(root, self.storage_preferred, self.storage_fallback)
        if root != self.workspace_root:
            if self.busy or self.sending_message:
                raise ValueError('Wait for the current operation before changing workspace.')
            draft = selected.prompt
            if save_origin and (self.demand_id or draft or self.context_paths
                                or self.attachments or self.assistant_history
                                or self.query_one('#message', TextArea).text):
                self.checkpoint('draft_saved', selection=replace(selected, root=self.workspace_root))
            self.new_demand()
            if not save_origin:
                self.query_one('#prompt', TextArea).load_text(draft)
            with self.prevent(Select.Changed):
                for name in ('saved-demand', 'work', 'session', 'saved-research'):
                    self.query_one('#'+name, Select).set_options([])
            self.query_one('#output').clear()
            self.workspace_root = self.root = root
            self.clear_context()
            self.navigation_generation += 1
            self.invalidate_message_observations()
            self.navigation_rows = self.navigation_shown = []
            if self.panel == 'navigation':
                self.load_conversations()
            self.update_mode()
            self.set_status('Workspace changed. Start a new conversation or open one saved for this folder.')
        if chosen is not None:
            os.environ['TARE_STORAGE_ROOT'] = str(chosen)
        self.load_project_permissions(root)
        self.load_project_graph(root)
        return root

    def checkpoint(self, kind, detail=None, *, selection=None, required=True):
        selected = selection or self.selection()
        try:
            snapshot = {'selection': asdict(selected), 'interface_mode': self.query_one('#mode', Select).value,
                'assistant_session': self.assistant_session, 'assistant_history': self.assistant_history,
                'entry_history': list(self.entry_history),
                'entry_questions': list(self.entry_questions),
                'entry_proposal': self.entry_proposal[1] if self.entry_proposal else None,
                'entry_selection': asdict(self.entry_proposal[0]) if self.entry_proposal else None,
                'last_result': console_demand.result_summary(self.last_result),
                'message_draft': self.query_one('#message', TextArea).text,
                'order_draft': self.order_snapshot()}
            value = console_demand.save(self.demand_id, self.demand_revision, selected.root,
                relay_mesh.RELAY_ROOT, kind, snapshot, detail)
            self.demand_id, self.demand_revision = value['id'], value['revision']
            self.demand_state = value
            self.query_one('#demand-status').update('Demand '+self.demand_id[:8]+' · '+console_demand.guidance(value))
            self.stage_options(value)
            return value
        except Exception as exc:
            if required:
                raise
            self.say('Demand checkpoint not saved: '+str(exc)+'. Existing flow receipts remain authoritative.')

    async def confirmed_mutation(self, kind, detail, action, *args, selection=None):
        from uuid import uuid4
        from .console_mutation import record
        if not hasattr(self, '_mutation_lock'):
            self._mutation_lock = asyncio.Lock()
        async with self._mutation_lock:
            scope = {'selection': selection} if selection is not None else {}
            value = self.checkpoint(kind, {**detail, 'mutation_id': uuid4().hex}, **scope)
            task = asyncio.create_task(asyncio.to_thread(record, value, lambda: action(*args)))
            cancelled = False
            while True:
                try:
                    result = await asyncio.shield(task)
                    break
                except asyncio.CancelledError:
                    if task.cancelled():
                        raise
                    # Repeated cancellation must not release serialization
                    # while the non-cancellable owner thread is still writing.
                    cancelled = True
            if cancelled:
                raise asyncio.CancelledError
            return result

    def stage_options(self, value):
        rows = []
        self.demand_stages = {}
        for i, event in enumerate(value['events']):
            selection = event['selection']; mode = selection['mode']
            if mode == 'conversation':
                continue
            key = str(i)
            self.demand_stages[key] = selection
            owner = selection.get('batch' if mode == 'parallel' else mode) or ''
            rows.append((mode+' · '+event['kind']+' · '+owner[-24:], key))
            if event['kind'] in {'graph_applied', 'work_order_applied', 'work_replacement_applied'} or (event['kind'] == 'mutation_reconciled'
                    and event['detail']['operation'] in {'graph_apply_requested', 'work_order_requested', 'work_replacement_requested'}):
                detail = event.get('detail') or {}
                result = detail.get('result') or detail
                for work_id in result.get('work_ids', []):
                    key = str(i)+':'+work_id
                    self.demand_stages[key] = {**selection, 'mode': 'work', 'work': work_id,
                        'graph_path': result.get('graph_path', selection.get('graph_path', ''))}
                    rows.append(('Work · '+work_id, key))
        self.query_one('#demand-stage', Select).set_options(rows)

    def restore_selection(self, payload):
        selected = saved_selection(payload)
        from .execution_scope import Scope, current
        recorded = Scope(**selected.execution_policy).validate()
        # Historical switches are not permission for this instance. Only the
        # coordinator selection survives reopening; current restrictions still apply.
        from dataclasses import replace
        policy = replace(current(), coordinator=recorded.coordinator).validate()
        self.conversation_coordinator = policy.coordinator
        banner = self.query_one('#execution-scope')
        banner.update('This TUI only: '+policy.label())
        banner.display = policy.active
        self.project_admission = dict(selected.project_admission)
        self.permission_write_paths = list(selected.write_paths)
        self.permission_profile_root = str(Path(selected.root).resolve())
        self.permission_profile_error = None
        self.project_graph_path = selected.graph_path
        self.graph_profile_root = str(Path(selected.root).resolve())
        self.graph_profile_error = None
        self.restore_context(selected)
        inputs = {'findings': 'findings', 'findings_sha256': 'findings-sha256', 'finding_items': 'finding-items', 'finding_note': 'finding-note', 'root': 'root', 'profile': 'profile', 'work': 'work-id', 'verifier': 'verifier',
            'plan': 'plan', 'criterion_ids': 'criterion-ids',
            'max_model_calls': 'max-model-calls', 'max_seconds': 'max-seconds',
            'work_verifier': 'work-verifier', 'revision_reason': 'revision-reason', 'revision_paths': 'revision-paths',
            'sequence': 'sequence', 'sequence_works': 'sequence-works',
            'sequence_revision_reason': 'sequence-revision-reason', 'sequence_revision_works': 'sequence-revision-works',
            'batch': 'batch', 'workers': 'workers',
            'research': 'research', 'synthesis': 'synthesis', 'synthesis_sha256': 'synthesis-sha256',
            'plan_reviewer_profile': 'plan-reviewer-profile'}
        selects = {'mode': 'mode', 'deployment': 'model', 'role': 'session-role', 'session': 'session',
                   'source_ref': 'source-ref', 'auditor': 'auditor', 'plan_reviewer': 'plan-reviewer',
                   'outcome_disposition': 'outcome-disposition'}
        with self.prevent(Select.Changed):
            for key, name in inputs.items():
                self.query_one('#'+name, Input).value = getattr(selected, key)
            for key, name in selects.items():
                widget = self.query_one('#'+name, Select); value = getattr(selected, key)
                try:
                    widget.value = value if value else ('' if key in {'auditor', 'plan_reviewer'} else EMPTY_SELECTION)
                except Exception:
                    widget.set_options([(value+' (saved)', value)])
                    widget.value = value
            for key in ('unlimited', 'refresh', 'retry_plan', 'retry_audit', 'auto_planner', 'auto_execution', 'guide_next', 'streaming', 'terminal', 'local_models', 'specgraph_context'):
                self.query_one('#'+key.replace('_', '-'), Checkbox).value = getattr(selected, key)
            for key, name in {'prompt': 'prompt', 'urls': 'research-urls', 'synthesis_inputs': 'synthesis-inputs',
                              'intake_criteria': 'intake-criteria', 'intake_argv': 'intake-argv',
                              'outcome_summary': 'outcome-summary', 'outcome_remaining': 'outcome-remaining',
                              'outcome_resolutions': 'outcome-resolutions'}.items():
                self.query_one('#'+name, TextArea).load_text(getattr(selected, key))
            for name in ('wait-question', 'sequence-wait-question'):
                self.query_one('#'+name, Input).value = selected.wait_before_implement
        self.query_one('#intake-options').display = bool(selected.intake_criteria)
        self.reviewed_findings = None
        self.reviewed_sequence_revision = None
        self.reviewed_outcome = None
        self.reviewed_plan = self.reviewed_preparation = self.reviewed_revision = self.reviewed_decision = None
        self.active = selected
        self.update_mode()

    @work(exclusive=True, group='demand-list')
    async def list_demands(self):
        if self.busy:
            return
        try:
            root = self.prepare_workspace()
            rows, errors = await asyncio.to_thread(console_demand.inventory, root, relay_mesh.RELAY_ROOT)
            if self.busy or self.selection().root != root:
                return
            self.query_one('#saved-demand', Select).set_options([(r['title'], r['id']) for r in rows])
            for error in errors:
                self.say('Demand unavailable: '+error)
            self.set_status(f'{len(rows)} saved demands for this project and control root.')
        except Exception as exc:
            self.set_status('Demand list unavailable: '+str(exc))

    def open_demand(self):
        if self.busy or self.sending_message:
            return
        identity = self.query_one('#saved-demand', Select).value
        if identity is EMPTY_SELECTION:
            return
        try:
            self.navigation_generation += 1
            self.invalidate_message_observations()
            value = console_demand.read(str(identity), self.selection().root, relay_mesh.RELAY_ROOT)
            snapshot = value['snapshot']
            self.restore_selection(snapshot['selection'])
            self.assistant_session = snapshot.get('assistant_session')
            from .folder_access import pending
            request=pending(self.assistant_session) if self.assistant_session else None
            self.folder_request={**request,'session':self.assistant_session} if request else None
            self.assistant_history = snapshot.get('assistant_history', [])
            self.query_one('#output').clear()
            if snapshot.get('interface_mode') == 'assistant':
                self.query_one('#mode', Select).value = 'assistant'
                for message in self.assistant_history:
                    self.query_one('#output').write(message)
            self.restore_order(snapshot.get('order_draft', {}))
            self.demand_id, self.demand_revision, self.demand_state = value['id'], value['revision'], value
            self.entry_history, self.entry_questions = tuple(snapshot['entry_history']), tuple(snapshot['entry_questions'])
            self.entry_root = snapshot['selection']['root']
            self.entry_proposal = (saved_selection(snapshot['entry_selection']), snapshot['entry_proposal']) if snapshot['entry_proposal'] else None
            self.last_result = snapshot['last_result']
            self.reset_presence()
            self.query_one('#message', TextArea).load_text(snapshot.get('message_draft', ''))
            self.stage_options(value)
            if snapshot.get('interface_mode') != 'assistant':
                events = value['events'][-20:]
                if events:
                    details = '\n\n'.join(event['at']+' · '+event['kind']+'\n'+
                        json.dumps(event.get('detail'), ensure_ascii=False) for event in events)
                    self.query_one('#output').activity('Saved flow history', details)
                for row in self.assistant_history:
                    if isinstance(row, dict) and row.get('kind') == 'tool-activity':
                        self.query_one('#output').write(row)
            self.query_one('#demand-status').update('Demand '+self.demand_id[:8]+' · '+console_demand.guidance(value))
            self.update_mode()
            self.set_status('Conversation reopened · saved history and draft. Review changes with F3.'
                            if snapshot.get('interface_mode') == 'assistant' else
                            'Demand reopened. Saved observations; no model or flow was started. Review rechecks current owner state.')
            self.run_worker(self.refresh_messages(), group='message-refresh')
            self.reconcile_demand()
        except Exception as exc:
            self.set_status('Cannot reopen demand: '+str(exc))

    @work(exclusive=True, group='demand-recovery')
    async def reconcile_demand(self):
        if self.busy or not self.demand_state or not self.query_one_optional('#root'):
            return
        from .console_recovery import observe
        identity, revision = self.demand_id, self.demand_revision
        try:
            selected = self.selection()
        except NoMatches:
            return  # The screen may already be dismantling its controls.
        observation = await asyncio.to_thread(observe, self.demand_state)
        try:
            current = self.selection()
        except NoMatches:
            return
        if (not self.query_one_optional('#root') or self.busy or (identity, revision) != (self.demand_id, self.demand_revision)
                or selected != current or observation is None):
            return
        kind = 'session_reconciled' if observation['state'] == 'RECOVERED' else 'session_recovery_checked'
        if observation['mode'] != 'session':
            kind = 'flow_reconciled' if observation['state'] == 'RECOVERED' else 'flow_recovery_checked'
        if 'operation' in observation:
            kind = 'mutation_reconciled' if observation['state'] == 'RECOVERED' else 'mutation_recovery_checked'
        result = observation.get('result')
        detail = {**observation, 'result': console_demand.result_summary(result)}
        prior = next((e for e in reversed(self.demand_state['events'])
            if e['kind'] in {'session_reconciled', 'session_recovery_checked', 'flow_reconciled', 'flow_recovery_checked', 'mutation_reconciled', 'mutation_recovery_checked'}
            and e['detail'].get('request_event') == detail['request_event']), None)
        if prior and prior['detail'] == detail:
            self.set_status(observation['reason'])
            return
        previous = self.last_result
        previous_order = self.order_snapshot()
        if observation.get('operation') in {'work_order_requested', 'work_replacement_requested'} and result:
            from graph_backlog.jsonutil import sha256_canonical
            request = self.demand_state['events'][observation['request_event']]['detail']
            if request.get('draft_sha256') == sha256_canonical(previous_order):
                self.restore_order({})
        selection_before = asdict(selected)
        if (result and observation.get('operation') != 'work_control_requested'
                and selected.mode == observation['mode'] and getattr(selected, observation['owner_field'], None) == observation['owner']):
            self.last_result = result
            operation = observation.get('operation')
            if operation == 'graph_apply_requested' and result.get('work_ids'):
                self.project_graph_path = result.get('graph_path', selected.graph_path)
                self.query_one('#work-id', Input).value = result['work_ids'][0]
                self.query_one('#mode', Select).value = 'work'
            self.reviewed_findings = None
        self.reviewed_plan = self.reviewed_preparation = self.reviewed_revision = self.reviewed_decision = None
        try:
            self.checkpoint(kind, detail)
        except Exception as exc:
            self.last_result = previous
            self.restore_order(previous_order)
            self.restore_selection(selection_before)
            self.set_status('Recovery observation could not be saved: '+str(exc))
            return
        if result:
            if 'operation' in observation:
                self.say(json.dumps(result, ensure_ascii=False, indent=2), literal=True)
            elif observation['mode'] in {'parallel', 'research', 'sequence', 'synthesis'}:
                from importlib import import_module
                module = {'parallel': 'parallel_explore', 'research': 'research_flow', 'sequence': 'work_sequence',
                          'synthesis': 'research_synthesis'}[observation['mode']]
                self.say(import_module('relay.'+module).render(result), literal=True)
            elif observation['mode'] == 'plan':
                self.show_plan(result)
            elif observation['mode'] == 'work':
                from .work_console import render
                self.say(render('run', result), literal=True)
            else:
                from .native_display import render_result
                self.say(render_result(result), literal=True)
        self.set_status(observation['reason'])

    def continue_demand(self, *, automatic=False):
        # Reset only at the explicit entry, before the worker is scheduled.
        if not automatic and not self.busy:
            self.cancel_requested = False
        return self._continue_demand(getattr(self, 'cancel_generation', 0))

    @work(exclusive=True, group='demand-conduction')
    async def _continue_demand(self, cancellation_generation):
        def stopped():
            return (self.cancel_requested or self.close_after_run
                    or cancellation_generation != getattr(self, 'cancel_generation', 0))

        if self.busy or stopped():
            return
        from .demand_conduction import next_step
        try:
            if not self.demand_state:
                self.set_status('Start or reopen a demand first.'); return
            if Path(self.selection().root).resolve() != Path(self.demand_state['root']).resolve():
                raise ValueError('Reopen a demand for the selected project.')
            self.checkpoint('draft_saved')
            await self.reconcile_demand().wait()
            if stopped():
                return
            selected = self.selection()
            identity, revision = self.demand_id, self.demand_revision
            step = await asyncio.to_thread(next_step, self.demand_state)
            if (self.busy or stopped() or selected != self.selection()
                    or (identity, revision) != (self.demand_id, self.demand_revision)):
                return
            if step['action'] == 'REVIEW_FINDINGS':
                from .flow_assessment import after_findings
                from tare_tools_agent_runtime import CancellationToken
                cancellation = self.entry_cancel = CancellationToken()
                self.controls(True)
                self.set_status('Assessing the next stage from completed findings…')
                try:
                    step = await asyncio.to_thread(after_findings, self.demand_state, step, cancellation)
                finally:
                    self.entry_cancel = None
                    self.controls(False)
                    if self.close_after_run:
                        self.exit()
                if (cancellation.is_cancelled() or stopped() or selected != self.selection()
                        or (identity, revision) != (self.demand_id, self.demand_revision)):
                    return
            action = step['action']
            target = selected
            if action == 'SUGGEST_EXPLORATION':
                # Populate the existing read-only flow; Start remains the execution boundary.
                target = replace(selected, mode='session', role='explorer', session='', verifier='',
                                 prompt=step['prompt'], terminal=False, context_paths=[], attachments=[])
                self.restore_selection(asdict(target))
            elif action in {'REVIEW_FINDINGS', 'RESUME_FLOW'}:
                choice = replace(selected, mode=step['mode'], **{step['owner_field']: step['owner']})
                self.restore_selection(asdict(choice))
                target = self.selection()
                if action == 'REVIEW_FINDINGS':
                    await self.preview_findings().wait()
                else:
                    await self.inspect_selection().wait()
            elif action in {'LINKED_WORKS_DONE', 'REVIEW_DEMAND_RESULT'}:
                self.query_one('#outcome-options').display = True
                # The result review owns its checkpoint and fresh evidence checks.
                await self.review_demand_outcome(navigation=step).wait()
                return
            elif action == 'START_PLAN':
                self.query_one('#prompt', TextArea).focus()
            elif action == 'RESUME_SEQUENCE':
                self.restore_selection(asdict(replace(selected, mode='sequence', sequence=step['sequence'])))
                target = self.selection()
                await self.inspect_selection().wait()
            elif action == 'REVIEW_PLAN':
                self.restore_selection(asdict(replace(selected, mode='plan', plan=step['plan'])))
                target = self.selection()
                await self.review_selection().wait()
                target = replace(target, graph_path=self.project_graph_path)
            elif 'work_id' in step and action != 'BLOCKED':
                choice = replace(selected, mode='work', work=step['work_id'])
                if selected.work != choice.work:
                    choice = replace(choice, source_ref='', criterion_ids='', work_verifier='',
                        intake_criteria='', intake_argv='', revision_reason='', revision_paths='',
                        retry_plan=False, retry_audit=False)
                self.restore_selection(asdict(choice))
                target = self.selection()
                if action == 'PREPARE_WORK':
                    if not choice.intake_criteria:
                        await self.draft_intake().wait()
                        # Drafting intentionally fills these two fields; other input changes remain stale.
                        current = self.selection()
                        target = replace(target, intake_criteria=current.intake_criteria, intake_argv=current.intake_argv)
                elif action == 'HUMAN_DECISION':
                    await self.open_decision().wait()
                else:
                    await self.inspect_selection().wait()
            if (self.busy or stopped() or target != self.selection()
                    or (identity, revision) != (self.demand_id, self.demand_revision)):
                return
            # Navigation carries no execution approval; the existing controls own confirmation.
            self.checkpoint('next_step_opened', step)
            self.say(json.dumps(step, ensure_ascii=False, indent=2))
            self.set_status(step['reason'])
        except Exception as exc:
            self.set_status('Cannot continue demand: '+str(exc))

    @work(exclusive=True, group='demand-conduction')
    async def review_demand_sequence(self):
        if self.busy:
            return
        self.reviewed_demand_sequence = None
        try:
            from .demand_conduction import preview_sequence
            if not self.demand_state:
                raise ValueError('Start or reopen a demand first.')
            if Path(self.selection().root).resolve() != Path(self.demand_state['root']).resolve():
                raise ValueError('Reopen a demand for the selected project.')
            self.checkpoint('draft_saved')
            await self.reconcile_demand().wait()
            selected = self.selection()
            identity, revision = self.demand_id, self.demand_revision
            view = await asyncio.to_thread(preview_sequence, self.demand_state)
            if (self.busy or selected != self.selection()
                    or (identity, revision) != (self.demand_id, self.demand_revision)):
                return
            self.checkpoint('demand_sequence_reviewed', view)
            self.reviewed_demand_sequence = (selected, self.demand_id, self.demand_revision, view)
            self.say('Prepared Works, in execution order:\n'+
                     '\n'.join(str(i)+'. '+r['work_id'] for i,r in enumerate(view['items'],1)))
            self.set_status('Run reviewed Works starts this set with the selected model/settings. '
                            'Each Work keeps its checks and settlement; a failure or human decision stops the sequence.')
        except Exception as exc:
            self.set_status('Cannot review linked Works: '+str(exc))

    def run_demand_sequence(self):
        if self.busy:
            return
        reviewed = getattr(self, 'reviewed_demand_sequence', None)
        try:
            if not reviewed:
                raise ValueError('Review linked Works first.')
            selected, identity, revision, view = reviewed
            if selected != self.selection() or (identity, revision) != (self.demand_id, self.demand_revision):
                raise ValueError('Demand or settings changed. Review linked Works again.')
            target = replace(selected, mode='sequence', sequence='',
                             sequence_works=', '.join(view['requested_order']))
            self.restore_selection(asdict(target))
            self.reviewed_demand_sequence = None
            self.execute(False, expected_sequence=view['sha256'])
        except Exception as exc:
            self.set_status('Cannot run linked Works: '+str(exc))

    @work(exclusive=True, group='sequence-revision-preview')
    async def review_sequence_revision(self):
        if self.busy:
            return
        self.reviewed_sequence_revision = None
        try:
            from .work_sequence import preview_revision
            selected = self.selection()
            if selected.mode != 'sequence' or not selected.sequence:
                raise ValueError('Open a saved sequence first.')
            self.checkpoint('draft_saved')
            await self.reconcile_demand().wait()
            from .console_recovery import pending
            if pending(self.demand_state):
                raise ValueError('Reconcile the unconfirmed operation before revising.')
            identity, revision = self.demand_id, self.demand_revision
            ids = ([key.strip() for key in selected.sequence_revision_works.split(',')]
                   if selected.sequence_revision_works.strip() else None)
            view = await asyncio.to_thread(preview_revision, selected.sequence, selected.sequence_revision_reason,
                                          work_ids=ids)
            if (self.busy or selected != self.selection()
                    or (identity, revision) != (self.demand_id, self.demand_revision)):
                return
            if Path(view['root']).resolve() != Path(selected.root).resolve():
                raise ValueError('Sequence belongs to another project.')
            self.reviewed_sequence_revision = (selected, identity, revision, view)
            self.say('Retained results: '+', '.join(view['retained'])+'\nChanges to remaining Works:\n'+
                     json.dumps(view['changes'], ensure_ascii=False, indent=2), literal=True)
            if view.get('rerun'):
                self.say('Run again after Work revision:\n'+json.dumps(view['rerun'], ensure_ascii=False, indent=2)+
                         '\nPrevious attempts remain in the revision history.', literal=True)
            self.say('Added Works:\n'+json.dumps(view['added'], ensure_ascii=False, indent=2)+
                     '\nRemoved from this sequence:\n'+json.dumps(view['removed'], ensure_ascii=False, indent=2)+
                     '\nExecution order: '+', '.join(r['work_id'] for r in view['items']), literal=True)
            self.set_status('Review the updated criteria, scope, verification and prerequisites. Confirm saves the revision; Resume runs it.')
        except Exception as exc:
            self.set_status('Sequence revision unavailable: '+str(exc))
        finally:
            self.update_mode()

    def confirm_sequence_revision(self):
        if self.busy or not self.reviewed_sequence_revision:
            return
        selected, identity, revision, view = self.reviewed_sequence_revision
        if selected != self.selection() or (identity, revision) != (self.demand_id, self.demand_revision):
            self.reviewed_sequence_revision = None
            self.update_mode()
            self.set_status('Selection or demand changed; review the sequence changes again.')
            return
        self.controls(True)
        self.query_one('#cancel').disabled = True
        self.save_sequence_revision(view)

    @work(group='execution')
    async def save_sequence_revision(self, view):
        try:
            from .work_sequence import revise, render, RevisionRefused
            text = self.selection().sequence_revision_works
            ids = [key.strip() for key in text.split(',')] if text.strip() else None
            def confirm():
                try:
                    return revise(view['sequence'], view['sha256'], view['reason'], work_ids=ids)
                except RevisionRefused as exc:
                    return {'status': 'REFUSED', 'error': str(exc)}
            result = await self.confirmed_mutation('sequence_revision_requested',
                {'sequence': view['sequence'], 'sha256': view['sha256'], 'reason': view['reason'],
                 'requested_order': view['requested_order']},
                confirm)
            self.last_result = result
            self.say(render(result), literal=True)
            self.checkpoint('sequence_revised', result, required=False)
            self.set_status('Sequence revision saved. Prior results retained; use Resume to continue.'
                            if result['status'] == 'READY' else 'Revision refused: '+result['error']+'. Review again.')
        except Exception as exc:
            self.set_status('Sequence revision not confirmed. Inspect its saved state: '+str(exc))
        finally:
            self.reviewed_sequence_revision = None
            self.controls(False)
            if self.close_after_run:
                self.exit()

    def open_demand_stage(self):
        if self.busy:
            return
        key = self.query_one('#demand-stage', Select).value
        if key not in self.demand_stages:
            return
        try:
            current = self.selection()
            stage = saved_selection(self.demand_stages[key])
            self.checkpoint('draft_saved')
            shared = ('prompt', 'outcome_summary', 'outcome_remaining', 'outcome_disposition', 'outcome_resolutions', 'guide_next',
                      'deployment', 'profile', 'unlimited', 'auditor', 'auto_execution', 'auto_planner',
                      'plan_reviewer', 'plan_reviewer_profile')
            self.restore_selection(asdict(replace(stage, **{key: getattr(current, key) for key in shared})))
            self.entry_proposal = None
            self.checkpoint('stage_opened')
            self.set_status('Linked stage opened. Review its current receipts before resuming.')
        except Exception as exc:
            self.set_status('Stage unavailable: '+str(exc))

    def has_draft(self):
        selected = self.selection()
        return bool(selected.prompt.strip() or self.context_paths or self.attachments or self.assistant_history
                    or self.query_one('#message', TextArea).text.strip()
                    or (selected.mode == 'synthesis' and selected.synthesis_inputs.strip() not in {'', '[]'}))

    def save_demand(self):
        if not self.busy:
            try:
                self.prepare_workspace()
                self.checkpoint('draft_saved')
                return True
            except Exception as exc:
                self.set_status(str(exc))
        return False

    def follow_demand(self):
        """Advance the interface after success; execution approvals remain with their controls."""
        if (not self.busy and not self.cancel_requested and not self.close_after_run and self.demand_state
                and self.query_one('#guide-next', Checkbox).value):
            self.continue_demand(automatic=True)

    @work(exclusive=True, group='demand-outcome')
    async def review_demand_outcome(self, navigation=None):
        if self.busy:
            return
        from . import demand_outcome
        self.reviewed_outcome = None
        self.query_one('#outcome-options').display = True
        try:
            if not self.demand_state:
                raise ValueError('Start or reopen a demand first.')
            self.checkpoint('draft_saved')
            await self.reconcile_demand().wait()
            if navigation is not None:
                self.checkpoint('next_step_opened', navigation)
            selected = self.selection()
            identity, revision = self.demand_id, self.demand_revision
            resolutions = demand_outcome.parse_resolutions(selected.outcome_resolutions)
            evidence = await asyncio.to_thread(demand_outcome.inspect, self.demand_state, resolutions=resolutions)
            if (self.busy or selected != self.selection()
                    or (identity, revision) != (self.demand_id, self.demand_revision)):
                return
            self.say(demand_outcome.render(evidence))
            if not selected.outcome_summary.strip():
                self.set_status('Describe the delivery and remaining obligations, then review the result.')
                self.query_one('#outcome-summary', TextArea).focus()
                return
            view = await asyncio.to_thread(demand_outcome.preview, self.demand_state,
                selected.outcome_summary, selected.outcome_remaining, disposition=selected.outcome_disposition,
                resolutions=resolutions)
            if (self.busy or selected != self.selection()
                    or (identity, revision) != (self.demand_id, self.demand_revision)):
                return
            self.reviewed_outcome = (selected, identity, revision, view)
            self.say(demand_outcome.render(view))
            self.set_status('Confirm records your assessment against these receipts. Partial results retain open obligations.')
        except Exception as exc:
            self.set_status('Cannot review demand result: '+str(exc))
        finally:
            self.update_mode()

    @work(exclusive=True, group='demand-outcome-confirm')
    async def confirm_demand_outcome(self):
        if self.busy or not self.reviewed_outcome:
            return
        from . import demand_outcome
        selected, identity, revision, view = self.reviewed_outcome
        if selected != self.selection() or (identity, revision) != (self.demand_id, self.demand_revision):
            self.reviewed_outcome = None
            self.update_mode()
            self.set_status('Demand or result changed. Review again.')
            return
        self.controls(True)
        self.query_one('#cancel').disabled = True
        try:
            value = await asyncio.to_thread(demand_outcome.confirm, identity, selected.root, relay_mesh.RELAY_ROOT, view)
            self.demand_state, self.demand_revision = value, value['revision']
            self.query_one('#demand-status').update(console_demand.guidance(value))
            self.say(demand_outcome.render(view))
            self.set_status('Demand result saved: '+view['disposition']+'. Reopening preserves this reviewed assessment.')
        except Exception as exc:
            self.set_status('Result not confirmed: '+str(exc)+'. Reopen to inspect any saved result.')
        finally:
            self.reviewed_outcome = None
            self.controls(False)

    def new_demand(self):
        if self.busy or self.sending_message:
            return
        if self.demand_id and Path(self.demand_state['root']) == Path(self.selection().root).resolve():
            try:
                self.checkpoint('draft_saved')
            except Exception as exc:
                self.set_status('Current demand could not be saved: '+str(exc)); return
        self.navigation_generation += 1
        self.invalidate_message_observations()
        old = self.selection()
        mode = 'assistant' if old.mode == 'assistant' or self.initial_mode == 'assistant' else 'conversation'
        self.restore_selection(asdict(Selection(old.root, mode=mode, deployment=old.deployment,
            profile=old.profile, unlimited=old.unlimited, guide_next=old.guide_next,
            max_model_calls=old.max_model_calls, max_seconds=old.max_seconds, local_models=old.local_models,
            execution_policy={'coordinator': self.default_coordinator})))
        self.assistant_session = None
        self.assistant_history = []
        self.folder_request = None  # it belonged to the conversation just left
        self.reviewed_demand_sequence = None
        self.entry_root = old.root
        self.last_sent_message = None
        with self.prevent(Select.Changed):
            self.query_one('#work', Select).clear()
        self.demand_id = None; self.demand_revision = 0; self.demand_state = None; self.demand_stages = {}
        self.entry_history = self.entry_questions = (); self.entry_proposal = None; self.last_result = None
        self.reset_presence()
        self.query_one('#output').clear()
        self.restore_order({})
        self.query_one('#message', TextArea).clear()
        self.query_one('#demand-stage', Select).set_options([])
        self.query_one('#demand-status').update('New demand · saved on Send, Start or Save draft.')
        self.load_project_permissions(old.root, force=True)
        self.load_project_graph(old.root, force=True)
        self.update_mode()
        if mode == 'assistant':
            self.set_status('Ready')
