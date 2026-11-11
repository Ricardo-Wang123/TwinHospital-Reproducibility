import json
from pathlib import Path
import tempfile
import unittest
from copy import deepcopy
from unittest.mock import patch

import requests
from sandbox.experiments.linked.provider_stream import receive, StreamProtocolError
from sandbox.experiments.linked.provider import BudgetLedger, BudgetedTransport, ModelCallError, GENERATION_POLICY_V9


MODEL='openpangu-2.0-pro'
def chunk(delta=None,finish=None,usage=None,**extra):
    return {'id':'fixture-stream','created':123,'model':MODEL,'object':'chat.completion.chunk',
        'choices':[] if usage else [{'index':0,'delta':delta or {},'finish_reason':finish}],
        **({'usage':usage} if usage else {}),**extra}


class StreamResponse:
    status_code=200
    def __init__(self,chunks):self.chunks=chunks;self.closed=False
    def iter_lines(self,**kwargs):
        for item in self.chunks:
            yield b'data: '+(item if isinstance(item,bytes) else json.dumps(item,ensure_ascii=False).encode())
            yield b''
    def close(self):self.closed=True


def complete_chunks():
    return [chunk({'role':'assistant','reasoning_content':'PRIVATE FIXTURE'}),
        chunk({'content':'{"ok":'}),chunk({'content':'true}'},'stop'),
        chunk(usage={'prompt_tokens':20,'completion_tokens':10,'total_tokens':30}),b'[DONE]']


