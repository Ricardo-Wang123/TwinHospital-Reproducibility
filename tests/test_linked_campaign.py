from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from sandbox.experiments.linked.campaign import (
    admission_status, bind_formal_inputs, validate_formal_design, validate_window, verify_engineering_acceptance,
)
from sandbox.experiments.linked.cases import GROUPS
from sandbox.experiments.linked.clinical import LinkedEncounterRunner
from sandbox.experiments.linked.contracts import digest
from sandbox.experiments.linked.linked_acceptance import BASELINE_LABEL, freeze_design, summarize_gate
from sandbox.experiments.linked.native_observation import filehash
from sandbox.experiments.linked.rules import EngineeringPPRS
from sandbox.experiments.linked.workflow_analysis import WINDOW_POLICY_TEMPLATE, validate_policy
from sandbox.experiments.linked.run_batch import _baseline_conditions
from test_linked_clinical import FakeFactory, FakePort, example_case
from test_linked_source_operation_replay import population


def policy():
    now = datetime.now(timezone.utc)
    p = deepcopy(WINDOW_POLICY_TEMPLATE)
    p.update(status='frozen_before_execution', frozen_at=(now-timedelta(hours=2)).isoformat(),
             window_seconds=3600, workers=1, resources={'host':'independent fictional test'},
             admission_policy='closed_loop_worker_availability',
             late_outcomes='retain_effects_and_final_disposition_exclude_late_completion',
             clinical_reference_scope='declared_programmatic_checks_not_unrestricted_gold_labels')
    p['submission_window'].update(opens_at=(now-timedelta(hours=1)).isoformat(),
                                  closes_at=(now+timedelta(hours=1)).isoformat())
    return p


