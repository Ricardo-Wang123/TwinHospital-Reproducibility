"""Recover source conversion gaps while retaining every selected patient.

Infection episodes choose treatment during the encounter, so conversion must
not require an old oral antibiotic. Unsupported chronic schedules remain
unresolved source material; they are never normalized to an invented schedule.
Previously generated cases, partitions and historical rows stay unchanged.
"""
import argparse
from collections import Counter
from copy import deepcopy
import json
import os
from pathlib import Path
import sqlite3

from .case_contract import _opaque, build_case, validate_package
from .closed_followup_v3 import source_seed, build_linked, medication_candidates
from .cohort_eligibility import DRUG_TERMS
from .current_episode_v10 import build as chronic
from .infection_episode_v11 import build as infection
from .adult_population import add_adult_population
from .supplemental_overlay import adapt
from .formulary_overlay import apply_formulary_overlay, file_hash, write
from .v4_source_qualification import load_frequency_context
from ..cases import validate_case
from ..contracts import digest, runtime_case

VERSION = 'source_conversion_recovery_v2'


def select_material(package, group):
    candidates, rejected = medication_candidates(package, group)
    if candidates:
        return candidates[0], False, rejected
    if group in {'pneumonia', 'uti'}:
        # v11 replaces current symptoms/use and does not adopt an old regimen.
        return None, False, rejected
    source = package['case']
    rows = []
    for row in source['clinical']['medications']['orders']:
        if row['source_encounter_ref'] != source['source_anchor_encounter_ref']:
            continue
        if row['prescription_component_type_raw'] != 'MAIN':
            continue
        if not any(term in (row['drug_name_raw'] or '').casefold() for term in DRUG_TERMS[group]):
            continue
        if row['regimen_raw'].get('route') not in {'PO', 'IH', 'INH', 'NEB'}:
            continue
        offset = row.get('time', {}).get('event_offset_hours')
        if type(offset) not in (int, float) or offset > 0:
            continue
        rows.append(row)
    if not rows:
        raise ValueError('No time-eligible explicit-route chronic material; retain as preparation failure')
    rows.sort(key=lambda r: (-r['time']['event_offset_hours'], r['record_id']))
    return rows[0], True, rejected


def initial_case(package, seed, selection, index):
    chosen, unresolved, rejected = select_material(package, selection['group'])
    case = build_linked(package, seed, selection, index, None if unresolved else chosen)
    if unresolved:
        case['provenance']['selected_historical_order'] = deepcopy(chosen)
        case['provenance']['current_medication_relation'] = (
            'Unchanged historical material with an unsupported or incomplete regimen; '
            'no current administration or normalized schedule is assigned. '
            'The chronic adapter retains its unresolved reconciliation state.')
        case['evaluation']['source_medication_candidate_ref'] = chosen['record_id']
    return case, {'closed_inventory_candidate_absent': chosen is None or unresolved,
                  'chronic_source_pattern_unresolved': unresolved,
                  'candidate_rejections': rejected}


