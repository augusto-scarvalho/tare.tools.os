"""Sequence owner reports preserve literal paths, IDs and row boundaries."""
import asyncio

from textual.widgets import Input

from relay.native_console import WorkConsole
from test_native_console import FakeBackend


def test_review_preserves_paths_and_each_work_row(tmp_path):
    path = r'C:\projects\_archives\flow_[10]\sequence'
    view = {'sequence': path, 'status': 'BLOCKED', 'completed': 1, 'total': 2,
        'workers': 1, 'workflow_revision': 1, 'reason': None,
        'items': [{'work_id': 'one_[0]', 'status': 'PENDING', 'attempts': []},
                  {'work_id': 'two_[1]', 'status': 'SETTLED', 'attempts': [{}]}],
        'frontier': {'status': 'BLOCKED', 'blocked': {'one_[0]': 'Requires external (NOT_DONE)'}}}
    class Backend(FakeBackend):
        def sequence_view(self, selection): return view
    async def scenario():
        app = WorkConsole(tmp_path, backend=Backend(), initial_mode='sequence')
        async with app.run_test(size=(150, 52)) as pilot:
            await app.workers.wait_for_complete()
            app.query_one('#sequence', Input).value = path
            await pilot.click('#review')
            await app.workers.wait_for_complete()
            await pilot.pause()
            lines = [''.join(s.text for s in widget.render_line(y)).strip()
                     for widget in app.query('#output .message') for y in range(widget.region.height)]
            assert 'Sequence: '+path in lines
            assert 'one_[0] · PENDING · 0 dispatches' in lines
            assert 'two_[1] · SETTLED · 1 dispatches' in lines
            assert 'one_[0]: Requires external (NOT_DONE)' in lines
            assert not app.backend.launches
    asyncio.run(scenario())
