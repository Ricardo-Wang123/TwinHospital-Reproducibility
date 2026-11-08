from copy import deepcopy
import json

import unittest

from sandbox.experiments.linked.cases import build_synthetic_case
from sandbox.experiments.linked.chart import HistoryChart, INITIAL_BYTE_LIMIT, PAGE_BYTE_LIMIT, json_bytes


def enc(n):
    return "enc_" + f"{n:024x}"


def rx(n):
    return "rx_" + f"{n:024x}"


def history():
    return {"demographics": {"age": None, "age_status": "90_plus", "sex": "F"},
        "time_basis": {"history_offset_origin": "source_anchor_discharge", "new_visit_offset_hours": 24},
        "historical_encounters": [{"encounter_ref": enc(1), "discharge_offset_hours": 0,
            "diagnoses": [{"display": "fictional diagnosis"}], "order_refs": [rx(i) for i in range(45)], "observation_refs": []}],
        "historical_orders": [{"record_id": rx(i), "source_encounter_ref": enc(1),
            "drug_name_raw": "medicine alpha" if i % 2 else "medicine beta",
            "time": {"event_offset_hours": -i}, "evidence_type": "historical_order"} for i in range(45)],
        "historical_observations": [], "allergy_record": {"status": "unknown", "items": []},
        "current_use_record": {"status": "not_reconciled", "items": []}, "current_observations": []}


