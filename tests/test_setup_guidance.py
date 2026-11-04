"""Setup distinguishes installed, authenticated and tested without generating on open."""
import asyncio
import json

import pytest
from textual.app import App
from textual.containers import VerticalScroll
from textual.widget import Widget
from textual.widgets import Button, Checkbox, Input, Select, Static, TabbedContent, TabPane

from relay import cli_discovery, environment, setup_status
from relay.console_setup import SetupPanel
from test_http_api_providers import api


@pytest.mark.parametrize('login,expected', [(False,'sign-in needed'),(True,'signed in'),(None,'sign-in not verified')])
def test_cli_install_login_and_reply_evidence_are_separate(tmp_path, monkeypatch, login, expected):
    executable=tmp_path/'codex.exe';executable.write_bytes(b'fixture')
    cache=tmp_path/'cache.json'
    monkeypatch.setattr(cli_discovery,'cache_file',lambda:cache)
    monkeypatch.setattr(cli_discovery,'logged_in',lambda *args:login)
    assert 'Not installed' in setup_status.cli_status('cli://codex',None)
    text=setup_status.cli_status('cli://codex',str(executable))
    assert expected in text and 'reply test passed' not in text
    if login is False:assert 'codex login' in text
    cache.write_text(json.dumps({'cli://codex':{'fingerprint':cli_discovery.fingerprint(str(executable)), 'checked_at':'2026-10-23'}}))
    text=setup_status.cli_status('cli://codex',str(executable))
    assert ('reply test passed' in text) is (login is not False)
    executable.write_bytes(b'updated executable')
    assert 'reply test passed' not in setup_status.cli_status('cli://codex',str(executable))


@pytest.mark.parametrize('cached',[[],None,{'cli://codex':None},{'cli://codex':[]}])
def test_invalid_old_reply_cache_does_not_hide_cli_sign_in(tmp_path,monkeypatch,cached):
    cache=tmp_path/'cache.json';cache.write_text(json.dumps(cached))
    monkeypatch.setattr(cli_discovery,'cache_file',lambda:cache)
    monkeypatch.setattr(cli_discovery,'logged_in',lambda *args:True)
    text=setup_status.cli_status('cli://codex','fixture-codex')
    assert 'signed in' in text and 'reply not tested' in text


@pytest.fixture
def observed(monkeypatch):
    found={'clis':{'cli://codex':None},'api_providers':{'OPENAI_API_KEY':'absent','GEMINI_API_KEY':'env'},
           'decision_models':{'jev':'absent'}}
    monkeypatch.setattr(environment,'detect',lambda:found)
    monkeypatch.setattr(environment,'replacements',lambda *args:[])
    monkeypatch.setattr(setup_status,'local_helper_status',lambda:'Qwen3.8 not available now · no API key needed')
    monkeypatch.setattr('relay.native_generation.generate_text',lambda *args,**kwargs:pytest.fail('unexpected model request'))
    monkeypatch.setattr(environment,'prove',lambda *args,**kwargs:pytest.fail('reply test was not authorized'))
    return found


class Host(App):
    def on_mount(self):self.push_screen(SetupPanel())


@pytest.mark.parametrize('size',[(80,24),(120,44)])
def test_guided_tabs_key_state_refresh_and_unsaved_choices(observed,monkeypatch,size):
    async def scenario():
        app=Host()
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            panel=app.screen;tabs=panel.query_one('#setup-tabs',TabbedContent)
            assert tabs.active=='setup-connections'
            assert 'No API key' in str(panel.query_one('#setup-key-status-OPENAI_API_KEY',Static).render())
            assert 'outside Tare' in str(panel.query_one('#setup-key-status-GEMINI_API_KEY',Static).render())
            assert panel.query_one('#setup-check-api-OPENAI_API_KEY',Button).disabled
            assert not panel.query_one('#setup-check-api-GEMINI_API_KEY',Button).disabled
            assert panel.query_one('#setup-save',Button).region.height==1
            assert panel.region.contains_region(panel.query_one('#setup-save',Button).region)
            panel.query_one('#setup-api-OPENAI_API_KEY',Select).value='no'
            observed['api_providers']['OPENAI_API_KEY']='vault'
            await pilot.click('#setup-refresh');await app.workers.wait_for_complete()
            assert 'secure vault' in str(panel.query_one('#setup-key-status-OPENAI_API_KEY',Static).render())
            assert not panel.query_one('#setup-check-api-OPENAI_API_KEY',Button).disabled
            assert panel.query_one('#setup-api-OPENAI_API_KEY',Select).value=='no'
            tabs.active='setup-helpers';await pilot.pause()
            assert panel.query_one('#setup-key-TYPESAFE_API_KEY',Button)
            assert 'Qwen3.8' in str(panel.query_one('#setup-local-helper-status',Static).render())
            assert panel.query_one('#setup-run-proof',Button).disabled
            await pilot.click('#setup-save');await pilot.pause()
            saved=environment.read()
            assert saved['setup_complete'] and environment.decided(saved)
            assert saved['api_providers']['items']['OPENAI_API_KEY']['use'] is False
            assert 'GEMINI_API_KEY' not in json.dumps(saved)  # unchanged credential presence is not copied into policy
    asyncio.run(scenario())


