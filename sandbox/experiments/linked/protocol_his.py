"""Paper-protocol gateway used by the complete harness and local safety tests.

Queue linkage and signatory authorization are persistent runtime protections.
The staff registry is built from successful isolated OpenHIS logins plus an
explicit experiment authorization policy. It is not inferred from caller role
strings, and is not claimed to audit the backend's independent RBAC or CA signing.
"""
from __future__ import annotations
import copy,fcntl,json,os,threading
from .native_his import NativeBlocked,NativeError,ROLE_SETTINGS,digest,write_private,now,VALID_SIGNED_REQUEST_STATES,SIGNED_REQUEST_POLICY_VERSION

PROTOCOL_VERSION='paper-protocol-gateway-2'
ACTOR_ROUTES={'doctor-1':'doctor','physician-1':'doctor','attending_physician-1':'doctor',
 'nurse-1':'nurse','pharmacist-1':'pharmacist','cashier-1':'cashier',
 'administrator-1':'cashier','administrative-1':'cashier','billing-1':'cashier','scribe-1':'doctor'}
ROLE_ROUTES={a[:-2]:r for a,r in ACTOR_ROUTES.items()}

class PaperProtocolPort:
    backend_name='openhis_whale_native_with_paper_protocol_gateway'
    def __init__(self,native_port):
        self.native=native_port;self.root=native_port.root
        self.state_path=self.root/'protocol_state.json';self.journal=self.root/'protocol_operations.jsonl'
        self._lock=threading.RLock();self.registry={};self.native.defer_consultation_start=True
        self.state=self._load()
    def __getattr__(self,key):return getattr(self.native,key)
    def _load(self):
        return json.loads(self.state_path.read_text()) if self.state_path.exists() else {'protocol_version':PROTOCOL_VERSION,'queues':{},'attestations':{},'queue_idempotency':{},'staff_registry':{}}
    def preflight_order(self,prescription):
        return self.native.preflight_order(prescription)
    def _save(self):write_private(self.state_path,self.state)
    def _event(self,row):
        fd=os.open(self.journal,os.O_CREAT|os.O_APPEND|os.O_WRONLY,0o600)
        with os.fdopen(fd,'a') as f:f.write(json.dumps(row,ensure_ascii=False)+'\n')
    def _key(self,ctx):return ctx['run_id']+':'+ctx['case_id']
    def _principal(self,role):
        c=self.native._client(role);identity=c.identity
        expected_username,org,expected_pid=ROLE_SETTINGS[role]
        if str(identity.get('practitionerId'))!=expected_pid or identity.get('userName')!=expected_username or not identity.get('isActive') or identity.get('isLockedOut'):
            raise NativeBlocked('Authenticated native personnel identity is not the registered experiment principal')
        member={'registry_id':expected_pid,'native_user_id':str(identity['id']),'native_practitioner_id':expected_pid,
          'native_username':expected_username,'selected_organization_id':org,'runtime_role':role,
          'can_sign_orders':role=='doctor','authority_source':'explicit_experiment_policy_after_native_login',
          'native_permission_audit':False,'native_login_verified':True}
        previous=self.state['staff_registry'].get(expected_pid)
        if previous and previous!=member:raise NativeBlocked('Personnel registry changed; new run version required')
        self.registry[role]=member;self.state['staff_registry'][expected_pid]=member;self._save()
        return member
    def _executor(self,ctx,role):
        actor=ctx.get('actor_id');claimed=ctx.get('role')
        if ACTOR_ROUTES.get(actor)!=role or ROLE_ROUTES.get(claimed)!=role or actor!=str(claimed)+'-1':
            raise NativeBlocked('Execution actor is not bound to the required isolated role session')
        return self._principal(role)
    def _signatory(self,ctx,signatory_id):
        doctor=self._executor(ctx,'doctor')
        # Populate a real authenticated nurse record too. A registered person
        # still lacks signing authorization; changing executor role is not needed.
        self._principal('nurse')
        staff=self.state['staff_registry'].get(str(signatory_id))
        if not staff or staff['can_sign_orders'] is not True or staff['native_practitioner_id']!=doctor['native_practitioner_id']:
            raise NativeBlocked('Order signatory is not an authorized registered physician for this executor')
        return staff
    def _queue(self,ctx):
        b=self.native._binding(ctx);entry=self.state['queues'].get(self._key(ctx))
        if not entry or not entry.get('encounter_id') or str(entry['encounter_id'])!=b['encounter_id'] or str(entry.get('patient_id'))!=b['patient_id']:
            raise NativeBlocked('Triage queue entry lacks the bound encounter linkage')
        return entry
    def _signature_readback(self,ctx,order_id):
        b=self.native._binding(ctx);proof=self.state['attestations'].get(self._key(ctx)+':'+str(order_id))
        if not proof:raise NativeBlocked('No verified signatory record for this order before payment')
        staff=self._principal('doctor')
        if proof['signatory_id']!=staff['native_practitioner_id'] or not staff['can_sign_orders']:
            raise NativeBlocked('Persisted signatory is not authorized at settlement')
        order=b.get('orders',{}).get(order_id)
        if not order or set(order['native_ids'])!=set(proof['native_ids']):raise NativeBlocked('Signed order linkage changed')
        rows=self.native._client('doctor').post('/doctorStationAdvice/listDoctorStationAdvice',{'patientId':b['patient_id'],'encounterId':b['encounter_id']}).get('content',[])
        selected=[x for x in rows if str(x.get('requestId')) in proof['native_ids']]
        if len(selected)!=len(proof['native_ids']):raise NativeError('Signed native requests not found at settlement')
        for row in selected:
            if str(row.get('patientId'))!=b['patient_id'] or str(row.get('encounterId'))!=b['encounter_id'] or row.get('statusEnum') not in VALID_SIGNED_REQUEST_STATES or not row.get('reqAuthoredTime') or not row.get('prescriptionNo'):
                raise NativeBlocked('Native signed-order state is not valid before payment')
            if str(row.get('authoredById'))!=staff['native_practitioner_id']:
                raise NativeBlocked('Native order author does not match authenticated registered physician')
            if row.get('signedById') and str(row['signedById'])!=staff['native_practitioner_id']:
                raise NativeBlocked('Exposed native signatory does not match the registry')
        return {'signatory_id':proof['signatory_id'],'native_ids':proof['native_ids'],'native_author_verified':True,
                'native_independent_signer_field_available':all(bool(r.get('signedById')) for r in selected),
                'signed_state_policy_version':SIGNED_REQUEST_POLICY_VERSION,
                'allowed_request_states':sorted(VALID_SIGNED_REQUEST_STATES),
                'verification_scope':'authenticated request operator plus native author/signed state; absent native signedById stays unknown'}
    def execute(self,operation,payload=None,context=None):
        if isinstance(operation,dict):
            action=operation;context=payload if context is None else context;operation=action.get('operation') or action.get('name');payload=action.get('payload',{})
        p=copy.deepcopy(payload or {});ctx=dict(context or {});op=str(operation)
        if not ctx.get('case_id') or not ctx.get('run_id'):raise NativeBlocked('Protocol requires case and run')
        with self._lock:
            fd=os.open(self.root/'protocol.lock',os.O_CREAT|os.O_RDWR,0o600)
            try:
                fcntl.flock(fd,fcntl.LOCK_EX);self.state=self._load()
                # Other wrapper instances share this lock; native writes replace state atomically.
                if self.native.state_path.exists():self.native.state=json.loads(self.native.state_path.read_text())
                result=self._dispatch(op,p,ctx)
                self._event({'at':now(),'operation':op,'case_id':ctx['case_id'],'run_id':ctx['run_id'],'request_hash':digest(p),'actor_id':ctx.get('actor_id'),'role':ctx.get('role'),'status':result['status'],'native_effect':result.get('native_effect'),'protection_source':result.get('protection_source')})
                return result
            except Exception as e:
                self._event({'at':now(),'operation':op,'case_id':ctx['case_id'],'run_id':ctx['run_id'],'request_hash':digest(p),'actor_id':ctx.get('actor_id'),'role':ctx.get('role'),'status':'blocked' if isinstance(e,NativeBlocked) else 'unresolved','error_type':type(e).__name__,'message':str(e)[:240]})
                raise
            finally:os.close(fd)
    def _dispatch(self,op,p,ctx):
        if op=='registration.create':
            self._executor(ctx,'cashier')
            return self.native.execute(op,p,ctx)
        b=self.native._binding(ctx);key=self._key(ctx)
        if op=='personnel.registry':
            person=self._executor(ctx,'doctor');nurse=self._principal('nurse')
            rb=json.loads(self.state_path.read_text())['staff_registry']
            return self.native._result({'signatory_id':person['native_practitioner_id'],'registry_version':PROTOCOL_VERSION,
                'members':[person,nurse]}, {'registry':rb,'authentication':'native successful isolated role sessions'},native=False,source='authenticated_staff_registry_runtime_policy')
        if op=='queue.enter':
            self._executor(ctx,'cashier')
            if not p.get('encounter_id') or str(p['encounter_id'])!=b['encounter_id']:
                raise NativeBlocked('Queue-entry encounter_id is required and must match registration')
            if set(p)!={'encounter_id'}:raise NativeBlocked('Queue entry accepts only explicit bound encounter_id')
            old=self.state['queues'].get(key)
            entry={'entry_id':'queue_'+digest(key)[:20],'case_id':ctx['case_id'],'run_id':ctx['run_id'],
                'patient_id':b['patient_id'],'encounter_id':str(p['encounter_id']),'status':'waiting'}
            if old:
                if any(old.get(k)!=v for k,v in entry.items() if k!='status'):raise NativeBlocked('Existing queue entry differs')
                result=self.native._result(old,old,native=False,source='persistent_queue_runtime_gateway');result['status']='replayed';return result
            self.state['queues'][key]=entry;self._save();rb=json.loads(self.state_path.read_text())['queues'][key]
            return self.native._result(entry,rb,native=False,source='persistent_queue_runtime_gateway')
        if op=='queue.read':
            entry=self._queue(ctx);rb=json.loads(self.state_path.read_text())['queues'][key]
            return self.native._result({'entry':entry},rb,native=False,source='persistent_queue_runtime_gateway')
        if op=='encounter.begin_consultation':
            self._queue(ctx);self._executor(ctx,'doctor')
            result=self.native.execute(op,p,ctx)
            self.state['queues'][key]['status']='in_consultation';self._save()
            return result
        if op in ('consultation.save','order.save','order.attest','note.save','note.revise','note.attest','payment.prepare','payment.commit'):
            self._queue(ctx)
            if not b.get('consultation_started'):raise NativeBlocked('Doctor reception has not begun')
        if op=='order.attest':
            signer=self._signatory(ctx,p.get('signatory_id'))
            payload={k:v for k,v in p.items() if k!='signatory_id'}
            result=self.native.execute(op,payload,ctx)
            rows=result['readback'];order=b['orders'][p['order_id']]
            if not rows or any(r.get('statusEnum') not in VALID_SIGNED_REQUEST_STATES or not r.get('reqAuthoredTime') or not r.get('prescriptionNo') or str(r.get('authoredById'))!=signer['native_practitioner_id'] or (r.get('signedById') and str(r['signedById'])!=signer['native_practitioner_id']) for r in rows):
                raise NativeError('Native order authorship not bound to authenticated signatory')
            proof={'signatory_id':signer['native_practitioner_id'],'native_user_id':signer['native_user_id'],
                'native_ids':order['native_ids'],'role_session':'doctor','actor_id':ctx['actor_id'],
                'native_readback_hash':digest(rows),'native_signed_by_id_status':'available' if all(r.get('signedById') for r in rows) else 'not_exposed',
                'signed_state_policy_version':SIGNED_REQUEST_POLICY_VERSION,
                'authorization_scope':'runtime signatory registry, not native-only identity enforcement'}
            self.state['attestations'][key+':'+p['order_id']]=proof;self._save()
            result['data']['signatory_verification']=proof;result['protection_source']='native_signed_state_plus_authenticated_signatory_runtime_gateway'
            return result
        if op in ('payment.prepare','payment.commit'):
            self._executor(ctx,'cashier')
            order_id=p.get('order_id') if op=='payment.prepare' else b.get('bills',{}).get(p.get('bill_id'),{}).get('order_id')
            if not order_id:raise NativeBlocked('Bill is not linked to this patient and signed order')
            verification=self._signature_readback(ctx,order_id)
            result=self.native.execute(op,p,ctx);result['data']['prepayment_signatory_verification']=verification
            result['protection_source']='native_cash_state_plus_signed_order_runtime_gateway'
            if op=='payment.commit' and result['data'].get('status')=='paid':
                self.state['queues'][key]['status']='settled';self._save()
            return result
        return self.native.execute(op,p,ctx)
    def snapshot(self,case_id):
        return {**self.native.snapshot(case_id),'protocol':self._load(),'protocol_claim_scope':'persistent queue/signatory gateway; native-only enforcement not established'}
