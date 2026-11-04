"""Snapshot selection and bounded previews for the conversation composer."""
import asyncio
import hashlib
import os
from pathlib import Path

from rich.text import Text
from textual import work
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Input, Label, OptionList, Static, TextArea

from .console_output import plain
from .console_panels import SupportPanel


def draft_store(root):
    from .native_installation import storage_root
    from compute_plane.native_attachments import NativeAttachmentStore
    storage = storage_root().resolve()
    root = Path(root).resolve()
    if storage.is_relative_to(root):
        raise ValueError('Attachment history must be outside the project.')
    identity = hashlib.sha256(os.path.normcase(str(root)).encode()).hexdigest()
    return NativeAttachmentStore(storage/'console'/'attachments'/identity/'blobs')


def selected_content(selection):
    from .native_attachments import references
    refs = references(selection.attachments)
    if refs and (Path(selection.attachment_project).resolve() != Path(selection.root).resolve()
                 or not selection.attachment_root):
        raise ValueError('These attachments belong to another project. Return there or remove them.')
    return refs


def input_file(selection):
    from .native_installation import storage_root
    from .native_attachments import write_input
    storage = storage_root().resolve()
    if storage.is_relative_to(Path(selection.root).resolve()):
        raise ValueError('Attachment history must be outside the project.')
    return write_input(storage/'console'/'attachment-inputs', selection.attachment_root,
                       selected_content(selection))


def check_image_profile(selection, saved=None):
    from compute_plane.native_attachments import IMAGE_TYPES
    refs=selected_content(selection)
    images={ref.media_type for ref in refs if ref.media_type in IMAGE_TYPES}
    if not images:return
    if saved is None:
        from .native_catalog import select
        profile=select(None if selection.profile.strip() else 'auto' if selection.auto_execution else selection.deployment,
                       Path(selection.profile).resolve() if selection.profile.strip() else None,
                       role=selection.role)
    else:profile=saved['deployment']
    if not images.issubset(profile.get('native_image_media_types', [])):
        raise ValueError('This model cannot see images, and a message queued during a turn goes to it as is. '
                         'Send the image after the turn ends: it is then described for the model.')


def directory_entries(path):
    from compute_plane.native_journal import regular
    path = regular(path).resolve()
    rows, limited = [], False
    with os.scandir(path) as entries:
        for index, entry in enumerate(entries):
            if index == 2000:
                limited = True
                break
            target = Path(entry.path)
            if entry.name == '.git' or entry.is_symlink() or getattr(target, 'is_junction', lambda:False)():
                continue
            folder = entry.is_dir(follow_symlinks=False)
            if folder or entry.is_file(follow_symlinks=False):
                rows.append((target, folder))
    rows.sort(key=lambda row:(not row[1],row[0].name.casefold(),row[0].name))
    return rows[:200], limited or len(rows)>200


def preview_error(error):
    messages = {
        'SOURCE_UNAVAILABLE': 'Cannot read this file. Check its path and access permissions.',
        'SNAPSHOT_UNAVAILABLE': 'The saved copy is unavailable. Remove it and attach the file again.',
        'SNAPSHOT_CHANGED': 'The saved copy has changed. Remove it and attach the file again.',
        'SOURCE_CHANGED': 'The file changed while it was being read. Try attaching it again.',
        'FILE_SIZE_EXCEEDED': 'This file is too large. Choose a file no larger than 16 MiB.',
        'IMAGE_DIMENSIONS_EXCEEDED': 'This image is too large. Resize it to at most 16 million pixels.',
        'ANIMATED_IMAGE_UNSUPPORTED': 'Animated images are not supported yet. Attach a still image.',
        'INVALID_IMAGE': 'This image could not be decoded. Export it as PNG or JPEG and try again.',
        'IMAGE_TYPE_MISMATCH': 'The image format does not match its type. Export a new PNG or JPEG.',
        'IMAGE_DECODER_UNAVAILABLE': 'Image support is missing from this installation. Attach text or install the attachment dependencies.',
        'DOCUMENT_REQUIRES_UTF8': 'This document is not UTF-8 text. Save a UTF-8 copy and attach it.',
        'BINARY_DOCUMENT_UNSUPPORTED': 'This is a binary file. Attach a UTF-8 text document or a supported image.',
        'DOCUMENT_FORMAT_UNSUPPORTED': 'This document format is not supported yet. Attach an image or export it as UTF-8 text.',
        'UNSUPPORTED_MEDIA_TYPE': 'This file type is not supported. Attach UTF-8 text, PNG, JPEG, GIF or WebP.',
        'REGULAR_FILE_REQUIRED': 'Choose a regular file to attach.',
    }
    return messages.get(str(error).removeprefix('ATTACHMENT_'), plain(str(error)))