def test_jev_key_uses_existing_vault_and_keeps_settings(observed,monkeypatch):
    import keyring
    written=[]
    def store(service,entry,secret):
        written.append((service,entry,secret));observed['decision_models']['jev']='vault'
    monkeypatch.setattr(keyring,'set_password',store)
    async def scenario():
        app=Host()
        async with app.run_test(size=(120,44)) as pilot:
            await app.workers.wait_for_complete()
            panel=app.screen
            panel.query_one('#setup-decision-jev',Select).value='no'
            panel.query_one('#setup-tabs',TabbedContent).active='setup-helpers';await pilot.pause()
            button=panel.query_one('#setup-key-TYPESAFE_API_KEY',Button);button.scroll_visible();await pilot.pause()
            await pilot.click('#setup-key-TYPESAFE_API_KEY');await pilot.pause()
            field=app.screen.query_one('#credential-value',Input)
            assert field.password
            await pilot.press(*'fixture-jev-key');await pilot.click('#credential-save');await pilot.pause()
            assert written==[('universal-agent-harness','TYPESAFE_API_KEY','fixture-jev-key')]
            assert field.value=='' and panel.query_one('#setup-decision-jev',Select).value=='no'
            assert 'secure vault' in str(panel.query_one('#setup-key-status-TYPESAFE_API_KEY',Static).render())
            assert 'fixture-jev-key' not in json.dumps(environment.read())
    asyncio.run(scenario())


def test_key_presence_is_not_claimed_as_authentication():
    for where in ('vault','env'):
        assert 'not checked' in setup_status.key_status(where)
    assert 'No API key' in setup_status.key_status('absent')


def test_contributor_choice_is_visible_and_saved_without_inference(observed, monkeypatch):
    monkeypatch.setattr('relay.native_generation.generate_text', lambda *a, **k: pytest.fail('Settings made an inference call'))
    async def scenario():
        app = Host()
        async with app.run_test(size=(120, 44)) as pilot:
            await app.workers.wait_for_complete()
            panel = app.screen
            choice = panel.query_one('#setup-meta-contributor', Checkbox)
            assert not choice.value
            choice.scroll_visible(); await pilot.pause()
            await pilot.click('#setup-meta-contributor')
            save = panel.query_one('#setup-save', Button)
            save.scroll_visible(); await pilot.pause()
            await pilot.click('#setup-save')
            await app.workers.wait_for_complete()
            assert environment.read()['model_data_use']['meta_contributor'] is True
    asyncio.run(scenario())


def test_late_status_after_child_panes_unmount_does_not_crash_setup(observed,monkeypatch):
    from threading import Event
    entered,release=Event(),Event()
    def slow_status():
        entered.set();assert release.wait(5);return 'Late local observation'
    monkeypatch.setattr(setup_status,'local_helper_status',slow_status)
    async def scenario():
        app=Host()
        try:
            async with app.run_test(size=(100,40)) as pilot:
                assert await asyncio.to_thread(entered.wait,3)
                panel=app.screen
                await panel.query_one('#setup-tabs',TabbedContent).remove()
                assert panel.is_mounted  # the parent outlives its disappearing panes
                release.set()
                await app.workers.wait_for_complete()
                assert app._exception is None
        finally:release.set()
    asyncio.run(scenario())


