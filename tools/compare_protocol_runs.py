"""Compare closed protocol arms without merging patients or result denominators."""
import argparse
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path


def read(path):
    return json.loads(path.read_bytes())


def plan_pairs(spec):
    rows = spec['linked_acceptance']['assignment_plan']['assignments']
    pairs = [(row['case_id'], row['profile']) for row in rows]
    if len(pairs) != len(set(pairs)):
        raise ValueError('The assignment plan repeats a case/configuration pair')
    return set(pairs)


def verified_case_metadata(config, base, spec):
    manifest_path = (base / config['manifest']).resolve()
    rows = read(manifest_path)['cases']
    indexed = {row['case_id']: row for row in rows}
    if len(indexed) != len(rows):
        raise ValueError('The input manifest repeats a case identity')
    verified = []
    for frozen in spec['cases']:
        row = indexed.get(frozen['case_id'])
        if row is None:
            raise ValueError('An archived case is absent from its input manifest')
        for key in ('group', 'split', 'data_kind', 'file_sha256', 'runtime_input_hash'):
            if row.get(key) != frozen.get(key):
                raise ValueError('Manifest metadata differs from the archived case: ' + key)
        path = (manifest_path.parent / row['path']).resolve()
        payload = path.read_bytes()
        if sha256(payload).hexdigest() != frozen['file_sha256']:
            raise ValueError('A source case differs from its archived file checksum')
        case = json.loads(payload)
        if any(case.get(key) != row.get(key) for key in ('case_id', 'patient_key', 'group', 'split', 'data_kind')):
            raise ValueError('Manifest source identity differs from the frozen case bytes')
        if not case.get('patient_key'):
            raise ValueError('The frozen case lacks a source-patient identity')
        verified.append({**frozen, 'patient_key': case['patient_key']})
    if len({row['patient_key'] for row in verified}) != len(verified):
        raise ValueError('Source histories repeat a source-patient identity')
    return verified


def validate_shared_inputs(first, second):
    fields = ('case_id', 'patient_key', 'group', 'file_sha256', 'runtime_input_hash')
    cases = lambda spec: sorted(tuple(row.get(k) for k in fields)
                               for row in spec.get('verified_case_metadata', spec['cases']))
    if cases(first) != cases(second) or plan_pairs(first) != plan_pairs(second):
        raise ValueError('Protocol arms do not use identical cases and assignments')
    for field in ('models', 'generation', 'generation_policy', 'transport_policy',
                  'workflow_limits', 'response_contract_policy', 'rules', 'modeled_activity_seconds',
                  'catalog_hash', 'diagnosis_catalog_hash', 'memory_policy',
                  'prescription_followthrough_policy', 'clinical_disposition_policy',
                  'current_measurement_policy', 'history_access_policy',
                  'consultation_dialogue_policy', 'diagnosis_code_policy'):
        if first.get(field) != second.get(field):
            raise ValueError('Protocol arms differ in shared settings: ' + field)
    if first['code_hashes'] != second['code_hashes']:
        raise ValueError('Protocol arms differ in the frozen runtime implementation')
    if first.get('his_interface', {}).get('version') != 'twinhospital-fhir-r4-2':
        raise ValueError('First arm is not the independent FHIR R4 workflow')
    if second.get('his_interface', {}).get('version') != 'twinhospital-soap-wst846-order-workflow-1':
        raise ValueError('Second arm is not the independent WS/T service profile')


