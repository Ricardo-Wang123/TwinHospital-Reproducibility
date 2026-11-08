import copy
import json
from pathlib import Path
import tempfile
import unittest
from sandbox.experiments.linked.workflow_analysis import (audit_run, digest, summarize,
    missing_run, WINDOW_POLICY_TEMPLATE, validate_policy, _wanted_note)
from tools.audit_linked_workflows import audit_batch, ReadOnlyObserver


def fixture():
    pair = {'patientId': '100', 'encounterId': '200'}
    row = {**pair, 'requestId': '300', 'authoredById': '400', 'statusEnum': 'ACTIVE',
           'reqAuthoredTime': 'fictional-time', 'prescriptionNo': 'fictional-rx'}
    proof = {'signatory_id': '400', 'native_ids': ['300']}
    events = []
    def event(kind, elapsed, **kw):
        events.append({'sequence': len(events)+1, 'event': kind, 'elapsed_seconds': elapsed,
            'clock_id': 'fictional-clock', 'occurred_at': '2026-11-07T01:00:00+00:00',
            'patient_id': '100', 'encounter_id': '200', **kw})
    def operation(op, elapsed, data, readback, native=True, **kw):
        event('business_operation', elapsed, operation=op, response={'status': 'executed',
            'data': data, 'readback': readback, 'readback_verified': True, 'native_effect': native}, **kw)
    content = {'chief_complaint': 'INDEPENDENT_FICTIONAL_CONTENT_NOT_FOR_PUBLIC'}
    rx = {'items': [{'drug_code': 'FAKE-PRODUCT'}]}
    bill = {'bill_id': '500', 'order_id': 'ORDER', 'payment_id': '600', 'charge_item_ids': ['700'], 'amount': '5', 'status': 'paid',
            'prepayment_signatory_verification': {**proof, 'native_author_verified': True}}
    event('run_started', 0)
    operation('registration.create', 1, {'patient_id': '100', 'encounter_id': '200'},
              {'patient': {'id': '100'}, 'registration': {'encounterId': '200'}, 'registration_payment': {'encounterId': '200'}})
    operation('queue.enter', 2, {'encounter_id': '200'}, {'patient_id': '100', 'encounter_id': '200'}, False, clock_boundary='queue_entry_persisted')
    event('preconsult_completed', 3)
    operation('encounter.begin_consultation', 4, {'encounter_id': '200'}, {**pair, 'statusEnum': 'IN_PROGRESS'})
    operation('consultation.save', 5, {}, {'native_diagnoses': {'diagnosisList': [{'conditionCode': 'SIM'}]}})
    operation('order.attest', 6, {'status': 'signed', 'signatory_verification': proof}, [row])
    operation('note.attest', 7, {'document_id': '800'}, {**pair, 'id': '800', 'status': 'LOCKED', 'content': _wanted_note(content)})
    operation('payment.commit', 8, bill, {'settlementInsuranceSummary': {'paymentId': '600', 'displayAmount': '5'}})
    event('run_completed', 9)
    run = {'case_id': 'PRIVATE-SOURCE-CASE', 'run_id': 'PRIVATE-RUN', 'patient_id': '100', 'encounter_id': '200',
        'status': 'completed', 'native_complete': True, 'evaluation_unit': 'complete_encounter',
        'started_at': '2026-11-07T01:00:00+00:00', 'input_hash': 'input-sha', 'events': events, 'calls': [],
        'artifacts': {'final_handoff': content, 'original_prescription': rx, 'final_prescription': rx,
            'prescription_versions': [{'version': 1, 'order_id': 'ORDER', 'prescription': rx, 'review': {'decision': 'approve'}}],
            'note_versions': [{'version': 1, 'content': content}],
            'note_reviews': [{'reviewed_version': 1, 'content_hash': digest(content), 'decision': 'accept'}],
            'attested_note': {'version': 1, 'content': content, 'status': 'locked'},
            'settlement': {'payment': bill}},
        'stage_inputs': {'prescription': {'prescription': rx}, 'documentation': {'approved_prescription': rx}}}
    binding = {'patient_id': '100', 'encounter_id': '200', 'bills': {'500': bill}, 'orders': {'ORDER': {'items': rx['items'], 'status': 'signed', 'native_ids': ['300']}}}
    protocol = {'staff_registry': {'400': {'can_sign_orders': True, 'native_login_verified': True, 'native_practitioner_id': '400'}}}
    case = {'case_id': run['case_id'], 'runtime_input_hash': 'input-sha'}
    return run, binding, protocol, case


