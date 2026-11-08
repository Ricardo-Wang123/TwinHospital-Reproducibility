from copy import deepcopy
import unittest
from sandbox.experiments.linked.chart_projection import project_history_page,verify_projection,expand_projected_record,deduplicate_projected_page,ProjectionBudgetExceeded

def page():
    return {'section':'historical_observations','time_basis':{'history_offset_origin':'source_anchor_discharge','new_visit_offset_hours':24},
        'selection':{'offset':0,'limit':2,'time_order':'latest_first'},'matching_count':2,'total_count':2,
        'returned_count':2,'next_offset':None,'records':[
            {'record_ref':f'chart_example_{i}','record':{'record_id':f'obs_{i}',
             'source_ref':f'src_{i}','dictionary_source_ref':'src_dictionary','source_encounter_ref':'enc_fixture',
             'display':'Creatinine','value_raw':'___','numeric_value_raw':v,'unit_raw':'mg/dL',
             'status':'historical','flag_raw':None,'reference_range_raw':{'ref_range_lower':'0.5','ref_range_upper':'1.2'},
             'time':{'basis':'source_relative_to_anchor_discharge','event_offset_hours':t,'recorded_offset_hours':t,
                'event_time_known':True,'available_by_source_cutoff':True,'availability_basis':'known at source discharge'}}}
            for i,(v,t) in enumerate([(1.1,-12),(None,-24)])]}

