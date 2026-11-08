"""Export paper tables from counts, annotations, activities and observed effects.

No model calls or hospital writes occur here. See docs/experiments/code.md.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import csv
import hashlib
import json
import math
from pathlib import Path

from ..metrics import fact_metrics, prescription_metrics, effort_metrics, safety_metrics
from .table_specs import VERSION, MODULES, PATIENT_ITEMS, CLINICIAN_ITEMS, PRE_ROWS, spec

HOSPITAL_TABLES = {6, 9, 11, 13}
CONSTRUCTED = {"constructed", "engineering_example", "protocol_synthetic", "scripted", "model_generated"}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _cell(numerator=None, denominator=None, *, unit="%", reason=None, state=None, value=None, **detail):
    if state is None:
        state = "missing" if reason else ("undefined" if denominator == 0 else "value")
    if state == "value" and value is None:
        if numerator is None or denominator is None:
            state, reason = "missing", reason or "Required raw counts were not supplied"
        else:
            value = numerator / denominator * (100 if unit == "%" else 1)
    if value is not None and (not isinstance(value, (str, int, float)) or
                              isinstance(value, float) and not math.isfinite(value)):
        raise ValueError("non-finite or unsupported table value")
    return dict(state=state, value=value if state == "value" else None, numerator=numerator,
                denominator=denominator, unit=unit, reason=reason, **detail)


def _missing(reason):
    return _cell(reason=reason)


def _display(cell):
    if cell["state"] == "not_applicable":
        return "N/A"
    if cell["state"] == "undefined":
        return "UNDEFINED"
    if cell["state"] != "value":
        return ""
    value = cell["value"]
    return f"{value:.6f}".rstrip("0").rstrip(".") if isinstance(value, float) else str(value)


def _refs(rows):
    return sorted({str(ref) for row in rows for ref in row.get("evidence_refs", [])})


def _formal(table, rows, context):
    if not rows:
        return False
    if any(row.get("source_kind") in CONSTRUCTED or row.get("formal_eligible") is False for row in rows):
        return False
    if any(not row.get("evidence_refs") for row in rows):
        return False
    if table in HOSPITAL_TABLES:
        return all(row.get("source_kind") == "hospital_independent" for row in rows)
    if table in (3, 16, 17):
        return all(row.get("source_kind") == "human_participant" for row in rows)
    # Explicit attestation by the caller is needed; model execution alone is not clinical verification.
    return context.get("formal_eligible") is True and all(row.get("source_kind") for row in rows)


def _unique(rows):
    """One case/config/table; identical repeats collapse, conflicting repeats fail closed."""
    by_case = {}
    for row in rows:
        key = row.get("case_id")
        if not key:
            raise ValueError("observation lacks case_id")
        if key in by_case and _json(by_case[key].get("data")) != _json(row.get("data")):
            raise ValueError(f"conflicting observations for case {key}; select one artifact version explicitly")
        if key in by_case:
            by_case[key] = {**by_case[key], "evidence_refs": _refs([by_case[key], row])}
        else:
            by_case[key] = row
    return list(by_case.values())


def _prescription(rows):
    inputs = []
    for row in rows:
        raw = row["data"].get("prescription")
        if not isinstance(raw, dict):
            raise ValueError("data.prescription with final original-prescription decision is required")
        inputs.append({"case_id": row["case_id"], "status": row.get("status", "unknown"), **raw})
    summary = prescription_metrics(inputs)
    n, c = summary["n_cases"], summary["counts"]
    pairs = {"pass": (c["approved"], n), "severe_miss": (c["severe_missed"], c["severe"]),
             "fpr": (c["fp"], c["negative"]), "recall": (c["tp"], c["positive"]),
             "precision": (c["tp"], c["blocked"])}
    missing_decisions = summary["missing_decisions"]
    out = {key: _cell(*counts, reason="Missing final original-prescription decisions" if missing_decisions else None)
           for key, counts in pairs.items()}
    precision, recall = summary["precision"], summary["recall"]
    # Harmonic mean sufficient statistics retain missing-positive gold denominators.
    denominator = c["blocked"] + c["positive"]
    out["f1"] = _cell(2 * c["tp"], denominator,
                      reason="Missing final original-prescription decisions" if missing_decisions else None)
    if precision is None or recall is None:
        out["f1"] = _cell(2 * c["tp"], 0)
    return out, summary


def _clinical(table, rows):
    annotations, complete_cases, handoffs, signals = [], [], [], []
    required_by_module = {module: [] for module in MODULES}
    missing_annotations = missing_modules = missing_handoff = missing_signals = False
    for row in rows:
        data = row["data"]
        ann = data.get("annotations")
        if not isinstance(ann, list):
            missing_annotations = True
            continue
        identities = set()
        required = []
        for item in ann:
            if item.get("unit") not in ("claim", "required_item"):
                raise ValueError("clinical annotations require explicit unit=claim|required_item")
            identity = (item["unit"], item.get("item_id", item.get("claim_id")))
            if not identity[1] or identity in identities:
                raise ValueError("annotations need unique item_id/claim_id within each case and unit")
            identities.add(identity)
            if item["unit"] == "required_item":
                required.append(item)
                if table in (5, 6, 7):
                    if item.get("module") not in MODULES:
                        raise ValueError("required item lacks one of the four paper module assignments")
                    required_by_module[item["module"]].append(item)
        annotations.extend(ann)
        if table in (5, 6, 7):
            inventory_known = (data.get("module_inventory_complete") is True or
                               {item["module"] for item in required} == set(MODULES))
            missing_modules |= not inventory_known
            applicable = [item for item in required if item.get("applicable", True)]
            complete_cases.append(bool(applicable) and all(item["label"] == "correct" for item in applicable))
            usable = data.get("handoff_usable")
            if type(usable) is not bool:
                missing_handoff = True
            handoffs.append(usable)
            risks = data.get("risk_hits")
            if not isinstance(risks, list):
                missing_signals = True
            else:
                seen = set()
                for signal in risks:
                    if not signal.get("signal_id") or signal["signal_id"] in seen or type(signal.get("hit")) is not bool:
                        raise ValueError("risk_hits need unique signal_id and boolean hit for every reference signal")
                    seen.add(signal["signal_id"])
                signals.extend(risks)
    summary = fact_metrics(annotations)
    summary["n_cases"] = len(rows)
    summary["module_inventory_complete"] = not missing_modules
    summary["assessable_case_count"] = len(complete_cases)
    if missing_annotations:
        return {key: _missing("Missing independent item/claim annotation set") for key, _ in spec(table)[2]}, summary
    claim_counts = Counter(item["label"] for item in annotations if item["unit"] == "claim" and item.get("applicable", True))
    required = [item for item in annotations if item["unit"] == "required_item" and item.get("applicable", True)]
    assessable = sum(claim_counts[label] for label in ("correct", "incorrect", "unsupported"))
    omission = _cell(sum(item["label"] == "omitted" for item in required), len(required))
    if table in (8, 9):
        return {"accuracy": _cell(claim_counts["correct"], assessable),
                "unsupported": _cell(claim_counts["unsupported"], assessable), "omission": omission}, summary
    if table == 7:
        return {module: _cell(sum(item["label"] == "correct" for item in items if item.get("applicable", True)),
                             sum(item.get("applicable", True) for item in items),
                             reason="Incomplete four-module required-item inventory" if missing_modules else None)
                for module, items in required_by_module.items()}, summary
    summary["reference_risk_signals"] = len(signals)
    summary["identified_risk_signals"] = sum(s["hit"] for s in signals)
    return {
        "completeness": _cell(sum(complete_cases), len(rows), reason="Incomplete four-module required-item inventory" if missing_modules else None),
        "omission": omission,
        "factual_error": _cell(claim_counts["incorrect"] + claim_counts["unsupported"], assessable),
        "risk_recall": _cell(sum(s["hit"] for s in signals), len(signals), reason="Missing reference-signal hit annotations" if missing_signals else None),
        "handoff_usability": _cell(sum(value is True for value in handoffs), len(rows), reason="Missing independent hand-off usability annotation" if missing_handoff else None),
    }, summary


def _effort(table, row_id, rows):
    pre = table in (10, 11)
    results, activities = [], []
    capture_complete = True
    for row in rows:
        data = row["data"]
        if not isinstance(data.get("result"), dict) or not isinstance(data.get("activities"), list):
            raise ValueError("data.result and data.activities are required for human-effort tables")
        result = {"case_id": row["case_id"], "status": row.get("status", "unknown"), **data["result"]}
        result.setdefault("task", "preconsult" if pre else "documentation")
        result.setdefault("workflow", row_id.split(":")[1])
        capture_complete &= data.get("activity_capture_complete") is True
        case_activities = []
        for activity in data["activities"]:
            activity = {"case_id": row["case_id"], **activity}
            role = activity.get("role")
            if role in ("ai", "agent", "model", "patient"):
                continue
            if role == "attending_physician":
                activity["role"] = "physician"
            elif role in ("documentation_staff", "scribe"):
                activity["role"] = "staff"
            if activity.get("role") not in (("nurse", "physician", "doctor") if pre else ("staff", "physician", "doctor")):
                raise ValueError("unknown participating human role; map it explicitly")
            case_activities.append(activity)
        if not pre and data.get("activity_capture_complete") is True:
            rework = [activity for activity in case_activities if activity.get("activity_type") == "note_rework"]
            result["rework_seconds"] = sum(a["active_seconds"] for a in rework)
        if pre and "first_handoff_qualified" in result:
            first = result["first_handoff_qualified"]
            correction = result.get("downstream_supplementation_or_correction")
            # Either known disqualifier is sufficient; a failed first handoff
            # does not become unknown because no downstream encounter occurred.
            result["first_pass_completed"] = (False if first is False or correction is True else
                True if first is True and correction is False else None)
        results.append(result)
        activities.extend(case_activities)
    summary = effort_metrics(results, activities)
    n, nq = summary["n_cases"], summary["qualified_cases"]
    quality_missing = bool(summary["unknown_quality_cases"])
    reason = "Missing final-stage independent qualification" if quality_missing else (
        "Human active-activity capture is not declared complete" if not capture_complete else None)
    role_seconds = summary["role_active_seconds"]
    def time_cell(seconds):
        return _cell(seconds, nq * 60, unit="min/valid_task", reason=reason,
                     numerator_unit="seconds", qualified_cases=nq)
    out = {"human_time": time_cell(summary["total_active_seconds"]),
           "physician_time": time_cell(role_seconds.get("physician", 0))}
    if pre:
        out["nurse_time"] = time_cell(role_seconds.get("nurse", 0))
        redundant = sum(activity["active_seconds"] for activity in activities if activity["activity_type"] in
                        ("redundant_inquiry", "physician_redundant_inquiry"))
        out["redundant_time"] = _cell(redundant, n * 60, unit="min/case",
                                     reason=None if capture_complete else "Incomplete redundant-inquiry activity capture", numerator_unit="seconds")
        first = [result.get("first_pass_completed", result.get("first_pass_correct")) for result in results]
        out["first_pass"] = _cell(sum(value is True for value in first), n,
            reason="Missing independent first-hand-off quality/correction outcome" if summary["first_pass_completed_missing"] else None)
    else:
        out["staff_time"] = time_cell(role_seconds.get("staff", 0))
        for key, field, unit, scale in (("rework_rounds", "rework_rounds", "rounds/case", 1),
                                        ("rework_time", "rework_seconds", "min/case", 60)):
            values = [result.get(field) for result in results]
            out[key] = _cell(sum(value for value in values if value is not None), n * scale, unit=unit,
                reason=f"Missing {field}" if any(value is None for value in values) else None)
        out["turnaround"] = _cell(
            (summary["mean_turnaround_seconds"] or 0) * summary["turnaround_observed_cases"],
            n * 60, unit="min/case", reason="Missing consultation-end/final-attestation timestamps" if summary["turnaround_missing_cases"] else None,
            numerator_unit="seconds")
        first = [result.get("first_draft_accepted") for result in results]
        out["first_draft"] = _cell(sum(value is True for value in first), n,
            reason="Missing independent first-draft quality" if summary["first_draft_accepted_missing"] else None)
        if not summary["draft_metrics_applicable"]:
            for key in ("rework_rounds", "rework_time", "first_draft"):
                out[key] = _cell(state="not_applicable", reason="Manual baseline has no AI draft")
    return out, summary


def _safety(table, rows):
    outcomes = []
    for row in rows:
        outcome = row["data"].get("outcome")
        if not isinstance(outcome, dict):
            raise ValueError("data.outcome with request/effect disposition is required")
        outcome = {"case_id": row["case_id"], **outcome}
        outcome.setdefault("effect_evidence", "missing")
        if table == 15 and outcome.get("is_error") and outcome.get("outcome") == "sandbox_prevention":
            enforcement = outcome.get("enforcement_source", outcome.get("enforcement"))
            if isinstance(enforcement, dict):
                enforcement = enforcement.get("source", enforcement.get("layer"))
            if enforcement not in ("python_sandbox", "java_sandbox", "sandbox", "python_experiment", "java_experiment"):
                outcome["outcome"] = "downstream_safe" if enforcement in ("native_service", "database", "sql", "downstream") else "unresolved"
        if table == 15 and outcome.get("is_error") and not any(outcome.get(key) is True for key in
                ("unsafe_effect", "actual_effect", "prohibited_effect_observed")) and outcome.get("effect_evidence") in (None, "missing", "unknown", "unresolved"):
            outcome["outcome"] = "unresolved"
        outcomes.append(outcome)
    summary = safety_metrics(outcomes, end_to_end=table == 14)
    if table == 14:
        n, counts = summary["n_cases"], summary["counts"]
        duration_n = summary["duration_cases"]
        missing_duration = counts["missing_duration"] > 0
        return {"cases": _cell(n, 1, unit="cases"), "correct": _cell(counts["correct_completed"], n),
                "unsafe": _cell(counts["unsafe_execution"], n,
                    reason="Unknown prohibited-effect observations; observed count is only a lower bound" if summary["unresolved_effect_cases"] else None,
                    lower_bound_percent=100 * counts["unsafe_execution"] / n if n else None),
                "timeout": _cell(counts["timeout"], n),
                "mean": _cell(sum(outcome["duration_s"] for outcome in outcomes if outcome.get("status") == "correct_completed"
                    and not any(outcome.get(k) for k in ("unsafe_effect", "actual_effect", "prohibited_effect_observed"))
                    and outcome.get("effect_evidence") not in (None, "missing", "unknown", "unresolved") and outcome.get("duration_s") is not None),
                    duration_n, unit="s", reason="Missing duration for a correctly completed case" if missing_duration else None),
                "p95": _cell(denominator=duration_n, unit="s", value=summary["p95_completion_seconds"],
                    state="undefined" if not duration_n else None,
                    reason="Missing duration for a correctly completed case" if missing_duration else None,
                    aggregation="linear_interpolation_quantile", quantile=.95)}, summary
    category = outcomes[0]["category"]
    c = summary["by_category"][category]
    error_n, control_n = c["error_cases"], c["control_cases"]
    out = {"downstream": _cell(c["downstream_safe"], 1, unit="cases"),
           "unsafe": _cell(c["unsafe_execution"], 1, unit="cases"),
           "unresolved": _cell(c["unresolved"], 1, unit="cases")}
    for key, count, denominator in (("prevention", c["sandbox_prevention"], error_n),
                                    ("false_blocks", c["control_false_block"], control_n)):
        out[key] = _cell(count, denominator, unit="n (%)",
                         state="undefined" if not denominator else "value",
                         value=f"{count} ({100 * count / denominator:.2f}%)" if denominator else None,
                         percentage=100 * count / denominator if denominator else None)
    if error_n == 0:
        for key in ("downstream", "unsafe", "unresolved"):
            out[key] = _missing("No erroneous local test inputs for this category")
    return out, summary


def _materials(table, row_id, context):
    name = {1: "material_catalog", 2: "operation_inputs", 3: "participant_rows"}[table]
    if name not in context or not isinstance(context[name], list):
        return {key: _missing(f"context.{name} not supplied") for key, _ in spec(table)[2]}, {}, []
    catalog = context[name]
    if table == 1:
        rows = [row for row in catalog if row.get("task") == row_id]
        out, detail = {}, {}
        for column, split in (("anchors", "anchor"), ("tests", "test")):
            selected = [row for row in rows if row.get("split") == split]
            unique = {row.get("case_id") for row in selected if row.get("case_id")}
            if split == "anchor":
                eligible = [row for row in selected if row.get("source_kind") in ("real_deidentified", "hospital_record")]
            else:
                eligible = [row for row in selected if row.get("verified") is True and
                            row.get("source_kind") not in ("constructed", "engineering_example", "scripted")]
            ids = {row["case_id"] for row in eligible if row.get("case_id")}
            detail[split] = {"catalog_cases": len(unique), "eligible_cases": len(ids),
                             "source_groups": sorted({row["source_group_id"] for row in selected if row.get("source_group_id")})}
            out[column] = _cell(len(ids), 1, unit="cases", reason=("No independently sourced real anchor records" if split == "anchor" else
                "No clinically verified synthetic test cases") if not ids else None)
        overlap = set(detail["anchor"]["source_groups"]) & set(detail["test"]["source_groups"])
        detail["source_group_overlap"] = sorted(overlap)
        if overlap:
            out = {key: _missing("Anchor and test source groups overlap") for key in out}
        return out, detail, rows
    if table == 2:
        rows = [row for row in catalog if row.get("evaluation_set") == row_id]
        ids = [row.get("case_id", row.get("test_id")) for row in rows]
        if any(not value for value in ids):
            raise ValueError("operation material needs case_id or test_id")
        detail = {"unique_cases": len(set(ids)), "categories": {}}
        for category, _ in spec(15)[1]:
            local = {row.get("case_id", row.get("test_id")): row for row in rows if row.get("category") == category}
            detail["categories"][category] = {"error": sum(row.get("is_error") is True for row in local.values()),
                                               "control": sum(row.get("is_error") is False for row in local.values())}
        return {"cases": _cell(len(set(ids)), 1, unit="cases", reason="No matching operation inputs" if not rows else None)}, detail, rows
    rows = [row for row in catalog if row.get("participant_group") == row_id]
    people = {row["participant_id"] for row in rows if row.get("participant_id")}
    items = PATIENT_ITEMS if row_id == "patient" else CLINICIAN_ITEMS
    valid_people, paired_people = set(), set()
    for person in people:
        valid = {(r.get("dimension"), r.get("workflow")) for r in rows if r.get("participant_id") == person and
                 r.get("valid", True) and type(r.get("score")) is int and 1 <= r["score"] <= 5}
        if valid:
            valid_people.add(person)
        if all((item, workflow) in valid for item, _ in items for workflow in ("existing", "human_ai")):
            paired_people.add(person)
    detail = {"unique_participants": len(people), "any_valid_participants": len(valid_people),
              "complete_paired_participants": len(paired_people), "incomplete_participants": len(people - paired_people)}
    return {"participants": _cell(len(people), 1, unit="participants", reason="No participant records" if not people else None)}, detail, rows


def _survey(table, row_id, context, observations):
    rows = list(context.get("participant_rows", []))
    rows.extend({"source_kind": row.get("source_kind"), "evidence_refs": row.get("evidence_refs", []),
                 **row["data"]["response"]} for row in observations
                if isinstance(row.get("data", {}).get("response"), dict))
    group = "patient" if table == 16 else "clinician"
    item, workflow = (row_id, None) if table == 16 else row_id.rsplit(":", 1)
    aliases = context.get("questionnaire_dimension_map", {})
    rows = [row for row in rows if row.get("participant_group") == group and
            aliases.get(row.get("dimension"), row.get("dimension")) == item]
    cells, detail = {}, {}
    all_people = {row["participant_id"] for row in context.get("participant_rows", []) if
                  row.get("participant_group") == group and row.get("participant_id")}
    for mode in (("existing", "human_ai") if table == 16 else (workflow,)):
        grouped = defaultdict(list)
        selected = [row for row in rows if row.get("workflow") == mode]
        for row in selected:
            if not row.get("participant_id"):
                raise ValueError("questionnaire needs participant_id")
            score = row.get("score")
            if score is not None and (type(score) is not int or not 1 <= score <= 5):
                raise ValueError("questionnaire scores must be integer 1..5 or null")
            if row.get("valid", True) and score is not None:
                grouped[row["participant_id"]].append(row)
        ratings, conflicts = [], []
        for person, responses in grouped.items():
            # Identical repeated imports collapse. Clinician distributions require one explicit integer response.
            unique = {_json({key: value for key, value in response.items() if key != "evidence_refs"}): response for response in responses}
            responses = list(unique.values())
            if group == "patient":
                by_item = defaultdict(list)
                for response in responses:
                    by_item[response.get("item_id", item)].append(response["score"])
                ratings.append(sum(sum(values) / len(values) for values in by_item.values()) / len(by_item))
            else:
                final = [response for response in responses if response.get("is_final") is True]
                if len(responses) == 1:
                    ratings.append(responses[0]["score"])
                elif len(final) == 1:
                    ratings.append(final[0]["score"])
                else:
                    conflicts.append(person)
        n = len(ratings)
        detail[mode] = {"valid_participants": n, "missing_participants": len(all_people) - n,
                        "conflicting_participants": conflicts,
                        "score_counts": {str(score): ratings.count(score) for score in range(1, 6)}}
        reason = "Conflicting repeat ratings need exactly one is_final=true response" if conflicts else (
            "No valid participant-level responses" if not n else None)
        mean = _cell(sum(ratings), n, unit="score (1–5)", reason=reason)
        if table == 16:
            cells[mode] = mean
        else:
            cells["mean"] = mean
            cells["workflow"] = _cell(value="Existing" if mode == "existing" else "Human–AI", unit="label")
            for score in range(1, 6):
                cells[f"score_{score}"] = _cell(ratings.count(score), n, reason=reason)
    return cells, detail, rows


def export_tables(run_dir: Path, observations: list[dict], context: dict) -> dict:
    """Write all 17 paper-shaped CSVs plus cell counts, evidence and missing inputs.

    Empty observations are accepted; absent observations are never fabricated.
    Files are replaced only in this run's ``tables`` directory.
    """
    run_dir = Path(run_dir)
    destination = run_dir / "tables"
    destination.mkdir(parents=True, exist_ok=True)
    evidence, missing, files, table_metadata = [], [], {}, {}
    context = dict(context)
    for index, row in enumerate(observations):
        if not isinstance(row, dict) or not isinstance(row.get("data"), dict) or not isinstance(row.get("table_ids"), list):
            raise ValueError(f"observation {index} requires data object and table_ids list")
        if any(type(number) is not int or not 1 <= number <= 17 for number in row["table_ids"]):
            raise ValueError(f"observation {index} has invalid table_ids")
    mapping = context.get("row_id_by_config", {})
    def matches(row, number, row_id):
        if number not in row["table_ids"]:
            return False
        if number == 14:
            return True
        if number == 15:
            return row.get("row_id", row.get("data", {}).get("outcome", {}).get("category")) == row_id
        return row.get("row_id", mapping.get(row.get("config_id"), row.get("config_id"))) == row_id
    for number in range(1, 18):
        first_header, row_specs, column_specs = spec(number)
        csv_rows, summaries = [], {}
        for row_id, row_label in row_specs:
            rows = [row for row in observations if matches(row, number, row_id)]
            sources = rows
            try:
                rows = _unique(rows)
                sources = rows
                if number <= 3:
                    cells, summary, sources = _materials(number, row_id, context)
                elif number in (16, 17):
                    cells, summary, sources = _survey(number, row_id, context, rows)
                elif not rows:
                    cells, summary = {key: _missing("No observations for this table/configuration") for key, _ in column_specs}, {}
                elif number == 4:
                    cells, summary = _prescription(rows)
                elif number in (5, 6, 7, 8, 9):
                    cells, summary = _clinical(number, rows)
                elif number in (10, 11, 12, 13):
                    cells, summary = _effort(number, row_id, rows)
                else:
                    cells, summary = _safety(number, rows)
            except (ValueError, TypeError, KeyError) as exc:
                cells, summary = {key: _missing(f"Invalid or incomplete raw input: {exc}") for key, _ in column_specs}, {"input_error": str(exc)}
            # Even an empty manual row has the paper's structural N/A cells.
            if number in (12, 13) and row_id == "documentation:manual_documentation":
                for key in ("rework_rounds", "rework_time", "first_draft"):
                    cells[key] = _cell(state="not_applicable", reason="Manual baseline has no AI draft")
            if number == 17:
                workflow = row_id.rsplit(":", 1)[1]
                cells["workflow"] = _cell(value="Existing" if workflow == "existing" else "Human–AI", unit="label")
            record = {first_header: row_label}
            complete = True
            for key, header in column_specs:
                cell = cells.get(key, _missing("Metric input not available"))
                record[header] = _display(cell)
                entry = dict(table=number, row_id=row_id, row_label=row_label, column_id=key, column_label=header,
                    metric_version=VERSION, **cell, case_ids=sorted({str(row.get("case_id", row.get("participant_id"))) for row in sources
                        if row.get("case_id", row.get("participant_id"))}),
                    config_ids=sorted({str(row["config_id"]) for row in sources if row.get("config_id")}),
                    source_kinds=sorted({str(row.get("source_kind", "unknown")) for row in sources}),
                    evidence_refs=_refs(sources), formal_eligible=_formal(number, sources, context),
                    artifact_versions=sorted({str(row.get("artifact_version", row.get("data", {}).get("artifact_version"))) for row in sources
                        if row.get("artifact_version", row.get("data", {}).get("artifact_version"))}),
                    stages=sorted({str(row.get("stage", row.get("data", {}).get("stage"))) for row in sources
                        if row.get("stage", row.get("data", {}).get("stage"))}))
                evidence.append(entry)
                if cell["state"] in ("missing", "undefined"):
                    complete = False
                    missing.append({"table": number, "row_id": row_id, "column_id": key,
                                    "state": cell["state"], "reason": cell["reason"] or "Zero denominator"})
            csv_rows.append(record)
            summaries[row_id] = {"complete": complete, "observations": len(rows), "summary": summary,
                                 "formal_eligible": complete and _formal(number, sources, context)}
        path = destination / f"table_{number:02d}.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=[first_header] + [header for _, header in column_specs])
            writer.writeheader()
            writer.writerows(csv_rows)
        files[str(number)] = str(path)
        table_metadata[str(number)] = {"file": path.name, "rows": summaries,
            "complete": all(row["complete"] for row in summaries.values()),
            "formal_eligible": all(row["formal_eligible"] for row in summaries.values())}
    # Retain medical-model time results without silently averaging model families into the three-row main table.
    for number in (10, 11):
        _, _, columns = spec(number)
        appendix = destination / f"table_{number:02d}_medical_stratum.csv"
        with appendix.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=["Configuration"] + [header for _, header in columns])
            writer.writeheader()
            for row_id, label in PRE_ROWS:
                if not row_id.endswith(":medical"):
                    continue
                rows = [row for row in observations if matches(row, number, row_id)]
                try:
                    cells, summary = _effort(number, row_id, _unique(rows)) if rows else ({}, {})
                except (ValueError, KeyError, TypeError) as exc:
                    cells, summary = {}, {"input_error": str(exc)}
                writer.writerow({"Configuration": label, **{header: _display(cells.get(key, _missing("No medical-stratum input"))) for key, header in columns}})
                table_metadata[str(number)].setdefault("medical_stratum", {})[row_id] = summary
                for key, header in columns:
                    cell = cells.get(key, _missing("No medical-stratum input"))
                    evidence.append(dict(table=number, stratum="medical_appendix", row_id=row_id,
                        row_label=label, column_id=key, column_label=header, metric_version=VERSION, **cell,
                        case_ids=sorted({str(row["case_id"]) for row in rows if row.get("case_id")}),
                        config_ids=sorted({str(row["config_id"]) for row in rows if row.get("config_id")}),
                        source_kinds=sorted({str(row.get("source_kind", "unknown")) for row in rows}),
                        evidence_refs=_refs(rows), formal_eligible=_formal(number, rows, context),
                        artifact_versions=sorted({str(row["artifact_version"]) for row in rows if row.get("artifact_version")}),
                        stages=sorted({str(row["stage"]) for row in rows if row.get("stage")})))
                    if cell["state"] in ("missing", "undefined"):
                        missing.append(dict(table=number, stratum="medical_appendix", row_id=row_id,
                            column_id=key, state=cell["state"], reason=cell["reason"] or "Zero denominator"))
    digests = {}
    for ref in {ref for cell in evidence for ref in cell["evidence_refs"]}:
        local = Path(ref)
        if not local.is_absolute():
            local = run_dir / local
        digests[ref] = hashlib.sha256(local.read_bytes()).hexdigest() if local.is_file() else None
    metadata = dict(metric_version=VERSION, generated_at=datetime.now(timezone.utc).isoformat(),
        source_kind=context.get("source_kind", "mixed_or_unavailable"),
        formal_eligible=all(table["formal_eligible"] for table in table_metadata.values()),
        table_metadata=table_metadata, model_roles=context.get("model_roles", {}), evidence_sha256=digests,
        missing_conventions={"missing": "empty CSV field", "undefined": "UNDEFINED", "not_applicable": "N/A", "zero": "0"},
        decisions={"preconsult_effort_main_model": "foundation", "medical_effort": "separate appendix",
                   "effort_cohort": "reuse E2/E3; never additional cases", "p95": "linear interpolation at (n-1)*0.95"},
        input_observations_sha256=hashlib.sha256(_json(observations).encode()).hexdigest(),
        input_context_sha256=hashlib.sha256(_json(context).encode()).hexdigest())
    (destination / "table_metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    (destination / "missing_inputs.json").write_text(json.dumps(missing, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    with (destination / "cell_evidence.jsonl").open("w", encoding="utf-8") as stream:
        for entry in evidence:
            stream.write(_json(entry) + "\n")
    return {"tables_dir": str(destination), "files": files, "missing_cells": len(missing),
            "complete_tables": [int(number) for number, table in table_metadata.items() if table["complete"]],
            "formal_eligible": metadata["formal_eligible"], "metric_version": VERSION}
