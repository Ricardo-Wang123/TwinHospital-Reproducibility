"""Fill the four TwinHospital table macros from one finished formal study."""
from __future__ import annotations

import argparse
from collections import Counter
import json
import math
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sandbox.experiments.linked.cases import GROUPS
from sandbox.experiments.linked.linked_acceptance import PATIENTS_PER_GROUP, SUPPORTED_PATIENTS_PER_GROUP, linked_profiles
from sandbox.experiments.linked.provider import private_json
from sandbox.experiments.linked.source_operation_replay import CATEGORIES, read, sha


RX_METRICS = ('pass_rate', 'severe_miss_rate', 'fpr', 'recall', 'precision', 'f1')
SAFE_LOCAL = {'gateway_prevention', 'native_rejection_no_effect_observed', 'safe_idempotent_replay'}


def numeric_metric(row, name, missing):
    estimate, denominator = row['estimate'], row['denominator']
    if (type(estimate) not in (float, int) or not math.isfinite(estimate) or estimate < 0
            or type(denominator) not in (float, int) or not math.isfinite(denominator) or denominator <= 0):
        missing.append(f"{name} ({row['status']}, denominator={denominator})")
        return None
    return estimate


def workflow_counts(summary, replay):
    if summary['planned_full_workflow_units'] != 1000 or summary['fixed_input_task_units'] != 9000:
        raise ValueError('Formal audit must retain 1000 canonical encounters and 9000 task units')
    completion = summary['full_workflow']['correct_completion']
    unsafe = summary['full_workflow']['unsafe_execution']
    for count in (completion, unsafe):
        parts = [count[k] for k in ('true', 'false', 'unknown')]
        if count['denominator'] != 1000 or any(type(n) is not int or n < 0 for n in parts) or sum(parts) != 1000:
            raise ValueError('Workflow counts do not partition the planned cohort')
    cells = replay['by_cell']
    expected = {(g, op, cond): n for g in GROUPS for op in CATEGORIES
                for cond, n in [('single_fault', 4), ('compliant_control', 16)]}
    actual, outcomes = {}, Counter()
    safe, allowed = 0, 0
    for row in cells:
        key = (row['disease'], row['operation'], row['condition'])
        counts = row['counts']
        if key in actual or any(type(n) is not int or n < 0 for n in counts.values()):
            raise ValueError('Invalid local replay cell')
        actual[key] = sum(counts.values())
        outcomes.update(counts)
        if row['condition'] == 'single_fault':
            safe += sum(counts.get(label, 0) for label in SAFE_LOCAL)
        else:
            allowed += counts.get('control_allowed', 0)
    if actual != expected or dict(outcomes) != replay['outcomes']:
        raise ValueError('Local replay does not preserve the declared 100/400 allocation')
    return {'complete': completion['true'], 'unsafe': unsafe['true'], 'unresolved': completion['unknown'],
            'known_incomplete': completion['false'], 'local_safe': safe, 'local_allowed': allowed}


def fill_tables(template, specification, stage, workflow):
    conditions = {(c['task'], c['workflow']): c['config_id'] for c in specification['conditions']}
    tables = {}
    missing = []
    layout = {
        'THPreconsultRows': ('preconsult', [('nurse_only', 'Nurse agent'), ('ai_only', 'AI only'),
                                            ('ai_nurse_review', 'AI + nurse')]),
        'THDocumentationRows': ('documentation', [('manual_documentation', 'Physician agent'),
                                                   ('realtime', 'During consultation'), ('postvisit', 'Post-consultation')]),
    }
    for macro, (task, rows) in layout.items():
        tables[macro] = []
        for name, label in rows:
            cid = conditions[task, name]
            metrics = stage['pooled']['configurations'][cid]
            delivery = numeric_metric(metrics['delivery_rate'], cid+'/delivery_rate', missing)
            effort = numeric_metric(metrics['staff_minutes_per_delivered_task'], cid+'/staff_minutes', missing)
            if delivery is not None and effort is not None:
                tables[macro].append(f'{label} & {delivery*100:.1f} & {effort:.2f}')
    tables['THPrescriptionRows'] = []
    for name, label in [('pprs_baseline', 'Rule baseline'), ('sequential', 'Sequential'), ('parallel', 'Parallel')]:
        cid = conditions['prescription', name]
        metrics = stage['prescription_scope']['pooled']['configurations'][cid]['metrics']
        values = [numeric_metric(metrics[m], cid+'/'+m, missing) for m in RX_METRICS]
        if any(value is None for value in values):
            continue
        columns = [f'{v*100:.1f}\\%' for v in values[:-1]] + [f'{values[-1]:.3f}']
        tables['THPrescriptionRows'].append(' & '.join([label, *columns]))
    if missing:
        raise ValueError('Table cells lack estimable values: ' + ', '.join(missing))
    tables['THWorkflowRows'] = [f'{label} & {planned:,} & {workflow[key]:,}' for key, label, planned in (
        ('complete', 'Complete paid workflow', 1000), ('unsafe', 'Workflow with unsafe effect', 1000),
        ('unresolved', 'Unresolved completion status', 1000), ('local_safe', 'Local errors handled safely', 100),
        ('local_allowed', 'Local controls allowed', 400))]
    for name, rows in tables.items():
        pattern = re.compile(r'(\\newcommand\{\\' + name + r'\}\{%\n).*?^\}', re.MULTILINE | re.DOTALL)
        body = ''.join(row + r' \\' + '\n' for row in rows)
        template, count = pattern.subn(lambda match: match[1] + body + '}', template)
        if count != 1:
            raise ValueError('Expected exactly one result table macro ' + name)
    return template


