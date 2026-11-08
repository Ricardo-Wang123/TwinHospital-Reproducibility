"""Render the numerical layer of the linked-encounter manuscript."""
from __future__ import annotations

import argparse
from collections import Counter
from hashlib import sha256
from itertools import product
import json
import math
from pathlib import Path
import re


STRATA = (
    ('type2_diabetes', 'Type 2 diabetes', 'Endocrine'),
    ('heart_failure', 'Heart failure', 'Circulatory'),
    ('copd', 'COPD', 'Respiratory'),
    ('pneumonia', 'Pneumonia', 'Respiratory'),
    ('uti', 'Urinary tract infection', 'Genitourinary'),
)
CONFIGURATIONS = {
    'prescription': (('pprs_baseline', 'Rule baseline'), ('sequential', 'Sequential'),
                     ('parallel', 'Parallel')),
    'preconsult': (('nurse_only', 'Nurse agent'), ('ai_only', 'AI only'),
                   ('ai_nurse_review', 'AI + nurse')),
    'documentation': (('manual_documentation', 'Physician agent'),
                      ('realtime', 'During consultation'), ('postvisit', 'Post-consultation')),
}
OPERATIONS = (
    ('patient_record_mismatch', 'Patient identity'),
    ('missing_encounter_link', 'Encounter linkage'),
    ('invalid_note_state', 'Note lifecycle'),
    ('unauthorized_signing', 'Signing authority'),
    ('duplicate_transaction', 'Transaction uniqueness'),
)
SAFE_FAULT_OUTCOMES = {
    'gateway_prevention', 'native_rejection_no_effect_observed', 'safe_idempotent_replay',
}
DASH = r'\textemdash{}'


def read_json(path):
    return json.loads(path.read_bytes())


def ratio(numerator, denominator):
    if type(numerator) is not int or type(denominator) is not int or not 0 <= numerator <= denominator:
        raise ValueError(f'Invalid count ratio {numerator!r}/{denominator!r}')
    return f'{numerator}/{denominator}' if denominator else DASH


def number(value, digits=2, scale=1):
    if value is None:
        return DASH
    if type(value) not in (float, int) or not math.isfinite(value) or value < 0:
        raise ValueError(f'Invalid nonnegative measurement {value!r}')
    return f'{value * scale:.{digits}f}'


def command(name, value):
    return r'\newcommand{\TH' + name + '}{' + str(value) + '}\n'


def table(name, rows):
    return command(name, '%\n' + ''.join(' & '.join(map(str, row)) + r' \\' + '\n' for row in rows))


def paid_cohort(audit):
    audit = audit.get('audit', audit)
    return [r for r in audit['runs'] if r['verified_paid'] is True
            and r['formal']['correct_completion'] is True]


def cohort_fingerprint(paid):
    hashes = [r['source_run_file_sha256'] for r in paid]
    if (len(set(hashes)) != len(hashes)
            or any(not isinstance(h, str) or not re.fullmatch('[0-9a-f]{64}', h) for h in hashes)):
        raise ValueError('Invalid or duplicate paid-run file hashes in the workflow audit')
    return sha256('\n'.join(sorted(hashes)).encode()).hexdigest()


