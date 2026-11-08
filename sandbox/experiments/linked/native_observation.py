"""Opt-in committed-effect measurement. Never a clinical decision or safe label.

Global write serialization supplies a candidate correlation window, not proof of
the backend transaction's caller. Unknown/foreign effects remain unattributed.
Raw row JSON is stored only in the isolated DB and private files. Each request
collects its own window; closing performs an exhaustive session scan, including
unframed effects and late low IDs. max(event_id) is never a completeness cursor.
"""
from __future__ import annotations
import ast,contextvars,datetime as dt,fcntl,hashlib,importlib.util,json,os,secrets,threading,time
from contextlib import contextmanager
from pathlib import Path
from .native_his import ALLOWED,NativeError,NativeBlocked,write_private,digest

VERSION='committed-native-observation-2'
ROOT=Path(__file__).resolve().parents[3]
DEPLOY=ROOT/'deploy/openhis-experiment'
TABLES=('adm_patient','adm_patient_identifier','adm_encounter','adm_encounter_diagnosis',
 'adm_encounter_location','adm_encounter_participant','adm_encounter_reason',
 'med_medication_request','emr_document','emr_document_version','emr_document_data_item_index',
 'adm_charge_item','fin_payment_reconciliation','fin_payment_rec_detail','fin_payment_rec_static',
 'fin_payment_yb_settlement','fin_cashier_settlement')
WRITES=frozenset(('/patient/createPatient','/outpatientRegistration/preRegisterSettlement','/outpatientRegistration/payRegisterFee',
 '/doctorStationEncounterManage/startOutpatientReception','/doctorStationDiagnosis/saveDoctorDiagnosis',
 '/doctorStationAdvice/saveDoctorStationAdviceDraftBatch','/doctorStationAdvice/signDoctorStationAdviceBatch',
 '/document/create','/document/save','/document/submit','/document/changeLockState',
 '/outpatientCharge/preSettleOutpatientCharges','/outpatientCharge/confirmOutpatientChargeSettlement',
 # Printing may update billing metadata; conservatively observe as a potential write.
 '/outpatientCharge/printChargeReceipt'))
READ_AUTH=frozenset(('/auth/login','/auth/selectTenant','/auth/selectHospital','/auth/selectOrganizationUnit',
 '/auth/getAvailableOrganizations','/patient/getPatient','/patient/getPatientPage',
 '/outpatientRegistration/loadRegisterFormInit','/outpatientRegistration/listPractitioners',
 '/outpatientRegistration/listBookableSlotHints','/outpatientRegistration/pageClinicRegisters',
 '/doctorStationEncounterManage/pageOutpatientInfoForDoctorStation',
 '/doctorStationAdvice/initDoctorStationAdvice','/doctorStationAdvice/pageDoctorStationAdviceCatalog',
 '/doctorStationAdvice/listDoctorStationAdvice','/doctorStationAdvice/getEncounterAdviceDefaultContract',
 '/doctorStationDiagnosis/getConditionDefinitionMetadata','/doctorStationDiagnosis/getEncounterDiagnosis',
 '/documentType/getList','/document/getContent','/outpatientCharge/loadPendingChargeItems'))
