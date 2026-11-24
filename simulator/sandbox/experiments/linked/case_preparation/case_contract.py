"""Compact, source-grounded case package for an offline MIMIC import.

No model or HIS calls occur here.  ``provenance`` is private: it contains raw
source identifiers and rows and must never be sent to a working agent.
Scenario additions have the following explicit contract::

    {"fact_id": "...", "field": "chief_complaint", "value": "...",
     "status": "reported", "source_kind": "constructed", "source_refs": []}

The source visit is a completed historical stay.  It is not the new simulated
encounter, and its discharge codes/orders do not become patient memories.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import hmac
import json
from collections import defaultdict


VERSION = "twinhospital.case/0.2"
DATA_KINDS = {"mimic_derived", "engineering_fixture"}
SCENARIO_FIELDS = {
    "chief_complaint", "onset", "symptom", "current_medication", "allergy",
    "past_medical_history",
}
RAW_IDENTIFIER_KEYS = {
    "subject_id", "hadm_id", "stay_id", "labevent_id", "emar_id", "pharmacy_id",
    "poe_id", "order_provider_id", "provider_id", "caregiver_id",
}
RAW_REGIMEN_FIELDS = (
    "prod_strength", "form_rx", "dose_val_rx", "dose_unit_rx", "form_val_disp",
    "form_unit_disp", "doses_per_24_hrs", "route", "frequency",
)


def _text(value):
    if value is None:
        return None
    value = str(value)
    return value if value.strip() else None


def _dt(value):
    if not _text(value):
        return None
    try:
        return datetime.fromisoformat(str(value).strip())
    except (ValueError, TypeError):
        return None


def _integer(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _opaque(secret, kind, value):
    digest = hmac.new(secret, (kind + ":" + _canonical(value)).encode(),
                      hashlib.sha256).hexdigest()[:24]
    return kind + "_" + digest


def _hours(value, cutoff):
    parsed = _dt(value)
    return round((parsed - cutoff).total_seconds() / 3600, 6) if parsed else None


def _nonempty(value):
    return value is not None and value != "" and value != [] and value != {}


def _is_opaque(value, prefix):
    return (isinstance(value, str) and value.startswith(prefix + "_")
            and len(value) == len(prefix) + 25
            and all(ch in "0123456789abcdef" for ch in value[len(prefix) + 1:]))


def _source_time(row, cutoff, event_key, recorded_key=None):
    event = _dt(row.get(event_key))
    recorded = _dt(row.get(recorded_key)) if recorded_key else event
    return {
        "basis": "source_relative_to_anchor_discharge",
        "event_offset_hours": _hours(row.get(event_key), cutoff),
        "recorded_offset_hours": _hours(row.get(recorded_key), cutoff) if recorded_key else None,
        "available_by_source_cutoff": recorded is not None and recorded <= cutoff,
        "availability_basis": recorded_key or event_key,
        "event_time_known": event is not None,
    }


def build_case(seed, group, secret: bytes, *, data_kind="mimic_derived"):
    """Build clinical/source/scenario/evaluation documents from a selected seed.

    Input rows are CSV dictionaries.  Codes and raw regimen strings are never
    coerced to numbers.  Invalid or out-of-window rows are retained privately
    with an exclusion reason, not silently attached to another encounter.
    """
    if not isinstance(secret, bytes) or not secret:
        raise ValueError("secret must be non-empty bytes; do not use an unsalted identifier hash")
    if data_kind not in DATA_KINDS:
        raise ValueError("unsupported data_kind")
    if not isinstance(group, str) or not group.strip():
        raise ValueError("group must be an explicit non-empty protocol label")
    patient = deepcopy(seed.get("patient") or {})
    anchor = deepcopy(seed.get("anchor_admission") or {})
    subject = _text(patient.get("subject_id"))
    anchor_id = _text(anchor.get("hadm_id"))
    cutoff = _dt(anchor.get("dischtime"))
    anchor_start = _dt(anchor.get("admittime"))
    if not subject or not anchor_id or subject != _text(anchor.get("subject_id")):
        raise ValueError("patient and anchor must identify the same source patient")
    if cutoff is None or anchor_start is None or cutoff < anchor_start:
        raise ValueError("anchor must be a completed source admission with valid times")
    if _integer(anchor.get("hospital_expire_flag")) == 1 or _text(anchor.get("deathtime")):
        raise ValueError("a source admission ending in death cannot seed this later-visit scenario")

    def enc_ref(hadm):
        return _opaque(secret, "enc", [subject, str(hadm)])

    patient_key = _opaque(secret, "pt", subject)
    anchor_ref = enc_ref(anchor_id)
    case_id = _opaque(secret, "case", [subject, anchor_id, group])
    registry = {}
    occurrences = defaultdict(int)
    excluded = []

    def register(table, row):
        key = (table, _canonical(row))
        ordinal = occurrences[key]
        occurrences[key] += 1
        ref = _opaque(secret, "src", [table, row, ordinal])
        registry[ref] = {"table": table, "duplicate_ordinal": ordinal, "row": deepcopy(row)}
        return ref

    def exclude(ref, reason):
        excluded.append({"source_ref": ref, "reason": reason})

    patient_ref = register("patients", patient)
    admissions = {}
    admission_refs = {}
    # Add the anchor exactly once, even when the reader already included it.
    supplied = list(seed.get("admissions") or [])
    supplied = [r for r in supplied if _text(r.get("hadm_id")) != anchor_id] + [anchor]
    for row in supplied:
        ref = register("admissions", row)
        hadm = _text(row.get("hadm_id"))
        start, end = _dt(row.get("admittime")), _dt(row.get("dischtime"))
        if _text(row.get("subject_id")) != subject:
            exclude(ref, "different_patient")
        elif not hadm or start is None or end is None or end < start:
            exclude(ref, "invalid_or_unfinished_source_encounter")
        elif end > cutoff:
            exclude(ref, "source_encounter_after_history_cutoff")
        elif hadm in admissions:
            exclude(ref, "duplicate_source_encounter")
        else:
            admissions[hadm] = deepcopy(row)
            admission_refs[hadm] = ref

    diagnosis_dictionary = {
        (_text(r.get("icd_version")), _text(r.get("icd_code"))): r
        for r in seed.get("diagnosis_dictionary") or []
    }
    lab_dictionary = {
        _text(r.get("itemid")): r for r in seed.get("lab_dictionary") or []
    }
    dictionary_refs = {}

    def dictionary_ref(table, key, row):
        token = (table, key)
        if row is None:
            return None
        if token not in dictionary_refs:
            dictionary_refs[token] = register(table, row)
        return dictionary_refs[token]

    def attachable(row, source_ref):
        if _text(row.get("subject_id")) != subject:
            exclude(source_ref, "different_patient")
            return False
        if _text(row.get("hadm_id")) not in admissions:
            exclude(source_ref, "unlinked_or_out_of_window_source_encounter")
            return False
        return True

    diagnoses = defaultdict(list)
    for row in seed.get("diagnoses") or []:
        ref = register("diagnoses_icd", row)
        if not attachable(row, ref):
            continue
        hadm = _text(row.get("hadm_id"))
        coding_key = (_text(row.get("icd_version")), _text(row.get("icd_code")))
        dictionary = diagnosis_dictionary.get(coding_key)
        diagnoses[hadm].append({
            "record_id": _opaque(secret, "dx", ref),
            "source_ref": ref, "source_encounter_ref": enc_ref(hadm),
            "status": "source_recorded_discharge_code",
            "coding": {"system": "ICD", "version": coding_key[0], "code": coding_key[1]},
            "display": _text(dictionary.get("long_title")) if dictionary else None,
            "dictionary_source_ref": dictionary_ref("d_icd_diagnoses", coding_key, dictionary),
            "source_sequence": _integer(row.get("seq_num")),
        })

    orders = []
    for row in seed.get("prescriptions") or []:
        ref = register("prescriptions", row)
        if not attachable(row, ref):
            continue
        orders.append({
            "record_id": _opaque(secret, "rx", ref),
            "source_ref": ref, "source_encounter_ref": enc_ref(row["hadm_id"]),
            "evidence_type": "historical_order",
            "drug_name_raw": _text(row.get("drug")),
            "prescription_component_type_raw": _text(row.get("drug_type")),
            "codes_raw": {key: _text(row.get(key)) for key in ("formulary_drug_cd", "gsn", "ndc")},
            "regimen_raw": {key: _text(row.get(key)) for key in RAW_REGIMEN_FIELDS},
            "time": _source_time(row, cutoff, "starttime"),
            "planned_stop_offset_hours": _hours(row.get("stoptime"), cutoff),
            "normalization": {"status": "unmapped", "drug_id": None,
                              "ingredient_id": None, "drug_class": None, "version": None},
        })

    observations = []
    for row in seed.get("labs") or []:
        ref = register("labevents", row)
        if not attachable(row, ref):
            continue
        item_code = _text(row.get("itemid"))
        dictionary = lab_dictionary.get(item_code)
        observations.append({
            "record_id": _opaque(secret, "obs", ref),
            "source_ref": ref, "source_encounter_ref": enc_ref(row["hadm_id"]),
            "status": "source_recorded_observation",
            "coding": {"system": "MIMIC_LAB_ITEM", "code": item_code},
            "display": _text(dictionary.get("label")) if dictionary else None,
            "fluid": _text(dictionary.get("fluid")) if dictionary else None,
            "dictionary_source_ref": dictionary_ref("d_labitems", item_code, dictionary),
            "value_raw": _text(row.get("value")), "numeric_value_raw": _text(row.get("valuenum")),
            "unit_raw": _text(row.get("valueuom")), "flag_raw": _text(row.get("flag")),
            "reference_range_raw": {key: _text(row.get(key)) for key in ("ref_range_lower", "ref_range_upper")},
            "time": _source_time(row, cutoff, "charttime", "storetime"),
        })

    anchor_age, anchor_year = _integer(patient.get("anchor_age")), _integer(patient.get("anchor_year"))
    topcoded = anchor_age is not None and anchor_age > 89
    age = None if topcoded or anchor_age is None or anchor_year is None else anchor_age + anchor_start.year - anchor_year
    age_status = "90_plus" if topcoded else "approximate" if age is not None else "unknown"
    encounters = []
    for hadm, row in sorted(admissions.items(), key=lambda item: (item[1]["admittime"], item[0])):
        eref = enc_ref(hadm)
        encounters.append({
            "encounter_ref": eref, "source_ref": admission_refs[hadm],
            "role": "anchor_historical_stay" if hadm == anchor_id else "earlier_historical_stay",
            "status": "completed_source_encounter", "source_setting": "inpatient",
            "admission_type_raw": _text(row.get("admission_type")),
            "admission_offset_hours": _hours(row.get("admittime"), cutoff),
            "discharge_offset_hours": _hours(row.get("dischtime"), cutoff),
            "diagnoses": diagnoses[hadm],
            "order_refs": [x["record_id"] for x in orders if x["source_encounter_ref"] == eref],
            "observation_refs": [x["record_id"] for x in observations if x["source_encounter_ref"] == eref],
        })

    coverage = deepcopy(seed.get("coverage") or {})
    dataset = "ENGINEERING_FIXTURE" if data_kind == "engineering_fixture" else "MIMIC-IV"
    public_coverage = {
        table: {k: v for k, v in data.items() if k in {"status", "rows"} or k.startswith("excluded_")}
        for table, data in coverage.get("tables", {}).items() if isinstance(data, dict)
    }
    case = {
        "schema_version": VERSION, "artifact_type": "source_case",
        "case_id": case_id, "patient_key": patient_key, "data_kind": data_kind,
        "source_group_id": _opaque(secret, "grp", subject),
        "source_anchor_encounter_ref": anchor_ref,
        "source_summary": {"dataset": dataset, "version": "2.2" if data_kind == "mimic_derived" else "fixture",
                           "version_basis": "configured_for_2.2_not_inferred_from_rows" if data_kind == "mimic_derived" else "engineering_fixture_explicitly_declared",
                           "data_kind": data_kind, "mode": "historical_seed", "tables": public_coverage},
        "cohort": {"primary_group": group, "protocol_status": "explicit_selection_not_a_validated_population_cohort",
                   "source_recorded_tags": [deepcopy(x["coding"]) for x in diagnoses[anchor_id]]},
        "clinical": {
            "demographics": {"source_ref": patient_ref, "sex_recorded": _text(patient.get("gender")),
                             "age_at_source_admission": {"value": age, "status": age_status,
                                                         "basis": "approximate_at_source_admission_not_current_simulation_age"}},
            "history": {"coverage": "available_completed_source_stays_only_not_lifetime_history",
                        "time_basis": "hours_relative_to_anchor_discharge", "encounters": encounters},
            "presenting_episode": {"chief_complaint": {"status": "unknown", "value": None},
                                   "onset": {"status": "unknown", "value": None}, "symptoms": []},
            "medications": {"orders": orders, "administrations": [], "reported_current_use": [],
                            "administrations_status": "not_loaded", "reported_current_use_status": "unknown"},
            "allergies": {"status": "unknown", "items": [], "source_refs": []},
            "observations": observations,
        },
        "formal_eligible": False,
    }
    scenario = {
        "schema_version": VERSION, "artifact_type": "scenario_config", "case_id": case_id,
        "mode": "historical_seed", "data_kind": data_kind,
        "new_visit": {"setting": "simulated_outpatient", "relation_to_source": "after_anchor_source_discharge",
                      "elapsed_days_after_source_discharge": None, "simulation_clock_origin": "new_visit_start"},
        "patient_policy_ref": "bounded_natural_patient/v0.2-default-unvalidated",
        "patient_facts": [], "status": "pending_scenario_construction",
        "formal_eligible": False,
    }
    provenance = {
        "schema_version": VERSION, "artifact_type": "restricted_source_index", "case_id": case_id,
        "scope": "local_restricted_not_agent_input", "data_kind": data_kind, "dataset": dataset,
        "source_cutoff": anchor["dischtime"], "source_rows": registry,
        "source_identifier_map": {"patient_key": patient_key, "subject_id": subject,
                                  "anchor_hadm_id": anchor_id,
                                  "encounters": {enc_ref(hadm): hadm for hadm in admissions}},
        "coverage": coverage, "excluded_records": excluded,
    }
    evaluation = {
        "schema_version": VERSION, "artifact_type": "evaluation_config", "case_id": case_id,
        "scope": "evaluator_only", "data_kind": data_kind,
        "preconsult_reference": None, "prescription_reference": None,
        "documentation_reference": None, "literature_mapping": None,
        "status": "pending_independent_references", "formal_eligible": False,
    }
    return {"case": case, "scenario": scenario, "provenance": provenance, "evaluation": evaluation}


def validate_package(package):
    """Return structural and source-association errors without exposing raw values."""
    errors = []
    if not isinstance(package, dict) or any(k not in package for k in ("case", "scenario", "provenance", "evaluation")):
        return ["package must contain case, scenario, provenance and evaluation"]
    case, scenario, provenance, evaluation = [package[k] for k in ("case", "scenario", "provenance", "evaluation")]
    if any(not isinstance(doc, dict) for doc in (case, scenario, provenance, evaluation)):
        return ["package documents must be objects"]
    cid = case.get("case_id")
    if not cid or any(doc.get("case_id") != cid for doc in (scenario, provenance, evaluation)):
        errors.append("package case identifiers disagree")
    if any(doc.get("schema_version") != VERSION for doc in (case, scenario, provenance, evaluation)):
        errors.append("unsupported schema version")
    for field, prefix in (("case_id", "case"), ("patient_key", "pt"),
                          ("source_group_id", "grp"), ("source_anchor_encounter_ref", "enc")):
        if not _is_opaque(case.get(field), prefix):
            errors.append("prepared identifiers must use opaque pseudonymous keys")
    if any(doc.get("formal_eligible") is not False for doc in (case, scenario, evaluation)):
        errors.append("this preparation format cannot declare formal eligibility")
    kind = case.get("source_summary", {}).get("data_kind")
    if kind not in DATA_KINDS or any(doc.get("data_kind") != kind for doc in (case, scenario, provenance, evaluation)):
        errors.append("data kind missing or inconsistent")
    if scenario.get("mode") != "historical_seed" or scenario.get("new_visit", {}).get("relation_to_source") != "after_anchor_source_discharge":
        errors.append("source stay must remain historical to the new simulated visit")
    rows = provenance.get("source_rows", {})
    identifiers = provenance.get("source_identifier_map", {})
    subject = _text(identifiers.get("subject_id"))
    cutoff = _dt(provenance.get("source_cutoff"))
    allowed = identifiers.get("encounters", {})
    if not isinstance(rows, dict) or not isinstance(allowed, dict) or cutoff is None or not subject:
        return errors + ["restricted source index is incomplete"]
    if identifiers.get("patient_key") != case.get("patient_key"):
        errors.append("restricted and public patient keys disagree")
    if allowed.get(case.get("source_anchor_encounter_ref")) != identifiers.get("anchor_hadm_id"):
        errors.append("anchor source identifier mapping disagrees")
    clinical = case.get("clinical", {})
    if not isinstance(clinical, dict) or not isinstance(clinical.get("history"), dict):
        return errors + ["clinical categories or historical encounters are missing"]
    history = clinical.get("history", {}).get("encounters", [])
    if not isinstance(history, list) or any(not isinstance(x, dict) for x in history):
        return errors + ["historical encounters must be an object list"]
    seen_encounters = set()
    for encounter in history:
        eref, sref = encounter.get("encounter_ref"), encounter.get("source_ref")
        if eref in seen_encounters:
            errors.append("duplicate historical encounter")
        seen_encounters.add(eref)
        source = rows.get(sref, {})
        row = source.get("row", {})
        start, end = _dt(row.get("admittime")), _dt(row.get("dischtime"))
        if eref not in allowed or source.get("table") != "admissions":
            errors.append("historical encounter references an unknown source admission")
        if _text(row.get("subject_id")) != subject or _text(row.get("hadm_id")) != _text(allowed.get(eref)):
            errors.append("historical encounter patient/admission association is invalid")
        if start is None or end is None or end < start or end > cutoff:
            errors.append("historical encounter is unfinished, invalid or after cutoff")
        if encounter.get("admission_offset_hours") != _hours(row.get("admittime"), cutoff) or encounter.get("discharge_offset_hours") != _hours(row.get("dischtime"), cutoff):
            errors.append("historical source times changed during import")
        if eref == case.get("source_anchor_encounter_ref") and end != cutoff:
            errors.append("history cutoff must equal the source anchor discharge")
        if eref == case.get("source_anchor_encounter_ref") and (_integer(row.get("hospital_expire_flag")) == 1 or _text(row.get("deathtime"))):
            errors.append("source anchor ending in death cannot seed a later visit")
    if case.get("source_anchor_encounter_ref") not in seen_encounters:
        errors.append("anchor historical encounter is absent")

    known_records = {}
    def check_record(record, table):
        rid, ref, eref = record.get("record_id"), record.get("source_ref"), record.get("source_encounter_ref")
        if not rid or rid in known_records:
            errors.append("missing or duplicate clinical record id")
        known_records[rid] = record
        source = rows.get(ref, {})
        row = source.get("row", {})
        if source.get("table") != table or eref not in seen_encounters:
            errors.append("clinical record has an unknown source or encounter")
        if _text(row.get("subject_id")) != subject or _text(row.get("hadm_id")) != _text(allowed.get(eref)):
            errors.append("clinical record is attached to the wrong source encounter")
        dref = record.get("dictionary_source_ref")
        if dref is not None and dref not in rows:
            errors.append("clinical record references an unknown dictionary row")

    for encounter in history:
        for diagnosis in encounter.get("diagnoses", []):
            check_record(diagnosis, "diagnoses_icd")
            if diagnosis.get("source_encounter_ref") != encounter.get("encounter_ref"):
                errors.append("diagnosis is nested under another source encounter")
    medications = clinical.get("medications", {})
    if not isinstance(medications, dict):
        return errors + ["medication evidence categories must be an object"]
    for record in medications.get("orders", []):
        check_record(record, "prescriptions")
        if record.get("evidence_type") != "historical_order":
            errors.append("source prescriptions cannot be reclassified as current use or administration")
        row = rows.get(record.get("source_ref"), {}).get("row", {})
        if record.get("codes_raw") != {key: _text(row.get(key)) for key in ("formulary_drug_cd", "gsn", "ndc")}:
            errors.append("raw medication code changed during import")
        if record.get("regimen_raw") != {key: _text(row.get(key)) for key in RAW_REGIMEN_FIELDS}:
            errors.append("raw medication regimen changed during import")
        if record.get("drug_name_raw") != _text(row.get("drug")) or record.get("prescription_component_type_raw") != _text(row.get("drug_type")):
            errors.append("raw medication identity or prescription component changed during import")
        if record.get("normalization") != {"status": "unmapped", "drug_id": None, "ingredient_id": None, "drug_class": None, "version": None}:
            errors.append("this import has no verified medication normalization")
        if record.get("time") != _source_time(row, cutoff, "starttime"):
            errors.append("source order time or initial availability changed")
    for record in clinical.get("observations", []):
        check_record(record, "labevents")
        row = rows.get(record.get("source_ref"), {}).get("row", {})
        if record.get("time") != _source_time(row, cutoff, "charttime", "storetime"):
            errors.append("source observation time or initial availability changed")
        for out_field, source_field in (("value_raw", "value"), ("numeric_value_raw", "valuenum"), ("unit_raw", "valueuom")):
            if record.get(out_field) != _text(row.get(source_field)):
                errors.append("raw observation value or unit changed during import")
    for encounter in history:
        for field in ("order_refs", "observation_refs"):
            for rid in encounter.get(field, []):
                if rid not in known_records or known_records[rid].get("source_encounter_ref") != encounter.get("encounter_ref"):
                    errors.append("historical record reference is missing or cross-encounter")
    demographics = clinical.get("demographics", {})
    psource = rows.get(demographics.get("source_ref"), {})
    if psource.get("table") != "patients" or _text(psource.get("row", {}).get("subject_id")) != subject:
        errors.append("demographics source does not match the selected patient")
    anchor_age = _integer(psource.get("row", {}).get("anchor_age"))
    age = demographics.get("age_at_source_admission", {})
    if anchor_age is not None and anchor_age > 89 and (age.get("value") is not None or age.get("status") != "90_plus"):
        errors.append("top-coded source age must not be emitted as a precise age")
    if medications.get("reported_current_use") or medications.get("reported_current_use_status") != "unknown":
        errors.append("source import must leave current medication use unknown; use explicit scenario facts")
    if medications.get("administrations") or medications.get("administrations_status") != "not_loaded":
        errors.append("this seed contract has not loaded administration records")
    allergies = clinical.get("allergies", {})
    if allergies.get("status") != "unknown" or allergies.get("items"):
        errors.append("source import has no allergy evidence; unknown must not become normal or denied")
    for field in ("chief_complaint", "onset"):
        item = clinical.get("presenting_episode", {}).get(field, {})
        if item.get("status") != "unknown" or item.get("value") is not None:
            errors.append("new presenting episode must be supplied explicitly in scenario facts")
    if clinical.get("presenting_episode", {}).get("symptoms"):
        errors.append("source import cannot invent new-visit symptoms")
    scenario_ids = set()
    scenario_facts = scenario.get("patient_facts", [])
    if not isinstance(scenario_facts, list) or any(not isinstance(x, dict) for x in scenario_facts):
        return errors + ["scenario patient facts must be an object list"]
    excluded_refs = {x.get("source_ref") for x in provenance.get("excluded_records", [])}
    for fact in scenario_facts:
        fid = fact.get("fact_id")
        if not fid or fid in scenario_ids:
            errors.append("scenario fact id missing or duplicated")
        scenario_ids.add(fid)
        if fact.get("field") not in SCENARIO_FIELDS:
            errors.append("unsupported scenario fact field")
        if fact.get("source_kind") not in {"constructed", "evidence_linked"}:
            errors.append("scenario additions require explicit constructed or evidence_linked source_kind")
        refs = fact.get("source_refs")
        if not isinstance(refs, list) or any(ref not in rows for ref in refs):
            errors.append("scenario fact source references are invalid")
        if fact.get("source_kind") == "evidence_linked" and not refs:
            errors.append("evidence-linked scenario addition lacks evidence")
        if fact.get("source_kind") == "evidence_linked" and isinstance(refs, list):
            for source_ref in refs:
                row = rows.get(source_ref, {}).get("row", {})
                if source_ref in excluded_refs or _text(row.get("subject_id")) != subject:
                    errors.append("scenario evidence does not belong to the selected historical patient")
            if fact.get("field") in {"chief_complaint", "onset", "symptom", "current_medication", "allergy"}:
                errors.append("this loaded source contract has no direct new-visit report evidence for that field; declare construction explicitly")
        status = fact.get("status")
        if status not in {"reported", "unknown"}:
            errors.append("scenario patient assertions must retain reported or unknown status")
        if status == "unknown" and fact.get("value") is not None:
            errors.append("unknown scenario fact must have a null value")
        if status == "reported" and not _nonempty(fact.get("value")):
            errors.append("reported scenario fact needs a value")

    forbidden = RAW_IDENTIFIER_KEYS | {"run_id", "encounter_id", "artifacts", "workflow_completed", "payment_success", "transaction_id"}
    def walk(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in forbidden:
                    errors.append("source identifiers or runtime outputs leaked into a prepared document")
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    for document in (case, scenario, evaluation):
        walk(document)
    return list(dict.fromkeys(errors))


def prepare_views(package):
    """Prepare isolated role views plus honest readiness, never execute the MAS."""
    errors = validate_package(package)
    if errors:
        case = package.get("case", {}) if isinstance(package, dict) else {}
        case = case if isinstance(case, dict) else {}
        base = {"schema_version": VERSION, "case_id": case.get("case_id"),
                "data_kind": case.get("data_kind"), "input_status": "blocked_invalid_package"}
        readiness = {"schema_version": VERSION, "case_id": case.get("case_id"),
                     "data_kind": case.get("data_kind"), "overall_ready_for_mas": False,
                     "formal_eligible": False}
        for name in ("source_import", "scenario", "preconsult", "prescription", "documentation", "mas_interface", "evaluation"):
            readiness[name] = {"status": "blocked", "errors": errors}
        return {"his": {**base, "artifact_type": "his_initial_view"},
                "patient": {**base, "artifact_type": "patient_initial_view"}, "readiness": readiness}
    case, scenario = package["case"], package["scenario"]
    clinical = case["clinical"]
    medications = clinical["medications"]
    def public_record(value):
        # Opaque source pointers themselves cannot resolve data in a working role.
        # The private provenance registry and raw source keys are never included.
        return deepcopy(value)
    historical_orders = [r for r in medications["orders"] if r.get("time", {}).get("available_by_source_cutoff")]
    historical_observations = [r for r in clinical["observations"] if r.get("time", {}).get("available_by_source_cutoff")]
    allowed_orders = {r["record_id"] for r in historical_orders}
    allowed_observations = {r["record_id"] for r in historical_observations}
    historical_encounters = deepcopy(clinical["history"]["encounters"])
    for encounter in historical_encounters:
        encounter["order_refs"] = [ref for ref in encounter["order_refs"] if ref in allowed_orders]
        encounter["observation_refs"] = [ref for ref in encounter["observation_refs"] if ref in allowed_observations]
    his = {
        "schema_version": VERSION, "artifact_type": "his_initial_view", "case_id": case["case_id"],
        "patient_key": case["patient_key"], "data_kind": case["source_summary"]["data_kind"],
        "demographics": public_record(clinical["demographics"]),
        "historical_encounters": historical_encounters,
        "historical_orders": public_record(historical_orders),
        "historical_observations": public_record(historical_observations),
        "allergy_record": {"status": "not_documented", "items": []},
        "current_use_record": {"status": "not_documented", "items": []},
        "input_status": "blocked_invalid_package" if errors else "prepared_source_view_only",
    }
    facts = deepcopy(scenario.get("patient_facts", []))
    patient = {
        "schema_version": VERSION, "artifact_type": "patient_initial_view", "case_id": case["case_id"],
        "data_kind": case["source_summary"]["data_kind"],
        "demographics": public_record(clinical["demographics"]),
        "patient_facts": facts, "patient_policy_ref": scenario["patient_policy_ref"],
        "input_status": "blocked_invalid_package" if errors else "pending_scenario_inputs",
    }
    reported = {f.get("field") for f in facts if f.get("status") == "reported" and _nonempty(f.get("value"))}
    missing = [field for field in ("chief_complaint", "onset") if field not in reported]
    if not missing and not errors:
        patient["input_status"] = "prepared_background_not_mas_validated"
    data_kind = case["source_summary"]["data_kind"]
    readiness = {
        "schema_version": VERSION, "case_id": case["case_id"], "data_kind": data_kind,
        "overall_ready_for_mas": False, "formal_eligible": False,
        "source_import": {"status": "blocked" if errors else "ready", "errors": errors,
                          "meaning": "结构及来源关联可检查；不等于临床资料完整或已经执行MAS",
                          "excluded_source_records": len(package["provenance"].get("excluded_records", [])),
                          "source_encounters": len(historical_encounters)},
        "scenario": {"status": "pending" if missing else "background_ready", "missing_fields": missing,
                     "notes": ["当前用药仍需显式口述设定或保留未知；历史处方不自动证明当前服药",
                               "源住院年龄不是新模拟就诊时的精确年龄；新就诊间隔尚未定"]},
        "preconsult": {"status": "pending" if missing else "background_ready", "missing_fields": missing,
                       "reason_class": "background_missing" if missing else "background_only",
                       "allergy_unknown_is_not_automatic_exclusion": True},
        "prescription": {"status": "pending", "reason_class": "setup_and_runtime_dependency",
                         "missing": ["真实药品规范化映射与适用临床规则", "医生在本次流程中动态生成的原始处方", "独立审方参考"],
                         "source_order_count": len(medications["orders"])},
        "documentation": {"status": "pending", "reason_class": "runtime_dependency",
                           "missing": ["本次实际对话与交接", "本次问诊/检查/处方处置产物"],
                           "note": "不预填最终病历；此项等待正常上游运行产物"},
        "mas_interface": {"status": "adapter_required", "reason_class": "adapter_missing",
                          "note": "v0.2为准备格式，尚不是legacy MAS可直接读取的输入；需适配并验证真实工具调用"},
        "evaluation": {"status": "pending", "reason_class": "independent_reference_missing",
                       "note": "评分依据和临床文献对应尚未建立；不会用源出院处方冒充本次标准答案"},
    }
    if data_kind == "engineering_fixture":
        readiness["fixture_notice"] = "完全虚构的转换测试资料，不是MIMIC病人，不可计入正式结果"
    if errors:
        # Do not expose even a partially built invalid record to working roles.
        his = {"schema_version": VERSION, "artifact_type": "his_initial_view", "case_id": case["case_id"],
               "data_kind": data_kind, "input_status": "blocked_invalid_package"}
        patient = {"schema_version": VERSION, "artifact_type": "patient_initial_view", "case_id": case["case_id"],
                   "data_kind": data_kind, "input_status": "blocked_invalid_package"}
        for name in ("scenario", "preconsult", "prescription", "documentation", "mas_interface", "evaluation"):
            readiness[name]["status"] = "blocked"
    return {"his": his, "patient": patient, "readiness": readiness}