class CampaignTests(unittest.TestCase):
    def test_reader_crossing_and_audit_preserve_an_unbalanced_independent_dataset(self):
        from tools.audit_linked_workflows import audit_batch

        cases = [{'case_id': f'reader-{i}', 'patient_key': f'patient-{i}', 'group': group,
                  'split': 'engineering', 'data_kind': 'synthetic_independent'}
                 for i, group in enumerate((GROUPS[0], GROUPS[0], GROUPS[1]))]
        models = {'tool_a': 'tool'}
        full = freeze_design(cases, models, factorial=True, max_attempts=1, reader=True)
        pilot = freeze_design(cases, models, factorial=True, max_attempts=1, reader=True, quick=True)
        self.assertEqual(full['planned_unique_encounters'], 81)
        self.assertEqual(pilot['planned_unique_encounters'], 9)
        for task in ('preconsult', 'prescription', 'documentation'):
            self.assertEqual(len({p['conditions'][task]['workflow'] for p in pilot['profiles']}), 3)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            batch = root/'batch'
            batch.mkdir()
            (batch/'batch.lock').touch()
            spec = {'batch': 'reader', 'mode': 'linked-acceptance', 'formal_experiment': False,
                    'models': {'role_model': 'role', 'tool_models': models},
                    'execution_policy': {'workers': 1}, 'linked_acceptance': full,
                    'cases': [{k: v for k, v in case.items() if k != 'patient_key'} for case in cases],
                    'acceptance_source_patient_hashes': [digest(case['patient_key']) for case in cases]}
            (batch/'specification.json').write_text(json.dumps(spec))
            (batch/'batch_summary.json').write_text(json.dumps({'batch': 'reader'}))
            private = root/'audit.json'
            audit_batch(batch, root/'public', private)
            audit = json.loads(private.read_text())['audit']
        self.assertEqual(len(audit['runs']), 81)
        self.assertFalse(any(row['verified_paid'] for row in audit['runs']))
        cells = audit['linked_acceptance_gate']['profile_cells']
        self.assertEqual({cell['group']: cell['planned_patients'] for cell in cells},
                         {GROUPS[0]: 2, GROUPS[1]: 1})

    def test_heldout_report_retains_every_assigned_encounter(self):
        from handoff.reader_report import comparison_summary
        from tools.audit_linked_workflows import audit_batch

        cases = [{'case_id': group, 'patient_key': f'test-{group}', 'group': group,
                  'split': 'test', 'data_kind': 'mimic_derived'} for group in GROUPS]
        models = {'tool_a': 'tool'}
        design = freeze_design(cases, models, patients_per_group=1, factorial=True, max_attempts=1, independent=True)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            batch = root / 'batch'
            batch.mkdir()
            (batch / 'batch.lock').touch()
            specification = {'batch': 'heldout', 'mode': 'linked-acceptance', 'formal_experiment': False,
                             'models': {'role_model': 'role', 'tool_models': models},
                             'execution_policy': {'workers': 1}, 'linked_acceptance': design, 'cases': cases,
                             'development_exclusion_sha256': 'a' * 64,
                             'acceptance_source_patient_hashes': [digest(case['patient_key']) for case in cases]}
            (batch / 'specification.json').write_text(json.dumps(specification))
            (batch / 'batch_summary.json').write_text(json.dumps({'batch': 'heldout', 'cases': []}))
            private = root / 'audit.json'
            audit_batch(batch, root / 'public', private)
            arguments = (batch, private, None, None, root / 'operations.json')
            report = comparison_summary(*arguments)
            self.assertEqual(report['evaluation_partition'], 'test')
            self.assertEqual(report['planned'], 135)
            self.assertEqual(len(report['profiles']), 27)
            self.assertEqual(report['verified_paid'], 0)
            self.assertIsNone(report['paid_cohort_metrics'])
            audit = json.loads(private.read_text())
            self.assertEqual(audit['audit']['experiment_status'], 'independent_heldout_crossing')
            audit['crosswalk'][0]['case_id'] = cases[1]['case_id']
            private.write_text(json.dumps(audit))
            with self.assertRaisesRegex(ValueError, 'every planned case and configuration'):
                comparison_summary(*arguments)

    def test_factorial_coverage_requires_every_combination_and_contains_one_control(self):
        cases = [{'case_id': f'{g}-{i}', 'patient_key': f'{g}-{i}', 'group': g,
                  'split': 'development', 'data_kind': 'mimic_derived'}
                 for g in GROUPS for i in range(2)]
        design = freeze_design(cases, {'tool_a': 'tool'}, patients_per_group=2, factorial=True)
        profiles = design['profiles']
        self.assertEqual(len(profiles), 27)
        self.assertEqual(len({p['config_id'] for p in profiles}), 27)
        self.assertEqual([p['label'] for p in profiles if all(
            c['model_family'] is None for c in p['conditions'].values())], [BASELINE_LABEL])
        rows = [{'study_id': f"{case['case_id']}:{p['label']}", 'group': case['group'],
                 'profile': p['label'], 'verified_paid': True} for case in cases for p in profiles]
        self.assertEqual(design['planned_unique_encounters'], 270)
        self.assertTrue(summarize_gate(design, rows)['passed'])
        rows[-1]['verified_paid'] = False
        gate = summarize_gate(design, rows)
        self.assertTrue(all(c['verified_paid_patients'] == 2 for c in gate['cells']))
        self.assertFalse(gate['passed'])
        self.assertEqual(len(gate['profile_cells']), 135)

    def test_changed_workers_or_late_freeze_rejected(self):
        with self.assertRaisesRegex(ValueError, 'worker count'):
            validate_window(policy(), workers=2)
        p=policy();p['frozen_at']=datetime.now(timezone.utc).isoformat()
        with self.assertRaisesRegex(ValueError, 'before its submission'):
            validate_window(p)

    def test_admission_boundaries_are_not_model_failures(self):
        p=policy();window=p['submission_window']
        opened=datetime.fromisoformat(window['opens_at']);closed=datetime.fromisoformat(window['closes_at'])
        self.assertEqual(admission_status(p,now=opened-timedelta(microseconds=1)), 'not_open')
        self.assertEqual(admission_status(p,now=opened), 'open')
        self.assertEqual(admission_status(p,now=closed), 'closed')

    def test_actual_runner_records_exact_prospective_policy(self):
        case=example_case();p=policy()
        runner=LinkedEncounterRunner(FakeFactory(),FakePort(),EngineeringPPRS(case['formulary']),workflow_policy=p)
        result=runner.run_case(case,run_id='prospective-fictional',**_baseline_conditions())
        self.assertEqual(result['status'],'completed',result.get('failure_reason'))
        self.assertEqual(result['workflow_policy_hash'],digest(p))
        self.assertEqual(validate_policy({'workflow_observation_policy':p},result)[1],[])
        p['window_seconds']=1
        self.assertNotEqual(result['workflow_policy_hash'],digest(p))

    def test_formal_flag_requires_the_full_user_resized_design(self):
        args=SimpleNamespace(formal_experiment=True,workers=1,mode='matrix',matrix_task=None,
              limit=None,anchor_batch=None,controlled_prescription_variant=None,engineering_only=False,
              close_prescription_loop=True,capture_native=True,rule_engine='documented-candidate')
        cases=population()[1]['cases'];config={'tool_models':{'tool_a':'A'}}
        validate_formal_design(args,cases,config,policy())
        self.assertEqual(len(cases),1000)
        with self.assertRaisesRegex(ValueError,'1000 distinct patients'):
            validate_formal_design(args,cases[:-1],config,policy())
        args.limit=10
        with self.assertRaisesRegex(ValueError,'all 9'):
            validate_formal_design(args,cases,config,policy())
        args.limit=None;cases[0]['split']='engineering'
        with self.assertRaisesRegex(ValueError,'excludes engineering'):
            validate_formal_design(args,cases,config,policy())

    def test_formal_inputs_bind_the_preassigned_operation_schedule(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root/'formal_test1000_manifest.json'
            schedule = root/'app3_local500_manifest.json'
            manifest.write_text('{}')
            schedule.write_text('{}')
            receipt = {'source_patients': 1000, 'local_tests': {'single_fault': 100, 'compliant_control': 400},
                       'patient_or_assignment_replacements': 0, 'execution_outcomes_read': False,
                       'prepared_manifest_sha256': filehash(manifest),
                       'prepared_operation_schedule_sha256': filehash(schedule)}
            (root/'preparation_receipt.json').write_text(json.dumps(receipt))
            self.assertEqual(bind_formal_inputs(manifest)['operation_schedule_sha256'], filehash(schedule))
            schedule.write_text('{"changed":true}')
            with self.assertRaisesRegex(ValueError, 'differs from the prepared input receipt'):
                bind_formal_inputs(manifest)

    def test_formal_acceptance_binds_one_version_and_separate_patients(self):
        for target in (10, 20):
            with self.subTest(patients_per_disease=target):
                self.check_formal_acceptance(target)

    def check_formal_acceptance(self, target):
        cases = [{'case_id': f'{g}-{i}', 'patient_key': f'dev-{g}-{i}', 'group': g,
                  'split': 'development', 'data_kind': 'mimic_derived'} for g in GROUPS for i in range(target)]
        spec = {k: {} for k in ('code_hashes', 'transport_policy', 'generation', 'generation_policy',
                               'data_policy_hash', 'catalog_hash', 'diagnosis_catalog_hash', 'rules',
                               'workflow_limits', 'modeled_activity_seconds', 'prescription_followthrough_policy')}
        spec.update(batch='acceptance', mode='linked-acceptance', formal_experiment=False, cases=cases,
                    models={'tool_models': {'tool_a': 'tool'}}, native_observation={'source_hashes': {}})
        spec['linked_acceptance'] = freeze_design(cases, spec['models']['tool_models'], patients_per_group=target)
        if target != 10:
            spec['engineering_acceptance_patients_per_disease'] = target
        spec['acceptance_source_patient_hashes'] = [digest(r['patient_key']) for r in cases]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            batch = root/'acceptance'
            batch.mkdir()
            (batch/'batch.lock').touch()
            specification = batch/'specification.json'
            specification.write_text(json.dumps(spec))
            summary = batch/'batch_summary.json'
            summary.write_text(json.dumps({'batch': 'acceptance', 'session_finished_at': 'closed',
                                           'native_observation': {'status': 'closed'}}))
            hashes = {str(p): filehash(p) for p in (specification, summary)}
            rows, crosswalk = [], []
            for case in cases:
                for profile in spec['linked_acceptance']['profiles']:
                    run_id = case['case_id'] + '-' + profile['label']
                    result_path = batch/(run_id + '.json')
                    result_path.write_text('{}')
                    hashes[str(result_path)] = filehash(result_path)
                    rows.append({'study_id': run_id, 'group': case['group'], 'profile': profile['label'], 'verified_paid': True})
                    crosswalk.append({'case_id': case['case_id'], 'condition': profile['label'],
                                      'run_id': run_id, 'result_path': str(result_path)})
            gate = {**summarize_gate(spec['linked_acceptance'], rows), 'reused_native_identity_runs': 0}
            audit = root/'private-audit.json'
            audit.write_text(json.dumps({'audit': {'native_observation_requested': True, 'source_files_unchanged': True,
                'source_specification_sha256': filehash(specification), 'linked_acceptance_gate': gate, 'runs': rows},
                'source_file_hashes': hashes, 'crosswalk': crosswalk}))
            held_out = [{'patient_key': 'formal-unique'}]
            self.assertEqual(verify_engineering_acceptance(batch, audit, spec, held_out)['verified_paid'],
                             5 * 7 * target)
            with self.assertRaisesRegex(ValueError, 'must be frozen'):
                verify_engineering_acceptance(batch, audit,
                    {**spec, 'engineering_acceptance_patients_per_disease': 30 - target}, held_out)
            with self.assertRaisesRegex(ValueError, 'runtime differs'):
                verify_engineering_acceptance(batch, audit, {**spec, 'code_hashes': {'changed': True}}, held_out)
            with self.assertRaisesRegex(ValueError, 'separate from formal patients'):
                verify_engineering_acceptance(batch, audit, spec, [cases[0]])
            result_path.write_text('{"changed":true}')
            with self.assertRaisesRegex(ValueError, 'audit inputs changed'):
                verify_engineering_acceptance(batch, audit, spec, held_out)


if __name__=='__main__':unittest.main()
