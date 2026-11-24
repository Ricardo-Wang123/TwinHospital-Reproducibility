"""Stream selected historical seeds from MIMIC-IV files without extracting the ZIP.

This module does not select disease groups, infer current/home medication use,
construct a new complaint, or claim that a seed is ready for a clinical task.
Returned source rows remain dictionaries of strings. Source identifiers belong
in restricted local data, not in agent prompts or public examples.

``extract_seeds`` treats the selected admission as completed historical material:
the proposed new simulated visit is after its discharge. Source diagnoses have
no event timestamp; inclusion means "recorded for a completed admission", not
"known at arrival". A different admission-replay design needs a different cutoff
policy and must not reuse this extractor's role-visibility assumptions.
"""

from __future__ import annotations

import csv
import gzip
import io
import re
import zipfile
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator


class MimicSourceError(ValueError):
    """A source or selection error; messages never include a patient row or ID."""


_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?\Z")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_CORE_TABLES = (
    "patients", "admissions", "diagnoses_icd", "d_icd_diagnoses", "prescriptions"
)
_OPTIONAL_NOT_EXTRACTED = ("pharmacy", "emar", "emar_detail", "omr")


def validate_selections(selections) -> list[dict[str, str]]:
    """Validate explicit selections; one patient and one source admission per seed.

    No disease-code or quota inference is performed. The caller supplies the
    group after applying an independently specified cohort-selection protocol.
    """
    if not isinstance(selections, (list, tuple)) or not selections:
        raise MimicSourceError("selections must be a non-empty list of explicit selections")
    normalized = []
    subjects, admissions = set(), set()
    for number, selection in enumerate(selections, 1):
        if not isinstance(selection, dict):
            raise MimicSourceError(f"selection {number}: expected a dictionary")
        for field in ("subject_id", "hadm_id", "group"):
            value = selection.get(field)
            if not isinstance(value, str) or not value or value != value.strip():
                raise MimicSourceError(
                    f"selection {number}: {field} must be a non-empty, trimmed string"
                )
        if selection["subject_id"] in subjects:
            raise MimicSourceError(
                f"selection {number}: duplicate patient; select each patient only once"
            )
        if selection["hadm_id"] in admissions:
            raise MimicSourceError(f"selection {number}: duplicate source admission")
        subjects.add(selection["subject_id"])
        admissions.add(selection["hadm_id"])
        normalized.append({key: selection[key] for key in ("subject_id", "hadm_id", "group")})
    return normalized


def _timestamp(value: str, table: str, field: str, row_number: int) -> datetime:
    if not value or not _TIMESTAMP.fullmatch(value):
        raise MimicSourceError(f"{table} row {row_number}: missing or invalid {field} timestamp")
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise MimicSourceError(f"{table} row {row_number}: invalid {field} timestamp") from None


def _date(value: str, table: str, field: str, selection_number: int):
    if not _DATE.fullmatch(value):
        raise MimicSourceError(f"{table} selection {selection_number}: invalid {field} date")
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise MimicSourceError(f"{table} selection {selection_number}: invalid {field} date") from None


