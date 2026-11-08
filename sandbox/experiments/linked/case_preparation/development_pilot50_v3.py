"""Private, outcome-independent any-sequence development pilot preparation.

No API calls or run-result files are read. Source patients remain development
only, with an explicit exclusion of every patient in the frozen test partition.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from ..cases import GROUPS
from .diagnosis_scope_labs import WINDOWS
from .mimic_cohort import assign_patients, rank, save, extract_private_cache

VERSION = "development-pilot50-anyseq-closed-scenario-v3-20261107"
SYSTEMS = {"type2_diabetes": "endocrine_metabolic", "heart_failure": "circulatory",
           "copd": "respiratory", "pneumonia": "respiratory", "uti": "genitourinary"}
REFERENCES = [
    "https://mimic.mit.edu/docs/iv/modules/hosp/diagnoses_icd.html",
    "https://www.nice.org.uk/guidance/ng5/chapter/Recommendations",
    "https://www.nice.org.uk/guidance/cg183/chapter/Recommendations",
    "https://www.nice.org.uk/guidance/ng106/chapter/recommendations/",
    "https://www.nice.org.uk/guidance/ng28/chapter/Blood-glucose-management",
    "https://www.nice.org.uk/guidance/NG115/chapter/recommendations",
    "https://www.nice.org.uk/guidance/NG250/chapter/recommendations",
    "https://www.nice.org.uk/guidance/ng109/chapter/Recommendations",
    "https://www.nice.org.uk/guidance/NG111/chapter/recommendations",
    "https://www.nice.org.uk/guidance/ng113/chapter/recommendations",
]
PROTOCOL = {
    "version": VERSION,
    "purpose": "development inputs; not formal test-set qualification or evidence of treatment safety",
    "target": {g: 10 for g in GROUPS}, "system_map": SYSTEMS,
    "diagnosis": "same original five ICD families, any recorded diagnosis sequence; historical classification only",
    "source_required": ["adult completed admission", "survives discharge +24h",
        "same admission: MAIN order, explicit PO/IH/INH/NEB, positive scalar dose and daily frequency, product strength",
        "group-related ingredient keyword only as a material-availability screen, not a treatment indication",
        "same admission historical lab availability windows; preserve ALL values and original times"],
    "descriptive_lab_windows_hours": WINDOWS,
    "window_status": "development scene-construction criterion, not paper-prespecified or universal clinical validity",
    "favorable_lab_values_selected": False,
    "source_history_requirement": "anchor itself supplies recorded history; an earlier admission is not required",
    "catalogue_requirement": "none; unknown products remain unmapped and are reported separately",
    "patient_assignment": "exclude all frozen test patients; one patient per group assignment; deterministic keyed maximum matching; no model-output access",
    "anchor_assignment": "deterministic keyed ranking among source-qualified stays, independent of clinical result values",
    "constructed_visit": "24h after source discharge; explicitly simulated medication review, not observed MIMIC outpatient visit",
    "constructed_current_facts": "closed common and disease-specific observable symptom slots; last 24h medication actions and supplies; source allergy/comorbidity unknowns retained",
    "antibiotic_course": "known historical orders are not discharge instructions; define actual constructed postdischarge doses, keep intended total course unknown unless explicitly authored in a separately reviewed scenario; do not invent susceptibility, imaging, infection site or cure",
    "infection_decisions": "reassess current symptoms, prior antibiotic exposure, allergy, renal and culture limitations; stop/defer/change/refer are permitted; no mandatory antibiotic prescription",
    "conflicts": "preserve all source diagnoses; identify known incompatible assumptions for review, never normalize history/labs or erase comorbidities",
    "clinical_reference_status": "pending_independent_review",
    "source_privacy": "raw source identifiers/registry private; only opaque IDs in runtime case",
    "references": REFERENCES,
}


def window_ok(row, group):
    return all(k in row["latest_lab_age_hours"] and row["latest_lab_age_hours"][k] <= hours
               for k, hours in WINDOWS[group].items())


def select(records, frozen, key, per_group=10):
    tests = {x["subject_id"] for x in frozen if x["split"] == "test"}
    existing_dev = {x["subject_id"] for x in frozen if x["split"] == "development"}
    if tests & existing_dev:
        raise ValueError("frozen cohort already has patient split overlap")
    candidates = {g: defaultdict(list) for g in GROUPS}
    funnel = {}
    for group in GROUPS:
        rows = [r for r in records if group in r["any_sequence"]]
        stages = [("adult_completed_with_anyseq_diagnosis", lambda r: True),
                  ("survives_next_day", lambda r: r["survives_next_day"]),
                  ("group_related_scalar_regimen", lambda r: group in r["group_parsed_rx"]),
                  ("descriptive_history_lab_windows", lambda r: window_ok(r, group)),
                  ("excluding_all_frozen_test_patients", lambda r: r["subject_id"] not in tests)]
        funnel[group] = {}
        for name, predicate in stages:
            rows = [r for r in rows if predicate(r)]
            funnel[group][name] = {"stays": len(rows), "patients": len({r["subject_id"] for r in rows})}
        for row in rows:
            candidates[group][row["subject_id"]].append(row)
    assigned = assign_patients(candidates, {g: per_group for g in GROUPS}, key)
    selections = []
    for group in GROUPS:
        for sid in sorted(assigned[group], key=lambda p: rank(key, VERSION + group + p)):
            anchor = min(candidates[group][sid], key=lambda r: rank(key, VERSION + ":anchor:" + r["hadm_id"]))
            selections.append({**deepcopy(anchor), "group": group, "system": SYSTEMS[group], "split": "development",
                "previous_assignment": "existing_development" if sid in existing_dev else "new_development_patient"})
    if len({r["subject_id"] for r in selections}) != len(selections) or any(r["subject_id"] in tests for r in selections):
        raise AssertionError("patient isolation failed")
    return selections, {"version": VERSION, "funnel": funnel,
        "selected_by_group": dict(Counter(r["group"] for r in selections)),
        "selected_by_system": dict(Counter(r["system"] for r in selections)),
        "selected_previous_assignment": dict(Counter(r["previous_assignment"] for r in selections)),
        "unique_selected_patients": len(selections), "frozen_test_patients_excluded": len(tests),
        "overlap_with_test": 0, "cross_group_selected_patient_overlap": 0,
        "source_readiness": "material availability only", "clinical_reference_status": "pending_independent_review",
        "model_results_read": False}


def prepare(source, private_root, out):
    private_root, out = Path(private_root), Path(out)
    out.mkdir(parents=True, exist_ok=True, mode=0o700); out.chmod(0o700)
    frozen_path = private_root / "mimic_batch/selection_private.json"
    lab_path = private_root / "diagnosis_scope_labs_v2/lab_flags_private.json"
    key = (private_root / "mimic_batch/identity.key").read_bytes()
    protocol_path = out / "protocol_frozen_before_selection.json"
    protocol_payload = {"protocol": PROTOCOL, "new_case_model_calls_at_freeze": 0,
        "frozen_2500_selection_sha256": hashlib.sha256(frozen_path.read_bytes()).hexdigest(),
        "lab_flags_v2_sha256": hashlib.sha256(lab_path.read_bytes()).hexdigest(),
        "implementation_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    if protocol_path.exists():
        # Resume extraction, but never silently change selection or protocol.
        if json.loads(protocol_path.read_text())["protocol"] != PROTOCOL:
            raise ValueError("protocol changed; choose a new version")
        selected = json.loads((out / "selection_private.json").read_text())
    else:
        save(protocol_path, protocol_payload)
        selected, summary = select(json.loads(lab_path.read_text())["records"], json.loads(frozen_path.read_text()), key)
        save(out / "selection_private.json", selected)
        save(out / "selection_summary.json", summary)
    extract_private_cache(source, out, selected)
    return json.loads((out / "selection_summary.json").read_text())


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", required=True); p.add_argument("--private-root", required=True); p.add_argument("--out", required=True)
    a = p.parse_args()
    print(json.dumps(prepare(a.source, a.private_root, a.out), ensure_ascii=False))
