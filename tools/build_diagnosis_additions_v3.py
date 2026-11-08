"""Append source-verified electrolyte/volume terms to the simulation vocabulary."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'deploy/openhis-experiment'))
from build_diagnoses_mimic_v2 import read_dictionary
from build_diagnoses_v2 import q

VERSION='TH-DIAGNOSIS-3-MIMICIV22-SOURCE'
CODES=('E87.0','E87.1','E87.2','E87.3','E87.5','E87.6','E86.0','E86.1','E86.9')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--archive',type=Path,required=True)
    p.add_argument('--deployment-dir',type=Path,required=True)
    a=p.parse_args();root=a.deployment_dir
    old=json.loads((root/'diagnosis_mapping_v2.json').read_text())
    mapping=deepcopy(old);dictionary,source=read_dictionary(a.archive)
    if source['archive_sha256']!=next(iter(old.values()))['source']['archive_sha256']:
        raise ValueError('Source archive differs from the verified earlier dictionary')
    added=[];pending=[];values=[]
    for code in CODES:
        if code in mapping:continue
        bare=code.replace('.','')
        if bare not in dictionary or any(k!=bare and k.startswith(bare) for k in dictionary):
            pending.append(code);continue
        row=deepcopy(next(iter(old.values())))
        row.update(conditionDefinitionId=str(8700000000004600000+len(added)+1),code=code,
            display=dictionary[bare],source_code_raw=bare,source_dictionary_description=dictionary[bare],
            catalogue_version=VERSION,selection_group='electrolyte_and_volume_conditions',
            native_row_reused_without_modification=False,native_existing_display=None,source=source)
        mapping[code]=row;added.append(code)
        values.append('('+','.join([row['conditionDefinitionId'],q(code),q(row['display'])])+')')
    sql="""-- Append only to isolated twinhospital-exp; source-era dictionary, not a patient diagnosis.
BEGIN;
CREATE TEMP TABLE th_dx_v3(id bigint,code text,name text) ON COMMIT DROP;
INSERT INTO th_dx_v3 VALUES
"""+',\n'.join(values)+""";
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM th_dx_v3 x JOIN cln_condition_definition d ON d.id=x.id
 WHERE (d.condition_code,d.name,d.source_version) IS DISTINCT FROM (x.code,x.name,'TH-DIAGNOSIS-3-MIMICIV22-SOURCE'))
 THEN RAISE EXCEPTION 'Diagnosis ID collision; no overwrite'; END IF;
END $$;
INSERT INTO cln_condition_definition(id,tenant_id,created_at,is_deleted,version,source_enum,condition_code,clinic_code,name,status_enum,yb_flag,yb_match_flag,official_verified_flag,description,data_provenance,source_version)
SELECT id,1,now(),false,0,'general_dx',code,code,name,'ACTIVE',false,false,false,
'MIMIC-IV 2.2 dictionary; code year/current billability unverified; not a patient diagnosis',
'MIMICIV22_D_ICD_DIAGNOSES_VERSION10_SOURCE_SUBSET','TH-DIAGNOSIS-3-MIMICIV22-SOURCE'
FROM th_dx_v3 ON CONFLICT(id) DO NOTHING;
COMMIT;
"""
    report={'version':VERSION,'old_count':len(old),'total_count':len(mapping),'added_codes':added,
            'unavailable_or_nonterminal_not_substituted':pending,'source':source,
            'old_rows_unchanged':all(mapping[k]==v for k,v in old.items()),
            'current_national_leaf_verified':False,'native_applied':False}
    for name,value in [('diagnosis_mapping_v3.json',mapping),('diagnosis_metadata_v3.json',report),
                       ('experiment-diagnoses-v3.sql',sql)]:
        with (root/name).open('x')as f:f.write(value if isinstance(value,str)else json.dumps(value,indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items()if k!='source'}))


if __name__=='__main__':main()
