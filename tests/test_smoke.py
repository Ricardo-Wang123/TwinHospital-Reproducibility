"""A few offline regressions for the cleanup; no API calls or clinical cases."""

import os
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agents.base_agent import BaseAgent
from sandbox.core.config_loader import ConfigLoader
from sandbox.core.sandbox_env import SandboxEnvironment
from sandbox.core.state_manager import StateManager
from sandbox.paths import PROJECT_ROOT
from sandbox.run_experiment import run_clock_loop


class SmokeTests(unittest.TestCase):
    def config(self, folder):
        path = Path(folder) / "experiment.yaml"
        path.write_text(
            "experiment: {name: test}\n"
            "model: {model_type: '${TEST_MODEL:-fallback}', api_key: dummy, api_url: 'http://example.invalid/v1'}\n"
            "models: {doctor: {temperature: 0.2}}\n"
            "paths: {output: output}\n"
            "pipeline: {max_clocks: 3, stages: {triage: {mode: ai}, consultation: {enabled: true}}}\n"
            "openhis_integration: {enabled: false}\n",
            encoding="utf-8",
        )
        with patch("sandbox.core.config_loader.load_dotenv"), patch.dict(os.environ, {"TEST_MODEL": "test-model"}):
            return ConfigLoader(path)

    def test_config_local_override_env_paths_and_cli(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "experiment.local.yaml").write_text("resources: {num_patients: 2}\n", encoding="utf-8")
            config = self.config(folder)
            self.assertEqual(config.get("resources.num_patients"), 2)
            self.assertEqual(config.get_model_config("doctor")["model_type"], "test-model")
            self.assertEqual(Path(config.resolve_path("output")), PROJECT_ROOT / "output")
            config.apply_cli_overrides("no_ai_triage", ["emr"])
            self.assertEqual(config.get_stage_config("triage")["mode"], "no_ai")
            self.assertEqual(config.get("ablation.disabled_tools"), ["emr"])
            config.to_dict()["model"]["api_key"] = "changed"
            self.assertEqual(config.get("model.api_key"), "dummy")

    def test_role_models_and_disabled_tools(self):
        with tempfile.TemporaryDirectory() as folder:
            config = self.config(folder)
            config.config["tools"] = {"exam": {"enabled": False}}
            config.apply_cli_overrides(disabled_tools=["emr"])
            env = SandboxEnvironment(config.to_dict())
            self.assertEqual(env.get_model_config("doctor").temperature, 0.2)
            self.assertEqual(env.get_model_config("patient").model_type, "test-model")
            self.assertFalse(env.should_enable_tool("exam"))
            self.assertFalse(env.should_enable_tool("emr"))
            self.assertTrue(env.should_enable_tool("pharmacy"))

    def test_json_parser_handles_dict_and_non_object_responses(self):
        agent = BaseAgent.__new__(BaseAgent)
        self.assertEqual(agent.parse_json_response({"reply": "ok"}), {"reply": "ok"})
        self.assertEqual(agent.parse_json_response('{"reply": "ok",}'), {"reply": "ok"})
        self.assertEqual(agent.parse_json_response("[]"), {})
        self.assertEqual(agent.parse_json_response(None), {})

    def test_doctor_queue_continues_after_triage_queue_is_empty(self):
        from types import SimpleNamespace

        queue = []
        coordinator = SimpleNamespace(doctor_status={"doctor": {"waiting_queue": queue}}, pending=True)
        coordinator.has_patients_in_queue = lambda: coordinator.pending
        env = SimpleNamespace(get_coordinator=lambda: coordinator)

        def triage(*args):
            coordinator.pending = False
            for patient_id in ("p1", "p2"):
                queue.append({"patient_info": {"patient_id": patient_id}, "patient_name": patient_id, "status": "waiting"})
            return [{"patient_id": p, "status": "completed"} for p in ("p1", "p2")]

        def consult(*args):
            patient = queue.pop(0)
            return [{"patient_id": patient["patient_name"], "status": "completed"}]

        with tempfile.TemporaryDirectory() as folder:
            with patch("sandbox.run_experiment.TriageStage.execute", side_effect=triage), patch("sandbox.run_experiment.ConsultationStage.execute", side_effect=consult):
                _, results, clocks = run_clock_loop(self.config(folder), StateManager("offline"), env)
            self.assertEqual([r["patient_id"] for r in results], ["p1", "p2"])
            self.assertEqual(clocks, 2)
            self.assertEqual(queue, [])

    def test_stalled_queue_reports_failure(self):
        from types import SimpleNamespace

        coordinator = SimpleNamespace(doctor_status={}, has_patients_in_queue=lambda: True)
        env = SimpleNamespace(get_coordinator=lambda: coordinator)
        with tempfile.TemporaryDirectory() as folder:
            with patch("sandbox.run_experiment.TriageStage.execute", return_value=[]):
                with self.assertRaisesRegex(RuntimeError, "避免空转"):
                    run_clock_loop(self.config(folder), StateManager("stalled"), env)
            failure = [{"patient_id": "p1", "status": "failed", "error": "business failure", "openhis_emr_id": "33"}]
            for stop_on_error in (True, False):
                with self.subTest(stop_on_error=stop_on_error), patch("sandbox.run_experiment.TriageStage.execute", return_value=failure):
                    config = self.config(folder)
                    config.config["pipeline"]["stop_on_error"] = stop_on_error
                    state = StateManager("failed")
                    with self.assertRaises(RuntimeError):
                        run_clock_loop(config, state, env)
                    self.assertEqual(state.get_stage_input("triage"), failure)
                    self.assertEqual(state.state["stage_results"]["triage"]["status"], "failed")


