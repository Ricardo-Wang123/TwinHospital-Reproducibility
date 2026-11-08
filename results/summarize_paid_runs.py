"""Summarize paid encounters only, after matching the archived native audit."""
import argparse
from collections import Counter, defaultdict
from hashlib import sha256
import json
import math
from pathlib import Path
from statistics import fmean, median, quantiles

from sandbox.experiments.linked.clinical_faults import registry
from sandbox.experiments.linked.contracts import digest
from sandbox.experiments.linked.prescription_analysis import measures, reference_from_stage
from sandbox.experiments.linked.programmatic_delivery import (
    COLLECTION_ACTIVITIES, STAFF_ROLES, VERIFICATION_ACTIVITIES, aggregate, evaluate,
)
from sandbox.experiments.linked.safe_context_adapter import VisibleDocumentedCandidatePPRS


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
    seconds_by_activity = Counter()
    scoped = [task_activities(run, task) for run in runs]
    for activity in (a for group in scoped for a in group):
        seconds_by_role[activity['role']] += activity['active_seconds']
        seconds_by_activity[activity['activity_type']] += activity['active_seconds']
    collection = sum(seconds_by_activity[key] for key in COLLECTION_ACTIVITIES)
    verification = sum(seconds_by_activity[key] for key in VERIFICATION_ACTIVITIES)
    row = {
        'mean_modeled_minutes': sum(seconds_by_role.values()) / (60 * qualified) if qualified else None,
        'modeled_minutes_by_role_per_delivered': {role: seconds / (60 * qualified)
                                                  for role, seconds in seconds_by_role.items()} if qualified else {},
        'effort_components_seconds': {
            'collection_and_drafting': collection,
            'verification_and_rework': verification,
            'other': sum(seconds_by_role.values()) - collection - verification,
        },
    }
    if task == 'preconsult':
        types = [Counter(a['activity_type'] for a in group) for group in scoped]
        row.update(physician_supplementation_actions=sum(x['handoff_supplementation'] for x in types),
                   physician_correction_actions=sum(x['handoff_correction'] for x in types),
                   first_pass_delivered=sum(evaluate(run, task)['qualifies'] is True
                       and not (x['handoff_supplementation'] or x['handoff_correction'])
                       for run, x in zip(runs, types, strict=True)))
    else:
        row.update(mean_note_turnaround_seconds=fmean(note_turnaround_seconds(run) for run in runs) if runs else None,
                   note_rework_seconds=sum(a['active_seconds'] for group in scoped for a in group
                                           if a['activity_type'] == 'note_rework'))
        first = [next(review for review in run['artifacts']['note_reviews'] if review['is_final_prefix'])
                 for run in runs]
        row['first_final_draft_accepted'] = sum(review['decision'] == 'accept' for review in first)
        interim_before_close = 0
        for run in runs:
            events = run['events']
            closed = next(event['sequence'] for event in events if event['event'] == 'consultation_completed')
            if any(event['event'] == 'realtime_note_available_during_consultation'
                   and event['sequence'] < closed for event in events):
                interim_before_close += 1
        row['interim_notes_before_close'] = interim_before_close
    return row


def read(path):
    return json.loads(path.read_bytes())


def prescription_references(runs, spec, manifest, clinical_catalogue=None):
    rules = registry()
    if digest(rules) != spec['rules']['registry_hash']:
        raise ValueError('Reference rules must match the batch registry.')
    catalogue_hash = spec['rules'].get('source_catalogue_file_sha256')
    catalogue = None
    if catalogue_hash:
        if clinical_catalogue is None:
            raise ValueError('Documented rules require the frozen --clinical-catalogue.')
        raw_catalogue = clinical_catalogue.read_bytes()
        if sha256(raw_catalogue).hexdigest() != catalogue_hash:
            raise ValueError('Reference product catalogue differs from the frozen batch.')
        catalogue = json.loads(raw_catalogue)['products']
    expected = {case['case_id']: case['file_sha256'] for case in spec['cases']}
    entries = read(manifest)['cases']
    if len(entries) != len(expected) or {case['case_id'] for case in entries} != set(expected):
        raise ValueError('Manifest does not contain the frozen source cases.')
    formularies = {}
    projections = {}
    for entry in entries:
        raw = (manifest.parent / entry['path']).read_bytes()
        case_id = entry['case_id']
        if sha256(raw).hexdigest() != expected[case_id]:
            raise ValueError('Source case differs from the frozen batch.')
        case = json.loads(raw)
        if case['case_id'] != case_id:
            raise ValueError('Source case identity differs from its manifest row.')
        formularies[case_id] = case['formulary']
        if catalogue is not None:
            projections[case_id] = VisibleDocumentedCandidatePPRS(case['formulary'], catalogue)
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
        case_id = run['case_id']
        if case_id in projections:
            projected = projections[case_id].prepare_review_inputs(stage['prescription'], stage['clinical_context'])
            archived_hash = first['rule_review'].get('rule_input_projection_hash')
            if archived_hash is not None and digest(projected['prescription']) != archived_hash:
                raise ValueError('Literal prescription projection differs from the recorded input.')
            stage = {'prescription': projected['prescription'], 'clinical_context': projected['shared_context']}
        references[run['run_id']] = reference_from_stage(stage, rule_ids, formularies[case_id])
    return references


