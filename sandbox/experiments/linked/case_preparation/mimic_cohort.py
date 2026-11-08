"""Private, resumable MIMIC-IV cohort preparation; no network/model calls.

Run with --out outside the repository. Only aggregate progress is printed.
Source identifiers and rows are stored in a 0700 directory / 0600 files.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from collections import Counter, defaultdict
from datetime import datetime, timedelta
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time

from .mimic_reader import _Source
from .case_contract import build_case, validate_package, prepare_views
from ..cases import GROUPS, build_synthetic_case, runtime_visible_case, write_case


PROTOCOL = {
    "version": "five-group-historical-seed/20261107-v1",
    "source": "MIMIC-IV 2.2", "principal_diagnosis": "seq_num=1",
    "groups": {"type2_diabetes": {"10_prefix": ["E11"]},
               "heart_failure": {"9_prefix": ["428"], "10_prefix": ["I50"]},
               "copd": {"10_prefix": ["J44"]},
               "pneumonia": {"9_exact": ["486"], "10_exact": ["J189"]},
               "uti": {"9_exact": ["5990"], "10_exact": ["N390"]}},
    "required": ["adult at source admission", "completed source stay",
                 "no in-stay death and survival past constructed next-day visit",
                 "same-encounter MAIN order with name, positive dose, unit and route",
                 "same-stay personal-history code or earlier completed disease/injury admission"],
    "new_encounter": "constructed outpatient visit one day after source discharge",
    "source_cutoff": "source discharge; future records excluded",
    "target_per_group": 500, "development_per_group": 100, "test_per_group": 400,
    "assignment": "deterministic keyed ranking plus augmenting-path patient matching",
    "patient_split": "source patient and all derivatives stay in one partition",
    "clinical_eligibility": "pending independent clinical review; source availability is not clinical validation",
}


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)
        f.write("\n")
    tmp.replace(path)
    path.chmod(0o600)


def progress(out, phase, **values):
    record = {"phase": phase, "updated_unix": time.time(), **values}
    save(out / "progress.json", record)
    print(json.dumps(record, ensure_ascii=False), flush=True)


def verify_source_identity(path, out):
    """A resumed cache must refer to the same bytes, not just the same filename."""
    path = Path(path).resolve()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    identity = {"source_name": path.name, "size_bytes": path.stat().st_size,
                "sha256": digest.hexdigest(), "version": "2.2"}
    target = out / "source_identity.json"
    if target.exists() and json.loads(target.read_text()) != identity:
        raise RuntimeError("source bytes changed; refuse to reuse the existing cohort cache")
    save(target, identity)
    return identity


def timestamp(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def code_group(version, code):
    code = code.upper().replace(".", "")
    for group, rules in PROTOCOL["groups"].items():
        if code in rules.get(version + "_exact", []):
            return group
        if any(code.startswith(p) for p in rules.get(version + "_prefix", [])):
            return group
    return None


def disease_code(version, code):
    if version == "9":
        p = code[:3]
        return p.isdigit() and (1 <= int(p) <= 779 or 800 <= int(p) <= 999)
    return version == "10" and bool(re.fullmatch(r"(?:[A-QST][0-9][0-9A-Z][A-Z0-9]*|U071)", code))


def personal_history(version, code, title):
    return ("personal history" in title.lower() and code != "Z87891"
            and ((version == "9" and code[:3] in {"V10", "V11", "V12", "V13"})
                 or (version == "10" and code[:3] in {"Z85", "Z86", "Z87"})))


def usable_order(row):
    if row.get("drug_type", "").upper() != "MAIN":
        return False
    missing = {"", "-", "?", "___", "n/a", "na", "none", "null", "unknown", "unspecified"}
    if any(row.get(k, "").strip().lower() in missing for k in ("drug", "dose_unit_rx", "route")):
        return False
    dose = row.get("dose_val_rx", "").strip()
    return not dose.startswith(("-", "−")) and any(float(v) > 0 for v in re.findall(r"(?:\d+(?:\.\d*)?|\.\d+)", dose))


def rank(key, value):
    return hmac.new(key, value.encode(), hashlib.sha256).hexdigest()


def assign_patients(candidates, quotas, key):
    """Maximum bipartite matching with group capacity, deterministic ordering."""
    choices = {g: sorted(candidates[g], key=lambda p: rank(key, g + ":" + p)) for g in GROUPS}
    assigned = {g: set() for g in GROUPS}
    owner = {}

    def add(group, visited_groups, visited_patients):
        if group in visited_groups:
            return False
        visited_groups.add(group)
        for patient in choices[group]:
            if patient in visited_patients or owner.get(patient) == group:
                continue
            visited_patients.add(patient)
            previous = owner.get(patient)
            if previous is None or add(previous, visited_groups, visited_patients):
                if previous is not None:
                    assigned[previous].remove(patient)
                assigned[group].add(patient)
                owner[patient] = group
                return True
        return False

    for group in sorted(GROUPS, key=lambda g: len(choices[g])):
        for _ in range(quotas[group]):
            if not add(group, set(), set()):
                break
    return assigned


def select_cohort(source_path, out, key):
    existing = out / "selection_private.json"
    if existing.exists():
        return json.loads(existing.read_text())
    progress(out, "scanning_patients")
    with _Source(source_path) as source:
        patients = {r["subject_id"]: r for _, r in source.rows("patients")}
        visits = {}
        for _, row in source.rows("admissions"):
            patient = patients[row["subject_id"]]
            start, end = timestamp(row["admittime"]), timestamp(row["dischtime"])
            if start is None or end is None or end < start:
                continue
            age = int(patient["anchor_age"]) + start.year - int(patient["anchor_year"])
            if age >= 18:
                visits[(row["subject_id"], row["hadm_id"])] = row
        progress(out, "scanning_diagnoses", source_patients=len(patients), adult_stays=len(visits))
        personal_keys = set()
        for _, row in source.rows("d_icd_diagnoses"):
            keycode = (row["icd_version"], row["icd_code"].replace(".", "").upper())
            if personal_history(*keycode, row["long_title"]):
                personal_keys.add(keycode)
        groups = {g: set() for g in GROUPS}
        recorded = set()
        first_discharge = {}
        for _, row in source.rows("diagnoses_icd"):
            visit = (row["subject_id"], row["hadm_id"])
            if visit not in visits:
                continue
            code = row["icd_code"].replace(".", "").upper()
            version = row["icd_version"]
            if (version, code) in personal_keys:
                recorded.add(visit)
            if row["seq_num"] == "1":
                group = code_group(version, code)
                if group:
                    groups[group].add(visit)
                if disease_code(version, code):
                    end = visits[visit]["dischtime"]
                    if visit[0] not in first_discharge or end < first_discharge[visit[0]]:
                        first_discharge[visit[0]] = end
        target_visits = set().union(*groups.values())
        with_order = set()
        invalid_time_orders = 0
        progress(out, "scanning_prescriptions", principal_stays_by_group={g: len(v) for g, v in groups.items()})
        for n, row in source.rows("prescriptions"):
            visit = (row["subject_id"], row["hadm_id"])
            if visit not in target_visits or visit in with_order or not usable_order(row):
                continue
            start, stop = timestamp(row["starttime"]), timestamp(row["stoptime"])
            end = timestamp(visits[visit]["dischtime"])
            if start is None or start > end or (stop is not None and stop < start):
                invalid_time_orders += 1
                continue
            with_order.add(visit)
        candidates = {g: defaultdict(list) for g in GROUPS}
        stats = {}
        for group in GROUPS:
            eligible = []
            rejected = Counter()
            for visit in groups[group] & with_order:
                row, patient = visits[visit], patients[visit[0]]
                history_ok = visit in recorded or (visit[0] in first_discharge and first_discharge[visit[0]] < row["admittime"])
                if not history_ok:
                    rejected["no_required_structured_history"] += 1
                    continue
                if row["hospital_expire_flag"] == "1" or row.get("deathtime"):
                    rejected["anchor_records_death"] += 1
                    continue
                simulation_day = (timestamp(row["dischtime"]) + timedelta(days=1)).date()
                if patient.get("dod") and datetime.fromisoformat(patient["dod"]).date() <= simulation_day:
                    rejected["death_by_constructed_visit"] += 1
                    continue
                eligible.append(visit)
                candidates[group][visit[0]].append(visit[1])
            stats[group] = {"principal_with_usable_order_patients": len({p for p, h in groups[group] & with_order}),
                            "qualified_source_patients": len(candidates[group]), "qualified_source_stays": len(eligible),
                            "excluded_stays": dict(rejected)}
        assignments = assign_patients(candidates, {g: 500 for g in GROUPS}, key)
        selections = []
        for group in GROUPS:
            ordered = sorted(assignments[group], key=lambda p: rank(key, "split:" + p))
            # Reserve the planned test quota; development shortfalls remain explicit.
            test_count = min(400, len(ordered))
            for n, patient in enumerate(ordered):
                source_stay = min(candidates[group][patient], key=lambda h: rank(key, "anchor:" + h))
                selections.append({"subject_id": patient, "hadm_id": source_stay, "group": group,
                                   "split": "test" if n < test_count else "development"})
            stats[group]["selected_test"] = test_count
            stats[group]["selected_development"] = len(ordered) - test_count
            stats[group]["source_target_shortfall"] = max(0, 500 - len(ordered))
        report = {"protocol": PROTOCOL, "groups": stats, "selected_source_patients": len(selections),
                  "unique_source_patients": len({r["subject_id"] for r in selections}),
                  "invalid_target_order_time_rows_skipped": invalid_time_orders,
                  "formal_eligible": False, "qualification": "source availability only; scenario/rule/reference checks pending"}
        save(out / "cohort_summary.json", report)
        save(existing, selections)
        progress(out, "source_selection_complete", selected=len(selections), groups=stats)
        return selections


def extract_private_cache(source_path, out, selections, *, include_labs=True):
    database = out / "source_cache.sqlite"
    ready = out / "source_cache_complete.json"
    if database.exists() and ready.exists():
        return database
    if database.exists():
        raise RuntimeError("incomplete source cache exists; inspect before replacing")
    selected = {x["subject_id"]: x for x in selections}
    db = sqlite3.connect(database)
    database.chmod(0o600)
    db.execute("CREATE TABLE rows (table_name TEXT, subject TEXT, payload TEXT)")
    db.execute("CREATE INDEX rows_by_subject ON rows(subject,table_name)")
    db.execute("CREATE TABLE dictionary (table_name TEXT, code TEXT, version TEXT, payload TEXT)")
    coverage = Counter()
    excluded = Counter()
    buffer = []

    def put(table, subject, row):
        buffer.append((table, subject, json.dumps(row, ensure_ascii=False)))
        coverage[table] += 1
        if len(buffer) >= 2000:
            db.executemany("INSERT INTO rows VALUES (?,?,?)", buffer)
            db.commit()
            buffer.clear()

    with _Source(source_path) as source:
        for _, row in source.rows("patients"):
            if row["subject_id"] in selected:
                put("patients", row["subject_id"], row)
        admissions = defaultdict(list)
        anchors = {}
        for _, row in source.rows("admissions"):
            if row["subject_id"] in selected:
                admissions[row["subject_id"]].append(row)
                if row["hadm_id"] == selected[row["subject_id"]]["hadm_id"]:
                    anchors[row["subject_id"]] = row
        eligible = set()
        cutoffs = {}
        for subject, rows in admissions.items():
            anchor = anchors[subject]
            cutoff = timestamp(anchor["dischtime"])
            cutoffs[subject] = cutoff
            for row in rows:
                start, end = timestamp(row["admittime"]), timestamp(row["dischtime"])
                if start is None or end is None or end < start or start > timestamp(anchor["admittime"]) or end > cutoff:
                    excluded["admissions_outside_or_invalid_history"] += 1
                    continue
                eligible.add((subject, row["hadm_id"]))
                put("admissions", subject, row)
        tables = ["diagnoses_icd", "prescriptions", "pharmacy"] + (["labevents"] if include_labs else [])
        for table in tables:
            progress(out, "extracting_" + table, retained_rows=dict(coverage), excluded_rows=dict(excluded))
            for n, row in source.rows(table):
                subject = row.get("subject_id")
                if subject not in selected:
                    continue
                if (subject, row.get("hadm_id")) not in eligible:
                    excluded[table + "_unlinked_or_outside_history"] += 1
                    continue
                cutoff = cutoffs[subject]
                if table in {"prescriptions", "pharmacy"}:
                    start, stop = timestamp(row.get("starttime")), timestamp(row.get("stoptime"))
                    if start is None or start > cutoff or (stop is not None and stop < start):
                        excluded[table + "_invalid_or_future_start"] += 1
                        continue
                elif table == "labevents":
                    chart, stored = timestamp(row.get("charttime")), timestamp(row.get("storetime"))
                    if chart is None or stored is None or stored < chart or stored > cutoff:
                        excluded["labevents_invalid_missing_or_future_availability"] += 1
                        continue
                put(table, subject, row)
                if n % 2_000_000 == 0:
                    progress(out, "extracting_" + table, scanned=n, retained_rows=dict(coverage))
        for table, fields in [("d_icd_diagnoses", ("icd_code", "icd_version"))] + ([("d_labitems", ("itemid", None))] if include_labs else []):
            dictionaries = []
            for _, row in source.rows(table):
                dictionaries.append((table, row[fields[0]], row[fields[1]] if fields[1] else "", json.dumps(row)))
            db.executemany("INSERT INTO dictionary VALUES (?,?,?,?)", dictionaries)
        if buffer:
            db.executemany("INSERT INTO rows VALUES (?,?,?)", buffer)
        db.commit()
    db.close()
    save(ready, {"retained_rows": dict(coverage), "excluded_rows": dict(excluded), "include_labs": include_labs,
                 "omitted_tables": ["emar", "emar_detail", "omr", "ICU event tables", "Note", "ED"],
                 "omission_means": "not loaded, not absent"})
    return database


def make_packages(out, selections, key, database):
    db = sqlite3.connect(database)
    cache_metadata = json.loads((out / "source_cache_complete.json").read_text())
    dictionary = {(code, version): json.loads(payload) for code, version, payload in
                  db.execute("SELECT code,version,payload FROM dictionary WHERE table_name='d_icd_diagnoses'")}
    labs_dict = {code: json.loads(payload) for code, payload in
                 db.execute("SELECT code,payload FROM dictionary WHERE table_name='d_labitems'")}
    records, errors, completeness = [], Counter(), Counter()
    for index, selection in enumerate(selections):
        rows = defaultdict(list)
        for table, payload in db.execute("SELECT table_name,payload FROM rows WHERE subject=?", (selection["subject_id"],)):
            rows[table].append(json.loads(payload))
        anchor = next(r for r in rows["admissions"] if r["hadm_id"] == selection["hadm_id"])
        used_codes = {(r["icd_code"], r["icd_version"]) for r in rows["diagnoses_icd"]}
        used_items = {r["itemid"] for r in rows["labevents"]}
        coverage_tables = {table: {"status": "loaded", "rows": len(values)} for table, values in rows.items()}
        for table in cache_metadata.get("omitted_tables", []):
            coverage_tables[table] = {"status": "omitted", "rows": None, "reason": "not_extracted"}
        if not cache_metadata.get("include_labs"):
            for table in ("labevents", "d_labitems"):
                coverage_tables[table] = {"status": "omitted", "rows": None, "reason": "skip_labs"}
        seed = {"patient": rows["patients"][0], "anchor_admission": anchor,
                "admissions": rows["admissions"], "diagnoses": rows["diagnoses_icd"],
                "prescriptions": rows["prescriptions"], "labs": rows["labevents"],
                "diagnosis_dictionary": [dictionary[k] for k in used_codes if k in dictionary],
                "lab_dictionary": [labs_dict[k] for k in used_items if k in labs_dict],
                "coverage": {"mode": "historical_seed", "tables": coverage_tables}}
        package = build_case(seed, selection["group"], key)
        package["scenario"]["new_visit"]["elapsed_days_after_source_discharge"] = 1
        # The scenario is independently constructed and explicitly distinguished
        # from source facts. Historical source orders are never current-use labels.
        constructed = build_synthetic_case(selection["group"], index, split=selection["split"])
        for fact in constructed["patient_initial"]["patient_facts"]:
            if fact["topic"] == "onset":
                fact["value"] = "出院后约半天开始注意到这些变化，今天来复诊"
        constructed["evaluation"]["required_facts"] = deepcopy(constructed["patient_initial"]["patient_facts"])
        field_map = {"medications": "current_medication", "allergy_history": "allergy", "symptom_progression": "symptom"}
        package["scenario"]["patient_facts"] = [dict(fact, field=field_map.get(fact["field"], fact["field"]))
                                                     for fact in constructed["patient_initial"]["patient_facts"]]
        issues = validate_package(package)
        if issues:
            for issue in issues:
                errors[issue] += 1
            continue
        source_case = package["case"]
        clinical = source_case["clinical"]
        cid = source_case["case_id"]
        folder = out / "cases" / cid
        for name, value in package.items():
            save(folder / (name + ".json"), value)
        save(folder / "supplementary_pharmacy_private.json", rows["pharmacy"])
        age_info = clinical["demographics"]["age_at_source_admission"]
        simulated_year = (timestamp(anchor["dischtime"]) + timedelta(days=1)).year
        current_age = (int(seed["patient"]["anchor_age"]) + simulated_year - int(seed["patient"]["anchor_year"])) if age_info["status"] == "approximate" else None
        demographics = {"age": current_age, "age_status": age_info["status"],
                        "source_admission_age": age_info, "sex": clinical["demographics"]["sex_recorded"],
                        "age_time_basis": "approximate at constructed next-day visit; 90+ remains nonnumeric"}
        linked = {"schema_version": "twinhospital.linked.case/1", "case_id": cid,
            "patient_key": source_case["patient_key"], "group": selection["group"],
            "data_kind": "mimic_derived", "split": selection["split"],
            "patient_initial": {"demographics": deepcopy(demographics),
                                "patient_facts": constructed["patient_initial"]["patient_facts"],
                                "communication_profile": constructed["patient_initial"]["communication_profile"]},
            "his_initial": {"demographics": deepcopy(demographics),
                "time_basis": {"history_offset_origin": "source_anchor_discharge",
                               "new_visit_offset_hours": 24, "source_mode": "historical_seed",
                               "meaning": "Offset 0 belongs to yesterday's source discharge, not to the new outpatient encounter."},
                "historical_encounters": clinical["history"]["encounters"],
                "historical_orders": clinical["medications"]["orders"],
                "historical_observations": clinical["observations"],
                "allergy_record": clinical["allergies"],
                "current_use_record": {"status": "not_reconciled", "items": []},
                "current_observations": []},
            "formulary": [],
            "evaluation": {"clinical_reference_status": "pending_independent_review",
                           "required_facts": constructed["evaluation"]["required_facts"],
                           "prescription_labels": None, "risk_labels": None},
            "provenance": {"source_dataset": "MIMIC-IV 2.2", "uses_patient_records": True,
                           "private_package_path": str(folder), "source_mode": "historical_seed"}}
        write_case(folder / "linked_case.json", linked)
        # This may be loaded only in an authorized environment. It is not an
        # assertion that a third-party endpoint is allowed to receive this file.
        save(folder / "runtime_input.json", runtime_visible_case(linked))
        readiness = {"source_import": "passed", "patient_and_history_views": "prepared",
                     "scenario": "constructed_pending_review", "external_api_allowed": False,
                     "clinical_reference": "pending_independent_review", "native_drug_mapping": "pending",
                     "current_observations": "not_supplied", "formal_eligible": False}
        save(folder / "readiness.json", readiness)
        completeness["has_completed_historical_stay"] += bool(clinical["history"]["encounters"])
        completeness["has_earlier_stay_in_addition_to_anchor"] += len(clinical["history"]["encounters"]) > 1
        completeness["has_historical_orders"] += bool(clinical["medications"]["orders"])
        completeness["has_historical_labs"] += bool(clinical["observations"])
        completeness["has_pharmacy_records"] += bool(rows["pharmacy"])
        completeness["has_order_with_daily_dose_count"] += any(r.get("doses_per_24_hrs", "").strip() for r in rows["prescriptions"])
        completeness["has_pharmacy_frequency"] += any(r.get("frequency", "").strip() for r in rows["pharmacy"])
        completeness["has_ndc_or_gsn_or_formulary_code"] += any(any(r.get(k, "").strip() for k in ("ndc", "gsn", "formulary_drug_cd")) for r in rows["prescriptions"])
        completeness["topcoded_age_90_plus"] += age_info["status"] == "90_plus"
        records.append({"case_id": cid, "patient_key": linked["patient_key"], "group": selection["group"],
                        "split": selection["split"], "path": "cases/" + cid + "/linked_case.json",
                        "source_history_encounters": len(clinical["history"]["encounters"]),
                        "source_orders": len(clinical["medications"]["orders"]),
                        "source_observations": len(clinical["observations"]), "formal_eligible": False})
        if (index + 1) % 100 == 0:
            progress(out, "writing_source_packages", processed=index + 1, written=len(records))
    db.close()
    manifest = {"schema_version": "twinhospital.cohort/1", "protocol": PROTOCOL,
                "cases": records, "case_count": len(records), "rejected_packages": dict(errors),
                "formal_eligible": False, "external_api_allowed": False}
    save(out / "manifest_private.json", manifest)
    pilot_records = []
    for group in GROUPS:
        pilot_records.extend([r for r in records if r["group"] == group and r["split"] == "development"][:10])
    save(out / "pilot50_private_manifest.json", {
        "case_count": len(pilot_records), "data_kind": "mimic_derived",
        "external_api_allowed": False, "cases": pilot_records,
        "purpose": "local development selection; no transmission until data-handling requirements verified"})
    summary = {"converted_cases": len(records), "by_group_split": dict(Counter(r["group"] + ":" + r["split"] for r in records)),
               "unique_source_patients": len({r["patient_key"] for r in records}),
               "history_encounters": sum(r["source_history_encounters"] for r in records),
               "historical_orders": sum(r["source_orders"] for r in records),
               "historical_observations": sum(r["source_observations"] for r in records),
               "rejected_packages": dict(errors), "formal_eligible": False,
               "case_field_coverage_counts": dict(completeness),
               "source_cache": cache_metadata,
               "source_limitations": ["No MIMIC-Note or MIMIC-ED supplied", "eMAR/OMR/ICU event tables not extracted in this preparation pass",
                                      "historical orders do not establish current medication use or actual administration",
                                      "new outpatient complaint/onset/current self-report are constructed",
                                      "clinical plausibility and clinical reference labels await independent review"],
               "pending": ["independent scenario/reference review", "drug normalization/native mapping",
                           "verified third-party model data handling", "current encounter observations when required"]}
    save(out / "conversion_summary.json", summary)
    progress(out, "conversion_complete", **summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--skip-labs", action="store_true")
    parser.add_argument("--selection-only", action="store_true")
    args = parser.parse_args()
    out = Path(args.out).resolve()
    repo = Path(__file__).resolve().parents[4]
    if out == repo or repo in out.parents:
        raise SystemExit("patient outputs must remain outside the repository")
    os.umask(0o077)
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    out.chmod(0o700)
    key_file = out / "identity.key"
    if not key_file.exists():
        fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(secrets.token_bytes(32))
    key = key_file.read_bytes()
    verify_source_identity(Path(args.source), out)
    selections = select_cohort(Path(args.source), out, key)
    if not args.selection_only:
        db = extract_private_cache(Path(args.source), out, selections, include_labs=not args.skip_labs)
        make_packages(out, selections, key, db)


if __name__ == "__main__":
    main()
