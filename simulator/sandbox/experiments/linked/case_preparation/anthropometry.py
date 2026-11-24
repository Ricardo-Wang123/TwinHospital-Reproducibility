"""Author explicit body measurements for a counterfactual outpatient scenario.

This preparation step never runs inside a clinical decision. It preserves the
source history and patient-memory view and does not inspect drugs, laboratory
values, model responses, or experiment outcomes.
"""
import argparse
from copy import deepcopy
from hashlib import sha256
import json
import math
import os
from pathlib import Path

VERSION = 'counterfactual_baseline_anthropometry_v1'
REFERENCE_URL = 'https://stacks.cdc.gov/view/cdc/174595/cdc_174595_DS1.pdf'
# NHANES August 2021–August 2023, adult 20+ medians, Tables 3 and 7.
# These are scenario defaults, never estimates of a missing patient's true value.
REFERENCE = {'M': {'weight': 86.6, 'height': 175.2},
             'F': {'weight': 73.5, 'height': 161.2}}
UNITS = {'weight': 'kg', 'height': 'cm'}


def positive(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def history_time(row):
    time = row.get('time', {})
    event = time.get('event_offset_upper_hours', time.get('event_offset_hours'))
    recorded = time.get('recorded_offset_upper_hours', time.get('recorded_offset_hours'))
    if (time.get('available_by_source_cutoff') is not True
            or type(event) not in (int, float) or type(recorded) not in (int, float)
            or not math.isfinite(event) or not math.isfinite(recorded)
            or event > 24 or recorded > 24):
        return None
    return event, recorded, str(row.get('record_id', ''))


def historical_candidates(case, kind):
    labels = {'weight': 'Weight (Lbs)', 'height': 'Height (Inches)', 'bmi': 'BMI (kg/m2)'}
    scale = {'weight': 0.45359237, 'height': 2.54, 'bmi': 1}
    rows = []
    for row in case['his_initial'].get('historical_outpatient_measurements', []):
        when, number = history_time(row), positive(row.get('value_raw'))
        if row.get('display') == labels[kind] and when is not None and number is not None:
            rows.append((when, number * scale[kind], row))
    return sorted(rows, key=lambda item: item[0])


def historical_basis(case, kind):
    rows = historical_candidates(case, kind)
    if rows:
        _, value, row = rows[-1]
        return value, 'counterfactual_initialized_from_historical_omr', [row]
    if kind == 'height':
        # A same-date weight/BMI pair can supply a calculated scenario height.
        # It remains a construction, not a measured height recovered from MIMIC.
        bmi = {when[:2]: (value, row) for when, value, row in historical_candidates(case, 'bmi')}
        for when, weight, row in reversed(historical_candidates(case, 'weight')):
            if when[:2] in bmi:
                index, bmi_row = bmi[when[:2]]
                return math.sqrt(weight / index) * 100, 'counterfactual_height_from_same_date_weight_and_bmi', [row, bmi_row]
    return None


def current_values(case):
    values = {}
    aliases = {'weight': 'weight', 'body_weight': 'weight', 'weight_kg': 'weight',
               'height': 'height', 'body_height': 'height', 'height_cm': 'height'}
    for row in case['his_initial'].get('current_observations', []):
        name = aliases.get(str(row.get('name', '')).strip().lower())
        if name is None:
            continue
        value = positive(row.get('value'))
        if row.get('unit') != UNITS[name] or value is None or name in values:
            raise ValueError('Current anthropometry needs one finite positive value in kg/cm per field')
        values[name] = value
    return values


def author(case, *, allow_reference_scenario=False, parent_file_sha256=None):
    original = deepcopy(case)
    result = deepcopy(case)
    existing = current_values(case)
    current = case['his_initial'].get('current_observations', [])
    times = [row.get('time') for row in current
             if row.get('available_at') == 'physician_consultation' and isinstance(row.get('time'), dict)]
    if not times:
        raise ValueError('An explicitly timed current-visit scenario is required')
    additions = []
    for kind in UNITS:
        if kind in existing:
            continue
        basis = historical_basis(case, kind)
        if basis is None:
            demographics = case['his_initial']['demographics']
            age, sex = demographics.get('age'), demographics.get('sex')
            if (not allow_reference_scenario or sex not in REFERENCE
                    or type(age) not in (int, float) or not math.isfinite(age) or age < 20):
                raise ValueError('Author the missing body measurement explicitly; no eligible historical/default basis')
            basis = REFERENCE[sex][kind], 'independently_authored_adult_reference_scenario', []
        value, method, sources = basis
        value = round(value, 1)
        identity = json.dumps([VERSION, case['case_id'], kind, value, UNITS[kind]], separators=(',', ':'))
        row = {'record_id': 'obs_' + sha256(identity.encode()).hexdigest()[:24],
               'name': kind, 'value': value, 'unit': UNITS[kind],
               'source_kind': 'constructed', 'status': 'simulated_new_visit_observation',
               'available_at': 'physician_consultation', 'construction_basis': method,
               'time': deepcopy(times[0])}
        if sources:
            row['source_ref'] = sources[0]['record_id']
        evidence = {'field': kind, 'method': method, 'value': value, 'unit': UNITS[kind],
                    'source_record_ids': [source['record_id'] for source in sources],
                    'source_is_current_patient_measurement': False}
        if not sources:
            evidence['reference_url'] = REFERENCE_URL
            evidence['reference_scope'] = 'adult 20+ sex-specific population median; constructed scenario, not individual imputation'
        result['his_initial']['current_observations'].append(row)
        additions.append(evidence)
    if additions:
        result.setdefault('provenance', {})['baseline_anthropometry'] = {
            'version': VERSION, 'parent_file_sha256': parent_file_sha256,
            'generator_module': 'sandbox.experiments.linked.case_preparation.anthropometry',
            'generator_sha256': sha256(Path(__file__).read_bytes()).hexdigest(),
            'status': 'authored_before_execution', 'fields': additions,
            'clinical_outcomes_used_for_values': False,
            'history_and_patient_memory_changed': False,
            'meaning': 'Hypothetical current findings. Neither a recovered source measurement nor a claim about the actual post-discharge course.'}
    if result['patient_initial'] != original['patient_initial']:
        raise AssertionError('Patient memory changed during scenario authoring')
    for name, value in original['his_initial'].items():
        if name != 'current_observations' and result['his_initial'][name] != value:
            raise AssertionError('Source chart changed during scenario authoring')
    return result, additions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--author-reference-baseline', action='store_true',
                        help='Explicitly author missing values from adult reference medians, with construction provenance.')
    args = parser.parse_args()
    os.umask(0o077)
    target = args.output.resolve()
    if target.is_relative_to(Path(__file__).resolve().parents[4]):
        raise ValueError('Keep authored patient-derived cases outside the source checkout')
    if target.exists():
        raise FileExistsError(target)
    target.mkdir(parents=True, mode=0o700)
    (target / 'cases').mkdir()
    source = json.loads(args.manifest.read_bytes())
    manifest, reports = [], []
    for number, row in enumerate(source['cases'], 1):
        path = Path(row['path'])
        path = path if path.is_absolute() else args.manifest.parent / path
        raw = path.read_bytes()
        case, changes = author(json.loads(raw), allow_reference_scenario=args.author_reference_baseline,
                               parent_file_sha256=sha256(raw).hexdigest())
        name = f'cases/{number:04}.json'
        (target / name).write_text(json.dumps(case, ensure_ascii=False, indent=2) + '\n')
        manifest.append({key: case[key] for key in ('case_id', 'patient_key', 'group', 'split', 'data_kind')})
        manifest[-1]['path'] = name
        reports.append({'case_id': case['case_id'], 'fields': changes})
    (target / 'manifest.json').write_text(json.dumps({'cases': manifest}, indent=2) + '\n')
    (target / 'construction_report.json').write_text(json.dumps({'version': VERSION, 'cases': reports}, indent=2) + '\n')
    print(json.dumps({'cases': len(manifest), 'authored_fields': sum(len(row['fields']) for row in reports), 'model_calls': 0}))


if __name__ == '__main__':
    main()
