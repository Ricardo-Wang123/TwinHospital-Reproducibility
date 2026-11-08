"""Deterministic integration tests; their outputs are not clinical evidence."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sandbox.experiments.linked.clinical import LinkedEncounterRunner
from sandbox.experiments.linked.contracts import TaskCondition, paper_conditions, digest
from sandbox.experiments.linked.chart import HistoryChart
from sandbox.experiments.linked.chart_projection import expand_projected_record, verify_projection
from sandbox.experiments.linked.actors import system_prompt
from sandbox.experiments.linked.response_contracts import contract_for, shape_errors, schema_correction_allowed
from sandbox.experiments.linked.fhir_his import OPERATIONS, fhir_workflow, operation_code
from sandbox.experiments.linked.native_his import NativeBlocked, NativeError, NativeRejected


def example_case():
    return {"case_id": "case-test", "patient_key": "patient-test", "group": "type2_diabetes",
            "data_kind": "synthetic_independent", "split": "engineering",
            "patient_initial": {"demographics": {"age": 50, "sex": "F"},
                "patient_facts": [{"fact_id": "f-private", "topic": "chief_complaint", "value": "咳嗽三天",
                                   "status": "reported", "source_kind": "constructed"}],
                "communication_profile": {"style": "plain"}},
            "his_initial": {"historical_orders": [{"drug_code": "HISTORICAL_ONLY"}]},
            "formulary": [{"drug_code": "NEW", "dose_unit": "mg", "route": "oral",
                            "frequency": "daily", "quantity_unit": "tablet"}],
            "evaluation": {"secret": "DO_NOT_DISCLOSE_EVALUATOR"}}


def new_rx(dose="5"):
    return {"items": [{"drug_code": "NEW", "dose": dose, "dose_unit": "mg", "route": "oral",
                       "frequency": "daily", "quantity": "7", "quantity_unit": "tablet"}]}


class FakeFactory:
    def __init__(self, *, revision=False):
        self.observations = []
        self.revision = revision

    def session(self, case_id, role, model_family=None):
        parent = self
        class Session:
            n = 0
            last_usage = {"prompt_tokens": 0, "completion_tokens": 0}
            def respond(self, obs):
                parent.observations.append((role, model_family, deepcopy(obs)))
                action = obs["action"]
                self.n += 1
                if role == "patient":
                    return {"action": "answer", "text": "咳嗽三天，没有忘记的资料。", "expressed_fact_ids": ["f-private"]}
                if action in {"collect", "review_summary", "verify_handoff"}:
                    if action == "collect" and self.n == 1:
                        return {"action": "ask", "question": "今天哪里不舒服？"}
                    return {"action": "summary", "content": {"text": "患者报告咳嗽三天。", "facts": [], "risk_flags": []}}
                if action == "consult":
                    if self.n == 1:
                        return {"action": "ask", "question": "咳嗽还有其他变化吗？", "question_purpose": "diagnostic"}
                    return {"action": "consultation_complete", "assessment": "NEW_ASSESSMENT",
                            "plan": "新处方供测试", "prescription": new_rx("100" if parent.revision else "5")}
                if action == "review_prescription":
                    return {"action": "review", "decision": "reject" if obs["prescription"]["items"][0]["dose"] == "100" else "approve",
                            "flags": [], "reason": "engineering fake"}
                if action == "revise_prescription":
                    return {"decision": "revise", "prescription": new_rx("5"), "reason": "changed after review"}
                if action == "adjudicate":
                    return {"decision": "reject"}
                if action == "document":
                    return {"action": "draft", "content": {"text": "独立生成病历，并非预问诊摘要。",
                            "source_sequences": [x["sequence"] for x in obs["transcript"]]}}
                if action == "review_note":
                    return {"decision": "accept", "reason": "engineering fake"}
                raise AssertionError(action)
        return Session()


class FakeRules:
    def review(self, prescription, context):
        return {"decision": "reject" if prescription["items"][0]["dose"] == "100" else "pass",
                "flags": [], "rule_version": "engineering-test-only", "engineering_only": True}


class FakePort:
    """A persistence double. Never advertises a native or clinical result."""
    def __init__(self, failed_operation=None):
        self.calls, self.state = [], {}
        self.failed_operation = failed_operation

    def execute(self, operation, payload, context):
        self.calls.append((operation, deepcopy(payload), deepcopy(context)))
        if operation == self.failed_operation:
            return {"status": "executed", "data": {}, "readback_verified": False, "native_effect": False}
        data = {}
        if operation == "registration.create":
            data = {"patient_id": "p:"+context["run_id"], "encounter_id": "e:"+context["run_id"]}
        elif operation in {"queue.enter", "encounter.begin_consultation"}:
            data = {"encounter_id": context["encounter_id"]}
        elif operation == "personnel.registry":
            data = {"signatory_id": "authenticated-doctor-test", "registry_version": "fake-v1"}
        elif operation == "history.import":
            self.state["history"] = deepcopy(payload["history"])
        elif operation == "history.index":
            data = {"history": HistoryChart(self.state["history"]).initial_view()}
        elif operation == "history.query":
            data = {"page": HistoryChart(self.state["history"]).query(**payload)}
        elif operation == "handoff.save":
            self.state["handoff"] = deepcopy(payload["content"])
        elif operation == "handoff.read":
            data = {"content": deepcopy(self.state["handoff"])}
        elif operation == "order.save":
            data = {"order_id": payload["order_id"], "status": "draft"}
        elif operation == "order.attest":
            data = {"status": "signed"}
        elif operation in {"note.save", "note.revise"}:
            data = {"version": payload["expected_version"]+1}
        elif operation == "note.attest":
            data = {"status": "locked"}
        elif operation == "payment.prepare":
            data = {"bill_id": "b:one", "amount": "10.00", "currency": "CNY"}
        elif operation == "payment.commit":
            data = {"status": "paid", "transaction_id": payload["transaction_id"]}
        return {"status": "executed", "data": data, "readback_verified": True,
                "native_effect": False, "protection_source": "engineering_test_double"}


class LinkedClinicalTests(unittest.TestCase):
    def runner(self, factory=None, port=None, **kwargs):
        return LinkedEncounterRunner(factory or FakeFactory(), port or FakePort(), FakeRules(), **kwargs)

    def test_fhir_operations_carry_existing_workflow_and_reject_cross_assignment(self):
        case = example_case()
        backend = FakePort()
        with fhir_workflow(backend, case_id=case['case_id'], run_id='fhir-check') as port:
            metadata = port.session.get(port.base_url + '/metadata', timeout=5).json()
            self.assertEqual(metadata['fhirVersion'], '4.0.1')
            definitions = {x['name'] for x in metadata['rest'][0]['operation']}
            self.assertEqual(definitions, {operation_code(name) for name in OPERATIONS})
            definition = port.session.get(port.base_url + '/OperationDefinition/th-payment-commit', timeout=5).json()
            self.assertEqual(definition['resourceType'], 'OperationDefinition')
            with patch.object(port.session, 'post', wraps=port.session.post) as requests:
                result = self.runner(port=port).run_case(case, run_id='fhir-check')
            self.assertEqual(result['status'], 'completed', result.get('error'))
            self.assertFalse(result['native_complete'])
            self.assertEqual(result['artifacts']['settlement']['payment']['status'], 'paid')
            forwarded = [operation_code(operation) for operation, _, _ in backend.calls if operation in OPERATIONS]
            self.assertEqual([call.args[0].split('$')[1] for call in requests.call_args_list], forwarded)
            self.assertTrue(all(call.kwargs['json']['resourceType'] == 'Parameters' for call in requests.call_args_list))
            count = len(backend.calls)
            with self.assertRaisesRegex(NativeBlocked, 'assigned case'):
                port.execute('registration.create', {'patient_key': 'other', 'demographics': {}},
                             {'case_id': 'other', 'run_id': 'fhir-check', 'actor_id': 'cashier-1', 'role': 'cashier'})
            self.assertEqual(len(backend.calls), count)

    def test_fhir_keeps_native_rejection_distinct_from_an_uncertain_write(self):
        backend = FakePort()
        scope = {'case_id': 'case-test', 'run_id': 'rejection-test', 'actor_id': 'doctor-1',
                 'role': 'doctor', 'patient_id': 'patient-test', 'encounter_id': 'encounter-test'}
        payload = {'note_id': 'note-test', 'expected_version': 1, 'content': {'text': 'revision'}}
        with fhir_workflow(backend, case_id=scope['case_id'], run_id=scope['run_id']) as port:
            with patch.object(backend, 'execute', side_effect=NativeRejected('/document/save', 'Rejected')):
                with self.assertRaises(NativeRejected) as rejected:
                    port.execute('note.revise', payload, scope)
                self.assertEqual(rejected.exception.path, '/document/save')
            with patch.object(backend, 'execute', side_effect=NativeError('Outcome uncertain')):
                with self.assertRaises(NativeError) as unknown:
                    port.execute('note.revise', payload, scope)
                self.assertIs(type(unknown.exception), NativeError)

    def test_exact_paper_matrix_and_model_routing(self):
        conditions = paper_conditions()
        self.assertEqual(len(conditions), 15)
        for task in ("preconsult", "prescription", "documentation"):
            self.assertEqual(len(paper_conditions(task)), 5)
        factory = FakeFactory()
        result = self.runner(factory).run_case(example_case(), run_id="routing")
        self.assertEqual(result["status"], "completed")
        for role, family, _ in factory.observations:
            if role in {"patient", "doctor", "nurse", "pharmacist", "senior_pharmacist"}:
                self.assertIsNone(family)
            elif role in {"preconsult_ai", "rx_ai", "scribe"}:
                self.assertEqual(family, "glm")

    def test_real_linkage_no_answer_key_or_patient_memory_leak(self):
        factory, port = FakeFactory(), FakePort()
        result = self.runner(factory, port).run_case(example_case(), run_id="linked")
        self.assertEqual(result["status"], "completed")
        self.assertFalse(result["native_complete"])
        self.assertEqual(result["artifacts"]["original_prescription"]["items"][0]["drug_code"], "NEW")
        self.assertNotEqual(result["artifacts"]["attested_note"]["content"]["text"],
                            result["artifacts"]["final_handoff"]["text"])
        self.assertIn("handoff.read", [x[0] for x in port.calls])
        operations = [x[0] for x in port.calls]
        self.assertLess(operations.index('registration.create'), operations.index('queue.enter'))
        self.assertLess(operations.index('queue.enter'), operations.index('handoff.save'))
        self.assertLess(operations.index('handoff.read'), operations.index('encounter.begin_consultation'))
        queue = next(payload for op, payload, _ in port.calls if op == 'queue.enter')
        self.assertEqual(queue, {'encounter_id': 'e:linked'})
        attest = next((payload, context) for op, payload, context in port.calls if op == 'order.attest')
        self.assertEqual(attest[0]['signatory_id'], 'authenticated-doctor-test')
        self.assertEqual(attest[1]['role'], 'doctor')
        for role, _, obs in factory.observations:
            self.assertNotIn("DO_NOT_DISCLOSE_EVALUATOR", json.dumps(obs))
            if role == "patient":
                self.assertEqual(len(obs["patient_memory"]["patient_facts"]), 1)
                self.assertNotIn("hospital_history", obs)
            else:
                self.assertNotIn("patient_memory", obs)
                self.assertNotIn("patient_facts", obs)
        after_registration = [context for op, _, context in port.calls if op != "registration.create"]
        self.assertEqual({x["patient_id"] for x in after_registration}, {"p:linked"})
        self.assertEqual({x["encounter_id"] for x in after_registration}, {"e:linked"})

    def test_rejected_original_is_preserved_after_revision_and_payment(self):
        factory = FakeFactory(revision=True)
        result = self.runner(factory).run_case(example_case(), run_id="revision")
        self.assertEqual(result["status"], "completed")
        artifacts = result["artifacts"]
        self.assertEqual(artifacts["original_prescription_review"]["decision"], "reject")
        self.assertEqual(artifacts["original_prescription"]["items"][0]["dose"], "100")
        self.assertEqual(artifacts["final_prescription"]["items"][0]["dose"], "5")
        self.assertEqual(len(artifacts["prescription_versions"]), 2)
        self.assertEqual(artifacts["settlement"]["payment"]["status"], "paid")
        observed = artifacts["prescription_business_state"]
        self.assertEqual(observed["status"], "signed")
        self.assertEqual(observed["source_operation"], "order.attest")
        self.assertEqual(observed["items"][0]["dose"], "5")
        self.assertEqual(result["stage_inputs"]["documentation"]["observed_business_state"]["prescription"], observed)
        for role, _, obs in factory.observations:
            if obs["action"] in {"document", "review_note"}:
                self.assertEqual(obs["observed_business_state"]["prescription"], observed)

    def test_physician_retaining_rejected_prescription_is_clinical_incomplete_without_retry(self):
        class RetainFactory(FakeFactory):
            def session(self,case_id,role,model_family=None):
                original=super().session(case_id,role,model_family);parent=self
                class Session:
                    def respond(self,obs):
                        if obs['action']=='revise_prescription':
                            parent.observations.append((role,model_family,deepcopy(obs)))
                            return {'decision':'retain_rejected','reason':'Synthetic unresolved evidence; do not invent it.'}
                        return original.respond(obs)
                return Session()
        factory=RetainFactory(revision=True);port=FakePort()
        result=self.runner(factory,port).run_case(example_case(),run_id='retain-clinical-stop')
        self.assertEqual(result['status'],'incomplete_clinical_decision')
        self.assertEqual(result['error_type'],'ClinicalDecisionIncomplete')
        self.assertFalse(result['workflow_complete'])
        termination=result['artifacts']['prescription_termination']
        self.assertEqual(termination['physician_response']['decision'],'retain_rejected')
        self.assertEqual(termination['clinical_appropriateness'],'not_assessed_outside_programmatic_scope')
        self.assertEqual(len([c for c in result['calls'] if c['phase'].startswith('prescription_revision')]),1)
        self.assertEqual(len(result['artifacts']['prescription_versions']),1)
        operations=[op for op,_,_ in port.calls]
        self.assertNotIn('order.attest',operations);self.assertNotIn('payment.commit',operations)
        self.assertIn('note.save',operations);self.assertIn('note.attest',operations)
        self.assertEqual(result['artifacts']['disposition_documentation_outcome']['status'],'attested')
        self.assertFalse(result['three_task_eligible'])
        source=result['stage_inputs']['documentation']
        self.assertEqual(source['approved_prescription'],{'items':[]})
        self.assertEqual(source['clinical_disposition']['prescription_termination']['physician_response']['decision'],'retain_rejected')

    def test_review_limit_is_unresolved_but_fixed_task_rejection_is_a_completed_review(self):
        class RepeatFactory(FakeFactory):
            def session(self,case_id,role,model_family=None):
                original=super().session(case_id,role,model_family);parent=self
                class Session:
                    def respond(self,obs):
                        if obs['action']=='revise_prescription':
                            parent.observations.append((role,model_family,deepcopy(obs)))
                            return {'decision':'revise','prescription':new_rx('100'),'reason':'Synthetic persistent disagreement'}
                        return original.respond(obs)
                return Session()
        port=FakePort();result=self.runner(RepeatFactory(revision=True),port).run_case(example_case(),run_id='bounded-rejection')
        self.assertEqual(result['status'],'incomplete_clinical_decision')
        self.assertEqual(result['artifacts']['prescription_termination']['completed_revisions'],2)
        self.assertEqual(len(result['artifacts']['prescription_versions']),3)
        self.assertNotIn('payment.commit',[op for op,_,_ in port.calls])
        fixed=self.runner().run_task(example_case(),TaskCondition('prescription','pprs_baseline'),
            run_id='fixed-rejection',stage_input={'prescription':new_rx('100'),'clinical_context':{}})
        self.assertEqual(fixed['status'],'completed')
        self.assertEqual(fixed['artifacts']['original_prescription_review']['decision'],'reject')
        self.assertFalse(fixed['workflow_complete'])

    def test_no_rx_documentation_failure_preserves_clinical_stop_and_never_charges(self):
        class NoRxFactory(FakeFactory):
            def session(self,case_id,role,model_family=None):
                original=super().session(case_id,role,model_family);parent=self
                class Session:
                    def respond(self,obs):
                        if obs['action']=='consult':
                            parent.observations.append((role,model_family,deepcopy(obs)))
                            return {'action':'consultation_complete','assessment':'Undetermined',
                                'plan':'Confirm missing information','prescription':{'items':[]},
                                'no_prescription_reason':'Synthetic uncertainty'}
                        return original.respond(obs)
                return Session()
        port=FakePort('note.save')
        result=self.runner(NoRxFactory(),port).run_case(example_case(),run_id='no-rx-note-failure')
        self.assertEqual(result['status'],'incomplete_clinical_decision')
        self.assertEqual(result['error_type'],'ClinicalDecisionIncomplete')
        self.assertEqual(result['artifacts']['disposition_documentation_outcome']['status'],'failed')
        self.assertIn('documentation',result['stage_inputs'])
        self.assertNotIn('prescription',result['stage_inputs'])
        self.assertFalse(result['three_task_eligible'])
        self.assertNotIn('payment.prepare',[op for op,_,_ in port.calls])
        self.assertNotIn('payment.commit',[op for op,_,_ in port.calls])
        self.assertNotIn('attested_note',result['artifacts'])

    def test_budget_stop_during_decision_note_keeps_original_clinical_disposition(self):
        class BudgetExceeded(Exception): pass
        class BudgetFactory(FakeFactory):
            def session(self, case_id, role, model_family=None):
                original=super().session(case_id,role,model_family)
                class Session:
                    def respond(self,obs):
                        if obs['action']=='consult':
                            return {'action':'consultation_complete','assessment':'Undetermined',
                                'diagnoses':[],'plan':'Obtain missing evidence','prescription':{'items':[]},
                                'no_prescription_reason':'Synthetic uncertainty'}
                        if obs['action']=='document': raise BudgetExceeded('test reservation exhausted')
                        return original.respond(obs)
                return Session()
        port=FakePort(); result=self.runner(BudgetFactory(),port).run_case(example_case(),run_id='decision-note-budget')
        self.assertEqual(result['status'],'stopped_budget')
        self.assertEqual(result['artifacts']['clinical_disposition']['status'],'incomplete_clinical_decision')
        self.assertEqual(result['artifacts']['disposition_documentation_outcome']['status'],'stopped_budget')
        self.assertFalse(result['three_task_eligible'])
        self.assertNotIn('payment.prepare',[op for op,_,_ in port.calls])

    def test_failed_readback_blocks_downstream_and_retains_failure(self):
        port = FakePort("handoff.read")
        result = self.runner(port=port).run_case(example_case(), run_id="failure")
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["workflow_complete"])
        self.assertNotIn("payment.commit", [x[0] for x in port.calls])
        self.assertIn("read-back", result["failure_reason"])

    def test_interrupted_role_call_retains_operator_stop_and_run_evidence(self):
        class InterruptedFactory:
            def session(self, scope, role, model_family):
                return self

            def respond(self, observation):
                raise KeyboardInterrupt

        with tempfile.TemporaryDirectory() as output:
            port = FakePort()
            runner = self.runner(InterruptedFactory(), port, output_dir=output)
            with self.assertRaises(KeyboardInterrupt):
                runner.run_case(example_case(), run_id="interrupted-call")
            result = json.loads(next(Path(output).rglob("run.json")).read_text())
        self.assertEqual(result["error_type"], "KeyboardInterrupt")
        self.assertEqual(result["calls"][0]["status"], "interrupted")
        self.assertEqual(result["events"][-1]["event"], "run_interrupted")
        self.assertFalse(result["workflow_complete"])
        self.assertNotIn("payment.commit", [x[0] for x in port.calls])

    def test_queue_and_registry_failures_stop_before_authoritative_write(self):
        for bad_op, forbidden in [('queue.enter', 'handoff.save'), ('personnel.registry', 'order.attest')]:
            port = FakePort(bad_op)
            result = self.runner(port=port).run_case(example_case(), run_id='bad-'+bad_op)
            self.assertEqual(result['status'], 'failed')
            self.assertNotIn(forbidden, [op for op, _, _ in port.calls])

    def test_realtime_full_chain_generates_before_consultation_finishes(self):
        factory = FakeFactory()
        result = self.runner(factory).run_case(example_case(), run_id="realtime",
                    documentation=TaskCondition("documentation", "realtime", "glm"))
        self.assertEqual(result["status"], "completed")
        calls = result["calls"]
        interim = next(i for i, c in enumerate(calls) if c["observation"]["action"] == "document")
        consult_final = next(i for i, c in enumerate(calls) if c.get("response", {}).get("action") == "consultation_complete")
        self.assertLess(interim, consult_final)
        self.assertIsNone(calls[interim]["observation"]["consultation"])
        self.assertIsNone(calls[interim]["observation"]["approved_prescription"])

    def test_fixed_documentation_prefixes_do_not_see_future(self):
        factory = FakeFactory()
        transcript = [{"sequence": i, "speaker_role": "patient", "phase": "consultation", "text": f"turn {i}"} for i in range(1, 9)]
        result = self.runner(factory, limits={"realtime_chunk_segments": 3}).run_task(example_case(),
            TaskCondition("documentation", "realtime", "deepseek"), run_id="prefix",
            stage_input={"transcript": transcript, "consultation": {"assessment": "FINAL_ONLY"},
                         "approved_prescription": new_rx()})
        self.assertEqual(result["status"], "completed")
        self.assertFalse(result["workflow_complete"])
        drafts = [obs for role, _, obs in factory.observations if obs["action"] == "document"]
        self.assertEqual([len(x["transcript"]) for x in drafts], [2, 5, 8, 8])
        self.assertIsNone(drafts[0]["consultation"])
        self.assertTrue(all(x["consultation"] is None for x in drafts[:-1]))
        self.assertEqual(drafts[-1]["consultation"]["assessment"], "FINAL_ONLY")

    def test_parallel_review_does_not_share_judgments(self):
        factory = FakeFactory()
        result = self.runner(factory).run_task(example_case(), TaskCondition("prescription", "parallel", "glm"),
             run_id="parallel", stage_input={"prescription": new_rx(), "clinical_context": {"history": []}})
        self.assertEqual(result["status"], "completed")
        reviews = [obs for role, _, obs in factory.observations if role in {"pharmacist", "rx_ai"}]
        self.assertEqual(len(reviews), 2)
        for obs in reviews:
            self.assertNotIn("ai_review", obs)
            self.assertNotIn("rule_review", obs)

    def test_appendix_trace_has_no_dialogue_or_native_ids(self):
        with tempfile.TemporaryDirectory() as root:
            result = self.runner(output_dir=root).run_case(example_case(), run_id="private")
            self.assertEqual(result["status"], "completed")
            trace = next(Path(root).glob("*/appendix_trace.json")).read_text()
            self.assertNotIn("咳嗽三天", trace)
            self.assertNotIn("p:private", trace)
            self.assertNotIn("e:private", trace)
            self.assertIn("artifact_hashes", trace)
            self.assertTrue(next(Path(root).glob("*/run.json")).stat().st_mode & 0o777 == 0o600)

    def test_duplicate_run_is_rejected_before_any_call_and_journals_survive(self):
        with tempfile.TemporaryDirectory() as root:
            factory, port = FakeFactory(), FakePort()
            runner = self.runner(factory, port, output_dir=root)
            runner.run_case(example_case(), run_id="unique")
            counts = (len(factory.observations), len(port.calls))
            with self.assertRaises(FileExistsError):
                runner.run_case(example_case(), run_id="unique")
            self.assertEqual(counts, (len(factory.observations), len(port.calls)))
            events = next(Path(root).glob("*/events.jsonl")).read_text().splitlines()
            calls = next(Path(root).glob("*/calls.jsonl")).read_text().splitlines()
            self.assertGreater(len(events), 0)
            self.assertEqual(len(calls), len(factory.observations))

    def test_handoff_omission_cannot_be_bypassed_by_raw_collector_dialogue(self):
        class OmittedFactFactory(FakeFactory):
            def session(self, scope, role, model_family=None):
                session = super().session(scope, role, model_family)
                original = session.respond
                turns = 0
                def respond(obs):
                    nonlocal turns
                    turns += 1
                    if role == "patient":
                        self.observations.append((role, model_family, deepcopy(obs)))
                        text = "PRECONSULT_ONLY_OMITTED_FACT" if "preconsult_collection" in scope else "REELICITED_AT_DOCTOR"
                        return {"action": "answer", "text": text, "expressed_fact_ids": []}
                    if obs["action"] == "verify_handoff" and turns == 1:
                        self.observations.append((role, model_family, deepcopy(obs)))
                        return {"action": "ask", "question": "请补充遗漏的症状信息。",
                                "question_purpose": "handoff_supplementation"}
                    return original(obs)
                session.respond = respond
                return session
        factory, port = OmittedFactFactory(), FakePort()
        result = self.runner(factory, port).run_case(example_case(), run_id="visibility")
        self.assertEqual(result["status"], "completed")
        self.assertIn("PRECONSULT_ONLY_OMITTED_FACT", json.dumps(result["artifacts"]["transcript"]))
        first_verification = next(obs for role, _, obs in factory.observations
                                  if role == "doctor" and obs["action"] == "verify_handoff")
        self.assertEqual(first_verification["conversation"], [])
        self.assertNotIn("REELICITED_AT_DOCTOR", json.dumps(first_verification))
        for role, _, obs in factory.observations:
            if role in {"doctor", "rx_ai", "pharmacist", "scribe", "senior_pharmacist"}:
                self.assertNotIn("PRECONSULT_ONLY_OMITTED_FACT", json.dumps(obs))
        self.assertTrue(any(role == "nurse" and "PRECONSULT_ONLY_OMITTED_FACT" in json.dumps(obs)
                            for role, _, obs in factory.observations))
        self.assertIn("REELICITED_AT_DOCTOR", json.dumps(result["stage_inputs"]["documentation"]))
        self.assertNotIn("PRECONSULT_ONLY_OMITTED_FACT", json.dumps(result["stage_inputs"]))
        self.assertTrue(any(a["activity_type"] == "handoff_supplementation" for a in result["activities"]))
        saved = next(payload for op, payload, _ in port.calls if op == "consultation.save")
        self.assertNotIn("PRECONSULT_ONLY_OMITTED_FACT", json.dumps(saved))

    def test_fixed_documentation_rejects_collector_transcript(self):
        result = self.runner().run_task(example_case(), TaskCondition("documentation", "postvisit", "glm"),
            run_id="bad-source", stage_input={"transcript": [{"sequence": 1, "text": "unshared",
                "speaker_role": "patient", "phase": "preconsult_collection"}], "consultation": {"assessment": "test"}})
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["call_count"], 0)
        self.assertIn("visibility", result["failure_reason"])

    def test_history_is_queried_without_spending_patient_turns_or_exposing_unread_records(self):
        class QueryFactory(FakeFactory):
            def session(self, scope, role, model_family=None):
                session = super().session(scope, role, model_family)
                original = session.respond
                turns = 0
                def respond(obs):
                    nonlocal turns
                    turns += 1
                    if role == 'doctor' and obs['action'] == 'verify_handoff' and turns <= 2:
                        self.observations.append((role, model_family, deepcopy(obs)))
                        if turns == 1:
                            return {'action': 'read_history', 'section': 'historical_orders', 'term': 'LOOKUP_VISIBLE', 'limit': 1}
                        return {'action': 'ask', 'question': '您现在还用这种药吗？', 'question_purpose': 'handoff_supplementation'}
                    return original(obs)
                session.respond = respond
                return session
        case = example_case()
        case['his_initial']['historical_orders'] = [
            {'drug_name': 'LOOKUP_VISIBLE', 'dose': '5', 'source_kind': 'constructed'},
            {'drug_name': 'NEVER_QUERIED_SECRET', 'dose': '10', 'source_kind': 'constructed'}]
        factory, port = QueryFactory(), FakePort()
        result = self.runner(factory, port).run_case(case, run_id='history-query')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['history_query_counts'], {'doctor_handoff_verification': 1})
        observations = [o for role, _, o in factory.observations if role == 'doctor' and o['action'] == 'verify_handoff']
        self.assertEqual([o['remaining_interactions'] for o in observations], [4, 4, 3])
        self.assertEqual([o['remaining_history_queries'] for o in observations], [8, 7, 7])
        self.assertNotIn('LOOKUP_VISIBLE', json.dumps(observations[0]['hospital_history']))
        self.assertIn('LOOKUP_VISIBLE', json.dumps(observations[1]['hospital_history']))
        for role, _, obs in factory.observations:
            self.assertNotIn('NEVER_QUERIED_SECRET', json.dumps(obs))
            if role == 'patient':
                self.assertNotIn('LOOKUP_VISIBLE', json.dumps(obs))
        self.assertNotIn('history.read', [operation for operation, _, _ in port.calls])
        self.assertIn('LOOKUP_VISIBLE', json.dumps(result['stage_inputs']['prescription']))
        self.assertIn('LOOKUP_VISIBLE', json.dumps(result['stage_inputs']['documentation']))
        self.assertTrue(any(a['activity_type'] == 'history_lookup' for a in result['activities']))
        event = next(e for e in result['events'] if e['event'] == 'history_lookup')
        self.assertEqual(len(event['returned_refs']), 1)
        self.assertGreaterEqual(event['wall_seconds'], 0)

    def test_invalid_history_request_returns_feedback_without_read_or_clamping(self):
        class InvalidQueryFactory(FakeFactory):
            def session(self,scope,role,model_family=None):
                session=super().session(scope,role,model_family);original=session.respond
                queries=[21,1]
                def respond(obs):
                    if role=='doctor' and obs['action']=='verify_handoff' and queries:
                        self.observations.append((role,model_family,deepcopy(obs)))
                        return {'action':'read_history','section':'historical_orders','limit':queries.pop(0)}
                    return original(obs)
                session.respond=respond
                return session
        factory,port=InvalidQueryFactory(),FakePort()
        result=self.runner(factory,port).run_case(example_case(),run_id='invalid-query-corrected')
        self.assertEqual(result['status'],'completed')
        lookups=[e for e in result['events'] if e['event']=='history_lookup']
        self.assertEqual(lookups[0]['result_status'],'invalid_request')
        self.assertFalse(lookups[0]['native_read_attempted'])
        self.assertEqual(result['history_query_counts']['doctor_handoff_verification'],2)
        native=[p for op,p,_ in port.calls if op=='history.query' and p['section']=='historical_orders']
        self.assertEqual([p['limit'] for p in native],[1])
        observations=[o for r,_,o in factory.observations if r=='doctor' and o['action']=='verify_handoff']
        feedback=observations[1]['hospital_history']['last_query_feedback']
        self.assertEqual(feedback['status'],'invalid_request')
        self.assertEqual(feedback['request']['limit'],21)
        self.assertEqual(feedback['remaining_queries_this_phase'],7)

    def test_invalid_history_requests_cannot_bypass_existing_query_limit(self):
        class InvalidForever(FakeFactory):
            def session(self,scope,role,model_family=None):
                session=super().session(scope,role,model_family);original=session.respond
                def respond(obs):
                    if role=='doctor' and obs['action']=='verify_handoff':
                        return {'action':'read_history','section':'invented_section','limit':21}
                    return original(obs)
                session.respond=respond
                return session
        port=FakePort();result=self.runner(InvalidForever(),port).run_case(example_case(),run_id='invalid-query-limit')
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['history_query_counts']['doctor_handoff_verification'],8)
        self.assertFalse(any(op=='history.query' for op,_,_ in port.calls))
        self.assertEqual(len([e for e in result['events'] if e.get('result_status')=='invalid_request']),8)

    def test_repeated_history_reads_stop_at_eight_without_infinite_loop(self):
        class RepeatedQueryFactory(FakeFactory):
            def session(self, scope, role, model_family=None):
                session = super().session(scope, role, model_family)
                original = session.respond
                def respond(obs):
                    if role == 'doctor' and obs['action'] == 'verify_handoff':
                        self.observations.append((role, model_family, deepcopy(obs)))
                        return {'action': 'read_history', 'section': 'historical_orders', 'limit': 1}
                    return original(obs)
                session.respond = respond
                return session
        factory, port = RepeatedQueryFactory(), FakePort()
        result = self.runner(factory, port).run_case(example_case(), run_id='history-limit')
        self.assertEqual(result['status'], 'failed')
        self.assertIn('lookup limit', result['failure_reason'])
        self.assertEqual(sum(op == 'history.query' for op, _, _ in port.calls), 8)
        remaining = [obs['remaining_interactions'] for role, _, obs in factory.observations
                     if role == 'doctor' and obs['action'] == 'verify_handoff']
        self.assertEqual(remaining, [4]*9)

    def test_projected_history_preserves_raw_evidence_and_deduplicates_changed_query_limits(self):
        class QueryFactory(FakeFactory):
            def session(self,scope,role,model_family=None):
                original=super().session(scope,role,model_family);parent=self
                queries=[{'limit':2},{'limit':5},{'limit':5,'term':'UNMATCHED_TEST_TERM'}]
                class Session:
                    def respond(self,obs):
                        if role=='doctor' and obs['action']=='verify_handoff' and queries:
                            parent.observations.append((role,model_family,deepcopy(obs)))
                            return {'action':'read_history','section':'historical_orders',**queries.pop(0)}
                        return original.respond(obs)
                return Session()
        case=example_case()
        case['his_initial']['time_basis']={'history_offset_origin':'source_anchor_discharge','new_visit_offset_hours':24}
        case['his_initial']['historical_orders']=[{'record_id':'rx_'+str(i)*24,
            'drug_name':'SyntheticProduct'+str(i),'dose':'5','dose_unit':'mg','frequency':'BID',
            'planned_stop_offset_hours':12,
            'status':'historical_order_not_current_use','source_kind':'constructed',
            'source_ref':'src_'+str(i)*24,'time':{'basis':'source_relative_to_anchor_discharge',
                'event_time_known':True,'event_offset_hours':-12}}
            for i in range(2)]
        factory=QueryFactory()
        with tempfile.TemporaryDirectory() as output:
            result=self.runner(factory,output_dir=output).run_case(case,run_id='projected-history')
            self.assertEqual(result['status'],'completed',result.get('failure_reason'))
            pages=result['artifacts']['visible_history']['queried_pages']
            self.assertEqual(len(pages),2) # original records plus the actual empty-query evidence
            self.assertEqual(len(pages[0]['page']['records']),2)
            self.assertEqual(pages[1]['page']['returned_count'],0)
            expanded=expand_projected_record(pages[0]['page'],pages[0]['page']['records'][0])
            self.assertEqual(expanded['source_kind'],'constructed')
            self.assertEqual(expanded['time']['event_offset_hours'],-12)
            self.assertEqual(expanded['planned_stop_offset_hours'],12)
            self.assertEqual(pages[0]['page']['records'][0]['derived_time_display']['event_hours_before_new_visit'],36)
            self.assertNotIn('src_'+'0'*24,json.dumps(pages))
            raw=[json.loads(line) for line in next(Path(output).glob('*/history_query_raw.jsonl')).read_text().splitlines()]
            self.assertEqual(len(raw),3)
            self.assertIn('src_'+'0'*24,json.dumps(raw))
            self.assertTrue(verify_projection(raw[0]['raw_page'],pages[0]['page']))
            lookups=[e for e in result['events'] if e['event']=='history_lookup']
            self.assertEqual(lookups[1]['result_status'],'already_visible')
            self.assertEqual(len(lookups[1]['already_visible_record_refs']),2)
            self.assertEqual(lookups[1]['new_record_refs'],[])
            self.assertEqual(result['history_query_counts'],{'doctor_handoff_verification':3})
            self.assertLessEqual(lookups[-1]['retained_context_bytes'],60000)
            self.assertEqual(result['stage_inputs']['prescription']['clinical_context']['hospital_history'],
                result['stage_inputs']['documentation']['hospital_history'])

    def test_projection_framing_overflow_is_a_bounded_unread_page_not_a_failed_visit(self):
        class QueryFactory(FakeFactory):
            def session(self,scope,role,model_family=None):
                original=super().session(scope,role,model_family)
                requested=False
                class Session:
                    def respond(self,obs):
                        nonlocal requested
                        if role=='doctor' and obs['action']=='verify_handoff' and not requested:
                            requested=True
                            return {'action':'read_history','section':'historical_orders','limit':1}
                        return original.respond(obs)
                return Session()
        class LargePort(FakePort):
            def execute(self,operation,payload,context):
                response=super().execute(operation,payload,context)
                if operation=='history.query' and payload['section']=='historical_orders':
                    response['data']['page']={'section':'historical_orders','records':[
                        {'record_ref':'chart_large_0','record':{'drug_name':'UNREAD_LARGE_'+'x'*29500}}],
                        'returned_count':1,'matching_count':1,'next_offset':None}
                return response
        factory=QueryFactory();result=self.runner(factory,LargePort()).run_case(example_case(),run_id='projection-framing')
        self.assertEqual(result['status'],'completed',result.get('failure_reason'))
        self.assertEqual(result['artifacts']['visible_history']['queried_pages'],[])
        event=next(e for e in result['events'] if e['event']=='history_lookup')
        self.assertEqual(event['result_status'],'requires_narrower_query_for_projected_page_budget')
        self.assertFalse(event['accepted_into_context'])
        self.assertGreater(event['projected_page_bytes'],30000)
        self.assertLess(event['raw_page_bytes'],30000)
        self.assertEqual(result['history_query_counts'],{'doctor_handoff_verification':1})
        for _,_,obs in factory.observations:self.assertNotIn('UNREAD_LARGE_',json.dumps(obs))

    def test_realtime_fixed_replay_does_not_see_later_history_queries(self):
        case = example_case()
        case['his_initial']['historical_orders'] = [{'drug_name': 'FUTURE_QUERY_VALUE'}]
        chart = HistoryChart(case['his_initial'])
        history = {'initial_view': chart.initial_view(), 'queried_pages': [{
            'page': chart.query('historical_orders'), 'phase': 'consultation', 'queried_after_sequence': 6}],
            'last_query_feedback': None, 'visible_history_byte_limit': 60000, 'unshown_is_not_absent': True}
        transcript = [{'sequence': i, 'phase': 'consultation', 'speaker_role': 'patient', 'text': str(i)} for i in range(1, 9)]
        factory = FakeFactory()
        result = self.runner(factory, limits={'realtime_chunk_segments': 3}).run_task(case,
            TaskCondition('documentation', 'realtime', 'glm'), run_id='history-prefix', stage_input={
                'transcript': transcript, 'hospital_history': history, 'consultation': {'assessment': 'test'},
                'approved_prescription': new_rx()})
        self.assertEqual(result['status'], 'completed')
        drafts = [obs for role, _, obs in factory.observations if obs['action'] == 'document']
        self.assertNotIn('FUTURE_QUERY_VALUE', json.dumps(drafts[0]))
        self.assertNotIn('FUTURE_QUERY_VALUE', json.dumps(drafts[1]))
        self.assertIn('FUTURE_QUERY_VALUE', json.dumps(drafts[2]))

    def test_fixed_rx_candidate_reaches_review_without_native_route_coercion(self):
        rx = new_rx()
        rx['items'][0]['route'] = 'UNSUPPORTED_NATIVE_ROUTE'
        port = FakePort('order.save')
        with tempfile.TemporaryDirectory() as root:
            result = self.runner(port=port, output_dir=root).run_task(example_case(),
                TaskCondition('prescription', 'parallel', 'glm'), run_id='candidate-only',
                stage_input={'prescription': rx, 'clinical_context': {}})
            self.assertEqual(result['status'], 'completed')
            self.assertNotIn('order.save', [op for op, _, _ in port.calls])
            self.assertEqual(result['artifacts']['original_prescription']['items'][0]['route'], 'UNSUPPORTED_NATIVE_ROUTE')
            self.assertIn('original_prescription_review', result['artifacts'])
            self.assertFalse(result['artifacts']['prescription_versions'][0]['native_write_attempted'])
            events = next(Path(root).glob('*/events.jsonl')).read_text()
            self.assertIn('UNSUPPORTED_NATIVE_ROUTE', events)
            self.assertIn('private_local_audit_journal', events)

    def test_native_order_exception_is_observed_before_any_clinical_review(self):
        class UnsupportedPort(FakePort):
            def execute(self, operation, payload, context):
                if operation == 'order.save':
                    raise KeyError('unsupported synthetic mapping')
                return super().execute(operation, payload, context)
        result = self.runner(port=UnsupportedPort()).run_case(example_case(), run_id='unsupported-native')
        self.assertEqual(result['status'], 'failed')
        self.assertNotIn('original_prescription_review', result['artifacts'])
        failure = [e for e in result['events'] if e['event'] == 'business_operation'][-1]
        self.assertEqual(failure['operation'], 'order.save')
        self.assertEqual(failure['response']['status'], 'failed_exception')
        self.assertIsNone(failure['response']['native_effect'])

    def test_large_queried_page_is_explicitly_deferred_not_silently_truncated(self):
        class LargeQueryFactory(FakeFactory):
            def session(self, scope, role, model_family=None):
                session = super().session(scope, role, model_family)
                original = session.respond
                reads = 0
                def respond(obs):
                    nonlocal reads
                    if role == 'doctor' and obs['action'] == 'verify_handoff' and reads < 3:
                        self.observations.append((role, model_family, deepcopy(obs)))
                        action = {'action': 'read_history', 'section': 'historical_orders', 'offset': reads, 'limit': 1}
                        reads += 1
                        return action
                    return original(obs)
                session.respond = respond
                return session
        case = example_case()
        case['his_initial']['historical_orders'] = [
            # Two pages fit alongside the declared initial chart overview;
            # the third must be deferred whole, never silently truncated.
            {'drug_name': 'first_' + 'a'*24000}, {'drug_name': 'second_' + 'b'*24000},
            {'drug_name': 'DEFERRED_RECORD_VALUE_' + 'c'*24000}]
        factory = LargeQueryFactory()
        result = self.runner(factory).run_case(case, run_id='large-query')
        self.assertEqual(result['status'], 'completed')
        lookups = [e for e in result['events'] if e['event'] == 'history_lookup']
        self.assertEqual([e['accepted_into_context'] for e in lookups], [True, True, False])
        self.assertIn('requires_narrower_query', lookups[-1]['result_status'])
        self.assertLessEqual(lookups[-1]['retained_context_bytes'], 60000)
        self.assertNotIn('DEFERRED_RECORD_VALUE_', json.dumps(factory.observations))
        self.assertEqual(result['history_query_counts']['doctor_handoff_verification'], 3)

    def test_each_note_revision_is_reviewed_afresh_with_full_feedback_retained_in_audit(self):
        class RevisingFactory(FakeFactory):
            def session(self, scope, role, model_family=None):
                session = super().session(scope, role, model_family)
                original = session.respond
                def respond(obs):
                    if obs['action'] == 'review_note':
                        self.observations.append((role, model_family, deepcopy(obs)))
                        n = obs['version'] - 1
                        if n < 3:
                            return {'decision': 'revise', 'reason': 'source correction '+str(n),
                                    'content': {'text': 'corrected '+str(n), 'source_sequences': []}}
                        return {'decision': 'accept', 'reason': 'all source corrections resolved'}
                    return original(obs)
                session.respond = respond
                return session
        for condition in paper_conditions('documentation'):
            with self.subTest(config=condition.config_id):
                factory, port = RevisingFactory(), FakePort()
                result = self.runner(factory, port, limits={'max_note_revisions': 3}).run_task(example_case(), condition,
                    run_id='note-loop-'+condition.config_id,
                    stage_input={'transcript': [], 'consultation': {'assessment': 'visible assessment'},
                                 'approved_prescription': new_rx()})
                self.assertEqual(result['status'], 'completed')
                reviews = [o for _, _, o in factory.observations if o['action'] == 'review_note']
                self.assertEqual([o['version'] for o in reviews], [1, 2, 3, 4])
                self.assertEqual([o['remaining_note_revisions'] for o in reviews], [3, 2, 1, 0])
                self.assertEqual([len(o['previous_review_index']) for o in reviews], [0, 1, 2, 3])
                self.assertTrue(all('previous_review_history' not in o for o in reviews))
                self.assertTrue(all('source correction' not in json.dumps(o) for o in reviews))
                self.assertEqual(result['artifacts']['note_reviews'][0]['reason'], 'source correction 0')
                self.assertTrue(all(o['current_note_identity']['content_hash'] == digest(o['content']) for o in reviews))
                self.assertTrue(all(o['is_final_prefix'] for o in reviews))
                self.assertEqual(len(result['artifacts']['note_versions']), 4)
                self.assertEqual(result['artifacts']['attested_note']['version'], 4)
                self.assertEqual(sum(a['activity_type']=='note_rework' for a in result['activities']), 3)

    def test_final_unaccepted_note_proposal_is_retained_but_not_saved_or_attested(self):
        class NeverAcceptFactory(FakeFactory):
            def session(self, scope, role, model_family=None):
                session = super().session(scope, role, model_family)
                original = session.respond
                def respond(obs):
                    if obs['action'] == 'review_note':
                        self.observations.append((role, model_family, deepcopy(obs)))
                        return {'decision': 'revise', 'reason': 'still unsupported',
                            'content': {'text': 'proposed after version '+str(obs['version']), 'source_sequences': []}}
                    return original(obs)
                session.respond = respond
                return session
        factory, port = NeverAcceptFactory(), FakePort()
        result = self.runner(factory, port, limits={'max_note_revisions': 3}).run_case(example_case(), run_id='never-accept')
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(len(result['artifacts']['note_versions']), 4)
        self.assertEqual(len(result['artifacts']['note_reviews']), 4)
        last = result['artifacts']['note_reviews'][-1]
        self.assertEqual(last['reviewed_version'], 4)
        self.assertFalse(last['revision_saved'])
        self.assertEqual(last['response']['content']['text'], 'proposed after version 4')
        self.assertNotIn('note.attest', [op for op, _, _ in port.calls])
        self.assertNotIn('payment.commit', [op for op, _, _ in port.calls])

    def test_realtime_review_scope_and_feedback_do_not_include_future_sources(self):
        factory = FakeFactory()
        transcript = [{'sequence': i, 'speaker_role': 'patient', 'phase': 'consultation',
                       'text': 'visible '+str(i)} for i in range(1, 9)]
        result = self.runner(factory, limits={'realtime_chunk_segments': 3}).run_task(example_case(),
            TaskCondition('documentation', 'realtime', 'glm'), run_id='review-prefix',
            stage_input={'transcript': transcript, 'consultation': {'assessment': 'FUTURE_FINAL'},
                         'approved_prescription': new_rx()})
        self.assertEqual(result['status'], 'completed')
        reviews = [o for _, _, o in factory.observations if o['action'] == 'review_note']
        self.assertEqual([o['is_final_prefix'] for o in reviews], [False, False, False, True])
        for obs in reviews[:-1]:
            self.assertIsNone(obs['consultation'])
            self.assertIsNone(obs['approved_prescription'])
            self.assertEqual(obs['allowed_source_scope']['review_standard'], 'interim_visible_facts_only')
            self.assertNotIn('FUTURE_FINAL', json.dumps(obs))
        for obs in reviews:
            for feedback in obs['previous_review_index']:
                self.assertTrue(set(feedback['source_sequences']).issubset(obs['allowed_source_scope']['source_sequences']))
                self.assertNotIn('reason', feedback)
                self.assertNotIn('decision', feedback)
        self.assertEqual(len(reviews[-1]['previous_review_index']), 3)

    def test_no_prescription_retains_consultation_and_is_not_a_claimed_clinical_error(self):
        class NoPrescriptionFactory(FakeFactory):
            def session(self, scope, role, model_family=None):
                session = super().session(scope, role, model_family)
                original = session.respond
                def respond(obs):
                    if obs['action'] == 'consult':
                        self.observations.append((role, model_family, deepcopy(obs)))
                        return {'action': 'consultation_complete', 'assessment': 'insufficient information',
                            'plan': 'obtain missing reconciliation', 'no_prescription_reason': 'defer pending reconciliation',
                            'prescription': {'items': []}}
                    return original(obs)
                session.respond = respond
                return session
        port = FakePort()
        result = self.runner(NoPrescriptionFactory(), port).run_case(example_case(), run_id='no-prescription')
        self.assertEqual(result['status'], 'incomplete_clinical_decision')
        self.assertFalse(result['workflow_complete'])
        self.assertEqual(result['artifacts']['consultation']['prescription']['items'], [])
        self.assertEqual(result['artifacts']['prescription_eligibility']['clinical_appropriateness'], 'not_assessed_outside_programmatic_scope')
        self.assertIn('consultation.save', [op for op, _, _ in port.calls])
        self.assertNotIn('order.save', [op for op, _, _ in port.calls])
        self.assertIn('note.attest', [op for op, _, _ in port.calls])
        self.assertEqual(result['artifacts']['disposition_documentation_outcome']['status'],'attested')
        self.assertFalse(result['three_task_eligible'])
        self.assertNotIn('payment.commit', [op for op, _, _ in port.calls])
        self.assertNotIn('prescription', result['stage_inputs'])

    def test_adjudication_defer_and_invalid_flag_are_never_mapped_to_rejection(self):
        class AdjudicationFactory(FakeFactory):
            disposition = 'defer'
            def session(self, scope, role, model_family=None):
                session = super().session(scope, role, model_family)
                original = session.respond
                def respond(obs):
                    if role == 'rx_ai':
                        return {'decision': 'flag', 'reason': 'needs reconciliation'}
                    if role == 'senior_pharmacist':
                        self.observations.append((role, model_family, deepcopy(obs)))
                        return {'action': 'adjudication', 'decision': self.disposition, 'reason': 'still uncertain'}
                    return original(obs)
                session.respond = respond
                return session
        for decision, status in [('defer', 'incomplete_clinical_decision'), ('flag', 'failed')]:
            with self.subTest(decision=decision):
                factory = AdjudicationFactory()
                factory.disposition = decision
                result = self.runner(factory).run_task(example_case(),
                    TaskCondition('prescription', 'parallel', 'glm'), run_id='senior-'+decision,
                    stage_input={'prescription': new_rx(), 'clinical_context': {}})
                self.assertEqual(result['status'], status)
                unresolved = result['artifacts']['unresolved_prescription_adjudication']
                self.assertEqual(unresolved['response']['decision'], decision)
                self.assertNotIn('final_prescription', result['artifacts'])
                if decision == 'defer':
                    self.assertEqual(result['artifacts']['original_prescription_review']['decision'], 'defer')
                observation = next(o for r, _, o in factory.observations if r == 'senior_pharmacist')
                self.assertEqual(observation['allowed_decisions'], ['approve', 'reject', 'defer'])

    def test_pharmacist_flag_is_unresolved_without_forcing_rejection(self):
        class FlagRules(FakeRules):
            def review(self, prescription, context):
                return {'decision': 'flag', 'flags': ['unverified_scope'], 'rule_version': 'fixture'}
        class FlagFactory(FakeFactory):
            def session(self, scope, role, model_family=None):
                session = super().session(scope, role, model_family)
                original = session.respond
                def respond(obs):
                    if role == 'pharmacist':
                        return {'action': 'review', 'decision': 'flag', 'flags': ['needs_information'], 'reason': 'Unknown, not incorrect'}
                    return original(obs)
                session.respond = respond
                return session
        for workflow, family in [('pprs_baseline', None), ('sequential', 'glm')]:
            with self.subTest(workflow=workflow):
                port = FakePort()
                runner = LinkedEncounterRunner(FlagFactory(), port, FlagRules())
                result = runner.run_task(example_case(), TaskCondition('prescription', workflow, family),
                    run_id='flag-'+workflow, stage_input={'prescription': new_rx(), 'clinical_context': {}})
                self.assertEqual(result['status'], 'incomplete_clinical_decision')
                review = result['artifacts']['original_prescription_review']
                self.assertEqual(review['decision'], 'defer')
                self.assertIsNone(review['senior_review'])
                self.assertEqual(result['artifacts']['unresolved_prescription_adjudication']['response_source'], 'pharmacist_review')
                self.assertNotIn('final_prescription', result['artifacts'])
                self.assertFalse(any(op in {'order.attest', 'payment.commit'} for op, _, _ in port.calls))

    def test_full_flow_can_return_flag_feedback_without_erasing_original_disposition(self):
        class AlwaysFlagRules(FakeRules):
            def review(self,prescription,context):
                return {'decision':'flag','flags':['needs_review'],'rule_version':'fixture'}
        class FollowupFactory(FakeFactory):
            def session(self,scope,role,model_family=None):
                session=super().session(scope,role,model_family);original=session.respond
                def respond(obs):
                    if role=='pharmacist':
                        self.observations.append((role,model_family,deepcopy(obs)))
                        history=obs['clinical_context']['consultation'].get('prescription_review_followups',[])
                        return {'decision':'approve' if history else 'flag','reason':'Fictional response to actual physician followup','flags':[]}
                    return original(obs)
                session.respond=respond;return session
        factory=FollowupFactory();port=FakePort()
        result=LinkedEncounterRunner(factory,port,AlwaysFlagRules()).run_case(example_case(),run_id='flag-followup',
            prescription=TaskCondition('prescription','pprs_baseline'))
        self.assertEqual(result['status'],'completed')
        artifacts=result['artifacts']
        self.assertEqual(artifacts['original_prescription_review']['decision'],'defer')
        self.assertEqual(len(artifacts['prescription_versions']),2)
        self.assertEqual(artifacts['prescription_versions'][1]['review']['decision'],'approve')
        self.assertEqual(artifacts['resolved_prescription_adjudications'][0]['previous_unresolved_record']['response']['decision'],'flag')
        self.assertNotIn('unresolved_prescription_adjudication',artifacts)
        self.assertEqual(len(artifacts['prescription_followups']),1)
        self.assertEqual(len(result['stage_inputs']['documentation']['consultation']['prescription_review_followups']),1)
        self.assertIn('payment.commit',[op for op,_,_ in port.calls])

    def test_prescription_feedback_can_query_actual_history_with_shared_consultation_budget(self):
        class FlagRules(FakeRules):
            def review(self,prescription,context):return {'decision':'flag','flags':[],'rule_version':'fixture'}
        class QueryFactory(FakeFactory):
            read_requested=False
            def session(self,scope,role,model_family=None):
                session=super().session(scope,role,model_family);original=session.respond
                def respond(obs):
                    if role=='pharmacist':
                        seen=bool(obs['clinical_context']['hospital_history'].get('queried_pages'))
                        return {'decision':'approve' if seen else 'flag','flags':[],'reason':'Fixture history requirement'}
                    if role=='doctor' and obs['action']=='revise_prescription' and not self.read_requested:
                        self.read_requested=True
                        return {'action':'read_history','section':'historical_orders','offset':0,'limit':5}
                    return original(obs)
                session.respond=respond;return session
        port=FakePort();factory=QueryFactory()
        result=LinkedEncounterRunner(factory,port,FlagRules()).run_case(example_case(),run_id='feedback-history',
            prescription=TaskCondition('prescription','pprs_baseline'))
        self.assertEqual(result['status'],'completed')
        self.assertEqual(result['history_query_counts']['consultation'],1)
        self.assertEqual(len([1 for op,payload,_ in port.calls if op=='history.query' and payload['section']=='historical_orders']),1)
        self.assertEqual(result['artifacts']['original_prescription_review']['decision'],'defer')
        self.assertEqual(len(result['artifacts']['prescription_versions']),2)
        self.assertTrue(result['stage_inputs']['documentation']['hospital_history']['queried_pages'])
        self.assertIn('payment.commit',[op for op,_,_ in port.calls])

    def test_prescription_clarification_is_real_dialogue_and_has_no_fresh_turn_budget(self):
        class FlagRules(FakeRules):
            def review(self,prescription,context):return {'decision':'flag','flags':[],'rule_version':'fixture'}
        class ClarifyFactory(FakeFactory):
            followup_count=0
            keep_asking=False
            def session(self,scope,role,model_family=None):
                session=super().session(scope,role,model_family);original=session.respond
                def respond(obs):
                    if role=='doctor' and obs['action']=='consult':
                        return {'action':'consultation_complete','assessment':'fixture','plan':'fixture','prescription':new_rx()}
                    if role=='pharmacist':
                        clarified=any(s['speaker_role']=='doctor' and s['text']=='请出示用药装置。'
                                      for s in obs['clinical_context']['conversation'])
                        return {'decision':'approve' if clarified else 'flag','flags':[],'reason':'fixture clarification'}
                    if role=='doctor' and obs['action']=='revise_prescription':
                        self.followup_count+=1
                        if self.keep_asking or self.followup_count==1:
                            return {'action':'ask','question':'请出示用药装置。','question_purpose':'prescription_clarification'}
                    return original(obs)
                session.respond=respond;return session
        for keep_asking in (False,True):
            factory=ClarifyFactory();factory.keep_asking=keep_asking;port=FakePort()
            runner=LinkedEncounterRunner(factory,port,FlagRules(),limits={'consultation_turns':1})
            result=runner.run_case(example_case(),run_id='clarify-'+str(keep_asking),
                prescription=TaskCondition('prescription','pprs_baseline'))
            self.assertEqual(result['status'],'failed' if keep_asking else 'completed')
            events=[e for e in result['events'] if e['event']=='prescription_followup_question']
            self.assertEqual(len(events),1)
            self.assertEqual(events[0]['remaining_interactions'],0)
            patient_calls=[c for c in result['calls'] if c['role']=='patient' and c['observation'].get('question')=='请出示用药装置。']
            self.assertEqual(len(patient_calls),1)
            self.assertNotIn('hospital_history',patient_calls[0]['observation'])
            if keep_asking:self.assertNotIn('payment.commit',[op for op,_,_ in port.calls])

    def test_unmapped_proposal_returns_to_physician_without_native_write_or_silent_substitution(self):
        class UnmappedFactory(FakeFactory):
            def session(self,scope,role,model_family=None):
                session=super().session(scope,role,model_family);original=session.respond
                def respond(obs):
                    response=original(obs)
                    if response.get('action')=='consultation_complete':
                        response['prescription']['items'][0]['drug_code']='NOT_IN_FORMULARY'
                    return response
                session.respond=respond;return session
        port=FakePort();factory=UnmappedFactory()
        result=LinkedEncounterRunner(factory,port,FakeRules()).run_case(example_case(),run_id='unmapped-feedback',
            prescription=TaskCondition('prescription','pprs_baseline'))
        self.assertEqual(result['status'],'completed')
        versions=result['artifacts']['prescription_versions']
        self.assertEqual(len(versions),2)
        self.assertFalse(versions[0]['native_write_attempted'])
        self.assertEqual(versions[0]['prescription']['items'][0]['drug_code'],'NOT_IN_FORMULARY')
        self.assertEqual(versions[0]['review']['decision'],'approve')  # Preserve the candidate review even when a business guard blocks it.
        self.assertTrue(versions[1]['native_write_attempted'])
        writes=[payload for op,payload,_ in port.calls if op=='order.save']
        self.assertEqual(len(writes),1)
        self.assertEqual(writes[0]['items'][0]['drug_code'],'NEW')
        self.assertIsNone(writes[0]['parent_order_id'])
        obs=next(o for role,_,o in factory.observations if role=='doctor' and o['action']=='revise_prescription')
        self.assertEqual(obs['business_validation']['unsupported_codes'],['NOT_IN_FORMULARY'])
        self.assertFalse(obs['business_validation']['automatic_substitution'])

    def test_persistent_flag_stops_at_existing_revision_limit_without_payment(self):
        class FlagRules(FakeRules):
            def review(self,prescription,context):return {'decision':'flag','flags':[],'rule_version':'fixture'}
        class FlagFactory(FakeFactory):
            def session(self,scope,role,model_family=None):
                session=super().session(scope,role,model_family);original=session.respond
                def respond(obs):
                    if role=='pharmacist':return {'decision':'flag','flags':[],'reason':'Still unresolved'}
                    return original(obs)
                session.respond=respond;return session
        port=FakePort();runner=LinkedEncounterRunner(FlagFactory(),port,FlagRules())
        result=runner.run_case(example_case(),run_id='flag-bounded')
        self.assertEqual(result['status'],'incomplete_clinical_decision')
        self.assertEqual(len(result['artifacts']['prescription_versions']),runner.limits['prescription_revisions']+1)
        self.assertEqual(result['artifacts']['original_prescription_review']['decision'],'defer')
        self.assertNotIn('order.attest',[op for op,_,_ in port.calls])
        self.assertNotIn('payment.commit',[op for op,_,_ in port.calls])

    def test_physician_may_explicitly_defer_without_a_format_retry(self):
        class FlagRules(FakeRules):
            def review(self,prescription,context):return {'decision':'flag','flags':[],'rule_version':'fixture'}
        class DeferFactory(FakeFactory):
            def session(self,scope,role,model_family=None):
                session=super().session(scope,role,model_family);original=session.respond
                def respond(obs):
                    if role=='pharmacist':return {'decision':'flag','flags':[],'reason':'Missing source evidence'}
                    if obs['action']=='revise_prescription':
                        self.observations.append((role,model_family,deepcopy(obs)))
                        return {'decision':'defer','reason':'Needed information unavailable; preserve uncertainty'}
                    return original(obs)
                session.respond=respond;return session
        factory=DeferFactory();port=FakePort()
        result=LinkedEncounterRunner(factory,port,FlagRules()).run_case(example_case(),run_id='doctor-defer')
        self.assertEqual(result['status'],'incomplete_clinical_decision')
        self.assertEqual(result['artifacts']['prescription_termination']['status'],'physician_deferred_after_review')
        self.assertEqual(len([o for _,_,o in factory.observations if o['action']=='revise_prescription']),1)
        self.assertNotIn('payment.commit',[op for op,_,_ in port.calls])

    def test_parallel_flag_versus_rejection_requires_adjudication(self):
        class DifferentInterceptions(FakeFactory):
            def session(self, scope, role, model_family=None):
                session = super().session(scope, role, model_family)
                original = session.respond
                def respond(obs):
                    if role in {'rx_ai', 'pharmacist'}:
                        return {'action': 'review', 'decision': 'flag' if role == 'rx_ai' else 'reject',
                                'flags': [], 'reason': 'Different observed dispositions'}
                    if role == 'senior_pharmacist':
                        self.observations.append((role, model_family, deepcopy(obs)))
                        return {'action': 'adjudication', 'decision': 'defer', 'reason': 'Cannot resolve'}
                    return original(obs)
                session.respond = respond
                return session
        factory = DifferentInterceptions()
        result = self.runner(factory).run_task(example_case(), TaskCondition('prescription', 'parallel', 'glm'),
            run_id='distinct-interceptions', stage_input={'prescription': new_rx(), 'clinical_context': {}})
        self.assertEqual(result['status'], 'incomplete_clinical_decision')
        review = result['artifacts']['original_prescription_review']
        self.assertEqual(review['decision'], 'defer')
        self.assertEqual(review['senior_review']['decision'], 'defer')
        self.assertEqual(len([r for r, _, _ in factory.observations if r == 'senior_pharmacist']), 1)

    def test_current_measurements_release_at_consultation_not_handoff_and_preserve_sources(self):
        case = example_case()
        case['his_initial']['current_observations'] = [
            {'name': 'panel_marker', 'value': 76, 'unit': 'bpm', 'source_kind': 'constructed',
             'available_at': 'physician_consultation'},
            {'name': 'FUTURE_TEST_RESULT', 'value': 99, 'unit': 'mg/dL', 'source_kind': 'constructed',
             'available_at': 'after_future_test'},
            {'name': 'UNSPECIFIED_AVAILABILITY', 'value': 10}]
        factory, port = FakeFactory(), FakePort()
        result = self.runner(factory, port).run_case(case, run_id='measurement-panel')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['history_query_counts'], {})
        panel = result['artifacts']['current_encounter_measurements']
        self.assertEqual(len(panel['records']), 1)
        self.assertEqual(panel['records'][0]['record']['value'], 76)
        self.assertEqual(panel['records'][0]['record']['source_kind'], 'constructed')
        self.assertEqual(panel['withheld_or_oversize_returned_records'], 2)
        for role, _, obs in factory.observations:
            if obs['action'] in {'collect', 'verify_handoff', 'review_summary'}:
                self.assertNotIn('panel_marker', json.dumps(obs))
            if obs['action'] in {'consult', 'document', 'review_note'}:
                self.assertEqual(obs['current_encounter_measurements'], panel)
            self.assertNotIn('FUTURE_TEST_RESULT', json.dumps(obs))
            self.assertNotIn('UNSPECIFIED_AVAILABILITY', json.dumps(obs))
        operations = [op for op, _, _ in port.calls]
        self.assertLess(operations.index('encounter.begin_consultation'), operations.index('history.query'))
        self.assertEqual(result['stage_inputs']['documentation']['current_encounter_measurements'], panel)
        changed = deepcopy(case)
        changed['his_initial']['current_observations'][0]['value'] = 999
        branch_factory, branch_port = FakeFactory(), FakePort()
        branch = self.runner(branch_factory, branch_port).run_task(changed,
            TaskCondition('documentation', 'postvisit', 'glm'), run_id='frozen-panel',
            stage_input=result['stage_inputs']['documentation'])
        self.assertEqual(branch['status'], 'completed')
        self.assertNotIn('history.query', [op for op, _, _ in branch_port.calls])
        for _, _, obs in branch_factory.observations:
            if obs['action'] in {'document', 'review_note'}:
                self.assertEqual(obs['current_encounter_measurements'], panel)

    def test_prescribing_with_empty_diagnosis_gets_one_same_input_contract_correction(self):
        class MissingDiagnosisFactory(FakeFactory):
            def session(self, scope, role, model_family=None):
                session=super().session(scope,role,model_family)
                original=session.respond
                def respond(obs):
                    response=original(obs)
                    if response.get('action')=='consultation_complete':
                        response['diagnoses']=([{'code':'I50.9','display':'Heart failure','system':'ICD-10',
                            'certainty':'working','primary':True}] if obs.get('schema_correction') else [])
                    return response
                session.respond=respond
                return session
        class CataloguePort(FakePort):
            def execute(self,op,payload,context):
                result=super().execute(op,payload,context)
                if op=='diagnosis.catalogue':
                    result['data']={'items':[{'code':'I50.9','display':'Heart failure','system':'ICD-10'}]}
                return result
        result=self.runner(MissingDiagnosisFactory(),CataloguePort()).run_case(example_case(),run_id='diagnosis-contract')
        self.assertEqual(result['status'],'completed')
        calls=[c for c in result['calls'] if c.get('response',{}).get('action')=='consultation_complete']
        self.assertEqual([c['status'] for c in calls],['invalid_schema','completed'])
        self.assertEqual(calls[0]['response']['diagnoses'],[])
        self.assertEqual(calls[0]['original_clinical_input_hash'],calls[1]['original_clinical_input_hash'])
        self.assertEqual(result['artifacts']['consultation']['diagnoses'][0]['code'],'I50.9')

    def test_no_rx_with_undetermined_diagnosis_is_audited_without_fabricated_native_code(self):
        class UndeterminedFactory(FakeFactory):
            def session(self, scope, role, model_family=None):
                session = super().session(scope, role, model_family)
                original = session.respond
                def respond(obs):
                    if obs['action'] == 'consult':
                        return {'action': 'consultation_complete', 'assessment': 'current diagnosis undetermined',
                            'diagnoses': [], 'plan': 'defer for further evaluation',
                            'prescription': {'items': [], 'no_prescription_reason': 'insufficient current evidence'}}
                    return original(obs)
                session.respond = respond
                return session
        class CataloguePort(FakePort):
            def execute(self, op, payload, context):
                response = super().execute(op, payload, context)
                if op == 'diagnosis.catalogue':
                    response['data'] = {'items': [{'code': 'I50.9', 'display': 'Heart failure', 'system': 'ICD-10'}]}
                return response
        port = CataloguePort()
        result = self.runner(UndeterminedFactory(), port).run_case(example_case(), run_id='undetermined-no-rx')
        self.assertEqual(result['status'], 'incomplete_clinical_decision')
        self.assertEqual(result['artifacts']['consultation']['diagnoses'], [])
        self.assertFalse(result['artifacts']['consultation_persistence']['native_diagnosis_recorded'])
        self.assertEqual(result['artifacts']['prescription_eligibility']['stated_reason'], 'insufficient current evidence')
        self.assertNotIn('consultation.save', [op for op, _, _ in port.calls])
        self.assertNotIn('order.save', [op for op, _, _ in port.calls])

    def test_manual_current_measurement_query_cannot_cross_release_phase(self):
        class QueryFactory(FakeFactory):
            def session(self, scope, role, model_family=None):
                session = super().session(scope, role, model_family)
                original = session.respond
                requested = set()
                def respond(obs):
                    if role == 'doctor' and obs['action'] in {'verify_handoff', 'consult'} and obs['action'] not in requested:
                        requested.add(obs['action'])
                        self.observations.append((role, model_family, deepcopy(obs)))
                        return {'action': 'read_history', 'section': 'current_observations', 'offset': 0, 'limit': 5}
                    return original(obs)
                session.respond = respond
                return session
        case = example_case()
        case['his_initial']['current_observations'] = [
            {'name': 'CURRENT_PANEL_VALUE', 'value': 76, 'available_at': 'physician_consultation'},
            {'name': 'FUTURE_UNAVAILABLE', 'value': 999, 'available_at': 'future_test'}]
        factory = QueryFactory()
        result = self.runner(factory).run_case(case, run_id='panel-phase-guard')
        self.assertEqual(result['status'], 'completed')
        for _, _, obs in factory.observations:
            self.assertNotIn('FUTURE_UNAVAILABLE', json.dumps(obs))
            if obs['action'] == 'verify_handoff':
                self.assertNotIn('CURRENT_PANEL_VALUE', json.dumps(obs))
        self.assertEqual(result['history_query_counts'], {'doctor_handoff_verification': 1, 'consultation': 1})
        self.assertTrue(any(e.get('result_status') == 'not_available_before_physician_consultation' for e in result['events']))

    def test_role_prompts_and_observation_contracts_do_not_offer_other_role_actions(self):
        self.assertIn('document:', system_prompt('scribe'))
        self.assertNotIn('review_note:', system_prompt('scribe'))
        self.assertNotIn('consult:', system_prompt('scribe'))
        for role in ('pharmacist', 'rx_ai'):
            self.assertIn('review_prescription:', system_prompt(role))
            self.assertNotIn('document:', system_prompt(role))
            self.assertNotIn('adjudicate:', system_prompt(role))
        self.assertIn('adjudicate:', system_prompt('senior_pharmacist'))
        self.assertNotIn('review_prescription:', system_prompt('senior_pharmacist'))
        with self.assertRaises(ValueError):
            contract_for('scribe', 'review_note')
        factory = FakeFactory()
        result = self.runner(factory).run_case(example_case(), run_id='role-contracts')
        self.assertEqual(result['status'], 'completed')
        for role, _, obs in factory.observations:
            contract = contract_for(role, obs['action'])
            self.assertEqual(obs['allowed_decisions'], contract.get('allowed_decisions', []))
            self.assertEqual(obs['required_response']['format'], 'json_object')

    def test_one_schema_correction_preserves_input_model_original_response_and_usage(self):
        class MisroutedDraftFactory(FakeFactory):
            def session(self, scope, role, model_family=None):
                session = super().session(scope, role, model_family)
                original = session.respond
                wrong_sent = False
                def respond(obs):
                    nonlocal wrong_sent
                    if role == 'scribe' and obs['action'] == 'document':
                        session.last_usage = {'prompt_tokens': 11, 'completion_tokens': 7}
                        session.model_name = 'fake-same-tool-model'
                        if not wrong_sent:
                            wrong_sent = True
                            self.observations.append((role, model_family, deepcopy(obs)))
                            return {'decision': 'accept', 'reason': 'mistaken reviewer action'}
                    return original(obs)
                session.respond = respond
                return session
        factory = MisroutedDraftFactory()
        result = self.runner(factory).run_task(example_case(), TaskCondition('documentation', 'postvisit', 'glm'),
            run_id='schema-corrected', stage_input={'transcript': [], 'consultation': {'plan': 'unchanged fact'}})
        self.assertEqual(result['status'], 'completed')
        calls = [c for c in result['calls'] if c['role'] == 'scribe']
        self.assertEqual(len(calls), 2)
        self.assertEqual([c['status'] for c in calls], ['invalid_schema', 'completed'])
        self.assertEqual([c['schema_correction_attempt'] for c in calls], [0, 1])
        self.assertEqual({c['model_name'] for c in calls}, {'fake-same-tool-model'})
        self.assertEqual({c['model_family'] for c in calls}, {'glm'})
        self.assertEqual(sum(c['usage']['completion_tokens'] for c in calls), 14)
        repaired_observation = deepcopy(calls[1]['observation'])
        feedback = repaired_observation.pop('schema_correction')
        self.assertEqual(repaired_observation, calls[0]['observation'])
        self.assertEqual(feedback['previous_invalid_response'], calls[0]['response'])
        self.assertEqual(calls[0]['original_clinical_input_hash'], calls[1]['original_clinical_input_hash'])
        self.assertEqual(len(result['artifacts']['note_versions']), 1)
        self.assertNotIn('mistaken reviewer action', result['artifacts']['note_versions'][0]['content']['text'])

    def test_schema_correction_is_bounded_and_never_retries_clinical_stop_decisions(self):
        class AlwaysWrongDraftFactory(FakeFactory):
            answer = {'decision': 'accept', 'reason': 'wrong role'}
            def session(self, scope, role, model_family=None):
                session = super().session(scope, role, model_family)
                original = session.respond
                def respond(obs):
                    if role == 'scribe':
                        self.observations.append((role, model_family, deepcopy(obs)))
                        return deepcopy(self.answer)
                    return original(obs)
                session.respond = respond
                return session
        for answer, expected_calls in [({'decision': 'accept'}, 2), ({'decision': 'reject'}, 1),
                ({'decision': 'defer'}, 1), ({'prescription': {'items': []}}, 1)]:
            with self.subTest(answer=answer):
                factory = AlwaysWrongDraftFactory()
                factory.answer = answer
                result = self.runner(factory).run_task(example_case(), TaskCondition('documentation', 'postvisit', 'glm'),
                    run_id='bounded-'+str(expected_calls)+str(answer),
                    stage_input={'transcript': [], 'consultation': {'plan': 'unchanged fact'}})
                self.assertEqual(result['status'], 'failed')
                self.assertEqual(len([c for c in result['calls'] if c['role']=='scribe']), expected_calls)
                self.assertEqual(result['artifacts']['note_versions'], [])
                self.assertNotIn('attested_note', result['artifacts'])
        for response in ({'decision': 'reject'}, {'decision': 'defer'}, {'decision': 'retain_rejected'},
                         {'prescription': {'items': []}}):
            self.assertFalse(schema_correction_allowed(response))
        self.assertEqual(shape_errors('senior_pharmacist', 'adjudicate', {'decision': 'defer'}), [])
        # A clinically wrong positive dose is not a formatting correction target.
        self.assertEqual(shape_errors('doctor', 'consult', {'action': 'consultation_complete',
            'prescription': new_rx('999999')}), [])

    def test_structured_current_assessment_validates_shape_without_inventing_omissions(self):
        base={'action':'consultation_complete','prescription':{'items':[]}}
        self.assertEqual(shape_errors('doctor','consult',base),[])
        good={**base,'medication_reconciliation':{'status':'partial','basis':'One reported product unresolved.',
                                                'unresolved':['product identity']},
              'allergy_assessment':{'status':'unable_to_ascertain','basis':'Patient cannot recall.'}}
        self.assertEqual(shape_errors('doctor','consult',good),[])
        for field,value in [('medication_reconciliation',{'status':'verified_gold','basis':'x','unresolved':[]}),
                            ('medication_reconciliation',{'status':'partial','basis':'x','unresolved':'unknown'}),
                            ('allergy_assessment',{'status':'none_known','basis':''})]:
            changed=deepcopy(good);changed[field]=value
            self.assertTrue(shape_errors('doctor','consult',changed))
        self.assertNotIn('allergy_assessment',base)

    def test_completed_json_syntax_error_has_one_same_input_correction(self):
        class SyntaxFactory(FakeFactory):
            always=False
            def session(self,scope,role,model_family=None):
                session=super().session(scope,role,model_family);original=session.respond;count=0
                def respond(obs):
                    nonlocal count
                    if role=='scribe':
                        count+=1
                        if count==1 or self.always:
                            exc=json.JSONDecodeError('Extra data','{"action":"draft"} prose',19)
                            exc.parse_metadata={'finish_reason':'stop','normalization_performed':False}
                            session.last_usage={'prompt_tokens':11,'completion_tokens':7}
                            raise exc
                    return original(obs)
                session.respond=respond
                return session
        for always,status in [(False,'completed'),(True,'failed')]:
            factory=SyntaxFactory();factory.always=always
            result=self.runner(factory).run_task(example_case(),TaskCondition('documentation','postvisit','glm'),
                run_id='syntax-'+str(always),stage_input={'transcript':[],'consultation':{'plan':'unaltered fact'}})
            self.assertEqual(result['status'],status,result.get('failure_reason'))
            calls=[c for c in result['calls'] if c['role']=='scribe'];self.assertEqual(len(calls),2)
            self.assertEqual(calls[0]['status'],'failed')
            self.assertEqual(calls[0]['error_type'],'JSONDecodeError')
            self.assertEqual(calls[0]['original_clinical_input_hash'],calls[1]['original_clinical_input_hash'])
            second=deepcopy(calls[1]['observation']);correction=second.pop('schema_correction')
            self.assertEqual(second,calls[0]['observation'])
            self.assertTrue(correction['previous_invalid_response_hash'])
            self.assertNotIn('previous_invalid_response_text',correction)
            self.assertEqual(calls[0]['usage']['completion_tokens'],7)
            self.assertEqual(sum(e['correction_scheduled'] for e in result['events'] if e['event']=='role_response_json_invalid'),1)

    def test_provider_parse_exception_is_not_retried_as_schema_correction(self):
        class ParseFailureFactory(FakeFactory):
            def session(self, scope, role, model_family=None):
                session = super().session(scope, role, model_family)
                original = session.respond
                def respond(obs):
                    if role == 'scribe':
                        raise ValueError('provider JSON parse exception')
                    return original(obs)
                session.respond = respond
                return session
        result = self.runner(ParseFailureFactory()).run_task(example_case(),
            TaskCondition('documentation', 'postvisit', 'glm'), run_id='parse-failure',
            stage_input={'transcript': [], 'consultation': {'plan': 'source'}})
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(len(result['calls']), 1)
        self.assertEqual(result['calls'][0]['schema_correction_attempt'], 0)

    def test_duplicate_key_response_has_one_recorded_same_input_format_correction(self):
        from sandbox.experiments.linked.json_boundary import POLICY, parse_json_response
        class DuplicateFactory(FakeFactory):
            always=False
            def session(self,scope,role,model_family=None):
                session=super().session(scope,role,model_family)
                original=session.respond
                count=0
                def respond(obs):
                    nonlocal count
                    if role=='scribe':
                        count+=1
                        if count==1 or self.always:
                            session.last_usage={'prompt_tokens':13,'completion_tokens':9}
                            parse_json_response('{"action":"document","action":"reject"}',
                                finish_reason='stop',policy=POLICY)
                    return original(obs)
                session.respond=respond
                return session
        for always,expected in [(False,'completed'),(True,'failed')]:
            factory=DuplicateFactory();factory.always=always
            result=self.runner(factory).run_task(example_case(),TaskCondition('documentation','postvisit','glm'),
                run_id='duplicate-format-'+str(always),stage_input={'transcript':[],
                    'consultation':{'plan':'unchanged visible source'}})
            self.assertEqual(result['status'],expected,result.get('failure_reason'))
            calls=[c for c in result['calls'] if c['role']=='scribe']
            self.assertEqual(len(calls),2)
            self.assertEqual(calls[0]['error_type'],'DuplicateJSONKeyError')
            self.assertEqual(calls[0]['status'],'failed')
            self.assertEqual(calls[0]['usage']['completion_tokens'],9)
            self.assertEqual(calls[0]['original_clinical_input_hash'],calls[1]['original_clinical_input_hash'])
            second=deepcopy(calls[1]['observation']);correction=second.pop('schema_correction')
            self.assertEqual(second,calls[0]['observation'])
            self.assertNotIn('previous_invalid_response',correction)
            self.assertNotIn('previous_invalid_response_text',correction)
            self.assertEqual(sum(e['correction_scheduled'] for e in result['events']
                if e['event']=='role_response_json_invalid'),1)


if __name__ == "__main__":
    unittest.main()
