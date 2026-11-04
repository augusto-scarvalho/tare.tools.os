#!/usr/bin/env python3
"""Run the installed ComfyUI normally, with mandatory shared GPU admission."""
from __future__ import annotations

import argparse
import hashlib
import importlib.abc
import importlib.machinery
import json
from pathlib import Path
import runpy
import sys
from uuid import uuid4

from tare_node.comfy_gpu import ComfyCoordinator, release_models, yield_text_backend
from tare_node.gpu_lease import SharedGpuLease

_STARTUP_QUARANTINE = []

class AfterImport(importlib.abc.MetaPathFinder):
    def __init__(self, root, callbacks):
        self.root = root
        self.callbacks = callbacks

    def find_spec(self, fullname, path=None, target=None):
        callback = self.callbacks.get(fullname)
        if callback is None:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or Path(spec.origin).resolve() != self.root / (fullname + '.py'):
            raise ImportError('Unexpected ComfyUI module origin: ' + fullname)
        original = spec.loader

        class Loader(importlib.abc.Loader):
            def create_module(self, spec):
                return original.create_module(spec)

            def exec_module(self, module):
                original.exec_module(module)
                callback(module)

        spec.loader = Loader()
        return spec


def verify_sources(root, manifest):
    expected = json.loads(manifest.read_text())
    paths = {'main.py', 'execution.py', 'server.py', 'comfy/model_management.py'}
    if set(expected) != paths:
        raise ValueError('A complete ComfyUI compatibility manifest is required.')
    for name, digest in expected.items():
        path = root / name
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError('ComfyUI changed; review GPU integration before starting: ' + name)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--comfy-root', type=Path, required=True)
    parser.add_argument('--gateway', default='http://127.0.0.1:8080')
    parser.add_argument('--gpu-lock', required=True)
    parser.add_argument('--gpu-wait', type=float, default=600)
    parser.add_argument('--source-manifest', type=Path, required=True)
    parser.add_argument('--history-snapshot', type=Path)
    parser.add_argument('comfy_args', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    root = args.comfy_root.resolve(strict=True)
    verify_sources(root, args.source_manifest)
    history = None
    if args.history_snapshot and args.history_snapshot.exists():
        raw = args.history_snapshot.read_bytes()
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError('ComfyUI history snapshot exceeded its limit.')
        history = json.loads(raw)
        if not isinstance(history, dict):
            raise ValueError('ComfyUI history snapshot must be an object.')
    coordinator = ComfyCoordinator(SharedGpuLease(args.gpu_lock, timeout=args.gpu_wait), args.gateway)
    startup = coordinator.lease.hold('image', 'comfy-startup-' + uuid4().hex)
    receipt = startup.__enter__()
    released = False
    hook = None
    try:
        yield_text_backend(args.gateway, receipt['nonce'])

        def install_execution(module):
            coordinator.install_executor(module, sys.modules['comfy.model_management'])

        def install_server(module):
            constructor = module.PromptServer.__init__
            start = module.PromptServer.start_multi_address

            def init(server, *positional, **keywords):
                constructor(server, *positional, **keywords)
                if history is not None:
                    with server.prompt_queue.mutex:
                        server.prompt_queue.history.update(history)

                async def status(request):
                    from aiohttp import web
                    return web.json_response(coordinator.status())

                server.routes.get('/tare/gpu/status')(status)

            async def start_managed(server, *positional, **keywords):
                nonlocal released
                coordinator.verify_executor(sys.modules['execution'])
                if not released:
                    release_models(sys.modules['comfy.model_management'])
                    startup.__exit__(None, None, None)
                    released = True
                result = await start(server, *positional, **keywords)
                if history is not None and args.history_snapshot.exists():
                    # Consume only after a successful bind. Keep the private
                    # backup, without re-importing deleted history on restart.
                    args.history_snapshot.rename(args.history_snapshot.with_suffix('.imported.json'))
                return result

            module.PromptServer.__init__ = init
            module.PromptServer.start_multi_address = start_managed

        hook = AfterImport(root, {'execution': install_execution, 'server': install_server})
        sys.meta_path.insert(0, hook)
        sys.path.insert(0, str(root))
        arguments = args.comfy_args
        if arguments[:1] == ['--']:
            arguments = arguments[1:]
        sys.argv = [str(root / 'main.py'), *arguments]
        runpy.run_path(str(root / 'main.py'), run_name='__main__')
    finally:
        if hook in sys.meta_path:
            sys.meta_path.remove(hook)
        if not released:
            # Startup may have allocated VRAM before failing. Keep the lease
            # alive until process exit instead of promising successful cleanup.
            coordinator.quarantine.append(startup)
            _STARTUP_QUARANTINE.append(coordinator)


if __name__ == '__main__':
    main()
