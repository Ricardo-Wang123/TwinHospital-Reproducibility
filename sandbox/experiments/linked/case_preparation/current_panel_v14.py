"""Explicit counterfactual current blood panel over immutable MIMIC history.

This defines simulation inputs, not new observations of source patients. Values
are selected by specimen/analyte/time before any new run, never by normality or
the desired prescription. Old cases and all prior outcomes remain separate.
"""
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import argparse
import json
import math
import os
from pathlib import Path

from .formulary_overlay import file_hash, write
from ..cases import validate_case
from ..chart import HistoryChart
from ..chart_projection import project_history_page, verify_projection
from ..contracts import digest, runtime_case

VERSION = 'counterfactual_current_blood_panel_v14'
# Fasting status is not inferred. No eGFR is calculated from a censored age.
ANALYTES = {
    'creatinine': ({'Creatinine', 'Creatinine, Whole Blood'}, 'mg/dL'),
    'potassium': ({'Potassium', 'Potassium, Whole Blood'}, 'mEq/L'),
    'glucose': ({'Glucose', 'Glucose, Whole Blood'}, 'mg/dL'),
    'sodium': ({'Sodium', 'Sodium, Whole Blood'}, 'mEq/L'),
    'urea_nitrogen': ({'Urea Nitrogen'}, 'mg/dL'),
    'hemoglobin_a1c': ({'% Hemoglobin A1c'}, '%'),
}
POLICY = {
    'version': VERSION, 'formal_eligible': False,
    'development_feedback_informed_scenario_revision': True,
    'all_source_patients_retained': True, 'reads_model_outputs': False,
    'construction': 'The new virtual visit has a defined blood panel at consultation entry. Each newly authored value equals the latest eligible source blood result. This is an explicit counterfactual constant-value assumption, not evidence that the real patient remained unchanged or that a new test actually happened.',
    'selection': 'Exact analyte label, Blood specimen and unchanged unit; finite numeric value; event and recording times known and available by the source cutoff. Latest event time, then recording time, then record identifier. No threshold or normality selection.',
    'unknowns': 'Missing source result stays missing. No invented normal values, inferred fasting status, calculated eGFR, negative allergy or treatment response.',
    'existing_current_findings': 'Never overwrite a previously defined current finding. Conflicting existing duplicate names require inspection.',
    'history': 'All original hospital history and its dates remain identical. New rows have new IDs, constructed source_kind, original-source links and explicit simulation times.',
    'visibility': 'Hospital physician view at consultation entry; not patient memory or a preconsultation answer. All paired conditions receive the same frozen scenario.',
    'outcomes': 'No diagnosis, prescription, reviewer approval, signature, payment, or clinical error label is assigned. Abnormal values can still require additional work.',
    'references': ['https://mimic.mit.edu/docs/iv/modules/hosp/labevents.html',
                   'https://mimic.mit.edu/docs/iv/modules/hosp/d_labitems.html'],
}


def finite(value):
    if isinstance(value, bool): return None
    try: number = float(value)
    except (TypeError, ValueError): return None
    return number if math.isfinite(number) else None


def source_result(his, labels, unit):
    candidates = []
    for row in his.get('historical_observations', []):
        if row.get('display') not in labels or row.get('fluid') != 'Blood' or row.get('unit_raw') != unit:
            continue
        time = row.get('time', {})
        event, recorded = finite(time.get('event_offset_hours')), finite(time.get('recorded_offset_hours'))
        value = finite(row.get('numeric_value_raw'))
        if (event is None or recorded is None or value is None or not event <= recorded <= 0
                or time.get('available_by_source_cutoff') is not True):
            continue
        candidates.append((event, recorded, str(row['record_id']), row))
    return max(candidates, key=lambda r:r[:3])[-1] if candidates else None


