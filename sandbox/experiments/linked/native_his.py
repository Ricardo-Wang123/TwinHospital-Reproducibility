"""Current whale OpenHIS adapter for an isolated local experiment.

Only the allowlisted application APIs are callable. HTTP writes are never retried
blindly: request intent is journalled before dispatch. Native effects and runtime
sidecar protections are reported separately. Application LOCKED is not a CA signature.
"""
from __future__ import annotations
import copy, datetime as dt, fcntl, hashlib, json, os, secrets, threading, calendar
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit
import requests
from .chart import HistoryChart
from .order_encoding import encode_items, preflight as preflight_encoding, OrderEncodingError

class NativeError(RuntimeError): pass
class NativeBlocked(NativeError): pass

def isolated_port():
    port = int(os.environ.get('TH_OPENHIS_PORT', '28081'))
    project = os.environ.get('TH_OPENHIS_PROJECT', 'twinhospital-exp')
    if port != 28081 and not (28082 <= port <= 28199 and project.startswith('twinhospital-exp-smoke-')):
        raise NativeBlocked('An alternate loopback port requires a dedicated isolated smoke project.')
    return port

def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def write_private(path,value):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    temp=path.with_name(path.name+'.'+secrets.token_hex(5)+'.tmp')
    fd=os.open(temp,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    with os.fdopen(fd,'w') as f: json.dump(value,f,ensure_ascii=False,indent=2); f.flush(); os.fsync(f.fileno())
    os.replace(temp,path)
def now():return dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).replace(tzinfo=None).isoformat(timespec='seconds')

ALLOWED={
 '/auth/login','/auth/selectTenant','/auth/selectHospital','/auth/selectOrganizationUnit','/auth/getAvailableOrganizations',
 '/patient/createPatient','/patient/getPatient','/patient/getPatientPage',
 '/outpatientRegistration/loadRegisterFormInit','/outpatientRegistration/listPractitioners','/outpatientRegistration/listBookableSlotHints','/outpatientRegistration/preRegisterSettlement','/outpatientRegistration/payRegisterFee','/outpatientRegistration/pageClinicRegisters',
 '/doctorStationEncounterManage/startOutpatientReception','/doctorStationEncounterManage/pageOutpatientInfoForDoctorStation',
 '/doctorStationAdvice/initDoctorStationAdvice','/doctorStationAdvice/pageDoctorStationAdviceCatalog','/doctorStationAdvice/listDoctorStationAdvice','/doctorStationAdvice/getEncounterAdviceDefaultContract','/doctorStationAdvice/saveDoctorStationAdviceDraftBatch','/doctorStationAdvice/signDoctorStationAdviceBatch',
 '/doctorStationDiagnosis/getConditionDefinitionMetadata','/doctorStationDiagnosis/saveDoctorDiagnosis','/doctorStationDiagnosis/getEncounterDiagnosis',
 '/documentType/getList','/document/create','/document/save','/document/getContent','/document/submit','/document/changeLockState',
 '/outpatientCharge/loadPendingChargeItems','/outpatientCharge/preSettleOutpatientCharges','/outpatientCharge/confirmOutpatientChargeSettlement','/outpatientCharge/printChargeReceipt',
}
class OpenHISClient:
    def __init__(self,base_url='http://127.0.0.1:28081/api/app',*,timeout=45,session=None):
        u=urlsplit(base_url)
        if u.scheme!='http' or u.hostname not in ('127.0.0.1','localhost','::1') or u.port!=isolated_port() or u.path.rstrip('/')!='/api/app' or u.query or u.fragment or u.username:
            raise NativeBlocked('Native adapter accepts only the isolated loopback /api/app endpoint')
        self.base=base_url.rstrip('/'); self.timeout=timeout; self.session=session or requests.Session(); self.session.trust_env=False; self.token=None; self.identity={}
    def post(self,path,payload=None):
        if path not in ALLOWED:raise NativeBlocked('Operation not allowlisted: '+path)
        headers={'Content-Type':'application/json'}
        if self.token:headers['Authorization']='Bearer '+self.token
        try:
            response=self.session.post(self.base+path,json=payload or {},headers=headers,timeout=self.timeout,allow_redirects=False)
            if response.status_code!=200:raise NativeError('HTTP '+str(response.status_code)+' on '+path)
            body=response.json()
        except (requests.RequestException,ValueError) as e:raise NativeError('Uncertain request outcome on '+path+'; no blind retry') from e
        if not isinstance(body,dict) or body.get('success') is not True:
            code=body.get('errorCode','UNKNOWN') if isinstance(body,dict) else 'INVALID_RESPONSE'
            msg=str(body.get('message',''))[:240] if isinstance(body,dict) else ''
            raise NativeError(path+' rejected '+str(code)+': '+msg)
        return body.get('data')
    def login(self,username,password,*,tenant='1',hospital='322663105141149696',organization=None):
        data=self.post('/auth/login',{'userName':username,'password':password})
        self.token=data['accessToken']; self.identity=data.get('user',{})
        self.post('/auth/selectTenant',{'tenantId':str(tenant)})
        self.post('/auth/selectHospital',{'hospitalId':str(hospital)})
        if organization:self.post('/auth/selectOrganizationUnit',{'organizationUnitId':str(organization)})
        return {'authenticated':True,'user_id':str(self.identity.get('id')),'practitioner_id':str(self.identity.get('practitionerId') or '')}

