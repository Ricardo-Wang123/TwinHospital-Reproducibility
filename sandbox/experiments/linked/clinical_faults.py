"""Reproducible clinical-rule candidates, never clinician gold labels.

No network, model, native HIS write, or disease-treatment assignment occurs here.
Use ``actor_input`` only for blinded system runs. The base, diff, basis and
reference candidate are coordinator/evaluator-only. All cases need independent
review, including controls. These six narrow rules do not certify total safety.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path


REGISTRY_PATH = Path(__file__).with_name("clinical_rules_registry_v2.json")
FREQUENCIES = {"QD": Decimal(1), "BID": Decimal(2), "TID": Decimal(3), "QID": Decimal(4)}
VISIBLE_CONTEXT_KEYS = {"demographics", "product_bindings", "active_medications", "medication_reconciliation",
    "allergies", "regimen_intents", "scenario_setting", "iv_therapy_appropriate", "iv_therapy_basis_ref"}
FORBIDDEN_KEYS = {"evaluation", "provenance", "reference_candidate", "gold_label", "expected_label", "mutation_diff",
                  "subject_id", "hadm_id", "stay_id", "patient_name", "source_identifier_map"}


def registry():
    return json.loads(REGISTRY_PATH.read_text())


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        n = Decimal(str(value))
        return n if n.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _guard(value):
    if isinstance(value, dict):
        if set(value) & FORBIDDEN_KEYS:
            raise ValueError("actor context contains private label or raw identity fields")
        for item in value.values():
            _guard(item)
    elif isinstance(value, list):
        for item in value:
            _guard(item)


def _inputs(prescription, context):
    if not isinstance(prescription, dict) or not isinstance(prescription.get("items"), list):
        raise ValueError("prescription.items must be a list")
    if not isinstance(context, dict) or set(context) - VISIBLE_CONTEXT_KEYS:
        raise ValueError("use the declared clinical-reference context; do not attach complete hidden case packages")
    _guard(prescription)
    _guard(context)
    orders = prescription["items"] + context.get("active_medications", [])
    if any(not isinstance(x, dict) or not x.get("order_id") for x in orders):
        raise ValueError("each current order needs a stable order_id")
    if len({x["order_id"] for x in orders}) != len(orders):
        raise ValueError("order IDs must be unique across candidate and reconciled active medication lists")
    return orders


def _complete(context):
    rec = context.get("medication_reconciliation", {})
    return (rec.get("status") == "complete" and rec.get("all_current_products_mapped") is True
            and bool(rec.get("evidence_ref")))


def _binding(order, context, product_id):
    binding = context.get("product_bindings", {}).get(order.get("drug_code"), {})
    return (binding.get("reference_product_id") == product_id
        and binding.get("verification_status") in {"verified", "constructed_exact_reference_product"}
        and bool(binding.get("evidence_ref")))


def _exposure(order):
    interval = order.get("exposure", {})
    start, end = _number(interval.get("start_hour")), _number(interval.get("end_hour"))
    if (order.get("status") != "active" or interval.get("administration_intent") != "concurrent_active"
            or not interval.get("evidence_ref") or start is None or end is None or end <= start):
        return None
    return start, end


def _overlap(a, b):
    x, y = _exposure(a), _exposure(b)
    if x is None or y is None:
        return None
    return max(x[0], y[0]) < min(x[1], y[1])


def _signature(order):
    # Quantity is intentionally included: this is a clone of one order, not
    # same-ingredient split dosing. Unique business IDs are deliberately excluded.
    keys = ("drug_code", "dose", "dose_unit", "frequency", "route", "route_intent",
            "quantity", "quantity_unit", "device", "regimen_intent_id")
    return {k: order.get(k) for k in keys}


def evaluate_rule(rule_id, prescription, context):
    """Return violation/scoped_nonviolation/indeterminate/not_applicable.

    Even a mechanical violation is an independent-review *candidate*. Uncertain
    applicability, ambiguous dosing and incomplete negative evidence fail closed.
    """
    rules = {x["rule_id"]: x for x in registry()["rules"]}
    if rule_id not in rules:
        raise ValueError("unknown clinical candidate rule")
    rule = rules[rule_id]
    orders = _inputs(prescription, context)
    def result(status, reason, evidence=None):
        return {"rule_id": rule_id, "category": rule["category"], "status": status,
                "reason": reason, "evidence": evidence or {}, "reference_status": "independent_review_pending",
                "clinical_normality": "not_established", "source_ids": rule["sources"]}
    targets = [x for x in orders if x.get("drug_code") == rule["catalogue_code"]]
    if not targets:
        return result("not_applicable", "The named product is not a candidate/current order; history alone is not exposure.")
    if any(not _binding(x, context, rule["product_id"]) for x in targets):
        return result("indeterminate", "Exact reference-product binding is missing or unverified.")
    if any(_exposure(x) is None for x in targets):
        return result("indeterminate", "Active status or intended exposure interval is not fully established.")

    if rule_id == "metformin_ir_adult_daily_max":
        age = _number(context.get("demographics", {}).get("age"))
        population=context.get('demographics',{}).get('age_population') or {}
        documented_adult=(population.get('category')=='adult'
            and population.get('status')=='documented_category'
            and population.get('minimum_age_years')==18 and bool(population.get('evidence_ref')))
        if population.get('status')=='conflicting_demographic_evidence' or (documented_adult and age is not None and age<18):
            return result('indeterminate','Adult category conflicts with the reported age; resolve the demographic record.')
        if age is None and not documented_adult:
            return result("indeterminate", "Neither an exact adult age nor an evidenced adult category is established.")
        if age is not None and age < 18:
            return result("not_applicable", "Adult IR daily ceiling does not cover this age.")
        if not _complete(context):
            return result("indeterminate", "Current regimen reconciliation is incomplete.")
        family = context.get("medication_reconciliation", {}).get("product_family_scope", {}).get("metformin_ir", {})
        if (family.get("all_ingredient_orders_identified") is not True or family.get("mixed_release_forms") is not False
                or not family.get("evidence_ref")):
            return result("indeterminate", "All metformin orders and release forms must be reconciled; an exact-product subset cannot establish total daily dose.")
        if any(_binding(x, context, "metformin_ir") and x.get("drug_code") != rule["catalogue_code"] for x in orders):
            return result("indeterminate", "Same reference product appears under an unnormalized alternate code.")
        amounts = []
        for order in targets:
            dose = _number(order.get("dose"))
            if dose is None or dose <= 0 or order.get("dose_unit") != "mg" or order.get("frequency") not in FREQUENCIES or order.get("route") != "oral":
                return result("indeterminate", "Requires fixed positive oral mg dose and a declared non-PRN schedule.")
            amounts.append(dose * FREQUENCIES[order["frequency"]])
        # Ceiling evaluation uses overlapping declared regimens, never a sum
        # over years of prescriptions. Maximum at an interval boundary suffices.
        points = sorted({p for x in targets for p in _exposure(x)})
        peak = max((sum(amounts[i] for i, x in enumerate(targets) if _exposure(x)[0] <= p < _exposure(x)[1]) for p in points), default=Decimal(0))
        return result("violation" if peak > 2550 else "scoped_nonviolation", "Adult IR total scheduled daily-dose ceiling only.", {"peak_scheduled_mg_per_day": str(peak), "ceiling_mg_per_day": 2550})

    if rule_id == "amoxicillin_confirmed_serious_allergy":
        allergy = context.get("allergies", {})
        if allergy.get("desensitization_or_rechallenge") is not False:
            return result("indeterminate", "Specialist exception/rechallenge context has not been excluded.")
        known = [a for a in allergy.get("items", []) if a.get("ingredient") == "amoxicillin"
                 and a.get("reaction") in {"anaphylaxis", "Stevens-Johnson syndrome"}
                 and a.get("confirmation") == "confirmed" and a.get("evidence_ref")]
        if known:
            return result("violation", "Confirmed serious hypersensitivity to this ingredient.", {"allergy_evidence_refs": [a["evidence_ref"] for a in known]})
        if allergy.get("assessment_status") != "complete" or not allergy.get("assessment_ref"):
            return result("indeterminate", "Unknown or unassessed allergy is not a negative control.")
        if allergy.get("items"):
            return result("indeterminate", "Other, unclear or incompletely characterized allergy needs clinical review.")
        return result("scoped_nonviolation", "A completed explicit negative allergy assessment covers this same-ingredient serious-allergy rule only.")

    if rule_id == "tiotropium_handihaler_not_swallowed":
        if any(x.get("route") == "oral" and x.get("route_intent") == "swallow_capsule" for x in targets):
            return result("violation", "Inhalation-powder capsule explicitly ordered to be swallowed.")
        if all(x.get("route") == "inhaled" and isinstance(x.get('device'),str)
               and x['device'].strip().casefold() == 'handihaler' for x in targets):
            return result("scoped_nonviolation", "Declared route/device matches this label; actual technique is not established.")
        return result("indeterminate", "Route intent or exact inhalation device is missing/ambiguous.")

    if rule_id == "exact_unintended_active_order_clone":
        if not _complete(context):
            return result("indeterminate", "Complete active order scope is needed to assess duplication.")
        for first in targets:
            intent = context.get("regimen_intents", {}).get(first.get("regimen_intent_id"), {})
            if (intent.get("planned_active_order_count") != 1 or not intent.get("evidence_ref")
                    or intent.get("split_replacement_or_taper") is not False
                    or intent.get("reference_signature") != _signature(first)):
                return result("indeterminate", "The independently documented single-order intent is missing or does not match.")
            duplicates = [x for x in targets if _signature(x) == _signature(first) and _overlap(x, first) is True]
            if len(duplicates) > 1:
                return result("violation", "Distinct exact-clone active orders exceed the recorded one-order intent.", {"order_ids": [x["order_id"] for x in duplicates], "intent_evidence_ref": intent["evidence_ref"]})
        return result("scoped_nonviolation", "No duplicate of the explicitly documented single-order intent in the complete current-order scope.")

    if rule_id == "lisinopril_sacubitril_concomitant":
        others = [x for x in orders if x.get("drug_code") == rule["co_catalogue_code"]]
        if any(not _binding(x, context, rule["co_product_id"]) or _exposure(x) is None for x in others):
            return result("indeterminate", "The other exact product or administration timing is not established.")
        pairs = [(a, b) for a in targets for b in others]
        if any(_overlap(a, b) is True for a, b in pairs):
            return result("violation", "The two specifically contraindicated products have documented concurrent administration intent.")
        if others:
            # Do not label switching controls normal without a separate last-dose,
            # first-dose and 36-hour washout assessment.
            return result("indeterminate", "Nonoverlap may be a switching/washout case; it requires separate review.")
        if not _complete(context):
            return result("indeterminate", "Absence of the second product is not established by incomplete reconciliation.")
        return result("scoped_nonviolation", "Complete reconciled scope contains no active sacubitril/valsartan; other interactions are unassessed.")

    if rule_id == "ceftriaxone_calcium_iv_diluent":
        if context.get("scenario_setting") not in {"iv_treatment", "infusion_clinic"} or context.get("iv_therapy_appropriate") is not True or not context.get("iv_therapy_basis_ref"):
            return result("not_applicable", "An independently supported IV-therapy scenario is required; do not force this onto routine oral visits.")
        for order in targets:
            prep = order.get("preparation", {})
            if order.get("route") != "intravenous" or prep.get("purpose") not in {"reconstitution", "further_iv_dilution"}:
                return result("not_applicable", "This rule covers the named IV preparation operation only.")
            if not prep.get("diluent_identity_verified") or not prep.get("evidence_ref"):
                return result("indeterminate", "Exact diluent identity/composition needs evidence.")
            if prep.get("contains_calcium") is True:
                return result("violation", "Calcium-containing liquid is used in the same ceftriaxone IV preparation.", {"diluent_code": prep.get("diluent_code")})
            concentration = _number(prep.get("final_concentration_mg_ml"))
            if (prep.get("contains_calcium") is not False or prep.get("diluent_code") not in {"SIM-NACL-09-100ML", "SIM-DEXTROSE-5-100ML"}
                    or concentration is None or not 10 <= concentration <= 40):
                return result("indeterminate", "A scoped control requires explicit NS/D5W identity, calcium absence and label-range concentration.")
        return result("scoped_nonviolation", "No calcium incompatibility under the declared narrow preparation conditions; full infusion safety is unassessed.")
    raise AssertionError("unimplemented registry rule")


def _diff(before, after, path=""):
    if isinstance(before, dict) and isinstance(after, dict):
        result = []
        for key in sorted(set(before) | set(after)):
            pointer = path + "/" + str(key).replace("~", "~0").replace("/", "~1")
            if key not in before:
                result.append({"op": "add", "path": pointer, "after": deepcopy(after[key])})
            elif key not in after:
                result.append({"op": "remove", "path": pointer, "before": deepcopy(before[key])})
            else:
                result.extend(_diff(before[key], after[key], pointer))
        return result
    if isinstance(before, list) and isinstance(after, list):
        result = []
        for index in range(max(len(before), len(after))):
            pointer = path + "/" + str(index)
            if index >= len(before):
                result.append({"op": "add", "path": pointer, "after": deepcopy(after[index])})
            elif index >= len(after):
                result.append({"op": "remove", "path": pointer, "before": deepcopy(before[index])})
            else:
                result.extend(_diff(before[index], after[index], pointer))
        return result
    if before == after:
        return []
    return [{"op": "replace", "path": path or "/", "before": deepcopy(before), "after": deepcopy(after)}]


def _bundle(rule_id, base, variant, assessment, base_assessment):
    rule = next(x for x in registry()["rules"] if x["rule_id"] == rule_id)
    identity = _hash({"rule_id": rule_id, "base": base, "variant": variant, "registry_hash": _hash(registry())})
    return {"schema_version": "twinhospital.clinical-reference-candidate/2", "candidate_id": "refcand_" + identity[:24],
        "base": deepcopy(base), "actor_input": deepcopy(variant), "mutation_diff": _diff(base, variant),
        "basis": {"rule": rule, "sources": {s: registry()["sources"][s] for s in rule["sources"]}},
        "reference_candidate": {"assessment": assessment, "base_assessment": base_assessment,
            "independent_review_status": "independent_review_pending", "clinical_gold_label": None,
            "reviewer_1": None, "reviewer_2": None, "adjudication": None,
            "full_clinical_normality": "not_established", "other_error_categories": "not_excluded"},
        "audit": {"registry_hash": _hash(registry()), "base_hash": _hash(base), "variant_hash": _hash(variant),
            "native_write_performed": False, "model_judgment_used_as_reference": False,
            "label_visibility": "base/diff/basis/reference_candidate are coordinator-only; send only actor_input to a working agent"}}


def make_fault_candidate(rule_id, prescription, context, *, item_index=0, mutation=None):
    """Apply one explicit local mutation after a complete scoped baseline check.

    No missing allergy/current medication facts are silently filled. Caller must
    provide the clinical facts and their provenance before a mutation is eligible.
    Context changes are permitted only for an explicitly constructed scenario.
    """
    mutation = mutation or {}
    base_result = evaluate_rule(rule_id, prescription, context)
    if base_result["status"] != "scoped_nonviolation":
        raise ValueError("baseline is not a complete scoped nonviolation: " + base_result["reason"])
    base = {"prescription": deepcopy(prescription), "clinical_context": deepcopy(context)}
    variant = deepcopy(base)
    items, ctx = variant["prescription"]["items"], variant["clinical_context"]
    if type(item_index) is not int or not 0 <= item_index < len(items):
        raise ValueError("item_index must select an existing candidate order")
    item = items[item_index]
    rule = next(x for x in registry()["rules"] if x["rule_id"] == rule_id)
    if item.get("drug_code") != rule["catalogue_code"]:
        raise ValueError("selected order is outside the rule's product scope")
    if rule_id == "metformin_ir_adult_daily_max":
        if set(mutation) != {"dose"}:
            raise ValueError("explicit dose mutation required")
        item["dose"] = mutation["dose"]
    elif rule_id == "amoxicillin_confirmed_serious_allergy":
        if set(mutation) != {"allergy_record"} or mutation["allergy_record"].get("source_kind") != "constructed":
            raise ValueError("allergy mutation must explicitly declare constructed scenario facts")
        ctx["allergies"] = deepcopy(mutation["allergy_record"])
    elif rule_id == "tiotropium_handihaler_not_swallowed":
        item.update(route="oral", route_intent="swallow_capsule")
    elif rule_id == "exact_unintended_active_order_clone":
        clone = deepcopy(item)
        clone["order_id"] = "order_" + _hash({"base": base, "rule": rule_id})[:24]
        items.append(clone)
    elif rule_id == "lisinopril_sacubitril_concomitant":
        if set(mutation) != {"co_medication"} or mutation["co_medication"].get("drug_code") != rule["co_catalogue_code"]:
            raise ValueError("explicit exact co-medication order required; no default treatment is generated")
        items.append(deepcopy(mutation["co_medication"]))
    elif rule_id == "ceftriaxone_calcium_iv_diluent":
        if set(mutation) != {"preparation"}:
            raise ValueError("explicit preparation mutation required")
        item["preparation"] = deepcopy(mutation["preparation"])
    assessment = evaluate_rule(rule_id, variant["prescription"], ctx)
    if assessment["status"] != "violation":
        raise ValueError("mutation does not establish the declared narrow violation: " + assessment["reason"])
    return _bundle(rule_id, base, variant, assessment, base_result)


def make_control_candidate(rule_id, prescription, context):
    """Return a pending reference; unknown does not become a negative label."""
    assessment = evaluate_rule(rule_id, prescription, context)
    base = {"prescription": deepcopy(prescription), "clinical_context": deepcopy(context)}
    result = _bundle(rule_id, base, base, assessment, assessment)
    result["reference_candidate"]["control_eligible_in_rule_scope"] = assessment["status"] == "scoped_nonviolation"
    return result


def review_materials(candidates):
    """Two blinded independent forms plus a separate coordinator key.

    The same global rule pack is supplied for every case; the selected target
    category, expected result and mutation are hidden from both initial raters.
    """
    blinded, key = [], []
    for candidate in sorted(candidates, key=lambda x: x["candidate_id"]):
        token = "review_" + _hash(candidate["candidate_id"])[:20]
        blinded.append({"review_id": token, **deepcopy(candidate["actor_input"]),
            "rating": {"categories": None, "overall_appropriateness": None,
                "facts_sufficient": None, "additional_errors": None, "basis_refs": [], "rationale": None},
            "instruction": "Use patient facts and product labels. Record uncertain/not applicable separately; assess additional errors independently."})
        key.append({"review_id": token, "candidate_id": candidate["candidate_id"],
                    "reference_candidate": deepcopy(candidate["reference_candidate"]), "mutation_diff": deepcopy(candidate["mutation_diff"])})
    return {"reviewer_1": deepcopy(blinded), "reviewer_2": deepcopy(blinded),
            "shared_rule_materials": registry(), "coordinator_only": key,
            "review_status": "independent_review_pending"}
