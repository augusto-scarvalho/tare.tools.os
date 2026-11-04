"""Credential entry uses the vault and never echoes backend errors."""
from types import SimpleNamespace

import keyring
import asyncio
from textual.app import App
from textual.widgets import Input, Static

from relay.credential_panel import CredentialPanel


def test_masked_entry_backend_failure_and_success(monkeypatch):
    asyncio.run(check_panel(monkeypatch))


async def check_panel(monkeypatch):
    saved = []
    target = SimpleNamespace(model='fixture', endpoint='http://localhost', credential_env='TARE_FIXTURE_KEY')
    class Host(App):
        def on_mount(self):
            self.push_screen(CredentialPanel(target), saved.append)
    def fail(*args):
        raise RuntimeError('secret-should-not-leak')
    monkeypatch.setattr(keyring, 'set_password', fail)
    app = Host()
    async with app.run_test(size=(120, 40)) as pilot:
        field = app.screen.query_one('#credential-value', Input)
        assert field.password
        await pilot.click('#credential-save')
        await pilot.pause()
        assert field.has_focus and not saved
        assert 'Enter an API key.' in str(app.screen.query_one('#credential-error', Static).render())
        await pilot.press(*'secret-should-not-leak')
        await pilot.pause(.35)  # Let the prior button's active/debounce interval finish.
        await pilot.click('#credential-save')
        await pilot.pause(.3)
        assert not saved
        assert 'Could not save' in str(app.screen.query_one('#credential-error', Static).render())
        assert 'secret-should-not-leak' not in str(app.screen.query_one('#credential-error', Static).render())
        calls = []
        monkeypatch.setattr(keyring, 'set_password', lambda *args: calls.append(args))
        await pilot.click('#credential-save')
        await pilot.pause(.3)
        assert calls == [('universal-agent-harness', 'TARE_FIXTURE_KEY', 'secret-should-not-leak')]
        assert saved == [True] and field.value == ''
