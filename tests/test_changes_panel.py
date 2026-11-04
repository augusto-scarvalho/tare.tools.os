import asyncio

import pytest
from textual.app import App
from textual.widgets import Button, Select, Static

from relay.changes_panel import ChangesPanel
from test_native_review import session


def content(widget):
    return str(getattr(widget,'content',widget.render()))


@pytest.mark.parametrize('size',[(80,24),(130,44)])
def test_panel_renders_real_edit_and_reports_external_change(session,size):
    repo, root, tools, events, edit, check, report = session
    edit(); check()
    calls=[]
    def read(custody,**kwargs):
        assert custody==root and kwargs['project']==repo
        calls.append(kwargs)
        return report()

    async def scenario():
        app=App()
        copies=[]; app.copy_to_clipboard=copies.append
        async with app.run_test(size=size) as pilot:
            comments=[]
            panel=ChangesPanel(root,repo,reader=read,commenter=comments.append)
            app.push_screen(panel)
            for _ in range(40):
                await pilot.pause(.05)
                if '+value = 2' in content(panel.query_one('#changes-diff',Static)): break
            assert '+value = 2' in content(panel.query_one('#changes-diff',Static))
            assert 'recorded PASS' in content(panel.query_one('#changes-summary',Static))
            assert panel.query_one('#changes-close').region.bottom <= size[1]
            assert any(c.get('include_diffs') is False for c in calls)
            assert any(c.get('max_diff_bytes')==256*1024 for c in calls)
            reads=len(calls)
            panel.query_one('#changes-copy',Button).press(); await pilot.pause()
            # Copy the displayed text; terminal rendering normalizes CRLF.
            assert copies == [report()['edits'][0]['diff'].replace('\r\n','\n')]
            assert len(calls)==reads  # Copy the verified view without a new owner operation.
            panel.query_one('#changes-comment',Button).press(); await pilot.pause()
            assert len(comments)==1 and comments[0]['body']==copies[0]
            assert comments[0]['reference']['effect_id']==panel.diff_effect_id
            assert comments[0]['reference']['session']==str(root)
            assert len(calls)==reads  # Comment on the captured diff, not a later file version.
            (repo/'a.py').write_text('user changes\n')
            await pilot.press('ctrl+r')
            for _ in range(40):
                await pilot.pause(.05)
                if 'STALE' in content(panel.query_one('#changes-summary',Static)): break
            assert 'STALE' in content(panel.query_one('#changes-summary',Static))
            await pilot.press('escape'); await pilot.pause()
            assert app.screen is not panel
            assert (repo/'a.py').read_text()=='user changes\n'
    asyncio.run(scenario())
