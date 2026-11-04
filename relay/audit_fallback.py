"""Qualified OpenRouter routes for the Claude audit host, independent of Aider."""

# Exact public model IDs qualified against OpenRouter's model catalogue and CLI.
# Transport aliases preserve the auditor identity; they never select another model.
OPENROUTER_MODELS = {
    'claude-fable-5-1': 'anthropic/claude-fable-5.1',
    'claude-fable-5': 'anthropic/claude-fable-5',
    'openrouter/z-ai/glm-5.3': 'z-ai/glm-5.3',
}


def openrouter_driver(command, model):
    from compute_plane.audit_driver import ClaudeAuditDriver
    from .native_generation import vault_secret
    import os
    if model not in OPENROUTER_MODELS:
        raise ValueError('audit_gateway_model_not_qualified')
    token = os.environ.get('OPENROUTER_API_KEY') or vault_secret('OPENROUTER_API_KEY')
    if not token:
        raise ValueError('OPENROUTER_API_KEY is required for OpenRouter')
    # Retain qualified receipt IDs without consulting the retired Aider table.
    gateway = {'provider': 'OpenRouter',
        'deployment_id': 'chair-glm-openrouter' if model == 'openrouter/z-ai/glm-5.3' else 'chair-anthropic-openrouter',
        'quota_domain': 'openrouter-credits', 'base_url': 'https://openrouter.ai/api',
        'model': model, 'wire_model': OPENROUTER_MODELS[model]}
    return ClaudeAuditDriver(command, gateway=gateway, gateway_token=token)


def supports_packet_identity(driver, auditor):
    """The CLI is the tool executor; the streamed model remains the auditor."""
    if auditor['runtime_key'] == 'claude':
        return True
    gateway = getattr(driver, 'gateway', None)
    return (auditor['runtime_key'] in {'claude-gateway', 'aider-openrouter'}
            and auditor['runtime_model'] == 'openrouter/z-ai/glm-5.3'
            and gateway is not None and gateway.get('provider') == 'OpenRouter'
            and gateway.get('deployment_id') == 'chair-glm-openrouter'
            and gateway.get('model') == auditor['runtime_model']
            and gateway.get('wire_model') == 'z-ai/glm-5.3')
