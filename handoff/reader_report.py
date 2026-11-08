"""Report crossed encounters using the same task metrics as the study."""

from collections import Counter
from hashlib import sha256
import json

from results.render_linked_manuscript import OPERATIONS, SAFE_FAULT_OUTCOMES, probe_results
from results.summarize_paid_runs import read, summarize
from sandbox.experiments.linked.linked_acceptance import READER_DESIGN_VERSION


WORKFLOWS = {
    'nurse_only': 'Nurse',
    'ai_only': 'AI alone',
    'ai_nurse_review': 'AI + nurse',
    'pprs_baseline': 'Rules + pharmacist routing',
    'sequential': 'Sequential',
    'parallel': 'Parallel',
    'manual_documentation': 'Physician',
    'realtime': 'During visit',
    'postvisit': 'After visit',
}


def comparison_summary(batch, audit_path, manifest, clinical_catalogue, operation_path):
    spec_path = batch / 'specification.json'
    specification_bytes = spec_path.read_bytes()
    specification_hash = sha256(specification_bytes).hexdigest()
    specification = json.loads(specification_bytes)
    design = specification.get('linked_acceptance', {})
    heldout = design.get('independent_crossed') is True and design.get('evaluation_partition') == 'test'
    if design.get('version') != READER_DESIGN_VERSION and not heldout:
        raise ValueError('The batch is not a reader comparison or independent held-out crossing.')
    audit_document = read(audit_path)
    audit = audit_document['audit']
    rows = audit['runs']
    if (audit['source_specification_sha256'] != specification_hash
            or len(rows) != design['planned_unique_encounters']
            or len({row['study_id'] for row in rows}) != len(rows)
            or Counter(row['profile'] for row in rows)
            != Counter({profile['label']: len(specification['cases']) for profile in design['profiles']})):
        raise ValueError('The audit does not cover the planned assignments for this batch.')
    if heldout:
        expected = {(case['case_id'], profile['label']) for case in specification['cases']
                    for profile in design['profiles']}
        crosswalk = audit_document['crosswalk']
        profiles_by_id = {row['study_id']: row['profile'] for row in rows}
        if (len(crosswalk) != len(rows) or {(row['case_id'], row['condition']) for row in crosswalk} != expected
                or {row['study_id'] for row in crosswalk} != profiles_by_id.keys()
                or any(profiles_by_id[row['study_id']] != row['condition'] for row in crosswalk)):
            raise ValueError('The held-out audit does not bind every planned case and configuration.')
    profiles = []
    for profile in design['profiles']:
        selected = [row for row in rows if row['profile'] == profile['label']]
        profiles.append({
            'workflows': {task: c['workflow'] for task, c in profile['conditions'].items()},
            'planned': len(selected),
            'outcomes': dict(Counter(row['runtime_status'] for row in selected)),
            'verified_paid': sum(row['verified_paid'] is True for row in selected),
            'eligible_for_paper_metrics': sum(row['verified_paid'] is True
                                              and row['formal']['correct_completion'] is True for row in selected),
        })
    operations = None
    if operation_path.is_file():
        replay = read(operation_path)
        if replay['source_inputs_sha256'].get(str(spec_path.resolve())) != specification_hash:
            raise ValueError('The operation replay belongs to a different batch.')
        if heldout:
            frozen = specification.get('heldout_operations')
            if not frozen or replay.get('heldout_operations') != frozen or replay['planned_units'] != frozen['planned_units']:
                raise ValueError('The operation replay does not match the held-out schedule.')
        probe_results(replay)  # Check that all scheduled requests are represented.
        operations = []
        for key, label, *_ in OPERATIONS:
            tests = {condition: Counter() for condition in ('single_fault', 'compliant_control')}
            for cell in replay['by_cell']:
                if cell['operation'] == key:
                    tests[cell['condition']].update(cell['counts'])
            faults, controls = tests.values()
            operations.append({
                'operation': label,
                'faults': dict(faults), 'controls': dict(controls),
                'verified_safe_faults': sum(faults[name] for name in SAFE_FAULT_OUTCOMES),
                'fault_requests': sum(faults.values()),
                'executed_controls': controls['control_allowed'], 'control_requests': sum(controls.values()),
            })
    paid = summarize(batch, audit_path, manifest, clinical_catalogue, allow_unverified=True)
    return {
        'batch': specification['batch'],
        'design': 'three-profile pilot' if design.get('quick') else '27-profile full crossing',
        'source_cases_by_group': dict(Counter(case['group'] for case in specification['cases'])),
        **({'evaluation_partition': 'test'} if heldout else {}),
        'role_model': specification['models']['role_model'],
        'tool_model': specification['models']['tool_models']['tool_a'],
        'planned': len(rows),
        'terminal_outcomes': dict(Counter(row['runtime_status'] for row in rows)),
        'verified_paid': sum(profile['verified_paid'] for profile in profiles),
        'profiles': profiles,
        'paid_cohort_metrics': paid,
        'operation_replay': operations,
    }


def number(value):
    return '—' if value is None else f'{value:.2f}'


