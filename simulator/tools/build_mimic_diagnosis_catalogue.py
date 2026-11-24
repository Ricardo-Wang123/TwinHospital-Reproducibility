"""Build the full source-era ICD-10-CM catalogue for an isolated OpenHIS instance."""
import argparse
import csv
import gzip
import hashlib
import io
import json
from pathlib import Path
import zipfile


VERSION = 'TH-DIAGNOSIS-4-MIMICIV22-FULL'
SYSTEM = 'ICD-10-CM'
ID_BASE = 8700000000005600000


def build(archive, existing, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as package:
        members = [n for n in package.namelist() if n.endswith('/hosp/d_icd_diagnoses.csv.gz')]
        if len(members) != 1:
            raise ValueError('Expected one source diagnosis dictionary')
        compressed = package.read(members[0])
    rows = {}
    for row in csv.DictReader(io.StringIO(gzip.decompress(compressed).decode('utf-8-sig'))):
        if row['icd_version'] != '10':
            continue
        code = row['icd_code']
        if code in rows:
            raise ValueError('Duplicate source ICD-10 code')
        rows[code] = row['long_title']
    if not rows:
        raise ValueError('Empty source ICD-10 dictionary')
    display_code = lambda c: c[:3] + '.' + c[3:] if len(c) > 3 else c
    source_codes = {display_code(c) for c in rows}
    if any(k not in source_codes or v['system'] != SYSTEM
           or k != v['code'] for k, v in existing.items()):
        raise ValueError('Existing entries must belong to the exact source vocabulary')
    mapping, additions = {}, []
    for index, (raw, title) in enumerate(sorted(rows.items())):
        code = display_code(raw)
        old = existing.get(code)
        native_id = old['conditionDefinitionId'] if old else str(ID_BASE + index + 1)
        mapping[code] = {'conditionDefinitionId': native_id, 'code': code,
            'display': title, 'system': SYSTEM, 'code_version': 'MIMIC-IV-2.2-source-era-unspecified',
            'catalogue_version': VERSION, 'source_code_raw': raw,
            'source_icd_version': '10', 'default_visible': bool(old),
            'source_release': 'MIMIC-IV 2.2', 'current_national_leaf_verified': False}
        if not old:
            additions.append((native_id, code, title))
    if len({v['conditionDefinitionId'] for v in mapping.values()}) != len(mapping):
        raise ValueError('Native definition identity collision')
    (output / 'diagnosis_mapping_v4.json').write_text(json.dumps(mapping, ensure_ascii=False) + '\n')
    quote = lambda s: "'" + str(s).replace("'", "''") + "'"
    values = ',\n'.join('(' + ','.join([i, quote(c), quote(t)]) + ')' for i, c, t in additions)
    sql = '''BEGIN;
CREATE TEMP TABLE th_dx_full(id bigint,code text,name text) ON COMMIT DROP;
'''
    if values:
        sql += 'INSERT INTO th_dx_full VALUES\n' + values + ';\n'
    sql += '''DO $$ BEGIN
 IF EXISTS (SELECT 1 FROM th_dx_full x JOIN cln_condition_definition d ON d.id=x.id
 WHERE d.condition_code IS DISTINCT FROM x.code OR d.name IS DISTINCT FROM x.name
 OR d.source_version IS DISTINCT FROM 'TH-DIAGNOSIS-4-MIMICIV22-FULL') THEN
 RAISE EXCEPTION 'Diagnosis definition ID collision; existing rows were not changed';
 END IF;
END $$;
INSERT INTO cln_condition_definition(id,tenant_id,created_at,is_deleted,version,source_enum,
 condition_code,name,status_enum,yb_flag,yb_match_flag,official_verified_flag,
 description,data_provenance,source_version)
SELECT id,1,now(),false,0,'general_dx',code,name,'ACTIVE',false,false,false,
 'MIMIC-IV 2.2 source-era ICD-10-CM term; not a patient diagnosis or current billing certification',
 'MIMICIV22_D_ICD_DIAGNOSES_VERSION10_FULL','TH-DIAGNOSIS-4-MIMICIV22-FULL'
FROM th_dx_full ON CONFLICT(id) DO NOTHING;
COMMIT;
'''
    (output / 'diagnosis_catalogue_v4.sql').write_text(sql)
    summary = {'version': VERSION, 'system': SYSTEM, 'source_release': 'MIMIC-IV 2.2',
        'source_member': members[0], 'compressed_member_sha256': hashlib.sha256(compressed).hexdigest(),
        'entries': len(mapping), 'reused_native_definitions': len(existing),
        'new_native_definitions': len(additions), 'old_rows_modified': False,
        'historical_icd9_converted': False, 'current_billability_certified': False,
        'source_patient_tables_read': False, 'native_applied': False}
    (output / 'diagnosis_catalogue_v4.json').write_text(json.dumps(summary, indent=2) + '\n')
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', required=True)
    parser.add_argument('--existing-mapping', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.archive, json.loads(Path(args.existing_mapping).read_text()), args.output)))


if __name__ == '__main__':
    main()
