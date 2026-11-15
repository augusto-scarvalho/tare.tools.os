"""A reply cut by the route's output cap: tell the model and go on, instead of ending the session.

The Runtime ends a turn whose reply is incomplete (PARTIAL_REPLY) before acting on any of it. Where the provider says
the cap cut it, that is not the model giving up: on an API where thinking counts toward the cap, a model can spend
the whole allowance thinking and write nothing (an Opus 5.5 implementer did, 2026-11-15, and its unit ended as "the
implementer changed nothing"). Claude Code continues after such a cut with a message of its own; so does this.
"""
import json

ATTEMPTS = 2  # cuts in a row, with no complete reply between them: each already cost a whole output allowance
CUT = {'length', 'max_tokens'}


def cut(journal):
    """The last model reply's event when the provider's output cap cut it, else None."""
    returned = next((r.event() for r in reversed(journal.snapshot().records)
                     if r.event()['kind'] == 'model_returned'), None)
    if returned is None:
        return None
    try:
        reason = json.loads(returned['reply'].get('metadata_json') or '{}').get('provider_stop_reason')
    except (TypeError, ValueError, AttributeError):
        return None
    return returned if reason in CUT else None


def message(reply, cap):
    """What the host tells the model: nothing of the cut reply was acted on, and how to fit the next one."""
    nothing = not (reply.get('text') or '').strip() and not reply.get('tool_calls')
    from .native_refresh import HOST  # the prefix that keeps this out of the user's words (consent answers)
    return (HOST+'Your last reply was cut by this route\'s output limit ('+str(cap)+' tokens, thinking '
            'included) before it finished. '+('It produced no text and no tool call: the whole allowance went to '
            'thinking. ' if nothing else 'Nothing in it was acted on, no tool call included. ')+
            'Continue the task from where you were, with no recap. Think less before acting and keep each reply '
            'small: one tool call at a time, and a large edit or answer split into several smaller ones.')


def recover(session, control, journal, outcome, run, cap):
    """Run on while the outcome is a reply the output cap cut, until ATTEMPTS cuts came in a row; returns the last
    outcome. A complete reply between two cuts starts the count again: the session was getting somewhere."""
    streak, after = 0, None
    while outcome.status == 'blocked' and outcome.reason == 'PARTIAL_REPLY' and not control.should_stop():
        returned = cut(journal)
        if returned is None:
            break
        if after is not None and any(e['kind'] == 'model_returned' and e['reply'].get('complete')
                                     for e in (r.event() for r in journal.snapshot().records[after:])):
            streak = 0
        if streak >= ATTEMPTS:
            break
        streak, after = streak+1, len(journal.snapshot().records)
        try:
            session.continue_with(control, message_id='output-limit-'+returned['call_id'].replace('/', '-'),
                                  text=message(returned['reply'], cap), interrupts_turn=True)
        except ValueError:
            break  # no calls left, or the Runtime refuses this continuation: the cut stands
        outcome = run()
    return outcome