def render(report):
    lines = [
        '# TwinHospital comparison', '',
        f"Batch `{report['batch']}` uses the {report['design']} with "
        f"`{report['role_model']}` for roles and `{report['tool_model']}` for AI tools.",
        '',
        '## Application I · Clinical artifacts', '',
    ]
    paid = report['paid_cohort_metrics']
    if paid is None:
        lines.extend(['The batch has no paid encounter eligible for the paper metrics.', '',
                      '## Application II · Modeled staff effort', '',
                      'Modeled staff effort per delivered artifact is unavailable for the paid analysis cohort.', ''])
    else:
        tasks = paid['task']
        lines.extend([
            f"Artifact and effort tables use the {paid['verified_paid']} verified paid encounters inside "
            'the frozen observation window.', '',
            '| Task and condition | Analyzed | Delivery or original decision |',
            '| --- | ---: | --- |',
        ])
        for task, label in (('preconsult', 'Handoff'), ('prescription', 'Prescription'), ('documentation', 'Note')):
            for workflow, row in tasks[task].items():
                if task == 'prescription':
                    value = ', '.join(f'{count} {decision}' for decision, count in row['first_decision'].items())
                    metric = row['rule_scoped_metrics']['fpr']
                    rate = f"{metric['numerator']}/{metric['denominator']}" if metric['denominator'] else '—'
                    value += f' · Rule-scoped FPR {rate}'
                else:
                    value = f"{row['qualified']} delivered"
                    if task == 'documentation' and workflow == 'realtime':
                        value += f", {row['interim_notes_before_close']} interim notes before consultation ended"
                lines.append(f"| {label} · {WORKFLOWS[workflow]} | {row['observed']} | {value} |")
        lines.extend([
            '', 'First and follow-up alert counts by reviewer are in the JSON report.', '',
            '## Application II · Modeled staff effort', '',
            '| Task and condition | Delivered | Modeled min per delivery | Mean note turnaround (s) |',
            '| --- | ---: | ---: | ---: |',
        ])
        for task, label in (('preconsult', 'Handoff'), ('documentation', 'Note')):
            for workflow, row in tasks[task].items():
                lines.append(f"| {label} · {WORKFLOWS[workflow]} | {row['qualified']} | "
                             f"{number(row['mean_modeled_minutes'])} | {number(row.get('mean_note_turnaround_seconds'))} |")
        lines.extend(['', 'Modeled work uses the study activity weights. Turnaround uses elapsed runtime. '
                      'The JSON includes rework, role effort, and matched contrasts when paired cells are available.', ''])
        plan = paid.get('heldout_effort_analysis')
        if plan is not None:
            lines.extend([
                '| Matched contrast | Blocks | Source histories | Difference (modeled min) | 95% CI |',
                '| --- | ---: | ---: | ---: | ---: |',
            ])
            for task, label in (('preconsult', 'Handoff'), ('documentation', 'Note')):
                baseline = WORKFLOWS[plan['contrasts'][task]['baseline']]
                for workflow, row in paid['matched_stage_effort'][task].items():
                    interval = row['bootstrap']['ci']
                    limits = ' to '.join(map(number, interval)) if interval is not None else '—'
                    lines.append(f"| {label} · {WORKFLOWS[workflow]} minus {baseline} | {row['matched_blocks']} | "
                                 f"{row['source_histories']} | {number(row['mean_difference_minutes'])} | {limits} |")
            lines.extend(['', 'Each interval resamples the contributing source histories within disease. '
                          'An interval requires at least two contributing histories per represented disease.', ''])
    lines.extend([
        '## Application III · Workflow integrity', '',
        f"{report['verified_paid']}/{report['planned']} assigned encounters had verified native payment and workflow checks.",
        '',
    ])
    if paid is not None:
        latency = number(paid['full_workflow']['queue_to_payment_seconds_median'])
        lines.extend([f'Median elapsed queue-to-payment time in the paid analysis cohort was {latency} seconds.', ''])
    lines.extend([
        '| Intake | Prescription | Documentation | Assigned | Verified paid | In analysis |',
        '| --- | --- | --- | ---: | ---: | ---: |',
    ])
    for row in report['profiles']:
        labels = [WORKFLOWS[row['workflows'][task]] for task in ('preconsult', 'prescription', 'documentation')]
        lines.append(f"| {' | '.join(labels)} | {row['planned']} | {row['verified_paid']} | "
                     f"{row['eligible_for_paper_metrics']} |")
    lines.extend(['', '### Paired operation checks', ''])
    if report['operation_replay'] is None:
        lines.append('Run the configured operation schedule to populate this section.')
    else:
        lines.extend(['| Operation | Verified safe faults | Executed controls |', '| --- | ---: | ---: |'])
        for row in report['operation_replay']:
            lines.append(f"| {row['operation']} | {row['verified_safe_faults']}/{row['fault_requests']} | "
                         f"{row['executed_controls']}/{row['control_requests']} |")
    return '\n'.join(lines) + '\n'


def write_report(batch, audit_path, manifest, clinical_catalogue, operation_path, output):
    report = comparison_summary(batch, audit_path, manifest, clinical_catalogue, operation_path)
    output.mkdir(parents=True, mode=0o700)
    (output / 'summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n',
                                         encoding='utf-8')
    (output / 'REPORT.md').write_text(render(report), encoding='utf-8')
    print(f'Comparison report written to {output / "REPORT.md"}')
