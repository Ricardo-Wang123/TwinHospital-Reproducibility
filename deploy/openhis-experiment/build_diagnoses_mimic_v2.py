#!/usr/bin/env python3
"""Build the engineering vocabulary from the authorized local MIMIC dictionary.

This version is explicitly source-era, not FY2027-certified or a current billing
vocabulary. No diagnoses_icd/patients/admissions data is read by this builder.
"""
import argparse
import csv
import gzip
import hashlib
import io
import json
from pathlib import Path
import zipfile
from build_diagnoses_v2 import GROUPS, q, sha, HERE

VERSION = 'TH-DIAGNOSIS-2-MIMICIV22-SOURCE'


def file_sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(4*1024*1024), b''):
            h.update(block)
    return h.hexdigest()


def read_dictionary(archive):
    with zipfile.ZipFile(archive) as z:
        names=[n for n in z.namelist() if n.endswith('/hosp/d_icd_diagnoses.csv.gz')]
        if len(names)!=1:
            raise ValueError('Expected one MIMIC hosp/d_icd_diagnoses.csv.gz member')
        compressed=z.read(names[0]); raw=gzip.decompress(compressed)
    rows={}
    for row in csv.DictReader(io.StringIO(raw.decode('utf-8-sig'))):
        if row['icd_version']!='10':
            continue
        if row['icd_code'] in rows:
            raise ValueError('Duplicate ICD10 dictionary code')
        rows[row['icd_code']]=row['long_title']
    return rows, {'source_vocabulary':'MIMIC-IV 2.2 hosp/d_icd_diagnoses, icd_version=10',
        'source_release':'MIMIC-IV 2.2', 'source_member':names[0],
        'archive_bytes':Path(archive).stat().st_size, 'archive_sha256':file_sha(archive),
        'compressed_member_sha256':sha(compressed),'csv_sha256':sha(raw),
        'documentation_url':'https://mimic.mit.edu/docs/iv/modules/hosp/d_icd_diagnoses.html',
        'dataset_url':'https://physionet.org/content/mimiciv/2.2/',
        'code_year':'not_independently_verified', 'current_billability':'not_assessed',
        'terminal_code_check':'Exact code exists and has no longer prefix-descendant in this complete supplied dictionary; not a certification against the current national tabular list.'}


