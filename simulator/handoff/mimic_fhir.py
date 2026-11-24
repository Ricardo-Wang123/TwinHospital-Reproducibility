"""Attach a published MIMIC-IV on FHIR history to a prepared TwinHospital scenario."""
from __future__ import annotations

import argparse
from collections import defaultdict
import csv
from datetime import datetime
import gzip
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from sandbox.experiments.linked.cases import VERSION, validate_case
from sandbox.experiments.linked.mimic_fhir import (
    CASE_VERSION, RELEASE, indexed, instant, read_patient, source_id, with_diagnosis_displays)


def native_rows(root, table, subject):
    if root is None:
        return
    for name in (f"{table}.csv.gz", f"{table}.csv", f"hosp_{table}.csv"):
        path = root / name
        if path.is_file():
            opener = gzip.open if name.endswith(".gz") else open
            with opener(path, "rt", newline="") as stream:
                for row in csv.DictReader(stream):
                    if str(row["subject_id"]) == str(subject):
                        yield row
            return


def source_index(registry, resources, native, zone):
    ids = registry["source_identifier_map"]
    index = defaultdict(dict)
    def key(resource_type, namespace, token):
        return resource_type + "/" + source_id(namespace, token)
    for ref, number in ids["encounters"].items():
        index[key("Encounter", "Encounter", number)]["encounter_ref"] = ref
    for ref, evidence in registry["source_rows"].items():
        row, table = evidence["row"], evidence["table"]
        if table == "diagnoses_icd":
            target = key("Condition", "Condition", f"{row['hadm_id']}-{row['seq_num']}-{row['icd_code']}")
            index[target].update(source_ref=ref, diagnosis_sequence=int(row["seq_num"]))
        elif table == "admissions":
            index[key("Encounter", "Encounter", row["hadm_id"])]["source_ref"] = ref
        elif table == "labevents":
            index[key("Observation", "ObservationLabevents", row["labevent_id"])]["source_ref"] = ref
        elif table == "prescriptions" and row.get("pharmacy_id"):
            target = key("MedicationRequest", "MedicationRequest", row["pharmacy_id"])
            index[target].setdefault("source_ref", ref)
            index[target].setdefault("source_refs", []).append(ref)
            names = index[target].setdefault("source_drug_names", [])
            if row.get("drug") and row["drug"] not in names:
                names.append(row["drug"])
    def timestamp(raw):
        if len(raw) == 10:
            return raw
        stamp = datetime.fromisoformat(raw)
        return (stamp if stamp.tzinfo else stamp.replace(tzinfo=zone)).isoformat()
    def store_latest(target, raw):
        if not raw or target not in resources:
            return
        new = timestamp(raw)
        old = index[target].get("available_at")
        # With dates of mixed precision, an un-timed same-day result is known only after that day.
        if old is None or (new[:10], len(new) == 10, new) > (old[:10], len(old) == 10, old):
            index[target]["available_at"] = new
    subject = ids["subject_id"]
    for row in native_rows(native, "pharmacy", subject):
        store_latest(key("MedicationDispense","MedicationDispense",row["pharmacy_id"]), row.get("verifiedtime"))
    admin_times = {r["emar_id"]: r.get("storetime") for r in native_rows(native, "emar", subject)}
    for row in native_rows(native, "emar_detail", subject):
        if row.get("parent_field_ordinal"):
            target = key("MedicationAdministration", "MedicationAdministration",
                         f"{row['emar_id']}-{row['parent_field_ordinal']}")
            store_latest(target, admin_times.get(row["emar_id"]))
    unknown = set()
    for row in native_rows(native, "microbiologyevents", subject):
        specimen, test, org = row["micro_specimen_id"], row.get("test_itemid"), row.get("org_itemid")
        if not test:
            continue
        tokens = [("ObservationMicroTest", f"{specimen}-{test}")]
        if org:
            tokens.append(("ObservationMicroOrg", f"{test}-{specimen}-{org}"))
        if row.get("ab_itemid"):
            tokens.append(("ObservationMicroSusc", row["microevent_id"]))
        for ns, token in tokens:
            target = key("Observation", ns, token)
            date = row.get("storetime") or row.get("storedate")
            if not date:
                unknown.add(target)
            store_latest(target, date)
    for target in unknown:
        if target in index:
            index[target].pop("available_at", None)
    return {name: row for name, row in index.items() if name in resources and row}