def screening_counts(runs):
    sources = ('rule_review', 'ai_review', 'pharmacist_review')

    def alert_from(review, source):
        return (review.get(source) or {}).get('decision') in {'flag', 'reject'}

    def alerts(review):
        return any(alert_from(review, source) for source in sources)

    def reviewed(review):
        return bool(review['pharmacist_review'] or review['senior_review'])

    histories = [[v['review'] for v in r['artifacts']['prescription_versions']] for r in runs]
    originals = [history[0] for history in histories]
    followups = [review for history in histories for review in history[1:]]
    finals = [history[-1] for history in histories]
    first_alerts = [r for r in originals if alerts(r)]
    followup_alerts = [r for r in followups if alerts(r)]
    alerts_by_encounter = [[r for r in history if alerts(r)] for history in histories]
    alert_events = [r for history in alerts_by_encounter for r in history]
    evidence_flags = [r for r in finals if r['rule_review']['decision'] == 'flag' and any(
        f['status'] == 'indeterminate' for f in r['rule_review'].get('documented_rule_findings', []))]
    return {
        'original_rule_alerts': sum(alert_from(r, 'rule_review') for r in originals),
        'original_ai_alerts': sum(alert_from(r, 'ai_review') for r in originals),
        'original_pharmacist_alerts': sum(alert_from(r, 'pharmacist_review') for r in originals),
        'original_alerts': len(first_alerts),
        'original_alerts_reviewed': sum(reviewed(r) for r in first_alerts),
        'followup_review_rounds': len(followups),
        'followup_rule_alerts': sum(alert_from(r, 'rule_review') for r in followups),
        'followup_ai_alerts': sum(alert_from(r, 'ai_review') for r in followups),
        'followup_pharmacist_alerts': sum(alert_from(r, 'pharmacist_review') for r in followups),
        'followup_alerts': len(followup_alerts),
        'followup_alerts_reviewed': sum(reviewed(r) for r in followup_alerts),
        'followup_rule_rounds_with_repeated_flag': sum(
            alert_from(current, 'rule_review') and bool(
                set(current['rule_review']['flags']).intersection(previous['rule_review']['flags']))
            for history in histories for previous, current in zip(history, history[1:])
        ),
        'encounters_with_alerts': sum(bool(history) for history in alerts_by_encounter),
        'alerting_review_rounds': len(alert_events),
        'alerting_rounds_reviewed': sum(reviewed(r) for r in alert_events),
        'final_rule_evidence_alerts': len(evidence_flags),
        'final_evidence_alerts_reviewed': sum(reviewed(r) for r in evidence_flags),
        'final_evidence_alerts_approved': sum(r['decision'] == 'approve' for r in evidence_flags),
    }


def linked_progression(runs):
    """Retain the distinction between review approval and a signable order."""
    profiles = Counter(tuple(run['conditions'][task]['workflow']
                             for task in ('preconsult', 'prescription', 'documentation')) for run in runs)
    progression = Counter()
    for run in runs:
        versions = run['artifacts']['prescription_versions']
        original, final = versions[0], versions[-1]
        if final['review']['decision'] != 'approve' or final['native_write_attempted'] is not True:
            raise ValueError('A verified paid encounter lacks an approved, persisted final order.')
        if len(versions) == 1:
            progression['first_candidate_signed'] += 1
            continue
        if original['review']['decision'] != 'approve':
            progression['first_review_nonapproval'] += 1
        elif original['native_write_attempted'] is False and original['business_validation']['status'] in {
            'native_encoding_unavailable', 'unmapped_product',
        }:
            progression['approved_but_unencodable'] += 1
        else:
            raise ValueError('Follow-up after approval has no recorded reason in the linked protocol.')
    return {
        'configuration_cells': len(profiles),
        'encounters_per_cell_min': min(profiles.values()),
        'encounters_per_cell_max': max(profiles.values()),
        'first_candidate_signed': progression['first_candidate_signed'],
        'first_review_nonapproval': progression['first_review_nonapproval'],
        'approved_but_unencodable': progression['approved_but_unencodable'],
    }


