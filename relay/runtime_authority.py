"""OS coordinator adapter, independent of the tactical executor realization."""
from __future__ import annotations
from typing import Any
from contextlib import contextmanager

def _binding_identity(binding: Any) -> dict[str, Any]:
    return {
        "work_id": binding.work_id,
        "binding_id": binding.binding_id,
        "owner_id": binding.owner_id,
        "fence_seq": binding.fence,
        "token": binding.token,
    }


def _binding_ref(raw: dict[str, Any]) -> Any:
    from tare_tools_agent_runtime import BindingRef

    return BindingRef(
        work_id=raw["work_id"],
        binding_id=raw["binding_id"],
        owner_id=raw["owner_id"],
        fence=raw["fence_seq"],
        token=raw["token"],
    )


class CoordinatorAuthorityGate:
    """Structural adapter from runtime AuthorityGate to CoordinatorDB."""

    def __init__(self, coordinator_db: Any) -> None:
        self._db = coordinator_db

    def acquire(
        self,
        work_id: str,
        owner_id: str,
        *,
        ttl_s: int | float = 120,
        resume_token: str | None = None,
    ) -> Any | None:
        raw = self._db.acquire_agent_runtime_binding(
            work_id, owner_id, ttl_s=ttl_s, resume_token=resume_token
        )
        return _binding_ref(raw) if raw else None

    def is_current(self, binding: Any) -> bool:
        return self._db.is_agent_runtime_binding_current(**_binding_identity(binding))

    def renew(self, binding: Any, *, ttl_s: int | float = 120) -> bool:
        return self._db.renew_agent_runtime_binding(
            **_binding_identity(binding), ttl_s=ttl_s
        )

    @contextmanager
    def maintain(self, binding, cancellation, *, ttl_s=120, on_renew=None):
        """Renew only the held fence; existing live checks stop on loss, never reacquire."""
        from threading import Event, Thread
        stop = Event()
        def renew():
            try:
                current = not cancellation.is_cancelled() and self.renew(binding, ttl_s=ttl_s)
                if current and on_renew is not None and not on_renew():
                    cancellation.cancel()
                    current = False
            except Exception:
                cancellation.cancel()
                current = False
            return current
        if not renew():
            raise ValueError('NATIVE_WORK_BINDING_LOST')
        def heartbeat():
            while not stop.wait(ttl_s / 3):
                if not renew():
                    return
        worker = Thread(target=heartbeat, name='native-work-lease', daemon=True)
        worker.start()
        try:
            yield
        finally:
            stop.set()
            worker.join()

    def revoke(self, binding: Any, *, reason: str) -> bool:
        return self._db.revoke_agent_runtime_binding(
            **_binding_identity(binding), reason=reason
        )

    def pause(self, binding):
        return self._db.pause_agent_runtime_binding(**_binding_identity(binding))

    def recover(self, binding, *, successor_token, ttl_s=120):
        raw = self._db.recover_agent_runtime_binding(**_binding_identity(binding),
            successor_token=successor_token, ttl_s=ttl_s)
        return _binding_ref(raw) if raw else None

    def handoff(
        self,
        binding: Any,
        new_owner_id: str,
        *,
        ttl_s: int | float = 120,
    ) -> Any | None:
        raw = self._db.handoff_agent_runtime_binding(
            **_binding_identity(binding), new_owner_id=new_owner_id, ttl_s=ttl_s
        )
        return _binding_ref(raw) if raw else None

    def submit_terminal_claim(self, claim: Any) -> bool:
        status = claim.status.value if hasattr(claim.status, "value") else str(claim.status)
        return self._db.submit_agent_runtime_terminal_claim(
            **_binding_identity(claim.binding),
            status=status,
            output=claim.output,
            turns=claim.turns,
            attempts=claim.attempts,
        )

    def handoff_receipt(self, previous: dict[str, Any], successor: Any) -> dict[str, Any] | None:
        return self._db.agent_runtime_handoff_receipt(previous=previous, **_binding_identity(successor))

    def accepted_claim(self, claim: Any) -> dict[str, Any] | None:
        identity = {key: getattr(claim.binding, key) for key in ('work_id', 'binding_id', 'owner_id')}
        identity['fence_seq'] = claim.binding.fence  # historical receipt needs no bearer token
        status = claim.status.value if hasattr(claim.status, 'value') else str(claim.status)
        return self._db.accepted_agent_runtime_claim(**identity, status=status,
            output=claim.output, turns=claim.turns, attempts=claim.attempts)
