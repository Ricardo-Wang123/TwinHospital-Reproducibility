-- Add rolling virtual appointments without resetting any previous bookings.
BEGIN;
SET LOCAL TIME ZONE 'Asia/Shanghai';
SELECT pg_advisory_xact_lock(8400000000000000000);

INSERT INTO wkf_schedule
    (id, tenant_id, created_at, organization_id, name, status_enum, active_flag,
     planning_horizon_start, planning_horizon_end, healthcare_service_id)
SELECT 8400000000000020000+n, 1, now(),
    CASE WHEN n=3 THEN 8100000000000000002 ELSE 322663218269917184 END,
    'TwinHospital outpatient schedule ' || n, 'active', true, CURRENT_DATE-1, CURRENT_DATE+3, 322957264586018816
FROM generate_series(1,3) n
ON CONFLICT (id) DO UPDATE
    SET planning_horizon_start=LEAST(wkf_schedule.planning_horizon_start, EXCLUDED.planning_horizon_start),
        planning_horizon_end=GREATEST(wkf_schedule.planning_horizon_end, EXCLUDED.planning_horizon_end);

INSERT INTO wkf_schedule_actor (id, tenant_id, created_at, schedule_id, actor_type, actor_id, sort_no)
SELECT 8400000000000020100+n, 1, now(), 8400000000000020000+n, 10, 8200000000000000203+n, 1
FROM generate_series(1,3) n
UNION ALL
SELECT 8400000000000020200+n, 1, now(), 8400000000000020000+n, 30, 322957264586018816, 2
FROM generate_series(1,3) n
ON CONFLICT (id) DO NOTHING;

INSERT INTO wkf_slot
    (id, tenant_id, created_at, organization_id, schedule_id, start_time, end_time,
     status_code, capacity, booked_count, overbooked_flag, comment_text)
SELECT 8410000000000000000+(CURRENT_DATE+d-DATE '2000-01-01')*10+n, 1, now(),
    CASE WHEN n=3 THEN 8100000000000000002 ELSE 322663218269917184 END,
    8400000000000020000+n, CURRENT_DATE+d, CURRENT_DATE+d+interval '1 day'-interval '1 second',
    'free', 100000, 0, false, 'Simulated appointment capacity'
FROM generate_series(1,3) n CROSS JOIN generate_series(-1,2) d
ON CONFLICT (id) DO NOTHING;

COMMIT;
