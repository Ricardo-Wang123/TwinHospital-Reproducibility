from copy import deepcopy
import json
import unittest
import tempfile
from pathlib import Path
from sandbox.experiments.linked.cases import GROUPS, validate_patient_splits, runtime_visible_case
from sandbox.experiments.linked.chart import HistoryChart
from sandbox.experiments.linked.case_preparation.synthetic_execution_v2 import build_execution_case, prepare, VERSION
FORMULARY=json.loads((Path(__file__).resolve().parents[2]/'deploy/openhis-experiment/formulary_v4.json').read_text())


class SyntheticExecutionInputTests(unittest.TestCase):
    def test_all_five_share_a_visible_current_measurement_contract(self):
        for group in GROUPS:
            with self.subTest(group=group):
                case=build_execution_case(group,100101,FORMULARY)
                chart=HistoryChart(case['his_initial'])
                self.assertEqual(chart.initial_view()['time_basis']['new_visit_offset_hours'],0)
                measured=chart.query('current_observations',limit=20)['records']
                expected={r['name']:r for r in case['his_initial']['current_observations']}
                self.assertEqual({r['record']['name'] for r in measured},set(expected))
                for row in measured:
                    record=row['record'];self.assertEqual(record['value'],expected[record['name']]['value'])
                    self.assertEqual(record['time']['event_offset_hours'],0)
                    self.assertEqual(record['source_kind'],'constructed')
                self.assertIsNone(case['evaluation']['prescription_labels'])
                self.assertNotIn('evaluation',runtime_visible_case(case))
                self.assertFalse(case['provenance']['uses_patient_records'])
                self.assertIsNone(case['provenance']['source_dataset'])

    def test_new_findings_are_in_hospital_view_not_leaked_as_patient_knowledge(self):
        for group,name in [('pneumonia','chest_radiograph'),('uti','urine_nitrite')]:
            case=build_execution_case(group,100101,FORMULARY)
            self.assertIn(name,json.dumps(case['his_initial']))
            self.assertNotIn(name,json.dumps(case['patient_initial']))
            self.assertNotIn('prescription',case)
            self.assertNotIn('settlement',case)

    def test_current_use_and_supply_survive_chart_projection_without_becoming_verified(self):
        case=build_execution_case('type2_diabetes',100101,FORMULARY)
        chart=HistoryChart(case['his_initial'])
        record=chart.query('current_use_items',limit=20)['records'][0]['record']
        self.assertEqual(record['dose'],500)
        self.assertEqual(record['supply']['remaining_dose_equivalents'],2)
        self.assertEqual(record['actual_use']['reported_taken_doses'],12)
        self.assertEqual(record['current_product']['drug_code'],'SIM-METFORMIN-500')
        self.assertEqual(chart.initial_view()['current_use_record']['reconciliation_status'],
            'not_yet_verified_by_current_visit_team')

    def test_same_patient_cannot_be_relabelled_into_a_different_split(self):
        a=build_execution_case('copd',100101,FORMULARY,split='development')
        b=build_execution_case('copd',100101,FORMULARY,split='test')
        self.assertEqual(a['patient_key'],b['patient_key'])
        with self.assertRaises(ValueError):validate_patient_splits([a,b])

    def test_constructor_is_deterministic_and_keeps_shared_formulary_unchanged(self):
        formulary=deepcopy(FORMULARY);before=json.dumps(formulary,sort_keys=True)
        a=build_execution_case('heart_failure',100101,formulary)
        self.assertEqual(a,build_execution_case('heart_failure',100101,formulary))
        self.assertEqual(json.dumps(formulary,sort_keys=True),before)
        self.assertEqual(a['provenance']['construction_version'],VERSION)

    def test_manifest_uses_real_dictionary_all_five_and_refuses_overwrite(self):
        from sandbox.experiments.linked.run_batch import select_cases
        with tempfile.TemporaryDirectory() as temp:
            out=Path(temp)/'frozen'
            formulary_path=Path(__file__).resolve().parents[2]/'deploy/openhis-experiment/formulary_v4.json'
            result=prepare(out,formulary_path)
            manifest=json.loads((out/'manifest.json').read_text())
            self.assertEqual(result['case_count'],5)
            self.assertEqual(len({r['system'] for r in manifest['cases']}),4)
            self.assertEqual(len(select_cases(manifest)),5)
            self.assertFalse(manifest['model_outcomes_read'])
            with self.assertRaises(FileExistsError):prepare(out,formulary_path)


if __name__=='__main__':unittest.main()
