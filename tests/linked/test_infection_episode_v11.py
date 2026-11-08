from copy import deepcopy
import unittest
from sandbox.experiments.linked.cases import build_synthetic_case,runtime_visible_case
from sandbox.experiments.linked.case_preparation.infection_episode_v11 import build,VISIT_HOURS


def source(group='pneumonia'):
    c=build_synthetic_case(group,1)
    c['data_kind']='mimic_derived'  # Entirely invented fixture exercising the restricted-source path.
    c['his_initial']['historical_observations']=[{
        'record_id':'obs_'+'1'*24,'source_kind':'observed_source',
        'display':'Creatinine','fluid':'Blood','numeric_value_raw':'7.1','unit_raw':'mg/dL',
        'time':{'event_offset_hours':-3,'recorded_offset_hours':-2,'available_by_source_cutoff':True}},
        {'record_id':'obs_'+'2'*24,'source_kind':'observed_source',
        'display':'Creatinine','fluid':'Blood','numeric_value_raw':'0.9','unit_raw':'mg/dL',
        'time':{'event_offset_hours':-1,'recorded_offset_hours':1,'available_by_source_cutoff':False}}]
    return c


class InfectionEpisodeTests(unittest.TestCase):
    def test_new_presentation_is_explicit_and_preserves_source_and_old_episode(self):
        old=source();before=deepcopy(old);case,audit=build(old)
        self.assertEqual(old,before)
        self.assertEqual(case['his_initial']['historical_observations'],old['his_initial']['historical_observations'])
        self.assertEqual(case['his_initial']['historical_orders'],old['his_initial']['historical_orders'])
        self.assertEqual(case['patient_key'],old['patient_key'])
        self.assertEqual(case['split'],old['split'])
        self.assertEqual(case['his_initial']['time_basis']['new_visit_offset_hours'],VISIT_HOURS)
        cr=next(r for r in case['his_initial']['current_observations'] if r['name']=='creatinine')
        self.assertEqual(cr['value'],7.1)  # No normalization and no post-cutoff result.
        self.assertEqual(cr['fluid'],'Blood')
        self.assertEqual(cr['source_kind'],'constructed')
        self.assertIn('persistence_assumption',cr['construction_basis'])
        self.assertFalse(cr['time']['available_by_source_cutoff'])
        self.assertEqual(case['provenance']['source_allergy_record'],old['his_initial']['allergy_record'])
        self.assertEqual(case['his_initial']['allergy_record']['patient_report']['status'],'unable_to_ascertain')
        self.assertIsNone(case['evaluation']['prescription_labels'])
        self.assertFalse(audit['prescription_preassigned'])
        self.assertNotIn('evaluation',runtime_visible_case(case))

    def test_missing_source_safety_measurement_is_not_filled_with_a_normal_value(self):
        old=source();old['his_initial']['historical_observations']=[]
        case,audit=build(old)
        names={r['name'] for r in case['his_initial']['current_observations']}
        self.assertNotIn('creatinine',names);self.assertNotIn('potassium',names)
        self.assertEqual(audit['constructed_lab_persistence_count'],0)

    def test_newer_urine_or_unknown_specimen_never_replaces_blood_result(self):
        old=source()
        for fluid in ('Urine',None):
            other=deepcopy(old['his_initial']['historical_observations'][0])
            other.update(record_id='obs_'+'3'*24,fluid=fluid,numeric_value_raw='0.8')
            other['time'].update(event_offset_hours=-1,recorded_offset_hours=0)
            case_input=deepcopy(old);case_input['his_initial']['historical_observations'].append(other)
            case,_=build(case_input)
            cr=next(r for r in case['his_initial']['current_observations']if r['name']=='creatinine')
            self.assertEqual(cr['value'],7.1)
            case_input['his_initial']['historical_observations']=[other]
            case,_=build(case_input)
            self.assertNotIn('creatinine',{r['name']for r in case['his_initial']['current_observations']})

    def test_patient_view_has_symptoms_but_no_imaging_or_numeric_renal_results(self):
        case,_=build(source())
        patient=str(case['patient_initial'])
        self.assertNotIn('creatinine',patient);self.assertNotIn('7.1',patient)
        self.assertNotIn('浸润影',patient)
        self.assertTrue(any(f['topic']=='chief_complaint' for f in case['patient_initial']['patient_facts']))
        self.assertTrue(any(o['name']=='chest_radiograph' for o in case['his_initial']['current_observations']))
        self.assertEqual(case['his_initial']['current_use_record']['items'],[])

    def test_urinary_presentation_uses_same_interface_without_a_chosen_antibiotic(self):
        case,_=build(source('uti'))
        names={o['name'] for o in case['his_initial']['current_observations']}
        self.assertIn('urine_nitrite',names);self.assertNotIn('chest_radiograph',names)
        self.assertNotIn('prescription',case)
        self.assertTrue(all(o['source_kind']=='constructed' for o in case['his_initial']['current_observations']))
        with self.assertRaises(ValueError):build(build_synthetic_case('copd',1))


if __name__=='__main__':unittest.main()