class _Source:
    def __init__(self, path):
        self.path = Path(path).expanduser()
        self.archive = None
        self.entries = defaultdict(list)

    def __enter__(self):
        if self.path.is_dir():
            self.kind = "directory"
            names = [p.relative_to(self.path).as_posix() for p in self.path.rglob("*") if p.is_file()]
        elif self.path.is_file() and zipfile.is_zipfile(self.path):
            self.kind = "zip"
            self.archive = zipfile.ZipFile(self.path)
            names = [item.filename for item in self.archive.infolist() if not item.is_dir()]
        else:
            raise MimicSourceError("source must be an existing CSV directory or ZIP archive")
        for name in sorted(names):
            if "__MACOSX" in Path(name).parts:
                continue
            filename = Path(name).name
            if filename.startswith("._"):
                continue
            if filename.endswith(".csv.gz"):
                table = filename[:-7]
            elif filename.endswith(".csv"):
                table = filename[:-4]
            else:
                continue
            self.entries[table].append(name)
        if not self.entries:
            self.__exit__(None, None, None)
            raise MimicSourceError("source contains no CSV or CSV.gz tables")
        return self

    def __exit__(self, *_):
        if self.archive is not None:
            self.archive.close()

    def _entry(self, table):
        matches = self.entries.get(table, [])
        if not matches:
            raise MimicSourceError(f"required table is missing: {table}")
        if len(matches) != 1:
            raise MimicSourceError(f"ambiguous table {table}: multiple CSV copies found")
        return matches[0]

    @contextmanager
    def _text(self, table):
        entry = self._entry(table)
        raw = self.archive.open(entry) if self.archive is not None else (self.path / entry).open("rb")
        with raw:
            if entry.endswith(".gz"):
                with gzip.GzipFile(fileobj=raw) as decompressed:
                    with io.TextIOWrapper(decompressed, encoding="utf-8-sig", newline="") as text:
                        yield text
            else:
                with io.TextIOWrapper(raw, encoding="utf-8-sig", newline="") as text:
                    yield text

    def header(self, table):
        with self._text(table) as stream:
            fields = next(csv.reader(stream), None)
        if not fields or any(not field for field in fields) or len(fields) != len(set(fields)):
            raise MimicSourceError(f"{table}: missing, empty, or duplicated CSV header")
        return fields

    def rows(self, table, required=()) -> Iterator[tuple[int, dict[str, str]]]:
        with self._text(table) as stream:
            reader = csv.DictReader(stream)
            fields = reader.fieldnames or []
            if not fields or any(not field for field in fields) or len(fields) != len(set(fields)):
                raise MimicSourceError(f"{table}: missing, empty, or duplicated CSV header")
            missing = set(required) - set(fields)
            if missing:
                raise MimicSourceError(f"{table}: missing required columns: {', '.join(sorted(missing))}")
            for number, row in enumerate(reader, 2):
                if None in row or any(value is None for value in row.values()):
                    raise MimicSourceError(f"{table} row {number}: CSV column count does not match header")
                yield number, row


def inspect_source(path) -> dict:
    """Return only file/module metadata and table headers, never patient records."""
    with _Source(path) as source:
        tables = {}
        for table in sorted(source.entries):
            relative_path = source._entry(table)
            parts = Path(relative_path).parts
            module = next((part for part in reversed(parts[:-1]) if part in {"hosp", "icu", "ed", "note"}), "unspecified")
            tables[table] = {
                "module": module,
                "relative_path": relative_path,
                "columns": source.header(table),
            }
        return {
            "source_kind": source.kind,
            "table_count": len(tables),
            "modules": sorted({table["module"] for table in tables.values()}),
            "tables": tables,
            "inspection_scope": "headers_only; no patient rows returned or counted",
        }


def _loaded():
    return {"status": "loaded", "rows": 0}


def _bump(coverage, table, key):
    item = coverage["tables"][table]
    item[key] = item.get(key, 0) + 1


