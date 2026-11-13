"""Mediate command actions: assessments advise; only owner authorization and guard execute."""
from dataclasses import replace

from compute_plane.action_control import ActionRefused, ActionSnapshot
from compute_plane.command_mediation import CommandFacts
from relay.action_control import ActionDecision


class CommandActionMediator:
    def __init__(self, broker, snapshot_factory, context_factory, authorization_factory, guard, control_factory,
                 *, result_handler=None, reviewed_execution=None):
        self.broker = broker
        self.snapshot_factory = snapshot_factory
        self.context_factory = context_factory
        self.authorization_factory = authorization_factory
        self.guard = guard
        self.control_factory = control_factory
        if result_handler is not None and not callable(result_handler):
            raise ValueError('command_result_handler_must_be_callable')
        self.result_handler = result_handler
        if reviewed_execution is not None and not callable(getattr(reviewed_execution, 'run', None)):
            raise ValueError('command_reviewed_execution_run_must_be_callable')
        self.reviewed_execution = reviewed_execution

    @staticmethod
    def _validate(facts, action):
        if not isinstance(facts, CommandFacts) or not isinstance(action, ActionSnapshot):
            raise ActionRefused("invalid command facts or action snapshot")
        fields = ("operation", "repository", "cwd", "environment_sha256",
                  "grant_sha256", "argv", "payload_json")
        if any(getattr(action, name, None) != getattr(facts, name, None) for name in fields):
            raise ActionRefused("action snapshot does not match command facts")
        for name in ("session_id", "effect_id", "work_id", "binding_id"):
            value = getattr(facts, name, None)
            if value and getattr(action, name, None) != value:
                raise ActionRefused("action snapshot identity does not match command facts")
        fence = getattr(facts, "fence_seq", None)
        if facts.binding_id and (type(fence) is not int or getattr(action, "fence_seq", None) != fence):
            raise ActionRefused("action snapshot fence does not match command facts")

    def execute(self, facts, effect):
        control = self.control_factory()

        def refresh():
            action = self.snapshot_factory(facts, control)
            self._validate(facts, action)
            return action

        action = refresh()
        context = self.context_factory(action, control)
        decision = self.broker.assess(action, context, control)
        if (not isinstance(decision, ActionDecision) or decision.allowed is not True
                or decision.authority != "NONE"):
            detail = ": " + decision.reason if isinstance(decision, ActionDecision) else ""
            raise ActionRefused("action assessment denied or invalid" + detail)
        if type(decision.execution_review_required) is not bool:
            raise ActionRefused('invalid execution-review requirement')
        if decision.execution_review_required and self.reviewed_execution is None:
            raise ActionRefused('mandatory execution review is unavailable')
        # The assessment may have waited for the user's answer. They approved the command, never its deadline: take
        # the action again on a fresh control, where the host may give it a new deadline and nothing else.
        control = self.control_factory()
        approved, action = action, refresh()
        if replace(action, deadline=approved.deadline) != approved:
            raise ActionRefused('the command changed while it was assessed')
        authorization = self.authorization_factory(action, decision, control)
        def execute_effect():
            if self.reviewed_execution is None:
                result = effect()
            else:
                result = self.reviewed_execution.run(
                    action, authorization, control, effect, public_context=context,
                    before_effect=lambda: self.guard.revalidate(action, authorization, control, refresh))
            return self.result_handler(action, result, control) if self.result_handler is not None else result
        return self.guard.run(action, authorization, control, refresh=refresh, effect=execute_effect)