def build(source):
    validate_case(source)
    case = deepcopy(source)
    his = case['his_initial']
    visit = finite(his.get('time_basis', {}).get('new_visit_offset_hours'))
    if visit is None or visit <= 0:
        raise ValueError('Explicit hypothetical new-visit time after the historical cutoff is required')
    observations = his.setdefault('current_observations', [])
    existing = Counter(o.get('name') for o in observations)
    if any(existing[name] > 1 for name in ANALYTES):
        raise ValueError('Duplicate existing panel analytes require inspection')
    added, missing, retained = [], [], []
    for name, (labels, unit) in ANALYTES.items():
        if existing[name]:
            retained.append(name)
            continue
        raw = source_result(his, labels, unit)
        if raw is None:
            missing.append(name)
            continue
        row = {'record_id':'obs_'+digest({'source_case':source['case_id'],'version':VERSION,'name':name})[:24],
            'source_ref':raw['record_id'],'name':name,'fluid':'Blood',
            'value':finite(raw['numeric_value_raw']),'unit':unit,
            'source_kind':'constructed','status':'simulated_new_visit_observation',
            'available_at':'physician_consultation',
            'construction_basis':'Explicit counterfactual constant-value assumption; not an observed new MIMIC result. Fasting status is not inferred.',
            'time':{'basis':'source_relative_to_anchor_discharge','event_offset_hours':visit,
                'recorded_offset_hours':visit,'event_time_known':True,'available_by_source_cutoff':False,
                'availability_basis':'constructed_new_episode_not_a_source_measurement'}}
        observations.append(row)
        added.append({'name':name,'new_record_ref':row['record_id'],'source_record_ref':raw['record_id'],
            'source_event_time':deepcopy(raw['time']), 'source_value_raw':raw.get('value_raw'),
            'source_numeric_value_raw':raw['numeric_value_raw'],'source_unit':unit,'source_fluid':'Blood',
            'new_value':row['value'],'source_value_preserved':True})
    if len(observations) > 20:
        raise ValueError('Panel exceeds the consultation-entry record budget; do not silently truncate')
    case['case_id'] += '_panel14'
    case.setdefault('provenance', {})['current_panel_v14'] = {'policy':deepcopy(POLICY),
        'parent_case_id':source['case_id'],'parent_case_hash':digest(source),
        'added':added,'missing_not_imputed':missing,'existing_findings_unchanged':retained}
    for key, value in source['his_initial'].items():
        if key != 'current_observations' and his.get(key) != value:
            raise ValueError('Historical or current-use source state changed')
    if case['patient_initial'] != source['patient_initial'] or case['evaluation'] != source['evaluation']:
        raise ValueError('Patient memory or evaluation metadata changed')
    validate_case(case)
    chart=HistoryChart(his);page=chart.query('current_observations',limit=20)
    if not verify_projection(page,project_history_page(page)):
        raise ValueError('Panel projection lost clinical values')
    return case, {'added_count':len(added),'missing':missing,'retained_existing':retained,
        'source_history_unchanged':True,'patient_memory_unchanged':True,'prescription_preassigned':False}


def prepare(manifest_path, output):
    manifest_path, output=Path(manifest_path),Path(output)
    if output.exists(): raise FileExistsError('Choose a new version; existing cases are immutable')
    manifest= json.loads(manifest_path.read_text())
    output.mkdir(parents=True,mode=0o700)
    protected={str(manifest_path):file_hash(manifest_path),str(Path(__file__)):file_hash(__file__)}
    write(output/'policy_frozen_before_cases.json',{'frozen_at':datetime.now(timezone.utc).isoformat(),
        'policy':POLICY,'source_hashes':deepcopy(protected)})
    rows, audits=[],[]
    for entry in manifest['cases']:
        path=Path(entry['path']);raw_hash=file_hash(path)
        if raw_hash!=entry.get('file_sha256',entry.get('sha256')):raise ValueError('Source file changed')
        protected[str(path)]=raw_hash
        source=json.loads(path.read_text())
        if source['split']!='development' or source['data_kind']!='mimic_derived':
            raise ValueError('This engineering preparation accepts only MIMIC-derived development cases')
        case,audit=build(source);target=output/'cases'/(case['case_id']+'.json');write(target,case)
        rows.append({**{k:case[k]for k in ('case_id','patient_key','group','split','data_kind')},
            'path':str(target.resolve()),'file_sha256':file_hash(target),'case_hash':digest(case),
            'runtime_input_hash':digest(runtime_case(case)),'formal_eligible':False,
            'source_case_id':source['case_id'],'source_file_sha256':raw_hash})
        audits.append({'case_id':case['case_id'],'group':case['group'],**audit})
    if any(file_hash(p)!=h for p,h in protected.items()):raise ValueError('Input changed during preparation')
    write(output/'manifest.json',{'version':VERSION,'cases':rows,'case_count':len(rows),
        'formal_eligible':False,'policy':POLICY})
    write(output/'input_audit.json',{'cases':audits,'source_hashes':protected,'clinical_outputs_read':False})
    summary={'version':VERSION,'patients':len(rows),'groups':dict(Counter(r['group']for r in rows)),
        'constructed_panel_rows_added':sum(a['added_count']for a in audits),
        'missing_analytes':dict(Counter(x for a in audits for x in a['missing'])),
        'source_files_changed':False,'patient_memory_changed':False,'model_calls':0,
        'new_current_values_are_observed_MIMIC_facts':False,'execution_started':False}
    write(output/'summary.json',summary)
    return summary


if __name__=='__main__':
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args();print(json.dumps(prepare(args.manifest,args.output)))
