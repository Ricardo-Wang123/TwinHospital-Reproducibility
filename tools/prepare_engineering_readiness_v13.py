"""Version the pilot's input-readiness rule without selecting by model outcome.

Only source-case manifests are read. Original attempted patients and their run
denominators are retained separately; this is an engineering revision, not a
claim that previously unfinished encounters succeeded.
"""
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sandbox.experiments.linked.cases import GROUPS, validate_case
from sandbox.experiments.linked.contracts import digest, runtime_case
from sandbox.experiments.linked.case_preparation.cohort_eligibility import DRUG_TERMS
from sandbox.experiments.linked.case_preparation.current_episode_v10 import PRODUCTS
from sandbox.experiments.linked.case_preparation.reported_allergy_episode_v12 import build as add_allergy_report
from sandbox.experiments.linked.case_preparation.formulary_overlay import file_hash, write

VERSION = 'engineering_current_medication_readiness_v13'
CHRONIC = {'type2_diabetes', 'heart_failure', 'copd'}
POLICY = {
    'version': VERSION, 'development_feedback_informed_revision': True,
    'formal_eligible': False, 'reads_model_results': False,
    'chronic_requirement': 'At least one disease-related scheduled item with an explicitly authored prior plan and a stated dose, unit, route and strength. No requirement is imposed on the observed model decision or laboratory value.',
    'infection_requirement': 'Preserve the separately authored new infection episode; a prior chronic regimen is not required.',
    'replacement': 'Retain every eligible original patient, including those with unsuccessful runs. Fill only source-input-ineligible slots from unused development patients by a fixed identity hash; exclude all original pilot patients and all held-out test patients.',
    'chief_complaint': 'Mention medication supply already recorded as at most one day; no new remaining quantity, dose, test, diagnosis or approval is created.',
    'product_identity': 'Finite exact name/strength authoring rules describe products in the hypothetical current episode; they do not establish historical manufacturer/NDC identity or treatment appropriateness.',
    'history': 'Source hospital records and dates are byte-equivalent as JSON values. Original incomplete cases and runs remain available.',
    'completion': 'No prescription, signature or payment is supplied by this builder. An actual clinician-role decision may still be unfinished.',
}
SALT = 'TH-engineering-ready13-development-only-20261107'
PRODUCT_RULES = {**PRODUCTS,
    ('fluticasone-salmeterol diskus (500/50)', '500/50mcg diskus'):
        'SIM-FLUTICASONE-SALMETEROL-DISKUS-500-50'}


def number(value):
    try:
        n = Decimal(str(value))
        return n if n.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def screen(case):
    if case['group'] not in CHRONIC:
        return {'eligible': True, 'reason': 'separate_infection_episode', 'supporting_items': []}
    relevant = []
    for item in case['his_initial'].get('current_use_record', {}).get('items', []):
        name = item.get('name', '').lower()
        if not item.get('plan') or not any(term in name for term in DRUG_TERMS[case['group']]):
            continue
        dose = number(item.get('dose'))
        if dose is not None and dose > 0 and all(item.get(k) for k in ('strength', 'dose_unit', 'route', 'frequency')):
            relevant.append(item['record_id'])
    return {'eligible': bool(relevant), 'reason': 'declared_disease_related_prior_regimen' if relevant else
            'no_fully_stated_disease_related_prior_regimen', 'supporting_items': relevant}


