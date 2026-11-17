"""The owner chooses, per role, the class of model the role asks for (/routes > Order); with no choice saved every order
is tare's own (native_routes.WANTED), and independence, another vendor first, pins and the owner's lists are untouched
(owner, 2026-11-17)."""
import asyncio
import json

import pytest
from textual.widgets import Button, Select

from relay import execution_scope as scope
from relay import native_catalog, native_routes, turn_engine, work_audit
from relay.console_routes import RoutesPanel
from relay.native_console import WorkConsole
from test_cross_vendor_auditors import pool  # noqa: F401  (fixture)
from test_native_console import FakeBackend
from test_role_pin_dispatch import pin_catalog  # noqa: F401  (fixture)
from test_role_pins import parse
from test_tiers_by_class import class_of, everything, ranked_names  # noqa: F401  (fixture)


def saved():
    return json.loads(turn_engine.policy_path().read_text(encoding='utf-8'))


def test_save_keeps_only_the_roles_that_differ_and_refuses_what_it_does_not_know():
    assert turn_engine.save_policy({}, wanted=dict(native_routes.WANTED)).get('wanted') is None
    assert 'wanted' not in saved()  # all at tare's default: no key
    turn_engine.save_policy({}, wanted={**native_routes.WANTED, 'implementer': 'frontier', 'planner': 'cheap'})
    assert saved()['wanted'] == {'implementer': 'frontier', 'planner': 'cheap'}
    turn_engine.save_policy({'planner': [['openai-api']]})  # a save that does not name it keeps it
    assert saved()['wanted'] == {'implementer': 'frontier', 'planner': 'cheap'}
    with pytest.raises(ValueError, match='nonsense'):
        turn_engine.save_policy({}, wanted={'nonsense': 'cheap'})
    with pytest.raises(ValueError, match='huge'):
        turn_engine.save_policy({}, wanted={'planner': 'huge'})
    with pytest.raises(ValueError, match='explorer'):
        turn_engine.save_policy({}, wanted={'explorer': 'cheap'})
    assert saved()['wanted'] == {'implementer': 'frontier', 'planner': 'cheap'}  # a refused save changes nothing
    turn_engine.reset_policy()
    assert not turn_engine.policy_path().exists() and native_routes.wanted('implementer') == 'cheap'


@pytest.mark.parametrize('bad', ['frontier', ['frontier'], {'implementer': 'huge'}, {'implementer': ['frontier']},
                                 {'explorer': 'frontier'}, None])
def test_a_malformed_wanted_in_the_file_is_ignored(tmp_path, bad):
    turn_engine.policy_path().parent.mkdir(parents=True, exist_ok=True)
    turn_engine.policy_path().write_text(json.dumps({'wanted': bad}), encoding='utf-8')
    assert {role: native_routes.wanted(role) for role in native_routes.WANTED} == native_routes.WANTED
    assert native_routes.wanted('explorer') is None
    turn_engine.save_policy({})  # and a save drops it
    assert 'wanted' not in saved()


def test_an_implementer_set_to_frontier_goes_to_a_frontier_route_and_back(everything):
    before = ranked_names('implementer')
    assert class_of(before[0]) != 'frontier'
    turn_engine.save_policy({}, wanted={'implementer': 'frontier'})
    after = ranked_names('implementer')
    assert class_of(after[0]) == 'frontier' and sorted(after) == sorted(before)
    turn_engine.save_policy({}, wanted={'implementer': 'cheap'})
    assert ranked_names('implementer') == before
    turn_engine.save_policy({}, wanted={'implementer': 'frontier'})
    turn_engine.reset_policy()
    assert ranked_names('implementer') == before


def test_a_planner_set_to_cheap_goes_to_a_cheaper_route_first(everything):
    before = ranked_names('planner')
    assert class_of(before[0]) == 'frontier'
    turn_engine.save_policy({}, wanted={'planner': 'cheap'})
    after = ranked_names('planner')
    assert class_of(after[0]) != 'frontier' and sorted(after) == sorted(before)
    turn_engine.reset_policy()
    assert ranked_names('planner') == before


