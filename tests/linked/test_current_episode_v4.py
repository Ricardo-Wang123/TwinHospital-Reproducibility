from copy import deepcopy
import unittest

from sandbox.experiments.linked.cases import build_synthetic_case, runtime_visible_case
from sandbox.experiments.linked.chart import HistoryChart
from sandbox.experiments.linked.chart_projection import project_history_page, verify_projection, expand_projected_record
from sandbox.experiments.linked.case_preparation.current_episode_v4 import (
    build_episode, candidate_plan, validate_episode, VISIT_HOURS, frequency_spoken, historical_patient_memory,
)
from sandbox.experiments.linked.case_preparation.v4_source_qualification import product_form_gate, qualify


def fixture():
    # Entirely invented local fixture. No private record is read by tests.
    c = build_synthetic_case("type2_diabetes", 0)
    c["data_kind"] = "mimic_derived"
    c["provenance"] = {"source_dataset": "test_fixture_not_real_data", "uses_patient_records": False}
    o = {"record_id": "rx_"+"1"*24, "source_encounter_ref": "enc_"+"2"*24,
        "drug_name_raw": "fictional alpha tablet", "prescription_component_type_raw": "MAIN",
        "regimen_raw": {"prod_strength": "5 mg Tablet", "dose_val_rx": "5", "dose_unit_rx": "mg",
            "doses_per_24_hrs": "2", "route": "PO", "form_rx": None, "frequency": "BID"},
        "time": {"event_offset_hours": -48}, "planned_stop_offset_hours": 1}
    c["his_initial"]["historical_orders"] = [o]
    c["his_initial"]["allergy_record"] = {"status": "unknown", "items": [], "source_refs": []}
    c["his_initial"]["current_use_record"] = {"status": "not_reconciled", "items": []}
    c["provenance"]["selected_historical_order"] = deepcopy(o)
    c["evaluation"]["source_risk_cues"] = []
    return c


