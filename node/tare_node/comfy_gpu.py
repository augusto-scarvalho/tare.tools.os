"""Coordinate the managed ComfyUI executor with the local text gateway.

The lease covers execution and GPU cleanup. A failed cleanup retains ownership
until the ComfyUI process exits; elapsed time is never proof of released VRAM.
"""
from __future__ import annotations

import functools
import gc
import json
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def yield_text_backend(gateway, nonce):
    parsed = urlsplit(gateway)
    if (parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', '::1'}
            or parsed.username or parsed.password or parsed.path not in {'', '/'}
            or parsed.query or parsed.fragment):
        raise ValueError('GPU coordination requires a loopback HTTP gateway.')
    request = Request(gateway.rstrip('/') + '/internal/gpu/yield',
                      data=json.dumps({'nonce': nonce}).encode(),
                      headers={'Content-Type': 'application/json'})
    with urlopen(request, timeout=110) as response:
        raw = response.read(4097)
        if len(raw) > 4096:
            raise ValueError('GPU release response exceeded its limit.')
        receipt = json.loads(raw)
    if receipt.get('status') != 'released' or receipt.get('backend_pid') is not None:
        raise ValueError('Text backend release was not confirmed.')


def release_models(memory, executor=None):
    """Discard executor GPU caches while preserving the completed job history."""
    memory.synchronize()
    memory.unload_all_models()
    if executor is not None:
        saved = {name: getattr(executor, name) for name in
                 ('history_result', 'status_messages', 'success')}
        executor.reset()
        for name, value in saved.items():
            setattr(executor, name, value)
    gc.collect()
    memory.unload_all_models()
    memory.synchronize()
    memory.soft_empty_cache(force=True)
    if memory.current_loaded_models:
        raise RuntimeError('ComfyUI still reports resident models after cleanup.')


class ComfyCoordinator:
    def __init__(self, lease, gateway):
        self.lease = lease
        self.gateway = gateway
        self.quarantine = []
        self.execute_wrapper = None

    def status(self):
        return {'role': 'tare-comfy-gpu-coordinator',
                'cleanup_blocked': bool(self.quarantine),
                'gpu': self.lease.status()}

    def _report(self, executor, prompt_id, message, *, interrupted=False):
        executor.success = False
        if interrupted:
            executor.add_message('execution_interrupted', {
                'prompt_id': prompt_id, 'node_id': None, 'node_type': 'GpuCoordination',
                'executed': []}, broadcast=True)
        else:
            executor.add_message('execution_error', {
                'prompt_id': prompt_id, 'node_id': None, 'node_type': 'GpuCoordination',
                'executed': [], 'exception_message': message,
                'exception_type': 'GpuCoordinationError', 'traceback': [],
                'current_inputs': {}, 'current_outputs': []}, broadcast=True)

    def install_executor(self, execution, memory):
        original = execution.PromptExecutor.execute
        if self.execute_wrapper is not None:
            raise RuntimeError('GPU executor was already installed.')

        @functools.wraps(original)
        def execute(executor, prompt, prompt_id, extra_data=None, execute_outputs=None):
            extra_data = {} if extra_data is None else extra_data
            execute_outputs = [] if execute_outputs is None else execute_outputs
            executor.status_messages = []
            executor.history_result = {'outputs': {}, 'meta': {}}
            executor.success = False
            executor.server.client_id = extra_data.get('client_id')
            if self.quarantine:
                self._report(executor, prompt_id,
                             'GPU cleanup is unconfirmed. Restart the managed ComfyUI service.')
                return
            memory.interrupt_current_processing(False)
            context = self.lease.hold('image', str(prompt_id),
                                      cancelled=memory.processing_interrupted)
            acquired = False
            try:
                receipt = context.__enter__()
                acquired = True
                yield_text_backend(self.gateway, receipt['nonce'])
                if memory.processing_interrupted():
                    raise InterruptedError('Image job cancelled while waiting for the GPU.')
                return original(executor, prompt, prompt_id, extra_data, execute_outputs)
            except (InterruptedError, memory.InterruptProcessingException):
                self._report(executor, prompt_id, '', interrupted=True)
            except Exception as exc:
                self._report(executor, prompt_id,
                             f'GPU coordination failed ({type(exc).__name__}). Check gateway health and retry.')
            finally:
                if acquired:
                    try:
                        release_models(memory, executor)
                    except BaseException:
                        # Keep the generator/context alive: its finalizer must not
                        # release ownership while model cleanup is unconfirmed.
                        self.quarantine.append(context)
                        self._report(executor, prompt_id,
                                     'GPU cleanup is unconfirmed. Restart the managed ComfyUI service.')
                    else:
                        context.__exit__(None, None, None)

        self.execute_wrapper = execute
        execution.PromptExecutor.execute = execute

    def verify_executor(self, execution):
        if execution.PromptExecutor.execute is not self.execute_wrapper:
            raise RuntimeError('The coordinated ComfyUI executor was replaced during startup.')
