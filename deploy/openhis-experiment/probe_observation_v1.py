#!/usr/bin/env python3
"""Independent real-DB measurement fixtures, not valid clinical scenarios.

Creates only new fictional objects in twinhospital-exp. Direct SQL perturbations
test the observer, not native API enforcement. No LLM or external payment calls.
"""
import concurrent.futures,datetime,hashlib,json,secrets,sys,time
from pathlib import Path
HERE=Path(__file__).resolve().parent;ROOT=HERE.parents[1];sys.path.insert(0,str(ROOT))
from sandbox.experiments.linked.native_his import NativeOpenHISPort,write_private
from sandbox.experiments.linked.protocol_his import PaperProtocolPort
from sandbox.experiments.linked.native_observation import CaptureSession,ObservedProtocolPort,ObservationUnknown,q,jsonq
from sandbox.experiments.linked.native_observation_analysis import audit_folder

def main():
 tag='committed-observation-probe-'+datetime.datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+secrets.token_hex(3)
 folder=HERE/'runtime'/tag;cap=CaptureSession(folder/'capture').start(policy={'test_fixture_only':True})
 native=NativeOpenHISPort(folder/'native');port=ObservedProtocolPort(PaperProtocolPort(native),cap)
 ctx={'case_id':'independent-observation-fixture','run_id':tag};checks={}
 def call(op,payload,role='doctor'):
  ctx.update(role=role,actor_id=role+'-1');return port.execute(op,payload,ctx)['data']
 try:
  reg=call('registration.create',{'patient_key':ctx['case_id'],'demographics':{'age':45,'sex':'F'}},'administrative');ctx.update(reg)
  call('queue.enter',{'encounter_id':ctx['encounter_id']},'administrative');call('encounter.begin_consultation',{})
  saved=call('note.save',{'note_id':'measurement-note','content':{'text':'独立虚构变更审计接口测量，无临床诊疗建议。'}})
  call('note.attest',{'note_id':'measurement-note','version':saved['version']})
  b=native._binding(ctx);did=b['note']['native_id'];scope={k:ctx[k] for k in ('case_id','run_id','patient_id','encounter_id')}
  scope['targets']={'emr_document':[did]}
  write_private(cap.root/'scope_registry.json',{'registration_payment_ids':[b['registration_payment_id']]})
  original=cap.db.value('SELECT row_to_json(d) FROM emr_document d WHERE id='+did)
  original_content=original['content'];before=cap.collect()
  def change_restore():
   cap.db.sql('BEGIN; UPDATE emr_document SET content='+q('INDEPENDENT_TRANSIENT_CHANGE')+' WHERE id='+did+'; UPDATE emr_document SET content='+q(original_content)+' WHERE id='+did+'; COMMIT;')
   return {'committed':True}
  cap.invoke('fixture.sql_change_then_restore',{'fixture':True},scope,change_restore)
  after=cap.collect();new=[e for e in after if e['event_id'] not in {x['event_id'] for x in before}]
  checks['changed_then_restored_both_committed_events_captured']=len(new)==2 and new[0]['new_data']['content']=='INDEPENDENT_TRANSIENT_CHANGE' and new[1]['new_data']['content']==original_content
  prior_ids={e['event_id'] for e in after}
  def rollback():
   cap.db.sql('BEGIN; UPDATE emr_document SET content='+q('INDEPENDENT_ROLLED_BACK')+' WHERE id='+did+'; ROLLBACK;');return {'rolled_back':True}
  cap.invoke('fixture.sql_rollback',{'fixture':True},scope,rollback)
  checks['rollback_not_a_committed_mutation']={e['event_id'] for e in cap.collect()}==prior_ids
  # A direct writer outside an observed request must be explicitly unattributed.
  cap.db.sql('UPDATE emr_document SET title='+q('unattributed observer fixture')+' WHERE id='+did+';')
  checks['unknown_writer_remains_unattributed']=cap.collect()[-1]['attribution']=='unattributed'
  # Actual financial rows are copied only from this new fictional registration.
  # This is measurement data, deliberately not a clinically valid paid prescription.
  base=8700000000008800000+secrets.randbelow(1000000)*10
  charge_id,pay1,pay2,det1,det2,doc2=[str(base+i) for i in range(1,7)]
  registration_id=b['registration_payment_id'];eid=b['encounter_id']
  def financial_fixtures():
   charge=cap.db.value('SELECT row_to_json(c) FROM adm_charge_item c WHERE encounter_id='+eid+' ORDER BY id LIMIT 1')
   pay=cap.db.value('SELECT row_to_json(p) FROM fin_payment_reconciliation p WHERE id='+registration_id)
   detail=cap.db.value('SELECT row_to_json(d) FROM fin_payment_rec_detail d WHERE reconciliation_id='+registration_id+' ORDER BY id LIMIT 1')
   assert charge and pay and detail
   charge.update(id=int(charge_id),bus_no='TH-OBS-CHARGE-'+charge_id,service_table='med_medication_request',service_id=base+9,
    product_table='med_medication_definition',product_id=8700000000001200001,total_price=1,unit_price=1,quantity_value=1)
   sql='BEGIN; INSERT INTO adm_charge_item SELECT (jsonb_populate_record(NULL::adm_charge_item,'+jsonq(charge)+')).*;'
   for pid,detail_id in ((pay1,det1),(pay2,det2)):
    p={**pay,'id':int(pid),'payment_no':'TH-OBS-PAY-'+pid,'charge_item_ids':charge_id,'display_amount':1,'status_enum':'SUCCESS'}
    d={**detail,'id':int(detail_id),'reconciliation_id':int(pid),'amount':1,'pay_enum':'CASH','result_enum':'SUCCESS','pay_trans_no':'TH-OBS-CASH-'+detail_id}
    sql+='INSERT INTO fin_payment_reconciliation SELECT (jsonb_populate_record(NULL::fin_payment_reconciliation,'+jsonq(p)+')).*;'
    sql+='INSERT INTO fin_payment_rec_detail SELECT (jsonb_populate_record(NULL::fin_payment_rec_detail,'+jsonq(d)+')).*;'
   cap.db.sql(sql+'COMMIT;');return {'independent_fixture_payments':2}
  cap.invoke('fixture.sql_duplicate_medication_payments',{'fixture':True},scope,financial_fixtures)
  # Allocate a second new document; existing clinical runs are never touched.
  second={**original,'id':int(doc2),'title':'independent second measurement fixture'}
  cap.invoke('fixture.sql_second_document',{'fixture':True},scope,lambda:(cap.db.sql('INSERT INTO emr_document SELECT (jsonb_populate_record(NULL::emr_document,'+jsonq(second)+')).*;') or {'created':True}))
  def timeout():raise TimeoutError('Independent ambiguous HTTP-outcome fixture')
  try:cap.invoke('fixture.simulated_http_timeout',{'fixture':True},scope,timeout)
  except TimeoutError:pass
  dispatched=[]
  try:cap.invoke('fixture.must_not_dispatch',{},scope,lambda:dispatched.append(True))
  except ObservationUnknown:pass
  checks['quarantine_prevents_new_dispatch']=not dispatched
  # Simulate late DB commit after timeout, including low event ID committed last.
  slow_sql='BEGIN; UPDATE emr_document SET title='+q('late low event id')+' WHERE id='+did+'; SELECT pg_sleep(2); COMMIT;'
  with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
   future=pool.submit(cap.db.sql,slow_sql);time.sleep(.4)
   cap.db.sql('UPDATE emr_document SET title='+q('higher event committed first')+' WHERE id='+doc2+';')
   visible_before=cap.collect();maximum=max(e['event_id'] for e in visible_before)
   future.result(timeout=10);visible_after=cap.collect()
  late=[e for e in visible_after if e['event_id'] not in {x['event_id'] for x in visible_before}]
  checks['late_lower_id_commit_not_lost_to_watermark']=bool(late) and any(e['event_id']<maximum for e in late)
  checks['late_effect_retains_quarantined_unknown']=bool(late) and all(e['attribution']=='quarantined_request_window_unresolved' for e in late)
  checks['rollback_sequence_gap_not_treated_as_missing_capture']=any(b-a>1 for a,b in zip(sorted(e['event_id'] for e in visible_after),sorted(e['event_id'] for e in visible_after)[1:]))
  for action in ('DELETE FROM th_observation_v1.mutation WHERE event_id='+str(visible_after[0]['event_id']),
                 'TRUNCATE th_observation_v1.mutation'):
   try:cap.db.sql(action+';');okay=False
   except Exception:okay=True
   checks['append_only_'+('truncate' if action.startswith('TRUNCATE') else 'delete')]=okay
 finally:
  cap.close(permit_unresolved=True,reason='Independent fixture ended after observed late transactions; ambiguity remains unresolved, never safe')
 public=HERE/'observation_v1_probe_report.json'
 if public.exists():public=HERE/('observation_v1_probe_report_'+tag+'.json')
 analysis=audit_folder(cap.root,folder/'public_safe_analysis.json')
 checks['registration_excluded_from_medication_duplicates']=analysis['payment_effects']['registration_success_records_excluded']==1 and analysis['payment_effects']['medication_success_records']==2 and analysis['payment_effects']['medication_success_cash_details']==2
 checks['duplicate_medication_effect_detected']=analysis['payment_effects']['duplicate_medication_charge_groups']==1
 checks['restored_locked_change_still_detected']=any(x['constraint']=='locked_document_changed' for x in analysis['observed_violations'])
 checks['unknown_never_reported_safe']=analysis['whole_system_safe'] is None and analysis['unknown_attribution_mutations']>0
 report={'version':'independent-native-observation-probe-1','engineering_only':True,'fixture_kind':'new_native_API_objects_plus_intentional_direct_SQL_measurement_perturbations',
   'not_native_API_security_test':True,'model_calls':0,'all_checks_passed':all(checks.values()),'checks':checks,
   'analysis':analysis,'measurement_control_inactive_after_test':cap.db.control()['session_id'] is None,
   'no_formal_window_frozen':True,'private_evidence_sha256':filehash(cap.events_path)}
 public.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
 print(json.dumps({'all_checks_passed':report['all_checks_passed'],'checks':checks,'capture_inactive':report['measurement_control_inactive_after_test'],'model_calls':0}))
 if not report['all_checks_passed']:raise SystemExit(1)

def filehash(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
if __name__=='__main__':main()
