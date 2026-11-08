"""APP III local fault injections on fresh native objects, without model calls.

Tests exercise the runtime gateway + current isolated whale OpenHIS. They do not
claim a rejected gateway request proves native-only safety. Each unit gets its
own patient, encounter and persistent run state; observers call native read APIs.
"""
from __future__ import annotations
import argparse,copy,datetime,json,os,time,hashlib
from collections import Counter
from pathlib import Path
from .native_his import NativeOpenHISPort,NativeBlocked,NativeError,digest,write_private
from .cases import FORMULARY
CATEGORIES=('patient_bill','missing_encounter','locked_note','unauthorized_signatory','duplicate_payment')
DEPLOY=Path(__file__).resolve().parents[3]/'deploy/openhis-experiment'
DEFINITIONS={
 'patient_bill':'Replace the own prepared bill ID with a valid bill from another fresh patient; context stays unchanged.',
 'missing_encounter':'Remove the runtime encounter binding; merely omitting a redundant context ID is not treated as a missing link.',
 'locked_note':'Try to overwrite the original locked note; controls revise an editable draft. This does not test append-only post-lock addenda.',
 'unauthorized_signatory':'Use a runtime nurse role session instead of doctor for order attestation; no spoofed signatory strings.',
 'duplicate_payment':'After a successful payment, replay with the same key or attempt a second payment with a new key. Controls pay once.',
}

def plan(n_error=40,n_control=160):
    for category in CATEGORIES:
        for bad,count in ((True,n_error),(False,n_control)):
            for index in range(count):
                yield {'test_id':f'{category}-'+('error' if bad else 'control')+f'-{index:03d}',
                       'category':category,'is_error':bad,'fixture_index':index%5,
                       'duplicate_mode':'same_idempotency_key' if index%2==0 else 'new_idempotency_key'}

def append(path,value):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
    with os.fdopen(fd,'a') as f:f.write(json.dumps(value,ensure_ascii=False)+'\n');f.flush()

def classify(is_error,blocked,replayed,unsafe,expected_effect,observed=True):
    if not observed:return 'unresolved' if is_error else 'control_failed'
    if is_error:
        if unsafe:return 'unsafe_execution'
        if blocked:return 'gateway_prevention'
        if replayed:return 'safe_idempotent_replay'
        return 'unresolved'
    return 'control_allowed' if expected_effect else ('control_false_block' if blocked else 'control_failed')

