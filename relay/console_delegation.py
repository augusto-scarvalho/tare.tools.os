"""Operator controls for read-only planning and exploration inside native sessions."""
import asyncio
import json
from pathlib import Path

from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Input, Select, Static, Switch, TextArea

from .console_panels import SupportPanel


EXPERIMENT_DECIDERS = [('off', 'Off · tare refuses more candidates than fit'),
                       ('shadow', 'Shadow · every decider logs, nothing changes'),
                       ('rule', 'Rule · the question words rank them'),
                       ('jev', 'Jev online (TypeSafe) ranks search results; use the rule if unavailable')]
EXPERIMENT_SWITCHES = [('ordered_candidates', 'Explorers see their candidates best first, the likely ones marked'),
                       ('candidate_windows', 'Each candidate comes with the lines around it; the code map is short'),
                       ('jev_first', 'With Jev and a small candidate set, no explorer: the session judges the lines'),
                       ('adaptive_k', 'With Jev, as many candidates as it finds likely, 10 to 40 per explorer'),
                       ('instruction_flags', 'Comments and documents that instruct agents are marked as data'),
                       ('contradiction_flags', 'Candidates that contradict a premise of the question are marked'),
                       ('markdown_candidates', 'Search candidates include Markdown documents'),
                       ('location_status', 'Explorers mark each location supported or hypothesis, and conclude'),
                       ('previous_locations', "Candidates may come from this session's earlier locations"),
                       ('specgraph_candidates', "Candidates may be a symbol's references in a published SpecGraph index"),
                       ('plan_tool', 'Sessions may write a plan of units into their ledger, checked without a model (plan tool)'),
                       ('implement_child', 'Sessions may hand a unit to a child implementer; tare verifies and applies (implement)'),
                       ('implement_parallel', 'Units handed at once run in parallel, 2 at a time, applied one after another'),
                       ('leaf_project_instructions', "An explorer child reads the repository's root AGENTS.md")]


