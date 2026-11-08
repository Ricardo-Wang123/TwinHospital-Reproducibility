import copy,fcntl,json,multiprocessing,os,tempfile,unittest
from pathlib import Path
from sandbox.experiments.linked.native_observation import CaptureSession,ObservationUnknown,ObservedHTTPClient,api_coverage,READ_AUTH,WRITES
from sandbox.experiments.linked.native_observation_analysis import analyze,audit_folder

def child_lock(path,queue):
    fd=os.open(path,os.O_RDWR)
    try:
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);queue.put(True)
        except BlockingIOError:queue.put(False)
    finally:os.close(fd)

class FakeDB:
    def __init__(self):self.ctl={'session_id':None,'request_id':None,'quarantined':False};self.rows=[]
    def coverage(self):return {'declared_tables':['emr_document'],'declared_table_set_verified':True,'append_only_guards_enabled':True}
    def control(self):return dict(self.ctl)
    def start(self,session,policy):self.ctl.update(session_id=session)
    def frame(self,session,request):self.ctl.update(request_id=request)
    def clear_frame(self,session,request):self.ctl.update(request_id=None)
    def quarantine(self,session,reason):self.ctl.update(quarantined=True)
    def events(self,session):return copy.deepcopy(self.rows)
    def request_events(self,session,request):return copy.deepcopy([r for r in self.rows if r.get('request_window_id')==request])
    def snapshot(self,scope):return {'scope':copy.deepcopy(scope)}
    def end(self,session,unresolved=False):self.ctl.update(session_id=None,request_id=None,quarantined=False)

