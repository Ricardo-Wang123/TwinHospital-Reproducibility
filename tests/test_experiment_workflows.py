"""Synthetic-only workflow contracts. Gate records mutations; no HIS or LLM."""
import copy
import importlib
import sys
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class Gate:
    def __init__(self):
        self.operations, self.events, self.activities, self.notes = [], [], [], {}

    def emit(self, event_type, **fields):
        event = {"event_type": event_type, "timestamp": len(self.events), **fields}
        self.events.append(event)
        return event

    def effort(self, role, activity_type, seconds=None):
        self.activities.append((role, activity_type, seconds))

    def perform(self, operation, payload, role):
        self.operations.append((operation, copy.deepcopy(payload), role))
        if operation == "order.save":
            return {"status": "executed", "outcome": "executed", "state": {"orders": {
                payload["order_id"]: {"items": payload["items"], "status": "draft"}}}}
        if operation == "encounter.end":
            return {"success": True, "timestamp": len(self.events)}
        note_id = payload["note_id"]
        if operation == "note.attest":
            if role != "doctor" or payload["version"] != self.notes[note_id]["version"]:
                return {"success": False, "error": "invalid attestation"}
            return {"success": True, "note_id": note_id, "version": payload["version"], "timestamp": len(self.events)}
        current = self.notes.get(note_id, {"version": 0})
        if payload.get("expected_version", current["version"]) != current["version"]:
            return {"success": False, "error": "version conflict"}
        self.notes[note_id] = {"version": current["version"] + 1, "content": payload["content"]}
        return {"success": True, "note_id": note_id, "version": self.notes[note_id]["version"]}


