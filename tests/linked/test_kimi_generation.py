from copy import deepcopy
import tempfile
from pathlib import Path
import unittest
from sandbox.experiments.linked.provider import (
    BudgetLedger,BudgetedTransport,ModelCallError,GENERATION_POLICY_V11,GENERATION_POLICY_V12,generation_settings)
from sandbox.experiments.linked.contracts import paper_conditions
from test_provider import FakeHTTP,FakeResponse


class KimiTests(unittest.TestCase):
    def config(self):
        return {'endpoint':'https://api.modelarts-maas.com/v2','api_protocol':'maas_standard_v2',
            'role_model':'kimi-k2.6','generation_policy':deepcopy(GENERATION_POLICY_V12),'max_attempts':1}

    def test_fixed_people_and_two_tool_conditions_use_explicit_nonreasoning_settings(self):
        cfg=self.config()
        for role,model in [('doctor','kimi-k2.6'),('scribe','kimi-k2.6'),('scribe','qwen3-30b-a3b')]:
            settings=generation_settings(cfg,role,model)
            self.assertIs(settings['thinking'],False);self.assertEqual(settings['temperature'],.6)
            self.assertIsNone(settings.get('reasoning_effort'))
        with self.assertRaises(ValueError):generation_settings(cfg,'doctor','qwen3-30b-a3b')
        cfg['generation_policy']=deepcopy(GENERATION_POLICY_V11)
        with self.assertRaises(ValueError):generation_settings(cfg,'scribe','kimi-k2.6')
        self.assertEqual(len(paper_conditions(model_families=('kimi26','qwen30b'))),15)
        self.assertIs(GENERATION_POLICY_V11['tools']['qwen']['thinking'],True)

    def test_kimi_uses_only_max_tokens_and_rejects_unexpected_reasoning(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);ledger=BudgetLedger(root/'ledger.sqlite')
            response=FakeResponse();response.value['model']='kimi-k2.6';http=FakeHTTP(response)
            transport=BudgetedTransport(self.config(),ledger,root/'calls',key='fixture',http_session=http)
            transport.complete([],model='kimi-k2.6',case_id='c',role='doctor',data_kind='synthetic_independent')
            body=http.calls[0][1]['json']
            self.assertEqual(body['max_tokens'],16384);self.assertNotIn('max_completion_tokens',body)
            self.assertEqual(body['thinking'],{'type':'disabled'})
            response.value['choices'][0]['message']['reasoning_content']='PRIVATE TEST FIXTURE'
            with self.assertRaises(ModelCallError):
                transport.complete([],model='kimi-k2.6',case_id='c',role='doctor',data_kind='synthetic_independent')
            self.assertEqual(ledger.summary()['requests'],2)
            self.assertGreater(ledger.summary()['accounted_upper_cny'],0)

    def test_truncation_retry_keeps_single_wire_limit_and_does_not_change_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);ledger=BudgetLedger(root/'ledger.sqlite');cfg=self.config();cfg['max_attempts']=3
            class SequenceHTTP(FakeHTTP):
                def post(self,*args,**kwargs):
                    self.calls.append((args,deepcopy(kwargs)))
                    r=FakeResponse();r.value['model']='kimi-k2.6'
                    r.value['choices'][0]['finish_reason']='length' if len(self.calls)==1 else 'stop'
                    return r
            http=SequenceHTTP();t=BudgetedTransport(cfg,ledger,root/'calls',key='fixture',http_session=http)
            t.complete([{'role':'user','content':'Synthetic fixture'}],model='kimi-k2.6',case_id='c',role='doctor',data_kind='synthetic_independent')
            bodies=[x[1]['json'] for x in http.calls]
            self.assertEqual([x['max_tokens'] for x in bodies],[16384,32768])
            self.assertTrue(all('max_completion_tokens' not in x for x in bodies))
            self.assertEqual(bodies[0]['messages'],bodies[1]['messages'])


if __name__=='__main__':unittest.main()
