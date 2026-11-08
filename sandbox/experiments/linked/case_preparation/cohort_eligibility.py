"""Outcome-independent local prescreen of the unchanged 2,500 source cases.

Time windows are preregisterable engineering selection windows, not universal
clinical-validity cutoffs. Historical findings are never current measurements.
This audit does not convert unknown allergy/current use into negative findings.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re

from .mimic_cohort import save


VERSION = "source-prescreen-v1-20261107"
DRUG_TERMS = {
    "type2_diabetes": ("metformin", "glipizide", "glyburide", "glimepiride", "sitagliptin", "insulin", "empagliflozin"),
    "heart_failure": ("furosemide", "torsemide", "bumetanide", "spironolactone", "lisinopril", "carvedilol", "metoprolol", "losartan", "valsartan", "empagliflozin"),
    "copd": ("tiotropium", "albuterol", "ipratropium", "formoterol", "salmeterol", "budesonide", "fluticasone"),
    "pneumonia": ("amoxicillin", "ampicillin", "azithromycin", "ceftriaxone", "cefepime", "cefpodoxime", "cefuroxime", "levofloxacin", "moxifloxacin", "doxycycline", "piperacillin", "meropenem", "vancomycin"),
    "uti": ("nitrofurantoin", "trimethoprim", "sulfamethoxazole", "ciprofloxacin", "levofloxacin", "ceftriaxone", "cefepime", "cefazolin", "amoxicillin", "ampicillin", "fosfomycin", "ertapenem", "meropenem", "piperacillin"),
}
LAB_GROUPS = {
    "creatinine": ({"Creatinine", "Creatinine, Whole Blood"}, "Blood"),
    "egfr": ({"Estimated GFR (MDRD equation)"}, "Blood"),
    "potassium": ({"Potassium", "Potassium, Whole Blood"}, "Blood"),
    "glucose": ({"Glucose", "Glucose, Whole Blood"}, "Blood"),
    "hba1c": ({"% Hemoglobin A1c"}, "Blood"),
    "wbc": ({"White Blood Cells"}, "Blood"),
    "urinalysis": ({"Leukocytes", "Nitrite", "White Blood Cells"}, "Urine"),
}
FREQUENCY = {"1": "QD", "2": "BID", "3": "TID", "4": "QID"}


def number(value):
    if isinstance(value, bool):
        return None
    try:
        n = Decimal(str(value).strip())
        return n if n.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _source_drug_candidate(order, group):
    """Strict small mapping proposal; every original field is retained separately.

    PO/NG, variable doses, unknown frequency, unqualified inhaler strengths,
    nitrofurantoin release variants and salt/base ambiguities are not guessed.
    The supported subset is deliberately narrower than the common catalogue.
    """
    raw = order.get("regimen_raw", {})
    name = (order.get("drug_name_raw") or "").strip().lower()
    strength = re.sub(r"\s+", "", (raw.get("prod_strength") or "").lower())
    route = (raw.get("route") or "").strip().upper()
    dose = number(raw.get("dose_val_rx")); daily = number(raw.get("doses_per_24_hrs"))
    if dose is None or dose <= 0 or daily is None or str(int(daily)) not in FREQUENCY or int(daily) != daily:
        return None
    freq = FREQUENCY[str(int(daily))]
    unit = (raw.get("dose_unit_rx") or "").strip().lower()
    ndc = (order.get("codes_raw", {}).get("ndc") or "").strip()
    product_basis = None
    code = None
    if group == "type2_diabetes" and name in {"metformin", "metformin (glucophage)"} and strength in {"500mgtablet", "500mgtab"} and route == "PO" and unit == "mg" and dose*daily <= 2550:
        code = "SIM-METFORMIN-500"
    elif group == "heart_failure" and name == "furosemide" and strength in {"20mgtablet", "20mgtab"} and route == "PO" and unit == "mg" and dose <= 40:
        code = "SIM-FUROSEMIDE-20"
    elif group == "copd" and name in {"tiotropium bromide", "tiotropium"} and strength == "18mcgcapsule-inhalationdevice" and route in {"IH", "INH"} and ((unit in {"mcg", "microgram"} and dose == 18) or (unit in {"cap", "capsule"} and dose == 1)) and daily == 1 and ndc in {"00597007575", "00597007541", "00597007547"}:
        # These three exact 11-digit NDCs are the zero-padded 0597-0075-xx
        # packages listed in the manufacturer label; their strength basis is
        # tiotropium, not bromide. No generic NDC digit-guessing is performed.
        code = "SIM-TIOTROPIUM-18"
        product_basis = "https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=820839ef-e53d-47e8-a3b9-d911ff92e6a9"
    elif group in {"pneumonia", "uti"} and name == "amoxicillin" and strength in {"500mgcapsule", "500mgcap"} and route == "PO" and unit == "mg" and dose <= 1000:
        code = "SIM-AMOXICILLIN-500"
    elif group == "pneumonia" and name == "azithromycin" and strength in {"250mgtablet", "250mgtab"} and route == "PO" and unit == "mg" and dose <= 500 and daily == 1:
        code = "SIM-AZITHROMYCIN-250"
    elif group == "uti" and name in {"sulfameth/trimethoprim ds", "sulfamethoxazole-trimethoprim ds", "trimethoprim-sulfamethoxazole ds"} and (strength in {"800-160mgtablet", "800mg-160mgtablet", "800-160mgtab"} or (strength == "dstablet" and ndc == "00904272561")) and route == "PO" and unit in {"tab", "tablet"} and dose == 1 and daily <= 2:
        code = "SIM-TMP-SMX-160-800"
        product_basis = "https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=abe0d2d5-4879-4b4e-9b57-47ea83a17ba0"
    if not code:
        return None
    normalized_unit = "capsule" if code == "SIM-TIOTROPIUM-18" else "tablet" if code == "SIM-TMP-SMX-160-800" else "mg"
    normalized_dose = 1 if normalized_unit in {"capsule", "tablet"} else float(dose)
    return {"source_order_ref": order["record_id"], "source_encounter_ref": order.get("source_encounter_ref"),
            "catalogue_code": code, "dose": normalized_dose, "dose_unit": normalized_unit, "frequency": freq,
            "route": "inhaled" if code == "SIM-TIOTROPIUM-18" else "oral",
            "mapping_status": "candidate_pending_product_review", "current_use_inferred": False,
            "product_identity_reference": product_basis,
            "source_time": order.get("time"), "source_stop_offset_hours": order.get("planned_stop_offset_hours"),
            "source_codes_raw": order.get("codes_raw", {}), "source_regimen_raw": raw}


def screen(case):
    group, his = case["group"], case["his_initial"]
    encounters = his.get("historical_encounters", [])
    anchor = next((x for x in encounters if x.get("role") == "anchor_historical_stay"), None)
    ref = anchor.get("encounter_ref") if anchor else None
    diagnoses = [x for x in (anchor or {}).get("diagnoses", []) if x.get("source_sequence") == 1]
    orders = [x for x in his.get("historical_orders", []) if x.get("source_encounter_ref") == ref]
    usable = [x for x in orders if number(x.get("regimen_raw", {}).get("dose_val_rx")) is not None
        and number(x["regimen_raw"]["dose_val_rx"]) > 0 and x["regimen_raw"].get("dose_unit_rx") and x["regimen_raw"].get("route")]
    relevant = [x for x in usable if any(t in (x.get("drug_name_raw") or "").lower() for t in DRUG_TERMS[group])]
    explicit_outpatient = [x for x in relevant if (x["regimen_raw"].get("route") or "").strip().upper() in {"PO", "IH", "INH", "NEB"}]
    candidates = [c for x in orders if (c := _source_drug_candidate(x, group))]
    latest = {}
    for lab in his.get("historical_observations", []):
        if lab.get("source_encounter_ref") != ref:
            continue
        tm = lab.get("time", {})
        when, available = number(tm.get("event_offset_hours")), number(tm.get("recorded_offset_hours"))
        if when is None or available is None or when > 0 or available > 0 or tm.get("available_by_source_cutoff") is not True:
            continue
        for kind, (names, fluid) in LAB_GROUPS.items():
            if lab.get("display") not in names or lab.get("fluid") != fluid:
                continue
            if kind != "urinalysis" and number(lab.get("numeric_value_raw")) is None:
                # A nonnumeric eGFR such as >60 is retained as an interval, not
                # silently converted to an exact numeric measurement.
                if kind != "egfr" or not re.fullmatch(r"\s*>\s*=?\s*\d+(?:\.\d+)?\s*", lab.get("value_raw") or ""):
                    continue
            age = Decimal(24) - when
            if kind not in latest or age < latest[kind]["age_hours_at_new_visit"]:
                latest[kind] = {"record_ref": lab["record_id"], "age_hours_at_new_visit": float(age),
                    "value_raw": lab.get("value_raw"), "numeric_value_raw": lab.get("numeric_value_raw"),
                    "unit_raw": lab.get("unit_raw"), "source_time": tm, "reference_range_raw": lab.get("reference_range_raw"),
                    "not_a_current_measurement": True}
    flags = {"historical_primary_diagnosis": bool(diagnoses), "same_anchor_usable_rx": bool(usable),
        "same_anchor_group_related_rx": bool(relevant), "same_anchor_explicit_oral_inhaled_rx": bool(explicit_outpatient),
        "same_anchor_common_catalogue_parse_candidate": bool(candidates),
        "allergy_unknown_preserved": his.get("allergy_record", {}).get("status") in {"unknown", "not_documented"},
        "current_use_unreconciled_preserved": his.get("current_use_record", {}).get("status") != "reconciled",
        "current_observations_present": bool(his.get("current_observations")), "current_formulary_present": bool(case.get("formulary"))}
    for kind in LAB_GROUPS:
        for hours in (72, 168):
            flags[f"{kind}_within_{hours}h"] = kind in latest and latest[kind]["age_hours_at_new_visit"] <= hours
    req = {"type2_diabetes": ["creatinine_within_168h", "glucose_within_72h"],
           "heart_failure": ["creatinine_within_168h", "potassium_within_72h"], "copd": [],
           "pneumonia": ["creatinine_within_168h", "wbc_within_168h"],
           "uti": ["creatinine_within_168h", "urinalysis_within_168h"]}[group]
    flags["source_material_ready_for_followup_draft"] = all(flags[x] for x in ["historical_primary_diagnosis", "same_anchor_explicit_oral_inhaled_rx", *req])
    flags["source_common_catalogue_candidate_with_materials"] = flags["source_material_ready_for_followup_draft"] and bool(candidates)
    return {"case_id": case["case_id"], "patient_key": case["patient_key"], "group": group, "split": case["split"],
            "flags": flags, "current_three_task_readiness": "not_established",
            "common_catalogue_drug_candidates": candidates, "latest_relevant_historical_labs": latest,
            "source_anchor_encounter_ref": ref,
            "pending": ["reviewed constructed current-visit context", "current medication reconciliation", "product mapping",
                        "allergy assessment, retaining unresolved unknowns", "independent clinical suitability review"]}


def audit(folder):
    folder = Path(folder).resolve()
    manifest_path = folder / "manifest_private.json"
    manifest = json.loads(manifest_path.read_text())
    rows, counts = [], defaultdict(Counter)
    for item in manifest["cases"]:
        result = screen(json.loads((folder / item["path"]).read_text()))
        result["source_case_path"] = str(folder / item["path"])
        rows.append(result)
        counts[result["group"]]["source_patients"] += 1
        counts[result["group"]].update(k for k, value in result["flags"].items() if value)
    report = {"version": VERSION, "source_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "case_count": len(rows), "counts_by_group": dict(counts),
        "selection_uses_model_outputs": False, "source_or_existing_scenario_mutated": False,
        "current_three_task_ready_confirmed": 0,
        "ready_meaning": "Source materials are available for drafting a prospective follow-up scenario; not confirmed clinical appropriateness, guaranteed prescribing, or three-task completion.",
        "time_windows": {"new_visit": "source discharge +24h", "windows_hours": [72, 168],
            "meaning": "Engineering availability windows measured from proposed visit; not universal clinical validity standards."},
        "common_catalogue_mapping": "Conservative proposal only; no PO/NG, PRN, variable dose, unidentified strength or inferred current use.",
        "limitations": ["Source inpatient diagnosis is not the new visit diagnosis", "Historical prescriptions are not discharge medication instructions",
            "No Note/ED/imaging/microbiology extracts were used", "Unknown allergy and current use retained", "Full lab safety and drug indications not independently reviewed"],
        "reference_urls": ["https://physionet.org/content/mimiciv/2.2/", "https://mimic.mit.edu/docs/iv/modules/hosp/prescriptions.html", "https://mimic.mit.edu/docs/iv/modules/hosp/labevents.html"]}
    save(folder / "source_eligibility_private.json", {"version": VERSION, "cases": rows})
    save(folder / "source_eligibility_summary.json", report)
    return report


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__);p.add_argument("--folder", required=True)
    print(json.dumps(audit(p.parse_args().folder), ensure_ascii=False))