class CurrentEpisodeV4Tests(unittest.TestCase):
    def test_current_episode_does_not_change_history_source_unknowns_or_split(self):
        source = fixture(); before = deepcopy(source)
        c, audit = build_episode(source)
        self.assertEqual(source, before)
        self.assertTrue(audit["source_history_unchanged"])
        self.assertEqual(c["his_initial"]["allergy_record"]["status"], "unknown")
        self.assertEqual(c["his_initial"]["allergy_record"]["patient_report"]["status"], "reported_none_known")
        self.assertEqual(c["provenance"]["source_current_use_record"], before["his_initial"]["current_use_record"])
        self.assertEqual(c["patient_key"], before["patient_key"])
        self.assertEqual(c["split"], before["split"])
        self.assertEqual(c["evaluation"]["required_facts"], [])
        self.assertIsNone(c["evaluation"]["prescription_labels"])
        self.assertNotIn("evaluation", runtime_visible_case(c))
        initial=HistoryChart(c['his_initial']).initial_view()
        self.assertEqual(initial['allergy_record']['patient_report'],c['his_initial']['allergy_record']['patient_report'])
        self.assertEqual(initial['allergy_record']['status'],'unknown')

    def test_unknown_prn_or_liquid_identity_is_not_assumed_scheduled(self):
        source = fixture(); o = source["his_initial"]["historical_orders"][0]
        o["regimen_raw"]["doses_per_24_hrs"] = None
        entries, _ = candidate_plan(source)
        self.assertTrue(entries[0]["reasons"])
        c, audit = build_episode(source)
        self.assertEqual(audit["scheduled_plan_count"], 0)
        self.assertNotIn("actual_use", c["his_initial"]["current_use_record"]["items"][0])
        o["regimen_raw"].update(doses_per_24_hrs="2", prod_strength="2.5mL Vial")
        entries, _ = candidate_plan(source)
        self.assertIn("source_liquid_concentration_missing", entries[0]["reasons"])

    def test_conflicting_regimens_remain_unresolved(self):
        source = fixture(); second = deepcopy(source["his_initial"]["historical_orders"][0])
        second["record_id"] = "rx_"+"3"*24
        second["regimen_raw"]["dose_val_rx"] = "10"
        source["his_initial"]["historical_orders"].append(second)
        entries, _ = candidate_plan(source)
        self.assertEqual(len(entries), 1)
        self.assertIn("competing_near_cutoff_regimens_require_reconciliation", entries[0]["reasons"])

    def test_prn_and_interval_conflict_are_not_regular_use(self):
        source = fixture(); r=source["his_initial"]["historical_orders"][0]["regimen_raw"]
        for frequency,reason in (("BID PRN","explicit_prn_frequency"),("q6h","explicit_frequency_conflicts_with_daily_count"),("ONCE","frequency_interval_or_one_time_not_supported"),(None,"full_frequency_missing")):
            r["frequency"]=frequency
            self.assertIn(reason,candidate_plan(source)[0][0]["reasons"])

    def test_pharmacy_frequency_is_a_separate_annotation(self):
        source=fixture();o=source["his_initial"]["historical_orders"][0];o["regimen_raw"]["frequency"]=None
        context={o["record_id"]:{"frequency":"BID","join_status":"unique_agreement","source_kind":"observed_source"}}
        c,a=build_episode(source,context)
        self.assertEqual(a["scheduled_plan_count"],1)
        self.assertIsNone(c["his_initial"]["historical_orders"][0]["regimen_raw"]["frequency"])
        self.assertEqual(c["his_initial"]["current_use_record"]["items"][0]["frequency"],"BID")

    def test_fixed_interval_spoken_wording_preserves_interval(self):
        self.assertEqual(frequency_spoken("Q12H",2),"每12小时一次")
        self.assertEqual(frequency_spoken("BID",2),"一天2次")

    def test_common_history_memory_is_based_on_codes_not_new_diagnosis(self):
        source=fixture();source["his_initial"]["historical_encounters"][0]["diagnoses"]=[
            {"record_id":"dx_"+"a"*24,"coding":{"version":"10","code":"E11.9"}},
            {"record_id":"dx_"+"b"*24,"coding":{"version":"9","code":"250.01"}}]
        memory=historical_patient_memory(source)
        self.assertEqual(len(memory),1)
        self.assertEqual(memory[0]["source_refs"],["dx_"+"a"*24])
        c,_=build_episode(source)
        self.assertTrue(any(f["knowledge_state"]=="constructed_memory_based_on_historical_code" for f in c["patient_initial"]["patient_facts"]))
        self.assertEqual(c["his_initial"]["historical_encounters"],source["his_initial"]["historical_encounters"])

    def test_fact_episode_window_does_not_expand_last24h_negatives(self):
        c,_=build_episode(fixture())
        fact=next(f for f in c["patient_initial"]["patient_facts"] if f["topic"]=="recent_adverse_experience")
        self.assertIn("last24h",fact["narrative_time_scope"])
        self.assertIn("not fact validity",fact["episode_window"]["meaning"])
        self.assertNotIn("time_interval",fact)

    def test_source_family_gate_is_not_clinical_approval_and_package_weight_is_not_dose(self):
        source=fixture();o=source["his_initial"]["historical_orders"][0];o["drug_name_raw"]="Metformin"
        self.assertTrue(qualify(source,{})["source_material_gate"])
        self.assertEqual(qualify(source,{})["clinical_qualification"],"pending")
        entry=candidate_plan(source)[0][0];entry["source_order"]["regimen_raw"].update(route="IH",prod_strength="12g Inhaler")
        self.assertFalse(product_form_gate(entry)[0])
        entry["source_order"]["regimen_raw"]["prod_strength"]="250/50mcg Diskus"
        self.assertTrue(product_form_gate(entry)[0])

    def test_old_stop_date_is_only_a_candidate_window_not_current_stop(self):
        source = fixture(); source["his_initial"]["historical_orders"][0]["planned_stop_offset_hours"] = -25
        c, audit = build_episode(source)
        self.assertEqual(audit["candidate_count"], 0)
        self.assertEqual(c["his_initial"]["historical_orders"], source["his_initial"]["historical_orders"])
        self.assertFalse(any(i.get("current_use_status") == "stopped" for i in c["his_initial"]["current_use_record"]["items"]))

    def test_known_source_allergy_is_not_negated(self):
        source = fixture(); source["his_initial"]["allergy_record"] = {
            "status": "known", "items": [{"name": "fictional beta", "reaction": "rash", "source_kind": "observed_source"}]}
        c, _ = build_episode(source)
        self.assertEqual(c["his_initial"]["allergy_record"]["status"], "known")
        self.assertEqual(c["his_initial"]["allergy_record"]["items"], source["his_initial"]["allergy_record"]["items"])
        self.assertEqual(c["his_initial"]["allergy_record"]["patient_report"]["status"], "unable_to_ascertain")

    def test_plan_actual_use_and_inventory_are_coherent(self):
        source = fixture(); c, _ = build_episode(source)
        i = c["his_initial"]["current_use_record"]["items"][0]
        self.assertEqual(i["actual_use"]["reported_taken_doses"], 12)
        self.assertEqual(i["supply"]["remaining_dose_equivalents"], 2)
        self.assertEqual(i["time"]["event_offset_hours"], VISIT_HOURS)
        for o in c["his_initial"]["current_observations"]:
            self.assertEqual(o["time"]["event_offset_hours"], VISIT_HOURS)
        i["supply"]["remaining_dose_equivalents"] = 200
        with self.assertRaisesRegex(ValueError, "Supply"):
            validate_episode(source, c)

    def test_current_plan_fields_survive_chart_and_compact_projection(self):
        source = fixture(); c, _ = build_episode(source); chart = HistoryChart(c["his_initial"])
        expected = c["his_initial"]["current_use_record"]["items"][0]
        raw = chart.query("current_use_items"); projected = project_history_page(raw)
        verify_projection(raw, projected)
        restored = expand_projected_record(projected, projected["records"][0])
        for k in ("plan", "actual_use", "supply", "time", "product_identity_status", "current_use_status", "historical_order_stop_offset_hours"):
            self.assertEqual(restored[k], expected[k])
        index = chart.initial_view()["current_use_record"]
        self.assertEqual(index["query_section"], "current_use_items")
        self.assertEqual(index["current_plan_item_count"], 1)
        self.assertEqual(index["as_of_offset_hours"], VISIT_HOURS)
        self.assertNotIn("clinical_review_status", index)
        allergy = chart.query("allergy_record")["records"][0]["record"]
        self.assertEqual(allergy["patient_report"], c["his_initial"]["allergy_record"]["patient_report"])

    def test_historical_or_vital_mutation_is_rejected(self):
        source = fixture(); c, _ = build_episode(source)
        c["his_initial"]["historical_orders"][0]["drug_name_raw"] = "changed"
        with self.assertRaisesRegex(ValueError, "Historical"):
            validate_episode(source, c)
        c, _ = build_episode(source)
        c["his_initial"]["current_observations"][0]["value"] = "normal"
        with self.assertRaisesRegex(ValueError, "Vital"):
            validate_episode(source, c)
        c,_=build_episode(source);c["his_initial"]["current_observations"][0]["time"]["event_offset_hours"]=24
        with self.assertRaisesRegex(ValueError,"observation time"):
            validate_episode(source,c)

    def test_balanced_inventory_cannot_hide_wrong_actual_doses(self):
        source=fixture();c,_=build_episode(source);i=c["his_initial"]["current_use_record"]["items"][0]
        i["actual_use"]["reported_taken_doses"]=1;i["supply"]["used_dose_equivalents"]=1
        i["supply"]["remaining_dose_equivalents"]=i["supply"]["initial_dose_equivalents"]-1
        with self.assertRaisesRegex(ValueError,"Planned, taken"):
            validate_episode(source,c)

    def test_infection_is_not_silently_filled_by_chronic_policy(self):
        source = fixture(); source["group"] = "pneumonia"
        with self.assertRaisesRegex(ValueError, "infection"):
            build_episode(source)


if __name__ == "__main__": unittest.main()
