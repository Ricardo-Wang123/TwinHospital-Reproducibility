-- Independent institution and billing definitions for the reader database.
BEGIN;

INSERT INTO whale_tenants (id, name, code)
VALUES (1, 'TwinHospital Research Hospital', 'TH-RESEARCH');

INSERT INTO adm_organization
    (id, tenant_id, created_at, name, bus_no, parent_id, type_enum, class_enum, status_enum, active_flag)
VALUES
    (322663105141149696, 1, now(), 'TwinHospital', 'TH', NULL, 'HOSPITAL', NULL, 'ACTIVE', true),
    (322663218269917184, 1, now(), 'Outpatient Medicine', 'TH-OP', 322663105141149696, 'DEPARTMENT', 'CLINIC', 'ACTIVE', true),
    (8100000000000000002, 1, now(), 'General Outpatient Clinic', 'TH-GP', 322663105141149696, 'DEPARTMENT', 'CLINIC', 'ACTIVE', true),
    (322663308812357632, 1, now(), 'Inpatient Medicine', 'TH-IP', 322663105141149696, 'DEPARTMENT', 'INPATIENT', 'ACTIVE', true),
    (322663652522987520, 1, now(), 'Nursing', 'TH-NS', 322663105141149696, 'DEPARTMENT', 'NS', 'ACTIVE', true),
    (322663815429754880, 1, now(), 'Cashier', 'TH-CASH', 322663105141149696, 'DEPARTMENT', 'FIN', 'ACTIVE', true),
    (8100000000000000016, 1, now(), 'Laboratory', 'TH-LAB', 322663105141149696, 'DEPARTMENT', 'TECH', 'ACTIVE', true),
    (8100000000000000018, 1, now(), 'Pharmacy', 'TH-PHARM', 322663105141149696, 'DEPARTMENT', 'MANAGER', 'ACTIVE', true);

INSERT INTO whale_organization_units (id, tenant_id, created_at, code, display_name)
VALUES (322663105157926912, 1, now(), 'TH', 'TwinHospital');

INSERT INTO adm_location (id, tenant_id, created_at, name, bus_no, status_enum, form_enum, mode_enum, organization_id)
VALUES
    (322954885380706304, 1, now(), 'Virtual Warehouse', 'TH-WARE', 'ACTIVE', 'WAREHOUSE', 'INSTANCE', 322663105141149696),
    (322954949549363200, 1, now(), 'Virtual Pharmacy', 'TH-DISP', 'ACTIVE', 'PHARMACY', 'INSTANCE', 322663105141149696),
    (322955305339588608, 1, now(), 'Research Ward', 'TH-WARD', 'ACTIVE', 'WARD', 'INSTANCE', 322663308812357632),
    (340100000000000001, 1, now(), 'Outpatient Supply Room', 'TH-SUPPLY', 'ACTIVE', 'DEPARTMENT_PHARMACY', 'INSTANCE', 322663218269917184),
    (8100000000000010016, 1, now(), 'Virtual Laboratory', 'TH-LAB-RM', 'ACTIVE', 'ROOM', 'INSTANCE', 8100000000000000016);

INSERT INTO adm_healthcare_service (id, tenant_id, created_at, name, type_code, status_enum, active_flag, appointment_required_flag, bus_no)
VALUES (322957264586018816, 1, now(), 'Outpatient visit', '1', 'ACTIVE', true, false, 'TH-OP-VISIT');

INSERT INTO adm_charge_item_definition
    (id, tenant_id, created_at, instance_table, instance_id, charge_name, type_code, yb_type, price_type_code, price_value, priority)
VALUES (322957264586018817, 1, now(), 'adm_healthcare_service', 322957264586018816, 'Outpatient registration', '1012', 'SELF_PAY', 'SALE', 10, 1);

INSERT INTO fin_contract (id, tenant_id, created_at, bus_no, name, category_enum, status_enum, active_flag, org_id, yb_flag)
VALUES (3100000000000000001, 1, now(), 'TH-CASH', '自费', '1', '1', true, 322663218269917184, false);

INSERT INTO cus_supplier (id, tenant_id, created_at, bus_no, name, type_enum, credit_code, py_str, wb_str, org_id, status_enum)
VALUES (4100000000000000004, 1, now(), 'TH-SUPPLIER', 'Simulated supplier', '2', 'TH-FICTIONAL', 'SIM', 'SIM', 1, 'ACTIVE');

INSERT INTO emr_document_type (id, tenant_id, created_at, code, name, category_code)
VALUES (323100000000000101, 1, now(), 'OUTPATIENT_RECORD', 'Outpatient record', 'OUTPATIENT');

-- The native order-entry API resolves this definition even for orders without a skin test.
INSERT INTO wkf_activity_definition (id, tenant_id, created_at, category_code, bus_no, name, type_enum, status_enum)
VALUES (323002894106234880, 1, now(), '22', 'TH-SKIN-TEST', '皮试检查', '1', 'ACTIVE');

INSERT INTO cus_service_location_template
    (id, tenant_id, created_at, organization_id, item_table, distribution_category_code, def_location_id, start_time, end_time, priority, display_order)
VALUES
    (322976478253289472, 1, now(), 322663218269917184, 'med_medication_definition', '1', 322954949549363200, '00:00:00', '23:59:59.999999', 1, 1),
    (322976478257483776, 1, now(), 322663218269917184, 'med_medication_definition', '2', 322954949549363200, '00:00:00', '23:59:59.999999', 2, 2);

COMMIT;
