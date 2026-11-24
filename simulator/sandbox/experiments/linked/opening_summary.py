"""Physician-facing sections from the HL7 International Patient Summary 2.0.1."""
from copy import deepcopy

from .chart import json_bytes


def _fields(record, keys):
    return {key: deepcopy(record[key]) for key in keys if record.get(key) is not None}


def _section(code, title, detail_section, rows, keys, *, header=None, limit=8):
    section = {"title": title, "detail_section": detail_section,
               "entries": [], "entry_count": 0}
    if code:
        section["loinc"] = code
    if header:
        section.update(header if json_bytes(header) <= 900 else {"status": "see_source_record"})
    for wrapped in rows:
        section["entry_count"] += 1
        if len(section["entries"]) < limit:
            row = {"record_ref": wrapped["record_ref"], **_fields(wrapped["record"], keys)}
            if json_bytes(row) <= 768 and json_bytes(section["entries"]) + json_bytes(row) < 2400:
                section["entries"].append(row)
    return section


def clinician_summary(sections, time_basis, discharged_encounters):
    def header(name):
        record = sections[name][0]["record"]
        return {"status": record.get("status") or "unknown",
                **_fields(record, ("source_record_status", "reconciliation_status", "source_kind",
                                   "as_of_offset_hours", "record_label"))}

    problem = _section("11450-4", "Problem list", "problem_items", sections["problem_items"],
                       ("display", "coding", "status", "source_kind"), header=header("problem_list"))
    allergy = _section("48765-2", "Allergies and intolerances", "allergy_items", sections["allergy_items"],
                       ("name", "drug_name", "ingredient", "value", "reaction", "severity", "status", "source_kind"),
                       header=header("allergy_record"))
    report = sections["allergy_record"][0]["record"].get("patient_report")
    if report:
        allergy["patient_report"] = deepcopy(report) if json_bytes(report) <= 768 else {"detail_section": "allergy_record"}
    prior_allergies = [r["record"] for r in sections["historical_observations"]
                       if r["record"].get("source_kind") == "MIMIC-IV-Note prior discharge"
                       and r["record"].get("name", "").startswith("Prior allergies (")
                       and r["record"].get("time", {}).get("recorded_offset_hours", 0) < 0]
    if prior_allergies:
        latest = max(prior_allergies, key=lambda r: r["time"]["recorded_offset_hours"])
        allergy["prior_documentation"] = {"detail_section": "historical_observations", "query": "Prior allergies",
            "recorded_offset_hours": latest["time"]["recorded_offset_hours"], "source_kind": latest["source_kind"]}
        if json_bytes(latest["value_raw"]) <= 768 and latest["name"].endswith("(1/1)"):
            allergy["prior_documentation"]["statement"] = latest["value_raw"]
    medication = _section("10160-0", "Medication summary", "current_use_items", sections["current_use_items"],
                          ("name", "drug_name", "current_use_status", "status", "dose", "dose_unit", "route",
                           "frequency", "source_kind"), header=header("current_use_record"))
    past_rows = (row for row in sections["diagnoses"]
                 if row["record"].get("source_encounter_ref") in discharged_encounters)
    past = _section(None, "Prior discharge diagnoses", "diagnoses", past_rows,
                    ("display", "coding", "source_sequence", "status", "source_kind", "source_encounter_ref"),
                    limit=3)
    past["scope"] = "Discharge diagnoses from the recent historical encounters, in source order."
    patient = _fields(sections["demographics"][0]["record"], ("age", "age_status", "age_time_basis", "sex"))
    summary = {
        "template": "TwinHospital clinician view / HL7 IPS 2.0.1 sections",
        "patient": patient if json_bytes(patient) <= 512 else {"detail_section": "demographics"},
        "as_of_offset_hours": time_basis.get("new_visit_offset_hours"),
        "time_origin": time_basis.get("history_offset_origin"),
        "problem_list": problem, "allergies": allergy, "medications": medication,
        "prior_discharge_diagnoses": past,
        "diagnostic_results": {"loinc": "30954-2", "title": "Diagnostic results",
                               "view": "historical_laboratory_overview", "detail_section": "historical_observations"},
    }
    if "historical_procedures" in sections:
        summary["procedure_history"] = {"loinc": "47519-4", "title": "History of procedures",
                "entry_count": len(sections["historical_procedures"]), "detail_section": "historical_procedures"}
    prior = sections["current_use_record"][0]["record"].get("prior_visit", {})
    if prior.get("diagnoses"):
        dated = _fields(prior, ("event_offset_hours", "source_kind"))
        entries = [_fields(d, ("display", "status", "source_kind")) for d in prior["diagnoses"][:3]]
        summary["prior_working_assessment"] = {"detail_section": "current_use_record",
                "entry_count": len(prior["diagnoses"]), **dated,
                "entries": [entry for entry in entries if json_bytes(entry) <= 512]}
    return summary