class DelegationPanel(SupportPanel):
    BINDINGS = [Binding('escape', 'close', 'Back', priority=True)]
    DEFAULT_CSS = '''
    DelegationPanel #delegation-scroll { height: 1fr; }
    DelegationPanel .delegation-label { height: auto; margin-top: 1; }
    DelegationPanel Switch { height: 1; border: none; }
    DelegationPanel TextArea { height: 5; }
    DelegationPanel Select { width: 40; }
    DelegationPanel #delegation-status { height: auto; max-height: 4; }
    '''

    def compose(self):
        from .delegation import class_values, experiments, policy
        from .work_forms import availability, availability_text
        self.value, self.trials = policy(), experiments()
        self.planner_profile = None
        coordinated = True
        try:
            from .native_agent import read_json
            source = getattr(self.app, 'assistant_session', None)
            coordinated = not source or read_json(Path(source)/'native-agent.json', 1024*1024).get('conversation_flow')
            forms = ('New chats coordinate separate planning, implementation, checks and independent review. '
                     'Questions and investigations stay read-only. A plan-only request stops at the plan. '
                     'A valid plan and its findings are reused when you ask to continue.' if coordinated else
                     availability_text(availability(self.app.workspace_root)))
        except Exception as exc:  # a report only: the settings below stay usable
            forms = 'Ways of working: unavailable (' + type(exc).__name__ + ')'
        with Vertical(classes='support-body'):
            with Horizontal(classes='panel-header'):
                yield Static('Planning and exploration', classes='panel-title')
                yield Button('Back · Esc', id='delegation-close')
            with VerticalScroll(id='delegation-scroll'):
                yield Static(forms, classes='delegation-label', markup=False)
                yield Static('Separate planner', classes='delegation-label')
                yield Static('Choose the planner model below. Planning for changes runs in a separate session. All calls share '
                             'the conversation budget and appear in /usage; saved conversations keep their settings.',
                             classes='delegation-label')
                optional_planner = Switch(self.trials['planner_child'], id='delegation-planner_child')
                optional_planner.display = not coordinated
                yield optional_planner
                yield Static(planner_label(), id='delegation-planner-model', markup=False)
                yield Button('Choose planner model…', id='delegation-planner-model-choose')
                yield Static('Plan review: checking available reviewers…', id='delegation-plan-reviewer', markup=False)
                yield Button('Review settings…', id='delegation-review-settings')
                yield Static('Allow delegated planners and implementers to use other providers or allowances '
                             '(may spend a separate API balance or subscription quota)', classes='delegation-label')
                yield Switch(self.trials['child_routes_any'], id='delegation-child_routes_any')
                yield Static('Assisted exploration', classes='delegation-label')
                yield Static('Allow new native conversations and Work reviews to delegate read-only investigation.', classes='delegation-label')
                yield Switch(self.value['enabled'], id='delegation-enabled')
                yield Static('Explorer profile · name or variant JSON', classes='delegation-label')
                yield TextArea(self.value['profile'] if isinstance(self.value['profile'], str) else json.dumps(self.value['profile'], indent=2), id='delegation-profile')
                yield Static('Hosted explorer beyond the local slot, or when the local GPU is unavailable · name, variant JSON or null', classes='delegation-label')
                yield TextArea(self.value['overflow'] if isinstance(self.value['overflow'], str) else json.dumps(self.value['overflow'], indent=2), id='delegation-overflow')
                for key, label in [('calls', 'Maximum model calls per investigation'),
                                   ('requests', 'Maximum investigations per parent session'),
                                   ('seconds', 'Maximum seconds per investigation'),
                                   ('max_usd', 'Total API allowance USD for exploration in each parent session')]:
                    yield Static(label, classes='delegation-label')
                    yield Input(str(self.value[key]), id='delegation-'+key)
                yield Static('Experimental explorer · optional variant JSON or null', classes='delegation-label')
                yield TextArea(json.dumps(self.value['challenger'], indent=2), id='delegation-challenger')
                yield Static('Parent sessions assigned to the experimental explorer (%)', classes='delegation-label')
                yield Input(str(self.value['sample_percent']), id='delegation-sample_percent')
                yield Static('Experiments · each is off by default, logged, and summarized under Measurements', classes='delegation-label')
                yield Static('Who chooses which candidates explorers judge when they do not all fit', classes='delegation-label')
                yield Select([(label, key) for key, label in EXPERIMENT_DECIDERS], value=self.trials['decider'],
                             allow_blank=False, id='delegation-decider')
                for key, label in EXPERIMENT_SWITCHES:
                    yield Static(label, classes='delegation-label')
                    yield Switch(self.trials[key], id='delegation-'+key)
                yield Static('By class of session route, ahead of the switches above: ' + ('; '.join(
                    name + ' ' + ', '.join(f"{k} {'on' if v is True else 'off' if v is False else v}" for k, v in values.items())
                    for name, values in class_values().items()) or 'none') + '. Change one with python -m relay.delegation '
                    '--experiment local.jev_first=on (classes: local, quota, usd).', classes='delegation-label', markup=False)
                yield Static('These are ceilings. The session chooses how many investigations to run and the breadth of '
                    'each: quick 3 calls and 60 s, medium 6 and 180 s, thorough 10 and 300 s, cut to these limits and '
                    'to its own remaining calls. '
                    'Investigations requested together run in parallel. A local explorer takes one at a time; the '
                    'hosted explorer takes the rest at once. Explorers always use their own model, effort and parameters, '
                    'never those of the session. Calls and time also consume the parent limits. '
                    'Subscription calls use quota; their USD cost remains unknown. The experimental profile replaces '
                    'the regular explorer for the sampled session. No duplicate race or automatic promotion. '
                    'Use /usage to inspect the parent and its exploration sessions.', classes='delegation-label')
            yield Static('Save applies to new sessions in this installation. Existing sessions retain their settings.', id='delegation-status', markup=False)
            with Horizontal(classes='panel-actions'):
                yield Button('Save', id='delegation-save', classes='panel-primary')
                yield Button('Measurements', id='delegation-report')

    async def on_button_pressed(self, event):
        event.stop()
        if event.button.id == 'delegation-close':
            self.action_close()
        elif event.button.id == 'delegation-review-settings':
            from .console_routes import RoutesPanel
            self.app.push_screen(RoutesPanel(), lambda _: self.refresh_plan_reviewer())
        elif event.button.id == 'delegation-planner-model-choose':
            from . import model_choices
            from .model_picker import ModelPicker
            try:
                from .planner_policy import choices as planner_choices
                from .execution_scope import for_role
                with for_role('planner'):choices = planner_choices(model_choices.inventory(role='planner'))
                def chosen(value):
                    if value is None or not self.is_mounted:
                        return
                    try:
                        path = model_choices.materialize(choices, value, self.app.model_preferences_root())
                        item, route = model_choices.resolve(choices, value)
                        self.planner_profile = path
                        self.query_one('#delegation-planner-model', Static).update(
                            'Planner: '+item['label']+' · '+route['label']+' (Save to apply)')
                        self.refresh_plan_reviewer()
                    except (ValueError, OSError, KeyError, TypeError) as exc:
                        self.query_one('#delegation-status', Static).update(str(exc))
                self.app.push_screen(ModelPicker(choices, current=current_planner_choice(choices), note='Choose the delegated planner; then Save on the previous screen.\n'
                    'Minimum reasoning: high. Applies to new sessions; the coordinator is a separate role.'), chosen)
            except (ValueError, OSError, KeyError, TypeError) as exc:
                self.query_one('#delegation-status', Static).update(str(exc))
        elif event.button.id == 'delegation-report':
            from .delegation import digest, digest_text, report
            from .console_workbench import WorkbenchPanel
            measured = await asyncio.to_thread(report)
            summary = await asyncio.to_thread(lambda: digest_text(digest()))
            if self.is_mounted:
                rows = [{'id': 'digest', 'label': 'Summary · last 7 days', 'body': summary}] + [
                    {'id': r['session'], 'label': r['profile']+' · '+r.get('parent_status', 'UNKNOWN'),
                     'body': json.dumps(r, indent=2)} for r in measured['sessions']]
                self.app.push_screen(WorkbenchPanel('Exploration measurements', rows or [
                    {'id': 'empty', 'label': 'No recorded investigations', 'body': measured['measurement']}], kind='usage'))
        elif event.button.id == 'delegation-save':
            try:
                value = dict(self.value, enabled=self.query_one('#delegation-enabled', Switch).value)
                for key in ('profile', 'overflow', 'challenger'):
                    text = self.query_one('#delegation-'+key, TextArea).text.strip()
                    value[key] = json.loads(text) if text.startswith(('{', '"')) or text == 'null' else text or None
                for key in ('calls', 'requests', 'seconds', 'sample_percent'):
                    value[key] = int(self.query_one('#delegation-'+key, Input).value)
                value['max_usd'] = self.query_one('#delegation-max_usd', Input).value
                trials = {'decider': self.query_one('#delegation-decider', Select).value,
                          'planner_child': self.query_one('#delegation-planner_child', Switch).value,
                          'child_routes_any': self.query_one('#delegation-child_routes_any', Switch).value,
                          **{key: self.query_one('#delegation-'+key, Switch).value for key, _ in EXPERIMENT_SWITCHES}}
                from .delegation import configure, save_experiments, validate
                validate(value)  # Bad exploration fields must not partly save a new planner.
                if self.planner_profile:
                    from .execution_scope import current
                    if current().pin('planner'):
                        raise ValueError('Temporary --planner pins cannot be saved as preferences; consult tare models --role planner.')
                    from .planning_tools import configure_model
                    await asyncio.to_thread(configure_model, self.planner_profile)
                await asyncio.to_thread(configure, value)
                self.trials = await asyncio.to_thread(lambda: save_experiments(**trials))
                self.value = value
                self.planner_profile = None
                self.query_one('#delegation-planner-model', Static).update(planner_label())
                self.refresh_plan_reviewer()
                message = ('Saved for new sessions. Planning: '+('on' if trials['planner_child'] else 'off')+
                           '; exploration: '+('on' if value['enabled'] else 'off')+'.')
            except (ValueError, OSError) as exc:
                message = str(exc)
            if self.is_mounted:
                self.query_one('#delegation-status', Static).update(message)

    def action_close(self):
        self.dismiss(None)

    def on_mount(self):
        self.refresh_plan_reviewer()

    def refresh_plan_reviewer(self):
        async def update():
            text = await asyncio.to_thread(plan_reviewer_label, self.planner_profile)
            if self.is_mounted:
                self.query_one('#delegation-plan-reviewer', Static).update(text)
        self.run_worker(update(), group='plan-reviewer-preview', exclusive=True)