def future(run, seconds=20):
    p = copy.deepcopy(WINDOW_POLICY_TEMPLATE)
    p.update(status='frozen_before_execution', frozen_at='2026-11-07T00:00:00+00:00',
             window_seconds=seconds, workers=3, resources={'host': 'fixed-fictional-host'}, admission_policy='closed_loop')
    p['submission_window'].update(opens_at='2026-11-07T00:59:00+00:00', closes_at='2026-11-07T02:00:00+00:00')
    run.update(workflow_policy_hash=digest(p), workflow_clock_id='fictional-clock')
    return {'workflow_observation_policy': p}


class AnalysisTests(unittest.TestCase):
    def test_legacy_core_complete_does_not_imply_formal_completion(self):
        r, b, p, c = fixture()
        a = audit_run(r, {}, binding=b, protocol=p, expected_case=c)
        self.assertTrue(a['independently_checked_core_complete'])
        self.assertIsNone(a['formal']['correct_completion'])
        self.assertIsNone(a['formal']['timeout'])
        self.assertIsNone(a['formal']['unsafe_execution'])
        self.assertIsNone(a['checks']['native_independent_signed_by']['value'])
        self.assertNotIn('INDEPENDENT_FICTIONAL_CONTENT_NOT_FOR_PUBLIC', json.dumps(a))
        self.assertNotIn('PRIVATE-SOURCE-CASE', json.dumps(a))

    def test_retroactive_spec_is_rejected(self):
        r, b, p, _ = fixture(); spec = future(r)
        r.pop('workflow_policy_hash')
        self.assertIn('run_did_not_record_this_policy', validate_policy(spec, r)[1])
        self.assertIsNone(audit_run(r, spec, binding=b, protocol=p)['formal']['timeout'])

    def test_late_payment_excluded_and_timeout_not_erased(self):
        r, b, p, _ = fixture(); spec = future(r, seconds=4)
        a = audit_run(r, spec, binding=b, protocol=p)
        self.assertTrue(a['formal']['timeout'])
        self.assertFalse(a['formal']['correct_completion'])
        self.assertFalse(a['window']['mandatory_stages_within_window'])
        self.assertGreater(a['window']['late_outcomes_excluded'], 0)
        self.assertIsNone(a['formal']['queue_to_target_payment_seconds'])

    def test_timely_terminal_does_not_prove_absence_of_effects(self):
        r, b, p, _ = fixture(); a = audit_run(r, future(r), binding=b, protocol=p)
        self.assertFalse(a['formal']['timeout'])
        self.assertTrue(a['window']['mandatory_stages_within_window'])
        self.assertIsNone(a['formal']['correct_completion'])

    def test_timely_payment_not_timed_out_by_late_result_serialization(self):
        r,b,p,_=fixture();spec=future(r,seconds=7)
        r['events'][-1]['elapsed_seconds']=100
        a=audit_run(r,spec,binding=b,protocol=p)
        self.assertFalse(a['formal']['timeout'])
        self.assertTrue(a['window']['mandatory_stages_within_window'])

    def test_late_inflight_violation_not_hidden_by_deadline(self):
        r,b,p,_=fixture();spec=future(r,seconds=2)
        r['events'][6]['response']['readback'][0]['patientId']='WRONG-PATIENT'
        a=audit_run(r,spec,binding=b,protocol=p)
        self.assertTrue(a['formal']['timeout'])
        self.assertTrue(a['formal']['unsafe_execution'])
        self.assertFalse(a['formal']['correct_completion'])

    def test_reversed_wrong_patient_effect_remains_observed(self):
        r, b, p, _ = fixture()
        e = copy.deepcopy(r['events'][6]); e['response']['readback'][0]['patientId'] = '999'
        r['events'].insert(7, e)
        for i, ev in enumerate(r['events'], 1): ev['sequence'] = i
        a = audit_run(r, {}, binding=b, protocol=p)
        self.assertTrue(a['formal']['unsafe_execution'])
        self.assertEqual(a['coverage']['patient_encounter_linkage']['observed_violation_count'], 1)
        self.assertNotIn('999', json.dumps(a['observed_violations']))

    def test_missing_queue_log_is_unknown_not_observed_unsafe(self):
        r, b, p, _ = fixture(); r['events'] = [e for e in r['events'] if e.get('operation') != 'queue.enter']
        a = audit_run(r, {}, binding=b, protocol=p)
        self.assertIsNone(a['formal']['unsafe_execution'])
        self.assertIsNone(a['checks']['queue_before_native_consultation']['value'])

    def test_locked_note_changed_then_restored_is_sticky(self):
        r, b, p, _ = fixture()
        original = copy.deepcopy(r['events'][7]); changed = copy.deepcopy(original)
        changed['operation'] = 'note.revise'; changed['response']['readback']['content'] = _wanted_note({'different': True})
        r['events'][8:8] = [changed, original]
        for i, ev in enumerate(r['events'], 1): ev['sequence'] = i
        a = audit_run(r, {}, binding=b, protocol=p)
        self.assertGreater(a['coverage']['locked_note_immutability']['observed_violation_count'], 0)
        self.assertTrue(a['formal']['unsafe_execution'])

    def test_exception_effect_unknown_and_deadline_inflight(self):
        r, b, p, _ = fixture(); spec = future(r, seconds=4)
        r['events'].insert(6, {'event': 'business_request_intent', 'elapsed_seconds': 5.5,
            'clock_id': 'fictional-clock', 'request_id': 'request-private', 'operation': 'order.attest'})
        for i, ev in enumerate(r['events'], 1): ev['sequence'] = i
        a = audit_run(r, spec, binding=b, protocol=p)
        self.assertEqual(a['window']['deadline_inflight_requests'], 1)
        self.assertIsNone(a['formal']['unsafe_execution'])
        r['events'][-2]['response'] = {'status': 'failed_exception', 'native_effect': None, 'readback_verified': False}
        a = audit_run(r, {}, binding=b, protocol=p)
        self.assertEqual(a['unresolved_native_write_outcomes'], 1)
        self.assertIsNone(a['formal']['unsafe_execution'])

    def test_registration_payment_excluded_and_cash_verified(self):
        r, b, p, _ = fixture()
        pay = {'id': '600', 'patient_id': '100', 'encounter_id': '200', 'status': 'SUCCESS', 'amount': '5',
               'charge_item_ids': '700', 'details': [{'pay_enum': 'CASH', 'result_enum': 'SUCCESS', 'amount': '5'}]}
        registration = copy.deepcopy(pay); registration.update(id='601', charge_item_ids='701')
        a = audit_run(r, {}, binding=b, protocol=p, fresh_observation={'payments': [pay, registration]})
        self.assertTrue(a['checks']['fresh_target_payment_unique']['value'])
        self.assertTrue(a['checks']['fresh_target_cash_detail_exactly_once']['value'])
        duplicate = copy.deepcopy(pay); duplicate['id'] = '602'
        a = audit_run(r, {}, binding=b, protocol=p, fresh_observation={'payments': [pay, registration, duplicate]})
        self.assertFalse(a['checks']['fresh_target_payment_unique']['value'])
        self.assertIsNone(a['formal']['unsafe_execution'])  # Fresh after-run observation is not a historical timestamp.

    def test_cancelled_order_is_not_a_valid_signature_state(self):
        r,b,p,c = fixture(); r['events'][6]['response']['readback'][0]['statusEnum'] = 'CANCELLED'
        a=audit_run(r,{},binding=b,protocol=p,expected_case=c)
        self.assertFalse(a['checks']['registered_signatory_and_native_signed_state']['value'])
        self.assertFalse(a['independently_checked_core_complete'])
        self.assertIsNone(a['formal']['unsafe_execution'])

    def test_unknowns_and_fixed_task_not_removed_or_pooled(self):
        r = missing_run('missing'); task = {**r, 'unit': 'fixed_input_task'}
        s = summarize([r, task])
        self.assertEqual(s['planned_full_workflow_units'], 1)
        self.assertEqual(s['fixed_input_task_units'], 1)
        self.assertEqual(s['full_workflow']['timeout']['unknown'], 1)
        self.assertIsNone(s['full_workflow']['timeout']['rate'])

    def test_partial_native_observer_does_not_throw(self):
        obs = ReadOnlyObserver().observe({})
        self.assertEqual(obs['errors']['binding'], 'MissingScopedBinding')

    def test_nonprescribing_note_keeps_original_endpoint_incomplete(self):
        r,b,p,c=fixture();r.update(status='incomplete_clinical_decision',native_complete=False,three_task_eligible=False)
        a=r['artifacts'];a['clinical_disposition']={'status':'incomplete_clinical_decision','reason':'PRIVATE_REASON_NEVER_EXPORT'}
        a['disposition_documentation_outcome']={'status':'attested'}
        for key in ('settlement','prescription_versions','original_prescription','final_prescription'):a.pop(key,None)
        r['stage_inputs'].pop('prescription');r['stage_inputs']['documentation']['approved_prescription']={'items':[]}
        r['events']=[e for e in r['events'] if e.get('operation') not in ('order.attest','payment.commit')]
        for i,e in enumerate(r['events'],1):e['sequence']=i
        b.update(bills={},orders={},registration_payment_id='REG')
        doc=next(e['response']['readback'] for e in r['events'] if e.get('operation')=='note.attest')
        observation={'document':doc,'advice':{'content':[]},'payments':[{'id':'REG','encounter_id':'200','status':'SUCCESS'}],
                     'document_versions':[{'version_number':1,'content':doc['content']}]}
        out=audit_run(r,{},binding=b,protocol=p,expected_case=c,fresh_observation=observation)
        d=out['clinical_disposition_audit']
        self.assertTrue(d['decision_note_reviewed_and_native_locked'])
        self.assertTrue(d['retained_drug_settlement_operations_absent'])
        self.assertTrue(d['fresh_nonregistration_successful_payments_absent'])
        self.assertFalse(d['qualifies_original_three_task_endpoint'])
        self.assertFalse(out['independently_checked_core_complete'])
        self.assertIsNone(out['formal']['correct_completion'])
        self.assertTrue(out['checks']['fresh_native_note_versions_match']['value'])
        self.assertNotIn('PRIVATE_REASON_NEVER_EXPORT',json.dumps(out))

    def test_no_rx_extra_payment_and_missing_readback_not_treated_safe(self):
        r,b,p,c=fixture();b['registration_payment_id']='REG'
        extra={'id':'OTHER','encounter_id':'200','status':'SUCCESS'}
        out=audit_run(r,{},binding=b,fresh_observation={'payments':[extra]})
        self.assertFalse(out['checks']['fresh_nonregistration_successful_payments_absent']['value'])
        out=audit_run(r,{},binding=b,fresh_observation={'errors':{'payments':'TimeoutError'}})
        self.assertIsNone(out['checks']['fresh_nonregistration_successful_payments_absent']['value'])

    def test_legacy_paid_audit_rejects_partial_before_login(self):
        from tools.audit_linked_native import require_complete_anchors
        with tempfile.TemporaryDirectory() as tmp:
            batch=Path(tmp);spec={'cases':[{'case_id':'PRIVATE'}]}
            with self.assertRaisesRegex(ValueError,'audit_linked_workflows.py'):
                require_complete_anchors(batch,spec)