def extract_seeds(path, selections, include_labs=False, *, skip_invalid_history=False) -> list[dict]:
    """Extract only explicitly selected patients as post-discharge historical seeds.

    Historical admissions must finish no later than the anchor discharge and
    start no later than the anchor admission. Prescription start times after the
    cutoff are excluded. Labs require a nonempty storetime at/before cutoff;
    missing storetime is counted as a coverage gap, not assigned a guessed time.

    All source values remain strings. Repeated diagnoses, orders and dictionary
    matches are preserved as lists. A selected anchor recording an in-hospital
    death is rejected. Missing/invalid required times and patient/admission
    ownership conflicts raise MimicSourceError without printing identifiers.
    With skip_invalid_history, malformed order and laboratory intervals are
    excluded and counted while patient/admission validation remains strict.
    """
    selections = validate_selections(selections)
    selected = {item["subject_id"]: item for item in selections}
    selection_number = {item["subject_id"]: n for n, item in enumerate(selections, 1)}
    anchor_owners = {item["hadm_id"]: item["subject_id"] for item in selections}
    patient_rows, admission_rows = {}, defaultdict(list)
    admission_owners, parsed_times = {}, {}

    with _Source(path) as source:
        for table in _CORE_TABLES + (("labevents", "d_labitems") if include_labs else ()):
            source._entry(table)

        for number, row in source.rows("patients", ("subject_id",)):
            subject = row["subject_id"]
            if subject not in selected:
                continue
            if subject in patient_rows:
                raise MimicSourceError(f"patients row {number}: duplicate selected patient record")
            patient_rows[subject] = row
        for subject in selected:
            if subject not in patient_rows:
                raise MimicSourceError(f"selection {selection_number[subject]}: patient not found")

        required_admission = ("subject_id", "hadm_id", "admittime", "dischtime", "deathtime", "hospital_expire_flag")
        for number, row in source.rows("admissions", required_admission):
            subject, admission = row["subject_id"], row["hadm_id"]
            if admission in anchor_owners and subject != anchor_owners[admission]:
                raise MimicSourceError(f"admissions row {number}: selected anchor belongs to another patient")
            if subject not in selected:
                continue
            if not admission:
                raise MimicSourceError(f"admissions row {number}: missing admission identifier")
            if admission in admission_owners:
                raise MimicSourceError(f"admissions row {number}: duplicate selected admission record")
            admission_owners[admission] = subject
            start = _timestamp(row["admittime"], "admissions", "admittime", number)
            end = _timestamp(row["dischtime"], "admissions", "dischtime", number)
            if end < start:
                raise MimicSourceError(f"admissions row {number}: discharge precedes admission")
            if row["deathtime"]:
                death = _timestamp(row["deathtime"], "admissions", "deathtime", number)
                if death < start:
                    raise MimicSourceError(f"admissions row {number}: death precedes admission")
            if row["hospital_expire_flag"] not in {"0", "1"}:
                raise MimicSourceError(f"admissions row {number}: missing or invalid hospital_expire_flag")
            parsed_times[admission] = (start, end)
            admission_rows[subject].append(row)

        seeds, by_subject, cutoffs, eligible_pairs = [], {}, {}, set()
        for selection in selections:
            subject = selection["subject_id"]
            anchor = next((r for r in admission_rows[subject] if r["hadm_id"] == selection["hadm_id"]), None)
            if anchor is None:
                raise MimicSourceError(f"selection {selection_number[subject]}: anchor admission not found")
            if anchor["hospital_expire_flag"] == "1" or anchor["deathtime"]:
                raise MimicSourceError(f"selection {selection_number[subject]}: anchor records death; cannot seed a later visit")
            anchor_start, cutoff = parsed_times[anchor["hadm_id"]]
            dod = patient_rows[subject].get("dod", "")
            if dod and _date(dod, "patients", "dod", selection_number[subject]) <= cutoff.date():
                raise MimicSourceError(f"selection {selection_number[subject]}: recorded death is not after anchor discharge")
            history = [r for r in admission_rows[subject]
                       if parsed_times[r["hadm_id"]][0] <= anchor_start
                       and parsed_times[r["hadm_id"]][1] <= cutoff]
            history.sort(key=lambda r: parsed_times[r["hadm_id"]])
            eligible_pairs.update((subject, row["hadm_id"]) for row in history)
            cutoffs[subject] = cutoff
            table_coverage = {table: _loaded() for table in _CORE_TABLES}
            for table in _OPTIONAL_NOT_EXTRACTED:
                table_coverage[table] = {"status": "omitted", "rows": None, "reason": "not_extracted_in_this_pass"}
            for table in ("labevents", "d_labitems"):
                table_coverage[table] = _loaded() if include_labs else {"status": "omitted", "rows": None, "reason": "include_labs_false"}
            table_coverage["patients"]["rows"] = 1
            table_coverage["admissions"].update(rows=len(history), excluded_outside_history=len(admission_rows[subject]) - len(history))
            seed = {
                "patient": patient_rows[subject], "anchor_admission": anchor,
                "admissions": history, "diagnoses": [], "prescriptions": [], "labs": [],
                "diagnosis_dictionary": [], "lab_dictionary": [],
                "coverage": {
                    "mode": "historical_seed", "cutoff": anchor["dischtime"],
                    "tables": table_coverage,
                    "assumptions": [
                        "New simulation occurs after anchor discharge; not an admission-arrival replay.",
                        "Diagnosis availability uses completed-admission provenance, not an observed diagnosis timestamp.",
                        "Prescription rows are orders, not administration evidence or current/home medication reports.",
                        "Source strings and unknown units are preserved; no drug normalization or clinical validation is performed.",
                        "Loaded zero rows differs from omitted; omitted tables must not be interpreted as no history.",
                        "Raw future-dated patient attributes and order end fields remain source metadata, not initial role-visible facts.",
                    ],
                },
            }
            by_subject[subject] = seed
            seeds.append(seed)

        def eligible(row, table, number):
            subject, admission = row["subject_id"], row["hadm_id"]
            if admission in admission_owners and admission_owners[admission] != subject:
                raise MimicSourceError(f"{table} row {number}: patient/admission ownership conflict")
            if subject not in selected:
                return False
            if not admission:
                if table == "labevents":
                    _bump(by_subject[subject]["coverage"], table, "excluded_unlinked_admission")
                    return False
                raise MimicSourceError(f"{table} row {number}: missing admission identifier")
            if admission not in admission_owners:
                raise MimicSourceError(f"{table} row {number}: selected patient record has no matching admission")
            if (subject, admission) not in eligible_pairs:
                _bump(by_subject[subject]["coverage"], table, "excluded_outside_history")
                return False
            return True

        used_codes = set()
        for number, row in source.rows("diagnoses_icd", ("subject_id", "hadm_id", "icd_code", "icd_version")):
            if not eligible(row, "diagnoses_icd", number):
                continue
            by_subject[row["subject_id"]]["diagnoses"].append(row)
            used_codes.add((row["icd_code"], row["icd_version"]))
        diagnoses_by_code = defaultdict(list)
        for _, row in source.rows("d_icd_diagnoses", ("icd_code", "icd_version", "long_title")):
            key = (row["icd_code"], row["icd_version"])
            if key in used_codes:
                diagnoses_by_code[key].append(row)
        for seed in seeds:
            codes = list(dict.fromkeys((r["icd_code"], r["icd_version"]) for r in seed["diagnoses"]))
            seed["diagnosis_dictionary"] = [row for key in codes for row in diagnoses_by_code[key]]
            seed["coverage"]["tables"]["d_icd_diagnoses"]["unresolved_code_count"] = sum(not diagnoses_by_code[key] for key in codes)

        for number, row in source.rows("prescriptions", ("subject_id", "hadm_id", "starttime")):
            if not eligible(row, "prescriptions", number):
                continue
            subject = row["subject_id"]
            coverage = by_subject[subject]["coverage"]
            try:
                start = _timestamp(row["starttime"], "prescriptions", "starttime", number)
                stop = _timestamp(row["stoptime"], "prescriptions", "stoptime", number) if row.get("stoptime") else None
                if stop is not None and stop < start:
                    raise MimicSourceError(f"prescriptions row {number}: stop precedes start")
            except MimicSourceError:
                if not skip_invalid_history:
                    raise
                _bump(coverage, "prescriptions", "excluded_invalid_interval")
                continue
            if start > cutoffs[subject]:
                _bump(coverage, "prescriptions", "excluded_start_after_cutoff")
                continue
            if stop is not None and stop > cutoffs[subject]:
                _bump(coverage, "prescriptions", "order_stop_after_cutoff_preserved")
            by_subject[subject]["prescriptions"].append(row)

        if include_labs:
            used_items = set()
            for number, row in source.rows("labevents", ("subject_id", "hadm_id", "itemid", "charttime", "storetime")):
                if not eligible(row, "labevents", number):
                    continue
                subject = row["subject_id"]
                coverage = by_subject[subject]["coverage"]
                if not row["storetime"]:
                    _bump(coverage, "labevents", "excluded_missing_storetime")
                    continue
                try:
                    stored = _timestamp(row["storetime"], "labevents", "storetime", number)
                    charted = _timestamp(row["charttime"], "labevents", "charttime", number)
                    if stored < charted:
                        raise MimicSourceError(f"labevents row {number}: storetime precedes charttime")
                except MimicSourceError:
                    if not skip_invalid_history:
                        raise
                    _bump(coverage, "labevents", "excluded_invalid_interval")
                    continue
                if stored > cutoffs[subject]:
                    _bump(coverage, "labevents", "excluded_storetime_after_cutoff")
                    continue
                by_subject[subject]["labs"].append(row)
                used_items.add(row["itemid"])
            lab_by_item = defaultdict(list)
            for _, row in source.rows("d_labitems", ("itemid", "label", "fluid", "category")):
                if row["itemid"] in used_items:
                    lab_by_item[row["itemid"]].append(row)
            for seed in seeds:
                items = list(dict.fromkeys(row["itemid"] for row in seed["labs"]))
                seed["lab_dictionary"] = [row for item in items for row in lab_by_item[item]]
                seed["coverage"]["tables"]["d_labitems"]["unresolved_item_count"] = sum(not lab_by_item[item] for item in items)

        for seed in seeds:
            for table, key in (("diagnoses_icd", "diagnoses"), ("d_icd_diagnoses", "diagnosis_dictionary"), ("prescriptions", "prescriptions")):
                seed["coverage"]["tables"][table]["rows"] = len(seed[key])
            if include_labs:
                seed["coverage"]["tables"]["labevents"]["rows"] = len(seed["labs"])
                seed["coverage"]["tables"]["d_labitems"]["rows"] = len(seed["lab_dictionary"])
        return seeds


__all__ = ["MimicSourceError", "validate_selections", "inspect_source", "extract_seeds"]
