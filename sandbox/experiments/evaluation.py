"""Independent offline scoring of sealed simulation and imported hospital outputs.

No agents, database, HTTP, or model calls. Evidence is never written over the run;
each invocation creates analysis/<evidence-hash>/ exclusively. The JSONL output
case_metrics is immediately consumable by metrics.paired_comparison. Aggregate
human effort/Nq remains a ratio of totals, not a mean of per-case ratios.
"""
from collections import defaultdict
import csv
import hashlib
from pathlib import Path

from .data import CaseProvider
from .metrics import _bool, _checked, _number, _text, _timestamp, effort_metrics, fact_metrics, prescription_metrics
from .research import (_canonical, _configuration_plan, _digest, _hash_string, _json, _jsonl,
                       _loads, _output, _safe_child, _sha, _verified_run, _write_new_bundle,
                       adjudicate_reviews)


def _rows(path):
    path = Path(path)
    if path.suffix.lower() != ".csv":
        return _jsonl(path)
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    for row in rows:
        for key in ("active_seconds", "start_sim_s", "end_sim_s"):
            if key in row:
                row[key] = float(row[key]) if row[key] else None
        if "independent_execution" in row:
            if row["independent_execution"] not in ("true", "false"):
                raise ValueError("CSV independent_execution must be true/false")
            row["independent_execution"] = row["independent_execution"] == "true"
    return rows


def _dataset(dataset_dir, manifest, cases):
    data = CaseProvider(dataset_dir)
    if data.dataset_hash != manifest["datasets_hash"]:
        raise ValueError("evaluation dataset does not match frozen datasets_hash")
    names = {r["path"] for r in data.catalog.get("artifacts", [])}
    if not {"cases.jsonl", "inputs.jsonl", "reference.jsonl"} <= names:
        raise ValueError("dataset catalog must bind case, input and reference artifacts")
    for cid, case in cases.items():
        metadata = data.cases.get(cid)
        if not metadata or metadata["split"] != "validation" or metadata["task"] != case["task"]:
            raise ValueError("sealed case is not a matching validation case")
        if metadata["input_sha256"] != manifest["case_hashes"][cid]:
            raise ValueError("evaluation case input hash mismatch")
        if case.get("source_group_id") and case["source_group_id"] != metadata["source_group_id"]:
            raise ValueError("sealed source group differs from evaluation dataset")
    return data


def _hospital_rows(run, manifest, seal_hash, expected, files, evidence_hashes):
    rows = {}
    root = run / "hospital"
    if not root.exists():
        return rows
    for directory in sorted(root.iterdir()):
        if not directory.is_dir():
            continue
        report_path = _safe_child(run, directory.relative_to(run).as_posix() + "/import_report.json")
        report = _json(report_path)
        if report.get("run_id") != manifest["run_id"] or report.get("source_seal_sha256") != seal_hash:
            raise ValueError("hospital receipt belongs to a different sealed run")
        if report.get("source_sha256") != directory.name:
            raise ValueError("hospital receipt source hash/directory mismatch")
        result_path = _safe_child(directory, "results.jsonl")
        if _sha(result_path) != _hash_string(report.get("results_sha256"), "results_sha256"):
            raise ValueError("hospital results changed after import")
        prefix = "hospital_" + directory.name + "_"
        files[prefix + "results.jsonl"] = result_path.read_bytes()
        files[prefix + "import_report.json"] = report_path.read_bytes()
        evidence_hashes[prefix + "receipt"] = _sha(report_path)
        for name, digest in report.get("evidence_artifacts", {}).items():
            path = _safe_child(directory, name)
            if _sha(path) != _hash_string(digest, "hospital evidence sha256"):
                raise ValueError("hospital independent evidence changed after import")
            files[prefix + path.name] = path.read_bytes()
        for row in _jsonl(result_path):
            key = row.get("case_id"), row.get("config_id")
            if key not in expected or key in rows:
                raise ValueError("unknown or ambiguous repeated hospital case/config; select a single observation version")
            if row.get("source_kind") != "hospital_observed" or row.get("independent_execution") is not True:
                raise ValueError("hospital row lacks independent hospital provenance")
            if row.get("environment", "hospital") != "hospital":
                raise ValueError("simulation record cannot be relabeled as hospital")
            if row.get("input_sha256") != manifest["case_hashes"][key[0]]:
                raise ValueError("hospital case input digest differs from sealed input")
            if _timestamp(row.get("hospital_execution_started_at")) < _timestamp(_json(run / "seal.json")["sealed_at"]):
                raise ValueError("hospital execution predates sealing")
            value, digest = _output(row, directory)
            if row.get("potential_reuse"):
                ev = row.get("independent_execution_evidence") or {}
                if ev.get("execution_log_ref") not in report.get("evidence_artifacts", {}):
                    row = {**row, "evaluation_missing_independent_evidence": True}
            rows[key] = {**row, "output": value, "output_sha256": digest, "environment": "hospital"}
    return rows


def _stage_output(record, stage):
    """Select actual evidence from the sealed/receipt-bound execution record.

    Hash equality between final and early content never substitutes for recording
    the early artifact. Note version 1 must explicitly bind its canonical content.
    """
    if stage == "final":
        return record.get("output")
    if stage == "first_handoff" and record["task"] == "preconsult":
        value = record.get("first_handoff_output")
        if value is not None and not isinstance(value, dict):
            raise ValueError("first_handoff_output must be structured")
        declared = record.get("first_handoff_output_sha256", record.get("first_handoff_sha256"))
        if declared is not None and (value is None or declared != _digest(value)):
            raise ValueError("first handoff content digest mismatch")
        return value
    if stage == "first_draft" and record["task"] == "documentation" and record["workflow"] not in ("manual", "manual_documentation", "fully_manual"):
        versions = record.get("artifact_versions", [])
        if not isinstance(versions, list) or any(not isinstance(v, dict) for v in versions):
            raise ValueError("artifact_versions must be an array of version objects")
        first = [v for v in versions if type(v.get("version")) is int and v["version"] == 1]
        if not first:
            return None
        if len(first) != 1 or first[0].get("parent_version") is not None:
            raise ValueError("ambiguous or invalid first draft version")
        value = first[0].get("content")
        if not isinstance(value, dict) or first[0].get("content_hash") != _digest(value):
            raise ValueError("first draft content_hash does not bind actual version content")
        return value
    raise ValueError("artifact_stage is unknown or not applicable to task/workflow")


