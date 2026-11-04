"""Images a reply names show as thumbnails under it; a click opens them in the system's own viewer."""
import asyncio
import os

import pytest
from PIL import Image

from relay import console_images
from relay.console_images import ImagePreview, half_blocks, image_paths, open_externally


def picture(path, size, color=(200, 30, 30)):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new('RGB', size, color).save(path)
    return path


def test_reply_paths_relative_quoted_or_absolute_that_exist(tmp_path):
    root = tmp_path/'project'
    sprite = picture(root/'output'/'sprite_v7.png', (64, 64))
    spaced = picture(root/'planos'/'fluxo final.png', (8, 8))
    outside = picture(tmp_path/'elsewhere'/'ref.jpg', (8, 8))
    (root/'notes.txt').write_text('x')
    reply = (f'Fixed the eye in output/sprite_v7.png (see also `planos/fluxo final.png`), compared with {outside}; '
             'missing.png and notes.txt are not images to show. Again: output\\sprite_v7.png')
    assert image_paths(reply, root) == [spaced.resolve(), sprite.resolve(), outside.resolve()]
    assert image_paths(reply, root, limit=1) == [spaced.resolve()]
    assert image_paths('no images here', root) == [] and image_paths('output/sprite_v7.png', None) == []


def test_pixel_art_is_drawn_pixel_for_pixel_and_photos_are_scaled_down(tmp_path):
    two = Image.new('RGB', (2, 2))
    two.putdata([(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255)])
    cells = half_blocks(two, 64, 64)
    assert cells.plain == '▀▀'
    assert [(str(s.style.color.triplet.hex), str(s.style.bgcolor.triplet.hex)) for s in cells.spans] == [
        ('#ff0000', '#0000ff'), ('#00ff00', '#ffffff')]
    sprite = half_blocks(Image.new('RGB', (64, 64)), 64, 64).plain.split('\n')
    assert len(sprite) == 32 and {len(row) for row in sprite} == {64}  # 1:1, nothing lost
    photo = half_blocks(Image.new('RGB', (1200, 800)), 64, 64).plain.split('\n')
    assert max(len(row) for row in photo) <= 64


def test_only_existing_images_open_in_the_system_viewer(tmp_path, monkeypatch):
    opened = []
    if os.name == 'nt':
        monkeypatch.setattr(console_images.os, 'startfile', opened.append, raising=False)
    else:
        monkeypatch.setattr(console_images.subprocess, 'Popen', lambda argv, **k: opened.append(argv[-1]))
    image = picture(tmp_path/'a.png', (4, 4))
    open_externally(image)
    assert [str(p) for p in opened] == [str(image)]
    (tmp_path/'run.exe').write_text('x')
    for refused in [tmp_path/'run.exe', tmp_path/'missing.png']:
        with pytest.raises(ValueError):
            open_externally(refused)
    assert len(opened) == 1


def test_a_reply_shows_the_thumbnail_and_a_click_opens_it(tmp_path, monkeypatch):
    from relay.native_console import WorkConsole
    from test_native_console import FakeBackend
    project = tmp_path/'project'; project.mkdir()
    sprite = picture(project/'output'/'sprite_v7.png', (32, 32))
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))
    opened = []
    monkeypatch.setattr(console_images, 'open_externally', opened.append)
    async def scenario():
        app = WorkConsole(project, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=(120, 50)) as pilot:
            await app.workers.wait_for_complete()
            app.query_one('#output').write({'kind': 'execution-result', 'body': 'Fixed it: output/sprite_v7.png',
                                            'notice': '', 'details': ''})
            await pilot.pause()
            preview = app.query_one(ImagePreview)
            await app.workers.wait_for_complete(); await pilot.pause()
            assert '▀' in str(preview.render()) and '32×32' in str(preview.render())
            await pilot.click(ImagePreview); await pilot.pause()
            return preview.path
    assert asyncio.run(scenario()) == sprite.resolve() and opened == [sprite.resolve()]
