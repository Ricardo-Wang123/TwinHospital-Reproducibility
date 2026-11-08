"""Audit-only disease/medication taxonomy sidecars; never rewrites a case.

The four systems describe historical source diagnosis codes. They are neither
new outpatient diagnoses nor a claim that every patient is clinically eligible.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

from .mimic_cohort import PROTOCOL, save

REGISTRY_PATH = Path(__file__).with_name("classification_v1.json")


def registry():
    return json.loads(REGISTRY_PATH.read_text())


def classify_source_code(version, code):
    """Match the exact frozen five-subtype scope, keeping ICD versions separate."""
    if not isinstance(code, str):
        raise TypeError("ICD codes must remain strings")
    version, code = str(version), code.strip().upper().replace(".", "")
    for group, spec in registry()["source_subtypes"].items():
        rule = spec["frozen_selection"]
        if code in rule.get(version + "_exact", []) or any(
                code.startswith(prefix) for prefix in rule.get(version + "_prefix", [])):
            return {"source_subtype": group, "source_system": spec["system"],
                    "icd_version": version, "icd_code": code,
                    "scope": "historical_coded_source_stratum"}
    return None


def classify_case(case):
    anchors = [x for x in case["his_initial"]["historical_encounters"]
               if x.get("role") == "anchor_historical_stay"]
    if len(anchors) != 1:
        raise ValueError("expected exactly one source anchor")
    diagnoses = [x for x in anchors[0].get("diagnoses", []) if x.get("source_sequence") == 1]
    if len(diagnoses) != 1:
        raise ValueError("expected exactly one priority-1 source diagnosis")
    diagnosis = diagnoses[0]
    coding = diagnosis["coding"]
    result = classify_source_code(coding["version"], coding["code"])
    if result is None or result["source_subtype"] != case["group"]:
        raise ValueError("source code does not agree with the frozen stratum")
    return {**result, "source_diagnosis_record_ref": diagnosis["record_id"],
            "source_dictionary_title": diagnosis["display"],
            "source_sequence": 1, "sequence_meaning": "discharge_billing_priority",
            "new_outpatient_diagnosis_inferred": False}


def audit(folder, output):
    folder, output = Path(folder), Path(output)
    if output.exists():
        raise FileExistsError("taxonomy outputs are immutable; choose a new output directory")
    reg = registry()
    expected = {group: value["frozen_selection"] for group, value in reg["source_subtypes"].items()}
    if PROTOCOL["groups"] != expected:
        raise ValueError("taxonomy no longer matches the frozen source selection protocol")
    manifest_path = folder / "manifest_private.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    source_counts, system_counts, version_counts, code_counts = Counter(), Counter(), Counter(), Counter()
    public_titles, case_records = {}, []
    seen_patients, seen_cases = set(), set()
    for item in manifest["cases"]:
        source_path = (folder / item["path"]).resolve()
        if not source_path.is_relative_to(folder.resolve()):
            raise ValueError("case path escapes the private cohort")
        raw = source_path.read_bytes()
        case = json.loads(raw)
        if case["case_id"] in seen_cases or case["patient_key"] in seen_patients:
            raise ValueError("expected unique source patients and case IDs")
        if any(case[key] != item[key] for key in ("case_id", "patient_key", "group", "split")):
            raise ValueError("case metadata differs from frozen manifest")
        seen_patients.add(case["patient_key"])
        seen_cases.add(case["case_id"])
        classified = classify_case(case)
        group, system, split = case["group"], classified["source_system"], case["split"]
        version, code = classified["icd_version"], classified["icd_code"]
        source_counts[(group, split)] += 1
        system_counts[(system, split)] += 1
        version_counts[(group, version)] += 1
        code_counts[(group, version, code)] += 1
        public_titles[(group, version, code)] = classified["source_dictionary_title"]
        case_records.append({"case_id": case["case_id"], "split": split,
                             "source_case_sha256": hashlib.sha256(raw).hexdigest(), **classified})
    if manifest_path.read_bytes() != manifest_bytes:
        raise RuntimeError("source manifest changed during audit")
    common = {"version": reg["version"], "registry_sha256": hashlib.sha256(REGISTRY_PATH.read_bytes()).hexdigest(),
              "source_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
              "source_protocol_version": PROTOCOL["version"], "source_case_count": len(case_records),
              "source_mutated": False, "split_reassigned": False,
              "classification_uses_model_results": False,
              "clinical_eligibility": "not_established_by_taxonomy"}
    private = {**common, "cases": case_records}
    summary = {**common,
        "source_patients_unique": len(seen_patients),
        "four_systems_balanced": False,
        "by_system": [{"system": key, "label_zh": spec["label_zh"],
                       "total": sum(system_counts[(key, split)] for split in ("development", "test")),
                       "development": system_counts[(key, "development")], "test": system_counts[(key, "test")]}
                      for key, spec in reg["disease_systems"].items()],
        "by_source_subtype": [{"group": key, "label_zh": spec["label_zh"],
                               "total": sum(source_counts[(key, split)] for split in ("development", "test")),
                               "development": source_counts[(key, "development")], "test": source_counts[(key, "test")]}
                              for key, spec in reg["source_subtypes"].items()],
        "by_icd_version": [{"group": g, "icd_version": v, "count": n} for (g, v), n in sorted(version_counts.items())],
        "by_exact_code": [{"group": g, "icd_version": v, "icd_code": c, "count": n,
                           "source_dictionary_title": public_titles[(g, v, c)]}
                          for (g, v, c), n in sorted(code_counts.items())],
        "interpretation": reg["reporting_limits"]}
    save(output / "taxonomy_private_sidecar.json", private)
    save(output / "taxonomy_aggregate_summary.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cohort", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    summary = audit(args.cohort, args.out)
    print(json.dumps({key: summary[key] for key in ("version", "source_case_count", "by_system", "source_mutated", "split_reassigned")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
