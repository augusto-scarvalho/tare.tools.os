import asyncio
from textual.app import App
from textual.widgets import Select
from relay.permissions_panel import PermissionsPanel


def test_automatic_review_roundtrips_without_inactive_manual_fields(tmp_path):
    captured=[]
    defaults={'mode':'commands','write_paths':['.'],'command_protection':{
        'review':'auto','backend':'mxc','max_reviews':24,'review_seconds':60}}
    def view():
        return {'project':str(tmp_path),'revision':'initial','defaults':defaults,
            'effective_text':'Execution policy','instructions_text':''}
    def save(value,revision):
        captured.append(value)
        return {**view(),'revision':'saved','defaults':value}
    async def scenario():
        app=App();panel=PermissionsPanel(tmp_path,reader=view,saver=save)
        async with app.run_test(size=(130,44)) as pilot:
            app.push_screen(panel)
            await panel.workers.wait_for_complete();await pilot.pause()
            assert panel.query_one('#permissions-review',Select).value=='auto'
            assert panel.query_one('#permissions-classifier',Select).disabled
            assert panel.query_one('#permissions-isolation',Select).disabled
            assert not panel.query_one('#permissions-classifier',Select).parent.display
            assert not panel.query_one('#permissions-isolation',Select).parent.display
            panel.action_save()
            await panel.workers.wait_for_complete();await pilot.pause()
            assert captured and captured[0]['command_protection']==defaults['command_protection']
    asyncio.run(scenario())