class ConsultationQueueTests(unittest.TestCase):
    def run_case(self, openhis_fails=False):
        from types import SimpleNamespace
        from agents.coordinator import Coordinator
        from sandbox.stages.consultation import ConsultationStage

        patient = {"patient_id": "p1", "profile_path": "synthetic.json"}
        coordinator = Coordinator.__new__(Coordinator)
        coordinator.patient_queue = [patient]
        coordinator.doctor_agents = {"d1": SimpleNamespace()}
        coordinator.doctor_status = {"d1": {"waiting_queue": [{"patient_info": patient, "patient_name": "test", "status": "waiting"}], "current_patient": None, "completed_patients": [], "room_id": None}}
        room = SimpleNamespace(room_id="room", messages=[{}, {}], run_conversation=lambda **kw: {"is_end": True}, close=lambda: None)
        coordinator.chat_rooms = {"room": room}
        coordinator._create_room = lambda **kw: "room"
        env = SimpleNamespace(
            get_coordinator=lambda: coordinator,
            get_model_config=lambda role: SimpleNamespace(to_dict=lambda: {}),
            record_system=None, config={"openhis_integration": {}},
            openhis_adapter=object() if openhis_fails else None,
        )
        stage = ConsultationStage({})
        with patch("sandbox.stages.consultation.PatientAgent"), patch.object(stage, "_enrich_prescription_from_record", side_effect=lambda env, patient, data: data), patch.object(stage, "_call_openhis_finalize_registration", return_value={"success": False, "error": "business failure"}), patch.object(stage, "_call_openhis_create_emr") as emr:
            results = stage.execute(StateManager("queue"), env, {"triage_results": [{**patient, "patient_name": "test", "assigned_doctor": "d1"}]})
            if openhis_fails:
                emr.assert_not_called()
        return results, coordinator.doctor_status["d1"]

    def test_consultation_removes_and_completes_waiting_patient(self):
        results, status = self.run_case()
        self.assertEqual(results[0]["status"], "completed")
        self.assertEqual(status["waiting_queue"], [])
        self.assertIsNone(status["current_patient"])
        self.assertEqual(len(status["completed_patients"]), 1)

    def test_registration_failure_stops_downstream_writes(self):
        results, status = self.run_case(openhis_fails=True)
        self.assertEqual(results[0]["status"], "failed")
        self.assertEqual(status["waiting_queue"], [])
        self.assertIsNone(status["current_patient"])
        self.assertEqual(len(status["failed_patients"]), 1)


