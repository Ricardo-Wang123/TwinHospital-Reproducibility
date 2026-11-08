from copy import deepcopy
import unittest

from sandbox.experiments.linked.case_preparation.recover_scaled_inputs_v2 import initial_case, select_material
from sandbox.experiments.linked.case_preparation.current_episode_v4 import regimen_candidate


def package(orders):
    return {'case': {'case_id': 'fictional', 'patient_key': 'fixture',
        'source_anchor_encounter_ref': 'anchor', 'clinical': {
            'demographics': {'age_at_source_admission': {'status': 'approximate'}, 'sex_recorded': 'M'},
            'history': {'encounters': []}, 'medications': {'orders': orders},
            'observations': [], 'allergies': {'status': 'unknown', 'items': []}}}}


def order(name='Ipratropium-Albuterol Neb', daily='8', route='NEB', offset=-12):
    return {'record_id': 'fixture-order', 'source_encounter_ref': 'anchor',
        'prescription_component_type_raw': 'MAIN', 'drug_name_raw': name,
        'time': {'event_offset_hours': offset}, 'regimen_raw': {'route': route,
            'dose_val_rx': '1', 'dose_unit_rx': 'NEB', 'doses_per_24_hrs': daily,
            'prod_strength': '(0.5mg - 3 mg) - 3 mL Neb'}}


class RecoveryTests(unittest.TestCase):
    def test_infection_does_not_adopt_oral_vancomycin_as_pneumonia_treatment(self):
        source = package([order('Vancomycin', '4', 'PO')])
        before = deepcopy(source)
        chosen, unresolved, reasons = select_material(source, 'pneumonia')
        self.assertIsNone(chosen)
        self.assertFalse(unresolved)
        self.assertEqual(reasons['oral_vancomycin_not_pneumonia_treatment'], 1)
        self.assertEqual(source, before)

    def test_chronic_unsupported_count_stays_unresolved_without_invented_use(self):
        source = package([order()]); before = deepcopy(source)
        seed = {'patient': {'anchor_age': '50', 'anchor_year': '2100'},
                'anchor_admission': {'dischtime': '2100-01-01 00:00:00'}}
        case, audit = initial_case(source, seed, {'group': 'copd', 'split': 'test'}, 0)
        self.assertTrue(audit['chronic_source_pattern_unresolved'])
        self.assertEqual(case['split'], 'test')
        self.assertEqual(case['provenance']['selected_historical_order'], order())
        facts = case['patient_initial']['patient_facts']
        self.assertEqual(next(f for f in facts if f['topic']=='recent_administration')['status'], 'unknown')
        parsed = regimen_candidate([order()], {'fixture-order': {'join_status':'unique_agreement','frequency':'Q3H'}})
        self.assertEqual(parsed['candidate_state'], 'unresolved_not_assumed_taken')
        self.assertEqual(parsed['daily'], 8)
        self.assertEqual(source, before)

    def test_unknown_route_and_future_material_cannot_become_fallback(self):
        for row in (order(route='PO/NG'), order(offset=4)):
            with self.assertRaises(ValueError):
                select_material(package([row]), 'copd')


if __name__ == '__main__':
    unittest.main()
