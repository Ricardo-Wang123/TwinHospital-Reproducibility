"""Patient-paired analysis of the paper's checkable delivery and role effort.

This is a separate endpoint analysis, not an unrestricted clinical-accuracy
score. It reads finished matrix batches, retains every planned patient and
configuration, and never calls a model or writes to the HIS.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime
import fcntl
import hashlib
import json
import math
from pathlib import Path
import random
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sandbox.experiments.linked.analysis_counts import percentile
from sandbox.experiments.linked.cases import GROUPS
from sandbox.experiments.linked.clinical_faults import REGISTRY_PATH
from sandbox.experiments.linked.contracts import baseline_conditions, digest, paper_conditions, runtime_case
from sandbox.experiments.linked import prescription_analysis as prescription_scope
from sandbox.experiments.linked.programmatic_delivery import evaluate, VERSION as DELIVERY_VERSION

VERSION = 'paired-delivery-effort-analysis-v1'
TASKS = ('preconsult', 'documentation')
STAFF = ('nurse', 'doctor', 'pharmacist', 'senior_pharmacist')
METRICS = ('delivery_rate', 'staff_minutes_per_delivered_task',
           'nurse_minutes_per_delivered_task', 'physician_minutes_per_delivered_task')
NOT_RUN = {'not_run_operator_interruption', 'not_run_submission_window',
           'not_run_budget_limit', 'not_run_provider_unavailable', 'not_run_missing_frozen_input'}


def paper_contrasts(conditions):
    contrasts = []
    for task in ('preconsult', 'prescription', 'documentation'):
        rows = [c for c in conditions if c['task'] == task]
        if len(rows) not in (3,5) or rows[0]['model_family'] is not None:
            raise ValueError('Three single-tool configurations, or five legacy pair configurations, are required per task')
        contrasts.extend({'a': row['config_id'], 'b': rows[0]['config_id']} for row in rows[1:])
        for workflow in dict.fromkeys(row['workflow'] for row in rows[1:]):
            pair = [row for row in rows if row['workflow'] == workflow]
            if len(pair) not in (1,2):
                raise ValueError('Each assisted workflow needs its fixed tool condition or declared legacy pair')
            if len(pair) == 2:
                contrasts.append({'a': pair[0]['config_id'], 'b': pair[1]['config_id']})
    return contrasts


def documentation_timing_contrasts(conditions):
    """Within-model timing comparisons, without adding model or patient groups."""
    rows=[c for c in conditions if c['task']=='documentation' and c['model_family'] is not None]
    result=[]
    for family in dict.fromkeys(c['model_family'] for c in rows):
        pair={c['workflow']:c['config_id'] for c in rows if c['model_family']==family}
        if set(pair)!={'realtime','postvisit'}:
            raise ValueError('Both note timing conditions must be present for each selected tool')
        result.append({'a':pair['realtime'],'b':pair['postvisit']})
    return result


def validate_formal_cohort(units, planned):
    total, per_disease = planned.get('source_patients'), planned.get('per_disease')
    if (type(total) is not int or type(per_disease) is not int or per_disease < 1
            or total != len(GROUPS) * per_disease or len(units) != total
            or len({u['patient_key'] for u in units}) != total
            or Counter(u['group'] for u in units) != Counter({group: per_disease for group in GROUPS})):
        raise ValueError('Formal analysis requires the complete prospectively planned source-patient cohort')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def observations(run, task, *, declared_weights):
    delivery = evaluate(run, task)
    activities = run.get('activities')
    roles = Counter()
    valid = isinstance(activities, list) and bool(run.get('timing_source'))
    if valid:
        for activity in activities:
            seconds = activity.get('active_seconds')
            kind = activity.get('activity_type')
            if (type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0
                    or seconds != declared_weights.get(kind)):
                valid = False
                break
            if activity.get('role') in STAFF:
                roles[activity['role']] += seconds
    qualified = delivery['qualifies']
    return {'qualifies': qualified, 'work_observed': valid,
            'staff_seconds': sum(roles.values()) if valid else None,
            'nurse_seconds': roles['nurse'] if valid else None,
            'physician_seconds': roles['doctor'] if valid else None,
            'checks': delivery['checks'], 'execution_status': run['status'],
            'scope': 'artifact_delivery_and_logged_role_activity_not_clinical_truth'}


def contributions(observation, metric):
    qualifies = observation.get('qualifies')
    if type(qualifies) is not bool:
        return None
    if metric == 'delivery_rate':
        return int(qualifies), 1
    if observation.get('work_observed') is not True:
        return None
    field = {'staff_minutes_per_delivered_task': 'staff_seconds',
             'nurse_minutes_per_delivered_task': 'nurse_seconds',
             'physician_minutes_per_delivered_task': 'physician_seconds'}[metric]
    value = observation.get(field)
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        return None
    return value / 60, int(qualifies)


def paired_statistics(units, configurations, contrasts, bootstrap):
    """Resample source patients once per draw for every paired configuration."""
    if not units or len({u['unit_id'] for u in units}) != len(units):
        raise ValueError('Nonempty distinct planned units are required')
    seed, repeats, alpha = (bootstrap[k] for k in ('seed', 'repeats', 'alpha'))
    if type(seed) is not int or type(repeats) is not int or repeats < 100 or not 0 < alpha < 1:
        raise ValueError('Invalid frozen bootstrap settings')
    if bootstrap.get('stratify_by_disease') is not True:
        raise ValueError('The paper requires resampling within disease strata')
    patient_groups, strata = {}, defaultdict(list)
    for unit in units:
        patient, group = unit['patient_key'], unit['group']
        if patient in patient_groups and patient_groups[patient] != group:
            raise ValueError('One source patient cannot cross disease strata')
        patient_groups[patient] = group
    for patient, group in sorted(patient_groups.items()):
        strata[group].append(patient)
    expected = {u['unit_id'] for u in units}
    results, vectors = {}, {}
    for cid, rows in configurations.items():
        if set(rows) != expected:
            raise ValueError('Each condition must retain every planned unit')
        results[cid] = {}
        for metric in METRICS:
            grouped = {patient: [0., 0.] for patient in patient_groups}
            missing = 0
            for unit in units:
                piece = contributions(rows[unit['unit_id']], metric)
                if piece is None:
                    missing += 1
                else:
                    grouped[unit['patient_key']][0] += piece[0]
                    grouped[unit['patient_key']][1] += piece[1]
            num = sum(v[0] for v in grouped.values())
            den = sum(v[1] for v in grouped.values())
            status = 'missing_evidence' if missing else 'estimated' if den else 'undefined_zero_denominator'
            results[cid][metric] = {'estimate': num/den if status == 'estimated' else None,
                'numerator': num if not missing else None, 'denominator': den if not missing else None,
                'observed_numerator': num, 'observed_denominator': den,
                'planned_units': len(units), 'unknown_units': missing, 'status': status}
            if not missing:
                vectors[cid, metric] = grouped
    paired = []
    for contrast in contrasts:
        a, b = contrast['a'], contrast['b']
        if a not in results or b not in results or a == b:
            raise ValueError('Invalid prespecified contrast')
        row = {'a': a, 'b': b, 'direction': 'a_minus_b', 'metrics': {}}
        for metric in METRICS:
            x, y = results[a][metric]['estimate'], results[b][metric]['estimate']
            row['metrics'][metric] = {'estimate': x-y if x is not None and y is not None else None,
                'status': 'estimated' if x is not None and y is not None else 'undefined_component',
                'paired_planned_units': len(units)}
        paired.append(row)
    draws = {key: [] for key in vectors}
    paired_draws = {(i, metric): [] for i in range(len(paired)) for metric in METRICS}
    rng = random.Random(seed)
    can_resample = all(len(group) >= 2 for group in strata.values())
    if can_resample:
        for _ in range(repeats):
            selected = Counter(patient for group in sorted(strata)
                               for patient in rng.choices(strata[group], k=len(strata[group])))
            current = {}
            for key, vector in vectors.items():
                num = sum(vector[patient][0] * n for patient, n in selected.items())
                den = sum(vector[patient][1] * n for patient, n in selected.items())
                value = num/den if den else None
                current[key] = value
                draws[key].append(value)
            for i, contrast in enumerate(paired):
                for metric in METRICS:
                    a, b = current.get((contrast['a'], metric)), current.get((contrast['b'], metric))
                    paired_draws[i, metric].append(a-b if a is not None and b is not None else None)

    def interval(values, available):
        if not available:
            return {'ci': None, 'status': 'missing_evidence', 'valid_resamples': 0, 'undefined_resamples': 0}
        if len(patient_groups) < 2:
            return {'ci': None, 'status': 'fewer_than_two_source_patients', 'valid_resamples': 0, 'undefined_resamples': 0}
        if not can_resample:
            return {'ci': None, 'status': 'insufficient_within_stratum_replication', 'valid_resamples': 0, 'undefined_resamples': 0}
        valid = [value for value in values if value is not None]
        undefined = len(values)-len(valid)
        return {'ci': None if undefined else [percentile(valid, alpha/2), percentile(valid, 1-alpha/2)],
                'status': 'undefined_in_resamples' if undefined else 'estimated',
                'valid_resamples': len(valid), 'undefined_resamples': undefined}

    for cid, metrics in results.items():
        for metric, result in metrics.items():
            result['bootstrap'] = interval(draws.get((cid, metric), []), (cid, metric) in vectors)
    for i, contrast in enumerate(paired):
        for metric, result in contrast['metrics'].items():
            result['bootstrap'] = interval(paired_draws[i, metric],
                (contrast['a'], metric) in vectors and (contrast['b'], metric) in vectors)
    return {'planned_units': len(units), 'distinct_source_patients': len(patient_groups),
            'source_patients_per_stratum': {g: len(v) for g, v in sorted(strata.items())},
            'configurations': results, 'contrasts': paired, 'bootstrap': bootstrap,
            'human_staff_time': None, 'clinical_accuracy': None}


def analyze_batch(batch, manifest, plan, output):
    batch, manifest, plan, output = map(lambda p: Path(p).resolve(), (batch, manifest, plan, output))
    if output.exists() or output.is_relative_to(batch):
        raise ValueError('Use a new analysis directory outside the source batch')
    if not (batch/'batch_summary.json').exists():
        raise ValueError('A finished batch is required')
    with (batch/'batch.lock').open() as lock:
        fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        return _analyze_locked(batch, manifest, plan, output)


def canonical_prescription_stage(batch, case_ref, entry, pinned):
    if not entry or not entry.get('result_path'):
        return None
    path = Path(entry['result_path']).resolve()
    if not path.is_relative_to(batch):
        raise ValueError('Canonical source escapes its batch')
    anchor = pinned(path)
    expected = {k: v.to_dict() for k, v in baseline_conditions().items()}
    if (anchor['case_id'] != case_ref['case_id'] or anchor['input_hash'] != case_ref['runtime_input_hash']
            or anchor['status'] != entry['status'] or anchor['evaluation_unit'] != 'complete_encounter'
            or anchor['conditions'] != expected):
        raise ValueError('Canonical prescription source differs from the frozen assignment')
    stage = anchor['stage_inputs'].get('prescription')
    if stage and stage['prescription'] != anchor['artifacts']['consultation']['prescription']:
        raise ValueError('Canonical prescription stage differs from the recorded consultation')
    return stage


def _analyze_locked(batch, manifest, plan_path, output):
    sources = {}
    analyzer_hash = sha(Path(__file__))
    def pinned(path):
        sources[str(path)] = sha(path)
        return read(path)
    spec = pinned(batch/'specification.json')
    summary = pinned(batch/'batch_summary.json')
    plan = pinned(plan_path)
    rows = pinned(manifest)['cases']
    formal = spec.get('formal_experiment') is True
    if formal:
        bound = spec.get('formal_inputs') or {}
        if (sources[str(manifest)] != bound.get('cohort_sha256')
                or sources[str(plan_path)] != bound.get('analysis_plan_sha256')
                or any(plan.get('runtime_inputs', {}).get(k) != bound.get(k)
                       for k in ('cohort_sha256', 'operation_schedule_sha256'))):
            raise ValueError('Formal analysis plan, cohort, or operation schedule differs from the execution input freeze')
    if len({r['case_id'] for r in rows}) != len(rows):
        raise ValueError('Duplicate manifest case')
    lookup = {r['case_id']: r for r in rows}
    expected_conditions = [c.to_dict() for c in paper_conditions(model_families=spec['models']['tool_models'])]
    if spec['mode'] != 'matrix' or spec['conditions'] != expected_conditions:
        raise ValueError('All paper conditions declared for the fixed tool configuration must be retained')
    if plan['conditions'] != expected_conditions or plan['delivery_criteria_version'] != DELIVERY_VERSION:
        raise ValueError('Analysis plan does not match the frozen task definitions')
    criteria_path = ROOT/'sandbox/experiments/linked/programmatic_delivery.py'
    if plan.get('delivery_criteria_sha256') != sha(criteria_path):
        raise ValueError('Delivery criteria code changed after the analysis plan was frozen')
    sources[str(criteria_path)] = sha(criteria_path)
    if plan['metrics'] != list(METRICS):
        raise ValueError('Analysis endpoints differ from the prespecified plan')
    if plan['contrasts'] != paper_contrasts(expected_conditions):
        raise ValueError('Retain every declared baseline contrast and any explicitly declared legacy model contrast')
    secondary=plan.get('secondary_contrasts',[])
    if secondary and secondary!=documentation_timing_contrasts(expected_conditions):
        raise ValueError('Secondary note timing contrasts must preserve the same tool model')
    frozen, started = (datetime.fromisoformat(v.replace('Z', '+00:00')) for v in
                       (plan['frozen_at'], summary['session_started_at']))
    if frozen.tzinfo is None or started.tzinfo is None:
        raise ValueError('Explicit timestamp zones are required')
    prospective = frozen <= started
    if formal and not prospective:
        raise ValueError('Formal analysis plan must predate execution')
    rx_scope = bool(plan.get('prescription_analysis'))
    if formal and not rx_scope:
        raise ValueError('Formal analysis requires the planned prescription rule scope')
    rx_configs, rule_coverage = {}, {}
    if rx_scope:
        if spec.get('anchor_reuse') or spec.get('controlled_prescription_variant_policy'):
            raise ValueError('Prescription scope requires fresh canonical originals without controlled injection')
        rule_data = pinned(REGISTRY_PATH)
        if digest(rule_data) != spec.get('rules', {}).get('registry_hash'):
            raise ValueError('Declared prescription rule registry differs from the executed registry')
        for filename in ('clinical_faults.py', 'documented_pprs.py'):
            path = REGISTRY_PATH.with_name(filename)
            sources[str(path)] = sha(path)
            if sources[str(path)] != spec['code_hashes'].get(filename):
                raise ValueError('Prescription rule implementation differs from the executed version')
        scope_path = Path(prescription_scope.__file__).resolve()
        sources[str(scope_path)] = sha(scope_path)
        scope_freeze = {'version': prescription_scope.VERSION, 'registry_sha256': sources[str(REGISTRY_PATH)],
                        'scorer_sha256': sources[str(scope_path)],
                        'catalogue_route_rule': prescription_scope.CATALOGUE_ROUTE_RULE,
                        'high_risk_rule_sources': prescription_scope.HIGH_RISK_RULE_SOURCES}
        if formal and plan.get('prescription_scope_freeze') != scope_freeze:
            raise ValueError('Prescription scorer and reference scope must match the prospective analysis plan')
        rule_ids = [r['rule_id'] for r in rule_data['rules']]
        rule_coverage = {r: Counter() for r in [prescription_scope.CATALOGUE_ROUTE_RULE, *rule_ids]}
        rx_configs = {c['config_id']: {} for c in expected_conditions if c['task'] == 'prescription'}
    units, configs, checks, statuses = [], {}, {}, {}
    for c in expected_conditions:
        if c['task'] in TASKS:
            configs[c['config_id']], checks[c['config_id']], statuses[c['config_id']] = {}, {}, Counter()
        elif c['config_id'] in rx_configs:
            statuses[c['config_id']] = Counter()
    for case_ref in spec['cases']:
        row = lookup[case_ref['case_id']]
        path = Path(row['path'])
        case = pinned(path)
        if (sources[str(path)] != case_ref['file_sha256'] or digest(runtime_case(case)) != case_ref['runtime_input_hash']
                or case['case_id'] != case_ref['case_id'] or case['patient_key'] != row['patient_key']
                or case['group'] != case_ref['group']):
            raise ValueError('Source identity or byte hash differs from the frozen run')
        if formal and (case.get('data_kind') != 'mimic_derived' or case.get('split') != 'test'):
            raise ValueError('Formal analysis excludes engineering and fictional inputs')
        unit = {'unit_id': case['case_id'], 'patient_key': case['patient_key'], 'group': case['group']}
        units.append(unit)
        index = pinned(batch/case['case_id']/'attempts.json')
        if index['case_id'] != case['case_id']:
            raise ValueError('Attempt index identity mismatch')
        if rx_scope:
            stage = canonical_prescription_stage(batch, case_ref, index['attempts'].get('linked_baseline_anchor'), pinned)
            reference = prescription_scope.reference_from_stage(stage, rule_ids, case['formulary'])
            reference_fields = (reference['status'], reference['high_risk_predicate_confirmed'])
            for rule_id, counter in rule_coverage.items():
                counter[reference['rules'].get(rule_id, 'source_unavailable')] += 1
        for condition in expected_conditions:
            cid, task = condition['config_id'], condition['task']
            if task not in TASKS and cid not in rx_configs:
                continue
            entry = index['attempts'].get(cid.replace(':', '__'))
            if not entry or entry.get('config_id') != cid:
                raise ValueError('A planned assignment is missing or changed')
            statuses[cid][entry['status']] += 1
            if not entry.get('result_path'):
                if cid in rx_configs:
                    rx_configs[cid][unit['unit_id']] = ('unavailable', *reference_fields)
                    continue
                unstarted = entry['status'] in NOT_RUN
                observation = {'qualifies': False if unstarted else None,
                    'work_observed': unstarted, 'staff_seconds': 0 if unstarted else None,
                    'nurse_seconds': 0 if unstarted else None, 'physician_seconds': 0 if unstarted else None,
                    'checks': {}, 'execution_status': entry['status']}
            else:
                path = Path(entry['result_path']).resolve()
                if not path.is_relative_to(batch):
                    raise ValueError('Source run escapes its batch')
                run = pinned(path)
                if (run['case_id'] != case['case_id'] or run['config_id'] != cid
                        or run['status'] != entry['status'] or run['input_hash'] != case_ref['runtime_input_hash']
                        or run['evaluation_unit'] != 'fixed_input_task'
                        or ((formal or cid in rx_configs) and run.get('conditions', {}).get(task) != condition)):
                    raise ValueError('Run differs from its planned assignment')
                if cid in rx_configs:
                    decision = prescription_scope.original_disposition(run, stage)
                    rx_configs[cid][unit['unit_id']] = (decision, *reference_fields)
                    continue
                observation = observations(run, task, declared_weights=spec['modeled_activity_seconds'])
            configs[cid][unit['unit_id']] = observation
            for name, value in observation['checks'].items():
                count = checks[cid].setdefault(name, {'pass': 0, 'fail': 0, 'unknown': 0})
                count['pass' if value is True else 'fail' if value is False else 'unknown'] += 1
    if formal:
        validate_formal_cohort(units, plan.get('formal_cohort', {}))
    contrasts = [c for c in plan['contrasts']+secondary if c['a'] in configs and c['b'] in configs]
    full = paired_statistics(units, configs, contrasts, plan['bootstrap'])
    stratified = {}
    for group in sorted({u['group'] for u in units}):
        selected = [u for u in units if u['group'] == group]
        ids = {u['unit_id'] for u in selected}
        stratified[group] = paired_statistics(selected,
            {cid: {uid: row for uid, row in values.items() if uid in ids} for cid, values in configs.items()},
            contrasts, plan['bootstrap'])
    rx_results = None
    if rx_scope:
        rx_contrasts = [c for c in plan['contrasts'] if c['a'] in rx_configs and c['b'] in rx_configs]
        rx_results = {**scope_freeze, 'rule_coverage': dict(rule_coverage),
                      'pooled': prescription_scope.paired_scope_statistics(units, rx_configs, rx_contrasts, plan['bootstrap']),
                      'by_disease': {}}
        for group in stratified:
            selected = [u for u in units if u['group'] == group]
            ids = {u['unit_id'] for u in selected}
            subset = {cid: {uid: outcomes[uid] for uid in ids} for cid, outcomes in rx_configs.items()}
            rx_results['by_disease'][group] = prescription_scope.paired_scope_statistics(
                selected, subset, rx_contrasts, plan['bootstrap'])
    if any(sha(path) != expected for path, expected in sources.items()) or sha(Path(__file__)) != analyzer_hash:
        raise ValueError('Source changed during analysis')
    report = {'version': VERSION, 'formal_result': spec.get('formal_experiment') is True,
        'prospective_analysis_plan': prospective, 'delivery_criteria_version': DELIVERY_VERSION,
        'analysis_plan_sha256': sources[str(plan_path)], 'source_specification_sha256': sources[str(batch/'specification.json')],
        'source_files_unchanged': True, 'model_calls': 0, 'native_mutations': 0,
        'pooled': full, 'by_disease': stratified, 'check_counts': checks, 'prescription_scope': rx_results,
        'execution_status_counts': {cid: dict(v) for cid, v in statuses.items()},
        'primary_contrasts':plan['contrasts'],'secondary_contrasts':secondary,
        'interpretation': ['Delivery checks do not establish unrestricted clinical accuracy.',
            'All planned units remain in the denominator; explicitly unstarted tasks have no delivered artifact or role activity.',
            'Unknown work or qualification remains missing, never zero.',
            'Staff minutes are simulated activity durations; API latency and human work time are separate.',
            'All configuration contrasts use the same within-disease patient bootstrap draws. Undefined draws are retained.']}
    output.mkdir(parents=True, mode=0o700)
    for name, value in [('delivery_analysis.json', report),
                        ('private_source_audit.json', {'source_file_hashes': sources, 'units': units,
                                                       'analyzer_sha256': analyzer_hash})]:
        path = output/name
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n')
        path.chmod(0o600)
    return {'planned_source_patients': full['distinct_source_patients'],
            'delivery_configurations': len(configs), 'paired_contrasts': len(contrasts),
            'formal_result': report['formal_result'], 'output': str(output)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ('batch', 'case-manifest', 'analysis-plan', 'output'):
        parser.add_argument('--'+flag, type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(analyze_batch(args.batch, args.case_manifest, args.analysis_plan, args.output)))
