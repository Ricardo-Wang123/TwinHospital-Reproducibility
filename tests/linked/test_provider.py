from copy import deepcopy
import json
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sandbox.experiments.linked.provider import (
    BudgetLedger, BudgetExceeded, BudgetedTransport, DataPolicyError, ModelCallError, ProviderUnavailable, check_data_policy, GENERATION_POLICY_V2, GENERATION_POLICY_V3, GENERATION_POLICY_V4, GENERATION_POLICY_V5, GENERATION_POLICY_V6, GENERATION_POLICY_V7, GENERATION_POLICY_V8, GENERATION_POLICY_V9,
)


class FakeResponse:
    def __init__(self, status=200, value=None):
        self.status_code = status
        self.value = value if value is not None else {
            'model': 'glm-5.3', 'usage': {'prompt_tokens': 20, 'completion_tokens': 10},
            'choices': [{'finish_reason': 'stop', 'message': {'content': '{"ok":true}'}}]}
    def json(self):
        return self.value


class FakeHTTP:
    def __init__(self, response=None):
        self.response, self.calls = response or FakeResponse(), []
    def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.response


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
    def tearDown(self):
        self.tmp.cleanup()

    def provider(self, response=None, cap=1000):
        ledger = BudgetLedger(self.root/'budget.sqlite', cap)
        http = FakeHTTP(response)
        transport = BudgetedTransport({'endpoint': 'https://api.modelarts-maas.com/openai/v1',
            'max_attempts': 1, 'generation_policy': deepcopy(GENERATION_POLICY_V2)}, ledger, self.root/'calls', key='test-secret-only', http_session=http)
        return ledger, http, transport

    def test_atomic_budget_and_restart(self):
        ledger = BudgetLedger(self.root/'budget.sqlite', 1)
        def reserve(i):
            try:
                ledger.reserve(str(i), .1, case_id='c', role='doctor', model='test')
                return True
            except BudgetExceeded:
                return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            accepted = list(pool.map(reserve, range(20)))
        self.assertEqual(sum(accepted), 10)
        restored = BudgetLedger(self.root/'budget.sqlite', 1)
        self.assertEqual(restored.summary()['remaining_cny'], 0)
        with self.assertRaises(ValueError):
            BudgetLedger(self.root/'budget.sqlite', 1000)

    def test_explicit_uncapped_authorization_preserves_charges_and_ledger_identity(self):
        path=self.root/'uncapped.sqlite';ledger=BudgetLedger(path,1)
        ledger.reserve('old',.5,case_id='c',role='doctor',model='test')
        ledger.settle('old',amount=.3,status='completed')
        identity=ledger.identity()
        auth={'authorized_by':'user','authorization_id':'explicit-test','evidence':'Remove total budget limit','scope':'this experiment'}
        result=ledger.authorize_cap(None,authorization=auth)
        self.assertIsNone(result['cap_cny']);self.assertIsNone(result['remaining_cny'])
        self.assertEqual(result['accounted_upper_cny'],.3)
        restored=BudgetLedger(path,None)
        self.assertEqual(restored.identity(),identity)
        restored.reserve('new',2000,case_id='c',role='doctor',model='test')
        self.assertEqual(restored.summary()['reserved_cny'],2000)
        self.assertEqual(restored.summary()['requests'],2)
        with restored._db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM budget_authorizations').fetchone()[0],1)
        with self.assertRaises(ValueError):BudgetLedger(path,1)

    def test_cap_cannot_be_changed_without_authorization_or_during_an_active_request(self):
        ledger=BudgetLedger(self.root/'auth.sqlite',1)
        with self.assertRaises(ValueError):ledger.authorize_cap(None,authorization={})
        ledger.reserve('active',.1,case_id='c',role='doctor',model='test')
        auth={'authorized_by':'user','authorization_id':'test','evidence':'Remove limit','scope':'test'}
        with self.assertRaisesRegex(ValueError,'in-flight'):ledger.authorize_cap(None,authorization=auth)
        self.assertEqual(ledger.summary()['cap_cny'],1)

    def test_mimic_rejected_before_network_or_budget(self):
        ledger, http, transport = self.provider()
        with self.assertRaises(DataPolicyError):
            transport.complete([], model='glm-5.3', case_id='c', role='rx_ai', data_kind='mimic_derived')
        self.assertEqual(http.calls, [])
        self.assertEqual(ledger.summary()['requests'], 0)

    def test_declared_glm_backbone_uses_role_policy_without_changing_other_roles(self):
        from sandbox.experiments.linked.provider import generation_settings
        config={'generation_policy':deepcopy(GENERATION_POLICY_V5),'role_model':'glm-5.2'}
        for role in ('patient','doctor','nurse','pharmacist'):
            self.assertEqual(generation_settings(config,role,'glm-5.2')['role_class'],'simulated_role')
            with self.assertRaises(ValueError):generation_settings(config,role,'deepseek-v4-flash')
        self.assertEqual(generation_settings(config,'scribe','glm-5.1')['tool_family'],'glm')

    def test_explicit_final_channel_keeps_raw_evidence_and_exact_billing(self):
        response = FakeResponse()
        raw = 'PRIVATE FIXTURE ONLY</think>\n{"action":"defer"}'
        response.value['choices'][0]['message'].update(content=raw, reasoning_content='PRIVATE CHANNEL FIXTURE')
        ledger, http, transport = self.provider(response)
        transport.config.update(generation_policy=deepcopy(GENERATION_POLICY_V6), role_model='glm-5.2')
        value, rid = transport.complete([], model='glm-5.2', case_id='c',role='doctor',data_kind='synthetic_independent')
        self.assertEqual(value['choices'][0]['message']['content'], '{"action":"defer"}')
        self.assertNotIn('reasoning_content', value['choices'][0]['message'])
        evidence = json.loads((self.root/'calls'/(rid+'.json')).read_text())
        self.assertEqual(evidence['response_body']['choices'][0]['message']['content'], raw)
        self.assertTrue(evidence['content_boundary']['applied'])
        self.assertEqual(len(http.calls), 1)
        self.assertAlmostEqual(ledger.summary()['accounted_upper_cny'], .00044)
        self.assertNotIn('provider_content_boundary', http.calls[0][1]['json'])

    def test_pangu_tool_requires_explicit_new_policy_and_keeps_fixed_role(self):
        from sandbox.experiments.linked.provider import generation_settings
        from sandbox.experiments.linked.contracts import paper_conditions
        config = {'generation_policy':deepcopy(GENERATION_POLICY_V7),'role_model':'glm-5.1'}
        setting=generation_settings(config,'scribe','openpangu-2.0-flash')
        self.assertEqual(setting['tool_family'],'pangu');self.assertTrue(setting['thinking'])
        self.assertNotIn('reasoning_effort', setting)
        self.assertEqual(len(paper_conditions(model_families=('glm51','panguflash'))),15)
        with self.assertRaises(ValueError):generation_settings(config,'doctor','openpangu-2.0-flash')
        config['generation_policy']=deepcopy(GENERATION_POLICY_V6)
        with self.assertRaises(ValueError):generation_settings(config,'scribe','openpangu-2.0-flash')

    def test_v8_omits_undocumented_glm51_effort_without_changing_sampling_or_thinking(self):
        from sandbox.experiments.linked.provider import generation_settings
        ledger,http,transport=self.provider()
        transport.config.update(role_model='glm-5.1',generation_policy=deepcopy(GENERATION_POLICY_V8))
        for role in ('doctor','scribe'):
            transport.complete([],model='glm-5.1',case_id='c',role=role,data_kind='synthetic_independent')
            self.assertNotIn('reasoning_effort',http.calls[-1][1]['json'])
            self.assertEqual(http.calls[-1][1]['json']['max_completion_tokens'],16384)
        probe=generation_settings(transport.config,'provider_probe','glm-5.1')
        self.assertIsNone(probe['reasoning_effort'])
        self.assertEqual(GENERATION_POLICY_V7['role']['reasoning_effort'],'low')

    def test_token_accounting_total_cap_and_tls(self):
        ledger, http, transport = self.provider()
        value, request_id = transport.complete([{'role':'user','content':'Synthetic check'}],
            model='glm-5.3', case_id='c', role='rx_ai', data_kind='synthetic_independent')
        options = http.calls[0][1]
        self.assertTrue(options['verify'])
        self.assertEqual(options['json']['max_completion_tokens'], 8192)
        self.assertNotIn('max_tokens', options['json'])
        self.assertAlmostEqual(ledger.summary()['accounted_upper_cny'], .00044)
        self.assertEqual(ledger.summary()['reserved_cny'], 0)
        record = self.root/'calls'/(request_id+'.json')
        self.assertEqual(record.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('test-secret-only', record.read_text())

    def test_explicit_loopback_route_is_session_local_and_keeps_tls(self):
        import os
        original = dict(os.environ)
        config = {'endpoint':'https://api.modelarts-maas.com/v2', 'api_protocol':'maas_standard_v2',
            'max_attempts':1, 'generation_policy':deepcopy(GENERATION_POLICY_V9),
            'role_model':'openpangu-2.0-pro', 'timeout_seconds':180, 'connect_timeout_seconds':15,
            'network_route':{'mode':'explicit_loopback_proxy','url':'http://127.0.0.1:17997'}}
        ledger=BudgetLedger(self.root/'route.sqlite', None);http=FakeHTTP()
        http.response.value['model']='openpangu-2.0-pro'
        transport=BudgetedTransport(config,ledger,self.root/'route_calls',key='test-secret-only',http_session=http)
        _,rid=transport.complete([],model='openpangu-2.0-pro',case_id='c',role='doctor',data_kind='synthetic_independent')
        self.assertFalse(http.trust_env)
        self.assertEqual(http.proxies['https'],'http://127.0.0.1:17997')
        self.assertEqual(http.calls[0][1]['timeout'],(15,180))
        self.assertTrue(http.calls[0][1]['verify']);self.assertFalse(http.calls[0][1]['allow_redirects'])
        self.assertNotIn('reasoning_effort',http.calls[0][1]['json'])
        self.assertEqual(dict(os.environ),original)
        record=json.loads((self.root/'route_calls'/(rid+'.json')).read_text())
        self.assertEqual(record['network_route'],config['network_route'])
        self.assertEqual(record['connect_timeout_seconds'],15)
        self.assertAlmostEqual(ledger.summary()['accounted_upper_cny'],.000272)
        for url in ('http://external.invalid:17997','https://127.0.0.1:17997','http://user:secret@127.0.0.1:17997'):
            with self.assertRaises(ValueError):
                BudgetedTransport({**config,'network_route':{'mode':'explicit_loopback_proxy','url':url}},
                    ledger,self.root/'bad_route',key='test-secret-only',http_session=FakeHTTP())

    def test_pangu_role_requires_new_policy_and_remains_fixed_across_conditions(self):
        from sandbox.experiments.linked.provider import generation_settings
        from sandbox.experiments.linked.contracts import paper_conditions
        config={'role_model':'openpangu-2.0-pro','generation_policy':deepcopy(GENERATION_POLICY_V9)}
        for role in ('patient','doctor','nurse','pharmacist'):
            self.assertEqual(generation_settings(config,role,'openpangu-2.0-pro')['role_class'],'simulated_role')
            with self.assertRaises(ValueError):generation_settings(config,role,'openpangu-2.0-flash')
        self.assertEqual(len(paper_conditions(model_families=('pangupro','panguflash'))),15)
        config['generation_policy']=deepcopy(GENERATION_POLICY_V8)
        with self.assertRaises(ValueError):generation_settings(config,'doctor','openpangu-2.0-pro')

    def test_transport_errors_are_not_misreported_as_provider_freezes(self):
        import requests
        for index,error in enumerate((requests.ReadTimeout('private fixture'),
                requests.exceptions.SSLError('private fixture timed out'))):
            class BrokenHTTP:
                def post(self,*args,**kwargs):raise error
            ledger=BudgetLedger(self.root/f'error{index}.sqlite',None)
            transport=BudgetedTransport({'endpoint':'https://api.modelarts-maas.com/v2',
                'api_protocol':'maas_standard_v2','max_attempts':1,'generation_policy':deepcopy(GENERATION_POLICY_V9),
                'role_model':'openpangu-2.0-pro'},ledger,self.root/f'error{index}',key='test-secret-only',http_session=BrokenHTTP())
            with self.assertRaises(ModelCallError) as caught:
                transport.complete([],model='openpangu-2.0-pro',case_id='c',role='doctor',data_kind='synthetic_independent')
            record=json.loads((self.root/f'error{index}'/(caught.exception.request_id+'.json')).read_text())
            self.assertEqual(record['failure_reason_code'],('read_timeout','tls_connection_error')[index])
            self.assertTrue(record['transport_timeout_indicated'])
            self.assertNotIn('private fixture',json.dumps(record))
            self.assertFalse(ledger.summary()['provider_unavailable'])

    def test_unknown_usage_conservatively_charged(self):
        ledger, http, transport = self.provider(FakeResponse(value={'choices':[]}))
        with self.assertRaises(ModelCallError):
            transport.complete([], model='glm-5.3', case_id='c', role='rx_ai', data_kind='synthetic_independent')
        self.assertGreater(ledger.summary()['accounted_upper_cny'], .2)
        self.assertEqual(ledger.summary()['statuses'], {'unknown_charge':1})

    def test_interrupted_request_keeps_reservation_and_marks_evidence(self):
        ledger, http, transport = self.provider()

        def interrupt(*args, **kwargs):
            raise KeyboardInterrupt

        http.post = interrupt
        with self.assertRaises(KeyboardInterrupt):
            transport.complete([], model='glm-5.3', case_id='c', role='rx_ai', data_kind='synthetic_independent')
        self.assertEqual(ledger.summary()['statuses'], {'reserved': 1})
        evidence = json.loads(next((self.root/'calls').glob('*.json')).read_text())
        self.assertEqual(evidence['outcome'], 'interrupted')
        self.assertFalse(evidence['retry_planned'])

    def test_auth_failure_not_retried_and_secret_redacted(self):
        ledger, http, transport = self.provider(FakeResponse(401, {'error':'echo test-secret-only'}))
        with self.assertRaises(ModelCallError):
            transport.complete([], model='glm-5.3', case_id='c', role='rx_ai', data_kind='synthetic_independent')
        self.assertEqual(len(http.calls), 1)
        self.assertEqual(ledger.summary()['accounted_upper_cny'], 0)
        self.assertNotIn('test-secret-only', next((self.root/'calls').glob('*.json')).read_text())

    def test_account_freeze_stops_other_workers_without_changing_budget_or_spending(self):
        reply=FakeResponse(403,{'error':{'code':'ModelArts.81006','message':'The resource is frozen.'}})
        ledger,http,transport=self.provider(reply,cap=None)
        with self.assertRaises(ProviderUnavailable) as caught:
            transport.complete([],model='glm-5.3',case_id='c',role='rx_ai',data_kind='synthetic_independent')
        self.assertIsNotNone(caught.exception.request_id)
        self.assertTrue(ledger.summary()['provider_unavailable'])
        self.assertIsNone(ledger.summary()['cap_cny'])
        self.assertEqual(ledger.summary()['accounted_upper_cny'],0)
        restored=BudgetLedger(ledger.path,None)
        with self.assertRaises(ProviderUnavailable):
            restored.reserve('blocked',1,case_id='next',role='doctor',model='deepseek-v4.1-flash')
        self.assertEqual(restored.summary()['requests'],1)
        self.assertEqual(len(http.calls),1)
        record=json.loads((self.root/'calls'/(caught.exception.request_id+'.json')).read_text())
        self.assertEqual(record['outcome'],'stopped_provider')
        self.assertFalse(record['retry_planned'])

    def test_recovery_probe_is_bounded_nonclinical_and_does_not_unlock_work(self):
        ledger,http,transport=self.provider(FakeResponse(403,{'error':{'code':'ModelArts.81006'}}))
        with self.assertRaises(ProviderUnavailable):
            transport.complete([],model='glm-5.3',case_id='c',role='rx_ai',data_kind='synthetic_independent')
        http.response=FakeResponse()
        _,rid=transport.complete([{'role':'user','content':'Return the word OK.'}],model='glm-5.3',
            case_id='provider_health_probe',role='provider_probe',data_kind='synthetic_independent',provider_probe=True)
        self.assertEqual(len(http.calls),2)
        self.assertTrue(ledger.summary()['provider_unavailable'])
        self.assertGreater(ledger.summary()['accounted_upper_cny'],0)
        self.assertTrue(json.loads((self.root/'calls'/(rid+'.json')).read_text())['provider_health_probe'])
        with self.assertRaises(ValueError):
            transport.complete([{'role':'user','content':'PRIVATE CLINICAL TEXT'}],model='glm-5.3',
                case_id='provider_health_probe',role='provider_probe',data_kind='synthetic_independent',provider_probe=True)
        self.assertEqual(len(http.calls),2)

    def test_ordinary_model_permission_error_does_not_halt_unrelated_routes(self):
        ledger,http,transport=self.provider(FakeResponse(403,{'error':{'code':'ModelArts.81004'}}))
        with self.assertRaises(ModelCallError) as caught:
            transport.complete([],model='glm-5.3',case_id='c',role='rx_ai',data_kind='synthetic_independent')
        self.assertIsNotNone(caught.exception.request_id)
        self.assertFalse(ledger.summary()['provider_unavailable'])

    def test_verified_recovery_preserves_cost_and_restricts_calls_to_healthy_models(self):
        ledger,http,transport=self.provider(FakeResponse(403,{'error':{'code':'ModelArts.81006'}}),cap=None)
        with self.assertRaises(ProviderUnavailable):
            transport.complete([],model='glm-5.3',case_id='c',role='rx_ai',data_kind='synthetic_independent')
        probes={}
        for model in ('deepseek-v4-flash','glm-5.2'):
            http.response=FakeResponse();http.response.value['model']=model
            _,rid=transport.complete([{'role':'user','content':'Return the word OK.'}],model=model,
                case_id='provider_health_probe',role='provider_probe',data_kind='synthetic_independent',provider_probe=True)
            probes[model]=rid
        before=ledger.summary()
        with self.assertRaisesRegex(ValueError,'every configured model'):
            ledger.recover_provider(recovery_id='bad',config_sha256='a'*64,
                probes_by_model={'glm-5.3':probes['glm-5.2']},evidence={'test':True})
        after=ledger.recover_provider(recovery_id='good',config_sha256='a'*64,probes_by_model=probes,evidence={'test':True})
        self.assertFalse(after['new_requests_stopped']);self.assertIsNone(after['cap_cny'])
        self.assertEqual(before['accounted_upper_cny'],after['accounted_upper_cny'])
        self.assertEqual(before['requests'],after['requests'])
        ledger.reserve('permitted',1,case_id='c',role='doctor',model='deepseek-v4-flash')
        with self.assertRaises(ProviderUnavailable):
            ledger.reserve('old-config',1,case_id='c',role='doctor',model='deepseek-v4.1-flash')
        with ledger._db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM provider_recoveries').fetchone()[0],1)

    def test_provider_probe_cannot_clear_accounting_stop(self):
        ledger,_,_=self.provider()
        ledger.reserve('over',.01,case_id='c',role='doctor',model='glm-5.2')
        with self.assertRaises(BudgetExceeded):ledger.settle('over',amount=.02,status='completed')
        with self.assertRaisesRegex(ValueError,'only a provider-resource stop'):
            ledger.recover_provider(recovery_id='bad',config_sha256='a'*64,
                probes_by_model={'glm-5.2':'over'},evidence={'test':True})
        self.assertTrue(ledger.summary()['halted_for_reconciliation'])

    def test_shared_pacing_spaces_workers_and_does_not_prebook_overdue_slots(self):
        ledger=BudgetLedger(self.root/'paced.sqlite',None)
        def enter(_):
            worker=BudgetLedger(ledger.path,None)
            while True:
                delay=worker.dispatch_delay(scope='shared',minimum_interval=.04)
                if not delay:return time.monotonic()
                time.sleep(delay)
        with ThreadPoolExecutor(max_workers=4) as pool:starts=sorted(pool.map(enter,range(4)))
        self.assertTrue(all(b-a>=.03 for a,b in zip(starts,starts[1:])))
        self.assertEqual(ledger.summary()['requests'],0)
        time.sleep(.05)
        self.assertEqual(ledger.dispatch_delay(scope='shared',minimum_interval=.04),0)
        self.assertGreater(ledger.dispatch_delay(scope='shared',minimum_interval=.001),.025)

    def test_retry_is_paced_and_queue_delay_is_not_api_elapsed_time(self):
        ledger,http,transport=self.provider()
        transport.minimum_interval=.01
        transport.config['max_attempts']=2
        reply=FakeResponse(429,{'error':{'code':'ModelArts.81101'}})
        responses=[reply,FakeResponse()]
        def post(*args,**kwargs):
            http.calls.append((args,kwargs));return responses.pop(0)
        http.post=post
        transport.complete([],model='glm-5.3',case_id='c',role='scribe',data_kind='synthetic_independent')
        records=[json.loads(p.read_text()) for p in (self.root/'calls').glob('*.json')]
        self.assertEqual(len(records),2)
        for r in records:
            self.assertEqual(r['minimum_request_interval_seconds'],.01)
            self.assertGreaterEqual(r['api_dispatch_wait_seconds'],0)
            self.assertGreaterEqual(r['elapsed_seconds'],0)

    def test_truncated_output_is_failure_but_billed(self):
        reply = FakeResponse()
        reply.value['choices'][0]['finish_reason'] = 'length'
        ledger, http, transport = self.provider(reply)
        with self.assertRaises(ModelCallError):
            transport.complete([], model='glm-5.3', case_id='c', role='rx_ai', data_kind='synthetic_independent')
        self.assertGreater(ledger.summary()['accounted_upper_cny'], 0)
        record = json.loads(next((self.root/'calls').glob('*.json')).read_text())
        self.assertEqual(record['outcome'], 'failed')

    def test_v5_length_retry_is_same_input_larger_budget_and_preserves_both_charges(self):
        ledger,http,transport=self.provider()
        transport.config.update(generation_policy=deepcopy(GENERATION_POLICY_V5),max_attempts=3)
        first=FakeResponse();first.value['choices'][0]['finish_reason']='length'
        responses=[first,FakeResponse()]
        def post(*args,**kwargs):
            http.calls.append((args,deepcopy(kwargs)));return responses.pop(0)
        http.post=post
        messages=[{'role':'user','content':'independent fictional test'}]
        value,rid=transport.complete(messages,model='glm-5.3',case_id='c',role='scribe',data_kind='synthetic_independent')
        bodies=[x[1]['json'] for x in http.calls]
        self.assertEqual([b['max_completion_tokens'] for b in bodies],[16384,32768])
        self.assertEqual(bodies[0]['messages'],messages);self.assertEqual(bodies[1]['messages'],messages)
        records=sorted((json.loads(p.read_text()) for p in (self.root/'calls').glob('*.json')),key=lambda r:r['attempt'])
        self.assertEqual([r['outcome'] for r in records],['failed','completed'])
        self.assertEqual(records[0]['failure_reason_code'],'completion_length_limit')
        self.assertEqual(records[0]['request_body']['max_completion_tokens'],16384)
        self.assertEqual(records[1]['request_id'],rid)
        self.assertGreater(records[1]['reserved_upper_cny'],records[0]['reserved_upper_cny'])
        self.assertAlmostEqual(ledger.summary()['accounted_upper_cny'],.00088)
        self.assertEqual(ledger.summary()['reserved_cny'],0)

    def test_v5_repeated_truncation_is_bounded_and_valid_defer_is_not_retried(self):
        reply=FakeResponse();reply.value['choices'][0]['finish_reason']='length'
        ledger,http,transport=self.provider(reply)
        transport.config.update(generation_policy=deepcopy(GENERATION_POLICY_V5),max_attempts=3)
        with self.assertRaises(ModelCallError):
            transport.complete([],model='glm-5.3',case_id='c',role='scribe',data_kind='synthetic_independent')
        self.assertEqual(len(http.calls),2)
        reply.value['choices'][0].update(finish_reason='stop',message={'content':'{"decision":"defer"}'})
        value,_=transport.complete([],model='glm-5.3',case_id='c',role='scribe',data_kind='synthetic_independent')
        self.assertEqual(len(http.calls),3);self.assertEqual(json.loads(value['choices'][0]['message']['content'])['decision'],'defer')

    def test_v5_expanded_retry_cannot_exceed_remaining_shared_budget(self):
        reply=FakeResponse();reply.value['choices'][0]['finish_reason']='length'
        ledger,http,transport=self.provider(reply,cap=.2)
        transport.config.update(generation_policy=deepcopy(GENERATION_POLICY_V5),max_attempts=3)
        with self.assertRaises(BudgetExceeded):
            transport.complete([],model='deepseek-v4.1-flash',case_id='c',role='doctor',data_kind='synthetic_independent')
        self.assertEqual(len(http.calls),1)
        self.assertTrue(ledger.summary()['new_requests_stopped'])
        self.assertEqual(ledger.summary()['requests'],1)
        self.assertGreater(ledger.summary()['accounted_upper_cny'],0)

    def test_disabled_reasoning_mismatch_is_retained_billed_and_not_retried(self):
        for reasoning_content, reasoning_tokens in [('hidden reasoning', 0), ('', 15)]:
            with self.subTest(content_present=bool(reasoning_content)):
                reply = FakeResponse()
                reply.value['model'] = 'deepseek-v4.1-flash'
                reply.value['choices'][0]['message']['reasoning_content'] = reasoning_content
                reply.value['usage']['completion_tokens_details'] = {'reasoning_tokens': reasoning_tokens}
                ledger, http, transport = self.provider(reply)
                transport.config['max_attempts'] = 3
                before = ledger.summary()['accounted_upper_cny']
                with self.assertRaises(ModelCallError):
                    transport.complete([], model='deepseek-v4.1-flash', case_id='c',
                        role='doctor', data_kind='synthetic_independent')
                self.assertEqual(len(http.calls), 1)
                self.assertGreater(ledger.summary()['accounted_upper_cny'], before)
                record = json.loads(max((self.root/'calls').glob('*.json'), key=lambda p:p.stat().st_mtime_ns).read_text())
                self.assertEqual(record['generation_contract_error'], 'reasoning_returned_when_disabled')
                self.assertEqual(record['outcome'], 'failed')
                self.assertFalse(record['retry_planned'])

    def test_reasoning_returned_for_enabled_tool_is_valid(self):
        reply = FakeResponse()
        reply.value['choices'][0]['message']['reasoning_content'] = 'tool reasoning'
        reply.value['usage']['completion_tokens_details'] = {'reasoning_tokens': 5}
        ledger, http, transport = self.provider(reply)
        transport.complete([], model='glm-5.3', case_id='c', role='rx_ai', data_kind='synthetic_independent')
        self.assertEqual(len(http.calls), 1)

    def test_v2_uses_documented_thinking_type_without_changing_generation(self):
        ledger = BudgetLedger(self.root/'v2.sqlite', 1000)
        http = FakeHTTP()
        transport = BudgetedTransport({'endpoint':'https://api.modelarts-maas.com/v2',
            'api_protocol':'maas_standard_v2', 'max_attempts':1,
            'generation_policy':deepcopy(GENERATION_POLICY_V3)}, ledger,
            self.root/'v2_calls',key='test-secret-only',http_session=http)
        for role,model in [('doctor','deepseek-v4.1-flash'),('scribe','deepseek-v4.1-flash'),('scribe','glm-5.3')]:
            transport.complete([],model=model,case_id='c',role=role,data_kind='synthetic_independent')
        bodies=[x[1]['json'] for x in http.calls]
        self.assertEqual([b['thinking'] for b in bodies],[{'type':'disabled'},{'type':'enabled'},{'type':'enabled'}])
        for body in bodies:
            self.assertNotIn('chat_template_kwargs',body)
            self.assertEqual(body['max_completion_tokens'],8192)
            self.assertEqual(body['temperature'],.2)
        self.assertNotIn('reasoning_effort',bodies[0])
        self.assertEqual([b['reasoning_effort'] for b in bodies[1:]],['low','low'])

    def test_role_reasoning_requires_explicit_new_policy_and_remains_budgeted(self):
        reply=FakeResponse();reply.value['model']='deepseek-v4.1-flash'
        reply.value['choices'][0]['message']['reasoning_content']='private fixture reasoning'
        reply.value['usage']['completion_tokens_details']={'reasoning_tokens':5}
        ledger=BudgetLedger(self.root/'enabled.sqlite',1000);http=FakeHTTP(reply)
        transport=BudgetedTransport({'endpoint':'https://api.modelarts-maas.com/v2',
            'api_protocol':'maas_standard_v2','max_attempts':1,'generation_policy':deepcopy(GENERATION_POLICY_V4)},
            ledger,self.root/'enabled_calls',key='test-secret-only',http_session=http)
        transport.complete([],model='deepseek-v4.1-flash',case_id='c',role='doctor',data_kind='synthetic_independent')
        body=http.calls[0][1]['json']
        self.assertEqual(body['thinking'],{'type':'enabled'})
        self.assertEqual(body['reasoning_effort'],'low')
        self.assertEqual(body['max_completion_tokens'],8192)
        self.assertGreater(ledger.summary()['accounted_upper_cny'],0)
        self.assertEqual(ledger.summary()['reserved_cny'],0)
        self.assertFalse(GENERATION_POLICY_V3['role']['thinking'])
        self.assertEqual(GENERATION_POLICY_V4['tools'],GENERATION_POLICY_V3['tools'])

    def test_wrong_api_dialect_is_rejected_before_requests(self):
        ledger=BudgetLedger(self.root/'wrong-route.sqlite',1000)
        for endpoint,protocol in [('https://api.modelarts-maas.com/v2','openai_compatible'),
                                  ('https://api.modelarts-maas.com/openai/v1','maas_standard_v2')]:
            with self.assertRaises(ValueError):
                BudgetedTransport({'endpoint':endpoint,'api_protocol':protocol},ledger,
                    self.root/'wrong_route_calls',key='test-secret-only',http_session=FakeHTTP())
        self.assertEqual(ledger.summary()['requests'],0)

    def test_actual_charge_above_reservation_is_persisted_then_halts(self):
        ledger = BudgetLedger(self.root/'overage.sqlite', 10)
        ledger.reserve('overage', 0.2, case_id='c', role='doctor', model='test')
        with self.assertRaises(BudgetExceeded):
            ledger.settle('overage', amount=0.4, status='completed', usage={'prompt_tokens': 50})
        restored = BudgetLedger(self.root/'overage.sqlite', 10)
        self.assertEqual(restored.summary()['accounted_upper_cny'], .4)
        self.assertEqual(restored.summary()['reserved_cny'], 0)
        self.assertTrue(restored.summary()['halted_for_reconciliation'])
        self.assertEqual(restored.identity(), ledger.identity())
        with self.assertRaises(BudgetExceeded):
            restored.reserve('later', .1, case_id='c', role='doctor', model='test')
        self.assertEqual(restored.summary()['requests'], 1)

    def test_budget_refusal_latches_stop_and_new_ledger_has_new_identity(self):
        ledger = BudgetLedger(self.root/'original.sqlite', 1)
        ledger.reserve('first', .8, case_id='c', role='doctor', model='test')
        ledger.settle('first', amount=.8, status='completed')
        with self.assertRaises(BudgetExceeded):
            ledger.reserve('too-large', .3, case_id='c', role='doctor', model='test')
        with self.assertRaises(BudgetExceeded):
            ledger.reserve('smaller', .1, case_id='c', role='doctor', model='test')
        self.assertTrue(ledger.summary()['new_requests_stopped'])
        self.assertFalse(ledger.summary()['halted_for_reconciliation'])
        other = BudgetLedger(self.root/'different.sqlite', 1)
        self.assertNotEqual(ledger.identity()['ledger_id'], other.identity()['ledger_id'])

    def test_user_attestation_is_explicit_scoped_and_not_independent_verification(self):
        endpoint = 'https://api.modelarts-maas.com/openai/v1'
        policy = {'mode': 'user_attested', 'authorization_id': 'test-authorization-1',
                  'confirmed_at': '2026-11-07T16:00:00+08:00', 'endpoint': endpoint,
                  'data_kinds': ['mimic_derived'], 'dataset_scope': 'local_mimic_iv_2.2_and_derivatives',
                  'evidence': 'Explicit user confirmation after disclosure; synthetic test record.'}
        provenance = check_data_policy('mimic_derived', policy, endpoint)
        self.assertEqual(provenance['authorization_basis'], 'user_attested')
        self.assertEqual(provenance['independent_provider_verification'], 'not_established_by_user_attestation')
        self.assertNotIn('verified_checks', provenance)
        for key in ('authorization_id', 'confirmed_at', 'endpoint', 'evidence', 'data_kinds', 'dataset_scope'):
            bad = {k: v for k, v in policy.items() if k != key}
            with self.assertRaises(DataPolicyError):
                check_data_policy('mimic_derived', bad, endpoint)
        with self.assertRaises(DataPolicyError):
            check_data_policy('hospital_derived', policy, endpoint)
        with self.assertRaises(DataPolicyError):
            check_data_policy('mimic_derived', policy, endpoint+'/another')
        with self.assertRaises(DataPolicyError):
            check_data_policy('mimic_derived', {}, endpoint)
        ledger = BudgetLedger(self.root/'attested.sqlite', 1)
        transport = BudgetedTransport({'endpoint': endpoint, 'data_policy': policy, 'max_attempts': 1, 'generation_policy': deepcopy(GENERATION_POLICY_V2)},
            ledger, self.root/'attested_calls', key='test-secret-only', http_session=FakeHTTP())
        _, rid = transport.complete([{'role': 'user', 'content': 'Constructed test only; no patient data.'}],
            model='glm-5.3', case_id='synthetic-policy-test', role='rx_ai', data_kind='mimic_derived')
        record = json.loads((self.root/'attested_calls'/(rid+'.json')).read_text())
        self.assertEqual(record['data_policy_provenance'], provenance)

    def test_independently_verified_policy_remains_a_separate_path(self):
        endpoint = 'https://api.modelarts-maas.com/openai/v1'
        policy = {'mode': 'independently_verified', 'endpoint': endpoint,
            'verified_at': '2026-11-07T00:00:00Z', 'evidence': 'Test evidence only',
            'authorized_data_access': True, 'verified_no_retention': True,
            'no_training': True, 'no_human_review': True, 'cache_policy_verified': True}
        result = check_data_policy('mimic_derived', policy, endpoint)
        self.assertEqual(result['authorization_basis'], 'independently_verified')
        self.assertEqual(len(result['verified_checks']), 5)
        policy['verified_no_retention'] = False
        with self.assertRaises(DataPolicyError):
            check_data_policy('mimic_derived', policy, endpoint)

    def test_formal_use_requires_specific_processing_attestation(self):
        endpoint = 'https://api.modelarts-maas.com/openai/v1'
        policy = {'mode': 'user_attested', 'authorization_id': 'test-formal-authorization',
                  'confirmed_at': '2026-11-07T16:00:00+08:00', 'endpoint': endpoint,
                  'data_kinds': ['mimic_derived'], 'dataset_scope': 'local_mimic_iv_2.2_and_derivatives',
                  'evidence': 'Test record of explicit processing confirmation.'}
        with self.assertRaises(DataPolicyError):
            check_data_policy('mimic_derived', policy, endpoint, formal=True)
        processing = {'scope': 'endpoint_and_upstream', 'zero_retention': True,
                      'no_training': True, 'no_human_review': True}
        policy['processing_conditions'] = processing
        result = check_data_policy('mimic_derived', policy, endpoint, formal=True)
        self.assertEqual(result['attested_processing'], processing)
        self.assertEqual(result['independent_provider_verification'], 'not_established_by_user_attestation')
        self.assertNotIn('verified_checks', result)
        for field in processing:
            incomplete = {key: value for key, value in processing.items() if key != field}
            with self.assertRaises(DataPolicyError):
                check_data_policy('mimic_derived', {**policy, 'processing_conditions': incomplete},
                                  endpoint, formal=True)

    def test_fixed_generation_settings_route_roles_and_tools_without_overrides(self):
        ledger, http, transport = self.provider()
        for role, model in [('doctor', 'deepseek-v4.1-flash-1017'),
                            ('rx_ai', 'deepseek-v4.1-flash-1017'), ('scribe', 'glm-5.3')]:
            transport.complete([], model=model, case_id='c', role=role, data_kind='synthetic_independent')
        role, ds_tool, glm_tool = [call[1]['json'] for call in http.calls]
        self.assertEqual(role['chat_template_kwargs'], {'thinking': False})
        self.assertNotIn('reasoning_effort', role)
        self.assertEqual(ds_tool['chat_template_kwargs'], {'thinking': True})
        self.assertEqual(ds_tool['reasoning_effort'], 'low')
        self.assertNotIn('chat_template_kwargs', glm_tool)
        self.assertEqual(glm_tool['reasoning_effort'], 'low')
        for body in (role, ds_tool, glm_tool):
            self.assertEqual(body['max_completion_tokens'], 8192)
            self.assertEqual(body['temperature'], .2)
        count = len(http.calls)
        with self.assertRaises(ValueError):
            transport.complete([], model='glm-5.3', case_id='c', role='scribe',
                data_kind='synthetic_independent', max_completion_tokens=2048)
        transport.config.pop('generation_policy')
        with self.assertRaises(ValueError):
            transport.complete([], model='glm-5.3', case_id='c', role='scribe', data_kind='synthetic_independent')
        self.assertEqual(len(http.calls), count)

    def test_v3_parsing_policy_is_explicit_and_not_an_unsupported_api_parameter(self):
        ledger,http,transport=self.provider()
        transport.config['generation_policy']=deepcopy(GENERATION_POLICY_V3)
        _,rid=transport.complete([],model='glm-5.3',case_id='c',role='scribe',data_kind='synthetic_independent')
        body=http.calls[0][1]['json']
        self.assertNotIn('response_format',body)
        self.assertNotIn('response_parsing',body)
        self.assertEqual(body['reasoning_effort'],'low')
        self.assertEqual(body['max_completion_tokens'],8192)
        evidence=json.loads((self.root/'calls'/(rid+'.json')).read_text())
        self.assertEqual(evidence['generation_policy']['response_parsing'],GENERATION_POLICY_V3['response_parsing'])
        self.assertNotIn('response_parsing',GENERATION_POLICY_V2)


if __name__ == '__main__':
    unittest.main()
