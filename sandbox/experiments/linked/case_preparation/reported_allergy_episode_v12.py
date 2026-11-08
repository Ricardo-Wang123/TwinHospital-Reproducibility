"""A declared patient-report scenario, preserving the source allergy record.

This creates a new version of hypothetical encounters. It does not extract a
negative allergy status from missing MIMIC data or certify clinical normality.
"""
from copy import deepcopy
from collections import Counter
from pathlib import Path
import argparse
import json
import os

from ..cases import validate_case
from ..contracts import digest, runtime_case
from .formulary_overlay import file_hash, write

VERSION = 'constructed_patient_allergy_report_v12'
POLICY = {
    'version': VERSION,
    'scope': 'new_hypothetical_outpatient_episode_not_source_patient_reconstruction',
    'population_unchanged': True,
    'reads_execution_results': False,
    'source_history_changed': False,
    'source_unknown_promoted_to_negative': False,
    'construction': 'Author a clear no-known-drug-allergy patient report as an explicit baseline scenario assumption only where the input contains no positive or conflicting allergy cue. It is not inferred from a missing source table.',
    'positive_or_conflicting_source_evidence': 'Keep the original patient report and record unresolved provenance; do not erase an allergy-related diagnosis, source item, source-derived fact or risk cue.',
    'evidence_status': 'patient_report_not_confirmed_absence; clinicians still perform and record their own assessment',
    'clinical_decision_or_label_authored': False,
    'applicability': 'same construction for every declared disease stratum before new model execution; no selection by success',
    'interpretation': 'Results concern explicitly constructed communicative outpatient scenarios with retained MIMIC history, not the prevalence of allergy or complete original MIMIC encounters.',
    'references': [
        'https://www.nice.org.uk/guidance/cg183/chapter/Recommendations',
        'https://mimic.mit.edu/docs/iv/modules/hosp/',
    ],
}
REPORT_TEXT = '据我所知，我没有已知的药物过敏；以前没有因用药过敏被医生要求停药或接受抢救的经历。'
KNOWN_AUTHORED_INPUTS = {
    '我不记得以前用药出现过过敏反应。',
    '以前药物过敏的细节我不能确定，请帮我查已有档案；这三天没有用药后起风团或嘴唇舌头肿胀的经历。',
    '以前的过敏情况需要一起核对，我把已有记录带来了。',
    '未发现明确药物过敏史，但愿意配合核对',
    '不确定自己有没有药物过敏，过去没有明确记录',
}


def conflict_reasons(case):
    reasons = []
    record = case['his_initial'].get('allergy_record', {})
    if record.get('items'):
        reasons.append('source_allergy_items_present')
    if record.get('status') not in {None, 'unknown', 'not_supplied', 'none_known', 'no_known_allergies'}:
        reasons.append('source_allergy_status_not_unknown_or_none_known')
    for cue in case.get('evaluation', {}).get('source_risk_cues', []):
        if any('allerg' in str(reason).lower() for reason in cue.get('reasons', [])):
            reasons.append('source_allergy_related_risk_cue')
    for encounter in case['his_initial'].get('historical_encounters', []):
        for diagnosis in encounter.get('diagnoses', []):
            coding = diagnosis.get('coding') or {}
            code = str(coding.get('code', '')).upper().replace('.', '')
            display = str(diagnosis.get('display', '')).lower()
            if code.startswith(('Z88', 'V14')) or any(word in display for word in ('allerg', 'anaphyla', 'urticaria', '过敏')):
                reasons.append('source_diagnosis_requires_allergy_reconciliation')
    facts = [f for f in case['patient_initial']['patient_facts'] if f.get('topic') == 'allergy_history']
    if len(facts) != 1:
        reasons.append('allergy_memory_not_one_explicit_fact')
    elif facts[0].get('source_kind') != 'constructed':
        reasons.append('allergy_memory_is_not_an_authored_scenario_fact')
    elif (facts[0].get('value') or facts[0].get('reported_expression')) not in KNOWN_AUTHORED_INPUTS:
        reasons.append('unrecognized_or_potentially_positive_memory_preserved')
    prior_report = record.get('patient_report') or {}
    if prior_report.get('status') in {'allergy_reported', 'drug_allergy', 'confirmed'}:
        reasons.append('prior_positive_report_preserved')
    return sorted(set(reasons))