def preview_content(store, ref, *, width=56, height=28):
    import io
    from compute_plane.native_attachments import inspect_content
    raw = store.load(ref)
    info = inspect_content(ref, raw)
    description = f'{ref.media_type} · {ref.size_bytes:,} bytes'
    if info.text is not None:
        text = plain(info.text[:12000])
        if len(info.text)>12000:
            text += '\n\n[Preview shortened. The full snapshot will be sent.]'
        return description, Text(text)
    from PIL import Image
    from .console_images import half_blocks
    with Image.open(io.BytesIO(raw)) as original:
        result = half_blocks(original, width, height)
    return f'{ref.media_type.split("/")[-1].upper()} · {info.width}×{info.height} · {ref.size_bytes/1024:.1f} KB', result


class AttachmentPicker(SupportPanel):
    BINDINGS = [('escape','close','Cancel')]
    DEFAULT_CSS = '''
    #attachment-location-row { height: 1; margin: 0 0 1 0; }
    #attachment-location { width: 1fr; height: 1; border: none; padding: 0 1; background: $background; }
    #attachment-up { min-width: 8; width: 8; height: 1; min-height: 1; margin-right: 1;
                     border: none; background: transparent; color: $primary; }
    #attachment-body { height: 1fr; }
    #attachment-files { width: 32%; margin-right: 2; border: none; padding: 0 1; background: $background; }
    #attachment-preview-pane { width: 1fr; }
    #attachment-preview-name { height: auto; text-style: bold; }
    #attachment-preview-info { height: auto; color: $text-muted; margin-bottom: 1; }
    #attachment-preview { height: auto; }
    #attachment-picker-status { height: auto; max-height: 2; color: $text-muted; }
    #attachment-up:focus { background: $primary; color: $background; }
    #attachment-location:focus { background: $primary 12%; }
    #attachment-confirm:focus { text-style: bold underline; }
    AttachmentPicker.narrow #attachment-body { margin: 0; }
    AttachmentPicker.narrow #attachment-picker-status { max-height: 1; }
    '''

    def __init__(self, root, store, *, ref=None, initial_path=None):
        super().__init__()
        self.directory, self.store = Path(root), store
        self.rows, self.selected, self.generation = [], None, 0
        self.initial_ref = ref
        self.initial_path = initial_path

    def compose(self):
        with Vertical(id='attachment-dialog', classes='support-body'):
            with Horizontal(classes='panel-header'):
                yield Static('Attachment preview' if self.initial_ref else 'Attach a file',id='attachment-title',classes='panel-title')
                yield Button('Back · Esc',id='attachment-cancel')
            with Horizontal(id='attachment-location-row'):
                yield Button('Up',id='attachment-up')
                yield Input(str(self.directory),id='attachment-location',placeholder='Folder or file path')
            with Horizontal(id='attachment-body'):
                yield OptionList(id='attachment-files',markup=False)
                with VerticalScroll(id='attachment-preview-pane'):
                    yield Static('',id='attachment-preview-name',markup=False)
                    yield Static('',id='attachment-preview-info',markup=False)
                    yield Static('Choose a file and press Enter to preview.',id='attachment-preview',markup=False)
            yield Static('Enter opens folders or previews files · Esc closes',id='attachment-picker-status',markup=False)
            with Horizontal(id='attachment-actions', classes='panel-actions'):
                yield Button('Attach file',id='attachment-confirm',classes='panel-primary',disabled=True)

    def on_mount(self):
        if self.initial_ref:
            self.query_one('#attachment-location-row').display=False
            self.query_one('#attachment-files').display=False
            self.query_one('#attachment-confirm').display=False
            self.query_one('#attachment-actions').display=False
            self.show_snapshot(ref=self.initial_ref)
        elif self.initial_path:
            self.open_path(self.initial_path)
        else:
            self.browse(self.directory)

    def action_close(self):
        self.generation += 1
        self.dismiss(None)

    def on_resize(self,event):
        self.set_class(event.size.width<100 or event.size.height<30,'narrow')
        if self.selected:
            ref=self.selected
            self.call_after_refresh(lambda:self.show_snapshot(ref=ref) if self.is_mounted else None)

    def browse(self,path):
        self.generation += 1
        generation = self.generation
        self.selected=None
        self.query_one('#attachment-confirm',Button).disabled=True
        expected=str(path)
        self.query_one('#attachment-location',Input).value=expected
        self.load_directory(path,generation,expected,self.app.focused)

    @work(group='attachment-browser')
    async def load_directory(self,path,generation,expected,focus):
        try:
            rows, limited = await asyncio.to_thread(directory_entries,path)
            if not self.is_mounted or generation!=self.generation: return
            self.directory=Path(path).resolve();self.rows=rows
            menu=self.query_one('#attachment-files',OptionList)
            menu.set_options([plain(p.name)+('/' if folder else '') for p,folder in rows])
            if rows: menu.highlighted=0
            self.query_one('#attachment-picker-status',Static).update(
                'List limited; enter a more specific path.' if limited else 'Enter opens folders or previews files · Esc closes')
            field=self.query_one('#attachment-location',Input)
            if field.value==expected:
                field.value=str(self.directory)
                if self.app.focused is focus:menu.focus()
        except (ValueError,OSError) as exc:
            if self.is_mounted and generation==self.generation:
                self.query_one('#attachment-picker-status',Static).update(plain(str(exc)))

    @work(group='attachment-preview')
    async def show_snapshot(self,path=None,*,ref=None):
        self.generation += 1
        generation=self.generation
        self.selected=None
        self.query_one('#attachment-confirm',Button).disabled=True
        self.query_one('#attachment-picker-status',Static).update('Saving a copy…')
        self.query_one('#attachment-preview-name',Static).update(plain(ref.name if ref else Path(path).name))
        self.query_one('#attachment-preview-info',Static).update('')
        self.query_one('#attachment-preview',Static).update('Preparing preview…')
        try:
            if ref is None: ref=await asyncio.to_thread(self.store.import_file,path)
            pane=self.query_one('#attachment-preview-pane')
            width=min(56,max(8,pane.content_size.width))
            height=min(28,max(4,2*(pane.content_size.height-3)))
            description,preview=await asyncio.to_thread(preview_content,self.store,ref,width=width,height=height)
            if not self.is_mounted or generation!=self.generation:return
            self.selected=ref
            self.query_one('#attachment-preview-name',Static).update(Text(plain(ref.name)))
            self.query_one('#attachment-preview-info',Static).update(description)
            self.query_one('#attachment-preview',Static).update(preview)
            self.query_one('#attachment-confirm',Button).disabled=False
            self.query_one('#attachment-picker-status',Static).update('Original file changes will not alter this attached copy.')
        except Exception as exc:
            if self.is_mounted and generation==self.generation:
                self.query_one('#attachment-preview',Static).update(preview_error(exc))
                self.query_one('#attachment-picker-status',Static).update('Not attached · choose another file or try again')

    def open_path(self,path):
        path=Path(path).expanduser()
        if not path.is_absolute():path=self.directory/path
        if path.is_dir():self.browse(path)
        else:self.show_snapshot(path)

    def on_input_submitted(self,event):
        if event.input.id=='attachment-location':
            event.stop();self.open_path(event.value)

    def on_option_list_option_selected(self,event):
        if event.option_list.id=='attachment-files' and 0<=event.option_index<len(self.rows):
            event.stop();self.open_path(self.rows[event.option_index][0])

    def on_button_pressed(self,event):
        event.stop()
        if event.button.id=='attachment-cancel':self.action_close()
        elif event.button.id=='attachment-up':self.browse(self.directory.parent)
        elif event.button.id=='attachment-confirm' and self.selected:
            self.dismiss(self.selected)


