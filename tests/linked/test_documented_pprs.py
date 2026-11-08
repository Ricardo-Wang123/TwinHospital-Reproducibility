from copy import deepcopy
import unittest

from sandbox.experiments.linked.documented_pprs import DocumentedCandidatePPRS, adapt_visible_inputs
from sandbox.experiments.linked.medication_catalogue import get_formulary
from test_clinical_faults import fixtures


class DocumentedPPRSAuditTests(unittest.TestCase):
    def test_existing_role_context_does_not_manufacture_current_exposure(self):
        _, rx, _, _ = fixtures()[0]
        del rx['items'][0]['order_id']
        del rx['items'][0]['status']
        del rx['items'][0]['exposure']
        before = deepcopy(rx)
        context = {'hospital_history': {'historical_orders': [{'drug_code': 'SIM-METFORMIN-500'}]},
                   'handoff': {}, 'consultation': {}, 'conversation': []}
        adapted = adapt_visible_inputs(rx, context)
        self.assertEqual(rx, before)
        self.assertNotIn('status', adapted['prescription']['items'][0])
        self.assertNotIn('exposure', adapted['prescription']['items'][0])
        self.assertEqual(adapted['clinical_context'], {})
        result = DocumentedCandidatePPRS(get_formulary()).review(rx, context)
        self.assertEqual(result['decision'], 'flag')
        self.assertTrue(result['development_only'])
        self.assertIsNone(result['clinical_gold_label'])

    def test_exact_documented_dose_evidence_has_scoped_decision_not_gold(self):
        rule, rx, ctx, _ = fixtures()[0]
        pprs = DocumentedCandidatePPRS(get_formulary(), rule_ids=[rule])
        baseline = pprs.review(rx, {'documented_rule_context': ctx})
        self.assertEqual(baseline['decision'], 'pass')
        self.assertEqual(baseline['clinical_clearance'], 'not_established')
        rx['items'][0]['dose'] = 1500
        result = pprs.review(rx, {'documented_rule_context': ctx})
        self.assertEqual(result['decision'], 'reject')
        self.assertEqual(result['structural_review']['decision'], 'pass')
        self.assertIsNone(result['clinical_gold_label'])
        self.assertEqual(result['reference_status'], 'independent_review_pending')

    def test_unknown_allergy_and_incomplete_reconciliation_never_pass(self):
        rule, rx, ctx, _ = fixtures()[1]
        ctx['allergies']['assessment_status'] = 'unknown'
        result = DocumentedCandidatePPRS(get_formulary(), rule_ids=[rule]).review(rx, ctx)
        self.assertEqual(result['decision'], 'flag')
        rule, rx, ctx, _ = fixtures()[0]
        ctx['medication_reconciliation']['status'] = 'incomplete'
        result = DocumentedCandidatePPRS(get_formulary(), rule_ids=[rule]).review(rx, ctx)
        self.assertEqual(result['decision'], 'flag')

    def test_hidden_label_or_identity_is_rejected_before_rule_evaluation(self):
        _, rx, ctx, _ = fixtures()[0]
        for forbidden in ('reference_candidate', 'gold_label', 'mutation_diff', 'subject_id'):
            context = {'documented_rule_context': deepcopy(ctx), forbidden: 'private'}
            with self.subTest(field=forbidden), self.assertRaises(ValueError):
                DocumentedCandidatePPRS(get_formulary()).review(rx, context)

    def test_unsupported_drug_scope_is_flagged_not_called_normal(self):
        _, rx, _, _ = fixtures()[0]
        rx['items'][0]['drug_code'] = 'SIM-ACETAMINOPHEN-500'
        result = DocumentedCandidatePPRS(get_formulary()).review(rx, {})
        self.assertEqual(result['decision'], 'flag')
        self.assertIn('outside_documented_clinical_rule_scope', result['flags'])


if __name__ == '__main__':
    unittest.main()
