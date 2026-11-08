"""Independent read-only audit of the15 paired branches for each source case.

Clinical content is compared locally. Public evidence contains study aliases,
booleans, counts and hashes. Authentication is the only non-read API activity.
No native business write or original experiment-file write is permitted.
"""
from __future__ import annotations
import argparse
from collections import Counter
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from sandbox.experiments.linked.native_his import OpenHISClient,ROLE_SETTINGS,digest,write_private
from sandbox.experiments.linked.contracts import runtime_case


def read(path):return json.loads(Path(path).read_text())
def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Observer(OpenHISClient):
    READS={"/document/getContent","/doctorStationAdvice/listDoctorStationAdvice",
           "/doctorStationEncounterManage/pageOutpatientInfoForDoctorStation"}
    AUTH={"/auth/login","/auth/selectTenant","/auth/selectHospital","/auth/selectOrganizationUnit"}
    def post(self,path,payload=None):
        if path not in self.READS|self.AUTH:raise ValueError("Audit refuses non-read native operation")
        return super().post(path,payload)


def payment_observation(encounter_ids):
    if any(not re.fullmatch(r"[0-9]{1,19}",str(x)) for x in encounter_ids):raise ValueError("invalid native identity")
    spec=importlib.util.spec_from_file_location("stage_audit_db",ROOT/"deploy/openhis-experiment/manage.py")
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    sql="""BEGIN READ ONLY;
SELECT coalesce(jsonb_agg(jsonb_build_object('id',p.id::text,'patient_id',p.patient_id::text,
 'encounter_id',p.encounter_id::text,'status',p.status_enum,'amount',p.display_amount::text,
 'charge_item_ids',p.charge_item_ids,'details',(SELECT coalesce(jsonb_agg(jsonb_build_object(
 'id',d.id::text,'pay_enum',d.pay_enum,'result_enum',d.result_enum,'amount',d.amount::text)),'[]'::jsonb)
 FROM fin_payment_rec_detail d WHERE d.reconciliation_id=p.id AND NOT d.is_deleted))),'[]'::jsonb)
 FROM fin_payment_reconciliation p WHERE NOT p.is_deleted AND p.encounter_id IN ("""+",".join(encounter_ids)+"); COMMIT;"
    return json.loads(module.sql(args=["-A","-t"],text=sql).stdout.strip())


