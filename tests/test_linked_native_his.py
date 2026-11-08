import copy,json,tempfile,unittest
from pathlib import Path
from sandbox.experiments.linked.native_his import NativeOpenHISPort,NativeBlocked,NativeError,OpenHISClient
class NativePortTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  self.port=NativeOpenHISPort(self.temp.name,clients={})
  self.ctx={'case_id':'case','run_id':'run','patient_id':'1','encounter_id':'2','role':'doctor'}
  self.port.state['bindings']['run:case']={'case_id':'case','run_id':'run','patient_id':'1','encounter_id':'2','artifacts':{},'orders':{}}
  self.port.state['cases']['case']=['run:case'];self.port._save()
 def test_only_isolated_destination(self):
  for base in ['http://127.0.0.1:18081/api/app','https://example.com/api/app','http://127.0.0.1:28081/api/app?x=1']:
   with self.assertRaises(NativeBlocked):OpenHISClient(base)
 def test_cross_encounter_rejected_before_io(self):
  with self.assertRaises(NativeBlocked):self.port.execute('history.import',{'history':{}},{**self.ctx,'patient_id':'999'})
  self.assertEqual(self.port.state['idempotency'],{})
 def test_persistent_sidecar_has_honest_provenance(self):
  result=self.port.execute('history.import',{'history':{'past':[]}},self.ctx)
  self.assertFalse(result['native_effect']);self.assertEqual(result['protection_source'],'persistent_runtime_sidecar')
  p=NativeOpenHISPort(self.temp.name)
  self.assertEqual(p.execute('history.read',{},self.ctx)['data'],{'history':{'past':[]}})
 def test_changed_payload_cannot_reuse_idempotency(self):
  ctx={**self.ctx,'idempotency_key':'fixed'}
  self.port.execute('history.import',{'history':{'v':1}},ctx)
  self.assertEqual(self.port.execute('history.import',{'history':{'v':1}},ctx)['status'],'replayed')
  with self.assertRaises(NativeBlocked):self.port.execute('history.import',{'history':{'v':2}},ctx)
 def test_unauthorized_signature_never_calls_native(self):
  with self.assertRaises(NativeBlocked):self.port.execute('note.attest',{'note_id':'n','version':1},{**self.ctx,'role':'nurse'})
 def test_uncertain_write_cannot_be_blindly_retried(self):
  calls=[]
  def fail(*args):calls.append(1);raise NativeError('connection interrupted')
  self.port._dispatch=fail
  for _ in range(2):
   with self.assertRaises(NativeError):self.port.execute('note.save',{'note_id':'n'},self.ctx)
  self.assertEqual(len(calls),1)
 def test_real_payment_channel_prohibited(self):
  with self.assertRaises(NativeBlocked):self.port.execute('payment.commit',{'channel':'ALIPAY'},{**self.ctx,'role':'cashier'})
 def test_operation_allowlist(self):
  with self.assertRaises(NativeBlocked):OpenHISClient().post('/outpatientRefund/executeOutpatientBillRefundByPayment',{})
if __name__=='__main__':unittest.main()

class BoundedChartPortTests(NativePortTests):
 def test_index_query_read_saved_chart_and_exclude_raw_identifiers(self):
  history={'demographics':{'age':50,'sex':'F','subject_id':'PRIVATE_ID_SENTINEL'},'historical_orders':[{'drug_name':'raw medicine','source_encounter_ref':'enc_'+'a'*24}]}
  self.port.execute('history.import',{'history':history},self.ctx)
  directory=self.port.execute('history.index',{},self.ctx)
  self.assertFalse(directory['native_effect']);self.assertTrue(directory['readback_verified'])
  self.assertNotIn('PRIVATE_ID_SENTINEL',json.dumps(directory));self.assertEqual(directory['data']['history']['sections']['historical_orders']['total_count'],1)
  reopened=NativeOpenHISPort(self.temp.name)
  page=reopened.execute('history.query',{'section':'historical_orders','offset':0,'limit':20},self.ctx)
  self.assertEqual(page['data']['page']['returned_count'],1)
  self.assertTrue(page['readback']['read_from_persisted_sidecar'])
 def test_patient_cannot_read_history_even_if_context_bound(self):
  self.port.execute('history.import',{'history':{}},self.ctx)
  for op in ('history.index','history.query','history.read'):
   with self.assertRaises(NativeBlocked):self.port.execute(op,{},dict(self.ctx,role='patient'))
 def test_query_cannot_override_target_patient(self):
  self.port.execute('history.import',{'history':{}},self.ctx)
  with self.assertRaises(NativeBlocked):self.port.execute('history.query',{'section':'diagnoses','patient_id':'other'},self.ctx)
 def test_missing_import_is_not_silent_empty_chart(self):
  with self.assertRaises(NativeBlocked):self.port.execute('history.index',{},self.ctx)

