"""Consultative, fail-closed assessment of a proposed action."""

from dataclasses import dataclass
import math
import time

from compute_plane.action_assessment import AssessmentRequest, AssessmentObservation


@dataclass(frozen=True)
class ActionPolicy:
    mode: str = "tare"
    review_required: bool = True
    allow_routine: bool = False
    fallback_on_unavailable: bool = False
    max_assessments: int = 3
    assessment_seconds: float = 30.0
    output_tokens: int = 256
    external_execution_review: bool = False
    pause_on_insufficient: bool = False

    def __post_init__(self):
        if self.mode not in ("tare", "codex_native", "both_required"):
            raise ValueError("unsupported assessment mode")
        if type(self.external_execution_review) is not bool or type(self.pause_on_insufficient) is not bool:
            raise ValueError('execution-review and insufficient-context switches must be bools')
        if self.external_execution_review and self.mode == 'both_required':
            raise ValueError('external execution review is separate from standalone both-required assessments')
        if not isinstance(self.review_required, bool) or not isinstance(self.allow_routine, bool) or not isinstance(self.fallback_on_unavailable, bool):
            raise ValueError("policy switches must be bools")
        if isinstance(self.max_assessments, bool) or not isinstance(self.max_assessments, int) or self.max_assessments <= 0:
            raise ValueError("max_assessments must be a positive integer")
        if isinstance(self.assessment_seconds, bool) or not math.isfinite(self.assessment_seconds) or self.assessment_seconds <= 0:
            raise ValueError("assessment_seconds must be finite and positive")
        if isinstance(self.output_tokens, bool) or not isinstance(self.output_tokens, int) or self.output_tokens <= 0:
            raise ValueError("output_tokens must be a positive integer")


@dataclass(frozen=True)
class ActionDecision:
    allowed: bool
    reason: str
    observations: tuple = ()
    authority: str = "NONE"
    execution_review_required: bool = False


class ActionReviewBroker:
    def __init__(self, policy, classifier, reviewers, deterministic_check, reserve_assessment, clock=time.monotonic):
        self.policy = policy
        self.classifier = classifier
        self.reviewers = reviewers
        self.deterministic_check = deterministic_check
        self.reserve_assessment = reserve_assessment
        self.clock = clock

    def assess(self, action, public_context, control):
        observations = []
        count = 0

        def done(allowed, reason, execution_review=False):
            return ActionDecision(bool(allowed), reason, tuple(observations), "NONE", bool(execution_review))

        def live():
            try:
                binding = control.binding
                return (not control.should_stop() and self.clock() < action.deadline
                        and control.remaining_seconds() > 0
                        and binding.work_id == action.work_id
                        and binding.binding_id == action.binding_id
                        and binding.fence == action.fence_seq)
            except Exception:
                return False

        if not live():
            return done(False, "invalid, stale, cancelled, or expired execution control")
        try:
            if self.deterministic_check(action, control) is not True:
                return done(False, "deterministic check denied action")
        except Exception:
            return done(False, "deterministic check failed")
        if not live():
            return done(False, "execution control changed before assessment")

        def invoke(stage, backend, port):
            nonlocal count
            if count >= self.policy.max_assessments or not live():
                return None
            try:
                deadline = min(action.deadline, control.deadline_monotonic, self.clock() + self.policy.assessment_seconds)
                request = AssessmentRequest(action, stage, backend, public_context, self.policy.output_tokens, deadline)
                if self.reserve_assessment(stage, backend, control) is not True:
                    return None
                count += 1
                if not live() or self.clock() >= deadline:
                    return None
                result = port.assess(request, control)
                if (not isinstance(result, AssessmentObservation) or result.authority != 'NONE'
                        or not live() or self.clock() >= deadline or not result.matches(request)):
                    return None
                observations.append(result)
                return result
            except Exception:
                return None

        triage_port = self.classifier
        triage = invoke("classifier", "tare", triage_port)
        if triage is None or triage.status != "OBSERVED":
            return done(False, "classification unavailable or invalid")
        if triage.verdict == "DENY_RECOMMENDED":
            return done(False, "classifier recommends denial")
        if triage.verdict not in ("ROUTINE_ELIGIBLE", "REVIEW_REQUIRED", "INSUFFICIENT_EVIDENCE"):
            return done(False, "classifier returned an invalid verdict")
        if triage.verdict == "INSUFFICIENT_EVIDENCE" and self.policy.pause_on_insufficient:
            return done(False, "classification needs additional operator context")
        if triage.verdict == "ROUTINE_ELIGIBLE" and self.policy.allow_routine and not self.policy.review_required:
            return done(True, "routine action allowed by configured policy")

        if self.policy.external_execution_review and self.policy.mode == "codex_native":
            return done(True, "native execution review required before effect", True)
        if self.policy.mode == "both_required":
            backends = ("tare", "codex_native")
        else:
            backends = (self.policy.mode,)
        approvals = []
        for backend in backends:
            port = self.reviewers.get(backend)
            if port is None:
                return done(False, f"{backend} reviewer unavailable")
            result = invoke("reviewer", backend, port)
            if result is None:
                return done(False, f"{backend} assessment failed or budget/control unavailable")
            if result.status == "UNAVAILABLE" and backend == "codex_native" and self.policy.mode == "codex_native" and self.policy.fallback_on_unavailable:
                fallback = self.reviewers.get("tare")
                if fallback is None:
                    return done(False, "fallback reviewer unavailable")
                result = invoke("reviewer", "tare", fallback)
                if result is None:
                    return done(False, "fallback assessment failed or budget/control unavailable")
            if result.status != "OBSERVED" or result.verdict != "APPROVE_RECOMMENDED":
                return done(False, f"{backend} did not provide an observed approval")
            approvals.append(result)
        return done(bool(approvals), "required reviewer approval observed" if approvals else "no reviewer approval",
                    self.policy.external_execution_review and bool(approvals))
