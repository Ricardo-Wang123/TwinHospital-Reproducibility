from copy import deepcopy
import unittest

from sandbox.experiments.linked.safe_context_adapter import (
    VisibleDocumentedCandidatePPRS, prepare_shared_rule_context)
from sandbox.experiments.linked.medication_catalogue import get_formulary_v3, get_catalogue
from sandbox.experiments.visibility import assert_visible


def fixture(age=72,status='approximate'):
    context={'hospital_history':{'initial_view':{'demographics':{'age':age,'age_status':status},
        'time_basis':{'history_offset_origin':'source_discharge','new_visit_offset_hours':24},
        'allergy_record':{'status':'unknown','item_count':0}},'queried_pages':[]},
        'consultation':{'plan':'Synthetic reported current use; not structured confirmation.'},
        'conversation':[{'sequence':1,'speaker_role':'patient','phase':'consultation',
            'text':'I take this now and cannot remember other medicines.'}],
        'handoff':{},'current_encounter_measurements':{}}
    rx={'items':[{'drug_code':'SIM-METFORMIN-500','dose':500,'dose_unit':'mg',
        'frequency':'BID','route':'oral','quantity':28,'quantity_unit':'tablet'}]}
    return context,rx,get_formulary_v3(),get_catalogue(include_iv=True,include_fault_only=True)


