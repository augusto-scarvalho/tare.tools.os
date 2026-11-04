import asyncio

import pytest
from rich.text import Text
from textual.widgets import Button, Input, TextArea

from relay.changes_panel import ChangesPanel
from relay.console_attachments import AttachmentPicker, draft_store
from relay.native_console import WorkConsole
from relay.terminal_panel import TerminalPanel
from test_native_console import FakeBackend


DIFF = '--- a/src/parser.py\n+++ b/src/parser.py\n@@ -1,2 +1,2 @@\n-value = 1\n+value = 2\n # Keep the public interface unchanged.\n'


def recorded_review(custody, **kwargs):
    return {'turns': [{'id': 'turn-1', 'status': 'PROPOSED'}],
            'edits': [{'effect_id': 'edit-1', 'repository': 'project',
                       'path': 'src/parser.py', 'status': 'succeeded', 'diff': DIFF}],
            'checks': [{'name': 'Parser tests', 'returncode': 0, 'status': 'succeeded', 'freshness': 'STALE'}],
            'unresolved_effects': False, 'changed_since_observation': True, 'unavailable_files': []}


class PanelService:
    def __init__(self):
        self.state = 'running'
        self.mutations = []
        self.empty = False

    def list(self):
        return [] if self.empty else [{'handle': 'process-1', 'command': 'python -m project.server --port 8080',
                                      'status': self.state, 'mode': 'pty'}]

    def read(self, handle, cursors):
        return {'status': self.state, 'elapsed_ms': 12500, 'exit_code': None if self.state == 'running' else 0,
                'cleanup_verified': self.state != 'running', 'mode': 'pty', 'output': {}}

    def view(self, handle, cursor=False):
        return {'text': Text('Starting development server…\nListening on http://localhost:8080\nGET /health 200\n'), 'issue': None}

    def send(self, handle, text):
        self.mutations.append(('send', handle, text))
        return {'state': 'written'}

    def stop(self, handle):
        self.mutations.append(('stop', handle))
        self.state = 'stopped'
        return {'cleanup_verified': True}


@pytest.mark.parametrize('size', [(80, 24), (130, 44)])
@pytest.mark.parametrize('theme', ['tare-dark', 'tare-light', 'tare-contrast'])
def test_panels_share_visible_navigation_and_preserve_draft(tmp_path, monkeypatch, size, theme):
    root = tmp_path/'project'; root.mkdir()
    (root/'notes.md').write_text('Preserve the original delimiter.\n')
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))

    async def scenario():
        app = WorkConsole(root, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete(); app.theme = theme
            prompt = app.query_one('#prompt', TextArea); prompt.load_text('Keep this unsent question.')
            copies = []; app.copy_to_clipboard = copies.append
            changes = ChangesPanel(tmp_path/'custody', root, reader=recorded_review)
            app.push_screen(changes); await pilot.pause(); await app.workers.wait_for_complete(); await pilot.pause()
            assert changes.query_one('#changes-scroll').region.height >= 6
            for name in ('changes-close', 'changes-refresh', 'changes-copy'):
                button = changes.query_one('#'+name, Button)
                assert changes.region.contains_region(button.region)
                assert str(button.label) in button.render_line(0).text
            changes.query_one('#changes-copy', Button).press(); await pilot.pause()
            assert copies == [DIFF]
            await pilot.click('#changes-close'); await pilot.pause()
            service = PanelService()
            terminal = TerminalPanel(service, title=root.name)
            app.push_screen(terminal); await pilot.pause(.5)
            assert terminal.query_one('#terminal-scroll').region.height >= 8
            for button in terminal.query(Button):
                assert terminal.region.contains_region(button.region), button.id
                assert str(button.label) in button.render_line(0).text, button.id
            entry = terminal.query_one('#terminal-entry', Input)
            entry.focus(); entry.value = 'visible program input'; await pilot.pause()
            assert entry.content_region.height == 1
            assert 'visible program input' in entry.render_line(0).text
            service.state = 'exited'; await pilot.pause(.5)
            assert terminal.query_one('#terminal-send').disabled
            assert not terminal.query_one('#terminal-close').disabled
            await pilot.click('#terminal-close'); await pilot.pause()
            assert not service.mutations
            picker = AttachmentPicker(root, draft_store(root), initial_path=root/'notes.md')
            app.push_screen(picker); await pilot.pause(); await app.workers.wait_for_complete(); await pilot.pause()
            assert picker.selected is not None
            assert picker.query_one('.panel-header').region.contains_region(picker.query_one('#attachment-title').region)
            for name in ('attachment-cancel', 'attachment-confirm', 'attachment-up'):
                button = picker.query_one('#'+name, Button)
                assert picker.region.contains_region(button.region)
                assert str(button.label) in button.render_line(0).text
            await pilot.click('#attachment-cancel'); await pilot.pause()
            assert prompt.text == 'Keep this unsent question.' and not app.attachments
            assert not app.backend.launches
    asyncio.run(scenario())


def test_empty_terminal_keeps_back_available_without_enabling_process_actions(tmp_path):
    async def scenario():
        app = WorkConsole(tmp_path, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            service = PanelService(); service.empty = True
            panel = TerminalPanel(service, title='Empty conversation')
            app.push_screen(panel); await pilot.pause(.5)
            assert panel.query_one('#terminal-entry', Input).disabled
            assert all(button.disabled for button in panel.query('#terminal-buttons Button'))
            assert not panel.query_one('#terminal-close').disabled
            await pilot.click('#terminal-close'); await pilot.pause()
            assert app.screen is not panel and not service.mutations
    asyncio.run(scenario())


def test_refresh_cannot_copy_a_previous_diff_when_review_is_unavailable(tmp_path):
    state = {'available': True}

    def reader(custody, **kwargs):
        if not state['available']:
            raise OSError('Saved review is unavailable.')
        return recorded_review(custody, **kwargs)

    async def scenario():
        app = WorkConsole(tmp_path, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            copies = []; app.copy_to_clipboard = copies.append
            panel = ChangesPanel(tmp_path/'custody', tmp_path, reader=reader)
            app.push_screen(panel); await pilot.pause(); await app.workers.wait_for_complete(); await pilot.pause()
            copy = panel.query_one('#changes-copy', Button)
            copy.press(); await pilot.pause()
            assert copies == [DIFF]
            state['available'] = False
            await pilot.press('ctrl+r'); await app.workers.wait_for_complete(); await pilot.pause()
            assert panel.diff_text is None and copy.disabled and str(copy.label) == 'Copy diff'
            copy.press(); await pilot.pause()
            assert copies == [DIFF]
            await pilot.click('#changes-close'); await pilot.pause()
            assert app.screen is not panel and not app.backend.launches
    asyncio.run(scenario())
