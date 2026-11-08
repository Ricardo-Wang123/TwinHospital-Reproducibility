#!/usr/bin/env python3
"""Build a versioned small ICD-10-CM vocabulary from official FY2027 files.

Reads code/description data only. Does not diagnose patients or convert historical
ICD-9 codes. Applying generated append-only SQL is deliberately a separate step.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import zipfile

HERE = Path(__file__).resolve().parent
VERSION = 'TH-DIAGNOSIS-2-ICD10CM-FY2027'
SOURCE_URL = 'https://www.cms.gov/files/zip/2027-code-descriptions-tabular-order.zip'
GROUPS = {
    'diabetes': 'E11.9 E11.21 E11.22 E11.40 E11.42 E11.65 E11.649 E11.8 E11.319'.split(),
    'heart_failure': 'I50.9 I50.20 I50.21 I50.22 I50.23 I50.30 I50.31 I50.32 I50.33 I50.40 I50.41 I50.42 I50.43 I11.0 I13.0'.split(),
    'respiratory': 'J44.9 J44.0 J44.1 J43.9 J96.11 J96.12 J18.9 J18.0 J18.1 J15.9 J12.9 J69.0'.split(),
    'urinary': 'N39.0 N30.00 N30.01 N30.90 N30.91 N10 N12'.split(),
    'visit_and_status': 'Z09 Z76.0 Z79.4 Z79.84 Z87.891 Z87.440'.split(),
    'common_related_conditions_or_symptoms': 'I10 E78.5 N18.30 N18.31 N18.32 N18.4 N18.9 N17.9 I48.91 I25.10 G47.33 R06.02 R05.9 R30.0 R35.0 R50.9'.split(),
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def q(value):
    return "'" + str(value).replace("'", "''") + "'"


def read_archive(path):
    data = Path(path).read_bytes()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        simple = [n for n in names if re.search(r'icd10cm[-_]codes[-_]2027\.txt$', n, re.I)]
        order = [n for n in names if re.search(r'icd10cm[-_]order[-_]2027\.txt$', n, re.I)]
        if len(simple) != 1 or len(order) != 1:
            raise ValueError('Expected official FY2027 codes and order text members')
        raw = z.read(simple[0]); raw_order = z.read(order[0])
    rows = {}
    for line in raw.decode('utf-8-sig').splitlines():
        match = re.fullmatch(r'([A-Z][0-9A-Z]{2,6})\s+(.+?)\s*', line)
        if not match:
            raise ValueError('Unexpected official code-description row')
        code, description = match.groups()
        if code in rows:
            raise ValueError('Duplicate official code')
        rows[code] = description
    billable = {}
    for line in raw_order.decode('utf-8-sig').splitlines():
        match = re.match(r'^\d{5}\s+([A-Z][0-9A-Z]{2,6})\s+([01])\s+', line)
        if not match:
            raise ValueError('Unexpected official tabular-order row')
        billable[match[1]] = match[2] == '1'
    return rows, billable, {'url': SOURCE_URL, 'archive_sha256': sha(data), 'archive_bytes': len(data),
        'codes_member': simple[0], 'codes_sha256': sha(raw), 'order_member': order[0],
        'order_sha256': sha(raw_order), 'coding_system': 'ICD-10-CM', 'release': 'FY2027',
        'effective_from': '2026-10-01', 'effective_to': '2027-09-30',
        'authority_page': 'https://www.cdc.gov/nchs/icd/icd-10-cm/files.html'}


def build(archive, *, output_dir=HERE):
    rows, billable, provenance = read_archive(archive)
    old_path = HERE / 'diagnosis_mapping.json'
    old_bytes = old_path.read_bytes(); old = json.loads(old_bytes)
    mapping = {}; values = []; selected = []
    for group, codes in GROUPS.items():
        for code in codes:
            bare = code.replace('.', '')
            if bare not in rows or billable.get(bare) is not True:
                raise ValueError('Selected code missing/non-billable in official FY2027 data: ' + code)
            if code in mapping:
                raise ValueError('Duplicate selected code')
            description = rows[bare]
            # Reuse the original five code rows without overwriting their names,
            # source_version or previous observations. New definitions use a new range.
            native_id = old[code]['conditionDefinitionId'] if code in old else str(8700000000002500000 + len(mapping) + 1)
            mapping[code] = {'conditionDefinitionId': native_id, 'code': code,
                'display': description, 'system': 'ICD-10-CM', 'code_version': 'FY2027',
                'catalogue_version': VERSION, 'selection_group': group, 'source': provenance,
                'native_row_reused_without_modification': code in old,
                'native_existing_display': old[code]['display'] if code in old else None,
                'patient_diagnosis_inferred': False, 'clinical_appropriateness': 'not_assessed'}
            selected.append({'code': code, 'display': description, 'system': 'ICD-10-CM',
                             'version': 'FY2027', 'group': group, 'is_leaf': True})
            if code not in old:
                values.append('(' + ','.join([native_id, q(code), q(description)]) + ')')
    if old_bytes != old_path.read_bytes():
        raise RuntimeError('Legacy dictionary changed during build')
    manifest = {'version': VERSION, 'source': provenance, 'selected_codes': selected,
        'selected_count': len(selected), 'official_leaf_code_count': len(rows),
        'old_dictionary_sha256': sha(old_bytes), 'old_five_native_rows_modified': False,
        'code_system_policy': 'New simulated decisions use explicit ICD-10-CM FY2027. Historical MIMIC codes/version and old runs are preserved; no automatic ICD-9 or WHO ICD-10 conversion.',
        'selection_policy': 'Small shared development vocabulary for five cohorts, common related conditions/symptoms and visit purposes. Historical aggregate frequencies inform coverage only; no current diagnosis is assigned.',
        'out_of_catalogue_policy': 'Explicit unknown/referral/defer; never choose a nearest unrelated code to complete billing.',
        'usage_notes': {'Z09': 'Follow-up examination after completed treatment; not a blanket label for ongoing chronic-disease management.',
            'Z76.0': 'Encounter for issue of repeat prescription; choose only when supported by this encounter.',
            'acute_conditions': 'Catalogue availability does not establish outpatient suitability or confirmed diagnosis.',
            'certainty': 'Preserve physician working/confirmed status; this experiment does not assert real coding or reimbursement eligibility.'}}
    output_dir = Path(output_dir); output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir/'diagnosis_mapping_v2.json').write_text(json.dumps(mapping, ensure_ascii=False, indent=2)+'\n')
    (output_dir/'diagnosis_metadata_v2.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+'\n')
    sql = '''-- Append only to isolated twinhospital-exp. No original codes or patient records changed.
BEGIN;
CREATE TEMP TABLE th_dx_v2(id bigint,code text,name text) ON COMMIT DROP;
INSERT INTO th_dx_v2 VALUES
''' + ',\n'.join(values) + ''';
DO $$ BEGIN
 IF EXISTS (SELECT 1 FROM th_dx_v2 x JOIN cln_condition_definition d ON d.id=x.id
 WHERE d.condition_code IS DISTINCT FROM x.code OR d.name IS DISTINCT FROM x.name
 OR d.source_version IS DISTINCT FROM 'TH-DIAGNOSIS-2-ICD10CM-FY2027') THEN
 RAISE EXCEPTION 'Experimental diagnosis ID collision; existing definitions not overwritten';
 END IF;
END $$;
INSERT INTO cln_condition_definition(id,tenant_id,created_at,is_deleted,version,source_enum,condition_code,clinic_code,name,status_enum,yb_flag,yb_match_flag,official_verified_flag,description,data_provenance,source_version)
SELECT id,1,now(),false,0,'general_dx',code,code,name,'ACTIVE',false,false,false,
'Experiment vocabulary; official ICD-10-CM FY2027 description, not a patient diagnosis or reimbursement approval',
'OFFICIAL_ICD10CM_FY2027_EXPERIMENT_SUBSET','TH-DIAGNOSIS-2-ICD10CM-FY2027'
FROM th_dx_v2 ON CONFLICT(id) DO NOTHING;
COMMIT;
'''
    (output_dir/'experiment-diagnoses-v2.sql').write_text(sql)
    return manifest


if __name__ == '__main__':
    p = argparse.ArgumentParser(); p.add_argument('--archive', type=Path, required=True)
    args = p.parse_args(); manifest = build(args.archive)
    print(json.dumps({'version': VERSION, 'selected_codes': manifest['selected_count'],
        'official_leaf_codes': manifest['official_leaf_code_count'], 'native_applied': False}))
