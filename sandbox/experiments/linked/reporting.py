"""Trace-derived engineering summaries for fully virtual experiments."""
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timezone
import html
import json
from pathlib import Path

from .contracts import digest


def _write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    path.chmod(0o600)


def load_runs(root):
    """Return completed envelopes and observable incomplete/crashed directories."""
    root = Path(root)
    runs, issues = [], []
    for path in sorted(root.rglob("run.json")):
        try:
            value = json.loads(path.read_text())
            if value.get("schema_version") != "twinhospital.linked.run/1":
                continue
            value["local_evidence_path"] = str(path)
            runs.append(value)
        except (OSError, ValueError, AttributeError) as exc:
            issues.append({"path": str(path), "status": "unreadable_run", "error_type": type(exc).__name__})
    for journal in sorted(root.rglob("events.jsonl")):
        if not (journal.parent / "run.json").exists():
            issues.append({"path": str(journal.parent), "status": "interrupted_without_final_envelope"})
    # Attempt indices preserve planned configurations with no final run envelope
    # (budget stop, unavailable anchor input, or a process interruption).
    for path in sorted(root.rglob("attempts.json")):
        try:
            index = json.loads(path.read_text())
            for label, record in index.get("attempts", {}).items():
                if not record.get("result_path"):
                    issues.append({"path": str(path), "case_id": index.get("case_id"),
                        "attempt": label, "status": record.get("status", "unknown_attempt_state"),
                        "config_id": record.get("config_id"), "dependency": record.get("dependency")})
        except (OSError, ValueError, AttributeError) as exc:
            issues.append({"path": str(path), "status": "unreadable_attempt_index", "error_type": type(exc).__name__})
    return runs, issues


def workflow_interval(run):
    """Observed queue admission to paid read-back; never a safety certification."""
    selected = {}
    for event in run.get("events", []):
        if event.get("event") != "business_operation":
            continue
        response = event.get("response") or {}
        if response.get("status") not in {"executed", "replayed"} or response.get("readback_verified") is not True:
            continue
        operation = event.get("operation")
        if operation == "queue.enter" and "queue" not in selected:
            selected["queue"] = event
        elif operation == "payment.commit" and (response.get("data") or {}).get("status") == "paid":
            selected["paid"] = event
    result = {"boundary": "verified_queue_entry_to_verified_paid_readback",
        "registration_included": False, "observed_seconds": None,
        "observation_status": "missing_boundary_evidence", "formal_correct_completion": None,
        "whole_run_safety_observation": "unknown", "observation_window_timeout": None,
        "formal_latency_eligible": None}
    if set(selected) != {"queue", "paid"}:
        return result
    try:
        start, end = [datetime.fromisoformat(selected[k]["occurred_at"]) for k in ("queue", "paid")]
        if start.tzinfo is None or end.tzinfo is None or end < start:
            raise ValueError("invalid timestamp ordering")
        result.update(observed_seconds=(end-start).total_seconds(), observation_status="observed",
            start_event_sequence=selected["queue"].get("sequence"),
            end_event_sequence=selected["paid"].get("sequence"))
    except (KeyError, TypeError, ValueError):
        result["observation_status"] = "invalid_boundary_timestamps"
    return result


