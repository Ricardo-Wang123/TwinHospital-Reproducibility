-- Opt-in measurement only. Apply only to twinhospital-exp/openhis_205_local.
-- No business rows, permissions, clinical decisions, or historical results change.
BEGIN;
CREATE SCHEMA IF NOT EXISTS th_observation_v1;
REVOKE ALL ON SCHEMA th_observation_v1 FROM PUBLIC;
CREATE TABLE IF NOT EXISTS th_observation_v1.session (
 session_id text PRIMARY KEY, started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 status text NOT NULL, policy jsonb NOT NULL, finished_at timestamptz,
 version text NOT NULL DEFAULT 'committed-native-observation-1');
CREATE TABLE IF NOT EXISTS th_observation_v1.control (
 singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
 session_id text REFERENCES th_observation_v1.session(session_id),
 request_id text, quarantined boolean NOT NULL DEFAULT false,
 reason text, updated_at timestamptz NOT NULL DEFAULT clock_timestamp());
INSERT INTO th_observation_v1.control(singleton) VALUES(true) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS th_observation_v1.mutation (
 event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 session_id text NOT NULL REFERENCES th_observation_v1.session(session_id),
 request_window_id text, attribution text NOT NULL,
 database_time timestamptz NOT NULL DEFAULT clock_timestamp(),
 transaction_id bigint NOT NULL, backend_pid integer NOT NULL,
 table_schema text NOT NULL, table_name text NOT NULL, action text NOT NULL,
 object_id text, old_data jsonb, new_data jsonb,
 old_hash text, new_hash text);
CREATE INDEX IF NOT EXISTS th_observation_v1_session_event_idx
 ON th_observation_v1.mutation(session_id,event_id);
CREATE INDEX IF NOT EXISTS th_observation_v1_session_request_idx
  ON th_observation_v1.mutation(session_id,request_window_id,event_id);
CREATE OR REPLACE FUNCTION th_observation_v1.capture() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE ctl th_observation_v1.control%ROWTYPE; prior jsonb; after_row jsonb;
BEGIN
 SELECT * INTO ctl FROM th_observation_v1.control WHERE singleton;
 IF ctl.session_id IS NULL THEN RETURN COALESCE(NEW,OLD); END IF;
 IF TG_OP<>'INSERT' THEN prior:=to_jsonb(OLD); END IF;
 IF TG_OP<>'DELETE' THEN after_row:=to_jsonb(NEW); END IF;
 INSERT INTO th_observation_v1.mutation(session_id,request_window_id,attribution,
 transaction_id,backend_pid,table_schema,table_name,action,object_id,old_data,new_data,old_hash,new_hash)
 VALUES(ctl.session_id,ctl.request_id,
 CASE WHEN ctl.quarantined THEN 'quarantined_request_window_unresolved'
 WHEN ctl.request_id IS NULL THEN 'unattributed' ELSE 'exclusive_window_candidate_not_transaction_proof' END,
 txid_current(),pg_backend_pid(),TG_TABLE_SCHEMA,TG_TABLE_NAME,TG_OP,
 COALESCE(after_row->>'id',prior->>'id'),prior,after_row,
 CASE WHEN prior IS NULL THEN NULL ELSE encode(sha256(convert_to(prior::text,'UTF8')),'hex') END,
 CASE WHEN after_row IS NULL THEN NULL ELSE encode(sha256(convert_to(after_row::text,'UTF8')),'hex') END);
 RETURN COALESCE(NEW,OLD);
END $$;
CREATE OR REPLACE FUNCTION th_observation_v1.immutable() RETURNS trigger
LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'Observation mutation evidence is append-only'; END $$;
DO $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_trigger WHERE tgname='th_obs_immutable_rows' AND tgrelid='th_observation_v1.mutation'::regclass) THEN
  CREATE TRIGGER th_obs_immutable_rows BEFORE UPDATE OR DELETE ON th_observation_v1.mutation FOR EACH ROW EXECUTE FUNCTION th_observation_v1.immutable();
 END IF;
 IF NOT EXISTS(SELECT 1 FROM pg_trigger WHERE tgname='th_obs_immutable_truncate' AND tgrelid='th_observation_v1.mutation'::regclass) THEN
  CREATE TRIGGER th_obs_immutable_truncate BEFORE TRUNCATE ON th_observation_v1.mutation FOR EACH STATEMENT EXECUTE FUNCTION th_observation_v1.immutable();
 END IF;
END $$;
DO $$ DECLARE item text; BEGIN
 FOREACH item IN ARRAY ARRAY[
  'adm_patient','adm_patient_identifier','adm_encounter','adm_encounter_diagnosis',
  'adm_encounter_location','adm_encounter_participant','adm_encounter_reason',
  'med_medication_request','emr_document','emr_document_version','emr_document_data_item_index',
  'adm_charge_item','fin_payment_reconciliation','fin_payment_rec_detail',
  'fin_payment_rec_static','fin_payment_yb_settlement','fin_cashier_settlement']
 LOOP
  IF to_regclass('public.'||item) IS NULL THEN RAISE EXCEPTION 'Required observation table missing: %',item; END IF;
  IF NOT EXISTS(SELECT 1 FROM pg_trigger WHERE tgname='th_obs_committed_v1' AND tgrelid=to_regclass('public.'||item)) THEN
   EXECUTE format('CREATE TRIGGER th_obs_committed_v1 AFTER INSERT OR UPDATE OR DELETE ON public.%I FOR EACH ROW EXECUTE FUNCTION th_observation_v1.capture()',item);
  END IF;
 END LOOP;
END $$;
REVOKE ALL ON ALL TABLES IN SCHEMA th_observation_v1 FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA th_observation_v1 FROM PUBLIC;
COMMIT;
