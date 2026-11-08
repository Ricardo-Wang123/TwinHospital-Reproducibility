"""Auditable engineering checks and a disabled clinical-rule review registry.

No output from this PPRS is an independent clinical reference label. In
particular, a 'pass' only means that the enabled engineering checks did not
trigger. All six clinical error categories still need independently approved,
product-specific applicability and case labels.
"""
from collections import defaultdict
from copy import deepcopy
from decimal import Decimal, InvalidOperation

from .contracts import digest

ERROR_CATEGORIES = ("solvent_error", "dosage_error", "drug_drug_interaction",
                    "duplicate_medication", "administration_route_error", "contraindication")

CLINICAL_RULE_REGISTRY = {
    "registry_version": "candidate-label-rules-20261107-v1",
    "status": "pending_independent_clinical_review",
    "rules_enabled": False,
    "notes": "Source existence is verified; product mapping, local applicability and independent case labels are not.",
    "sources": [
        {"source_id": "furosemide-injection-fda-2024", "label_version": "FDA reference 5454016, 2024 label",
         "url": "https://www.accessdata.fda.gov/drugsatfda_docs/label/2024/018902s036lbl.pdf",
         "reviewed_at": "2026-11-07", "sections": ["2.2"]},
        {"source_id": "metformin-coated-dailymed-20240229",
         "label_version": "SPL section date 2024-02-29; setid c2bbaa0c-5b05-4335-882f-14f1b443edd7",
         "url": "https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=c2bbaa0c-5b05-4335-882f-14f1b443edd7",
         "reviewed_at": "2026-11-07", "sections": ["2.1", "2.3", "4", "7"]},
        {"source_id": "albuterol-cipla-dailymed-v2",
         "label_version": "SPL version 2; label page updated 2026-01-04",
         "url": "https://dailymed.nlm.nih.gov/dailymed/lookup.cfm?setid=fd0389e0-3713-42e0-8ef2-3bb97676fded&version=2",
         "reviewed_at": "2026-11-07", "sections": ["2.3", "7", "7.2"]},
    ],
    "candidates": [
        {"rule_id": "candidate-solvent-furosemide", "category": "solvent_error",
         "source_id": "furosemide-injection-fda-2024", "section": "2.2",
         "candidate_statement": "High-dose furosemide infusions have product-specific diluent and pH requirements; acidic co-infusions may precipitate the drug.",
         "required_inputs": ["exact_injectable_product", "diluent", "prepared_solution_pH", "same_line_co_infusions", "infusion_rate"],
         "applicability": "Specified intravenous formulation and infusion preparation only; never apply to oral furosemide tablets."},
        {"rule_id": "candidate-dose-metformin-ir", "category": "dosage_error",
         "source_id": "metformin-coated-dailymed-20240229", "section": "2.1",
         "candidate_statement": "This immediate-release product's adult daily maximum is 2550 mg; pediatric and other formulations have different limits.",
         "required_inputs": ["exact_product_and_release_form", "age", "mg_per_dose", "doses_per_day", "concurrent_same_ingredient_orders"],
         "applicability": "Mapped product and population; do not infer daily dose from ambiguous PRN or variable schedules."},
        {"rule_id": "candidate-interaction-albuterol-diuretic", "category": "drug_drug_interaction",
         "source_id": "albuterol-cipla-dailymed-v2", "section": "7.2",
         "candidate_statement": "Non-potassium-sparing diuretics may intensify hypokalemia or ECG effects; the label advises caution/monitoring, not a universal prohibition.",
         "required_inputs": ["active_products", "overlapping_exposure", "diuretic_class", "potassium", "monitoring_plan"],
         "applicability": "Concurrent exposure with relevant clinical context; a warning is not automatically an erroneous prescription."},
        {"rule_id": "candidate-duplicate-beta-agonist", "category": "duplicate_medication",
         "source_id": "albuterol-cipla-dailymed-v2", "section": "7",
         "candidate_statement": "Other short-acting sympathomimetic aerosol bronchodilators can potentiate effects; concurrent regimens require product and intent review.",
         "required_inputs": ["normalized_ingredients", "dose_form", "exposure_overlap", "regimen_intent", "replacement_or_taper_status"],
         "applicability": "Does not prove all repeated ingredient lines are errors; split dosing and replacement prescriptions require adjudication."},
        {"rule_id": "candidate-route-albuterol-aerosol", "category": "administration_route_error",
         "source_id": "albuterol-cipla-dailymed-v2", "section": "2.3",
         "candidate_statement": "The selected metered aerosol is administered by oral inhalation, not swallowed or injected.",
         "required_inputs": ["exact_product", "dose_form", "normalized_route"],
         "applicability": "Requires verified equivalence of the simulated salbutamol product and selected label; ingredient name alone is insufficient."},
        {"rule_id": "candidate-contraindication-metformin-egfr", "category": "contraindication",
         "source_id": "metformin-coated-dailymed-20240229", "section": "2.3 and 4",
         "candidate_statement": "The product is contraindicated below eGFR 30 mL/min/1.73 m²; initiation at 30–45 has a different recommendation.",
         "required_inputs": ["exact_product", "time_relevant_eGFR", "eGFR_unit", "initiation_or_continuation"],
         "applicability": "Do not treat creatinine alone, a stale result, or missing renal information as a known eGFR."},
    ],
}
for _row in CLINICAL_RULE_REGISTRY["candidates"]:
    _row.update(status="pending_independent_review", enabled=False,
                product_mapping_status="unverified", independent_reviewer=None)


