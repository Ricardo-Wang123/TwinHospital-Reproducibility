-- Generated versioned SIM products. Apply only to twinhospital-exp.
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
(1,'V2-SIM-METFORMIN-500','盐酸二甲双胍普通片500mg〔实验V2〕','metformin','500 mg/tablet','1','mg','1','oral_tablet_immediate_release','outpatient'),
(2,'V2-SIM-FUROSEMIDE-20','呋塞米片20mg〔实验V2〕','furosemide','20 mg/tablet','1','mg','1','oral_tablet_immediate_release','outpatient'),
(3,'V2-SIM-ALBUTEROL-90','沙丁胺醇HFA吸入器90μg/揿〔实验V2〕','albuterol','90 mcg/actuation','TH-INHALER','TH-PUFF','5','metered_dose_inhalation_aerosol','outpatient'),
(4,'V2-SIM-AMOXICILLIN-500','阿莫西林胶囊500mg〔实验V2〕','amoxicillin','500 mg/capsule','5','mg','1','oral_capsule','outpatient'),
(5,'V2-SIM-NITROFURANTOIN-MONO-MACRO-100','呋喃妥因一水合物/大晶体胶囊100mg〔实验V2〕','nitrofurantoin','100 mg/capsule','5','mg','1','oral_capsule_monohydrate_macrocrystals','outpatient'),
(6,'V2-SIM-LISINOPRIL-10','赖诺普利片10mg〔实验V2〕','lisinopril','10 mg/tablet','1','mg','1','oral_tablet_immediate_release','outpatient'),
(7,'V2-SIM-SPIRONOLACTONE-25','螺内酯片25mg〔实验V2〕','spironolactone','25 mg/tablet','1','mg','1','oral_tablet_immediate_release','outpatient'),
(8,'V2-SIM-EMPAGLIFLOZIN-10','恩格列净片10mg〔实验V2〕','empagliflozin','10 mg/tablet','1','mg','1','oral_tablet_immediate_release','outpatient'),
(9,'V2-SIM-TIOTROPIUM-18','噻托溴铵吸入粉末胶囊18μg〔实验V2〕','tiotropium','18 mcg/capsule','5','TH-CAPSULE','5','inhalation_powder_capsule','outpatient'),
(10,'V2-SIM-PREDNISONE-20','泼尼松片20mg〔实验V2〕','prednisone','20 mg/tablet','1','mg','1','oral_tablet_immediate_release','outpatient'),
(11,'V2-SIM-AZITHROMYCIN-250','阿奇霉素片250mg〔实验V2〕','azithromycin','250 mg/tablet','1','mg','1','oral_tablet_immediate_release','outpatient'),
(12,'V2-SIM-TMP-SMX-160-800','复方磺胺甲噁唑片TMP160/SMX800mg〔实验V2〕','trimethoprim+sulfamethoxazole','160 mg trimethoprim + 800 mg sulfamethoxazole/tablet','1','TH-TABLET','1','oral_tablet_immediate_release','outpatient'),
(13,'V2-SIM-WARFARIN-5','华法林钠片5mg〔实验V2〕','warfarin','5 mg/tablet','1','mg','1','oral_tablet_immediate_release','outpatient'),
(14,'V2-SIM-ACETAMINOPHEN-500','对乙酰氨基酚片500mg〔实验V2〕','acetaminophen','500 mg/tablet','1','mg','1','oral_tablet_immediate_release','outpatient'),
(15,'V2-SIM-CEFTRIAXONE-1G','注射用头孢曲松钠1g〔实验V2〕','ceftriaxone','1 g/vial','9','mg','405','powder_for_injection','iv_extension'),
(16,'V2-SIM-NACL-09-100ML','氯化钠注射液0.9%100mL〔实验V2〕','sodium chloride','9 mg/mL; 100 mL/bag','11','ml','405','intravenous_solution','iv_extension'),
(17,'V2-SIM-DEXTROSE-5-100ML','葡萄糖注射液5%100mL〔实验V2〕','dextrose','50 mg/mL; 100 mL/bag','11','ml','405','intravenous_solution','iv_extension'),
(18,'V2-SIM-LACTATED-RINGERS-500ML','乳酸林格液500mL〔实验V2〕','sodium chloride+sodium lactate+potassium chloride+calcium chloride','NaCl 6 + sodium lactate 3.1 + KCl 0.3 + calcium chloride 0.2 g/L; 500 mL/bag','11','ml','405','intravenous_solution','iv_extension'),
(19,'V2-SIM-SACUBITRIL-VALSARTAN-24-26','沙库巴曲缬沙坦片24/26mg〔实验V2〕','sacubitril+valsartan','24 mg sacubitril + 26 mg valsartan/tablet','1','TH-TABLET','1','oral_tablet_immediate_release','fault_only');
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
