-- Only twinhospital-exp, append-only; never real clinical inventory.
BEGIN;
CREATE TEMP TABLE th_catalog_v4(n int,code text,name text,ingredient text,strength text,quantity_unit text,dose_unit text,route text,form text) ON COMMIT DROP;
INSERT INTO th_catalog_v4 VALUES
('1','V4-SIM-METFORMIN-ER-500','盐酸二甲双胍缓释片500mg〔实验V4〕','metformin','500 mg/tablet','1','mg','1','oral_tablet_extended_release'),
('2','V4-SIM-METOPROLOL-SUCCINATE-ER-25','琥珀酸美托洛尔缓释片（标示25mg）〔实验V4〕','metoprolol','25 mg tartrate-equivalent/tablet','1','mg','1','oral_tablet_extended_release'),
('3','V4-SIM-FLUTICASONE-SALMETEROL-DISKUS-250-50','氟替卡松/沙美特罗Diskus吸入粉250/50μg〔实验V4〕','fluticasone propionate+salmeterol','250 mcg fluticasone propionate + 50 mcg salmeterol/inhalation','TH-INHALER','TH-INHALATION','5','dry_powder_inhaler_diskus'),
('4','V4-SIM-LEVOFLOXACIN-750','左氧氟沙星片750mg〔实验V4〕','levofloxacin','750 mg/tablet','1','mg','1','oral_tablet_immediate_release'),
('5','V4-SIM-METOPROLOL-SUCCINATE-ER-50','琥珀酸美托洛尔缓释片（标示50mg）〔实验V4〕','metoprolol','50 mg tartrate-equivalent/tablet','1','mg','1','oral_tablet_extended_release'),
('6','V4-SIM-METOPROLOL-SUCCINATE-ER-100','琥珀酸美托洛尔缓释片（标示100mg）〔实验V4〕','metoprolol','100 mg tartrate-equivalent/tablet','1','mg','1','oral_tablet_extended_release'),
('7','V4-SIM-LEVOFLOXACIN-500','左氧氟沙星片500mg〔实验V4〕','levofloxacin','500 mg/tablet','1','mg','1','oral_tablet_immediate_release'),
('8','V4-SIM-IPRATROPIUM-NEB-05-25ML','异丙托溴铵雾化液0.5mg/2.5mL〔实验V4〕','ipratropium','0.5 mg ipratropium bromide/2.5 mL','9','ml','5','nebulizer_solution'),
('9','V4-SIM-IPRATROPIUM-ALBUTEROL-RESPIMAT-20-100','异丙托溴铵/沙丁胺醇Respimat20/100μg〔实验V4〕','ipratropium+albuterol','20 mcg ipratropium bromide + 100 mcg albuterol/actuation','TH-INHALER','TH-PUFF','5','metered_inhalation_spray_respimat'),
('10','V4-SIM-IPRATROPIUM-ALBUTEROL-NEB-05-3-3ML','异丙托溴铵/硫酸沙丁胺醇雾化液0.5/3mg每3mL〔实验V4〕','ipratropium+albuterol','0.5 mg ipratropium bromide + 3 mg albuterol sulfate/3 mL','9','ml','5','nebulizer_solution'),
('11','V4-SIM-LISINOPRIL-5','赖诺普利片5mg〔实验V4〕','lisinopril','5 mg/tablet','1','mg','1','oral_tablet_immediate_release'),
('12','V4-SIM-LOSARTAN-POTASSIUM-50','氯沙坦钾片50mg〔实验V4〕','losartan','50 mg/tablet','1','mg','1','oral_tablet_immediate_release'),
('13','V4-SIM-METOPROLOL-TARTRATE-25','酒石酸美托洛尔普通片25mg〔实验V4〕','metoprolol','25 mg/tablet','1','mg','1','oral_tablet_immediate_release'),
('14','V4-SIM-GLIPIZIDE-IR-5','格列吡嗪普通片5mg〔实验V4〕','glipizide','5 mg/tablet','1','mg','1','oral_tablet_immediate_release'),
('15','V4-SIM-GLIPIZIDE-ER-5','格列吡嗪缓释片5mg〔实验V4〕','glipizide','5 mg/tablet','1','mg','1','oral_tablet_extended_release'),
('16','V4-SIM-GLIPIZIDE-ER-10','格列吡嗪缓释片10mg〔实验V4〕','glipizide','10 mg/tablet','1','mg','1','oral_tablet_extended_release'),
('17','V4-SIM-AMOXICILLIN-CLAVULANATE-875-125','阿莫西林/克拉维酸片875/125mg〔实验V4〕','amoxicillin+clavulanic acid','875 mg amoxicillin + 125 mg clavulanic acid/tablet','1','TH-TABLET','1','oral_tablet_immediate_release'),
('18','V4-SIM-DOXYCYCLINE-HYCLATE-100','多西环素盐酸盐胶囊100mg〔实验V4〕','doxycycline','100 mg doxycycline/capsule','5','mg','1','oral_capsule'),
('19','V4-SIM-CIPROFLOXACIN-250','盐酸环丙沙星片250mg〔实验V4〕','ciprofloxacin','250 mg ciprofloxacin/tablet','1','mg','1','oral_tablet_immediate_release'),
('20','V4-SIM-CIPROFLOXACIN-500','盐酸环丙沙星片500mg〔实验V4〕','ciprofloxacin','500 mg ciprofloxacin/tablet','1','mg','1','oral_tablet_immediate_release'),
('21','V4-SIM-NITROFURANTOIN-MACRO-50','呋喃妥因大晶体胶囊50mg〔实验V4〕','nitrofurantoin','50 mg/capsule','5','mg','1','oral_capsule_macrocrystals'),
('22','V4-SIM-TMP-SMX-SUSP-40-200-5ML','复方磺胺甲噁唑口服混悬液TMP40/SMX200mg每5mL〔实验V4〕','trimethoprim+sulfamethoxazole','40 mg trimethoprim + 200 mg sulfamethoxazole/5 mL','TH-ML','ml','1','oral_suspension'),
('23','V4-SIM-AMPICILLIN-250','氨苄西林胶囊250mg〔实验V4〕','ampicillin','250 mg ampicillin/capsule','5','mg','1','oral_capsule');
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
