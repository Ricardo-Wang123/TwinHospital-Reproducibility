"""Read-only independent audit of completed linked runs and target medication bills.

Clinical text is compared locally and never printed or exported. Authentication
creates isolated observer sessions; all subsequent native calls are read APIs.
The PostgreSQL supplement executes BEGIN READ ONLY and distinguishes target
medication charge items from registration payments.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
from decimal import Decimal

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sandbox.experiments.linked.native_his import OpenHISClient, ROLE_SETTINGS, digest, write_private

def require_complete_anchors(batch,spec):
    """Legacy deep audit has a paid-anchor contract; fail before login on partials."""
    for number,case in enumerate(spec.get('cases',[]),1):
        try:
            index=read(batch/case['case_id']/'attempts.json')
            path=Path(index['attempts']['linked_baseline_anchor']['result_path']).resolve()
            if batch.resolve() not in path.parents:raise ValueError('Out-of-scope result')
            run=read(path);a=run.get('artifacts') or {};stages=run.get('stage_inputs') or {}
            okay=run.get('status')=='completed' and run.get('native_complete') is True and all(
                a.get(k) for k in ('prescription_versions','note_versions','note_reviews','attested_note','settlement')) and all(
                k in a for k in ('original_prescription','final_prescription')) and all(k in stages for k in ('prescription','documentation'))
        except (OSError,ValueError,KeyError,TypeError):okay=False
        if not okay:
            raise ValueError(f'Legacy paid-anchor audit cannot evaluate planned unit S{number:04d}; use tools/audit_linked_workflows.py --observe-native to retain incomplete, failed and missing cases.')


def read(path):
    return json.loads(Path(path).read_text())


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def audit(batch_dir, public_path, private_path):
    batch, public, private = map(lambda x: Path(x).resolve(), (batch_dir, public_path, private_path))
    if public.exists() or private.exists():
        raise ValueError("Audit outputs must be new; previous observations are retained")
    if not (batch / "batch_summary.json").exists():
        raise ValueError("Finished batch required")
    spec = read(batch / "specification.json")
    if spec.get("mode") != "linked":
        raise ValueError("This audit requires complete linked encounters")
    require_complete_anchors(batch,spec)
    mapping = next((read(p) for p in (ROOT / "deploy/openhis-experiment").glob("catalog_mapping*.json")
                    if digest(read(p)) == spec["catalog_hash"]), None)
    if mapping is None:
        raise ValueError("Exact frozen catalogue mapping unavailable")
    credentials_path = ROOT / "deploy/openhis-experiment/runtime/accounts/credentials.json"
    if credentials_path.stat().st_mode & 0o077:
        raise ValueError("Credentials require mode 0600")
    credentials = read(credentials_path)
    clients = {}
    for role in ("doctor", "cashier"):
        name, org, _ = ROLE_SETTINGS[role]
        client = OpenHISClient()
        client.login(name, credentials[name], organization=org)
        clients[role] = client
    credentials.clear()
    doctor, cashier = clients["doctor"], clients["cashier"]
    originals, records, target_ids = {}, [], []
    for number, case in enumerate(spec["cases"], 1):
        index_path = batch / case["case_id"] / "attempts.json"
        index = read(index_path)
        run_path = Path(index["attempts"]["linked_baseline_anchor"]["result_path"])
        if batch not in run_path.resolve().parents:
            raise ValueError("Run outside source batch")
        native_path = run_path.parents[2] / "native/native_state.json"
        protocol_path = native_path.with_name("protocol_state.json")
        for p in (index_path, run_path, native_path, protocol_path, batch / "specification.json", batch / "batch_summary.json"):
            originals[str(p)] = sha(p)
        run, state, protocol = read(run_path), read(native_path), read(protocol_path)
        key = run["run_id"] + ":" + run["case_id"]
        binding = state["bindings"][key]
        a, stages = run["artifacts"], run["stage_inputs"]
        checks = {}
        def verify(name, value):
            checks[name] = bool(value)
        verify("run_completed_native", run["status"] == "completed" and run["native_complete"] is True)
        verify("case_input_matches_frozen_spec", run["case_id"] == case["case_id"] and run["input_hash"] == case["runtime_input_hash"])
        verify("run_binding_matches", all(str(run[k]) == str(binding[k]) for k in ("patient_id", "encounter_id")))
        verify("events_share_encounter", all(str(e[k]) == str(binding[k]) for e in run["events"]
                                              for k in ("patient_id", "encounter_id") if e.get(k)))
        verify("model_observation_response_hashes", all(digest(c["observation"]) == c["observation_hash"] and
            ("response" not in c or digest(c["response"]) == c["response_hash"]) for c in run["calls"]))
        verify("prescription_stage_is_original_candidate", stages["prescription"]["prescription"] == a["original_prescription"])
        verify("documentation_stage_is_final_prescription", stages["documentation"]["approved_prescription"] == a["final_prescription"])
        verify("documentation_stage_has_actual_consultation_and_handoff", bool(stages["documentation"].get("consultation")) and bool(stages["documentation"].get("handoff")))
        verify("documentation_uses_physician_transcript", bool(stages["documentation"].get("transcript")) and
            all(x.get("phase") in {"doctor_handoff_verification", "consultation"} for x in stages["documentation"]["transcript"]))
        rx_versions, note_versions, note_reviews = a["prescription_versions"], a["note_versions"], a["note_reviews"]
        verify("prescription_version_chain", [x["version"] for x in rx_versions] == list(range(1, len(rx_versions) + 1)) and
               all(rx_versions[i]["parent_order_id"] == rx_versions[i-1]["order_id"] for i in range(1, len(rx_versions))))
        verify("prescription_original_and_final_retained", rx_versions[0]["prescription"] == a["original_prescription"] and
               rx_versions[-1]["prescription"] == a["final_prescription"] and rx_versions[-1]["review"]["decision"] == "approve")
        verify("note_version_chain", [x["version"] for x in note_versions] == list(range(1, len(note_versions) + 1)))
        verify("note_review_versions_match", all(x["content_hash"] == digest(note_versions[x["reviewed_version"]-1]["content"])
                                                for x in note_reviews))
        verify("final_note_accepted_and_attested", note_reviews[-1]["decision"] == "accept" and
               note_reviews[-1]["reviewed_version"] == a["attested_note"]["version"] == note_versions[-1]["version"] and
               a["attested_note"]["content"] == note_versions[-1]["content"] and a["attested_note"]["status"] == "locked")
        verify("independent_note_generation_call", any(c["phase"].startswith("documentation_draft_") for c in run["calls"]))
        pair = {"patientId": binding["patient_id"], "encounterId": binding["encounter_id"]}
        advice = doctor.post("/doctorStationAdvice/listDoctorStationAdvice", pair)
        doc = doctor.post("/document/getContent", {"documentId": binding["note"]["native_id"]})
        content = json.loads(doc["content"]) if isinstance(doc["content"], str) else doc["content"]
        wanted_content = {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text",
            "text": json.dumps(a["attested_note"]["content"], ensure_ascii=False, sort_keys=True)}]}]}
        verify("native_note_locked_and_text_matches", doc.get("status") == "LOCKED" and content == wanted_content and
               str(doc["patientId"]) == pair["patientId"] and str(doc["encounterId"]) == pair["encounterId"])
        bill = next(x for x in binding["bills"].values() if x["bill_id"] == a["settlement"]["payment"]["bill_id"])
        order = binding["orders"][bill["order_id"]]
        verify("target_order_is_final_reviewed_version", bill["order_id"] == rx_versions[-1]["order_id"] and
               order["items"] == a["final_prescription"]["items"] and order["status"] == "signed")
        observed_rows = {str(x["requestId"]): x for x in advice["content"]}
        order_checks = []
        for i, native_id in enumerate(order["native_ids"]):
            row, item = observed_rows[native_id], order["items"][i]
            m = mapping[item.get("drug_code", item.get("medication_code"))]
            canonical = order.get("frequency_normalizations", [])[i]["canonical"]
            unit = (m.get("dose_unit_codes") or {m["source_dose_unit"]: m["dose_unit_code"]})[item["dose_unit"]]
            expected = {"patientId": pair["patientId"], "encounterId": pair["encounterId"],
                "productDefinitionId": m["medicationDefinitionId"], "unitCode": m["quantity_unit_code"],
                "doseUnitCode": unit, "methodCode": m["route_codes"][item["route"]],
                "rateCode": m["frequency_codes"][canonical], "authoredById": ROLE_SETTINGS["doctor"][2]}
            order_checks.append(all(str(row.get(k)) == str(v) for k, v in expected.items()) and
                Decimal(str(row["dose"])) == Decimal(str(item["dose"])) and
                Decimal(str(row["quantity"])) == Decimal(str(item["quantity"])) and
                row.get("statusEnum") not in (None, "DRAFT") and bool(row.get("prescriptionNo")) and bool(row.get("reqAuthoredTime")))
        verify("native_order_fields_signed_author_and_binding_match", len(order_checks) == len(order["items"]) and all(order_checks))
        proof = protocol["attestations"][key + ":" + bill["order_id"]]
        verify("runtime_signatory_and_native_author_match", proof["signatory_id"] == ROLE_SETTINGS["doctor"][2] and
               set(proof["native_ids"]) == set(order["native_ids"]))
        verify("runtime_queue_settled", protocol["queues"][key]["status"] == "settled" and
               protocol["queues"][key]["encounter_id"] == binding["encounter_id"])
        charge = cashier.post("/outpatientCharge/loadPendingChargeItems", {"encounterId": binding["encounter_id"], "paymentId": bill["payment_id"]})
        candidates = [charge.get("settlementInsuranceSummary") or {}] + (charge.get("settlementInsuranceOptions") or [])
        actual_payment = next((x for x in candidates if str(x.get("paymentId")) == bill["payment_id"]), None)
        verify("native_target_payment_identity_and_amount", actual_payment is not None and
               Decimal(str(actual_payment["displayAmount"])) == Decimal(bill["amount"]) and bill["status"] == "paid")
        verify("all_business_writes_verified", all(e["response"].get("readback_verified") is True
               for e in run["events"] if e["event"] == "business_operation" and e.get("status") != "failed_exception"))
        target_ids.append(binding["encounter_id"])
        records.append({"study_case": f"S{number:04d}", "checks": checks,
            "version_counts": {"prescription": len(rx_versions), "note": len(note_versions), "note_reviews": len(note_reviews)},
            "stage_input_hashes": {k: digest(v) for k, v in stages.items()},
            "source_run_file_sha256": sha(run_path), "source_run_canonical_hash": digest(run),
            "source_generation_version": spec['batch'], "native_signedById": "not_exposed" if
                all(not observed_rows[k].get("signedById") for k in order["native_ids"]) else "exposed",
            "private": {"case_id": run["case_id"], "run_id": run["run_id"], "encounter_id": binding["encounter_id"],
                "patient_id": binding["patient_id"], "bill": bill, "native_order_ids": order["native_ids"],
                "native_document_id": binding["note"]["native_id"],
                "fresh_native_response_hashes": {"advice": digest(advice), "document": digest(doc), "charge": digest(charge)}}})
    if not all(re.fullmatch(r"[0-9]{1,19}", x) for x in target_ids):
        raise ValueError("Non-numeric native encounter identifier")
    module_spec = importlib.util.spec_from_file_location("isolated_manage", ROOT / "deploy/openhis-experiment/manage.py")
    manage = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(manage)
    query = """BEGIN READ ONLY;
