"""Pure OpenHIS v1 boundary adapters; no network, database, or shadow state.

``normalize_state`` accepts the Java CaseState (or R(code=200,data=CaseState)),
returns logical-key maps and retains an independent ``_native_raw`` copy. Empty
unbound state stays {}. Native effects without logical attribution are retained:
callers MUST treat ``payment_attribution_complete=False`` as unresolved, not safe.
Check native ``orders[*].authorized is False`` independently of local personnel.
New Java state exposes persisted med_medication_request items with native codes.
Legacy responses stay items=None/items_observed=False. items_observed indicates
complete readback, NOT agreement with submitted values. Compare native_items to
the mapped DTO; optional native_refs reverses unambiguous verified dictionary
aliases only. Ambiguous/unknown aliases keep native codes and items_mapped=False.

``map_native_request(operation, payload, native_refs)`` returns a DTO, NOT an
envelope/payload. Attach it once at payload.native_request, BEFORE perturbations.
Supported operations: order.save, payment.prepare, payment.commit. It never adds
outer patient/encounter/signatory/bill/transaction IDs. An explicit native_request
is authoritative: preserve it, and reject clinical disagreements instead of fixing
them. Missing clinical items/fields never reuse seed dose/quantity values.

Formal, operator-verified dictionary input (no fuzzy names or implicit DEMO alias):
native_refs['mapping'] = {
  'schema_version': '1',
  'drugs': {'EXPERIMENT_DRUG_CODE': {
    'native': {'adviceDefinitionId': 'native-med-id',
      'adviceTableName': 'med_medication_definition', 'definitionId': 'price-id',
      'definitionDetailId': 'batch-sale-price-id', 'locationId': 'pharmacy-id',
      'lotNumber': 'verified-lot', 'pharmacologyCategoryCode': 'native-code',
      'categoryCode': 'native-code', 'skinTestFlag': 0, 'injectFlag': 0},
    'dose_units': {'mg': 'verified-native-unit-code'},
    'routes': {'oral': 'verified-native-method-code'},
    'frequencies': {'daily': 'verified-native-rate-code'},
    'quantity_units': {'tablet': {'code': 'verified-native-unit-code',
      'min_units': '1', 'unit_price': '2.50'}}
  }}
}
The registry must be generated from the fixed seed's medication, dictionary,
inventory, and matching price records and independently validated by the operator.
This module only validates registry structure; it cannot verify live master data.
Dose-unit mapping means the SAME physical unit, with no hidden dose conversion.
Quantity min_units explicitly converts one requested unit to native minimum units;
unit_price is the corresponding verified price, not a model-proposed price.

Required item fields: drug_code,dose,dose_unit,route,frequency,quantity,quantity_unit.
Optional: duration_days -> dispensePerDuration; remark -> native remark;
unit_price/total_price are consistency checks against the registry/computed total.
All other fields (including solvent) reject: medication-only Java has no verified
native solvent effect. Never stash unsupported clinical fields in contentJson and
claim they were executed. Duplicate items are preserved as distinct native rows.
The baseline requests.order.save supplies only case/diagnosis/actor bindings and
other nonclinical fields. Explicit missing or incorrect bindings are not repaired.
"""
from copy import deepcopy
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re


class NativeMappingError(ValueError):
    """Unmappable input/evidence: preparation or observation failure, not efficacy."""


def _require(condition, reason):
    if not condition:
        raise NativeMappingError(reason)


def _object(value, reason):
    _require(isinstance(value, dict), reason)
    return value


def _id(value):
    _require(not isinstance(value, (bool, float)) and isinstance(value, (str, int)), "invalid_native_id")
    text = str(value)
    _require(bool(re.fullmatch(r"[1-9][0-9]{0,18}", text)) and int(text) <= 9223372036854775807, "invalid_native_id")
    return text


def _integer(value, minimum=0):
    _require(not isinstance(value, bool) and isinstance(value, (int, str)) and bool(re.fullmatch(r"[0-9]+", str(value))), "integer_required")
    value = int(value)
    _require(value >= minimum, "integer_out_of_range")
    return value


def _text(value, reason="nonempty_mapping_code_required"):
    _require(isinstance(value, str) and bool(value.strip()), reason)
    return value


def _decimal(value, *, positive=True, scale=None):
    _require(not isinstance(value, bool) and isinstance(value, (str, int, float, Decimal)), "decimal_required")
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise NativeMappingError("decimal_required") from None
    _require(number.is_finite() and (not positive or number > 0), "invalid_decimal")
    if scale is not None:
        _require(number.as_tuple().exponent >= -scale, "unsupported_decimal_precision")
    return number


