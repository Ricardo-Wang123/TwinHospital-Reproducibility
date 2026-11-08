"""Actual CAMEL role sessions whose only inference path is the budgeted transport."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from copy import deepcopy
from pathlib import Path

from .provider import BudgetedTransport, ModelCallError, TOOLS, check_data_policy, private_json
from .json_boundary import parse_json_response
from . import provider_profile

MEMORY_POLICY = 'explicit_visible_state_per_turn'


class BudgetedCamelFactory:
    def __init__(self, config, ledger, evidence_dir, *, case_id, data_kind,
                 run_id, config_id=None, transport=None):
        self.config, self.ledger = deepcopy(config), ledger
        self.transport = transport or BudgetedTransport(config, ledger, evidence_dir)
        self.case_id, self.data_kind = case_id, data_kind
        self.run_id, self.config_id = run_id, config_id
        self.call_count = 0

    def session(self, scope, role, model_family=None):
        if model_family is not None and role not in TOOLS:
            raise ValueError('AI tool family cannot change the fixed role backbone')
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
        from ..visibility import assert_visible
        from .actors import system_prompt
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
                if factory.call_count >= factory.config.get('max_calls_per_run', 150):
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
        agent = ChatAgent(system_message=BaseMessage.make_assistant_message(
            role_name=role, content=system_prompt(role)), model=backend,
            retry_attempts=1, max_iteration=1, summarize_threshold=None,
            token_limit=context_limit)

        class Session:
            model_name = model
            memory_policy = MEMORY_POLICY
            last_request_id = None
            last_usage = None
            last_parse_metadata = None
            def respond(self, observation):
                self.last_request_id = None
                self.last_usage = None
                self.last_parse_metadata = None
                backend.last_request_id = None
                backend.last_usage = None
                backend.last_finish_reason = None
                assert_visible(observation)
                if observation.get('data_kind', factory.data_kind) != factory.data_kind:
                    raise ValueError('case data provenance changed within a run')
                # Runtime supplies the complete permitted state on every turn.
                # Keeping old complete observations would duplicate input and may
                # retain information removed by a corrected visibility policy.
                agent.reset()
                try:
                    response = agent.step(BaseMessage.make_user_message(role_name='workflow',
                        content=json.dumps(observation, ensure_ascii=False, separators=(',', ':'), allow_nan=False)))
                finally:
                    self.last_request_id = backend.last_request_id
                    self.last_usage = deepcopy(backend.last_usage)
                if not response.msgs:
                    raise ValueError('CAMEL returned no assistant message')
                parsing = factory.config.get('generation_policy',{}).get('response_parsing')
                if parsing is not None:
                    try:
                        value, self.last_parse_metadata = parse_json_response(response.msg.content,
                            finish_reason=backend.last_finish_reason, policy=parsing)
                    except Exception as exc:
                        self.last_parse_metadata = deepcopy(getattr(exc,'parse_metadata',None))
                        raise
                    finally:
                        directory = getattr(factory.transport,'evidence_dir',None)
                        if directory is not None and self.last_request_id and self.last_parse_metadata:
                            private_json(Path(directory)/'response_parsing'/(self.last_request_id+'.json'),
                                {'request_id':self.last_request_id,'response_parse':self.last_parse_metadata})
                    assert_visible(value)
                    return value
                # V2 preserves its original strict parser without normalization.
                text = response.msg.content.strip()
                if text.startswith('```') and text.endswith('```'):
                    text = '\n'.join(text.splitlines()[1:-1])
                value = json.loads(text)
                if not isinstance(value, dict):
                    raise ValueError('model response must be a JSON object')
                assert_visible(value)
                return value
        return Session()
