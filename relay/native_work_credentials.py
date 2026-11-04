"""Local Work recovery credentials in the existing OS keyring; never in receipts."""
import json
import secrets

SERVICE = 'tare.tools.work-recovery'


def read(binding_id):
    import keyring
    try:
        raw = keyring.get_password(SERVICE, binding_id)
        if raw is None:
            raise ValueError('NATIVE_WORK_RECOVERY_CREDENTIAL_MISSING')
        return json.loads(raw)
    except keyring.errors.KeyringError:
        raise ValueError('NATIVE_WORK_RECOVERY_KEYRING_UNAVAILABLE') from None


def write(binding_id, value):
    import keyring
    try:
        keyring.set_password(SERVICE, binding_id, json.dumps(value))
    except keyring.errors.KeyringError:
        raise ValueError('NATIVE_WORK_RECOVERY_KEYRING_UNAVAILABLE') from None


def discard(binding_id):
    import keyring
    try:
        if keyring.get_password(SERVICE, binding_id) is not None:
            keyring.delete_password(SERVICE, binding_id)
    except keyring.errors.KeyringError:
        # Accepted delivery does not become a failed landing due to vault cleanup.
        return False
    return True


def recover(gate, identity):
    from tare_tools_agent_runtime import BindingRef
    value = read(identity['binding_id'])
    if not value.get('pending'):
        value['pending'] = secrets.token_urlsafe(32)
        write(identity['binding_id'], value)  # persist intent before the coordinator transaction
    binding = gate.recover(BindingRef(**identity, token=value['token']), successor_token=value['pending'])
    if binding is None:
        raise ValueError('NATIVE_WORK_RECOVERY_AUTHORITY_REFUSED')
    write(binding.binding_id, {'token': binding.token})
    return binding
