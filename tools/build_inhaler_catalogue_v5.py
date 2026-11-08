"""Append a label-verified inhaler strength; never alter existing catalogue rows."""
from copy import deepcopy
import argparse
import hashlib
import json
from pathlib import Path

URL='https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=4eeb5f6a-593f-4a9e-9692-adefa2caf8fc'
OLD='SIM-FLUTICASONE-SALMETEROL-DISKUS-250-50'
NEW='SIM-FLUTICASONE-SALMETEROL-DISKUS-500-50'
VERSION='TH-CATALOGUE-3.1'


def write(path,value):
    content=value if isinstance(value,str)else json.dumps(value,ensure_ascii=False,indent=2)+'\n'
    with path.open('x')as f:f.write(content)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--deployment-dir',type=Path,required=True)
    a=p.parse_args();root=a.deployment_dir
    mapping=json.loads((root/'catalog_mapping_v4.json').read_text())
    metadata=json.loads((root/'catalogue_metadata_v3.json').read_text())
    formulary=json.loads((root/'formulary_v4.json').read_text())
    product=deepcopy(next(x for x in metadata['products']if x['drug_code']==OLD))
    def strength_strings(obj):
        if isinstance(obj,dict):return {k:strength_strings(v)for k,v in obj.items()}
        if isinstance(obj,list):return [strength_strings(v)for v in obj]
        if isinstance(obj,str):return obj.replace('250-50','500-50').replace('250/50','500/50').replace('250 mcg','500 mcg')
        return obj
    product=strength_strings(product);product['components'][0]['strength_value']=500
    product['catalogue_version']=VERSION
    product['label_indication_scope']={
        'basis':'DailyMed label revised March 2026, sections 1.2, 2.1, 2.2, 3 and 16',
        'asthma_strength':'500/50 is a labelled asthma strength; individual suitability is not assigned',
        'copd_strength':'The US label identifies 250/50 twice daily as the only approved COPD dosage; 500/50 is not automatically a COPD regimen',
        'acute_rescue':'Not indicated for relief of acute bronchospasm',
        'severe_milk_protein_hypersensitivity':'Labelled contraindication; do not infer absence from missing data'}
    product['notes'].append('Catalogue availability is not a recommendation to continue a historical 500/50 regimen.')
    item=strength_strings(deepcopy(mapping[OLD]));item.update(medicationDefinitionId='8700000000003700001',
        catalog_bus_no='V5-'+NEW,catalogue_version=VERSION)
    mapping[NEW]=item
    f=strength_strings(deepcopy(next(x for x in formulary if x['drug_code']==OLD)))
    f['catalogue_version']=VERSION;f['label_indication_scope']=deepcopy(product['label_indication_scope'])
    if 'components'in f:f['components'][0]['strength_value']=500
    formulary.append(f);metadata['products'].append(product);metadata['version']=VERSION
    metadata['subset']='37 retained products plus the 500/50 strength observed in actual current-history material'
    metadata['previous_mapping_sha256']=hashlib.sha256((root/'catalog_mapping_v4.json').read_bytes()).hexdigest()
    metadata['v5_identity_evidence']={'url':URL,'retrieved_on':'2026-11-07','label_updated':'2026-03-26',
        'sections':['1.2','2.1','2.2','3','16'],'strength_mcg':[500,50],
        'salmeterol_xinafoate_mcg':72.5,'blisters_per_device':60,'source_patient_ndc_match':False,
        'catalogue_exists_is_not_clinical_approval':True}
    original=(root/'experiment-fixtures-v4.sql').read_text()
    tail=original[original.index('DO $$ BEGIN'):]
    for old,new in [('th_catalog_v4','th_catalog_v5'),('8700000000002700000','8700000000003700000'),
                    ('8700000000002800000','8700000000003800000'),('8700000000002900000','8700000000003900000'),
                    ('TH-CATALOGUE-3.0',VERSION),('V4','V5')]:tail=tail.replace(old,new)
    def q(value):return "'"+str(value).replace("'","''")+"'"
    values=[1,'V5-'+NEW,product['display_name_zh']+'〔实验V5〕',product['ingredient'],product['strength'],
            'TH-INHALER','TH-INHALATION','5',product['dosage_form']]
    sql='-- Append-only, isolated twinhospital-exp; no clinical regimen assigned.\nBEGIN;\n'
    sql+='CREATE TEMP TABLE th_catalog_v5(n int,code text,name text,ingredient text,strength text,quantity_unit text,dose_unit text,route text,form text) ON COMMIT DROP;\n'
    sql+='INSERT INTO th_catalog_v5 VALUES ('+','.join(q(v)for v in values)+');\n'+tail
    for name,data in [('catalog_mapping_v5.json',mapping),('catalogue_metadata_v4.json',metadata),
                      ('formulary_v5.json',formulary),('experiment-fixtures-v5.sql',sql)]:write(root/name,data)
    print(json.dumps({'generated_only':True,'native_mutations':0,'products':len(formulary),'added_code':NEW}))


if __name__=='__main__':main()