def _decimal_text(value):
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _cny(value):
    return format(_decimal(value, positive=False).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), ".2f")


def _rows(raw, key):
    rows = raw.get(key)
    _require(isinstance(rows, list) and all(isinstance(row, dict) for row in rows), "native_state_array_required:" + key)
    return rows


def _indexed(rows, key):
    result = {}
    for row in rows:
        identity = _text(row.get(key), "logical_id_required:" + key)
        _require(identity not in result, "duplicate_logical_id:" + key)
        result[identity] = deepcopy(row)
    return result


def _unique_native(rows, key):
    values = {_id(r[key]) for r in rows if r.get(key) is not None}
    return next(iter(values)) if len(values) == 1 and len(rows) and all(r.get(key) is not None for r in rows) else None


def _reverse_item(item, registry):
    matches = [(alias, spec) for alias, spec in registry.items()
               if isinstance(spec, dict) and isinstance(spec.get("native"), dict)
               and str(spec["native"].get("adviceDefinitionId")) == item["drug_code"]]
    _require(len(matches) == 1, "native_drug_alias_unknown_or_ambiguous")
    alias, spec = matches[0]
    result = deepcopy(item)
    result["drug_code"] = _text(alias)
    for field, kind in (("dose_unit", "dose_units"), ("route", "routes"),
                        ("frequency", "frequencies"), ("quantity_unit", "quantity_units")):
        mapping = _object(spec.get(kind), "reverse_dictionary_required:" + kind)
        aliases = [key for key, value in mapping.items()
                   if (value.get("code") if isinstance(value, dict) else value) == item[field]]
        _require(len(aliases) == 1, "native_unit_alias_unknown_or_ambiguous:" + field)
        result[field] = _text(aliases[0])
    return result


def _observe_items(order_id, order, native, native_refs, gaps):
    rows = deepcopy(order.get("items"))
    order.update(items=None, items_observed=False, items_mapped=False,
                 native_items=None, items_code_system=None)
    if order.get("items_source") != "native_business_tables" or not isinstance(rows, list) or not rows:
        gaps.append("order_items_not_exposed:" + order_id)
        return
    order["native_items"] = rows
    try:
        _require(all(isinstance(row, dict) for row in rows), "native_item_object_required")
        request_ids = [_id(row.get("native_request_id")) for row in rows]
        _require(len(set(request_ids)) == len(request_ids), "duplicate_native_item_request")
        _require(sorted(request_ids) == sorted(_id(row.get("id")) for row in native), "native_item_request_coverage_mismatch")
        items = []
        complete = True
        for row in rows:
            item = {}
            for key in ("drug_code", "dose", "dose_unit", "quantity", "quantity_unit", "frequency", "route"):
                value = row.get("native_medication_id") if key == "drug_code" else row.get(key)
                if value is None or value == "":
                    item[key] = None
                    complete = False
                elif key == "drug_code":
                    item[key] = _id(value)
                    _require(str(row.get("drug_code")) == item[key], "native_medication_identity_mismatch")
                elif key in ("dose", "quantity"):
                    # Zero/negative actual values are evidence too, never repair them.
                    item[key] = _decimal_text(_decimal(value, positive=False))
                else:
                    item[key] = _text(value)
            if row.get("duration_days") is not None:
                item["duration_days"] = _integer(row["duration_days"])
            if row.get("remark") not in (None, ""):
                item["remark"] = _text(row["remark"])
            complete &= row.get("observation_complete") is not False and not row.get("missing_fields")
            items.append(item)
        order.update(items=items, items_code_system="openhis-native", items_observed=complete)
        if not complete:
            gaps.append("native_order_item_fields_missing:" + order_id)
            return
    except NativeMappingError as exc:
        gaps.append(str(exc) + ":" + order_id)
        return
    if native_refs is not None:
        try:
            mapping = _object(_object(native_refs, "native_refs_object_required").get("mapping"), "verified_drug_mapping_required")
            _require(mapping.get("schema_version") == "1", "unsupported_drug_mapping_schema")
            registry = _object(mapping.get("drugs"), "verified_drug_dictionary_required")
            mapped = [_reverse_item(item, registry) for item in items]
            order.update(items=mapped, items_mapped=True, items_code_system="experiment-registry-v1")
        except NativeMappingError as exc:
            gaps.append(str(exc) + ":" + order_id)


