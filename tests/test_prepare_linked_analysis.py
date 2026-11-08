import json
from pathlib import Path
import tempfile
import unittest

from sandbox.experiments.linked.contracts import digest, paper_conditions, runtime_case
from tools.prepare_linked_analysis import linked_configuration_id, prepare, sha


class PlannedWorkflowAnalysisTests(unittest.TestCase):
    def fixture(self, root, *, all_unstarted=False, wrong_config=False):
        batch = root / 'batch'
        batch.mkdir()
        conditions = [c.to_dict() for c in paper_conditions(model_families=('tool_a', 'tool_b'))]
        spec = {'batch': 'fixture-only', 'cases': [], 'conditions': conditions, 'code_hashes': {}}
        config = linked_configuration_id(spec)
        manifest = {'cases': []}
        for index in range(2):
            cid = 'fixture-' + str(index)
            case = {'case_id': cid, 'patient_key': 'patient-' + str(index),
                    'group': 'fixture-disease', 'data_kind': 'independent_fictional',
                    'patient_initial': {'patient_facts': []}, 'his_initial': {}, 'formulary': []}
            source = root / (cid + '.json')
            source.write_text(json.dumps(case))
            spec['cases'].append({'case_id': cid, 'file_sha256': sha(source),
                                  'runtime_input_hash': digest(runtime_case(case))})
            manifest['cases'].append({'case_id': cid, 'path': str(source), 'patient_key': case['patient_key']})
            folder = batch / cid
            folder.mkdir()
            if index == 0 and not all_unstarted:
                run = {'case_id': cid, 'run_id': 'fixture-run', 'config_id': 'linked:wrong' if wrong_config else config,
                       'status': 'completed', 'input_hash': digest(runtime_case(case)),
                       'evaluation_unit': 'complete_encounter', 'artifacts': {}}
                path = folder / 'run.json'
                path.write_text(json.dumps(run))
                entry = {'status': 'completed', 'config_id': run['config_id'], 'result_path': str(path)}
            else:
                entry = {'status': 'not_run_operator_interruption', 'config_id': 'linked_baseline_anchor'}
            (folder / 'attempts.json').write_text(json.dumps({'case_id': cid, 'attempts': {'linked_baseline_anchor': entry}}))
        (batch / 'specification.json').write_text(json.dumps(spec))
        path = root / 'manifest.json'
        path.write_text(json.dumps(manifest))
        return batch, path, config

    def test_unstarted_patients_remain_in_one_canonical_denominator(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            batch, manifest, config = self.fixture(root)
            result = prepare(batch, manifest, root / 'analysis', slice_name='workflow')
            self.assertEqual(result['configurations'], 1)
            self.assertEqual(result['planned_units'], 2)
            self.assertEqual(result['observed_attempts'], 1)
            analysis = json.loads((root / 'analysis/analysis_output.json').read_text())
            self.assertEqual(set(analysis['configurations']), {config})
            row = analysis['configurations'][config]
            self.assertEqual(row['engineering_observed_completion_rate'], .5)
            self.assertEqual(row['missing_unit_ids'], ['fixture-1:r0'])
            self.assertIsNone(row['metrics']['correct_completion_rate']['estimate'])

    def test_entirely_unstarted_batch_keeps_declared_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            batch, manifest, config = self.fixture(root, all_unstarted=True)
            result = prepare(batch, manifest, root / 'analysis', slice_name='workflow')
            self.assertEqual(result['configurations'], 1)
            self.assertEqual(result['observed_attempts'], 0)
            analysis = json.loads((root / 'analysis/analysis_input.json').read_text())
            self.assertEqual(analysis['configurations'][0]['config_id'], config)
            self.assertEqual(len(analysis['planned_units']), 2)

    def test_changed_observed_configuration_is_not_relabelled_as_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            batch, manifest, _ = self.fixture(root, wrong_config=True)
            with self.assertRaisesRegex(ValueError, 'frozen canonical baseline'):
                prepare(batch, manifest, root / 'analysis', slice_name='workflow')


if __name__ == '__main__':
    unittest.main()
