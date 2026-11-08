from copy import deepcopy
import unittest
from sandbox.experiments.linked.contracts import TaskCondition
from sandbox.experiments.linked.clinical import LinkedEncounterRunner
from sandbox.experiments.linked.rules import EngineeringPPRS
from sandbox.experiments.linked.prescription_variants import make_route_variant,continuation_state
from sandbox.experiments.linked.variant_analysis import audit_route_run,summarize_route_rows
from test_prescription_variants import case,Factory,Port


class AnalysisTests(unittest.TestCase):
    def fixture(self):
        c=case();anchor=LinkedEncounterRunner(Factory(),Port(),EngineeringPPRS(c['formulary'])).run_case(c,run_id='source')
        v=make_route_variant(anchor['stage_inputs']['prescription'],c['formulary'])
        run=LinkedEncounterRunner(Factory(),Port(),EngineeringPPRS(c['formulary']),close_prescription_loop=True).run_task(
            c,TaskCondition('prescription','pprs_baseline'),run_id='controlled',stage_input=v['stage_input'],
            continuation_state=continuation_state(anchor,v,c['formulary']),controlled_variant=v)
        self.assertEqual(run['status'],'completed')
        return c,v,run

    def test_paid_repair_never_overwrites_first_approval_or_adds_a_patient(self):
        c,v,run=self.fixture()
        # Construct a metric fixture with an observed permissive first review.
        run['artifacts']['original_prescription_review']['decision']='approve'
        next(e for e in run['events'] if e['event']=='prescription_reviewed' and e['revision']==0)['decision']='approve'
        row=audit_route_run(run,v,c['formulary'])
        self.assertTrue(row['first_review_approved_known_route_fault'])
        self.assertEqual(row['final_target_route_state'],'target_route_restored')
        self.assertFalse(row['paid_repair_branch_observed'])  # The port is explicitly a test double.
        # Separate synthetic metric fixture for the positive evidence branch;
        # these fabricated unit-test events are never exported as experiment data.
        for e in run['events']:
            if e.get('event')=='business_operation':e['response']['native_effect']=True
        row=audit_route_run(run,v,c['formulary'])
        self.assertTrue(row['paid_repair_branch_observed'])
        self.assertIsNone(row['rejection_reason_targets_injected_fault'])
        self.assertFalse(row['counts_as_new_patient_or_complete_encounter'])
        summary=summarize_route_rows([row])
        self.assertEqual(summary['complete_encounters_added'],0)
        self.assertEqual(summary['configurations'][0]['approved_known_route_fault'],1)
        self.assertIsNone(summary['configurations'][0]['specificity'])

    def test_missing_native_payment_evidence_is_not_inferred_from_completed_status(self):
        c,v,run=self.fixture()
        run['events']=[e for e in run['events'] if e.get('operation')!='payment.commit']
        row=audit_route_run(run,v,c['formulary'])
        self.assertFalse(row['paid_repair_branch_observed'])

    def test_endpoint_tampering_and_false_complete_encounter_are_rejected(self):
        c,v,run=self.fixture()
        wrong=deepcopy(run);wrong['artifacts']['original_prescription_review']['decision']='approve'
        with self.assertRaises(ValueError):audit_route_run(wrong,v,c['formulary'])
        wrong=deepcopy(run);wrong['native_complete']=True
        with self.assertRaises(ValueError):audit_route_run(wrong,v,c['formulary'])

    def test_zero_observed_review_denominator_is_unknown_and_missing_rows_stay(self):
        row={'config_id':'prescription:pprs_baseline','status':'not_run_provider_unavailable'}
        summary=summarize_route_rows([row])['configurations'][0]
        self.assertEqual(summary['planned_candidates'],1)
        self.assertEqual(summary['first_review_decisions'],{'not_observed':1})
        self.assertIsNone(summary['approval_fraction_among_observed'])


if __name__=='__main__':unittest.main()
