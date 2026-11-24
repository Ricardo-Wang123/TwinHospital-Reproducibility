from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from sandbox.experiments.linked.emergency import (NOTE_HEADINGS, chart_excerpt_present, check_findings,
                                                 render_soap_sections, soap_sections)
from sandbox.experiments.linked.clinical import _Execution, _response_shape_errors
from sandbox.experiments.linked.chart import HistoryChart, NO_MEDICATION_RECORD
from sandbox.experiments.linked.contracts import TaskCondition
from sandbox.experiments.linked.ed_treatment_review import complete_administration_instructions, missing_committed_products
from sandbox.experiments.linked.ollama import response_format, signed_order_format


@pytest.mark.parametrize("decision,expected", [("approve", "approve"), ("flag", "defer"), ("reject", "reject")])
def test_pharmacist_decision_controls_the_return(decision, expected):
    review = {"action": "review", "decision": decision, "flags": ["Evaluate the measured follow-up result."],
              "reason": "Review of a fictional prescription."}
    events = []
    workflow = SimpleNamespace(
        activity=lambda *args: None, prescribing_required=True, primary_treatment_only=False,
        post_signature_review=False, single_prescription=True, emergency=True,
        ed_checked_order={"findings": {}}, repeated_pharmacy_issues=lambda: False,
        ed_prescribing_replies={}, order_id="test:order:0", ed_checkpoint_index=0, ed_questions={},
        case={"formulary": []}, work_steps=lambda *args, **kwargs: range(1),
        respond=lambda *args, **kwargs: deepcopy(review),
        event=lambda event, **fields: events.append((event, fields)),
    )
    result = _Execution.review_prescription(
        workflow, {"items": []}, TaskCondition("prescription", "physician_prescribing"), {}, 0)
    assert result["decision"] == expected
    assert result["pharmacist_review"] == review
    assert ("required_followup" in result) == (decision != "approve")
    assert ("return_source" in result) == (decision != "approve")
    assert not workflow.ed_questions if decision == "approve" else workflow.ed_questions
    assert events[-1][0] == "prescription_reviewed"


def test_local_document_format_bounds_citations_to_available_dialogue():
    messages = [{"role": "user", "content": json.dumps({
        "action": "document", "allowed_source_scope": {"source_sequences": [2, 3]},
    })}]
    properties = response_format(messages)["properties"]["content"]["properties"]
    assert properties["source_sequences"]["items"]["enum"] == [2, 3]
    assert properties["source_sequences"]["maxItems"] == 2


def test_ips_history_lookup_requires_a_specific_term():
    observation = {"action": "assess", "ips_on_demand": True}
    messages = [{"role": "user", "content": json.dumps(observation)}]
    schema = response_format(messages)
    query = next(branch for branch in schema["oneOf"]
                 if branch["properties"]["action"]["enum"] == ["read_history"])
    assert "term" in query["required"]
    request = {"action": "read_history", "section": "ed_diagnostic_reports", "offset": 0, "limit": 5}
    assert _response_shape_errors("doctor", observation, request)
    assert _response_shape_errors("doctor", observation, {**request, "term": "   "})
    assert not _response_shape_errors("doctor", observation, {**request, "term": "Chest X-ray"})
    assert not _response_shape_errors("doctor", {"action": "assess"}, request)


def test_ips_prescribing_source_retry_uses_the_available_chart_sections():
    observation = {"action": "locate_ed_prescribing_source", "ips_on_demand": True,
                   "available_chart_sections": ["ed_diagnostic_reports", "current_use_record"],
                   "schema_correction": {"attempt": 1}}
    schema = response_format([{"role": "user", "content": json.dumps(observation)}])
    lookup, ask, reconcile = schema["oneOf"]
    assert "term" in lookup["required"]
    assert lookup["properties"]["section"]["enum"] == observation["available_chart_sections"]
    assert ask["properties"]["recipient"]["enum"] == ["patient"]
    assert reconcile["properties"]["issues"]["minItems"] == 1
    del observation["schema_correction"]
    assert response_format([{"role": "user", "content": json.dumps(observation)}]) == "json"


def test_note_draft_retry_requires_all_four_authored_sections():
    messages = [{"role": "user", "content": json.dumps({
        "action": "document", "allowed_source_scope": {"source_sequences": [2]},
        "schema_correction": {"ed_note_sections_required": True},
    })}]
    fields = response_format(messages)["properties"]["content"]["properties"]
    assert fields["sections"]["required"] == list(NOTE_HEADINGS)
    assert fields["source_sequences"]["items"]["enum"] == [2]
    sections = {heading: f"Authored {heading}." for heading in reversed(NOTE_HEADINGS)}
    assert soap_sections(render_soap_sections(sections)) == sections
    with pytest.raises(ValueError):
        render_soap_sections({"Subjective": "Only the first section."})


