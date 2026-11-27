"""OS composition of external attachment custody for direct conversations."""
from pathlib import Path


def require_support():
    try:
        from tare_tools_agent_runtime import AttachmentRef, NativeMessage
        from compute_plane.native_attachments import NativeAttachmentStore
    except ImportError:
        raise ValueError('Attachment support requires the updated Runtime and Kernel together.') from None


def read_input(path):
    from .native_agent import read_json
    require_support()
    value = read_json(path, 16384)
    if (set(value) != {'schema', 'root', 'attachments'}
            or value['schema'] != 'tare.tools/native-attachment-input/1'
            or not isinstance(value['root'], str) or not Path(value['root']).is_absolute()):
        raise ValueError('Invalid attachment input metadata.')
    refs = references(value['attachments'])
    if not refs:
        raise ValueError('Attachment input must contain at least one snapshot.')
    return refs, Path(value['root'])


def write_input(directory, source_root, refs):
    from uuid import uuid4
    from compute_plane.native_journal import regular, save_json
    from compute_plane.native_attachments import NativeAttachmentStore
    require_support()
    rows = records(refs)
    if not rows:
        raise ValueError('Attachment input must contain at least one snapshot.')
    source = NativeAttachmentStore(source_root)
    for ref in refs:
        source.load(ref)
    directory = regular(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory/(uuid4().hex+'.json')
    save_json(path, {'schema':'tare.tools/native-attachment-input/1',
                     'root':str(source.root.absolute()), 'attachments':rows})
    return path


def records(refs):
    if not isinstance(refs, tuple):
        raise TypeError('attachments must be an immutable tuple')
    if not refs:
        return []
    from tare_tools_agent_runtime.native_content import attachment_records
    return attachment_records(refs)


def references(rows):
    if not isinstance(rows, list):
        raise ValueError('attachment records must be a list')
    if not rows:
        return ()
    from tare_tools_agent_runtime.native_content import attachments_from_records
    return attachments_from_records(rows)


def prepare(directory, refs, *, source_root=None):
    """Copy verified draft snapshots into this session before recording input."""
    from compute_plane.native_attachments import NativeAttachmentStore
    from tare_tools_agent_runtime.native_content import validate_attachments
    validate_attachments(refs)
    destination = NativeAttachmentStore(Path(directory)/'attachments')
    source = NativeAttachmentStore(source_root) if source_root is not None else destination
    for ref in refs:
        data = source.load(ref)
        if source_root is not None:
            destination.put_bytes(ref.name, data, ref.media_type)
    return refs


def describe_for_blind(refs, source_root, deployment, text, *, custody=None, on_activity=None):
    """Images the deployment cannot see are described by the turn engine's vision routes instead of sent:
    (the references it still receives, its text with the description appended within the 16 KiB bound).
    When `custody` is given, the original image snapshots are still preserved under `<custody>/attachments/` for
    multimodal inspection; when `on_activity` is given, vision progress is emitted to the live activity stream."""
    from compute_plane.native_attachments import IMAGE_TYPES, NativeAttachmentStore
    seen = set((deployment or {}).get('native_image_media_types', []))
    blind = [ref for ref in refs if ref.media_type in IMAGE_TYPES and ref.media_type not in seen]
    if not blind or source_root is None:
        return refs, text
    import tempfile
    from uuid import uuid4
    from .console_entry import ASSISTANT_CONTEXT
    from .native_activity import activity_view
    from .turn_engine import clip, describe_files
    store = NativeAttachmentStore(source_root)
    destination = NativeAttachmentStore(Path(custody)/'attachments') if custody is not None else None
    preserved_notes = []
    with tempfile.TemporaryDirectory(prefix='tare-blind-images-') as folder:
        paths = []
        for index, ref in enumerate(blind[:4]):
            data = store.load(ref)
            if destination is not None:
                destination.put_bytes(ref.name, data, ref.media_type)
                preserved_notes.append(f'{ref.name} (repository="attachments", path="{ref.sha256}")')
            paths.append(Path(folder)/f"image-{index + 1}.{ref.media_type.split('/')[1]}")
            paths[-1].write_bytes(data)
        names_label = ', '.join(ref.name for ref in blind[:4])
        activity_id = f'vision-{uuid4().hex[:12]}'

        def _vision_event(ev):
            if on_activity is None:
                return
            route = str(ev.get('route') or 'vision')
            target = f'{names_label} ({route})' if names_label else f'{len(paths)} image(s) ({route})'
            actor = None
            dep = ev.get('deployment')
            if isinstance(dep, dict):
                try:
                    from .execution_team import actor as build_actor
                    built = build_actor({'deployment': dep, 'config': {'role': 'vision'}})
                    if isinstance(built, dict) and all(isinstance(built.get(k), str) and built.get(k)
                                                       for k in ('role', 'model', 'reasoning', 'connection', 'route')):
                        actor = {k: built[k] for k in ('role', 'model', 'reasoning', 'connection', 'route')}
                except Exception:
                    actor = None
            if ev.get('phase') == 'start':
                state, detail = 'running', f'Inspecting {names_label or "attached image(s)"} with vision route {route}.'
            elif ev.get('ok'):
                state, detail = 'succeeded', f'Vision description received from {route}.'
            else:
                reason = str(ev.get('reason') or '').strip()
                state, detail = 'failed', f'Vision route {route} failed' + (f': {reason}' if reason else '.')
            row = activity_view({
                'kind': 'tool-activity',
                'id': activity_id,
                'operation': 'inspect_image',
                'state': state,
                'target': target,
                'detail': detail,
                **({'duration_ms': ev['duration_ms']} if ev.get('duration_ms') is not None else {}),
                **({'actor': actor} if actor is not None else {}),
            })
            try:
                on_activity({'channel': 'activity', 'activity': row})
            except Exception:
                pass

        import inspect
        question = text or ''
        if question.startswith(ASSISTANT_CONTEXT):
            question = question[len(ASSISTANT_CONTEXT):]
        params = inspect.signature(describe_files).parameters
        has_varkw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
        kwargs = {}
        if has_varkw or 'question' in params:
            kwargs['question'] = question
        if has_varkw or 'on_vision' in params:
            kwargs['on_vision'] = _vision_event
        note = describe_files(paths, lambda progress: None, **kwargs)
    if preserved_notes:
        note = note + f"\n[Original image(s) preserved in session attachments: {', '.join(preserved_notes)}]"
    text = text or ''
    return tuple(ref for ref in refs if ref not in blind), text + clip(note, max(0, 16384 - len(text.encode('utf-8')) - 64))


def model_options(directory, deployment):
    from compute_plane.native_attachments import IMAGE_TYPES, NativeAttachmentStore
    image_types = deployment.get('native_image_media_types', [])
    if (not isinstance(image_types, list) or any(not isinstance(t, str) or t not in IMAGE_TYPES for t in image_types)
            or len(set(image_types)) != len(image_types)):
        raise ValueError('native_image_media_types_requires_unique_supported_types')
    return {'attachment_loader': NativeAttachmentStore(Path(directory)/'attachments').load,
            'image_media_types': tuple(image_types)}
