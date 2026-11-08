from collections import Counter
from copy import deepcopy
import unittest

from tools.prepare_acl_submission_inputs import build_safety_schedule, classify_product
from sandbox.experiments.linked.cases import GROUPS
from sandbox.experiments.linked.analysis_counts import analyze
from test_analysis_counts import dataset


class SubmissionPreparationTests(unittest.TestCase):
    def test_fixed_disease_operation_allocation_without_run_outcome_selection(self):
        cases = [{'case_id':f'{g}-{i}','patient_key':f'{g}-patient-{i}',
                  'group':g,'split':'test'} for g in GROUPS for i in range(400)]
        scheduled = build_safety_schedule(cases)
        self.assertEqual(scheduled,build_safety_schedule(list(reversed(cases))))
        self.assertEqual(len(scheduled),1000)
        self.assertEqual(len({x['patient_key'] for x in scheduled}),1000)
        counts = Counter((r['group'],r['operation_category'],r['request_condition']) for r in scheduled)
        self.assertEqual(len(counts),50)
        self.assertTrue(all(n==(8 if k[2]=='single_fault' else 32) for k,n in counts.items()))
        self.assertEqual(Counter(x['request_condition'] for x in scheduled),{'single_fault':200,'compliant_control':800})
        bad=deepcopy(cases);bad[0]['split']='development'
        with self.assertRaises(ValueError):build_safety_schedule(bad)
        bad=deepcopy(cases);bad[1]['patient_key']=bad[0]['patient_key']
        with self.assertRaises(ValueError):build_safety_schedule(bad)

    def test_atc_class_does_not_collapse_release_form_or_create_dose(self):
        a={'drug_code':'ir','ingredient':'metformin','dosage_form':'immediate_release','strength':'500 mg/tablet','allowed_routes':['oral']}
        b={**a,'drug_code':'er','dosage_form':'extended_release'}
        one,two=[classify_product(x,{'A10BA02'}) for x in (a,b)]
        self.assertEqual(one['atc_code'],two['atc_code'])
        self.assertNotEqual(one['dosage_form'],two['dosage_form'])
        self.assertNotIn('dose',one)
        self.assertFalse(one['ddd_used_as_prescription_dose'])
        self.assertIsNone(classify_product({**a,'ingredient':'unmapped'},set()))

    def test_bootstrap_preserves_stratum_composition_and_patient_pairing(self):
        d=dataset()
        for i,u in enumerate(d['planned_units']):u['group']='even' if i%2==0 else 'odd'
        d['bootstrap']['stratify_by_disease']=True
        out=analyze(d)
        a=out['configurations']['a']['metrics']['factual_error']['bootstrap']
        self.assertEqual(a['stratum_patient_counts'],{'even':2,'odd':2})
        self.assertEqual(a['ci'],[.5,.5])
        bad=deepcopy(d);bad['planned_units'][1]['patient_key']='p0'
        with self.assertRaises(ValueError):analyze(bad)
        bad=deepcopy(d);bad['planned_units'][0].pop('group')
        with self.assertRaises(ValueError):analyze(bad)


if __name__=='__main__':unittest.main()