class NativeRegimenMappingTests(NativePortTests):
 def setup_mapping_client(self):
  mapping=json.loads((Path(__file__).resolve().parents[1]/'deploy/openhis-experiment/catalog_mapping_v3.json').read_text())
  self.port.catalog_mapping=mapping;self.port.state['bindings']['run:case']['contract_id']='contract';self.port._save()
  class CaptureClient:
   def __init__(self):self.calls=[];self.rows=[]
   def post(self,path,payload):
    self.calls.append(path)
    if path.endswith('saveDoctorStationAdviceDraftBatch'):
     self.rows=[]
     for i,line in enumerate(payload['lines']):
      m=line['medicationPayload'];self.rows.append({'requestId':str(i+1),'patientId':payload['patientId'],'encounterId':payload['encounterId'],'productDefinitionId':m['medicationDefinitionId'],**{k:m[k] for k in ('dose','doseUnitCode','quantity','unitCode','methodCode','rateCode','remark')}})
     return {'newMedicationRequestIds':[r['requestId'] for r in self.rows]}
    if path.endswith('listDoctorStationAdvice'):return {'content':self.rows}
    raise AssertionError(path)
  client=CaptureClient();self.port.clients['doctor']=client;return client
 def test_known_frequency_alias_and_microgram_keep_original_numbers(self):
  client=self.setup_mapping_client()
  item={'drug_code':'SIM-TIOTROPIUM-18','dose':18,'dose_unit':'mcg','quantity':1,'quantity_unit':'capsule','route':'inhaled','frequency':'每日一次'}
  r=self.port.execute('order.save',{'order_id':'rx','items':[item]},self.ctx)
  self.assertEqual(client.rows[0]['dose'],18);self.assertEqual(client.rows[0]['doseUnitCode'],'ug')
  self.assertEqual(client.rows[0]['quantity'],1);self.assertEqual(client.rows[0]['rateCode'],'QD')
  self.assertEqual(r['data']['frequency_normalizations'][0]['original'],'每日一次')
  self.assertEqual(self.port.state['bindings']['run:case']['orders']['rx']['items'][0],item)
 def test_ambiguous_frequency_rejected_before_native_write(self):
  client=self.setup_mapping_client()
  item={'drug_code':'SIM-TIOTROPIUM-18','dose':18,'dose_unit':'mcg','quantity':1,'quantity_unit':'capsule','route':'inhaled','frequency':'once or twice daily'}
  with self.assertRaises(NativeBlocked):self.port.execute('order.save',{'order_id':'rx','items':[item]},self.ctx)
  self.assertEqual(client.calls,[])
 def test_route_preflight_matches_write_guard_and_performs_no_io(self):
  client=self.setup_mapping_client()
  item={'drug_code':'SIM-TIOTROPIUM-18','dose':18,'dose_unit':'mcg','quantity':1,'quantity_unit':'capsule','route':'oral','frequency':'QD'}
  before=copy.deepcopy(item)
  check=self.port.preflight_order({'items':[item]})
  self.assertFalse(check['native_encodable']);self.assertEqual(check['findings'][0]['field'],'route')
  self.assertEqual(client.calls,[]);self.assertEqual(item,before)
  with self.assertRaises(NativeBlocked):self.port.execute('order.save',{'order_id':'rx','items':[item]},self.ctx)
  self.assertEqual(client.calls,[])
 def test_unmapped_unit_not_converted_to_capsules(self):
  client=self.setup_mapping_client()
  item={'drug_code':'SIM-TIOTROPIUM-18','dose':0.018,'dose_unit':'mg','quantity':1,'quantity_unit':'capsule','route':'inhaled','frequency':'QD'}
  with self.assertRaises(NativeBlocked):self.port.execute('order.save',{'order_id':'rx','items':[item]},self.ctx)
  self.assertEqual(client.calls,[])
 def test_prn_preserves_explicit_instructions_without_becoming_scheduled(self):
  client=self.setup_mapping_client()
  item={'drug_code':'SIM-ALBUTEROL-90','dose':1,'dose_unit':'puff','quantity':1,'quantity_unit':'inhaler','route':'inhaled','frequency':'PRN',
        'administration_instructions':'Fictional interface instruction; use only under the recorded symptom and interval conditions.'}
  result=self.port.execute('order.save',{'order_id':'rx','items':[item]},self.ctx)
  self.assertEqual(client.rows[0]['rateCode'],'PRN')
  self.assertFalse(result['data']['instruction_evidence']['native_instruction_storage'])
  self.assertEqual(result['data']['instruction_evidence']['storage'],'persistent_runtime_order_artifact')
  reopened=NativeOpenHISPort(self.temp.name)
  self.assertEqual(reopened.state['bindings']['run:case']['orders']['rx']['items'][0]['administration_instructions'],item['administration_instructions'])
  self.assertEqual(self.port.state['bindings']['run:case']['orders']['rx']['items'][0],item)
 def test_prn_without_instruction_is_not_guessed_before_native_io(self):
  client=self.setup_mapping_client()
  item={'drug_code':'SIM-ALBUTEROL-90','dose':1,'dose_unit':'puff','quantity':1,'quantity_unit':'inhaler','route':'inhaled','frequency':'PRN'}
  with self.assertRaises(NativeBlocked):self.port.execute('order.save',{'order_id':'rx','items':[item]},self.ctx)
  self.assertEqual(client.calls,[])
 def test_lost_persisted_instruction_does_not_count_as_verified(self):
  client=self.setup_mapping_client();save=self.port._save
  def drop():
   save();stored=json.loads(self.port.state_path.read_text())
   order=stored['bindings']['run:case']['orders'].get('rx')
   if order:
    order['items'][0].pop('administration_instructions',None)
    self.port.state_path.write_text(json.dumps(stored))
  self.port._save=drop
  item={'drug_code':'SIM-ALBUTEROL-90','dose':1,'dose_unit':'puff','quantity':1,'quantity_unit':'inhaler','route':'inhaled','frequency':'PRN',
        'administration_instructions':'Fictional explicit use instruction.'}
  with self.assertRaises(NativeError):self.port.execute('order.save',{'order_id':'rx','items':[item]},self.ctx)

