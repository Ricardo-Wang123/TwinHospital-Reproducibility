from copy import deepcopy
import unittest

from sandbox.experiments.linked.evidence_bindings import (
    build_visible_evidence, resolve_evidence, validate_claims, build_product_bindings,
    load_applicability_registry, _hash)
from sandbox.experiments.linked.medication_catalogue import get_formulary_v3, get_catalogue


def fixture():
    context = {'hospital_history': {'initial_view': {'demographics': {'age': 55},
        'allergy_record': {'status': 'unknown', 'items_expanded': False},
        'sections': {'historical_orders': {'total_count': 3, 'expanded_record_count': 0}}},
        'queried_pages': [{'phase': 'consultation', 'queried_after_sequence': 3,
          'page': {'time_basis': {'origin': 'historical_admission'}, 'records': [
              {'record_ref': 'chart_order_0', 'record': {'name': 'metformin', 'status': 'historical',
               'dose': 500, 'dose_unit': 'mg', 'source_kind': 'source_record'}}]}}]},
        'handoff': {'allergies': 'Unknown'},
        'consultation': {'plan': 'Review the current medication list.'},
        'current_encounter_measurements': {'readback_verified': True, 'queried_after_sequence': 3,
            'time_basis': {'origin': 'new_simulated_encounter'}, 'records': [
                {'record_ref': 'chart_current_0', 'record': {'label': 'Creatinine', 'value': 1.1,
                 'unit': 'mg/dL', 'source_kind': 'constructed', 'available_at': 'physician_consultation'}}]},
        'conversation': [{'sequence': 4, 'speaker_role': 'patient', 'text': 'I think I take it once daily.',
                           'phase': 'consultation', 'available_at_ms': 7000}]}
    return context, get_formulary_v3(), get_catalogue(include_iv=True, include_fault_only=True)


