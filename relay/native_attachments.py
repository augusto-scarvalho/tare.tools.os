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


def describe_for_blind(refs, source_root, deployment, text):
    """Images the deployment cannot see are described by the turn engine's vision routes instead of sent:
    (the references it still receives, its text with the description appended within the 16 KiB bound)."""
    from compute_plane.native_attachments import IMAGE_TYPES, NativeAttachmentStore
    seen = set((deployment or {}).get('native_image_media_types', []))
    blind = [ref for ref in refs if ref.media_type in IMAGE_TYPES and ref.media_type not in seen]
    if not blind or source_root is None:
        return refs, text
    import tempfile
    from .turn_engine import clip, describe_files
    store = NativeAttachmentStore(source_root)
    with tempfile.TemporaryDirectory(prefix='tare-blind-images-') as folder:
        paths = []
        for index, ref in enumerate(blind[:4]):
            paths.append(Path(folder)/f"image-{index + 1}.{ref.media_type.split('/')[1]}")
            paths[-1].write_bytes(store.load(ref))
        note = describe_files(paths, lambda progress: None)
    text = text or ''
    return tuple(ref for ref in refs if ref not in blind), text + clip(note, 16384 - len(text.encode('utf-8')) - 64)


def model_options(directory, deployment):
    from compute_plane.native_attachments import IMAGE_TYPES, NativeAttachmentStore
    image_types = deployment.get('native_image_media_types', [])
    if (not isinstance(image_types, list) or any(not isinstance(t, str) or t not in IMAGE_TYPES for t in image_types)
            or len(set(image_types)) != len(image_types)):
        raise ValueError('native_image_media_types_requires_unique_supported_types')
    return {'attachment_loader': NativeAttachmentStore(Path(directory)/'attachments').load,
            'image_media_types': tuple(image_types)}
