"""Prepare index-ED cases and separate reference outcomes from a local MIMIC-IV hospital release."""
import argparse
import csv
import gzip
import hashlib
import hmac
import json
import os
import shutil
import tempfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from sandbox.experiments.linked.cases import VERSION, validate_patient_splits, write_case
from sandbox.experiments.linked.chart import HistoryChart
from handoff.ed_workup import lab_fields

def table(directory, name, subjects=None, subject_column=0):
    path = directory / (name + '.csv.gz')
    stream = gzip.open(path, 'rt', newline='') if path.exists() else (directory / (name + '.csv')).open(newline='')
    with stream:
        # Keep only selected subjects, including for CSV fields that contain embedded newlines.
        if subjects is None:
            yield from csv.DictReader(stream)
        else:
            reader = csv.reader(stream)
            header = next(reader)
            if header[subject_column] != 'subject_id':
                raise ValueError('Unexpected subject column in ' + name)
            for fields in reader:
                if fields[subject_column] in subjects:
                    yield dict(zip(header, fields, strict=True))


def prepare(cohort, hosp, identity_key, destination, native_mapping, medication_directory,
            primary_per_stratum=None):
    if len(identity_key) < 32:
        raise ValueError('Use a private identity key of at least 32 bytes.')
    def identifier(kind, value):
        prefix = {'prior': 'enc_', 'lab': 'obs_'}.get(kind, kind + '-')
        return prefix + hmac.new(identity_key, (kind + '/' + value).encode(), hashlib.sha256).hexdigest()[:24]
    def offsets(t, origin):
        return round((datetime.fromisoformat(t) - datetime.fromisoformat(origin)).total_seconds() / 3600, 5)
    indexed = {r['source_subject_id']: r for r in cohort}
    if not indexed or len(indexed) != len(cohort) or len({r['source_hadm_id'] for r in cohort}) != len(cohort):
        raise ValueError('Duplicate or missing index patient/encounter')
    people = {r['subject_id']: r for r in table(hosp, 'patients', indexed)}
    if set(people) != set(indexed):
        raise ValueError('Selected patient missing from this release')
    diagnoses = {(d['icd_version'], d['icd_code']): d['long_title'] for d in table(hosp, 'd_icd_diagnoses')}
    lab_names = {d['itemid']: d for d in table(hosp, 'd_labitems')}
    stays, histories, labs, orders, cutoffs = {}, defaultdict(list), defaultdict(list), defaultdict(list), {}
    previous_tests, previous_measurements = defaultdict(dict), defaultdict(dict)
    references, cases = {}, []
    for subject, row in indexed.items():
        start = row['encounter_evidence']['last_ed_arrival']
        end = row['cut']
        medication_rows = row['recorded_medication_orders']
        if not (row['latest_observed_ed'] and row['latest_observed_hospital_admission']
                and str(row['hospital_primary_icd']['seq_num']) == '1' and row['site'] == 'ed_to_ward'
                and row['encounter_evidence']['first_ward_time'] >= end and row['encounter_evidence']['first_ward']
                and row['flags']['all_visible_poe_verified'] and row['reference_drugs'] and medication_rows
                and row['ed_intervals'] == [[start, end]]
                and all(start <= o['time'] <= end for o in medication_rows)):
            raise ValueError('Index record does not satisfy the declared ED cohort')
        cutoffs[subject] = (start, min(o['time'] for o in medication_rows))
    for row in table(hosp, 'admissions', indexed):
        subject, hadm = row['subject_id'], row['hadm_id']
        start = cutoffs[subject][0]
        if hadm == indexed[subject]['source_hadm_id'] or not row['dischtime'] or row['dischtime'] >= start:
            continue
        record = {'encounter_ref': identifier('prior', hadm), 'status': 'finished',
                  'admission_offset_hours': offsets(row['admittime'], start),
                  'discharge_offset_hours': offsets(row['dischtime'], start),
                  'source_kind': 'MIMIC-IV hosp.admissions', 'diagnoses': []}
        stays[subject, hadm] = record
        histories[subject].append(record)
    for row in table(hosp, 'diagnoses_icd', indexed):
        stay = stays.get((row['subject_id'], row['hadm_id']))
        if stay is not None:
            version, code = row['icd_version'], row['icd_code']
            stay['diagnoses'].append({'coding': {'system': 'ICD-10-CM' if version == '10' else 'ICD-9-CM', 'code': code},
                'display': diagnoses.get((version, code), code), 'source_sequence': row['seq_num'],
                'source_kind': 'MIMIC-IV hosp.diagnoses_icd', 'source_encounter_ref': stay['encounter_ref']})
    for row in table(hosp, 'prescriptions', indexed):
        subject = row['subject_id']
        stay = stays.get((subject, row['hadm_id']))
        if stay and row['drug'] and row['starttime'] and row['starttime'] < cutoffs[subject][0]:
            orders[subject].append({'drug_name_raw': row['drug'], 'source_encounter_ref': stay['encounter_ref'],
                'time': {'event_offset_hours': offsets(row['starttime'], cutoffs[subject][0])},
                'source_kind': 'MIMIC-IV hosp.prescriptions', 'current_use_inferred': False})
    for row in table(hosp, 'labevents', indexed, 1):
        subject = row['subject_id']
        start, stop = cutoffs[subject]
        collection, available = row['charttime'], row['storetime']
        if not (collection and available and collection <= available):
            continue
        values = lab_fields(row)
        if values is None:
            continue
        historical = available < start and row['hadm_id'] != indexed[subject]['source_hadm_id']
        current = (row['hadm_id'] == indexed[subject]['source_hadm_id'] and start <= collection and available < stop)
        if not (historical or current):
            continue
        key = row['itemid']
        if historical and key in previous_tests[subject] and previous_tests[subject][key][0] >= (available, row['labevent_id']):
            continue
        item = lab_names.get(key, {})
        record = {'record_id': identifier('lab', row['labevent_id']), 'name': item.get('label', key),
            'display': item.get('label', key), **values,
            'unit_raw': row['valueuom'], 'fluid': item.get('fluid'), 'time': {'event_offset_hours': offsets(collection, start),
            'recorded_offset_hours': offsets(available, start)}, 'source_kind': 'MIMIC-IV hosp.labevents'}
        if historical:
            previous_tests[subject][key] = ((available, row['labevent_id']), record)
        else:
            labs[subject].append({**record, 'available_at': 'physician_consultation'})
    for row in table(hosp, 'omr', indexed):
        subject, date, name = row['subject_id'], row['chartdate'], row['result_name']
        start = cutoffs[subject][0]
        rank = (date, int(row['seq_num']))
        if not date or date >= start[:10] or not row['result_value']:
            continue
        if name not in previous_measurements[subject] or rank > previous_measurements[subject][name][0]:
            previous_measurements[subject][name] = (rank, {'name': name, 'value_raw': row['result_value'],
                'display': name, 'source_kind': 'MIMIC-IV hosp.omr', 'time': {'event_precision': 'day',
                'event_offset_lower_hours': offsets(date, start), 'event_offset_upper_hours': offsets(date, start) + 24}})
    for subject, row in indexed.items():
        start = cutoffs[subject][0]
        person = people[subject]
        for stay in histories[subject]:
            stay['diagnoses'].sort(key=lambda item: int(item['source_sequence']) if item['source_sequence'] else float('inf'))
        demographics = {'sex': person['gender'], 'age': int(person['anchor_age']) + int(start[:4]) - int(person['anchor_year']),
                        'age_status': 'year-based deidentified MIMIC anchor age'}
        if int(person['anchor_age']) == 91:
            demographics.pop('age')
            demographics['age_status'] = 'top-coded older adult; exact age unknown'
            demographics['age_population'] = {'category': 'older_adult', 'minimum_age_years': 90 + int(start[:4]) - int(person['anchor_year']),
                                               'exact_age_claim': False}
        cid = identifier('ed', row['source_hadm_id'])
        chart = {'demographics': demographics, 'time_basis': {'history_offset_origin': 'index_ED_arrival', 'new_visit_offset_hours': 0},
            'historical_encounters': sorted(histories[subject], key=lambda h: h['admission_offset_hours']),
            'historical_orders': orders[subject],
            'historical_observations': [r for _, r in previous_tests[subject].values()],
            'historical_outpatient_measurements': [r for _, r in previous_measurements[subject].values()],
            'current_observations': sorted(labs[subject], key=lambda x: x['time']['recorded_offset_hours']),
            'allergy_record': {'status': 'unknown', 'items': []}, 'current_use_record': {'status': 'unknown', 'items': []}}
        cases.append({'schema_version': VERSION, 'case_id': cid, 'patient_key': identifier('patient', subject),
            'encounter_setting': 'emergency', 'group': row['disease'], 'split': 'test', 'data_kind': 'mimic_derived',
            'patient_initial': {'demographics': demographics, 'patient_facts': [], 'interview_source': 'unrecorded'}, 'his_initial': chart, 'formulary': []})
        references[cid] = {'drug_names': sorted({d['match_name'] for d in row['reference_drugs']}),
                           'disposition': 'admit', 'source_order_count': len(row['recorded_medication_orders']),
                           'source_subject_id': subject, 'source_hadm_id': row['source_hadm_id'],
                           'hospital_primary_icd': row['hospital_primary_icd'], 'ed_start': start, 'ed_end': row['cut'],
                           'first_ward_time': row['encounter_evidence']['first_ward_time']}
    return write_dataset(cases, references, destination, native_mapping, medication_directory, primary_per_stratum)