@pytest.mark.parametrize('wanted', ['cheap', 'middling', 'frontier'])
@pytest.mark.parametrize('role', ['auditor', 'plan-auditor'])
@pytest.mark.parametrize('producer', ['claude-api-opus-5-5', 'gemini-api'])
def test_whatever_class_a_reviewer_wants_the_author_is_never_its_own_reviewer_and_another_vendor_comes_first(
        everything, producer, role, wanted):
    author = native_catalog.select(producer)
    turn_engine.save_policy({}, wanted={role: wanted})
    candidates, _ = native_routes.reviewers(author, role)
    kinds = [work_audit.independence(author, candidate) for candidate in candidates]
    assert kinds and 'AUDITOR_NOT_INDEPENDENT' not in kinds  # the author's own model or bias domain is never listed
    assert kinds[0] == 'INDEPENDENT'  # another vendor first
    assert kinds == sorted(kinds, key=lambda kind: kind != 'INDEPENDENT')  # and every one of them before the same vendor


def test_the_owners_lists_are_untouched(everything):
    turn_engine.save_policy({'planner': [['openai-api'], ['claude-api-opus-5-5']], 'implementer': [['bedrock-sonnet']]},
                            wanted={'planner': 'cheap', 'implementer': 'frontier'})
    assert turn_engine.planner_routes() == ['openai-api', 'claude-api-opus-5-5']
    assert turn_engine.implementer_tiers() == [['bedrock-sonnet']]


def test_a_pin_is_untouched(pin_catalog):  # noqa: F811
    turn_engine.save_policy({}, wanted={'planner': 'cheap', 'auditor': 'frontier'})
    with scope.activated(parse(['--planner', 'anthropic/test-cli@max'])):
        chosen, receipt = native_routes.ranked('planner')
        assert receipt['reason'] == 'EXACT_TEMPORARY_PIN' and scope.pin_matches('anthropic/test-cli@max', chosen[0])
    with scope.activated(parse(['--auditor', 'anthropic/test-cli@max'])):
        chosen, receipt = native_routes.reviewers(pin_catalog['producer-cli'], 'plan-auditor')
        assert receipt['reason'] == 'EXACT_TEMPORARY_PIN' and scope.pin_matches('anthropic/test-cli@max', chosen[0])


def test_the_panel_shows_the_saved_choice_marks_tares_default_saves_and_restores(tmp_path):
    turn_engine.save_policy({}, wanted={'implementer': 'frontier'})

    async def scenario():
        app = WorkConsole(tmp_path, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=(130, 44)) as pilot:
            await app.workers.wait_for_complete()
            assert app.dispatch_conversation_command('/routes', '')
            await pilot.pause()
            panel = app.screen
            assert isinstance(panel, RoutesPanel)
            box = {role: panel.query_one('#routes-wanted-' + role, Select) for role in native_routes.WANTED}
            assert list(box) == ['planner', 'plan-auditor', 'auditor', 'implementer', 'coordinator']
            assert [box[r].value for r in box] == ['frontier', 'middling', 'middling', 'frontier', 'cheap']
            options = lambda role: [prompt for prompt, _ in RoutesPanel.class_options(role)]  # noqa: E731
            assert options('planner') == ['basic · the cheapest route that is clever enough first', 'middling',
                                          "frontier · the most capable first (tare's default)"]
            assert options('auditor')[1] == "middling (tare's default)"
            assert options('implementer')[0].endswith("first (tare's default)")
            box['planner'].value = 'cheap'
            box['implementer'].value = 'cheap'
            await pilot.click('#routes-save')
            await pilot.pause()
            assert saved()['wanted'] == {'planner': 'cheap'}  # the implementer is back at tare's default
            await pilot.click('#routes-default')
            await pilot.pause()
            assert not turn_engine.policy_path().exists()
            assert [box[r].value for r in box] == ['frontier', 'middling', 'middling', 'cheap', 'cheap']
            assert not app.backend.launches
    asyncio.run(scenario())
