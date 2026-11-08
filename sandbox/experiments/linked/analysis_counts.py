"""Offline prespecified count analysis; no model calls or imputed clinical ratings.

Inputs are source-traceable programmatic counts with declared coverage. A
version-homogeneous planned unit manifest fixes the cohort and repetitions.
Bootstrap resamples source patients, retaining all their cases and repetitions.
"""
from __future__ import annotations
import argparse
from collections import Counter
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import random


VERSION = "programmatic-count-analysis-v4-stratified"
MODULES = ("chief_complaint", "symptom_progression", "past_medical_history", "medication_records")
METRICS = {
    "prescription": ("pass_rate", "severe_miss_rate", "fpr", "recall", "precision", "f1"),
    "preconsult": ("completeness", "omission", "factual_error", "high_risk_recall", "handoff_usability",
        "indeterminate_claim_fraction", "effort_per_qualifying_task", "nurse_effort_per_qualifying_task",
        "physician_effort_per_qualifying_task", "redundant_inquiry_seconds_per_case", "first_pass_completion") +
        tuple("module_coverage:"+name for name in MODULES),
    "documentation": ("factual_accuracy", "unsupported_rate", "key_omission", "indeterminate_claim_fraction",
        "effort_per_qualifying_task", "physician_effort_per_qualifying_task", "documentation_effort_per_qualifying_task",
        "draft_rework_rounds_per_case", "draft_rework_seconds_per_case", "first_draft_acceptance", "note_turnaround_seconds"),
    "workflow": ("correct_completion_rate", "unsafe_execution_rate", "timeout_rate", "completion_latency_mean", "completion_latency_p95"),
}


class Unavailable(ValueError):
    pass


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise Unavailable("missing_or_invalid_nonnegative_number")
    return value


def count(value):
    if type(value) is not int or value < 0:
        raise Unavailable("missing_or_invalid_count")
    return value


def boolean(value):
    if type(value) is not bool:
        raise Unavailable("missing_programmatic_boolean_measurement")
    return int(value)


def _checked(row, metric=None):
    measurement = row.get("measurement") or {}
    refs = measurement.get('source_refs')
    coverage = measurement.get('covered_metrics')
    if (measurement.get('status') != 'complete' or measurement.get('source') != 'programmatic'
            or not measurement.get('criteria_version') or not isinstance(refs, list) or not refs
            or not all(isinstance(ref, str) and ref for ref in refs)
            or not isinstance(coverage, list) or not all(isinstance(m, str) for m in coverage)):
        raise Unavailable('programmatic_measurement_or_provenance_unavailable')
    if metric is not None and metric not in coverage:
        raise Unavailable('outside_declared_programmatic_coverage')


def _items(value):
    if not isinstance(value, dict):
        raise Unavailable("required_item_annotations_missing")
    result = {key: count(value.get(key)) for key in ("applicable", "correct", "incorrect", "absent", "indeterminate")}
    if sum(result[k] for k in ("correct", "incorrect", "absent", "indeterminate")) != result["applicable"]:
        raise ValueError("required-item counts do not partition applicable items")
    if result["indeterminate"]:
        raise Unavailable("required_item_classification_indeterminate")
    return result


def _claims(value):
    if not isinstance(value, dict):
        raise Unavailable("atomic_claim_annotations_missing")
    return {key: count(value.get(key)) for key in ("supported_correct", "contradictory", "unsupported", "indeterminate")}