def _review_index(reviews, records, data, sealed_at):
    groups, artifacts = defaultdict(list), {}
    for row in reviews:
        key = row.get("case_id"), row.get("config_id"), row.get("environment")
        if key not in records:
            raise ValueError("review refers to an unknown execution pair/environment")
        record = records[key]
        stage = row.get("artifact_stage", "final")
        value = _stage_output(record, stage)
        stage_key = (*key, stage)
        artifact = _text(row.get("artifact_id"), "artifact_id")
        if artifact in artifacts and artifacts[artifact] != stage_key:
            raise ValueError("blinded artifact ID reused across execution pairs/stages")
        artifacts[artifact] = stage_key
        if value is None or row.get("artifact_sha256") != _digest(value):
            raise ValueError("review artifact_sha256 does not bind actual output")
        if row.get("source_sha256") != _digest(data.references[key[0]]):
            raise ValueError("review source_sha256 does not bind independent reference")
        if row.get("quality_source", "independent_review") != "independent_review":
            raise ValueError("workflow/engineering self-reports are not independent reviews")
        start = record.get("hospital_execution_started_at", sealed_at)
        if _timestamp(row.get("reviewed_at")) < _timestamp(start):
            raise ValueError("review predates its execution evidence")
        groups[stage_key].append(row)
    for items in groups.values():
        if len({r["artifact_id"] for r in items}) != 1:
            raise ValueError("one output needs one blinded artifact ID")
    return groups


def _qualify(record, reference, items, decision, rubric):
    """No default clinical thresholds; unknown or unresolved review stays None."""
    if record.get("status") in ("failed", "timeout"):
        return False, [], [], "sealed_execution_failure"
    if record.get("status") != "completed":
        return None, [], ["execution_status"], "unknown"
    if not items:
        return None, [], ["reviews"], "unknown"
    if not decision or not decision["complete"]:
        return None, [], [(decision or {}).get("missing") or "reviews"], "unknown"
    if record["task"] == "prescription":
        positive = _bool(reference.get("positive"), "reference.positive")
        _bool(reference.get("severe"), "reference.severe")
        final = record.get("final_decision")
        if final not in ("approve", "block", "reject"):
            return None, [], ["final_decision"], "unknown"
        return ((final in ("block", "reject")) == positive), [], [], "independent_reference_and_sealed_decision"
    labels = decision["labels"]
    reference_items = {r["fact_id"] for r in reference.get("required_items", [])}
    unit_maps = [r.get("units", {}) for r in items]
    if any(not isinstance(u, dict) for u in unit_maps) or any(u != unit_maps[0] for u in unit_maps):
        raise ValueError("review annotation units disagree or are not mappings")
    annotations = []
    for item, label in labels.items():
        unit = unit_maps[0].get(item, "fact" if item in reference_items else "claim")
        annotations.append(dict(item_id=item, unit=unit, applicable=label != "not_applicable",
                                label="indeterminate" if label == "not_applicable" else label))
    fact_metrics(annotations)
    rule = (rubric or {}).get("tasks", {}).get(record["task"]) if isinstance(rubric, dict) else None
    needed = {"required_items", "severe_errors", "max_incorrect_items", "max_unsupported_items", "max_omitted_items"}
    if not isinstance(rule, dict) or not needed <= rule.keys() or not rubric.get("version"):
        return None, annotations, ["quality_rubric"], "unknown"
    if any(r["rubric_version"] != rubric["version"] for r in items):
        raise ValueError("review rubric_version differs from frozen quality rubric")
    for field in ("required_items", "severe_errors"):
        if not isinstance(rule[field], list) or any(not isinstance(x, str) or not x for x in rule[field]):
            raise ValueError("rubric item lists must be explicit string arrays")
    required = rule["required_items"]
    if required == ["$reference"]:
        required = [r["fact_id"] for r in reference.get("required_items", [])]
    if not required or "$reference" in required:
        return None, annotations, ["required_items"], "unknown"
    for field in needed - {"required_items", "severe_errors"}:
        if type(rule[field]) is not int or rule[field] < 0:
            raise ValueError("rubric thresholds must be explicit nonnegative integers")
    if not set(required) <= labels.keys() or not set(rule["severe_errors"]) <= labels.keys():
        return None, annotations, ["review_item_coverage"], "unknown"
    if any(label == "indeterminate" for label in labels.values()) or any(labels[k] == "not_applicable" for k in required):
        return None, annotations, ["indeterminate_quality"], "unknown"
    qualified = (all(labels[k] == "correct" for k in rule["severe_errors"])
                 and sum(v == "incorrect" for v in labels.values()) <= rule["max_incorrect_items"]
                 and sum(v == "unsupported" for v in labels.values()) <= rule["max_unsupported_items"]
                 and sum(v == "omitted" for v in labels.values()) <= rule["max_omitted_items"])
    return qualified, annotations, [], "independent_review_and_frozen_rubric"


