#!/usr/bin/env python3
"""One independent fictional native decision-note probe; zero model calls.

Registers a new fictional patient (ordinary simulated registration cash fee),
starts reception, saves/revises/locks a no-Rx note. Never submits a diagnosis,
prescription or drug-settlement request. This tests interfaces, not clinical care.
"""
import datetime,hashlib,json,secrets,sys
from pathlib import Path
HERE=Path(__file__).resolve().parent;ROOT=HERE.parents[1];sys.path.insert(0,str(ROOT))
from sandbox.experiments.linked.native_his import NativeOpenHISPort,write_private,digest
from sandbox.experiments.linked.protocol_his import PaperProtocolPort
from tools.audit_linked_workflows import ReadOnlyObserver

def main():
    if (HERE/'no_rx_document_probe_v1.json').exists():raise RuntimeError('Probe already exists; use a new output version before new native writes')
    tag='no-rx-note-probe-'+datetime.datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+secrets.token_hex(3)
    folder=HERE/'runtime'/tag;native=NativeOpenHISPort(folder);port=PaperProtocolPort(native)
    ctx={'case_id':'independent-fictional-no-rx','run_id':tag,'role':'administrative','actor_id':'administrative-1'}
    steps=[];full=[]
    def call(op,payload,role):
        ctx.update(role=role,actor_id=role+'-1');r=port.execute(op,payload,ctx)
        full.append({'operation':op,'result':r});steps.append({'operation':op,'status':r['status'],
          'native_effect':r['native_effect'],'readback_verified':r['readback_verified'],'protection_source':r['protection_source']})
        return r['data']
    reg=call('registration.create',{'patient_key':ctx['case_id'],'demographics':{'age':45,'sex':'F'}},'administrative');ctx.update(reg)
    call('queue.enter',{'encounter_id':ctx['encounter_id']},'administrative')
    call('encounter.begin_consultation',{},'doctor')
    note={'text':'独立虚构接口测试。未开处方、未作疾病诊断；记录暂缓处方决定及后续核查需要。不是临床建议。',
          'diagnoses':[],'prescription':{'items':[]},'clinical_disposition':{'status':'no_prescription_issued','three_task_eligible':False}}
    saved=call('note.save',{'note_id':'decision-note','content':note},'doctor')
    revised={**note,'review_record':'独立接口复核版本；无处方与无药费状态保持。'}
    rev=call('note.revise',{'note_id':'decision-note','content':revised,'expected_version':saved['version']},'doctor')
    call('note.attest',{'note_id':'decision-note','version':rev['version']},'doctor')
    binding=native._binding(ctx);obs=ReadOnlyObserver().observe(binding)
    diagnosis=native._client('doctor').post('/doctorStationDiagnosis/getEncounterDiagnosis',{'patientId':binding['patient_id'],'encounterId':binding['encounter_id']})
    pending=native._client('cashier').post('/outpatientCharge/loadPendingChargeItems',{'encounterId':binding['encounter_id']})
    doc=obs.get('document',{});content=doc.get('content');content=json.loads(content) if isinstance(content,str) else content
    expected={'type':'doc','content':[{'type':'paragraph','content':[{'type':'text','text':json.dumps(revised,ensure_ascii=False,sort_keys=True)}]}]}
    payments=obs.get('payments',[]);registration=[p for p in payments if str(p.get('id'))==binding['registration_payment_id']]
    other=[p for p in payments if str(p.get('id'))!=binding['registration_payment_id']]
    checks={'native_note_locked':doc.get('status')=='LOCKED','native_note_content_exact':content==expected,
      'native_note_patient_encounter_bound':str(doc.get('patientId'))==binding['patient_id'] and str(doc.get('encounterId'))==binding['encounter_id'],
      'native_orders_empty':obs.get('advice',{}).get('content')==[],
      'native_diagnoses_empty':diagnosis.get('diagnosisList',diagnosis.get('content',[]))==[],
      'no_drug_settlement_calls':not any(x['operation'].startswith('payment.') for x in steps),
      'no_nonregistration_payment':len(other)==0,'registration_payment_separately_observed':len(registration)==1,
      'read_observer_errors_absent':not obs.get('errors'),'no_pending_charge_items':pending.get('items')==[],
      'note_versions_saved':rev['version']==2 and binding['note']['version']==2}
    evidence={'tag':tag,'steps':full,'read_only_observation':obs,'diagnosis_readback':diagnosis,'pending_readback':pending,'checks':checks}
    private=folder/'independent_readback.json';write_private(private,evidence)
    summary={'version':'no-rx-native-note-probe-1','data_kind':'independent_fictional','engineering_only':True,
      'model_calls':0,'clinical_quality_assessed':False,'all_checks_passed':all(checks.values()),'checks':checks,'steps':steps,
      'clinical_decision_status':'no_prescription_issued','original_three_task_eligible':False,
      'drug_payment_completion':False,'note_versions':rev['version'],
      'registration_fee':{'count':len(registration),'amounts':[p['amount'] for p in registration],'simulated':True},
      'nonregistration_payments':len(other),'raw_native_evidence_sha256':hashlib.sha256(private.read_bytes()).hexdigest(),
      'signed_by_id_independently_verified':False,'attestation_scope':'authenticated doctor application document LOCKED; not CA signing',
      'consultation_without_diagnosis_persistence':'decision captured in native note; no native diagnosis inferred or invented'}
    target=HERE/'no_rx_document_probe_v1.json'
    if target.exists():raise RuntimeError('Previous probe summary already exists; preserve it and use a new report version')
    target.write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'all_checks_passed':summary['all_checks_passed'],'note_versions':rev['version'],'drug_payments':len(other),'model_calls':0}))
    if not summary['all_checks_passed']:raise SystemExit(1)
if __name__=='__main__':main()
