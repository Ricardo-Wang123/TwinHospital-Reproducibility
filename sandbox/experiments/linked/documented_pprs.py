"""Development-only documented rule adapter; independent labels stay separate.

The working clinical context may carry ``documented_rule_context`` containing
structured, role-visible facts in clinical_faults' declared schema. All tested
reviewers must receive that same context. This adapter does not read case
evaluation records, mutation diffs, prior outcomes or hidden source histories.
Missing facts remain missing; it never treats old orders as current exposure.
"""
from copy import deepcopy

from .clinical_faults import VISIBLE_CONTEXT_KEYS, evaluate_rule, registry, _guard
from .contracts import digest
from .rules import EngineeringPPRS


VERSION = "documented-candidate-pprs-dev-v1"


def adapt_visible_inputs(prescription, clinical_context):
    """Add only stable business identifiers; never manufacture clinical facts."""
    if not isinstance(prescription, dict) or not isinstance(prescription.get("items"), list):
        raise ValueError("prescription.items is required")
    if not isinstance(clinical_context, dict):
        raise ValueError("clinical context must be an object")
    _guard(prescription)
    _guard(clinical_context)
    if "documented_rule_context" in clinical_context:
        evidence = clinical_context["documented_rule_context"]
        declared = True
    elif clinical_context and set(clinical_context) <= VISIBLE_CONTEXT_KEYS:
        # Direct clinical_faults.actor_input is already a shared clinical view.
        evidence, declared = clinical_context, True
    else:
        evidence, declared = {}, False
    if not isinstance(evidence, dict) or set(evidence) - VISIBLE_CONTEXT_KEYS:
        raise ValueError("documented_rule_context contains unsupported/hidden fields")
    candidate = deepcopy(prescription)
    assigned = []
    identity = digest(prescription)[:20]
    for index, item in enumerate(candidate["items"]):
        if not isinstance(item, dict):
            raise ValueError("prescription item must be an object")
        if not item.get("order_id"):
            item["order_id"] = f"candidate_{identity}_{index}"
            assigned.append({"item_index": index, "order_id": item["order_id"]})
    return {"prescription": candidate, "clinical_context": deepcopy(evidence),
        "adaptation": {"structured_context_supplied": declared,
            "assigned_business_identifiers": assigned, "clinical_facts_inferred": False,
            "source_claims_independently_verified": False,
            "input_hash": digest({"prescription": prescription, "clinical_context": clinical_context}),
            "actor_visible_evidence_hash": digest(evidence)}}


class DocumentedCandidatePPRS:
    """A candidate decision system, not the oracle used to measure its accuracy.

    Definite findings reject, uncertain/unsupported coverage flags a pharmacist,
    and scoped nonviolations can pass only the enabled narrow checks. Even a
    pass never establishes overall clinical normality. Do not report sensitivity
    against these same rule outputs; two independent reviews remain required.
    """
    rule_version = VERSION
    engineering_only = False
    development_only = True

    def __init__(self, formulary, *, rule_ids=None):
        self.structural = EngineeringPPRS(formulary)
        self.registry = registry()
        known = {row["rule_id"] for row in self.registry["rules"]}
        self.rule_ids = tuple(rule_ids if rule_ids is not None else sorted(known))
        if not self.rule_ids or len(set(self.rule_ids)) != len(self.rule_ids) or set(self.rule_ids) - known:
            raise ValueError("declare distinct documented registry rule IDs")
        self.registry_hash = digest(self.registry)

    def review(self, prescription, clinical_context=None):
        context = {} if clinical_context is None else clinical_context
        adapted = adapt_visible_inputs(prescription, context)
        structural = self.structural.review(prescription, context)
        findings = []
        categories = {row["rule_id"]: row["category"] for row in self.registry["rules"]}
        for rule_id in self.rule_ids:
            try:
                value = evaluate_rule(rule_id, adapted["prescription"], adapted["clinical_context"])
            except ValueError as exc:
                # Unusable clinical evidence is not an implicit negative label.
                value = {"rule_id": rule_id, "category": categories[rule_id], "status": "indeterminate",
                    "reason": "Structured clinical evidence could not be evaluated: " + str(exc),
                    "reference_status": "independent_review_pending", "clinical_normality": "not_established"}
            findings.append(value)
        violations = [r for r in findings if r["status"] == "violation"]
        uncertain = [r for r in findings if r["status"] == "indeterminate"]
        scoped = [r for r in findings if r["status"] == "scoped_nonviolation"]
        flags = list(structural["flags"])
        flags += ["documented_violation:" + r["rule_id"] for r in violations]
        flags += ["clinical_evidence_indeterminate:" + r["rule_id"] for r in uncertain]
        if not violations and not uncertain and not scoped:
            flags.append("outside_documented_clinical_rule_scope")
        if structural["decision"] == "reject" or violations:
            decision = "reject"
        elif structural["decision"] == "flag" or uncertain or not scoped:
            decision = "flag"
        else:
            decision = "pass"
        return {"decision": decision, "flags": flags, "rule_version": self.rule_version,
            "engineering_only": False, "development_only": True,
            "structural_review": structural, "documented_rule_findings": findings,
            "registry_hash": self.registry_hash, "adaptation": adapted["adaptation"],
            "clinical_clearance": "not_established",
            "reference_status": "independent_review_pending", "clinical_gold_label": None,
            "pass_meaning": "No enabled narrow rule found a violation in its declared scope; full prescription safety is unassessed.",
            "baseline_workflow": "reject returns to physician; flag receives modeled pharmacist review; pass follows existing flow"}
