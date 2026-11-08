import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from handoff.inputs import relocate_operation_schedule
from sandbox.experiments.linked import source_operation_replay as replay
from sandbox.experiments.linked.native_his import NativeBlocked, NativeError, NativeRejected, digest, write_private


def population():
    cohort, tests = [], []
    for group in replay.GROUPS:
        for i in range(200):
            row = {'case_id': f'{group}_{i}', 'patient_key': f'patient_{group}_{i}',
                   'group': group, 'split': 'test', 'data_kind': 'mimic_derived',
                   'path': f'/frozen/{group}_{i}.json',
                   'file_sha256': 'a' * 64, 'runtime_input_hash': 'b' * 64}
            cohort.append(row)
            if i < 100:
                tests.append({**row, 'local_test_id': f'test_{group}_{i}',
                              'operation_category': list(replay.CATEGORIES)[i // 20],
                              'request_condition': 'single_fault' if i % 20 < 4 else 'compliant_control'})
    return {'cases': tests}, {'cases': cohort}


class AllocationTests(unittest.TestCase):
    def test_exact_patient_and_operation_allocation(self):
        schedule, cohort = population()
        self.assertEqual(len(replay.validate_schedule(schedule, cohort)), 500)

    def test_more_controls_cannot_hide_missing_fault_cell(self):
        schedule, cohort = population()
        schedule['cases'][0]['request_condition'] = 'compliant_control'
        with self.assertRaisesRegex(ValueError, '25 disease/operation cells'):
            replay.validate_schedule(schedule, cohort)

    def test_repeated_patient_cannot_count_as_two_sources(self):
        schedule, cohort = population()
        cohort['cases'][1]['patient_key'] = cohort['cases'][0]['patient_key']
        schedule['cases'][1]['patient_key'] = cohort['cases'][0]['patient_key']
        with self.assertRaisesRegex(ValueError, 'distinct source-patient'):
            replay.validate_schedule(schedule, cohort)

    def test_engineering_cannot_be_promoted_by_matching_sample_size(self):
        schedule, cohort = population()
        cohort['cases'][0]['data_kind'] = schedule['cases'][0]['data_kind'] = 'synthetic_independent'
        with self.assertRaisesRegex(ValueError, 'engineering material'):
            replay.validate_schedule(schedule, cohort)

    def test_schedule_cannot_change_case_content_or_disease(self):
        schedule, cohort = population()
        schedule['cases'][0]['runtime_input_hash'] = 'c' * 64
        with self.assertRaisesRegex(ValueError, 'shared-cohort source'):
            replay.validate_schedule(schedule, cohort, engineering=True)

    def test_packed_schedule_changes_only_source_paths(self):
        schedule, cohort = population()
        relocated = [{**row, 'path': f'cases/{i:04}.json'} for i, row in enumerate(cohort['cases'], 1)]
        copied = relocate_operation_schedule(schedule, cohort['cases'], relocated)
        self.assertEqual(len(replay.validate_schedule(copied, {'cases': relocated})), 500)
        self.assertEqual([{k: v for k, v in row.items() if k != 'path'} for row in copied['cases']],
                         [{k: v for k, v in row.items() if k != 'path'} for row in schedule['cases']])

    def test_heldout_replay_accepts_only_the_preassigned_schedule_bytes(self):
        schedule, cohort = population()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            batch, source, operations = root / 'batch', root / 'cohort.json', root / 'operations.json'
            batch.mkdir()
            write_private(source, cohort)
            write_private(operations, schedule)
            write_private(batch / 'specification.json', {
                'batch': 'heldout', 'mode': 'linked-acceptance', 'formal_experiment': False,
                'linked_acceptance': {'independent_crossed': True, 'evaluation_partition': 'test'},
                'heldout_operations': {'source_profile': replay.BASELINE_LABEL, 'planned_units': 500,
                                       'cohort_sha256': replay.sha(source), 'operation_schedule_sha256': replay.sha(operations),
                                       'code_sha256': replay.replay_code_hashes()},
            })
            write_private(batch / 'batch_summary.json', {'batch': 'heldout', 'session_finished_at': 'closed',
                                                          'native_observation': {'status': 'closed'}})
            parameters = (operations, source, batch, root / 'output', 'replay', None, None, False, None)
            with self.assertRaisesRegex(ValueError, 'explicit source deployment'):
                replay._run_closed_batch(*parameters)
            with self.assertRaisesRegex(ValueError, 'Only engineering comparisons'):
                replay._run_closed_batch(*parameters, his_interface='fhir-r4')
            with patch.object(replay, 'replay_code_hashes', return_value={}):
                with self.assertRaisesRegex(ValueError, 'implementation differs from the freeze'):
                    replay._run_closed_batch(*parameters)
            schedule['cases'].reverse()  # Same balanced cells, different frozen schedule.
            write_private(operations, schedule)
            with self.assertRaisesRegex(ValueError, 'differs from the source batch input freeze'):
                replay._run_closed_batch(*parameters)
            self.assertFalse((root / 'output').exists())


class BindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.row = population()[0]['cases'][0]
        self.source = self.root / 'actual-source.json'
        write_private(self.source, {'immutable': True})
        self.checkpoint = {'demographics': {'age': 57, 'sex': 'F'},
                           'consultation': {'diagnoses': [{'code': 'TEST'}]},
                           'prescription': {'items': [{'drug_code': 'ACTUAL', 'dose': 2}]},
                           'note': {'text': 'Actual source document.', 'sections': {'HPI': 'source'}}}
        self.proof = {'checkpoint_sha256': digest(self.checkpoint),
                      'source_files_sha256': {str(self.source): replay.sha(self.source)}}
        self.executor = replay.SourceOperationReplay(self.root / 'execution', 'new_run', {'ACTUAL': {}}, {})

    def tearDown(self):
        self.temp.cleanup()

    def freeze(self):
        with patch.object(replay, 'load_checkpoint', return_value=(self.checkpoint, self.proof)):
            bindings, _ = replay.prepare_bindings([self.row], self.root, {}, self.root)
        return bindings[self.row['local_test_id']]

    def test_changed_source_prevents_every_native_effect(self):
        binding = self.freeze()
        write_private(self.source, {'immutable': False})
        with patch.object(replay.PaperLocalSafety, 'run_one') as native:
            result = self.executor.run_bound(self.row, binding)
        native.assert_not_called()
        self.assertEqual(result['outcome'], 'unresolved_source')
        self.assertFalse(result['native_effects_started'])
        self.assertFalse(result['source_replaced'])

    def test_frozen_missing_source_does_not_get_replaced_later(self):
        with patch.object(replay, 'load_checkpoint', side_effect=ValueError('source incomplete')):
            bindings, _ = replay.prepare_bindings([self.row], self.root, {}, self.root)
        with patch.object(replay, 'load_checkpoint', return_value=(self.checkpoint, self.proof)) as load, \
                patch.object(replay.PaperLocalSafety, 'run_one') as native:
            result = self.executor.run_bound(self.row, bindings[self.row['local_test_id']])
        load.assert_not_called()
        native.assert_not_called()
        self.assertEqual(result['outcome'], 'unresolved_source')

    def test_actual_note_is_preserved_in_both_conditions(self):
        self.executor.checkpoint = copy.deepcopy(self.checkpoint)
        original = self.executor.note_content({})
        replacement = self.executor.note_content({}, replacement=True)
        self.assertEqual(original, self.checkpoint['note'])
        self.assertEqual(replacement.pop('operation_replay_revision'), 2)
        self.assertEqual(replacement, original)
        original['text'] = 'caller changed copy'
        self.assertEqual(self.executor.checkpoint['note'], self.checkpoint['note'])

    def test_unknown_original_product_has_no_substitution(self):
        self.executor.checkpoint = copy.deepcopy(self.checkpoint)
        self.executor.mapping = {'OTHER': {}}
        with patch.object(self.executor, 'perform') as native:
            with self.assertRaisesRegex(ValueError, 'actual source product'):
                self.executor.order(None, {}, {})
        native.assert_not_called()

    def test_original_patient_never_reused_as_write_target(self):
        self.executor.source_native_ids = {'old-patient'}
        with patch.object(replay.PaperLocalSafety, 'perform') as native:
            with self.assertRaises(NativeBlocked):
                self.executor.perform(None, {'patient_id': 'old-patient'}, 'order.save', {})
        native.assert_not_called()

    def test_replay_preserves_source_failure_and_trace_scope(self):
        binding = self.freeze()
        with patch.object(replay.PaperLocalSafety, 'run_one', return_value={'outcome': 'unsafe_execution'}):
            result = self.executor.run_bound(self.row, binding)
        self.assertEqual(result['outcome'], 'unsafe_execution')
        self.assertFalse(result['clinical_validity_assessed'])
        self.assertTrue(result['source_files_unchanged'])
        self.assertIsNone(self.executor.checkpoint)

    def test_tampered_checkpoint_is_not_used(self):
        binding = self.freeze()
        write_private(binding['path'], {'checkpoint': {'note': 'invented'}})
        with patch.object(replay.PaperLocalSafety, 'run_one') as native:
            self.assertEqual(self.executor.run_bound(self.row, binding)['outcome'], 'unresolved_source')
        native.assert_not_called()

    def test_uncertain_outcome_or_rejected_readback_is_not_a_native_rejection(self):
        self.executor.checkpoint = self.checkpoint
        test = {'test_id': 'locked', 'category': 'locked_note', 'is_error': True}
        state = {'native': {}, 'sidecar': {}}
        for error, expected in (
            (NativeError('Unknown write outcome'), 'unresolved'),
            (NativeRejected('/document/getContent', 'Read failed'), 'unresolved'),
            (NativeRejected('/document/save', 'Write rejected'), 'native_rejection_no_effect_observed'),
        ):
            with self.subTest(error=error):
                with patch.object(self.executor, 'fixture', return_value=(None, {})), \
                        patch.object(self.executor, 'observe', return_value=state), \
                        patch.object(self.executor, 'perform', side_effect=[{}, {}, error]):
                    result = self.executor.run_one(test)
                self.assertEqual(result['outcome'], expected)


if __name__ == '__main__':
    unittest.main()