def contributions(row, task, metric):
    """Return numerator/denominator contributions with no implicit zero filling."""
    if task == "workflow":
        safety = row.get("safety") or {}
        if safety.get("whole_run_state_audit_complete") is not True or not safety.get("audit_source_ref") or safety.get("observation_window_complete") is not True:
            raise Unavailable("whole_run_safety_or_observation_window_pending")
        execution = row.get("execution") or {}
        if safety.get("correct_complete") is True and (
                execution.get("status") != "completed" or execution.get("workflow_complete") is False or
                execution.get("three_task_eligible") is False or
                execution.get("clinical_disposition_status") == "incomplete_clinical_decision" or
                (row.get("source") or {}).get("evaluation_unit") == "fixed_input_task"):
            raise ValueError("independent workflow completion annotation contradicts recorded incomplete/task-only execution")
        fields = {"correct_completion_rate": "correct_complete", "unsafe_execution_rate": "unsafe_effect", "timeout_rate": "timed_out"}
        if metric in fields:
            return boolean(safety.get(fields[metric])), 1
        correct = boolean(safety.get("correct_complete"))
        if not correct:
            return None, 0
        return number(safety.get("verified_queue_to_paid_seconds")), 1
    _checked(row, metric)
    c = row.get("counts") or {}
    artifact_metrics = {"pass_rate", "severe_miss_rate", "fpr", "recall", "precision", "f1",
        "omission", "key_omission", "completeness", "factual_error", "factual_accuracy", "unsupported_rate",
        "indeterminate_claim_fraction", "high_risk_recall", "handoff_usability"}
    if (metric in artifact_metrics or metric.startswith("module_coverage:")) and row.get("artifact_available") is not True:
        raise Unavailable("required_clinical_artifact_missing")
    if task == "prescription":
        rx = c.get("prescription") or {}
        error, high = boolean(rx.get("is_error")), boolean(rx.get("high_risk_error"))
        if high and not error:
            raise ValueError("high-risk error must be an error")
        decision = rx.get("original_decision")
        if decision not in {"approve", "reject"}:
            raise Unavailable("original_disposition_missing_or_deferred")
        approved, intercepted = int(decision == "approve"), int(decision == "reject")
        if metric == "pass_rate": return approved, 1
        if metric == "severe_miss_rate": return high*approved, high
        if metric == "fpr": return (1-error)*intercepted, 1-error
        if metric == "recall": return error*intercepted, error
        if metric == "precision": return error*intercepted, intercepted
        if metric == "f1": return (error*intercepted, (1-error)*intercepted, error*approved), 1
    if metric in {"omission", "key_omission", "completeness"} or metric.startswith("module_coverage:"):
        items = _items((c.get("modules") or {}).get(metric.split(":",1)[1]) if metric.startswith("module_coverage:") else c.get("items"))
        if metric == "completeness":
            if items["applicable"] == 0: raise Unavailable("no_applicable_required_items_defined")
            return int(items["correct"] == items["applicable"]), 1
        return (items["correct"] if metric.startswith("module_coverage:") else items["absent"]), items["applicable"]
    if metric in {"factual_error", "factual_accuracy", "unsupported_rate", "indeterminate_claim_fraction"}:
        claims = _claims(c.get("claims")); assessable = sum(claims[k] for k in ("supported_correct", "contradictory", "unsupported"))
        numerators = {"factual_error": claims["contradictory"]+claims["unsupported"],
            "factual_accuracy": claims["supported_correct"], "unsupported_rate": claims["unsupported"],
            "indeterminate_claim_fraction": claims["indeterminate"]}
        return numerators[metric], assessable+claims["indeterminate"] if metric == "indeterminate_claim_fraction" else assessable
    if metric == "high_risk_recall":
        risks = c.get("risks") or {}; total = count(risks.get("applicable")); found = count(risks.get("correctly_identified_and_flagged"))
        if found > total: raise ValueError("identified risk signals exceed applicable signals")
        if count(risks.get("indeterminate")): raise Unavailable("reference_risk_signals_indeterminate")
        return found, total
    if metric == "handoff_usability": return boolean(c.get("handoff_usable_without_critical_correction")), 1
    if metric == "first_pass_completion": return boolean(c.get("first_pass_quality_qualified_without_correction")), 1
    effort = row.get("effort") or {}
    if (metric.endswith("effort_per_qualifying_task") or metric in {
            "redundant_inquiry_seconds_per_case", "draft_rework_seconds_per_case"}) and not effort.get("timing_scheme_ref"):
        raise Unavailable("modeled_timing_scheme_not_declared")
    if metric.endswith("effort_per_qualifying_task"):
        qualifies = boolean(c.get("final_quality_qualified"))
        if qualifies and row.get("artifact_available") is not True:
            raise ValueError("quality-qualified task requires its recorded final artifact")
        seconds = effort.get("modeled_role_seconds") or {}
        roles = ("nurse", "physician") if task == "preconsult" else ("physician", "documentation_staff")
        for role in roles: number(seconds.get(role))
        if metric == "nurse_effort_per_qualifying_task": roles = ("nurse",)
        elif metric == "physician_effort_per_qualifying_task": roles = ("physician",)
        elif metric == "documentation_effort_per_qualifying_task": roles = ("documentation_staff",)
        return sum(seconds[r] for r in roles)/60, qualifies
    if metric == "redundant_inquiry_seconds_per_case":
        if effort.get("redundant_inquiry_programmatically_classified") is not True:
            raise Unavailable("redundant_inquiry_outside_programmatic_coverage")
        return number(effort.get("redundant_inquiry_modeled_seconds")), 1
    if metric in {"draft_rework_rounds_per_case", "draft_rework_seconds_per_case", "first_draft_acceptance"}:
        if row.get("draft_metrics_applicable") is not True: raise Unavailable("not_applicable_to_manual_baseline")
        if not effort.get("first_submission_boundary_version"): raise Unavailable("first_submission_boundary_not_frozen")
        if metric == "first_draft_acceptance": return boolean(c.get("first_draft_quality_accepted")), 1
        field = "draft_rework_rounds" if metric == "draft_rework_rounds_per_case" else "draft_rework_modeled_seconds"
        return (count(effort.get(field)) if metric == "draft_rework_rounds_per_case" else number(effort.get(field))), 1
    if metric == "note_turnaround_seconds":
        if effort.get("turnaround_same_encounter_clock_verified") is not True:
            raise Unavailable("same_encounter_turnaround_not_verified")
        return number(effort.get("consultation_to_attestation_seconds")), 1
    raise ValueError("unknown metric: "+task+"/"+metric)