@pytest.mark.parametrize('size', [(80,24),(120,44)])
@pytest.mark.parametrize('delayed_tab',[False,True])
def test_cli_reply_button_opens_visible_cost_gate_without_generating(observed,monkeypatch,size,delayed_tab):
    observed['clis']['cli://codex']='fixture-codex'
    monkeypatch.setattr(setup_status,'cli_status',lambda *args:'Installed and signed in · reply not tested')
    monkeypatch.setattr(environment,'route_for',lambda name:({'id':name},None))
    monkeypatch.setattr(environment,'cost_line',lambda deployment:'Uses subscription quota')
    activate=TabbedContent._watch_active
    def delayed_activate(tabs,active):
        if delayed_tab and tabs.id=='setup-tabs' and active=='setup-advanced':
            tabs.set_timer(.1,lambda:activate(tabs,active))
        else:activate(tabs,active)
    monkeypatch.setattr(TabbedContent,'_watch_active',delayed_activate)
    async def scenario():
        app=Host()
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete();panel=app.screen
            button=panel.query_one('#setup-test-cli-codex',Button)
            button.scroll_visible();await pilot.pause()
            assert not button.disabled
            # A slow animation must not hide the next action after switching
            # tabs. Reproduce the delayed reveal seen on the Windows runner.
            reveal = Widget.scroll_visible
            def slow_reveal(widget, animate=True, **kwargs):
                if animate:
                    kwargs.update(speed=None, duration=5)
                return reveal(widget, animate=animate, **kwargs)
            monkeypatch.setattr(Widget, 'scroll_visible', slow_reveal)
            assert await pilot.click('#setup-test-cli-codex')
            tabs=panel.query_one('#setup-tabs',TabbedContent)
            old_pane=panel.query_one('#setup-connections',TabPane)
            tabs.post_message(TabPane.Focused(old_pane))
            await pilot.pause(.4)
            assert panel.query_one('#setup-tabs',TabbedContent).active=='setup-advanced'
            assert panel.query_one('#setup-prove',Select).value=='cli:codex'
            button=panel.query_one('#setup-run-proof',Button)
            assert panel.pending is None and button.disabled
            assert panel.region.contains_region(button.region)
            viewport=panel.query_one('#setup-advanced').query_one(VerticalScroll)
            assert viewport.scrollable_content_region.contains_region(button.region)
            assert app.focused is panel.query_one('#setup-cost',Button)
            # A focus notification already queued by the old pane must not
            # switch back after the new action has received keyboard focus.
            tabs.post_message(TabPane.Focused(old_pane))
            await pilot.pause()
            assert tabs.active=='setup-advanced'
            assert app.focused is panel.query_one('#setup-cost',Button)
            await pilot.click('#setup-cost');await pilot.pause(.4)
            assert panel.pending=='cli:codex' and not button.disabled
            assert viewport.scrollable_content_region.contains_region(button.region)
            assert 'subscription quota' in str(panel.query_one('#setup-status',Static).render())
    asyncio.run(scenario())


def test_api_check_is_metadata_only_and_does_not_claim_a_model_reply(api,observed,monkeypatch):
    from copy import deepcopy
    from relay import native_catalog
    original=native_catalog.select
    deployment=original('openai-api')
    deployment['native_profile']['endpoint']=api['endpoint']
    api['model']=deployment['native_profile']['model']
    monkeypatch.setattr(native_catalog,'select',lambda name,*args,**kwargs:deepcopy(deployment) if name=='openai-api' else original(name,*args,**kwargs))
    monkeypatch.setenv('OPENAI_API_KEY','fixture-key-not-for-display')
    observed['api_providers']['OPENAI_API_KEY']='env'
    async def scenario():
        app=Host()
        async with app.run_test(size=(120,44)) as pilot:
            await app.workers.wait_for_complete()
            panel=app.screen;button=panel.query_one('#setup-check-api-OPENAI_API_KEY',Button)
            button.scroll_visible();await pilot.pause()
            await pilot.click('#setup-check-api-OPENAI_API_KEY');await pilot.pause(.5)
            assert [(x[0],x[1]) for x in api['calls']]==[('GET','/v1/models')]
            text=str(panel.query_one('#setup-key-status-OPENAI_API_KEY',Static).render())
            assert 'Model listed' in text and 'not tested' in text
            assert 'fixture-key-not-for-display' not in text
    asyncio.run(scenario())


def test_changed_reply_test_selection_cannot_use_an_old_quote(observed,monkeypatch):
    from threading import Event
    observed['clis']['cli://agy']=None
    entered,release=Event(),Event()
    monkeypatch.setattr(environment,'route_for',lambda name:({'id':name},None))
    def cost(deployment):
        entered.set();assert release.wait(3);return 'fixture quota allowance'
    monkeypatch.setattr(environment,'cost_line',cost)
    async def scenario():
        app=Host()
        async with app.run_test(size=(120,44)) as pilot:
            await app.workers.wait_for_complete();panel=app.screen
            route=panel.query_one('#setup-prove',Select);route.value='cli:codex'
            await pilot.pause()
            quote=asyncio.create_task(panel.check_cost())
            assert await asyncio.to_thread(entered.wait,2)
            route.value='cli:agy';release.set();await quote;await pilot.pause()
            assert panel.pending is None and panel.query_one('#setup-run-proof',Button).disabled
    asyncio.run(scenario())
