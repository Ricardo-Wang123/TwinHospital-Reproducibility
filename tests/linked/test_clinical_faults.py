from copy import deepcopy
import json
import unittest

from sandbox.experiments.linked.clinical_faults import (
    evaluate_rule, make_control_candidate, make_fault_candidate, registry, review_materials,
)


def order(code, oid="ord-A", dose=500, unit="mg", frequency="BID", route="oral"):
    return {"order_id": oid, "drug_code": code, "dose": dose, "dose_unit": unit,
            "frequency": frequency, "route": route, "quantity": 14, "quantity_unit": "tablet",
            "status": "active", "exposure": {"start_hour": 0, "end_hour": 168,
            "administration_intent": "concurrent_active", "evidence_ref": "constructed-plan"}}


def context():
    products = {r["catalogue_code"]: r["product_id"] for r in registry()["rules"]}
    products["SIM-SACUBITRIL-VALSARTAN-24-26"] = "entresto"
    return {"demographics": {"age": 55}, "product_bindings": {code: {
        "reference_product_id": product, "verification_status": "constructed_exact_reference_product",
        "evidence_ref": "constructed-exact-product-definition"} for code, product in products.items()},
        "active_medications": [], "medication_reconciliation": {"status": "complete",
        "all_current_products_mapped": True, "evidence_ref": "constructed-reconciliation",
        "product_family_scope": {"metformin_ir": {"all_ingredient_orders_identified": True,
        "mixed_release_forms": False, "evidence_ref": "constructed-metformin-reconciliation"}}},
        "allergies": {"assessment_status": "complete", "assessment_ref": "constructed-allergy-assessment",
        "items": [], "desensitization_or_rechallenge": False, "source_kind": "constructed"}}


def fixtures():
    rows = []
    ctx = context()
    rows.append(("metformin_ir_adult_daily_max", {"items": [order("SIM-METFORMIN-500")]}, ctx, {"dose": 1500}))
    allergy = deepcopy(ctx["allergies"])
    allergy["items"] = [{"ingredient": "amoxicillin", "reaction": "anaphylaxis", "confirmation": "confirmed", "evidence_ref": "constructed-allergy-assessment"}]
    rows.append(("amoxicillin_confirmed_serious_allergy", {"items": [order("SIM-AMOXICILLIN-500")]}, ctx, {"allergy_record": allergy}))
    inhaled = order("SIM-TIOTROPIUM-18", dose=18, unit="microgram", frequency="QD", route="inhaled")
    inhaled.update(device="HANDIHALER", quantity_unit="capsule", route_intent="inhalation")
    rows.append(("tiotropium_handihaler_not_swallowed", {"items": [inhaled]}, ctx, {}))
    single = order("SIM-METFORMIN-500")
    single["regimen_intent_id"] = "plan-A"
    clone_ctx = deepcopy(ctx)
    clone_ctx["regimen_intents"] = {"plan-A": {"planned_active_order_count": 1,
        "evidence_ref": "constructed-independent-single-order-plan", "split_replacement_or_taper": False,
        "reference_signature": {"drug_code": "SIM-METFORMIN-500", "dose": 500, "dose_unit": "mg", "frequency": "BID",
        "route": "oral", "route_intent": None, "quantity": 14, "quantity_unit": "tablet", "device": None, "regimen_intent_id": "plan-A"}}}
    rows.append(("exact_unintended_active_order_clone", {"items": [single]}, clone_ctx, {}))
    rows.append(("lisinopril_sacubitril_concomitant", {"items": [order("SIM-LISINOPRIL-10", dose=10, frequency="QD")]}, ctx,
                 {"co_medication": order("SIM-SACUBITRIL-VALSARTAN-24-26", "ord-B", dose=1, unit="tablet")}))
    iv = order("SIM-CEFTRIAXONE-1G", dose=1000, frequency="QD", route="intravenous")
    iv["preparation"] = {"purpose": "further_iv_dilution", "diluent_code": "SIM-NACL-09-100ML",
        "contains_calcium": False, "diluent_identity_verified": True,
        "final_concentration_mg_ml": 10, "evidence_ref": "constructed-preparation"}
    ivctx = deepcopy(ctx)
    ivctx.update(scenario_setting="infusion_clinic", iv_therapy_appropriate=True, iv_therapy_basis_ref="constructed-independently-planned-IV-scenario")
    bad_prep = {**iv["preparation"], "diluent_code": "SIM-LACTATED-RINGERS-500ML", "contains_calcium": True}
    rows.append(("ceftriaxone_calcium_iv_diluent", {"items": [iv]}, ivctx, {"preparation": bad_prep}))
    return rows