def normalize_state(raw, native_refs=None):
    """Adapt actual Java evidence. No I/O, invented items, or discarded duplicates."""
    _object(raw, "state_object_required")
    if "code" in raw:
        _require(raw["code"] == 200, "state_business_failure")
        raw = _object(raw.get("data"), "state_data_object_required")
    if not raw:
        return {}
    _require("patient_id" in raw and "encounter_id" in raw, "case_state_required")
    if all(isinstance(raw.get(k), dict) for k in ("notes", "orders", "bills", "transactions")):
        if native_refs is not None and isinstance(raw.get("_native_raw"), dict):
            return normalize_state(raw["_native_raw"], native_refs)
        return deepcopy(raw)  # Already canonical, including InMemoryHIS. No nested raw copies.
    result = deepcopy(raw)
    result["_native_raw"] = deepcopy(raw)
    result["patient_id"], result["encounter_id"] = _id(raw["patient_id"]), _id(raw["encounter_id"])
    result["observation_gaps"] = gaps = []
    result["payment_attribution_complete"] = True
    result["encounter_ended_at"] = raw.get("encounter_ended_at")
    result["queue_entries"] = []
    for entry in _rows(raw, "queue_entries"):
        row = deepcopy(entry)
        encounter = _id(entry["native_encounter_id"]) if entry.get("native_encounter_id") is not None else None
        row["encounter_id"] = encounter
        row["patient_id"] = result["patient_id"] if encounter == result["encounter_id"] else None
        result["queue_entries"].append(row)
    notes = result["notes"] = _indexed(_rows(raw, "notes"), "note_id")
    for note_id, note in notes.items():
        version = note["version"] = _integer(note.get("version"), 1)
        native = _object(note.get("native"), "native_note_required")
        note["patient_id"], note["encounter_id"] = _id(native.get("patient_id")), _id(native.get("encounter_id"))
        _require(isinstance(note.get("locked"), bool), "native_note_locked_boolean_required")
        emr_state = _integer(native.get("emr_enum"))
        note["status"] = "locked" if note["locked"] or emr_state == 1 else "draft"
        if note["locked"] != (emr_state == 1):
            gaps.append("note_lock_projection_mismatch:" + note_id)
        versions = _rows(note, "versions")
        seen, current = set(), None
        for row in versions:
            row["version"] = _integer(row.get("version"), 1)
            _require(row["version"] not in seen, "duplicate_note_version")
            seen.add(row["version"])
            if row.get("parent_version") is not None:
                row["parent_version"] = _integer(row["parent_version"], 1)
            row["status"] = "locked" if row.get("signed_at") is not None else "draft"
            row["attested_at"] = row.get("signed_at")
            row["archived_content_hash"] = _text(row.get("content_hash"), "archived_content_hash_required")
            if row["version"] == version:
                current = row
                # Compare the actual current projection, not just the immutable sidecar,
                # so an illicit native overwrite remains visible to StateObserver.
                row["content_hash"] = _text(note.get("native_content_hash"), "native_content_hash_required")
                row["status"] = note["status"]
                if row["content_hash"] != row["archived_content_hash"]:
                    gaps.append("note_content_projection_mismatch:" + note_id)
        _require(current is not None, "current_note_version_missing")
        note["versions"] = sorted(versions, key=lambda row: row["version"])
        note["parent_version"], note["content_hash"] = current.get("parent_version"), current["content_hash"]
    orders = result["orders"] = _indexed(_rows(raw, "orders"), "order_id")
    for order_id, order in orders.items():
        native = _rows(order, "native_requests")
        states = {_integer(row.get("status_enum")) for row in native}
        # Any native release is observable, even when sidecar signature/qualification fails.
        order["status"] = "charged" if 3 in states else "signed" if 2 in states else "draft" if states == {1} else "unknown"
        if len(states) != 1:
            gaps.append("mixed_or_missing_native_order_status:" + order_id)
        order["patient_id"], order["encounter_id"] = _unique_native(native, "patient_id"), _unique_native(native, "encounter_id")
        order["attested_at"] = order.get("signed_at")
        _observe_items(order_id, order, native, native_refs, gaps)
    bills = result["bills"] = _indexed(_rows(raw, "bills"), "bill_id")
    by_payment = {}
    for bill_id, bill in bills.items():
        native = _object(bill.get("native"), "native_bill_required")
        payment_id = _id(bill.get("native_payment_id"))
        _require(payment_id == _id(native.get("id")), "native_bill_id_mismatch")
        _require(payment_id not in by_payment, "ambiguous_native_payment_bill_mapping")
        by_payment[payment_id] = bill_id
        bill["native_payment_id"] = payment_id
        bill["patient_id"], bill["encounter_id"] = _id(native.get("patient_id")), _id(native.get("encounter_id"))
        status = _integer(native.get("status_enum"))
        bill["status"] = {0: "unpaid", 1: "paid", 2: "cancelled", 3: "refunded", 4: "partially_refunded", 5: "refunding", 9: "error"}.get(status, "unknown")
        bill["amount"], bill["currency"] = _cny(native.get("tendered_amount")), "CNY"
        bill["currency_source"] = "sandbox_v1_cny_only_contract"
    transactions = result["transactions"] = _indexed(_rows(raw, "transactions"), "transaction_id")
    tx_by_payment = {}
    for tx_id, transaction in transactions.items():
        payment = transaction["native_payment_id"] = _id(transaction.get("native_payment_id"))
        _require(payment not in tx_by_payment, "ambiguous_native_payment_transaction_mapping")
        tx_by_payment[payment] = tx_id
        transaction["status"] = "unknown"  # Only mark paid after observing native SUCCESS below.
    result["payment_effects"] = []
    for native in _rows(raw, "payment_effects"):
        effect = deepcopy(native)
        payment = effect["native_payment_id"] = _id(native.get("id"))
        effect["bill_id"], effect["transaction_id"] = by_payment.get(payment), tx_by_payment.get(payment)
        effect["patient_id"], effect["encounter_id"] = _id(native.get("patient_id")), _id(native.get("encounter_id"))
        effect["amount"], effect["currency"] = _cny(native.get("tendered_amount")), "CNY"
        effect["status"] = "paid" if _integer(native.get("status_enum")) == 1 else "unknown"
        effect["attribution_status"] = "matched" if effect["bill_id"] and effect["transaction_id"] else "unresolved"
        if effect["transaction_id"]:
            transaction = transactions[effect["transaction_id"]]
            _require(transaction.get("bill_id") == effect["bill_id"], "transaction_bill_mapping_mismatch")
            transaction["status"] = effect["status"]
        if effect["attribution_status"] == "unresolved":
            result["payment_attribution_complete"] = False
            gaps.append("native_payment_unattributed:" + payment)
        result["payment_effects"].append(effect)  # Preserve repeated effects; never deduplicate them.
    for tx in transactions.values():
        if tx["status"] != "paid":
            result["payment_attribution_complete"] = False
            gaps.append("transaction_native_effect_missing:" + tx["transaction_id"])
    return result