SELECT coalesce(jsonb_agg(jsonb_build_object('id',p.id::text,'patient_id',p.patient_id::text,
 'encounter_id',p.encounter_id::text,'status',p.status_enum,'amount',p.display_amount::text,
 'charge_item_ids',p.charge_item_ids,'details',(SELECT coalesce(jsonb_agg(jsonb_build_object(
 'id',d.id::text,'pay_enum',d.pay_enum,'result_enum',d.result_enum,'amount',d.amount::text)),'[]'::jsonb)
 FROM fin_payment_rec_detail d WHERE d.reconciliation_id=p.id AND NOT d.is_deleted))),'[]'::jsonb)
 FROM fin_payment_reconciliation p WHERE NOT p.is_deleted AND p.encounter_id IN (""" + ",".join(target_ids) + "); COMMIT;"
    payments = json.loads(manage.sql(args=["-A", "-t"], text=query).stdout.strip())
    for row in records:
        info, checks = row["private"], row["checks"]
        bill = info["bill"]
        own = [p for p in payments if p["encounter_id"] == info["encounter_id"]]
        target = [p for p in own if set(bill["charge_item_ids"]).intersection(re.findall(r"\d+", p["charge_item_ids"] or ""))]
        successful = [p for p in target if p["status"] == "SUCCESS"]
        cash = [d for p in successful for d in p["details"] if d["pay_enum"] == "CASH" and d["result_enum"] == "SUCCESS"]
        checks["target_medication_bill_one_successful_payment"] = len(successful) == 1 and successful[0]["id"] == bill["payment_id"]
        checks["target_medication_bill_one_cash_detail_and_amount"] = len(cash) == 1 and Decimal(cash[0]["amount"]) == Decimal(bill["amount"])
        checks["target_payment_patient_binding"] = bool(target) and all(p["patient_id"] == info["patient_id"] for p in target)
        row["payment_audit_scope"] = "target_medication_charge_items_only_registration_payments_excluded"
        row["other_encounter_payment_records_excluded"] = len(own) - len(target)
        info["target_payment_database_readback"] = target
    unchanged = all(sha(p) == value for p, value in originals.items())
    public_rows = [{k: v for k, v in row.items() if k != "private"} for row in records]
    report = {"audit_version": "linked-native-readonly-1", "cases": public_rows,
        "source_specification_sha256": sha(batch / "specification.json"), "source_files_unchanged": unchanged,
        "all_checks_passed": unchanged and all(all(x["checks"].values()) for x in records),
        "native_observation": "fresh_authenticated_read_APIs_and_read_only_SQL",
        "scope": "business_integrity_and_freezable_stage_sources_not_clinical_correctness",
        "native_version_history_independently_verified": False,
        "version_evidence": "retained_runtime_versions_plus_native_current_text_and_LOCKED_state",
        "signatory_evidence": "authenticated_operator_runtime_registry_native_author_and_signed_state_no_native_only_claim",
        "post_run_audit_not_original_observation_window": True}
    write_private(private, {**report, "cases": records, "original_source_file_hashes": originals})
    public.parent.mkdir(parents=True, exist_ok=True)
    public.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return {"public_audit": str(public), "cases": len(records), "all_checks_passed": report["all_checks_passed"],
            "failed_checks": {x["study_case"]: [k for k, v in x["checks"].items() if not v] for x in records},
            "source_files_unchanged": unchanged}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-dir", type=Path, required=True)
    parser.add_argument("--public-path", type=Path, required=True)
    parser.add_argument("--private-path", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(audit(args.batch_dir, args.public_path, args.private_path), indent=2))
