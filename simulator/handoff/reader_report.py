"""Report crossed encounters using the same task metrics as the study."""

from collections import Counter
from hashlib import sha256
import json
from statistics import fmean

from results.summarize_paid_runs import read
from sandbox.experiments.linked.linked_acceptance import READER_DESIGN_VERSION
from sandbox.experiments.linked.assignment_repeat import VERSION as REPEAT_DESIGN_VERSION
from sandbox.experiments.linked import scenario_design as scenarios


WORKFLOWS = {
    'manual_documentation': 'Physician-written note',
    'postvisit': 'Encounter dialogue and physician addendum',
    'direct_consultation': 'Direct consultation',
    'report_before_consultation': 'Read AI report before consultation',
    'report_during_consultation': 'Read AI report during consultation',
    'physician_prescribing': 'Physician prescription',
    'ai_first_prescribing': 'AI draft, physician finalization',
    'physician_first_prescribing': 'Physician draft, AI advice, physician finalization',
    'brief_to_note': 'Physician dictation, AI note, physician finalization',
}


def assignment_counts(rows):
    return {'planned': len(rows), 'outcomes': dict(Counter(row['runtime_status'] for row in rows)),
            'verified_paid': sum(row['verified_paid'] is True for row in rows),
            'eligible_for_paper_metrics': sum(row['verified_paid'] is True
                                              and row['formal']['correct_completion'] is True for row in rows)}


def review_outcomes(runs, assigned):
    reviewed = [run for run in runs if run['measurements']['pharmacist_review_rounds']]
    returned = [run for run in reviewed if run['measurements']['pharmacist_return_rounds']]
    first = sum(run['measurements']['first_pharmacist_return'] for run in reviewed)
    rounds = sum(run['measurements']['pharmacist_return_rounds'] for run in reviewed)
    approved = sum(bool(run['artifacts'].get('final_prescription', {}).get('items'))
                   and run['measurements']['last_pharmacist_approved'] for run in returned)
    paid = sum(run['audited_payment'] for run in returned)
    return {
        'assigned': assigned, 'reviewed': len(reviewed),
        'review_coverage': len(reviewed) / assigned if assigned else None,
        'first_return_encounters': first, 'first_return_rate': first / len(reviewed) if reviewed else None,
        'any_return_encounters': len(returned), 'any_return_rate': len(returned) / len(reviewed) if reviewed else None,
        'total_return_rounds': rounds, 'mean_return_rounds': rounds / len(reviewed) if reviewed else None,
        'encoding_only_revisions': sum(run['measurements']['encoding_only_revisions'] for run in reviewed),
        'returned_then_approved': approved, 'approved_given_return': approved / len(returned) if returned else None,
        'returned_then_paid': paid, 'paid_given_return': paid / len(returned) if returned else None,
    }