class ComposerAttachments:
    def action_attach(self, path=None):
        try:
            from .native_attachments import require_support
            require_support()
            root=self.prepare_workspace()
            if self.selection().mode not in {'assistant','session'}:
                raise ValueError('Attachments are available in direct conversations.')
            if len(self.attachments)>=16:
                raise ValueError('Select at most 16 attachments.')
            store=draft_store(root)
            def chosen(ref):
                if ref is None:return
                if root!=self.workspace_root or Path(self.selection().root).resolve()!=Path(root):
                    self.notify('Workspace changed; the attachment was not selected.');return
                from .native_attachments import records
                row=records((ref,))[0]
                if row not in self.attachments:self.attachments.append(row)
                self.attachment_project=root;self.attachment_root=str(store.root)
                self.refresh_attachment_chips()
                self.query_one('#prompt',TextArea).focus()
            self.push_screen(AttachmentPicker(root,store,initial_path=path),chosen)
        except Exception as exc:self.notify(str(exc),severity='error')

    @work(group='attachment-chips')
    async def refresh_attachment_chips(self):
        async with self.attachment_chip_lock:
            if not self.query_one_optional('#attachment-chips'):return
            row=self.query_one('#attachment-chips')
            await row.remove_children()
            if not row.is_mounted:return
            row.display=bool(self.attachments)
            for index,ref in enumerate(self.attachments):
                button=Button(Text(plain(ref['name'])),id=f'attachment-preview-{index}')
                button.attachment_ref=dict(ref)
                button.tooltip='Preview saved attachment'
                remove=Button('×',id=f'attachment-remove-{index}')
                remove.attachment_ref=dict(ref)
                remove.tooltip='Remove '+plain(ref['name'])
                await row.mount(button,remove)

    def attachment_button(self,button):
        ref=button.attachment_ref
        if ref not in self.attachments:return
        if button.id.startswith('attachment-remove-'):
            self.attachments.remove(ref);self.refresh_attachment_chips()
            self.query_one('#prompt',TextArea).focus()
        else:
            from .native_attachments import references
            from compute_plane.native_attachments import NativeAttachmentStore
            self.push_screen(AttachmentPicker(self.workspace_root,NativeAttachmentStore(self.attachment_root),
                                              ref=references([ref])[0]))

    def restore_attachments(self,selection):
        self.attachments=[dict(ref) for ref in selection.attachments]
        self.attachment_root=selection.attachment_root
        self.attachment_project=selection.attachment_project
        self.refresh_attachment_chips()

    def clear_attachments(self):
        self.attachments=[];self.attachment_project=self.attachment_root=''
        self.refresh_attachment_chips()
