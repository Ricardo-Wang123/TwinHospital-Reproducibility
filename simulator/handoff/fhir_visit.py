"""Export an OpenHIS-attested simulated encounter as an HL7 FHIR R4 document."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from html import escape
import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from .mimic_fhir import write_private


BASE = "https://twinhospital.invalid"


def signed_document(run, receipts, formulary):
    verified = {}
    for row in receipts:
        result = row["result"]
        if (row["run_id"] == run["run_id"] and row["case_id"] == run["case_id"]
                and result.get("readback_verified") and result.get("status") == "executed"):
            verified[row["operation"]] = row
    consultation = verified["encounter.begin_consultation"]["result"]["data"]
    attest = verified["order.attest"]
    lock = verified["note.attest"]
    note, business = run["artifacts"]["attested_note"], run["artifacts"]["prescription_business_state"]
    rx = run["artifacts"]["final_prescription"]["items"]
    order = attest["result"]["data"]
    native_orders = attest["result"]["readback"]
    saved = lock["result"]["readback"]
    doc_info = lock["result"]["data"]
    if (note["status"] != "locked" or doc_info["status"] != "locked" or saved["status"].upper() != "LOCKED"
            or str(saved["id"]) != str(doc_info["document_id"]) or note["version"] != doc_info["version"]
            or str(saved["encounterId"]) != str(consultation["encounter_id"])):
        raise ValueError("The recorded note lacks a matching signed encounter readback")
    editor = json.loads(saved["content"])
    bodies = [json.loads(span["text"]) for paragraph in editor["content"] for span in paragraph.get("content", [])
              if span.get("type") == "text"]
    if note["content"] not in bodies:
        raise ValueError("The attested text differs from the stored clinical document")
    if (order["status"] != "signed" or business["status"] != "signed" or not business["readback_verified"]
            or business["logical_order_id"] != order["order_id"] or business["items"] != rx
            or {str(i["requestId"]) for i in native_orders} != set(map(str, order["native_ids"]))
            or len(native_orders) != len(rx) or any(str(i["encounterId"]) != str(saved["encounterId"])
            or str(i["patientId"]) != str(saved["patientId"])
            or str(i.get("authoredById")) != str(consultation["doctor_id"]) for i in native_orders)):
        raise ValueError("The final prescription lacks a matching signed order readback")
    def uid(label):
        return str(uuid5(NAMESPACE_URL, f"{BASE}/{run['run_id']}/{label}"))
    def entry(resource):
        return {"fullUrl": "urn:uuid:" + resource["id"], "resource": resource}
    def ref(resource):
        return {"reference": "urn:uuid:" + resource["id"]}
    def recorded_time(value):
        value = datetime.fromisoformat(value)
        # NativeOpenHIS.now() journals the simulated instance's UTC+08:00 local time.
        return (value if value.tzinfo else value.replace(tzinfo=timezone(timedelta(hours=8)))).isoformat()
    signed = recorded_time(lock["at"])
    patient = {"resourceType":"Patient", "id":uid("patient/" + str(saved["patientId"]))}
    author = {"resourceType":"Practitioner", "id":uid("doctor/" + str(consultation["doctor_id"])),
              "identifier":[{"system":BASE + "/id/simulated-clinician", "value":str(consultation["doctor_id"])}]}
    # The order and document were signed while the ambulatory encounter was in progress.
    visit = {"resourceType":"Encounter", "id":uid("visit"), "status":"in-progress",
             "class":{"system":"http://terminology.hl7.org/CodeSystem/v3-ActCode","code":"AMB"},
             "subject":ref(patient)}
    is_ed = run.get('scenario_design') == 'emergency-v1'
    if is_ed:
        from sandbox.experiments.linked.emergency import verified_closure
        evidence = verified_closure(run)
        visit.update(status='finished', hospitalization={'dischargeDisposition': {'text': evidence['disposition']}})
        visit['class']['code'] = 'EMER'
    products = {row["drug_code"]: row for row in formulary}
    authored = {recorded_time(i["reqAuthoredTime"]) for i in native_orders if i.get("reqAuthoredTime")}
    dates = {dt[:10] for dt in authored}
    # Native readbacks may reorder rows. Keep only the precision shared by all signed items.
    authored_on = None
    if native_orders and all(i.get("reqAuthoredTime") for i in native_orders):
        if len(authored) == 1:
            authored_on = next(iter(authored))
        elif len(dates) == 1:
            authored_on = next(iter(dates))
    orders = []
    for i, item in enumerate(rx, start=1):
        dose = item["dose"]
        parts = [str(x) for x in (dose,item.get("dose_unit"),item.get("route"),item.get("frequency"),
                                  item.get("device"),item.get("administration_instructions")) if x not in (None,"")]
        course = item.get("intended_course", {})
        if course.get("duration_days") is not None:
            parts.append(f"Duration: {course['duration_days']} days")
        if course.get("start_offset_hours") is not None:
            parts.append(f"Start offset: {course['start_offset_hours']} hours from the study time origin")
        dosage = {"text":" ".join(parts)}
        if item.get("route"): dosage["route"] = {"text":str(item["route"])}
        if item.get("frequency"): dosage["timing"] = {"code":{"text":str(item["frequency"])}}
        if type(dose) in (float,int):
            dosage["doseAndRate"] = [{"doseQuantity":{"value":dose, **({"unit":item["dose_unit"]} if item.get("dose_unit") else {})}}]
        code = item["drug_code"]
        product = products.get(code, {})
        coding = [{"system": BASE+"/CodeSystem/formulary", "code": code, "display": product.get("name") or code}]
        if product.get("source_formulary_code"):
            from sandbox.experiments.linked.formulary_search import CODE_SYSTEM, NAME_SYSTEM
            coding.extend([{"system": CODE_SYSTEM, "code": product["source_formulary_code"]},
                           {"system": NAME_SYSTEM, "code": product["name"]}])
        resource = {"resourceType":"MedicationRequest", "id":uid(f"{order['order_id']}/item/{i}"),
            "status":"active", "intent":"order", "subject":ref(patient), "encounter":ref(visit), "requester":ref(author),
            "groupIdentifier":{"system":BASE+"/id/simulated-order","value":str(order["order_id"])},
            "medicationCodeableConcept":{"coding":coding}, "dosageInstruction":[dosage]}
        if type(item.get("quantity")) in (float,int):
            resource["dispenseRequest"] = {"quantity":{"value":item["quantity"],
                                               **({"unit":item["quantity_unit"]} if item.get("quantity_unit") else {})}}
        if authored_on:
            resource["authoredOn"] = authored_on
        orders.append(resource)
    narrative = {"status":"additional", "div":'<div xmlns="http://www.w3.org/1999/xhtml">' +
                 "".join("<p>"+escape(line)+"</p>" for line in note["content"]["text"].splitlines() if line.strip())+"</div>"}
    sections = [{"title":"Signed ED note" if is_ed else "Signed outpatient note", "text":narrative}]
    if is_ed:
        from sandbox.experiments.linked.emergency import soap_sections
        sections = [{'title': heading, 'text': {'status': 'generated',
                     'div': '<div xmlns="http://www.w3.org/1999/xhtml">' +
                            ''.join('<p>' + escape(line) + '</p>' for line in text.splitlines()) + '</div>'}}
                    for heading, text in soap_sections(note['content']['text']).items()]
        disposition = note['content']['ed_disposition']
        sections.append({'title':'ED disposition', 'text':{'status':'generated',
            'div':'<div xmlns="http://www.w3.org/1999/xhtml"><p>' + escape(disposition['code'] + ': ' + disposition['reason']) + '</p></div>'}})
    if orders:
        sections.append({"title":"Signed prescriptions", "entry":[ref(item) for item in orders]})
    composition = {"resourceType":"Composition","id":uid("note/" + str(saved["id"]) + "/" + str(note["version"])),
                   "status":"final","type":{"text":"Emergency department encounter note" if is_ed else "Outpatient encounter note"},"subject":ref(patient),
                   "encounter":ref(visit),"date":signed,"author":[ref(author)],"title":"Simulated ED encounter" if is_ed else "Simulated outpatient encounter",
                   "attester":[{"mode":"professional","time":signed,"party":ref(author)}], "section":sections}
    return {"resourceType":"Bundle","type":"document","identifier":{"system":BASE+"/id/document",
            "value":composition["id"]},"timestamp":signed,
            "entry":[entry(v) for v in (composition,patient,visit,author,*orders)]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for field in ("run", "receipts", "case", "output"):
        parser.add_argument("--"+field, required=True, type=Path)
    args = parser.parse_args()
    run = json.loads(args.run.read_bytes())
    case = json.loads(args.case.read_bytes())
    if case["case_id"] != run["case_id"]:
        raise ValueError("The run and case must match")
    with args.receipts.open() as f:
        from sandbox.experiments.linked.formulary_search import directory_for_case
        directory = directory_for_case(case, args.case)
        formulary = directory.products.values() if directory else case["formulary"]
        doc = signed_document(run, (json.loads(line) for line in f if line.strip()), formulary)
    write_private(args.output, doc)


if __name__ == "__main__":
    main()