def validate_cohort(metrics, paid):
    groups = dict(Counter(r['group'] for r in paid))
    if (len(paid) != metrics['verified_paid'] or not paid or groups != metrics['groups']
            or metrics['paid_cohort_sha256'] != cohort_fingerprint(paid)):
        raise ValueError('The task summary and native paid cohort disagree')
    if set(groups) - {key for key, _, _ in STRATA}:
        raise ValueError('Unexpected disease stratum in the linked cohort')
    by_source = metrics['source_cases_by_group']
    if (set(by_source) != set(groups) or sum(by_source.values()) != metrics['source_cases']
            or any(type(count) is not int or not 0 < count <= groups[key]
                   for key, count in by_source.items())):
        raise ValueError('Source histories do not partition by reported disease stratum')
    expected = {
        'correct_completion': len(paid),
        'observed_unsafe_execution': sum(r['formal']['unsafe_execution'] is True for r in paid),
        'unsafe_execution_unknown': sum(r['formal']['unsafe_execution'] is None for r in paid),
        'absence_within_declared_scope_verified': sum(
            r['formal']['absence_of_prohibited_transient_effects'] is True for r in paid),
    }
    if any(metrics['full_workflow'][key] != count for key, count in expected.items()):
        raise ValueError('The workflow summary disagrees with verified native outcomes')
    for task, configurations in CONFIGURATIONS.items():
        rows = metrics['task'][task]
        if set(rows) != {key for key, _ in configurations} or sum(r['observed'] for r in rows.values()) != len(paid):
            raise ValueError(f'{task} configuration counts do not partition the paid cohort')
    for task in ('preconsult', 'documentation'):
        if any(row['qualified'] is None for row in metrics['task'][task].values()):
            raise ValueError(f'{task} delivery evidence is indeterminate; resolve it before rendering the paper')


def human_review_statement(review, paid):
    if review['cohort_sha256'] != cohort_fingerprint(paid):
        raise ValueError('The human review summary is bound to a different paid cohort')
    reviewed, total = review['reviewed'], len(paid)
    ratio(reviewed, total)
    if reviewed == 0:
        return ''
    statement = ('After simulation, human reviewers on the study team assessed the clinical appropriateness '
                 f'of the final prescription in {reviewed} of the {total} completed encounters.')
    outcomes = review.get('outcomes')
    if outcomes is not None:
        fields = ('appropriate', 'inappropriate', 'indeterminate')
        if (set(outcomes) != set(fields) or any(type(outcomes[k]) is not int or outcomes[k] < 0 for k in fields)
                or sum(outcomes.values()) != reviewed):
            raise ValueError('Human review outcomes do not partition the reviewed prescriptions')
        statement += (' Reviewers judged '
                      f"{outcomes['appropriate']} appropriate, {outcomes['inappropriate']} inappropriate, "
                      f"and {outcomes['indeterminate']} indeterminate.")
    return statement


def probe_results(probes):
    by_operation = {key: {'single_fault': Counter(), 'compliant_control': Counter()}
                    for key, _ in OPERATIONS}
    observed = Counter()
    for cell in probes['by_cell']:
        counts = cell['counts']
        if any(type(n) is not int or n < 0 for n in counts.values()):
            raise ValueError('Invalid native probe outcome count')
        by_operation[cell['operation']][cell['condition']].update(counts)
        observed.update(counts)
    if (observed != Counter(probes['outcomes']) or sum(observed.values()) != probes['recorded_units']
            or probes['planned_units'] != probes['recorded_units']
            or probes['model_calls'] != 0 or probes['source_inputs_unchanged'] is not True):
        raise ValueError('The probe summary does not bind the complete recorded replay')
    rows = []
    fault_total = control_total = safe_total = allowed_total = 0
    for key, label in OPERATIONS:
        faults = by_operation[key]['single_fault']
        controls = by_operation[key]['compliant_control']
        f, c = sum(faults.values()), sum(controls.values())
        safe = sum(faults[o] for o in SAFE_FAULT_OUTCOMES)
        allowed = controls['control_allowed']
        fault_total += f
        control_total += c
        safe_total += safe
        allowed_total += allowed
        rows.append((label, faults['gateway_prevention'], faults['safe_idempotent_replay'],
                     ratio(safe, f), ratio(allowed, c)))
    statement = (f'In separate operation replays, {safe_total}/{fault_total} fault-bearing requests '
                 f'had a verified safe disposition and {allowed_total}/{control_total} valid controls executed.')
    return rows, statement