class HistoryChartTests(unittest.TestCase):
    def test_last_dose_timing_is_bound_to_report_window_and_preserves_unknown(self):
        raw = history()
        use = {'window_start_offset_hours': 0, 'window_end_offset_hours': 24,
               'reported_taken_doses': 2, 'last_taken_offset_hours': 18,
               'source_kind': 'constructed'}
        raw['current_use_record'] = {'as_of_offset_hours': 24, 'items': [
            {'record_id': rx(1), 'name': 'reported', 'actual_use': use},
            {'record_id': rx(2), 'name': 'missing'},
            {'record_id': rx(3), 'name': 'future',
             'actual_use': {**use, 'last_taken_offset_hours': 25}},
            {'record_id': rx(4), 'name': 'stale',
             'actual_use': {**use, 'window_end_offset_hours': 23}},
        ]}
        original = deepcopy(raw)
        chart = HistoryChart(raw)
        summary = chart.initial_view()['recent_reported_dose_timing']
        self.assertEqual(summary['items_in_report'], 4)
        self.assertEqual(summary['eligible_timing_count'], 1)
        self.assertEqual([(r['name'], r['hours_before_visit']) for r in summary['records']],
                         [('reported', 6)])
        item = chart.query('current_use_items')['records'][0]
        self.assertEqual(summary['records'][0]['record_ref'], item['record_ref'])
        self.assertEqual(item['record']['actual_use'], use)
        self.assertTrue(summary['unshown_or_unknown_is_not_no_use'])
        self.assertEqual(raw, original)

    def test_initial_labs_are_literal_latest_two_not_normal_value_selection(self):
        raw = history()
        raw['historical_observations'] = [
            {'record_id':'obs_'+f'{i:024x}', 'display':'Creatinine', 'fluid':'Blood',
             'value_raw':str(value), 'numeric_value_raw':str(value), 'unit_raw':'mg/dL',
             'flag_raw':'abnormal' if value>2 else None, 'source_kind':'observed_source',
             'time':{'event_offset_hours':-i, 'recorded_offset_hours':-i,
                     'available_by_source_cutoff':True}}
            for i,value in [(1,5.2),(2,4.8),(3,0.8)]]
        before=deepcopy(raw); chart=HistoryChart(raw); view=chart.initial_view()
        overview=view['historical_laboratory_overview']
        self.assertEqual([r['record']['value_raw'] for r in overview['records']], ['5.2','4.8'])
        self.assertEqual(overview['eligible_record_count'],3)
        self.assertEqual(view['sections']['historical_observations']['unexpanded_count'],1)
        self.assertEqual([r['record'] for r in overview['records']],raw['historical_observations'][:2])
        self.assertEqual(raw,before)

    def test_initial_labs_withhold_future_unknown_time_and_nonclinical_fields(self):
        raw=history(); raw['historical_observations']=[]
        for i in range(5):
            row={'record_id':'obs_'+f'{i:024x}','display':'Glucose','fluid':'Blood',
                 'value_raw':'___','numeric_value_raw':'270','unit_raw':'mg/dL',
                 'evaluation':'SECRET','subject_id':'PRIVATE',
                 'time':{'event_offset_hours':-1,'recorded_offset_hours':0,'available_by_source_cutoff':True}}
            if i==1:row['time']['available_by_source_cutoff']=False
            if i==2:row['time']['recorded_offset_hours']=30
            if i==3:row['time'].pop('event_offset_hours')
            if i==4:row['time'].pop('available_by_source_cutoff')
            raw['historical_observations'].append(row)
        view=HistoryChart(raw).initial_view(); rows=view['historical_laboratory_overview']['records']
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['record']['numeric_value_raw'],'270')
        self.assertEqual(rows[0]['record']['value_raw'],'___')
        self.assertNotIn('SECRET',json.dumps(view));self.assertNotIn('PRIVATE',json.dumps(view))

    def test_initial_lab_byte_budget_defers_whole_records(self):
        raw=history();raw['historical_observations']=[{'record_id':'obs_'+f'{1:024x}',
            'display':'Creatinine','value_raw':'not a negative '+('x'*40000),
            'time':{'event_offset_hours':-1,'recorded_offset_hours':0,'available_by_source_cutoff':True}}]
        view=HistoryChart(raw).initial_view();o=view['historical_laboratory_overview']
        self.assertEqual(o['records'],[]);self.assertEqual(o['omitted_for_byte_limit'],1)
        self.assertLessEqual(json_bytes(view),INITIAL_BYTE_LIMIT)

    def test_latest_first_ties_stable_and_missing_times_last(self):
        raw = history()
        for key in ("historical_orders", "historical_observations"):
            raw[key] = [{"record_id": rx(1), "time": {"event_offset_hours": -100}},
                        {"record_id": rx(2)},
                        {"record_id": rx(3), "time": {"event_offset_hours": -1}},
                        {"record_id": rx(4), "time": {"event_offset_hours": -1}}]
        original = deepcopy(raw)
        chart = HistoryChart(raw)
        for key in ("historical_orders", "historical_observations"):
            page = chart.query(key)
            self.assertEqual([x["record"]["record_id"] for x in page["records"]], [rx(3), rx(4), rx(1), rx(2)])
            self.assertEqual(page["selection"]["time_order"], "latest_first")
            self.assertEqual(page["selection"]["equal_time_order"], "original_source_order")
            self.assertIn("after_all_known", page["selection"]["missing_time_order"])
        self.assertEqual(raw, original)

    def test_term_search_excludes_metadata_keys_and_source_refs(self):
        raw = history()
        raw["historical_orders"][0]["time"]["availability_basis"] = "source_discharge"
        raw["historical_orders"][1]["time"]["basis"] = "source_discharge"
        chart = HistoryChart(raw)
        for term in ("discharge", "drug_name_raw", "source_encounter_ref", enc(1)):
            with self.subTest(term=term):
                self.assertEqual(chart.query("historical_orders", term=term)["matching_count"], 0)
        page = chart.query("historical_orders", term="medicine alpha")
        self.assertEqual(page["matching_count"], 22)
        self.assertIn("drug_name_raw", page["selection"]["search_fields"])
        self.assertNotIn("time.availability_basis", page["selection"]["search_fields"])
        self.assertIn("clinical_values_only", page["selection"]["matching"])

    def test_clinical_codes_labels_and_real_values_still_match(self):
        raw = history()
        raw["historical_encounters"][0]["diagnoses"][0]["coding"] = {"system": "ICD", "version": "10", "code": "J441"}
        raw["historical_observations"] = [{"display": "Creatinine", "value_raw": "0.9", "unit_raw": "mg/dL",
            "time": {"availability_basis": "source_discharge"}}]
        raw["current_observations"] = [{"name": "temperature", "value": 36.7, "unit": "C", "source_kind": "constructed"}]
        # A real clinical value containing the word must remain searchable.
        raw["current_use_record"]["items"] = [{"name": "discharge medicine list supplied by patient"}]
        chart = HistoryChart(raw)
        self.assertEqual(chart.query("diagnoses", term="J441")["matching_count"], 1)
        self.assertEqual(chart.query("historical_encounters", term="J441")["matching_count"], 1)
        self.assertEqual(chart.query("historical_observations", term="CREATININE")["matching_count"], 1)
        self.assertEqual(chart.query("historical_observations", term="discharge")["matching_count"], 0)
        self.assertEqual(chart.query("current_observations", term="temperature")["matching_count"], 1)
        self.assertEqual(chart.query("current_observations", term="36.7")["matching_count"], 1)
        self.assertEqual(chart.query("current_use_record", term="discharge")["matching_count"], 1)
        initial = chart.initial_view()
        self.assertEqual(initial["sections"]["current_observations"]["total_count"], 1)
        self.assertFalse(initial["unavailable_document_views"]["discharge_summaries"]["queryable"])
        self.assertEqual(initial["unavailable_document_views"]["free_text_notes"]["presence_in_original_patient_record"], "unknown_not_inferred")

    def test_pagination_is_complete_stable_and_preserves_source(self):
        raw = history()
        original = deepcopy(raw)
        chart = HistoryChart(raw)
        cursor, rows = 0, []
        while cursor is not None:
            page = chart.query("historical_orders", offset=cursor, limit=20)
            assert json_bytes(page) <= PAGE_BYTE_LIMIT
            assert page["matching_count"] == 45
            rows += page["records"]
            cursor = page["next_offset"]
        assert [x["record"] for x in rows] == raw["historical_orders"]
        assert len({x["record_ref"] for x in rows}) == 45
        assert HistoryChart(raw).query("historical_orders") == chart.query("historical_orders")
        assert raw == original
        view = chart.initial_view()
        assert json_bytes(view) <= INITIAL_BYTE_LIMIT
        assert view["sections"]["historical_orders"]["unexpanded_count"] == 45
        assert view["allergy_record"]["status"] == "unknown"
        assert view["time_basis"]["new_visit_offset_hours"] == 24
        assert view["demographics"]["age"] is None


    def test_filters_count_matches_and_do_not_treat_unshown_as_absent(self):
        chart = HistoryChart(history())
        page = chart.query("historical_orders", encounter_key=enc(1), term="ALPHA", offset=20)
        assert page["total_count"] == 45
        assert page["matching_count"] == 22
        assert page["returned_count"] == 2
        assert page["next_offset"] is None
        empty = chart.query("historical_orders", encounter_key=enc(2))
        assert empty["matching_count"] == 0
        assert "not negative" in empty["missingness_rule"]
        with self.assertRaises(ValueError):
            chart.query("historical_orders", limit=21)
        with self.assertRaises(ValueError):
            chart.query("historical_orders", offset=True)
        with self.assertRaises(ValueError):
            chart.query("provenance")
        with self.assertRaises(ValueError):
            chart.query("historical_orders", encounter_key="12345678")


    def test_whitelist_excludes_evaluation_and_raw_identity_at_every_object(self):
        raw = history()
        raw["evaluation"] = {"answer": "SECRET_LABEL"}
        raw["provenance"] = {"subject_id": "SECRET_ID"}
        raw["demographics"]["subject_id"] = "SECRET_ID"
        raw["historical_orders"][0]["hadm_id"] = "SECRET_ID"
        raw["historical_orders"][0]["time"]["evaluation"] = "SECRET_LABEL"
        chart = HistoryChart(raw)
        serialized = json.dumps([chart.initial_view(), chart.query("historical_orders")])
        assert "SECRET_ID" not in serialized
        assert "SECRET_LABEL" not in serialized
        assert chart.initial_view()["excluded_non_whitelisted_field_count"] == 5
        raw["historical_orders"][0]["record_id"] = "12345678"
        with self.assertRaisesRegex(ValueError, "opaque"):
            HistoryChart(raw)


    def test_big_encounter_is_explicitly_deferred_to_component_sections(self):
        raw = history()
        raw["historical_encounters"][0]["observation_refs"] = ["obs_" + f"{i:024x}" for i in range(2000)]
        chart = HistoryChart(raw)
        page = chart.query("historical_encounters")
        assert page["records"] == []
        assert page["requires_expansion"][0]["reason"] == "single_record_exceeds_page_budget"
        assert "encounter_observation_refs" in page["requires_expansion"][0]["suggested_sections"]
        assert page["next_offset"] is None
        refs = chart.query("encounter_observation_refs", encounter_key=enc(1))
        assert refs["matching_count"] == 2000
        assert refs["next_offset"] == 20
        assert json_bytes(chart.initial_view()) <= INITIAL_BYTE_LIMIT
        assert len(raw["historical_encounters"][0]["observation_refs"]) == 2000


    def test_single_large_scalar_never_gets_sliced(self):
        raw = history()
        text = "药物记录" * 12_000
        raw["historical_orders"][0]["drug_name_raw"] = text
        chart = HistoryChart(raw)
        page = chart.query("historical_orders", limit=1)
        assert not page["records"]
        assert page["requires_expansion"][0]["suggested_sections"] == []
        assert page["next_offset"] == 1
        assert json_bytes(page) <= PAGE_BYTE_LIMIT
        assert raw["historical_orders"][0]["drug_name_raw"] == text


    def test_frozen_synthetic_schema_is_supported_without_modification(self):
        for group in ("type2_diabetes", "heart_failure", "copd", "pneumonia", "uti"):
            with self.subTest(group=group):
                raw = build_synthetic_case(group, 0)["his_initial"]
                chart = HistoryChart(raw)
                assert chart.initial_view()["excluded_non_whitelisted_field_count"] == 0
                assert json_bytes(chart.initial_view()) <= INITIAL_BYTE_LIMIT
                assert chart.query("current_observations")["matching_count"] > 0


if __name__ == "__main__":
    unittest.main()
