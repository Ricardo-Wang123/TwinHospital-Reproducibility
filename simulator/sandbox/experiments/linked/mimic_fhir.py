"""Patient-scoped collections and terminology from MIMIC-IV on FHIR v2.1."""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from datetime import datetime
import gzip
import json
from pathlib import Path
import re
from uuid import NAMESPACE_OID, uuid5


CANONICAL = "http://mimic.mit.edu/fhir/mimic"
RELEASE = "https://physionet.org/content/mimic-iv-fhir/2.1/"
PROFILE = CANONICAL + "/StructureDefinition/"
BASE = "https://twinhospital.invalid/fhir/"
CASE_VERSION = "twinhospital.linked.case/2"
STATIC = {"Location", "Medication", "Organization"}
CLINICAL = {"Patient", "Encounter", "Condition", "Procedure", "Observation", "Specimen",
            "MedicationRequest", "MedicationAdministration", "MedicationDispense", "MedicationStatement"}
ID = re.compile(r"[A-Za-z0-9.-]{1,64}")
PROFILE_TYPES = {
    "Patient": ("patient",), "Encounter": ("encounter",), "Condition": ("condition",),
    "Medication": ("medication",), "MedicationRequest": ("medication-request",),
    "MedicationAdministration": ("medication-administration", "medication-administration-icu"),
    "MedicationDispense": ("medication-dispense", "medication-dispense-ed"),
    "MedicationStatement": ("medication-statement-ed",), "Specimen": ("specimen",),
    "Location": ("location",), "Organization": ("organization",),
    "Procedure": ("procedure", "procedure-icu", "procedure-ed"),
    "Observation": tuple("observation-" + kind for kind in ("labevents", "micro-test", "micro-org", "micro-susc",
                        "chartevents", "datetimeevents", "outputevents", "ed", "vital-signs")),
}


def source_id(kind, value):
    """UUID derivation from the published sql/fhir_etl/uuid_namespace.sql."""
    dataset = uuid5(NAMESPACE_OID, "MIMIC-IV")
    return str(uuid5(uuid5(dataset, kind), str(value)))


def resource_key(resource):
    resource_type = resource.get("resourceType")
    resource_id = resource.get("id")
    if resource_type not in STATIC | CLINICAL or not isinstance(resource_id, str) or not ID.fullmatch(resource_id):
        raise ValueError("Expected an identifiable MIMIC R4 resource")
    return f"{resource_type}/{resource_id}"


def profile(resource):
    profiles = resource.get("meta", {}).get("profile", [])
    return next((item[len(PROFILE):] for item in profiles if item.startswith(PROFILE)), "")


