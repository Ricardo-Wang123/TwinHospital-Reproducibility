from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from sandbox.experiments.linked.emergency import NOTE_HEADINGS, check_findings, render_soap_sections, soap_sections
from sandbox.experiments.linked.clinical import _Execution, _response_shape_errors
from sandbox.experiments.linked.chart import HistoryChart, NO_MEDICATION_RECORD
from sandbox.experiments.linked.contracts import TaskCondition
from sandbox.experiments.linked.ollama import response_format


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


def test_known_ips_medications_need_no_source_quote():
    questions = {"medicines": "What medicines are documented?"}
    findings = {"medicines": {"status": "verified", "source": "ips",
                               "finding": "The opening medication list was reviewed."}}
    sources = {"ips": {"medications": {"status": "reported", "entries": [{"name": "Fixture product"}]}}}
    assert not check_findings(questions, findings, sources, {})
    assert check_findings(questions, findings, {}, {}) == questions


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