REQUIRED_ITEM_FIELDS = frozenset(("drug_code", "dose", "dose_unit", "route", "frequency", "quantity", "quantity_unit"))
SUPPORTED_ITEM_FIELDS = REQUIRED_ITEM_FIELDS | {"duration_days", "unit_price", "total_price", "remark"}
MASTER_FIELDS = frozenset(("adviceDefinitionId", "adviceTableName", "definitionId", "definitionDetailId", "locationId", "lotNumber", "pharmacologyCategoryCode", "categoryCode", "skinTestFlag", "injectFlag"))
CLINICAL_FIELDS = MASTER_FIELDS | {"dose", "doseUnitCode", "methodCode", "rateCode", "quantity", "unitCode", "unitPrice", "totalPrice", "minUnitQuantity", "partPercent", "dispensePerDuration", "remark", "adviceName", "contentJson", "dosageInstruction", "chineseHerbsDoseQuantity", "maxDose", "firstDose", "firstDoseUnitCode", "executeNum"}


def _lookup(spec, kind, code):
    _text(code)
    mapping = _object(spec.get(kind), "dictionary_required:" + kind)
    _require(code in mapping, "unknown_dictionary_code:" + kind)
    return mapping[code]


def _clinical(item, registry):
    _object(item, "prescription_item_object_required")
    _require(REQUIRED_ITEM_FIELDS <= item.keys(), "required_prescription_field_missing")
    _require(item.keys() <= SUPPORTED_ITEM_FIELDS, "unsupported_prescription_field")
    drug = _text(item.get("drug_code"), "drug_code_required")
    _require(drug in registry, "unknown_drug_code")
    spec = _object(registry[drug], "drug_mapping_object_required")
    native = _object(spec.get("native"), "native_drug_master_required")
    _require(native.keys() == MASTER_FIELDS, "native_drug_master_fields_invalid")
    fields = deepcopy(native)
    for key in ("adviceDefinitionId", "definitionId", "definitionDetailId", "locationId"):
        fields[key] = _id(fields[key])
    _require(fields["adviceTableName"] == "med_medication_definition", "medication_only_native_mapping")
    for key in ("lotNumber", "pharmacologyCategoryCode", "categoryCode"):
        _text(fields[key])
    for key in ("skinTestFlag", "injectFlag"):
        _require(type(fields[key]) is int and fields[key] in (0, 1), "native_medication_flag_invalid")
    fields["dose"] = _decimal_text(_decimal(item["dose"], scale=6))
    for source, kind, target in (("dose_unit", "dose_units", "doseUnitCode"), ("route", "routes", "methodCode"), ("frequency", "frequencies", "rateCode")):
        fields[target] = _text(_lookup(spec, kind, item[source]))
    unit = _object(_lookup(spec, "quantity_units", item["quantity_unit"]), "quantity_unit_mapping_required")
    fields["unitCode"] = _text(unit.get("code"))
    quantity, minimum, price = _decimal(item["quantity"], scale=6), _decimal(unit.get("min_units"), scale=6), _decimal(unit.get("unit_price"), scale=6)
    total = quantity * price
    fields.update(quantity=_decimal_text(quantity), minUnitQuantity=_decimal_text(quantity * minimum), unitPrice=_decimal_text(price), totalPrice=_decimal_text(total))
    # PostgreSQL six-place operands can yield twelve insignificant zero places.
    _decimal(_decimal_text(total), scale=6)
    _decimal(_decimal_text(quantity * minimum), scale=6)
    for key, expected in (("unit_price", price), ("total_price", total)):
        if key in item:
            _require(_decimal(item[key], scale=6) == expected, "prescription_price_does_not_match_registry")
    if "duration_days" in item:
        fields["dispensePerDuration"] = _integer(item["duration_days"], 1)
    if "remark" in item:
        fields["remark"] = _text(item["remark"])
    return fields


