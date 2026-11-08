from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from results.summarize_paid_runs import heldout_effort_plan, matched_stage_effort
from sandbox.experiments.linked.contracts import digest, paper_conditions, runtime_case
from sandbox.experiments.linked.cases import GROUPS
from sandbox.experiments.linked.programmatic_delivery import VERSION as DELIVERY_VERSION
from sandbox.experiments.linked.prescription_analysis import catalogue_route_reference, measures
from test_programmatic_delivery import handoff_run
from tools.analyze_stage_delivery import (METRICS, analyze_batch, observations,
                                         paired_statistics, paper_contrasts, sha, validate_formal_cohort,
                                         documentation_timing_contrasts)


BOOTSTRAP = {'seed': 23, 'repeats': 200, 'alpha': .05, 'stratify_by_disease': True}


def row(qualifies, seconds=60):
    return {'qualifies': qualifies, 'work_observed': True, 'staff_seconds': seconds,
            'nurse_seconds': seconds/2, 'physician_seconds': seconds/2}


def units():
    return [{'unit_id': str(i), 'patient_key': 'p'+str(i), 'group': 'disease'} for i in range(4)]


class HeldoutMatchedEffortTests(unittest.TestCase):
    @staticmethod
    def block(case_id, group, difference, prescription='pprs_baseline'):
        return [
            {'case_id': case_id, 'group': group, 'conditions': {
                'preconsult': {'workflow': intake}, 'prescription': {'workflow': prescription},
                'documentation': {'workflow': 'manual_documentation'},
            }, 'stages': {'preconsult': {'qualifies': True,
                                        'activities': [('nurse', 'information_collection', minutes * 60)]}}}
            for intake, minutes in (('nurse_only', 5), ('ai_only', 5 + difference))
        ]

    def test_stratified_interval_weights_histories_equally_across_matched_blocks(self):
        first, second = GROUPS[:2]
        runs = (self.block('a', first, -2) + self.block('a', first, 0, 'sequential')
                + self.block('b', first, 3) + self.block('c', second, -3) + self.block('d', second, 5))
        plan = heldout_effort_plan()
        contrast = matched_stage_effort(runs, 'preconsult', 'nurse_only', analysis_plan=plan)['ai_only']
        self.assertEqual(contrast['matched_blocks'], 5)
        self.assertEqual(contrast['source_histories'], 4)
        self.assertEqual(contrast['mean_difference_minutes'], 1)
        bootstrap = contrast['bootstrap']
        self.assertEqual(bootstrap['status'], 'estimated')
        self.assertEqual(bootstrap['ci'], [-2, 4])
        self.assertEqual(bootstrap['valid_resamples'], 10000)
        self.assertEqual(bootstrap['stratum_patient_counts'], {group: 2 if group in (first, second) else 0
                                                               for group in GROUPS})
        reordered = matched_stage_effort(runs[::-1], 'preconsult', 'nurse_only', analysis_plan=plan)
        self.assertEqual(reordered['ai_only'], contrast)

    def test_sparse_stratum_and_missing_paired_condition_keep_explicit_denominators(self):
        first, second = GROUPS[:2]
        runs = self.block('a', first, 0) + self.block('b', first, 0) + self.block('c', second, 0)
        contrasts = matched_stage_effort(runs, 'preconsult', 'nurse_only', analysis_plan=heldout_effort_plan())
        observed = contrasts['ai_only']
        self.assertEqual(observed['source_histories'], 3)
        self.assertEqual(observed['mean_difference_minutes'], 0)
        self.assertEqual(observed['bootstrap']['status'], 'insufficient_within_stratum_replication')
        self.assertIsNone(observed['bootstrap']['ci'])
        missing = contrasts['ai_nurse_review']
        self.assertEqual(missing['matched_blocks'], 0)
        self.assertIsNone(missing['mean_difference_minutes'])
        self.assertEqual(missing['bootstrap']['status'], 'fewer_than_two_source_patients')
        self.assertIsNone(missing['bootstrap']['ci'])


