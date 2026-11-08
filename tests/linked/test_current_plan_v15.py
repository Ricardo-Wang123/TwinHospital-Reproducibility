"""Input authoring cannot imply duplicate use or delete conflicting history."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from test_current_episode_v4 import fixture
from sandbox.experiments.linked.case_preparation.current_episode_v4 import build_episode
from sandbox.experiments.linked.case_preparation.current_plan_v15 import build, load_private_order_links


def case_pair(*, modified=False, sequential=False):
    source = fixture()
    first = source['his_initial']['historical_orders'][0]
    first['source_ref'] = 'src_' + 'a'*24
    first['drug_name_raw'] = 'Fictional Alpha XL' if modified else 'Fictional Alpha'
    first['regimen_raw']['prod_strength'] = '5 mg XL Tab' if modified else '5 mg Tablet'
    second = deepcopy(first)
    second['record_id'] = 'rx_' + '3'*24
    second['source_ref'] = 'src_' + 'b'*24
    second['regimen_raw']['prod_strength'] = '10 mg XL Tab' if modified else '5 mg UD Cup'
    if sequential:
        first['time']['event_offset_hours'] = -36
        first['planned_stop_offset_hours'] = -6
        second['time']['event_offset_hours'] = -4
        second['planned_stop_offset_hours'] = 3
        second['regimen_raw']['dose_val_rx'] = '10'
    source['his_initial']['historical_orders'].append(second)
    source['provenance']['selected_historical_order'] = deepcopy(second if sequential else first)
    case, _ = build_episode(source)
    case['provenance']['core_source_ref'] = (second if sequential else first)['record_id']
    token = 'a'*64
    links = {first['record_id']: {'logical_order_token': token},
             second['record_id']: {'logical_order_token': 'b'*64 if sequential else token}}
    return case, links


class PlanChoiceTests(unittest.TestCase):
    def test_one_order_keeps_one_pattern_and_updates_memory_without_touching_history(self):
        source, links = case_pair()
        old = deepcopy(source)
        case, audit = build(source, links)
        self.assertEqual(source, old)
        self.assertEqual(audit['nonadopted_pattern_count'], 1)
        self.assertEqual(case['his_initial']['historical_orders'], old['his_initial']['historical_orders'])
        self.assertEqual(case['his_initial']['current_observations'], old['his_initial']['current_observations'])
        self.assertEqual(case['his_initial']['allergy_record'], old['his_initial']['allergy_record'])
        items = case['his_initial']['current_use_record']['items']
        active = [i for i in items if i.get('plan')]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]['source_ref'], case['provenance']['core_source_ref'])
        self.assertEqual(active[0]['actual_use']['reported_taken_doses'], 12)
        inactive = next(i for i in items if not i.get('plan'))
        self.assertNotIn('actual_use', inactive)
        self.assertNotIn('supply', inactive)
        self.assertEqual(case['evaluation']['reference_facts'], case['patient_initial']['patient_facts'])
        claims = [f for f in case['patient_initial']['patient_facts'] if
                  inactive['record_id'] in f.get('source_refs', [])]
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]['topic'], 'nonadopted_medication_pattern')
        self.assertEqual(case['patient_key'], old['patient_key'])
        self.assertEqual(case['split'], old['split'])
        self.assertIsNone(case['evaluation']['prescription_labels'])

    def test_distinct_concurrent_orders_and_missing_link_are_not_collapsed(self):
        for missing in (False, True):
            with self.subTest(missing=missing):
                source, links = case_pair()
                links['rx_'+'3'*24]['logical_order_token'] = None if missing else 'b'*64
                case, audit = build(source, links)
                self.assertEqual(audit['nonadopted_pattern_count'], 0)
                self.assertEqual(case['patient_initial'], source['patient_initial'])
                self.assertEqual(case['his_initial']['current_use_record'], source['his_initial']['current_use_record'])
                self.assertEqual(len(audit['unresolved_groups']), 1)

    def test_same_order_but_disagreeing_dose_is_not_assumed_equivalent(self):
        source, links = case_pair()
        source['his_initial']['current_use_record']['items'][1]['dose'] = '10'
        _, audit = build(source, links)
        self.assertEqual(audit['nonadopted_pattern_count'], 0)

    def test_separate_nonoverlapping_modified_release_patterns_choose_latest(self):
        source, links = case_pair(modified=True, sequential=True)
        case, audit = build(source, links)
        self.assertEqual(audit['nonadopted_pattern_count'], 1)
        active = [i for i in case['his_initial']['current_use_record']['items'] if i.get('plan')]
        self.assertEqual(active[0]['dose'], '10')
        self.assertEqual(active[0]['source_ref'], 'rx_'+'3'*24)
        self.assertEqual(case['his_initial']['historical_orders'], source['his_initial']['historical_orders'])

    def test_overlap_release_unknown_or_dropped_core_remain_unresolved(self):
        for problem in ('overlap', 'unknown_release', 'old_core'):
            with self.subTest(problem=problem):
                source, links = case_pair(modified=True, sequential=True)
                if problem == 'overlap':
                    source['his_initial']['historical_orders'][0]['planned_stop_offset_hours'] = -3
                elif problem == 'unknown_release':
                    item = source['his_initial']['current_use_record']['items'][0]
                    item['name'], item['strength'] = 'Fictional Alpha XL', '5 mg tablet'
                    # The name carries XL; remove the explicit name marker from both.
                    for item in source['his_initial']['current_use_record']['items']:
                        item['name'] = 'Fictional Alpha'
                        item['strength'] = item['strength'].replace('XL ', '')
                else:
                    source['provenance']['core_source_ref'] = 'rx_'+'1'*24
                _, audit = build(source, links)
                self.assertEqual(audit['nonadopted_pattern_count'], 0)

    def test_unknown_memory_dependency_fails_instead_of_leaving_contradictory_statement(self):
        source, links = case_pair()
        item = next(i for i in source['his_initial']['current_use_record']['items'] if i['source_ref'] == 'rx_'+'3'*24)
        source['patient_initial']['patient_facts'].append({
            'topic': 'unexpected_dependency', 'source_refs': [item['record_id']], 'value': 'ambiguous statement',
            'status': 'reported', 'source_kind': 'constructed'})
        with self.assertRaisesRegex(ValueError, 'memory dependency'):
            build(source, links)

    def test_existing_low_supply_reason_is_relinked_only_if_still_supported(self):
        source, links = case_pair()
        reason = source['patient_initial']['patient_facts'][0]
        reason['construction_basis'] = 'salience_of_existing_declared_supply_not_new_clinical_information'
        reason['source_refs'] = [i['record_id'] for i in source['his_initial']['current_use_record']['items']]
        original_text = reason['value']
        case, _ = build(source, links)
        new_reason = case['patient_initial']['patient_facts'][0]
        self.assertEqual(new_reason['value'], original_text)
        self.assertEqual(len(new_reason['source_refs']), 1)
        for item in source['his_initial']['current_use_record']['items']:
            item['supply']['remaining_dose_equivalents'] = 1000
        with self.assertRaisesRegex(ValueError, 'Remaining supply'):
            build(source, links)

    def test_private_link_uses_poe_order_not_pharmacy_product_and_exposes_only_hashes(self):
        case, _ = case_pair()
        case['provenance']['source_case_id'] = 'invented_fixture'
        rows = {}
        for n, order in enumerate(case['his_initial']['historical_orders']):
            rows[order['source_ref']] = {'row': {**order['regimen_raw'],
                'drug': order['drug_name_raw'], 'subject_id': 'fixture_subject',
                'hadm_id': 'fixture_admission', 'poe_id': 'fixture_order',
                'pharmacy_id': 'fixture_pharmacy_' + str(n)}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'invented_fixture'
            path.mkdir()
            (path/'provenance.json').write_text(json.dumps({'source_rows': rows}))
            links, hashes = load_private_order_links(case, tmp, source_package_root=tmp)
            self.assertEqual(len({x['logical_order_token'] for x in links.values()}), 1)
            self.assertNotIn('fixture_subject', json.dumps(links))
            self.assertNotIn('fixture_order', json.dumps(links))
            self.assertEqual(len(hashes), 1)
            rows['src_'+'b'*24]['row']['dose_val_rx'] = '999'
            (path/'provenance.json').write_text(json.dumps({'source_rows': rows}))
            with self.assertRaisesRegex(ValueError, 'historical pattern disagree'):
                load_private_order_links(case, tmp, source_package_root=tmp)


if __name__ == '__main__':
    unittest.main()