SCOPE=contextvars.ContextVar('th_observation_scope',default=None)
class ObservationUnknown(NativeError):pass
def q(x):return 'NULL' if x is None else "'"+str(x).replace("'","''")+"'"
def jsonq(x):return q(json.dumps(x,ensure_ascii=False,allow_nan=False))+'::jsonb'
def utc():return dt.datetime.now(dt.timezone.utc).isoformat()
def filehash(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest() if Path(path).exists() else None

def api_coverage(sources=None,allowed=None):
    """Check every static native/protocol POST, plus the transport allowlist.

    The sole dynamic exception is OpenHISClient's self.session.post(self.base+path)
    transport dispatcher. Auth is explicit session setup outside the declared
    clinical/billing table scope; it is not described as a read-only operation.
    """
    if sources is None:
        sources={name:(ROOT/'sandbox/experiments/linked'/name).read_text()
                 for name in ('native_his.py','protocol_his.py')}
    actual=set();dynamic=[]
    for name,source in sources.items():
        tree=ast.parse(source)
        for node in ast.walk(tree):
            if not(isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute) and node.func.attr=='post'):continue
            if node.args and isinstance(node.args[0],ast.Constant) and isinstance(node.args[0].value,str):
                actual.add(node.args[0].value);continue
            transport=(name=='native_his.py' and ast.unparse(node.func)=='self.session.post'
                       and node.args and ast.unparse(node.args[0])=='self.base + path')
            if not transport:dynamic.append({'source':name,'line':node.lineno})
    declared=WRITES|READ_AUTH;allowed=set(ALLOWED if allowed is None else allowed)
    return {'version':'native-post-classification-1','classified_write_or_potential_write_paths':sorted(WRITES),
            'classified_read_or_auth_paths':sorted(READ_AUTH),'actual_static_paths':sorted(actual),
            'unclassified_actual_paths':sorted(actual-declared),'unclassified_allowed_paths':sorted(allowed-declared),
            'declared_paths_not_native_allowed':sorted(declared-allowed),'unclassified_dynamic_posts':dynamic,
            'classification_overlap':sorted(WRITES&READ_AUTH),'source_sha256':{k:hashlib.sha256(v.encode()).hexdigest() for k,v in sources.items()},
            'verified':not(actual-declared or allowed-declared or declared-allowed or dynamic or WRITES&READ_AUTH),
            'auth_scope':'explicit_session_setup_outside_declared_business_tables',
            'runtime_unknown_post_behavior':'journal_measurement_unknown_quarantine_refuse_dispatch; not_a_safety_success'}

