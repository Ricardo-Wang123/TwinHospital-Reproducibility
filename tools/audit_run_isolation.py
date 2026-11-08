"""Read-only audit of local run/HIS identities and declared agent memory policy.

Only aggregate counts are exported. This does not audit a provider's retention,
training, caching, or internal session implementation.
"""
from collections import Counter
from datetime import datetime, timezone
import argparse
import json
from pathlib import Path

EXPECTED_MEMORY = 'explicit_visible_state_per_turn'
FIELDS = ('run_id', 'patient_id', 'encounter_id')


def snapshot(batch):
    batch = Path(batch)
    spec = json.loads((batch/'specification.json').read_text())
    identities = {key:Counter() for key in FIELDS}
    missing = Counter()
    native_states = 0
    for path in sorted(batch.glob('*/*/native/native_state.json')):
        state = json.loads(path.read_text())
        for binding in state.get('bindings', {}).values():
            native_states += 1
            for key in FIELDS:
                value = binding.get(key)
                if value is None or str(value) == '':
                    missing[key] += 1
                else:
                    identities[key][str(value)] += 1
    calls = Counter()
    partial_lines = 0
    for path in sorted(batch.glob('*/*/runs/*/calls.jsonl')):
        lines = path.read_text().splitlines()
        for i, line in enumerate(lines):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                if i != len(lines)-1:
                    raise ValueError('Non-terminal malformed call log; inspect private evidence')
                partial_lines += 1
                continue
            calls[record.get('memory_policy') or 'not_recorded'] += 1
    capture_path = batch/'native_observation/observation_session.json'
    capture = json.loads(capture_path.read_text()) if capture_path.exists() else {}
    public = {
        'batch':batch.name, 'native_bindings_observed':native_states,
        'identities':{key:{'unique':len(values), 'duplicate_occurrences':sum(n-1 for n in values.values()),
                           'missing':missing[key]} for key,values in identities.items()},
        'memory_policy_counts':dict(calls), 'incomplete_trailing_log_lines':partial_lines,
        'anchor_reuse_declared':spec.get('anchor_reuse') is not None,
        'specification_memory_policy':spec.get('memory_policy'),
        'capture_status':capture.get('status', 'not_present'),
    }
    return public, identities


def audit(batches):
    batches = [Path(p).resolve() for p in batches]
    if len(set(batches)) != len(batches):
        raise ValueError('Comparing a batch with itself is not a restart audit')
    snapshots = [snapshot(p) for p in batches]
    overlaps = []
    for i, (left, ids_left) in enumerate(snapshots):
        for right, ids_right in snapshots[i+1:]:
            overlaps.append({'left_batch':left['batch'], 'right_batch':right['batch'],
                'overlap_counts':{key:len(set(ids_left[key]) & set(ids_right[key])) for key in FIELDS}})
    public = [p for p,_ in snapshots]
    identity_ok = all(p['native_bindings_observed'] > 0 and all(
        v['missing'] == 0 and v['duplicate_occurrences'] == 0 for v in p['identities'].values()) for p in public)
    identity_ok = identity_ok and all(not any(row['overlap_counts'].values()) for row in overlaps)
    policy_ok = all(p['memory_policy_counts'] and set(p['memory_policy_counts']) == {EXPECTED_MEMORY}
                    and p['specification_memory_policy'] == EXPECTED_MEMORY for p in public)
    return {'created_at':datetime.now(timezone.utc).isoformat(), 'batches':public,
        'cross_batch_comparisons':overlaps,
        'observed_local_identity_isolation_passed':identity_ok,
        'declared_call_memory_policy_passed':policy_ok,
        'previous_anchor_not_reused':all(not p['anchor_reuse_declared'] for p in public),
        'running_snapshot':any(p['capture_status'] == 'active' for p in public),
        'scope':'Observed local identity separation and logged memory policy only; no provider storage/training claim',
        'fixed_case_history_deliberately_retained':True,
        'within_run_visible_dialogue_deliberately_retained':True,
        'source_input_and_native_state_mutations':0}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--batch', action='append', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    report = audit(args.batch)
    with args.output.open('x') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print(json.dumps({k:report[k] for k in ('observed_local_identity_isolation_passed',
        'declared_call_memory_policy_passed','previous_anchor_not_reused','running_snapshot')}))
