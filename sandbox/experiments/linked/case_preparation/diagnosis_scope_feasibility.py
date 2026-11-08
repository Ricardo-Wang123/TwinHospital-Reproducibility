"""Full-source count only: priority-1 versus any-sequence coded history.

No patient is assigned, no frozen input changes, and no model result is read.
Intermediate identifiers stay in a private output folder. Only aggregates print.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path

from .mimic_reader import _Source
from .mimic_cohort import code_group, disease_code, personal_history, timestamp, usable_order, save, progress
from .cohort_eligibility import DRUG_TERMS, number
from ..cases import GROUPS


VERSION = "diagnosis-scope-source-feasibility/20261107-v1"


def summarize(records, frozen_patients):
    output = {}
    for scope in ("priority_1", "any_sequence"):
        groups = {}
        all_patients = set()
        for group in GROUPS:
            selected = [r for r in records if group in r[scope]]
            all_patients.update(r["subject_id"] for r in selected)
            stages = {
                "adult_completed_stay_with_diagnosis": lambda r: True,
                "survives_constructed_next_day": lambda r: r["survives_next_day"],
                "same_stay_usable_main_prescription": lambda r: r["survives_next_day"] and r["usable_rx"],
                "same_stay_explicit_oral_prescription": lambda r: r["survives_next_day"] and r["oral_rx"],
                "same_stay_explicit_inhaled_prescription": lambda r: r["survives_next_day"] and r["inhaled_rx"],
                "same_stay_oral_or_inhaled_prescription": lambda r: r["survives_next_day"] and (r["oral_rx"] or r["inhaled_rx"]),
                "same_stay_numeric_dose_frequency_and_strength": lambda r: r["survives_next_day"] and r["parsed_oral_inhaled_rx"],
                "same_stay_group_keyword_oral_inhaled_prescription": lambda r: r["survives_next_day"] and group in r["group_drug_rx"],
                "same_stay_group_keyword_parsed_regimen": lambda r: r["survives_next_day"] and group in r["group_parsed_rx"],
                "legacy_history_requirement_plus_usable_prescription": lambda r: r["survives_next_day"] and r["usable_rx"] and r["legacy_history_ok"],
                "legacy_history_requirement_plus_group_keyword_parsed_regimen": lambda r: r["survives_next_day"] and group in r["group_parsed_rx"] and r["legacy_history_ok"],
            }
            counts = {}
            for label, predicate in stages.items():
                matching = [r for r in selected if predicate(r)]
                patients = {r["subject_id"] for r in matching}
                counts[label] = {"unique_source_patients": len(patients), "source_stays": len(matching),
                                 "patients_not_in_frozen_2500": len(patients - frozen_patients)}
            groups[group] = counts
        output[scope] = {"groups": groups, "unique_patients_across_groups_at_first_stage": len(all_patients),
                         "group_patient_counts_overlap": True}
    return output


def run(source_path, output, frozen_selection):
    source_path, output, frozen_selection = Path(source_path), Path(output), Path(frozen_selection)
    frozen_bytes = frozen_selection.read_bytes()
    frozen_patients = {r["subject_id"] for r in json.loads(frozen_bytes)}
    population_path = output / "candidate_stays_private.json"
    if population_path.exists():
        payload = json.loads(population_path.read_text())
        if payload["version"] != VERSION or payload["source_size"] != source_path.stat().st_size:
            raise ValueError("private cache version/source mismatch")
        records = payload["records"]
    else:
        progress(output, "source_scope_reading_demographics")
        with _Source(source_path) as source:
            patients = {r["subject_id"]: (int(r["anchor_age"]), int(r["anchor_year"]), r.get("dod"))
                        for _, r in source.rows("patients")}
            visits = {}
            for _, r in source.rows("admissions"):
                start, end = timestamp(r["admittime"]), timestamp(r["dischtime"])
                if start is None or end is None or end < start:
                    continue
                age, anchor_year, dod = patients[r["subject_id"]]
                if age + start.year - anchor_year < 18:
                    continue
                visit = (r["subject_id"], r["hadm_id"])
                survives = not (r["hospital_expire_flag"] == "1" or r.get("deathtime"))
                if dod and datetime.fromisoformat(dod).date() <= (end + timedelta(days=1)).date():
                    survives = False
                visits[visit] = {"subject_id": visit[0], "hadm_id": visit[1],
                    "admittime": r["admittime"], "dischtime": r["dischtime"], "survives_next_day": survives}
            personal_keys = set()
            for _, r in source.rows("d_icd_diagnoses"):
                key = (r["icd_version"], r["icd_code"].replace(".", "").upper())
                if personal_history(*key, r["long_title"]):
                    personal_keys.add(key)
            recorded, first_discharge, targets = set(), {}, {}
            progress(output, "source_scope_reading_all_diagnosis_sequences", adult_completed_stays=len(visits))
            for _, r in source.rows("diagnoses_icd"):
                visit = (r["subject_id"], r["hadm_id"])
                if visit not in visits:
                    continue
                code, version = r["icd_code"].replace(".", "").upper(), r["icd_version"]
                if (version, code) in personal_keys:
                    recorded.add(visit)
                if r["seq_num"] == "1" and disease_code(version, code):
                    end = visits[visit]["dischtime"]
                    if visit[0] not in first_discharge or end < first_discharge[visit[0]]:
                        first_discharge[visit[0]] = end
                group = code_group(version, code)
                if group:
                    if visit not in targets:
                        targets[visit] = {**visits[visit], "priority_1": set(), "any_sequence": set()}
                    targets[visit]["any_sequence"].add(group)
                    if r["seq_num"] == "1":
                        targets[visit]["priority_1"].add(group)
            records = []
            for visit, r in targets.items():
                r["priority_1"], r["any_sequence"] = sorted(r["priority_1"]), sorted(r["any_sequence"])
                r["legacy_history_ok"] = visit in recorded or first_discharge.get(visit[0], "9999") < r["admittime"]
                r.update({"usable_rx": False, "oral_rx": False, "inhaled_rx": False,
                          "parsed_oral_inhaled_rx": False, "group_drug_rx": [], "group_parsed_rx": []})
                records.append(r)
            save(population_path, {"version": VERSION, "source_size": source_path.stat().st_size, "records": records})
            del patients, visits, recorded, first_discharge, targets
    ready = output / "prescription_scan_complete.json"
    if ready.exists():
        records = json.loads((output / "prescription_flags_private.json").read_text())["records"]
        counters = json.loads(ready.read_text())
    else:
        lookup = {(r["subject_id"], r["hadm_id"]): r for r in records}
        discharge = {key: timestamp(r["dischtime"]) for key, r in lookup.items()}
        counters = Counter()
        progress(output, "source_scope_reading_prescriptions", target_stays=len(records),
                 target_patients=len({r["subject_id"] for r in records}))
        with _Source(source_path) as source:
            for n, row in source.rows("prescriptions"):
                if n % 2000000 == 0:
                    progress(output, "source_scope_reading_prescriptions", rows_scanned=n)
                key = (row["subject_id"], row["hadm_id"])
                r = lookup.get(key)
                if r is None or not usable_order(row):
                    continue
                start, stop = timestamp(row["starttime"]), timestamp(row["stoptime"])
                if start is None or start > discharge[key] or (stop is not None and stop < start):
                    counters["invalid_target_order_time_rows"] += 1
                    continue
                counters["usable_main_prescription_rows"] += 1
                r["usable_rx"] = True
                route = row["route"].strip().upper()
                oral, inhaled = route == "PO", route in {"IH", "INH", "NEB"}
                r["oral_rx"] |= oral
                r["inhaled_rx"] |= inhaled
                if not (oral or inhaled):
                    continue
                dose, daily = number(row["dose_val_rx"]), number(row.get("doses_per_24_hrs"))
                parsed = bool(dose is not None and dose > 0 and daily is not None and daily > 0 and row.get("prod_strength", "").strip())
                r["parsed_oral_inhaled_rx"] |= parsed
                name = row["drug"].casefold()
                for group in r["any_sequence"]:
                    if any(term in name for term in DRUG_TERMS[group]):
                        if group not in r["group_drug_rx"]:
                            r["group_drug_rx"].append(group)
                        if parsed and group not in r["group_parsed_rx"]:
                            r["group_parsed_rx"].append(group)
        save(output / "prescription_flags_private.json", {"version": VERSION, "records": records})
        save(ready, dict(counters))
    if frozen_selection.read_bytes() != frozen_bytes:
        raise RuntimeError("frozen selection changed during count")
    result = {"version": VERSION, "source": "MIMIC-IV 2.2 full local ZIP", "source_size_bytes": source_path.stat().st_size,
              "frozen_selection_sha256": hashlib.sha256(frozen_bytes).hexdigest(),
              "source_codes": "same five narrow code families as frozen cohort; only diagnosis priority restriction differs",
              "counts": summarize(records, frozen_patients), "scan_counters": dict(counters),
              "patient_assignment_performed": False, "frozen_cases_mutated": False, "model_outputs_read": False,
              "oral_route_definition": "PO exactly; PO/NG and mixed routes not silently treated as oral",
              "inhaled_route_definition": ["IH", "INH", "NEB"],
              "numeric_regimen_definition": "positive single numeric dose and daily-frequency plus nonempty product-strength field; exact product and schedule not thereby validated",
              "drug_keyword_groups": DRUG_TERMS,
              "keyword_matching_limits": "non-exhaustive descriptive ingredient-name matching; not normalized drug coding, indication validation or suitability",
              "completeness_not_established": ["recent clinically appropriate labs (not scanned in this count)", "allergy reconciliation", "current medication use", "note/imaging information", "constructed current-visit suitability", "independent clinical review"],
              "historical_prescription_meaning": "prescribed in source stay, not administered/current or discharge prescription",
              "no_readiness_claim": "counts describe available historical source materials, not guaranteed completed three-task cases"}
    save(output / "aggregate_summary.json", result)
    progress(output, "source_scope_count_complete", groups=list(GROUPS), source_mutated=False)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--frozen-selection", required=True)
    args = parser.parse_args()
    result = run(args.source, args.out, args.frozen_selection)
    print(json.dumps({"version": result["version"], "counts": result["counts"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
