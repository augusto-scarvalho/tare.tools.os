"""Disk origins, protected items and explicit cleanup preview."""
import asyncio
from pathlib import Path
import threading

from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Checkbox, Input, SelectionList, Static, TextArea

from .console_output import plain
from .console_panels import SupportPanel
from . import storage


def amount(value):
    return f'{value/1024**3:.2f} GiB' if value >= 1024**3 else f'{value/1024**2:.1f} MiB'


class StoragePanel(SupportPanel):
    BINDINGS = [Binding('escape', 'close', 'Back', priority=True)]
    DEFAULT_CSS = '''
    StoragePanel #storage-summary { height: auto; max-height: 3; }
    StoragePanel #storage-origins { height: 1fr; min-height: 3; border: none; }
    StoragePanel #storage-items { height: 1fr; min-height: 3; border: none; }
    StoragePanel #storage-confirm { height: auto; border: none; padding: 0; }
    StoragePanel #storage-status { height: auto; max-height: 3; }
    StoragePanel .panel-actions Button { min-width: 10; }
    StoragePanel #storage-auto-row { height: auto; }
    StoragePanel #storage-days { width: 14; }
    '''

    def __init__(self, root, home):
        super().__init__()
        self.root, self.home = Path(root), Path(home)
        self.report, self.running = None, False
        self.cleaning = False
        self.cancelled = threading.Event()

    def compose(self):
        with Vertical(classes='support-body'):
            with Horizontal(classes='panel-header'):
                yield Static('Storage and cleanup', classes='panel-title')
                yield Button('Back · Esc', id='storage-close')
            yield Input(str(self.root), placeholder='Operational storage folder', id='storage-root')
            yield Static('Scan measures file sizes; it does not delete anything.', id='storage-summary')
            yield TextArea('', read_only=True, id='storage-origins')
            yield SelectionList(id='storage-items')
            yield Checkbox('Delete the selected items', id='storage-confirm')
            settings = storage.auto_settings()
            with Horizontal(id='storage-auto-row'):
                yield Checkbox('Clean old logs, versions and caches of other tools automatically (daily; hourly '
                               'while the disk is low)', settings['enabled'], id='storage-auto')
                yield Input(str(settings['days']), placeholder='days kept', id='storage-days', restrict='[0-9]*',
                            max_length=3, tooltip='Days old logs and test folders are kept (1-365)')
            yield Static('Sessions, evidence, workspaces and rollback are preserved.', id='storage-status')
            with Horizontal(classes='panel-actions'):
                yield Button('Scan', id='storage-scan')
                yield Button('Clean selected', id='storage-clean', disabled=True, classes='panel-danger')

    def on_mount(self):
        self.run_worker(self.scan(), exclusive=True, group='storage')

    def action_close(self):
        if self.cleaning:
            self.query_one('#storage-status', Static).update('Cleanup is finishing. Return when the result is recorded.')
        else:
            self.cancelled.set()
            self.dismiss(None)

    def on_unmount(self):
        self.cancelled.set()

    def scan_ui_available(self):
        # A screen can still report mounted while its children are being pruned.
        return (not self.cancelled.is_set() and self.is_mounted
                and all(self.query('#storage-'+name) for name in
                        ('root', 'status', 'clean', 'confirm', 'items', 'origins', 'summary')))

    def update_button(self):
        self.query_one('#storage-clean', Button).disabled = (
            self.running or self.report is None or not self.query_one('#storage-confirm', Checkbox).value
            or not self.query_one('#storage-items', SelectionList).selected)

    def on_checkbox_changed(self, event):
        if event.checkbox.id == 'storage-confirm':
            self.update_button()
        elif event.checkbox.id == 'storage-auto':
            self.save_auto()

    def days(self):
        value = self.query_one('#storage-days', Input).value
        return int(value) if value.isdigit() and 1 <= int(value) <= 365 else None

    def save_auto(self):
        """The machine's opt-in (relay/storage.py); the product updater runs it when due."""
        days = self.days()
        if days is None:
            self.query_one('#storage-status', Static).update('Keep logs between 1 and 365 days.')
            return
        try:
            settings = storage.save_auto_settings(enabled=self.query_one('#storage-auto', Checkbox).value, days=days)
        except OSError as exc:
            self.query_one('#storage-status', Static).update(plain('Could not save automatic cleanup: '+str(exc)))
            return
        self.query_one('#storage-status', Static).update(
            f'Automatic cleanup on: daily, keeping {days} days of logs from other tools.' if settings['enabled'] else
            'Automatic cleanup off: nothing is removed unless you select it here.')

    def on_selection_list_selected_changed(self, event):
        self.query_one('#storage-confirm', Checkbox).value = False
        self.update_button()

    def on_input_changed(self, event):
        if event.input.id == 'storage-days' and self.days() is not None:
            self.save_auto()
            self.report = None  # what is due depends on the days kept
            self.query_one('#storage-items', SelectionList).clear_options()
            self.update_button()
        if event.input.id == 'storage-root' and self.report is not None:
            self.report = None
            self.query_one('#storage-confirm', Checkbox).value = False
            self.update_button()

    async def scan(self):
        if self.running:
            return
        self.running = True
        self.cancelled.clear()
        self.report = None
        self.update_button()
        self.query_one('#storage-confirm', Checkbox).value = False
        self.query_one('#storage-root', Input).disabled = True
        self.query_one('#storage-status', Static).update('Measuring storage and checking protections…')
        try:
            root = Path(self.query_one('#storage-root', Input).value)
            if not root.is_absolute() or not root.is_dir():
                raise ValueError('Choose an existing absolute storage folder.')
            def progress(message):
                if self.scan_ui_available():
                    self.app.call_from_thread(self.show_progress, message)
            report = await asyncio.to_thread(storage.scan, root, self.home, progress=progress, cancelled=self.cancelled,
                                             days=self.days())
            if not self.scan_ui_available():
                return
            self.report = report
            groups = {}
            lines = []
            for row in report['rows']:
                group = groups.setdefault(row['origin'], [0, 0, False])
                group[0] += row['bytes']
                group[1] += row['bytes'] if row['eligible'] else 0
                group[2] |= not row['complete']
            for name, (total, removable, partial) in sorted(groups.items(), key=lambda item: -item[1][0]):
                lines.append(f'{name}: {amount(total)}'+('+' if partial else '')+f' · cleanup {amount(removable)}')
            lines.extend(['', 'All measured paths (logical bytes; linked targets excluded):'])
            for row in sorted(report['rows'], key=lambda r: -r['bytes']):
                lines.append(f"{amount(row['bytes'])}{'+' if not row['complete'] else ''} | "
                             f"{'CANDIDATE' if row['eligible'] else 'KEEP'} | {row['path']}\n  {row['reason']}")
            self.query_one('#storage-origins', TextArea).load_text(plain('\n'.join(lines)))
            choices = self.query_one('#storage-items', SelectionList)
            choices.clear_options()
            choices.add_options([(plain(amount(r['bytes'])+' · '+Path(r['path']).name+' · '+r['origin']), r['id'], False)
                                 for r in report['rows'] if r['eligible']])
            total = sum(r['bytes'] for r in report['rows'] if r['eligible'])
            free = ' / '.join(f'{amount(v["free"])} free'+(' · LOW SPACE' if v['low'] else '') for v in report['volumes'].values())
            self.query_one('#storage-summary', Static).update(plain(free+f'\nAvailable cleanup: {amount(total)}. Select items below.'))
            complete = all(r['complete'] for r in report['rows'])
            self.query_one('#storage-status', Static).update(
                'Preview ready. Unselected items are kept.' if complete else
                'Some paths could not be fully measured and are kept; their sizes are lower bounds.')
        except Exception as exc:
            if self.scan_ui_available():
                self.query_one('#storage-status', Static).update(plain('Scan failed: '+str(exc)))
        finally:
            self.running = False
            if self.scan_ui_available():
                self.query_one('#storage-root', Input).disabled = False
                self.update_button()

    def show_progress(self, message):
        if self.scan_ui_available():
            self.query_one('#storage-status', Static).update(plain('Scanning: '+message))

    async def clean(self):
        if self.running or self.report is None or not self.query_one('#storage-confirm', Checkbox).value:
            return
        self.running = True
        self.cleaning = True
        self.update_button()
        self.query_one('#storage-root', Input).disabled = True
        try:
            selected = list(self.query_one('#storage-items', SelectionList).selected)
            result = await asyncio.to_thread(storage.cleanup, self.report, selected)
            failed = sum(r['status'] != 'removed' for r in result['items'])
            message = f'Removed {amount(result["removed_bytes"])} of files. {failed} item(s) kept or incomplete. Scan to refresh.'
            details = '\n'.join(r['path']+' · '+r['status']+' '+r.get('reason', '') for r in result['items'])
            self.query_one('#storage-origins', TextArea).load_text(plain(details))
            self.query_one('#storage-status', Static).update(plain(message))
            self.report = None
            self.query_one('#storage-items', SelectionList).clear_options()
        except Exception as exc:
            self.query_one('#storage-status', Static).update(plain('Cleanup stopped: '+str(exc)))
        finally:
            self.running = False
            self.cleaning = False
            self.query_one('#storage-root', Input).disabled = False
            self.query_one('#storage-confirm', Checkbox).value = False
            self.update_button()

    async def on_button_pressed(self, event):
        event.stop()
        if event.button.id == 'storage-close':
            self.action_close()
        elif event.button.id == 'storage-scan':
            self.run_worker(self.scan(), group='storage')
        elif event.button.id == 'storage-clean':
            self.run_worker(self.clean(), group='storage')
