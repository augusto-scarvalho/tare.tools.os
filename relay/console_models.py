"""Model defaults and explicitly reviewed provider continuation for saved conversations."""
from pathlib import Path

from textual.widgets import Button, Checkbox, Input, Select

from . import model_choices


class ConversationModels:
    def model_preferences_root(self):
        root = self.appearance_root()
        if root is None:
            from .native_installation import storage_root
            root = storage_root()/'console'
        if Path(root).resolve().is_relative_to(Path(self.workspace_root).resolve()):
            raise ValueError('Model preferences must be stored outside the workspace.')
        return root

    def load_model_preferences(self):
        if self.model_preferences is not None:
            return
        self.model_preferences = {'recent': [], 'models': {}, 'last': None}
        if self.appearance_root() is None:
            return  # Embedded/test consoles do not inherit user preferences.
        try:
            root = self.model_preferences_root()
            value = model_choices.read_preferences(root)
            chosen = value.get('last')
            if chosen:
                profile = Path(chosen['profile'])
                if not profile.resolve().is_relative_to((root/'model-selections').resolve()):
                    raise ValueError('Saved model preference is outside its storage directory.')
                from .native_generation import load_deployment
                saved = load_deployment(profile)
                from .execution_scope import current
                if not current().allows(saved,'coordinator')[0]:
                    self.model_preferences=value
                    return  # A temporary flag never overwrites the remembered model.
                if any(saved.get('native_model_choice', {}).get(k) != chosen.get(k)
                       for k in ('key', 'connection', 'reasoning')):
                    raise ValueError('Saved model preference does not match its profile.')
                try:
                    profile = Path(model_choices.current(root, chosen))
                except (OSError, ValueError, KeyError, TypeError):
                    pass  # the route went away or changed shape: the saved profile stays
                self.query_one('#profile', Input).value = str(profile)
            self.model_preferences = value
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.notify('Could not restore model preferences: '+str(exc), severity='warning')

    def pending_model_choice(self, choices):
        from .native_generation import load_deployment
        profile = self.query_one('#profile', Input).value.strip()
        if profile:
            try:
                value = load_deployment(profile).get('native_model_choice')
                if value:
                    model_choices.resolve(choices, value)
                    return {k: value[k] for k in ('key', 'connection', 'reasoning')}
            except (OSError, ValueError, KeyError, TypeError):
                return None
        name = str(self.query_one('#model', Select).value)
        for item in choices:
            if any(r['id'] == name for r in item['routes']):
                return {'key': item['key'], 'connection': name, 'reasoning': 'default'}
        return None

    def action_model(self):
        self.open_model_picker()

    def action_reasoning(self):
        self.open_model_picker(reasoning=True)

    def open_model_picker(self, *, reasoning=False):
        from .model_picker import ModelPicker
        if self.busy or self.sending_message:
            self.notify('Wait for the current operation before changing model preferences.')
            return
        if Path(self.selection().root).resolve() != Path(self.workspace_root).resolve():
            self.notify('Apply the workspace change before choosing a model.')
            return
        try:
            choices = model_choices.inventory()
            self.load_model_preferences()
            current = self.pending_model_choice(choices)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.notify('Model choices unavailable: '+str(exc), severity='error')
            return
        workspace = self.workspace_root
        selected = self.selection()
        source = self.assistant_session if selected.mode == 'assistant' else selected.session if selected.mode == 'session' else None
        if source:
            try:
                from .native_agent import read_json
                saved_choice = read_json(Path(source)/'native-agent.json', 1024*1024)['deployment'].get('native_model_choice')
                if saved_choice:
                    model_choices.resolve(choices, saved_choice)
                    current = {k: saved_choice[k] for k in ('key', 'connection', 'reasoning')}
            except (OSError, ValueError, KeyError, TypeError):
                pass  # A staged switch may not have written its execution state yet.

        def applied(chosen):
            if chosen is None:
                return
            if workspace != self.workspace_root or self.busy or self.sending_message:
                self.notify('The workspace or execution changed. Choose the model again.')
                return
            continue_task = chosen.pop('continue_task', False)
            try:
                root = self.model_preferences_root()
                path = model_choices.materialize(choices, chosen, root)
                from .execution_scope import current
                temporary=current().active or bool(self.conversation_coordinator)
                preferences = model_choices.remember(root, chosen, path) if not continue_task and not temporary else self.model_preferences
                item, route = model_choices.resolve(choices, chosen)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                self.notify('Model choice was not applied: '+str(exc), severity='error')
                return
            if continue_task:
                self.review_provider_continuation(source, path, item['label']+' · '+route['label'])
                return
            self.model_preferences = preferences
            if not temporary:
                self.query_one('#auto-execution', Checkbox).value = False
                self.query_one('#auto-planner', Checkbox).value = False
            # Existing session owners ignore these future-execution preferences.
            self.query_one('#profile', Input).value = path
            self.sync_header(force=True)
            effort = route['default_effort'] if chosen['reasoning'] == 'default' else chosen['reasoning']
            self.set_status('Next conversation: '+item['label']+' · '+(effort or 'Profile default')+
                            ' · '+route['label']+'. Saved conversations keep their settings.')
            self.query_one('#prompt').focus()

        self.push_screen(ModelPicker(choices, current=current, preferences=self.model_preferences,
                                     reasoning_focus=reasoning, can_continue=bool(source)), applied)

    def judge_answer(self, source, request, reply):
        """tare auto: a completed turn that changed no files and whose short reply only announced the work
        continues on the next tier (turn_engine.unanswered), judged off the UI thread like the audit."""
        local = self.selection().local_models  # read here: the worker runs off the UI thread
        self.run_worker(lambda: self._judge_answer(source, request, reply, local),
                        thread=True, group='answer-judge', exclusive=True)

    def _judge_answer(self, source, request, reply, local=True):
        from .native_agent import read_json
        from .turn_engine import local_models, next_auto, unanswered
        try:
            state = read_json(Path(source)/'native-agent.json', 1024*1024)
            if state.get('conversation_flow'):
                return  # a question/plan/pending stage is not a failed implementation to escalate
            saved = state['deployment']
            with local_models(local):  # the conversation's choice: no local route or local judge when it said so
                if not saved.get('native_auto_selection') or next_auto(saved) is None:
                    return  # not tare auto, or no stronger tier to go to: the judge is not asked
                verdict = unanswered(request, reply, {'source': 'tui', 'route': saved['native_auto_selection']['route']})
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.call_from_thread(self.notify, 'tare auto: ' + str(exc)[:200], severity='error')
            return
        if verdict:
            why = (f"only announced the work (judge {verdict['model']}, "
                   f"{verdict['probabilities']['announced']:.2f})")
            self.call_from_thread(self.escalate_unanswered, source, why)

    def escalate_unanswered(self, source, why):
        current = self.selection()
        active = self.assistant_session if current.mode == 'assistant' else current.session
        if not active or Path(active).resolve() != Path(source).resolve():  # the operator moved on while it ran
            self.notify('tare auto: a reply in another conversation ' + why + '; reopen it and ask again.', timeout=20)
            return
        self.auto_escalate(source, None, why=why)

    def auto_escalate(self, source, status=None, why=None):
        """tare auto: a blocked or exhausted turn, or a completed one whose reply only announced the work (`why`),
        continues on the next policy tier, as a provider change the operator chose in advance by picking tare auto
        (no review panel)."""
        from .native_agent import read_json
        from .native_continuity import preview
        from .turn_engine import local_models, next_auto
        try:
            saved = read_json(Path(source)/'native-agent.json', 1024*1024)['deployment']
            if not saved.get('native_auto_selection') or self.busy:
                return
            from .turn_engine import successor
            if successor(status, per_message_limits=False) == 'stop':  # the operator's budget is the cap
                self.say(f"tare auto: this conversation spent the budget you set, so it stays on "
                         f"{saved['native_auto_selection']['route']}. Raise the limit, or start a new conversation, "
                         f"to continue on a stronger route.")
                return
            with local_models(self.selection().local_models):
                deployment = next_auto(saved)
            if deployment is None:
                self.say('tare auto: no further route is available.')
                return
            value = preview(source, self.workspace_root, terminal_connection=self.backend.continuity_terminal(source))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.notify('tare auto: ' + str(exc), severity='error')
            return
        current = self.selection()
        if deployment['native_profile']['protocol'] not in {'vendor-cli', 'anthropic-messages', 'gemini-generate-content'}:
            deployment = {**deployment, 'native_streaming': current.streaming}
        route = deployment['native_auto_selection']['route']
        self.say(f"tare auto: {saved['native_auto_selection']['route']} {why or 'stopped'}; continuing on {route}.")
        from .turn_engine import UNANSWERED_CONTINUE
        text = value['text'] + '\n' + (UNANSWERED_CONTINUE if why else 'Continue the task from the brief and history.')
        self.execute(False, assistant=current.mode == 'assistant', continuation=(value, text, deployment))

    def audit_changes(self, source, files, root, request, checks=None):
        """As in tare serve: a turn that changed code (`files`, from turn_changes) gets an independent review of
        what it changed (another vendor first); REVISE findings go back to the conversation as one fix message."""
        self.run_worker(lambda: self._audit_changes(source, files, root, request, checks),
                        thread=True, group='audit', exclusive=True)

    def _audit_changes(self, source, files, root, request, checks=None):
        from .native_agent import read_json
        from .turn_engine import audit_turn
        try:
            saved = read_json(Path(source)/'native-agent.json', 1024*1024)
            if saved.get('conversation_flow'):
                return  # each candidate was reviewed before application; never buy another legacy audit
            if 'project_admission' in saved['config']:
                # Intake consent does not authorize the legacy auditor's models, budget or instruction scope.
                # Independent review remains an explicit session/flow; legacy conversations keep their policy.
                return
            implementer = saved['deployment']
            _, fix = audit_turn(files, root, request, implementer,
                                lambda text: self.call_from_thread(self.audit_say, text.strip(), source), checks=checks)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.call_from_thread(self.notify, 'tare audit: ' + str(exc)[:200], severity='error')
            return
        if fix:
            self.call_from_thread(self.send_audit_fix, fix, source)

    def audited_open(self, source):
        """The audited conversation is the one on screen (the operator may have moved on while it ran)."""
        return (self.selection().mode == 'assistant' and bool(self.assistant_session)
                and Path(self.assistant_session).resolve() == Path(source).resolve())

    def audit_say(self, text, source, label='tare audit'):
        if self.audited_open(source):
            self.say(text)
        else:
            self.notify(label + ' of another conversation: ' + text[:300], timeout=20)

    def heads_up(self, source, ids, request):
        """A finished turn's heads-up notes (relay/heads_up.py), off the UI thread like the audit."""
        local = self.selection().local_models  # read here: the worker runs off the UI thread
        self.run_worker(lambda: self._heads_up(source, ids, request, local),
                        thread=True, group='heads-up', exclusive=True)

    def _heads_up(self, source, ids, request, local=True):
        from . import heads_up
        from .native_agent import read_json
        from .turn_engine import local_models
        if not heads_up.enabled():
            return
        try:
            turn = heads_up.from_journal([(source, i) for i in ids], request)
            route = read_json(Path(source)/'native-agent.json', 1024*1024)['deployment'].get('id')
            with local_models(local):  # the conversation's choice: no local judge or writer when it said so
                heads_up.followed(source, turn)  # before the new notes: this request may carry an earlier one
                mine = heads_up.shown(source)
                new = heads_up.notes(turn, seen=mine['shown'], pace=mine, observed={'source': 'tui', 'route': route, 'session': Path(source).name})
            heads_up.keep(source, new, turn, mine)
        except (OSError, ValueError, KeyError, TypeError, StopIteration) as exc:
            self.call_from_thread(self.notify, 'tare heads-up: ' + str(exc)[:200], severity='error')
            return
        if new:
            self.call_from_thread(self.show_heads_up, new, source)

    def show_heads_up(self, new, source):
        from . import heads_up
        from .console_output import HeadsUpNote, Transcript
        if not self.audited_open(source):
            self.notify('tare heads-up of another conversation: ' + heads_up.shown_text(new).strip()[:300], timeout=20)
            return
        for note in new:  # live only, like the audit's text: a reopened conversation is rebuilt from assistant_history, which has no notes
            self.query_one('#output', Transcript).append(HeadsUpNote(note, source))

    def heads_up_ask(self, note, source):
        """The note becomes a question in the composer, after any draft; the operator edits and sends it."""
        from textual.widgets import TextArea
        from . import heads_up
        prompt = self.query_one('#prompt', TextArea)
        text = heads_up.ask([note])
        if text not in prompt.text:  # a second click adds nothing
            prompt.load_text(prompt.text.rstrip() + '\n\n' + text if prompt.text.strip() else text)
        prompt.move_cursor(prompt.document.end)
        prompt.focus()
        try:  # not a label: only tells followed() which note a message that rewrites it is about
            heads_up.loaded(source, [note])
        except OSError:
            pass  # then only a message that keeps the note's text counts
        self.set_status('The heads-up is in the composer: edit it and press Enter to send it; nothing is sent until then. '
                        'Keep its first line and tare knows the message is about this note.')

    def heads_up_label(self, note, source, value):
        from . import heads_up
        try:
            done = heads_up.label(source, note, value)
        except OSError as exc:
            self.notify('tare heads-up: could not save the label: ' + str(exc)[:200], severity='error')
            return False
        if done:
            self.set_status('Recorded: this heads-up was ' + value.replace('_', ' ') + '. Press the other button to change it.')
        return done

    def send_audit_fix(self, text, source):
        from textual.widgets import TextArea
        prompt = self.query_one('#prompt', TextArea)
        if not self.audited_open(source):
            self.notify('tare audit: findings for another conversation were not sent; reopen it to ask for the fix.',
                        timeout=20)
            return
        if self.busy or prompt.text.strip():
            self.say('tare audit: the findings were not sent (a turn or draft is in progress); ask for the fix when ready.')
            return
        prompt.load_text(text)
        self.execute(False)  # the Send path: one fix round in the audited conversation

    def review_provider_continuation(self, source, profile, label):
        from .native_continuity import preview
        from .native_generation import load_deployment
        from .continuity_panel import ContinuityPanel
        selected = self.selection()
        if selected.context_paths or selected.attachments:
            self.notify('Send or remove the selected context and attachments before changing provider.')
            return
        try:
            value = preview(source, self.workspace_root,
                            terminal_connection=self.backend.continuity_terminal(source))
            deployment = load_deployment(profile)
            if deployment['native_profile']['protocol'] not in {'vendor-cli', 'anthropic-messages', 'gemini-generate-content'}:
                deployment = {**deployment, 'native_streaming': selected.streaming}
            if selected.prompt.strip():
                value['text'] += '\nNew user instruction:\n'+selected.prompt
                value['user_context'] += '\nNew user instruction:\n'+selected.prompt
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.notify(str(exc), severity='error')
            return
        def confirmed(text):
            if text is None:
                return
            current = self.selection()
            active_source = self.assistant_session if current.mode == 'assistant' else current.session
            if self.busy or self.sending_message or current.root != selected.root or active_source != source:
                self.notify('The conversation changed. Open the provider switch again.')
                return
            self.execute(False, assistant=current.mode == 'assistant', continuation=(value, text, deployment))
            self.query_one('#prompt').focus()
        self.push_screen(ContinuityPanel(value['text'], label), confirmed)

    def render_model_controls(self, details):
        if not self.query_one_optional('#choose-model'):
            return
        name = (details.display_model or details.model) if details else 'Model'
        effort = details.reasoning if details else ''
        model = self.query_one('#choose-model', Button)
        if name.startswith('Reading '):  # the header is still reading the saved model: the button keeps its name
            name = str(model.label).removesuffix(' ▾')
        model.label = name+' ▾'
        model.tooltip = 'Choose a model or continue this task with another provider'
        reasoning = self.query_one('#choose-reasoning', Button)
        reasoning.label = ({'xhigh': 'Extra high'}.get(effort, effort.capitalize()) or 'Default')+' ▾'
        reasoning.tooltip = 'Choose reasoning for new conversations'
        model.disabled = reasoning.disabled = self.busy or self.sending_message
        # Button.label repaints but does not re-measure; width: auto must follow the new label.
        model.refresh(layout=True); reasoning.refresh(layout=True)