class SafeContextTests(unittest.TestCase):
    def test_preparation_is_idempotent_and_does_not_expose_full_index_or_infer_clinical_facts(self):
        context,rx,formula,catalogue=fixture();before=deepcopy(context)
        first=prepare_shared_rule_context(context,formula,catalogue)
        second=prepare_shared_rule_context(first['shared_context'],formula,catalogue)
        self.assertEqual(first,second);self.assertEqual(before,context)
        assert_visible(first['shared_context'])
        self.assertNotIn('entries',first['shared_context']['documented_rule_evidence'])
        facts=first['shared_context']['documented_rule_context']
        self.assertEqual(facts['active_medications'],[])
        self.assertIsNone(facts['medication_reconciliation']['all_current_products_mapped'])
        self.assertEqual(facts['allergies']['assessment_status'],'unknown')
        self.assertIsNone(facts['iv_therapy_appropriate'])
        self.assertFalse(first['audit']['policy']['natural_language_extraction'])

    def test_approximate_near_threshold_and_topcoded_ages_are_not_exact(self):
        for age,status in [(18,'approximate'),('90+','90_plus'),(None,'unknown'),(72,'approximate')]:
            context,_,formula,catalogue=fixture(age,status)
            facts=prepare_shared_rule_context(context,formula,catalogue)['shared_context']['documented_rule_context']['demographics']
            self.assertIsNone(facts['age']);self.assertEqual(facts['reported_age'],age)
            self.assertEqual(facts['age_status'],status)
        context,_,formula,catalogue=fixture(18,'exact')
        self.assertEqual(prepare_shared_rule_context(context,formula,catalogue)['shared_context']['documented_rule_context']['demographics']['age'],18)

    def test_tampered_context_or_product_source_is_not_trusted_by_nonempty_refs(self):
        context,rx,formula,catalogue=fixture();pprs=VisibleDocumentedCandidatePPRS(formula,catalogue)
        first=pprs.prepare_context(context)['shared_context']
        for key,value in [('age',50),('evidence_ref','made-up-nonempty-reference')]:
            changed=deepcopy(first);changed['documented_rule_context']['demographics'][key]=value
            with self.assertRaises(ValueError):pprs.review(rx,changed)
        changed=deepcopy(catalogue);changed[0]['source_references'][0]['setid']='other-label'
        with self.assertRaises(ValueError):VisibleDocumentedCandidatePPRS(formula,changed).prepare_context(first)

    def test_runtime_unknowns_flag_instead_of_becoming_compliant_gold(self):
        context,rx,formula,catalogue=fixture();original=deepcopy(rx)
        pprs=VisibleDocumentedCandidatePPRS(formula,catalogue)
        result=pprs.review(rx,pprs.prepare_context(context)['shared_context'])
        self.assertEqual(result['decision'],'flag');self.assertTrue(result['development_only'])
        self.assertIsNone(result['clinical_gold_label'])
        self.assertGreater(result['finding_status_counts'].get('indeterminate',0),0)
        self.assertGreater(result['finding_status_counts'].get('not_applicable',0),0)
        self.assertEqual(rx,original);self.assertNotIn('exposure',rx['items'][0])
        self.assertEqual(result['visible_evidence_audit']['source_catalogue_hash'],pprs.source_catalogue_hash)

    def test_hidden_fields_never_enter_rule_context(self):
        context,_,formula,catalogue=fixture()
        for key in ['evaluation','his_initial','patient_view','provenance']:
            changed=deepcopy(context);changed[key]={}
            with self.subTest(key=key),self.assertRaises(ValueError):prepare_shared_rule_context(changed,formula,catalogue)

    def test_visible_current_reports_survive_without_becoming_verified_exposure(self):
        context,_,formula,catalogue=fixture()
        context['hospital_history']['initial_view']['current_use_record']={
            'status':'reported_prior_plan_available_current_review_pending',
            'scope_status':'defined_report_window', 'item_count':3, 'items_expanded':False}
        report={'name':'fictional medicine', 'source_kind':'constructed',
            'current_use_status':'reported_taken_in_defined_episode',
            'actual_use':{'reported_taken_doses':12}}
        context['hospital_history']['queried_pages']=[{'phase':'consultation',
            'page':{'section':'current_use_items','time_basis':{'origin':'constructed'},
                    'records':[{'record_ref':'reported-use-1','record':report}]}}]
        prepared=prepare_shared_rule_context(context,formula,catalogue)
        facts=prepared['shared_context']['documented_rule_context']
        rec=facts['medication_reconciliation']
        self.assertEqual(rec['status'],'reported_unverified')
        self.assertEqual(rec['reported_evidence'][1]['content'],report)
        self.assertEqual(len(rec['reported_evidence']),2)  # directory plus one read item, not three
        self.assertEqual(facts['active_medications'],[])
        self.assertIsNone(rec['all_current_products_mapped'])
        self.assertEqual(prepared,prepare_shared_rule_context(prepared['shared_context'],formula,catalogue))
        # Exact same object in historical orders does not become current use.
        del context['hospital_history']['initial_view']['current_use_record']
        context['hospital_history']['queried_pages'][0]['page']['section']='historical_orders'
        facts=prepare_shared_rule_context(context,formula,catalogue)['shared_context']['documented_rule_context']
        self.assertEqual(facts['medication_reconciliation']['status'],'not_established')
        self.assertEqual(facts['medication_reconciliation']['reported_evidence'],[])

    def test_none_known_report_and_source_unknown_are_both_retained(self):
        context,_,formula,catalogue=fixture()
        allergy=context['hospital_history']['initial_view']['allergy_record']
        allergy['patient_report']={'status':'reported_none_known','verification_status':'patient_report'}
        facts=prepare_shared_rule_context(context,formula,catalogue)['shared_context']['documented_rule_context']
        record=facts['allergies']
        self.assertEqual(record['assessment_status'],'patient_report_available_unverified')
        self.assertEqual(record['reported_evidence'][0]['content']['status'],'unknown')
        self.assertEqual(record['reported_evidence'][0]['content']['patient_report']['status'],'reported_none_known')
        self.assertEqual(record['items'],[])
        self.assertIsNone(record['assessment_ref'])
        self.assertIsNone(record['desensitization_or_rechallenge'])

    def test_conflicting_queried_reports_are_preserved_not_silently_reconciled(self):
        context,_,formula,catalogue=fixture()
        context['hospital_history']['queried_pages']=[{'phase':'consultation','page':{
            'section':'current_use_items','records':[
                {'record_ref':'one','record':{'name':'fictional medicine','current_use_status':'reported_taken'}},
                {'record_ref':'two','record':{'name':'fictional medicine','current_use_status':'reported_not_taken'}}]}}]
        rec=prepare_shared_rule_context(context,formula,catalogue)['shared_context']['documented_rule_context']['medication_reconciliation']
        self.assertEqual(len(rec['reported_evidence']),2)
        self.assertEqual(rec['status'],'reported_unverified')
        self.assertIsNone(rec['all_current_products_mapped'])

    def test_current_physician_assessments_survive_without_becoming_gold_or_exposure(self):
        context,_,formula,catalogue=fixture()
        assessment={'status':'complete_declared_episode','basis':'Reviewed the reported episode list with patient.',
                    'unresolved':[]}
        allergy={'status':'none_known','basis':'Patient denies known prior reactions; source record remains unknown.'}
        context['consultation'].update(medication_reconciliation=assessment,allergy_assessment=allergy)
        prepared=prepare_shared_rule_context(context,formula,catalogue)
        documented=prepared['shared_context']['documented_rule_context']
        for name,field,value in [('medication_reconciliation','medication_reconciliation',assessment),
                                 ('allergies','allergy_assessment',allergy)]:
            preserved=documented[name]['current_physician_assessment']
            self.assertEqual(preserved['content'],value)
            self.assertEqual(preserved['epistemic_status'],'agent_reported')
            self.assertEqual(preserved['independent_clinical_truth'],'not_established')
            self.assertTrue(preserved['source_pointer'].endswith('/'+field))
        self.assertEqual(documented['active_medications'],[])
        self.assertIsNone(documented['medication_reconciliation']['all_current_products_mapped'])
        self.assertEqual(documented['allergies']['assessment_status'],'unknown')
        self.assertIsNone(documented['allergies']['assessment_ref'])
        self.assertEqual(prepared,prepare_shared_rule_context(prepared['shared_context'],formula,catalogue))
        # Missing assessments cannot become a completed reconciliation or NKA.
        context['consultation']={'plan':'No structured assessment supplied.'}
        documented=prepare_shared_rule_context(context,formula,catalogue)['shared_context']['documented_rule_context']
        self.assertNotIn('current_physician_assessment',documented['medication_reconciliation'])
        self.assertNotIn('current_physician_assessment',documented['allergies'])

    def test_nonempty_exposure_ref_is_not_proof_but_exact_visible_intent_can_be_used(self):
        context,_,formula,catalogue=fixture()
        context['consultation']['declared_order_intent']={'status':'active',
            'exposure':{'start_hour':24,'end_hour':48,'administration_intent':'concurrent_active'}}
        pprs=VisibleDocumentedCandidatePPRS(formula,catalogue,rule_ids=['tiotropium_handihaler_not_swallowed'])
        pack=pprs.prepare_context(context);index=pack['evidence_index']
        source=next(e for e in index['entries'] if e['source_kind']=='consultation')
        # This is an independent virtual fixture, not a real patient or gold label.
        rx={'items':[{'drug_code':'SIM-TIOTROPIUM-18','dose':1,'dose_unit':'capsule','frequency':'QD',
            'quantity':1,'quantity_unit':'capsule','route':'oral','route_intent':'swallow_capsule','status':'active',
            'exposure':{'start_hour':24,'end_hour':48,'administration_intent':'concurrent_active',
                'evidence_ref':'made-up-reference','evidence_pointer':'/declared_order_intent','time_basis':{}}}]}
        first=pprs.review(rx,pack['shared_context'])
        self.assertEqual(first['documented_rule_findings'][0]['status'],'indeterminate')
        self.assertEqual(rx['items'][0]['status'],'active')
        rx['items'][0]['exposure'].update(evidence_ref=source['ref'],time_basis=source['time_basis'])
        # An absent time basis is not a verified common clock.
        self.assertEqual(pprs.review(rx,pack['shared_context'])['order_exposure_resolution'][0]['status'],'indeterminate')
        context['hospital_history']['queried_pages']=[{'phase':'consultation','queried_after_sequence':1,
            'page':{'time_basis':{'origin':'constructed_current_encounter'},'records':[{
                'record_ref':'constructed-intent','record':context['consultation']['declared_order_intent']}]}}]
        pack=pprs.prepare_context(context);source=next(e for e in pack['evidence_index']['entries'] if e['source_kind']=='queried_chart_record')
        rx['items'][0]['exposure'].update(evidence_ref=source['ref'],evidence_pointer='',time_basis=source['time_basis'])
        result=pprs.review(rx,pack['shared_context'])
        self.assertEqual(result['order_exposure_resolution'][0]['status'],'resolved')
        self.assertEqual(result['documented_rule_findings'][0]['status'],'violation')
        self.assertIsNone(result['clinical_gold_label'])

    def test_physician_course_binds_only_exact_visible_candidate_and_not_ingestion(self):
        context,rx,formula,catalogue=fixture()
        rx['items'][0]['intended_course']={'start_offset_hours':24,'duration_days':14,
                                         'basis':'Physician explicitly chooses fourteen days in this fixture.'}
        context['consultation']['prescription']=deepcopy(rx)
        pprs=VisibleDocumentedCandidatePPRS(formula,catalogue)
        before=deepcopy(rx);result=pprs.review(rx,context)
        binding=result['order_exposure_resolution'][0]
        self.assertEqual(binding['status'],'resolved')
        self.assertEqual(binding['reason'],'literal_physician_proposed_course')
        self.assertIn('not_observed_ingestion',binding['meaning'])
        self.assertEqual(rx,before)
        self.assertEqual(result['decision'],'flag')  # Other actual missing clinical predicates remain missing.
        self.assertIsNone(result['clinical_gold_label'])
        changed=deepcopy(rx);changed['items'][0]['dose']=1000
        self.assertEqual(pprs.review(changed,context)['order_exposure_resolution'][0]['status'],'indeterminate')
        context['consultation'].pop('prescription')
        context['consultation']['prescription_review_followups']=[{'physician_response':{'prescription':deepcopy(rx)}}]
        self.assertEqual(pprs.review(rx,context)['order_exposure_resolution'][0]['status'],'resolved')
        # A guessed date, backwards interval, absent clock, or historical use is not filled.
        for course in ({'start_offset_hours':-10,'duration_days':14,'basis':'x'},
                       {'start_offset_hours':24,'duration_days':0,'basis':'x'},
                       {'start_offset_hours':24,'basis':'x'}):
            changed=deepcopy(rx);changed['items'][0]['intended_course']=course
            context['consultation']={'prescription':deepcopy(changed)}
            self.assertEqual(pprs.review(changed,context)['order_exposure_resolution'][0]['status'],'indeterminate')


if __name__=='__main__':unittest.main()
