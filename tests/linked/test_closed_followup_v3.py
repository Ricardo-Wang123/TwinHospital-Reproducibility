from copy import deepcopy
import unittest

from sandbox.experiments.linked.case_preparation.closed_followup_v3 import facts_for, medication_candidates, source_risk_cues


def order(name="MetFORMIN", route="PO", time=-24, ref="rx_fixture", daily="2"):
    return {"record_id":ref,"source_encounter_ref":"anchor","prescription_component_type_raw":"MAIN",
            "drug_name_raw":name,"regimen_raw":{"route":route,"prod_strength":"500mg Tablet","dose_val_rx":"500",
            "dose_unit_rx":"mg","doses_per_24_hrs":daily},"time":{"event_offset_hours":time}}


class ClosedSceneTests(unittest.TestCase):
    def package(self, orders):
        return {"case":{"source_anchor_encounter_ref":"anchor","clinical":{"medications":{"orders":orders},"history":{"encounters":[]}}}}

    def test_oral_vancomycin_not_pneumonia_link(self):
        p=self.package([order("Vancomycin Oral Liquid"),order("Doxycycline Hyclate",ref="rx_second")]); original=deepcopy(p)
        candidates,rejected=medication_candidates(p,"pneumonia")
        self.assertEqual([x["record_id"] for x in candidates],["rx_second"])
        self.assertEqual(rejected["oral_vancomycin_not_pneumonia_treatment"],1)
        self.assertEqual(p,original)

    def test_recency_is_explicit_and_fractional_frequency_not_invented(self):
        p=self.package([order(time=-100,ref="old"),order(time=-2,ref="recent"),order(daily="0.5",ref="unsupported")])
        candidates,rejected=medication_candidates(p,"type2_diabetes")
        self.assertEqual([x["record_id"] for x in candidates],["recent","old"])
        self.assertIn("regimen_not_supported_by_closed_daily_inventory_parser",rejected)

    def test_unknown_allergy_and_course_preserved(self):
        facts={x["topic"]:x for x in facts_for("pneumonia",0,order("Doxycycline Hyclate"))}
        for key in ("allergy_history","antibiotic_total_course","imaging_microbiology"):
            self.assertIsNone(facts[key]["value"])
            self.assertEqual(facts[key]["status"],"unknown")
        self.assertEqual(facts["recent_administration"]["source_kind"],"constructed")
        self.assertIn("2次",facts["recent_administration"]["value"])
        self.assertIn("6次",facts["supply_inventory"]["value"])
        self.assertIn("4次",facts["supply_inventory"]["value"])

    def test_hf_observable_questions_defined_without_objective_invention(self):
        facts={x["topic"]:x for x in facts_for("heart_failure",0,order("Furosemide"))}
        for key in ("dizziness_syncope","urination","orthopnea_night","chest_palpitations"):
            self.assertEqual(facts[key]["status"],"reported")
        self.assertEqual(facts["weight_change"]["knowledge_state"],"not_measured")
        self.assertIn("没称体重",facts["weight_change"]["value"])

    def test_history_risk_cues_do_not_edit_comorbidity(self):
        p=self.package([]); p["case"]["clinical"]["history"]["encounters"]=[{"encounter_ref":"anchor","diagnoses":[
            {"record_id":"dx-fixture","coding":{"code":"N18.4"},"display":"Chronic kidney disease stage4"}]}]
        original=deepcopy(p); cues=source_risk_cues(p)
        self.assertEqual(len(cues),1);self.assertEqual(p,original)
        self.assertTrue(cues[0]["not_a_current_state_inference"])


if __name__=="__main__":unittest.main()
