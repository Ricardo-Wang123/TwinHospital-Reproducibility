from copy import deepcopy
import unittest
from test_current_episode_v4 import fixture
from sandbox.experiments.linked.case_preparation.current_episode_v10 import build
from sandbox.experiments.linked.medication_catalogue_v4 import formulary


def source(frequency='BID'):
    c=fixture();c['formulary']=formulary()
    o=c['his_initial']['historical_orders'][0]
    o['drug_name_raw']='MetFORMIN XR (Glucophage XR)'
    o['regimen_raw'].update(prod_strength='500mg Tab',dose_val_rx='500',frequency=frequency)
    o['planned_stop_offset_hours']=-220
    c['provenance']['selected_historical_order']=deepcopy(o)
    c['his_initial']['historical_encounters'][0]['diagnoses']=[{
        'record_id':'dx_'+'5'*24,'display':'Type 2 diabetes mellitus',
        'coding':{'system':'ICD','version':'10','code':'E119'},'source_kind':'test_fixture'}]
    return c


class Episode10Tests(unittest.TestCase):
    def test_core_is_not_dropped_but_historical_stop_remains_historical(self):
        old=source();before=deepcopy(old);case,audit=build(old,{})
        self.assertEqual(old,before)
        self.assertTrue(audit['selected_core_scheduled'])
        self.assertTrue(audit['core_added_outside_retrieval_window'])
        self.assertEqual(case['his_initial']['historical_orders'],old['his_initial']['historical_orders'])
        item=case['his_initial']['current_use_record']['items'][0]
        self.assertEqual(item['historical_order_stop_offset_hours'],-220)
        self.assertEqual(item['plan']['start_offset_hours'],24)
        self.assertEqual(item['source_kind'],'constructed')
        self.assertEqual(item['current_product']['drug_code'],'SIM-METFORMIN-ER-500')
        self.assertEqual(item['current_product']['historical_exact_identity'],'not_established')
        self.assertIsNone(case['evaluation']['prescription_labels'])
        self.assertEqual(case['his_initial']['historical_observations'],old['his_initial']['historical_observations'])

    def test_once_is_retained_unresolved_instead_of_changed_to_regular_use(self):
        case,audit=build(source('ONCE'),{})
        self.assertFalse(audit['selected_core_scheduled'])
        item=case['his_initial']['current_use_record']['items'][0]
        self.assertNotIn('plan',item)
        self.assertNotIn('current_product',item)
        self.assertEqual(item['frequency'],'ONCE')

    def test_no_core_or_diagnosis_is_fabricated_from_group_label(self):
        c=source();c['his_initial']['historical_encounters'][0]['diagnoses']=[]
        with self.assertRaises(ValueError):build(c,{})
        c=source();c['provenance']['selected_historical_order']['drug_name_raw']='changed source'
        with self.assertRaises(ValueError):build(c,{})

    def test_existing_newer_pattern_does_not_create_two_conflicting_current_plans(self):
        c=source();other=deepcopy(c['his_initial']['historical_orders'][0])
        other['record_id']='rx_'+'9'*24;other['planned_stop_offset_hours']=2
        other['regimen_raw']['dose_val_rx']='1000';c['his_initial']['historical_orders'].append(other)
        case,audit=build(c,{})
        self.assertFalse(audit['selected_core_scheduled'])
        self.assertIn('another_current_candidate_for_same_product_requires_reconciliation',audit['core_unresolved_reasons'])
        self.assertEqual(sum('plan' in i for i in case['his_initial']['current_use_record']['items']),1)


if __name__=='__main__':unittest.main()