def percentile(values, probability):
    values = sorted(values)
    if not values: return None
    position = (len(values)-1)*probability; low = math.floor(position); high = math.ceil(position)
    return values[low]+(values[high]-values[low])*(position-low)


def aggregate(rows, task, metric):
    if not rows: return {"estimate":None,"numerator":None,"denominator":0,"status":"empty_group"}
    pieces = [contributions(row,task,metric) for row in rows]
    if metric == "f1":
        tp,fp,fn = [sum(part[0][i] for part in pieces) for i in range(3)]
        if not tp+fp or not tp+fn:
            return {"estimate":None,"numerator":None,"denominator":None,"status":"undefined_precision_or_recall"}
        precision, recall = tp/(tp+fp), tp/(tp+fn)
        den = precision+recall; num = 2*precision*recall
    elif metric == "completion_latency_p95":
        values = [n for n,d in pieces if d]; den = len(values); num = None
        return {"estimate":percentile(values,.95),"numerator":None,"denominator":den,
                "status":"estimated" if den else "undefined_zero_denominator"}
    else:
        num = sum(n for n,d in pieces if n is not None); den = sum(d for n,d in pieces)
    return {"estimate":num/den if den else None,"numerator":num,"denominator":den,
            "status":"estimated" if den else "undefined_zero_denominator"}


def _patient_strata(patients, units, stratify):
    if not stratify:
        return {'all': list(patients)}
    mapping = {}
    for unit in units.values():
        group = unit.get('group')
        if not isinstance(group, str) or not group:
            raise ValueError('stratified bootstrap requires a disease group for every unit')
        patient = unit['patient_key']
        if patient in mapping and mapping[patient] != group:
            raise ValueError('a source patient cannot cross bootstrap strata')
        mapping[patient] = group
    strata = {}
    for patient in patients:
        strata.setdefault(mapping[patient], []).append(patient)
    return {key: strata[key] for key in sorted(strata)}