class EvidenceBindingTests(unittest.TestCase):
    def test_initial_laboratory_record_is_visible_historical_evidence_not_new_measurement(self):
        from sandbox.experiments.linked.chart import HistoryChart
        context,formula,catalogue=fixture()
        record={'record_id':'obs_'+'1'*24,'display':'Creatinine','fluid':'Blood',
            'numeric_value_raw':'4.8','value_raw':'4.8','unit_raw':'mg/dL','source_kind':'observed_source',
            'time':{'event_offset_hours':-20,'recorded_offset_hours':-19,'available_by_source_cutoff':True}}
        initial=HistoryChart({'historical_observations':[record],
            'time_basis':{'new_visit_offset_hours':168}}).initial_view()
        context['hospital_history']['initial_view']=initial
        before=deepcopy(context);index=build_visible_evidence(context,formula,catalogue)
        entry=next(e for e in index['entries'] if e['source_kind']=='initial_chart_record')
        self.assertEqual(entry['content'],record)
        self.assertEqual(entry['chart_section'],'historical_observations')
        self.assertEqual(self.resolve(index,entry,pointer='/numeric_value_raw',expected_value='4.8')['status'],'resolved')
        self.assertEqual(resolve_evidence(index,entry['ref'],role='patient',phase='prescription_review')['status'],'indeterminate')
        self.assertEqual(context,before)

    def index(self):
        return build_visible_evidence(*fixture())

    def resolve(self, index, entry, **kwargs):
        return resolve_evidence(index, entry['ref'], role='pharmacist', phase='prescription_review', **kwargs)

    def test_deterministic_snapshot_no_inference_or_mutation(self):
        args = fixture(); original = deepcopy(args)
        one = build_visible_evidence(*args)
        self.assertEqual(one, build_visible_evidence(*args))
        self.assertEqual(args, original)
        self.assertFalse(one['current_use_inferred_from_history'])
        self.assertFalse(one['clinical_facts_inferred'])
        self.assertNotIn('active_medications', one)
        # Only one of three historical orders was actually read.
        rows = [e for e in one['entries'] if e['source_kind'] == 'queried_chart_record']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['content']['status'], 'historical')

    def test_projected_defaults_restore_literal_status_source_and_time(self):
        from sandbox.experiments.linked.chart_projection import VERSION as PROJECTION_VERSION
        context,formula,catalogue=fixture()
        page=context['hospital_history']['queried_pages'][0]['page']
        page.update(projection_version=PROJECTION_VERSION,source_page_sha256='a'*64,
            record_defaults={'status':'historical','source_kind':'constructed',
                'time':{'basis':'source_relative_to_anchor_discharge','event_time_known':True}})
        record=page['records'][0]['record'];record.pop('status');record.pop('source_kind')
        record['time']={'event_offset_hours':-4}
        original=deepcopy(context)
        index=build_visible_evidence(context,formula,catalogue)
        entry=next(x for x in index['entries'] if x['source_kind']=='queried_chart_record')
        self.assertEqual(entry['content']['status'],'historical')
        self.assertEqual(entry['epistemic_status'],'constructed')
        self.assertEqual(entry['content']['time'],{'basis':'source_relative_to_anchor_discharge','event_time_known':True,'event_offset_hours':-4})
        self.assertEqual(self.resolve(index,entry,pointer='/status',expected_value='historical')['status'],'resolved')
        self.assertEqual(entry['source_projection']['version'],PROJECTION_VERSION)
        self.assertEqual(context,original)
        page['projection_version']='unknown-projection'
        with self.assertRaises(ValueError):build_visible_evidence(context,formula,catalogue)

    def test_exact_typed_value_unit_and_temporal_basis(self):
        index = self.index(); e = next(x for x in index['entries'] if x['source_kind'] == 'current_measurement_readback')
        result = self.resolve(index, e, pointer='/value', expected_value=1.1, unit_pointer='/unit',
                              expected_unit='mg/dL', expected_time_basis={'origin': 'new_simulated_encounter'})
        self.assertEqual(result['status'], 'resolved')
        self.assertEqual(result['epistemic_status'], 'constructed')
        self.assertEqual(result['clinical_truth'], 'not_established')
        for kwargs in ({'pointer': '/value', 'expected_value': '1.1'},
                       {'pointer': '/value', 'unit_pointer': '/unit', 'expected_unit': 'mmol/L'},
                       {'expected_time_basis': {'origin': 'historical_admission'}},
                       {'required_epistemic_status': 'independently_confirmed'}):
            self.assertEqual(self.resolve(index, e, **kwargs)['status'], 'indeterminate')

    def test_visibility_phase_sequence_and_unknown_are_not_negative(self):
        index = self.index(); e = next(x for x in index['entries'] if x['source_kind'] == 'queried_chart_record')
        self.assertEqual(self.resolve(index, e, at_sequence=2)['status'], 'indeterminate')
        self.assertEqual(self.resolve(index, e, at_sequence=3)['status'], 'resolved')
        self.assertEqual(resolve_evidence(index, e['ref'], role='patient', phase='prescription_review')['status'], 'indeterminate')
        self.assertEqual(resolve_evidence(index, e['ref'], role='doctor', phase='preconsult')['status'], 'indeterminate')
        self.assertEqual(resolve_evidence(index, 'unread-order', role='doctor', phase='prescription_review')['status'], 'indeterminate')
        directory = next(x for x in index['entries'] if x['source_kind'] == 'visible_chart_directory')
        self.assertEqual(self.resolve(index, directory, pointer='/allergy_record/status', expected_value='none')['status'], 'indeterminate')

    def test_index_integrity_and_pinned_hash(self):
        index = self.index(); e = index['entries'][0]
        self.assertEqual(self.resolve(index, e, expected_index_hash='0' * 64)['status'], 'indeterminate')
        index['entries'][0]['content']['demographics']['age'] = 99
        self.assertEqual(self.resolve(index, e)['status'], 'indeterminate')

    def test_forbidden_inputs_and_non_visible_dialogue_rejected(self):
        for bad in ({'evaluation': {'label': 'bad'}}, {'his_initial': {}}, {'case_id': 'case'}):
            with self.assertRaises(ValueError):
                build_visible_evidence(bad, [])
        context, formula, catalogue = fixture()
        context['consultation']['plan_data'] = {'subject_id': 'hidden'}
        with self.assertRaises(ValueError):
            build_visible_evidence(context, formula, catalogue)
        context, formula, catalogue = fixture(); context['conversation'][0]['phase'] = 'preconsult'
        with self.assertRaises(ValueError):
            build_visible_evidence(context, formula, catalogue)

    def test_text_is_not_automatically_entailment_or_confirmed(self):
        index = self.index(); e = next(x for x in index['entries'] if x['source_kind'] == 'physician_visible_dialogue')
        self.assertEqual(self.resolve(index, e, pointer='/text', expected_value='QD')['status'], 'indeterminate')
        result = validate_claims(index, [{'name': 'reported_words', 'evidence_ref': e['ref'],
            'pointer': '/text', 'value': e['content']['text']}], role='rx_ai', phase='prescription_review')
        self.assertTrue(result['all_literals_resolved'])
        self.assertEqual(result['results'][0]['epistemic_status'], 'agent_reported')
        self.assertIsNone(result['clinical_gold_label'])
        self.assertFalse(validate_claims(index, [], role='rx_ai', phase='prescription_review')['all_literals_resolved'])

    def test_six_explicit_product_bindings_nine_labels_no_gold(self):
        catalogue = get_catalogue(include_iv=True, include_fault_only=True)
        index = build_visible_evidence({}, catalogue, catalogue)
        result = build_product_bindings(index)
        passed = [b for b in result['product_bindings'].values() if b['verification_status'] == 'verified']
        self.assertEqual(len(passed), 6)
        self.assertEqual(len(load_applicability_registry()['label_identity_records']), 9)
        for b in passed:
            self.assertFalse(b['clinical_gold_eligible'])
            self.assertFalse(b['full_label_snapshot_verified'])
            self.assertFalse(b['iv_appropriateness_inferred'])
            self.assertIsNone(b['clinical_gold_label'])
            self.assertTrue(b['label_source_urls'])

    def test_code_cannot_override_form_strength_ingredient_or_basis(self):
        for field, value in (('strength', '850 mg/tablet'), ('dosage_form', 'oral_tablet_extended_release'),
                             ('ingredient', 'other'), ('quantity_unit', 'capsule')):
            _, formula, catalogue = fixture(); formula[0][field] = value
            result = build_product_bindings(build_visible_evidence({}, formula, catalogue))
            self.assertEqual(result['product_bindings']['SIM-METFORMIN-500']['verification_status'], 'indeterminate')
        _, formula, catalogue = fixture()
        formula[0]['components'][0]['basis_of_strength'] = 'metformin base'
        self.assertEqual(build_product_bindings(build_visible_evidence({}, formula, catalogue))['product_bindings']['SIM-METFORMIN-500']['verification_status'], 'indeterminate')

    def test_missing_or_changed_source_setid_never_guessed(self):
        _, formula, catalogue = fixture()
        absent = build_product_bindings(build_visible_evidence({}, formula))
        self.assertEqual(absent['product_bindings']['SIM-METFORMIN-500']['verification_status'], 'indeterminate')
        catalogue[0]['source_references'][0]['setid'] = 'unverified-label'
        changed = build_product_bindings(build_visible_evidence({}, formula, catalogue))
        self.assertEqual(changed['product_bindings']['SIM-METFORMIN-500']['verification_status'], 'indeterminate')

    def test_registry_change_or_unsupported_cross_label_fact_fails(self):
        index = self.index(); r = load_applicability_registry()
        r['products']['SIM-METFORMIN-500']['reference_set_id'] = 'not-verified'
        with self.assertRaises(ValueError):
            build_product_bindings(index, registry=r)
        r['manifest_hash'] = _hash({k: v for k, v in r.items() if k != 'manifest_hash'})
        result = build_product_bindings(index, registry=r)
        self.assertEqual(result['product_bindings']['SIM-METFORMIN-500']['verification_status'], 'indeterminate')


if __name__ == '__main__':
    unittest.main()
