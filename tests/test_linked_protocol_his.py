import copy,json,tempfile,unittest
from pathlib import Path
from sandbox.experiments.linked.native_his import NativeOpenHISPort,NativeBlocked,ROLE_SETTINGS
from sandbox.experiments.linked.protocol_his import PaperProtocolPort

class Session:
 def __init__(self,role):
  username,org,pid=ROLE_SETTINGS[role]
  self.identity={'userName':username,'practitionerId':pid,'id':'user-'+role,'isActive':True,'isLockedOut':False}
 def post(self,*a):raise AssertionError('Unexpected native request')

class PaperProtocolTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.native=NativeOpenHISPort(self.tmp.name,clients={r:Session(r) for r in ('doctor','nurse','cashier')})
  self.context={'case_id':'c','run_id':'r','patient_id':'p','encounter_id':'e','role':'doctor','actor_id':'doctor-1'}
  self.native.state['bindings']['r:c']={'patient_id':'p','encounter_id':'e','orders':{},'artifacts':{},'consultation_started':True}
  self.native._save();self.p=PaperProtocolPort(self.native)
 def ctx(self,role):return dict(self.context,role=role,actor_id=role+'-1')
 def test_deferred_registration_is_enforced_by_wrapper(self):
  self.assertTrue(self.native.defer_consultation_start)
 def test_missing_queue_encounter_not_repaired_from_context(self):
  with self.assertRaises(NativeBlocked):self.p.execute('queue.enter',{},self.ctx('administrator'))
  self.assertEqual(self.p._load()['queues'],{})
 def test_queue_persists_explicit_bound_id(self):
  r=self.p.execute('queue.enter',{'encounter_id':'e'},self.ctx('administrator'))
  self.assertFalse(r['native_effect']);self.assertEqual(r['data']['encounter_id'],'e')
  self.assertEqual(self.p._load()['queues']['r:c']['encounter_id'],'e')
 def test_wrong_queue_patient_context_cannot_write(self):
  with self.assertRaises(NativeBlocked):self.p.execute('queue.enter',{'encounter_id':'e'},dict(self.ctx('administrator'),patient_id='other'))
 def test_nurse_signatory_rejected_without_changing_doctor_executor(self):
  self.p.execute('queue.enter',{'encounter_id':'e'},self.ctx('administrator'))
  before=copy.deepcopy(self.context)
  with self.assertRaises(NativeBlocked):self.p.execute('order.attest',{'order_id':'rx','signatory_id':ROLE_SETTINGS['nurse'][2]},self.context)
  self.assertEqual(self.context,before)
 def test_registered_signatory_does_not_authenticate_fake_executor(self):
  with self.assertRaises(NativeBlocked):self.p.execute('personnel.registry',{},dict(self.context,actor_id='nurse-1'))
 def test_native_login_principal_must_match_registry(self):
  self.native.clients['doctor'].identity['practitionerId']='other'
  with self.assertRaises(NativeBlocked):self.p.execute('personnel.registry',{},self.context)
 def test_correct_actor_without_signed_proof_cannot_settle(self):
  self.p.execute('queue.enter',{'encounter_id':'e'},self.ctx('administrator'))
  with self.assertRaises(NativeBlocked):self.p.execute('payment.prepare',{'order_id':'rx'},self.ctx('cashier'))
 def test_registry_returns_authenticated_signatory_and_scope(self):
  r=self.p.execute('personnel.registry',{},self.context)
  self.assertEqual(r['data']['signatory_id'],ROLE_SETTINGS['doctor'][2])
  self.assertFalse(r['data']['members'][0]['native_permission_audit'])
  self.assertFalse(r['native_effect'])
if __name__=='__main__':unittest.main()

class ProtocolReadbackRegressionTests(PaperProtocolTests):
 def test_peer_wrapper_native_state_is_refreshed_before_binding_check(self):
  other=NativeOpenHISPort(self.tmp.name,clients=self.native.clients)
  other.state['bindings']['r:c']['patient_id']='changed'
  other._save()
  with self.assertRaises(NativeBlocked):self.p.execute('queue.enter',{'encounter_id':'e'},self.ctx('administrator'))
 def test_exposed_wrong_native_signer_rejected_before_proof(self):
  self.p.execute('queue.enter',{'encounter_id':'e'},self.ctx('administrator'))
  self.native.state['bindings']['r:c']['orders']['rx']={'native_ids':['rx-native'],'status':'draft'};self.native._save()
  self.native.execute=lambda *a:{'status':'executed','readback':[{'authoredById':ROLE_SETTINGS['doctor'][2],'signedById':ROLE_SETTINGS['nurse'][2]}],'data':{'status':'signed'}}
  from sandbox.experiments.linked.native_his import NativeError
  with self.assertRaises(NativeError):self.p.execute('order.attest',{'order_id':'rx','signatory_id':ROLE_SETTINGS['doctor'][2]},self.context)
  self.assertEqual(self.p._load()['attestations'],{})

class ProtocolSignedStateTests(PaperProtocolTests):
 def signed_fixture(self,status):
  self.p.execute('queue.enter',{'encounter_id':'e'},self.ctx('administrator'))
  b=self.native.state['bindings']['r:c'];b['orders']['rx']={'native_ids':['native-rx'],'status':'signed'}
  b['bills']={'bill':{'order_id':'rx','status':'prepared','amount':'5'}};self.native._save()
  self.p.state['attestations']['r:c:rx']={'signatory_id':ROLE_SETTINGS['doctor'][2],'native_ids':['native-rx']};self.p._save()
  row={'requestId':'native-rx','patientId':'p','encounterId':'e','statusEnum':status,
       'authoredById':ROLE_SETTINGS['doctor'][2],'reqAuthoredTime':'fictional-time','prescriptionNo':'fictional-rx'}
  self.native.clients['doctor'].post=lambda *a:{'content':[copy.deepcopy(row)]}
  called=[]
  def execute(*a):called.append(a);raise AssertionError('Invalid signed state forwarded to native payment')
  self.native.execute=execute
  return called,row
 def test_cancelled_held_stopped_never_reach_native_settlement(self):
  for status in ('CANCELLED','HOLD','STOPPED'):
   with self.subTest(status=status):
    calls,row=self.signed_fixture(status)
    for op,payload in [('payment.prepare',{'order_id':'rx'}),('payment.commit',{'bill_id':'bill','amount':'5','currency':'CNY','channel':'CASH'})]:
     with self.assertRaises(NativeBlocked):self.p.execute(op,payload,self.ctx('cashier'))
    self.assertEqual(calls,[])
 def test_invalid_native_signed_state_cannot_create_authorization_proof(self):
  from sandbox.experiments.linked.native_his import NativeError
  calls,row=self.signed_fixture('HOLD');self.p.state['attestations']={};self.p._save()
  self.native.execute=lambda *a:{'status':'executed','readback':[row],'data':{'status':'signed'}}
  with self.assertRaises(NativeError):self.p.execute('order.attest',{'order_id':'rx','signatory_id':ROLE_SETTINGS['doctor'][2]},self.context)
  self.assertEqual(self.p._load()['attestations'],{})
