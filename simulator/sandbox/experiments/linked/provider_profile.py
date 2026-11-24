"""Explicit Chat Completions profiles for provider migration.

No credentials, network requests, model substitutions, or capability discovery.
Legacy MaaS profiles remain unchanged; a new endpoint needs its own key binding.
"""
from copy import deepcopy
import math
from urllib.parse import urlsplit

VERSION = 'explicit-chat-completions-v1'
PROTOCOL = 'configured_chat_completions'
RESERVED = {'model', 'messages', 'stream', 'stream_options', 'max_tokens',
            'max_completion_tokens', 'temperature', 'api_key', 'authorization'}


def is_explicit(config):
    return (config.get('generation_policy') or {}).get('version') == VERSION


def validate(config):
    policy = config.get('generation_policy') or {}
    if policy.get('version') != VERSION or config.get('api_protocol') != PROTOCOL:
        raise ValueError('an explicit model profile and configured Chat Completions protocol are required together')
    endpoint = config.get('endpoint', '').rstrip('/')
    parsed = urlsplit(endpoint)
    loopback = parsed.hostname in {'127.0.0.1', '::1', 'localhost'}
    if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
            or (parsed.scheme != 'https' and not (parsed.scheme == 'http' and loopback))
            or parsed.path.endswith('/chat/completions')):
        raise ValueError('use an HTTPS base URL, or an explicitly bound loopback server; no credential URL or completion suffix')
    scope = config.get('credential_scope') or {}
    if scope.get('endpoint') != endpoint or not scope.get('credential_id'):
        raise ValueError('bind the new credential explicitly to this exact provider endpoint')
    tools = config.get('tool_models') or {}
    role = config.get('role_model')
    if (set(tools) not in ({'tool_a'}, {'tool_a', 'tool_b'})
            or len(set(tools.values())) != len(tools) or not role):
        raise ValueError('one fixed role model and one tool_a model, or an explicit legacy tool_a/tool_b pair, are required')
    models = policy.get('models') or {}
    selected = {role, *tools.values()}
    if set(models) != selected:
        raise ValueError('declare parameters for every selected model, with no hidden alternatives')
    prices = config.get('pricing_cny_per_million_tokens') or {}
    if set(prices) != selected or not config.get('pricing_evidence'):
        raise ValueError('declare priced input/output including reasoning and a pricing source for every model')
    margin = config.get('request_rounding_margin_cny', 0)
    if type(margin) not in (int, float) or not math.isfinite(margin) or not 0 <= margin <= 1:
        raise ValueError('request rounding margin must be a finite nonnegative CNY allowance')
    for model, settings in models.items():
        if not isinstance(model, str) or not model.strip() or not isinstance(settings, dict):
            raise ValueError('invalid model identity or settings')
        limit = settings.get('max_completion_tokens')
        window = settings.get('context_window_tokens')
        if type(limit) is not int or not 1 <= limit <= 32768 or type(window) is not int or window <= limit:
            raise ValueError('declare bounded completion and context-window sizes')
        if settings.get('completion_limit_field') not in {'max_tokens', 'max_completion_tokens'}:
            raise ValueError('declare the supported completion-limit parameter')
        if type(settings.get('thinking')) is not bool:
            raise ValueError('declare whether reasoning is enabled; never infer it from the model name')
        temperature = settings.get('temperature')
        if temperature is not None and (type(temperature) not in (int, float)
                                       or not math.isfinite(temperature) or not 0 <= temperature <= 2):
            raise ValueError('temperature must be null (omitted) or a finite value in [0,2]')
        params = settings.get('request_parameters', {})
        if not isinstance(params, dict) or RESERVED & {str(k).lower() for k in params}:
            raise ValueError('provider parameters cannot replace routing, messages, accounting limits or credentials')
        if not isinstance(settings.get('completion_limit_includes_reasoning'), bool):
            raise ValueError('record whether the completion limit includes reasoning tokens')
        if settings['thinking'] and not settings['completion_limit_includes_reasoning']:
            raise ValueError('reasoning must be included in the configured completion bound')
        rates = prices[model]
        if (not isinstance(rates, (list, tuple)) or len(rates) != 2
                or any(type(x) not in (int, float) or not math.isfinite(x) or x < 0 for x in rates)):
            raise ValueError('input/output token rates must be finite nonnegative CNY per million tokens')
    return deepcopy(policy)


def settings(config, role, model, tool_roles):
    policy = validate(config)
    if model not in policy['models']:
        raise ValueError('model is outside the frozen provider profile')
    if role not in tool_roles and role != 'provider_probe' and model != config['role_model']:
        raise ValueError('all simulated roles must use the fixed role backbone')
    if role in tool_roles and model not in config['tool_models'].values():
        raise ValueError('tool model is outside the declared tool selection')
    value = deepcopy(policy['models'][model])
    value['role_class'] = ('tool' if role in tool_roles else
                           'infrastructure_probe' if role == 'provider_probe' else 'simulated_role')
    value['tool_family'] = next((k for k, v in config['tool_models'].items() if v == model), None) if role in tool_roles else None
    for key in ('response_parsing', 'provider_content_boundary'):
        if key in policy:
            value[key] = deepcopy(policy[key])
    return value
