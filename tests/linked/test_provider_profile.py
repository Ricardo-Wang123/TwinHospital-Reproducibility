"""Provider migration tests use only independent fictional messages and mock HTTP."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from sandbox.experiments.linked.provider import BudgetedTransport, BudgetLedger, ModelCallError, DataPolicyError
from sandbox.experiments.linked.provider_profile import VERSION, PROTOCOL, validate
from sandbox.experiments.linked.json_boundary import POLICY as JSON_POLICY
from sandbox.experiments.linked.provider_content import POLICY as CONTENT_POLICY
from sandbox.experiments.linked.camel_provider import BudgetedCamelFactory
from sandbox.experiments.linked.contracts import paper_conditions


def profile():
    model_names = ['test-role-v1', 'test-tool-a-v1', 'test-tool-b-v1']
    return {'api_protocol': PROTOCOL, 'endpoint': 'https://api.example.invalid/v1',
        'credential_scope': {'credential_id': 'test-key-binding', 'endpoint': 'https://api.example.invalid/v1'},
        'role_model': model_names[0], 'tool_models': dict(zip(['tool_a','tool_b'],model_names[1:])),
        'pricing_cny_per_million_tokens': {m:[1.,2.] for m in model_names},
        'pricing_evidence': 'independent test fixture; not a vendor tariff', 'max_attempts': 1,
        'generation_policy': {'version':VERSION, 'response_parsing':deepcopy(JSON_POLICY),
            'provider_content_boundary':deepcopy(CONTENT_POLICY),
            'models':{m:{'thinking':False, 'temperature':0.2, 'max_completion_tokens':256,
                'completion_limit_field':'max_tokens','completion_limit_includes_reasoning':False,
                'context_window_tokens':32768,'request_parameters':{}} for m in model_names}}}


class Response:
    status_code = 200
    def __init__(self, model):
        self.value={'id':'chat-fixture','object':'chat.completion','created':1,'model':model,
            'usage':{'prompt_tokens':20,'completion_tokens':10,'total_tokens':30},
            'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':'{"ok":true}'}}]}
    def json(self): return self.value


class HTTP:
    def __init__(self): self.calls=[]; self.override_model=None
    def post(self,url,**kwargs):
        self.calls.append((url,deepcopy(kwargs)))
        return Response(self.override_model or kwargs['json']['model'])


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
    def tearDown(self): self.tmp.cleanup()
    def setup_transport(self,cfg=None):
        cfg=cfg or profile(); ledger=BudgetLedger(self.root/'fees.sqlite',None); http=HTTP()
        transport=BudgetedTransport(cfg,ledger,self.root/'calls',key='fictional-test-key',http_session=http)
        return cfg,ledger,http,transport
    def test_actual_camel_routes_fixed_roles_and_two_new_models(self):
        cfg,ledger,http,t=self.setup_transport()
        f=BudgetedCamelFactory(cfg,ledger,self.root/'calls',case_id='independent-fiction',
            data_kind='synthetic_independent',run_id='profile-test',transport=t)
        for role,label in [('patient',None),('doctor',None),('nurse',None),('scribe','tool_a'),('rx_ai','tool_b')]:
            self.assertEqual(f.session('test',role,label).respond({'instruction':'Return a JSON ok object'}),{'ok':True})
        self.assertEqual([r[1]['json']['model'] for r in http.calls],
            ['test-role-v1']*3+['test-tool-a-v1','test-tool-b-v1'])
        self.assertEqual(len(paper_conditions(model_families=cfg['tool_models'])),15)
        self.assertAlmostEqual(ledger.summary()['accounted_upper_cny'],.0002)
        for url,kw in http.calls:
            self.assertEqual(url,'https://api.example.invalid/v1/chat/completions')
            self.assertTrue(kw['verify']);self.assertFalse(kw['allow_redirects'])
            self.assertEqual(kw['json']['max_tokens'],256)
            self.assertNotIn('thinking',kw['json']);self.assertNotIn('chat_template_kwargs',kw['json'])
    def test_generic_wire_preserves_declared_reasoning_parameter_and_omits_temperature(self):
        cfg=profile();setting=cfg['generation_policy']['models']['test-tool-a-v1']
        setting.update(thinking=True,temperature=None,completion_limit_includes_reasoning=True,
                       request_parameters={'enable_thinking':True})
        cfg,ledger,http,t=self.setup_transport(cfg)
        t.complete([],model='test-tool-a-v1',case_id='fiction',role='scribe',data_kind='synthetic_independent')
        body=http.calls[0][1]['json'];self.assertTrue(body['enable_thinking']);self.assertNotIn('temperature',body)

    def test_single_tool_uses_the_same_model_in_every_assisted_workflow(self):
        cfg=profile();unused=cfg['tool_models'].pop('tool_b')
        del cfg['generation_policy']['models'][unused]
        del cfg['pricing_cny_per_million_tokens'][unused]
        cfg,ledger,http,t=self.setup_transport(cfg)
        f=BudgetedCamelFactory(cfg,ledger,self.root/'calls',case_id='independent-fiction',
            data_kind='synthetic_independent',run_id='single-tool-test',transport=t)
        for role,label in [('patient',None),('doctor',None),('preconsult_ai','tool_a'),('rx_ai','tool_a'),('scribe','tool_a')]:
            self.assertEqual(f.session('test',role,label).respond({'instruction':'Return JSON'}),{'ok':True})
        self.assertEqual([r[1]['json']['model'] for r in http.calls],['test-role-v1']*2+['test-tool-a-v1']*3)
        self.assertEqual(len(paper_conditions(model_families=cfg['tool_models'])),9)
        with self.assertRaises(KeyError):f.session('test','scribe','tool_b')
    def test_profile_rejects_key_destination_drift_and_reserved_parameters(self):
        for field in ['endpoint','request','price','role']:
            cfg=profile()
            if field=='endpoint':cfg['endpoint']='https://different.example.invalid/v1'
            elif field=='request':cfg['generation_policy']['models'][cfg['role_model']]['request_parameters']={'model':'silent-substitution'}
            elif field=='price':cfg['pricing_cny_per_million_tokens'][cfg['role_model']]=[-1,2]
            else:cfg['role_model']='unlisted'
            with self.assertRaises(ValueError):validate(cfg)
    def test_restricted_data_permission_is_not_transferred_to_a_new_provider(self):
        cfg,ledger,http,t=self.setup_transport()
        with self.assertRaises(DataPolicyError):
            t.complete([],model=cfg['role_model'],case_id='restricted',role='doctor',data_kind='mimic_derived')
        self.assertEqual(http.calls,[]);self.assertEqual(ledger.summary()['requests'],0)
    def test_model_mismatch_is_billed_and_retained_but_not_used(self):
        cfg,ledger,http,t=self.setup_transport();http.override_model='different-server-model'
        with self.assertRaises(ModelCallError):
            t.complete([],model=cfg['role_model'],case_id='fiction',role='doctor',data_kind='synthetic_independent')
        self.assertEqual(len(http.calls),1);self.assertGreater(ledger.summary()['accounted_upper_cny'],0)
        record=json.loads(next((self.root/'calls').glob('*.json')).read_text())
        self.assertEqual(record['generation_contract_error'],'returned_model_differs_from_frozen_identity')
    def test_provider_rounding_margin_is_reserved_and_accounted(self):
        cfg=profile();cfg['request_rounding_margin_cny']=.000015
        cfg,ledger,http,t=self.setup_transport(cfg)
        t.complete([],model=cfg['role_model'],case_id='fiction',role='doctor',data_kind='synthetic_independent')
        record=json.loads(next((self.root/'calls').glob('*.json')).read_text())
        self.assertAlmostEqual(record['reserved_upper_cny'],(1026+512)/1e6+.000015)
        self.assertAlmostEqual(ledger.summary()['accounted_upper_cny'],.000055)
        for bad in (-1,float('nan'),True):
            cfg['request_rounding_margin_cny']=bad
            with self.assertRaises(ValueError):validate(cfg)
    def test_local_zero_fee_profile_keeps_accounting_and_requires_bound_loopback(self):
        cfg=profile();cfg['endpoint']='http://127.0.0.1:18888/v1';cfg['credential_scope']['endpoint']=cfg['endpoint']
        cfg['pricing_cny_per_million_tokens']={m:[0,0] for m in cfg['generation_policy']['models']}
        cfg,ledger,http,t=self.setup_transport(cfg)
        t.complete([],model=cfg['role_model'],case_id='fiction',role='doctor',data_kind='synthetic_independent')
        self.assertEqual(ledger.summary()['accounted_upper_cny'],0)
        self.assertEqual(ledger.summary()['requests'],1)
        cfg['endpoint']='http://remote.example.invalid/v1';cfg['credential_scope']['endpoint']=cfg['endpoint']
        with self.assertRaises(ValueError):validate(cfg)

    def test_explicit_provider_recovery_requires_all_selected_models(self):
        cfg,ledger,http,t=self.setup_transport()
        ledger.reserve('frozen',.01,case_id='probe',role='provider_probe',model=cfg['role_model'])
        ledger.settle('frozen',amount=0,status='http_403')
        ledger.halt_provider('frozen','ModelArts.81006')
        probes={}
        for i,model in enumerate(cfg['generation_policy']['models']):
            ident='healthy_'+str(i)
            ledger.reserve(ident,.1,case_id='provider_health_probe',role='provider_probe',model=model,health_probe=True)
            ledger.settle(ident,amount=.01,status='completed')
            probes[model]=ident
        with self.assertRaisesRegex(ValueError,'every model selected'):
            ledger.recover_provider(recovery_id='partial',config_sha256='a'*64,
                probes_by_model={cfg['role_model']:probes[cfg['role_model']]},evidence={'fixture':True},config=cfg)
        before=ledger.summary()['accounted_upper_cny']
        result=ledger.recover_provider(recovery_id='all_models',config_sha256='a'*64,
            probes_by_model=probes,evidence={'fixture':True},config=cfg)
        self.assertFalse(result['provider_unavailable'])
        self.assertEqual(result['accounted_upper_cny'],before)
        self.assertEqual(http.calls,[])


if __name__=='__main__': unittest.main()