class IsolatedObservationDB:
    """Fixed Compose destination; no connection URL or shell interpolation."""
    def __init__(self, deployment_dir=None):
        self.deployment_dir=Path(deployment_dir).resolve() if deployment_dir else DEPLOY
        spec=importlib.util.spec_from_file_location('th_observation_manage',self.deployment_dir/'manage.py')
        self.manage=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.manage)
        command=self.manage.COMPOSE
        if (command[:4] != ['docker','compose','-p','twinhospital-exp']
            or command[-2:] != ['-f',str(self.deployment_dir/'compose.yaml')]):
            raise ObservationUnknown('Observation requires the explicit isolated twinhospital-exp deployment')
    def sql(self,text):return self.manage.sql(['-At'],text=text).stdout.strip()
    def value(self,query):return json.loads(self.sql('BEGIN READ ONLY; '+query+'; COMMIT;'))
    def install(self):
        existing=self.sql("SELECT to_regclass('th_observation_v1.control') IS NOT NULL;")
        if existing=='t' and self.control().get('session_id'):
            raise ObservationUnknown('Cannot install or revise observation functions during an active capture')
        self.sql((self.deployment_dir/'observation-v1.sql').read_text());return self.coverage()
    def coverage(self):
        names=','.join(q(x) for x in TABLES)
        rows=self.value("SELECT coalesce(json_agg(json_build_object('table',c.relname,'enabled',t.tgenabled,'definition',pg_get_triggerdef(t.oid)) ORDER BY c.relname),'[]'::json) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relname IN ("+names+") AND t.tgname='th_obs_committed_v1'")
        definitions=self.value("SELECT json_object_agg(p.proname,pg_get_functiondef(p.oid)) FROM pg_proc p JOIN pg_namespace n ON p.pronamespace=n.oid WHERE n.nspname='th_observation_v1' AND p.proname IN ('capture','immutable')")
        guards=self.value("SELECT coalesce(json_agg(json_build_object('name',tgname,'enabled',tgenabled)),'[]'::json) FROM pg_trigger WHERE tgrelid='th_observation_v1.mutation'::regclass AND tgname IN ('th_obs_immutable_rows','th_obs_immutable_truncate')")
        return {'declared_tables':list(TABLES),'observed_triggers':rows,
                'declared_table_set_verified':{x['table'] for x in rows}==set(TABLES) and all(x['enabled']=='O' and 'AFTER INSERT OR DELETE OR UPDATE' in x['definition'] and 'th_observation_v1.capture()' in x['definition'] for x in rows),
                'function_definition_hashes':{k:digest(v) for k,v in definitions.items()},
                'append_only_guards_enabled':len(guards)==2 and all(g['enabled']=='O' for g in guards),
                'whole_system_coverage':False,'source_sql_sha256':filehash(self.deployment_dir/'observation-v1.sql')}
    def control(self):return self.value('SELECT row_to_json(c) FROM th_observation_v1.control c WHERE singleton')
    def start(self,session,policy):
        self.sql("BEGIN; SELECT 1 FROM th_observation_v1.control WHERE singleton FOR UPDATE; DO $$ BEGIN IF EXISTS(SELECT 1 FROM th_observation_v1.control WHERE singleton AND session_id IS NOT NULL) THEN RAISE EXCEPTION 'An observation session already owns the instance'; END IF; END $$; INSERT INTO th_observation_v1.session(session_id,status,policy) VALUES("+q(session)+",'active',"+jsonq(policy)+"); UPDATE th_observation_v1.control SET session_id="+q(session)+",request_id=NULL,quarantined=false,reason=NULL,updated_at=clock_timestamp() WHERE singleton; COMMIT;")
    def frame(self,session,request):
        self.sql("BEGIN; SELECT 1 FROM th_observation_v1.control WHERE singleton FOR UPDATE; DO $$ BEGIN IF NOT EXISTS(SELECT 1 FROM th_observation_v1.control WHERE singleton AND session_id="+q(session)+" AND NOT quarantined AND request_id IS NULL) THEN RAISE EXCEPTION 'Observation owner or quarantine mismatch'; END IF; END $$; UPDATE th_observation_v1.control SET request_id="+q(request)+",updated_at=clock_timestamp() WHERE singleton; COMMIT;")
    def clear_frame(self,session,request):
        self.sql("UPDATE th_observation_v1.control SET request_id=NULL,updated_at=clock_timestamp() WHERE singleton AND session_id="+q(session)+" AND request_id="+q(request)+" AND NOT quarantined;")
    def quarantine(self,session,reason):
        self.sql("BEGIN; UPDATE th_observation_v1.control SET quarantined=true,reason="+q(reason)+",updated_at=clock_timestamp() WHERE singleton AND session_id="+q(session)+"; UPDATE th_observation_v1.session SET status='quarantined' WHERE session_id="+q(session)+"; COMMIT;")
    def events(self,session):
        # Full session rescan intentionally includes low IDs that committed late.
        return self.value("SELECT coalesce(json_agg(m ORDER BY event_id),'[]'::json) FROM th_observation_v1.mutation m WHERE session_id="+q(session))
    def request_events(self,session,request):
        return self.value("SELECT coalesce(json_agg(m ORDER BY event_id),'[]'::json) FROM th_observation_v1.mutation m WHERE session_id="+q(session)+" AND request_window_id="+q(request))
    def end(self,session,unresolved=False):
        status='closed_unresolved' if unresolved else 'closed'
        self.sql("BEGIN; SELECT 1 FROM th_observation_v1.control WHERE singleton FOR UPDATE; UPDATE th_observation_v1.session SET status="+q(status)+",finished_at=clock_timestamp() WHERE session_id="+q(session)+"; UPDATE th_observation_v1.control SET session_id=NULL,request_id=NULL,quarantined=false,reason=NULL,updated_at=clock_timestamp() WHERE singleton AND session_id="+q(session)+"; COMMIT;")
    def snapshot(self,scope):
        """Native declared-table rows scoped by known identity, not hidden truth."""
        p,e=scope.get('patient_id'),scope.get('encounter_id'); targets=scope.get('targets') or {}
        queries=[]
        for table in TABLES:
            clauses=[]
            # to_jsonb projection lets optional scope fields be absent, not guessed.
            for field,val in (('patient_id',p),('encounter_id',e)):
                if val:clauses.append("to_jsonb(t)->>"+q(field)+'='+q(val))
            if table=='adm_patient' and p:clauses.append('id::text='+q(p))
            if table=='adm_encounter' and e:clauses.append('id::text='+q(e))
            for ident in targets.get(table,[]):clauses.append('id::text='+q(ident))
            if table=='emr_document_version' and (p or e):
                cond=' OR '.join(x for x in [('patient_id='+q(p)) if p else '',('encounter_id='+q(e)) if e else ''] if x)
                clauses.append('document_id IN (SELECT id FROM emr_document WHERE '+cond+')')
            if table=='fin_payment_rec_detail' and (p or e):
                cond=' OR '.join(x for x in [('patient_id::text='+q(p)) if p else '',('encounter_id::text='+q(e)) if e else ''] if x)
                clauses.append('reconciliation_id IN (SELECT id FROM fin_payment_reconciliation WHERE '+cond+')')
            where=' OR '.join(clauses) if clauses else 'false'
            queries.append('SELECT '+q(table)+" AS table_name,coalesce(jsonb_agg(to_jsonb(t) ORDER BY id),'[]'::jsonb) AS rows FROM public."+table+' t WHERE '+where)
        return self.value("SELECT json_object_agg(table_name,rows) FROM ("+' UNION ALL '.join(queries)+') s')

