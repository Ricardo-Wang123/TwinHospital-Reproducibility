"""Project source FHIR resources into the physician's existing, cutoff-limited chart."""
from __future__ import annotations

from hashlib import sha256

from .mimic_fhir import clinical_window, encounter, indexed, instant, resource_key


def pointer(kind, key):
    return kind + "_" + sha256(key.encode()).hexdigest()[:24]


def coding(concept):
    return next(iter((concept or {}).get("coding", [])), {})


def label(concept):
    code = coding(concept)
    return (concept or {}).get("text") or code.get("display") or code.get("code")


def drug_name(record, resources, hints):
    concept = record.get("medicationCodeableConcept")
    if concept:
        return label(concept)
    reference = record.get("medicationReference", {}).get("reference")
    if not reference:
        return None
    if hints.get("source_drug_names"):
        return " + ".join(hints["source_drug_names"])
    def names(key, parents):
        if key in parents:
            raise ValueError("Circular Medication ingredient reference")
        med = resources[key]
        display = label(med.get("code"))
        if display:
            return [display]
        result = []
        for component in med.get("ingredient", []):
            if "itemReference" in component:
                result.extend(names(component["itemReference"]["reference"], parents | {key}))
            elif "itemCodeableConcept" in component:
                result.append(label(component["itemCodeableConcept"]))
        return result
    return " + ".join(x for x in names(reference, set()) if x) or None


def project_history(history):
    resources = indexed(history["fhir_history"])
    anchor = history["fhir_anchor"]
    hints = history.get("fhir_source_index", {})
    cutoff, section = clinical_window(resources, anchor, hints)
    output = {key: value for key, value in history.items() if not key.startswith("fhir_")}
    encounters = {}
    def offset(date):
        if date and len(date) > 10:
            return round((instant(date) - cutoff).total_seconds() / 3600, 6)
        return None
    def timed(event, recorded, basis):
        return {"event_offset_hours": offset(event), "recorded_offset_hours": offset(recorded),
                "available_by_source_cutoff": True, "event_time_known": offset(event) is not None,
                "availability_basis": basis}
    def entry(r, prefix):
        key = resource_key(r)
        visit = encounter(r)
        return {"record_id": pointer(prefix, key), "source_ref": hints.get(key, {}).get("source_ref") or pointer("src", key),
                "source_encounter_ref": encounters[visit]["encounter_ref"] if visit in encounters else None,
                "source_kind": "mimic_iv_on_fhir"}
    for r in section["encounters"]:
        key = resource_key(r)
        encounters[key] = {"encounter_ref": hints.get(key, {}).get("encounter_ref") or pointer("enc", key),
            "source_ref": hints.get(key, {}).get("source_ref") or pointer("src", key),
            "role": "anchor_historical_stay" if key == anchor else "earlier_historical_stay",
            "status": r["status"], "source_setting": r.get("class", {}).get("display") or r.get("class", {}).get("code"),
            "admission_offset_hours": offset(r["period"].get("start")), "discharge_offset_hours": offset(r["period"]["end"]),
            "diagnoses": [], "order_refs": [], "observation_refs": []}
    for r in section["diagnoses"]:
        c = coding(r["code"])
        encounters[encounter(r)]["diagnoses"].append({**entry(r, "dx"), "display": label(r["code"]),
            "coding": {k: c[k] for k in ("system", "code", "version") if k in c},
            "source_sequence": hints.get(resource_key(r), {}).get("diagnosis_sequence"),
            "status": "historical_source_diagnosis"})
    for visit in encounters.values():
        visit["diagnoses"].sort(key=lambda d: (d["source_sequence"] is None, d["source_sequence"], d["record_id"]))
    output["historical_encounters"] = list(encounters.values())
    orders = []
    for r in section["orders"]:
        dose = next(iter(r.get("dosageInstruction", [])), {})
        qty = next(iter(dose.get("doseAndRate", [])), {}).get("doseQuantity", {})
        stop = r.get("dispenseRequest", {}).get("validityPeriod", {}).get("end")
        row = {**entry(r, "rx"), "drug_name_raw": drug_name(r, resources, hints.get(resource_key(r), {})),
               "dose": qty.get("value"), "dose_unit": qty.get("unit"), "route": label(dose.get("route")),
               "frequency": label(dose.get("timing", {}).get("code")), "evidence_type": "historical_order",
               "planned_stop_offset_hours": offset(stop),
               "current_use_inferred": False, "time": timed(r["authoredOn"], r["authoredOn"], "MedicationRequest.authoredOn")}
        if encounter(r) in encounters:
            encounters[encounter(r)]["order_refs"].append(row["record_id"])
        orders.append(row)
    output["historical_orders"] = orders
    labs = []
    for r in section["observations"]:
        qty = r.get("valueQuantity", {})
        ref = next(iter(r.get("referenceRange", [])), {})
        c = coding(r["code"])
        spec = resources.get(r.get("specimen", {}).get("reference"), {})
        row = {**entry(r, "obs"), "status": r["status"], "display": label(r["code"]), "fluid": label(spec.get("type")),
               "coding": {"system": c.get("system"), "code": c.get("code")}, "numeric_value_raw": qty.get("value"),
               "value_raw": qty.get("value", r.get("valueString")), "unit_raw": qty.get("unit"),
               "flag_raw": label(next(iter(r.get("interpretation", [])), {})),
               "reference_range_raw": {"ref_range_lower": ref.get("low", {}).get("value"),
                                       "ref_range_upper": ref.get("high", {}).get("value")},
               "time": timed(r.get("effectiveDateTime"), r["issued"], "Observation.issued")}
        if encounter(r) in encounters:
            encounters[encounter(r)]["observation_refs"].append(row["record_id"])
        labs.append(row)
    output["historical_observations"] = labs
    for kind in ("administrations", "microbiology", "procedures", "dispenses"):
        rows = []
        for r in section[kind]:
            hint = hints.get(resource_key(r), {})
            row = {**entry(r, "src"), "status": r["status"]}
            if kind == "microbiology":
                spec = resources.get(r.get("specimen", {}).get("reference"), {})
                row.update(test_name=label(r["code"]), specimen_type=label(spec.get("type")),
                           interpretation=label(next(iter(r.get("interpretation", [])), {})) or r.get("valueString"),
                           time=timed(r.get("effectiveDateTime"), hint.get("available_at"), "MIMIC microbiology storage time"))
            elif kind == "procedures":
                row.update(display=label(r.get("code")), time=timed(r.get("performedDateTime"),
                           r.get("performedDateTime"), "Procedure.performedDateTime"))
            elif kind == "dispenses":
                when = r.get("whenHandedOver") or r.get("whenPrepared")
                row.update(medication=drug_name(r, resources, hint), time=timed(when, when or hint.get("available_at"),
                           "MedicationDispense recorded time" if when else "MIMIC pharmacy verification time"))
            else:
                dose = r.get("dosage", {})
                qty = dose.get("dose", {})
                when = r.get("effectiveDateTime")
                row.update(medication=drug_name(r, resources, hint), event_status_raw=r["status"],
                           administration_is_not_current_use=True, components=[{"dose_given": qty.get("value"),
                           "dose_given_unit": qty.get("unit"), "route": label(dose.get("route"))}],
                           time=timed(when, hint.get("available_at") or when, "eMAR storetime" if hint.get("available_at")
                                      else "MedicationAdministration.effectiveDateTime"))
            rows.append(row)
        output["historical_" + kind] = rows
    return output
