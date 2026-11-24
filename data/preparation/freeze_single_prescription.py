"""Freeze the same ED cohort with one prescription after the eligible evidence is read."""

from collections import Counter
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import argparse
import os
import shutil

from freeze_ed_protocol import digest, file_sha256, read, runtime_case, save
from handoff.mimic_ed_note import attach_index_note
from sandbox.experiments.linked.chart import reported_before
from sandbox.experiments.linked.ed_treatment_review import SINGLE_PRESCRIPTION


def prepare_single_prescription(source, output, *, align_source_notes=False):
    manifest = read(source / 'manifest.json')
    references = read(source / 'reference.private.json')
    audit = read(source / '病例核验.json')
    if len(manifest['cases']) != 100 or len(references) != 100:
        raise ValueError('Expected the frozen cohort of 100 ED stays')
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=output.parent) as temp:
        destination = Path(temp) / 'cohort'
        shutil.copytree(source / 'patients', destination / 'patients')
        signature_points = {}
        coverage = {}
        source_transactions = Counter()
        for row in manifest['cases']:
            case_id = row['case_id']
            reference = references[case_id]
            case = runtime_case(read(source / row['path']))
            if digest(case) != reference['runtime_input_hash']:
                raise ValueError(f'{case_id}: the old clinical input differs from the frozen case')
            source_timeline = reference['order_timeline']
            transactions = Counter(action['transaction'] for point in source_timeline for action in point['actions'])
            reference_ingredients = set(reference['continued'] + reference['added'])
            if set(transactions) != {'New'} or set(source_timeline[-1]['active']) != reference_ingredients or not reference_ingredients:
                raise ValueError(f'{case_id}: one final regimen cannot represent the recorded treatment')
            source_transactions.update(transactions)
            end = int((datetime.fromisoformat(reference['ed_end']) -
                       datetime.fromisoformat(reference['ed_start'])).total_seconds())
            chart = case['his_initial']
            timing = chart['time_basis']
            point = {'decision_seconds': end, 'release_before_seconds': end}
            timing.update(ed_prescribing_schedule=SINGLE_PRESCRIPTION, ed_order_checkpoints=[point])
            timing['ed_decision_offsets_hours'].update(reassessment=end / 3600,
                                                      reassessment_release_before=end / 3600)
            for kind in ('reports', 'workup'):
                closing = 'ed_closing_' + kind
                ready = chart.get(closing, [])
                destination_section = chart.setdefault('ed_reassessment_' + kind, [])
                destination_section.extend({**item, 'available_at': 'ed_reassessment'} if kind == 'reports' else item
                                           for item in ready if reported_before(item, end))
                chart[closing] = [item for item in ready if not reported_before(item, end)]
            if not chart.get('ed_diagnostic_reports') or not chart.get('current_observations'):
                raise ValueError(f'{case_id}: missing documented acute-visit evidence')
            index_path = destination / 'patients' / case_id / '原表索引.private.json'
            index = read(index_path)
            if align_source_notes:
                diagnosis = reference['ed_primary_icd']
                note = index['index_discharge_note']
                if (str(diagnosis['seq_num']) != '1' or note['note_id'] != reference['source_note_id']
                        or str(note['hadm_id']) != str(reference['source_hadm_id'])):
                    raise ValueError(f'{case_id}: the first ED diagnosis or linked admission note differs')
                case['ed_prescribing_target'] = {
                    'code': diagnosis['code'], 'system': f"ICD-{diagnosis['icd_version']}-CM",
                    'display': diagnosis['title']}
                selected = attach_index_note(case, note)
                intake_path = index_path.with_name('预问诊事实.private.json')
                intake = read(intake_path)
                intake['admission_history_sources'].extend(selected)
                intake['opening_patient_facts'] = case['patient_initial']['patient_facts']
                save(intake_path, intake)
            case_path = destination / row['path']
            save(case_path, case)
            runtime_hash = digest(runtime_case(case))
            reference['runtime_input_hash'] = runtime_hash
            reference['order_task_protocol'] = SINGLE_PRESCRIPTION
            reference['order_timeline'] = [{'offset_seconds': end, 'active': sorted(reference_ingredients),
                                            'actions': [], 'not_assessable': []}]
            reference.pop('predecessor_comparison', None)
            signature_points[case_id] = [point]
            coverage[case_id] = {
                'presentation_reports': len(chart['ed_diagnostic_reports']),
                'later_available_reports': len(chart['ed_reassessment_reports']),
                'reports_without_provable_predeparture_availability': len(chart['ed_closing_reports']),
                'recorded_primary_new_orders': transactions['New'],
            }
            index['report_inventory'] = {
                section: [{key: value for key, value in item.items() if key in {'source_ref', 'source_kind', 'time'}}
                          for item in records]
                for section, records in chart.items() if section.startswith('ed_') and isinstance(records, list)
            }
            save(index_path, index)
        for name in ('catalog.json', 'formulary_directory.json', 'history.private.sqlite'):
            shutil.copy2(source / name, destination / name)
        if file_sha256(destination / 'history.private.sqlite') != audit['history_library_sha256']:
            raise ValueError('The shared history catalogue changed')
        save(destination / 'manifest.json', manifest)
        save(destination / 'reference.private.json', references)
        save(destination / '主病开方节点.json', signature_points)
        save(destination / '病例核验.json', {**audit, 'version': SINGLE_PRESCRIPTION, 'cohort': str(source),
             'primary_checkpoints': len(signature_points), 'recorded_source_medication_transactions': source_transactions,
             'prescription_evidence_coverage': coverage,
             'cases': [{**item, 'nodes': 1} for item in audit['cases']]})
        destination.rename(output)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--align-source-notes', action='store_true',
                        help='Align prescribing evidence from the indexed MIMIC admission note.')
    args = parser.parse_args()
    os.umask(0o077)
    prepare_single_prescription(args.source.resolve(), args.output.resolve(),
                                align_source_notes=args.align_source_notes)