def build(source):
    validate_case(source)
    case = deepcopy(source)
    reasons = conflict_reasons(source)
    before_history = deepcopy(source['his_initial'])
    previous_facts = deepcopy(source['patient_initial']['patient_facts'])
    previous_report = deepcopy(before_history.get('allergy_record', {}).get('patient_report'))
    changed = not reasons
    if changed:
        fact = next(f for f in case['patient_initial']['patient_facts'] if f.get('topic') == 'allergy_history')
        fact.update(value=REPORT_TEXT, status='reported', source_kind='constructed',
            knowledge_state='known_self_experience', construction_version=VERSION,
            construction_basis='declared_hypothetical_patient_report_not_missing_source_imputation')
        fact.pop('reported_expression', None)
        report = {'status': 'reported_none_known', 'value': REPORT_TEXT,
            'source_kind': 'constructed', 'construction_version': VERSION,
            'as_of_offset_hours': case['his_initial'].get('time_basis', {}).get('new_visit_offset_hours'),
            'verification_status': 'patient_report_not_confirmed_absence',
            'construction_basis': 'explicit_counterfactual_episode_assumption_not_source_database_fact'}
        case['his_initial'].setdefault('allergy_record', {})['patient_report'] = report
        case['evaluation']['reference_facts'] = deepcopy(case['patient_initial']['patient_facts'])
    case['case_id'] += '_allergy12'
    case.setdefault('provenance', {})['allergy_report_scenario_v12'] = {
        'policy': deepcopy(POLICY), 'source_case_id': source['case_id'],
        'source_case_hash': digest(source), 'scenario_changed': changed,
        'preservation_reasons': reasons, 'previous_patient_report': previous_report,
        'previous_memory_facts_hash': digest(previous_facts),
        'original_source_allergy_status': before_history.get('allergy_record', {}).get('status'),
        'no_outcome_based_source_selection': True,
    }
    # Check the entire hospital view after excluding only the authored report.
    expected = deepcopy(case['his_initial'])
    if changed:
        if previous_report is None:
            expected['allergy_record'].pop('patient_report', None)
        else:
            expected['allergy_record']['patient_report'] = previous_report
    if expected != before_history:
        raise ValueError('Source history or non-allergy observation changed')
    if case['patient_key'] != source['patient_key'] or case['split'] != source['split']:
        raise ValueError('Source patient or partition changed')
    validate_case(case)
    return case, {'scenario_changed': changed, 'preservation_reasons': reasons,
                  'source_allergy_status_unchanged': True, 'source_history_unchanged': True}


def prepare(manifest_path, output):
    manifest_path, output = Path(manifest_path).resolve(), Path(output).resolve()
    if output.exists():
        raise FileExistsError('Preserve prior case versions; choose a new directory')
    manifest = json.loads(manifest_path.read_text())
    protected = {str(manifest_path): file_hash(manifest_path), str(Path(__file__)): file_hash(Path(__file__))}
    output.mkdir(parents=True, mode=0o700)
    write(output/'policy_frozen_before_cases.json', {'policy': POLICY, 'source_hashes': protected})
    rows, audits = [], []
    for row in manifest['cases']:
        path = Path(row['path'])
        protected[str(path)] = file_hash(path)
        if row.get('file_sha256', row.get('sha256')) != protected[str(path)]:
            raise ValueError('Source case hash changed')
        source = json.loads(path.read_text())
        for field in ('case_id', 'patient_key', 'data_kind', 'group', 'split'):
            if row[field] != source[field]:
                raise ValueError('Manifest/source identity differs')
        case, audit = build(source)
        target = output/'cases'/(case['case_id']+'.json')
        write(target, case)
        entry = {k: case[k] for k in ('case_id', 'patient_key', 'data_kind', 'group', 'split')}
        entry.update(path=str(target), file_sha256=file_hash(target), case_hash=digest(case),
            runtime_input_hash=digest(runtime_case(case)), source_case_id=source['case_id'],
            source_file_sha256=protected[str(path)], formal_eligible=False)
        rows.append(entry)
        audits.append({'case_id': case['case_id'], 'group': case['group'], **audit})
    if any(file_hash(path) != expected for path, expected in protected.items()):
        raise ValueError('A protected source changed during construction')
    write(output/'manifest.json', {'version': VERSION, 'cases': rows, 'case_count': len(rows),
        'policy': POLICY, 'formal_eligible': False, 'all_source_patients_retained': True,
        'selection_uses_model_outcomes': False, 'source_manifest_sha256': protected[str(manifest_path)]})
    write(output/'audit_private.json', {'cases': audits})
    write(output/'input_freeze.json', {'source_files': protected,
        'files': [{'path': row['path'], 'sha256': row['file_sha256']} for row in rows]})
    summary = {'version': VERSION, 'cases': len(rows), 'groups': dict(Counter(r['group'] for r in rows)),
        'authored_report_changed': sum(a['scenario_changed'] for a in audits),
        'conflicting_or_positive_evidence_preserved': sum(not a['scenario_changed'] for a in audits),
        'source_files_unchanged': True, 'new_source_patients': 0, 'model_calls': 0,
        'clinical_appropriateness_established': False}
    write(output/'summary.json', summary)
    return summary


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.manifest, args.output)))
