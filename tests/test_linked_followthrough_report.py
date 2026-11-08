from copy import deepcopy
import importlib.util
from pathlib import Path
import unittest

import test_linked_prescription_followthrough as fixtures

root=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('report',root/'tools/report_prescription_followthrough.py')
report=importlib.util.module_from_spec(spec);spec.loader.exec_module(report)
spec2=importlib.util.spec_from_file_location('walkthrough',root/'tools/export_linked_case_walkthrough.py')
walkthrough=importlib.util.module_from_spec(spec2);spec2.loader.exec_module(walkthrough)


class ReportTests(unittest.TestCase):
    def test_original_defer_and_later_payment_are_distinct_observations(self):
        result,*_=fixtures.FollowthroughTests().run_branch()
        row=report.summarize_run(result)
        self.assertEqual(row['original_review_decision'],'defer')
        self.assertEqual(row['observed_terminal_outcome'],'approved_documented_and_paid')
        self.assertIsNone(row['clinical_correctness'])
        self.assertFalse(row['whole_encounter_completion_claimed'])

    def test_plan_is_not_a_completed_test_or_drug_payment(self):
        result,*_=fixtures.FollowthroughTests().run_branch(fixtures.RepairFactory(unresolved=True))
        row=report.summarize_run(result)
        self.assertEqual(row['observed_terminal_outcome'],'non_dispensing_plan_documented')
        self.assertFalse(row['drug_payment_observed'])
        self.assertFalse(row['planned_investigation_or_referral_executed'])

    def test_paid_flag_without_bound_readback_is_not_enough(self):
        result,*_=fixtures.FollowthroughTests().run_branch()
        broken=deepcopy(result)
        for event in broken['events']:
            if event.get('operation')=='payment.commit':event['patient_id']='other'
        self.assertEqual(report.summarize_run(broken)['observed_terminal_outcome'],'not_closed')

    def test_overwritten_original_review_is_rejected(self):
        result,*_=fixtures.FollowthroughTests().run_branch()
        result['artifacts']['original_prescription_review']=deepcopy(result['artifacts']['prescription_versions'][-1]['review'])
        with self.assertRaisesRegex(ValueError,'Original review'):report.summarize_run(result)

    def test_failure_before_first_review_is_retained_as_unclosed(self):
        result,source,*_=fixtures.FollowthroughTests().run_branch(state_transform=lambda s:s.update(stage_input_hash='wrong'))
        row=report.summarize_run(result)
        self.assertEqual(row['observed_terminal_outcome'],'not_closed')
        self.assertIsNone(row['original_review_decision'])

    def test_walkthrough_refuses_mimic_cases(self):
        with self.assertRaisesRegex(ValueError,'fictional'):
            walkthrough.build_sections({'data_kind':'mimic_derived'},{'data_kind':'mimic_derived'})


if __name__=='__main__':unittest.main()