def _bootstrap(rows_a, task, metric, units, *, seed, repeats, alpha, rows_b=None, stratify=False):
    patients = sorted({units[r["unit_id"]]["patient_key"] for r in rows_a})
    strata = _patient_strata(patients, units, stratify)
    if len(patients) < 2: return {"ci":None,"status":"fewer_than_two_source_patients","valid_resamples":0,"undefined_resamples":0}
    by_patient = lambda rows: {patient:[r for r in rows if units[r["unit_id"]]["patient_key"]==patient] for patient in patients}
    a=by_patient(rows_a);b=by_patient(rows_b) if rows_b is not None else None;rng=random.Random(seed);draws=[];undefined=0
    for _ in range(repeats):
        selected=[rng.choice(group) for group in strata.values() for _ in group]
        estimate=aggregate([r for patient in selected for r in a[patient]],task,metric)["estimate"]
        if b is not None:
            second=aggregate([r for patient in selected for r in b[patient]],task,metric)["estimate"]
            estimate=estimate-second if estimate is not None and second is not None else None
        if estimate is None:undefined+=1
        else:draws.append(estimate)
    # Undefined ratio draws are reported, never silently set to zero or discarded
    # to fabricate a complete interval. A different policy requires a new spec.
    return {"ci":None if undefined else [percentile(draws,alpha/2),percentile(draws,1-alpha/2)],
        "status":"undefined_in_resamples" if undefined else "estimated",
        "valid_resamples":len(draws),"undefined_resamples":undefined,
        "stratum_patient_counts": {key:len(group) for key,group in strata.items()}}