ROLE_SETTINGS={
 'doctor':('zhangsining','322663218269917184','8200000000000000204'),
 'nurse':('zhaoyuning','322663652522987520','8200000000000000208'),
 'cashier':('songzixi','322663815429754880','8200000000000000209'),
 'pharmacist':('zhengzhihe','8100000000000000018','8200000000000000210'),
}
VALID_SIGNED_REQUEST_STATES=frozenset({'ACTIVE','VERIFIED','COMPLETED'})
SIGNED_REQUEST_POLICY_VERSION='native-signed-request-state-2'
# Source: OpenHIS RequestStatus.java. HOLD/CANCELLED/STOPPED are explicitly
# distinct from an active/verified/completed signed request. No non-DRAFT shortcut.

class NativeOpenHISPort:
    backend_name='openhis_whale_native'
    def __init__(self,state_dir,*,credentials_path=None,base_url='http://127.0.0.1:28081/api/app',clients=None,catalog_mapping=None,diagnosis_mapping=None,document_type='OUTPATIENT',defer_consultation_start=False):
        self.root=Path(state_dir);self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.state_path=self.root/'native_state.json';self.journal=self.root/'native_operations.jsonl'
        self.state=json.loads(self.state_path.read_text()) if self.state_path.exists() else {'bindings':{},'idempotency':{},'cases':{}}
        self.clients=clients or {};self.credentials_path=Path(credentials_path) if credentials_path else Path(__file__).resolve().parents[3]/'deploy/openhis-experiment/runtime/accounts/credentials.json'
        self.diagnosis_mapping=diagnosis_mapping if diagnosis_mapping is not None else json.loads((Path(__file__).resolve().parents[3]/'deploy/openhis-experiment/diagnosis_mapping.json').read_text())
        self.defer_consultation_start=defer_consultation_start
        self.base_url=base_url;self.catalog_mapping=catalog_mapping or {};self.document_type=document_type;self._lock=threading.RLock()
    def _client(self,role):
        if role not in self.clients:
            if self.credentials_path.stat().st_mode & 0o077:raise NativeBlocked('Credentials must have mode 0600')
            credentials=json.loads(self.credentials_path.read_text());username,org,pid=ROLE_SETTINGS[role]
            c=OpenHISClient(self.base_url); c.login(username,credentials[username],organization=org);self.clients[role]=c
        return self.clients[role]
    def _save(self):write_private(self.state_path,self.state)
    def _event(self,event):
        fd=os.open(self.journal,os.O_CREAT|os.O_APPEND|os.O_WRONLY,0o600)
        with os.fdopen(fd,'a') as f:f.write(json.dumps(event,ensure_ascii=False)+'\n')
    def _binding(self,ctx):
        key=ctx['run_id']+':'+ctx['case_id']; b=self.state['bindings'].get(key)
        if not b:raise NativeBlocked('Encounter not registered for this case/run')
        for field in ('patient_id','encounter_id'):
            if ctx.get(field) and str(ctx[field])!=str(b[field]):raise NativeBlocked('Cross-patient/encounter target rejected')
        return b
    def execute(self,operation,payload=None,context=None):
        if isinstance(operation,dict):
            action=operation;context=payload if context is None else context;operation=action.get('operation') or action.get('name');payload=action.get('payload',{})
        payload=payload or {};ctx=dict(context or {})
        if not ctx.get('run_id') or not ctx.get('case_id'):raise NativeBlocked('Missing case/run identity')
        op=str(operation); key=ctx.get('idempotency_key') or digest([ctx['run_id'],ctx['case_id'],op,payload]);finger=digest([op,payload,ctx.get('patient_id'),ctx.get('encounter_id'),ctx.get('role')])
        with self._lock:
            lockfd=os.open(self.root/'native.lock',os.O_CREAT|os.O_RDWR,0o600)
            try:
                fcntl.flock(lockfd,fcntl.LOCK_EX)
                if self.state_path.exists():self.state=json.loads(self.state_path.read_text())
                old=self.state['idempotency'].get(key)
                if old:
                    if old['fingerprint']!=finger:raise NativeBlocked('Idempotency key reused with changed payload')
                    if old['state']=='complete':
                        result=copy.deepcopy(old['result']);result['status']='replayed';return result
                    raise NativeBlocked('Previous request is unresolved; reconcile native state before retry')
                role=ctx.get('role','')
                if op in ('order.save','order.attest','note.attest','consultation.save','encounter.begin_consultation') and role not in ('doctor','physician','attending_physician'):
                    raise NativeBlocked('Only physician role may write or attest clinical outputs')
                if op in ('note.save','note.revise') and role not in ('doctor','physician','attending_physician','scribe'):
                    raise NativeBlocked('Draft recording role not authorized')
                if op=='payment.commit' and role not in ('cashier','administrative','administrator','billing'):
                    raise NativeBlocked('Payment role not authorized')
                if op in ('history.index','history.query','history.read') and role not in ('doctor','physician','attending_physician','nurse','pharmacist'):
                    raise NativeBlocked('Hospital history is restricted to clinical staff views')
                if op=='history.import' and role not in ('administrator','administrative','doctor','physician','attending_physician'):
                    raise NativeBlocked('History import is restricted to the encounter runtime')
                if op!='registration.create':self._binding(ctx)
                self.state['idempotency'][key]={'state':'in_flight','fingerprint':finger,'operation':op};self._save()
                try:
                    result=self._dispatch(op,payload,ctx)
                    self.state['idempotency'][key].update(state='complete',result=result);self._save()
                except Exception as e:
                    self.state['idempotency'][key].update(state='unresolved',error=type(e).__name__);self._save()
                    self._event({'at':now(),'run_id':ctx['run_id'],'case_id':ctx['case_id'],'operation':op,'idempotency_key':key,'request_hash':digest(payload),'status':'unresolved','error_type':type(e).__name__,'error':str(e)[:300]});raise
                self._event({'at':now(),'run_id':ctx['run_id'],'case_id':ctx['case_id'],'operation':op,'idempotency_key':key,'request_hash':digest(payload),'result':result})
                return result
            finally:os.close(lockfd)
    def _result(self,data,readback,*,native=True,source='native_service_and_runtime_gateway'):
        return {'status':'executed','data':data,'readback':readback,'readback_verified':True,'native_effect':native,'protection_source':source,'backend':self.backend_name}
    def _read_valid_signed_order(self,b,order):
        ids=[str(x) for x in order.get('native_ids',[])]
        if not ids or len(set(ids))!=len(ids):raise NativeBlocked('Signed native request identities invalid')
        pair={'patientId':b['patient_id'],'encounterId':b['encounter_id']}
        rows=self._client('doctor').post('/doctorStationAdvice/listDoctorStationAdvice',pair).get('content',[])
        selected=[r for r in rows if str(r.get('requestId')) in ids]
        if len(selected)!=len(ids) or len({str(r.get('requestId')) for r in selected})!=len(ids):
            raise NativeBlocked('Signed native requests not completely observed')
        principal=ROLE_SETTINGS['doctor'][2]
        for row in selected:
            if (str(row.get('patientId'))!=b['patient_id'] or str(row.get('encounterId'))!=b['encounter_id'] or
                row.get('statusEnum') not in VALID_SIGNED_REQUEST_STATES or not row.get('reqAuthoredTime') or not row.get('prescriptionNo') or
                str(row.get('authoredById'))!=principal or (row.get('signedById') and str(row['signedById'])!=principal)):
                raise NativeBlocked('Native request is not in an authorized payable signed state')
        return selected
    def _dispatch(self,op,p,ctx):
        if op=='registration.create':return self._register(p,ctx)
        b=self._binding(ctx); pair={'patientId':b['patient_id'],'encounterId':b['encounter_id']}; store=b.setdefault('artifacts',{})
        if op=='encounter.begin_consultation':
            c=self._client('doctor');principal=str(c.identity.get('practitionerId') or '')
            if principal!=ROLE_SETTINGS['doctor'][2]:raise NativeBlocked('Authenticated doctor principal mismatch')
            c.post('/doctorStationEncounterManage/startOutpatientReception',{'encounterId':b['encounter_id'],'doctorId':principal,'operatorId':principal})
            rb=self.read_encounter(ctx)
            if rb.get('statusEnum')!='IN_PROGRESS' or str(rb.get('doctorId'))!=principal:raise NativeError('Native reception transition/operator not observed')
            b['consultation_started']=True
            return self._result({'status':'in_progress','encounter_id':b['encounter_id'],'doctor_id':principal},rb)
        if op=='diagnosis.catalogue':return self._diagnosis_catalogue(pair)
        if op=='consultation.save':return self._save_consultation(p,ctx,b,pair)
        if op in ('history.import','handoff.save'):
            name={'history.import':'history','handoff.save':'handoff','consultation.save':'consultation'}[op]
            store[name]=copy.deepcopy(p);self._save();read=json.loads(self.state_path.read_text())['bindings'][ctx['run_id']+':'+ctx['case_id']]['artifacts'][name]
            return self._result(read,read,native=False,source='persistent_runtime_sidecar')
        if op in ('history.index','history.query'):
            # Chart selection is bound to the encounter, never a caller-provided ID.
            allowed = {'history'} if op=='history.index' else {'section','encounter_key','term','offset','limit'}
            if set(p)-allowed:raise NativeBlocked('Unsupported chart query fields')
            if op=='history.index' and 'history' in p:
                if store.get('history') is not None and store['history'].get('history')!=p['history']:
                    raise NativeBlocked('A chart index cannot replace the imported history')
                store['history']={'history':copy.deepcopy(p['history'])};self._save()
            read=json.loads(self.state_path.read_text())['bindings'][ctx['run_id']+':'+ctx['case_id']]['artifacts'].get('history')
            if not isinstance(read,dict) or not isinstance(read.get('history'),dict):raise NativeBlocked('History not imported for this encounter')
            chart=HistoryChart(read['history']); chart_hash=digest(read['history'])
            if op=='history.index':
                value=chart.initial_view(); field='history'
                store['history_index']={'source_hash':chart_hash,'chart_version':value.get('schema_version','bounded_history_v1')};self._save()
            else:
                value=chart.query(**p);field='page'
            return self._result({field:value},{'artifact':'history','source_hash':chart_hash,'read_from_persisted_sidecar':True},native=False,source='persistent_runtime_sidecar')
        if op in ('history.read','handoff.read'):

            name=op.split('.')[0]; value=store.get(name)
            if value is None:raise NativeBlocked('Artifact not persisted')
            return self._result(value,value,native=False,source='persistent_runtime_sidecar')
        if op in ('note.save','note.revise'):return self._save_note(p,ctx,b,pair)
        if op=='note.attest':
            note=b.get('note');
            if not note or p.get('note_id')!=note['logical_id'] or p.get('version')!=note['version']:raise NativeBlocked('Note version/identity mismatch')
            c=self._client('doctor');c.post('/document/submit',{'documentId':note['native_id']});c.post('/document/changeLockState',{'documentId':note['native_id'],'action':'LOCK'})
            rb=c.post('/document/getContent',{'documentId':note['native_id']})
            if rb.get('status')!='LOCKED':raise NativeError('Native note lock not observed')
            note['status']='locked';self._save();return self._result({'status':'locked','version':note['version'],'document_id':note['native_id'],'attestation_kind':'physician_role_application_lock'},rb)
        if op=='order.save':return self._save_order(p,ctx,b,pair)
        if op=='order.attest':
            order=b['orders'].get(p.get('order_id'))
            if not order:raise NativeBlocked('Unknown order')
            c=self._client('doctor');c.post('/doctorStationAdvice/signDoctorStationAdviceBatch',{**pair,'signTargets':[{'productTableName':'med_medication_definition','requestId':rid} for rid in order['native_ids']]})
            try:selected=self._read_valid_signed_order(b,order)
            except NativeBlocked as exc:raise NativeError('Native signed order not verified after signature request') from exc
            order['status']='signed';return self._result({'order_id':p['order_id'],'status':'signed','native_ids':order['native_ids']},selected)
        if op=='payment.prepare':
            order=b.get('orders',{}).get(p.get('order_id'))
            if not order or order.get('status')!='signed':raise NativeBlocked('Signed order required')
            self._read_valid_signed_order(b,order)
            c=self._client('cashier');pending=c.post('/outpatientCharge/loadPendingChargeItems',{'encounterId':b['encounter_id']})
            items=[x for x in pending.get('items',[]) if str(x.get('serviceId')) in order['native_ids'] and x.get('statusCode') not in ('BILLED','PAID')]
            if len(items)!=len(order['native_ids']):raise NativeError('Expected native charge items not observed')
            ids=[str(x['chargeItemId']) for x in items];draft=c.post('/outpatientCharge/preSettleOutpatientCharges',{'encounterId':b['encounter_id'],'chargeItemIds':ids,'contractId':b['contract_id']})
            if draft.get('insurancePreSettled') or draft.get('settlementComposition') not in (None,'SELF_PAY_ONLY'):raise NativeBlocked('Self-pay settlement required')
            bill={'bill_id':str(draft['paymentDraftId']),'amount':str(draft['displayAmount']),'currency':'CNY','charge_item_ids':ids,'order_id':p['order_id'],'status':'prepared'}
            b.setdefault('bills',{})[bill['bill_id']]=bill
            return self._result(bill,{'draft':draft,'native_charge_items':items})
        if op=='payment.commit':
            if p.get('channel')!='CASH':raise NativeBlocked('Only local CASH bookkeeping is permitted')
            bill=b.get('bills',{}).get(p.get('bill_id'))
            if not bill or bill['status']!='prepared':raise NativeBlocked('Bill not payable or already paid')
            if str(p.get('currency'))!='CNY' or Decimal(str(p.get('amount')))!=Decimal(bill['amount']):raise NativeBlocked('Bill currency/amount mismatch')
            order=b.get('orders',{}).get(bill.get('order_id'))
            if not order or order.get('status')!='signed':raise NativeBlocked('Bill is not linked to a signed order')
            self._read_valid_signed_order(b,order)
            c=self._client('cashier');paid=c.post('/outpatientCharge/confirmOutpatientChargeSettlement',{'encounterId':b['encounter_id'],'chargeItemIds':bill['charge_item_ids'],'paymentDraftId':bill['bill_id'],'outcomeEnum':'SUCCESS','billDate':now(),'paymentDetails':[{'payChannelCode':'CASH','amount':bill['amount']}]})
            rb=c.post('/outpatientCharge/loadPendingChargeItems',{'encounterId':b['encounter_id'],'paymentId':str(paid['paymentId'])})
            summary=rb.get('settlementInsuranceSummary') or {}; options=rb.get('settlementInsuranceOptions') or []
            match=next((x for x in [summary]+options if str(x.get('paymentId'))==str(paid['paymentId'])),None)
            if not match or Decimal(str(match.get('displayAmount'))) != Decimal(bill['amount']):raise NativeError('Payment read-back amount/identity mismatch')
            bill.update(status='paid',payment_id=str(paid['paymentId']),transaction_id=p.get('transaction_id'));return self._result(bill,rb)
        raise NativeBlocked('Unsupported operation '+op)
    def _register(self,p,ctx):
        c=self._client('cashier');d=p.get('demographics',{}); key=ctx['run_id']+':'+ctx['case_id']
        if key in self.state['bindings']:raise NativeBlocked('Case/run already registered')
        code=hashlib.sha256(key.encode()).hexdigest();age=int(d.get('age',d.get('age_years',55)) or 55);year=dt.date.today().year-min(max(age,18),89)
        # Construct a nonclinical registration identity, never use it as source age evidence.
        # 99xxxx reserved synthetic prefix + hash month/day/sequence avoids the old 999/year cap.
        for attempt in range(12):
            token=hashlib.sha256((key+':identity:'+str(attempt)).encode()).hexdigest()
            month=int(token[4:8],16)%12+1; day=int(token[8:12],16)%calendar.monthrange(year,month)[1]+1
            birth=f'{year:04d}-{month:02d}-{day:02d}'
            base='99'+str(int(token[:4],16)%10000).zfill(4)+birth.replace('-','')+str(int(token[12:18],16)%999+1).zfill(3)
            weights=[7,9,10,5,8,4,2,1,6,3,7,9,10,5,8,4,2]
            identity=base+'10X98765432'[sum(int(n)*w for n,w in zip(base,weights))%11]
            matches=c.post('/patient/getPatientPage',{'pageNo':1,'pageSize':100,'keyword':identity})
            if not matches.get('content'):break
        else:raise NativeBlocked('Synthetic identity allocation exhausted; no record adopted')
        patient={'name':'虚构实验'+code[:10],'genderEnum':'FEMALE' if str(d.get('sex',d.get('gender',''))).lower() in ('f','female','女') else 'MALE','birthDate':birth+' 00:00:00','idCard':identity,'phone':'000'+str(int(code[:8],16)%100000000).zfill(8),'activeFlag':True,'patientIdInfoList':[]}
        created=c.post('/patient/createPatient',patient);pid=str(created.get('patientId',created.get('id'))) if isinstance(created,dict) else str(created)
        rbpatient=c.post('/patient/getPatient',{'value':pid})
        if rbpatient.get('idCard')!=identity:raise NativeError('Created patient identity not verified')
        init=c.post('/outpatientRegistration/loadRegisterFormInit',{'organizationId':'322663218269917184','practitionerId':ROLE_SETTINGS['doctor'][2]})
        contract=next((x['value'] for x in init.get('contractOptions',[]) if init.get('contractCategoryById',{}).get(str(x['value'])) in ('SELF_PAY','SELF') or x.get('label')=='自费'),None)
        if not contract:raise NativeBlocked('No verified self-pay contract')
        service=next((x['value'] for x in init.get('serviceTypeOptions',[]) if str(x['value'])=='322957264586018816'),None)
        if not service:raise NativeBlocked('Expected ordinary outpatient service unavailable')
        request={'patientId':pid,'name':patient['name'],'gender':patient['genderEnum'],'birthDate':patient['birthDate'][:10],'idCardNo':identity,'phone':patient['phone'],'organizationId':'322663218269917184','practitionerId':ROLE_SETTINGS['doctor'][2],'serviceTypeId':service,'contractId':contract,'ybRegisterFee':False,'ybUseAcctPay':False}
        slots=c.post('/outpatientRegistration/listBookableSlotHints',{'organizationId':request['organizationId'],'practitionerId':request['practitionerId'],'healthcareServiceId':service,'registrationDate':dt.date.today().isoformat()})
        slot=next((x for x in slots.get('hints',[]) if (x.get('remainingCount') or 0)>0),None)
        if slot:request['slotId']=slot['slotId']
        draft=c.post('/outpatientRegistration/preRegisterSettlement',request)
        pay=c.post('/outpatientRegistration/payRegisterFee',{'paymentDraftId':str(draft['paymentDraftId']),'paymentDetails':[{'payChannelCode':'CASH','payAmount':draft['totalAmount']}]})
        eid=str(pay.get('encounterId') or draft.get('encounterId'));b={'case_id':ctx['case_id'],'run_id':ctx['run_id'],'patient_id':pid,'encounter_id':eid,'contract_id':str(contract),'patient_key':p.get('patient_key'),'registration_demographics_provenance':{'name':'constructed_unique_identifier','id_card':'constructed_nonclinical_identifier','phone':'constructed_nonclinical_identifier','date_of_birth':'constructed_hash_date_for_registration_only','source_age':d.get('age',d.get('age_years')),'source_age_status':d.get('age_status'),'must_not_use_for_clinical_age':True},'registration_name':patient['name'],'artifacts':{},'orders':{},'registration_payment_id':str(pay['paymentId'])}
        rows=c.post('/outpatientRegistration/pageClinicRegisters',{'dateFrom':dt.date.today().isoformat(),'dateTo':dt.date.today().isoformat(),'patientId':pid,'pageNo':1,'pageSize':100})
        record=next((x for x in rows.get('content',[]) if str(x.get('encounterId'))==eid),None)
        if not record:raise NativeError('Registered encounter not observed')
        if not self.defer_consultation_start:
            doctor=self._client('doctor');doctor.post('/doctorStationEncounterManage/startOutpatientReception',{'encounterId':eid,'doctorId':ROLE_SETTINGS['doctor'][2],'operatorId':ROLE_SETTINGS['doctor'][2]});b['consultation_started']=True
        self.state['bindings'][key]=b;self.state['cases'].setdefault(ctx['case_id'],[]).append(key)
        return self._result({'patient_id':pid,'encounter_id':eid},{'patient':rbpatient,'registration':record,'registration_payment':pay})
    def read_encounter(self,ctx):
        # Fresh native observer read, not an idempotency-cached response.
        b=self._binding(ctx);name=b.get('registration_name')
        if not name:raise NativeBlocked('Versioned registration name required for scoped native encounter read')
        c=self._client('doctor')
        rows=c.post('/doctorStationEncounterManage/pageOutpatientInfoForDoctorStation',{'staffPractitionerId':ROLE_SETTINGS['doctor'][2],'keyword':name,'pageNo':1,'pageSize':10})
        found=next((x for x in rows.get('content',[]) if str(x.get('encounterId'))==b['encounter_id'] and str(x.get('patientId'))==b['patient_id']),None)
        if not found:raise NativeError('Bound native encounter not observed')
        return found

    def _diagnosis_catalogue(self,pair):
        c=self._client('doctor');items=[];evidence=[]
        for code,mapping in self.diagnosis_mapping.items():
            rows=c.post('/doctorStationDiagnosis/getConditionDefinitionMetadata',{'keyword':code,'pageNo':1,'pageSize':100})
            rows=rows.get('content',rows.get('records',[]))
            found=next((x for x in rows if str(x.get('conditionDefinitionId',x.get('id')))==mapping['conditionDefinitionId']),None)
            if not found:raise NativeBlocked('Native diagnosis definition not found: '+code)
            if found.get('conditionCode')!=code:raise NativeBlocked('Native diagnosis code mismatch')
            items.append({k:mapping[k] for k in ('code','display','system')});evidence.append(found)
        return self._result({'items':items},evidence)
    def _save_consultation(self,p,ctx,b,pair):
        diagnoses=p.get('content',{}).get('diagnoses')
        if not isinstance(diagnoses,list) or not diagnoses:raise NativeBlocked('Physician-generated structured diagnoses required for native settlement')
        if sum(x.get('primary') is True for x in diagnoses)!=1:raise NativeBlocked('Exactly one primary diagnosis required')
        c=self._client('doctor');children=[]
        for index,d in enumerate(diagnoses):
            m=self.diagnosis_mapping.get(d.get('code'))
            if not m or d.get('system')!=m['system'] or d.get('certainty') not in ('working','confirmed'):raise NativeBlocked('Unmapped/invalid physician diagnosis')
            children.append({'conditionDefinitionId':m['conditionDefinitionId'],'conditionCode':m['code'],'conditionDefinitionName':m['display'],'mainDiagnosisFlag':1 if d['primary'] else 0,'diagnosisSortNo':index+1,'verificationStatusEnum':1 if d['certainty']=='working' else 4,'diagnosisDesc':json.dumps(d,ensure_ascii=False)})
        saved=c.post('/doctorStationDiagnosis/saveDoctorDiagnosis',{**pair,'diagnosisChildList':children})
        rb=c.post('/doctorStationDiagnosis/getEncounterDiagnosis',pair);rows=rb.get('diagnosisList',rb.get('content',[]))
        observed={str(x.get('conditionDefinitionId')) for x in rows}
        if observed!={x['conditionDefinitionId'] for x in children}:raise NativeError('Native consultation diagnoses not verified')
        b.setdefault('artifacts',{})['consultation']=copy.deepcopy(p);self._save()
        sidecar=json.loads(self.state_path.read_text())['bindings'][ctx['run_id']+':'+ctx['case_id']]['artifacts']['consultation']
        return self._result({**sidecar,'native_fields':['diagnoses'],'sidecar_fields':['content','transcript']},{'native_diagnoses':rb,'sidecar_content_hash':digest(sidecar)})
    def _save_note(self,p,ctx,b,pair):
        c=self._client('doctor');note=b.get('note');logical=p['note_id']
        if note:
            if logical!=note['logical_id'] or note['status']=='locked':raise NativeBlocked('Cannot overwrite locked/other note')
            if p.get('expected_version')!=note['version']:raise NativeBlocked('Stale note version')
        else:
            types=c.post('/documentType/getList',{'enabled':True});eligible=[x for x in types if x.get('categoryCode')=='OUTPATIENT']
            selected=next((x for x in eligible if x.get('code')==self.document_type),eligible[0] if eligible else None)
            if not selected:raise NativeBlocked('No outpatient document type')
            made=c.post('/document/create',{**pair,'documentTypeCode':selected['code']});note={'logical_id':logical,'native_id':str(made['id']),'version':0,'status':'draft'};b['note']=note
        content={'type':'doc','content':[{'type':'paragraph','content':[{'type':'text','text':json.dumps(p['content'],ensure_ascii=False,sort_keys=True)}]}]}
        text=json.dumps(content,ensure_ascii=False,sort_keys=True);c.post('/document/save',{'documentId':note['native_id'],'content':text,'saveVersion':True})
        rb=c.post('/document/getContent',{'documentId':note['native_id']})
        got=rb.get('content');got=json.loads(got) if isinstance(got,str) else got
        if got!=content or str(rb.get('patientId'))!=pair['patientId'] or str(rb.get('encounterId'))!=pair['encounterId']:raise NativeError('Document content/binding read-back mismatch')
        note['version']+=1;note['content_hash']=digest(p['content']);note['status']='draft'
        return self._result({'note_id':logical,'document_id':note['native_id'],'version':note['version'],'status':'draft','version_protection':'runtime_gateway','native_writer_role':'doctor','content_author_role':ctx.get('role')},rb)
    def preflight_order(self,prescription):
        """Read-only local encoding check; does not authenticate or call HIS."""
        return preflight_encoding(prescription,self.catalog_mapping)

    def _save_order(self,p,ctx,b,pair):
        logical=p['order_id'];items=p.get('items') or []
        if logical in b['orders']:raise NativeBlocked('Order exists; use new revision ID')
        try:encoded=encode_items(items,self.catalog_mapping)
        except OrderEncodingError as exc:raise NativeBlocked('Native order encoding: '+str(exc)) from exc
        c=self._client('doctor');lines=[]
        frequency_normalizations=encoded['frequency_normalizations'];dose_unit_mappings=encoded['dose_unit_mappings']
        for med in encoded['medications']:
            med.update(contractId=b['contract_id'],remark='TwinHospital synthetic experiment '+ctx['run_id'])
            lines.append({'lineOp':'CREATE','productTableName':'med_medication_definition','medicationPayload':med})
        made=c.post('/doctorStationAdvice/saveDoctorStationAdviceDraftBatch',{**pair,'lines':lines})
        ids=[str(x) for x in made.get('newMedicationRequestIds',[])];rows=c.post('/doctorStationAdvice/listDoctorStationAdvice',pair).get('content',[]);selected=[x for x in rows if str(x.get('requestId')) in ids]
        if len(ids)!=len(items) or len(selected)!=len(items):raise NativeError('Prescription lines not observed')
        by_id={str(row['requestId']):row for row in selected}
        selected=[by_id[rid] for rid in ids]
        for row,item,mapped in zip(selected,items,lines):
            med=mapped['medicationPayload']
            for field,expected in [('patientId',pair['patientId']),('encounterId',pair['encounterId']),('productDefinitionId',med['medicationDefinitionId']),('unitCode',med['unitCode']),('doseUnitCode',med['doseUnitCode']),('methodCode',med['methodCode']),('rateCode',med['rateCode'])]:
                if str(row.get(field))!=str(expected):raise NativeError('Prescription '+field+' read-back mismatch')
            if Decimal(str(row.get('dose')))!=Decimal(str(item['dose'])) or Decimal(str(row.get('quantity')))!=Decimal(str(item['quantity'])):raise NativeError('Prescription numeric read-back mismatch')
        b['orders'][logical]={'native_ids':ids,'status':'draft','parent_order_id':p.get('parent_order_id'),'items':items,'frequency_normalizations':frequency_normalizations,'dose_unit_mappings':dose_unit_mappings}
        instruction_evidence=None
        if any(item.get('administration_instructions') is not None for item in items):
            # This native draft DTO does not accept free-text remark/sig writes.
            # Retain the original instruction in the same persisted, bound order
            # artifact read by downstream agents; never call it native storage.
            self._save()
            saved=json.loads(self.state_path.read_text())['bindings'][ctx['run_id']+':'+ctx['case_id']]
            order=saved.get('orders',{}).get(logical,{})
            if (str(saved.get('patient_id'))!=pair['patientId'] or str(saved.get('encounter_id'))!=pair['encounterId']
                or order.get('items')!=items or order.get('native_ids')!=ids):
                raise NativeError('Prescription instruction artifact read-back mismatch')
            instruction_evidence={'storage':'persistent_runtime_order_artifact','native_instruction_storage':False,
                'readback_verified':True,'order_items_sha256':digest(order['items']),
                'native_order_ids_sha256':digest(order['native_ids']),'instruction_version':'explicit_sig_artifact_v1'}
        return self._result({'order_id':logical,'status':'draft','native_ids':ids,'frequency_normalizations':frequency_normalizations,
            'dose_unit_mappings':dose_unit_mappings,'instruction_evidence':instruction_evidence},selected)
    def snapshot(self,case_id):
        return {'backend':self.backend_name,'case_id':case_id,'bindings':[copy.deepcopy(self.state['bindings'][k]) for k in self.state['cases'].get(case_id,[])],'native_claim_scope':'native API read-back; version/idempotency/role gateway are separately recorded'}