class PatientPairedDeliveryTests(unittest.TestCase):
    def test_single_ai_tool_has_six_baseline_contrasts_and_both_note_timings(self):
        conditions=[c.to_dict() for c in paper_conditions(model_families=('tool_a',))]
        contrasts=paper_contrasts(conditions)
        self.assertEqual(len(contrasts),6)
        self.assertEqual({x['a'] for x in contrasts if x['a'].startswith('documentation:')},
            {'documentation:realtime:tool_a','documentation:postvisit:tool_a'})
        self.assertTrue(all('tool_b' not in str(x) for x in contrasts))
        self.assertEqual(documentation_timing_contrasts(conditions),[
            {'a':'documentation:realtime:tool_a','b':'documentation:postvisit:tool_a'}])
        with self.assertRaises(ValueError):
            documentation_timing_contrasts([c for c in conditions if c['workflow']!='postvisit'])

    def test_formal_cohort_matches_the_new_authorized_scale_not_a_partial_success_subset(self):
        rows = [{'unit_id': f'{group}-{i}', 'patient_key': f'{group}-{i}', 'group': group}
                for group in GROUPS for i in range(200)]
        plan = {'source_patients': 1000, 'per_disease': 200}
        validate_formal_cohort(rows, plan)
        with self.assertRaisesRegex(ValueError, 'complete prospectively planned'):
            validate_formal_cohort(rows[:-1], plan)
        changed = deepcopy(rows)
        changed[1]['patient_key'] = changed[0]['patient_key']
        with self.assertRaises(ValueError):
            validate_formal_cohort(changed, plan)
        with self.assertRaises(ValueError):
            validate_formal_cohort(rows, {'source_patients': 2000, 'per_disease': 400})

    def test_same_draws_pair_conditions_and_retain_failed_work(self):
        cases = units()
        a = {u['unit_id']: row(i != 0) for i, u in enumerate(cases)}
        result = paired_statistics(cases, {'a': a, 'b': deepcopy(a)}, [{'a': 'a', 'b': 'b'}], BOOTSTRAP)
        metric = result['configurations']['a']['staff_minutes_per_delivered_task']
        self.assertEqual(metric['numerator'], 4)
        self.assertEqual(metric['denominator'], 3)
        self.assertAlmostEqual(metric['estimate'], 4/3)
        paired = result['contrasts'][0]['metrics']['delivery_rate']
        self.assertEqual(paired['estimate'], 0)
        self.assertEqual(paired['bootstrap']['ci'], [0, 0])
        self.assertEqual(result, paired_statistics(cases, {'a': a, 'b': deepcopy(a)}, [{'a': 'a', 'b': 'b'}], BOOTSTRAP))
        self.assertIsNone(result['human_staff_time'])

    def test_missing_work_is_not_zero_and_zero_denominator_stays_undefined(self):
        cases = units()
        a = {u['unit_id']: row(False) for u in cases}
        result = paired_statistics(cases, {'a': a}, [], BOOTSTRAP)['configurations']['a']
        self.assertEqual(result['delivery_rate']['estimate'], 0)
        self.assertIsNone(result['staff_minutes_per_delivered_task']['estimate'])
        self.assertEqual(result['staff_minutes_per_delivered_task']['bootstrap']['undefined_resamples'], 200)
        a['0']['qualifies'] = True
        a['1']['work_observed'] = False
        result = paired_statistics(cases, {'a': a}, [], BOOTSTRAP)['configurations']['a']
        self.assertEqual(result['delivery_rate']['estimate'], .25)
        self.assertIsNone(result['staff_minutes_per_delivered_task']['estimate'])
        self.assertEqual(result['staff_minutes_per_delivered_task']['unknown_units'], 1)
        a['2']['qualifies'] = None
        self.assertIsNone(paired_statistics(cases, {'a': a}, [], BOOTSTRAP)['configurations']['a']['delivery_rate']['estimate'])

    def test_source_patient_clustering_and_small_strata_are_explicit(self):
        cases = units()
        cases[1]['patient_key'] = cases[0]['patient_key']
        a = {u['unit_id']: row(True) for u in cases}
        result = paired_statistics(cases, {'a': a}, [], BOOTSTRAP)
        self.assertEqual(result['planned_units'], 4)
        self.assertEqual(result['distinct_source_patients'], 3)
        cases[1]['group'] = 'different'
        with self.assertRaisesRegex(ValueError, 'cross disease strata'):
            paired_statistics(cases, {'a': a}, [], BOOTSTRAP)
        cases = units()
        cases[0]['group'] = 'singleton'
        result = paired_statistics(cases, {'a': a}, [], BOOTSTRAP)
        bootstrap = result['configurations']['a']['delivery_rate']['bootstrap']
        self.assertIsNone(bootstrap['ci'])
        self.assertEqual(bootstrap['status'], 'insufficient_within_stratum_replication')

    def test_role_work_must_match_declared_activity_weights(self):
        run = handoff_run()
        run['timing_source'] = 'fixture-unvalidated-human-duration'
        run['activities'][0]['activity_type'] = 'information_collection'
        result = observations(run, 'preconsult', declared_weights={'information_collection': 60})
        self.assertIs(result['qualifies'], True)
        self.assertEqual(result['nurse_seconds'], 60)
        run['activities'][0]['active_seconds'] = 61
        result = observations(run, 'preconsult', declared_weights={'information_collection': 60})
        self.assertFalse(result['work_observed'])
        self.assertIsNone(result['staff_seconds'])

    def test_finished_matrix_keeps_unstarted_assignments_and_source_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            batch = root/'batch'
            batch.mkdir()
            (batch/'batch.lock').touch()
            conditions = [c.to_dict() for c in paper_conditions(model_families=('tool_a', 'tool_b'))]
            spec = {'mode': 'matrix', 'formal_experiment': False, 'cases': [], 'conditions': conditions,
                    'models': {'tool_models': {'tool_a': 'fixture-model-a', 'tool_b': 'fixture-model-b'}},
                    'modeled_activity_seconds': {'information_collection': 60}}
            manifest = {'cases': []}
            for i in range(2):
                case = {'case_id': 'c'+str(i), 'patient_key': 'p'+str(i), 'group': 'fixture',
                        'data_kind': 'independent_fictional', 'split': 'development',
                        'patient_initial': {'patient_facts': []}, 'his_initial': {}, 'formulary': []}
                source = root/(case['case_id']+'.json')
                source.write_text(json.dumps(case))
                spec['cases'].append({k: case[k] for k in ('case_id', 'group')})
                spec['cases'][-1].update(file_sha256=sha(source), runtime_input_hash=digest(runtime_case(case)))
                manifest['cases'].append({'case_id': case['case_id'], 'patient_key': case['patient_key'], 'path': str(source)})
                folder = batch/case['case_id']
                folder.mkdir()
                entries = {c['config_id'].replace(':', '__'): {'config_id': c['config_id'], 'status': 'not_run_operator_interruption'} for c in conditions}
                if i == 0:
                    run = handoff_run()
                    run.update(case_id=case['case_id'], config_id='preconsult:nurse_only',
                               input_hash=digest(runtime_case(case)), timing_source='fixture')
                    run['activities'][0]['activity_type'] = 'information_collection'
                    path = folder/'run.json'
                    path.write_text(json.dumps(run))
                    entries['preconsult__nurse_only'].update(status='completed', result_path=str(path))
                (folder/'attempts.json').write_text(json.dumps({'case_id': case['case_id'], 'attempts': entries}))
            (batch/'specification.json').write_text(json.dumps(spec))
            (batch/'batch_summary.json').write_text(json.dumps({'session_started_at': '2026-02-01T00:00:00+00:00'}))
            plan = {'frozen_at': '2026-01-01T00:00:00+00:00', 'conditions': conditions,
                    'delivery_criteria_version': DELIVERY_VERSION, 'metrics': list(METRICS),
                    'delivery_criteria_sha256': sha(Path(__file__).resolve().parents[1]/'sandbox/experiments/linked/programmatic_delivery.py'),
                    'contrasts': paper_contrasts(conditions), 'bootstrap': BOOTSTRAP}
            for name, data in [('manifest.json', manifest), ('plan.json', plan)]:
                (root/name).write_text(json.dumps(data))
            before = {str(p): sha(p) for p in batch.rglob('*') if p.is_file()}
            result = analyze_batch(batch, root/'manifest.json', root/'plan.json', root/'report')
            self.assertEqual(result['planned_source_patients'], 2)
            self.assertEqual(result['delivery_configurations'], 10)
            self.assertEqual(result['paired_contrasts'], 12)
            report = read_json(root/'report/delivery_analysis.json')
            primary = report['pooled']['configurations']['preconsult:nurse_only']
            self.assertEqual(primary['delivery_rate']['estimate'], .5)
            self.assertEqual(primary['staff_minutes_per_delivered_task']['estimate'], 1)
            self.assertEqual(before, {p: sha(p) for p in before})
            self.assertFalse(report['formal_result'])
            self.assertNotIn('SYNTHETIC_PRIVATE', json.dumps(report))