class ExperimentWorkflows(unittest.TestCase):
    def setUp(self):
        try:
            self.actors = importlib.import_module("sandbox.experiments.actors")
            self.workflows = importlib.import_module("sandbox.experiments.workflows")
        except ModuleNotFoundError:
            self.fail("The approved offline workflow modules have not been implemented")

    def run_case(self, task, workflow, data, *, factory=None, family="medical", pprs=None):
        gate = Gate()
        case = {"case_id": "demo-p", "task": task, "input": data}
        variant = {"task": task, "workflow": workflow, "model_family": family, "config_id": "demo-config"}
        factory = factory or self.actors.AgentSessionFactory(provider="scripted", seed=7)
        result = self.workflows.execute_workflow(case, variant, actor_factory=factory,
            pprs=pprs or self.actors.FixturePPRS(), emit=gate.emit, perform=gate.perform, effort=gate.effort)
        return result, gate

    def test_all_five_documentation_conditions_require_physician_attestation(self):
        transcript = [{"sequence": 1, "speaker_role": "patient", "text": "DEMO early", "available_at_ms": 0, "verified": True},
                      {"sequence": 2, "speaker_role": "doctor", "text": "DEMO late", "available_at_ms": 20, "verified": True}]
        for workflow, family in [("manual_documentation", None), ("realtime", "medical"),
                                 ("realtime", "foundation"), ("postvisit", "medical"), ("postvisit", "foundation")]:
            with self.subTest(workflow=workflow, family=family):
                result, gate = self.run_case("documentation", workflow, {"transcript": transcript}, family=family)
                self.assertEqual(result["status"], "completed")
                self.assertNotIn("qualified", result)
                self.assertIsNone(result["first_pass_correct"])
                names = [x[0] for x in gate.operations]
                self.assertLess(names.index("encounter.end"), names.index("note.attest"))
                self.assertEqual(gate.operations[-1][2], "doctor")
                self.assertTrue(result["artifact_versions"])
                if workflow == "realtime":
                    self.assertNotIn("DEMO late", str(result["artifact_versions"][0]["content"]))
                    self.assertIn("DEMO late", str(result["artifact_versions"][-1]["content"]))

    def test_documentation_review_revision_preserves_history(self):
        factory = self.actors.AgentSessionFactory(provider="scripted", scripts={"doctor": [
            {"action": "review", "decision": "revise", "content": {"text": "DEMO corrected"}},
            {"action": "review", "decision": "accept"}]})
        result, gate = self.run_case("documentation", "postvisit", {"transcript": [
            {"sequence": 1, "speaker_role": "patient", "text": "DEMO draft", "available_at_ms": 0, "verified": True}]}, factory=factory)
        self.assertEqual(result["status"], "completed")
        self.assertIn("note.revise", [x[0] for x in gate.operations])
        self.assertEqual(result["rework_rounds"], 1)
        self.assertEqual(result["artifact_versions"][-1]["content"], {"text": "DEMO corrected"})

    def test_failed_attestation_never_completes(self):
        gate = Gate()
        def reject(operation, payload, role):
            return {"success": False, "error": "denied"} if operation == "note.attest" else gate.perform(operation, payload, role)
        result = self.workflows.execute_workflow({"case_id": "p", "task": "documentation", "input": {"transcript": [
            {"sequence": 1, "speaker_role": "patient", "text": "DEMO", "available_at_ms": 0}]}},
            {"task": "documentation", "workflow": "postvisit", "model_family": "medical", "config_id": "c"},
            actor_factory=self.actors.AgentSessionFactory(), pprs=self.actors.FixturePPRS(),
            emit=gate.emit, perform=reject, effort=gate.effort)
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["physician_attested_at"])

    def test_preconsult_unknown_is_preserved_without_nursing_in_ai_only(self):
        data = {"initial_visible_facts": [], "patient_known_facts": [
            {"fact_id": "allergies", "value": None, "status": "unknown", "release_on": ["allergies"]},
            {"fact_id": "chief_complaint", "value": "DEMO symptom", "status": "known", "release_on": ["chief_complaint"]}]}
        result, gate = self.run_case("preconsult", "ai_only", data)
        self.assertEqual(result["status"], "completed")
        facts = result["final_output"]["facts"]
        self.assertIn({"fact_id": "allergies", "value": None, "status": "unknown"},
                      [{k: f[k] for k in ("fact_id", "value", "status")} for f in facts])
        self.assertFalse(any(role == "nurse" for role, *_ in gate.activities))

    def test_nurse_review_can_elicit_omitted_information(self):
        factory = self.actors.AgentSessionFactory(scripts={"preconsult_ai": [{"action": "summary", "facts": []}]})
        result, gate = self.run_case("preconsult", "ai_nurse_review", {"initial_visible_facts": [], "patient_known_facts": [
            {"fact_id": "medications", "value": "DEMO med", "status": "known", "release_on": ["medications"]}]}, factory=factory)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(any(f["fact_id"] == "medications" and f["value"] == "DEMO med" for f in result["final_output"]["facts"]))
        self.assertTrue(any(role == "nurse" for role, *_ in gate.activities))

    def test_pprs_rejection_is_not_overridden(self):
        result, _ = self.run_case("prescription", "parallel", {"prescription": {"items": [{"code": "DEMO", "dose": -1}]}})
        self.assertEqual(result["final_decision"], "reject")

    def test_parallel_pharmacist_is_blinded_and_disagreement_escalates(self):
        seen = []
        class Factory:
            def session(self, case_id, role, model_family=None):
                class Session:
                    def respond(self, observation):
                        seen.append((role, model_family, copy.deepcopy(observation)))
                        decision = "flag" if role == "rx_ai" else "approve"
                        if role == "senior_pharmacist": decision = "reject"
                        return {"action": "review", "decision": decision, "flags": []}
                return Session()
        result, _ = self.run_case("prescription", "parallel", {"prescription": {"items": [{"code": "DEMO", "dose": 1}]}}, factory=Factory())
        pharmacist = next(obs for role, _, obs in seen if role == "pharmacist")
        self.assertNotIn("ai_review", pharmacist)
        self.assertIn("senior_pharmacist", [role for role, _, _ in seen])
        self.assertTrue(all(family is None for role, family, _ in seen if role != "rx_ai"))
        self.assertEqual(result["final_decision"], "reject")

    def test_scripted_model_family_does_not_encode_quality_ranking(self):
        data = {"prescription": {"items": [{"code": "DEMO", "dose": 1}]}}
        a, _ = self.run_case("prescription", "sequential", data, family="medical")
        b, _ = self.run_case("prescription", "sequential", data, family="foundation")
        self.assertEqual(a["final_output"], b["final_output"])
        self.assertEqual(a["final_decision"], b["final_decision"])

    def test_replay_pprs_validates_content_hash_and_version(self):
        prescription = {"items": []}
        from hashlib import sha256
        digest = sha256(b'{"items":[]}').hexdigest()
        replay = self.actors.ReplayPPRS([{ "input_hash": digest, "rule_version": "demo-v1",
            "result": {"decision": "pass", "flags": []}}], rule_version="demo-v1")
        self.assertEqual(replay.review(prescription)["decision"], "pass")
        with self.assertRaises(ValueError): replay.review({"items": [{"code": "changed"}]})
        with self.assertRaises(ValueError): self.actors.ReplayPPRS([
            {"input_hash": digest, "rule_version": "wrong", "result": {"decision": "pass"}}], rule_version="demo-v1")

    def test_runtime_rejects_gold_and_sessions_do_not_share_script_position(self):
        factory = self.actors.AgentSessionFactory(scripts={"doctor": [{"decision": "revise"}, {"decision": "accept"}]})
        a, b = factory.session("p", "doctor"), factory.session("p", "doctor")
        self.assertEqual(a.respond({"action": "review"})["decision"], "revise")
        self.assertEqual(b.respond({"action": "review"})["decision"], "revise")
        with self.assertRaises(ValueError): a.respond({"gold": {"secret": 1}})

    def test_six_demo_faults_are_decided_from_prescription_fields(self):
        valid = {"items": [{"drug_code": "DEMO-A", "dose": "10", "dose_unit": "mg", "route": "oral",
            "frequency": "daily", "duration_days": 3, "quantity": "3", "quantity_unit": "tablet", "solvent": "DEMO-WATER"}],
            "patient_conditions": []}
        rules = self.actors.FixturePPRS()
        self.assertEqual(rules.review(valid)["decision"], "pass")
        for field, value, flag in [("dose", "1000", "dosage_error"), ("solvent", "DEMO-WRONG", "solvent_error"),
                                   ("route", "intrathecal", "administration_route_error")]:
            case = copy.deepcopy(valid)
            case["items"][0][field] = value
            self.assertEqual(rules.review(case)["flags"], [flag])
        duplicate = copy.deepcopy(valid)
        duplicate["items"].append(copy.deepcopy(valid["items"][0]))
        self.assertEqual(rules.review(duplicate)["flags"], ["duplicate_medication"])
        interaction = copy.deepcopy(valid)
        interaction["items"].append({**valid["items"][0], "drug_code": "DEMO-B"})
        self.assertEqual(rules.review(interaction)["flags"], ["drug_drug_interaction"])
        contraindication = copy.deepcopy(valid)
        contraindication["patient_conditions"] = ["DEMO-ALLERGY-A"]
        self.assertEqual(rules.review(contraindication)["flags"], ["contraindication"])

    def test_documentation_with_real_in_memory_gateway(self):
        from sandbox.experiments.backends import InMemoryHIS
        from sandbox.experiments.gateway import SandboxGateway
        backend = InMemoryHIS()
        backend.restore({"personnel": {"doctor-1": ["note.attest", "order.attest"]}})
        gateway = SandboxGateway(backend, epoch=1)
        events = []
        def perform(op, payload, role):
            action = {"run_id": "demo-run", "config_id": "c", "case_id": "p", "request_id": str(len(events)),
                      "epoch": 1, "operation": op, "payload": payload}
            ctx = {"epoch": 1, "actor_id": role + "-1", "patient_id": "p", "encounter_id": "e", "permissions": ["*"]}
            return gateway.execute(action, ctx)
        perform("case.bind", {"patient_id": "p", "encounter_id": "e"}, "doctor")
        result = self.workflows.execute_workflow({"case_id": "p", "task": "documentation", "input": {"transcript": [
            {"sequence": 1, "speaker_role": "patient", "text": "DEMO", "available_at_ms": 0}]}},
            {"task": "documentation", "workflow": "postvisit", "model_family": "medical", "config_id": "c"},
            actor_factory=self.actors.AgentSessionFactory(), pprs=self.actors.FixturePPRS(),
            emit=lambda event, **fields: events.append((event, fields)), perform=perform, effort=lambda *a, **k: None)
        self.assertEqual(result["status"], "completed", result.get("failure_reason"))
        note = backend.snapshot("p")["notes"]["c:p:note"]
        self.assertEqual(note["status"], "locked")
        self.assertEqual(note["versions"][-1]["signatory_id"], "doctor-1")
        self.assertIsNotNone(result["encounter_ended_at"])
        self.assertIsNotNone(result["physician_attested_at"])

    def test_doctor_reasks_only_information_lost_from_handoff(self):
        factory = self.actors.AgentSessionFactory(scripts={"preconsult_ai": [
            {"action": "ask", "question_topics": ["medications"]}, {"action": "summary", "facts": []}]})
        result, gate = self.run_case("preconsult", "ai_only", {"required_topics": ["medications"],
            "patient_known_facts": [{"fact_id": "medications", "value": "DEMO", "status": "known", "release_on": ["medications"]}]}, factory=factory)
        self.assertEqual(result["status"], "completed")
        self.assertIn(("doctor", "redundant_inquiry", None), gate.activities)
        self.assertEqual(result["interaction_count"], 2)
        self.assertEqual(result["final_output"], {"facts": []})
        self.assertEqual(result["final_output"], result["first_handoff_output"])
        self.assertEqual(result["post_consultation_output"]["facts"][0]["value"], "DEMO")
        self.assertIsNone(result["first_pass_correct"])

    def test_unavailable_judge_does_not_affect_actor_execution(self):
        result, _ = self.run_case("preconsult", "nurse_only", {"initial_visible_facts": [
            {"fact_id": "chief_complaint", "status": "known", "value": "DEMO"}],
            "patient_known_facts": [], "required_topics": ["chief_complaint"]}, family=None)
        self.assertEqual(result["status"], "completed")
        self.assertIsNone(result["first_pass_correct"])
        self.assertNotIn("qualified", result)

    def test_unsupported_drug_code_is_not_silently_approved(self):
        result = self.actors.FixturePPRS().review({"items": [{"drug_code": "UNLISTED", "dose": "10"}]})
        self.assertEqual(result["decision"], "flag")
        self.assertIn("unsupported_demo_drug", result["flags"])

    def test_nurse_review_effort_exists_even_when_no_questions_are_missing(self):
        result, gate = self.run_case("preconsult", "ai_nurse_review", {"required_topics": ["chief_complaint"],
            "initial_visible_facts": [{"fact_id": "chief_complaint", "value": "DEMO", "status": "known"}]})
        self.assertEqual(result["status"], "completed")
        self.assertIn(("nurse", "summary_review", None), gate.activities)

    def test_automatic_transcript_reading_is_not_human_effort(self):
        data = {"transcript": [{"sequence": 1, "speaker_role": "patient", "text": "DEMO", "available_at_ms": 0}]}
        result, gate = self.run_case("documentation", "realtime", data)
        self.assertEqual(result["status"], "completed")
        self.assertFalse(any(role == "scribe" for role, *_ in gate.activities))
        gate = Gate()
        result = self.workflows.execute_workflow({"case_id": "p", "task": "documentation", "input": data},
            {"task": "documentation", "workflow": "realtime", "model_family": "medical", "config_id": "c",
             "human_transcript_handling": True}, actor_factory=self.actors.AgentSessionFactory(),
            pprs=self.actors.FixturePPRS(), emit=gate.emit, perform=gate.perform, effort=gate.effort)
        self.assertEqual(result["status"], "completed")
        self.assertEqual([a for a in gate.activities if a[0] == "scribe"], [("scribe", "transcript_handling", None)])

    def test_camel_timeout_and_tokens_are_bounded_before_model_creation(self):
        model = {"model_type": "offline-test", "api_url": "http://invalid.example", "api_key": "DEMO-NOT-A-KEY"}
        with patch.object(self.actors, "_camel_session") as create:
            self.actors.AgentSessionFactory({"provider": "camel", "role_models": {"doctor": model}}).session("p", "doctor")
            configured = create.call_args.args[2]
            self.assertEqual(configured["timeout_seconds"], 60)
            self.assertEqual(configured["max_tokens"], 2000)
            for key, value in [("timeout_seconds", 0), ("timeout_seconds", float("nan")),
                               ("timeout_seconds", 121), ("max_tokens", 0), ("max_tokens", True)]:
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    self.actors.AgentSessionFactory({"provider": "camel", "role_models": {
                        "doctor": {**model, key: value}}}).session("p", "doctor")

    def test_only_doctor_can_persist_revision_and_original_decision_is_retained(self):
        corrected = {"items": [{"code": "DEMO", "dose": 1}]}
        factory = self.actors.AgentSessionFactory(scripts={
            "rx_ai": [{"action": "review", "decision": "revise", "prescription": {"items": []}}],
            "pharmacist": [{"action": "review", "decision": "revise", "prescription": {"items": []}}],
            "doctor": [{"decision": "revise", "prescription": corrected}]})
        result, gate = self.run_case("prescription", "sequential", {"prescription": corrected}, factory=factory)
        self.assertEqual(result["status"], "completed", result.get("failure_reason"))
        self.assertEqual(result["final_decision"], "reject")
        self.assertEqual(result["final_output"]["prescription"], corrected)
        self.assertEqual(result["revision_proposal"]["prescription"], corrected)
        saves = [row for row in gate.operations if row[0] == "order.save"]
        self.assertEqual(len(saves), 1)
        self.assertEqual(saves[0][2], "doctor")
        self.assertEqual(saves[0][1]["items"], corrected["items"])
        self.assertNotIn("order.attest", [row[0] for row in gate.operations])

    def test_mixed_providers_choose_intervention_and_role_models_independently(self):
        model = {"provider": "camel", "model_type": "offline-test", "api_url": "http://invalid.example",
                 "api_key": "DEMO-NOT-A-KEY", "timeout_seconds": 17, "max_tokens": 111}
        with patch("camel.models.ModelFactory.create") as create, patch("agents.base_agent.ChatAgent"):
            factory = self.actors.AgentSessionFactory({"provider": "scripted",
                "intervention_models": {"medical": model, "foundation": {"provider": "scripted"}}})
            patient = factory.session("p", "patient")
            medical = factory.session("p", "preconsult_ai", model_family="medical")
            foundation = factory.session("p", "preconsult_ai", model_family="foundation")
            self.assertIsInstance(patient, self.actors.ScriptedSession)
            self.assertIsInstance(foundation, self.actors.ScriptedSession)
            self.assertNotIsInstance(medical, self.actors.ScriptedSession)
            create.assert_called_once()
            self.assertEqual(create.call_args.kwargs["timeout"], 17)
            self.assertEqual(create.call_args.kwargs["max_retries"], 0)
            self.assertEqual(create.call_args.kwargs["model_config_dict"]["max_tokens"], 111)
            self.assertEqual(factory.provider_for("preconsult_ai", "medical"), "camel")
            self.assertEqual(factory.provider_for("doctor"), "scripted")
        with patch("camel.models.ModelFactory.create") as create, patch("agents.base_agent.ChatAgent"):
            factory = self.actors.AgentSessionFactory({"provider": "camel", "role_models": {"doctor": model},
                "intervention_models": {"medical": {"provider": "scripted"}}})
            self.assertIsInstance(factory.session("p", "rx_ai", "medical"), self.actors.ScriptedSession)
            factory.session("p", "doctor")
            create.assert_called_once()

    def test_scripted_provider_cannot_override_human_role_with_intervention(self):
        factory = self.actors.AgentSessionFactory()
        with self.assertRaises(ValueError):
            factory.session("p", "doctor", model_family="medical")


if __name__ == "__main__":
    unittest.main()