def matched_stage_effort(runs, task, baseline):
    """Match delivery-qualified traces by history and the other two configurations."""
    blocks = defaultdict(lambda: defaultdict(list))
    variants = set()
    for run in runs:
        conditions = run['conditions']
        workflow = conditions[task]['workflow']
        variants.add(workflow)
        if evaluate(run, task)['qualifies'] is not True:
            continue
        other = tuple((name, conditions[name]['workflow']) for name in sorted(conditions) if name != task)
        staff_seconds = sum(a['active_seconds'] for a in task_activities(run, task))
        blocks[run['case_id'], other][workflow].append(staff_seconds)

    result = {}
    for assisted in sorted(variants - {baseline}):
        by_history = defaultdict(list)
        for (case_id, _), observed in blocks.items():
            if baseline in observed and assisted in observed:
                delta = fmean(observed[assisted]) - fmean(observed[baseline])
                by_history[case_id].append(delta / 60)
        history_means = [fmean(deltas) for deltas in by_history.values()]
        result[assisted] = {
            'matched_blocks': sum(map(len, by_history.values())),
            'source_histories': len(by_history),
            'mean_difference_minutes': fmean(history_means) if history_means else None,
            'min_history_difference_minutes': min(history_means, default=None),
            'max_history_difference_minutes': max(history_means, default=None),
        }
    return result


def summarize(batch, audit_path, case_manifest=None, clinical_catalogue=None):
    batch = batch.resolve()
    summary = read(batch / 'batch_summary.json')
    specification_bytes = (batch / 'specification.json').read_bytes()
    spec = json.loads(specification_bytes)
    audit = read(audit_path)
    audit = audit.get('audit', audit)
    specification_hash = audit.get('source_specification_sha256')
    if specification_hash is not None and specification_hash != sha256(specification_bytes).hexdigest():
        raise ValueError('The native audit belongs to a different batch specification.')
    audited = audit['runs']
    paid = [row for row in audited if row['verified_paid'] is True
            and row['formal']['correct_completion'] is True]
    expected_hashes = {r['source_run_file_sha256'] for r in paid}
    if len(paid) != len(expected_hashes):
        raise ValueError('The native audit repeats a paid trace digest.')
    hashes, runs = set(), []
    for case in summary['cases']:
        for item in case['attempts'].values():
            if item['status'] != 'completed':
                continue
            path = (batch / item['result_path']).resolve()
            if not path.is_relative_to(batch):
                raise ValueError('Run archive path is outside the specified batch.')
            raw = path.read_bytes()
            run_hash = sha256(raw).hexdigest()
            if run_hash in hashes or run_hash not in expected_hashes:
                raise ValueError('Paid run index does not match the archived native audit.')
            hashes.add(run_hash)
            runs.append(json.loads(raw))
    if not runs or hashes != expected_hashes:
        raise ValueError('Paid run index does not match the archived native audit.')
    if len({r['run_id'] for r in runs}) != len(runs):
        raise ValueError('Duplicate run identity.')
    if any(r['evaluation_unit'] != 'complete_encounter' for r in runs):
        raise ValueError('Non-encounter run present.')
    for run in runs:
        for a in run['activities']:
            if (not math.isfinite(a['active_seconds']) or a['active_seconds'] < 0
                    or a['active_seconds'] != spec['modeled_activity_seconds'][a['activity_type']]):
                raise ValueError('Activity does not use the frozen weights.')
    source_groups = {r['case_id']: r['group'] for r in runs}
    if any(source_groups[r['case_id']] != r['group'] for r in runs):
        raise ValueError('A source history appears in more than one sampling stratum.')
    references = prescription_references(runs, spec, case_manifest, clinical_catalogue) if case_manifest else None
    disease_counts = dict(Counter(r['group'] for r in runs))
    timings = [r['formal']['queue_to_target_payment_seconds'] for r in paid]
    if not all(type(t) in (int, float) and math.isfinite(t) and t >= 0 for t in timings):
        raise ValueError('Missing or invalid audited paid run time.')
    result = {'scope': 'descriptive analysis of verified paid linked encounters only',
              'paid_cohort_sha256': sha256('\n'.join(sorted(hashes)).encode()).hexdigest(),
              'source_cases': len(source_groups), 'verified_paid': len(runs),
              'source_cases_by_group': dict(Counter(source_groups.values())),
              'groups': disease_counts,
              'linked_progression': linked_progression(runs),
              'matched_stage_effort': {
                  'preconsult': matched_stage_effort(runs, 'preconsult', 'nurse_only'),
                  'documentation': matched_stage_effort(runs, 'documentation', 'manual_documentation'),
              },
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
                row['screening'] = screening_counts(sub)
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
    parser.add_argument('--clinical-catalogue', type=Path, help='Frozen product catalogue required for documented rules.')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = summarize(args.batch, args.audit, args.case_manifest, args.clinical_catalogue)
    args.output.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(f"Wrote aggregates from {result['verified_paid']} verified paid runs.")