class PrescriptionScopeTests(unittest.TestCase):
    def test_route_requires_exact_product_and_an_explicit_route(self):
        formulary = [{'drug_code': 'known', 'allowed_routes': ['oral']}]
        self.assertEqual(catalogue_route_reference([{'drug_code': 'known', 'route': 'oral'}], formulary),
                         'scoped_nonviolation')
        self.assertEqual(catalogue_route_reference([{'drug_code': 'known', 'route': 'inhaled'}], formulary),
                         'violation')
        self.assertEqual(catalogue_route_reference([{'drug_code': 'unknown', 'route': 'oral'}], formulary),
                         'indeterminate')

    def test_all_metrics_use_their_declared_denominators(self):
        scores = measures(Counter({('reject', 'violation', True): 2, ('approve', 'violation', True): 1,
            ('reject', 'scoped_nonviolation', False): 1, ('approve', 'scoped_nonviolation', False): 3,
            ('approve', 'indeterminate', False): 2, ('defer', 'violation', True): 1}))
        expected = {'pass_rate': (6, 9), 'severe_miss_rate': (1, 3), 'fpr': (1, 4),
                    'recall': (2, 3), 'precision': (2, 3), 'f1': (4, 6)}
        for metric, (num, den) in expected.items():
            self.assertEqual((scores[metric]['numerator'], scores[metric]['denominator']), (num, den))
            self.assertAlmostEqual(scores[metric]['estimate'], num/den)
        observed = measures(Counter({('approve', 'scoped_nonviolation', False): 5}))
        self.assertEqual(observed['fpr']['estimate'], 0)
        self.assertEqual(observed['recall']['denominator'], 0)
        self.assertIsNone(observed['recall']['estimate'])


def read_json(path):
    return json.loads(path.read_text())


if __name__ == '__main__':
    unittest.main()
