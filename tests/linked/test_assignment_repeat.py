from copy import deepcopy
import unittest

from sandbox.experiments.linked.assignment_repeat import freeze, summarize, units, VERSION, PLAN_VERSION
from sandbox.experiments.linked.contracts import linked_config_id
from sandbox.experiments.linked.linked_acceptance import freeze_design, linked_profiles
from sandbox.experiments.linked.cases import GROUPS


class FixedAssignmentTests(unittest.TestCase):
    def setUp(self):
        self.cases = [{'case_id':f'case-{n}', 'patient_key':f'patient-{n}', 'group':group,
                       'data_kind':'mimic_derived','split':'development'}
                      for n,group in enumerate(group for group in GROUPS for _ in range(2))]
        self.profiles,_=linked_profiles({'tool_a':'glm-5.3'},factorial=True)
        self.base=freeze_design(self.cases,{'tool_a':'glm-5.3'},patients_per_group=2,
                               workers=20,max_attempts=1,factorial=True)
        labels=list(self.profiles)
        selected=[(case,labels[0]) for case in self.cases]+[(self.cases[0],labels[1])]
        self.plan={'version':PLAN_VERSION,'planned_assignments':len(selected),'source_case_count':len(self.cases),
            'assignments':[{'case_id':case['case_id'],'group':case['group'],'profile':label,
                'config_id':linked_config_id(self.profiles[label]),'source_trace_sha256':'0'*64}
                for case,label in selected]}

    def frozen(self,plan=None):
        return freeze(self.base,plan or self.plan,self.cases,self.profiles,plan_sha256='1'*64)

    def test_repeats_exact_selected_assignments_without_expanding_the_factorial(self):
        design=self.frozen()
        self.assertEqual(design['version'],VERSION)
        self.assertEqual(design['planned_unique_encounters'],11)
        self.assertEqual(len(list(units(self.cases,design))),11)
        self.assertNotIn('target_per_disease_and_condition',design)
        self.assertEqual(len(design['profiles']),27)
        self.assertEqual(self.base['planned_unique_encounters'],270)

    def test_unknown_duplicate_or_changed_clinical_assignments_are_rejected(self):
        for field,value in [('case_id','unknown'),('profile','unknown'),('config_id','changed')]:
            plan=deepcopy(self.plan);plan['assignments'][0][field]=value
            with self.assertRaises(ValueError):self.frozen(plan)
        plan=deepcopy(self.plan);plan['assignments'][-1]=deepcopy(plan['assignments'][0])
        with self.assertRaises(ValueError):self.frozen(plan)

    def test_audit_retains_failures_and_detects_a_duplicated_source_patient(self):
        design=self.frozen()
        rows=[{'study_id':str(n),'case_id':a['case_id'],'profile':a['profile'],
               'group':a['group'],'verified_paid':True} for n,a in enumerate(self.plan['assignments'])]
        self.assertTrue(summarize(design,rows)['passed'])
        rows[0]['verified_paid']=False
        self.assertEqual(summarize(design,rows)['planned_unique_encounters'],11)
        self.assertFalse(summarize(design,rows)['passed'])
        rows[0]['verified_paid']=True;rows[1]['case_id']=rows[0]['case_id']
        self.assertFalse(summarize(design,rows)['passed'])