def build(archive, *, output_dir=HERE):
    rows,source=read_dictionary(archive)
    old_path=HERE/'diagnosis_mapping.json';old_bytes=old_path.read_bytes();old=json.loads(old_bytes)
    groups={k:list(v) for k,v in GROUPS.items()}
    # Explicit source-era alternatives; these are not implicit mappings from
    # missing modern codes. They are independently selected legacy concepts.
    groups['source_era_related_conditions_or_symptoms']=['N18.3','R05']
    mapping={};selected=[];pending=[];values=[]
    for group,codes in groups.items():
        for code in codes:
            bare=code.replace('.','')
            exists=bare in rows
            terminal=exists and not any(k!=bare and k.startswith(bare) for k in rows)
            if not exists or not terminal:
                pending.append({'code':code,'group':group,'reason':'absent_from_source_dictionary' if not exists else 'source_dictionary_has_descendant_codes','substitution_performed':False})
                continue
            if code in mapping:
                raise ValueError('Duplicate code selection')
            display=rows[bare]
            native_id=old[code]['conditionDefinitionId'] if code in old else str(8700000000002600000+len(mapping)+1)
            mapping[code]={'conditionDefinitionId':native_id,'code':code,'display':display,
                'system':'ICD-10-CM','code_version':'MIMIC-IV-2.2-source-era-unspecified',
                'catalogue_version':VERSION,'source_code_raw':bare,'source_icd_version':'10',
                'source_dictionary_description':display,'source_dictionary_terminal':True,
                'current_national_leaf_verified':False,'source':source,'selection_group':group,
                'native_row_reused_without_modification':code in old,
                'native_existing_display':old[code]['display'] if code in old else None,
                'clinical_appropriateness':'not_assessed','patient_diagnosis_inferred':False}
            selected.append({'code':code,'source_code_raw':bare,'display':display,'group':group,
                'source_dictionary_terminal':True,'current_national_leaf_verified':False})
            if code not in old:
                values.append('('+','.join([native_id,q(code),q(display)])+')')
    if set(old)-set(mapping):
        raise ValueError('Legacy five codes not all verified in source dictionary')
    report={'version':VERSION,'selected_count':len(mapping),'pending_count':len(pending),
        'source':source,'dictionary_icd10_entries':len(rows),'selected':selected,'pending':pending,
        'old_five_native_rows_modified':False,'old_dictionary_sha256':sha(old_bytes),
        'scope':'Engineering simulation vocabulary from the actual MIMIC source dictionary, not a currently approved hospital charging vocabulary.',
        'no_automatic_diagnosis':True,'no_icd9_conversion':True,'no_hidden_cohort_label_for_physician':True,
        'no_nearest_code_fallback':True,
        'official_FY2027_download':{'status':'not_completed','reason':'CMS and CDC HTTPS connection failed in curl/urllib and Safari; no certificate bypass.',
            'cms_url':'https://www.cms.gov/files/zip/2027-code-descriptions-tabular-order.zip',
            'cdc_url':'https://ftp.cdc.gov/pub/health_statistics/nchs/publications/ICD10CM/2027/icd10cm-code-descriptions-2027.zip',
            'unused_builder':'build_diagnoses_v2.py'},
        'official_usage_notes':{'url':'https://www.cms.gov/files/document/fy-2027-icd-10-cm-coding-guidelines.pdf',
            'Z09':{'section':'I.C.21.c.8','meaning':'Surveillance after completed treatment; not a blanket code for ongoing chronic-disease management.'},
            'Z76.0':{'section':'I.C.21.c.','meaning':'Encounter for issue of repeat prescription; choose only if supported by this encounter.'}},
        'known_limitations':['No verified code year in MIMIC dictionary','Terminal in supplied dictionary does not establish modern billability',
            'Catalogue includes acute diagnoses without asserting outpatient suitability','All diagnostic selections must come from actual physician decision and supporting encounter evidence']}
    output_dir=Path(output_dir);output_dir.mkdir(exist_ok=True,parents=True)
    (output_dir/'diagnosis_mapping_v2.json').write_text(json.dumps(mapping,ensure_ascii=False,indent=2)+'\n')
    (output_dir/'diagnosis_metadata_v2.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    sql='''-- Generated from source vocabulary; apply only to twinhospital-exp.
-- Old five definitions, all patient records and all previous runs remain unchanged.
BEGIN;
CREATE TEMP TABLE th_dx_mimic_v2(id bigint,code text,name text) ON COMMIT DROP;
INSERT INTO th_dx_mimic_v2 VALUES
'''+',\n'.join(values)+''';
DO $$ BEGIN
 IF EXISTS (SELECT 1 FROM th_dx_mimic_v2 x JOIN cln_condition_definition d ON d.id=x.id
 WHERE d.condition_code IS DISTINCT FROM x.code OR d.name IS DISTINCT FROM x.name
 OR d.source_version IS DISTINCT FROM 'TH-DIAGNOSIS-2-MIMICIV22-SOURCE') THEN
 RAISE EXCEPTION 'Experimental diagnosis ID collision; existing definitions not overwritten';
 END IF;
END $$;
INSERT INTO cln_condition_definition(id,tenant_id,created_at,is_deleted,version,source_enum,condition_code,clinic_code,name,status_enum,yb_flag,yb_match_flag,official_verified_flag,description,data_provenance,source_version)
SELECT id,1,now(),false,0,'general_dx',code,code,name,'ACTIVE',false,false,false,
'MIMIC-IV 2.2 source dictionary term; code year/current billability unverified; not a patient diagnosis',
'MIMICIV22_D_ICD_DIAGNOSES_VERSION10_SOURCE_SUBSET','TH-DIAGNOSIS-2-MIMICIV22-SOURCE'
FROM th_dx_mimic_v2 ON CONFLICT(id) DO NOTHING;
COMMIT;
'''
    (output_dir/'experiment-diagnoses-v2.sql').write_text(sql)
    if old_path.read_bytes()!=old_bytes:
        raise RuntimeError('Legacy mapping unexpectedly changed')
    return report


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--archive',type=Path,required=True)
    args=p.parse_args();r=build(args.archive)
    print(json.dumps({'version':VERSION,'selected':r['selected_count'],'pending':r['pending_count'],
        'native_applied':False,'current_national_leaf_verified':False}))
