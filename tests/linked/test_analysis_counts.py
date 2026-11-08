from copy import deepcopy
import unittest
from sandbox.experiments.linked.analysis_counts import analyze, aggregate, METRICS


def record(unit, name, *, correct=1, total=1, patient='unused'):
    return {'unit_id':unit,'run_id':name,'implementation_version':'fixture-v1',
        'execution':{'status':'completed'},'artifact_available':True,
        'measurement':{'status':'complete','source':'programmatic','source_refs':['fixture-source-sha'],
                  'covered_metrics':list(set(sum((list(v) for v in METRICS.values()),[]))),'criteria_version':'fixture-rubric'},
        'counts':{'items':{'applicable':total,'correct':correct,'incorrect':0,'absent':total-correct,'indeterminate':0},
            'claims':{'supported_correct':correct,'contradictory':0,'unsupported':total-correct,'indeterminate':0},
            'risks':{'applicable':1,'correctly_identified_and_flagged':correct>0,'indeterminate':0},
            'final_quality_qualified':correct==total,'handoff_usable_without_critical_correction':correct==total,
            'first_pass_quality_qualified_without_correction':correct==total},
        'effort':{'timing_scheme_ref':'fixture-only-not-calibrated','modeled_role_seconds':{'nurse':60,'physician':30,'documentation_staff':0},
            'redundant_inquiry_programmatically_classified':True,'redundant_inquiry_modeled_seconds':0}}


def dataset():
    units=[{'unit_id':f'u{i}','case_id':f'c{i}','patient_key':f'p{i}','replicate':0} for i in range(4)]
    a=[record(u['unit_id'],'a'+u['unit_id'],correct=i%2) for i,u in enumerate(units)]
    for r in a:r['counts']['risks']['correctly_identified_and_flagged']=int(r['counts']['risks']['correctly_identified_and_flagged'])
    b=deepcopy(a)
    for r in b:r['run_id']='b'+r['unit_id']
    return {'schema_version':'twinhospital.programmatic-analysis/1','implementation_version':'fixture-v1',
        'analysis_kind':'engineering_fixture_not_empirical','planned_units':units,
        'bootstrap':{'seed':7,'repeats':200,'alpha':.05},
        'configurations':[{'config_id':'a','task':'preconsult','records':a},{'config_id':'b','task':'preconsult','records':b}],
        'contrasts':[{'a':'a','b':'b'}]}


