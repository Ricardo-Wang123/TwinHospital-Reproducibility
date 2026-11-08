"""Counterfactual inputs must preserve source values, dates and uncertainty."""
from copy import deepcopy
import unittest

from sandbox.experiments.linked.cases import build_synthetic_case
from sandbox.experiments.linked.case_preparation.current_panel_v14 import build, source_result


def result(number, value, event, recorded, *, fluid='Blood', available=True):
    return {'record_id': 'obs_' + str(number).zfill(24), 'source_kind': 'observed_source',
        'display': 'Creatinine', 'fluid': fluid, 'numeric_value_raw': value,
        'value_raw': '___', 'unit_raw': 'mg/dL',
        'time': {'event_offset_hours': event, 'recorded_offset_hours': recorded,
                 'available_by_source_cutoff': available}}


def source():
    case = build_synthetic_case('type2_diabetes', 1)
    case['data_kind'] = 'mimic_derived'  # Invented fixture; no source patient.
    case['split'] = 'development'
    case['his_initial']['time_basis'] = {'new_visit_offset_hours': 168}
    case['his_initial']['historical_observations'] = [
        result(1, '0.8', -5, -4), result(2, '7.1', -3, -2),
        result(3, '0.9', -1, 1, available=False),
        result(4, '0.6', -1, 0, fluid='Urine')]
    case['his_initial']['current_observations'] = []
    return case


class CurrentPanelTests(unittest.TestCase):
    def test_latest_eligible_blood_value_is_retained_even_when_abnormal(self):
        old = source(); before = deepcopy(old)
        case, audit = build(old)
        self.assertEqual(old, before)
        self.assertEqual(case['patient_key'], old['patient_key'])
        self.assertEqual(case['split'], old['split'])
        self.assertEqual(case['patient_initial'], old['patient_initial'])
        self.assertEqual(case['evaluation'], old['evaluation'])
        self.assertEqual(case['his_initial']['historical_observations'], old['his_initial']['historical_observations'])
        row = case['his_initial']['current_observations'][0]
        self.assertEqual(row['value'], 7.1)
        self.assertEqual(row['source_ref'], 'obs_' + '2'.zfill(24))
        self.assertEqual(row['source_kind'], 'constructed')
        self.assertEqual(row['time']['event_offset_hours'], 168)
        self.assertFalse(row['time']['available_by_source_cutoff'])
        self.assertIn('counterfactual', row['construction_basis'])
        self.assertIn('not an observed new MIMIC result', row['construction_basis'])
        self.assertFalse(audit['prescription_preassigned'])
        self.assertIn('potassium', audit['missing'])
        self.assertNotIn('egfr', {o['name'] for o in case['his_initial']['current_observations']})

    def test_missing_blood_result_is_not_replaced_by_urine_or_normal_imputation(self):
        old = source()
        old['his_initial']['historical_observations'] = [result(1, '.9', -1, 0, fluid='Urine')]
        case, audit = build(old)
        self.assertEqual(case['his_initial']['current_observations'], [])
        self.assertEqual(audit['added_count'], 0)
        self.assertIn('creatinine', audit['missing'])

    def test_nonfinite_future_and_unavailable_rows_are_not_adopted(self):
        for value, event, recorded, available in [('nan', -1, 0, True), (True, -1, 0, True),
                (1.0, 1, 2, True), (1.0, -1, 0, False), (1.0, -1, -2, True)]:
            with self.subTest(value=value, event=event, available=available):
                his = {'historical_observations': [result(1, value, event, recorded, available=available)]}
                self.assertIsNone(source_result(his, {'Creatinine'}, 'mg/dL'))

    def test_existing_current_finding_is_never_overwritten(self):
        old = source(); current = {'record_id': 'obs_' + '8'*24, 'name': 'creatinine', 'value': 9.5,
            'unit': 'mg/dL', 'source_kind': 'constructed', 'available_at': 'physician_consultation'}
        old['his_initial']['current_observations'] = [current]
        case, audit = build(old)
        self.assertEqual(case['his_initial']['current_observations'], [current])
        self.assertEqual(audit['retained_existing'], ['creatinine'])

    def test_conflicting_existing_findings_and_unspecified_visit_fail_closed(self):
        old = source(); old['his_initial']['current_observations'] = [
            {'name': 'creatinine', 'value': 1}, {'name': 'creatinine', 'value': 9}]
        with self.assertRaises(ValueError): build(old)
        old = source(); del old['his_initial']['time_basis']
        with self.assertRaises(ValueError): build(old)


if __name__ == '__main__':
    unittest.main()
