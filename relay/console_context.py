"""Project-bound path references and asynchronous composer completion."""
import asyncio
import json
import os
from pathlib import Path
import re

from rich.text import Text
from textual import work
from textual.widgets import Button, Input, OptionList, Select, Static, TextArea

from .console_output import plain


def context_path(root, value):
    root = Path(root).resolve()
    path = Path(value.replace('\\', '/'))
    path = path if path.is_absolute() else root/path
    if not path.resolve().is_relative_to(root) or '.git' in path.relative_to(root).parts:
        raise ValueError('Choose a path inside this project.')
    for item in (path, *path.parents):
        if item == root.parent:
            break
        if item.is_symlink() or getattr(item, 'is_junction', lambda: False)():
            raise ValueError('Linked paths cannot be selected as file context.')
    if not path.is_file() and not path.is_dir():
        raise ValueError('Selected context is unavailable: '+value)
    path = path.resolve()
    return path, path.relative_to(root).as_posix()


def path_token(text, cursor):
    row, column = cursor
    lines = text.split('\n')
    if row >= len(lines):
        return None
    line = lines[row]
    for match in re.finditer(r'(?<!\S)(?:@"(?:\\.|[^"\\])*"?|[^\s]+)', line):
        if not match.start() < column <= match.end():
            continue
        token = line[match.start():column]
        if not (token.startswith('@') or '/' in token or '\\' in token):
            return None
        if '://' in token or (row == 0 and match.start() == 0 and token.startswith('/') and not token.startswith('//')):
            return None  # Leading slash belongs to commands; use @ for absolute paths.
        value = token[1:] if token.startswith('@') else token
        if value.startswith('"'):
            try:
                value = json.loads(value if value.endswith('"') and len(value)>1 else value+'"')
            except ValueError:
                value=value[1:].removesuffix('"')  # Pasted Windows paths use literal backslashes.
        return row, match.start(), match.end(), value
    return None


def path_choices(root, token):
    value = token[3].replace('\\', '/')
    if value.endswith('/'):
        parent, prefix = value, ''
    else:
        parent, prefix = str(Path(value).parent), Path(value).name
    directory, relative = context_path(root, parent)
    if not directory.is_dir():
        return [], False
    rows, limited = [], False
    with os.scandir(directory) as entries:
        for index, entry in enumerate(entries):
            if index == 2000:
                limited = True
                break
            if entry.name == '.git' or not entry.name.casefold().startswith(prefix.casefold()):
                continue
            if entry.is_symlink() or getattr(Path(entry.path),'is_junction',lambda:False)():
                continue
            folder = entry.is_dir(follow_symlinks=False)
            if not folder and not entry.is_file(follow_symlinks=False):
                continue
            name = entry.name if relative == '.' else relative+'/'+entry.name
            rows.append((name, folder))
    rows.sort(key=lambda row: (not row[1], row[0].casefold(), row[0]))
    return rows[:50], limited or len(rows)>50


def context_prompt(selection, text=None):
    text = selection.prompt if text is None else text
    if not selection.context_paths:
        return text
    if Path(selection.context_root).resolve() != Path(selection.root).resolve():
        raise ValueError('Selected file context belongs to another project. Remove it or return to that project.')
    if len(selection.context_paths)>16:
        raise ValueError('Select at most 16 context paths.')
    paths=[]
    for value in selection.context_paths:
        path, relative = context_path(selection.root,value)
        paths.append({'path':relative,'kind':'directory' if path.is_dir() else 'file'})
    return text+'\n\nUser-selected paths in this project (references only; read current contents with tools):\n'+json.dumps(paths,ensure_ascii=False)


