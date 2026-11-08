#!/usr/bin/env python3
"""Build append-only native fixtures. Apply separately only in twinhospital-exp."""
import argparse, hashlib, json, sys
from pathlib import Path
HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE.parents[1]))
from sandbox.experiments.linked.medication_catalogue_v4 import VERSION, additions, products, formulary

QTY={'tablet':'1','capsule':'5','inhaler':'TH-INHALER','vial':'9','mL':'TH-ML'}
DOSE={'mg':'mg','puff':'TH-PUFF','inhalation':'TH-INHALATION','tablet':'TH-TABLET','capsule':'TH-CAPSULE','mL':'ml'}
ROUTE={'oral':'1','inhaled':'5'}
def q(x):return 'NULL' if x is None else "'"+str(x).replace("'","''")+"'"
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()

def build(first5=False):
    suffix='_first5' if first5 else ''
    existing=json.loads((HERE/'catalog_mapping_v3.json').read_text())
    mapping=json.loads(json.dumps(existing)); rows=[]
    all_new=additions(); ordinals={p['drug_code']:n for n,p in enumerate(all_new,1)}
    freq=existing['SIM-METFORMIN-500']['frequency_codes']
    for p in additions(first5=first5):
        code=p['drug_code'];n=ordinals[code]
        mapping[code]={
          'medicationDefinitionId':str(8700000000002700000+n),
          'quantity_unit_code':QTY[p['quantity_unit']], 'dose_unit_code':DOSE[p['dose_unit']],
          'dose_unit_codes':{u:DOSE[u] for u in p['allowed_dose_units']},
          'source_dose_unit':p['dose_unit'],'source_quantity_unit':p['quantity_unit'],
          'route_codes':{r:ROUTE[r] for r in p['allowed_routes']},'frequency_codes':freq,
          'dispenseLocationId':'322954949549363200','ingredient':p['ingredient'],'strength':p['strength'],
          'dosage_form':p['dosage_form'],'catalog_bus_no':'V4-'+code,'catalogue_version':VERSION,
          'scope':'outpatient','dose_basis':p['dose_basis'],'active_salt_mass':p['active_salt_mass'],
          'label_package':p['label_package'],'source_references':p['source_references'],
          'provenance':'new independent SIM hospital product; official label identity; source NDC unverified',
          'historical_ndc_verified':False,'clinical_suitability':'not_assessed',
          'dose_quantity_conversion':'none; numeric values and independent dispense units preserved'}
        rows.append('('+','.join(map(q,[n,'V4-'+code,p['display_name_zh']+'〔实验V4〕',p['ingredient'],p['strength'],
                  QTY[p['quantity_unit']],DOSE[p['dose_unit']],ROUTE[p['route']],p['dosage_form']]))+')')
    evidence=[]
    for name in ('catalogue_v4_antimicrobial_label_facts.json','catalogue-evidence-root-cardiometabolic-v1.json'):
        path=HERE.parents[1]/'docs/experiments'/name
        if path.exists():evidence.append({'file':name,'sha256':sha(path)})
    meta={'version':VERSION,'subset':'first5_engineering_priorities' if first5 else 'full_50_product_requirements',
          'products':products(first5=first5),'source_evidence_files':evidence,
          'previous_mapping_sha256':sha(HERE/'catalog_mapping_v3.json'),
          'source_patient_ndc_identity_verified':False,'clinical_gold':False,
          'mapping_policy':'new generic simulated hospital products; do not automatically reconcile historical NDCs, salt masses, release technology or devices',
          'price_and_stock':'constructed: 1 CNY per independently specified dispense unit, 100000 units',
          'raw_label_bytes_archived':False,'regimen_assigned':False}
    files={f'catalog_mapping_v4{suffix}.json':mapping,f'catalogue_metadata_v3{suffix}.json':meta,
           f'formulary_v4{suffix}.json':formulary(first5=first5)}
    # Never overwrite a frozen artifact with changed content.
    for name,obj in files.items():
        path=HERE/name;text=json.dumps(obj,ensure_ascii=False,indent=2)+'\n'
        if path.exists() and path.read_text()!=text:raise ValueError('Refuse changed overwrite: '+name)
        path.write_text(text)
    sql='''-- Only twinhospital-exp, append-only; never real clinical inventory.
BEGIN;
CREATE TEMP TABLE th_catalog_v4(n int,code text,name text,ingredient text,strength text,quantity_unit text,dose_unit text,route text,form text) ON COMMIT DROP;
INSERT INTO th_catalog_v4 VALUES
'''+',\n'.join(rows)+''';
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM th_catalog_v4 t JOIN med_medication_definition m ON m.id=8700000000002700000+t.n
 WHERE (m.bus_no,m.name,m.ingredient,m.total_volume,m.package_unit_code,m.dose_unit_code,m.method_code)
 IS DISTINCT FROM (t.code,t.name,t.ingredient,t.strength,t.quantity_unit,t.dose_unit,t.route))
 THEN RAISE EXCEPTION 'V4 existing product identity differs; no overwrite'; END IF;
 IF EXISTS(SELECT 1 FROM th_catalog_v4 t JOIN med_medication_definition m ON m.bus_no=t.code
 WHERE m.id<>8700000000002700000+t.n) THEN RAISE EXCEPTION 'V4 busNo collision'; END IF;
END $$;
INSERT INTO ter_concept(id,code_system_id,tenant_id,created_at,is_deleted,is_active,code,display,definition,sort_order)
VALUES
(8700000000002500001,900001,1,now(),false,true,'TH-INHALATION','吸','One metered blister inhalation; not a device or mass',9921),
(8700000000002500002,279125561775755265,1,now(),false,true,'TH-INHALATION','吸','One metered blister inhalation; not a device or mass',9922),
(8700000000002500003,279125561775755265,1,now(),false,true,'TH-ML','毫升','Independent dispense volume in mL; not dose count',9923)
ON CONFLICT(id) DO NOTHING;
INSERT INTO med_medication_definition
(id,tenant_id,created_at,is_deleted,version,bus_no,name,domain_enum,med_version,name_en,category_code,merchandise_name,
purchase_unit_code,split_unit_code,ingredient,active_flag,rx_flag,total_volume,dose_unit_code,package_unit_code,stock_unit_code,
method_code,rate_code,dose,location_id,status_enum,org_id,definition,dose_unit_ratio,purchase_unit_ratio,stock_unit_ratio,package_unit_ratio,
official_verified_flag,data_provenance,source_version,antibiotic_flag)
SELECT 8700000000002700000+n,1,now(),false,0,code,name,'WESTERN','TH-CATALOGUE-3.0',ingredient,'2',name,
quantity_unit,quantity_unit,ingredient,true,true,strength,dose_unit,quantity_unit,quantity_unit,
route,NULL,NULL,322954949549363200,'ACTIVE',322663105141149696,
'New generic SIM product; historical NDC unverified; form='||form||'; no default regimen or dose conversion',
NULL,1,1,1,false,'INDEPENDENT_SIM_LABEL_IDENTITY','TH-CATALOGUE-3.0',
ingredient IN ('levofloxacin','amoxicillin+clavulanic acid','doxycycline','ciprofloxacin','nitrofurantoin','trimethoprim+sulfamethoxazole','ampicillin')
FROM th_catalog_v4 ON CONFLICT(id) DO NOTHING;
INSERT INTO wkf_inventory_item
(id,tenant_id,created_at,is_deleted,version,category_code,item_table,item_id,name,inventory_status_enum,supplier_id,
description_text,unit_code,quantity,lot_number,production_date,expiration_date,validity_mon,location_id,packaging_levels)
SELECT 8700000000002800000+n,1,now(),false,0,'MEDICATION','med_medication_definition',8700000000002700000+n,name,'ACTIVE',4100000000000000004,
'Simulated inventory only',quantity_unit,100000,'TH-V4-SIM-LOT-'||n,current_date,current_date+interval '5 years',60,322954949549363200,1
FROM th_catalog_v4 ON CONFLICT(id) DO NOTHING;
INSERT INTO adm_charge_item_definition
(id,tenant_id,created_at,is_deleted,version,instance_table,instance_id,charge_name,status_enum,org_id,type_code,yb_type,price_type_code,lot_no,price_value,priority,official_verified_flag,data_provenance,price_source)
SELECT 8700000000002900000+n*10+t.k,1,now(),false,0,'med_medication_definition',8700000000002700000+n,name,'ACTIVE',322663105141149696,
'1001','SELF_PAY',t.kind,CASE WHEN t.kind='LOT_NUMBER_PRICE' THEN 'TH-V4-SIM-LOT-'||n END,1.00,n,false,
'INDEPENDENT_SIM_LABEL_IDENTITY','Assumed 1 CNY per dispense unit, not real pricing'
FROM th_catalog_v4 CROSS JOIN (VALUES(1,'SALE'),(2,'LOT_NUMBER_PRICE'),(3,'PURCHASE')) t(k,kind)
ON CONFLICT(id) DO NOTHING;
COMMIT;
'''
    path=HERE/f'experiment-fixtures-v4{suffix}.sql'
    if path.exists() and path.read_text()!=sql:raise ValueError('Refuse changed SQL overwrite')
    path.write_text(sql)
    return {'version':VERSION,'new_products':len(rows),'total_outpatient_products':len(mapping),'generated_only':True,
            'files':[str(p) for p in files]}

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--first5',action='store_true');a=ap.parse_args()
    print(json.dumps(build(a.first5)))