def load_arm(config_path):
    config = read(config_path)
    base = config_path.resolve().parent
    batch = (base / config['batches'] / config['batch']).resolve()
    summary = read(batch / 'batch_summary.json')
    if not summary.get('session_finished_at'):
        raise ValueError('The batch has not closed')
    specification_path = batch / 'specification.json'
    specification = read(specification_path)
    # Archived specifications deliberately omit private host paths and patient
    # keys. Recover only verified source metadata from the configured manifest.
    specification['verified_case_metadata'] = verified_case_metadata(config, base, specification)
    audit_path = Path(config.get('audit_output', base / config['batches'] / (config['batch'] + '_audit')))
    if not audit_path.is_absolute():
        audit_path = base / audit_path
    audit = read(audit_path / 'audit.private.json')['audit']
    if audit['source_specification_sha256'] != sha256(specification_path.read_bytes()).hexdigest():
        raise ValueError('Audit belongs to a different batch')
    rows = audit['runs']
    pairs = [(row.get('case_id'), row['profile']) for row in rows]
    if len(pairs) != len(set(pairs)) or set(pairs) != plan_pairs(specification):
        raise ValueError('Audit does not cover exactly the assigned case/configuration pairs')
    report_path = base / 'reader_results/summary.json'
    report = read(report_path)
    if report['batch'] != specification['batch'] or report['planned'] != len(rows):
        raise ValueError('Task report does not match the audited batch')
    paid = sum(row['verified_paid'] is True and row['formal']['correct_completion'] is True for row in rows)
    if paid != (report.get('paid_cohort_metrics') or {}).get('verified_paid', 0):
        raise ValueError('Task analysis and native verified-paid cohort disagree')
    identities = {name: set() for name in ('run_id', 'patient_id', 'encounter_id')}
    for case in summary['cases']:
        for record in case.get('attempts', {}).values():
            if not record.get('result_path'):
                continue
            path = (batch / record['result_path']).resolve()
            if not path.is_relative_to(batch):
                raise ValueError('Trace path leaves the frozen batch')
            trace = read(path)
            if trace.get('status') == 'completed' and any(not trace.get(name) for name in identities):
                raise ValueError('Completed trace lacks its run/patient/encounter binding')
            for name in identities:
                if trace.get(name) is not None:
                    identities[name].add(str(trace[name]))
    return specification, rows, report, identities


def compare(first, second):
    spec_a, rows_a, report_a, ids_a = first
    spec_b, rows_b, report_b, ids_b = second
    validate_shared_inputs(spec_a, spec_b)
    if any(ids_a[name] & ids_b[name] for name in ids_a):
        raise ValueError('Protocol arms reuse a run, patient or encounter business identity')
    good = lambda row: row['verified_paid'] is True and row['formal']['correct_completion'] is True
    keyed = [{(row['case_id'], row['profile']): row for row in rows} for rows in (rows_a, rows_b)]
    paired = Counter((good(keyed[0][pair]), good(keyed[1][pair])) for pair in plan_pairs(spec_a))
    result = {'version': 'separate-protocol-comparison/1',
        'source_patients': len({row['patient_key'] for row in spec_a['verified_case_metadata']}),
        'source_histories': len(spec_a['cases']), 'assignments_per_arm': len(rows_a),
        'source_cases_by_disease': dict(Counter(row['group'] for row in spec_a['cases'])),
        'business_identities_disjoint': True,
        'models': {key: spec_a['models'][key] for key in ('role_model', 'tool_models')},
        'paired_native_completion': {'both_paid': paired[True, True], 'fhir_only': paired[True, False],
                                   'wst_only': paired[False, True], 'neither_paid': paired[False, False]},
        'arms': {}, 'interpretation': 'Descriptive paired repetitions of the same source histories and clinical configurations; source patients and result denominators are not merged.'}
    for label, rows, report in [('FHIR_R4', rows_a, report_a), ('WS_T_846', rows_b, report_b)]:
        result['arms'][label] = {'batch': report['batch'], 'assigned': len(rows),
            'verified_paid': sum(good(row) for row in rows),
            'terminal_outcomes': dict(Counter(row['runtime_status'] for row in rows)),
            'by_disease': report['assignments_by_disease'], 'profiles': report['profiles'],
            'task_metrics': report['paid_cohort_metrics'], 'operation_replay': report['operation_replay'],
            'interface_contract': report['his_interface']}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fhir-run', required=True, type=Path)
    parser.add_argument('--wst-run', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    result = compare(load_arm(args.fhir_run), load_arm(args.wst_run))
    args.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    print(json.dumps({key: result[key] for key in ('source_patients', 'assignments_per_arm',
                                                'business_identities_disjoint', 'paired_native_completion')}))


if __name__ == '__main__':
    main()
