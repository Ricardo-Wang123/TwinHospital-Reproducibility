"""Optional full-source laboratory-availability follow-up to scope counts.

The 72/168 h windows describe a prospective scenario-construction heuristic,
not a clinical recommendation or a new formal inclusion criterion. No values
are manufactured, and no patient is newly selected by this audit.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import timedelta
import json
from pathlib import Path

from .mimic_reader import _Source
from .mimic_cohort import timestamp, save, progress
from .cohort_eligibility import number
from ..cases import GROUPS


VERSION = "diagnosis-scope-laboratory-availability/20261107-v2"
LABS = {
    "creatinine": ({"Creatinine", "Creatinine, Whole Blood"}, "Blood"),
    "potassium": ({"Potassium", "Potassium, Whole Blood"}, "Blood"),
    "glucose": ({"Glucose", "Glucose, Whole Blood"}, "Blood"),
    "wbc": ({"White Blood Cells"}, "Blood"),
    "urinalysis": ({"Leukocytes", "Nitrite", "WBC", "Leuks", "LEUKS"}, "Urine"),
}
WINDOWS = {"type2_diabetes": {"creatinine": 168, "glucose": 72},
           "heart_failure": {"creatinine": 168, "potassium": 72},
           "copd": {}, "pneumonia": {"creatinine": 168, "wbc": 168},
           "uti": {"creatinine": 168, "urinalysis": 168}}


def has_result(row):
    # MIMIC may mask the free-text value as ___ while retaining valuenum.
    # A real source numeric field must not be thrown away with its masked text.
    if number(row.get("valuenum")) is not None:
        return True
    raw = (row.get("value") or "").strip()
    return bool(raw) and raw.casefold() not in {"___", "unknown", "null", "none", "not done", "cancelled"}


def relevant_rows(source, lookup, lab_kinds, output):
    """Full CSV scan, projecting only candidate stays and needed lab records."""
    with source._text("labevents") as stream:
        reader = csv.reader(stream)
        header = next(reader)
        if not header or len(header) != len(set(header)):
            raise ValueError("invalid laboratory header")
        indexes = {key: header.index(key) for key in ("subject_id", "hadm_id", "itemid")}
        for n, values in enumerate(reader, 2):
            if n % 20000000 == 0:
                progress(output, "reading_full_source_labevents", rows_scanned=n)
            if len(values) != len(header):
                raise ValueError("malformed laboratory CSV record")
            key = (values[indexes["subject_id"]], values[indexes["hadm_id"]])
            record = lookup.get(key)
            if record is None:
                continue
            if values[indexes["itemid"]] not in lab_kinds and record["any_valid_timed_lab"]:
                continue
            yield key, dict(zip(header, values))


def summarize(records):
    output = {}
    for scope in ("priority_1", "any_sequence"):
        groups = {}
        for group in GROUPS:
            selected = [r for r in records if group in r[scope] and r["survives_next_day"]]
            window_match = lambda r: all(kind in r["latest_lab_age_hours"] and r["latest_lab_age_hours"][kind] <= hours
                                         for kind, hours in WINDOWS[group].items())
            stages = {
                "has_any_valid_timed_laboratory_record": lambda r: r["any_valid_timed_lab"],
                "has_all_descriptive_historical_lab_windows": window_match,
                "usable_rx_and_historical_lab_windows": lambda r: r["usable_rx"] and window_match(r),
                "oral_or_inhaled_rx_and_historical_lab_windows": lambda r: (r["oral_rx"] or r["inhaled_rx"]) and window_match(r),
                "group_keyword_parsed_regimen_and_historical_lab_windows": lambda r: group in r["group_parsed_rx"] and window_match(r),
                "legacy_history_and_group_keyword_parsed_regimen_and_historical_lab_windows": lambda r: r["legacy_history_ok"] and group in r["group_parsed_rx"] and window_match(r),
            }
            counts = {}
            for stage, predicate in stages.items():
                rows = [r for r in selected if predicate(r)]
                counts[stage] = {"source_stays": len(rows), "unique_source_patients": len({r["subject_id"] for r in rows})}
            groups[group] = {"window_hours_at_constructed_next_day_visit": WINDOWS[group],
                             "no_required_lab_window_for_group": not bool(WINDOWS[group]), "counts": counts}
        output[scope] = groups
    return output


def run(source_path, scope_folder, output):
    source_path, scope_folder, output = Path(source_path), Path(scope_folder), Path(output)
    if (output / "aggregate_summary.json").exists():
        raise FileExistsError("finished laboratory audits are immutable; choose another output version")
    records = json.loads((scope_folder / "prescription_flags_private.json").read_text())["records"]
    lookup = {(r["subject_id"], r["hadm_id"]): r for r in records}
    timing = {}
    for key, r in lookup.items():
        end = timestamp(r["dischtime"])
        timing[key] = end, end + timedelta(days=1)
        r["latest_lab_age_hours"] = {}
        r["any_valid_timed_lab"] = False
    counters, dictionary = Counter(), []
    progress(output, "reading_source_laboratory_dictionary")
    with _Source(source_path) as source:
        lab_kinds = {}
        for _, row in source.rows("d_labitems"):
            kinds = [kind for kind, (names, fluid) in LABS.items() if row["label"] in names and row["fluid"] == fluid]
            if kinds:
                lab_kinds[row["itemid"]] = kinds
                dictionary.append({"itemid": row["itemid"], "label": row["label"], "fluid": row["fluid"], "kinds": kinds})
        progress(output, "reading_full_source_labevents", candidate_stays=len(records), selected_dictionary_items=len(dictionary))
        for key, row in relevant_rows(source, lookup, lab_kinds, output):
            r = lookup[key]
            chart, store = timestamp(row.get("charttime")), timestamp(row.get("storetime"))
            if chart is None or store is None:
                counters["target_rows_unknown_chart_or_store_time"] += 1
                continue
            end, new_visit = timing[key]
            if chart > end or store > end or store < chart:
                counters["target_rows_late_or_inconsistent_times"] += 1
                continue
            if not has_result(row):
                counters["target_rows_missing_result"] += 1
                continue
            r["any_valid_timed_lab"] = True
            counters["processed_target_valid_timed_result_rows"] += 1
            if (row.get("value") or "").strip() == "___" and number(row.get("valuenum")) is not None:
                counters["processed_rows_numeric_value_preserved_despite_masked_text"] += 1
            for kind in lab_kinds.get(row["itemid"], []):
                if kind != "urinalysis" and (number(row.get("valuenum")) is None or not row.get("valueuom", "").strip()):
                    counters["selected_lab_rows_nonnumeric_or_no_unit"] += 1
                    continue
                age = (new_visit - chart).total_seconds() / 3600
                if age < r["latest_lab_age_hours"].get(kind, float("inf")):
                    r["latest_lab_age_hours"][kind] = age
    save(output / "lab_flags_private.json", {"version": VERSION, "records": records})
    summary = {"version": VERSION, "counts": summarize(records), "lab_dictionary_items": dictionary,
               "scan_counters": dict(counters), "source_rows_mutated": False, "patient_assignment_performed": False,
               "scan_counter_scope": "all chosen lab item rows; other lab rows only until a valid timed result is found for that stay; counters are not counts of all source lab rows",
               "v1_correction": "numeric valuenum remains valid when free-text value is masked as ___; v1 laboratory counts invalidated and retained separately; original cohort conversion and prescription counts unaffected",
               "model_results_read": False, "time_policy": "charttime and storetime known; chart <= store <= source discharge; age relative to source discharge +24h",
               "encounter_link": "same explicit subject_id and hadm_id; rows without a linked hadm_id are not imputed",
               "window_status": "scenario-construction availability heuristic, not original-paper criterion, clinical validity threshold or final qualification",
               "urinalysis_status": "any listed urine leukocyte/WBC/nitrite result, qualitative or quantitative; not proof of infection or culture/susceptibility",
               "current_measurement_inferred": False, "clinical_values_selected_for_favorable_result": False,
               "clinical_eligibility": "pending, not established by source availability"}
    save(output / "aggregate_summary.json", summary)
    progress(output, "source_laboratory_availability_count_complete", candidate_stays=len(records))
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--scope-folder", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    result = run(args.source, args.scope_folder, args.out)
    print(json.dumps({"version": result["version"], "counts": result["counts"]}, ensure_ascii=False))
