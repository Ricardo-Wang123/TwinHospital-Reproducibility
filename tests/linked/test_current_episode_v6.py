from copy import deepcopy
import unittest
from test_current_episode_v4 import fixture
from sandbox.experiments.linked.case_preparation.current_episode_v6 import build_basic_episode,source_gate
from sandbox.experiments.linked.chart import HistoryChart
from sandbox.experiments.linked.chart_projection import project_history_page,verify_projection
from sandbox.experiments.linked.medication_catalogue import get_formulary_v3


def source():
    c=fixture();c['formulary']=get_formulary_v3();o=c['his_initial']['historical_orders'][0]
    o['drug_name_raw']='MetFORMIN (Glucophage)'
    o['regimen_raw'].update(prod_strength='500mg Tablet',dose_val_rx='500')
    c['his_initial']['historical_encounters'][0]['diagnoses']=[{
        'record_id':'dx_'+'5'*24,'display':'Type 2 diabetes mellitus',
        'coding':{'system':'ICD','version':'10','code':'E119'},'source_kind':'test_fixture'}]
    return c


class BasicEpisodeTests(unittest.TestCase):
    def test_current_product_is_constructed_without_relabelling_history_or_labels(self):
        old=source();before=deepcopy(old);case,audit=build_basic_episode(old,{})
        self.assertEqual(old,before)
        for key in ('historical_encounters','historical_orders','historical_observations'):
            self.assertEqual(case['his_initial'][key],old['his_initial'][key])
        product=case['his_initial']['current_use_record']['items'][0]['current_product']
        self.assertEqual(product['source_kind'],'constructed')
        self.assertEqual(product['historical_exact_identity'],'not_established')
        self.assertEqual(product['drug_code'],'SIM-METFORMIN-500')
        self.assertIsNone(case['evaluation']['prescription_labels'])
        self.assertEqual(case['patient_key'],old['patient_key'])
        self.assertEqual(case['split'],old['split'])
        self.assertFalse(audit['formal_eligible'])
        self.assertEqual(case['his_initial']['allergy_record']['status'],'unknown')
        chart=HistoryChart(case['his_initial']);p=chart.query('current_use_items',limit=20)
        self.assertTrue(verify_projection(p,project_history_page(p)))

    def test_recorded_risk_or_different_product_is_not_silently_removed_to_qualify(self):
        c=source();c['evaluation']['source_risk_cues']=[{'reasons':['renal_risk']}]
        before=deepcopy(c)
        self.assertFalse(source_gate(c,{})['eligible_for_this_probe'])
        with self.assertRaises(ValueError):build_basic_episode(c,{})
        self.assertEqual(c,before)
        c=source();c['his_initial']['historical_orders'][0]['regimen_raw']['frequency']='BID PRN'
        self.assertFalse(source_gate(c,{})['eligible_for_this_probe'])


if __name__=='__main__':unittest.main()