def attach(case, registry, bundle, terminology, native=None, source_timezone="UTC"):
    if case.get("schema_version") != VERSION or case.get("data_kind") != "mimic_derived":
        raise ValueError("Expected the original, MIMIC-derived scenario")
    ids = registry["source_identifier_map"]
    bundle, displays = with_diagnosis_displays(bundle, terminology)
    resources = indexed(bundle)
    anchor = "Encounter/" + source_id("Encounter", ids["anchor_hadm_id"])
    patient = "Patient/" + source_id("Patient", ids["subject_id"])
    if (ids["patient_key"] != case["patient_key"] or patient not in resources or anchor not in resources
            or resources[anchor]["subject"]["reference"] != patient):
        raise ValueError("The scenario, source index and FHIR history must identify the same patient and anchor")
    if case["his_initial"].get("time_basis", {}).get("history_offset_origin") != "source_anchor_discharge":
        raise ValueError("The prepared visit must use the source anchor discharge as its time origin")
    zone = ZoneInfo(source_timezone)
    raw_cutoff = datetime.fromisoformat(registry["source_cutoff"])
    if instant(resources[anchor]["period"]["end"]) != (raw_cutoff if raw_cutoff.tzinfo else raw_cutoff.replace(tzinfo=zone)):
        raise ValueError("Source cutoff or ETL timezone differs from the FHIR encounter discharge")
    replaced = {"historical_encounters", "historical_orders", "historical_observations",
                "historical_administrations", "historical_microbiology", "historical_procedures", "historical_dispenses"}
    history = {k:v for k,v in case["his_initial"].items() if k not in replaced}
    history.update(fhir_history=bundle, fhir_anchor=anchor, fhir_source_index=source_index(registry, resources, native, zone))
    result = {**case, "schema_version": CASE_VERSION, "his_initial": history, "provenance": {**case["provenance"],
              "history_exchange": {"dataset": RELEASE, "fhir_version": "4.0.1", "guide_version": "1.3.0",
                                   "source_timezone": source_timezone, "scope": "historical MIMIC resources",
                                   "source_diagnosis_displays": displays}}}
    validate_case(result)
    return result


def write_private(path, document):
    with open(path, "x", encoding="utf-8", opener=lambda name, flags: os.open(name, flags, 0o600)) as f:
        json.dump(document, f, ensure_ascii=False, indent=2, allow_nan=False)
        f.write("\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("case", "source-index", "output", "terminology"):
        parser.add_argument("--" + name, type=Path, required=True)
    sources = parser.add_mutually_exclusive_group(required=True)
    sources.add_argument("--resources", type=Path, help="Directory of MIMIC-IV on FHIR profile NDJSON files")
    sources.add_argument("--bundle", type=Path, help="Single-patient collection created by this importer")
    parser.add_argument("--hosp", type=Path, help="Optional native hospital tables for source availability timestamps")
    parser.add_argument("--history-output", type=Path, help="Also save the standardized history as a separate collection")
    parser.add_argument("--source-timezone", default="UTC", help="Timezone used by the published SQL conversion")
    args = parser.parse_args()
    registry = json.loads(args.source_index.read_bytes())
    bundle = (json.loads(args.bundle.read_bytes()) if args.bundle
              else read_patient(args.resources, registry["source_identifier_map"]["subject_id"]))
    case = attach(json.loads(args.case.read_bytes()), registry, bundle, args.terminology, args.hosp, args.source_timezone)
    if args.history_output and (args.history_output == args.output or args.history_output.exists() or args.output.exists()):
        parser.error("Provide two distinct, unused output paths")
    write_private(args.output, case)
    if args.history_output:
        write_private(args.history_output, case["his_initial"]["fhir_history"])


if __name__ == "__main__":
    main()