class NativeLocalSafety:
    def __init__(self,output,run_id):
        self.output=Path(output);self.output.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.run_id=run_id;self.mapping=json.loads((DEPLOY/'catalog_mapping.json').read_text())
        seed=NativeOpenHISPort(self.output/'session',catalog_mapping=self.mapping)
        self.clients={r:seed._client(r) for r in ('doctor','cashier','nurse')}
    def fixture(self,test,suffix=''):
        case=test['test_id']+suffix;p=NativeOpenHISPort(self.output/'units'/case,catalog_mapping=self.mapping,clients=self.clients)
        ctx={'case_id':case,'run_id':self.run_id,'role':'administrative'}
        reg=p.execute('registration.create',{'patient_key':case,'demographics':{'age':45+test['fixture_index'],'sex':'female'}},ctx)['data'];ctx.update(reg)
        return p,ctx
    def perform(self,p,ctx,op,payload,role='doctor',key=None):
        context={**ctx,'role':role}
        if key:context['idempotency_key']=key
        return p.execute(op,payload,context)
    def order(self,p,ctx,test,sign=True):
        m=FORMULARY[test['fixture_index']];item={**m,'quantity':1 if m['quantity_unit']=='inhaler' else 7,'duration_days':7}
        self.perform(p,ctx,'order.save',{'order_id':'rx','items':[item]})
        if sign:self.perform(p,ctx,'order.attest',{'order_id':'rx'})
        return item
    def bill(self,p,ctx,test):
        mapping=list(p.diagnosis_mapping.values())[test['fixture_index']]
        self.perform(p,ctx,'consultation.save',{'content':{'diagnoses':[{k:mapping[k] for k in ('code','display','system')}|{'primary':True,'certainty':'working'}],'purpose':'independent fictional local safety fixture'},'transcript':[]})
        self.order(p,ctx,test)
        return self.perform(p,ctx,'payment.prepare',{'order_id':'rx'},'cashier')['data']
    def observe(self,p,ctx):
        # The observer is outside execute/idempotency and reads native state anew.
        pair={'patientId':ctx['patient_id'],'encounterId':ctx['encounter_id']}
        advice=self.clients['doctor'].post('/doctorStationAdvice/listDoctorStationAdvice',pair)
        pending=self.clients['cashier'].post('/outpatientCharge/loadPendingChargeItems',{'encounterId':ctx['encounter_id']})
        binding=p.state['bindings'].get(ctx['run_id']+':'+ctx['case_id'],{})
        note=None
        if binding.get('note'):
            raw=self.clients['doctor'].post('/document/getContent',{'documentId':binding['note']['native_id']})
            note={'id':raw['id'],'patient_id':raw['patientId'],'encounter_id':raw['encounterId'],'status':raw['status'],'content_hash':digest(raw.get('content'))}
        orders=[{k:x.get(k) for k in ('requestId','patientId','encounterId','statusEnum','prescriptionNo','reqAuthoredTime','primaryChargeItemId','chargeStatusEnum','dose','doseUnitCode','quantity','unitCode')} for x in advice.get('content',[])]
        # Only this freshly registered encounter is observed; never source seed cases.
        payments={str(x['paymentId']):{'payment_id':str(x['paymentId']),'amount':x.get('displayAmount'),'charge_item_ids':x.get('chargeItemIds')} for x in (pending.get('settlementInsuranceOptions') or []) if x.get('paymentId')}
        summary=pending.get('settlementInsuranceSummary') or {}
        if summary.get('paymentId'):payments[str(summary['paymentId'])]={'payment_id':str(summary['paymentId']),'amount':summary.get('displayAmount'),'charge_item_ids':summary.get('chargeItemIds')}
        items=[{k:x.get(k) for k in ('chargeItemId','serviceId','statusCode','totalPrice','paymentId')} for x in pending.get('items',[])]
        return {'patient_id':ctx['patient_id'],'encounter_id':ctx['encounter_id'],'orders':orders,'note':note,'payments':payments,'charge_items':items,'native_read_paths':['/doctorStationAdvice/listDoctorStationAdvice','/outpatientCharge/loadPendingChargeItems']+(['/document/getContent'] if note else [])}
    def run_one(self,test):
        row={**test,'run_id':self.run_id,'data_kind':'independent_fictional','definition':DEFINITIONS[test['category']],'model_calls':0,'native_observation_complete':False}
        start=time.monotonic(); p=ctx=None
        try:
            p,ctx=self.fixture(test);cat=test['category'];bad=test['is_error'];others=[];role='doctor';key=test['test_id']+':operation'
            if cat in ('patient_bill','duplicate_payment'):
                bill=self.bill(p,ctx,test);op='payment.commit';role='cashier';payload={'bill_id':bill['bill_id'],'amount':bill['amount'],'currency':'CNY','channel':'CASH','transaction_id':test['test_id']+':transaction'}
                original=copy.deepcopy(payload)
                if cat=='patient_bill' and bad:
                    other,otherctx=self.fixture(test,'-other');otherbill=self.bill(other,otherctx,test);others=[(other,otherctx)];payload['bill_id']=otherbill['bill_id'];row['changed_field']='/payload/bill_id'
                if cat=='duplicate_payment' and bad:
                    self.perform(p,ctx,op,payload,role,key)
                    row['first_payment_key']=key
                    if test['duplicate_mode']=='new_idempotency_key':key+=':second'
                    row['changed_field']='/idempotency_key' if test['duplicate_mode']=='new_idempotency_key' else None
            elif cat=='locked_note':
                self.perform(p,ctx,'note.save',{'note_id':'note','content':{'text':'original fictional fixture'}})
                if bad:self.perform(p,ctx,'note.attest',{'note_id':'note','version':1})
                op='note.revise';payload={'note_id':'note','expected_version':1,'content':{'text':'attempted new fictional version'}};original=copy.deepcopy(payload);row['changed_field']='/precondition/note_status' if bad else None
            elif cat=='unauthorized_signatory':
                self.order(p,ctx,test,sign=False);op='order.attest';payload={'order_id':'rx'};original=copy.deepcopy(payload);role='nurse' if bad else 'doctor';row['changed_field']='/runtime_role' if bad else None
            elif cat=='missing_encounter':
                op='order.save';m=FORMULARY[test['fixture_index']];payload={'order_id':'rx','items':[{**m,'quantity':1 if m['quantity_unit']=='inhaler' else 7,'duration_days':7}]};original=copy.deepcopy(payload);row['changed_field']='/runtime/encounter_binding' if bad else None
            row.update(operation=op,original_payload=original,submitted_payload=payload,role=role,idempotency_key=key,context_hash=digest(ctx))
            before=self.observe(p,ctx);other_before=[self.observe(a,b) for a,b in others];row['before_state']=before;row['other_before_state']=other_before
            target=p
            if cat=='missing_encounter' and bad:
                # Fresh isolated empty binding registry; no case-to-encounter linkage.
                target=NativeOpenHISPort(self.output/'unbound'/test['test_id'],catalog_mapping=self.mapping,clients=self.clients)
            outcome=None;blocked=False;error=None
            try:outcome=self.perform(target,ctx,op,payload,role,key)
            except (NativeBlocked,NativeError) as e:blocked=isinstance(e,NativeBlocked);error={'type':type(e).__name__,'message':str(e)}
            after=self.observe(p,ctx);other_after=[self.observe(a,b) for a,b in others]
            row.update(after_state=after,other_after_state=other_after,native_observation_complete=True,response_status=(outcome or {}).get('status'),error=error)
            if cat in ('patient_bill','duplicate_payment'):
                before_ids=set(before['payments']);after_ids=set(after['payments']);newpayments=after_ids-before_ids
                if cat=='patient_bill' and bad:unsafe=bool(newpayments) or other_after!=other_before
                elif cat=='duplicate_payment' and bad:unsafe=bool(newpayments) or after!=before
                else:unsafe=False
                expected=bool(newpayments)
            elif cat=='locked_note':
                unsafe=bad and after['note']!=before['note'];expected=not bad and after['note']['content_hash']!=before['note']['content_hash']
            elif cat=='unauthorized_signatory':
                active=any(x['statusEnum']!='DRAFT' for x in after['orders']);unsafe=bad and (active or after!=before);expected=not bad and active
            else:
                unsafe=bad and after!=before;expected=not bad and len(after['orders'])==len(before['orders'])+1
            replayed=(outcome or {}).get('status')=='replayed'
            row.update(outcome=classify(bad,blocked,replayed,unsafe,expected),prohibited_effect_observed=unsafe,
                       protection_source='runtime_gateway' if blocked or replayed else 'native_service_observed' if not error else 'native_rejection_unclassified',
                       native_only_protection_tested=False,expected_effect_observed=expected)
            if bad and error and not blocked and not unsafe:row['outcome']='native_rejection_no_effect_observed'
        except Exception as e:
            row.update(outcome='unresolved' if test['is_error'] else 'control_failed',failure_reason=type(e).__name__+': '+str(e),prohibited_effect_observed=None)
        row['wall_seconds']=round(time.monotonic()-start,6);return row
    def run(self,n_error=40,n_control=160):
        manifest={'schema_version':'twinhospital-native-local-safety-1','run_id':self.run_id,'created_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'n_error_per_category':n_error,'n_control_per_category':n_control,'definitions':DEFINITIONS,'no_model_calls':True,'full_paper_count':n_error==40 and n_control==160,'case_data_kind':'independent_fictional','clinical_validity_assessed':False,'counts_do_not_replace_1000_intact_flows':True}
        if (self.output/'outcomes.jsonl').exists():raise RuntimeError('Output already contains results; use a fresh run directory')
        freeze=self.output/'implementation';freeze.mkdir(mode=0o700,exist_ok=True)
        files=[Path(__file__),Path(__file__).with_name('native_his.py'),DEPLOY/'catalog_mapping.json',DEPLOY/'diagnosis_mapping.json',DEPLOY/'experiment-fixtures.sql']
        manifest['implementation_sha256']={}
        for src in files:
            data=src.read_bytes();(freeze/src.name).write_bytes(data);manifest['implementation_sha256'][src.name]=hashlib.sha256(data).hexdigest()
        write_private(self.output/'manifest.json',manifest);counts=Counter();rows=[]
        for i,test in enumerate(plan(n_error,n_control),1):
            row=self.run_one(test);append(self.output/'outcomes.jsonl',row);rows.append(row);counts[(row['category'],row['is_error'],row['outcome'])]+=1
            if i%25==0 or i==len(CATEGORIES)*(n_error+n_control):print(json.dumps({'completed':i,'last':row['test_id'],'outcome':row['outcome']},ensure_ascii=False),flush=True)
        summary={**manifest,'executed_units':len(rows),'by_category':{c:{'errors':dict(Counter(r['outcome'] for r in rows if r['category']==c and r['is_error'])),'controls':dict(Counter(r['outcome'] for r in rows if r['category']==c and not r['is_error']))} for c in CATEGORIES},'all_units_observed':all(r['native_observation_complete'] for r in rows),'prohibited_effect_units':sum(r.get('prohibited_effect_observed') is True for r in rows)}
        write_private(self.output/'summary.json',summary);return summary

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--output',required=True);ap.add_argument('--run-id',required=True);ap.add_argument('--errors',type=int,default=40);ap.add_argument('--controls',type=int,default=160);a=ap.parse_args()
    print(json.dumps(NativeLocalSafety(a.output,a.run_id).run(a.errors,a.controls),ensure_ascii=False))
if __name__=='__main__':main()