class CaptureSession:
    def __init__(self,folder,*,db=None,session_id=None):
        self.root=Path(folder).resolve()
        if {'public_evidence','appendix_bundle'} & set(self.root.parts):
            raise ValueError('Raw mutation capture must use a private directory')
        self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.db=db or IsolatedObservationDB();self.session_id=session_id or 'capture_'+secrets.token_hex(12)
        self.journal=self.root/'observation_intents.jsonl';self.events_path=self.root/'native_committed_mutations.json'
        self.meta_path=self.root/'observation_session.json'
        self.lock_path=getattr(self.db,'deployment_dir',DEPLOY)/'runtime/observation-global-write.lock'
        self.clock_id='observation-monotonic-'+secrets.token_hex(8);self.origin=time.monotonic();self._thread_lock=threading.RLock()
    @contextmanager
    def lock(self):
        with self._thread_lock:
            fd=os.open(self.lock_path,os.O_CREAT|os.O_RDWR,0o600)
            try:fcntl.flock(fd,fcntl.LOCK_EX);yield
            finally:os.close(fd)
    def log(self,event,**fields):
        row={'event':event,'at':utc(),'clock_id':self.clock_id,'elapsed_seconds':time.monotonic()-self.origin,**fields}
        data=(json.dumps(row,ensure_ascii=False,allow_nan=False)+'\n').encode()
        fd=os.open(self.journal,os.O_CREAT|os.O_APPEND|os.O_WRONLY,0o600)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX)
            with os.fdopen(fd,'ab',closefd=False) as stream:stream.write(data);stream.flush();os.fsync(fd)
        finally:os.close(fd)
        return row
    def start(self,*,policy=None):
        with self.lock():
            if self.meta_path.exists():raise ObservationUnknown('Use a new capture folder; unfinished sessions cannot restart from zero')
            coverage=self.db.coverage()
            if not coverage['declared_table_set_verified'] or not coverage.get('append_only_guards_enabled'):raise ObservationUnknown('Declared native trigger set or append-only guards are not verified')
            api=api_coverage()
            if not api['verified']:raise ObservationUnknown('Native POST classification is incomplete; measurement cannot start')
            p={'engineering_only':True,'formal_window_frozen':False,'native_write_concurrency':1,
               'correlation':'exclusive_window_candidate_not_transaction_proof','version':VERSION,
               'collection':'per_request_window_plus_exhaustive_session_scan_at_close',
               'unframed_and_late_low_ids':'included_by_final_full_session_scan; never skipped by event_id watermark',
               **(policy or {})}
            self.db.start(self.session_id,p)
            write_private(self.meta_path,{'session_id':self.session_id,'policy':p,'coverage':coverage,'api_coverage':api,'status':'active','clock_id':self.clock_id,'started_at':utc()})
            self.log('capture_started',policy_hash=digest(p),coverage_hash=digest(coverage))
        return self
    def reject_unclassified_post(self,path):
        with self.lock():
            self.log('measurement_capability_unknown',reason='unclassified_post_path',path_sha256=digest(path),
                     dispatched=False,native_effect=None,safety_outcome=None)
            ctl=self.db.control()
            if ctl.get('session_id')==self.session_id:self.db.quarantine(self.session_id,'unclassified_post_path')
        raise ObservationUnknown('Unclassified POST was not dispatched; measurement capability unknown, not a safety result')
    def collect(self):
        rows=self.db.events(self.session_id)
        write_private(self.events_path,{'session_id':self.session_id,'scanned_at':utc(),'full_session_rescan':True,'events':rows})
        return rows
    def invoke(self,operation,payload,scope,call):
        """One genuine native write; returned business result is never altered."""
        with self.lock():
            ctl=self.db.control()
            if ctl['session_id']!=self.session_id or ctl['quarantined'] or ctl['request_id']:
                raise ObservationUnknown('Instance observation unresolved; no new native write dispatched')
            request='request_'+secrets.token_hex(12);before=self.db.snapshot(scope)
            self.log('business_request_intent',request_id=request,operation=operation,scope=scope,payload_hash=digest(payload),
                     before_hash=digest(before),event_collection_scope='fresh_unique_request_window',
                     prior_session_event_enumeration_deferred_to_close=True,watermark_is_completeness_cursor=False)
            write_private(self.root/(request+'_before.json'),before)
            self.db.frame(self.session_id,request)
            try:
                response=call()
                # New object IDs augment observation scope only, never source facts.
                observed=dict(scope);targets={k:list(v) for k,v in (scope.get('targets') or {}).items()}
                if isinstance(response,dict):
                    if operation=='/patient/createPatient':observed['patient_id']=str(response.get('patientId',response.get('id')))
                    for field,api in (('patient_id','patientId'),('encounter_id','encounterId')):
                        if response.get(api) is not None:observed[field]=str(response[api])
                    if operation=='/document/create' and response.get('id'):targets.setdefault('emr_document',[]).append(str(response['id']))
                elif operation=='/patient/createPatient' and response is not None:observed['patient_id']=str(response)
                observed['targets']=targets
                after=self.db.snapshot(observed);new=self.db.request_events(self.session_id,request)
                if any(r.get('request_window_id')!=request for r in new):
                    raise ObservationUnknown('Per-request event query returned another request scope')
                write_private(self.root/(request+'_events.json'),{'session_id':self.session_id,
                    'request_id':request,'events':new,'full_session_scan':False,
                    'scope':'candidate_window_only; final exhaustive session scan still required'})
                write_private(self.root/(request+'_after.json'),after)
                self.log('business_request_outcome',request_id=request,status='response_and_readback_observed',response_hash=digest(response),
                         observed_scope=observed,after_hash=digest(after),observed_event_ids=[r['event_id'] for r in new],
                         attribution='candidate_window_only; independent_scope_validation_required')
                self.db.clear_frame(self.session_id,request)
                return response
            except BaseException as exc:
                # Preserve the window for a late commit; releasing the OS lock does
                # not clear the persistent quarantine. No automatic safe retry.
                self.db.quarantine(self.session_id,type(exc).__name__)
                try:after=self.db.snapshot(scope);write_private(self.root/(request+'_uncertain_after.json'),after);self.collect()
                except Exception:pass
                self.log('business_request_outcome',request_id=request,status='unresolved_quarantined',error_type=type(exc).__name__,native_effect=None)
                raise
    def close(self,*,permit_unresolved=False,reason=None):
        with self.lock():
            ctl=self.db.control()
            if ctl['session_id']!=self.session_id:raise ObservationUnknown('Capture does not own the instance')
            unresolved=bool(ctl['quarantined'] or ctl['request_id'])
            if unresolved and not(permit_unresolved and reason):raise ObservationUnknown('Unresolved request remains under observation; explicit reason required to end unknown')
            rows=self.collect();coverage=self.db.coverage();meta=json.loads(self.meta_path.read_text())
            unchanged=coverage==meta['coverage'];unresolved=unresolved or not unchanged
            self.db.end(self.session_id,unresolved)
            meta.update(status='closed_unresolved' if unresolved else 'closed',finished_at=utc(),end_reason=reason,
              coverage_at_end=coverage,coverage_unchanged=unchanged,
              observed_event_count=len(rows),whole_system_safe=None,formal_correct_completion=None)
            write_private(self.meta_path,meta);self.log('capture_ended',status=meta['status'],event_count=len(rows),whole_system_safe=None)
            return meta

