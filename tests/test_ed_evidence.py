from handoff.ed_metrics import prescribing_evidence
from handoff.emergency_results import compare, PRIMARY_SCOPE
from sandbox.experiments.linked.chart import HistoryChart
from sandbox.experiments.linked.chart_projection import project_history_page, verify_projection
from sandbox.experiments.linked.ed_order_actions import INDICATION_POLICY
from sandbox.experiments.linked.ed_treatment_review import validate_omitted_products
import pytest


def test_problem_list_and_items_can_be_queried_and_projected():
    chart = HistoryChart({'problem_list': {'status': 'recorded', 'items': [{'display': 'Fixture problem'}]}})
    for name in ('problem_list', 'problem_items'):
        page = chart.query(name)
        assert page['returned_count'] == 1
        assert verify_projection(page, project_history_page(page))


def test_reports_distinguish_a_returned_record_from_actual_model_delivery():
    run = {'artifacts': {'physician_opening_ips': {'fixture': True}}, 'events': [
        {'sequence': 1, 'event': 'history_lookup', 'request': {'term': 'fixture'},
         'section': 'problem_list', 'returned_refs': ['chart_sent', 'chart_unsent'],
         'result_status': 'visible', 'accepted_into_context': True},
        {'sequence': 2, 'event': 'role_call', 'call_sequence': 1},
    ], 'calls': [{'sequence': 1, 'role': 'doctor', 'observation': {
        'clinical_context': {'history': {'queried_pages': [{'page': {'records': [
            {'record_ref': 'chart_sent', 'record': {'display': 'Fixture problem'}},
        ]}}]}},
        'formulary': [{'drug_code': 'MIM-FIXTURE'}],
    }, 'response': {}}]}
    evidence = prescribing_evidence(run, 1)
    assert evidence['delivered_rows'] == 1
    assert evidence['read_rows'] == 0
    assert evidence['chart_searches'][0]['delivered_refs'] == ['chart_sent']
    assert evidence['chart_searches'][0]['returned_without_delivery'] == ['chart_unsent']
    assert evidence['visible_product_codes'] == ['MIM-FIXTURE']


def test_only_product_specific_signed_decisions_count_as_named_omissions():
    directory = [{'name': 'beta', 'drug_code': 'MIM-FIXTURE'}]
    reference = {'scope': PRIMARY_SCOPE, 'continued': [], 'added': ['beta'], 'disposition': 'home',
                 'order_timeline': [{'offset_seconds': 1, 'active': ['beta'], 'not_assessable': []}]}
    state = {'offset_seconds': 1, 'primary_drug_codes': [], 'other_drug_codes': [], 'transactions': [],
             'physician_call': 1, 'reason': 'Withhold alpha and start beta.',
             'treatment_review': {'therapy': {'decision': 'omit', 'reason': 'Withhold alpha and start beta.'}}}
    evidence = {'disposition': 'home', 'ed_order_indication_policy': INDICATION_POLICY, 'signed_order_states': [state]}

    def check():
        return compare(evidence, reference, directory)['signed_decisions'][0]['omission_review']['beta']

    assert check()['status'] == 'no_ingredient_reason_recorded'
    state['considered_not_ordered'] = [{'drug_code': 'MIM-FIXTURE', 'reason': 'Fixture reason', 'check_ids': []}]
    assert check()['named_decisions'] == state['considered_not_ordered']
    with pytest.raises(ValueError):
        validate_omitted_products(state['considered_not_ordered'], visible_codes={'MIM-FIXTURE'},
                                  active_codes={'MIM-FIXTURE'})
