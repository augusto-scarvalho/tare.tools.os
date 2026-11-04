"""Comfy execution-boundary tests without importing torch or using a GPU."""
from contextlib import contextmanager
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tare_node import comfy_gpu


@pytest.fixture
def rig(monkeypatch):
    events = []

    class Lease:
        active = False
        cancel_wait = False

        @contextmanager
        def hold(self, kind, request_id, *, cancelled=None):
            assert kind == 'image'
            if self.cancel_wait:
                memory.interrupted = True
                assert cancelled()
                raise InterruptedError()
            self.active = True
            events.append('acquired')
            try:
                yield {'nonce': 'receipt'}
            finally:
                events.append('released')
                self.active = False

        def status(self):
            return {'active': self.active}

    class Memory:
        interrupted = False
        current_loaded_models = []
        fail_cleanup = False

        class InterruptProcessingException(BaseException):
            pass

        def interrupt_current_processing(self, flag):
            self.interrupted = flag

        def processing_interrupted(self):
            return self.interrupted

        def synchronize(self):
            assert lease.active
            events.append('sync')

        def unload_all_models(self):
            assert lease.active
            events.append('unload')
            if self.fail_cleanup:
                raise RuntimeError('fixture cleanup failed')

        def soft_empty_cache(self, force):
            assert lease.active and force
            events.append('empty')

    class Executor:
        fail = False

        def __init__(self):
            self.server = SimpleNamespace(client_id=None)
            self.reset()

        def reset(self):
            self.success = True
            self.status_messages = []
            self.history_result = {}
            events.append('reset')

        def add_message(self, kind, data, broadcast):
            self.status_messages.append((kind, data))

        def execute(self, prompt, prompt_id, extra_data, execute_outputs):
            assert lease.active and events.index('yield_text') < len(events)
            events.append('execute')
            if self.fail:
                raise RuntimeError('fixture node failed')
            self.success = True
            self.history_result = {'outputs': {'image': 'kept'}}
            self.add_message('execution_success', {'prompt_id': prompt_id}, False)

    lease, memory = Lease(), Memory()

    def yield_backend(url, nonce):
        assert lease.active and nonce == 'receipt'
        events.append('yield_text')

    monkeypatch.setattr(comfy_gpu, 'yield_text_backend', yield_backend)
    execution = SimpleNamespace(PromptExecutor=Executor)
    coordinator = comfy_gpu.ComfyCoordinator(lease, 'http://127.0.0.1:8080')
    coordinator.install_executor(execution, memory)
    return SimpleNamespace(events=events, lease=lease, memory=memory,
                           coordinator=coordinator, execution=execution, executor=Executor())


@pytest.mark.parametrize('origin', ['browser', 'api'])
def test_job_unloads_text_then_images_and_preserves_history(rig, origin):
    rig.executor.execute({}, origin, {'client_id': origin}, [])
    assert rig.executor.success
    assert rig.executor.history_result == {'outputs': {'image': 'kept'}}
    assert rig.executor.status_messages[-1][0] == 'execution_success'
    assert rig.events.index('yield_text') < rig.events.index('execute') < rig.events.index('unload')
    assert rig.events[-2:] == ['empty', 'released']
    assert not rig.lease.active


def test_node_failure_still_cleans_up_before_release(rig):
    rig.executor.fail = True
    rig.executor.execute({}, 'job')
    assert not rig.executor.success
    assert rig.executor.status_messages[-1][0] == 'execution_error'
    assert rig.events[-2:] == ['empty', 'released']


def test_cancel_while_waiting_does_not_execute_or_clear_cancellation(rig):
    rig.lease.cancel_wait = True
    rig.executor.execute({}, 'job')
    assert 'execute' not in rig.events and 'yield_text' not in rig.events
    assert rig.executor.status_messages[-1][0] == 'execution_interrupted'
    assert rig.memory.interrupted


def test_cleanup_failure_retains_lease_and_refuses_next_job(rig):
    rig.memory.fail_cleanup = True
    rig.executor.execute({}, 'one')
    assert rig.lease.active and not rig.executor.success
    assert rig.coordinator.status()['cleanup_blocked']
    assert rig.executor.history_result == {'outputs': {'image': 'kept'}}
    rig.executor.execute({}, 'two')
    assert rig.events.count('execute') == 1
    assert not rig.executor.success and rig.lease.active
    # Test teardown only: real quarantine lasts until the managed process exits.
    rig.coordinator.quarantine.pop().__exit__(None, None, None)


def test_changed_executor_cannot_silently_bypass_coordination(rig):
    rig.execution.PromptExecutor.execute = lambda *a: None
    with pytest.raises(RuntimeError, match='replaced'):
        rig.coordinator.verify_executor(rig.execution)


@pytest.mark.parametrize('gateway', ['http://gpu-node:8080', 'https://127.0.0.1',
                                     'http://user:pass@127.0.0.1', 'http://127.0.0.1/path'])
def test_internal_release_requires_loopback_gateway(gateway):
    with pytest.raises(ValueError, match='loopback'):
        comfy_gpu.yield_text_backend(gateway, 'nonce')
