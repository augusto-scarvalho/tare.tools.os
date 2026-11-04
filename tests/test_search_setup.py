"""Configure search through public TUI controls; no search or model call on setup."""
import asyncio
from io import StringIO
import json

import pytest
from textual.widgets import Button, Input, Select, Static, TabbedContent

from relay import environment, setup_status, web_search
from test_setup_guidance import Host, observed


@pytest.mark.parametrize('size', [(80, 24), (130, 44)])
def test_search_tab_saves_a_masked_vault_key_and_preference_without_generating(observed, monkeypatch, size):
    import keyring
    observed['api_providers']['DEEPSEEK_API_KEY'] = 'absent'  # One key can serve an inference profile and search.
    observed['search_providers'] = {'DEEPSEEK_API_KEY': 'absent', 'SERPAPI_API_KEY': 'env', 'FIRECRAWL_API_KEY': 'vault'}
    written = []
    def store(service, entry, secret):
        written.append((service, entry, secret))
        observed['search_providers'][entry] = 'vault'
    monkeypatch.setattr(keyring, 'set_password', store)
    monkeypatch.setattr(web_search, 'request', lambda *a, **kw: pytest.fail('Setup spent a search credit'))
    async def scenario():
        app = Host()
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            panel = app.screen
            assert len(panel.query('#setup-key-DEEPSEEK_API_KEY')) == 1
            await pilot.click('#--content-tab-setup-search'); await pilot.pause()
            assert panel.query_one('#setup-tabs', TabbedContent).active == 'setup-search'
            for env, text in [('DEEPSEEK_API_KEY', 'No API key'), ('SERPAPI_API_KEY', 'outside Tare'),
                              ('FIRECRAWL_API_KEY', 'secure vault')]:
                assert text in str(panel.query_one('#setup-key-status-'+env, Static).render())
                assert not list(panel.query('#setup-check-api-'+env))
            panel.query_one('#setup-search_providers', Select).value = 'no'
            panel.query_one('#setup-search-preferred', Select).value = 'firecrawl'
            button = panel.query_one('#setup-key-DEEPSEEK_API_KEY', Button)
            button.scroll_visible(animate=False, immediate=True); await pilot.pause()
            await pilot.click('#setup-key-DEEPSEEK_API_KEY'); await pilot.pause()
            field = app.screen.query_one('#credential-value', Input)
            assert field.password
            await pilot.press(*'fixture-search-key'); await pilot.click('#credential-save'); await pilot.pause()
            assert written == [('universal-agent-harness', 'DEEPSEEK_API_KEY', 'fixture-search-key')]
            assert field.value == '' and panel.query_one('#setup-search_providers', Select).value == 'no'
            assert 'secure vault' in str(panel.query_one('#setup-key-status-DEEPSEEK_API_KEY', Static).render())
            await pilot.click('#setup-save'); await pilot.pause()
            saved = environment.read()
            assert saved['search_providers']['use'] is False
            assert saved['search_providers']['preferred'] == 'firecrawl'
            assert 'fixture-search-key' not in json.dumps(saved)
    asyncio.run(scenario())


def test_cli_stores_search_credentials_in_the_search_category(monkeypatch):
    import keyring
    written = []
    monkeypatch.delenv('SERPAPI_API_KEY', raising=False)
    monkeypatch.setattr(keyring, 'set_password', lambda *args: written.append(args))
    value = environment.store_credential({}, 'SERPAPI_API_KEY', StringIO('fixture-serp-key\n'))
    assert written == [('universal-agent-harness', 'SERPAPI_API_KEY', 'fixture-serp-key')]
    assert value == {'search_providers': {'items': {'serpapi': {'use': True}}}}
    assert environment.choose({}, 'search:DeepSeek', False)['search_providers']['items']['deepseek']['use'] is False
    with pytest.raises(ValueError, match='search:deepseek'):
        environment.choose({}, 'search:unknown', False)
    for env, name in web_search.CREDENTIALS.items():
        target = setup_status.credential_target(env)
        assert target.credential_env == env and target.endpoint == web_search.PROVIDERS[name]['endpoint']
        assert not setup_status.can_check(env)
