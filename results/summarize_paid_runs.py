"""Summarize paid encounters only, after matching the archived native audit."""
import argparse
from collections import Counter
from hashlib import sha256
import json
import math
from pathlib import Path
from statistics import median, quantiles

from sandbox.experiments.linked.programmatic_delivery import aggregate, evaluate


def read(path):
    return json.loads(path.read_bytes())


def summarize(batch, audit_path):
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
    disease_counts = dict(Counter(r['group'] for r in runs))
    timings = [r['formal']['queue_to_target_payment_seconds'] for r in paid]
    if not all(type(t) in (int, float) and math.isfinite(t) and t >= 0 for t in timings):
        raise ValueError('Missing or invalid audited paid run time.')
    result = {'scope': 'descriptive analysis of verified paid linked encounters only',
              'source_cases': case_count, 'verified_paid': len(runs),
              'groups': disease_counts, 'task': {}, 'full_workflow': {
                  'correct_completion': len(paid),
                  'observed_unsafe_execution': sum(r['formal']['unsafe_execution'] is True for r in paid),
                  'unsafe_execution_unknown': sum(r['formal']['unsafe_execution'] is None for r in paid),
                  'absence_within_declared_scope_verified': sum(
                      r['formal']['absence_of_prohibited_transient_effects'] is True for r in paid),
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
            else:
                delivery = aggregate(sub, task, planned=len(sub))
                effort = delivery['effort_per_qualified_task_seconds']
                row.update(qualified=delivery['qualified_tasks'], unknown=delivery['unknown_qualification'],
                           failed=delivery['failed_delivery_checks'],
                           mean_modeled_minutes=None if effort is None else effort / 60,
                           failed_checks=dict(Counter(k for r in sub for k, v in evaluate(r, task)['checks'].items()
                                                      if v is not True)))
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
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = summarize(args.batch, args.audit)
    args.output.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(f"Wrote aggregates from {result['verified_paid']} verified paid runs.")