def effort_weight_rows(task, rows):
    """Compare each assisted configuration with its task baseline on a shared weight grid."""
    baseline, *alternatives = CONFIGURATIONS[task]
    reference = rows[baseline[0]]
    task_label = 'Pre-consultation' if task == 'preconsult' else 'Documentation'

    def effort(row, collection, verification):
        parts = row['effort_components_seconds']
        seconds = (collection * parts['collection_and_drafting']
                   + verification * parts['verification_and_rework'] + parts['other'])
        return seconds / (60 * row['qualified'])

    for key, label in alternatives:
        assisted = rows[key]
        if not assisted['qualified'] or not reference['qualified']:
            yield task_label, label, None, None, None
            continue
        shifts = [effort(assisted, c, v) - effort(reference, c, v)
                  for c, v in product((.5, 1., 2.), repeat=2)]
        baseline_shift = effort(assisted, 1., 1.) - effort(reference, 1., 1.)
        yield task_label, label, baseline_shift, min(shifts), max(shifts)


def render(metrics, audit, review, probes):
    paid = paid_cohort(audit)
    validate_cohort(metrics, paid)
    total = len(paid)
    workflow = metrics['full_workflow']
    rx = metrics['task']['prescription']
    pre = metrics['task']['preconsult']
    notes = metrics['task']['documentation']
    linked = metrics['linked_progression']
    total_cells = math.prod(len(configs) for configs in CONFIGURATIONS.values())
    windows = {round(row['window']['deadline_seconds'] - row['window']['clock_origin_seconds'], 6)
               for row in paid}
    if len(windows) != 1 or next(iter(windows)) <= 0:
        raise ValueError('The verified encounters must use a common positive observation window')
    window_minutes = next(iter(windows)) / 60

    decisions = Counter()
    labels = Counter()
    screens = Counter()
    rx_rows, alerts, followup_alerts, pre_rows, note_rows = [], [], [], [], []
    positives_reviewed = positives_rejected = false_rejects = negatives_reviewed = 0
    for key, label in CONFIGURATIONS['prescription']:
        row = rx[key]
        dec = Counter(row['first_decision'])
        ref = Counter(row['rule_reference_counts'])
        if (dec.keys() - {'approve', 'defer', 'reject'} or sum(dec.values()) != row['observed']
                or sum(ref.values()) != row['observed']):
            raise ValueError('The review decisions or reference labels do not partition original orders')
        decisions.update(dec)
        labels.update(ref)
        screen = row['screening']
        screens.update(screen)
        fpr, recall = (row['rule_scoped_metrics'][k] for k in ('fpr', 'recall'))
        false_rejects += fpr['numerator']
        negatives_reviewed += fpr['denominator']
        positives_rejected += recall['numerator']
        positives_reviewed += recall['denominator']
        rx_rows.append((label, row['observed'], dec['approve'], dec['defer'], dec['reject'],
                        ratio(dec['approve'], dec['approve'] + dec['reject']),
                        ratio(fpr['numerator'], fpr['denominator'])))
        for phase, rounds, target in [('original', row['observed'], alerts),
                                      ('followup', screen['followup_review_rounds'], followup_alerts)]:
            n_alert = screen[f'{phase}_alerts']
            ratio(n_alert, rounds)
            target.append((label, rounds, screen[f'{phase}_rule_alerts'],
                           DASH if key == 'pprs_baseline' else screen[f'{phase}_ai_alerts'],
                           screen[f'{phase}_pharmacist_alerts'], n_alert,
                           ratio(screen[f'{phase}_alerts_reviewed'], n_alert)))
    for phase, rounds, target in [('original', total, alerts),
                                  ('followup', screens['followup_review_rounds'], followup_alerts)]:
        n_alert = screens[f'{phase}_alerts']
        target.append((r'\textbf{Total}', rounds, screens[f'{phase}_rule_alerts'],
                       screens[f'{phase}_ai_alerts'], screens[f'{phase}_pharmacist_alerts'],
                       n_alert, ratio(screens[f'{phase}_alerts_reviewed'], n_alert)))
    if (linked['first_review_nonapproval'] != decisions['defer'] + decisions['reject']
            or linked['first_candidate_signed'] + linked['approved_but_unencodable'] != decisions['approve']
            or not 0 < linked['configuration_cells'] <= total_cells
            or not 0 < linked['encounters_per_cell_min'] <= linked['encounters_per_cell_max'] <= total):
        raise ValueError('Inconsistent linked progression or configuration counts')

    for key, label in CONFIGURATIONS['preconsult']:
        row = pre[key]
        role = row['modeled_minutes_by_role_per_delivered']
        pre_rows.append((label, ratio(row['qualified'], row['observed']),
                         number(role.get('nurse', 0) if row['qualified'] else None),
                         number(role.get('doctor', 0) if row['qualified'] else None),
                         number(row['mean_modeled_minutes']),
                         ratio(row['first_pass_delivered'], row['observed']),
                         row['physician_supplementation_actions']))
    for key, label in CONFIGURATIONS['documentation']:
        row = notes[key]
        note_rows.append((label, ratio(row['qualified'], row['observed']),
                          number(row['mean_modeled_minutes']), row['saved_physician_revisions'],
                          number(row['note_rework_seconds'] / row['observed'], 1) if row['observed'] else DASH,
                          ratio(row['first_final_draft_accepted'], row['observed']),
                          number(row['mean_note_turnaround_seconds'], 1)))

    positive_statement = (f'{positives_rejected}/{positives_reviewed} reference-positive orders with a resolved '
                          'first decision were rejected.' if positives_reviewed
                          else 'No reference-positive order had a resolved first decision.')
    unresolved = total - labels['scoped_nonviolation'] - labels['violation']
    fpr_statement = (f'False positives among resolved reference negatives numbered '
                     f'{ratio(false_rejects, negatives_reviewed)}.' if negatives_reviewed
                     else 'The false-positive denominator contains resolved reference negatives only.')
    ref_statement = (f'The rule reference labeled {labels["scoped_nonviolation"]} original orders as '
                     f'nonviolations and {labels["violation"]} as violations. The remaining {unresolved} '
                     'orders had no determinate label within the reference scope. ' + fpr_statement
                     + ' ' + positive_statement)
    alert_statement = f'At least one source alerted in {screens["original_alerts"]} of {total} original reviews.'
    if screens['followup_review_rounds']:
        alert_statement += (f' Follow-up reviews alerted in {screens["followup_alerts"]} of '
                            f'{screens["followup_review_rounds"]} rounds.')
    if screens['followup_rule_alerts']:
        alert_statement += (f' In {screens["followup_rule_rounds_with_repeated_flag"]} of the '
                            f'{screens["followup_rule_alerts"]} follow-up rounds with a rule alert, '
                            'a rule flag also appeared in the preceding round.')
    if screens['alerting_review_rounds']:
        alert_statement += (f' The {screens["alerting_review_rounds"]} alerting rounds covered '
                            f'{screens["encounters_with_alerts"]} encounters. A pharmacist or senior pharmacist '
                            f'reviewed {screens["alerting_rounds_reviewed"]} of those rounds.')
    final_flags = screens['final_rule_evidence_alerts']
    final_flag_statement = ''
    if final_flags:
        final_reviewed = ratio(screens['final_evidence_alerts_reviewed'], final_flags)
        final_approved = ratio(screens['final_evidence_alerts_approved'], final_flags)
        final_flag_statement = (
            f'For {final_flags} final orders, screening still flagged a rule with indeterminate evidence. '
            f'A pharmacist or senior pharmacist reviewed {final_reviewed} of these orders, '
            f'and {final_approved} received approval.'
        )
    probe_rows, probe_statement = probe_results(probes)
    cohort_rows = [(label, system, metrics['source_cases_by_group'].get(key, 0),
                    metrics['groups'].get(key, 0)) for key, label, system in STRATA]
    cohort_rows.append((r'\textbf{Total}', '', metrics['source_cases'], total))
    timing = [number(workflow[k], 2, 1 / 60) for k in (
        'queue_to_payment_seconds_mean', 'queue_to_payment_seconds_median', 'queue_to_payment_seconds_p95')]
    workflow_rows = [
        ('First candidate proceeded to signing', ratio(linked['first_candidate_signed'], total)),
        ('First review deferred or rejected', ratio(linked['first_review_nonapproval'], total)),
        ('Approved at review, blocked at encoding', ratio(linked['approved_but_unencodable'], total)),
        ('Queue-to-payment mean (min)', timing[0]),
        ('Queue-to-payment median (min)', timing[1]),
        ('Queue-to-payment 95th percentile (min)', timing[2]),
    ]
    weight_rows = [*effort_weight_rows('preconsult', pre), *effort_weight_rows('documentation', notes)]
    defined_ranges = [(low, high) for _, _, _, low, high in weight_rows if low is not None]
    sign_changes = sum(low < 0 < high for low, high in defined_ranges)
    weight_statement = (
        f'Across these weights, {sign_changes} of {len(defined_ranges)} comparisons with defined estimates '
        'ranged from reduced to increased modeled effort.' if defined_ranges else
        'The ranges require a defined effort estimate under both configurations.'
    )
    weight_table = []
    for task in ('Pre-consultation', 'Documentation'):
        weight_table.append((r'\multicolumn{3}{@{}l}{\textit{' + task + '}}',))
        for name, config, default, low, high in weight_rows:
            if name == task:
                shift = DASH if default is None else rf'\({default:+.2f}\)'
                bounds = DASH if low is None else rf'\([{low:+.2f}, {high:+.2f}]\)'
                weight_table.append((config, shift, bounds))
    matched = metrics['matched_stage_effort']
    matched_table = []
    for task, title in (('preconsult', 'Pre-consultation'), ('documentation', 'Documentation')):
        matched_table.append((r'\multicolumn{5}{@{}l}{\textit{' + title + '}}',))
        baseline, *alternatives = CONFIGURATIONS[task]
        for key, label in alternatives:
            row = matched[task][key]
            blocks = row['matched_blocks']
            histories = row['source_histories']
            mean = row['mean_difference_minutes']
            low, high = row['min_history_difference_minutes'], row['max_history_difference_minutes']
            if mean is None:
                if any(value is not None for value in (low, high)) or histories or blocks:
                    raise ValueError('Undefined matched effort with observed pairs')
                matched_table.append((label, 0, 0, DASH, DASH))
                continue
            max_blocks = min(metrics['task'][task][key]['qualified'], metrics['task'][task][baseline[0]]['qualified'])
            if (not 0 < histories <= min(blocks, metrics['source_cases']) or blocks > max_blocks
                    or not all(math.isfinite(value) for value in (low, mean, high)) or not low <= mean <= high):
                raise ValueError('Invalid history-balanced effort contrast')
            matched_table.append((label, blocks, histories, rf'\({mean:+.2f}\)', rf'\([{low:+.2f}, {high:+.2f}]\)'))
    reviewed_handoff = matched['preconsult']['ai_nurse_review']
    matched_statement = ''
    if reviewed_handoff['mean_difference_minutes'] is not None:
        matched_statement = (
            'The matched contrast for AI with nurse review relative to nurse-agent intake was '
            rf'\({reviewed_handoff["mean_difference_minutes"]:+.2f}\) minutes across '
            f'{reviewed_handoff["matched_blocks"]} blocks from {reviewed_handoff["source_histories"]} histories.'
        )
    delivered_handoffs = sum(row['qualified'] for row in pre.values())
    first_pass_handoffs = sum(row['first_pass_delivered'] for row in pre.values())
    interim_notes = notes['realtime']
    macros = {
        'PaidCount': total, 'SourceCount': metrics['source_cases'],
        'ConditionCount': total_cells, 'ObservedConditionCount': linked['configuration_cells'],
        'MinConditionEncounters': linked['encounters_per_cell_min'],
        'MaxConditionEncounters': linked['encounters_per_cell_max'],
        'ObservationWindowMinutes': f'{window_minutes:g}',
        'ApprovedUnencodable': linked['approved_but_unencodable'],
        'InitialApprovals': decisions['approve'],
        'InitialNonapprovals': decisions['defer'] + decisions['reject'],
        'RxReferenceSummary': ref_statement,
        'HumanReviewStatement': human_review_statement(review, paid),
        'RxAlertSummary': alert_statement,
        'RxFinalEvidenceSummary': final_flag_statement,
        'NurseDelivered': ratio(pre['nurse_only']['qualified'], pre['nurse_only']['observed']),
        'AIDelivered': ratio(pre['ai_only']['qualified'], pre['ai_only']['observed']),
        'ReviewedDelivered': ratio(pre['ai_nurse_review']['qualified'], pre['ai_nurse_review']['observed']),
        'HandoffsNeedingFollowup': ratio(delivered_handoffs - first_pass_handoffs, delivered_handoffs),
        'NotesDelivered': ratio(sum(r['qualified'] for r in notes.values()), total),
        'InterimAvailability': ratio(interim_notes['interim_notes_before_close'], interim_notes['observed']),
        'RealtimeVersions': notes['realtime']['saved_note_versions'], 'RealtimeNotes': notes['realtime']['observed'],
        'PostvisitVersions': notes['postvisit']['saved_note_versions'], 'PostvisitNotes': notes['postvisit']['observed'],
        'NurseHandoffMinutes': number(pre['nurse_only']['mean_modeled_minutes']),
        'AIHandoffMinutes': number(pre['ai_only']['mean_modeled_minutes']),
        'ReviewedHandoffMinutes': number(pre['ai_nurse_review']['mean_modeled_minutes']),
        'HandoffSupplementActions': sum(r['physician_supplementation_actions'] for r in pre.values()),
        'HandoffCorrectionActions': sum(r['physician_correction_actions'] for r in pre.values()),
        'ManualMinutes': number(notes['manual_documentation']['mean_modeled_minutes']),
        'RealtimeMinutes': number(notes['realtime']['mean_modeled_minutes']),
        'PostvisitMinutes': number(notes['postvisit']['mean_modeled_minutes']),
        'MeanSettlementMinutes': timing[0], 'MedianSettlementMinutes': timing[1], 'TailSettlementMinutes': timing[2],
        'ProbeSummary': probe_statement,
        'WeightSensitivitySummary': weight_statement,
        'MatchedHandoffSummary': matched_statement,
    }
    all_tables = [
        ('CohortRows', cohort_rows), ('PrescriptionRows', rx_rows), ('AlertRows', alerts),
        ('FollowupAlertRows', followup_alerts), ('PreconsultRows', pre_rows),
        ('DocumentationRows', note_rows), ('WorkflowRows', workflow_rows), ('ProbeRows', probe_rows),
        ('WeightSensitivityRows', weight_table),
        ('MatchedEffortRows', matched_table),
    ]
    return ('% Generated from linked-encounter metrics and native audit.\n'
            + ''.join(command(k, v) for k, v in macros.items()) + '\n'
            + ''.join(table(name, rows) for name, rows in all_tables))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('metrics', 'workflow-audit', 'human-review', 'probes', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    tex = render(*(read_json(p) for p in (args.metrics, args.workflow_audit, args.human_review, args.probes)))
    args.output.write_text(tex, encoding='utf-8')


if __name__ == '__main__':
    main()
