import asyncio

import pytest
from textual.widgets import Input, OptionList, Select, TextArea

from relay import console_entry
from relay.native_console import WorkConsole
from test_native_console import FakeBackend

NAMES = [name for name, _, _ in console_entry.commands_for('conversation')]


class RecordingBackend(FakeBackend):
    def __init__(self):
        super().__init__();self.proposals=[]
    def propose_entry(self,*args,**kwargs):
        self.proposals.append(args);raise AssertionError('Completion must not interpret.')


def run(tmp_path,scenario,*,size=(120,40),mode='conversation'):
    """Every scenario must finish with zero launches, interpretations and demand checkpoints."""
    async def main():
        backend=RecordingBackend();app=WorkConsole(tmp_path,backend=backend,initial_mode=mode)
        checkpoints=[];app.checkpoint=lambda *a,**k:checkpoints.append(a)
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            await settle(pilot)
            await scenario(app,pilot,app.query_one('#prompt',TextArea))
            assert not backend.launches and not backend.proposals and not checkpoints
            assert app.entry_proposal is None
    asyncio.run(main())


def shown(app):
    if not app.query_one('#slash-box').display:
        return []
    return [str(option.prompt).split()[0] for option in app.query_one('#slash-menu',OptionList).options]


async def settle(pilot):
    # Drain widget events, their bubbled app handlers, and the resulting layout.
    await pilot.pause()
    await pilot.pause()
    await pilot.pause()


async def edit(pilot,prompt,text,cursor):
    prompt.focus();prompt.load_text(text);prompt.move_cursor(cursor);await settle(pilot)


def test_help_and_completion_share_the_accepted_commands():
    assert '/session TASK, /explore QUESTION, /plan DEMAND, /research QUESTION URL, /work WORK-ID.' in console_entry.HELP
    for name,usage,description in console_entry.COMMANDS:
        assert name in console_entry.HELP and description
        if usage and name!='/attach':
            assert console_entry.explicit(name+' X')['flow']==name[1:]
        else:
            with pytest.raises(ValueError):console_entry.explicit(name+' X')


def test_filter_and_enter_accept_only_edit_the_token(tmp_path):
    async def scenario(app,pilot,prompt):
        prompt.focus();await settle(pilot)
        await pilot.press('/');await settle(pilot)
        assert shown(app)==NAMES
        await pilot.press('P','l');await settle(pilot)
        assert shown(app)==['/plan']
        await pilot.press('enter');await settle(pilot)
        assert prompt.text=='/plan ' and prompt.cursor_location==(0,6) and shown(app)==[]
        assert app.focused is prompt and app.query_one('#mode',Select).value=='conversation'
        await pilot.press('x','shift+enter');await settle(pilot)
        assert prompt.text=='/plan x\n'  # Explicit newline does not submit after completing a command.
    run(tmp_path,scenario)


def test_arrows_and_tab_keep_arguments_and_later_lines(tmp_path):
    async def scenario(app,pilot,prompt):
        await edit(pilot,prompt,'/pl keep  these args\nsecond /line',(0,3))
        assert shown(app)==['/plan']
        await pilot.press('tab');await settle(pilot)
        assert prompt.text=='/plan keep  these args\nsecond /line' and prompt.cursor_location==(0,6)
        assert app.focused is prompt and shown(app)==[]
        await edit(pilot,prompt,'/\nmore',(0,1))
        menu=app.query_one('#slash-menu',OptionList)
        await pilot.press('down','down');await settle(pilot)
        assert menu.highlighted==2 and prompt.cursor_location==(0,1)
        await pilot.press('up');await settle(pilot)
        assert menu.highlighted==1
        await pilot.press('enter');await settle(pilot)
        assert prompt.text=='/explore \nmore' and prompt.cursor_location==(0,9)
        await pilot.press('tab');await settle(pilot)
        assert app.focused is not prompt  # Closed menu leaves Tab to focus navigation.
    run(tmp_path,scenario)


def test_escape_stays_dismissed_until_the_token_is_edited(tmp_path):
    async def scenario(app,pilot,prompt):
        prompt.focus();await settle(pilot)
        await pilot.press('/');await settle(pilot)
        assert shown(app)==NAMES
        await pilot.press('escape');await settle(pilot)
        assert shown(app)==[] and prompt.text=='/' and app.focused is prompt
        await pilot.press('left','right');app.sync_slash();await settle(pilot)
        assert shown(app)==[]
        await pilot.press('p');await settle(pilot)
        assert shown(app)==['/plan']
        await pilot.press('backspace');await settle(pilot)
        assert prompt.text=='/' and shown(app)==NAMES
    run(tmp_path,scenario)