def audit(batch_dir,case_manifest,model_calls,public_path,private_path):
    batch,case_manifest,model_calls,public,private=map(lambda x:Path(x).resolve(),(batch_dir,case_manifest,model_calls,public_path,private_path))
    if public.exists() or private.exists():raise ValueError("New audit output paths required")
    spec=read(batch/"specification.json")
    if spec["mode"]!="matrix" or not (batch/"batch_summary.json").exists():raise ValueError("finished matrix required")
    if spec["anchor_reuse"]["new_anchor_execution"] is not False:raise ValueError("expected frozen external anchor reuse")
    before={str(p):sha(p) for p in batch.rglob('*') if p.is_file()}
    def pin(path):
        path=Path(path).resolve();value=sha(path)
        if str(path) in before and before[str(path)]!=value:raise ValueError("source changed before observation")
        before[str(path)]=value;return value
    pin(case_manifest)
    for name in ('input_change_audit.json','input_freeze.json'):
        if (case_manifest.parent/name).exists():pin(case_manifest.parent/name)
    case_paths={x['case_id']:Path(x['path']) for x in read(case_manifest)['cases']}
    source_spec=Path(spec['anchor_reuse']['source_batch_path'])/'specification.json'
    frozen_source_spec_ok=pin(source_spec)==spec['anchor_reuse']['source_specification_sha256']
    anchors={x['case_id']:x for x in spec['anchor_reuse']['cases']}
    credpath=ROOT/'deploy/openhis-experiment/runtime/accounts/credentials.json'
    if credpath.stat().st_mode & 0o077:raise ValueError("private credential mode required")
    credentials=read(credpath);name,org,_=ROLE_SETTINGS['doctor'];doctor=Observer()
    doctor.login(name,credentials[name],organization=org);credentials.clear()
    records=[];native_ids={name:set() for name in ['patient','encounter','contract','run','queue','handoff','document','registration_payment']}
    for sn,case in enumerate(spec['cases'],1):
        cp=case_paths[case['case_id']]
        if not cp.is_absolute():cp=case_manifest.parent/cp
        input_file_ok=pin(cp)==case['file_sha256'];input_case=read(cp)
        input_runtime_ok=digest(runtime_case(input_case))==case['runtime_input_hash'] and digest(input_case)==case['case_hash']
        anchor_ref=anchors[case['case_id']];anchor_path=Path(anchor_ref['source_run_path']);anchor=read(anchor_path)
        anchor_ok=(pin(anchor_path)==anchor_ref['source_run_file_sha256'] and digest(anchor)==anchor_ref['source_run_canonical_hash'] and
                   pin(anchor_ref['source_index_path'])==anchor_ref['source_index_sha256'] and frozen_source_spec_ok)
        idxpath=batch/case['case_id']/'attempts.json';idx=read(idxpath)
        if len(idx['attempts'])!=15:raise ValueError("expected15 indexed stage branches per case")
        for an,(label,index_record) in enumerate(idx['attempts'].items(),1):
            alias=f"S{sn:04d}-A{an:02d}";runpath=Path(index_record['result_path']).resolve()
            if batch not in runpath.parents:raise ValueError("stage run escapes audited batch")
            run=read(runpath);task=next(iter(run['conditions']));nativepath=runpath.parents[2]/'native/native_state.json'
            protocolpath=nativepath.with_name('protocol_state.json');state=read(nativepath);protocol=read(protocolpath)
            key=run['run_id']+':'+run['case_id'];binding=state['bindings'][key];a=run['artifacts'];checks={}
            def check(name,value):checks[name]=bool(value)
            check('case_input_frozen',input_file_ok and input_runtime_ok and run['input_hash']==case['runtime_input_hash'])
            check('source_anchor_frozen',anchor_ok)
            check('source_index_anchor_reference_matches',idx['anchor_source']['source_run_file_sha256']==anchor_ref['source_run_file_sha256'])
            check('run_case_config_index_match',run['case_id']==case['case_id']==idx['case_id'] and run['config_id']==index_record['config_id'] and
                  run['run_id']==spec['batch']+':'+case['case_id']+':'+label and run['status']==index_record['status'])
            check('one_native_binding_for_branch',len(state['bindings'])==1 and binding['case_id']==case['case_id'] and binding['run_id']==run['run_id'])
            check('run_encounter_patient_binding',all(str(run[k])==str(binding[k]) for k in ('patient_id','encounter_id')))
            check('all_events_same_case_run_and_business_pair',all(e['case_id']==run['case_id'] and e['run_id']==run['run_id'] and
                all(not e.get(k) or str(e[k])==str(binding[k]) for k in ('patient_id','encounter_id')) for e in run['events']))
            check('task_only_not_complete_encounter',run['evaluation_unit']=='fixed_input_task' and run['native_complete'] is False and run['workflow_complete'] is False)
            check('model_observation_response_hashes',all(digest(c['observation'])==c['observation_hash'] and
                ('response' not in c or digest(c['response'])==c['response_hash']) for c in run['calls']))
            check('clinical_gold_not_claimed',run['clinical_quality']['status'] in {'not_independently_evaluated','not_assessed_outside_programmatic_scope'})
            operations=[e for e in run['events'] if e['event']=='business_operation']
            check('no_stage_order_or_medication_payment_write',not any(e['operation'].startswith(('order.','payment.')) for e in operations))
            check('no_sidecar_order_or_bill',not binding.get('orders') and not binding.get('bills'))
            check('no_settlement_artifact',not a.get('settlement'))
            q=protocol['queues'][key]
            native_ids['queue'].add(q['entry_id'])
            check('protocol_queue_binding_and_unsettled',all(str(q[k])==str(binding[k]) for k in ['case_id','run_id','patient_id','encounter_id']) and q['status']!='settled')
            native={}
            pair={'patientId':binding['patient_id'],'encounterId':binding['encounter_id']}
            found=doctor.post('/doctorStationEncounterManage/pageOutpatientInfoForDoctorStation',{
                'staffPractitionerId':ROLE_SETTINGS['doctor'][2],'keyword':binding['registration_name'],'pageNo':1,'pageSize':10})
            observed=[x for x in found.get('content',[]) if str(x.get('encounterId'))==binding['encounter_id'] and str(x.get('patientId'))==binding['patient_id']]
            check('native_encounter_pair_fresh_readback',len(observed)==1);native['encounter_response_hash']=digest(found)
            advice=doctor.post('/doctorStationAdvice/listDoctorStationAdvice',pair)
            check('native_no_new_orders',not advice.get('content'));native['advice_response_hash']=digest(advice)
            for name,bfield in [('patient','patient_id'),('encounter','encounter_id'),('contract','contract_id'),('registration_payment','registration_payment_id')]:native_ids[name].add(binding[bfield])
            native_ids['run'].add(run['run_id'])
            counts={'calls':len(run['calls']),'business_operations':len(operations),'native_order_rows':len(advice.get('content',[]))}
            outcomes={'completed':run['status']=='completed','incomplete_clinical_decision':run['status']=='incomplete_clinical_decision',
                      'failed':run['status']=='failed','approve':False,'reject':False,'defer':False,'parse_failure':run.get('error_type')=='JSONDecodeError'}
            request_hashes={};missing_requests=0
            for call in run['calls']:
                rid=call.get('request_id')
                if not rid:missing_requests+=1;continue
                rp=model_calls/(rid+'.json')
                if not rp.exists():missing_requests+=1;continue
                request=read(rp);request_hashes[rid]=pin(rp)
                if request.get('case_id')!=run['case_id'] or request.get('run_id')!=run['run_id']:missing_requests+=1
            check('model_request_evidence_retained_and_bound',missing_requests==0)
            if outcomes['parse_failure']:
                failed=[c for c in run['calls'] if c.get('error_type')=='JSONDecodeError']
                check('parse_failure_raw_provider_response_retained',bool(failed) and all(c.get('request_id') in request_hashes and
                    isinstance(read(model_calls/(c['request_id']+'.json')).get('response_body',{}).get('choices',[{}])[0].get('message',{}).get('content'),str) for c in failed))
            if task=='preconsult':
                check('empty_shared_stage_input',run['stage_inputs']['preconsult']=={} and run['stage_input_hash']==digest({}))
                saved=binding['artifacts']['handoff'];read_events=[e for e in operations if e['operation']=='handoff.read']
                native_ids['handoff'].add(saved['artifact_id'])
                check('handoff_persisted_sidecar_content_matches',saved['content']==a['final_handoff'] and saved['version']==1)
                check('handoff_runtime_readback_matches',len(read_events)==1 and read_events[0]['response']['readback_verified'] is True and
                    read_events[0]['response']['data']['content']==a['final_handoff'] and read_events[0]['response']['native_effect'] is False)
                dc=[c for c in run['calls'] if c['role']=='doctor' and c['phase']=='doctor_handoff_verification']
                check('doctor_received_actual_persisted_handoff',bool(dc) and all(c['observation']['prior_summary']==saved['content'] for c in dc))
                check('doctor_summary_retained',any(c.get('response',{}).get('action')=='summary' and c['response']['content']==a['physician_verified_handoff'] for c in dc))
                asks=[c['response']['question'] for c in dc if c.get('response',{}).get('action')=='ask']
                tr=[x for x in a['transcript'] if x['phase']=='doctor_handoff_verification'];patient_calls=[c for c in run['calls'] if c['role']=='patient' and c['phase']=='doctor_handoff_verification']
                check('doctor_questions_actual_transcript',asks==[x['text'] for x in tr if x['speaker_role']=='doctor'])
                check('doctor_followup_patient_answers_actual',len(asks)==len(patient_calls) and asks==[c['observation']['question'] for c in patient_calls] and
                    [c.get('response',{}).get('text') for c in patient_calls]==[x['text'] for x in tr if x['speaker_role']=='patient'])
                check('doctor_did_not_receive_hidden_collection_transcript',all(all(x['phase'] in {'doctor_handoff_verification','consultation'} for x in c['observation']['conversation']) for c in dc))
                counts.update(doctor_followup_questions=len(asks),doctor_followup_answers=len(patient_calls))
                native['handoff_sidecar_content_hash']=digest(saved);native['verified_handoff_hash']=digest(a['physician_verified_handoff'])
            elif task=='prescription':
                frozen=anchor['stage_inputs']['prescription'];stage=run['stage_inputs']['prescription']
                check('stage_input_matches_v7_frozen_source',stage==frozen and run['stage_input_hash']==digest(frozen))
                check('original_candidate_matches_frozen_prescription',a['original_prescription']==frozen['prescription'])
                candidates=[e for e in run['events'] if e['event']=='prescription_candidate_recorded']
                check('original_candidate_private_journal_retained',len(candidates)==1 and candidates[0]['prescription']==frozen['prescription'] and candidates[0]['native_write_attempted'] is False)
                review=a.get('original_prescription_review');versions=a.get('prescription_versions',[])
                if review:
                    outcomes.update({k:review['decision']==k for k in ['approve','reject','defer']})
                    check('review_refers_to_unchanged_original',review['prescription_hash']==digest(frozen['prescription']) and len(versions)==1 and
                        versions[0]['prescription']==frozen['prescription'] and versions[0]['review']==review and versions[0]['native_write_attempted'] is False)
                    check('decision_outcome_preserved',((review['decision']=='defer')==(run['status']=='incomplete_clinical_decision')) and
                        (review['decision']!='defer' or a.get('unresolved_prescription_adjudication',{}).get('status')=='deferred'))
                else:
                    check('review_absence_kept_as_failure_not_approval',run['status']=='failed' and not versions and 'final_prescription' not in a)
                if outcomes['parse_failure']:
                    failed=[c for c in run['calls'] if c.get('error_type')=='JSONDecodeError']
                    check('parse_failure_raw_provider_response_retained',bool(failed) and all(c.get('request_id') in request_hashes and
                        isinstance(read(model_calls/(c['request_id']+'.json')).get('response_body',{}).get('choices',[{}])[0].get('message',{}).get('content'),str) for c in failed))
                reviewer_calls=[c for c in run['calls'] if c['role'] in {'pharmacist','rx_ai','senior_pharmacist'}]
                check('all_observed_reviewers_use_fixed_candidate_and_context',all(c['observation'].get('prescription')==frozen['prescription'] and c['observation'].get('clinical_context')==frozen['clinical_context'] for c in reviewer_calls))
                if not reviewer_calls:
                    check('zero_reviewer_calls_explicit_engineering_pass_path',run['conditions'][task]['workflow']=='pprs_baseline' and
                          bool(review) and review['rule_review']['decision']=='pass' and review['pharmacist_review'] is None and review['ai_review'] is None)
                counts.update(pharmacist_model_calls=sum(c['role']=='pharmacist' for c in reviewer_calls),
                              ai_review_model_calls=sum(c['role']=='rx_ai' for c in reviewer_calls),
                              senior_pharmacist_model_calls=sum(c['role']=='senior_pharmacist' for c in reviewer_calls))
                counts['prescription_versions']=len(versions)
            else:
                frozen=anchor['stage_inputs']['documentation'];stage=run['stage_inputs']['documentation'];versions=a['note_versions'];reviews=a.get('note_reviews',[])
                check('stage_input_matches_v7_frozen_source',stage==frozen and run['stage_input_hash']==digest(frozen))
                check('note_versions_sequential_and_retained',[x['version'] for x in versions]==list(range(1,len(versions)+1)))
                check('note_reviews_bound_to_exact_versions',bool(reviews) and all(1<=r['reviewed_version']<=len(versions) and r['content_hash']==digest(versions[r['reviewed_version']-1]['content']) for r in reviews))
                check('all_saved_versions_received_review',set(x['version'] for x in versions)<={r['reviewed_version'] for r in reviews})
                check('saved_revisions_match_review_content',all(not r.get('revision_saved') or (r['reviewed_version']<len(versions) and versions[r['reviewed_version']]['content']==r['response']['content']) for r in reviews))
                check('generation_uses_source_transcript_prefix',all(c['observation']['transcript']==frozen['transcript'][:len(c['observation']['transcript'])] for c in run['calls'] if c['phase'].startswith('documentation_draft_')))
                note=binding['note'];doc=doctor.post('/document/getContent',{'documentId':note['native_id']});native_ids['document'].add(note['native_id'])
                content=json.loads(doc['content']) if isinstance(doc['content'],str) else doc['content']
                wanted={'type':'doc','content':[{'type':'paragraph','content':[{'type':'text','text':json.dumps(versions[-1]['content'],ensure_ascii=False,sort_keys=True)}]}]}
                check('native_current_text_matches_last_saved_version',content==wanted and str(doc['patientId'])==pair['patientId'] and str(doc['encounterId'])==pair['encounterId'] and note['version']==versions[-1]['version'])
                if outcomes['completed']:
                    check('completed_note_locked_and_accepted',doc.get('status')=='LOCKED' and a['attested_note']['content']==versions[-1]['content'] and
                        reviews[-1]['decision']=='accept' and reviews[-1]['reviewed_version']==versions[-1]['version']==a['attested_note']['version'])
                else:
                    check('failed_note_draft_retained_not_attested',doc.get('status')!='LOCKED' and 'attested_note' not in a and bool(versions) and note['status']=='draft')
                native.update(document_response_hash=digest(doc),native_document_id=note['native_id'])
                counts.update(note_versions=len(versions),note_reviews=len(reviews),unsaved_revision_proposals=sum(r['decision']=='revise' and not r.get('revision_saved') for r in reviews))
            records.append({'study_attempt':alias,'checks':checks,'counts':counts,'outcomes':outcomes,'source_run_file_sha256':sha(runpath),
                'stage_input_hash':run['stage_input_hash'],'private':{'case_id':run['case_id'],'run_id':run['run_id'],'task':task,
                    'patient_id':binding['patient_id'],'encounter_id':binding['encounter_id'],'contract_id':binding['contract_id'],
                    'registration_payment_id':binding['registration_payment_id'],'run_path':str(runpath),'native_path':str(nativepath),
                    'native_response_hashes':native,'model_request_evidence_hashes':request_hashes}})
    payments=payment_observation(sorted(native_ids['encounter']))
    for row in records:
        p=row['private'];own=[x for x in payments if x['encounter_id']==p['encounter_id']]
        reg=[x for x in own if x['id']==p['registration_payment_id']];other=[x for x in own if x['id']!=p['registration_payment_id']]
        row['checks']['native_registration_payment_only']=len(reg)==1 and not other and reg[0]['patient_id']==p['patient_id']
        row['checks']['native_no_new_medication_settlement']=not other
        row['counts'].update(registration_payment_records=len(reg),other_payment_records=len(other),other_successful_payments=sum(x['status']=='SUCCESS' for x in other))
        p['payment_database_readback']=own
    unchanged=all(sha(p)==h for p,h in before.items())
    tasks={task:[r for r in records if r['private']['task']==task] for task in ['preconsult','prescription','documentation']}
    aggregate={task:{'attempts':len(rows),**{k:sum(r['outcomes'][k] for r in rows) for k in ['completed','incomplete_clinical_decision','failed','approve','reject','defer','parse_failure']}} for task,rows in tasks.items()}
    global_checks={'source_files_unchanged':unchanged,'stage_count45':len(records)==45,
        'unique_patients45':len(native_ids['patient'])==45,'unique_encounters45':len(native_ids['encounter'])==45,
        'shared_contract_definition_not_branch_identity':True,'unique_run_ids45':len(native_ids['run'])==45,
        'unique_queue_entries45':len(native_ids['queue'])==45,'unique_handoff_objects15':len(native_ids['handoff'])==15,
        'unique_registration_payments45':len(native_ids['registration_payment'])==45,'unique_documents15':len(native_ids['document'])==15,
        'all_task_counts15':all(len(v)==15 for v in tasks.values()),'no_clinical_correctness_claim':True,
        'no_native_version_history_claim':True,'post_run_observation_only':True}
    public_rows=[{k:v for k,v in r.items() if k!='private'} for r in records]
    result={'audit_version':2,'attempts':public_rows,'aggregate':aggregate,'checks':global_checks,
        'business_object_counts':{k:len(v) for k,v in native_ids.items()},
        'protected_file_count':len(before),'source_specification_sha256':sha(batch/'specification.json'),
        'all_evidence_checks_passed':all(global_checks.values()) and all(all(r['checks'].values()) for r in records)}
    write_private(private,{**result,'attempts':records,'source_file_hashes_before':before,
        'evidence_scope':'Version history is retained runtime artifacts, not independently read native historical versions; native current text/status read fresh. Handoff persistence is sidecar, not native EHR document. Approval is a model disposition, not clinical correctness.'})
    public.parent.mkdir(parents=True,exist_ok=True);public.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    return {'all_evidence_checks_passed':result['all_evidence_checks_passed'],'attempt_count':len(records),'aggregate':aggregate,
        'global_checks':global_checks,'failed_checks':{r['study_attempt']:[k for k,v in r['checks'].items() if not v] for r in records if not all(r['checks'].values())},'public_audit':str(public)}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for n in ['batch-dir','case-manifest','model-calls','public-path','private-path']:p.add_argument('--'+n,required=True)
    a=p.parse_args();print(json.dumps(audit(a.batch_dir,a.case_manifest,a.model_calls,a.public_path,a.private_path),ensure_ascii=False,indent=2))