def summarize_runs(runs, *, budget_summary=None, incomplete=None):
    """All attempts remain visible; task rows and patients are different units."""
    keys = [(r.get("run_id"), r.get("case_id"), r.get("config_id")) for r in runs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate run envelopes would double-count execution attempts")
    groups = defaultdict(list)
    model_calls = defaultdict(lambda: {"calls": 0, "failed_calls": 0, "wall_seconds": 0.0,
                                      "prompt_tokens": 0, "completion_tokens": 0,
                                      "reasoning_tokens": 0, "usage_missing_calls": 0})
    model_request_ids = set()
    duplicate_requests = []
    roles = defaultdict(lambda: {"activities": 0, "modeled_active_seconds": 0.0})
    failure_types = Counter()
    incomplete_clinical_decisions = Counter()
    disposition_note_outcomes = Counter()
    disposition_note_failures = Counter()
    operation_counts = Counter()
    prescription_decisions = Counter()
    candidate_counts = Counter()
    history_reads = Counter()
    schema_counts = Counter()
    history_read_wall_seconds = 0.0
    native_verified, sidecar_verified, unverified = 0, 0, 0
    for run in runs:
        groups[(run.get("evaluation_unit"), run.get("config_id"), run.get("data_kind"))].append(run)
        artifacts = run.get("artifacts", {})
        disposition = artifacts.get("clinical_disposition") or {}
        if run.get("status") == "incomplete_clinical_decision" or disposition.get("status") == "incomplete_clinical_decision":
            kind = artifacts.get("prescription_eligibility", {}).get("decision")
            if artifacts.get("unresolved_prescription_adjudication", {}).get("status") == "deferred":
                kind = "prescription_adjudication_deferred"
            if artifacts.get("prescription_termination", {}).get("status"):
                kind = artifacts["prescription_termination"]["status"]
            incomplete_clinical_decisions[kind or "other_recorded_clinical_decision"] += 1
        if run.get("status") not in {"completed", "incomplete_clinical_decision"}:
            failure_types[run.get("error_type", "unspecified_failure")] += 1
        if disposition:
            outcome = artifacts.get("disposition_documentation_outcome") or {}
            disposition_note_outcomes[outcome.get("status", "unrecorded")] += 1
            if outcome.get("status") in {"failed", "stopped_budget", "stopped_provider"}:
                disposition_note_failures[outcome.get("error_type", "unspecified_failure")] += 1
        if "original_prescription" in artifacts:
            candidate_counts["recorded_candidates"] += 1
            review = artifacts.get("original_prescription_review")
            if isinstance(review, dict):
                prescription_decisions[review.get("decision", "unknown")] += 1
                candidate_counts["with_recorded_review"] += 1
            else:
                candidate_counts["review_not_observed"] += 1
            versions = artifacts.get("prescription_versions", [])
            native_attempted = versions[0].get("native_write_attempted") if versions else None
            candidate_counts["native_write_attempted" if native_attempted is True else
                ("audit_only_candidate" if native_attempted is False else "native_attempt_status_unrecorded")] += 1
        for call in run.get("calls", []):
            if call.get("status") == "invalid_schema":
                schema_counts["invalid_responses_retained"] += 1
            if (call.get("schema_correction_attempt") or 0) > 0:
                schema_counts["correction_calls"] += 1
                if call.get("status") == "completed":
                    schema_counts["format_compliant_correction_responses"] += 1
            request_id = call.get("request_id")
            if request_id and request_id in model_request_ids:
                duplicate_requests.append(request_id)
                continue
            if request_id:
                model_request_ids.add(request_id)
            key = (call.get("role"), call.get("model_name") or "unrecorded_model", call.get("model_family"))
            row = model_calls[key]
            row["calls"] += 1
            row["failed_calls"] += call.get("status") != "completed"
            row["wall_seconds"] += float(call.get("wall_seconds") or 0)
            usage = call.get("usage")
            if not isinstance(usage, dict) or not all(type(usage.get(k)) is int for k in ("prompt_tokens", "completion_tokens")):
                row["usage_missing_calls"] += 1
            else:
                row["prompt_tokens"] += usage["prompt_tokens"]
                row["completion_tokens"] += usage["completion_tokens"]
                details = usage.get("completion_tokens_details") or {}
                if isinstance(details, dict) and type(details.get("reasoning_tokens")) is int:
                    row["reasoning_tokens"] += details["reasoning_tokens"]
        for activity in run.get("activities", []):
            key = (activity.get("role"), activity.get("timing_source", run.get("timing_source")))
            roles[key]["activities"] += 1
            roles[key]["modeled_active_seconds"] += float(activity.get("active_seconds") or 0)
        for event in run.get("events", []):
            if event.get("event") == "history_query_requested":
                history_reads["requested"] += 1
            if event.get("event") == "history_lookup":
                history_reads[event.get("result_status", "unknown")] += 1
                history_read_wall_seconds += float(event.get("wall_seconds") or 0)
            if event.get("event") == "business_operation":
                response = event.get("response") or {}
                operation_counts[(event.get("operation"), response.get("status"))] += 1
                if response.get("readback_verified") is True:
                    if response.get("native_effect") is True:
                        native_verified += 1
                    else:
                        sidecar_verified += 1
                else:
                    unverified += 1
    configuration_rows = []
    for (unit, condition, kind), values in sorted(groups.items(), key=lambda x: str(x[0])):
        count = len(values)
        complete = sum(r.get("status") == "completed" for r in values)
        configuration_rows.append({"evaluation_unit": unit, "config_id": condition, "data_kind": kind,
            "attempts": count, "distinct_case_ids": len({r.get("case_id") for r in values}),
            "completed": complete, "failed_or_unresolved": count-complete,
            "incomplete_clinical_decisions": sum(r.get("status") == "incomplete_clinical_decision" for r in values),
            "completion_rate": complete/count if count else None,
            "native_complete_encounters": sum(r.get("native_complete") is True for r in values),
            "total_wall_seconds": sum(float(r.get("wall_seconds") or 0) for r in values)})
    complete_runs = [r for r in runs if r.get("evaluation_unit") == "complete_encounter"]
    intervals = [{"run_id": r.get("run_id"), "case_id": r.get("case_id"), **workflow_interval(r)}
                 for r in complete_runs]
    summary = {"schema_version": "twinhospital.execution.summary/1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "evidence_manifest_hash": digest(sorted(keys, key=str)),
        "attempts": len(runs), "distinct_case_ids": len({r.get("case_id") for r in runs}),
        "distinct_patients": None, "patient_count_note": "Case IDs alone do not establish distinct source patients; use the cohort manifest.",
        "complete_encounter_attempts": len(complete_runs),
        "complete_encounters_observed": sum(r.get("status") == "completed" for r in complete_runs),
        "native_complete_encounters": sum(r.get("native_complete") is True for r in complete_runs),
        "data_kinds": dict(Counter(r.get("data_kind", "unknown") for r in runs)),
        "configurations": configuration_rows, "failure_types": dict(failure_types),
        "incomplete_clinical_decisions": {"counts": dict(incomplete_clinical_decisions),
            "clinical_appropriateness": "not_assessed_outside_programmatic_scope",
            "interpretation": "No prescription, deferred adjudication, retained rejection and fixed revision limits remain incomplete task chains. A later documentation or budget failure does not erase that clinical disposition."},
        "clinical_disposition_documentation": {"outcome_counts": dict(disposition_note_outcomes),
            "failure_types": dict(disposition_note_failures),
            "programmatic_qualification_denominator": None,
            "qualifies_as_complete_three_task_chain": False,
            "interpretation": "Attested decision notes may be assessed for documentation quality. Their completion does not establish a prescription, drug settlement, full workflow completion or clinical appropriateness."},
        "readback_counts": {"native_verified_operations": native_verified,
                            "runtime_or_engineering_verified_operations": sidecar_verified,
                            "unverified_operations": unverified},
        "operations": [{"operation": op, "status": status, "count": n}
                       for (op, status), n in sorted(operation_counts.items(), key=lambda x: str(x[0]))],
        "prescription_execution": {"candidates": dict(candidate_counts),
            "recorded_system_dispositions": dict(prescription_decisions),
            "interpretation": "These are recorded system review decisions, not accuracy or independent reference labels. Audit-only candidates do not imply native order persistence."},
        "history_reads": {"counts": dict(history_reads), "lookup_wall_seconds": history_read_wall_seconds,
                          "full_chart_review_claimed": False},
        "response_contracts": {"counts": dict(schema_counts),
            "interpretation": "Format correction is not clinical rework or improved clinical quality. Both original and correction requests remain in token, cost and latency accounting."},
        "workflow_timing": {"intervals": intervals,
            "observed_interval_count": sum(row["observed_seconds"] is not None for row in intervals),
            "formal_correct_completion_mean_seconds": None, "formal_correct_completion_p95_seconds": None,
            "formal_timeout_rate": None, "formal_unsafe_execution_rate": None,
            "reason": "Native completion alone is not a whole-run invariant audit; the separate programmatic whole-run audit supplies window and safety eligibility."},
        "model_calls": [{"role": k[0], "actual_model": k[1], "tool_family": k[2], **v}
                        for k, v in sorted(model_calls.items(), key=lambda x: str(x[0]))],
        "duplicate_request_ids_excluded_from_usage": duplicate_requests,
        "activity_totals": [{"role": k[0], "timing_source": k[1], **v}
                            for k, v in sorted(roles.items(), key=lambda x: str(x[0]))],
        "budget": deepcopy(budget_summary), "incomplete_evidence": deepcopy(incomplete or []),
        "unexecuted_planned_attempts": dict(Counter(issue.get("status") for issue in (incomplete or [])
            if str(issue.get("status", "")).startswith("not_run_"))),
        "clinical_quality": {"status": "not_assessed_outside_programmatic_scope", "metrics": None,
                             "qualification_denominator": None, "quality_adjusted_effort": None},
        "interpretation": ["Execution completion is not clinical correctness.",
            "native_complete verifies core registration/order/note/payment read-backs; queue and signatory gateway protections are separately attributed and are not native-only safety claims.",
            "Modeled staff seconds, API call wall time and complete-run elapsed time are different quantities.",
            "Activity totals include all attempts; task qualification is exported separately. Nq comes from programmatic_delivery.py and the declared stage checks.",
            "Question counts or a role's purpose labels do not establish semantic redundancy.",
            "API retries and attempts without final role responses are accounted by the budget ledger, not only by the role-call table.",
            "Reasoning tokens are a subset of completion tokens and are not added twice.",
            "A missing annotation or zero denominator is not a zero error rate."]}
    return summary


def _tex(value):
    mapping = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#",
               "_": r"\_", "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}
    return "".join(mapping.get(c, c) for c in str(value))


def write_summary(summary, output_dir):
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    _write_json(root / "execution_summary.json", summary)
    rows = summary["configurations"]
    tex = ["% Generated from actual run envelopes. Programmatic task checks and business state are reported separately.",
           r"\begin{table*}[t]", r"\centering\small",
           r"\caption{Recorded execution attempts. All staff and patients are simulated roles; clinical claims remain within declared check coverage.}",
           r"\begin{tabular}{lrrr}", r"\hline",
           r"Configuration & Attempts & Completed & Core native steps verified \\", r"\hline"]
    for row in rows:
        tex.append(_tex(row["config_id"]) + " & " + str(row["attempts"]) + " & " + str(row["completed"]) +
                   " & " + str(row["native_complete_encounters"]) + r" \\")
    tex += [r"\hline", r"\end{tabular}", r"\end{table*}"]
    (root / "execution_summary.tex").write_text("\n".join(tex)+"\n", encoding="utf-8")
    html_rows = "".join("<tr>" + "".join("<td>"+html.escape(str(row[key]))+"</td>" for key in
                        ("config_id", "data_kind", "attempts", "completed", "failed_or_unresolved", "native_complete_encounters")) + "</tr>" for row in rows)
    document = """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>TwinHospital 运行汇总</title>
<style>body{max-width:1150px;margin:35px auto;padding:0 22px;font:16px/1.7 system-ui;color:#203040}table{border-collapse:collapse;width:100%;font-size:14px}th,td{border:1px solid #ddd;padding:9px;text-align:left}pre{white-space:pre-wrap;background:#f4f6f8;padding:18px}h1{font-size:27px}</style>
<h1>TwinHospital 运行汇总</h1><p>本页只展示实际执行证据。所有患者和医护均为 agent；完成情况按实际交付及程序核验报告。</p>
<table><thead><tr><th>配置</th><th>数据类型</th><th>尝试</th><th>完成</th><th>失败／未解决</th><th>核心原生业务读回完成</th></tr></thead><tbody>""" + html_rows + "</tbody></table>"
    for heading, field in (("模型调用与 token", "model_calls"), ("模拟活动秒数（不等于 API 耗时）", "activity_totals"),
                           ("动作合同与有界格式修正", "response_contracts"),
                           ("队列进入至支付读回（尚非正式安全指标）", "workflow_timing"),
                           ("病史读取", "history_reads"), ("处方候选、审方决定与原生写入", "prescription_execution"),
                           ("费用账本", "budget"), ("未执行的计划任务", "unexecuted_planned_attempts"),
                           ("临床决定导致未完成（尚未缴费）", "incomplete_clinical_decisions"),
                           ("失败类别", "failure_types"), ("指标边界", "interpretation")):
        document += "<h2>"+heading+"</h2><pre>"+html.escape(json.dumps(summary[field], ensure_ascii=False, indent=2))+"</pre>"
    document += "</html>"
    (root / "00_运行汇总.html").write_text(document, encoding="utf-8")
    return {"json": str(root / "execution_summary.json"), "latex": str(root / "execution_summary.tex"),
            "html": str(root / "00_运行汇总.html")}