def write_dataset(cases, references, destination, native_mapping, medication_directory, primary_per_stratum=None):
    from .emergency_results import catalogue_coverage
    from .mimic_formulary import fixture
    from sandbox.experiments.linked.contracts import digest, runtime_case
    from sandbox.experiments.linked.formulary_search import FILENAME, FormularyDirectory

    directory = FormularyDirectory(json.loads(medication_directory.read_bytes()))
    directory.require_mimic_products()
    if directory.mapping_hash != digest(native_mapping) or set(directory.products) != set(native_mapping):
        raise ValueError('Shared orderable products and the OpenHIS binding differ')
    product_fixture = fixture(directory.products.values(), native_mapping)

    validate_patient_splits(cases)
    if destination.exists() or not cases:
        raise ValueError('A nonempty cohort and fresh output directory are required.')
    groups = defaultdict(list)
    for case in cases:
        groups[case['group']].append(case)
    if primary_per_stratum is not None and (primary_per_stratum < 1 or any(
            len(rows) < primary_per_stratum for rows in groups.values())):
        raise ValueError('Each selected stratum must supply the requested number of distinct patients.')
    primary, reserve = [], []
    for group in sorted(groups):
        ranked = sorted(groups[group], key=lambda c: digest(['index_ed_stratified_order_v1', c['patient_key']]))
        stop = primary_per_stratum if primary_per_stratum is not None else len(ranked)
        primary.extend(ranked[:stop])
        reserve.extend(ranked[stop:])

    def write_partition(folder, selected):
        records, summaries, outcomes = [], {}, {}
        for case in selected:
            case = dict(case, formulary=[], formulary_directory=directory.descriptor)
            cid = case['case_id']
            path = Path('cases') / (cid + '.json')
            write_case(folder / path, case)
            records.append({**{k: case[k] for k in ('case_id','patient_key','group','split','data_kind')}, 'path': str(path)})
            summaries[cid] = HistoryChart(case['his_initial']).initial_view()['opening_summary']
            outcomes[cid] = dict(references[cid], runtime_input_hash=digest(runtime_case(case)))
        coverage = catalogue_coverage(outcomes, directory.products.values())
        shutil.copyfile(medication_directory, folder / FILENAME)
        (folder / FILENAME).chmod(0o600)
        (folder / 'products.sql').write_text(product_fixture)
        (folder / 'products.sql').chmod(0o600)
        for name, payload in [('manifest.json', {'cases': records}), ('opening_summaries.private.json', summaries),
                              ('reference.private.json', outcomes), ('catalog.json', native_mapping),
                              ('catalogue_coverage.private.json', coverage)]:
            (folder / name).write_text(json.dumps(payload, ensure_ascii=False, indent=None if name == 'catalog.json' else 2) + chr(10))
            (folder / name).chmod(0o600)

    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as tmp:
        build = Path(tmp) / 'prepared'
        write_partition(build, primary)
        if reserve:
            write_partition(build / 'reserve', reserve)
        build.rename(destination)
    return {'primary': len(primary), 'reserve': len(reserve),
            'primary_with_prior_admission': sum(bool(c['his_initial']['historical_encounters']) for c in primary),
            'primary_with_preorder_ed_labs': sum(bool(c['his_initial']['current_observations']) for c in primary),
            'primary_with_source_patient_report': sum(bool(c['patient_initial']['patient_facts']) for c in primary)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('cohort','hosp','identity-key','output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--medication-directory', type=Path, required=True,
                        help='Directory produced by handoff.mimic_formulary for indexed prescribing')
    parser.add_argument('--primary-per-stratum', type=int, help='Fixed number of primary study patients per disease; keep the remainder in reserve')
    args = parser.parse_args()
    with args.cohort.open() as rows:
        cohort = [json.loads(row) for row in rows if row.strip()]
    os.umask(0o077)
    from sandbox.experiments.linked.formulary_search import FILENAME
    directory_file = args.medication_directory / FILENAME
    mapping = json.loads((args.medication_directory / 'catalog.json').read_bytes())
    print(json.dumps(prepare(cohort, args.hosp, args.identity_key.read_bytes(), args.output, mapping,
                             directory_file, args.primary_per_stratum)))


if __name__ == '__main__':
    main()
