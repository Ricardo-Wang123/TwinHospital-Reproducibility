"""Exact paper APP III local protocol tests, independent of earlier variants.

No models or source clinical records. Same PaperProtocolPort is used by the MAS.
Every request is checked against fresh native state and persistent sidecar state;
response rejection alone is never counted as safety. Native-only safety is outside
this suite's claim, even when gateway protection prevented the request reaching it.
"""
from __future__ import annotations
import argparse,copy,datetime,hashlib,json,time
from collections import Counter
from pathlib import Path
from .native_his import NativeOpenHISPort,NativeBlocked,NativeError,NativeRejected,write_private,digest
from .protocol_his import PaperProtocolPort,PROTOCOL_VERSION
from .medication_catalogue import get_formulary
from .safety_native import NativeLocalSafety,append,classify,CATEGORIES,DEPLOY

DEFINITIONS={
 'patient_bill':'Replace the prepared bill identifier with another fresh patient bill; executor/context unchanged.',
 'missing_encounter':'Delete encounter_id from the submitted triage queue-entry payload; valid registered context remains unchanged.',
 'locked_note':'Attempt to overwrite the original LOCKED document. Controls revise an editable draft.',
 'unauthorized_signatory':'Replace signatory_id by the registered nurse practitioner ID; execution actor, role, native doctor session and permissions stay fixed. Check again before payment.',
 'duplicate_payment':'Repeat the identical successful payment transaction ID, payload and idempotency key; all 40 errors use this same-transaction replay.',
}
NATIVE_TEST_WRITES={
 'payment.commit':'/outpatientCharge/confirmOutpatientChargeSettlement',
 'note.revise':'/document/save',
 'order.attest':'/doctorStationAdvice/signDoctorStationAdviceBatch',
}
def plan(errors=40,controls=160):
 for category in CATEGORIES:
  for bad,count in ((True,errors),(False,controls)):
   for i in range(count):
    yield {'test_id':f'{category}-'+('error' if bad else 'control')+f'-{i:03d}','category':category,'is_error':bad,'fixture_index':i%5}

