"""New, explicitly constructed follow-up previews; never overwrite source cases.

Selection is source-only and restricted to development patients. No run results
are loaded. Clinical review remains pending and prescribing is never required.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from ..cases import GROUPS, runtime_visible_case, write_case
from ..medication_catalogue import get_catalogue, get_formulary
from .cohort_eligibility import number
from .mimic_cohort import save


VERSION = "constructed-stable-followup-preview-v2-20261107"
NAMES = {"type2_diabetes": "2型糖尿病", "heart_failure": "心力衰竭", "copd": "慢阻肺", "pneumonia": "肺炎", "uti": "尿路感染"}


def source_conflicts(row, candidate):
    """Conservative construction gates, not a comprehensive clinical clearance."""
    labs = row["latest_relevant_historical_labs"]
    problems = []
    if candidate["catalogue_code"] == "SIM-TIOTROPIUM-18" and not candidate.get("product_identity_reference"):
        problems.append("inhaler_strength_basis_not_verified")
    required = {"type2_diabetes": ("creatinine", "glucose"), "heart_failure": ("creatinine", "potassium"), "copd": ()}.get(row["group"], ())
    bounds = {"creatinine": (0.3, 1.3, {"mg/dL"}), "potassium": (3.5, 5.0, {"mEq/L", "mmol/L"}), "glucose": (70, 250, {"mg/dL"})}
    for name in required:
        lab = labs.get(name, {})
        value = number(lab.get("numeric_value_raw"))
        low, high, units = bounds[name]
        if value is None or lab.get("unit_raw") not in units:
            problems.append(name + "_value_or_unit_not_supported")
        elif not low <= value <= high:
            problems.append(name + "_outside_conservative_preview_window")
    if row["group"] in {"pneumonia", "uti"}:
        problems.append("antibiotic_course_and_remaining_duration_need_independent_scenario_review")
    return problems


def build(row, candidate):
    path = Path(row["source_case_path"])
    source_bytes = path.read_bytes()
    source = json.loads(source_bytes)
    case = deepcopy(source)
    old_id = case["case_id"]
    case["case_id"] = old_id + "_followup_v2"
    product = next(x for x in get_catalogue() if x["drug_code"] == candidate["catalogue_code"])
    label = product["display_name_zh"]
    regimen = f"{label}，每次{candidate['dose']} {candidate['dose_unit']}，频次{candidate['frequency']}"
    if candidate["route"] == "inhaled":
        regimen += "；使用HANDIHALER装置吸入胶囊内的药粉，不吞胶囊"
    facts = [
        ("chief_complaint", "昨天出院，今天来复诊核对后续用药。我带来的药快用完了，想请医生判断是否继续以及怎么用。", "reported"),
        ("onset", "这次是出院后的用药复核，没有突然加重的新症状。", "reported"),
        ("past_medical_history", f"住院时听医生说过{NAMES[case['group']]}，其他病名我记得不全，可以查住院记录。", "reported"),
        ("medications", "我这次带来的药盒记录是：" + regimen + "。现在只剩少量，后续是否继续需要医生决定。", "reported"),
        ("other_medications", "以前住院还用过哪些临时药、是否还该继续，我说不全，需要对照医院记录核对。", "unknown"),
        ("allergy_history", "我记不清以前有没有明确药物过敏记录，请帮我核对；不能确定的地方我不会说没有。", "unknown"),
        ("symptom_progression", {"type2_diabetes": "现在能正常进食饮水，没有呕吐、意识改变或明显发抖出汗。",
             "heart_failure": "今天活动后的感觉和出院时相近，没有静息时胸痛或明显气促，脚踝没有突然比出院时更肿。",
             "copd": "呼吸和出院时差不多，没有突然明显气短、咯血或自觉发热。"}[case["group"]], "reported"),
    ]
    if case["group"] == "copd":
        facts.append(("device_availability", "我带着HANDIHALER装置，愿意演示使用方法；不明白时会请医生解释。", "reported"))
    case["patient_initial"]["patient_facts"] = [{"fact_id": f"followup-{i:03d}", "topic": topic, "field": topic,
        "value": value if status != "unknown" else None, "reported_expression": value if status == "unknown" else None,
        "status": status, "source_kind": "constructed", "source_refs": []} for i, (topic, value, status) in enumerate(facts, 1)]
    case["patient_initial"]["communication_profile"] = {"style": "自然口语，能念药盒记录，不熟悉的医学术语会请解释",
        "intentional_misreport": False, "automatic_age_memory_penalty": False}
    # These are new simulated measurements, never copied from a source lab or
    # presented as observed MIMIC outpatient data.
    vitals = [("blood_pressure", "122/76", "mmHg"), ("pulse", 76, "/min"),
              ("respiratory_rate", 16, "/min"), ("temperature", 36.7, "degC"),
              ("SpO2", 95 if case["group"] == "copd" else 97, "%")]
    case["his_initial"]["current_observations"] = [{"name": name, "value": value, "unit": unit,
        "source_kind": "constructed", "available_at": "physician_consultation"} for name, value, unit in vitals]
    case["formulary"] = get_formulary()  # identical 14-product catalogue in every group
    case["evaluation"] = {"clinical_reference_status": "pending_independent_review",
        "required_facts": deepcopy(case["patient_initial"]["patient_facts"]), "prescription_labels": None, "risk_labels": None,
        "prescribing_required": False, "acceptable_process_outcomes": ["prescribe", "revise", "stop", "defer", "refer"],
        "source_prescreen": row, "known_lab_conflict_checks": source_conflicts(row, candidate),
        "unresolved": ["other current medications not fully recalled", "allergy not established", "full clinical suitability pending",
                       "historical renal tests are not current measurements; eGFR is not fabricated"]}
    case["provenance"] = {"source_dataset": "MIMIC-IV 2.2", "uses_patient_records": True,
        "construction_version": VERSION, "original_case_id": old_id, "original_case_path": str(path),
        "original_case_sha256": hashlib.sha256(source_bytes).hexdigest(), "source_mode": "historical_seed",
        "constructed_fields": ["patient_initial.patient_facts", "patient_initial.communication_profile", "his_initial.current_observations"],
        "medication_relation": {"historical_order": candidate,
            "source_value_policy": "Original order and all source lab values remain unchanged in source history.",
            "constructed_relation": "Patient reports holding and continuing this historical-formulation medication after discharge; this is NOT a sourced discharge prescription or observed adherence.",
            "other_current_medicines": "unknown, not assumed absent"},
        "lab_policy": "All original labs retain source times; no lab copied or relabeled as a current measurement.",
        "selection_uses_model_outcomes": False, "clinical_plausibility": "pending_independent_review"}
    return case


def prepare(folder, out):
    folder, out = Path(folder).resolve(), Path(out).resolve()
    if (out / "manifest.json").exists():
        raise FileExistsError("Version output exists; preserve it and choose a new version directory")
    rows = json.loads((folder / "source_eligibility_private.json").read_text())["cases"]
    selected, unavailable, funnel = [], [], {}
    for group in GROUPS:
        group_rows = [x for x in rows if x["group"] == group]
        development = [x for x in group_rows if x["split"] == "development"]
        eligible = [x for x in development if x["flags"]["source_common_catalogue_candidate_with_materials"]]
        rejected = Counter(); pairs = []
        for row in sorted(eligible, key=lambda x: x["case_id"]):
            for candidate in sorted(row["common_catalogue_drug_candidates"], key=lambda x: x["source_order_ref"]):
                issues = source_conflicts(row, candidate)
                if issues:
                    rejected.update(issues)
                else:
                    pairs.append((row, candidate))
        funnel[group] = {"source_patients": len(group_rows), "development_patients": len(development),
            "development_with_source_and_parse_candidate": len(eligible), "passing_candidate_orders": len(pairs),
            "rejected_order_reasons": dict(rejected), "selection": "first opaque case ID then first source order ref; no model outcomes read"}
        if not pairs:
            unavailable.append({"group": group, "status": "not_ready", "reasons": dict(rejected) or {"no_development_source_and_parse_candidate": 1}})
            continue
        row, candidate = pairs[0]
        case = build(row, candidate)
        path = out / "cases" / (case["case_id"] + ".json")
        write_case(path, case)
        selected.append({"case_id": case["case_id"], "patient_key": case["patient_key"], "group": group,
            "data_kind": "mimic_derived", "split": "development", "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "status": "engineering_preview_independent_review_pending",
            "prescribing_required": False, "clinical_reference_status": "pending_independent_review"})
    save(out / "manifest.json", {"version": VERSION, "cases": selected, "case_count": len(selected), "not_ready": unavailable,
        "selection_uses_model_outputs": False, "clinical_reference_status": "pending_independent_review", "common_catalogue_items": 14})
    if selected:
        save(out / "single_case_manifest.json", {"version": VERSION, "cases": [selected[0]], "case_count": 1,
             "clinical_reference_status": "pending_independent_review"})
    save(out / "selection_funnel.json", {"version": VERSION, "by_group": funnel,
        "timing_window_origin": "new construction criteria, not claimed as existing paper thresholds",
        "lab_window_policy": "Availability windows are measured at proposed discharge+24h visit; not universal lab-validity rules.",
        "lab_conflict_gate_policy": "Conservative engineering preview windows only: creatinine0.3–1.3mg/dL, K3.5–5.0mEq/L or mmol/L, glucose70–250mg/dL when required. Values outside or units unknown are deferred; this is not a treatment guideline or proof of normality.",
        "catalogue_scope": "Low match rates can be catalogue/form/route/parser limitations; they are not disease feasibility estimates."})
    return {"written_previews": len(selected), "groups": [x["group"] for x in selected], "not_ready": unavailable,
            "manifest": str(out / "manifest.json"), "single_case_manifest": str(out / "single_case_manifest.json") if selected else None}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__);p.add_argument("--folder", required=True);p.add_argument("--out", required=True)
    a = p.parse_args();print(json.dumps(prepare(a.folder, a.out), ensure_ascii=False))
