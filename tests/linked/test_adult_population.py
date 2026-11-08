from copy import deepcopy
import unittest

from test_current_episode_v6 import source
from sandbox.experiments.linked.case_preparation.current_episode_v6 import build_basic_episode
from sandbox.experiments.linked.case_preparation.adult_population import add_adult_population, POPULATION
from sandbox.experiments.linked.chart import HistoryChart
from sandbox.experiments.linked.safe_context_adapter import prepare_shared_rule_context
from test_safe_context_adapter import fixture
from test_clinical_faults import fixtures
from sandbox.experiments.linked.clinical_faults import evaluate_rule


class AdultPopulationTests(unittest.TestCase):
    def test_overlay_preserves_every_source_and_patient_field(self):
        case,_=build_basic_episode(source(),{});case['provenance']['source_dataset']='MIMIC-IV 2.2'
        original=deepcopy(case);updated=add_adult_population(case)
        self.assertEqual(case,original)
        for key in ('patient_key','patient_initial','evaluation','formulary','group','split'):
            self.assertEqual(case[key],updated[key])
        hospital=deepcopy(updated['his_initial']);population=hospital['demographics'].pop('age_population')
        self.assertEqual(hospital,case['his_initial'])
        self.assertFalse(population['exact_age_claim'])
        self.assertEqual(HistoryChart(updated['his_initial']).initial_view()['demographics']['age_population'],POPULATION)

    def test_wrong_dataset_backdating_or_underage_are_not_overridden(self):
        base,_=build_basic_episode(source(),{});base['provenance']['source_dataset']='MIMIC-IV 2.2'
        for mutate in (lambda c:c['provenance'].update(source_dataset='other'),
                       lambda c:c['his_initial']['time_basis'].update(new_visit_offset_hours=-1),
                       lambda c:c['his_initial']['demographics'].update(age=17)):
            case=deepcopy(base);mutate(case)
            with self.assertRaises(ValueError):add_adult_population(case)

    def test_category_support_does_not_create_exact_age_or_medication_reconciliation(self):
        context,_,formula,catalogue=fixture(age=44,status='approximate')
        context['hospital_history']['initial_view']['demographics']['age_population']=deepcopy(POPULATION)
        prepared=prepare_shared_rule_context(context,formula,catalogue)['shared_context']['documented_rule_context']
        self.assertIsNone(prepared['demographics']['age'])
        self.assertEqual(prepared['demographics']['reported_age'],44)
        self.assertEqual(prepared['demographics']['age_population']['status'],'documented_category')
        self.assertIsNone(prepared['medication_reconciliation']['all_current_products_mapped'])
        rule,rx,complete,_=fixtures()[0]
        complete['demographics']=prepared['demographics']
        self.assertEqual(evaluate_rule(rule,rx,complete)['status'],'scoped_nonviolation')
        complete['demographics']['age_population']['evidence_ref']=None
        self.assertEqual(evaluate_rule(rule,rx,complete)['status'],'indeterminate')

    def test_conflicting_adult_category_cannot_override_pediatric_or_unknown_evidence(self):
        rule,rx,complete,_=fixtures()[0]
        for age,status in [(17,'exact'),(17,'approximate')]:
            context,_,formula,catalogue=fixture(age,status)
            context['hospital_history']['initial_view']['demographics']['age_population']=deepcopy(POPULATION)
            complete['demographics']=prepare_shared_rule_context(context,formula,catalogue)['shared_context']['documented_rule_context']['demographics']
            self.assertEqual(evaluate_rule(rule,rx,complete)['status'],'indeterminate')
        complete['demographics']={'age':None,'reported_age':44,'age_status':'approximate'}
        self.assertEqual(evaluate_rule(rule,rx,complete)['status'],'indeterminate')


if __name__=='__main__':unittest.main()