class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.db=FakeDB()
        self.cap=CaptureSession(Path(self.tmp.name)/'private',db=self.db);self.cap.lock_path=Path(self.tmp.name)/'global.lock';self.cap.start()
    def test_intent_fsynced_before_dispatch_and_private(self):
        called=[]
        def call():
            rows=[json.loads(x) for x in self.cap.journal.read_text().splitlines()]
            called.append(rows[-1]['event']);self.assertEqual(rows[-1]['event'],'business_request_intent')
            self.assertIsNotNone(self.db.ctl['request_id']);return {'okay':True}
        result=self.cap.invoke('fixture',{'PRIVATE_BODY':'never public'},{'case_id':'PRIVATE_CASE'},call)
        self.assertEqual(result,{'okay':True});self.assertEqual(called,['business_request_intent'])
        for p in self.cap.root.iterdir():self.assertEqual(p.stat().st_mode&0o777,0o600)
        self.assertNotIn('never public',self.cap.journal.read_text())
    def test_exception_persists_quarantine_and_no_retry_dispatch(self):
        def timeout():raise TimeoutError()
        with self.assertRaises(TimeoutError):self.cap.invoke('write',{}, {},timeout)
        calls=[]
        with self.assertRaises(ObservationUnknown):self.cap.invoke('new',{}, {},lambda:calls.append(1))
        self.assertEqual(calls,[]);self.assertTrue(self.db.ctl['quarantined'])
        with self.assertRaises(ObservationUnknown):self.cap.close()
        ended=self.cap.close(permit_unresolved=True,reason='unit fixture ending unknown')
        self.assertEqual(ended['status'],'closed_unresolved');self.assertIsNone(ended['whole_system_safe'])
    def test_low_id_late_commit_is_rescanned(self):
        self.db.rows=[{'event_id':9}];self.cap.collect()
        self.db.rows.append({'event_id':4})
        self.assertEqual({r['event_id'] for r in self.cap.collect()},{4,9})
    def test_request_collection_does_not_reserialize_entire_session_and_close_keeps_late_foreign_effects(self):
        calls=[]
        original=self.db.events
        def all_events(session):calls.append('full');return original(session)
        self.db.events=all_events
        def write():
            self.db.rows.append({'event_id':10,'request_window_id':self.db.ctl['request_id']})
            return {'okay':True}
        self.cap.invoke('fixture',{}, {},write)
        self.assertEqual(calls,[])
        self.db.rows.append({'event_id':4,'request_window_id':None})
        self.cap.close()
        self.assertEqual(calls,['full'])
        result=json.loads(self.cap.events_path.read_text())
        self.assertEqual({r['event_id'] for r in result['events']},{4,10})
        local=json.loads(next(self.cap.root.glob('request_*_events.json')).read_text())
        self.assertEqual([r['event_id'] for r in local['events']],[10])
        self.assertFalse(local['full_session_scan'])
    def test_global_lock_is_cross_process(self):
        context=multiprocessing.get_context('fork');queue=context.Queue()
        with self.cap.lock():
            child=context.Process(target=child_lock,args=(str(self.cap.lock_path),queue));child.start();child.join(5)
            self.assertFalse(queue.get(timeout=1));self.assertEqual(child.exitcode,0)
        child=context.Process(target=child_lock,args=(str(self.cap.lock_path),queue));child.start();child.join(5)
        self.assertTrue(queue.get(timeout=1));self.assertEqual(child.exitcode,0)
    def test_raw_public_folder_forbidden(self):
        with self.assertRaises(ValueError):CaptureSession(Path(self.tmp.name)/'public_evidence'/'raw',db=self.db)
    def test_snapshot_coverage_drift_ends_unknown(self):
        self.db.coverage=lambda:{'declared_tables':[],'declared_table_set_verified':False,'append_only_guards_enabled':False}
        end=self.cap.close();self.assertFalse(end['coverage_unchanged']);self.assertEqual(end['status'],'closed_unresolved')
    def test_http_write_without_observed_scope_rejected(self):
        class Client:
            def post(self,*a):raise AssertionError('must not send')
        client=ObservedHTTPClient(Client(),self.cap)
        with self.assertRaises(ObservationUnknown):client.post('/document/save',{})
    def test_unknown_post_journalled_and_quarantined_without_dispatch(self):
        class Client:
            def post(self,*a):raise AssertionError('unknown API must not reach transport')
        client=ObservedHTTPClient(Client(),self.cap)
        with self.assertRaises(ObservationUnknown):client.post('/newUnclassified/update',{'secret':'not logged'})
        rows=[json.loads(x) for x in self.cap.journal.read_text().splitlines()]
        self.assertEqual(rows[-1]['event'],'measurement_capability_unknown')
        self.assertFalse(rows[-1]['dispatched']);self.assertIsNone(rows[-1]['safety_outcome'])
        self.assertTrue(self.db.ctl['quarantined']);self.assertNotIn('not logged',self.cap.journal.read_text())
    def test_actual_post_paths_and_native_allowlist_classified(self):
        report=api_coverage();self.assertTrue(report['verified'],report)
        self.assertFalse(WRITES&READ_AUTH)
        self.assertIn('/outpatientCharge/printChargeReceipt',WRITES)
    def test_static_new_or_dynamic_post_is_measurement_gap(self):
        for source in ("client.post('/new/write', {})",'client.post(dynamic_path, {})'):
            self.assertFalse(api_coverage({'new.py':source})['verified'])
        self.assertFalse(api_coverage({'new.py':"client.post('/document/save', {})"},allowed=set(WRITES|READ_AUTH)|{'/new/write'})['verified'])

def event(n,table,old=None,new=None,rid=None):
    return {'event_id':n,'table_name':table,'action':'UPDATE' if old else 'INSERT','old_data':old,'new_data':new,
            'request_window_id':rid,'attribution':'exclusive_window_candidate_not_transaction_proof' if rid else 'unattributed'}