def comparison_data(batch, audit_path):
    spec_path = batch / 'specification.json'
    specification_bytes = spec_path.read_bytes()
    specification_hash = sha256(specification_bytes).hexdigest()
    specification = json.loads(specification_bytes)
    if specification.get('scenario_design') != scenarios.VERSION:
        raise ValueError('The report requires an outpatient-v2 batch.')
    design = specification.get('linked_acceptance', {})
    heldout = design.get('independent_crossed') is True and design.get('evaluation_partition') == 'test'
    repeat = design.get('version') == REPEAT_DESIGN_VERSION
    if design.get('version') != READER_DESIGN_VERSION and not heldout and not repeat:
        raise ValueError('The batch is not a reader comparison or independent held-out crossing.')
    audit_document = read(audit_path)
    audit = audit_document['audit']
    rows = audit['runs']
    expected_profiles = (Counter(row['profile'] for row in design['assignment_plan']['assignments']) if repeat else
                         Counter({profile['label']: len(specification['cases']) for profile in design['profiles']}))
    if (audit['source_specification_sha256'] != specification_hash
            or len(rows) != design['planned_unique_encounters']
            or len({row['study_id'] for row in rows}) != len(rows)
            or Counter(row['profile'] for row in rows)
            != expected_profiles):
        raise ValueError('The audit does not cover the planned assignments for this batch.')
    if repeat:
        expected_pairs = {(row['case_id'], row['profile']) for row in design['assignment_plan']['assignments']}
        observed_pairs = [(row.get('case_id'), row['profile']) for row in rows]
        if len(observed_pairs) != len(set(observed_pairs)) or set(observed_pairs) != expected_pairs:
            raise ValueError('The audit does not match the frozen repeated case/configuration pairs.')
    groups = {case['case_id']: case['group'] for case in specification['cases']}
    if heldout:
        expected = {(case_id, profile['label']) for case_id in groups
                    for profile in design['profiles']}
        crosswalk = audit_document['crosswalk']
        rows_by_id = {row['study_id']: row for row in rows}
        if (len(crosswalk) != len(rows) or {(row['case_id'], row['condition']) for row in crosswalk} != expected
                or {row['study_id'] for row in crosswalk} != rows_by_id.keys()
                or any(rows_by_id[row['study_id']]['profile'] != row['condition']
                       or rows_by_id[row['study_id']]['group'] != groups[row['case_id']] for row in crosswalk)):
            raise ValueError('The held-out audit does not bind every planned case and configuration.')
    profiles = []
    for profile in design['profiles']:
        selected = [row for row in rows if row['profile'] == profile['label']]
        profiles.append({
            'workflows': {task: c['workflow'] for task, c in profile['conditions'].items()},
            **assignment_counts(selected),
        })
    return scenario_data(batch, specification, audit, profiles)


