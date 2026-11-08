"""Read-only, offline preparation of the ACL cohort and classification index.

Does not call a model, mutate a source case, relabel clinical outcomes, or read
experiment outputs. Public summaries contain counts and catalogue metadata only.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sandbox.experiments.linked.cases import GROUPS, validate_case
from sandbox.experiments.linked.contracts import digest, runtime_case

VERSION = 'acl-input-preparation-v1'
ATC = {
    'metformin': ('A10BA02', '双胍类'),
    'empagliflozin': ('A10BK03', 'SGLT2 抑制剂'),
    'furosemide': ('C03CA01', '袢利尿剂'),
    'spironolactone': ('C03DA01', '醛固酮拮抗剂'),
    'lisinopril': ('C09AA03', 'ACE 抑制剂'),
    'albuterol': ('R03AC02', '吸入选择性 β2 受体激动剂'),
    'tiotropium': ('R03BB04', '吸入抗胆碱药'),
    'prednisone': ('H02AB07', '全身用糖皮质激素'),
    'amoxicillin': ('J01CA04', '广谱青霉素类'),
    'azithromycin': ('J01FA10', '大环内酯类'),
    'nitrofurantoin': ('J01XE01', '硝基呋喃衍生物'),
    'trimethoprim+sulfamethoxazole': ('J01EE01', '磺胺与甲氧苄啶复方'),
    'warfarin': ('B01AA03', '维生素 K 拮抗剂'),
    'acetaminophen': ('N02BE01', '苯胺类解热镇痛药'),
    'metoprolol': ('C07AB02', '选择性 β 受体阻滞剂'),
    'fluticasone propionate+salmeterol': ('R03AK06', '长效 β2 激动剂与糖皮质激素复方'),
    'levofloxacin': ('J01MA12', '氟喹诺酮类'),
    'ipratropium': ('R03BB01', '吸入抗胆碱药'),
    'ipratropium+albuterol': ('R03AL02', 'β2 激动剂与抗胆碱药复方'),
    'losartan': ('C09CA01', '血管紧张素 II 受体拮抗剂'),
    'glipizide': ('A10BB07', '磺脲类'),
    'amoxicillin+clavulanic acid': ('J01CR02', '青霉素与 β 内酰胺酶抑制剂复方'),
    'doxycycline': ('J01AA02', '四环素类'),
    'ciprofloxacin': ('J01MA02', '氟喹诺酮类'),
    'ampicillin': ('J01CA01', '广谱青霉素类'),
    'sacubitril+valsartan': ('C09DX04', '血管紧张素受体与脑啡肽酶联合抑制剂'),
    'ceftriaxone': ('J01DD04', '第三代头孢菌素'),
}


def read(path):
    return json.loads(Path(path).read_text())


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def save(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with Path(path).open('x') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')


def classify_product(product, source_codes):
    ingredient = product.get('ingredient')
    # Exact normalized catalogue ingredient; no fuzzy matching of patient drugs.
    if ingredient not in ATC:
        return None
    code, label = ATC[ingredient]
    if code not in source_codes:
        raise ValueError('ATC source was not retrieved: ' + code)
    return {k: product.get(k) for k in ('drug_code', 'ingredient', 'dosage_form', 'strength', 'allowed_routes')} | {
        'atc_code': code, 'atc_class_code': code[:5], 'atc_class_zh': label,
        'reference_url': 'https://atcddd.fhi.no/atc_ddd_index/?code=' + code,
        'scope': 'substance classification; no dose or patient-suitability inference',
        'ddd_used_as_prescription_dose': False,
    }


def build_safety_schedule(held):
    """Freeze 25 disease/operation cells, each with 8 faults and 32 controls."""
    if len(held) != 2000 or len({r['patient_key'] for r in held}) != 2000:
        raise ValueError('Expected the same 2000 distinct formal-test patients')
    if any(r['split'] != 'test' for r in held):
        raise ValueError('Engineering cases cannot enter the formal operation schedule')
    chosen = []
    error_types = ('patient_record_mismatch','missing_encounter_link','invalid_note_state',
                   'unauthorized_signing','duplicate_transaction')
    for group in GROUPS:
        rows = [x for x in held if x['group'] == group]
        if len(rows) != 400:
            raise ValueError('Expected 400 formal source patients per disease')
        selected = sorted(rows, key=lambda x: hashlib.sha256(('app3-local-v1:'+x['case_id']).encode()).hexdigest())[:200]
        for category_index, category in enumerate(error_types):
            for index, row in enumerate(selected[category_index*40:(category_index+1)*40]):
                chosen.append({**row, 'local_test_id':'local-'+hashlib.sha256((category+':'+row['case_id']).encode()).hexdigest()[:24],
                    'operation_category':category,'request_condition':'single_fault' if index<8 else 'compliant_control',
                    'source_operation_binding':'bind to actual recorded operation and preserved checkpoint after complete-run execution',
                    'missing_source_operation_policy':'retain unresolved test; no outcome-based replacement'})
    return chosen


def prepare(source_manifest, synthetic_manifest, catalogue, taxonomy, atc_sources, output):
    output = Path(output)
    if output.exists():
        raise FileExistsError('Use a new versioned output; never replace a prepared index')
    protected = {str(Path(p).resolve()): sha(p) for p in
                 (source_manifest, synthetic_manifest, catalogue, taxonomy, atc_sources)}
    source, synthetic, tax = read(source_manifest), read(synthetic_manifest), read(taxonomy)
    source_codes = {x['code'] for x in read(atc_sources)['sources'] if x.get('rows')}
    products = {}
    for p in read(catalogue)['products']:
        code = p['drug_code']
        if code in products:
            raise ValueError('Duplicate catalogue identity')
        products[code] = classify_product(p, source_codes)
    counts, history, candidates, unmatched = Counter(), defaultdict(Counter), Counter(), Counter()
    partitions, case_ids, private_rows, failures, seen_formulary = {}, set(), [], [], {}
    entries = [(x, 'mimic_derived') for x in source['cases']] + [(x, 'synthetic_independent') for x in synthetic['cases']]
    for index, (entry, expected_kind) in enumerate(entries):
        path = Path(entry['path'])
        if sha(path) != entry['file_sha256']:
            raise ValueError('Frozen case digest mismatch; no case content was printed')
        case = read(path)
        validate_case(case)
        for key in ('case_id', 'patient_key', 'group', 'split', 'data_kind'):
            if case[key] != entry[key]:
                raise ValueError('Manifest identity disagrees with case')
        if case['data_kind'] != expected_kind or case['group'] not in GROUPS:
            raise ValueError('Provenance or group mismatch')
        if entry.get('runtime_input_hash') and digest(runtime_case(case)) != entry['runtime_input_hash']:
            raise ValueError('Runtime input digest mismatch')
        if case['case_id'] in case_ids or case['patient_key'] in partitions:
            raise ValueError('Repeated patient/case across planned cohort')
        partitions[case['patient_key']] = case['split']
        case_ids.add(case['case_id'])
        group, split = case['group'], case['split']
        system = tax['source_subtypes'][group]['system']
        counts[(expected_kind, split, group, system)] += 1
        h = case['his_initial']
        for key in ('historical_encounters', 'historical_orders', 'historical_observations',
                    'historical_administrations', 'historical_microbiology', 'historical_outpatient_measurements',
                    'current_observations'):
            history[(expected_kind, split, group)][key + '_present'] += bool(h.get(key))
        history[(expected_kind, split, group)]['allergy_unknown'] += h.get('allergy_record', {}).get('status') == 'unknown'
        history[(expected_kind, split, group)]['current_use_unreconciled'] += h.get('current_use_record', {}).get('status') == 'not_reconciled'
        for p in case['formulary']:
            code = p['drug_code']
            identity = {k: p.get(k) for k in ('ingredient', 'dosage_form', 'strength', 'allowed_routes')}
            if code in seen_formulary and seen_formulary[code] != identity:
                raise ValueError('Same catalogue code has inconsistent identities')
            seen_formulary[code] = identity
            if code not in products or products[code] is None:
                products[code] = classify_product(p, source_codes)
            if products[code] is None:
                unmatched[code] += 1
        candidate = case.get('provenance', {}).get('new_current_product_code')
        if candidate:
            candidates[(group, candidate)] += 1
        forbidden = {'evaluation', 'provenance'} & set(runtime_case(case))
        if forbidden:
            raise ValueError('Evaluation material leaked into runtime root')
        private_rows.append({**{k: case[k] for k in ('case_id', 'patient_key', 'group', 'split', 'data_kind')},
            'system': system, 'path': str(path), 'file_sha256': entry['file_sha256'],
            'runtime_input_hash': digest(runtime_case(case)), 'candidate_product_code': candidate,
            'structural_checks': 'passed', 'external_human_assessment_required': False,
            'whole_encounter_completion': 'requires_execution', 'clinical_appropriateness': 'not_assessed'})
        if (index + 1) % 250 == 0:
            print(json.dumps({'checked_cases': index + 1, 'planned_cases': len(entries)}), flush=True)
    for group in GROUPS:
        system = tax['source_subtypes'][group]['system']
        for kind, split, n in [('mimic_derived', 'development', 100), ('mimic_derived', 'test', 400),
                               ('synthetic_independent', 'development', 100)]:
            if counts[(kind, split, group, system)] != n:
                raise ValueError('Prepared count does not match the authorized design')
    if any(sha(path) != value for path, value in protected.items()):
        raise ValueError('A protected source changed during preparation')
    summary = {'version': VERSION, 'model_calls': 0, 'native_writes': 0, 'source_cases_mutated': False,
        'structurally_checked': len(entries), 'distinct_patients': len(partitions), 'structural_failures': len(failures),
        'cross_partition_patient_overlap': 0, 'external_human_assessment_required': False,
        'source_manifests_sha256': protected,
        'counts': [{'data_kind': k, 'split': s, 'group': g, 'system': y, 'count': n} for (k,s,g,y),n in counts.items()],
        'availability': [{'data_kind': k, 'split': s, 'group': g, **v} for (k,s,g),v in history.items()],
        'candidate_products': [{'group':g,'drug_code':c,'count':n} for (g,c),n in sorted(candidates.items())],
        'catalogue_products': len(products), 'atc_classified_products': sum(v is not None for v in products.values()),
        'unmapped_catalogue_products': dict(unmatched),
        'scope': 'Preparation and provenance only; no model outcome or clinical efficacy was assessed',
        'independent_constructed_templates': synthetic.get('independent_scenario_templates'),
        'formal_results_available': False}
    output.mkdir(parents=True, mode=0o700)
    save(output/'private_case_index.json', {'version': VERSION, 'cases': private_rows})
    save(output/'summary.json', summary)
    save(output/'disease_taxonomy.json', tax)
    save(output/'medication_classification.json', {'version':VERSION,'source_manifest_sha256':sha(atc_sources),
        'classification_only':True,'drug_products':[v for v in products.values() if v]})
    # APP III stability reuses all 2,000 complete encounters. Only the local
    # operation tests select a subset; they never create a new patient cohort.
    held = [x for x in private_rows if x['split'] == 'test']
    save(output/'formal_test2000_manifest.json', {'version':VERSION,'case_count':len(held),'cases':held,
        'usage':'Shared APP I, APP II and APP III full-flow source cohort; one unique source patient per case'})
    chosen = build_safety_schedule(held)
    save(output/'app3_local1000_manifest.json', {'version':VERSION,'case_count':len(chosen),'cases':chosen,
        'selection':'200 per disease; 5 operation categories x (8 faulty + 32 compliant); hash rank before outcomes',
        'usage':'Local operation tests from the same 2000 patients; full-flow stability reuses their original complete runs'})
    return summary


if __name__ == '__main__':
    os.umask(0o077)
    p = argparse.ArgumentParser(description=__doc__)
    for key in ('source-manifest','synthetic-manifest','catalogue','taxonomy','atc-sources','output'):
        p.add_argument('--'+key, type=Path, required=True)
    a=p.parse_args()
    value=prepare(a.source_manifest,a.synthetic_manifest,a.catalogue,a.taxonomy,a.atc_sources,a.output)
    print(json.dumps({k:value[k] for k in ('version','structurally_checked','structural_failures',
        'catalogue_products','atc_classified_products','unmapped_catalogue_products')},ensure_ascii=False),flush=True)