def _early_assessments(record, reference, key, indexed, decisions, rubric):
    stages = ["first_handoff"] if record["task"] == "preconsult" else []
    if record["task"] == "documentation" and record["workflow"] not in ("manual", "manual_documentation", "fully_manual"):
        stages = ["first_draft"]
    result = {}
    for stage in stages:
        output = _stage_output(record, stage)
        items = indexed.get((*key, stage), [])
        decision = decisions.get(items[0]["artifact_id"]) if items else None
        stage_rule = ((rubric or {}).get("tasks", {}).get(record["task"]) or {}).get("stages", {}).get(stage)
        stage_rubric = {"version": (rubric or {}).get("version"), "tasks": {record["task"]: stage_rule}}
        if output is None:
            qualified, annotations, missing, basis = None, [], ["stage_artifact"], "unknown"
        else:
            # Score this earlier artifact independently even if a later step
            # failed. First-pass completion below also checks execution status.
            qualified, annotations, missing, basis = _qualify({**record, "status": "completed"}, reference,
                                                               items, decision, stage_rubric)
        if record.get("evaluation_missing_independent_evidence"):
            qualified = None
            missing.append("independent_execution_evidence")
        result[stage] = dict(artifact_stage=stage, qualified=qualified, quality_source="independent_review",
            quality_basis=basis, artifact_sha256=_digest(output) if output is not None else None,
            review_artifact_id=items[0]["artifact_id"] if items else None,
            source_sha256=_digest(reference), annotations=annotations, missing=sorted(set(missing)))
    return result


