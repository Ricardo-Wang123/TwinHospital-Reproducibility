from copy import deepcopy
import unittest

from sandbox.experiments.linked.diagnosis_codes import canonicalize, all_mapped
from sandbox.experiments.linked.provider import generation_settings, GENERATION_POLICY_V9, GENERATION_POLICY_V10
from sandbox.experiments.linked.contracts import paper_conditions
from test_linked_clinical import FakeFactory, FakePort, FakeRules, example_case, LinkedEncounterRunner


CAT = [{'code':'I50.33','system':'ICD-10-CM','display':'Test catalogue entry'}]


class DiagnosisCodesTests(unittest.TestCase):
    def test_only_decimal_changes_and_source_decision_is_preserved(self):
        value=[{'code':'I5033','system':'ICD-10-CM','primary':True,'certainty':'working','basis':'test'}]
        before=deepcopy(value)
        result,changes=canonicalize(value,CAT)
        self.assertEqual(value,before)
        self.assertEqual(result,[{**before[0],'code':'I50.33'}])
        self.assertEqual(len(changes),1)
        self.assertTrue(all_mapped(result,CAT))

    def test_never_translate_system_or_infer_different_specificity(self):
        for code,system in [('I5033','ICD-10'),('I503','ICD-10-CM'),('I50..33','ICD-10-CM'),
                            ('I50.3.3','ICD-10-CM'),('i5033','ICD-10-CM')]:
            value=[{'code':code,'system':system}]
            self.assertEqual(canonicalize(value,CAT),(value,[]))
            self.assertFalse(all_mapped(value,CAT))

    def test_ambiguous_catalogue_is_not_arbitrarily_selected(self):
        value=[{'code':'I5033','system':'ICD-10-CM'}]
        self.assertEqual(canonicalize(value,CAT+[{**CAT[0],'code':'I5033'}]),(value,[]))

    def test_workflow_keeps_original_model_response_and_persists_exact_catalogue(self):
        class Port(FakePort):
            def execute(self,operation,payload,context):
                result=super().execute(operation,payload,context)
                if operation=='diagnosis.catalogue':result['data']={'items':deepcopy(CAT)}
                return result
        class Factory(FakeFactory):
            def session(self,*args,**kwargs):
                session=super().session(*args,**kwargs);original=session.respond
                def respond(obs):
                    value=original(obs)
                    if value.get('action')=='consultation_complete':
                        value['diagnoses']=[{'code':'I5033','system':'ICD-10-CM','primary':True,'certainty':'working','basis':'engineering fixture'}]
                    return value
                session.respond=respond
                return session
        port=Port();result=LinkedEncounterRunner(Factory(),port,FakeRules()).run_case(example_case(),run_id='diagnosis-display')
        self.assertEqual(result['status'],'completed')
        self.assertFalse(result['native_complete'])
        original=[r['response'] for r in result['calls'] if r.get('response',{}).get('action')=='consultation_complete'][0]
        self.assertEqual(original['diagnoses'][0]['code'],'I5033')
        saved=[p for op,p,_ in port.calls if op=='consultation.save'][0]
        self.assertEqual(saved['content']['diagnoses'][0]['code'],'I50.33')
        self.assertEqual(result['artifacts']['diagnosis_code_normalizations'][0]['original_response'],original)

    def test_qwen_tool_is_explicit_and_does_not_change_fixed_backbone_or_group_count(self):
        cfg={'role_model':'openpangu-2.0-flash','generation_policy':deepcopy(GENERATION_POLICY_V10)}
        self.assertEqual(generation_settings(cfg,'scribe','qwen3-32b')['tool_family'],'qwen')
        self.assertIsNone(generation_settings(cfg,'scribe','qwen3-32b').get('reasoning_effort'))
        with self.assertRaises(ValueError):generation_settings(cfg,'doctor','qwen3-32b')
        self.assertEqual(len(paper_conditions(model_families=('panguflash','qwen32b'))),15)
        cfg['generation_policy']=deepcopy(GENERATION_POLICY_V9)
        with self.assertRaises(ValueError):generation_settings(cfg,'scribe','qwen3-32b')


if __name__=='__main__':unittest.main()