class StreamTests(unittest.TestCase):
    def test_incomplete_stream_is_retried_with_identical_input_and_both_attempts_accounted(self):
        class HTTP:
            def __init__(self):
                self.responses = [StreamResponse([chunk({'reasoning_content': 'unfinished'}),
                    chunk(usage={'prompt_tokens': 20, 'completion_tokens': 10}), b'[DONE]']),
                    StreamResponse(complete_chunks())]
                self.calls = []
            def post(self, *args, **kwargs):
                self.calls.append(deepcopy(kwargs['json']))
                return self.responses[len(self.calls) - 1]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); ledger = BudgetLedger(root / 'budget.sqlite', None); http = HTTP()
            config = {'endpoint': 'https://api.modelarts-maas.com/v2', 'api_protocol': 'maas_standard_v2',
                'generation_policy': deepcopy(GENERATION_POLICY_V9), 'role_model': MODEL,
                'max_attempts': 2, 'response_transport': 'sse', 'stream_max_duration_seconds': 900}
            transport = BudgetedTransport(config, ledger, root / 'calls', key='test-secret', http_session=http)
            with patch('sandbox.experiments.linked.provider.time.sleep'):
                value, _ = transport.complete([], model=MODEL, role='doctor', case_id='c', data_kind='synthetic_independent')
            self.assertEqual(value['choices'][0]['message']['content'], '{"ok":true}')
            self.assertEqual(len(http.calls), 2)
            self.assertEqual(http.calls[0], http.calls[1])
            records = [json.loads(p.read_bytes()) for p in (root / 'calls').glob('*.json')]
            incomplete = next(row for row in records if row['outcome'] == 'failed')
            self.assertTrue(incomplete['retry_planned'])
            self.assertEqual(incomplete['retry_reason'], 'incomplete_provider_stream_same_input')
            self.assertAlmostEqual(ledger.summary()['accounted_upper_cny'], .000544)
            self.assertEqual(ledger.summary()['reserved_cny'], 0)

    def test_stream_model_mismatch_is_not_retried_as_a_transient_incomplete_stream(self):
        class HTTP:
            def __init__(self): self.calls = 0
            def post(self, *args, **kwargs):
                self.calls += 1
                return StreamResponse([chunk({'content': '{}'}, 'stop', model='different'), b'[DONE]'])
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); ledger = BudgetLedger(root / 'budget.sqlite', None); http = HTTP()
            config = {'endpoint': 'https://api.modelarts-maas.com/v2', 'api_protocol': 'maas_standard_v2',
                'generation_policy': deepcopy(GENERATION_POLICY_V9), 'role_model': MODEL,
                'max_attempts': 2, 'response_transport': 'sse', 'stream_max_duration_seconds': 900}
            transport = BudgetedTransport(config, ledger, root / 'calls', key='test-secret', http_session=http)
            with self.assertRaises(ModelCallError):
                transport.complete([], model=MODEL, role='doctor', case_id='c', data_kind='synthetic_independent')
            self.assertEqual(http.calls, 1)

    def test_stream_longer_than_old_180_second_total_wait_is_complete(self):
        response=StreamResponse(complete_chunks());record={}
        value=receive(response,model=MODEL,started=0,max_duration=900,record=record,clock=lambda:250)
        self.assertEqual(value['choices'][0]['message']['content'],'{"ok":true}')
        self.assertEqual(value['usage']['completion_tokens'],10)
        self.assertTrue(record['stream_observation']['done_received']);self.assertTrue(response.closed)
        self.assertEqual(record['stream_observation']['first_content_seconds'],250)
        self.assertNotIn('PRIVATE FIXTURE',json.dumps(record))

    def test_sse_never_promotes_partial_or_mixed_model_output(self):
        for data in (complete_chunks()[:-1],[chunk({'content':'{"ok":true}'},'stop',model='different'),b'[DONE]'],
                     [chunk({'content':'{"ok":true}'}),b'[DONE]']):
            response=StreamResponse(data);record={}
            with self.assertRaises(StreamProtocolError):
                receive(response,model=MODEL,started=0,max_duration=900,record=record,clock=lambda:1)
            self.assertTrue(response.closed);self.assertIn('partial_response_body',record)

    def test_wall_deadline_retains_partial_evidence_and_closes_connection(self):
        response=StreamResponse(complete_chunks());record={}
        with self.assertRaises(requests.ReadTimeout):
            receive(response,model=MODEL,started=0,max_duration=900,record=record,clock=lambda:901)
        self.assertTrue(response.closed);self.assertEqual(record['failure_reason_code'],'stream_wall_deadline')

    def test_actual_transport_accounts_terminal_usage_and_strips_private_reasoning(self):
        class HTTP:
            def __init__(self,chunks):self.response=StreamResponse(chunks);self.calls=[]
            def post(self,*a,**kw):self.calls.append(kw);return self.response
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);ledger=BudgetLedger(root/'budget.sqlite',None);http=HTTP(complete_chunks())
            config={'endpoint':'https://api.modelarts-maas.com/v2','api_protocol':'maas_standard_v2',
                'generation_policy':deepcopy(GENERATION_POLICY_V9),'role_model':MODEL,'max_attempts':1,
                'response_transport':'sse','stream_max_duration_seconds':900,'timeout_seconds':60,'connect_timeout_seconds':15}
            transport=BudgetedTransport(config,ledger,root/'calls',key='test-secret',http_session=http)
            value,rid=transport.complete([],model=MODEL,role='doctor',case_id='c',data_kind='synthetic_independent')
            self.assertTrue(http.calls[0]['stream']);self.assertTrue(http.calls[0]['json']['stream'])
            self.assertEqual(http.calls[0]['json']['stream_options'],{'include_usage':True})
            self.assertEqual(http.calls[0]['timeout'],(15,60))
            self.assertNotIn('reasoning_content',value['choices'][0]['message'])
            evidence=json.loads((root/'calls'/(rid+'.json')).read_text())
            self.assertEqual(evidence['response_body']['choices'][0]['message']['reasoning_content'],'PRIVATE FIXTURE')
            self.assertAlmostEqual(ledger.summary()['accounted_upper_cny'],.000272)
            # Losing DONE after a usage record must still account actual billed
            # tokens, while never returning an incomplete clinical response.
            http.response=StreamResponse(complete_chunks()[:-1])
            with self.assertRaises(ModelCallError):
                transport.complete([],model=MODEL,role='doctor',case_id='c',data_kind='synthetic_independent')
            self.assertAlmostEqual(ledger.summary()['accounted_upper_cny'],.000544)
            self.assertEqual(ledger.summary()['reserved_cny'],0)


if __name__=='__main__':unittest.main()
