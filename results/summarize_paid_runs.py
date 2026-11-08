"""Summarize paid encounters only, after matching the archived native audit."""
import argparse
from collections import Counter
from hashlib import sha256
import json
import math
from pathlib import Path
from statistics import fmean, median, quantiles

from sandbox.experiments.linked.clinical_faults import registry
from sandbox.experiments.linked.contracts import digest
from sandbox.experiments.linked.prescription_analysis import measures, reference_from_stage
from sandbox.experiments.linked.programmatic_delivery import STAFF_ROLES, aggregate, evaluate


HANDOFF_ACTIVITY = {'information_collection', 'summary_documentation', 'summary_review',
                    'handoff_verification', 'handoff_supplementation', 'handoff_correction'}
NOTE_ACTIVITY = {'documentation', 'note_review', 'note_rework', 'attestation'}


def task_activities(run, task):
    activities = run['activities']
    if task == 'documentation':
        return [a for a in activities if a['role'] in STAFF_ROLES and a['activity_type'] in NOTE_ACTIVITY]
    if task != 'preconsult':
        raise ValueError(f'Unexpected effort task: {task}')
    handoff = [a for a in activities if a['role'] in {'nurse', 'doctor'} and a['activity_type'] in HANDOFF_ACTIVITY]
    lookups = [a for a in activities if a['activity_type'] == 'history_lookup']
    phases = [e['phase'] for e in run['events'] if e['event'] == 'history_query_requested']
    if len(lookups) != len(phases):
        raise ValueError('History lookup activities do not align with logged query phases.')
    handoff.extend(a for a, phase in zip(lookups, phases, strict=True)
                   if a['role'] == 'doctor' and phase == 'doctor_handoff_verification')
    return handoff


def note_turnaround_seconds(run):
    start, = [e for e in run['events'] if e['event'] == 'consultation_completed']
    end, = [e for e in run['events'] if e['event'] == 'business_operation' and e.get('operation') == 'note.attest'
            and e['response'].get('readback_verified') is True and e['response'].get('native_effect') is True]
    seconds = end['elapsed_seconds'] - start['elapsed_seconds']
    if start['clock_id'] != end['clock_id'] or not math.isfinite(seconds) or seconds < 0:
        raise ValueError('Note turnaround must use the same encounter clock.')
    return seconds


def stage_effort(runs, task, qualified):
    seconds_by_role = Counter()
    scoped = [task_activities(run, task) for run in runs]
    for activity in (a for group in scoped for a in group):
        seconds_by_role[activity['role']] += activity['active_seconds']
    row = {
        'mean_modeled_minutes': sum(seconds_by_role.values()) / (60 * qualified) if qualified else None,
        'modeled_minutes_by_role_per_delivered': {role: seconds / (60 * qualified)
                                                  for role, seconds in seconds_by_role.items()} if qualified else {},
    }
    if task == 'preconsult':
        types = [Counter(a['activity_type'] for a in group) for group in scoped]
        row.update(physician_supplementation_actions=sum(x['handoff_supplementation'] for x in types),
                   physician_correction_actions=sum(x['handoff_correction'] for x in types),
                   first_pass_delivered=sum(evaluate(run, task)['qualifies'] is True
                       and not (x['handoff_supplementation'] or x['handoff_correction'])
                       for run, x in zip(runs, types, strict=True)))
    else:
        row.update(mean_note_turnaround_seconds=fmean(note_turnaround_seconds(run) for run in runs),
                   note_rework_seconds=sum(a['active_seconds'] for group in scoped for a in group
                                           if a['activity_type'] == 'note_rework'))
        first = [next(review for review in run['artifacts']['note_reviews'] if review['is_final_prefix'])
                 for run in runs]
        row['first_final_draft_accepted'] = sum(review['decision'] == 'accept' for review in first)
    return row


def read(path):
    return json.loads(path.read_bytes())


def prescription_references(runs, spec, manifest):
    rules = registry()
    if digest(rules) != spec['rules']['registry_hash']:
        raise ValueError('Reference rules must match the batch registry.')
    expected = {case['case_id']: case['file_sha256'] for case in spec['cases']}
    entries = read(manifest)['cases']
    if len(entries) != len(expected) or {case['case_id'] for case in entries} != set(expected):
        raise ValueError('Manifest does not contain the frozen source cases.')
    formularies = {}
    for entry in entries:
        raw = Path(entry['path']).read_bytes()
        case_id = entry['case_id']
        if sha256(raw).hexdigest() != expected[case_id]:
            raise ValueError('Source case differs from the frozen batch.')
        case = json.loads(raw)
        if case['case_id'] != case_id:
            raise ValueError('Source case identity differs from its manifest row.')
        formularies[case_id] = case['formulary']
    rule_ids = [row['rule_id'] for row in rules['rules']]
    references = {}
    for run in runs:
        stage = run['stage_inputs']['prescription']
        artifacts = run['artifacts']
        first = artifacts['original_prescription_review']
        event, = [e for e in run['events'] if e['event'] == 'prescription_reviewed' and e.get('revision') == 0]
        if (stage['prescription'] != artifacts['original_prescription']
                or first['prescription_hash'] != digest(stage['prescription'])
                or any(event.get(k) != value for k, value in first.items())):
            raise ValueError('Original prescription review does not match its recorded input and event.')
        references[run['run_id']] = reference_from_stage(stage, rule_ids, formularies[run['case_id']])
    return references


