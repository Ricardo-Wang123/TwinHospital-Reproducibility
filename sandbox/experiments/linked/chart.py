"""Bounded, loss-explicit access to a complete local clinical history.

This is deterministic record selection, not a model-written summary. The input
history is never mutated. Pages carry stable references, counts and selection
rules; an omitted or unloaded record must never be interpreted as absent.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import re


INITIAL_BYTE_LIMIT = 40_000
PAGE_BYTE_LIMIT = 30_000
MAX_PAGE_RECORDS = 20
POLICY = "clinical_chart_whitelist_v9_reported_dose_timing"
DOSE_TIMING_BYTE_LIMIT = 3600

# A fixed EHR display selection, identical for every disease/configuration.
# It selects by analyte and time, never by whether a value looks reassuring.
LAB_OVERVIEW_LABELS = frozenset({
    'Creatinine', 'Creatinine, Whole Blood', 'Potassium', 'Potassium, Whole Blood',
    'Glucose', 'Glucose, Whole Blood', 'Sodium', 'Sodium, Whole Blood', 'Urea Nitrogen',
    'Estimated GFR (MDRD equation)', '% Hemoglobin A1c', 'White Blood Cells',
    'Alanine Aminotransferase (ALT)', 'Asparate Aminotransferase (AST)',
    'Aspartate Aminotransferase (AST)', 'Leukocytes', 'Nitrite',
})
LAB_OVERVIEW_BYTE_LIMIT = 16000

_LEAF = object()
_DEMOGRAPHICS = {k: _LEAF for k in (
    "age", "sex", "age_status", "source_kind", "age_time_basis")}
_DEMOGRAPHICS["source_admission_age"] = {k: _LEAF for k in ("value", "status", "basis")}
_DEMOGRAPHICS['age_population'] = {k: _LEAF for k in (
    'category','minimum_age_years','basis','source_url','source_kind','exact_age_claim')}
_TIME = {k: _LEAF for k in (
    "basis", "event_offset_hours", "recorded_offset_hours", "available_by_source_cutoff",
    "availability_basis", "event_time_known", "event_offset_lower_hours", "event_offset_upper_hours",
    "recorded_offset_lower_hours", "recorded_offset_upper_hours", "event_precision", "recorded_precision")}
_CODING = {k: _LEAF for k in ("system", "version", "code")}
_DIAGNOSIS = {k: _LEAF for k in (
    "record_id", "source_ref", "source_encounter_ref", "status", "display",
    "dictionary_source_ref", "source_sequence", "source_kind")}
_DIAGNOSIS["coding"] = _CODING
_ORDER = {k: _LEAF for k in (
    "record_id", "source_ref", "source_encounter_ref", "evidence_type", "drug_name_raw",
    "prescription_component_type_raw", "planned_stop_offset_hours", "drug_name",
    "drug_code", "dose", "dose_unit", "route", "frequency", "source_kind", "current_use_inferred")}
_ORDER.update({"time": _TIME,
    "codes_raw": {k: _LEAF for k in ("formulary_drug_cd", "gsn", "ndc")},
    "regimen_raw": {k: _LEAF for k in ("prod_strength", "form_rx", "dose_val_rx", "dose_unit_rx",
        "form_val_disp", "form_unit_disp", "doses_per_24_hrs", "route", "frequency")},
    "normalization": {k: _LEAF for k in ("status", "drug_id", "ingredient_id", "drug_class", "version")}})
_OBS = {k: _LEAF for k in (
    "record_id", "source_ref", "source_encounter_ref", "status", "display", "fluid",
    "dictionary_source_ref", "value_raw", "numeric_value_raw", "unit_raw", "flag_raw",
    "name", "value", "unit", "source_kind", "available_at")}
_OBS.update({"time": _TIME, "coding": _CODING,
    "reference_range_raw": {k: _LEAF for k in ("ref_range_lower", "ref_range_upper")}})
_ADMIN = {k:_LEAF for k in ('record_id','source_ref','source_encounter_ref','source_kind',
    'status','medication','event_status_raw','administration_is_not_current_use')}
_ADMIN['time']=_TIME
_ADMIN['components']=[{k:_LEAF for k in ('component_ordinal','administration_type',
    'complete_dose_not_given','dose_due','dose_due_unit','dose_given','dose_given_unit',
    'product_amount_given','product_unit','product_description','route',
    'infusion_rate','infusion_rate_unit','infusion_complete')}]
_MICRO = {k:_LEAF for k in ('record_id','source_ref','source_encounter_ref','source_kind','status',
    'specimen_ref','specimen_type','test_name','organism','isolate_number','quantity',
    'antibiotic','dilution_text','dilution_comparison','dilution_value','interpretation')}
_MICRO['time']=_TIME
_ENCOUNTER = {k: _LEAF for k in (
    "encounter_ref", "source_ref", "role", "status", "source_setting", "admission_type_raw",
    "admission_offset_hours", "discharge_offset_hours", "setting", "relative_discharge_days", "source_kind")}
_ENCOUNTER.update({"diagnoses": [_DIAGNOSIS], "order_refs": [_LEAF], "observation_refs": [_LEAF]})
_ITEM = {k: _LEAF for k in (
    "record_id", "name", "drug_code", "drug_name", "ingredient", "reaction", "severity",
    "status", "value", "dose", "dose_unit", "route", "frequency", "source_kind", "source_ref",
    "product_identity_status", "strength", "dosage_form", "device", "current_use_status",
    "indication_status", "historical_order_stop_offset_hours", "uncertainty", "planned_daily_dose_count")}
_ITEM.update({"time": _TIME,
    "current_product": {k: _LEAF for k in ("drug_code", "name", "ingredient", "dosage_form", "strength",
        "route", "dose_unit", "quantity_unit", "device", "source_kind", "identity_basis", "historical_exact_identity")},
    "plan": {k: _LEAF for k in ("document_kind", "issued_offset_hours", "start_offset_hours",
        "review_due_offset_hours", "stop_offset_hours", "purpose", "source_kind",
        "indication", "indication_status", "diagnosis_source_ref", "reference_url")},
    "actual_use": {k: _LEAF for k in ("window_start_offset_hours", "window_end_offset_hours",
        "planned_doses", "reported_taken_doses", "missed_doses", "additional_doses",
        "last_taken_offset_hours", "source_kind")},
    "supply": {k: _LEAF for k in ("initial_dose_equivalents", "used_dose_equivalents",
        "remaining_dose_equivalents", "meaning", "source_kind")}})
_STATUS = {"status": _LEAF, "source_kind": _LEAF, "items": [_ITEM], "source_refs": [_LEAF]}
_STATUS.update({k: _LEAF for k in ("as_of_offset_hours", "scope", "scope_status",
    "source_record_status", "reconciliation_status", "coverage_caveat", "current_plan_item_count")})
_STATUS["patient_report"] = {k: _LEAF for k in ("status", "value", "as_of_offset_hours",
    "source_kind", "verification_status")}
_STATUS["prior_visit"] = {k: _LEAF for k in ("source_kind", "document_kind", "event_offset_hours",
    "reason", "assessment_scope", "followup_plan", "current_prescribing_decision")}
_STATUS["prior_visit"]["diagnoses"] = [_DIAGNOSIS]
_TIME_BASIS = {k: _LEAF for k in ("history_offset_origin", "new_visit_offset_hours", "source_mode", "meaning")}
_REF_KEYS = {"record_id", "source_ref", "source_encounter_ref", "dictionary_source_ref", "encounter_ref", "specimen_ref"}
_REF_RE = re.compile(r"^(?:(?:enc|rx|obs|dx|src)_[a-f0-9]{24}|syn(?:hist|order)_[a-f0-9]{20})$")

# Search and disclosure are separate whitelists. In particular, a time basis
# may be disclosed for interpretation without making every order a match for
# "discharge". Match scalar clinical VALUES only, never JSON keys or metadata.
_SEARCH_DIAGNOSIS = ("display", "coding.code")
_SEARCH_ORDER = ("drug_name_raw", "drug_name", "drug_code", "dose", "dose_unit", "route", "frequency",
    "codes_raw.formulary_drug_cd", "codes_raw.gsn", "codes_raw.ndc",
    "regimen_raw.prod_strength", "regimen_raw.form_rx", "regimen_raw.dose_val_rx", "regimen_raw.dose_unit_rx",
    "regimen_raw.form_val_disp", "regimen_raw.form_unit_disp", "regimen_raw.doses_per_24_hrs",
    "regimen_raw.route", "regimen_raw.frequency", "normalization.drug_id", "normalization.ingredient_id",
    "normalization.drug_class")
_SEARCH_OBSERVATION = ("display", "name", "fluid", "value_raw", "numeric_value_raw", "unit_raw", "flag_raw",
    "value", "unit", "coding.code")
_SEARCH_ITEM = ("name", "drug_code", "drug_name", "ingredient", "reaction", "severity", "status",
    "value", "dose", "dose_unit", "route", "frequency", "strength", "dosage_form", "device",
    "current_use_status", "uncertainty", "plan.purpose")
_SEARCH_HEADER = ("source_setting", "admission_type_raw", "setting", "status")
_SEARCH_PATHS = {
    "historical_encounters": _SEARCH_HEADER + tuple("diagnoses." + p for p in _SEARCH_DIAGNOSIS),
    "encounter_headers": _SEARCH_HEADER, "diagnoses": _SEARCH_DIAGNOSIS,
    "historical_orders": _SEARCH_ORDER, "historical_observations": _SEARCH_OBSERVATION,
    "current_observations": _SEARCH_OBSERVATION,
    "historical_administrations": ('medication','event_status_raw','components.product_description','components.route'),
    "historical_microbiology": ('specimen_type','test_name','organism','antibiotic','interpretation'),
    "historical_outpatient_measurements": _SEARCH_OBSERVATION,
    "allergy_record": ("status",) + tuple("items." + p for p in _SEARCH_ITEM),
    "current_use_record": ("status",) + tuple("items." + p for p in _SEARCH_ITEM),
    "allergy_items": _SEARCH_ITEM, "current_use_items": _SEARCH_ITEM,
    "demographics": ("age", "sex", "age_status", "source_admission_age.value"),
    "time_basis": (), "encounter_order_refs": (), "encounter_observation_refs": (),
}


def _path_values(value, path):
    if isinstance(value, list):
        for item in value:
            yield from _path_values(item, path)
    elif path and isinstance(value, dict) and path[0] in value:
        yield from _path_values(value[path[0]], path[1:])
    elif not path and isinstance(value, (str, int, float)) and not isinstance(value, bool):
        yield str(value)


def _matches_clinical_value(section, record, term):
    if not term:
        return True
    needle = term.casefold()
    return any(needle in value.casefold() for path in _SEARCH_PATHS[section]
               for value in _path_values(record, path.split(".")))


def json_bytes(value):
    """The transport must use this compact representation for these byte caps."""
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _project(value, schema, field=""):
    """Copy only known clinical fields; return explicit excluded-field count."""
    if schema is _LEAF:
        if value is not None and not isinstance(value, (str, bool, int, float)):
            raise ValueError("clinical scalar has unexpected nested data")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("nonfinite clinical value")
        if field in _REF_KEYS and value is not None and not _REF_RE.fullmatch(str(value)):
            raise ValueError("clinical references must be opaque normalized identifiers")
        return value, 0
    if isinstance(schema, list):
        if not isinstance(value, list):
            raise ValueError("clinical list has wrong type")
        result, omitted = [], 0
        for item in value:
            item, n = _project(item, schema[0], "record_id" if field.endswith("_refs") else field)
            result.append(item)
            omitted += n
        return result, omitted
    if not isinstance(value, dict):
        raise ValueError("clinical object has wrong type")
    result, omitted = {}, len(set(value) - set(schema))
    for key in schema:
        if key in value:
            result[key], n = _project(value[key], schema[key], key)
            omitted += n
    return result, omitted


def _time_order(record):
    value = record.get("discharge_offset_hours")
    if value is None and record.get("relative_discharge_days") is not None:
        value = record["relative_discharge_days"] * 24
    if value is None:
        time = record.get("time", {})
        value = time.get("event_offset_hours")
        if value is None:value=time.get('event_offset_upper_hours')
    return value if isinstance(value, (int, float)) else float("-inf")


class HistoryChart:
    """One patient's local history, with bounded actor-visible read operations.

    ``query`` supports literal, case-insensitive term matching over clinical
    fields. It is not semantic search. Offsets refer to the filtered, stable
    order: newest known historical event first; ties preserve source order.
    """

    def __init__(self, his_initial):
        if not isinstance(his_initial, dict):
            raise ValueError("his_initial must be an object")
        self._sections = {}
        self._excluded_fields = len(set(his_initial) - {
            "demographics", "time_basis", "historical_encounters", "historical_orders",
            "historical_observations", "allergy_record", "current_use_record", "current_observations",
            "historical_administrations", "historical_microbiology", "historical_outpatient_measurements"})
        self.demographics, n = _project(his_initial.get("demographics", {}), _DEMOGRAPHICS)
        self._excluded_fields += n
        self.time_basis, n = _project(his_initial.get("time_basis", {}), _TIME_BASIS)
        self._excluded_fields += n
        if not self.time_basis:
            self.time_basis = {"history_offset_origin": "not_supplied",
                               "meaning": "Use each record's explicit relative time; no common origin may be inferred."}
        schemas = {"historical_encounters": [_ENCOUNTER], "historical_orders": [_ORDER],
            "historical_observations": [_OBS], "current_observations": [_OBS],
            "allergy_record": _STATUS, "current_use_record": _STATUS}
        # Missing/unextracted optional tables do not appear as empty clinical sections.
        schemas.update({key:schema for key,schema in {
            'historical_administrations':[_ADMIN], 'historical_microbiology':[_MICRO],
            'historical_outpatient_measurements':[_OBS]}.items() if key in his_initial})
        for section, schema in schemas.items():
            raw = his_initial.get(section, [] if isinstance(schema, list) else {"status": "not_supplied", "items": []})
            projected, omitted = _project(raw, schema)
            self._excluded_fields += omitted
            records = projected if isinstance(schema, list) else [projected]
            self._sections[section] = self._wrap(section, sorted(records, key=_time_order, reverse=True))
        diagnoses, headers, order_refs, observation_refs = [], [], [], []
        for entry in self._sections["historical_encounters"]:
            enc = entry["record"]
            key = enc.get("encounter_ref")
            header = {k: deepcopy(v) for k, v in enc.items() if k not in {"diagnoses", "order_refs", "observation_refs"}}
            header["component_counts"] = {k: len(enc.get(k, [])) for k in ("diagnoses", "order_refs", "observation_refs")}
            headers.append(header)
            diagnoses += [dict(x, source_encounter_ref=key) for x in enc.get("diagnoses", [])]
            order_refs += [{"record_id": x, "source_encounter_ref": key} for x in enc.get("order_refs", [])]
            observation_refs += [{"record_id": x, "source_encounter_ref": key} for x in enc.get("observation_refs", [])]
        self._sections["encounter_headers"] = self._wrap("encounter_headers", headers)
        self._sections["diagnoses"] = self._wrap("diagnoses", diagnoses)
        self._sections["encounter_order_refs"] = self._wrap("encounter_order_refs", order_refs)
        self._sections["encounter_observation_refs"] = self._wrap("encounter_observation_refs", observation_refs)
        self._sections["allergy_items"] = self._wrap("allergy_items", self._sections["allergy_record"][0]["record"].get("items", []))
        self._sections["current_use_items"] = self._wrap("current_use_items", self._sections["current_use_record"][0]["record"].get("items", []))
        self._sections["demographics"] = self._wrap("demographics", [self.demographics])
        self._sections["time_basis"] = self._wrap("time_basis", [self.time_basis])

    @staticmethod
    def _wrap(section, records):
        seen, result = {}, []
        for record in records:
            digest = hashlib.sha256((section + ":" + _canonical(record)).encode()).hexdigest()[:24]
            ordinal = seen.get(digest, 0)
            seen[digest] = ordinal + 1
            result.append({"record_ref": f"chart_{digest}_{ordinal}", "record": record})
        return result

    def _base(self):
        # The time-basis object itself is queryable when unexpectedly large.
        time_basis = deepcopy(self.time_basis) if json_bytes(self.time_basis) <= 2048 else {
            "status": "requires_query", "section": "time_basis", "not_absent": True}
        return {"chart_policy": POLICY, "time_basis": time_basis,
                "historical_order_means": "prescribed historically; current use and administration are not inferred",
                "missingness_rule": "Unshown, unavailable and unknown are not negative findings.",
                "excluded_non_whitelisted_field_count": self._excluded_fields}

    @staticmethod
    def _refinements(section):
        return {"historical_encounters": ["encounter_headers", "diagnoses", "encounter_order_refs", "encounter_observation_refs"],
                "allergy_record": ["allergy_items"], "current_use_record": ["current_use_items"]}.get(section, [])

    def query(self, section, encounter_key=None, term=None, offset=0, limit=20):
        if section not in self._sections:
            raise ValueError("unsupported chart section")
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= MAX_PAGE_RECORDS:
            raise ValueError("offset must be nonnegative; limit must be 1..20")
        if encounter_key is not None and (not isinstance(encounter_key, str) or not _REF_RE.fullmatch(encounter_key)):
            raise ValueError("encounter_key must be an opaque encounter reference")
        if term is not None and (not isinstance(term, str) or len(term) > 128):
            raise ValueError("term must be a string of at most 128 characters")
        records = self._sections[section]
        matches = [r for r in records if
            (encounter_key is None or r["record"].get("source_encounter_ref", r["record"].get("encounter_ref")) == encounter_key)
            and _matches_clinical_value(section, r["record"], term)]
        output = {**self._base(), "section": section,
            "selection": {"encounter_key": encounter_key, "term": term, "offset": offset, "limit": limit,
                          "matching": "literal_case_insensitive_clinical_values_only",
                          "search_fields": list(_SEARCH_PATHS[section]),
                          "search_excludes": "JSON keys, record/source identifiers, provenance, timing and availability metadata",
                          "ordering": "newest_known_event_first_then_source_order",
                          "time_order": "latest_first",
                          "equal_time_order": "original_source_order",
                          "missing_time_order": "after_all_known_times_in_original_source_order"},
            "total_count": len(records), "matching_count": len(matches), "records": [], "requires_expansion": [],
            "returned_count": 0, "next_offset": None, "unexpanded_matching_count": len(matches),
            "page_byte_limit": PAGE_BYTE_LIMIT, "size_policy": "complete records only; never slice strings or nested clinical records"}
        consumed = 0
        for item in matches[offset:offset + limit]:
            trial = deepcopy(output)
            trial["records"].append(item)
            # Reserve space for count/cursor metadata after selecting the page.
            if json_bytes(trial) <= PAGE_BYTE_LIMIT - 256:
                output["records"].append(deepcopy(item))
                consumed += 1
            elif not consumed:
                output["requires_expansion"].append({"record_ref": item["record_ref"],
                    "record_utf8_bytes": json_bytes(item["record"]), "reason": "single_record_exceeds_page_budget",
                    "encounter_key": item["record"].get("source_encounter_ref", item["record"].get("encounter_ref")),
                    "suggested_sections": self._refinements(section),
                    "action": "query finer section; if none is listed, add a reviewed finer schema before reading this record"})
                consumed += 1
                break
            else:
                break
        next_offset = offset + consumed
        output["returned_count"] = len(output["records"])
        output["next_offset"] = next_offset if next_offset < len(matches) else None
        output["unexpanded_matching_count"] = max(0, len(matches) - len(output["records"]))
        if json_bytes(output) > PAGE_BYTE_LIMIT:
            raise ValueError("chart page metadata exceeds byte budget; finer schema required")
        return output

    def initial_view(self):
        result = {**self._base(), "view_type": "bounded_history_directory",
            "selection_rule": "Complete section counts; current-use and last reported dose timing; newest three encounter headers; bounded historical laboratory overview. Query other full records explicitly.",
            "sections": {name: {"total_count": len(rows), "expanded_record_count": 0,
                "query_section": name} for name, rows in self._sections.items()},
            "reading_guide": {
                "current_observations": "Current-visit observations, if counted above, must be queried to read their values and source_kind; absence from this directory is not absence of measurements.",
                "historical_observations": "The historical_laboratory_overview expands recent available source records for a fixed set of analytes. It is dated historical evidence, never a new test. Query further records by clinical label, e.g. Creatinine or Potassium; unshown records are not absent.",
                "current_use_items": "Current reported medication items and any brought prior plan: query current_use_items (or current_use_record for scope/status). Plan start/review, actual-use window and uncertainty are separate. Historical orders do not establish current use.",
                "term_search": "Literal clinical values only; no semantic search. 'discharge' is not a filter for discharge prescriptions. Use encounter_key for a stay and inspect the returned original records."},
            "unavailable_document_views": {
                "free_text_notes": {"status": "not_loaded_in_this_chart", "queryable": False,
                                    "presence_in_original_patient_record": "unknown_not_inferred"},
                "discharge_summaries": {"status": "not_loaded_in_this_chart", "queryable": False,
                                        "presence_in_original_patient_record": "unknown_not_inferred"}},
            "demographics": deepcopy(self.demographics) if json_bytes(self.demographics) <= 4096 else {"status": "requires_query", "section": "demographics"},
            "allergy_record": {"status": self._sections["allergy_record"][0]["record"].get("status", "unknown"),
                               "item_count": len(self._sections["allergy_items"]), "items_expanded": False,
                               **{key: deepcopy(self._sections["allergy_record"][0]["record"][key])
                                  for key in ("source_record_status", "patient_report", "reconciliation_status")
                                  if key in self._sections["allergy_record"][0]["record"]}},
            "current_use_record": {"status": self._sections["current_use_record"][0]["record"].get("status", "unknown"),
                                   "item_count": len(self._sections["current_use_items"]), "items_expanded": False,
                                   "query_section": "current_use_items", "scope_query_section": "current_use_record",
                                   **{key: deepcopy(self._sections["current_use_record"][0]["record"][key])
                                      for key in ("as_of_offset_hours", "scope", "scope_status", "source_record_status",
                                                  "reconciliation_status", "current_plan_item_count", "prior_visit")
                                      if key in self._sections["current_use_record"][0]["record"]}},
            "recent_encounters": self.query("encounter_headers", limit=3),
            "initial_byte_limit": INITIAL_BYTE_LIMIT, "query_page_byte_limit": PAGE_BYTE_LIMIT,
            "query_max_records": MAX_PAGE_RECORDS, "complete_history_retained_locally": True}
        result['recent_reported_dose_timing'] = self._dose_timing_overview()
        overview = {'version': 'fixed_analyte_latest_two_v1', 'records': [],
            'selection': 'At most two latest known-time, source-cutoff-available records per exact analyte/specimen/unit; stable source order breaks ties. Selection does not inspect measured values or disease labels.',
            'time_interpretation': 'Original source-relative sampling and recording times are unchanged. These are historical records, not current-visit test results or evidence of present normality.',
            'all_other_records_queryable': True, 'missing_is_not_negative': True,
            'eligible_record_count': 0, 'returned_count': 0,
            'omitted_for_byte_limit': 0, 'byte_limit': LAB_OVERVIEW_BYTE_LIMIT}
        candidates, groups = [], {}
        for wrapped in self._sections['historical_observations']:
            row = wrapped['record']; tm = row.get('time') or {}
            event, recorded = tm.get('event_offset_hours'), tm.get('recorded_offset_hours')
            if (row.get('display') not in LAB_OVERVIEW_LABELS
                    or tm.get('available_by_source_cutoff') is not True
                    or type(event) not in (int, float) or type(recorded) not in (int, float)):
                continue
            visit = self.time_basis.get('new_visit_offset_hours')
            if type(visit) in (int, float) and (event > visit or recorded > visit):
                continue
            overview['eligible_record_count'] += 1
            key = (row.get('display'), row.get('fluid'), row.get('unit_raw'))
            if groups.get(key, 0) >= 2:
                continue
            groups[key] = groups.get(key, 0) + 1
            candidates.append(wrapped)
        for wrapped in candidates:
            proposed = {**overview, 'records': overview['records'] + [wrapped]}
            if (json_bytes(proposed) > LAB_OVERVIEW_BYTE_LIMIT-256
                    or json_bytes({**result, 'historical_laboratory_overview': proposed}) > INITIAL_BYTE_LIMIT-512):
                overview['omitted_for_byte_limit'] += 1
                continue
            overview['records'].append(deepcopy(wrapped))
        overview['returned_count'] = len(overview['records'])
        result['historical_laboratory_overview'] = overview
        result['sections']['historical_observations']['expanded_record_count'] = overview['returned_count']
        count = result["recent_encounters"]["returned_count"]
        result["sections"]["encounter_headers"]["expanded_record_count"] = count
        for info in result["sections"].values():
            info["unexpanded_count"] = info["total_count"] - info["expanded_record_count"]
        # An unexpectedly large status is a whole-field deferral, not truncation.
        for name in ("allergy_record", "current_use_record"):
            if json_bytes(result[name]) > 4096:
                result[name] = {"status": "requires_query", "section": name}
        if json_bytes(result) > INITIAL_BYTE_LIMIT:
            result["recent_encounters"] = {"status": "requires_query", "section": "encounter_headers",
                "total_count": len(self._sections["encounter_headers"]), "expanded_record_count": 0}
            result["sections"]["encounter_headers"]["expanded_record_count"] = 0
            result["sections"]["encounter_headers"]["unexpanded_count"] = len(self._sections["encounter_headers"])
        if json_bytes(result) > INITIAL_BYTE_LIMIT:
            raise ValueError("initial chart metadata exceeds byte budget; finer schema required")
        return result

    def _dose_timing_overview(self):
        """Expose existing encounter-relative last doses without inferring use."""
        items = self._sections['current_use_items']
        visit = self.time_basis.get('new_visit_offset_hours')
        report_at = self._sections['current_use_record'][0]['record'].get('as_of_offset_hours')
        overview = {
            'version': 'last_reported_dose_at_visit_v1', 'records': [],
            'scope': 'Last dose in an explicitly timed current-use report; not a verified administration.',
            'interpretation': 'Hours are relative to this visit. Calendar-day use and intervening doses are not inferred.',
            'items_in_report': len(items), 'eligible_timing_count': 0,
            'omitted_for_byte_limit': 0,
            'unshown_or_unknown_is_not_no_use': True, 'query_section': 'current_use_items',
        }
        def finite(value):
            return type(value) in (int, float) and math.isfinite(value)
        if not finite(visit) or report_at != visit:
            return overview
        for wrapped in items:
            item = wrapped['record']
            actual = item.get('actual_use') or {}
            start, end, last = (actual.get(key) for key in (
                'window_start_offset_hours', 'window_end_offset_hours', 'last_taken_offset_hours'))
            taken = actual.get('reported_taken_doses')
            if (not all(finite(v) for v in (start, end, last))
                    or end != visit or not start <= last <= visit
                    or type(taken) is not int or taken <= 0
                    or not actual.get('source_kind')):
                continue
            row = {'record_ref': wrapped['record_ref'], 'name': item.get('name') or item.get('drug_name'),
                   'hours_before_visit': visit-last, 'source_kind': actual['source_kind']}
            product = (item.get('current_product') or {}).get('drug_code')
            if product:
                row['reported_product_code'] = product
            overview['eligible_timing_count'] += 1
            candidate = {**overview, 'records': [*overview['records'], row]}
            if json_bytes(candidate) <= DOSE_TIMING_BYTE_LIMIT-128:
                overview['records'].append(row)
        overview['omitted_for_byte_limit'] = overview['eligible_timing_count']-len(overview['records'])
        return overview
