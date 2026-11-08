from copy import deepcopy
import unittest

from sandbox.experiments.linked.cases import build_synthetic_case, runtime_visible_case
from sandbox.experiments.linked.case_preparation.reported_allergy_episode_v12 import build, POLICY


class ReportedAllergyEpisodeTests(unittest.TestCase):
    def source(self):
        case = build_synthetic_case('uti', 1)
        case['data_kind'] = 'mimic_derived'  # Independently invented test fixture only.
        return case

    def test_report_is_new_hypothesis_not_source_negative_or_prescription(self):
        source = self.source(); before = deepcopy(source)
        case, audit = build(source)
        self.assertEqual(source, before)
        self.assertTrue(audit['scenario_changed'])
        self.assertEqual(case['his_initial']['allergy_record']['status'], 'unknown')
        report = case['his_initial']['allergy_record']['patient_report']
        self.assertEqual(report['source_kind'], 'constructed')
        self.assertEqual(report['verification_status'], 'patient_report_not_confirmed_absence')
        for field in ('historical_encounters', 'historical_orders', 'historical_observations'):
            self.assertEqual(case['his_initial'][field], before['his_initial'][field])
        self.assertEqual(case['patient_key'], source['patient_key'])
        self.assertEqual(case['split'], source['split'])
        self.assertEqual(case['data_kind'], 'mimic_derived')
        self.assertEqual(case['formulary'], source['formulary'])
        self.assertNotIn('provenance', runtime_visible_case(case))
        self.assertIsNone(case['evaluation']['prescription_labels'])
        self.assertFalse(POLICY['reads_execution_results'])

    def test_positive_source_item_risk_cue_and_diagnosis_are_preserved(self):
        for kind in ('item', 'risk', 'diagnosis'):
            source = self.source()
            if kind == 'item':
                source['his_initial']['allergy_record']['items'] = [{'drug': 'synthetic positive report'}]
            elif kind == 'risk':
                source['evaluation']['source_risk_cues'] = [{'reasons': ['historical_allergy_related_code_requires_reconciliation']}]
            else:
                source['his_initial']['historical_encounters'] = [{'diagnoses': [
                    {'display': 'Allergic urticaria', 'coding': {'version': '9', 'code': '7080'}}]}]
            case, audit = build(source)
            self.assertFalse(audit['scenario_changed'])
            self.assertEqual(case['patient_initial'], source['patient_initial'])
            self.assertEqual(case['his_initial'], source['his_initial'])
            self.assertTrue(audit['preservation_reasons'])

    def test_unrecognized_positive_or_source_derived_memory_is_never_overwritten(self):
        for kind in ('positive_authored', 'source_fact'):
            source = self.source()
            fact = next(f for f in source['patient_initial']['patient_facts'] if f['topic'] == 'allergy_history')
            if kind == 'positive_authored':
                fact['value'] = '我对青霉素过敏。'
            else:
                fact['source_kind'] = 'observed_source'
            case, audit = build(source)
            self.assertFalse(audit['scenario_changed'])
            self.assertEqual(case['patient_initial'], source['patient_initial'])
            self.assertEqual(case['his_initial'], source['his_initial'])


if __name__ == '__main__':
    unittest.main()
