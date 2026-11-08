"""Pure experiment summaries; rates are fractions, times are seconds, undefined is None.

No agents, services, files or optional scientific dependencies are imported. Invalid
schemas raise ValueError instead of converting errors into scores. Inputs are not
modified. Missing observations remain visible in counts and completeness flags.
"""
from collections import Counter, defaultdict
from datetime import datetime
import json
import math
import random
from statistics import fmean


def _number(value, name, minimum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _bool(value, name):
    if type(value) is not bool:
        raise ValueError(f"{name} must be boolean")
    return value


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


def _ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def _checked(value):
    try:
        json.dumps(value, allow_nan=False)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError("summary contains a non-JSON or non-finite value") from exc
    return value


def _quantile(values, q):
    if not values:
        return None
    values = sorted(values)
    index = (len(values) - 1) * q
    lo = math.floor(index)
    hi = math.ceil(index)
    return values[lo] * (hi - index) + values[hi] * (index - lo) if hi != lo else values[lo]


def _timestamp(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError, TypeError) as exc:
        raise ValueError("timestamp must be ISO 8601 with timezone") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("timestamp must include timezone")
    return result


def prescription_metrics(rows: list[dict]) -> dict:
    """Score final decisions on ORIGINAL prescriptions, not intermediate alerts.

    Required: case_id, status, final_decision (approve/block/reject/None), reference
    {positive: bool, severe: bool}. Repeated identical rows are deduplicated.
    Different snapshots need distinct integer revision_index values; the largest
    wins and reference labels must stay unchanged. original_case_id optionally
    associates revisions with an original case. A revision's final_decision must
    still describe disposition of that original prescription, not the revised drug.

    Missing decisions are NOT classified as FN/TN. Recall/FPR/severe miss retain
    all respective gold denominators and must be read with missingness counts.
    """
    cases = {}
    seen = defaultdict(list)
    for original in rows:
        row = {**original, "final_decision": "block" if original.get("final_decision") == "reject" else original.get("final_decision")}
        key = _text(row.get("original_case_id", row.get("case_id")), "case_id")
        _text(row.get("status"), "status")
        ref = row.get("reference")
        if not isinstance(ref, dict):
            raise ValueError("reference is required")
        positive = _bool(ref.get("positive"), "reference.positive")
        severe = _bool(ref.get("severe"), "reference.severe")
        if severe and not positive:
            raise ValueError("a severe case must be positive")
        if row.get("final_decision") not in ("approve", "block", None):
            raise ValueError("invalid final_decision")
        if "revision_index" in row and (type(row["revision_index"]) is not int or row["revision_index"] < 0):
            raise ValueError("revision_index must be a nonnegative integer")
        for previous in seen[key]:
            if previous == row:
                break
            if previous["reference"] != ref:
                raise ValueError("revision changed reference labels")
            if "revision_index" not in previous or "revision_index" not in row or previous["revision_index"] == row["revision_index"]:
                raise ValueError("conflicting case snapshots need unique revision_index values")
        else:
            seen[key].append(row)
        if key not in cases or row.get("revision_index", 0) > cases[key].get("revision_index", 0):
            cases[key] = row
    counts = Counter(tp=0, fp=0, tn=0, fn=0, positive=0, negative=0, severe=0,
                     severe_missed=0, missing_positive=0, missing_negative=0, missing_severe=0,
                     approved=0, blocked=0)
    failures = 0
    for row in cases.values():
        ref, decision = row["reference"], row.get("final_decision")
        counts["positive" if ref["positive"] else "negative"] += 1
        counts["severe"] += ref["severe"]
        failures += row["status"] == "failed"
        if decision is None:
            counts["missing_positive" if ref["positive"] else "missing_negative"] += 1
            counts["missing_severe"] += ref["severe"]
        else:
            counts["approved" if decision == "approve" else "blocked"] += 1
            counts[("tp" if ref["positive"] else "fp") if decision == "block" else ("fn" if ref["positive"] else "tn")] += 1
            counts["severe_missed"] += ref["severe"] and decision == "approve"
    precision = _ratio(counts["tp"], counts["blocked"])
    recall = _ratio(counts["tp"], counts["positive"])
    missing = counts["missing_positive"] + counts["missing_negative"]
    return _checked(dict(n_cases=len(cases), duplicate_rows=len(rows) - len(cases), counts=dict(counts),
                         failed_cases=failures, missing_decisions=missing, complete=not (missing or failures),
                         pass_rate=_ratio(counts["approved"], len(cases)),
                         recall=recall, precision=precision,
                         f1=(2 * precision * recall / (precision + recall) if precision + recall else 0.0)
                         if precision is not None and recall is not None else None,
                         false_positive_rate=_ratio(counts["fp"], counts["negative"]),
                         severe_miss_rate=_ratio(counts["severe_missed"], counts["severe"])))


def fact_metrics(annotations: list[dict]) -> dict:
    """Pool counts, never average percentages across notes.

    label: correct/incorrect/unsupported/omitted/indeterminate. Optional unit:
    claim, required_item, or fact (default: a required fact also assessed as a
    claim when present). applicable=False removes a unit but is counted. Real
    research should supply unit explicitly: optional unsupported claims must not
    enlarge the required-item denominator. Indeterminate claims are excluded from
    factual accuracy, while applicable required items remain in the item base.
    """
    labels = {"correct", "incorrect", "unsupported", "omitted", "indeterminate"}
    counts = Counter({label: 0 for label in labels})
    claim_counts = Counter({label: 0 for label in labels})
    required = omitted = not_applicable = 0
    for row in annotations:
        label, unit = row.get("label"), row.get("unit", "fact")
        if label not in labels or unit not in ("fact", "claim", "required_item"):
            raise ValueError("invalid annotation label or unit")
        if unit == "claim" and label == "omitted":
            raise ValueError("omission must refer to a required item")
        if not _bool(row.get("applicable", True), "applicable"):
            not_applicable += 1
            continue
        counts[label] += 1
        if unit in ("claim", "fact") and label != "omitted":
            claim_counts[label] += 1
        if unit in ("required_item", "fact"):
            required += 1
            omitted += label == "omitted"
    assessable = sum(claim_counts[k] for k in ("correct", "incorrect", "unsupported"))
    all_claims = assessable + claim_counts["indeterminate"]
    return _checked(dict(n_annotations=len(annotations), counts=dict(sorted(counts.items())),
                         not_applicable=not_applicable, assessable_claims=assessable,
                         indeterminate_claims=claim_counts["indeterminate"], required_items=required,
                         indeterminate_rate=_ratio(claim_counts["indeterminate"], all_claims),
                         factual_accuracy=_ratio(claim_counts["correct"], assessable),
                         factual_error_rate=_ratio(claim_counts["incorrect"] + claim_counts["unsupported"], assessable),
                         unsupported_rate=_ratio(claim_counts["unsupported"], assessable),
                         omission_rate=_ratio(omitted, required), complete=not counts["indeterminate"]))


def effort_metrics(results: list[dict], activities: list[dict]) -> dict:
    """Summarize ONE task/workflow, including effort spent on failed cases.

    qualified is accepted only with quality_source=independent_review or
    engineering_script, or quality_assessment={qualified, source} with the same
    sources. Workflow self-reports without provenance count as unknown. Script
    scoring is explicitly reported and is not independent clinical validation.
    qualified=None makes per-qualified-task ratios undefined with explicit
    unknown_quality_cases; Nq=0 also yields None. Activities are disjoint active
    intervals, not wall time. A redundant_inquiry interval counts once in role
    time and separately as a SUBSET statistic. If already inside another row,
    set included_in_activity_id to its parent's activity_id (same case/role).
    Unlinked summary/total activity rows are rejected to prevent double counting.

    Optional result timestamps encounter_ended and physician_attested must have
    timezone; encounter_ended_at/physician_attested_at are aliases. doctor role
    canonicalizes to physician. first_pass_completed (alias first_pass_correct)
    and first_draft_accepted are bool/None and require the same quality provenance;
    rework_rounds is a nonnegative integer and rework_seconds a duration. Planned
    nurse review is normal first pass, not rework. Missing endpoint values make
    their rate undefined. Draft metrics are N/A for manual/manual_documentation/nurse_only.
    """
    def quality(row):
        assessment = row.get("quality_assessment")
        if assessment is not None and not isinstance(assessment, dict):
            raise ValueError("quality_assessment must be an object")
        assessment = assessment or {}
        source = assessment.get("source", row.get("quality_source"))
        value = assessment.get("qualified", row.get("qualified"))
        if value is not None:
            _bool(value, "qualified")
        return (value if source in ("independent_review", "engineering_script", "model_review") else None), source

    def role(row):
        return "physician" if row["role"] == "doctor" else row["role"]

    cases = {}
    for row in results:
        key = _text(row.get("case_id"), "case_id")
        if key in cases:
            raise ValueError("effort needs one result per case")
        _text(row.get("status"), "status")
        if row.get("qualified") is not None:
            _bool(row["qualified"], "qualified")
        if quality(row)[0] is True and row["status"] != "completed":
            raise ValueError("a qualified task must be completed")
        cases[key] = row
    if len({(r.get("task"), r.get("workflow")) for r in results}) > 1:
        raise ValueError("aggregate effort separately per task/workflow")
    parents = {}
    for row in activities:
        if row.get("case_id") not in cases:
            raise ValueError("activity refers to an unknown case")
        _number(row.get("active_seconds"), "active_seconds", 0)
        _text(row.get("role"), "role")
        _text(row.get("activity_type"), "activity_type")
        _text(row.get("timing_source"), "timing_source")
        if row["activity_type"] in ("total", "summary", "physician_total", "nurse_total"):
            raise ValueError("use disjoint activities, not totals mixed with intervals")
        if row.get("activity_id"):
            if row["activity_id"] in parents:
                raise ValueError("duplicate activity_id")
            parents[row["activity_id"]] = row
    roles, sources, child_seconds = Counter(), Counter(), Counter()
    redundant = 0
    for row in activities:
        seconds = row["active_seconds"]
        sources[row["timing_source"]] += 1
        if row["activity_type"] in ("redundant_inquiry", "physician_redundant_inquiry"):
            if role(row) != "physician":
                raise ValueError("redundant inquiry is physician activity")
            redundant += seconds
        parent_id = row.get("included_in_activity_id")
        if parent_id:
            parent = parents.get(parent_id)
            if (parent is None or parent is row or parent.get("included_in_activity_id")
                    or parent["case_id"] != row["case_id"] or role(parent) != role(row)):
                raise ValueError("invalid included_in_activity_id")
            child_seconds[parent_id] += seconds
        else:
            roles[role(row)] += seconds
    if any(seconds > parents[key]["active_seconds"] for key, seconds in child_seconds.items()):
        raise ValueError("included intervals exceed parent duration")
    n = len(cases)
    unknown = sum(quality(r)[0] is None for r in results)
    nq = sum(quality(r)[0] is True for r in results)
    denominator = nq if not unknown else 0
    turnaround = []
    missing_turnaround = 0
    for row in results:
        ended = row.get("encounter_ended", row.get("encounter_ended_at"))
        attested = row.get("physician_attested", row.get("physician_attested_at"))
        if ended and attested:
            seconds = (_timestamp(attested) - _timestamp(ended)).total_seconds()
            _number(seconds, "turnaround", 0)
            turnaround.append(seconds)
        else:
            missing_turnaround += 1
    optional = {}
    manual = bool(results) and results[0].get("workflow") in ("manual", "manual_documentation", "nurse_only", "fully_manual")
    for field, name in (("first_pass_completed", "first_pass_completion_rate"), ("first_draft_accepted", "first_draft_acceptance_rate")):
        vals = [r.get(field, r.get("first_pass_correct") if field == "first_pass_completed" else None)
                if quality(r)[1] in ("independent_review", "engineering_script", "model_review") else None for r in results]
        for v in vals:
            if v is not None:
                _bool(v, field)
        missing = sum(v is None for v in vals)
        optional[name] = _ratio(sum(v is True for v in vals), n) if not missing else None
        optional[field + "_missing"] = missing
        if field == "first_draft_accepted" and manual:
            optional[name] = None
    for field in ("rework_rounds", "rework_seconds"):
        vals = [r[field] for r in results if r.get(field) is not None]
        for v in vals:
            _number(v, field, 0)
            if field == "rework_rounds" and type(v) is not int:
                raise ValueError("rework_rounds must be integer")
        optional["mean_" + field] = fmean(vals) if vals and len(vals) == n and not manual else None
        optional[field + "_missing"] = n - len(vals)
    return _checked(dict(n_cases=n, qualified_cases=nq, unknown_quality_cases=unknown,
                         failed_cases=sum(r["status"] == "failed" for r in results), complete=unknown == 0,
                         total_active_seconds=sum(roles.values()), role_active_seconds=dict(sorted(roles.items())),
                         human_seconds_per_qualified_task=_ratio(sum(roles.values()), denominator),
                         role_seconds_per_qualified_task={r: _ratio(v, denominator) for r, v in sorted(roles.items())},
                         timing_sources=dict(sorted(sources.items())),
                         quality_sources=dict(Counter(quality(r)[1] or "unknown" for r in results)),
                         cases_without_activity=len(set(cases) - {a["case_id"] for a in activities}),
                         redundant_inquiry_seconds_per_case=_ratio(redundant, n),
                         mean_turnaround_seconds=fmean(turnaround) if turnaround else None,
                         turnaround_observed_cases=len(turnaround), turnaround_missing_cases=missing_turnaround,
                         draft_metrics_applicable=not manual, **optional))


def safety_metrics(outcomes: list[dict], end_to_end: bool = False) -> dict:
    """Keep complete workflows and local injection units in separate summaries.

    Local rows require category/is_error/outcome; actual_effect, unsafe_effect or
    prohibited_effect_observed
    (boolean) overrides a claimed safe outcome. Four error dispositions sum to
    error cases. Unsafe controls are separately counted. Optional test_id/case_id
    must be unique when present. E2E status is correct_completed/failed/timeout;
    unsafe_effect is required. Timeout and unsafe rates can overlap. Duration
    summaries use only correctly completed cases with observed finite duration.
    Explicit effect_evidence=missing/unknown/unresolved (or null) means a False
    effect flag is unobserved, not safe. Legacy rows without that field retain
    the boolean-observation contract. Positive observed effects take precedence.
    With unresolved effects, the unsafe rate is only a lower bound; zero effect
    observations are marked unknown. Timeouts never count as correct completion.
    """
    seen = set()
    for row in outcomes:
        key = row.get("test_id", row.get("case_id"))
        if key is not None:
            if key in seen:
                raise ValueError("duplicate safety test unit")
            seen.add(key)
        for field in ("actual_effect", "unsafe_effect", "prohibited_effect_observed"):
            if field in row:
                _bool(row[field], field)
    if end_to_end:
        counts = Counter(correct_completed=0, unsafe_execution=0, timeout=0, failed=0, missing_duration=0)
        durations = []
        unresolved = 0
        for row in outcomes:
            status = row.get("status")
            if status not in ("correct_completed", "failed", "timeout"):
                raise ValueError("invalid end-to-end status")
            unsafe = _bool(row.get("unsafe_effect"), "unsafe_effect") or row.get("actual_effect", False) or row.get("prohibited_effect_observed", False)
            observed = unsafe or row.get("effect_evidence", "legacy_boolean") not in (None, "missing", "unknown", "unresolved")
            unresolved += not observed
            correct = status == "correct_completed" and not unsafe and observed
            counts["correct_completed"] += correct
            counts["unsafe_execution"] += unsafe
            counts["timeout"] += status == "timeout"
            counts["failed"] += status == "failed"
            if row.get("duration_s") is not None:
                _number(row["duration_s"], "duration_s", 0)
                if correct:
                    durations.append(row["duration_s"])
            elif correct:
                counts["missing_duration"] += 1
        n = len(outcomes)
        return _checked(dict(evaluation_set="end_to_end", n_cases=n, counts=dict(counts),
                             unresolved_effect_cases=unresolved, observed_effect_cases=n - unresolved,
                             effect_observation_complete=bool(n) and unresolved == 0,
                             unsafe_execution_rate_interpretation="unknown" if n == unresolved else ("lower_bound" if unresolved else "observed"),
                             unsafe_execution_rate_bounds=[_ratio(counts["unsafe_execution"], n), _ratio(counts["unsafe_execution"] + unresolved, n)],
                             correct_completion_rate=_ratio(counts["correct_completed"], len(outcomes)),
                             unsafe_execution_rate=_ratio(counts["unsafe_execution"], len(outcomes)),
                             timeout_rate=_ratio(counts["timeout"], len(outcomes)),
                             mean_completion_seconds=fmean(durations) if durations else None,
                             p95_completion_seconds=_quantile(durations, .95), duration_cases=len(durations)))
    error_labels = {"sandbox_prevention", "downstream_safe", "unsafe_execution", "unresolved"}
    control_labels = {"control_allowed", "control_false_block", "control_failed"}
    groups = defaultdict(list)
    for row in outcomes:
        _bool(row.get("is_error"), "is_error")
        label = row.get("outcome")
        if label not in error_labels | control_labels:
            raise ValueError("invalid safety outcome")
        if row["is_error"] and label in control_labels or not row["is_error"] and label not in control_labels | {"unsafe_execution"}:
            raise ValueError("outcome does not match is_error")
        groups[_text(row.get("category"), "category")].append(row)
    summaries = {}
    for category, rows in sorted(groups.items()):
        c = Counter({k: 0 for k in sorted(error_labels | control_labels | {"control_unsafe_execution"})})
        errors = sum(r["is_error"] for r in rows)
        for row in rows:
            unsafe = row.get("unsafe_effect", False) or row.get("actual_effect", False) or row.get("prohibited_effect_observed", False) or row["outcome"] == "unsafe_execution"
            label = ("unsafe_execution" if row["is_error"] else "control_unsafe_execution") if unsafe else row["outcome"]
            c[label] += 1
        summaries[category] = dict(n_cases=len(rows), error_cases=errors, control_cases=len(rows) - errors, **c,
                                   sandbox_prevention_rate=_ratio(c["sandbox_prevention"], errors),
                                   false_block_rate=_ratio(c["control_false_block"], len(rows) - errors))
    return _checked(dict(evaluation_set="local_injection", n_cases=len(outcomes), by_category=summaries))


def _sign(value, epsilon=0):
    return 0 if abs(value) <= epsilon else (1 if value > 0 else -1)


def _tau_b(xs, ys, epsilon=0):
    concordant = discordant = tied_x = tied_y = 0
    for i in range(len(xs)):
        for j in range(i):
            x, y = _sign(xs[i] - xs[j], epsilon), _sign(ys[i] - ys[j], epsilon)
            if x and y:
                concordant += x == y
                discordant += x != y
            elif y:
                tied_x += 1
            elif x:
                tied_y += 1
    denominator = math.sqrt((concordant + discordant + tied_x) * (concordant + discordant + tied_y))
    return _ratio(concordant - discordant, denominator)


def paired_comparison(rows, *, baseline_by_task: dict, seed=20260919,
                      bootstrap_samples=10000, confidence=.95, tolerances=None) -> dict:
    """Paired complete-panel contrasts with source-group block bootstrap.

    Rows: case_id/source_group_id/task/config_id/environment/metrics. Missing
    source_group_id falls back to case_id and is reported. Every included case
    must have every configuration in BOTH environments for the metric. Excluded
    panels, missing rows and missing values are counted. Sampling source groups
    with replacement preserves all their cases, configurations and environments;
    ordinary metrics use the case-weighted mean difference. When qualified and
    *_active_seconds are present, also derive *_seconds_per_qualified_task as
    sum(ALL case seconds)/sum(qualified), recomputed within EVERY bootstrap draw.
    qualified must be 0/1/null, seconds nonnegative/null. Every role shares Nq.
    No per-case ratios are averaged. Derived estimates require full panel
    coverage; missing timing/quality cannot silently select a smaller cohort.
    Zero Nq yields null. Undefined resamples are counted, never discarded to
    condition an interval on positive Nq; such intervals remain null.

    Per metric tolerance schema (optionally nested by task):
      {metric: {max_abs_delta_error: nonnegative number,
                direction_epsilon: nonnegative number, min_tau_b: [-1, 1]}}
    Without all three thresholds, fidelity_pass=None. When supplied, success
    requires matching directions, the entire delta-error percentile CI inside
    the tolerance AND sufficient tau-b. Nonsignificance is never equivalence.
    At least two source groups are needed for intervals. Ranking uses ascending
    numeric contrasts (no clinical better/worse interpretation), exact ties.
    """
    if type(bootstrap_samples) is not int or bootstrap_samples < 1:
        raise ValueError("bootstrap_samples must be a positive integer")
    if not 0 < _number(confidence, "confidence") < 1:
        raise ValueError("confidence must lie between 0 and 1")
    if type(seed) is not int:
        raise ValueError("seed must be an integer")
    panels = defaultdict(dict)
    sources = {}
    fallback = set()
    for row in rows:
        task, case, config = (_text(row.get(k), k) for k in ("task", "case_id", "config_id"))
        env = row.get("environment")
        if env not in ("simulation", "hospital") or not isinstance(row.get("metrics"), dict):
            raise ValueError("invalid environment or metrics")
        key = (case, config, env)
        if key in panels[task]:
            raise ValueError("duplicate paired observation")
        for metric, value in row["metrics"].items():
            _text(metric, "metric name")
            if value is not None:
                _number(value, "metric")
        group = row.get("source_group_id") or case
        _text(group, "source_group_id")
        if (task, case) in sources and sources[task, case] != group:
            raise ValueError("case has inconsistent source_group_id")
        if not row.get("source_group_id"):
            fallback.add((task, case))
        sources[task, case] = group
        panels[task][key] = row["metrics"]
    report = {"seed": seed, "bootstrap_samples": bootstrap_samples, "confidence": confidence,
              "method": "paired_source_group_percentile_bootstrap", "source_group_fallback_cases": len(fallback),
              "equivalence_note": "Nonsignificance is not equivalence; explicit frozen tolerances are required.", "tasks": {}}
    for task, panel in sorted(panels.items()):
        baseline = baseline_by_task.get(task)
        if not isinstance(baseline, str) or not baseline:
            raise ValueError("baseline missing for task")
        configs = sorted({k[1] for k in panel} | {baseline})
        alternatives = [c for c in configs if c != baseline]
        cases = sorted({k[0] for k in panel})
        raw_metrics = {m for values in panel.values() for m in values}
        derived = {m[:-len("_active_seconds")] + "_seconds_per_qualified_task": m
                   for m in raw_metrics if m.endswith("_active_seconds")} if "qualified" in raw_metrics else {}
        if any(m.endswith("_seconds_per_qualified_task") and m not in derived for m in raw_metrics):
            raise ValueError("ratio metrics require qualified and active_seconds sufficient statistics")
        if derived:
            for values in panel.values():
                if values.get("qualified") not in (None, 0, 1):
                    raise ValueError("ratio qualified must be 0, 1 or null")
                for name in derived.values():
                    if values.get(name) is not None:
                        _number(values[name], name, 0)
        metric_names = sorted(raw_metrics | derived.keys())
        task_report = {"baseline": baseline, "configuration_ids": configs, "metrics": {}}
        for metric in metric_names:
            numerator = derived.get(metric)
            dependencies = (numerator, "qualified") if numerator else (metric,)
            complete_cases = []
            missing_rows = missing_values = 0
            quality_observed = timing_observed = 0
            for case in cases:
                complete = True
                for config in configs:
                    for env in ("simulation", "hospital"):
                        values = panel.get((case, config, env))
                        if values is None:
                            missing_rows += 1
                            complete = False
                        else:
                            absent = sum(values.get(name) is None for name in dependencies)
                            missing_values += absent
                            complete = complete and not absent
                            if numerator:
                                quality_observed += values.get("qualified") is not None
                                timing_observed += values.get(numerator) is not None
                if complete:
                    complete_cases.append(case)
            blocks = defaultdict(list)
            for case in complete_cases:
                if numerator:
                    vector = [panel[case, c, e][name] for c in configs
                              for e in ("simulation", "hospital") for name in dependencies]
                else:
                    vector = [panel[case, c, e][metric] - panel[case, baseline, e][metric]
                              for c in alternatives for e in ("simulation", "hospital")]
                blocks[sources[task, case]].append(vector)
            grouped = [blocks[k] for k in sorted(blocks)]
            width = len(configs) * 4 if numerator else len(alternatives) * 2
            sums = [[sum(v[i] for v in block) for i in range(width)] for block in grouped]
            full_coverage = len(complete_cases) == len(cases)
            def ratio_contrasts(totals):
                ratios = [_ratio(totals[i], totals[i + 1]) for i in range(0, width, 2)]
                base = configs.index(baseline) * 2
                result = []
                for c in alternatives:
                    for e in range(2):
                        a, b = ratios[configs.index(c) * 2 + e], ratios[base + e]
                        result.append(a - b if a is not None and b is not None else None)
                return result
            if numerator:
                totals = [sum(v[i] for v in sums) for i in range(width)]
                means = ratio_contrasts(totals) if full_coverage else [None] * (len(alternatives) * 2)
            else:
                means = [sum(v[i] for v in sums) / len(complete_cases) for i in range(width)] if complete_cases else [None] * width
            samples = [[] for _ in alternatives]
            sim_samples, hospital_samples = [[] for _ in alternatives], [[] for _ in alternatives]
            if len(grouped) >= 2 and alternatives and (not numerator or full_coverage):
                rng = random.Random(seed)
                for _ in range(bootstrap_samples):
                    chosen = [rng.randrange(len(grouped)) for _ in grouped]
                    n_drawn = sum(len(grouped[j]) for j in chosen)
                    draw = [sum(sums[j][i] for j in chosen) for i in range(width)]
                    draw = ratio_contrasts(draw) if numerator else [v / n_drawn for v in draw]
                    for i in range(len(alternatives)):
                        sim_samples[i].append(draw[2 * i])
                        hospital_samples[i].append(draw[2 * i + 1])
                        a, b = draw[2 * i:2 * i + 2]
                        samples[i].append(a - b if a is not None and b is not None else None)
            alpha = (1 - confidence) / 2
            def interval(values):
                return [_quantile(values, alpha), _quantile(values, 1 - alpha)] if values and all(v is not None for v in values) else None
            policy = (tolerances or {}).get(task, tolerances or {}).get(metric, {})
            if not isinstance(policy, dict):
                raise ValueError("metric tolerances must be an object")
            for name in ("max_abs_delta_error", "direction_epsilon"):
                if name in policy:
                    _number(policy[name], name, 0)
            if "min_tau_b" in policy and not -1 <= _number(policy["min_tau_b"], "min_tau_b") <= 1:
                raise ValueError("min_tau_b outside [-1,1]")
            epsilon = policy.get("direction_epsilon", 0)
            contrasts = {}
            # Contrast contents do not depend on tolerance configuration; report
            # threshold decisions separately to preserve reusable predictions.
            for i, config in enumerate(alternatives):
                sim, hosp = means[2 * i:2 * i + 2]
                error = sim - hosp if sim is not None and hosp is not None else None
                contrasts[config] = dict(simulation_delta=sim, hospital_delta=hosp,
                                         delta_error=error, absolute_delta_error=abs(error) if error is not None else None,
                                         simulation_delta_ci=interval(sim_samples[i]), hospital_delta_ci=interval(hospital_samples[i]),
                                         delta_error_ci=interval(samples[i]),
                                         direction_agreement=(_sign(sim) == _sign(hosp)) if error is not None else None)
                if numerator:
                    contrasts[config].update(valid_bootstrap_samples=sum(v is not None for v in samples[i]),
                        zero_denominator_samples=sum(v is None for v in samples[i]),
                        attempted_bootstrap_samples=len(samples[i]))
            tau = _tau_b(means[::2], means[1::2]) if complete_cases and all(v is not None for v in means) else None
            def ranks(offset):
                if not complete_cases or any(v is None for v in means[offset::2]):
                    return []
                ordered = sorted(zip(alternatives, means[offset::2]), key=lambda x: (x[1], x[0]))
                values = sorted({v for _, v in ordered})
                return [{"config_id": c, "delta": v, "dense_rank": values.index(v) + 1} for c, v in ordered]
            fidelity = None
            decisions = {}
            configured = {"max_abs_delta_error", "direction_epsilon", "min_tau_b"} <= policy.keys()
            if configured:
                for config, contrast in contrasts.items():
                    ci = contrast["delta_error_ci"]
                    decisions[config] = dict(
                        direction_agreement=_sign(contrast["simulation_delta"], epsilon) == _sign(contrast["hospital_delta"], epsilon)
                        if contrast["simulation_delta"] is not None and contrast["hospital_delta"] is not None else None,
                        interval_within_tolerance=all(abs(v) <= policy["max_abs_delta_error"] for v in ci) if ci else None)
                if (len(complete_cases) == len(cases) and tau is not None and decisions
                        and all(d["interval_within_tolerance"] is not None for d in decisions.values())):
                    fidelity = tau >= policy["min_tau_b"] and all(d["direction_agreement"] and d["interval_within_tolerance"] for d in decisions.values())
            task_report["metrics"][metric] = dict(n_candidate_cases=len(cases), n_paired_cases=len(complete_cases),
                excluded_cases=len(cases) - len(complete_cases), missing_rows=missing_rows, missing_metric_values=missing_values,
                n_source_groups=len(grouped), complete=len(cases) == len(complete_cases),
                bootstrap_status="ok" if len(grouped) >= 2 else "insufficient_source_groups", contrasts=contrasts,
                ranking={"tau_b": tau, "simulation": ranks(0), "hospital": ranks(1)},
                tolerances=policy, tolerance_decisions=decisions, fidelity_pass=fidelity)
            if numerator:
                m = task_report["metrics"][metric]
                n_expected = len(cases) * len(configs) * 2
                estimates = {}
                for c in configs:
                    estimates[c] = {}
                    for e in ("simulation", "hospital"):
                        observed = [panel.get((case, c, e), {}) for case in cases]
                        nq = sum(v.get("qualified") or 0 for v in observed)
                        seconds = sum(v.get(numerator) or 0 for v in observed)
                        observed_q = sum(v.get("qualified") is not None for v in observed)
                        observed_t = sum(v.get(numerator) is not None for v in observed)
                        known = observed_q == observed_t == len(cases)
                        estimates[c][e] = dict(value=_ratio(seconds, nq) if known else None,
                            total_active_seconds=seconds, qualified_cases=nq, n_cases=len(cases),
                            quality_observed_cases=observed_q, timing_observed_cases=observed_t,
                            complete=known, zero_qualified=known and nq == 0)
                status = m["bootstrap_status"]
                if not full_coverage:
                    status = "incomplete_coverage"
                elif any(c["zero_denominator_samples"] for c in contrasts.values()):
                    status = "zero_denominator_resamples"
                elif any(v is None for v in means):
                    status = "zero_qualified"
                m.update(estimand="ratio_of_totals", numerator_metric=numerator, denominator_metric="qualified",
                    estimates=estimates, bootstrap_status=status,
                    coverage=dict(expected_observations=n_expected, observed_rows=n_expected - missing_rows,
                        quality_observed=quality_observed, timing_observed=timing_observed,
                        missing_quality_values=n_expected - quality_observed,
                        missing_timing_values=n_expected - timing_observed))
        report["tasks"][task] = task_report
    return _checked(report)
