"""Recovery and budget stop tests; no model or real OpenHIS calls."""
from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import gc
import io
import json
from pathlib import Path
import tempfile
from threading import Event, Lock
from types import SimpleNamespace
import unittest
import weakref
from unittest.mock import patch

from sandbox.experiments.linked.cases import build_synthetic_case
from sandbox.experiments.linked.contracts import digest
from sandbox.experiments.linked.provider import BudgetExceeded, BudgetLedger, ProviderUnavailable, GENERATION_POLICY_V2
from sandbox.experiments.linked import run_batch
from sandbox.experiments.linked.cases import GROUPS
from sandbox.experiments.linked.linked_acceptance import BASELINE_LABEL, attempt_label, documented_unpaid_stop, freeze_design
from test_linked_clinical import FakeFactory, FakePort, example_case


class LinkedBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.args = SimpleNamespace(batch='test_v2', mode='matrix', model_evidence=self.root/'evidence',
                                    his_credentials=None, his_url='http://127.0.0.1:28081/api/app')
        self.ledger = BudgetLedger(self.root/'ledger.sqlite', 1000)
        self.case = example_case()
        self.config = {}

    def tearDown(self):
        self.temp.cleanup()

    def test_linked_acceptance_stops_before_new_work_after_a_failed_regression(self):
        models = {'tool_a': 'glm-test'}
        refs = [{'case_id': f'{group}-{n}', 'patient_key': f'{group}-{n}',
                 'group': group, 'data_kind': 'mimic_derived', 'split': 'development'}
                for group in GROUPS for n in range(10)]
        design = freeze_design(refs, models)
        self.args.workflow_policy_data = None
        passed = {'status': 'completed', 'result_path': '/saved/run.json'}
        failed = {'status': 'incomplete_clinical_decision'}
        with patch.object(run_batch, 'execute_case_reference', side_effect=[
            {'case_id': refs[0]['case_id'], 'attempts': {BASELINE_LABEL: passed}},
            {'case_id': refs[1]['case_id'], 'attempts': {BASELINE_LABEL: failed}},
        ]) as run, patch.object(run_batch, '_acceptance_progression_checks',
            side_effect=[{'paid': True}, {'paid': False}]), redirect_stdout(io.StringIO()):
            rows, stop = run_batch.execute_acceptance_schedule(refs, design, args=self.args,
                config={'tool_models': models}, mapping={}, ledger=self.ledger,
                batch_root=self.root, stop_event=Event(), specification={},
                session_log=self.root/'events.jsonl', session_id='regression')
        self.assertEqual([call.args[0] for call in run.call_args_list], refs[:2])
        self.assertEqual(stop['case_id'], refs[1]['case_id'])
        records = [attempt for row in rows for attempt in row['attempts'].values()]
        self.assertEqual(len(records), 350)
        unstarted = [r for r in records if r['status'] == 'not_run_regression_gate']
        self.assertEqual(len(unstarted), 348)
        self.assertTrue(all(r['model_requests_sent'] is False and r['native_effects_started'] is False
                            for r in unstarted))

    def test_dispatch_journal_appends_across_sessions_and_stays_private(self):
        path = self.root/'batch_sessions.jsonl'
        path.write_text('{"event":"earlier_session"}\n')
        path.chmod(0o644)
        run_batch.record_batch_event(path, 'case_dispatched', session_id='new_session', case_id='case_a')
        run_batch.record_batch_event(path, 'case_worker_returned', session_id='new_session',
                                    case_id='case_a', status='index_returned')
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual([row['event'] for row in rows],
                         ['earlier_session', 'case_dispatched', 'case_worker_returned'])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertIn('+00:00', rows[-1]['observed_at'])
        before = path.read_bytes()
        with self.assertRaises(ValueError):
            run_batch.record_batch_event(path, 'bad', elapsed=float('nan'))
        self.assertEqual(path.read_bytes(), before)

    def test_completed_invalid_json_is_retryable_only_before_unresolved_or_payment_effects(self):
        result = {'status': 'failed', 'native_complete': False, 'error_type': 'JSONDecodeError',
            'events': [{'event': 'role_response_json_invalid', 'correction_scheduled': False,
                        'parse_metadata': {'finish_reason': 'stop'}},
                       {'event': 'run_failed', 'error_type': 'JSONDecodeError'}]}
        self.assertTrue(documented_unpaid_stop(result))
        for effect in [{'event': 'business_request_intent', 'operation': 'payment.create'},
                       {'event': 'business_request_outcome', 'status': 'unresolved_exception'}]:
            self.assertFalse(documented_unpaid_stop({**result, 'events': [effect, *result['events']]}))
        self.assertFalse(documented_unpaid_stop({**result, 'events': result['events'][1:]}))

    def test_factorial_assignment_workers_merge_concurrent_profiles_for_one_patient(self):
        from threading import Barrier

        models = {'tool_a': 'glm-test'}
        refs = [{'case_id': f'{group}-{n}', 'patient_key': f'{group}-{n}',
                 'group': group, 'data_kind': 'mimic_derived', 'split': 'development'}
                for group in GROUPS for n in range(2)]
        self.args.workflow_policy_data = None
        self.args.workers = 12
        self.args.prior_verified_paid = {(refs[0]['case_id'], BASELINE_LABEL)}
        design = freeze_design(refs, models, workers=12, patients_per_group=2, factorial=True)
        same_patient = Barrier(2)
        labels = {profile['label'] for profile in design['profiles'][1:3]}

        def execute(ref, *, acceptance_profile, isolated_acceptance_index, **_):
            self.assertTrue(isolated_acceptance_index)
            self.assertNotIn((ref['case_id'], acceptance_profile), self.args.prior_verified_paid)
            if ref == refs[0] and acceptance_profile in labels:
                same_patient.wait(timeout=5)
            record = {'status': 'completed', 'result_path': '/saved/paid.json'}
            return {'case_id': ref['case_id'], 'attempts': {acceptance_profile: record}}

        with patch.object(run_batch, 'execute_case_reference', side_effect=execute), \
                patch.object(run_batch, '_acceptance_progression_checks', return_value={'paid': True}):
            rows, stop = run_batch.execute_acceptance_schedule(refs, design, args=self.args,
                config={'tool_models': models}, mapping={}, ledger=self.ledger, batch_root=self.root,
                stop_event=Event(), specification={}, session_log=self.root/'factorial.jsonl', session_id='factorial')
        self.assertIsNone(stop)
        self.assertEqual(rows[0]['attempts'][BASELINE_LABEL]['status'], 'not_run_verified_paid_prior_capture')
        for row in rows:
            self.assertEqual(len(row['attempts']), 27)
            self.assertEqual(json.loads((self.root/row['case_id']/'attempts.json').read_text()), row)

    def test_acceptance_keeps_a_documented_deferral_and_retries_that_assignment(self):
        models = {'tool_a': 'glm-test'}
        refs = [{'case_id': f'{group}-{n}', 'patient_key': f'{group}-{n}',
                 'group': group, 'data_kind': 'mimic_derived', 'split': 'development'}
                for group in GROUPS for n in range(20)]
        design = freeze_design(refs, models, patients_per_group=20)
        self.args.workflow_policy_data = None
        deferral = self.root/'unpaid.json'
        deferral.write_text(json.dumps({'status': 'incomplete_clinical_decision', 'native_complete': False,
            'artifacts': {'clinical_disposition': {'drug_settlement_attempted': False},
                          'disposition_documentation_outcome': {'status': 'attested'}}}))

        def execute(ref, *, acceptance_profile, acceptance_attempt, **_):
            case_id = ref['case_id']
            path = self.root/case_id/'attempts.json'
            path.parent.mkdir(exist_ok=True)
            index = json.loads(path.read_text()) if path.exists() else {'case_id': case_id, 'attempts': {}}
            key = attempt_label(acceptance_profile, acceptance_attempt)
            if ref == refs[0]:
                index['attempts'][key] = ({'status': 'incomplete_clinical_decision', 'result_path': str(deferral)}
                    if acceptance_attempt == 1 else {'status': 'completed', 'result_path': '/saved/paid.json'})
            else:
                index['attempts'][key] = {'status': 'failed'}
            return index

        with patch.object(run_batch, 'execute_case_reference', side_effect=execute) as run, \
                patch.object(run_batch, '_acceptance_progression_checks',
                             side_effect=lambda _r, record, _p, _s: {'paid': record['status'] == 'completed'}), \
                redirect_stdout(io.StringIO()):
            rows, stop = run_batch.execute_acceptance_schedule(refs, design, args=self.args,
                config={'tool_models': models}, mapping={}, ledger=self.ledger, batch_root=self.root,
                stop_event=Event(), specification={}, session_log=self.root/'events.jsonl', session_id='retry')
        self.assertEqual([call.kwargs['acceptance_attempt'] for call in run.call_args_list], [1, 2, 1])
        self.assertEqual(rows[0]['attempts'][BASELINE_LABEL]['attempt_number'], 2)
        self.assertEqual(rows[0]['prior_attempts'][BASELINE_LABEL][0]['result_path'], str(deferral))
        self.assertEqual(sum(len(row['attempts']) for row in rows), 700)
        self.assertEqual(stop['case_id'], refs[1]['case_id'])

    def test_parallel_acceptance_has_one_index_writer_per_patient(self):
        from threading import Barrier, Lock

        models = {'tool_a': 'glm-test'}
        refs = [{'case_id': f'{group}-{n}', 'patient_key': f'{group}-{n}',
                 'group': group, 'data_kind': 'mimic_derived', 'split': 'development'}
                for group in GROUPS for n in range(10)]
        self.args.workflow_policy_data = None
        self.args.workers = 2
        design = freeze_design(refs, models, workers=2)
        active = set()
        dispatched = []
        lock = Lock()
        simultaneous = Barrier(2)

        def execute(ref, *, acceptance_profile, **_):
            case_id = ref['case_id']
            with lock:
                self.assertNotIn(case_id, active)
                active.add(case_id)
                dispatched.append((case_id, acceptance_profile))
                ordinal = len(dispatched)
            try:
                if ordinal in (3, 4):
                    simultaneous.wait(timeout=5)
                path = self.root/case_id/'attempts.json'
                index = json.loads(path.read_text()) if path.exists() else {'case_id': case_id, 'attempts': {}}
                index['attempts'][acceptance_profile] = {'status': 'completed', 'result_path': '/saved/paid.json'}
                return index
            finally:
                with lock:
                    active.remove(case_id)

        with patch.object(run_batch, 'execute_case_reference', side_effect=execute), \
                patch.object(run_batch, '_acceptance_progression_checks', return_value={'paid': True}):
            rows, stop = run_batch.execute_acceptance_schedule(refs, design, args=self.args,
                config={'tool_models': models}, mapping={}, ledger=self.ledger,
                batch_root=self.root, stop_event=Event(), specification={}, session_log=self.root/'parallel.jsonl',
                session_id='parallel')
        self.assertIsNone(stop)
        self.assertEqual(dispatched[:2], [(ref['case_id'], BASELINE_LABEL) for ref in refs[:2]])
        self.assertEqual(len(set(dispatched)), 350)
        self.assertTrue(all(len(row['attempts']) == 7 for row in rows))

    def test_diagnosis_preflight_preserves_system_and_rejects_ambiguous_native_binding(self):
        mapping = {'E11.9': {'code': 'E11.9', 'display': 'Legacy diabetes',
                            'system': 'ICD-10', 'conditionDefinitionId': '1'},
                   'E11.65': {'code': 'E11.65', 'display': 'Diabetes with hyperglycemia',
                             'system': 'ICD-10-CM', 'conditionDefinitionId': '2'}}
        self.assertEqual(run_batch.validate_diagnosis_mapping(mapping), mapping)
        bad = deepcopy(mapping)
        bad['E11.65']['conditionDefinitionId'] = '1'
        with self.assertRaisesRegex(ValueError, 'one native definition'):
            run_batch.validate_diagnosis_mapping(bad)
        bad = deepcopy(mapping)
        del bad['E11.65']['system']
        with self.assertRaisesRegex(ValueError, 'explicit code/system'):
            run_batch.validate_diagnosis_mapping(bad)
        with self.assertRaises(ValueError):
            run_batch.validate_diagnosis_mapping({})

    def invoke(self, factory=None, *, stop=None, batch_root=None):
        batch_root = batch_root or self.root
        batch_root.mkdir(parents=True, exist_ok=True)
        with ExitStack() as stack:
            models = stack.enter_context(patch.object(run_batch, 'BudgetedCamelFactory',
                side_effect=lambda *a, **kw: factory or FakeFactory()))
            ports = stack.enter_context(patch.object(run_batch, 'NativeOpenHISPort',
                side_effect=lambda *a, **kw: FakePort()))
            stack.enter_context(patch.object(run_batch, 'PaperProtocolPort', new=lambda port: port))
            stack.enter_context(redirect_stdout(io.StringIO()))
            index = run_batch.execute_case(self.case, args=self.args, config=self.config, mapping={},
                ledger=self.ledger, batch_root=batch_root, stop_event=stop)
        return index, models.call_count, ports.call_count

    def test_matrix_shares_frozen_input_and_resume_does_not_repeat_effects(self):
        index, models, ports = self.invoke()
        self.assertEqual((models, ports, len(index['attempts'])), (16, 16, 16))
        anchor = json.loads(Path(index['attempts']['linked_baseline_anchor']['result_path']).read_text())
        for label, row in index['attempts'].items():
            result = json.loads(Path(row['result_path']).read_text())
            self.assertEqual(result['status'], 'completed')
            if label.startswith(('prescription', 'documentation')):
                task = next(iter(result['conditions']))
                self.assertEqual(result['stage_inputs'][task], anchor['stage_inputs'][task])
                self.assertEqual(result['stage_input_hash'], digest(anchor['stage_inputs'][task]))
        resumed, models, ports = self.invoke()
        self.assertEqual(resumed, index)
        self.assertEqual((models, ports), (0, 0))

    def test_provider_freeze_preserves_unstarted_cells_without_mislabelling_budget(self):
        self.ledger.reserve('frozen-response',.1,case_id='probe',role='rx_ai',model='glm-5.3')
        self.ledger.settle('frozen-response',amount=0,status='http_403')
        self.ledger.halt_provider('frozen-response','ModelArts.81006')
        index,models,ports=self.invoke()
        self.assertEqual((models,ports),(0,0))
        self.assertEqual(len(index['attempts']),16)
        self.assertEqual({x['status'] for x in index['attempts'].values()},{'not_run_provider_unavailable'})
        self.assertTrue(all(x['native_effects_started'] is False for x in index['attempts'].values()))

    def test_operator_stop_before_admission_retains_every_cell_without_model_calls(self):
        run_batch._atomic(self.root/'operator_stop.json', {'version':'operator_stop_v1',
            'requested_at':'2026-11-07T12:00:00+00:00','reason_code':'implementation_repair'})
        event=Event()
        index,models,ports=self.invoke(stop=event)
        self.assertEqual((models,ports),(0,0))
        self.assertEqual(len(index['attempts']),16)
        self.assertFalse(event.is_set())
        self.assertTrue(all(row['status']=='not_run_operator_interruption' for row in index['attempts'].values()))
        self.assertTrue(all(not row['model_requests_sent'] and not row['native_effects_started']
                            for row in index['attempts'].values()))
        self.assertFalse(self.ledger.summary().get('provider_unavailable'))

    def test_operator_stop_during_anchor_finishes_it_and_preserves_other_assignments(self):
        root=self.root
        class StopAfterStarting(FakeFactory):
            def session(self,scope,role,model_family=None):
                session=super().session(scope,role,model_family)
                original=session.respond
                def respond(observation):
                    if not (root/'operator_stop.json').exists():
                        run_batch._atomic(root/'operator_stop.json',{'version':'operator_stop_v1',
                            'requested_at':'2026-11-07T12:00:00+00:00','reason_code':'implementation_repair'})
                    return original(observation)
                session.respond=respond
                return session
        index,models,ports=self.invoke(StopAfterStarting())
        self.assertEqual((models,ports),(1,1))
        self.assertEqual(index['attempts']['linked_baseline_anchor']['status'],'completed')
        self.assertEqual(sum(row['status']=='not_run_operator_interruption' for row in index['attempts'].values()),15)

    def test_operator_stop_requires_explicit_reason_and_aware_timestamp(self):
        for fields in ({'reason_code':'silent_budget_freeze','requested_at':'2026-11-07T12:00:00+00:00'},
                       {'reason_code':'implementation_repair','requested_at':'2026-11-07T12:00:00'}):
            run_batch._atomic(self.root/'operator_stop.json',{'version':'operator_stop_v1',**fields})
            with self.assertRaises(ValueError):run_batch.operator_stop_request(self.root)

    def test_route_variant_is_shared_across_all_five_conditions_and_resume_is_bound(self):
        from test_prescription_variants import Factory,case
        self.case=case();self.args.matrix_task='prescription';self.args.engineering_only=True
        self.args.close_prescription_loop=True;self.args.controlled_prescription_variant='exact-product-route'
        index,models,ports=self.invoke(Factory())
        self.assertEqual((models,ports,len(index['attempts'])),(6,6,6))
        anchor=json.loads(Path(index['attempts']['linked_baseline_anchor']['result_path']).read_text())
        hashes=set();paths=[]
        for label,row in index['attempts'].items():
            if label=='linked_baseline_anchor':continue
            result=json.loads(Path(row['result_path']).read_text());paths.append(Path(row['result_path']))
            self.assertEqual(result['status'],'completed',result.get('failure_reason'))
            hashes.add(result['stage_input_hash'])
            self.assertNotEqual(result['stage_input_hash'],digest(anchor['stage_inputs']['prescription']))
            self.assertEqual(result['stage_inputs']['prescription']['clinical_context'],anchor['stage_inputs']['prescription']['clinical_context'])
            self.assertEqual(result['artifacts']['original_prescription_review']['decision'],'reject')
            self.assertEqual(result['artifacts']['settlement']['payment']['status'],'paid')
        self.assertEqual(len(hashes),1)
        resumed,models,ports=self.invoke(Factory())
        self.assertEqual(resumed,index);self.assertEqual((models,ports),(0,0))
        result=json.loads(paths[0].read_text());result['controlled_variant_hash']='tampered'
        paths[0].write_text(json.dumps(result))
        with self.assertRaisesRegex(ValueError,'controlled_variant_hash'):self.invoke(Factory())

    def test_inapplicable_product_keeps_all_five_cells_and_does_not_change_patients(self):
        self.args.matrix_task='prescription';self.args.engineering_only=True
        self.args.close_prescription_loop=True;self.args.controlled_prescription_variant='exact-product-route'
        index,models,ports=self.invoke()
        self.assertEqual((models,ports,len(index['attempts'])),(1,1,6))
        rows=[r for k,r in index['attempts'].items() if k!='linked_baseline_anchor']
        self.assertEqual(len(rows),5)
        self.assertTrue(all(r['status']=='not_run_variant_not_applicable' and r['patient_excluded'] is False for r in rows))
        self.assertTrue(all(not r['model_requests_sent'] and not r['native_effects_started'] for r in rows))

    def test_mid_encounter_provider_outage_is_incomplete_and_not_a_clinical_rejection(self):
        class OfflineFactory:
            def session(self,*args,**kwargs):
                class Offline:
                    def respond(self,observation):
                        raise ProviderUnavailable('resource frozen',request_id='failed-request')
                return Offline()
        self.args.mode='linked'
        index,models,ports=self.invoke(OfflineFactory())
        row=index['attempts']['linked_baseline_anchor']
        self.assertEqual(row['status'],'stopped_provider')
        self.assertFalse(row['native_complete'])
        run=json.loads(Path(row['result_path']).read_text())
        self.assertNotIn('clinical_disposition',run['artifacts'])
        self.assertNotIn('settlement',run['artifacts'])

    def test_optional_capture_wraps_the_actual_protocol_backend(self):
        self.args.mode='linked'; self.args.active_native_capture=object()
        with patch('sandbox.experiments.linked.native_observation.ObservedProtocolPort',
                   side_effect=lambda backend,capture: backend) as wrapper:
            index,models,ports=self.invoke()
        wrapper.assert_called_once()
        self.assertIs(wrapper.call_args.args[1],self.args.active_native_capture)
        self.assertEqual(index['attempts']['linked_baseline_anchor']['status'],'completed')
        self.assertEqual((models,ports),(1,1))

    def test_resume_rejects_tampered_frozen_input_without_model_or_native_calls(self):
        index, _, _ = self.invoke()
        row = next(v for k, v in index['attempts'].items() if k.startswith('documentation'))
        path = Path(row['result_path'])
        result = json.loads(path.read_text())
        result['stage_inputs']['documentation']['consultation']['assessment'] = 'CHANGED'
        path.write_text(json.dumps(result))
        with patch.object(run_batch, 'BudgetedCamelFactory') as factory, patch.object(run_batch, 'NativeOpenHISPort') as port:
            with self.assertRaisesRegex(ValueError, 'frozen input'):
                self.invoke()
            factory.assert_not_called()
            port.assert_not_called()

    def test_saved_identity_mismatch_is_not_accepted(self):
        self.args.mode = 'linked'
        index, _, _ = self.invoke()
        path = Path(index['attempts']['linked_baseline_anchor']['result_path'])
        result = json.loads(path.read_text())
        result['run_id'] = 'another-run'
        path.write_text(json.dumps(result))
        with self.assertRaisesRegex(ValueError, 'run_id'):
            self.invoke()

    def test_budget_stop_preserves_all_planned_arms_without_new_registration(self):
        stop = Event()
        stop.set()
        index, models, ports = self.invoke(stop=stop)
        self.assertEqual((models, ports), (0, 0))
        self.assertEqual(len(index['attempts']), 16)
        self.assertEqual({r['status'] for r in index['attempts'].values()}, {'not_run_budget_limit'})

    def test_mid_anchor_budget_exhaustion_stops_remaining_arms(self):
        class ExhaustedFactory:
            def session(self, *args):
                class Session:
                    def respond(self, observation):
                        raise BudgetExceeded('synthetic exhaustion')
                return Session()
        stop = Event()
        index, models, ports = self.invoke(ExhaustedFactory(), stop=stop)
        self.assertEqual((models, ports), (1, 1))
        self.assertTrue(stop.is_set())
        self.assertEqual(index['attempts']['linked_baseline_anchor']['status'], 'stopped_budget')
        self.assertEqual(sum(v['status'] == 'not_run_budget_limit' for v in index['attempts'].values()), 15)

    def test_interrupted_native_attempt_is_not_retried(self):
        self.args.mode = 'linked'
        (self.root/self.case['case_id']/'linked_baseline_anchor').mkdir(parents=True)
        index, models, ports = self.invoke()
        self.assertEqual((models, ports), (0, 0))
        self.assertEqual(index['attempts']['linked_baseline_anchor']['status'], 'interrupted_requires_audit')

    def test_main_pins_ledger_identity_before_resume(self):
        case = build_synthetic_case('type2_diabetes', 1001, split='engineering')
        case_path = self.root/'case.json'
        case_path.write_text(json.dumps(case))
        row = {k: case[k] for k in ('case_id', 'patient_key', 'data_kind', 'group', 'split')}
        manifest = self.root/'manifest.json'
        manifest.write_text(json.dumps({'cases': [{**row, 'path': str(case_path)}]}))
        config = self.root/'config.json'
        config.write_text(json.dumps({'endpoint': 'https://api.modelarts-maas.com/openai/v1',
            'generation_policy': deepcopy(GENERATION_POLICY_V2), 'role_model': 'deepseek-v4.1-flash-1017', 'tool_models': {'deepseek': 'deepseek-v4.1-flash-1017', 'glm': 'glm-5.3'}}))
        catalogue = self.root/'catalogue.json'
        catalogue.write_text('{}')
        argv = ['run_batch', '--manifest', str(manifest), '--provider-config', str(config),
                '--budget-ledger', str(self.ledger.path), '--model-evidence', str(self.root/'evidence'),
                '--output', str(self.root/'batch'), '--batch', 'pinned', '--catalog', str(catalogue)]
        with patch.object(run_batch, 'execute_case', return_value={'attempts': {}}), patch('sys.argv', argv), redirect_stdout(io.StringIO()):
            run_batch.main()
        spec = json.loads((self.root/'batch'/'pinned'/'specification.json').read_text())
        self.assertEqual(spec['budget_ledger'], self.ledger.identity())
        self.assertEqual(spec['memory_policy'], 'explicit_visible_state_per_turn')
        self.assertIn('glm-5.3', spec['pricing_cny_per_million_tokens'])
        argv[argv.index('--budget-ledger')+1] = str(self.root/'replacement.sqlite')
        with patch.object(run_batch, 'execute_case') as execute, patch('sys.argv', argv):
            with self.assertRaisesRegex(ValueError, 'changed'):
                run_batch.main()
            execute.assert_not_called()

    def test_preflight_and_worker_queue_retain_only_bounded_case_objects(self):
        rows = []
        for index in range(24):
            case = build_synthetic_case('type2_diabetes', 1200+index, split='engineering')
            case['private_large_source_metadata'] = 'x'*250000
            path = self.root/f'large-{index}.json'
            path.write_text(json.dumps(case))
            rows.append({**{key: case[key] for key in ('case_id', 'patient_key', 'data_kind', 'group', 'split')},
                         'path': str(path)})
        del case
        counts = {'live': 0, 'peak': 0}
        lock = Lock()
        class TrackedCase(dict):
            pass
        def release():
            with lock:
                counts['live'] -= 1
        def tracked_load(path):
            value = TrackedCase(json.loads(Path(path).read_text()))
            with lock:
                counts['live'] += 1
                counts['peak'] = max(counts['peak'], counts['live'])
            weakref.finalize(value, release)
            return value
        with patch.object(run_batch, 'load_case', side_effect=tracked_load):
            references = run_batch.select_cases({'cases': rows})
            gc.collect()
            self.assertEqual(counts, {'live': 0, 'peak': 1})
            self.assertTrue(all('his_initial' not in ref and 'private_large_source_metadata' not in ref for ref in references))
            self.assertLess(len(json.dumps(references)), 20000)
            counts['peak'] = 0
            # A Mock records all call arguments and would itself retain every
            # case; use a plain function to measure the worker's real lifetime.
            with patch.object(run_batch, 'execute_case', new=lambda case, **kw: case['case_id']):
                with ThreadPoolExecutor(max_workers=3) as pool:
                    results = list(pool.map(run_batch.execute_case_reference, references))
            gc.collect()
            self.assertEqual(len(results), 24)
            self.assertEqual(counts['live'], 0)
            self.assertLessEqual(counts['peak'], 3)

    def test_reference_detects_file_and_frozen_hash_tampering_before_execution(self):
        case = build_synthetic_case('type2_diabetes', 1400, split='engineering')
        path = self.root/'frozen-case.json'
        path.write_text(json.dumps(case))
        row = {**{key: case[key] for key in ('case_id', 'patient_key', 'data_kind', 'group', 'split')}, 'path': str(path)}
        reference = run_batch.select_cases({'cases': [row]})[0]
        for field in ('case_hash', 'runtime_input_hash'):
            changed = {**reference, field: '0'*64}
            with patch.object(run_batch, 'execute_case') as execute:
                with self.assertRaisesRegex(ValueError, 'content changed'):
                    run_batch.execute_case_reference(changed)
                execute.assert_not_called()
        case['patient_initial']['demographics']['age'] += 1
        path.write_text(json.dumps(case))
        with patch.object(run_batch, 'execute_case') as execute:
            with self.assertRaisesRegex(ValueError, 'file changed'):
                run_batch.execute_case_reference(reference)
            execute.assert_not_called()

    def prepare_source_anchor(self):
        source = self.root/'source_v7'
        self.args.mode, self.args.batch = 'linked', 'source_v7'
        self.config = {'role_model': 'fixed-role', 'tool_models': {'deepseek': 'fixed-role', 'glm': 'fixed-tool'},
                       'endpoint': 'https://provider.example/openai/v1'}
        index, _, _ = self.invoke(batch_root=source)
        # Construct the source-metadata fixture required by the reuse gate. This
        # temporary test record does not claim that FakePort is a native backend.
        record = index['attempts']['linked_baseline_anchor']
        run_path = Path(record['result_path'])
        result = json.loads(run_path.read_text())
        result.update(native_complete=True, unit_test_metadata_fixture=True)
        for call in result['calls']:
            call['model_name'] = self.config['role_model'] if call.get('model_family') is None else self.config['tool_models'][call['model_family']]
        run_path.write_text(json.dumps(result))
        record['native_complete'] = True
        (source/self.case['case_id']/'attempts.json').write_text(json.dumps(index))
        reference = {'case_id': self.case['case_id'], 'runtime_input_hash': digest(run_batch.runtime_case(self.case))}
        spec = {'batch': 'source_v7', 'models': self.config, 'cases': [reference],
                'generation_policy': {'role': {'temperature': .2}}}
        (source/'specification.json').write_text(json.dumps(spec))
        (source/'batch_summary.json').write_text('{}')
        (source/'batch.lock').touch()
        self.args.mode, self.args.batch = 'matrix', 'target_v8'
        self.args.engineering_only = True
        self.args.anchor_batch = source
        self.args.anchor_reuse = run_batch.prepare_anchor_reuse(source, [reference], self.config,
            mode='matrix', engineering_only=True)
        return source, reference, index

    def prepare_disposition_source(self):
        class NoPrescription(FakeFactory):
            def session(self, scope, role, model_family=None):
                session = super().session(scope, role, model_family)
                original = session.respond
                def respond(obs):
                    if role == 'doctor' and obs['action'] == 'consult':
                        return {'action': 'consultation_complete', 'assessment': 'Uncertain fictional condition',
                                'plan': 'Record decision', 'prescription': {'items': []},
                                'no_prescription_reason': 'No supported indication'}
                    return original(obs)
                session.respond = respond
                return session
        source = self.root/'disposition_source'
        self.args.mode, self.args.batch = 'linked', 'disposition_source'
        self.config = {'role_model': 'fixed-role', 'tool_models': {'deepseek': 'fixed-role', 'glm': 'fixed-tool'},
                       'endpoint': 'https://provider.example/openai/v1'}
        index, _, _ = self.invoke(NoPrescription(), batch_root=source)
        path = Path(index['attempts']['linked_baseline_anchor']['result_path'])
        result = json.loads(path.read_text())
        self.assertEqual(result['status'], 'incomplete_clinical_decision')
        for call in result['calls']:
            call['model_name'] = self.config['role_model']
        path.write_text(json.dumps(result))
        reference = {'case_id': self.case['case_id'], 'runtime_input_hash': digest(run_batch.runtime_case(self.case))}
        (source/'specification.json').write_text(json.dumps({'batch': 'disposition_source',
            'models': self.config, 'cases': [reference]}))
        (source/'batch_summary.json').write_text('{}')
        (source/'batch.lock').touch()
        self.args.mode, self.args.batch = 'matrix', 'disposition_tasks'
        self.args.engineering_only, self.args.anchor_batch = True, source
        return source, reference, path

    def test_disposition_reuse_preserves_all_fifteen_cells_without_inventing_prescriptions(self):
        source, reference, path = self.prepare_disposition_source()
        before = path.read_bytes()
        with self.assertRaises(ValueError):
            run_batch.prepare_anchor_reuse(source, [reference], self.config, mode='matrix', engineering_only=True)
        self.args.anchor_reuse = run_batch.prepare_anchor_reuse(source, [reference], self.config,
            mode='matrix', engineering_only=True, allow_clinical_dispositions=True)
        index, models, ports = self.invoke(batch_root=self.root/'disposition_tasks')
        self.assertEqual((len(index['attempts']), models, ports), (15, 10, 10))
        missing = [r for r in index['attempts'].values() if r['status'] == 'not_run_missing_frozen_input']
        self.assertEqual(len(missing), 5)
        self.assertTrue(all(r['config_id'].startswith('prescription:') for r in missing))
        self.assertNotIn('linked_baseline_anchor', index['attempts'])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(index['anchor_source']['source_status'], 'incomplete_clinical_decision')

    def test_disposition_reuse_rejects_source_subset_and_fabricated_stage_input(self):
        source, reference, path = self.prepare_disposition_source()
        with self.assertRaisesRegex(ValueError, 'every planned'):
            run_batch.prepare_anchor_reuse(source, [], self.config, mode='matrix', engineering_only=True,
                                          allow_clinical_dispositions=True)
        result = json.loads(path.read_text())
        result['stage_inputs']['prescription'] = {'prescription': {'items': [{'drug_code': 'FABRICATED'}]},
                                                'clinical_context': {}}
        path.write_text(json.dumps(result))
        with self.assertRaisesRegex(ValueError, 'actual original'):
            run_batch.prepare_anchor_reuse(source, [reference], self.config, mode='matrix', engineering_only=True,
                                          allow_clinical_dispositions=True)

    def test_reused_anchor_runs_only_fifteen_new_tasks_and_resume_is_read_only(self):
        source, _, source_index = self.prepare_source_anchor()
        source_path = Path(source_index['attempts']['linked_baseline_anchor']['result_path'])
        source_before = source_path.read_bytes()
        target = self.root/'target_v8'
        index, models, ports = self.invoke(batch_root=target)
        self.assertEqual((models, ports, len(index['attempts'])), (15, 15, 15))
        self.assertNotIn('linked_baseline_anchor', index['attempts'])
        self.assertEqual(index['anchor_source']['source_generation_version'], 'source_v7')
        anchor = json.loads(source_before)
        for label, row in index['attempts'].items():
            result = json.loads(Path(row['result_path']).read_text())
            self.assertTrue(result['run_id'].startswith('target_v8:'))
            self.assertEqual(result['evaluation_unit'], 'fixed_input_task')
            if label.startswith(('prescription', 'documentation')):
                task = next(iter(result['conditions']))
                self.assertEqual(result['stage_input_hash'], digest(anchor['stage_inputs'][task]))
            self.assertEqual(result['status'], 'completed')
        self.assertEqual(source_path.read_bytes(), source_before)
        resumed, models, ports = self.invoke(batch_root=target)
        self.assertEqual(resumed, index)
        self.assertEqual((models, ports), (0, 0))

    def test_reuse_requires_engineering_matrix_same_models_and_same_case_material(self):
        source, reference, _ = self.prepare_source_anchor()
        for mode, acknowledged in [('linked', True), ('matrix', False)]:
            with self.assertRaisesRegex(ValueError, 'requires'):
                run_batch.prepare_anchor_reuse(source, [reference], self.config,
                    mode=mode, engineering_only=acknowledged)
        with self.assertRaisesRegex(ValueError, 'model configuration differs'):
            run_batch.prepare_anchor_reuse(source, [reference], {**self.config, 'role_model': 'changed'},
                mode='matrix', engineering_only=True)
        with self.assertRaisesRegex(ValueError, 'runtime case hash differs'):
            run_batch.prepare_anchor_reuse(source, [{**reference, 'runtime_input_hash': '0'*64}], self.config,
                mode='matrix', engineering_only=True)

    def test_reuse_rejects_failed_source_anchor(self):
        source, reference, _ = self.prepare_source_anchor()
        index_path = source/self.case['case_id']/'attempts.json'
        index = json.loads(index_path.read_text())
        index['attempts']['linked_baseline_anchor']['native_complete'] = False
        index_path.write_text(json.dumps(index))
        with self.assertRaisesRegex(ValueError, 'not indexed complete'):
            run_batch.prepare_anchor_reuse(source, [reference], self.config, mode='matrix', engineering_only=True)

    def test_reuse_rechecks_source_bytes_spec_and_frozen_stage_hashes(self):
        source, _, _ = self.prepare_source_anchor()
        entry = self.args.anchor_reuse['cases'][0]
        path = Path(entry['source_run_path'])
        original = path.read_bytes()
        path.write_bytes(original + b'\n')
        with self.assertRaisesRegex(ValueError, 'source file changed'):
            self.invoke(batch_root=self.root/'not_started')
        path.write_bytes(original)
        self.args.anchor_reuse['cases'][0]['stage_input_hashes']['prescription'] = '0'*64
        with self.assertRaisesRegex(ValueError, 'stage contents changed'):
            self.invoke(batch_root=self.root/'not_started')
        self.args.anchor_reuse['cases'][0]['stage_input_hashes']['prescription'] = digest(json.loads(original)['stage_inputs']['prescription'])
        with (source/'specification.json').open('a') as f:
            f.write('\n')
        with self.assertRaisesRegex(ValueError, 'source file changed'):
            self.invoke(batch_root=self.root/'not_started')
        self.assertFalse((self.root/'not_started'/self.case['case_id']).exists())

    def test_reuse_budget_stop_preserves_fifteen_conditions_not_source_anchor(self):
        self.prepare_source_anchor()
        stop = Event(); stop.set()
        index, models, ports = self.invoke(batch_root=self.root/'target_v8', stop=stop)
        self.assertEqual((models, ports, len(index['attempts'])), (0, 0, 15))
        self.assertEqual({x['status'] for x in index['attempts'].values()}, {'not_run_budget_limit'})
        self.assertEqual(len(run_batch.planned_attempts('matrix', reuse_anchor=True)), 15)

    def test_reuse_rejects_actual_model_route_missing_or_different_from_spec(self):
        source, reference, index = self.prepare_source_anchor()
        path = Path(index['attempts']['linked_baseline_anchor']['result_path'])
        run = json.loads(path.read_text())
        run['calls'][0]['model_name'] = 'different-actual-backbone'
        path.write_text(json.dumps(run))
        with self.assertRaisesRegex(ValueError, 'actual model evidence'):
            run_batch.prepare_anchor_reuse(source, [reference], self.config, mode='matrix', engineering_only=True)

    def test_baseline_anchor_rejects_tool_calls_even_when_tool_model_matches_spec(self):
        source, reference, index = self.prepare_source_anchor()
        path = Path(index['attempts']['linked_baseline_anchor']['result_path'])
        run = json.loads(path.read_text())
        run['calls'][0].update(role='preconsult_ai', model_family='glm', model_name=self.config['tool_models']['glm'])
        path.write_text(json.dumps(run))
        with self.assertRaisesRegex(ValueError, 'clinical roles with no tool family'):
            run_batch.prepare_anchor_reuse(source, [reference], self.config, mode='matrix', engineering_only=True)

    def test_reuse_rejects_mixed_stage_artifacts_despite_self_consistent_file_hash(self):
        source, reference, index = self.prepare_source_anchor()
        path = Path(index['attempts']['linked_baseline_anchor']['result_path'])
        run = json.loads(path.read_text())
        run['stage_inputs']['documentation']['handoff'] = {'text': 'Wrong saved source'}
        path.write_text(json.dumps(run))
        with self.assertRaisesRegex(ValueError, 'stage/artifact consistency'):
            run_batch.prepare_anchor_reuse(source, [reference], self.config, mode='matrix', engineering_only=True)

    def test_reuse_validates_stage_time_history_and_shared_evidence_not_final_history(self):
        from sandbox.experiments.linked.safe_context_adapter import prepare_shared_rule_context
        source, reference, index = self.prepare_source_anchor()
        path = Path(index['attempts']['linked_baseline_anchor']['result_path'])
        run = json.loads(path.read_text())
        rx = run['stage_inputs']['prescription']
        consult = next(c for c in run['calls'] if c.get('response') == run['artifacts']['consultation'])
        prepared = prepare_shared_rule_context(rx['clinical_context'], consult['observation']['formulary'], [])
        rx['clinical_context'] = prepared['shared_context']
        evidence = prepared['evidence_index']
        run['artifacts']['rule_evidence_indexes'] = {evidence['index_hash']: evidence}
        run['events'].append({'event':'rule_context_prepared', 'evidence_index_hash':evidence['index_hash'],
            'structured_context_hash':prepared['audit']['structured_context_hash']})
        # Later runtime feedback is not retroactively added to earlier inputs.
        run['artifacts']['visible_history']['last_query_feedback'] = {'status':'later_feedback'}
        path.write_text(json.dumps(run))
        reuse = run_batch.prepare_anchor_reuse(source, [reference], self.config, mode='matrix', engineering_only=True)
        self.assertEqual(reuse['cases'][0]['stage_input_hashes']['prescription'], digest(rx))
        rx['clinical_context']['documented_rule_context']['demographics']['age'] = 99
        path.write_text(json.dumps(run))
        with self.assertRaisesRegex(ValueError, 'shared rule evidence'):
            run_batch.prepare_anchor_reuse(source, [reference], self.config, mode='matrix', engineering_only=True)

    def test_reuse_rejects_history_not_present_in_actual_note_generation(self):
        source, reference, index = self.prepare_source_anchor()
        path = Path(index['attempts']['linked_baseline_anchor']['result_path'])
        run = json.loads(path.read_text())
        run['stage_inputs']['documentation']['hospital_history']['last_query_feedback'] = {'status':'invented_feedback'}
        path.write_text(json.dumps(run))
        with self.assertRaisesRegex(ValueError, 'actual generation input'):
            run_batch.prepare_anchor_reuse(source, [reference], self.config, mode='matrix', engineering_only=True)


if __name__ == '__main__':
    unittest.main()