def plan_reviewer_label(profile=None):
    """A local settings preview. Actual dispatch checks resources again."""
    from .native_generation import load_deployment
    from .native_routes import reviewers
    from .model_choices import describe
    from .turn_engine import available, policy
    from .work_audit import identity
    try:
        from .planning_tools import configured
        if profile is None and not configured():
            return 'Plan review: selected from the effective planner when needed. Implementation candidates require an independent auditor.'
        from .execution_scope import refusal
        planner = load_deployment(profile) if profile else next(
            (value for name in policy()['planner'] if (value := available(name, probe=False)[0])
             and not refusal('planner', value, name)), None)
        if planner is None:
            return 'Plan review: choose an available planner to see its reviewer.'
        candidates, _ = reviewers(planner, 'plan-auditor', check_resources=False)
        if not candidates:
            return ('Plan review: separate planner session · independence not established. '
                    'Connect another provider in /setup, or require an independent reviewer for a formal plan.')
        value = candidates[0]
        card = describe(value['id'], value)
        from .landing_policy import SAME_VENDOR_REVIEW, independence
        cross = independence(identity(planner, 'implementer'), identity(value, 'auditor')) == 'INDEPENDENT'
        return ('Plan review: '+card['label']+' · '+card['route']['label']+
                (' · another provider' if cross else ' · '+SAME_VENDOR_REVIEW)+
                '\nFor formal plans; checked again before dispatch. Delegated planning advice stays with the coordinator.')
    except (ValueError, OSError, KeyError, TypeError):
        return 'Plan review: unavailable. Open Review settings to check the configured routes.'


def planner_label():
    from .model_choices import describe
    from .native_catalog import select
    from .turn_engine import policy
    from .planning_tools import configured
    if not configured():
        return 'Planner: first available configured planner · minimum high reasoning.'
    labels = []
    for name in policy()['planner']:
        try:
            from .planner_policy import effective
            value = describe(name, effective(select(name)))
            effort = value['route']['default_effort']
            labels.append(value['label']+' · '+value['route']['label']+(' · '+effort if effort else ''))
        except (ValueError, OSError, KeyError, TypeError):
            labels.append(name+' (unavailable)')
    return 'Planner: '+', '.join(labels)


def current_planner_choice(choices):
    from .model_choices import resolve
    from .native_catalog import select
    from .turn_engine import policy
    try:
        saved = select(policy()['planner'][0]).get('native_model_choice') or {}
        chosen = {k: saved[k] for k in ('key', 'connection', 'reasoning')}
        resolve(choices, chosen)
        return chosen
    except (ValueError, OSError, KeyError, TypeError, IndexError):
        return None