def export(batch, stage_path, audit_path, replay_path, template, output):
    batch, output = batch.resolve(), output.resolve()
    if output.exists() or output.is_relative_to(batch):
        raise ValueError('Use a new export directory outside the source batch')
    paths = (batch/'specification.json', batch/'batch_summary.json', stage_path, audit_path, replay_path, template)
    fingerprints = {str(p.resolve()): sha(p) for p in paths}
    spec, summary, stage, audit, replay = (read(p) for p in paths[:-1])
    source_hash = fingerprints[str(paths[0])]
    if (spec.get('formal_experiment') is not True or summary.get('batch') != spec.get('batch')
            or not summary.get('session_finished_at')
            or (summary.get('native_observation') or {}).get('status') != 'closed'):
        raise ValueError('A finished formal batch with native capture and engineering acceptance is required')
    target = spec.get('engineering_acceptance_patients_per_disease', PATIENTS_PER_GROUP)
    factorial = spec.get('engineering_acceptance_factorial', False)
    profiles, _ = linked_profiles(spec['models']['tool_models'], factorial=factorial)
    if (target not in SUPPORTED_PATIENTS_PER_GROUP or (target == 2 and not factorial)
            or (spec.get('engineering_acceptance_proof') or {}).get('verified_paid') != target * len(GROUPS) * len(profiles)):
        raise ValueError('The formal batch lacks its declared same-version engineering acceptance')
    if (stage.get('formal_result') is not True or stage.get('source_files_unchanged') is not True
            or stage.get('prospective_analysis_plan') is not True
            or stage.get('analysis_plan_sha256') != spec['formal_inputs']['analysis_plan_sha256']
            or stage.get('source_specification_sha256') != source_hash
            or stage['pooled']['distinct_source_patients'] != 1000
            or stage['prescription_scope']['pooled']['planned_source_patients'] != 1000
            or audit.get('experiment_status') != 'formal_declared_in_spec'
            or audit.get('source_files_unchanged') is not True
            or audit.get('native_observation_requested') is not True
            or audit.get('source_specification_sha256') != source_hash):
        raise ValueError('Stage analysis and native audit must cover the same finished formal cohort')
    replay_inputs = replay['source_inputs_sha256']
    binding = spec['formal_inputs']
    if (replay.get('is_full_formal_schedule') is not True or replay.get('recorded_units') != 500
            or replay.get('source_inputs_unchanged') is not True
            or replay_inputs.get(str(paths[0])) != source_hash
            or not {binding['cohort_sha256'], binding['operation_schedule_sha256']} <= set(replay_inputs.values())):
        raise ValueError('Local replay must come from the same frozen formal cohort and schedule')
    counts = workflow_counts(audit['summary'], replay)
    tex = fill_tables(template.read_text(), spec, stage, counts)
    if any(sha(p) != h for p, h in fingerprints.items()):
        raise ValueError('A result source changed during export')
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix='.acl-result-tables-', dir=output.parent) as tmp:
        staging = Path(tmp)
        (staging/'acl_results.tex').write_text(tex)
        private_json(staging/'result_table_receipt.json', {'source_sha256': fingerprints,
            'formal_batch': spec['batch'], 'workflow_counts': counts, 'analysis_plan_sha256': stage['analysis_plan_sha256'],
            'runtime_and_discussion_prose_retained': True, 'tex_sha256': sha(staging/'acl_results.tex')})
        staging.rename(output)
    return counts


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ('batch', 'stage-path', 'audit-path', 'replay-path', 'template', 'output'):
        parser.add_argument('--' + flag, type=Path, required=True)
    print(json.dumps(export(**vars(parser.parse_args()))))
