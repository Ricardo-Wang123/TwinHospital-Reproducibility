-- Independent software-engineering fixtures; not a medicinal product registry.
-- This file is applied ONLY via manage.py to the twinhospital-exp Compose DB.
BEGIN;
UPDATE fin_contract SET org_id=322663218269917184,yb_flag=false,updated_at=now()
WHERE id=3100000000000000001 AND name='自费';
INSERT INTO ter_concept(id,code_system_id,tenant_id,created_at,is_deleted,is_active,code,display,definition,sort_order)
VALUES
(8700000000000000001,279125561775755265,1,now(),false,true,'mg','毫克','TwinHospital simulated catalogue unit alias',9901),
(8700000000000000002,279125561775755265,1,now(),false,true,'TH-PUFF','揿','TwinHospital simulated one actuation unit',9902),
(8700000000000000003,279125561775755265,1,now(),false,true,'TH-INHALER','吸入器','TwinHospital simulated 200-actuation device',9903),
(8700000000000000004,900001,1,now(),false,true,'TH-PUFF','揿','TwinHospital simulated one actuation unit',9904)
ON CONFLICT(id) DO NOTHING;
CREATE TEMP TABLE th_catalog(n int,code text,name text,ingredient text,strength text,quantity_unit text,dose_unit text,dose_ratio numeric,dose numeric,route text,frequency text,price numeric) ON COMMIT DROP;
INSERT INTO th_catalog VALUES
(1,'SIM-METFORMIN-500','二甲双胍片〔实验目录〕','metformin','500 mg/tablet','1','mg',0.002,500,'1','BID',1.00),
(2,'SIM-FUROSEMIDE-20','呋塞米片〔实验目录〕','furosemide','20 mg/tablet','1','mg',0.05,20,'1','QD',1.00),
(3,'SIM-SALBUTAMOL-100','沙丁胺醇吸入器〔实验目录〕','salbutamol','100 microgram/actuation; 200 actuations/device','TH-INHALER','TH-PUFF',0.005,2,'5','QID',10.00),
(4,'SIM-AMOXICILLIN-500','阿莫西林胶囊〔实验目录〕','amoxicillin','500 mg/capsule','5','mg',0.002,500,'1','TID',1.00),
(5,'SIM-NITROFURANTOIN-100','呋喃妥因胶囊〔实验目录〕','nitrofurantoin','100 mg/capsule','5','mg',0.01,100,'1','BID',1.00);
INSERT INTO med_medication_definition
(id,tenant_id,created_at,is_deleted,version,bus_no,name,domain_enum,med_version,name_en,category_code,merchandise_name,
purchase_unit_code,split_unit_code,ingredient,active_flag,rx_flag,total_volume,dose_unit_code,package_unit_code,stock_unit_code,
method_code,rate_code,dose,location_id,status_enum,org_id,definition,dose_unit_ratio,purchase_unit_ratio,stock_unit_ratio,package_unit_ratio,
official_verified_flag,data_provenance,source_version,antibiotic_flag)
SELECT 8700000000000200000+n,1,now(),false,0,code,name,'WESTERN','TH-ENGINEERING-1',ingredient,'2',name,
quantity_unit,quantity_unit,ingredient,true,true,strength,dose_unit,quantity_unit,quantity_unit,
route,frequency,dose,322954949549363200,'ACTIVE',322663105141149696,
'独立虚构实验药品目录。成分与规格用于工程接口；价格和库存为模拟，不作为临床用药、采购或收费依据。',NULL,1,1,1,
false,'INDEPENDENT_SYNTHETIC_ENGINEERING_FIXTURE','TH-ENGINEERING-1',n IN (4,5)
FROM th_catalog ON CONFLICT(id) DO NOTHING;
INSERT INTO wkf_inventory_item
(id,tenant_id,created_at,is_deleted,version,category_code,item_table,item_id,name,inventory_status_enum,supplier_id,
description_text,unit_code,quantity,lot_number,production_date,expiration_date,validity_mon,location_id,packaging_levels)
SELECT 8700000000000300000+n,1,now(),false,0,'MEDICATION','med_medication_definition',8700000000000200000+n,name,'ACTIVE',4100000000000000004,
'TwinHospital independent simulated inventory; no physical stock',quantity_unit,100000,'TH-SIM-LOT-'||n,current_date,current_date+interval '5 years',60,322954949549363200,1
FROM th_catalog ON CONFLICT(id) DO NOTHING;
INSERT INTO adm_charge_item_definition
(id,tenant_id,created_at,is_deleted,version,instance_table,instance_id,charge_name,status_enum,org_id,type_code,yb_type,price_type_code,lot_no,price_value,priority,official_verified_flag,data_provenance,price_source)
SELECT 8700000000000400000+n*10+t.k,1,now(),false,0,'med_medication_definition',8700000000000200000+n,name,'ACTIVE',322663105141149696,
'1001','SELF_PAY',t.kind,CASE WHEN t.kind='LOT_NUMBER_PRICE' THEN 'TH-SIM-LOT-'||n END,price,n,false,
'INDEPENDENT_SYNTHETIC_ENGINEERING_FIXTURE','Assumed experimental price only'
FROM th_catalog CROSS JOIN (VALUES(1,'SALE'),(2,'LOT_NUMBER_PRICE'),(3,'PURCHASE')) t(k,kind)
ON CONFLICT(id) DO NOTHING;
-- Native display divides dose by this ratio while inventory converter multiplies it.
-- Deliberately leave unsupported dose conversion unset; preserve exact dose + unit and
-- explicit quantity + unit. Never dispense or charge in mg / puff.
UPDATE med_medication_definition SET dose_unit_ratio=NULL WHERE id BETWEEN 8700000000000200001 AND 8700000000000200005;
INSERT INTO cln_condition_definition(id,tenant_id,created_at,is_deleted,version,source_enum,condition_code,clinic_code,name,status_enum,yb_flag,yb_match_flag,official_verified_flag,description,data_provenance,source_version)
SELECT 8700000000000500000+n,1,now(),false,0,'general_dx',code,code,name,'ACTIVE',false,false,false,
'TwinHospital local experimental terminology entry; clinician agent must choose after consultation; not a patient label or billing authority',
'INDEPENDENT_EXPERIMENT_TERMINOLOGY','TH-ENGINEERING-1'
FROM (VALUES(1,'E11.9','2型糖尿病，不伴有并发症'),(2,'I50.9','心力衰竭，未特指'),(3,'J44.9','慢性阻塞性肺病，未特指'),(4,'J18.9','肺炎，病原体未特指'),(5,'N39.0','泌尿道感染，部位未特指')) v(n,code,name)
ON CONFLICT(id) DO NOTHING;
COMMIT;