def summarize(batch, audit_path, case_manifest=None):
    batch = batch.resolve()
    summary, spec = (read(batch / name) for name in ('batch_summary.json', 'specification.json'))
    audit = read(audit_path)
    audited = audit.get('audit', audit)['runs']
    paid = [row for row in audited if row['verified_paid'] is True
            and row['formal']['correct_completion'] is True]
    raw_runs = []
    for case in summary['cases']:
        for item in case['attempts'].values():
            if item['status'] != 'completed':
                continue
            path = Path(item['result_path']).resolve()
            if not path.is_relative_to(batch):
                raise ValueError('Run archive path is outside the specified batch.')
            raw_runs.append(path.read_bytes())
    hashes = {sha256(raw).hexdigest() for raw in raw_runs}
    if (not raw_runs or len(hashes) != len(raw_runs) or len(paid) != len(raw_runs)
            or hashes != {r['source_run_file_sha256'] for r in paid}):
        raise ValueError('Paid run index does not match the archived native audit.')
    runs = [json.loads(raw) for raw in raw_runs]
    if len({r['run_id'] for r in runs}) != len(runs):
        raise ValueError('Duplicate run identity.')
    if any(r['evaluation_unit'] != 'complete_encounter' for r in runs):
        raise ValueError('Non-encounter run present.')
    for run in runs:
        for a in run['activities']:
            if (not math.isfinite(a['active_seconds']) or a['active_seconds'] < 0
                    or a['active_seconds'] != spec['modeled_activity_seconds'][a['activity_type']]):
                raise ValueError('Activity does not use the frozen weights.')
    case_count = len({r['case_id'] for r in runs})
    references = prescription_references(runs, spec, case_manifest) if case_manifest else None
    disease_counts = dict(Counter(r['group'] for r in runs))
    timings = [r['formal']['queue_to_target_payment_seconds'] for r in paid]
    if not all(type(t) in (int, float) and math.isfinite(t) and t >= 0 for t in timings):
        raise ValueError('Missing or invalid audited paid run time.')
    result = {'scope': 'descriptive analysis of verified paid linked encounters only',
              'source_cases': case_count, 'verified_paid': len(runs),
              'groups': disease_counts,
              'effort_scope': 'task-specific staff activities; encounter-wide effort retained separately',
              'task': {}, 'full_workflow': {
                  'correct_completion': len(paid),
                  'observed_unsafe_execution': sum(r['formal']['unsafe_execution'] is True for r in paid),
                  'unsafe_execution_unknown': sum(r['formal']['unsafe_execution'] is None for r in paid),
                  'absence_within_declared_scope_verified': sum(
                      r['formal']['absence_of_prohibited_transient_effects'] is True for r in paid),
                  'queue_to_payment_seconds_mean': fmean(timings),
                  'queue_to_payment_seconds_median': median(timings),
                  'queue_to_payment_seconds_p95': quantiles(timings, n=20, method='inclusive')[-1] if len(timings)>1 else None}}
    for task in ('preconsult', 'documentation', 'prescription'):
        rows = {}
        for workflow in sorted({r['conditions'][task]['workflow'] for r in runs}):
            sub = [r for r in runs if r['conditions'][task]['workflow'] == workflow]
            row = {'observed': len(sub)}
            if task == 'prescription':
                decisions = Counter(r['artifacts']['original_prescription_review']['decision'] for r in sub)
                if decisions.keys() - {'approve', 'revise', 'defer', 'reject', 'block', 'request_revision', 'escalate'}:
                    raise ValueError('Unexpected original prescription decision category.')
                row['first_decision'] = dict(decisions)
                if references is not None:
                    counts = Counter((r['artifacts']['original_prescription_review']['decision'],
                                      references[r['run_id']]['status'],
                                      references[r['run_id']]['high_risk_predicate_confirmed']) for r in sub)
                    row['rule_reference_counts'] = dict(Counter(references[r['run_id']]['status'] for r in sub))
                    row['confirmed_high_risk'] = sum(references[r['run_id']]['high_risk_predicate_confirmed'] for r in sub)
                    row['rule_scoped_metrics'] = measures(counts)
            else:
                delivery = aggregate(sub, task, planned=len(sub))
                effort = delivery['effort_per_qualified_task_seconds']
                row.update(qualified=delivery['qualified_tasks'], unknown=delivery['unknown_qualification'],
                           failed=delivery['failed_delivery_checks'],
                           mean_encounter_modeled_minutes=None if effort is None else effort / 60,
                           failed_checks=dict(Counter(k for r in sub for k, v in evaluate(r, task)['checks'].items()
                                                      if v is not True)))
                row.update(stage_effort(sub, task, delivery['qualified_tasks']))
                if task == 'documentation':
                    row.update(saved_note_versions=sum(len(r['artifacts']['note_versions']) for r in sub),
                               saved_physician_revisions=sum(v.get('reason') == 'physician_revision'
                                   for r in sub for v in r['artifacts']['note_versions']))
            rows[workflow] = row
        result['task'][task] = rows
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--batch', type=Path, required=True)
    parser.add_argument('--audit', type=Path, required=True)
    parser.add_argument('--case-manifest', type=Path, help='Frozen source manifest for rule-scoped prescription analysis.')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = summarize(args.batch, args.audit, args.case_manifest)
    args.output.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(f"Wrote aggregates from {result['verified_paid']} verified paid runs.")