def transform(source, formulary):
    validate_case(source)
    case = deepcopy(source)
    if 'allergy_report_scenario_v12' not in case.get('provenance', {}):
        case, _ = add_allergy_report(case)
    before_his = deepcopy(case['his_initial'])
    before_formulary = {p['drug_code']: p for p in case['formulary']}
    new_formulary = {p['drug_code']: p for p in formulary}
    if any(new_formulary.get(code) != p for code, p in before_formulary.items()):
        raise ValueError('Existing formulary product changed')
    case['formulary'] = deepcopy(formulary)
    supply, product_assignments = [], []
    if case['group'] in CHRONIC:
        for item in case['his_initial'].get('current_use_record', {}).get('items', []):
            if not item.get('plan'):
                continue
            key = (item.get('name', '').strip().casefold(), item.get('strength', '').strip().casefold())
            code = PRODUCT_RULES.get(key)
            if code and not item.get('current_product'):
                product = new_formulary[code]
                item['current_product'] = {k: deepcopy(product[k]) for k in
                    ('drug_code','name','ingredient','dosage_form','strength','route','dose_unit','quantity_unit')}
                item['current_product'].update(source_kind='constructed', construction_version=VERSION,
                    identity_basis='explicit_new_episode_product_assignment_not_source_NDC_match',
                    historical_exact_identity='not_established')
                package = product.get('label_package') or {}
                device = package.get('device') or package.get('device_required')
                if code == 'SIM-TIOTROPIUM-18':
                    device = 'HandiHaler'
                if device:
                    item['current_product']['device'] = device
                if product.get('label_indication_scope'):
                    item['current_product']['label_indication_scope'] = deepcopy(product['label_indication_scope'])
                product_assignments.append({'record_id': item['record_id'], 'drug_code': code})
            remaining = number(item.get('supply', {}).get('remaining_dose_equivalents'))
            daily = number(item.get('planned_daily_dose_count'))
            if remaining is not None and daily is not None and daily > 0 and 0 <= remaining <= daily:
                supply.append({'record_id': item['record_id'], 'remaining': str(remaining), 'daily': str(daily)})
        if supply:
            facts = case['patient_initial']['patient_facts']
            chief = next(f for f in facts if f['topic'] == 'chief_complaint')
            chief['value'] += ' 我带来的用药记录里，有药剩余量只够约一天或更少，想请医生评估是否需要续方或调整。'
            chief.update(source_kind='constructed', construction_version=VERSION,
                         source_refs=sorted(set(chief.get('source_refs', [])) | {s['record_id'] for s in supply}))
            chief['construction_basis'] = 'salience_of_existing_declared_supply_not_new_clinical_information'
        case['evaluation']['reference_facts'] = deepcopy(case['patient_initial']['patient_facts'])
    # The only hospital-view edits are current fictional product identities.
    for key, value in before_his.items():
        if key != 'current_use_record' and case['his_initial'].get(key) != value:
            raise ValueError('Hospital history changed')
    check = deepcopy(case['his_initial'].get('current_use_record'))
    original = before_his.get('current_use_record')
    if check and original:
        for i, item in enumerate(check['items']):
            if 'current_product' not in original['items'][i]:
                item.pop('current_product', None)
        if check != original:
            raise ValueError('Current regimen or supply was altered')
    case['case_id'] += '_ready13'
    case.setdefault('provenance', {})['readiness_v13'] = {
        'policy': deepcopy(POLICY), 'source_case_id': source['case_id'], 'source_case_hash': digest(source),
        'supply_mentions': supply, 'current_product_assignments': product_assignments,
        'clinical_outcome_preassigned': False}
    validate_case(case)
    return case


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for key in ('source', 'reserve', 'heldout', 'formulary', 'output'):
        p.add_argument('--'+key, type=Path, required=True)
    a = p.parse_args()
    if a.output.exists():
        raise FileExistsError('Preserve earlier case versions')
    a.output.mkdir(parents=True, mode=0o700)
    frozen = {'frozen_at': datetime.now(timezone.utc).isoformat(), 'policy': POLICY, 'selection_salt': SALT,
              'input_hashes': {str(x): file_hash(x) for x in [a.source, a.reserve, a.heldout, a.formulary, Path(__file__)]}}
    write(a.output/'policy_frozen_before_selection.json', frozen)
    source = json.loads(a.source.read_text())['cases']
    reserve = json.loads(a.reserve.read_text())['cases']
    heldout = {r['patient_key'] for r in json.loads(a.heldout.read_text())['cases']}
    original_keys = {r['patient_key'] for r in source}
    protected = deepcopy(frozen['input_hashes'])
    source_cases, selection, exclusions = {}, [], []
    def load(row):
        path = Path(row['path']); raw = path.read_bytes(); h = hashlib.sha256(raw).hexdigest()
        if h != row.get('file_sha256', row.get('sha256')):
            raise ValueError('Input case hash changed')
        c = json.loads(raw)
        if any(c[k] != row[k] for k in ('case_id','patient_key','group','split','data_kind')):
            raise ValueError('Input identity mismatch')
        if c['split'] != 'development' or c['data_kind'] != 'mimic_derived' or c['patient_key'] in heldout:
            raise ValueError('Only disjoint MIMIC-derived development patients are allowed')
        protected[str(path)] = h
        return c
    for row in source:
        c = load(row); report = screen(c)
        if report['eligible']:
            selection.append(row); source_cases[row['patient_key']] = c
        else:
            exclusions.append({**{k: row[k] for k in ('case_id','patient_key','group')}, **report,
                               'original_files_and_outcomes_preserved': True})
    chosen = {r['patient_key'] for r in selection}
    replacements = []
    for group in GROUPS:
        needed = 10 - sum(r['group'] == group for r in selection)
        candidates = sorted((r for r in reserve if r['group'] == group and r['patient_key'] not in original_keys | heldout),
                            key=lambda r: hashlib.sha256((SALT+'|'+r['patient_key']).encode()).hexdigest())
        for row in candidates:
            if not needed:
                break
            c = load(row)
            if row['patient_key'] not in chosen and screen(c)['eligible']:
                selection.append(row); chosen.add(row['patient_key']); source_cases[row['patient_key']] = c
                replacements.append({k: row[k] for k in ('case_id','patient_key','group')}); needed -= 1
        if needed:
            raise ValueError('Insufficient source-ready development patients')
    assert len(selection) == len(chosen) == 50
    # Interleave strata without ordering on symptoms, tests or model results.
    grouped = {g: sorted((r for r in selection if r['group'] == g), key=lambda r: r['patient_key']) for g in GROUPS}
    rows = []
    formulary = json.loads(a.formulary.read_text())
    for i in range(10):
        for group in GROUPS:
            row = grouped[group][i]; c = transform(source_cases[row['patient_key']], formulary)
            target = a.output/'cases'/(c['case_id']+'.json'); write(target, c)
            rows.append({**{k:c[k] for k in ('case_id','patient_key','group','split','data_kind')},
                         'path':str(target.resolve()),'file_sha256':file_hash(target),'case_hash':digest(c),
                         'runtime_input_hash':digest(runtime_case(c)),'formal_eligible':False,
                         'source_case_id':row['case_id'],'source_file_sha256':row.get('file_sha256',row.get('sha256'))})
    if any(file_hash(path) != h for path,h in protected.items()):
        raise ValueError('Source changed during construction')
    write(a.output/'manifest.json', {'version':VERSION,'cases':rows,'case_count':50,'policy':POLICY,'formal_eligible':False})
    write(a.output/'selection_audit_private.json', {'original_planned_count':50,'source_ineligible':exclusions,
         'replacement_patients':replacements,'original_eligible_retained':50-len(exclusions),
         'heldout_overlap':0,'model_result_files_read':0,'source_hashes':protected})
    summary = {'version':VERSION,'cases':50,'groups':dict(Counter(r['group'] for r in rows)),
        'source_ineligible':len(exclusions),'replacements':len(replacements),'source_history_changed':False,
        'source_files_unchanged':True,'model_calls':0,'clinical_appropriateness_established':False}
    write(a.output/'summary.json',summary); print(json.dumps(summary))


if __name__ == '__main__':
    main()