def clinical_rule_registry():
    return deepcopy(CLINICAL_RULE_REGISTRY)


def _positive_decimal(value):
    if isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() and number > 0 else None
    except (InvalidOperation, ValueError):
        return None


class EngineeringPPRS:
    """Deterministic schema/catalogue checks, with deliberately limited coverage."""
    rule_version = "engineering-catalogue-v1"
    engineering_only = True

    def __init__(self, formulary):
        self.formulary = {str(row["drug_code"]): deepcopy(row) for row in formulary}
        if len(self.formulary) != len(formulary):
            raise ValueError("catalogue drug_code values must be unique")
        self.catalogue_hash = digest(formulary)

    def review(self, prescription, context=None):
        findings = []
        def add(code, index, *, severity="review", detail=None):
            findings.append({"code": code, "item_index": index, "severity": severity,
                             "detail": detail, "basis": "engineering_contract"})
        if not isinstance(prescription, dict) or not isinstance(prescription.get("items"), list):
            add("invalid_prescription_shape", None, severity="reject")
            items = []
        else:
            items = prescription["items"]
            if not items:
                add("empty_prescription", None, severity="review")
        ingredient_rows = defaultdict(list)
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                add("invalid_item_shape", index, severity="reject")
                continue
            required = ("drug_code", "dose", "dose_unit", "route", "frequency", "quantity", "quantity_unit")
            missing = [key for key in required if item.get(key) in (None, "")]
            if missing:
                add("missing_required_fields", index, severity="reject", detail=missing)
            for field in ("dose", "quantity"):
                raw = item.get(field)
                if raw not in (None, "") and _positive_decimal(raw) is None:
                    add("invalid_positive_" + field, index, severity="reject")
            product = self.formulary.get(str(item.get("drug_code")))
            if product is None:
                add("unmapped_catalogue_drug", index)
                continue
            permitted = product.get("routes", [product.get("route")])
            if item.get("route") not in permitted:
                add("catalogue_route_mismatch", index, severity="reject",
                    detail={"submitted": item.get("route"), "permitted": permitted})
            for field in ("dose_unit", "quantity_unit"):
                allowed_units = product.get("allowed_dose_units", [product.get(field)]) if field == "dose_unit" else [product.get(field)]
                if product.get(field) and item.get(field) not in allowed_units:
                    add("unmapped_" + field, index)
            ingredient = product.get("ingredient")
            if ingredient:
                ingredient_rows[str(ingredient).strip().casefold()].append(index)
        for ingredient, indices in ingredient_rows.items():
            if len(indices) > 1:
                # Repeat ingredient requires reconciliation; overlapping clinical
                # duplication is NOT established merely by seeing two rows.
                add("repeated_ingredient_requires_review", indices, detail=ingredient)
        decision = ("reject" if any(x["severity"] == "reject" for x in findings)
                    else "flag" if findings else "pass")
        return {"decision": decision, "flags": sorted({x["code"] for x in findings}),
                "findings": findings, "rule_version": self.rule_version,
                "catalogue_hash": self.catalogue_hash,
                "input_hash": digest({"prescription": prescription, "context": context}),
                "engineering_only": True, "clinical_clearance": "not_assessed",
                "clinical_categories_not_validated": list(ERROR_CATEGORIES),
                "pass_meaning": "No enabled engineering check triggered; not a clinically compliant label."}

    __call__ = review


def inject_engineering_fault(prescription, kind):
    """Create a clearly labelled engineering variant; never a clinical gold label."""
    changed = deepcopy(prescription)
    if not changed.get("items"):
        raise ValueError("an existing candidate prescription is required")
    if kind == "nonpositive_dose":
        changed["items"][0]["dose"] = "0"
    elif kind == "catalogue_route_mismatch":
        changed["items"][0]["route"] = "UNMAPPED_TEST_ROUTE"
    elif kind == "repeated_ingredient":
        changed["items"].append(deepcopy(changed["items"][0]))
    else:
        raise ValueError("only declared engineering mutations are enabled")
    return {"prescription": changed, "mutation_kind": kind,
            "original_hash": digest(prescription), "modified_hash": digest(changed),
            "evidence_scope": "engineering_mutation_only", "clinical_label": None,
            "clinical_review_status": "pending_independent_review"}
