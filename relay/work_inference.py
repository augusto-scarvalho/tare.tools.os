"""Optional Work/call attribution at durable request creation, never by timestamps."""
from contextlib import contextmanager
from contextvars import ContextVar

_current = ContextVar('work_inference', default=None)


@contextmanager
def scope():
    token = _current.set(None)
    try:
        yield
    finally:
        _current.reset(token)


def bind(receipt, path):
    receipt['inference_calls'] = []
    receipt['inference_observation_failures'] = 0
    _current.set((receipt, path))


def wrap(journal, session_id, stage):
    owner = _current.get()
    return RequestJournal(journal, owner, session_id, stage) if owner else journal


class RequestJournal:
    """Record only newly appended model/summary intents after the owner journal."""
    def __init__(self, journal, owner, session_id, stage):
        self.journal, self.owner = journal, owner
        self.session_id, self.stage = session_id, stage

    def snapshot(self):
        return self.journal.snapshot()

    def project(self, spec):
        return self.journal.project(spec)

    def append(self, event):
        record = self.journal.append(event)
        if event.get('kind') in {'model_requested', 'context_summary_requested'}:
            receipt, path = self.owner
            try:
                from compute_plane.native_journal import save_json
                claim = {'session_id': self.session_id, 'call_id': event['call_id'], 'stage': self.stage}
                if claim not in receipt['inference_calls']:
                    if len(receipt['inference_calls']) >= 8192:
                        raise ValueError('work_inference_receipt_limit')
                    receipt['inference_calls'].append(claim)
                save_json(path, receipt)
            except Exception:
                # Optional telemetry cannot convert a dispatched intent into a retry.
                receipt['inference_observation_failures'] += 1
        return record