def evaluate_reviewed_run(run_dir, dataset_dir, reviews_file, adjudications_file, activity_file=None) -> dict:
    """Verify evidence, independently score cases, and write a NEW analysis bundle.

    reviews_file/adjudications_file are UTF-8 JSONL (empty files allowed). Reviews
    use research.validate_review fields PLUS case_id/config_id/environment,
    artifact_sha256 (canonical actual output), source_sha256 (canonical reference
    row), reviewed_at (timezone, after seal/hospital execution). These identity
    fields are joined by the custodian AFTER blinded review. Optional units maps
    item IDs to fact/claim/required_item. Two reviewers/third-party adjudication
    use research.adjudicate_reviews; unreviewed or unresolved cases stay unknown.
    artifact_stage defaults to final; first_handoff selects recorded preconsult
    first_handoff_output, first_draft selects documentation version 1 content.
    Every stage needs its own blinded artifact ID, two reviews/adjudication and
    canonical content hash. Stage rules MUST be frozen explicitly under
    quality_rubric.tasks[task].stages[stage], with the same five required fields
    as the final rule. Realtime first-draft required items must reflect only the
    available prefix. No final-review or final-rubric substitution is performed.
    first_draft_accepted means independent rubric acceptance of the initial
    artifact, not observed physician willingness to sign it. Manual is N/A.

    Frozen config.quality_rubric: {version, tasks: {preconsult|documentation:
    {required_items: [item IDs] or ['$reference'], severe_errors: [item IDs],
    max_incorrect_items: integer, max_unsupported_items: integer,
    max_omitted_items: integer}}}. No thresholds are inferred or defaulted. A
    completed prescription instead uses its actual sealed/imported final_decision
    and independent reference.positive, after complete independent review.
    Failed/timeout executions cannot qualify, regardless of self-reported quality.

    Hospital rows come only from validated run/hospital imports; ambiguous repeated
    pairs are rejected, never silently pick the latest. Optional activity_file is
    hospital-only JSONL/CSV: ordinary activity columns + config_id/environment,
    source_kind=hospital_observed, independent_execution=True, timing_source=measured,
    operator_id/source_run_id/source_ref/source_sha256. Referenced external timing
    logs must be outside run and are hash-checked and copied. Simulation activity
    always comes from sealed human_activity.csv. Missing activity is not zero time.

    Returns report and writes case_metrics.jsonl, case_assessments.jsonl, report.json,
    analysis_manifest.json and copied evidence under analysis/<evidence-hash>/.
    Original files are reverified before writing. Reports retain failures,
    missing pairs, reviews, rubric and activity coverage. No clinical conclusion
    or proof of real human participation follows from passing these local checks.
    """
    run, manifest, seal, config, cases = _verified_run(run_dir)
    seal_hash = _sha(run / "seal.json")
    configs, expected = _configuration_plan(manifest, cases, config)
    configuration = {r["config_id"]: r for r in configs}
    data = _dataset(dataset_dir, manifest, cases)
    evidence_hashes = {"source_seal_sha256": seal_hash, "datasets_hash": data.dataset_hash,
                       "evaluation_source_sha256": _sha(Path(__file__))}
    files = {}
    source_paths = {}
    for name, path in (("reviews.jsonl", reviews_file), ("adjudications.jsonl", adjudications_file)):
        source = Path(path).resolve()
        files[name] = source.read_bytes()
        evidence_hashes[name] = _sha(source)
        source_paths[source] = evidence_hashes[name]
    reviews, adjudications = _jsonl(Path(reviews_file)), _jsonl(Path(adjudications_file))
    simulation = {}
    for row in _jsonl(run / "case_results.jsonl"):
        key = row.get("case_id"), row.get("config_id")
        if key not in expected or key in simulation or row.get("environment", "simulation") != "simulation":
            raise ValueError("invalid or duplicate sealed simulation execution pair")
        if row.get("task") != cases[key[0]]["task"]:
            raise ValueError("simulation task differs from sealed case")
        if row.get("input_sha256", manifest["case_hashes"][key[0]]) != manifest["case_hashes"][key[0]]:
            raise ValueError("simulation result input hash mismatch")
        value, digest = _output(row, run)
        simulation[key] = {**row, "output": value, "output_sha256": digest}
    hospital = _hospital_rows(run, manifest, seal_hash, expected, files, evidence_hashes)
    environments = {"simulation": simulation}
    if hospital:
        environments["hospital"] = hospital
    records = {}
    issues = []
    for environment, observations in environments.items():
        for cid, config_id in sorted(expected):
            row = observations.get((cid, config_id), {})
            if row.get("task", cases[cid]["task"]) != cases[cid]["task"]:
                raise ValueError("observed task/config pairing mismatch")
            records[cid, config_id, environment] = {**row, "case_id": cid, "config_id": config_id, "environment": environment,
                "task": cases[cid]["task"], "workflow": configuration[config_id].get("workflow", config_id),
                "source_group_id": data.cases[cid]["source_group_id"], "status": row.get("status", "missing")}
    external_keys = {(r.get("case_id"), r.get("config_id"), r.get("environment"), r.get("artifact_stage", "final")) for r in reviews}
    for key, record in records.items():
        if key[2] == "hospital":
            embedded = [r for r in record.get("reviews", []) if (*key, r.get("artifact_stage", "final")) not in external_keys]
            reviews.extend({**r, "case_id": key[0], "config_id": key[1], "environment": "hospital"} for r in embedded)
            if record.get("adjudication") and record["adjudication"].get("artifact_id") in {r.get("artifact_id") for r in embedded}:
                adjudications.append(record["adjudication"])
    indexed = _review_index(reviews, records, data, seal["sealed_at"])
    decisions = {r["artifact_id"]: r for r in adjudicate_reviews(reviews, adjudications)["artifacts"]}
    activities = _rows(run / "human_activity.csv")
    for row in activities:
        if row.get("environment", "simulation") != "simulation":
            raise ValueError("sealed simulation activity claims hospital origin")
        row["environment"] = "simulation"
    if activity_file is not None:
        path = Path(activity_file).resolve()
        if path.is_relative_to(run):
            raise ValueError("hospital activity source must be external to platform run")
        files["hospital_activity" + path.suffix.lower()] = path.read_bytes()
        evidence_hashes["hospital_activity"] = _sha(path)
        source_paths[path] = _sha(path)
        for row in _rows(path):
            if (row.get("environment") != "hospital" or row.get("source_kind") != "hospital_observed"
                    or row.get("independent_execution") is not True or row.get("timing_source") != "measured"):
                raise ValueError("hospital activity requires independent measured hospital provenance")
            _text(row.get("operator_id"), "operator_id")
            if _text(row.get("source_run_id"), "source_run_id") == manifest["run_id"]:
                raise ValueError("hospital activity cannot identify platform run")
            source = _safe_child(path.parent, row.get("source_ref"))
            if source.is_relative_to(run) or _sha(source) != _hash_string(row.get("source_sha256"), "activity source_sha256"):
                raise ValueError("hospital activity log source/digest mismatch")
            files["activity-log-" + row["source_sha256"] + ".log"] = source.read_bytes()
            source_paths[source] = row["source_sha256"]
            activities.append(row)
    grouped_activities = defaultdict(list)
    for row in activities:
        key = row.get("case_id"), row.get("config_id"), row.get("environment")
        if key not in records:
            raise ValueError("activity has no matching case/config/environment")
        if row.get("task", records[key]["task"]) != records[key]["task"]:
            raise ValueError("activity task differs from case")
        grouped_activities[key].append(row)
    role_universe = defaultdict(set)
    for key, activity_rows in grouped_activities.items():
        role_universe[records[key]["task"]].update("physician" if a["role"] == "doctor" else a["role"] for a in activity_rows)
    assessments, case_metrics, aggregate_rows, aggregate_annotations = [], [], defaultdict(list), defaultdict(list)
    stage_issues = []
    for key, record in sorted(records.items()):
        reference = data.references[key[0]]
        items = indexed.get((*key, "final"), [])
        decision = decisions.get(items[0]["artifact_id"]) if items else None
        qualified, annotations, missing, basis = _qualify(record, reference, items, decision, config.get("quality_rubric"))
        if record.get("evaluation_missing_independent_evidence"):
            qualified = None
            missing.append("independent_execution_evidence")
        if record["task"] == "documentation" and record["environment"] == "hospital" and record["status"] == "completed":
            attestation = record.get("attestation")
            if (not attestation or attestation.get("role") != "physician" or not attestation.get("signer_id")
                    or attestation.get("artifact_sha256") != record.get("output_sha256")):
                qualified = None
                missing.append("physician_attestation")
        actual_activities = grouped_activities[key]
        if not actual_activities:
            missing.append("human_activity")
        early = _early_assessments(record, reference, key, indexed, decisions, config.get("quality_rubric"))
        for stage, assessment in early.items():
            if assessment["missing"]:
                stage_issues.append(dict(case_id=key[0], config_id=key[1], environment=key[2],
                                         artifact_stage=stage, missing=assessment["missing"]))
        first_pass = early.get("first_handoff", {}).get("qualified")
        first_completed = (first_pass and record["status"] == "completed") if first_pass is not None else None
        first_draft = early.get("first_draft", {}).get("qualified")
        scored = {**record, "qualified": qualified, "quality_source": "independent_review", "quality_basis": basis,
                  "quality_assessment": {"qualified": qualified, "source": "independent_review", "basis": basis},
                  "first_pass_correct": first_pass, "first_pass_completed": first_completed, "first_draft_accepted": first_draft,
                  "reference": reference}
        effort = effort_metrics([scored], actual_activities)
        metrics = {"qualified": None if qualified is None else int(qualified),
                   "human_active_seconds": effort["total_active_seconds"] if actual_activities else None,
                   "turnaround_seconds": effort["mean_turnaround_seconds"],
                   "first_pass_correct": None if first_pass is None else int(first_pass),
                   "first_pass_completed": None if first_completed is None else int(first_completed),
                   "first_draft_accepted": None if first_draft is None else int(first_draft)}
        for role in sorted(role_universe[record["task"]]):
            # Absent role intervals are unobserved, not automatically zero.
            # A known zero contribution needs an explicit zero-second record.
            metrics[role + "_active_seconds"] = effort["role_active_seconds"].get(role) if actual_activities else None
        if annotations:
            fm = fact_metrics(annotations)
            metrics.update({k: fm[k] for k in ("factual_accuracy", "unsupported_rate", "omission_rate")})
        if missing:
            issues.append(dict(case_id=key[0], config_id=key[1], environment=key[2], missing=sorted(set(missing))))
        group = record["task"], key[1], key[2]
        aggregate_rows[group].append(scored)
        aggregate_annotations[group].extend(annotations)
        identity = {k: record[k] for k in ("case_id", "config_id", "environment", "task", "source_group_id")}
        assessments.append({**identity, "status": record["status"], "qualified": qualified, "quality_source": "independent_review",
                            "quality_basis": basis, "artifact_sha256": record.get("output_sha256"),
                            "source_sha256": _digest(reference), "review_artifact_id": items[0]["artifact_id"] if items else None,
                            "annotations": annotations, "missing": sorted(set(missing)), "stage_assessments": early})
        case_metrics.append({**identity, "metrics": metrics})
    groups = []
    for (task, config_id, environment), scored in sorted(aggregate_rows.items()):
        activity_rows = [a for r in scored for a in grouped_activities[r["case_id"], config_id, environment]]
        effort = effort_metrics(scored, activity_rows)
        missing_time = effort["cases_without_activity"]
        effort["timing_complete"] = missing_time == 0
        if missing_time:
            effort["human_seconds_per_qualified_task"] = None
            effort["role_seconds_per_qualified_task"] = {k: None for k in effort["role_seconds_per_qualified_task"]}
        role_coverage = {}
        for role in sorted(role_universe[task]):
            observed = sum(any(("physician" if a["role"] == "doctor" else a["role"]) == role
                               for a in grouped_activities[r["case_id"], config_id, environment]) for r in scored)
            role_coverage[role] = dict(observed_cases=observed, n_cases=len(scored), complete=observed == len(scored))
            if observed != len(scored):
                effort["role_seconds_per_qualified_task"][role] = None
        effort["role_timing_coverage"] = role_coverage
        group = dict(task=task, config_id=config_id, environment=environment, effort=effort)
        if task == "prescription":
            group["prescription"] = prescription_metrics(scored)
        else:
            group["facts"] = fact_metrics(aggregate_annotations[task, config_id, environment])
        groups.append(group)
    analysis_id = _digest(evidence_hashes)
    destination = _safe_child(run, "analysis/" + analysis_id)
    stage_requested = (any(r.get("artifact_stage", "final") != "final" for r in reviews)
                       or any(rule.get("stages") for rule in ((config.get("quality_rubric") or {}).get("tasks") or {}).values() if isinstance(rule, dict)))
    all_issues = issues + stage_issues if stage_requested else issues
    report = _checked(dict(schema_version="1", run_id=manifest["run_id"], analysis_dir=str(destination), n_case_metrics=len(case_metrics),
                          complete=not all_issues, issues=all_issues, groups=groups, source_seal_sha256=seal_hash,
                          final_evaluation_complete=not issues, stage_evaluation_complete=not stage_issues,
                          stage_evaluation_requested=stage_requested, stage_issues=stage_issues,
                          research_eligible=False, provenance_assurance="local_evidence_validation_not_human_execution_proof"))
    files.update({"case_metrics.jsonl": "".join(_canonical(r) + "\n" for r in case_metrics),
                  "case_assessments.jsonl": "".join(_canonical(r) + "\n" for r in assessments), "report.json": _canonical(report) + "\n"})
    artifacts = {name: hashlib.sha256(value if isinstance(value, bytes) else value.encode("utf-8")).hexdigest() for name, value in files.items()}
    files["analysis_manifest.json"] = _canonical(dict(schema_version="1", run_id=manifest["run_id"],
        analysis_id=analysis_id, source_hashes=evidence_hashes, artifacts=artifacts)) + "\n"
    _verified_run(run)
    _dataset(dataset_dir, manifest, cases)
    if _sha(run / "seal.json") != seal_hash or any(_sha(p) != h for p, h in source_paths.items()):
        raise ValueError("evaluation source changed while being read")
    _write_new_bundle(destination, files)
    return report