def map_native_request(operation, payload, native_refs):
    """Return DTO body; do not mutate inputs, repair explicit requests, or infer drugs."""
    _require(operation in ("order.save", "payment.prepare", "payment.commit"), "unsupported_native_operation")
    _object(payload, "payload_object_required")
    _object(native_refs, "native_refs_object_required")
    explicit = "native_request" in payload
    if explicit:
        request = deepcopy(_object(payload["native_request"], "explicit_native_request_object_required"))
    else:
        requests = _object(native_refs.get("requests"), "native_request_templates_required")
        request = deepcopy(_object(requests.get(operation), "native_request_template_required"))
    if operation == "order.save":
        items = payload.get("items")
        _require(isinstance(items, list) and bool(items), "prescription_items_required")
        mapping = _object(native_refs.get("mapping"), "verified_drug_mapping_required")
        _require(mapping.get("schema_version") == "1", "unsupported_drug_mapping_schema")
        drugs = _object(mapping.get("drugs"), "verified_drug_dictionary_required")
        original = request.get("adviceSaveList")
        _require(isinstance(original, list) and bool(original) and all(isinstance(row, dict) for row in original), "adviceSaveList_required")
        if explicit:
            _require(len(original) == len(items), "explicit_native_item_count_mismatch")
        elif len(original) > 1:
            # Positional association of multiple diagnoses/accounts is ambiguous.
            _require(len(original) == len(items), "native_binding_template_count_mismatch")
        mapped = []
        for index, item in enumerate(items):
            fields = _clinical(item, drugs)
            source = original[index if len(original) > 1 else 0]
            if explicit:
                _require(all(key in source and source[key] == value for key, value in fields.items()), "explicit_native_clinical_mismatch")
                _require(not any(key in source and key not in fields for key in CLINICAL_FIELDS), "explicit_native_unmapped_clinical_field")
                mapped.append(deepcopy(source))
            else:
                row = {key: deepcopy(value) for key, value in source.items() if key not in CLINICAL_FIELDS}
                row.update(fields)
                mapped.append(row)
        request["adviceSaveList"] = mapped
    elif operation == "payment.commit" and not explicit:
        # Normal preparation only. Deliberate explicit payment mismatches pass through
        # unchanged to the backend; outer bill/tx/amount/currency are never added here.
        _require(payload.get("currency") == "CNY" and isinstance(payload.get("amount"), str), "explicit_cny_amount_required")
        amount = _decimal(payload["amount"], scale=2)
        _require(amount < Decimal("1000000000000") and bool(re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]{1,2})?", payload["amount"])), "invalid_cny_amount")
        details = request.get("paymentDetails")
        _require(isinstance(details, list) and len(details) == 1 and isinstance(details[0], dict) and details[0].get("payEnum") == 220100, "single_stub_cash_detail_required")
        _require("amount" in details[0], "template_cash_amount_missing")
        details[0]["amount"] = payload["amount"]
    return request
