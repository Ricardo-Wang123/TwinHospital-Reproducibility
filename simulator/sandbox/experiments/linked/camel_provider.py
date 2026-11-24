"""Actual CAMEL role sessions whose only inference path is the budgeted transport."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from copy import deepcopy

from agents.base_agent import MEMORY_POLICY
from .provider import BudgetedTransport, ModelCallError, check_data_policy
from .actors import role_type, validate_role_route
from . import provider_profile


class BudgetedCamelFactory:
    def __init__(self, config, ledger, evidence_dir, *, case_id, data_kind,
                 run_id, config_id=None, transport=None):
        self.config, self.ledger = deepcopy(config), ledger
        self.transport = transport or BudgetedTransport(config, ledger, evidence_dir)
        self.case_id, self.data_kind = case_id, data_kind
        self.run_id, self.config_id = run_id, config_id
        self.call_count = 0

    def session(self, scope, role, model_family=None):
        validate_role_route(role, model_family)
        model = self.config['tool_models'][model_family] if model_family else self.config['role_model']
        check_data_policy(self.data_kind, self.config.get('data_policy', {}), self.transport.endpoint)
        # The private, scrubbed transport archive is the sole content logger.
        os.environ['CAMEL_MODEL_LOG_ENABLED'] = 'false'
        os.environ['TRACEROOT_ENABLED'] = 'false'
        os.environ['LANGFUSE_ENABLED'] = 'false'
        os.environ['OTEL_SDK_DISABLED'] = 'true'
        from camel.agents import ChatAgent
        from camel.messages import BaseMessage
        from camel.models import BaseModelBackend
        from camel.types import ChatCompletion
        from camel.utils import BaseTokenCounter
        for name in list(logging.Logger.manager.loggerDict):
            if name.startswith('camel'):
                logging.getLogger(name).setLevel(logging.CRITICAL)

        factory = self
        context_limit = (provider_profile.validate(self.config)['models'][model]['context_window_tokens']
                         if provider_profile.is_explicit(self.config) else 120_000)

        class ByteUpperCounter(BaseTokenCounter):
            def count_tokens_from_messages(self, messages):
                return len(json.dumps(messages, ensure_ascii=False).encode()) + 1024
            def encode(self, text):
                return list(text.encode('utf-8'))
            def decode(self, ids):
                return bytes(ids).decode('utf-8', errors='replace')

        class Backend(BaseModelBackend):
            last_request_id = None
            last_usage = None
            last_finish_reason = None
            @property
            def token_counter(self):
                if not self._token_counter:
                    self._token_counter = ByteUpperCounter()
                return self._token_counter
            @property
            def token_limit(self):
                return context_limit
            def _run(self, messages, response_format=None, tools=None):
                self.last_request_id = None
                self.last_usage = None
                self.last_finish_reason = None
                if tools or response_format:
                    raise ValueError('clinical runtime owns actions; model receives text observations only')
                ceiling = factory.config.get('max_calls_per_run')
                if ceiling is not None and (type(ceiling) is not int or ceiling < 1):
                    raise ValueError('max_calls_per_run must be a positive integer or null')
                if ceiling is not None and factory.call_count >= ceiling:
                    raise RuntimeError('per-run model-call ceiling reached')
                factory.call_count += 1
                try:
                    envelope, rid = factory.transport.complete(messages, model=model,
                        case_id=factory.case_id, role=role, data_kind=factory.data_kind,
                        run_id=factory.run_id, config_id=factory.config_id or scope)
                except ModelCallError as exc:
                    self.last_request_id = exc.request_id
                    raise
                self.last_request_id = rid
                self.last_usage = deepcopy(envelope.get('usage'))
                self.last_finish_reason = envelope.get('choices',[{}])[0].get('finish_reason')
                return ChatCompletion.model_validate(envelope)
            async def _arun(self, messages, response_format=None, tools=None):
                return await asyncio.to_thread(self._run, messages, response_format, tools)

        backend = Backend(model_type=model, model_config_dict={}, max_retries=0,
                          timeout=self.config.get('timeout_seconds', 120))
        agent_class = role_type(role)
        agent = ChatAgent(system_message=BaseMessage.make_assistant_message(
            role_name=role, content=agent_class.system_prompt()), model=backend,
            retry_attempts=1, max_iteration=1, summarize_threshold=None,
            token_limit=context_limit)

        return agent_class(
            agent, backend, model_name=model, data_kind=self.data_kind,
            parsing=self.config.get('generation_policy', {}).get('response_parsing'),
            evidence_dir=getattr(self.transport, 'evidence_dir', None),
        )