def test_completion_stays_closed_outside_the_leading_token(tmp_path):
    async def scenario(app,pilot,prompt):
        for text,cursor in [('see /pl',(0,7)),('x\n/pl',(1,3)),('https://example.org/a',(0,5)),
                            ('/path/to/file broke',(0,5)),('/plan args',(0,10))]:
            await edit(pilot,prompt,text,cursor)
            assert shown(app)==[],text
        prompt.move_cursor((0,3));await settle(pilot)
        assert shown(app)==['/plan']
        await pilot.press('end');await settle(pilot)
        assert shown(app)==[]
        prompt.move_cursor((0,2));prompt.move_cursor((0,8),select=True);await settle(pilot)
        assert shown(app)==[]  # A selection reaching arguments is not replaced.
        await edit(pilot,prompt,'/pl',(0,3))
        assert shown(app)==['/plan']
        app.query_one('#root',Input).focus();await settle(pilot)
        assert shown(app)==[]
        await edit(pilot,prompt,'/pl',(0,3))
        prompt.load_text('restored draft');await settle(pilot)
        assert shown(app)==[]
        await edit(pilot,prompt,'/pl',(0,3))
        app.controls(True);await settle(pilot)
        assert shown(app)==[] and app.slash_key('enter') is False
        app.controls(False);await edit(pilot,prompt,'/pl',(0,3))
        app.query_one('#mode',Select).value='session';await settle(pilot)
        assert shown(app)==[]
        prompt.focus();await settle(pilot)
        await pilot.press('shift+enter');await settle(pilot)
        assert prompt.text=='/pl\n'
        app.query_one('#message',TextArea).focus();await settle(pilot)
        await pilot.press('/');await settle(pilot)
        assert shown(app)==[]
    run(tmp_path,scenario)


def test_ctrl_enter_keeps_explicit_send_for_help_and_status(tmp_path,monkeypatch):
    from relay import model_choices
    monkeypatch.setattr(model_choices,'help_summary',lambda:'Local model choices (offline).')
    async def scenario(app,pilot,prompt):
        said=[];app.say=said.append
        prompt.focus();await settle(pilot)
        await pilot.press('/','h','e');await settle(pilot)
        assert shown(app)==['/help']
        await pilot.press('l','p');await settle(pilot)
        await pilot.press('ctrl+enter');await settle(pilot)
        assert said==[console_entry.help_text()]
        assert said[0].startswith('Local model choices (offline).') and said[0].endswith(console_entry.HELP)
        await edit(pilot,prompt,'/status',(0,7))
        await pilot.press('ctrl+enter');await settle(pilot)
        assert said[-1].startswith('No execution')
    run(tmp_path,scenario)


def test_mouse_selection_completes_without_sending(tmp_path):
    async def scenario(app,pilot,prompt):
        prompt.focus();await settle(pilot)
        await pilot.press('/');await settle(pilot);await settle(pilot)
        menu = app.query_one('#slash-menu', OptionList)
        row = next(i for i in range(menu.size.height) if '/plan' in menu.render_line(i).text)
        await pilot.click('#slash-menu',offset=(4,row));await settle(pilot)
        assert prompt.text=='/plan ' and app.focused is prompt and shown(app)==[]
    run(tmp_path,scenario)


@pytest.mark.parametrize('size',[(140,48),(120,40),(80,24)])
def test_menu_keeps_selection_and_hint_visible(tmp_path,size):
    async def scenario(app,pilot,prompt):
        prompt.focus();await settle(pilot)
        await pilot.press('/');await settle(pilot)
        await pilot.press(*['down']*(len(NAMES)-1));await settle(pilot);await settle(pilot)
        menu,detail=app.query_one('#slash-menu',OptionList),app.query_one('#detail')
        assert menu.highlighted==len(NAMES)-1
        top=menu.scroll_offset.y
        assert top<=menu.highlighted<top+menu.scrollable_content_region.height
        for widget in (menu,app.query_one('#slash-hint')):
            assert detail.region.contains_region(widget.region),widget.id
        assert detail.region.overlaps(prompt.region)
        assert detail.max_scroll_x==0 and menu.max_scroll_x==0
        assert menu.region.right<=size[0] and menu.region.bottom<size[1]
    run(tmp_path,scenario,size=size)
