"""Exercise real CAMEL sessions with a deterministic, network-free transport."""
from copy import deepcopy
import importlib.util
import tempfile
import unittest
import json
from pathlib import Path

from sandbox.experiments.linked.camel_provider import BudgetedCamelFactory, MEMORY_POLICY
from sandbox.experiments.linked.provider import ModelCallError, ProviderUnavailable, GENERATION_POLICY_V3


class FakeTransport:
    endpoint = 'https://api.modelarts-maas.com/openai/v1'

    def __init__(self):
        self.calls = []
        self.fail_next = False
        self.content = '{"action":"summary","content":{"text":"test"}}'

    def complete(self, messages, **kwargs):
        self.calls.append(deepcopy(messages))
        if self.fail_next:
            raise ModelCallError('deterministic test failure')
        index = len(self.calls)
        return {'id': 'chat-test-' + str(index), 'object': 'chat.completion', 'created': 1,
            'model': kwargs['model'], 'choices': [{'index': 0, 'finish_reason': 'stop',
                'message': {'role': 'assistant', 'content': self.content}}],
            'usage': {'prompt_tokens': 20, 'completion_tokens': 10, 'total_tokens': 30}}, 'request-' + str(index)


@unittest.skipUnless(importlib.util.find_spec('camel'), 'CAMEL is installed in the project virtualenv')
class CamelProviderTests(unittest.TestCase):
    def test_provider_outage_keeps_exact_failed_request_link_without_stale_usage(self):
        with tempfile.TemporaryDirectory() as root:
            transport=FakeTransport()
            def unavailable(*args,**kwargs):
                raise ProviderUnavailable('resource frozen',request_id='failed-http-request')
            transport.complete=unavailable
            factory=BudgetedCamelFactory({'role_model':'deepseek-v4.1-flash',
                'tool_models':{'deepseek':'deepseek-v4.1-flash','glm':'glm-5.3'}},None,root,
                case_id='independent-fiction',data_kind='synthetic_independent',run_id='test',transport=transport)
            session=factory.session('scope','doctor')
            with self.assertRaises(ProviderUnavailable):session.respond({'action':'verify_handoff','conversation':[]})
            self.assertEqual(session.last_request_id,'failed-http-request')
            self.assertIsNone(session.last_usage)

    def test_current_visible_state_is_sent_once_and_failed_call_has_no_stale_usage(self):
        with tempfile.TemporaryDirectory() as root:
            transport = FakeTransport()
            factory = BudgetedCamelFactory({'role_model': 'deepseek-v4.1-flash-1017',
                    'tool_models': {'deepseek': 'deepseek-v4.1-flash-1017', 'glm': 'glm-5.3'},
                    'max_calls_per_run': 10}, None, root,
                case_id='synthetic-test', data_kind='synthetic_independent', run_id='test', transport=transport)
            session = factory.session('scope', 'doctor')
            session.respond({'action': 'verify_handoff', 'handoff': 'FIRST_ONLY_MARKER', 'conversation': []})
            session.respond({'action': 'verify_handoff', 'handoff': 'CURRENT_VIEW',
                             'conversation': [{'speaker': 'patient', 'text': 'Retained patient answer'}]})
            self.assertEqual(session.memory_policy, MEMORY_POLICY)
            self.assertEqual([len(call) for call in transport.calls], [2, 2])
            self.assertNotIn('FIRST_ONLY_MARKER', str(transport.calls[1]))
            self.assertIn('Retained patient answer', str(transport.calls[1]))
            self.assertEqual(session.last_request_id, 'request-2')
            self.assertEqual(session.last_usage['completion_tokens'], 10)
            transport.fail_next = True
            with self.assertRaises(Exception):
                session.respond({'action': 'verify_handoff', 'handoff': 'CURRENT_VIEW', 'conversation': []})
            self.assertIsNone(session.last_request_id)
            self.assertIsNone(session.last_usage)

    def test_v3_single_call_normalization_journal_preserves_clinical_defer(self):
        with tempfile.TemporaryDirectory() as root:
            transport=FakeTransport();transport.evidence_dir=Path(root)
            transport.content='{"action":"adjudication","decision":"defer","reason":"Synthetic uncertainty"'
            factory=BudgetedCamelFactory({'role_model':'deepseek-v4.1-flash',
                'tool_models':{'deepseek':'deepseek-v4.1-flash','glm':'glm-5.3'},
                'generation_policy':deepcopy(GENERATION_POLICY_V3)},None,root,
                case_id='independent-fiction',data_kind='synthetic_independent',run_id='test',transport=transport)
            session=factory.session('scope','senior_pharmacist')
            result=session.respond({'action':'adjudicate','prescription':{'items':[]}})
            self.assertEqual(result['decision'],'defer')
            self.assertEqual(len(transport.calls),1)
            self.assertTrue(session.last_parse_metadata['normalization_performed'])
            self.assertEqual(session.last_usage['completion_tokens'],10)
            log=json.loads((Path(root)/'response_parsing/request-1.json').read_text())
            self.assertEqual(log['response_parse'],session.last_parse_metadata)
            self.assertNotIn('Synthetic uncertainty',json.dumps(log))
            transport.content='{"a":1 "b":2}'
            with self.assertRaises(json.JSONDecodeError):session.respond({'action':'adjudicate','prescription':{'items':[]}})
            self.assertEqual(len(transport.calls),2)
            self.assertFalse(session.last_parse_metadata['normalization_performed'])
            self.assertTrue((Path(root)/'response_parsing/request-2.json').exists())
            transport.fail_next=True
            with self.assertRaises(Exception):session.respond({'action':'adjudicate','prescription':{'items':[]}})
            self.assertIsNone(session.last_parse_metadata)


if __name__ == '__main__':
    unittest.main()