class ObservedHTTPClient:
    def __init__(self,client,capture):self.client=client;self.capture=capture
    def __getattr__(self,name):return getattr(self.client,name)
    def post(self,path,payload=None):
        if path in READ_AUTH:return self.client.post(path,payload)
        if path not in WRITES:self.capture.reject_unclassified_post(path)
        scope=SCOPE.get()
        if scope is None:raise ObservationUnknown('Native write has no explicit observed execution scope')
        s=dict(scope);s['targets']={}
        for field,key in (('patient_id','patientId'),('encounter_id','encounterId')):
            if (payload or {}).get(key):s.setdefault('requested_'+field,str(payload[key]))
        if (payload or {}).get('documentId'):s['targets']['emr_document']=[str(payload['documentId'])]
        return self.capture.invoke(path,payload or {},s,lambda:self.client.post(path,payload))

class ObservedProtocolPort:
    """Explicit new entry point; existing clinical runners never enable this by default."""
    def __init__(self,protocol_port,capture):
        self.port=protocol_port;self.capture=capture;native=protocol_port.native
        base=native._client;wrapped={}
        def observed_client(role):
            if role not in wrapped:wrapped[role]=ObservedHTTPClient(base(role),capture)
            return wrapped[role]
        native._client=observed_client
    def __getattr__(self,name):return getattr(self.port,name)
    def execute(self,operation,payload=None,context=None):
        if isinstance(operation,dict):
            action=operation;context=payload if context is None else context;operation=action.get('operation') or action.get('name');payload=action.get('payload',{})
        ctx=dict(context or {});token=SCOPE.set({k:ctx[k] for k in ('case_id','run_id','patient_id','encounter_id','role','actor_id','action_request_id') if k in ctx})
        state_files=[self.port.native.state_path,self.port.state_path]
        before={p.name:filehash(p) for p in state_files}
        self.capture.log('gateway_action_intent',operation=operation,scope=SCOPE.get(),payload_hash=digest(payload or {}),sidecar_before=before)
        try:
            result=self.port.execute(operation,payload,ctx)
            self.capture.log('gateway_action_outcome',operation=operation,scope=SCOPE.get(),status=result.get('status'),native_effect=result.get('native_effect'),
                             protection_source=result.get('protection_source'),result_hash=digest(result),sidecar_after={p.name:filehash(p) for p in state_files})
            return result
        except BaseException as exc:
            self.capture.log('gateway_action_outcome',operation=operation,scope=SCOPE.get(),status='unresolved_or_blocked',error_type=type(exc).__name__,
                             sidecar_after={p.name:filehash(p) for p in state_files});raise
        finally:SCOPE.reset(token)