class ProjectionTests(unittest.TestCase):
    def test_frozen_v1_history_remains_readable_without_rewriting_evidence(self):
        raw=page();prior=project_history_page(raw)
        prior['projection_version']='clinical_field_projection_v1'
        original=deepcopy(prior)
        self.assertTrue(verify_projection(raw,prior))
        self.assertEqual(expand_projected_record(prior,prior['records'][0])['numeric_value_raw'],1.1)
        later=deduplicate_projected_page(project_history_page(raw),[prior])
        self.assertTrue(later['all_records_already_visible'])
        self.assertTrue(verify_projection(raw,later['page'],[prior]))
        self.assertEqual(prior,original)

    def test_unknown_version_and_supplemental_section_claiming_v1_are_rejected(self):
        raw=page();projected=project_history_page(raw)
        for version,section in [('future_unreviewed','historical_observations'),
                ('clinical_field_projection_v1','historical_administrations')]:
            altered=deepcopy(projected);altered.update(projection_version=version,section=section)
            self.assertFalse(verify_projection(raw,altered))
            with self.assertRaises(ValueError):expand_projected_record(altered,altered['records'][0])

    def test_lossless_clinical_restore_and_no_source_mutation(self):
        raw=page();before=deepcopy(raw);p=project_history_page(raw)
        self.assertTrue(verify_projection(raw,p));self.assertEqual(raw,before)
        self.assertEqual(expand_projected_record(p,p['records'][0])['value_raw'],'___')
        self.assertIsNone(expand_projected_record(p,p['records'][1])['numeric_value_raw'])
        self.assertEqual(p['records'][0]['derived_time_display']['event_hours_before_new_visit'],36)
        self.assertEqual(p['records'][0]['record_ref'],raw['records'][0]['record_ref'])

    def test_missing_and_null_distinct(self):
        raw=page();del raw['records'][1]['record']['time']['event_time_known']
        p=project_history_page(raw);self.assertTrue(verify_projection(raw,p))
        self.assertNotIn('derived_time_display',p['records'][1])
        self.assertNotIn('event_time_known',p['record_defaults'].get('time',{}))

    def test_unknown_anchor_does_not_invent_current_timing(self):
        raw=page();raw['time_basis']={'history_offset_origin':'unknown'}
        p=project_history_page(raw);self.assertTrue(all('derived_time_display' not in x for x in p['records']))

    def test_reject_hidden_fields_in_record(self):
        raw=page();raw['records'][0]['record']['evaluation']={'gold':'x'}
        with self.assertRaises(ValueError):project_history_page(raw)

    def test_changed_unit_or_value_detected(self):
        raw=page();p=project_history_page(raw);p['records'][0]['record']['unit_raw']='wrong'
        self.assertFalse(verify_projection(raw,p))

    def test_time_origin_or_derived_display_tampering_detected(self):
        raw=page();p=project_history_page(raw);p['time_basis']['new_visit_offset_hours']=48
        self.assertFalse(verify_projection(raw,p));p=project_history_page(raw)
        p['records'][0]['derived_time_display']['event_hours_before_new_visit']=0
        self.assertFalse(verify_projection(raw,p))

    def test_commorbidity_diagnosis_and_unknown_status_preserved(self):
        raw={'section':'diagnoses','records':[{'record_ref':'chart_dx_0','record':{
            'coding':{'system':'ICD','version':'10','code':'N189'},'display':'CKD, unspecified',
            'source_sequence':3,'status':'historical','source_ref':'src_any','record_id':'dx_any'}}]}
        p=project_history_page(raw);self.assertTrue(verify_projection(raw,p));self.assertEqual(p['records'][0]['record']['coding']['code'],'N189')

    def test_product_regimen_stop_and_exposure_uncertainty_preserved(self):
        rec={'record_id':'rx_fixture','source_ref':'src_fixture','drug_name_raw':'Example ER tablet',
            'evidence_type':'historical_order','planned_stop_offset_hours':12,'current_use_inferred':False,
            'regimen_raw':{'prod_strength':'10 mg ER','form_rx':'tablet','dose_val_rx':'10','dose_unit_rx':'mg',
                'form_val_disp':'1','form_unit_disp':'tablet','doses_per_24_hrs':'2','route':'PO','frequency':'BID'},
            'normalization':{'status':'unmapped','drug_id':None}}
        raw={'section':'historical_orders','records':[{'record_ref':'chart_rx_0','record':rec}]}
        p=project_history_page(raw);restored=expand_projected_record(p,p['records'][0])
        self.assertTrue(verify_projection(raw,p));self.assertEqual(restored['planned_stop_offset_hours'],12)
        self.assertEqual(restored['regimen_raw'],rec['regimen_raw']);self.assertFalse(restored['current_use_inferred'])

    def test_all_duplicate_refs_share_existing_evidence(self):
        raw=page();prior=project_history_page(raw);raw2=deepcopy(raw);raw2['selection']['limit']=5
        new=project_history_page(raw2);d=deduplicate_projected_page(new,[{'page':prior}])
        self.assertTrue(d['all_records_already_visible']);self.assertEqual(d['new_record_refs'],[])
        self.assertTrue(verify_projection(raw2,d['page'],[prior]));self.assertFalse(verify_projection(raw2,d['page']))
        self.assertEqual(len(prior['records']),2);self.assertEqual(len(new['records']),2)

    def test_partial_overlap_and_conflicting_ref(self):
        raw=page();first=deepcopy(raw);first['records']=first['records'][:1];first['returned_count']=1
        prior=project_history_page(first);new=project_history_page(raw);d=deduplicate_projected_page(new,[prior])
        self.assertFalse(d['all_records_already_visible']);self.assertEqual(len(d['new_record_refs']),1)
        self.assertTrue(verify_projection(raw,d['page'],[prior]))
        new['records'][0]['record']['numeric_value_raw']=99
        with self.assertRaises(ValueError):deduplicate_projected_page(new,[prior])

    def test_empty_query_and_required_expansion_not_discarded(self):
        raw=page();raw['records']=[];raw['returned_count']=0
        p=project_history_page(raw);d=deduplicate_projected_page(p,[])
        self.assertFalse(d['all_records_already_visible'])
        raw['requires_expansion']=[{'record_ref':'chart_large_0','suggested_sections':['diagnoses']}]
        self.assertFalse(deduplicate_projected_page(project_history_page(raw),[])['all_records_already_visible'])

    def test_budget_error_is_distinct_from_semantic_validation(self):
        raw={'section':'historical_observations','records':[{'record_ref':'chart_large_0','record':{'display':'X'*29500}}]}
        with self.assertRaises(ProjectionBudgetExceeded) as caught:project_history_page(raw)
        self.assertGreater(caught.exception.projected_bytes,caught.exception.limit)
        self.assertEqual(raw['records'][0]['record']['display'],'X'*29500)

if __name__=='__main__':unittest.main()