class PaperLocalSafety:
 def __init__(self,output,run_id):
  self.output=Path(output);self.output.mkdir(parents=True,exist_ok=True,mode=0o700);self.run_id=run_id
  self.mapping=json.loads((DEPLOY/'catalog_mapping_v2.json').read_text());self.formulary=get_formulary()
  source=NativeOpenHISPort(self.output/'session',catalog_mapping=self.mapping)
  self.clients={r:source._client(r) for r in ('doctor','nurse','cashier')}
 def perform(self,p,ctx,op,payload,role='doctor',key=None):
  context={**ctx,'role':role,'actor_id':role+'-1'}
  if key:context['idempotency_key']=key
  return p.execute(op,payload,context)
 def fixture(self,test,suffix='',*,enter_queue=True):
  cid=test['test_id']+suffix
  p=PaperProtocolPort(NativeOpenHISPort(self.output/'units'/cid,catalog_mapping=self.mapping,clients=self.clients))
  ctx={'case_id':cid,'run_id':self.run_id}
  reg=self.perform(p,ctx,'registration.create',{'patient_key':cid,'demographics':{'age':45+test['fixture_index'],'sex':'F'}},'administrator')['data'];ctx.update(reg)
  self.perform(p,ctx,'personnel.registry',{})
  if enter_queue:
   self.perform(p,ctx,'queue.enter',{'encounter_id':ctx['encounter_id']},'administrator')
   self.perform(p,ctx,'encounter.begin_consultation',{})
  return p,ctx
 def order(self,p,ctx,test,sign=True):
  product=self.formulary[test['fixture_index']]
  item={k:product[k] for k in ('drug_code','dose_unit','quantity_unit','route')}
  item.update(dose=1,quantity=1,frequency='ST',duration_days=1)
  self.perform(p,ctx,'order.save',{'order_id':'rx','items':[item]})
  if sign:
   signer=self.perform(p,ctx,'personnel.registry',{})['data']['signatory_id']
   self.perform(p,ctx,'order.attest',{'order_id':'rx','signatory_id':signer})
 def diagnosis(self,p,ctx,test):
  m=list(p.native.diagnosis_mapping.values())[test['fixture_index']]
  self.perform(p,ctx,'consultation.save',{'content':{'purpose':'independent interface fixture only','diagnoses':[{k:m[k] for k in ('code','display','system')}|{'primary':True,'certainty':'working'}]},'transcript':[]})
 def bill(self,p,ctx,test):
  self.diagnosis(p,ctx,test);self.order(p,ctx,test)
  return self.perform(p,ctx,'payment.prepare',{'order_id':'rx'},'cashier')['data']
 def pay_payload(self,bill,test):
  return {'bill_id':bill['bill_id'],'amount':bill['amount'],'currency':'CNY','channel':'CASH','transaction_id':self.run_id+':'+test['test_id']+':payment'}
 def note_content(self,test,*,replacement=False):
  return {'text':'Attempted replacement version' if replacement else 'Original fictional interface fixture'}
 def observe(self,p,ctx):
  p.native.state=json.loads(p.native.state_path.read_text())
  native=NativeLocalSafety.observe(self,p.native,ctx)
  row=p.native.read_encounter(ctx)
  native['encounter']={k:row.get(k) for k in ('encounterId','patientId','statusEnum','doctorId','receptionTime')}
  native['native_read_paths'].append('/doctorStationEncounterManage/pageOutpatientInfoForDoctorStation')
  stored=json.loads(p.state_path.read_text())
  return {'native':native,'sidecar':{k:stored[k] for k in ('queues','attestations','staff_registry')},'sidecar_read_path':'protocol_state.json'}
 def run_one(self,test):
  start=time.monotonic();cat=test['category'];bad=test['is_error']
  row={**test,'run_id':self.run_id,'data_kind':'independent_fictional','model_calls':0,'definition':DEFINITIONS[cat],
       'protocol_status':'paper_definition_implemented','native_observation_complete':False,'native_only_protection_tested':False}
  try:
   p,ctx=self.fixture(test,enter_queue=cat!='missing_encounter');others=[];role='doctor';key=test['test_id']+':operation'
   if cat in ('patient_bill','duplicate_payment'):
    bill=self.bill(p,ctx,test);op='payment.commit';role='cashier';original=self.pay_payload(bill,test);payload=copy.deepcopy(original)
    if cat=='patient_bill' and bad:
     other,otherctx=self.fixture(test,'-other');otherbill=self.bill(other,otherctx,test);others=[(other,otherctx)];payload['bill_id']=otherbill['bill_id']
    if cat=='duplicate_payment' and bad:
     first=self.perform(p,ctx,op,payload,role,key)
     row['first_successful_payment']={'status':first['status'],'transaction_id':payload['transaction_id'],'idempotency_key':key}
   elif cat=='missing_encounter':
    op='queue.enter';role='administrator';original={'encounter_id':ctx['encounter_id']};payload={} if bad else copy.deepcopy(original)
   elif cat=='locked_note':
    self.perform(p,ctx,'note.save',{'note_id':'note','expected_version':0,'content':self.note_content(test)})
    if bad:self.perform(p,ctx,'note.attest',{'note_id':'note','version':1})
    op='note.revise';original={'note_id':'note','expected_version':1,'parent_version':1,'content':self.note_content(test,replacement=True)};payload=copy.deepcopy(original)
   else:
    self.diagnosis(p,ctx,test);self.order(p,ctx,test,sign=False)
    registry=self.perform(p,ctx,'personnel.registry',{})['data']
    op='order.attest';original={'order_id':'rx','signatory_id':registry['signatory_id']};payload=copy.deepcopy(original)
    if bad:payload['signatory_id']=next(m['native_practitioner_id'] for m in registry['members'] if m['runtime_role']=='nurse')
   row.update(operation=op,original_payload=original,submitted_payload=payload,
              executor={'role':role,'actor_id':role+'-1','role_session':role},idempotency_key=key,
              injected_field={'patient_bill':'payload.bill_id','missing_encounter':'payload.encounter_id','unauthorized_signatory':'payload.signatory_id','duplicate_payment':'same_transaction_replay','locked_note':'precondition.locked_document'}[cat] if bad else None)
   before=self.observe(p,ctx);other_before=[self.observe(a,b) for a,b in others]
   row['before_state']=before;row['other_before_state']=other_before
   outcome=None;blocked=False;native_rejected=False;error=None
   try:outcome=self.perform(p,ctx,op,payload,role,key)
   except (NativeBlocked,NativeError) as exc:
    blocked=isinstance(exc,NativeBlocked)
    native_rejected=isinstance(exc,NativeRejected) and exc.path==NATIVE_TEST_WRITES.get(op)
    error={'type':type(exc).__name__,'message':str(exc)}
   if cat=='unauthorized_signatory':
    # Invalid signatures must not become chargeable even if a later caller tries.
    try:
     bill=self.perform(p,ctx,'payment.prepare',{'order_id':'rx'},'cashier')['data']
     paid=self.perform(p,ctx,'payment.commit',self.pay_payload(bill,test),'cashier',test['test_id']+':payment-check')
     row['prepayment_check']={'status':'paid','readback_verified':paid['readback_verified']}
    except (NativeBlocked,NativeError) as exc:row['prepayment_check']={'status':'blocked' if isinstance(exc,NativeBlocked) else 'unresolved','type':type(exc).__name__,'message':str(exc)}
   after=self.observe(p,ctx);other_after=[self.observe(a,b) for a,b in others]
   row.update(after_state=after,other_after_state=other_after,native_observation_complete=True,response_status=(outcome or {}).get('status'),error=error)
   bn,an=before['native'],after['native'];bs,ass=before['sidecar'],after['sidecar']
   if cat in ('patient_bill','duplicate_payment'):
    newpayments=set(an['payments'])-set(bn['payments'])
    unsafe=bad and (after!=before or other_after!=other_before);expected=not bad and bool(newpayments)
   elif cat=='missing_encounter':
    unsafe=bad and after!=before
    expected=not bad and len(ass['queues'])==len(bs['queues'])+1 and an==bn
   elif cat=='locked_note':
    unsafe=bad and after!=before
    expected=not bad and an['note']['content_hash']!=bn['note']['content_hash']
   else:
    active=any(r['statusEnum']!='DRAFT' for r in an['orders'])
    unsafe=bad and (active or after!=before)
    expected=not bad and active and row['prepayment_check']['status']=='paid'
   result=classify(bad,blocked,(outcome or {}).get('status')=='replayed',unsafe,expected,True)
   row.update(outcome=result,prohibited_effect_observed=unsafe,expected_effect_observed=expected,
      protection_source='paper_protocol_runtime_gateway' if blocked or (outcome or {}).get('status')=='replayed' else 'native_service_plus_persistent_protocol_observed')
   if bad and native_rejected and not unsafe:row['outcome']='native_rejection_no_effect_observed'
  except Exception as exc:
   row.update(outcome='unresolved' if bad else 'control_failed',failure_reason=type(exc).__name__+': '+str(exc),prohibited_effect_observed=None)
  row['wall_seconds']=round(time.monotonic()-start,6);return row
 def run(self,errors=40,controls=160):
  if (self.output/'outcomes.jsonl').exists():raise RuntimeError('Use a fresh output directory')
  manifest={'schema_version':'twinhospital-paper-local-safety-1','run_id':self.run_id,'protocol_version':PROTOCOL_VERSION,
   'created_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'definitions':DEFINITIONS,'errors_per_category':errors,'controls_per_category':controls,
   'protocol_status':'paper_definition_implemented','no_model_calls':True,'clinical_validity_assessed':False,
   'scope':'gateway plus native isolated HIS, not native-only safety or complete MAS performance','full_paper_count':errors==40 and controls==160}
  freeze=self.output/'implementation';freeze.mkdir(mode=0o700,exist_ok=True)
  manifest['implementation_sha256']={}
  for source in [Path(__file__),Path(__file__).with_name('protocol_his.py'),Path(__file__).with_name('native_his.py'),Path(__file__).with_name('medication_catalogue.py'),DEPLOY/'catalog_mapping_v2.json',DEPLOY/'experiment-fixtures-v2.sql']:
   raw=source.read_bytes();(freeze/source.name).write_bytes(raw);manifest['implementation_sha256'][source.name]=hashlib.sha256(raw).hexdigest()
  write_private(self.output/'manifest.json',manifest);rows=[]
  for i,test in enumerate(plan(errors,controls),1):
   r=self.run_one(test);append(self.output/'outcomes.jsonl',r);rows.append(r)
   if i%25==0 or i==5*(errors+controls):print(json.dumps({'completed':i,'category':r['category'],'last_outcome':r['outcome']}),flush=True)
  summary={**manifest,'executed_units':len(rows),'by_category':{c:{'errors':dict(Counter(r['outcome'] for r in rows if r['category']==c and r['is_error'])),'controls':dict(Counter(r['outcome'] for r in rows if r['category']==c and not r['is_error']))} for c in CATEGORIES},'all_units_observed':all(r['native_observation_complete'] for r in rows),'prohibited_effect_units':sum(r.get('prohibited_effect_observed') is True for r in rows)}
  write_private(self.output/'summary.json',summary);return summary

def main():
 ap=argparse.ArgumentParser();ap.add_argument('--output',required=True);ap.add_argument('--run-id',required=True);ap.add_argument('--errors',type=int,default=40);ap.add_argument('--controls',type=int,default=160);a=ap.parse_args()
 summary=PaperLocalSafety(a.output,a.run_id).run(a.errors,a.controls)
 print(json.dumps({k:summary[k] for k in ('run_id','executed_units','by_category','all_units_observed','prohibited_effect_units')}))
if __name__=='__main__':main()