def test_repeated_citation_return_can_use_a_fresh_read_source():
    schema = signed_order_format({
        "signed_order_lines": [], "required_treatment_options": {"treatment": {}},
        "required_prescribing_checks": {"medicines": "Review documented medicines."},
        "allowed_response_actions": [], "ips_on_demand": True,
        "source_limits_review": {
            "citation_corrections": {"medicines": {"matching_read_sources": []}},
            "fresh_citation_sources": ["ips", "patient", "original_chart_unknown"],
        },
    })
    branches = schema["properties"]["check_results"]["properties"]["medicines"]["oneOf"]
    verified = [b for b in branches if b["properties"]["status"]["enum"] == ["verified"]]
    by_source = {tuple(b["properties"]["source"]["enum"]): b["required"] for b in verified}
    assert "quote" not in by_source[("ips",)]
    assert "quote" in by_source[("patient",)]
    assert ("original_chart_unknown",) not in by_source

    retained = {
        "signed_order_lines": [], "required_treatment_options": {"treatment": {}},
        "required_prescribing_checks": {"medicines": "Review documented medicines.", "q2": "Check the last dose time."},
        "allowed_response_actions": [], "ips_on_demand": True,
        "source_limits_review": {
            "fresh_citation_sources": ["ips", "patient"],
            "retained_check_results": {"medicines": {"status": "verified", "source": "ips", "finding": "Reviewed."}},
            "patient_reply_passages": ["这部分我不清楚。", "I used my inhaler yesterday."],
        },
    }
    checks = signed_order_format(retained)["properties"]["check_results"]
    assert checks["required"] == ["q2"]
    patient = [branch for branch in checks["properties"]["q2"]["oneOf"]
               if branch["properties"].get("source") == {"enum": ["patient"]}]
    assert [b["properties"]["quote"]["enum"] for b in patient] == [
        ["I used my inhaler yesterday."], ["这部分我不清楚。", "I used my inhaler yesterday."]]


def test_alternating_open_checks_do_not_reset_citation_recovery():
    questions = {"medicines": "Review medicines.", "q2": "Review the last dose."}
    evidence = {"medication_history": ["Listed medicine"], "ips": {}, "ed_chart": [], "chart_search_receipts": []}
    turns = []
    for pending in (questions, {"q2": questions["q2"]}, questions, {"q2": questions["q2"]}):
        turns.append({
            "checkpoint": 0, "outcome": "decide_after_source_review", "pending": pending,
            "source_rounds": {"medicines": 1, "q2": 2}, "treatment_conflicts": {}, "findings": {},
            "return_reason": dict.fromkeys(pending, "physician_reported_unresolved"),
        })
    workflow = SimpleNamespace(
        prescribing_history_sources=lambda: (evidence, evidence),
        result={"artifacts": {"ed_prescribing_checks": turns}},
        primary_treatment_only=True, ed_checkpoint_index=0, post_signature_review=True, ips_on_demand=True,
        ed_questions=questions, ed_checked_questions={"medicines": 1, "q2": 2},
    )
    observation = _Execution.prescribing_check_input(workflow)
    assert "ips" in observation["source_limits_review"]["fresh_citation_sources"]


def test_prescribing_scope_retry_addresses_all_selected_options():
    schema = signed_order_format({
        "signed_order_lines": [], "single_prescription": True, "orderable_drug_codes": [],
        "required_treatment_options": {"one": {}, "two": {}},
        "required_prescribing_checks": {"q2": "Review the available facts."},
        "allowed_response_actions": [], "ips_on_demand": True,
        "source_limits_review": {
            "treatments_marked_as_both_selected_and_blocked": {"q2": ["one", "two"]},
            "prior_selected_treatment_options": ["one", "two"],
        },
    })
    withdraw, keep = schema["anyOf"]
    treatments = withdraw["properties"]["treatments"]["properties"]
    assert all(treatments[k]["properties"]["items"]["maxItems"] == 0 for k in ("one", "two"))
    alternatives = keep["properties"]["check_results"]["properties"]["q2"]["oneOf"]
    assert all(item["properties"]["affected_treatments"]["maxItems"] == 0 for item in alternatives)


def test_route_encoding_retry_keeps_product_and_accepted_route_together():
    schema = signed_order_format({
        "signed_order_lines": [], "single_prescription": True, "orderable_drug_codes": ["a", "b"],
        "required_treatment_options": {"treatment": {}}, "required_prescribing_checks": {},
        "allowed_response_actions": [], "review_state": "encoding_correction",
        "business_validation": {"findings": [
            {"code": "unmapped_route", "field": "route", "drug_code": "a", "accepted_field_values": ["NG"]},
            {"code": "prn_instructions_missing", "field": "administration_instructions", "drug_code": "a"}]},
    })
    lines = schema["properties"]["treatments"]["properties"]["treatment"]
    branches = lines["properties"]["items"]["items"]["oneOf"]
    assert branches[0]["properties"]["drug_code"]["enum"] == ["a"]
    assert branches[0]["properties"]["route"]["enum"] == ["NG"]
    assert "administration_instructions" in branches[0]["required"]
    assert branches[1]["properties"]["drug_code"]["enum"] == ["b"]
    assert "administration_instructions" not in branches[1]["required"]


