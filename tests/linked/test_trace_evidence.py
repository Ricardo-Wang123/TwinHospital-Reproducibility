from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sandbox.experiments.linked.capture_index import indexed_capture
from sandbox.experiments.linked.trace_evidence import verify, prepare_capture
from sandbox.experiments.linked.native_his import digest
from sandbox.experiments.linked.native_observation import TABLES, api_coverage
from test_workflow_analysis import fixture, future
from tools.audit_linked_workflows import audit_batch


def bundle():
    run,binding,protocol,_=fixture();spec=future(run)
    run['ended_at']='2026-11-07T01:00:09+00:00'
    api=api_coverage();spec['code_hashes']=api['source_sha256']
    coverage={'declared_tables':list(TABLES),'observed_triggers':[
        {'table':t,'enabled':'O','definition':'AFTER INSERT OR DELETE OR UPDATE th_observation_v1.capture()'} for t in TABLES],
        'append_only_guards_enabled':True,'function_definition_hashes':{'capture':'test-only-hash'}}
    meta={'session_id':'test-session','started_at':'2026-11-07T00:59:00+00:00',
          'finished_at':'2026-11-07T01:01:00+00:00','status':'closed',
          'policy':{'workflow_policy_hash':digest(spec['workflow_observation_policy'])},
          'coverage':coverage,'coverage_at_end':deepcopy(coverage),'coverage_unchanged':True,'api_coverage':api}
    rows={
        'registration.create':[('adm_patient',{'id':100}),('adm_encounter',{'id':200,'patient_id':100})],
        'encounter.begin_consultation':[('adm_encounter',{'id':200,'patient_id':100,'status_enum':'IN_PROGRESS'})],
        'consultation.save':[('adm_encounter_diagnosis',{'id':210,'encounter_id':200})],
        'order.attest':[('med_medication_request',{'id':300,'patient_id':100,'encounter_id':200,
            'requester_id':400,'status_enum':'ACTIVE','req_authored_time':'fixture-time','prescription_no':'fixture-no'})],
        'note.attest':[('emr_document',{'id':800,'patient_id':100,'encounter_id':200,'status':'LOCKED',
            'content':next(e['response']['readback']['content'] for e in run['events'] if e.get('operation')=='note.attest')})],
        'payment.commit':[('adm_charge_item',{'id':700,'patient_id':100,'encounter_id':200,
                              'service_table':'med_medication_request','service_id':300}),
                          ('fin_payment_reconciliation',{'id':600,'patient_id':100,'encounter_id':200,
                              'status_enum':'SUCCESS','charge_item_ids':[700]}),
                          ('fin_payment_rec_detail',{'id':900,'reconciliation_id':600,
                              'pay_enum':'CASH','result_enum':'SUCCESS','amount':5})]}
    paths={'registration.create':'/patient/createPatient',
           'encounter.begin_consultation':'/doctorStationEncounterManage/startOutpatientReception',
           'consultation.save':'/doctorStationDiagnosis/saveDoctorDiagnosis',
           'order.attest':'/doctorStationAdvice/signDoctorStationAdviceBatch',
           'note.attest':'/document/changeLockState',
           'payment.commit':'/outpatientCharge/confirmOutpatientChargeSettlement'}
    events=[];journal=[]
    for event in run['events']:
        if event.get('event')!='business_operation':continue
        op=event['operation'];request='action-'+str(event['sequence'])
        event.update(request_id=request,payload_hash=digest({'fixture':op}))
        scope={'run_id':run['run_id'],'action_request_id':request,'patient_id':'100','encounter_id':'200'}
        journal.append({'event':'gateway_action_intent','scope':scope,'operation':op,'payload_hash':event['payload_hash']})
        if event['response']['native_effect']:
            native_id='native-'+request;ids=[]
            journal.append({'event':'business_request_intent','request_id':native_id,'scope':scope,'operation':paths[op]})
            for table,row in rows[op]:
                eid=len(events)+1;ids.append(eid)
                events.append({'event_id':eid,'session_id':'test-session','request_window_id':native_id,
                    'attribution':'exclusive_window_candidate_not_transaction_proof','table_name':table,
                    'action':'INSERT','old_data':None,'new_data':deepcopy(row)})
            journal.append({'event':'business_request_outcome','request_id':native_id,'status':'response_and_readback_observed',
                            'observed_event_ids':ids,'observed_scope':scope})
        journal.append({'event':'gateway_action_outcome','scope':scope,'operation':op,'result_hash':digest(event['response'])})
    meta['observed_event_count']=len(events)
    capture={'metadata':meta,'mutations':{'events':events,'session_id':'test-session','full_session_rescan':True},'journal':journal}
    return run,spec,capture,protocol