def prepare(previous, source_folder, supplement, formulary_path, secret_path, output):
    os.umask(0o077)
    previous, source_folder, supplement, formulary_path, secret_path, output = map(
        Path, (previous, source_folder, supplement, formulary_path, secret_path, output))
    if output.exists():
        raise FileExistsError('Retain all previous inputs and recovery attempts')
    old = json.loads((previous/'manifest.json').read_text())
    old_failures = json.loads((previous/'preparation_failures_private.json').read_text())['cases']
    selected = json.loads((source_folder/'selection_private.json').read_text())
    secret = secret_path.read_bytes()
    selection_keys = [_opaque(secret, 'pt', row['subject_id']) for row in selected]
    old_rows = {row['patient_key']: row for row in old['cases']}
    failed = {row['patient_key']: row for row in old_failures}
    if (len(old_rows) != len(old['cases']) or len(failed) != len(old_failures)
            or len(selection_keys) != len(set(selection_keys)) or set(old_rows) & set(failed)
            or set(old_rows) | set(failed) != set(selection_keys)):
        raise ValueError('Selection denominator is not preserved')
    for selection, key in zip(selected, selection_keys):
        prior = old_rows.get(key) or failed[key]
        if (selection['group'], selection['split']) != (prior['group'], prior['split']):
            raise ValueError('Source partition or group changed')
    protected = {str(p.resolve()): file_hash(p) for p in (
        previous/'manifest.json', previous/'preparation_failures_private.json',
        source_folder/'selection_private.json', source_folder/'source_cache.sqlite',
        source_folder/'source_cache_complete.json', supplement/'summary.json',
        supplement/'target_anchors_private.json', formulary_path)}
    code_hashes = {str(p.resolve()): file_hash(p) for p in Path(__file__).parent.glob('*.py')}
    for row in old_rows.values():
        expected = row.get('file_sha256', row.get('sha256'))
        if not expected or file_hash(row['path']) != expected:
            raise ValueError('Previously prepared case changed')
        protected[str(Path(row['path']).resolve())] = expected
    supplement_summary = json.loads((supplement/'summary.json').read_text())
    if file_hash(supplement/'source_rows.sqlite') != supplement_summary['cache_sha256']:
        raise ValueError('Supplement source changed')
    protected[str((supplement/'source_rows.sqlite').resolve())] = supplement_summary['cache_sha256']
    targets = {(r['subject_id'], r['hadm_id']): r for r in json.loads(
        (supplement/'target_anchors_private.json').read_text())['targets']}
    formulary = json.loads(formulary_path.read_text())
    if isinstance(formulary, dict):
        formulary = formulary['items']
    cache_meta = json.loads((source_folder/'source_cache_complete.json').read_text())
    output.mkdir(parents=True, mode=0o700)
    policy = {'version': VERSION, 'formal_eligible': False, 'model_calls': 0,
        'prior_inputs_retained_byte_for_byte': len(old_rows), 'selected_patients_unchanged': True,
        'prior_failures_retained': str((previous/'preparation_failures_private.json').resolve()),
        'infection': 'A new v11 episode does not need an old oral antimicrobial regimen. All old medicines remain historical; no antimicrobial is preassigned.',
        'chronic': 'If no scalar inventory candidate exists, retain the latest time-eligible explicit-route disease-related historical row as unresolved material. The unchanged v10 parser must leave unsupported schedules unresolved; no source dose, frequency or route is edited.',
        'clinical_review': 'pending; structural conversion is not clinical qualification or successful execution',
        'source_files': protected, 'code_files': code_hashes}
    write(output/'policy_frozen_before_generation.json', policy)
    recovered, failures, audits = {}, [], []
    with sqlite3.connect('file:'+str(source_folder/'source_cache.sqlite')+'?mode=ro', uri=True) as db, \
         sqlite3.connect('file:'+str(supplement/'source_rows.sqlite')+'?mode=ro', uri=True) as supplemental:
        dictionaries = ({(c,v):json.loads(p) for c,v,p in db.execute(
            "SELECT code,version,payload FROM dictionary WHERE table_name='d_icd_diagnoses'")},
            {c:json.loads(p) for c,p in db.execute("SELECT code,payload FROM dictionary WHERE table_name='d_labitems'")})
        for index, (selection, key) in enumerate(zip(selected, selection_keys)):
            if key not in failed:
                continue
            try:
                seed, pharmacy = source_seed(db, selection, dictionaries, cache_meta)
                package = build_case(seed, selection['group'], secret)
                if validate_package(package):
                    raise ValueError('Historical package failed its contract')
                folder = output/'source_packages'/package['case']['case_id']
                for name, value in package.items():
                    write(folder/(name+'.json'), value)
                write(folder/'pharmacy_private.json', pharmacy)
                base, recovery = initial_case(package, seed, selection, index)
                base, _ = apply_formulary_overlay(base, formulary,
                    {'version': VERSION, 'formulary_sha256': protected[str(formulary_path.resolve())]})
                if selection['group'] in {'pneumonia', 'uti'}:
                    case, audit = infection(add_adult_population(base))
                else:
                    frequency, hashes, _ = load_frequency_context(base, output,
                        source_package_root=output/'source_packages')
                    protected.update(hashes)
                    case, audit = chronic(base, frequency)
                    case = add_adult_population(case)
                    if recovery['chronic_source_pattern_unresolved']:
                        core = next(item for item in case['his_initial']['current_use_record']['items']
                            if item['source_ref'] == base['provenance']['selected_historical_order']['record_id'])
                        if core.get('plan') or core.get('actual_use'):
                            raise ValueError('An unresolved regimen acquired invented administration')
                case, supplement_audit = adapt(case,
                    targets[(selection['subject_id'], selection['hadm_id'])], supplemental, secret)
                if any(case[k] != v for k,v in {'patient_key': key,
                    'group': selection['group'], 'split': selection['split']}.items()):
                    raise ValueError('Identity or partition changed')
                validate_case(case)
                path = output/'cases'/(case['case_id']+'.json')
                write(path, case)
                recovered[key] = {**{k:case[k] for k in ('case_id','patient_key','group','split','data_kind')},
                    'path': str(path.resolve()), 'file_sha256': file_hash(path), 'case_hash': digest(case),
                    'runtime_input_hash': digest(runtime_case(case)), 'formal_eligible': False}
                audits.append({'patient_key': key, 'group': selection['group'], 'split': selection['split'],
                    'recovery': recovery, 'case_audit': audit, 'supplement': supplement_audit})
            except (ValueError, KeyError, TypeError) as exc:
                failures.append({**failed[key], 'recovery_error_type': type(exc).__name__,
                    'recovery_reason': str(exc)})
    if any(file_hash(p) != h for p,h in {**protected, **code_hashes}.items()):
        raise ValueError('Source, retained case or adapter changed during recovery')
    rows = [deepcopy(old_rows.get(key) or recovered[key]) for key in selection_keys
            if key in old_rows or key in recovered]
    if len(rows)+len(failures) != len(selected):
        raise ValueError('Selection denominator lost')
    manifest = {'version': VERSION, 'cases': rows, 'case_count': len(rows),
        'selected_source_patient_count': len(selected), 'preparation_failures': len(failures),
        'formal_eligible': False, 'previous_manifest': str((previous/'manifest.json').resolve()),
        'policy': policy}
    write(output/'manifest.json', manifest)
    for split in ('development', 'test'):
        subset = [row for row in rows if row['split'] == split]
        write(output/(split+'_manifest.json'), {**manifest, 'cases': subset, 'case_count': len(subset)})
    write(output/'preparation_failures_private.json', {'cases': failures})
    write(output/'recovery_audit_private.json', {'cases': audits})
    write(output/'input_freeze.json', {'source_files': {**protected, **code_hashes},
        'files': [{'path': row['path'], 'sha256': row.get('file_sha256',row.get('sha256'))} for row in rows]})
    summary = {'selected': len(selected), 'prepared': len(rows), 'retained': len(old_rows),
        'recovered': len(recovered), 'failures': len(failures), 'formal_eligible': False,
        'clinical_qualified': None, 'model_calls': 0, 'source_hashes_unchanged': True,
        'by_partition': dict(Counter(row['split'] for row in rows)),
        'by_stratum': dict(Counter(row['split']+':'+row['group'] for row in rows))}
    write(output/'summary.json', summary)
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('previous','source-folder','supplement','formulary-path','secret-path','output'):
        parser.add_argument('--'+name, type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.previous, args.source_folder, args.supplement,
        args.formulary_path, args.secret_path, args.output)))
