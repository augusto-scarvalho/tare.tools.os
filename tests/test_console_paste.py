"""Large-draft composer behavior through Textual's real paste event path."""
import asyncio

from textual import events
from textual.widgets import Button, Static, TextArea

from relay.native_console import WorkConsole
from test_native_console import FakeBackend


def _visible_line(widget):
    return "".join(segment.text for segment in widget.render_line(0))


async def _hint_after_paste(pilot, hint, expected):
    # Paste, TextArea.Changed and the Static render run in separate queued passes.
    # Wait for the user-visible result; retain the assertions below on its content.
    for _ in range(40):
        await pilot.pause(.025)
        if expected in _visible_line(hint):
            break
    return _visible_line(hint)


async def _height_after_change(pilot, prompt, expected):
    # Text insertion, Changed and layout are separate queued passes.
    for _ in range(40):
        await pilot.pause(.025)
        if prompt.region.height == expected:
            break
    return prompt.region.height


def test_multiline_paste_grows_caps_shrinks_and_never_submits(tmp_path, monkeypatch):
    monkeypatch.setenv("TARE_STORAGE_ROOT", str(tmp_path/"state"))
    draft = "\n\n".join(f"Paragraph {number}: " + "wrapped words " * 8 for number in range(6))

    async def scenario():
        app = WorkConsole(tmp_path, backend=FakeBackend(), initial_mode="assistant")
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            prompt = app.query_one("#prompt", TextArea)
            prompt.focus()
            app.post_message(events.Paste(draft))
            height = await _height_after_change(pilot, prompt, 8)

            assert prompt.text == draft
            assert height == 8
            assert not app.backend.launches
            send = app.query_one("#start", Button)
            assert send.display and app.screen.region.contains_region(send.region)
            assert "Send" in _visible_line(send)

            prompt.clear()
            height = await _height_after_change(pilot, prompt, 1)
            assert prompt.text == ""
            assert height == 1
            assert not app.backend.launches
    asyncio.run(scenario())


def test_large_paste_indicator_and_visible_over_limit_warning(tmp_path, monkeypatch):
    monkeypatch.setenv("TARE_STORAGE_ROOT", str(tmp_path/"state"))
    large = "\n".join(["x" * 1800] * 10)

    async def scenario():
        app = WorkConsole(tmp_path, backend=FakeBackend(), initial_mode="assistant")
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            prompt = app.query_one("#prompt", TextArea)
            prompt.focus()
            app.post_message(events.Paste("y" * 1200 + "\nsecond line"))
            visible = await _hint_after_paste(pilot, app.query_one("#composer-hint", Static), "2 lines")
            assert "B" in visible and "2 lines" in visible

            prompt.clear(); await pilot.pause()
            app.post_message(events.Paste(large))
            hint = app.query_one("#composer-hint", Static)
            visible = await _hint_after_paste(pilot, hint, "18009 B")
            assert prompt.text == large
            assert not app.backend.launches
            assert visible.startswith("Over 16 KiB")
            assert "18009 B" in visible
            assert app.screen.region.contains_region(hint.region)
            send = app.query_one("#start", Button)
            assert send.display and app.screen.region.contains_region(send.region)
            assert "Send" in _visible_line(send)
    asyncio.run(scenario())