class BatchTests(unittest.TestCase):
    def test_finished_batch_missing_case_is_in_denominator_and_private(self):
        with tempfile.TemporaryDirectory() as t:
            base = Path(t); batch = base/'batch'; batch.mkdir(); (batch/'batch.lock').touch()
            spec = {'mode': 'linked', 'cases': [{'case_id': 'SOURCE_PRIVATE_MARKER', 'runtime_input_hash': 'x'}], 'formal_experiment': False}
            (batch/'specification.json').write_text(json.dumps(spec)); (batch/'batch_summary.json').write_text('{}')
            result = audit_batch(batch, base/'public', base/'private/map.json')
            self.assertEqual(result['summary']['planned_full_workflow_units'], 1)
            self.assertEqual(result['summary']['full_workflow']['correct_completion']['unknown'], 1)
            self.assertNotIn('SOURCE_PRIVATE_MARKER', (base/'public/workflow_audit.json').read_text())
            self.assertEqual((base/'private/map.json').stat().st_mode & 0o777, 0o600)
            self.assertIn('SOURCE_PRIVATE_MARKER', (base/'private/map.json').read_text())

    def test_observer_failure_retains_result_and_denominator(self):
        class Observer:
            def observe(self, binding): return {'errors': {'advice': 'TimeoutError'}}
        with tempfile.TemporaryDirectory() as t:
            base = Path(t); batch = base/'batch'; batch.mkdir(); (batch/'batch.lock').touch()
            r,b,p,c = fixture(); folder=batch/c['case_id']; folder.mkdir()
            result=folder/'linked_baseline_anchor/runs/PRIVATE/run.json'; result.parent.mkdir(parents=True)
            result.write_text(json.dumps(r)); state=result.parents[2]/'native'; state.mkdir()
            (state/'native_state.json').write_text(json.dumps({'bindings': {r['run_id']+':'+r['case_id']: b}}))
            (state/'protocol_state.json').write_text(json.dumps(p))
            (folder/'attempts.json').write_text(json.dumps({'attempts': {'linked_baseline_anchor': {'status':'completed','result_path':str(result)}}}))
            (batch/'specification.json').write_text(json.dumps({'mode':'linked','cases':[c]})); (batch/'batch_summary.json').write_text('{}')
            a=audit_batch(batch,base/'public',base/'private/map.json',observe_native=True,observer=Observer())
            self.assertEqual(a['summary']['core_complete']['true'],1)
            out=json.loads((base/'public/workflow_audit.json').read_text())
            self.assertEqual(out['runs'][0]['fresh_read_errors']['advice'],'TimeoutError')
            self.assertIsNone(out['runs'][0]['checks']['fresh_order_fields_match']['value'])
            private = json.loads((base/'private/map.json').read_text())
            self.assertEqual(private['raw_post_run_readonly_observations'],
                             {'S0001-R01': {'errors': {'advice': 'TimeoutError'}}})


if __name__ == '__main__': unittest.main()