class ClinicalFaultTests(unittest.TestCase):
    def test_six_categories_reproducible_preserve_original_and_pending(self):
        categories = set()
        for rule, rx, ctx, mutation in fixtures():
            with self.subTest(rule=rule):
                before = deepcopy((rx, ctx))
                one = make_fault_candidate(rule, rx, ctx, mutation=mutation)
                two = make_fault_candidate(rule, rx, ctx, mutation=mutation)
                self.assertEqual(one, two)
                self.assertEqual((rx, ctx), before)
                ref = one["reference_candidate"]
                self.assertEqual(ref["assessment"]["status"], "violation")
                self.assertEqual(ref["base_assessment"]["status"], "scoped_nonviolation")
                self.assertEqual(ref["independent_review_status"], "independent_review_pending")
                self.assertIsNone(ref["clinical_gold_label"])
                self.assertTrue(one["mutation_diff"])
                self.assertFalse(one["audit"]["native_write_performed"])
                categories.add(ref["assessment"]["category"])
        self.assertEqual(len(categories), 6)

    def test_dose_exact_boundary_and_ambiguous_frequency(self):
        rule, rx, ctx, _ = fixtures()[0]
        rx["items"][0].update(dose=850, frequency="TID")
        self.assertEqual(evaluate_rule(rule, rx, ctx)["status"], "scoped_nonviolation")
        rx["items"][0]["dose"] = 851
        self.assertEqual(evaluate_rule(rule, rx, ctx)["status"], "violation")
        rx["items"][0]["frequency"] = "PRN"
        self.assertEqual(evaluate_rule(rule, rx, ctx)["status"], "indeterminate")
        with self.assertRaises(ValueError):
            make_fault_candidate(rule, rx, ctx, mutation={"dose": 5000})

    def test_device_case_and_whitespace_do_not_change_device_identity(self):
        rule,rx,ctx,_=fixtures()[2]
        for device in ('HANDIHALER','HandiHaler',' handihaler '):
            rx['items'][0]['device']=device;before=deepcopy(rx)
            self.assertEqual(evaluate_rule(rule,rx,ctx)['status'],'scoped_nonviolation')
            self.assertEqual(rx,before)
        for device in ('RESPIMAT','inhaler','HandiHaler or Respimat',None):
            rx['items'][0]['device']=device
            self.assertEqual(evaluate_rule(rule,rx,ctx)['status'],'indeterminate')

    def test_unknown_allergy_and_mixed_formulation_are_not_negative(self):
        rule, rx, ctx, _ = fixtures()[1]
        ctx["allergies"] = {"assessment_status": "unknown", "items": [], "desensitization_or_rechallenge": False}
        control = make_control_candidate(rule, rx, ctx)
        self.assertFalse(control["reference_candidate"]["control_eligible_in_rule_scope"])
        self.assertEqual(control["reference_candidate"]["assessment"]["status"], "indeterminate")
        rule, rx, ctx, _ = fixtures()[0]
        ctx["medication_reconciliation"]["product_family_scope"]["metformin_ir"]["mixed_release_forms"] = True
        self.assertEqual(evaluate_rule(rule, rx, ctx)["status"], "indeterminate")

    def test_repeated_ingredient_without_single_order_intent_is_not_gold(self):
        rule, rx, ctx, _ = fixtures()[3]
        clone = deepcopy(rx["items"][0]); clone["order_id"] = "ord-B"
        rx["items"].append(clone)
        ctx["regimen_intents"] = {}
        self.assertEqual(evaluate_rule(rule, rx, ctx)["status"], "indeterminate")

    def test_historical_or_switching_drugs_do_not_imply_concurrent_ddi(self):
        rule, rx, ctx, mutation = fixtures()[4]
        co = deepcopy(mutation["co_medication"])
        co["status"] = "historical"
        ctx["active_medications"] = [co]
        self.assertEqual(evaluate_rule(rule, rx, ctx)["status"], "indeterminate")
        co["status"] = "active"; co["exposure"].update(start_hour=200, end_hour=300)
        self.assertEqual(evaluate_rule(rule, rx, ctx)["status"], "indeterminate")

    def test_iv_scenario_required_and_no_unknown_diluent_negative(self):
        rule, rx, ctx, mutation = fixtures()[5]
        ctx["scenario_setting"] = "routine_outpatient"
        self.assertEqual(evaluate_rule(rule, rx, ctx)["status"], "not_applicable")
        with self.assertRaises(ValueError):
            make_fault_candidate(rule, rx, ctx, mutation=mutation)
        ctx["scenario_setting"] = "infusion_clinic"
        del rx["items"][0]["preparation"]["contains_calcium"]
        self.assertEqual(evaluate_rule(rule, rx, ctx)["status"], "indeterminate")

    def test_all_controls_are_scoped_and_blind_packets_hide_target_and_diff(self):
        candidates = []
        for rule, rx, ctx, mutation in fixtures():
            candidates.append(make_control_candidate(rule, rx, ctx))
            candidates.append(make_fault_candidate(rule, rx, ctx, mutation=mutation))
        packet = review_materials(candidates)
        self.assertEqual(len(packet["reviewer_1"]), 12)
        blind = json.dumps(packet["reviewer_1"])
        for forbidden in ("reference_candidate", "mutation_diff", "clinical_gold_label", '"rule_id"'):
            self.assertNotIn(forbidden, blind)
        self.assertEqual(len(packet["coordinator_only"]), 12)
        self.assertEqual(packet["reviewer_1"], packet["reviewer_2"])

    def test_actor_boundary_rejects_raw_ids_and_hidden_labels(self):
        rule, rx, ctx, _ = fixtures()[0]
        ctx["demographics"]["subject_id"] = "SECRET"
        with self.assertRaises(ValueError):
            evaluate_rule(rule, rx, ctx)


if __name__ == "__main__":
    unittest.main()