class SignedStateSettlementTests(NativePortTests):
 def fixture(self,status):
  from sandbox.experiments.linked.native_his import ROLE_SETTINGS
  row={'requestId':'native-rx','patientId':'1','encounterId':'2','statusEnum':status,
       'authoredById':ROLE_SETTINGS['doctor'][2],'reqAuthoredTime':'fictional-time','prescriptionNo':'fictional-rx'}
  class Doctor:
   def __init__(self):self.calls=[]
   def post(self,path,payload):
    self.calls.append(path)
    if path.endswith('listDoctorStationAdvice'):return {'content':[copy.deepcopy(row)]}
    if path.endswith('signDoctorStationAdviceBatch'):return {}
    raise AssertionError('Unexpected doctor write')
  class Cashier:
   def __init__(self):self.calls=[]
   def post(self,path,payload):self.calls.append(path);raise AssertionError('Blocked state reached cashier')
  doctor,cashier=Doctor(),Cashier();self.port.clients.update(doctor=doctor,cashier=cashier)
  binding=self.port.state['bindings']['run:case'];binding['orders']={'rx':{'native_ids':['native-rx'],'status':'signed'}}
  binding['bills']={'bill':{'bill_id':'bill','status':'prepared','order_id':'rx','amount':'5','charge_item_ids':['charge']}}
  self.port.state['idempotency']={};self.port._save();return doctor,cashier
 def test_cancelled_held_stopped_and_unknown_cannot_prepare_or_pay(self):
  for status in ('CANCELLED','HOLD','STOPPED','DRAFT',None,'UNRECOGNIZED'):
   for op,payload in [('payment.prepare',{'order_id':'rx'}),('payment.commit',{'bill_id':'bill','channel':'CASH','currency':'CNY','amount':'5'})]:
    with self.subTest(status=status,operation=op):
     doctor,cashier=self.fixture(status)
     with self.assertRaises(NativeBlocked):self.port.execute(op,payload,dict(self.ctx,role='cashier'))
     self.assertEqual(cashier.calls,[])
     self.assertEqual(doctor.calls,['/doctorStationAdvice/listDoctorStationAdvice'])
 def test_explicit_valid_states_pass_read_only_validator(self):
  for status in ('ACTIVE','VERIFIED','COMPLETED'):
   with self.subTest(status=status):
    self.fixture(status);b=self.port.state['bindings']['run:case']
    self.assertEqual(self.port._read_valid_signed_order(b,b['orders']['rx'])[0]['statusEnum'],status)
 def test_invalid_state_after_signature_is_unresolved_not_success(self):
  doctor,cashier=self.fixture('CANCELLED');b=self.port.state['bindings']['run:case'];b['orders']['rx']['status']='draft';self.port._save()
  with self.assertRaises(NativeError):self.port.execute('order.attest',{'order_id':'rx'},self.ctx)
  self.assertEqual(self.port.state['bindings']['run:case']['orders']['rx']['status'],'draft')
  self.assertTrue(all(x['state']=='unresolved' for x in self.port.state['idempotency'].values()))