class OpenHISRepairTests(unittest.TestCase):
    def adapter(self):
        from sandbox.openhis.adapter import OpenHISAdapter
        from sandbox.openhis.id_mapper import OpenHISIDMapper
        adapter = OpenHISAdapter.__new__(OpenHISAdapter)
        adapter.config = {"fail_on_init_error": True}
        adapter.fail_on_error = True
        adapter.dry_run = False
        adapter.id_mapper = OpenHISIDMapper()
        adapter.stats = {"patients_initialized": 0, "errors": 0, "emrs_created": 0, "diagnoses_created": 0}
        adapter.client = Mock()
        return adapter

    @staticmethod
    def response(data=None, code=200):
        return Mock(status_code=200, json=lambda: {"code": code, "msg": "fixture", "data": data})

    def test_patient_fields_and_selected_initialization(self):
        from agents.patient_agent import PatientAgent
        from sandbox.openhis.clinical_fields import patient_payload
        patient = {"meta": {"patient_id": "p1"}, "identity": {"raw_data": {"name": "合成患者", "age": 45, "gender": "男"}}}
        agent = PatientAgent.__new__(PatientAgent)
        agent.name, agent.profile_data = None, patient
        self.assertEqual((agent.get_name(), agent.get_name()), ("p1", "p1"))
        self.assertEqual(agent.get_patient_info()["gender"], "男")
        self.assertEqual(patient_payload({"id": "p1", "data": patient})["genderEnum"], 0)
        self.assertEqual(patient_payload({"id": "p1", "data": patient})["age"], 45)
        adapter = self.adapter()
        with tempfile.TemporaryDirectory() as directory:
            doctor_dir, patient_dir = Path(directory, "doctors"), Path(directory, "patients")
            doctor_dir.mkdir(); patient_dir.mkdir()
            doctor_file = doctor_dir / "doctor_001.json"
            doctor_file.write_text(json.dumps({"identity": {"doctor_id": "d1"}}), encoding="utf-8")
            selected = patient_dir / "patient_001.json"
            selected.write_text(json.dumps(patient), encoding="utf-8")
            (patient_dir / "patient_002.json").write_text(json.dumps({"agent_meta": {"uuid": "p2"}}), encoding="utf-8")
            profiles = adapter.load_profiles_from_directory(str(doctor_dir), str(patient_dir), [str(doctor_file)], [str(selected)])
            self.assertEqual([p["patient"]["id"] for p in profiles], ["p1"])
        with patch.object(adapter, "_init_doctor", return_value=None), patch.object(adapter, "_init_patient") as create_patient:
            success, errors, mappings = adapter.init_data_from_local_profiles([{"doctor": {"id": "d1"}, "patient": {"id": "p1"}}])
            self.assertFalse(success)
            self.assertTrue(errors)
            self.assertEqual(mappings, {})
            create_patient.assert_not_called()
        with self.assertRaises(RuntimeError):
            adapter._response_json(self.response(code=500))
        adapter.resolve_department = Mock(return_value={"id": "10"})
        adapter.client.get.side_effect = lambda path, params: self.response({"records": [{"userName": params["searchKey"], "practitionerId": "123"}]})
        with patch.object(adapter, "_sync_doctor_department"):
            self.assertEqual(adapter._init_doctor({"id": "x" * 80, "department": "测试科室"}), "123")
        self.assertLessEqual(len(adapter.client.get.call_args.args[1]["searchKey"]), 30)

    def test_prescription_values_override_template_and_reject_ambiguity(self):
        from sandbox.openhis.clinical_fields import prescription_fields
        dictionaries = {kind: [{"dictValue": code, "dictLabel": label} for code, label in rows]
                        for kind, rows in {"unit_code": [("1", "片"), ("4", "mg"), ("10", "g")],
                                           "rate_code": [("QD", "每日一次")], "method_code": [("1", "口服")]}.items()}
        mapping = {"prescriptionParams": {"unitCode": "1", "unitPrice": 0.84, "dose": 0, "rateCode": "ST"},
                   "sourceRecord": {"ZCGG": "5mg(按氨氯地平计)", "ZXZJDW": "片"}}
        medication = {"dose": "5mg", "frequency": "每日一次", "route": "口服", "duration": "7天"}
        values = prescription_fields(medication, mapping, dictionaries)
        self.assertEqual((values["dose"], values["doseUnitCode"], values["rateCode"], values["quantity"]), (5, "4", "QD", 7))
        self.assertEqual(values["totalPrice"], 5.88)
        self.assertEqual(prescription_fields({**medication, "dose": "0.005g"}, mapping, dictionaries)["quantity"], 7)
        for bad in ({"dose": "0mg"}, {"frequency": "随便"}, {"duration": "长期"}, {"quantity": "1盒"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                prescription_fields({**medication, **bad}, mapping, dictionaries)
        self.assertEqual(mapping["prescriptionParams"]["dose"], 0)
        compound = {**mapping, "sourceRecord": {"ZCMC": "复方测试片", "ZCGG": "每片含5mg与0.4mg", "ZXZJDW": "片"}}
        with self.assertRaisesRegex(ValueError, "单次 dose"):
            prescription_fields({**medication, "quantity": "7片"}, compound, dictionaries)
        values = prescription_fields({**medication, "dose": "1片", "duration": "30天"}, compound, dictionaries)
        self.assertEqual((values["dose"], values["doseUnitCode"], values["quantity"]), (1, "1", 30))

    def test_pharmacy_information_and_record_validation(self):
        from tools.pharmacy_inventory import PharmacyInventory, PharmacyInventoryTool
        from tools.record_generator import MedicalRecordSystem
        with tempfile.TemporaryDirectory() as directory:
            path, slim = Path(directory, "mapping.json"), Path(directory, "slim.json")
            entries = {f"测试复方片({variant})": {"readyForPrescription": True,
                       "sourceRecord": {"ZCMC": "测试复方片", "ZCGG": "每片含5mg和0.4mg", "ZXZJDW": "片"},
                       "prescriptionParams": {"dose": 0, "rateCode": "ST"}}
                       for variant in ("Ⅰ", "Ⅱ")}
            path.write_text(json.dumps({"medicationsByName": entries}), encoding="utf-8")
            slim.write_text('{"items": [{"name": "旧缓存", "doseHint": 99}]}', encoding="utf-8")
            inventory = PharmacyInventory(str(path), str(slim))
            tool = PharmacyInventoryTool(inventory)
            item = json.loads(tool.get_pharmacy_medication("测试复方片(Ⅱ)"))["item"]
            self.assertEqual(item["name"], "测试复方片(Ⅱ)")
            self.assertEqual(item["doseRequirement"]["requiredDoseUnit"], "片")
            self.assertNotIn("doseHint", item)
            self.assertNotIn("frequencyCode", item)
            self.assertFalse(json.loads(tool.get_pharmacy_medication("测试复方片"))["found"])
            self.assertEqual(inventory.resolve_mapping_entry("测试复方片(Ⅰ)")[0], "测试复方片(Ⅰ)")
            record = MedicalRecordSystem.__new__(MedicalRecordSystem)
            record.pharmacy_inventory, record.save = inventory, Mock()
            record.records = {"p1": {"plan": {"prescription_reference": None}}}
            ref = {"decision": "medication", "medication": [{"name": "测试复方片(Ⅱ)", "dose": "5mg", "route": "口服", "frequency": "QD", "duration": "7天"}]}
            self.assertIn("未保存", record.update_record("p1", {"prescription_reference": ref}))
            record.save.assert_not_called()
            self.assertIsNone(record.records["p1"]["plan"]["prescription_reference"])
            ref["medication"][0]["dose"] = "1片"
            self.assertIn("已更新", record.update_record("p1", {"prescription_reference": ref}))
            record.save.assert_called_once_with("p1")

    def test_missing_prescription_fails_without_automatic_medication(self):
        from types import SimpleNamespace
        from sandbox.stages.consultation import ConsultationStage
        stage = ConsultationStage({})
        env = SimpleNamespace(record_system=None, openhis_adapter=None)
        for decision in ({"reply": "头痛时可临时服用止痛药"},
                         {"prescription_reference": {"decision": "medication", "medication": []}},
                         {"reply": "头痛时可临时服用止痛药", "prescription_reference": {"decision": "no prescription", "medication": []}}):
            with self.subTest(decision=decision), self.assertRaises(ValueError):
                stage._enrich_prescription_from_record(env, "p1", decision)
            self.assertNotIn("prescription_inferred", decision)
            self.assertNotIn("medication_list", decision)
        no_rx = {"prescription_reference": {"decision": "no prescription", "medication": []}}
        self.assertEqual(stage._enrich_prescription_from_record(env, "p1", no_rx)["medication_list"], [])

    def test_saved_emr_and_diagnosis_ids_are_read_back(self):
        adapter = self.adapter()
        adapter.id_mapper.set_mapping("patient", "p1", "11")
        adapter.id_mapper.set_mapping("encounter", "p1", "22")
        adapter.client.post.return_value = self.response()
        adapter.client.get.return_value = self.response({"id": "33", "encounterId": "22", "patientId": "11"})
        with patch.object(adapter, "receive_encounter", return_value=(True, {})), patch.object(adapter, "_build_emr_data", return_value={}):
            self.assertEqual(adapter.create_emr_from_consultation("p1", {}, {})[:2], (True, "33"))
        adapter.client.get.reset_mock()
        adapter.client.get.return_value = self.response([
            {"conditionId": "55", "encounterDiagnosisId": "66", "definitionId": "1", "maindiseFlag": 1}])
        success, diagnosis_id, response = adapter.create_diagnosis_from_consultation(
            "p1", {"diagnosis_description": "高血压相关头部不适"}, {})
        self.assertTrue(success)
        self.assertEqual(diagnosis_id, "55")
        child = adapter.client.post.call_args.args[1]["diagnosisChildList"][0]
        self.assertEqual((child["definitionId"], child["diagnosisDesc"]), (1, "高血压相关头部不适"))
        adapter.client.get.assert_called_once_with(
            "/doctor-station/diagnosis/get-encounter-diagnosis", {"encounterId": "22"})
        self.assertEqual(adapter._resolve_diagnosis_context("22", {"response": response})["encounter_diagnosis_id"], "66")
        for source, expected in (({"chief_complaint": "头胀", "diagnosis_description": "其他描述"}, "头胀"),
                                 ({}, "门诊就诊")):
            self.assertEqual(adapter._build_diagnosis_data("22", "11", source)["diagnosisChildList"][0]["diagnosisDesc"], expected)

    def test_initialization_failure_is_saved(self):
        from sandbox.run_experiment import run_experiment
        with tempfile.TemporaryDirectory() as directory:
            config = Mock()
            config.get_stage_config.side_effect = lambda stage: {"enabled": stage != "followup", "mode": "ai"}
            config.resolve_path.return_value = directory
            for phase in ("environment", "agents"):
                with self.subTest(phase=phase), patch("sandbox.run_experiment.ConfigLoader", return_value=config), \
                     patch("sandbox.run_experiment.SandboxEnvironment") as env, \
                     patch("sandbox.run_experiment.rebuild_departments_if_enabled", return_value={}):
                    env.return_value.models = {}
                    if phase == "environment":
                        env.side_effect = RuntimeError("login unavailable")
                    else:
                        env.return_value.initialize_agents.side_effect = RuntimeError("profile initialization failed")
                    result = run_experiment("unused.yaml", phase)
                    saved = json.loads(Path(directory, f"{phase}_result.json").read_text(encoding="utf-8"))
                    self.assertEqual(saved, result)
                    self.assertEqual(saved["status"], "failed")
                    self.assertEqual(saved["error"], "login unavailable" if phase == "environment" else "profile initialization failed")

    def test_dialogue_stops_on_completion_limit_or_model_error(self):
        from agents.coordinator import ChatRoom
        from agents.doctor_agent import DoctorAgent
        from agents.nurse_agent import NurseAgent
        patient, medical = Mock(), Mock()
        medical.parse_json_response = lambda text: json.loads(text)
        medical.respond.return_value = '{"is_end": true}'
        room = ChatRoom("test", patient, medical, max_round=1)
        self.assertTrue(room.run_conversation()["is_end"])
        patient.respond.assert_not_called()
        medical.respond.return_value = '{"is_end": false}'
        patient.respond.return_value = "测试回复"
        with self.assertRaisesRegex(RuntimeError, "仍未完成"):
            ChatRoom("unfinished", patient, medical, max_round=1).run_conversation()
        for role in (DoctorAgent, NurseAgent):
            agent = role.__new__(role)
            agent.agent = Mock()
            agent.agent.step.side_effect = RuntimeError("model unavailable")
            with self.subTest(role=role.__name__), self.assertRaisesRegex(RuntimeError, "model unavailable"):
                agent.respond("测试")


if __name__ == "__main__":
    unittest.main()
