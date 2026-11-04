"""Connections first; optional local models, helpers and advanced choices have their own tabs."""
import asyncio
from pathlib import Path

from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Checkbox, Collapsible, Input, Select, Static, TabbedContent, TabPane

from .console_panels import SupportPanel
from .setup_status import CLI_LABELS, PROVIDERS, can_check, credential_target, credential_where, key_status
from .web_search import CREDENTIALS, PROVIDERS as SEARCH_PROVIDERS

STATES = [('Automatic · allow when available', 'later'), ('Allow', 'use'), ('Do not use', 'no')]
CHOICE = {'use': True, 'no': False, 'later': None}


def state(section, item=None):
    value = section.get('use') if item is None else ((section.get('items') or {}).get(item) or {})
    value = value.get('use') if isinstance(value, dict) else value
    return {True: 'use', False: 'no'}.get(value, 'later')


class SetupTabs(TabbedContent):
    def on_tab_pane_focused(self,event:TabPane.Focused):
        focused=self.app.focused
        if focused is None or event.tab_pane not in focused.ancestors:
            # A queued notification from a pane that no longer owns focus must
            # not undo a newer explicit tab selection.
            event.stop();event.prevent_default()


class SetupPanel(SupportPanel):
    BINDINGS = [Binding('escape', 'close', 'Back', priority=True)]
    DEFAULT_CSS = '''
    SetupPanel TabbedContent { height: 1fr; }
    SetupPanel TabPane { padding: 0; }
    SetupPanel VerticalScroll { height: 1fr; }
    SetupPanel .setup-label { height: auto; margin-top: 1; }
    SetupPanel .setup-note { height: auto; color: $text-muted; }
    SetupPanel .setup-actions { height: auto; min-height: 1; margin-bottom: 1; }
    SetupPanel .setup-actions Button { min-width: 10; margin-right: 2; }
    SetupPanel Select { width: 1fr; }
    SetupPanel #setup-status { height: auto; max-height: 3; }
    SetupPanel Collapsible { padding: 0; border: none; }
    '''

    def compose(self):
        self.reply_gate_pending=False
        from .environment import detect, read, replacements
        self.value, self.found = read(), detect()
        value = {} if 'error' in self.value else self.value
        self.credentials = dict(self.found['api_providers'])
        self.credentials['TYPESAFE_API_KEY'] = (self.found.get('decision_models') or {}).get('jev', 'absent')
        self.credentials.update({env: credential_where(self.found, env) for env in CREDENTIALS})
        self.checks, self.pending = set(), None
        with Vertical(classes='support-body'):
            with Horizontal(classes='panel-header'):
                yield Static('Set up Tare', classes='panel-title')
                yield Button('Back · Esc', id='setup-close')
            yield Static('Connect one provider to start. Everything else is optional.', classes='setup-note')
            if 'error' in self.value:
                yield Static('Saved settings could not be read. Saving will replace them.', classes='setup-note')
            if any((value.get(c) or {}).get('use') is False for c in ('clis','api_providers','decision_models','search_providers')):
                yield Static('Some connections are disabled in your settings. Review their tabs and Advanced.', classes='setup-note')
            with SetupTabs(initial='setup-connections', id='setup-tabs'):
                with TabPane('Connections', id='setup-connections'):
                    with VerticalScroll():
                        yield Static('Use a subscription you already have', classes='setup-label')
                        yield Static('CLI login and API keys are separate connections.', classes='setup-note')
                        for endpoint, path in self.found['clis'].items():
                            suffix = endpoint.split('://')[1]
                            yield Static(CLI_LABELS.get(endpoint, endpoint), classes='setup-label', markup=False)
                            yield Static('Installed · checking sign-in…' if path else 'Not installed',
                                         id='setup-cli-status-'+suffix, classes='setup-note', markup=False)
                            with Horizontal(classes='setup-actions'):
                                yield Button('Check sign-in', id='setup-check-cli-'+suffix, disabled=not path)
                                yield Button('Test a reply…', id='setup-test-cli-'+suffix, disabled=not path)
                        yield Static('Or connect with an API key', classes='setup-label')
                        yield Static('APIs charge for usage. Adding a key does not send a model request.', classes='setup-note')
                        for env in self.found['api_providers']:
                            if env != 'TYPESAFE_API_KEY' and env not in CREDENTIALS:
                                yield from self.key_fields(env)
                        yield Checkbox('Allow Meta Contributor to use sent content to improve its products',
                            value=(value.get('model_data_use') or {}).get('meta_contributor') is True,
                            id='setup-meta-contributor')
                        yield Static('Optional. Includes prompts, code and responses sent to Muse Spark Contributor. '
                                     'Other models keep their own data policy.', classes='setup-note', markup=False)
                with TabPane('Local models', id='setup-local'):
                    with VerticalScroll():
                        yield Static('Do you have a model server on this PC or your network?', classes='setup-label')
                        yield Static('Skip this tab if you use subscriptions or API keys. No model is downloaded.', classes='setup-note')
                        yield Select(STATES, value=state(value.get('local_models') or {}), allow_blank=False, id='setup-local_models')
                        yield Static('Model server address', classes='setup-label')
                        gateways = (value.get('local_models') or {}).get('gateways') or []
                        yield Input(gateways[0]['origin'] if gateways else '', placeholder='http://127.0.0.1:8080', id='setup-gateway')
                        yield Static('Saving a new address asks which models the server offers; it does not load one.', classes='setup-note')
                with TabPane('Web search', id='setup-search'):
                    with VerticalScroll():
                        yield Static('Search the web with an API', classes='setup-label')
                        yield Static('For models without built-in search. Add one key; the agent can search directly without calling another model.', classes='setup-note')
                        yield Static('Search APIs charge for requests. Opening this tab or saving a key sends no search.', classes='setup-note')
                        yield Static('May Tare use these search APIs?', classes='setup-label')
                        yield Select(STATES, value=state(value.get('search_providers') or {}), allow_blank=False, id='setup-search_providers')
                        yield Static('Which service should Tare try first?', classes='setup-label')
                        preferred=(value.get('search_providers') or {}).get('preferred','auto')
                        yield Select([('Automatic · first connected service','auto'), *[(p['label'],name) for name,p in SEARCH_PROVIDERS.items()]],
                                     value=preferred if preferred in ('auto',*SEARCH_PROVIDERS) else 'auto', allow_blank=False, id='setup-search-preferred')
                        yield Static('Automatic tries DeepSeek, SerpApi, then Firecrawl. Only connected, allowed services are used. Another service is tried after a definite key or quota refusal; timeouts are not repeated automatically.', classes='setup-note')
                        for env in CREDENTIALS:
                            yield from self.key_fields(env)
                        yield Static('Results include source links. The agent reads relevant pages with the existing URL reader. Keys stay in your secure vault.', classes='setup-note')
                with TabPane('Helpers', id='setup-helpers'):
                    with VerticalScroll():
                        yield Static('Optional helpers for automatic decisions', classes='setup-label')
                        yield Static('These helpers assist with choosing models and checking answers. They do not implement your task.', classes='setup-note')
                        yield Static('Jev online · small classifier hosted by TypeSafe', classes='setup-label')
                        yield Static('Helps select routes and classify commands. Sends decision context to TypeSafe and uses API credit.', classes='setup-note')
                        yield from self.key_fields('TYPESAFE_API_KEY', heading=False)
                        for name, label, detail in [
                            ('jev','May Tare use Jev online?',None),
                            ('local_judge','Local helper · LitJEV with Qwen3.8',
                             'Runs on this PC when the model is already loaded and the GPU is idle. No TypeSafe key or download is needed.')]:
                            yield Static(label, classes='setup-label')
                            if detail:
                                yield Static(detail, classes='setup-note')
                                yield Static('Checking local availability…', id='setup-local-helper-status', classes='setup-note', markup=False)
                                yield Static('May Tare use the local helper?', classes='setup-label')
                            yield Select(STATES, value=state(value.get('decision_models') or {},name), allow_blank=False, id='setup-decision-'+name)
                        yield Static('Command safety has three jobs: classify commands, review uncertain actions, and isolate execution.', classes='setup-label')
                        yield Static('Choose command protections in Project permissions. This tab only allows the optional helpers.', classes='setup-note')
                with TabPane('Advanced', id='setup-advanced'):
                    with VerticalScroll():
                        yield from self.advanced_fields(value)
                        pending = replacements()
                        if pending:
                            with Collapsible(title='Newer models are available', collapsed=True):
                                yield Static('\n'.join(f"{r['newer']} can replace {r['model']} in {r['where']}" for r in pending), classes='setup-note', markup=False)
                                with Horizontal(classes='setup-actions'):
                                    yield Button('Use newer models', id='setup-replace')
                                    yield Button('Keep current models', id='setup-keep')
            yield Static('Keys save immediately to your secure vault. Save settings applies your connection choices.', id='setup-status', markup=False)
            with Horizontal(classes='panel-actions'):
                yield Button('Refresh status', id='setup-refresh')
                yield Button('Save settings', id='setup-save', classes='panel-primary')

    def key_fields(self, env, *, heading=True):
        where = self.credentials[env]
        if heading:
            yield Static(PROVIDERS.get(env,(env,))[0], classes='setup-label', markup=False)
        yield Static(key_status(where), id='setup-key-status-'+env, classes='setup-note', markup=False)
        with Horizontal(classes='setup-actions'):
            yield Button('Save vault key' if where=='env' else 'Change key' if where=='vault' else 'Add API key', id='setup-key-'+env)
            if can_check(env):
                yield Button('Check connection', id='setup-check-api-'+env, disabled=where not in {'vault','env'})
        if where=='env':
            yield Static('A key set outside Tare takes priority over a key saved here.', classes='setup-note')

    def advanced_fields(self, value):
        from .native_catalog import available_models
        yield Static('Connection permissions', classes='setup-label')
        yield Static('Automatic allows detected connections. A disabled group overrides its connections. Saved conversations keep their settings.', classes='setup-note')
        for category,label in [('clis','Allow subscription connections?'),('api_providers','Allow API connections?'),('decision_models','Allow automatic helpers?')]:
            yield Static(label, classes='setup-label')
            yield Select(STATES, value=state(value.get(category) or {}), allow_blank=False, id='setup-'+category)
        with Collapsible(title='Individual connections and older models', collapsed=True):
            for endpoint in self.found['clis']:
                yield Static(CLI_LABELS.get(endpoint,endpoint), classes='setup-label')
                yield Select(STATES, value=state(value.get('clis') or {},endpoint), allow_blank=False, id='setup-cli-'+endpoint.split('://')[1])
            for env in self.found['api_providers']:
                yield Static(PROVIDERS.get(env,(env,))[0], classes='setup-label')
                yield Select(STATES, value=state(value.get('api_providers') or {},env), allow_blank=False, id='setup-api-'+env)
            yield Static('Offer older CLI models as well?', classes='setup-label')
            yield Select([('Only current models','no'),('Include older models','use')], value='use' if (value.get('clis') or {}).get('old_models') else 'no', allow_blank=False, id='setup-old-models')
        yield Static('Optional reply test', classes='setup-label')
        yield Static('Sends one short model request. Check its cost first; only Run reply test spends quota or API credit.', classes='setup-note')
        yield Static('Connection to test', classes='setup-label')
        options=[(CLI_LABELS.get(e,e),'cli:'+e.split('://')[1]) for e in self.found['clis']]
        try:
            options += [(r['model']+' · '+r['deployment'],r['deployment']) for r in available_models()]
        except (ValueError,OSError,KeyError,TypeError):
            yield Static('Some model profiles could not be read. Key setup is still available.', classes='setup-note')
        yield Select(options, prompt='Choose a connection', id='setup-prove')
        with Horizontal(classes='setup-actions'):
            yield Button('Check test cost', id='setup-cost')
            yield Button('Run reply test', id='setup-run-proof', disabled=True)

    async def on_mount(self):
        # Capture this view's widgets before any probe yields. Child panes can be
        # unmounted before the enclosing Screen reports that it is unmounted.
        key_widgets={env:(self.query_one('#setup-key-status-'+env,Static),
                          self.query_one('#setup-key-'+env,Button),
                          self.query_one('#setup-check-api-'+env,Button) if can_check(env) else None)
                     for env in self.credentials}
        cli_widgets={endpoint:(self.query_one('#setup-cli-status-'+endpoint.split('://')[1],Static),
                               self.query_one('#setup-check-cli-'+endpoint.split('://')[1],Button),
                               self.query_one('#setup-test-cli-'+endpoint.split('://')[1],Button))
                     for endpoint in self.found['clis']}
        local_widget=self.query_one('#setup-local-helper-status',Static)
        self._status_widgets=key_widgets,cli_widgets,local_widget
        self.run_worker(self.refresh_status(), group='setup-status', exclusive=True)

    async def refresh_status(self):
        from .environment import detect
        from .setup_status import cli_status, local_helper_status
        key_widgets,cli_widgets,local_widget=self._status_widgets
        found=await asyncio.to_thread(detect)
        if not self.is_mounted:return
        for env in self.credentials:
            status,button,check=key_widgets[env]
            if not status.is_mounted:return
            where=credential_where(found,env)
            self.credentials[env]=where
            status.update(key_status(where))
            button.label='Save vault key' if where=='env' else 'Change key' if where=='vault' else 'Add API key'
            if check is not None:check.disabled=where not in {'env','vault'}
        self.found['clis']=found['clis']
        async def cli(endpoint,path):
            status,check,test=cli_widgets[endpoint]
            text=await asyncio.to_thread(cli_status,endpoint,path)
            if status.is_mounted:
                status.update(text)
                check.disabled=test.disabled=not path
        async def local():
            text=await asyncio.to_thread(local_helper_status)
            if local_widget.is_mounted:local_widget.update(text)
        await asyncio.gather(*(cli(e,p) for e,p in self.found['clis'].items()),local())

    def chosen(self):
        from .environment import choose,old_models,read
        pick=lambda id:CHOICE[self.query_one('#setup-'+id,Select).value]
        value=read();value={} if 'error' in value else value
        for category in ('local_models','clis','api_providers','decision_models','search_providers'):
            value=choose(value,category,pick(category))
        for endpoint in self.found['clis']:
            value=choose(value,'cli:'+endpoint,pick('cli-'+endpoint.split('://')[1]))
        value=old_models(value,pick('old-models') is True)
        for env in self.found['api_providers']:
            value=choose(value,'api:'+env,pick('api-'+env))
        for name in ('jev','local_judge'):
            value=choose(value,'decision:'+name,pick('decision-'+name))
        value['search_providers']['preferred']=self.query_one('#setup-search-preferred',Select).value
        value['setup_complete']=True
        value['model_data_use'] = {**(value.get('model_data_use') or {}),
            'meta_contributor': self.query_one('#setup-meta-contributor', Checkbox).value}
        return value

    async def check_cost(self):
        from .environment import cost_line,route_for
        selected=self.query_one('#setup-prove',Select).value
        name=selected if isinstance(selected,str) else ''
        def price():
            deployment,why=route_for(name)
            return (cost_line(deployment),True) if deployment else (why,False)
        try:
            line,ready=await asyncio.to_thread(price) if name else ('Choose a connection first.',False)
        except (ValueError,OSError):
            line,ready='Could not check the test cost. Refresh and try again.',False
        if not self.is_mounted or self.query_one('#setup-prove',Select).value!=selected:
            return
        self.pending=name if ready else None
        if self.is_mounted:
            self.query_one('#setup-status',Static).update(str(line)+(' · Run reply test spends this allowance.' if ready else ''))
            self.query_one('#setup-run-proof',Button).disabled=not ready

    def on_select_changed(self,event):
        if event.select.id=='setup-prove':
            self.pending=None
            self.query_one('#setup-run-proof',Button).disabled=True

    def on_tabbed_content_tab_activated(self,event):
        if event.tabbed_content.id!='setup-tabs':return
        if event.pane.id=='setup-advanced' and self.reply_gate_pending:
            self.call_after_refresh(self.reveal_reply_gate)
        elif event.pane.id!='setup-advanced':self.reply_gate_pending=False

    def reveal_reply_gate(self):
        if not self.is_mounted or self.query_one('#setup-tabs',TabbedContent).active!='setup-advanced':return
        self.reply_gate_pending=False
        gate=self.query_one('#setup-cost',Button)
        # Reveal the whole row after the pane is active and laid out. A refresh
        # scheduled before TabActivated can still see the hidden pane's geometry.
        gate.parent.scroll_visible(animate=False,immediate=True)
        gate.focus(scroll_visible=False)

    async def on_button_pressed(self,event):
        event.stop();action=event.button.id or ''
        if action=='setup-close':
            self.action_close()
        elif action=='setup-refresh' or action.startswith('setup-check-cli-'):
            self.run_worker(self.refresh_status(),group='setup-status',exclusive=True)
        elif action.startswith('setup-test-cli-'):
            self.reply_gate_pending=True
            tabs=self.query_one('#setup-tabs',TabbedContent)
            if tabs.active=='setup-advanced':self.call_after_refresh(self.reveal_reply_gate)
            else:
                self.set_focus(None)
                tabs.active='setup-advanced'
            self.query_one('#setup-prove',Select).value='cli:'+action.removeprefix('setup-test-cli-')
            self.query_one('#setup-status',Static).update('Check the test cost below. No model request has been sent.')
        elif action.startswith('setup-key-'):
            from .credential_panel import CredentialPanel
            from .environment import detect
            env=action.removeprefix('setup-key-');target=credential_target(env)
            if target is None:
                self.query_one('#setup-status',Static).update('The profile is no longer available. Reopen setup and try again.');return
            def saved(success):
                if success and self.is_mounted:
                    found=detect()
                    where=credential_where(found,env)
                    self.credentials[env]=where
                    self.query_one('#setup-key-status-'+env,Static).update(key_status(where))
                    self.query_one('#'+action,Button).label='Save vault key' if where=='env' else 'Change key'
                    if can_check(env):
                        self.query_one('#setup-check-api-'+env,Button).disabled=where not in {'env','vault'}
                    self.query_one('#setup-status',Static).update('Key saved to the secure vault. Connection has not been checked.')
            self.app.push_screen(CredentialPanel(target,label=PROVIDERS.get(env,(env,))[0]),saved)
        elif action.startswith('setup-check-api-'):
            from .console_connection import probe
            from .native_catalog import available_models,select
            from tare_tools_agent_runtime import CancellationToken
            env=action.removeprefix('setup-check-api-')
            if env in self.checks:return
            name=PROVIDERS.get(env,('',None))[1] or next((r['deployment'] for r in available_models() if select(r['deployment']).get('native_credential_env')==env),None)
            if not name:
                self.query_one('#setup-status',Static).update('No profile is available for this key. Reopen setup and try again.');return
            self.checks.add(env);event.button.disabled=True
            self.query_one('#setup-key-status-'+env,Static).update('Checking connection · no model request…')
            try:
                key=(str(getattr(self.app,'workspace_root',Path.cwd())),'','',name,False)
                result=await asyncio.to_thread(probe,key,CancellationToken())
                if self.is_mounted:self.query_one('#setup-key-status-'+env,Static).update(result.status+' · '+result.detail)
            finally:
                self.checks.discard(env)
                if self.is_mounted:event.button.disabled=False
        elif action=='setup-save':
            from .environment import add_gateway,apply
            value=self.chosen();url=self.query_one('#setup-gateway',Input).value.strip()
            def save():
                known=[g['origin'] for g in (value.get('local_models') or {}).get('gateways') or []]
                selected=value
                if (value.get('local_models') or {}).get('use') is not False and url and url.rstrip('/') not in known:
                    selected=add_gateway(selected,url)
                return apply(selected)
            try:
                self.value,_,_=await asyncio.to_thread(save)
                message='Saved.'
            except ValueError:
                message='Could not save settings. Check the local server address and try again.'
            except OSError:
                message='Could not write settings on this computer. Check disk space and file access, then try again.'
            if self.is_mounted:self.query_one('#setup-status',Static).update(message)
        elif action=='setup-cost':await self.check_cost()
        elif action=='setup-run-proof' and self.pending:
            from .environment import prove
            name,self.pending=self.pending,None;self.query_one('#setup-run-proof',Button).disabled=True
            result=await asyncio.to_thread(prove,name,lambda line:True)
            if self.is_mounted:
                self.query_one('#setup-status',Static).update('Reply test passed.' if result['ok'] else 'Reply test failed: '+result['reason'])
                await self.refresh_status()
        elif action in ('setup-replace','setup-keep'):
            from .environment import apply,keep_older,read,replace_older,replacements
            try:
                if action=='setup-replace':await asyncio.to_thread(replace_older,replacements())
                else:await asyncio.to_thread(apply,keep_older(read(),replacements()))
                message='Model choice saved for new conversations.'
            except (ValueError,OSError):message='Could not save the model choice. Refresh and try again.'
            if self.is_mounted:self.query_one('#setup-status',Static).update(message)

    def action_close(self):
        self.app.workers.cancel_group(self,'setup-status')
        self.dismiss(None)
