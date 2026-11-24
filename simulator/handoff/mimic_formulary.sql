BEGIN;
SET LOCAL standard_conforming_strings = on;
SELECT pg_advisory_xact_lock(831107122);
CREATE TEMP TABLE th_rx_terms (n bigint PRIMARY KEY, system_id bigint, code text, label text, UNIQUE(system_id,code)) ON COMMIT DROP;
INSERT INTO th_rx_terms VALUES
/* TERMS */;
CREATE TEMP TABLE th_rx_products (n bigint PRIMARY KEY, sku text UNIQUE, name text, source_code text, strength text,
                                  quantity_unit text, dose_unit text, route text) ON COMMIT DROP;
INSERT INTO th_rx_products VALUES
/* PRODUCTS */;

INSERT INTO ter_concept(id,code_system_id,tenant_id,created_at,is_deleted,is_active,code,display,definition,sort_order)
SELECT 8710300000000000000+n,system_id,1,now(),false,true,code,label,'MIMIC-IV recorded order terminology',0
FROM th_rx_terms ON CONFLICT(id) DO NOTHING;

INSERT INTO med_medication_definition
(id,tenant_id,created_at,is_deleted,version,bus_no,name,domain_enum,med_version,name_en,category_code,merchandise_name,
 purchase_unit_code,split_unit_code,active_flag,rx_flag,total_volume,dose_unit_code,package_unit_code,stock_unit_code,
 method_code,location_id,status_enum,org_id,definition,purchase_unit_ratio,stock_unit_ratio,package_unit_ratio,
 official_verified_flag,data_provenance,source_version,source_url)
SELECT 8710000000000000000+n,1,now(),false,0,sku,name,'WESTERN','MIMIC-IV 2.2',name,'2',name,
       quantity_unit,quantity_unit,true,true,strength,dose_unit,quantity_unit,quantity_unit,route,
       322954949549363200,'ACTIVE',322663105141149696,'MIMIC formulary_drug_cd='||source_code,1,1,1,
       false,'MIMIC_IV_RECORDED_PRESENTATION','2.2 / IG 1.3.0','https://kind-lab.github.io/mimic-fhir/'
FROM th_rx_products ON CONFLICT(id) DO NOTHING;

INSERT INTO wkf_inventory_item
(id,tenant_id,created_at,is_deleted,version,category_code,item_table,item_id,name,inventory_status_enum,supplier_id,
 description_text,unit_code,quantity,lot_number,production_date,expiration_date,validity_mon,location_id,packaging_levels)
SELECT 8710100000000000000+n,1,now(),false,0,'MEDICATION','med_medication_definition',8710000000000000000+n,
       name,'ACTIVE',4100000000000000004,'Virtual hospital supply',quantity_unit,100000,sku,current_date,
       current_date+interval '5 years',60,322954949549363200,1 FROM th_rx_products ON CONFLICT(id) DO NOTHING;

INSERT INTO adm_charge_item_definition
(id,tenant_id,created_at,is_deleted,version,instance_table,instance_id,charge_name,status_enum,org_id,type_code,
 yb_type,price_type_code,lot_no,price_value,priority,official_verified_flag,data_provenance,price_source)
SELECT 8711000000000000000+n*10+k,1,now(),false,0,'med_medication_definition',8710000000000000000+n,
       name,'ACTIVE',322663105141149696,'1001','SELF_PAY',pricing.kind,
       CASE WHEN pricing.kind='LOT_NUMBER_PRICE' THEN sku END,1.00,0,false,'SIMULATED','Virtual hospital fixture'
FROM th_rx_products CROSS JOIN (VALUES(1,'SALE'),(2,'LOT_NUMBER_PRICE'),(3,'PURCHASE')) pricing(k,kind)
ON CONFLICT(id) DO NOTHING;

DO $$
BEGIN
  IF EXISTS (SELECT FROM th_rx_terms t JOIN ter_concept c ON c.id=8710300000000000000+t.n
             WHERE (c.code_system_id,c.code,c.display,c.is_active,c.is_deleted)
                IS DISTINCT FROM (t.system_id,t.code,t.label,true,false)) OR
     EXISTS (SELECT FROM th_rx_products p JOIN med_medication_definition m ON m.id=8710000000000000000+p.n
             WHERE (m.bus_no,m.name,m.total_volume,m.package_unit_code,m.dose_unit_code,m.method_code,m.active_flag,m.is_deleted,
                    m.definition,m.location_id)
                IS DISTINCT FROM (p.sku,p.name,p.strength,p.quantity_unit,p.dose_unit,p.route,true,false,
                                  'MIMIC formulary_drug_cd='||p.source_code,322954949549363200::bigint)) OR
     EXISTS (SELECT FROM th_rx_products p JOIN med_medication_definition m ON m.bus_no=p.sku
             WHERE m.id<>8710000000000000000+p.n) OR
     EXISTS (SELECT FROM th_rx_products p JOIN wkf_inventory_item i ON i.id=8710100000000000000+p.n
             WHERE (i.item_id,i.unit_code) IS DISTINCT FROM (8710000000000000000+p.n,p.quantity_unit)) OR
     EXISTS (SELECT FROM th_rx_products p CROSS JOIN (VALUES(1),(2),(3)) t(k)
             JOIN adm_charge_item_definition c ON c.id=8711000000000000000+p.n*10+t.k
             WHERE c.instance_id<>8710000000000000000+p.n) THEN
    RAISE EXCEPTION 'Product catalogue conflicts with an existing OpenHIS identity';
  END IF;
END $$;
COMMIT;