class AnalysisTests(unittest.TestCase):
    def test_pooled_counts_are_not_average_patient_rates(self):
        rows=[record('a','a',correct=0,total=1),record('b','b',correct=9,total=9)]
        self.assertEqual(aggregate(rows,'documentation','factual_accuracy')['estimate'],.9)
        self.assertEqual(aggregate(rows,'preconsult','omission')['estimate'],.1)

    def test_paired_bootstrap_uses_same_patient_draw_for_both_configurations(self):
        result=analyze(dataset());paired=result['contrasts'][0]['metrics']['factual_error']
        self.assertEqual(paired['estimate'],0)
        self.assertEqual(paired['ci'],[0,0])
        self.assertEqual(result,analyze(dataset()))

    def test_replicates_and_derived_cases_do_not_increase_independent_patient_count(self):
        data=dataset();data['planned_units'][1]['patient_key']='p0'
        data['planned_units'][2]['patient_key']='p0';data['planned_units'][3]['patient_key']='p0'
        result=analyze(data)
        self.assertEqual(result['distinct_source_patients'],1)
        self.assertEqual(result['planned_units'],4)
        self.assertEqual(result['configurations']['a']['metrics']['factual_error']['bootstrap']['status'],'fewer_than_two_source_patients')
        duplicate=deepcopy(data);duplicate['configurations'][0]['records'].append(deepcopy(duplicate['configurations'][0]['records'][0]))
        with self.assertRaises(ValueError):analyze(duplicate)

    def test_partial_and_model_scoring_never_fill_formal_clinical_comparison(self):
        data=dataset();data['configurations'][0]['records'][0]['measurement']['status']='pending'
        result=analyze(data)
        self.assertIsNone(result['configurations']['a']['metrics']['completeness']['estimate'])
        self.assertEqual(result['configurations']['a']['engineering_observed_completion_rate'],1)
        self.assertIsNone(result['contrasts'][0]['metrics']['completeness']['estimate'])
        data=dataset();data['configurations'][0]['records'][0]['measurement']['source']='model'
        self.assertIsNone(analyze(data)['configurations']['a']['metrics']['factual_error']['estimate'])
        data=dataset();data['configurations'][0]['records'].pop()
        result=analyze(data);self.assertEqual(result['configurations']['a']['engineering_observed_completion_rate'],.75)
        self.assertEqual(result['configurations']['a']['metrics']['completeness']['status'],'planned_output_missing')

    def test_effort_keeps_unsuccessful_case_work_and_shared_qualification_denominator(self):
        rows=[record('a','a',correct=1),record('b','b',correct=0)]
        rows[1]['execution']['status']='failed';rows[1]['artifact_available']=False
        total=aggregate(rows,'preconsult','effort_per_qualifying_task')
        self.assertEqual(total['numerator'],3);self.assertEqual(total['denominator'],1);self.assertEqual(total['estimate'],3)
        nurse=aggregate(rows,'preconsult','nurse_effort_per_qualifying_task')['estimate']
        doctor=aggregate(rows,'preconsult','physician_effort_per_qualifying_task')['estimate']
        self.assertEqual(nurse+doctor,total['estimate'])
        rows[0]['counts']['final_quality_qualified']=False
        self.assertIsNone(aggregate(rows,'preconsult','effort_per_qualifying_task')['estimate'])

    def test_zero_denominator_and_undefined_resamples_remain_explicit(self):
        data=dataset()
        for config in data['configurations']:
            for i,row in enumerate(config['records']):
                row['counts']['risks']={'applicable':int(i==0),'correctly_identified_and_flagged':int(i==0),'indeterminate':0}
        result=analyze(data);metric=result['configurations']['a']['metrics']['high_risk_recall']
        self.assertEqual(metric['estimate'],1)
        self.assertIsNone(metric['bootstrap']['ci'])
        self.assertGreater(metric['bootstrap']['undefined_resamples'],0)
        for config in data['configurations']:
            for row in config['records']:row['counts']['risks']={'applicable':0,'correctly_identified_and_flagged':0,'indeterminate':0}
        self.assertEqual(analyze(data)['configurations']['a']['metrics']['high_risk_recall']['status'],'undefined_zero_denominator')

    def test_empty_groups_mixed_versions_and_missing_safety(self):
        data=dataset();data['planned_units']=[]
        for config in data['configurations']:config['records']=[]
        result=analyze(data);self.assertIsNone(result['configurations']['a']['engineering_observed_completion_rate'])
        self.assertEqual(result['configurations']['a']['metrics']['factual_error']['status'],'empty_group')
        data=dataset();data['configurations'][0]['records'][0]['implementation_version']='other-best-version'
        with self.assertRaises(ValueError):analyze(data)
        data=dataset();data['contrasts']=[];data['configurations']=data['configurations'][:1];data['configurations'][0]['task']='workflow'
        result=analyze(data);self.assertIsNone(result['configurations']['a']['metrics']['unsafe_execution_rate']['estimate'])
        self.assertIsNone(result['human_staff_time'])

    def test_run_identity_and_programmatic_source_coverage_are_required(self):
        data=dataset();data['configurations'][1]['records'][0]['run_id']=data['configurations'][0]['records'][0]['run_id']
        with self.assertRaisesRegex(ValueError,'multiple configurations'):analyze(data)
        data=dataset();measurement=data['configurations'][0]['records'][0]['measurement']
        measurement['source_refs']=[]
        result=analyze(data)['configurations']['a']
        self.assertEqual(result['programmatic_measurement_complete_units'],3)
        self.assertIsNone(result['metrics']['completeness']['estimate'])
        measurement['source_refs']=['actual-record-hash']
        measurement['covered_metrics']=['effort_per_qualifying_task']
        result=analyze(data)['configurations']['a']
        self.assertEqual(result['programmatic_measurement_complete_units'],4)
        self.assertEqual(result['metrics']['completeness']['status'],'outside_declared_programmatic_coverage')

    def test_signed_decision_note_can_be_assessed_but_is_not_complete_workflow_or_pending_zero_Nq(self):
        row=record('u','no-rx-note')
        row['execution'].update(status='incomplete_clinical_decision',workflow_complete=False,three_task_eligible=False,
            clinical_disposition_status='incomplete_clinical_decision',disposition_documentation_status='attested')
        self.assertEqual(aggregate([row],'documentation','factual_accuracy')['estimate'],1)
        row['safety']={'whole_run_state_audit_complete':True,'audit_source_ref':'fixture-audit',
            'observation_window_complete':True,'correct_complete':True,'unsafe_effect':False,'timed_out':False}
        with self.assertRaisesRegex(ValueError,'contradicts'):
            aggregate([row],'workflow','correct_completion_rate')
        row['safety']['correct_complete']=False
        self.assertEqual(aggregate([row],'workflow','correct_completion_rate')['estimate'],0)
        data=dataset();data['contrasts']=[];data['configurations']=data['configurations'][:1]
        for item in data['configurations'][0]['records']:
            item['counts']['final_quality_qualified']=None
        result=analyze(data)['configurations']['a']['metrics']['effort_per_qualifying_task']
        self.assertIsNone(result['estimate']);self.assertIsNone(result['denominator'])
        self.assertNotEqual(result['status'],'undefined_zero_denominator')


if __name__=='__main__':unittest.main()