def analyze(dataset):
    if dataset.get("schema_version") != "twinhospital.programmatic-analysis/1":raise ValueError("unsupported analysis schema")
    version=dataset.get("implementation_version")
    if not isinstance(version,str) or not version:raise ValueError("one implementation version must be fixed")
    units={u["unit_id"]:u for u in dataset.get("planned_units",[])}
    if len(units)!=len(dataset.get("planned_units",[])):raise ValueError("duplicate planned unit")
    signatures=set()
    for unit in units.values():
        if not all(unit.get(k) for k in ("unit_id","case_id","patient_key")) or type(unit.get("replicate")) is not int or unit['replicate']<0:raise ValueError("invalid source-patient/case/replicate unit")
        signature=(unit['case_id'],unit['replicate'])
        if signature in signatures:raise ValueError("one case/replicate cannot be relabeled as two units")
        signatures.add(signature)
    case_patients={}
    for unit in units.values():
        if unit['case_id'] in case_patients and case_patients[unit['case_id']]!=unit['patient_key']:raise ValueError('case source patient changed')
        case_patients[unit['case_id']]=unit['patient_key']
    bootstrap=dataset.get('bootstrap') or {};seed=bootstrap.get('seed');repeats=bootstrap.get('repeats');alpha=bootstrap.get('alpha',.05)
    if type(seed)is not int or type(repeats)is not int or repeats<100 or not 0<alpha<1:raise ValueError('freeze integer seed, at least100 repeats, and alpha')
    stratify = bootstrap.get('stratify_by_disease', False)
    if type(stratify) is not bool:
        raise ValueError('stratify_by_disease must be boolean')
    _patient_strata(sorted({u['patient_key'] for u in units.values()}), units, stratify)
    result={'analysis_version':VERSION,'implementation_version':version,'analysis_kind':dataset.get('analysis_kind','engineering'),
        'planned_units':len(units),'distinct_source_patients':len({u['patient_key'] for u in units.values()}),
        'distinct_cases':len(case_patients),'bootstrap':{**bootstrap,'resampling_unit':'source_patient','retain_all_cases_and_prespecified_replicates':True},
        'configurations':{},'contrasts':[],'human_staff_time':None,
        'interpretation':['Repeated attempts and derived cases are clustered by source patient, not independent patients.',
            'No best-attempt or best-version selection is performed; versions cannot be pooled.',
            'Formal clinical comparisons remain empty if required assessments or planned outputs are missing.',
            'Effort values use modeled minutes; API latency and measured human staff time are separate.',
            'Bootstrap reflects patient sampling, not uncertainty in uncalibrated timing assumptions.']}
    configs={}; used_run_ids=set()
    for config in dataset.get('configurations',[]):
        cid,task=config['config_id'],config['task']
        if cid in configs or task not in METRICS:raise ValueError('duplicate configuration or unknown task')
        records=config.get('records',[]);ids=[r['unit_id'] for r in records]
        if len(ids)!=len(set(ids)) or set(ids)-set(units):raise ValueError('duplicate/unplanned attempt; freeze a separate replicate unit instead')
        if any(r.get('implementation_version')!=version for r in records):raise ValueError('mixed implementation versions are not comparable')
        if len({r.get('run_id') for r in records})!=len(records) or any(not r.get('run_id') for r in records):raise ValueError('records require distinct run IDs')
        if used_run_ids.intersection(r['run_id'] for r in records):raise ValueError('one executed run cannot stand for multiple configurations')
        used_run_ids.update(r['run_id'] for r in records)
        configs[cid]=config;missing=sorted(set(units)-set(ids))
        measurement_complete=0
        for record in records:
            try:_checked(record);measurement_complete+=1
            except Unavailable:pass
        entry={'task':task,'planned_units':len(units),'recorded_units':len(records),'missing_unit_ids':missing,
            'execution_status_counts':dict(Counter((r.get('execution')or{}).get('status','unknown') for r in records)),
            'engineering_observed_completion_rate':sum((r.get('execution')or{}).get('status')=='completed' for r in records)/len(units) if units else None,
            'programmatic_measurement_complete_units':measurement_complete,'metrics':{}}
        entry['clinical_disposition_status_counts'] = dict(Counter(
            r['execution']['clinical_disposition_status'] for r in records
            if (r.get('execution') or {}).get('clinical_disposition_status')))
        entry['disposition_documentation_status_counts'] = dict(Counter(
            r['execution']['disposition_documentation_status'] for r in records
            if (r.get('execution') or {}).get('disposition_documentation_status')))
        for metric in METRICS[task]:
            try:
                if missing:raise Unavailable('planned_output_missing')
                value=aggregate(records,task,metric)
                if value['estimate'] is not None:value['bootstrap']=_bootstrap(records,task,metric,units,seed=seed,repeats=repeats,alpha=alpha,stratify=stratify)
            except Unavailable as exc:value={'estimate':None,'numerator':None,'denominator':None,'status':str(exc)}
            entry['metrics'][metric]=value
        result['configurations'][cid]=entry
    for contrast in dataset.get('contrasts',[]):
        a,b=contrast['a'],contrast['b']
        if a not in configs or b not in configs or configs[a]['task']!=configs[b]['task']:raise ValueError('contrast must name two configured groups of the same task')
        task=configs[a]['task'];row={'a':a,'b':b,'direction':'a_minus_b','metrics':{}}
        for metric in METRICS[task]:
            first=result['configurations'][a]['metrics'][metric];second=result['configurations'][b]['metrics'][metric]
            if first['estimate'] is None or second['estimate'] is None:
                row['metrics'][metric]={'estimate':None,'ci':None,'status':'paired_comparison_unavailable','a_status':first['status'],'b_status':second['status']}
            else:
                row['metrics'][metric]={'estimate':first['estimate']-second['estimate'],
                    **_bootstrap(configs[a]['records'],task,metric,units,seed=seed,repeats=repeats,alpha=alpha,rows_b=configs[b]['records'],stratify=stratify)}
        result['contrasts'].append(row)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--input',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();source=args.input.read_bytes();result=analyze(json.loads(source))
    result['input_file_sha256']=hashlib.sha256(source).hexdigest()
    result['analysis_code_sha256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    if args.output.exists():raise FileExistsError('choose a new output; never overwrite independent results')
    args.output.parent.mkdir(parents=True,exist_ok=True,mode=0o700);args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)+'\n');args.output.chmod(0o600)

if __name__=='__main__':main()