def scenario_data(batch, specification, audit, profiles):
    expected = {row['source_run_file_sha256'] for row in audit['runs']
                if row.get('source_run_file_sha256')}
    paid_hashes = {row['source_run_file_sha256'] for row in audit['runs'] if row.get('verified_paid') is True}
    runs, paid_runs = [], []
    observed = set()
    for case in read(batch / 'batch_summary.json')['cases']:
        for item in case['attempts'].values():
            if not item.get('result_path'):
                continue
            path = (batch / item['result_path']).resolve()
            if not path.is_relative_to(batch.resolve()):
                raise ValueError('Run archive path is outside the specified batch.')
            raw = path.read_bytes()
            identity = sha256(raw).hexdigest()
            if identity not in expected or identity in observed:
                raise ValueError('Run archive does not match the independent state audit.')
            run = json.loads(raw)
            doc = run.get('conditions', {}).get('documentation', {}).get('workflow')
            if doc in {'brief_to_note', 'postvisit'} and any(
                    c['role'] == 'scribe' and c['observation']['action'] == 'document' for c in run['calls']):
                source = run['artifacts'].get('documentation_source_policy', {})
                if source.get('version') != scenarios.DOCUMENTATION_SOURCE_VERSION:
                    raise ValueError('This archive uses an earlier documentation protocol. Use its original report; '
                                     'pool only matching dictation and encounter-transcript conditions.')
            stored = run.get('measurements') or {}
            version = stored.get('measurement_version', 1)
            if run.get('scenario_design') != scenarios.VERSION or stored != scenarios.measurements(run, version=version):
                raise ValueError('Scenario measurements differ from their actual run trace.')
            run = {**run, 'measurements': stored if version == 2 else scenarios.measurements(run),
                   'audited_payment': identity in paid_hashes}
            observed.add(identity)
            runs.append(run)
            if identity in paid_hashes:
                paid_runs.append(run)
    if observed != expected:
        raise ValueError('The report is missing an audited run trace.')
    rows = []
    for task, workflows in scenarios.WORKFLOWS.items():
        for workflow in workflows:
            selected = [run for run in runs if run['conditions'][task]['workflow'] == workflow]
            def mean(field):
                values = [run['measurements'][field] for run in selected if run['measurements'][field] is not None]
                return fmean(values) if values else None
            assigned = sum(p['planned'] for p in profiles if p['workflows'][task] == workflow) if profiles else None
            rows.append({'task': task, 'workflow': workflow, 'observed_traces': len(selected),
                'assigned': assigned,
                'runtime_completed': sum(run['status'] == 'completed' for run in selected),
                'prescription_candidates': sum(bool(run['artifacts'].get('original_prescription', {}).get('items'))
                                                for run in selected),
                'signed_prescriptions': sum(run['artifacts'].get('prescription_business_state', {}).get('status') == 'signed'
                                             for run in selected),
                'locked_notes': sum(run['artifacts'].get('attested_note', {}).get('status') == 'locked' for run in selected),
                'clinical_quality_status': dict(Counter(run['clinical_quality']['status'] for run in selected)),
                'timing_sources': sorted({run['timing_source'] for run in selected}),
                'mean_modeled_report_reading_seconds': mean('modeled_report_reading_seconds'),
                'mean_modeled_prescribing_seconds': mean('modeled_prescribing_seconds'),
                'mean_modeled_documentation_seconds': mean('modeled_documentation_seconds'),
                'mean_modeled_staff_seconds': mean('modeled_staff_seconds'),
                'physician_history_endpoints': sum(run['measurements']['physician_questions_before_first_candidate']
                                                  is not None for run in selected),
                'mean_physician_questions_before_first_candidate': mean('physician_questions_before_first_candidate'),
                'mean_patient_preconsult_answers': mean('patient_preconsult_answers'),
                'mean_patient_consultation_answers': mean('patient_consultation_answers'),
                'mean_patient_reconciliation_answers': mean('patient_reconciliation_answers'),
                'mean_patient_answers_total': mean('patient_answers_total'),
                'mean_note_revision_count': mean('note_revision_count'),
                'mean_prescription_revision_count': mean('prescription_revision_count')})
            if task == 'prescription':
                rows[-1]['pharmacist_review'] = review_outcomes(selected, assigned)
    design = specification['linked_acceptance']
    if design.get('version') == REPEAT_DESIGN_VERSION:
        design_label = f"Repeat assignments across {len(profiles)} configurations"
    elif design.get('quick'):
        design_label = 'Three-configuration pilot'
    else:
        design_label = f"{len(profiles)}-configuration crossing"
    return {'batch': specification['batch'], 'scenario_design': scenarios.VERSION, 'design': design_label,
        'his_interface': specification.get('his_interface'),
        'source_cases_by_group': dict(Counter(case['group'] for case in specification['cases'])),
        'role_model': specification['models']['role_model'],
        'tool_model': next(iter(specification['models']['tool_models'].values())),
        'planned': len(audit['runs']), 'terminal_outcomes': dict(Counter(row['runtime_status'] for row in audit['runs'])),
        'verified_paid': sum(row['verified_paid'] is True for row in audit['runs']),
        'profiles': profiles, 'tasks': rows,
        'matched_role_effort': (scenario_effort(paid_runs, specification['role_effort_analysis'])
                                if specification.get('role_effort_analysis') else None)}


def scenario_effort(runs, plan):
    from results.summarize_paid_runs import matched_stage_effort
    if plan != scenarios.effort_plan():
        raise ValueError('Role effort analysis differs from the frozen scenario specification.')
    kinds = {
        'preconsult': {'report_reading', 'diagnostic', 'history_lookup'},
        'prescription': {'prescription_drafting', 'prescribing_ai_review', 'prescription_revision',
                         'prescription_review'},
        'documentation': {'documentation', 'brief_documentation', 'note_supplementation', 'note_review', 'note_rework', 'attestation'},
    }
    adapted = [{**run, 'stages': {task: {'qualifies': True, 'activities': [
        (a['role'], a['activity_type'], a['active_seconds']) for a in run['activities']
        if a['role'] in {'doctor', 'pharmacist'} and (a.get('stage') == task if 'stage' in a else
            a['activity_type'] in types and (task != 'preconsult' or a['role'] == 'doctor'))]}
        for task, types in kinds.items()}} for run in runs]
    return {task: matched_stage_effort(adapted, task, flows[0], analysis_plan=plan)
            for task, flows in scenarios.WORKFLOWS.items()}


def number(value):
    return '—' if value is None else f'{value:.2f}'


