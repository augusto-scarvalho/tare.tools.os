"""Save the selected profile's API key in the existing OS credential vault."""
import os

from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Collapsible, Input, Static

from .console_panels import SupportPanel


class CredentialPanel(SupportPanel):
    DEFAULT_CSS = '''
    CredentialPanel .support-body { width: 90; max-width: 96%; height: auto; padding: 1 2; }
    CredentialPanel Static { height: auto; margin-bottom: 1; }
    '''

    def __init__(self, target, *, label=None):
        super().__init__()
        self.target = target
        self.label = label or target.model

    def compose(self):
        with Vertical(classes='support-body'):
            yield Static('API key · '+self.label, classes='panel-title', markup=False)
            yield Static('Paste your API key. It is saved securely on this computer, outside your conversations.', markup=False)
            if os.environ.get(self.target.credential_env):
                yield Static('A key configured outside Tare is active. It takes priority over a key saved here.', markup=False)
            yield Input(password=True, placeholder='API key', id='credential-value')
            with Collapsible(title='Connection details', collapsed=True):
                yield Static('Server: '+self.target.endpoint+'\nKey setting: '+self.target.credential_env, markup=False)
            yield Static('', id='credential-error', markup=False)
            with Horizontal(classes='panel-actions'):
                yield Button('Cancel', id='credential-cancel')
                yield Button('Save API key', id='credential-save', classes='panel-primary')

    def on_mount(self):
        self.query_one('#credential-value', Input).focus()

    def on_button_pressed(self, event):
        event.stop()
        field = self.query_one('#credential-value', Input)
        if event.button.id == 'credential-cancel':
            field.value = ''
            self.dismiss(False)
            return
        if event.button.id != 'credential-save':
            return
        secret = field.value.strip()
        if not secret:
            self.query_one('#credential-error', Static).update('Enter an API key.')
            field.focus()
            return
        import keyring
        try:
            keyring.set_password('universal-agent-harness', self.target.credential_env, secret)
        except Exception:
            # Backend exceptions can contain the secret; never render their message.
            self.query_one('#credential-error', Static).update('Could not save the credential. Unlock or configure your OS credential vault and try again.')
            return
        field.value = ''
        self.dismiss(True)

    def action_close(self):
        self.query_one('#credential-value', Input).value = ''
        self.dismiss(False)


def authenticate(app):
    from .console_connection import target_for
    try:
        target = target_for(app.header_key())
    except Exception:
        app.notify('Choose a valid model profile before authenticating.', severity='error')
        return
    if target is None or not target.credential_env:
        app.notify('This profile does not use an API key. Choose an API profile or sign in through its provider CLI.')
        return
    if os.environ.get(target.credential_env):
        app.notify('This credential is supplied by the environment. Remove that override before saving a different key.')
        return
    def saved(success):
        if success:
            app.notify('Credential saved. Use Check to verify the connection.')
            app.connection_observation = None
            app.sync_header(force=True)
    app.push_screen(CredentialPanel(target), saved)