def write_streamed_capture(folder, capture):
    folder.mkdir(parents=True, exist_ok=True)
    meta = folder / 'observation_session.json'
    mutations = folder / 'native_committed_mutations.json'
    journal = folder / 'observation_intents.jsonl'
    meta.write_text(json.dumps(capture['metadata']))
    journal.write_text(''.join(json.dumps(entry) + '\n' for entry in capture['journal']))
    data = capture['mutations']
    header = json.dumps({key: value for key, value in data.items() if key != 'events'})
    mutations.write_text(header[:-1] + ',\n"events": [\n' +
                         ',\n'.join(json.dumps(event) for event in data['events']) + '\n]}\n')
    return meta, mutations, journal


class TraceEvidenceTests(unittest.TestCase):
    def test_prepared_batch_matches_fresh_analysis_and_rejects_other_sources(self):
        r,s,c,p=bundle();index=prepare_capture(c,s)
        self.assertEqual(verify(r,s,c,p),verify(r,s,c,p,prepared=index))
        with self.assertRaisesRegex(ValueError,'another capture'):
            verify(r,s,deepcopy(c),p,prepared=index)
        changed=deepcopy(s);changed['workflow_observation_policy']['window_seconds']+=1
        with self.assertRaisesRegex(ValueError,'another capture'):
            verify(r,changed,c,p,prepared=index)

    def test_declared_scope_only_complete_with_bound_raw_evidence(self):
        r,s,c,p=bundle();a=verify(r,s,c,p)
        self.assertTrue(a['declared_invariant_coverage_complete'],a['unknown_reasons'])
        self.assertEqual(a['observed_violations'],[])
        self.assertIsNone(a['whole_system_safe'])
        self.assertFalse(a['backend_transaction_caller_identity_verified'])

    def test_streamed_capture_matches_memory_for_complete_and_unbound_events(self):
        r, s, original, p = bundle()
        with TemporaryDirectory() as directory:
            work = Path(directory)
            for scenario in ('complete', 'unbound', 'ambiguous_owner'):
                with self.subTest(scenario=scenario):
                    capture = deepcopy(original)
                    changes = capture['mutations']
                    if scenario == 'unbound':
                        extra = deepcopy(changes['events'][0])
                        extra['request_window_id'] = 'other-encounter'
                        changes['events'].append(extra)
                        capture['metadata']['observed_event_count'] += 1
                    elif scenario == 'ambiguous_owner':
                        extra = deepcopy(next(row for row in changes['events'] if row['table_name'] == 'adm_encounter'))
                        extra.update(event_id=1000, request_window_id='other-encounter')
                        extra['new_data']['patient_id'] = 999
                        changes['events'].append(extra)
                        capture['metadata']['observed_event_count'] += 1
                        scope = {'run_id': 'other-run', 'action_request_id': 'other-action'}
                        capture['journal'].extend([
                            {'event': 'business_request_intent', 'request_id': 'other-encounter', 'scope': scope,
                             'operation': '/outpatientRegistration/payRegisterFee'},
                            {'event': 'business_request_outcome', 'request_id': 'other-encounter',
                             'status': 'response_and_readback_observed', 'observed_event_ids': [1000]},
                        ])
                    meta, mutations, journal = write_streamed_capture(work, capture)
                    expected = verify(r, s, capture, p)
                    with indexed_capture(meta, mutations, journal, s, workspace=work) as (source, index):
                        actual = verify(r, s, source, p, prepared=index)
                    self.assertEqual(actual, expected)
                    self.assertFalse(list(work.glob('.capture-index-*')))
                    if scenario == 'unbound':
                        self.assertIn('unattributed_or_quarantined_native_effect', actual['unknown_reasons'])
                        self.assertIn('duplicate_committed_event_id', actual['unknown_reasons'])
                    if scenario == 'ambiguous_owner':
                        self.assertIn('mutation_business_ownership_unresolved', actual['unknown_reasons'])

    def test_batch_audit_uses_streamed_capture_and_releases_the_private_index(self):
        r, s, capture, p = bundle()
        _, binding, _, case = fixture()
        s.update(mode='linked', cases=[case])
        with TemporaryDirectory() as directory:
            root = Path(directory)
            batch = root / 'batch'
            case_dir = batch / case['case_id']
            attempt = case_dir / 'linked_baseline_anchor'
            result = attempt / 'runs/fixture/run.json'
            result.parent.mkdir(parents=True)
            result.write_text(json.dumps(r))
            (batch / 'batch.lock').touch()
            (batch / 'specification.json').write_text(json.dumps(s))
            (batch / 'batch_summary.json').write_text('{}')
            (case_dir / 'attempts.json').write_text(json.dumps({'attempts': {
                'linked_baseline_anchor': {'result_path': str(result), 'status': 'completed'},
            }}))
            native = attempt / 'native'
            native.mkdir()
            (native / 'native_state.json').write_text(json.dumps({'bindings': {
                r['run_id'] + ':' + r['case_id']: binding,
            }}))
            (native / 'protocol_state.json').write_text(json.dumps(p))
            write_streamed_capture(batch / 'native_observation', capture)
            private = root / 'private/audit.json'
            audit_batch(batch, root / 'public', private)
            report = json.loads(private.read_text())['audit']
            self.assertIs(report['runs'][0]['formal']['correct_completion'], True)
            self.assertEqual(report['runs'][0]['trace_capture_evidence'], verify(r, s, capture, p))
            self.assertFalse(list(private.parent.glob('.capture-index-*')))

    def test_full_auditor_joins_actual_trace_proof_with_paid_endpoint(self):
        from sandbox.experiments.linked.workflow_analysis import audit_run
        r,s,c,p=bundle();_,binding,_,case=fixture()
        a=audit_run(r,s,binding=binding,protocol=p,expected_case=case,raw_capture=c)
        self.assertTrue(a['formal']['correct_completion'],a['trace_capture_evidence']['unknown_reasons'])
        self.assertFalse(a['formal']['unsafe_execution'])
        self.assertEqual(a['formal']['queue_to_target_payment_seconds'],6)
        c['mutations']['full_session_rescan']=False
        a=audit_run(r,s,binding=binding,protocol=p,expected_case=case,raw_capture=c)
        self.assertIsNone(a['formal']['correct_completion'])

    def test_later_spec_cannot_promote_legacy_capture(self):
        r,s,c,p=bundle();c['metadata']['policy'].pop('workflow_policy_hash')
        a=verify(r,s,c,p)
        self.assertFalse(a['declared_invariant_coverage_complete'])

    def test_child_row_wrong_patient_is_detected_via_parent(self):
        r,s,c,p=bundle()
        event=next(e for e in c['mutations']['events'] if e['table_name']=='fin_payment_reconciliation')
        event['new_data']['patient_id']=999
        a=verify(r,s,c,p)
        self.assertIn('patient_encounter_linkage',{v['constraint'] for v in a['observed_violations']})

    def test_late_unframed_mutation_never_ignored(self):
        r,s,c,p=bundle();event=deepcopy(c['mutations']['events'][0]);event['event_id']=999;event['request_window_id']=None
        c['mutations']['events'].append(event);c['metadata']['observed_event_count']+=1
        a=verify(r,s,c,p)
        self.assertIn('unattributed_or_quarantined_native_effect',a['unknown_reasons'])

    def test_final_readback_cannot_replace_missing_committed_note(self):
        r,s,c,p=bundle()
        event=next(e for e in c['mutations']['events'] if e['table_name']=='emr_document')
        event['new_data']['status']='DRAFT'
        a=verify(r,s,c,p)
        self.assertIn('locked_note_readback_not_present_in_committed_capture',a['unknown_reasons'])

    def test_locked_note_mutation_stays_violation_after_restore(self):
        r,s,c,p=bundle();event=next(e for e in c['mutations']['events'] if e['table_name']=='emr_document')
        event['action']='UPDATE';event['old_data']=deepcopy(event['new_data'])
        event['new_data']['content']={'changed':'unauthorized'}
        a=verify(r,s,c,p)
        self.assertIn('locked_note_immutability',{v['constraint'] for v in a['observed_violations']})

    def test_unresolved_native_request_cannot_be_safe(self):
        r,s,c,p=bundle()
        next(x for x in c['journal'] if x['event']=='business_request_outcome')['status']='unresolved_quarantined'
        a=verify(r,s,c,p)
        self.assertFalse(a['declared_invariant_coverage_complete'])

    def test_native_author_is_checked_against_registered_signatory(self):
        r,s,c,p=bundle()
        next(e for e in c['mutations']['events'] if e['table_name']=='med_medication_request')['new_data']['requester_id']=999
        a=verify(r,s,c,p)
        self.assertIn('authorized_signatory_before_payment',{v['constraint'] for v in a['observed_violations']})

    def test_unjoined_gateway_action_does_not_get_a_pass(self):
        r,s,c,p=bundle();c['journal'][0]['scope']['action_request_id']='unbound'
        a=verify(r,s,c,p)
        self.assertFalse(a['declared_invariant_coverage_complete'])

    def test_native_backed_catalogue_read_does_not_require_a_mutation(self):
        r,s,c,p=bundle();event={'event':'business_operation','operation':'diagnosis.catalogue',
            'request_id':'catalogue-read','sequence':0,'payload_hash':digest({}),
            'response':{'native_effect':True,'readback_verified':True,'status':'executed'}}
        r['events'].append(event);scope={'run_id':r['run_id'],'action_request_id':'catalogue-read'}
        c['journal'].extend([
            {'event':'gateway_action_intent','scope':scope,'operation':event['operation'],'payload_hash':event['payload_hash']},
            {'event':'gateway_action_outcome','scope':scope,'operation':event['operation'],'result_hash':digest(event['response'])}])
        a=verify(r,s,c,p);self.assertTrue(a['declared_invariant_coverage_complete'],a['unknown_reasons'])
        # An unframed side effect during that read must still make coverage unknown.
        e=deepcopy(c['mutations']['events'][0]);e.update(event_id=999,request_window_id=None)
        c['mutations']['events'].append(e);c['metadata']['observed_event_count']+=1
        self.assertIn('unattributed_or_quarantined_native_effect',verify(r,s,c,p)['unknown_reasons'])


if __name__=='__main__':unittest.main()