def render(report):
    labels = WORKFLOWS
    lines = ['# TwinHospital comparison', '',
        f"Batch `{report['batch']}` · `{report['scenario_design']}` · {report['design']}", '',
        f"Role model: `{report['role_model']}`. AI tool model: `{report['tool_model']}`.", '',
        '## Application I · Clinical artifacts', '',
        '| Task | Condition | Traces | Signed prescriptions | Locked notes |',
        '| --- | --- | ---: | ---: | ---: |']
    for row in report['tasks']:
        lines.append(f"| {row['task']} | {labels[row['workflow']]} | {row['observed_traces']} | "
                     f"{row['signed_prescriptions']} | {row['locked_notes']} |")
    lines.extend(['', 'These are artifact counts. Content correctness uses the case-specific evaluation reference.', '',
        '| Prescribing | Reviewed / assigned | Returned at first review / reviewed | Ever returned / reviewed | Return rounds | Encoding-only revisions | Paid after return / returned |',
        '| --- | ---: | ---: | ---: | ---: | ---: | ---: |'])
    for row in report['tasks']:
        if row['task'] == 'prescription':
            m = row['pharmacist_review']
            lines.append(f"| {labels[row['workflow']]} | {m['reviewed']} / {m['assigned'] if m['assigned'] is not None else '—'} | "
                         f"{m['first_return_encounters']} / {m['reviewed']} | {m['any_return_encounters']} / {m['reviewed']} | "
                         f"{m['total_return_rounds']} | {m['encoding_only_revisions']} | "
                         f"{m['returned_then_paid']} / {m['any_return_encounters']} |")
    lines.extend(['', 'Counts are shown with their denominators. A zero denominator leaves the corresponding rate undefined.', '',
        '## Application II · Role workload', '',
        '| Condition | Physician reading (s) | Physician prescribing (s) | Physician documentation (s) | Total physician and pharmacist work (s) | Patient answers (intake / consultation / reconciliation) |',
        '| --- | ---: | ---: | ---: | ---: | ---: |'])
    for row in report['tasks']:
        lines.append(f"| {labels[row['workflow']]} | {number(row['mean_modeled_report_reading_seconds'])} | "
            f"{number(row['mean_modeled_prescribing_seconds'])} | {number(row['mean_modeled_documentation_seconds'])} | "
            f"{number(row['mean_modeled_staff_seconds'])} | {number(row['mean_patient_preconsult_answers'])} / "
            f"{number(row['mean_patient_consultation_answers'])} / {number(row['mean_patient_reconciliation_answers'])} |")
    lines.extend(['', 'Activity-weight estimates, dialogue counts, and API elapsed time are separate measurements.', '',
        ])
    if report.get('matched_role_effort'):
        lines.extend(['| Matched contrast | Source histories | Difference (modeled min) | 95% CI |',
                      '| --- | ---: | ---: | ---: |'])
        for task, contrasts in report['matched_role_effort'].items():
            for workflow, row in contrasts.items():
                ci = row['bootstrap']['ci']
                limits = ' to '.join(map(number, ci)) if ci is not None else '—'
                lines.append(f"| {labels[workflow]} minus {labels[scenarios.WORKFLOWS[task][0]]} | "
                    f"{row['source_histories']} | {number(row['mean_difference_minutes'])} | {limits} |")
        lines.extend(['', 'Contrasts use matched verified-paid encounters with the other stages held fixed. '
                      'Intervals resample source histories within disease strata.', ''])
    lines.extend(['## Application III · Complete encounters', '',
        f"{report['verified_paid']}/{report['planned']} assigned encounters have verified native payment.", '',
        '| Intake | Prescribing | Documentation | Assigned | Verified paid |',
        '| --- | --- | --- | ---: | ---: |'])
    for row in report['profiles']:
        names = [labels[row['workflows'][task]] for task in scenarios.WORKFLOWS]
        lines.append(f"| {' | '.join(names)} | {row['planned']} | {row['verified_paid']} |")
    return '\n'.join(lines) + '\n'


def write_report(batch, audit_path, output):
    report = comparison_data(batch, audit_path)
    output.mkdir(parents=True, mode=0o700)
    (output / 'summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n',
                                         encoding='utf-8')
    (output / 'REPORT.md').write_text(render(report), encoding='utf-8')
    print(f'Comparison report written to {output / "REPORT.md"}')