class FileContext:
    def context_state(self):
        if any(not self.query_one_optional('#'+name) for name in ('context-menu','prompt','root','mode')):
            return None
        prompt = self.query_one('#prompt',TextArea)
        if (prompt.disabled or self.focused not in (prompt,self.query_one('#context-menu'))
                or self.query_one('#mode',Select).value not in {'assistant','session'}
                or prompt.selection.start != prompt.selection.end):
            return None
        token = path_token(prompt.text,prompt.cursor_location)
        if token is None:
            return None
        try:
            root=str(Path(self.query_one('#root',Input).value).resolve())
        except (ValueError,OSError):
            return None
        if Path(root)!=Path(self.workspace_root):
            return None  # Apply the workspace change before choosing its files.
        return (root,prompt.text,prompt.cursor_location,token)

    def sync_context(self):
        if not self.query_one_optional('#context-box'):
            return
        state = self.context_state()
        if state == self.context_query:
            return
        self.context_query = state
        self.context_choices = []
        self.query_one('#context-box').display = False
        self.resize_composer()
        self.update_composer_hint()
        if state is not None and state[:2] != self.context_dismissed:
            self.load_context_choices(state)

    @work(exclusive=True,group='context-completion')
    async def load_context_choices(self,state):
        await asyncio.sleep(.12)
        try:
            rows, limited = await asyncio.to_thread(path_choices,state[0],state[3])
            hint = 'Tab opens folders · Enter selects · Esc closes'
            if limited:
                hint = 'Results limited; type a narrower path. '+hint
        except (ValueError,OSError):
            rows, hint = [], 'No accessible paths in this project.'
        if not self.is_running or self.context_state()!=state or self.context_dismissed==state[:2]:
            return
        self.context_choices = rows
        menu = self.query_one('#context-menu',OptionList)
        menu.set_options([plain(name)+('/' if folder else '') for name,folder in rows])
        if rows:
            menu.highlighted = 0
        self.query_one('#context-hint',Static).update(hint if rows else 'No matching paths. Type a project-relative path.')
        self.query_one('#context-box').display = True
        self.resize_composer()
        self.update_composer_hint()

    def context_key(self,key):
        if not self.query_one('#context-box').display:
            return False
        state = self.context_state()
        if state is None or state != self.context_query:
            self.sync_context()
            return False
        if key=='escape':
            self.context_dismissed = state[:2]
            self.query_one('#context-box').display = False
            self.resize_composer()
            self.update_composer_hint()
        elif not self.context_choices:
            return False
        elif key in {'up','down'}:
            getattr(self.query_one('#context-menu',OptionList),'action_cursor_'+key)()
        elif self.context_choices:
            self.accept_context(self.query_one('#context-menu',OptionList).highlighted or 0, descend=key=='tab')
        return True

    def add_context(self, root, name):
        """Attach a project path as a context chip: a reference the model may read, never the file's content.
        False (and a notice) when it is refused."""
        mode = self.query_one('#mode', Select).value
        if mode not in {'assistant', 'session'} or (self.busy and mode != 'assistant'):
            # Chips go with a conversation message; a flow refuses them, and a running task cannot take them.
            self.notify('File context goes with a conversation message: switch to the conversation, or wait '
                        'for this task to stop.'); return False
        try:
            context_path(root, name)
        except (ValueError, OSError) as exc:
            self.notify(str(exc)); return False
        if self.context_paths and Path(self.context_root) != Path(root):
            self.notify('Remove the context from the previous project first.'); return False
        if name not in self.context_paths:
            if len(self.context_paths) == 16:
                self.notify('Select at most 16 context paths.'); return False
            self.context_paths.append(name)
        self.context_root = root
        self.refresh_context_chips()
        return True

    def accept_context(self,index,*,descend=False):
        state=self.context_state()
        if state is None or state!=self.context_query or not 0<=index<len(self.context_choices):
            return
        name,folder=self.context_choices[index]
        try:
            context_path(state[0],name)  # Recheck before attaching it.
        except (ValueError,OSError) as exc:
            self.notify(str(exc)); return
        prompt=self.query_one('#prompt',TextArea)
        row,start,end,_=state[3]
        if folder and descend:
            value=name+'/'
            replacement='@'+(json.dumps(value,ensure_ascii=False)[:-1] if any(c.isspace() or c=='"' for c in value) else value)
        else:
            if not self.add_context(state[0],name):
                return
            replacement=''
        prompt.replace(replacement,(row,start),(row,end))
        prompt.move_cursor((row,start+len(replacement)))
        self.query_one('#context-box').display=False
        self.resize_composer()
        self.update_composer_hint()
        self.context_query=None
        prompt.focus()
        self.sync_context()

    @work(group='context-chips')
    async def refresh_context_chips(self):
        async with self.context_chip_lock:
            if not self.query_one_optional('#context-chips'):
                return
            state=(self.context_root,tuple(self.context_paths))
            if self.context_rendered==state:
                return
            row=self.query_one('#context-chips')
            await row.remove_children()
            if not row.is_mounted:
                return
            row.display=bool(state[1])
            for index,path in enumerate(state[1]):
                button=Button(Text(plain(path)+' ×'),id=f'context-remove-{index}')
                button.context_path=path
                button.tooltip=Text(plain(str(Path(state[0])/path))+' · reference only')
                await row.mount(button)
            self.context_rendered=state

    def remove_context(self,button):
        if button.context_path in self.context_paths:
            self.context_paths.remove(button.context_path)
            self.refresh_context_chips()
        self.query_one('#prompt',TextArea).focus()

    def restore_context(self,selection):
        self.restore_attachments(selection)
        self.context_root=selection.context_root
        self.context_paths=list(selection.context_paths)
        self.context_query=self.context_dismissed=None
        self.refresh_context_chips()

    def clear_context(self):
        self.clear_attachments()
        self.context_paths=[]
        self.context_root=''
        self.refresh_context_chips()
