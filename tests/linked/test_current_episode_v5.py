from copy import deepcopy
import unittest
from test_current_episode_v4 import fixture
from sandbox.experiments.linked.case_preparation.current_episode_v4 import build_episode
from sandbox.experiments.linked.case_preparation.current_episode_v5 import build_context
from sandbox.experiments.linked.chart import HistoryChart
from sandbox.experiments.linked.chart_projection import project_history_page, verify_projection


def episode():
    c=fixture()
    c['his_initial']['historical_orders'][0]['drug_name_raw']='MetFORMIN XR (Glucophage XR)'
    c['his_initial']['historical_encounters'][0]['diagnoses']=[{
        'record_id':'dx_'+'5'*24,'display':'Type 2 diabetes mellitus',
        'coding':{'system':'ICD','version':'10','code':'E119'},'source_kind':'test_fixture'}]
    return build_episode(c)[0]


class ContextTests(unittest.TestCase):
    def test_prior_context_is_authored_and_preserves_original_safety_data(self):
        old=episode();before=deepcopy(old);new,audit=build_context(old)
        self.assertEqual(old,before)
        self.assertNotEqual(new['case_id'],old['case_id'])
        self.assertEqual(new['patient_key'],old['patient_key'])
        self.assertEqual(new['his_initial']['historical_orders'],old['his_initial']['historical_orders'])
        self.assertEqual(new['his_initial']['allergy_record'],old['his_initial']['allergy_record'])
        self.assertEqual(new['his_initial']['current_observations'],old['his_initial']['current_observations'])
        prior=new['his_initial']['current_use_record']['prior_visit']
        self.assertEqual(prior['source_kind'],'constructed')
        self.assertEqual(prior['current_prescribing_decision'],'not_preassigned')
        self.assertIsNone(new['evaluation']['prescription_labels'])
        self.assertFalse(audit['formal_eligible'])
        chart=HistoryChart(new['his_initial'])
        self.assertEqual(chart.initial_view()['current_use_record']['prior_visit'],prior)
        for section in ('current_use_record','current_use_items'):
            page=chart.query(section,limit=20)
            self.assertTrue(verify_projection(page,project_history_page(page)))

    def test_absent_diagnosis_or_ambiguous_core_plan_cannot_be_filled_from_cohort_label(self):
        c=episode();c['his_initial']['historical_encounters'][0]['diagnoses']=[]
        with self.assertRaises(ValueError):build_context(c)
        c=episode();c['his_initial']['current_use_record']['items']*=2
        with self.assertRaises(ValueError):build_context(c)

    def test_infection_not_covered_by_chronic_policy(self):
        c=episode();c['group']='pneumonia'
        with self.assertRaises(ValueError):build_context(c)


if __name__=='__main__':unittest.main()
