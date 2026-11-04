"""Images in the conversation: a terminal thumbnail, and the system's own viewer one click away.

A reply that names image files (relative to the project, or absolute) shows a half-block thumbnail of each
under the message; clicking it, or Enter on it, opens the file in the app the operating system uses for
images. Small images (sprites) are drawn pixel for pixel, larger ones scaled down.
"""
import os
from pathlib import Path
import re
import subprocess
import sys

from rich.style import Style
from rich.text import Text
from textual.widgets import Static

SUFFIXES = ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp')
MAX_BYTES, MAX_PIXELS = 32 * 1024 * 1024, 16_000_000
_TICKED = re.compile(r'`([^`\n]+?\.(?:png|jpe?g|gif|webp|bmp))`', re.IGNORECASE)
_BARE = re.compile(r'(?:[A-Za-z]:)?[\w./\\-]+\.(?:png|jpe?g|gif|webp|bmp)\b', re.IGNORECASE)


def half_blocks(picture, width, height, background='#20252b'):
    """A PIL image as rows of '▀' (upper pixel as colour, lower as background): two pixels per cell."""
    from PIL import Image
    picture = picture.convert('RGBA')
    picture = Image.alpha_composite(Image.new('RGBA', picture.size, background), picture).convert('RGB')
    if picture.width > width or picture.height > height:
        # Pixel art stays crisp; photographs are averaged.
        small = picture.width <= 256 and picture.height <= 256
        picture.thumbnail((width, height), Image.NEAREST if small else Image.LANCZOS)
    pixels, result = picture.load(), Text(no_wrap=True)
    for y in range(0, picture.height, 2):
        for x in range(picture.width):
            top = '#%02x%02x%02x' % pixels[x, y]
            bottom = '#%02x%02x%02x' % pixels[x, min(y + 1, picture.height - 1)]
            result.append('▀', Style(color=top, bgcolor=bottom))
        if y + 2 < picture.height:
            result.append('\n')
    return result


def image_paths(text, root, limit=4):
    """Existing image files a reply names: `quoted` (spaces allowed) or bare, relative to root or absolute."""
    found = []
    for candidate in [*_TICKED.findall(text or ''), *_BARE.findall(text or '')]:
        path = Path(candidate.strip())
        if not path.is_absolute():
            if not root:
                continue
            path = Path(root)/candidate.strip().replace('\\', '/')
        try:
            path = path.resolve()
            usable = path.is_file() and path.suffix.lower() in SUFFIXES and path.stat().st_size <= MAX_BYTES
        except (OSError, ValueError):
            continue
        if usable and all(os.path.normcase(str(path)) != os.path.normcase(str(seen)) for seen in found):
            found.append(path)
        if len(found) == limit:
            break
    return found


def thumbnail(path, width=64, height=64):
    from PIL import Image
    with Image.open(path) as picture:
        if picture.width * picture.height > MAX_PIXELS:
            raise ValueError('image too large to preview')
        size = picture.size
        return size, half_blocks(picture, width, height)


def open_externally(path):
    """Open an image file in the operating system's default app for it."""
    path = Path(path)
    if path.suffix.lower() not in SUFFIXES or not path.is_file():
        raise ValueError('Not an image file: ' + str(path))
    if os.name == 'nt':
        os.startfile(str(path))  # noqa: S606 - the file association the user chose
    else:
        subprocess.Popen(['open' if sys.platform == 'darwin' else 'xdg-open', str(path)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class ImagePreview(Static, can_focus=True):
    """A reply's image: thumbnail and name; click or Enter opens it in the system viewer."""
    DEFAULT_CSS = '''
    ImagePreview { height: auto; width: auto; margin: 0 1 1 1; padding: 0 1; }
    ImagePreview:hover, ImagePreview:focus { background: $panel; }
    '''
    BINDINGS = [('enter', 'open', 'Open image')]

    def __init__(self, path):
        super().__init__(Text(Path(path).name + ' · loading preview…', style='dim'), classes='image-preview')
        self.path = Path(path)
        self.tooltip = f'Open {self.path} in the system image viewer'

    def on_mount(self):
        self.run_worker(self.load, thread=True, group='image-preview')

    def load(self):
        try:
            (width, height), picture = thumbnail(self.path)
            caption = Text(f'\n{self.path.name} · {width}×{height} · click to open', style='dim')
            body = Text.assemble(picture, caption)
        except Exception as exc:  # a broken or unsupported file must not break the conversation
            body = Text(f'{self.path.name} · no preview ({type(exc).__name__}) · click to open', style='dim')
        self.app.call_from_thread(self.update, body)

    def on_click(self, event):
        event.stop()
        self.action_open()

    def action_open(self):
        try:
            open_externally(self.path)
        except (OSError, ValueError) as exc:
            self.app.notify(f'Could not open {self.path.name}: {exc}', severity='error')
