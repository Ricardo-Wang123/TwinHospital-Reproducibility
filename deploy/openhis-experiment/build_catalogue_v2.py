#!/usr/bin/env python3
"""Generate reviewed v2 fixture metadata/SQL; applying it is a separate action."""
import json, sys, csv, hashlib
from pathlib import Path
HERE=Path(__file__).resolve().parent;sys.path.insert(0,str(HERE.parents[1]))
from sandbox.experiments.linked.medication_catalogue import get_catalogue,catalogue_manifest,VERSION

def q(value):
    return 'NULL' if value is None else "'"+str(value).replace("'","''")+"'"

def main():
    products=get_catalogue(include_iv=True,include_fault_only=True)
    frequencies=json.loads((HERE/'catalog_mapping.json').read_text())['SIM-METFORMIN-500']['frequency_codes']
    qty={'tablet':'1','capsule':'5','inhaler':'TH-INHALER','vial':'9','bag':'11'}
    dose={'mg':'mg','puff':'TH-PUFF','tablet':'TH-TABLET','capsule':'TH-CAPSULE','mL':'ml'}
    route={'oral':'1','inhaled':'5','intravenous_infusion':'405'}
    mapping={};values=[]
    for n,p in enumerate(products,1):
        code=p['drug_code']; mid=str(8700000000001200000+n)
        mapping[code]={'medicationDefinitionId':mid,'quantity_unit_code':qty[p['quantity_unit']],
            'dose_unit_code':dose[p['dose_unit']],'source_dose_unit':p['dose_unit'],'source_quantity_unit':p['quantity_unit'],
            'route_codes':{r:route[r] for r in p['allowed_routes']},'frequency_codes':frequencies,
            'dispenseLocationId':'322954949549363200','ingredient':p['ingredient'],'strength':p['strength'],
            'dosage_form':p['dosage_form'],'catalog_bus_no':'V2-'+code,'catalogue_version':VERSION,'scope':p['scope'],
            'provenance':'independent_SIM_product; identity backed by referenced label; prices/stock assumed',
            'source_references':p['source_references'],'dose_quantity_conversion':'unsupported; preserve independent dose and dispense quantities'}
        values.append('('+','.join([str(n),q('V2-'+code),q(p['display_name_zh']+'〔实验V2〕'),q(p['ingredient']),q(p['strength']),q(qty[p['quantity_unit']]),q(dose[p['dose_unit']]),q(route[p['route']]),q(p['dosage_form']),q(p['scope'])])+')')
    core={k:v for k,v in mapping.items() if v['scope']=='outpatient'}
    extension={k:v for k,v in mapping.items() if v['scope']!='outpatient'}
    for filename,obj in [('catalog_mapping_v2.json',core),('catalog_mapping_v2_extensions.json',extension),('catalogue_metadata_v2.json',catalogue_manifest())]:
        (HERE/filename).write_text(json.dumps(obj,ensure_ascii=False,indent=2)+'\n')
    sql='''-- Generated versioned SIM products. Apply only to twinhospital-exp.
-- Product identity references do not validate a treatment or approve a local SKU.
BEGIN;
INSERT INTO ter_concept(id,code_system_id,tenant_id,created_at,is_deleted,is_active,code,display,definition,sort_order)
VALUES
(8700000000001000001,900001,1,now(),false,true,'TH-TABLET','片','Independent v2 dose unit; no implicit mg conversion',9911),
(8700000000001000002,900001,1,now(),false,true,'TH-CAPSULE','粒','Independent v2 dose unit; not a swallowed route assumption',9912),
(8700000000001000003,279125561775755265,1,now(),false,true,'TH-TABLET','片','Independent v2 dose unit',9913),
(8700000000001000004,279125561775755265,1,now(),false,true,'TH-CAPSULE','粒','Independent v2 dose unit',9914)
ON CONFLICT(id) DO NOTHING;
CREATE TEMP TABLE th_catalog_v2(n int,code text,name text,ingredient text,strength text,quantity_unit text,dose_unit text,route text,form text,scope text) ON COMMIT DROP;
INSERT INTO th_catalog_v2 VALUES
'''+',\n'.join(values)+''';
INSERT INTO med_medication_definition
(id,tenant_id,created_at,is_deleted,version,bus_no,name,domain_enum,med_version,name_en,category_code,merchandise_name,
purchase_unit_code,split_unit_code,ingredient,active_flag,rx_flag,total_volume,dose_unit_code,package_unit_code,stock_unit_code,
method_code,rate_code,dose,location_id,status_enum,org_id,definition,dose_unit_ratio,purchase_unit_ratio,stock_unit_ratio,package_unit_ratio,
official_verified_flag,data_provenance,source_version,antibiotic_flag)
SELECT 8700000000001200000+n,1,now(),false,0,code,name,'WESTERN','TH-CATALOGUE-2.0',ingredient,'2',name,
quantity_unit,quantity_unit,ingredient,true,true,strength,dose_unit,quantity_unit,quantity_unit,
route,NULL,NULL,322954949549363200,'ACTIVE',322663105141149696,
'SIM only; label-backed ingredient/form/strength; simulated price and stock; no default regimen. form='||form||'; scope='||scope,
NULL,1,1,1,false,'INDEPENDENT_SIM_LABEL_IDENTITY','TH-CATALOGUE-2.0',ingredient IN ('amoxicillin','nitrofurantoin','azithromycin','trimethoprim+sulfamethoxazole','ceftriaxone')
FROM th_catalog_v2 ON CONFLICT(id) DO NOTHING;
INSERT INTO wkf_inventory_item
(id,tenant_id,created_at,is_deleted,version,category_code,item_table,item_id,name,inventory_status_enum,supplier_id,
description_text,unit_code,quantity,lot_number,production_date,expiration_date,validity_mon,location_id,packaging_levels)
SELECT 8700000000001300000+n,1,now(),false,0,'MEDICATION','med_medication_definition',8700000000001200000+n,name,'ACTIVE',4100000000000000004,
'Simulated inventory; no physical stock',quantity_unit,100000,'TH-V2-SIM-LOT-'||n,current_date,current_date+interval '5 years',60,322954949549363200,1
FROM th_catalog_v2 ON CONFLICT(id) DO NOTHING;
INSERT INTO adm_charge_item_definition
(id,tenant_id,created_at,is_deleted,version,instance_table,instance_id,charge_name,status_enum,org_id,type_code,yb_type,price_type_code,lot_no,price_value,priority,official_verified_flag,data_provenance,price_source)
SELECT 8700000000001400000+n*10+t.k,1,now(),false,0,'med_medication_definition',8700000000001200000+n,name,'ACTIVE',322663105141149696,
'1001','SELF_PAY',t.kind,CASE WHEN t.kind='LOT_NUMBER_PRICE' THEN 'TH-V2-SIM-LOT-'||n END,1.00,n,false,
'INDEPENDENT_SIM_LABEL_IDENTITY','Assumed 1 CNY per dispense unit, not real pricing'
FROM th_catalog_v2 CROSS JOIN (VALUES(1,'SALE'),(2,'LOT_NUMBER_PRICE'),(3,'PURCHASE')) t(k,kind)
ON CONFLICT(id) DO NOTHING;
COMMIT;
'''
    (HERE/'experiment-fixtures-v2.sql').write_text(sql)
    print(json.dumps({'version':VERSION,'core_products':len(core),'extension_products':len(extension),'generated_only':True}))
if __name__=='__main__':main()