def references(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "reference" and isinstance(child, str):
                yield child
            else:
                yield from references(child)
    elif isinstance(value, list):
        for child in value:
            yield from references(child)


def owner(resource):
    return (resource.get("subject") or resource.get("patient") or {}).get("reference")


def encounter(resource):
    return (resource.get("encounter") or resource.get("context") or {}).get("reference")


def instant(value):
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("A FHIR time with a time of day must include a timezone")
    return dt


def collect(resources):
    return {"resourceType": "Bundle", "type": "collection", "entry": [
        {"fullUrl": BASE + key, "resource": resource} for key, resource in sorted(resources.items())
    ]}


def indexed(bundle):
    if bundle.get("resourceType") != "Bundle" or bundle.get("type") != "collection":
        raise ValueError("Expected a FHIR R4 collection Bundle")
    resources = {}
    for entry in bundle.get("entry", []):
        resource = entry["resource"]
        key = resource_key(resource)
        p = profile(resource)
        if (not p.startswith("mimic-") or p[6:] not in PROFILE_TYPES[resource["resourceType"]]
                or key in resources or entry.get("fullUrl") != BASE + key):
            raise ValueError("Missing MIMIC profile, duplicate resource, or mismatched fullUrl")
        resources[key] = resource
    patients = {key for key in resources if key.startswith("Patient/")}
    if len(patients) != 1:
        raise ValueError("A history collection must identify exactly one patient")
    for key, resource in resources.items():
        if resource["resourceType"] not in STATIC | {"Patient"} and owner(resource) not in patients:
            raise ValueError(f"Resource belongs to a different patient: {key.split('/')[0]}")
        if any(ref not in resources for ref in references(resource)):
            raise ValueError(f"Unresolved reference in {resource['resourceType']}")
    return resources


def with_diagnosis_displays(bundle, directory):
    """Retain recorded wording in CodeableConcept.text and bind Coding.display to the guide."""
    terms = {}
    for name in ("mimic-diagnosis-icd9", "mimic-diagnosis-icd10"):
        definition = json.loads((directory / f"CodeSystem-{name}.json").read_bytes())
        system = CANONICAL + "/CodeSystem/" + name
        if definition.get("url") != system or definition.get("version") != "1.3.0" or definition.get("content") != "complete":
            raise ValueError("Expected the complete diagnosis CodeSystem from the MIMIC 1.3.0 implementation guide")
        terms[system] = {row["code"]: row["display"] for row in definition["concept"]}
    bundle = deepcopy(bundle)
    originals = {}
    for entry in bundle["entry"]:
        r = entry["resource"]
        if r["resourceType"] != "Condition":
            continue
        concept = r["code"]
        for c in concept.get("coding", []):
            dictionary = terms.get(c.get("system"))
            if dictionary is None:
                continue
            if c["code"] not in dictionary:
                raise ValueError("An historical diagnosis code is absent from the pinned MIMIC terminology")
            correct = dictionary[c["code"]]
            original = c.get("display")
            if original and original.casefold() != correct.casefold():
                originals.setdefault(resource_key(r), []).append({"system": c["system"], "code": c["code"],
                                                                 "display": original})
                concept.setdefault("text", original)
                c["display"] = correct
    return bundle, originals


def _rows(path, needle=None):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rb") as stream:
        for line in stream:
            if line.strip() and (needle is None or needle in line):
                yield json.loads(line)


def read_patient(directory: Path, subject_id):
    """Read published profile NDJSON or the same NDJSON generated by the official SQL."""
    files = sorted(path for path in directory.rglob("Mimic*.ndjson*")
                   if path.name.endswith((".ndjson", ".ndjson.gz")))
    if not files:
        raise ValueError("No MIMIC profile NDJSON files in the specified directory")
    patient_id = source_id("Patient", subject_id)
    patient = "Patient/" + patient_id
    selected = {}
    support_files = []
    for path in files:
        name = path.name.split(".")[0]
        if name in {"MimicLocation", "MimicOrganization", "MimicMedication", "MimicMedicationMix"}:
            support_files.append(path)
            continue
        for resource in _rows(path, patient_id.encode()):
            key = resource_key(resource)
            if key == patient or owner(resource) == patient:
                if key in selected:
                    raise ValueError("Duplicate patient resource in source exports")
                selected[key] = resource
    if patient not in selected:
        raise ValueError("The selected patient was not found in the MIMIC FHIR export")
    missing = {r for v in selected.values() for r in references(v)} - selected.keys()
    while missing:
        added = {}
        for path in support_files:
            for resource in _rows(path):
                key = resource_key(resource)
                if key in missing:
                    if key in added:
                        raise ValueError("Duplicate supporting resource in source exports")
                    added[key] = resource
        if missing - added.keys():
            types = sorted({key.split("/")[0] for key in missing - added.keys()})
            raise ValueError("Missing referenced FHIR resources: " + ", ".join(types))
        selected.update(added)
        missing = {r for v in added.values() for r in references(v)} - selected.keys()
    result = collect(selected)
    indexed(result)
    return result


def clinical_window(resources, anchor_key, supplemental):
    """Admit historical facts only when their availability at the anchor is supported."""
    anchor = resources.get(anchor_key, {})
    def hospital_stay(r):
        return profile(r) == "mimic-encounter" and r.get("status") == "finished" and any(
            item.get("system") == CANONICAL + "/identifier/encounter-hosp" for item in r.get("identifier", []))
    if not hospital_stay(anchor):
        raise ValueError("The source anchor must be a completed MIMIC hospital encounter")
    cutoff = instant(anchor["period"]["end"])
    admissions = {key for key, r in resources.items()
                  if hospital_stay(r) and r.get("period", {}).get("end")
                  and instant(r["period"]["end"]) <= cutoff}
    by_section = defaultdict(list)
    for key, resource in resources.items():
        p = profile(resource)
        visit = encounter(resource)
        if visit and visit not in admissions:
            continue
        if key in admissions:
            by_section["encounters"].append(resource)
            continue
        if p == "mimic-condition" and visit in admissions:
            by_section["diagnoses"].append(resource)
            continue
        observation_time = resource.get("effectiveDateTime")
        if p == "mimic-observation-labevents":
            date = resource.get("issued")
            section = "observations"
        elif p.startswith("mimic-observation-micro-"):
            date = supplemental.get(key, {}).get("available_at")
            section = "microbiology"
        elif p == "mimic-medication-request":
            date = resource.get("authoredOn")
            section = "orders"
        elif p == "mimic-medication-administration":
            date = supplemental.get(key, {}).get("available_at") or observation_time
            section = "administrations"
        elif p == "mimic-procedure":
            date = resource.get("performedDateTime")
            section = "procedures"
        elif p == "mimic-medication-dispense":
            date = resource.get("whenHandedOver") or resource.get("whenPrepared") or supplemental.get(key, {}).get("available_at")
            section = "dispenses"
        else:
            continue
        if date and len(date) == 10:
            # A date-only event can be placed safely only after the entire day.
            if datetime.fromisoformat(date).date() >= cutoff.date():
                continue
        elif not date or instant(date) > cutoff:
            continue
        if observation_time and ((len(observation_time) == 10 and observation_time >= cutoff.date().isoformat())
                                 or (len(observation_time) > 10 and instant(observation_time) > cutoff)):
            continue
        by_section[section].append(resource)
    return cutoff, by_section