def test_known_ips_medications_need_no_source_quote():
    questions = {"medicines": "What medicines are documented?"}
    findings = {"medicines": {"status": "verified", "source": "ips",
                               "finding": "The opening medication list was reviewed."}}
    sources = {"ips": {"medications": {"status": "reported", "entries": [{"name": "Fixture product"}]}}}
    assert not check_findings(questions, findings, sources, {})
    assert check_findings(questions, findings, {}, {}) == questions


def test_committed_ingredient_can_be_present_in_a_signed_combination():
    products = [
        {"drug_code": "single", "name": "Ipratropium", "ingredient": "ipratropium"},
        {"drug_code": "combo", "name": "Ipratropium/albuterol inhaler", "ingredient": "ipratropium+albuterol"},
    ]
    response = {"reason": "I am prescribing ipratropium/albuterol for bronchodilation.",
                "order_actions": [{"kind": "new", "item": {"drug_code": "combo"}}]}
    assert missing_committed_products({"formulary": products}, response) == set()
    response["order_actions"] = []
    assert {"ipratropium", "albuterol"} <= missing_committed_products({"formulary": products}, response)


def test_numeric_chart_quote_can_include_its_unit_without_repeating_the_test_name():
    table = {"section": "ed_diagnostic_reports", "common": {"name": "Oxygen saturation"},
             "columns": ["value_raw", "unit_raw"], "rows": [["98.0000", "%"]]}
    assert chart_excerpt_present("98.0000%", [table])
    assert chart_excerpt_present("98.0000 %", [table])
    assert not chart_excerpt_present("98.0000 mg/dL", [table])


def test_instruction_only_retry_preserves_existing_order_fields():
    item = {"drug_code": "fixture", "frequency": "PRN", "dose": 1}
    prior = {"treatments": {"example": {"items": [item], "reason": "Prescriber selected this order."}}}
    completion = {"prior_submission": prior, "lines": ["1"]}
    response = {"administration_instructions": {"1": "Use for the reason documented in this order."}}
    corrected = complete_administration_instructions(response, completion)
    assert corrected["treatments"]["example"]["items"] == [
        {**item, "administration_instructions": response["administration_instructions"]["1"]}]
    assert "administration_instructions" not in item
    schema = response_format([{"role": "user", "content": json.dumps({
        "action": "revise_prescription", "complete_native_instructions": completion})}])
    assert schema["properties"]["administration_instructions"]["required"] == ["1"]


def test_chart_lookup_without_results_preserves_unknown():
    questions = {"q1": "Does the record establish the last dose time?"}
    findings = {"q1": {"status": "reviewed_with_limits", "source": "original_chart_unknown",
                       "quote": "last dose", "finding": "Searched the chart. The last dose is unknown.",
                       "decision_basis": "The selected order does not depend on this time.",
                       "affected_treatments": []}}
    sources = {"ips": {"medications": {}}, "chart_search_receipts": []}
    assert check_findings(questions, findings, sources, {}) == questions
    sources["chart_search_receipts"].append({"term": "last dose", "status": "no_recorded_result"})
    assert not check_findings(questions, findings, sources, {})


@pytest.mark.parametrize("later_record,earlier_record", [(False, False), (True, False), (False, True)])
def test_empty_opening_list_only_completes_check_without_other_records(later_record, earlier_record):
    chart = HistoryChart({
        "current_use_record": {"status": "unknown", "items": []},
        "historical_orders": [{"drug_name": "Fictional prior product"}] if earlier_record else [],
    })
    opening = chart.initial_view()
    row = chart.query("current_use_record")["records"][0]["record"]
    assert opening["current_use_record"]["record_label"] == row["record_label"] == NO_MEDICATION_RECORD
    assert opening["opening_summary"]["medications"]["record_label"] == NO_MEDICATION_RECORD
    assert row["status"] == "unknown"
    workflow = SimpleNamespace(
        history={"initial_view": opening},
        ed_review={"presentation": {"results": [
            {"section": "current_use_record", "common": row, "columns": [], "rows": [[]]},
        ]}},
        released_medication_reconciliations=lambda: [{"display": "Fictional reconciliation entry"}] if later_record else [],
        calls=[{"sequence": 1, "role": "doctor", "status": "completed", "observation": {"action": "consult"}}],
        events=[], context={"patient_id": "fiction", "encounter_id": "fiction:1"},
        patient_memory={"patient_facts": []}, ed_prescribing_replies={}, ed_checked_questions={},
        ed_questions={"medicines": "Review medication history."}, result={"artifacts": {}},
        primary_treatment_only=False, post_signature_review=False, independent_order_actions=True,
        verify_prescribing_coverage=lambda: None,
    )
    workflow.prescribing_history_sources = lambda: _Execution.prescribing_history_sources(workflow)
    inputs = _Execution.prescribing_check_input(workflow)
    if later_record or earlier_record:
        assert "medicines" in inputs["required_prescribing_checks"]
        assert "completed_prescribing_checks" not in inputs
        return
    assert "medicines" not in inputs["required_prescribing_checks"]
    auto = inputs["completed_prescribing_checks"]["medicines"]
    assert auto["quote"] == NO_MEDICATION_RECORD
    assert _Execution.complete_prescribing_checks(workflow, {}, {}, {"items": []})
    assert workflow.ed_checked_order["findings"]["medicines"] == auto
