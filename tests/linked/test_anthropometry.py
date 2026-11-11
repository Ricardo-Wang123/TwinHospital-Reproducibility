import copy
import unittest

from sandbox.experiments.linked.case_preparation.anthropometry import author


def case():
    return {'case_id': 'fictional', 'patient_initial': {'memory': 'does not know weight'},
            'his_initial': {'demographics': {'sex': 'F', 'age': 78},
                'historical_outpatient_measurements': [],
                'current_observations': [{'name': 'temperature', 'value': 36.8, 'unit': 'degC',
                    'available_at': 'physician_consultation',
                    'time': {'event_offset_hours': 168, 'recorded_offset_hours': 168}}]}}


def historical(name, value, date, ident):
    return {'record_id': ident, 'display': name, 'value_raw': value,
            'time': {'event_offset_upper_hours': date, 'recorded_offset_upper_hours': date,
                     'available_by_source_cutoff': True}}


class AnthropometryTests(unittest.TestCase):
    def test_latest_eligible_value_and_large_source_weight_are_preserved(self):
        source = case()
        source['his_initial']['historical_outpatient_measurements'] = [
            historical('Weight (Lbs)', '100', -20, 'old'),
            historical('Weight (Lbs)', '433', -10, 'large'),
            historical('Weight (Lbs)', '120', 25, 'future'),
            historical('Height (Inches)', '72', -10, 'height')]
        output, changes = author(source)
        self.assertEqual([r['value'] for r in output['his_initial']['current_observations'][-2:]], [196.4, 182.9])
        self.assertEqual(changes[0]['source_record_ids'], ['large'])
        self.assertTrue(all(r['source_kind'] == 'constructed' for r in output['his_initial']['current_observations'][-2:]))

    def test_same_date_weight_bmi_can_initialize_height_without_claiming_a_source_measurement(self):
        source = case()
        source['his_initial']['historical_outpatient_measurements'] = [
            historical('Weight (Lbs)', str(100 / .45359237), -10, 'weight'),
            historical('BMI (kg/m2)', '25', -10, 'bmi')]
        output, changes = author(source)
        self.assertEqual(output['his_initial']['current_observations'][-1]['value'], 200.0)
        self.assertEqual(changes[-1]['source_record_ids'], ['weight', 'bmi'])
        self.assertFalse(changes[-1]['source_is_current_patient_measurement'])

    def test_reference_baseline_requires_explicit_authoring_and_is_not_patient_memory(self):
        source = case()
        frozen = copy.deepcopy(source)
        with self.assertRaises(ValueError):
            author(source)
        output, changes = author(source, allow_reference_scenario=True, parent_file_sha256='a' * 64)
        self.assertEqual(source, frozen)
        self.assertEqual(output['patient_initial'], source['patient_initial'])
        self.assertEqual(output['his_initial']['historical_outpatient_measurements'], [])
        self.assertEqual([r['value'] for r in changes], [73.5, 161.2])
        self.assertTrue(all(r['method'] == 'independently_authored_adult_reference_scenario' for r in changes))
        self.assertEqual(author(output, allow_reference_scenario=True)[0], output)

    def test_ambiguous_units_and_nonfinite_values_do_not_become_source_measurements(self):
        source = case()
        source['his_initial']['historical_outpatient_measurements'] = [
            historical('Weight', '100', -10, 'ambiguous'),
            historical('Weight (Lbs)', 'NaN', -5, 'nonfinite')]
        _, changes = author(source, allow_reference_scenario=True)
        self.assertEqual(changes[0]['source_record_ids'], [])

    def test_values_do_not_depend_on_regimen_lab_or_experiment_outcome(self):
        source = case()
        one, _ = author(source, allow_reference_scenario=True)
        source['evaluation'] = {'desired_outcome': 'approve'}
        source['his_initial']['historical_orders'] = [{'drug': 'a different drug'}]
        source['his_initial']['current_observations'][0]['value'] = 40
        two, _ = author(source, allow_reference_scenario=True)
        self.assertEqual(one['his_initial']['current_observations'][-2:], two['his_initial']['current_observations'][-2:])

    def test_invalid_current_measurement_is_not_silently_replaced(self):
        source = case()
        source['his_initial']['current_observations'].append({'name': 'weight', 'value': 100, 'unit': 'lb'})
        with self.assertRaises(ValueError):
            author(source, allow_reference_scenario=True)

    def test_unknown_sex_or_under_twenty_needs_an_authored_value(self):
        for update in ({'sex': 'unknown'}, {'age': 19}, {'age': '90+'}):
            source = case()
            source['his_initial']['demographics'].update(update)
            with self.assertRaises(ValueError):
                author(source, allow_reference_scenario=True)


if __name__ == '__main__':
    unittest.main()
