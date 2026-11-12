"""/guidance: the owner's edits of tare's role guidance and choices about what each vendor CLI adds on its own."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from textual.widgets import Button, OptionList, Static, TextArea

from relay import guidance, native_agent
from relay.console_guidance import GuidancePanel
from relay.native_console import WorkConsole
from test_native_agent import Client, deployment, setup, steps  # noqa: F401  (setup is a fixture)
from test_native_console import FakeBackend

IMPLEMENTER = 'role#role/implementer#system'
EMAIL = 'vendor#cli://claude#context:userEmail'
DATE = 'vendor#cli://claude#attachment:date'


def reading(date="Today's date is <date>.", extra=()):
    """What compute_plane.vendor_guidance.read returns, for two CLIs (texts already with placeholders)."""
    return {'cli://claude': {'cli': 'Claude Code', 'cli_version': '2.1.322', 'note': 'Read on this machine.',
                             'items': [
        {'id': 'system:2', 'title': 'System text: You are a Claude agent.', 'sent_as': 'system block 2',
         'controls': (), 'text': 'You are a Claude agent.'},
        {'id': 'context:userEmail', 'title': 'Context block "userEmail"', 'sent_as': "user message before tare's records",
         'controls': ('remove',), 'text': "The user's email address is <account email>."},
        {'id': 'attachment:date', 'title': 'Attachment "date"', 'sent_as': "system message after tare's records",
         'controls': ('remove', 'replace'), 'text': date}, *extra]},
            'cli://codex': {'cli': 'Codex', 'cli_version': '0.166.0', 'note': 'Read with prompt-input.', 'items': [
        {'id': 'environment_context', 'title': 'Block <environment_context>', 'sent_as': 'user message',
         'controls': ('remove',), 'text': '<environment_context>\n  <cwd><working directory></cwd>'}]},
            'cli://agy': {'cli': 'AGY', 'cli_version': '1.5.7', 'note': 'Read from its record.', 'items': []}}


def real_reading(claude_version='2.1.322', agy_tool='Manage background tasks.'):
    """What compute_plane.vendor_guidance.read_real returns: one small real call each."""
    return {'cli://claude': {'cli': 'Claude Code', 'cli_version': claude_version, 'note': 'Read on this machine.',
                             'usage': {'output_tokens': 4}, 'items': [
        {'id': 'context:userEmail', 'title': 'Context block "userEmail"', 'sent_as': "user message before tare's records",
         'controls': ('remove',), 'text': "The user's email address is <account email>.", 'local': 'same'},
        {'id': 'attachment:total_tokens_reminder', 'title': 'Attachment "total_tokens_reminder"',
         'sent_as': 'sent with the request; seen only in a real call', 'controls': ('remove', 'replace'),
         'text': '<total_tokens><tokens> tokens left</total_tokens>', 'local': 'absent'}]},
            'cli://agy': {'cli': 'AGY', 'cli_version': '1.5.7', 'note': 'Read from its record.',
                          'usage': {'total_tokens': 758}, 'items': [
        {'id': 'request:ADDITIONAL_METADATA', 'title': 'Block <ADDITIONAL_METADATA>', 'sent_as': 'user message',
         'controls': (), 'text': 'The current local time is: <local time>.'},
        {'id': 'tool:manage_task', 'title': 'Tool "manage_task"', 'sent_as': 'tool definition', 'controls': (),
         'text': agy_tool}]}}


def read_as(monkeypatch, value, real=None, calls=None, honours=True):
    """One TUI start's readings, from fixtures: never a CLI, never a real call. `real` is what a real reading
    returns per CLI (missing: it fails, as a spent quota does); `calls` collects the real readings made."""
    from compute_plane import vendor_guidance
    from relay import cli_discovery, turn_engine
    monkeypatch.setattr(cli_discovery, 'locate', lambda endpoint: f'C:/fixture/{endpoint[6:]}.exe')
    routes = {'claude-haiku-5-5-cli': 'claude-haiku-5-5', 'agy-cli': 'gemini-3.8-flash-high'}
    monkeypatch.setattr(turn_engine, 'available', lambda name, probe=True: (
        {'native_profile': {'model': routes[name]}, 'native_cli': {'executable': f'C:/fixture/{name}.exe'}}
        if name in routes else None, 'not installed'))

    def read(endpoint, executable, choices=None):  # with choices: the CLI leaves them out, unless it ignores them
        if isinstance(value.get(endpoint), Exception):
            raise value[endpoint]
        found = json.loads(json.dumps(value[endpoint]))
        found['items'] = [i for i in found['items'] if not (honours and i['id'] in (choices or {}))]
        return found

    def read_real(endpoint, executable, *, model):
        if calls is not None:
            calls.append((endpoint, model))
        if endpoint not in (real or {}):
            raise ValueError('You have hit your usage limit.')
        return json.loads(json.dumps(real[endpoint]))
    monkeypatch.setattr(vendor_guidance, 'read', read)
    monkeypatch.setattr(vendor_guidance, 'read_real', read_real)
    return guidance.refresh_readings()


def states():
    return {item['id']: item['state'] for item in guidance.items()}


def test_choices_round_trip_with_the_default_they_were_made_against(monkeypatch):
    read_as(monkeypatch, reading())
    assert not guidance.path().exists() and guidance.vendor_choices('cli://claude') == {}
    implementer = native_agent.prompt_capsule(deployment(), 'implementer')['system']
    guidance.save(IMPLEMENTER, 'Implement carefully.')
    guidance.save(EMAIL, None)
    guidance.save(DATE, 'Today is a working day.')
    base = lambda text: {'sha256': guidance.digest(text), 'text': text}  # noqa: E731
    saved = json.loads(guidance.path().read_text(encoding='utf-8'))
    assert saved == {'schema': guidance.SCHEMA,
                     'capsules': {'role/implementer': {'system': {'value': 'Implement carefully.',
                                                                  'base': base(implementer)}}},
                     'vendor': {'cli://claude': {
                         'context:userEmail': {'value': None, 'base': base("The user's email address is <account email>.")},
                         'attachment:date': {'value': 'Today is a working day.', 'base': base("Today's date is <date>.")}}}}
    assert (states()[IMPLEMENTER], states()[EMAIL], states()[DATE]) == ('yours', 'removed', 'yours')
    assert guidance.vendor_choices('cli://claude') == {'context:userEmail': None,
                                                       'attachment:date': 'Today is a working day.'}
    for refused in [(EMAIL, 'another text'),  # Claude Code can only drop a context block
                    ('vendor#cli://codex#environment_context', 'another text'),  # Codex never rewrites
                    ('vendor#cli://claude#system:2', None),  # read-only
                    ('transport#claude-native', 'another text'), ('vendor#cli://agy#anything', None)]:
        with pytest.raises(ValueError):
            guidance.save(*refused)
    assert json.loads(guidance.path().read_text(encoding='utf-8')) == saved  # a refused save changes nothing
    guidance.reset(DATE)
    guidance.save(IMPLEMENTER, '  ')  # an empty edit is tare's default
    assert guidance.capsule_edits() == {} and guidance.vendor_choices('cli://claude') == {'context:userEmail': None}
    guidance.reset()
    assert not guidance.path().exists() and set(states().values()) == {'default'}


def test_each_reading_keeps_one_earlier_text_flags_new_blocks_and_survives_a_failure(monkeypatch):
    read_as(monkeypatch, reading())
    assert guidance.news() == ''  # a first reading has nothing to compare with
    added = {'id': 'attachment:skills', 'title': 'Attachment "skills"', 'sent_as': 'system message',
             'controls': ('remove', 'replace'), 'text': 'Skills you may use.'}
    stored = read_as(monkeypatch, reading("Today is <date>.", [added]))
    date = stored['cli://claude']['items']['attachment:date']
    assert date['previous'] == {'text': "Today's date is <date>.", 'sha256': guidance.digest("Today's date is <date>.")}
    assert stored['cli://claude']['items']['attachment:skills']['new'] is True
    assert states()['vendor#cli://claude#attachment:skills'] == 'new' and '/guidance' in guidance.news()
    failed = read_as(monkeypatch, {**reading("Today is <date>.", [added]), 'cli://codex': OSError('gone')})
    assert failed['cli://codex']['items'] and 'gone' in failed['cli://codex']['error']  # the last reading stays
    assert 'new' not in failed['cli://claude']['items']['attachment:skills']  # new only for the reading that found it
    assert failed['cli://claude']['items']['attachment:date']['previous']['text'] == "Today's date is <date>."


def test_a_default_that_changed_under_a_choice_waits_for_the_owner(monkeypatch, tmp_path):
    read_as(monkeypatch, reading())
    guidance.save(DATE, 'My date.')
    read_as(monkeypatch, reading("Today is <date> (new wording)."))  # a CLI update
    assert states()[DATE] == 'changed'
    assert guidance.vendor_choices('cli://claude') == {'attachment:date': 'My date.'}  # nothing unapproved goes out
    row = next(r for r in guidance.items() if r['id'] == DATE)
    assert (row['base'], row['default']) == ("Today's date is <date>.", "Today is <date> (new wording).")
    guidance.keep(DATE)  # keep mine: approved against the new default
    assert states()[DATE] == 'yours' and guidance.load()['vendor']['cli://claude']['attachment:date'] == {
        'value': 'My date.', 'base': {'sha256': guidance.digest(row['default']), 'text': row['default']}}
    read_as(monkeypatch, reading("A third wording."))
    guidance.save(DATE, 'My new date.')  # edit and Save: also made against the newest default
    assert guidance.load()['vendor']['cli://claude']['attachment:date']['base']['text'] == 'A third wording.'
    read_as(monkeypatch, reading("A fourth wording."))
    guidance.reset(DATE)  # use the new default
    assert states()[DATE] == 'default' and guidance.vendor_choices('cli://claude') == {}
    # tare's own default can change under an edit too: the same states, no other path
    guidance.save(IMPLEMENTER, 'Implement carefully.')
    registry = json.loads(native_agent.PROMPT_CAPSULES.read_text(encoding='utf-8'))
    next(c for c in registry['capsules'] if c['id'] == 'role/implementer')['system'] += ' A tare update.'
    (tmp_path/'capsules.json').write_text(json.dumps(registry), encoding='utf-8')
    monkeypatch.setattr(native_agent, 'PROMPT_CAPSULES', tmp_path/'capsules.json')
    assert states()[IMPLEMENTER] == 'changed'
    assert native_agent.prompt_capsule(deployment(), 'implementer')['system'] == 'Implement carefully.'
    guidance.keep(IMPLEMENTER)
    assert states()[IMPLEMENTER] == 'yours'


def test_a_role_edit_reaches_new_sessions_and_reset_restores_tares_exact_capsule(setup):  # noqa: F811
    config, custody, root = setup
    before = native_agent.prompt_capsule(deployment(), 'implementer')
    guidance.save(IMPLEMENTER, 'Implement only what the user asked; say what you did not check.')
    edited = native_agent.prompt_capsule(deployment(), 'implementer')
    assert edited['system'] == 'Implement only what the user asked; say what you did not check.'
    assert edited['version'] == before['version'] + '-edited' and edited['sha256'] != before['sha256']
    client = Client(steps=steps())
    native_agent.run_session(config, deployment(), custody, 'Change the value to 2.', client=client,
                             local_counter=client)
    first = next(body for path, body in client.calls if path == '/chat/completions')
    assert 'Implement only what the user asked; say what you did not check.' in first['messages'][0]['content']
    assert before['system'] not in first['messages'][0]['content']
    frozen = json.loads((custody/'native-agent.json').read_bytes())['prompt_capsule']
    assert frozen['sha256'] == edited['sha256']
    guidance.reset(IMPLEMENTER)
    assert native_agent.prompt_capsule(deployment(), 'implementer') == before


def test_the_vendor_choices_reach_the_cli_client(tmp_path, monkeypatch):
    from relay.native_generation import inference_client
    read_as(monkeypatch, reading())
    guidance.save(EMAIL, None)
    executable = str(tmp_path/'claude.exe')
    client = inference_client({'native_cli': {'executable': executable, 'version': '2.1.322'}},
                              SimpleNamespace(protocol='vendor-cli', endpoint='cli://claude'),
                              credential=None, metadata_only=True)
    assert client.vendor_guidance == {'context:userEmail': None}
    codex = inference_client({'native_cli': {'executable': executable, 'version': '0.166.0',
                                             'model_catalog': {'slug': 'fixture'}}},
                             SimpleNamespace(protocol='vendor-cli', endpoint='cli://codex'),
                             credential=None, metadata_only=True)
    assert codex.vendor_guidance == {}  # no choice: exactly what tare sent before


def test_a_saved_choice_this_tare_cannot_apply_says_where_to_fix_it(tmp_path):
    from relay.native_generation import inference_client
    guidance.path().parent.mkdir(parents=True, exist_ok=True)
    stale = {'value': None, 'base': {'sha256': guidance.digest('old'), 'text': 'old'}}
    guidance.path().write_text(json.dumps({'schema': guidance.SCHEMA, 'capsules': {},
                                           'vendor': {'cli://claude': {'retired_block': stale}}}), encoding='utf-8')
    with pytest.raises(ValueError) as caught:
        inference_client({'native_cli': {'executable': str(tmp_path/'claude.exe'), 'version': '2.1.322'}},
                         SimpleNamespace(protocol='vendor-cli', endpoint='cli://claude'), credential=None,
                         metadata_only=True)
    # The session reports it as its blocked reason, 'ValueError:' plus 256 characters; some rows show far fewer.
    assert str(caught.value).startswith('Open /guidance and choose "Reset everything"')
    assert 'retired_block' in str(caught.value) and len(str(caught.value)) <= 256
    guidance.reset()  # what that button does
    assert guidance.vendor_choices('cli://claude') == {}


@pytest.mark.parametrize('size', [(80, 24), (130, 44)])
def test_guidance_in_the_tui_shows_before_and_after_and_settles_each_change(tmp_path, monkeypatch, size):
    read_as(monkeypatch, reading())
    guidance.save(DATE, 'My date.')
    read_as(monkeypatch, reading("Today is <date> (new wording)."))

    async def scenario():
        app = WorkConsole(tmp_path, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            assert app.dispatch_conversation_command('/guidance', '')
            await pilot.pause()
            panel = app.screen
            assert isinstance(panel, GuidancePanel)
            choice, text = panel.query_one('#guidance-item', OptionList), panel.query_one('#guidance-text', TextArea)
            assert str(choice.get_option('group#cli://claude').prompt) == 'Claude Code 2.1.322'
            assert str(choice.get_option('note#cli://claude#0').prompt).strip().startswith('Read without tokens')

            async def pick(key):
                choice.highlighted = choice.get_option_index(key)
                await pilot.pause()
            diff = panel.query_one('#guidance-diff', TextArea)
            buttons = {b.id: b for b in panel.query(Button)}
            assert panel.region.contains_region(buttons['guidance-save'].region)

            await pick('transport#claude-native')
            assert text.read_only and buttons['guidance-save'].disabled and not diff.display

            await pick(DATE)  # changed underneath: base -> new default, and the three ways out
            assert text.text == 'My date.' and not buttons['guidance-keep'].disabled
            assert "-Today's date is <date>." in diff.text and '+Today is <date> (new wording).' in diff.text
            assert str(buttons['guidance-default'].label) == 'Use the new default'
            await pilot.click('#guidance-keep')
            await pilot.pause()
            assert buttons['guidance-keep'].disabled and '+My date.' in diff.text  # now: default × yours

            await pick(EMAIL)
            assert text.read_only and not buttons['guidance-remove'].disabled
            assert str(buttons['guidance-default'].label) == "Vendor's default" and buttons['guidance-default'].disabled
            await pilot.click('#guidance-remove')
            await pilot.pause()
            assert guidance.vendor_choices('cli://claude')['context:userEmail'] is None
            assert "-The user's email address is <account email>." in diff.text
            await pilot.click('#guidance-default')
            await pilot.pause()
            assert 'context:userEmail' not in guidance.vendor_choices('cli://claude')

            await pick(IMPLEMENTER)
            assert not text.read_only and str(buttons['guidance-default'].label) == "tare's default"
            text.load_text('Implement carefully.')
            await pilot.click('#guidance-save')
            await pilot.pause()
            assert guidance.capsule_edits() == {'role/implementer': {'system': 'Implement carefully.'}}
            assert 'New sessions' in str(panel.query_one('#guidance-status').render())
            await pilot.click('#guidance-reset-all')
            await pilot.pause()
            assert not guidance.path().exists() and text.text == native_agent.prompt_capsule(
                deployment(), 'implementer')['system']
            assert not app.backend.launches
            await pilot.press('escape')
            assert app.screen is not panel
    asyncio.run(scenario())


def test_the_before_and_after_view_keeps_each_line_apart():
    from relay.console_guidance import compare
    lines = compare("Today's date is <date>.", 'Today is a test day.', 'default', 'yours').splitlines()
    assert lines[-2:] == ["-Today's date is <date>.", '+Today is a test day.']  # texts without a final newline


def test_a_real_reading_runs_once_per_cli_version_on_the_cheapest_ready_route(monkeypatch):
    calls = []
    stored = read_as(monkeypatch, reading(), real_reading(), calls)
    # Haiku is Claude's cheapest ready route; AGY's route model is read at its low tier. Codex needs no real reading.
    assert calls == [('cli://claude', 'claude-haiku-5-5'), ('cli://agy', 'gemini-3.8-flash-low')]
    assert stored['cli://claude']['real']['usage'] == {'output_tokens': 4}
    rows = {r['id']: r for r in guidance.items()}
    reminder = rows['vendor#cli://claude#attachment:total_tokens_reminder']
    assert reminder['seen'].startswith('Seen only in a real call') and reminder['controls'] == ('remove', 'replace')
    assert rows[EMAIL]['seen'] == ''  # the zero-token reading has it
    guidance.save(reminder['id'], None)  # a block seen only in a real call can be left out like any other
    assert guidance.vendor_choices('cli://claude') == {'attachment:total_tokens_reminder': None}
    agy = rows['vendor#cli://agy#tool:manage_task']
    assert agy['controls'] == () and agy['seen'] == "Read from AGY's own record of a real call."
    read_as(monkeypatch, reading(), real_reading(), calls)
    assert len(calls) == 2  # the same versions: no second real call
    newer = reading()
    newer['cli://claude']['cli_version'] = '2.1.323'
    read_as(monkeypatch, newer, real_reading('2.1.323'), calls)
    assert calls[2:] == [('cli://claude', 'claude-haiku-5-5')]  # only the CLI whose version changed


def test_agy_shows_its_starting_point_until_a_real_reading_and_says_when_one_fails(monkeypatch):
    from compute_plane.vendor_guidance import AGY_STARTING_POINT
    from relay.console_guidance import group_lines
    stored = read_as(monkeypatch, reading())  # every real call fails, as with a spent quota
    assert 'usage limit' in stored['cli://agy']['real']['error'] and 'items' not in stored['cli://agy']['real']
    rows = {r['id']: r for r in guidance.items() if r['id'].startswith('vendor#cli://agy#')}
    assert [r.split('#')[2] for r in rows] == [i['id'] for i in AGY_STARTING_POINT['items']]
    assert all(r['seen'].startswith('From what AGY 1.5.7 sent on 2026-11-12') for r in rows.values())
    line = ' '.join(group_lines('cli://agy', guidance.readings()['cli://agy'], False))
    assert 'Shown as AGY 1.5.7 sent it on 2026-11-12, until tare reads yours.' in line
    assert 'The real call could not be made' in line
    read_as(monkeypatch, reading(), real_reading(agy_tool='A new description.'))
    rows = {r['id']: r for r in guidance.items() if r['id'].startswith('vendor#cli://agy#')}
    assert rows['vendor#cli://agy#tool:manage_task']['previous']  # changed since the starting point
    assert 'Read in a real call' in group_lines('cli://agy', guidance.readings()['cli://agy'], False)[0]


@pytest.mark.parametrize('size', [(80, 24), (130, 44)])
def test_guidance_waits_for_the_startup_reading_then_groups_and_marks_every_text(tmp_path, monkeypatch, size):
    async def scenario():
        app = WorkConsole(tmp_path, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            app.guidance_reading = True  # a start whose reading has not landed, and no reading stored yet
            assert app.dispatch_conversation_command('/guidance', '')
            await pilot.pause()
            panel = app.screen
            options, text = panel.query_one('#guidance-item', OptionList), panel.query_one('#guidance-text', TextArea)
            buttons = {b.id: b for b in panel.query(Button)}
            assert panel.region.contains_region(buttons['guidance-save'].region)

            def listed():
                return [options.get_option_at_index(i).id for i in range(options.option_count)]
            headings = [key for key in listed() if key.startswith('group#')]
            assert headings == ['group#role', 'group#protocol', 'group#cli://claude', 'group#cli://codex',
                                'group#cli://agy']  # tare's texts first, then each CLI's
            assert all(options.get_option(key).disabled for key in headings)
            for cli in ('claude', 'codex', 'agy'):
                assert str(options.get_option(f'note#cli://{cli}#0').prompt).strip().startswith('Being read now')
            assert not any(key.startswith('vendor#') for key in listed())  # tare's own groups are usable at once
            assert 'M your choice' in str(panel.query_one('#guidance-read', Static).render())
            options.highlighted = options.get_option_index(IMPLEMENTER)
            await pilot.pause()
            text.load_text('Typed, not saved yet.')

            read_as(monkeypatch, reading())  # the reading lands while the panel is open
            guidance.save(EMAIL, None)
            guidance.save(DATE, 'My date.')
            app.guidance_landed()
            await pilot.pause()
            claude = [key.split('#')[2] for key in listed() if key.startswith('vendor#cli://claude#')]
            assert claude == ['system:2', 'context:userEmail', 'attachment:date']  # in the order they are sent
            assert str(options.get_option('group#cli://claude').prompt) == 'Claude Code 2.1.322'
            assert panel.current == IMPLEMENTER and text.text == 'Typed, not saved yet.'  # nothing lost
            assert str(options.get_option(DATE).prompt).endswith('  M')
            assert str(options.get_option(EMAIL).prompt).endswith('  D')
            colour = app.theme_variables.get('warning') or 'yellow'
            letter = options.get_option(DATE).prompt.spans[-1]
            assert 'bold' in str(letter.style) and colour in str(letter.style)
            for state, letter in (('yours', 'M'), ('removed', 'D'), ('changed', 'C'), ('new', 'U'), ('default', '')):
                assert panel.label({'title': 'Block <permissions instructions>', 'state': state}).plain == (
                    '  Block <permissions instructions>' + ('  ' + letter if letter else ''))
            await pilot.press('escape')
    asyncio.run(scenario())


def test_a_block_missing_from_the_newest_reading_stays_listed_with_when_it_was_last_seen(monkeypatch):
    added = {'id': 'attachment:skills', 'title': 'Attachment "skills"', 'sent_as': 'system message',
             'controls': ('remove', 'replace'), 'text': 'Skills you may use.'}
    read_as(monkeypatch, reading(extra=[added]), real_reading())
    read_as(monkeypatch, reading())  # the zero-token reading no longer sees it
    rows = {r['id']: r for r in guidance.items()}
    skills = rows['vendor#cli://claude#attachment:skills']
    assert skills['absent'] and skills['controls'] == ('remove', 'replace')
    assert skills['seen'].startswith('Not in the latest reading: last seen ') and 'on Claude Code 2.1.322' in skills['seen']
    guidance.save(skills['id'], None)  # one that comes and goes can still be left out
    assert guidance.vendor_choices('cli://claude') == {'attachment:skills': None}
    newer, real = reading(), real_reading('2.1.323')
    newer['cli://claude']['cli_version'] = '2.1.323'
    real['cli://claude']['items'] = [i for i in real['cli://claude']['items']
                                     if i['id'] != 'attachment:total_tokens_reminder']
    read_as(monkeypatch, newer, real)  # a real reading of a new version without the tokens-left reminder
    rows = {r['id']: r for r in guidance.items()}
    reminder = rows['vendor#cli://claude#attachment:total_tokens_reminder']
    assert reminder['absent'] and 'on Claude Code 2.1.322' in reminder['seen'] and reminder['controls']
    assert list(rows).index(reminder['id']) > list(rows).index(EMAIL)  # after the blocks sent now
    read_as(monkeypatch, reading(extra=[added]), real)  # seen again: listed as present
    assert not {r['id']: r for r in guidance.items()}['vendor#cli://claude#attachment:skills']['absent']


SKILLS = 'vendor#cli://codex#skills_instructions'


def with_skills(version='0.166.0'):
    value = reading()
    value['cli://codex']['cli_version'] = version
    value['cli://codex']['items'].insert(0, {
        'id': 'skills_instructions', 'title': 'Block <skills_instructions>', 'sent_as': 'developer message',
        'controls': ('remove',), 'text': '<skills_instructions>\n- imagegen: Generate images.\n</skills_instructions>'})
    return value


def test_tare_leaves_the_codex_skills_list_out_only_where_a_reading_proved_it(monkeypatch):
    assert guidance.vendor_choices('cli://codex') == {}  # before any reading, nothing is added
    read_as(monkeypatch, with_skills(), honours=False)  # an installed Codex that ignores the switch
    assert guidance.vendor_choices('cli://codex') == {}
    row = {r['id']: r for r in guidance.items()}[SKILLS]
    assert row['state'] == 'default' and not row['tare_proved'] and row['tare_default']
    read_as(monkeypatch, with_skills())  # this Codex leaves it out when asked: proved for 0.166.0
    assert guidance.readings()['cli://codex']['defaults'] == {'cli_version': '0.166.0',
                                                              'proved': ['skills_instructions']}
    assert guidance.vendor_choices('cli://codex') == {'skills_instructions': None}
    row = {r['id']: r for r in guidance.items()}[SKILLS]
    assert row['state'] == 'tare-default' and row['text'].startswith('<skills_instructions>')
    stored = guidance.readings()
    stored['cli://codex']['cli_version'] = '0.167.0'  # a reading of another version, its proof not made yet
    guidance._write(guidance.readings_path(), {'schema': guidance.READING_SCHEMA, **stored})
    assert guidance.vendor_choices('cli://codex') == {}


def test_the_owner_can_send_it_again_and_their_own_choice_wins(monkeypatch):
    read_as(monkeypatch, with_skills())
    guidance.send_again(SKILLS)  # a keep: the CLI's text, never passed to the Kernel as a choice
    assert guidance.vendor_choices('cli://codex') == {}
    assert {r['id']: r for r in guidance.items()}[SKILLS]['state'] == 'kept'
    guidance.reset(SKILLS)  # tare's default again
    assert guidance.vendor_choices('cli://codex') == {'skills_instructions': None}
    guidance.save('vendor#cli://codex#environment_context', None)  # the owner's own choices stand beside it
    assert guidance.vendor_choices('cli://codex') == {'environment_context': None, 'skills_instructions': None}
    guidance.save(SKILLS, None)  # the owner's own removal wins over tare's, and is shown as theirs
    assert {r['id']: r for r in guidance.items()}[SKILLS]['state'] == 'removed'
    guidance.send_again(SKILLS)
    guidance.reset()  # everything: back to tare's defaults
    assert guidance.vendor_choices('cli://codex') == {'skills_instructions': None} and not guidance.path().exists()


def test_the_panel_shows_what_tare_leaves_out_and_brings_it_back(tmp_path, monkeypatch):
    read_as(monkeypatch, with_skills())

    async def scenario():
        app = WorkConsole(tmp_path, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            assert app.dispatch_conversation_command('/guidance', '')
            await pilot.pause()
            panel = app.screen
            options, diff = panel.query_one('#guidance-item', OptionList), panel.query_one('#guidance-diff', TextArea)
            default = panel.query_one('#guidance-default', Button)
            assert str(options.get_option(SKILLS).prompt).endswith('  T')
            options.highlighted = options.get_option_index(SKILLS)
            await pilot.pause()
            assert "Left out by tare's default" in str(panel.query_one('#guidance-about', Static).render())
            assert '-- imagegen: Generate images.' in diff.text  # what is left out
            assert str(default.label) == "Vendor's default" and not default.disabled
            await pilot.click('#guidance-default')
            await pilot.pause()
            assert guidance.vendor_choices('cli://codex') == {}
            assert str(options.get_option(SKILLS).prompt).endswith('  M') and str(default.label) == "tare's default"
            await pilot.pause(0.3)  # a button ignores a second press while it still shows the first one
            await pilot.click('#guidance-default')
            await pilot.pause()
            assert guidance.vendor_choices('cli://codex') == {'skills_instructions': None}
            await pilot.press('escape')
    asyncio.run(scenario())