def compare_sealed_analysis(run_dir, analysis_dir) -> dict:
    """Read-only controlled comparison; no externally overridable statistics.

    Accept only run/analysis/<source-hash> produced by evaluate_reviewed_run.
    Verify the run seal, analysis manifest and every artifact, the frozen dataset
    and material files, copied reviews/adjudications, original imported hospital
    receipts/results/evidence, and copied measured timing logs. Recompute case
    assessments and metrics from these sources, so rehashing invented metrics
    does not make them admissible. Missing/changed source content is an error.

    Baselines follow the task-specific frozen workflow mapping. seed,
    bootstrap_samples, confidence and fidelity_tolerances come ONLY from sealed
    config.json. The existing paired_comparison runs with exactly that budget.
    No config loader, .env expansion, service, model, write, or CLI modification.

    Study conclusions require matching study purpose/provenance, frozen material
    links, read-only config.readiness revalidation, all expected independent
    hospital pairs, completed final independent reviews (execution failures are
    retained as failures), and known quality/timing. Demo/incomplete evidence
    keeps descriptive statistics with conclusion_allowed=False and all formal
    fidelity decisions suppressed. Stage metrics still require their own
    evidence and coverage. Allowed conclusions concern controlled paired offline
    fidelity only, not actual human participation, causality, or clinical benefit.

    The current evaluation implementation must match its analysis source hash;
    after an evaluator change, create a NEW evaluation bundle from the original
    evidence. Hashes establish local consistency, not independent timestamping.
    """
    from .config import readiness, resolve_path
    from .metrics import paired_comparison
    from .store import verify_seal

    run, manifest, seal, config, cases = _verified_run(run_dir)
    if not verify_seal(run)["valid"]:
        raise ValueError("run seal or artifact inventory is invalid")
    candidate = Path(analysis_dir).absolute()
    if not candidate.is_relative_to(run):
        raise ValueError("analysis must belong to the sealed run")
    analysis = _safe_child(run, candidate.relative_to(run).as_posix())
    if analysis.parent != run / "analysis":
        raise ValueError("analysis must be a direct analysis bundle")
    analysis_manifest = _json(analysis / "analysis_manifest.json")
    sources, artifacts = analysis_manifest.get("source_hashes"), analysis_manifest.get("artifacts")
    if not isinstance(sources, dict) or not isinstance(artifacts, dict):
        raise ValueError("invalid analysis manifest")
    analysis_id = _digest(sources)
    if (analysis_manifest.get("run_id") != manifest["run_id"] or analysis.name != analysis_id
            or analysis_manifest.get("analysis_id") != analysis_id):
        raise ValueError("analysis identity does not bind its sources/run")
    required = {"case_metrics.jsonl", "case_assessments.jsonl", "report.json", "reviews.jsonl", "adjudications.jsonl"}
    if not required <= artifacts.keys() or "analysis_manifest.json" in artifacts:
        raise ValueError("analysis is missing required evidence artifacts")
    checked_paths = {analysis / "analysis_manifest.json": _sha(analysis / "analysis_manifest.json")}
    for name, digest in artifacts.items():
        path = _safe_child(analysis, name)
        if not path.is_file() or _sha(path) != _hash_string(digest, "analysis artifact digest"):
            raise ValueError("analysis artifact is missing or changed: " + name)
        checked_paths[path] = digest
    inventory = {p.relative_to(analysis).as_posix() for p in analysis.rglob("*") if p.is_file()}
    if inventory != set(artifacts) | {"analysis_manifest.json"}:
        raise ValueError("analysis contains unmanifested artifacts")
    seal_hash = _sha(run / "seal.json")
    expected_sources = {"source_seal_sha256": seal_hash, "datasets_hash": manifest["datasets_hash"],
        "evaluation_source_sha256": _sha(Path(__file__)),
        "reviews.jsonl": artifacts["reviews.jsonl"], "adjudications.jsonl": artifacts["adjudications.jsonl"]}
    checked_paths[Path(__file__)] = expected_sources["evaluation_source_sha256"]
    metric_source = Path(__file__).with_name("metrics.py")
    checked_paths[metric_source] = _sha(metric_source)
    if any(sources.get(k) != v for k, v in expected_sources.items()):
        raise ValueError("analysis source seal/dataset/reviews/evaluator digest differs")
    if config.get("purpose", manifest["purpose"]) != manifest["purpose"]:
        raise ValueError("resolved configuration purpose differs from run manifest")
    for field in ("seed", "bootstrap_samples", "confidence"):
        if field not in config:
            raise ValueError("sealed configuration is missing statistical parameter: " + field)
    data = _dataset(resolve_path(_text(config.get("dataset"), "frozen dataset path")), manifest, cases)
    if "data_catalog.json" in seal["artifacts"] and _digest(_json(run / "data_catalog.json")) != data.dataset_hash:
        raise ValueError("sealed catalog differs from the evaluation dataset")
    configurations, expected = _configuration_plan(manifest, cases, config)
    configurations = {c["config_id"]: c for c in configurations}
    baseline_workflows = {"prescription": "pprs_baseline", "preconsult": "nurse_only", "documentation": "manual_documentation"}
    baselines = {}
    for task in {r["task"] for r in cases.values()}:
        matching = [c["config_id"] for c in configurations.values()
                    if c["task"] == task and c.get("workflow") == baseline_workflows.get(task)]
        if len(matching) != 1:
            raise ValueError("task requires exactly one frozen baseline workflow")
        baselines[task] = matching[0]
    reasons = []
    materials = manifest.get("frozen_materials", {})
    if not isinstance(materials, dict):
        raise ValueError("invalid frozen material index")
    configured_materials = {k: config[k] for k in ("seed_manifest", "calibration_file", "clinical_verification_file", "authorization_file", "catalog_file") if config.get(k)}
    if (config.get("pprs") or {}).get("replay_file"):
        configured_materials["pprs.replay_file"] = config["pprs"]["replay_file"]
    for field in materials.keys() | configured_materials.keys():
        binding = materials.get(field)
        if field not in configured_materials:
            raise ValueError("frozen material is not linked by resolved configuration")
        path = resolve_path(configured_materials[field])
        if not path.is_file():
            raise ValueError("configured source material is missing")
        digest = _sha(path)
        if binding is None:
            reasons.append("unfrozen_material:" + field)
        elif (not isinstance(binding, dict) or resolve_path(binding.get("path", "")) != path
              or digest != _hash_string(binding.get("sha256"), "material sha256")):
            raise ValueError("frozen material path or actual content differs")
        checked_paths[path] = digest

    copied_hospital, hospital_sources = {}, {}
    hospital = _hospital_rows(run, manifest, seal_hash, expected, copied_hospital, hospital_sources)
    if {k: v for k, v in sources.items() if k.startswith("hospital_") and k.endswith("_receipt")} != hospital_sources:
        raise ValueError("analysis hospital receipt set differs from original imports")
    for name, content in copied_hospital.items():
        if artifacts.get(name) != hashlib.sha256(content).hexdigest():
            raise ValueError("analysis hospital evidence differs from original import")
    simulation = {}
    for row in _jsonl(run / "case_results.jsonl"):
        key = row.get("case_id"), row.get("config_id")
        if key not in expected or key in simulation or row.get("environment", "simulation") != "simulation":
            raise ValueError("invalid sealed simulation execution pair")
        if row.get("task") != cases[key[0]]["task"] or row.get("input_sha256", manifest["case_hashes"][key[0]]) != manifest["case_hashes"][key[0]]:
            raise ValueError("simulation identity differs from frozen input")
        value, digest = _output(row, run)
        simulation[key] = {**row, "output": value, "output_sha256": digest}
    records = {}
    for environment, observed in {"simulation": simulation, **({"hospital": hospital} if hospital else {})}.items():
        for cid, config_id in sorted(expected):
            row = observed.get((cid, config_id), {})
            if row.get("task", cases[cid]["task"]) != cases[cid]["task"]:
                raise ValueError("observed task/config pairing mismatch")
            records[cid, config_id, environment] = {**row, "case_id": cid, "config_id": config_id,
                "environment": environment, "task": cases[cid]["task"], "source_group_id": data.cases[cid]["source_group_id"],
                "workflow": configurations[config_id].get("workflow", config_id), "status": row.get("status", "missing")}
    reviews, adjudications = _jsonl(analysis / "reviews.jsonl"), _jsonl(analysis / "adjudications.jsonl")
    external = {(r.get("case_id"), r.get("config_id"), r.get("environment"), r.get("artifact_stage", "final")) for r in reviews}
    for key, row in records.items():
        if key[2] == "hospital":
            embedded = [r for r in row.get("reviews", []) if (*key, r.get("artifact_stage", "final")) not in external]
            reviews.extend({**r, "case_id": key[0], "config_id": key[1], "environment": "hospital"} for r in embedded)
            if row.get("adjudication") and row["adjudication"].get("artifact_id") in {r.get("artifact_id") for r in embedded}:
                adjudications.append(row["adjudication"])
    indexed = _review_index(reviews, records, data, seal["sealed_at"])
    decisions = {r["artifact_id"]: r for r in adjudicate_reviews(reviews, adjudications)["artifacts"]}
    activities = _rows(run / "human_activity.csv")
    for row in activities:
        if row.get("environment", "simulation") != "simulation":
            raise ValueError("sealed activity cannot claim hospital origin")
        row["environment"] = "simulation"
    activity_files = [name for name in artifacts if name in ("hospital_activity.csv", "hospital_activity.jsonl")]
    if len(activity_files) > 1:
        raise ValueError("ambiguous hospital activity source")
    if activity_files:
        name = activity_files[0]
        if sources.get("hospital_activity") != artifacts[name]:
            raise ValueError("hospital activity source hash differs")
        for row in _rows(analysis / name):
            if (row.get("environment") != "hospital" or row.get("source_kind") != "hospital_observed"
                    or row.get("independent_execution") is not True or row.get("timing_source") != "measured"):
                raise ValueError("hospital timing lacks independent measured provenance")
            _text(row.get("operator_id"), "operator_id")
            _text(row.get("source_ref"), "source_ref")
            if _text(row.get("source_run_id"), "source_run_id") == manifest["run_id"]:
                raise ValueError("hospital timing refers to simulation run")
            digest = _hash_string(row.get("source_sha256"), "timing source sha256")
            if artifacts.get("activity-log-" + digest + ".log") != digest:
                raise ValueError("copied independent timing log is missing or changed")
            activities.append(row)
    elif "hospital_activity" in sources:
        raise ValueError("hospital activity source is missing")
    by_case, roles = defaultdict(list), defaultdict(set)
    for row in activities:
        key = row.get("case_id"), row.get("config_id"), row.get("environment")
        if key not in records or row.get("task", records[key]["task"]) != records[key]["task"]:
            raise ValueError("activity has no matching execution")
        by_case[key].append(row)
        roles[records[key]["task"]].add("physician" if row["role"] == "doctor" else row["role"])
    def index_rows(name):
        indexed_rows = {}
        for row in _jsonl(analysis / name):
            key = row.get("case_id"), row.get("config_id"), row.get("environment")
            if key in indexed_rows or key not in records:
                raise ValueError("analysis contains duplicate or unproven execution rows")
            indexed_rows[key] = row
        if indexed_rows.keys() != records.keys():
            raise ValueError("analysis does not retain every expected execution")
        return indexed_rows
    assessed, metric_rows = index_rows("case_assessments.jsonl"), index_rows("case_metrics.jsonl")
    final_complete = True
    stage_complete = True
    for key, record in sorted(records.items()):
        reference, items = data.references[key[0]], indexed.get((*key, "final"), [])
        decision = decisions.get(items[0]["artifact_id"]) if items else None
        qualified, annotations, missing, basis = _qualify(record, reference, items, decision, config.get("quality_rubric"))
        if record.get("evaluation_missing_independent_evidence"):
            qualified = None
            missing.append("independent_execution_evidence")
        if record["task"] == "documentation" and key[2] == "hospital" and record["status"] == "completed":
            attestation = record.get("attestation")
            if not attestation or attestation.get("role") != "physician" or not attestation.get("signer_id") or attestation.get("artifact_sha256") != record.get("output_sha256"):
                qualified = None
                missing.append("physician_attestation")
        if not by_case[key]:
            missing.append("human_activity")
        early = _early_assessments(record, reference, key, indexed, decisions, config.get("quality_rubric"))
        stage_complete = stage_complete and not any(a["missing"] for a in early.values())
        first_pass = early.get("first_handoff", {}).get("qualified")
        first_completed = (first_pass and record["status"] == "completed") if first_pass is not None else None
        first_draft = early.get("first_draft", {}).get("qualified")
        scored = {**record, "qualified": qualified, "quality_source": "independent_review",
                  "quality_assessment": {"qualified": qualified, "source": "independent_review", "basis": basis},
                  "first_pass_correct": first_pass, "first_pass_completed": first_completed, "first_draft_accepted": first_draft}
        effort = effort_metrics([scored], by_case[key])
        computed = {"qualified": None if qualified is None else int(qualified),
            "human_active_seconds": effort["total_active_seconds"] if by_case[key] else None,
            "turnaround_seconds": effort["mean_turnaround_seconds"],
            "first_pass_correct": None if first_pass is None else int(first_pass),
            "first_pass_completed": None if first_completed is None else int(first_completed),
            "first_draft_accepted": None if first_draft is None else int(first_draft)}
        for role in sorted(roles[record["task"]]):
            computed[role + "_active_seconds"] = effort["role_active_seconds"].get(role) if by_case[key] else None
        if annotations:
            facts = fact_metrics(annotations)
            computed.update({k: facts[k] for k in ("factual_accuracy", "unsupported_rate", "omission_rate")})
        identity = {k: record[k] for k in ("case_id", "config_id", "environment", "task", "source_group_id")}
        expected_assessment = {**identity, "status": record["status"], "qualified": qualified, "quality_source": "independent_review",
            "quality_basis": basis, "artifact_sha256": record.get("output_sha256"), "source_sha256": _digest(reference),
            "review_artifact_id": items[0]["artifact_id"] if items else None, "annotations": annotations,
            "missing": sorted(set(missing)), "stage_assessments": early}
        if _digest(metric_rows[key]) != _digest({**identity, "metrics": computed}) or _digest(assessed[key]) != _digest(expected_assessment):
            raise ValueError("analysis metrics/assessment differ from independent source recomputation")
        final_complete = final_complete and not missing and qualified is not None
    saved_report = _json(analysis / "report.json")
    if (saved_report.get("run_id") != manifest["run_id"] or saved_report.get("source_seal_sha256") != seal_hash
            or saved_report.get("n_case_metrics") != len(records)
            or saved_report.get("final_evaluation_complete") != final_complete
            or saved_report.get("stage_evaluation_complete") != stage_complete):
        raise ValueError("saved evaluation report differs from verified evidence")
    if manifest["purpose"] != "study":
        reasons.append("non_study_run")
    else:
        needed = {"seed_manifest", "calibration_file", "clinical_verification_file", "authorization_file", "catalog_file"}
        if not needed <= materials.keys():
            reasons.append("study_material_bindings_incomplete")
        if manifest.get("research_eligible") is not True or data.catalog.get("research_eligible") is False:
            reasons.append("engineering_or_unqualified_provenance")
        if any(data.cases[cid].get("source_kind") != "protocol_synthetic" for cid in cases):
            reasons.append("validation_provenance_not_protocol_synthetic")
        if set(cases) != {cid for cid, row in data.cases.items() if row.get("split") == "validation"}:
            reasons.append("frozen_validation_cohort_incomplete")
        ready = readiness(config, data)
        if ready.get("ready") is not True or ready.get("clinical_claims_allowed") is not True:
            reasons.append("study_materials_not_ready")
        if any(row.get("timing_source") != "calibrated" for row in activities if row["environment"] == "simulation"):
            reasons.append("simulation_timing_not_calibrated")
    if set(hospital) != expected:
        reasons.append("independent_hospital_pairs_incomplete")
    if not final_complete:
        reasons.append("final_review_quality_or_timing_incomplete")
    if not config.get("fidelity_tolerances"):
        reasons.append("frozen_fidelity_tolerances_missing")
    allowed = not reasons
    report = paired_comparison(list(metric_rows.values()), baseline_by_task=baselines, seed=config["seed"],
        bootstrap_samples=config["bootstrap_samples"], confidence=config["confidence"], tolerances=config.get("fidelity_tolerances"))
    for task in report["tasks"].values():
        for metric in task["metrics"].values():
            metric["conclusion_allowed"] = allowed and metric["fidelity_pass"] is not None
            if not allowed:
                metric["fidelity_pass"] = None
                metric["tolerance_decisions"] = {}
    report.update(run_id=manifest["run_id"], analysis_id=analysis_id, descriptive_only=not allowed,
        conclusion_allowed=allowed, conclusion_reasons=sorted(set(reasons)),
        conclusion_scope="controlled_paired_offline_fidelity" if allowed else "descriptive_statistics_only",
        source_seal_sha256=seal_hash, analysis_manifest_sha256=checked_paths[analysis / "analysis_manifest.json"],
        datasets_hash=data.dataset_hash, resolved_config_hash=manifest["resolved_config_hash"],
        statistics_source="sealed_resolved_config", metrics_source_sha256=checked_paths[metric_source],
        final_evaluation_complete=final_complete, stage_evaluation_complete=stage_complete,
        provenance_assurance="local_content_and_review_consistency_not_independent_human_execution_proof")
    # Check sources again after the fixed-budget calculation, before returning.
    _verified_run(run)
    _dataset(data.root, manifest, cases)
    fresh_files, fresh_sources = {}, {}
    _hospital_rows(run, manifest, seal_hash, expected, fresh_files, fresh_sources)
    if (fresh_sources != hospital_sources or fresh_files != copied_hospital or _sha(run / "seal.json") != seal_hash
            or not verify_seal(run)["valid"] or any(_sha(p) != h for p, h in checked_paths.items())):
        raise ValueError("comparison evidence changed while being verified")
    return _checked(report)