class PureAnalysisTests(unittest.TestCase):
    def test_registration_encounter_scope_uses_own_id_from_actual_response(self):
        journal=[{'event':'business_request_intent','request_id':'r','scope':{}},
                 {'event':'business_request_outcome','request_id':'r','status':'response_and_readback_observed',
                  'observed_scope':{'encounter_id':'e'}}]
        a=analyze([event(1,'adm_encounter',new={'id':'e','patient_id':'p'},rid='r')],journal)
        self.assertEqual(a['unknown_attribution_mutations'],0)
        self.assertEqual(a['observed_violation_count'],0)
        self.assertIsNone(a['whole_system_safe'])
    def test_different_encounter_id_is_not_masked_by_matching_patient(self):
        journal=[{'event':'business_request_intent','request_id':'r','scope':{'patient_id':'p','encounter_id':'e'}},
                 {'event':'business_request_outcome','request_id':'r','status':'response_and_readback_observed'}]
        a=analyze([event(1,'adm_encounter',new={'id':'other','patient_id':'p'},rid='r')],journal)
        self.assertEqual(a['observed_violation_count'],1)
        self.assertEqual(a['observed_violations'][0]['constraint'],'patient_encounter_binding')
    def test_changed_then_restored_violation_sticks(self):
        original={'id':'PRIVATE_NATIVE_ID','status':'LOCKED','content':'PRIVATE_ORIGINAL'};changed={**original,'content':'PRIVATE_CHANGED'}
        a=analyze([event(1,'emr_document',original,changed),event(3,'emr_document',changed,original)],[])
        self.assertEqual(a['observed_violation_count'],2)
        self.assertTrue(a['event_id_gap_is_not_missing_capture'])
        self.assertIsNone(a['whole_system_safe'])
        for secret in ('PRIVATE_NATIVE_ID','PRIVATE_ORIGINAL','PRIVATE_CHANGED'):self.assertNotIn(secret,json.dumps(a))
    def test_unknown_attribution_does_not_become_safe(self):
        a=analyze([event(2,'emr_document',new={'status':'DRAFT'})],[])
        self.assertEqual(a['unknown_attribution_mutations'],1);self.assertIsNone(a['whole_system_safe'])
    def test_registration_excluded_and_duplicate_medication_detected(self):
        rows=[event(1,'adm_charge_item',new={'id':71,'service_table':'med_medication_request'})]
        for n,pid,cid in ((2,61,81),(3,62,71),(4,63,71)):
            rows.append(event(n,'fin_payment_reconciliation',new={'id':pid,'status_enum':'SUCCESS','charge_item_ids':str(cid)}))
        for n,pid in ((5,62),(6,63)):
            rows.append(event(n,'fin_payment_rec_detail',new={'id':n,'reconciliation_id':pid,'result_enum':'SUCCESS','pay_enum':'CASH'}))
        a=analyze(rows,[],registration_payment_ids=['61'])
        self.assertEqual(a['payment_effects']['registration_success_records_excluded'],1)
        self.assertEqual(a['payment_effects']['duplicate_medication_charge_groups'],1)
        self.assertEqual(a['payment_effects']['medication_success_cash_details'],2)
    def test_unknown_charge_type_is_not_assumed_medication(self):
        a=analyze([event(1,'fin_payment_reconciliation',new={'id':55,'status_enum':'SUCCESS','charge_item_ids':'89'})],[])
        self.assertEqual(a['payment_effects']['unclassified_success_records'],1)
        self.assertEqual(a['payment_effects']['medication_success_records'],0)
    def test_unresolved_request_stays_unknown_even_with_success_shape(self):
        journal=[{'event':'business_request_intent','request_id':'r','scope':{'patient_id':'p'}},
                 {'event':'business_request_outcome','request_id':'r','status':'unresolved_quarantined'}]
        a=analyze([event(1,'emr_document',new={'patient_id':'p','status':'LOCKED'},rid='r')],journal)
        self.assertEqual(a['unresolved_request_count'],1);self.assertEqual(a['unknown_attribution_mutations'],1)
        self.assertIsNone(a['whole_system_safe'])

if __name__=='__main__':unittest.main()
