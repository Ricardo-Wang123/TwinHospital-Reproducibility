from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from sandbox.experiments.linked.clinical import _Execution
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
