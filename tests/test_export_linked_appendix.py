import fcntl
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from tools.export_linked_appendix import ProjectionReader, S, TRACE, export, project, safe_trace_metadata
from sandbox.experiments.linked.json_boundary import POLICY
from sandbox.experiments.linked.clinical import CLINICAL_DISPOSITION_POLICY


class AppendixExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.batch = self.root / "private_runs/batches/demo"
        self.batch.mkdir(parents=True)
        self.evidence = self.root / "private_runs/model_calls"
        self.evidence.mkdir()
        self.public = self.root / "public/bundle"
        self.maps = self.root / "private_maps"
        self.case, self.run = "private-case-123", "private-run-987"
        self.sent = "DO_NOT_PUBLISH_PATIENT_DIALOGUE_敏感"
        self.request1, self.request2 = "private-request-111", "private-request-222"
        self.ledger = self.root / "ledger.sqlite"
        db = sqlite3.connect(self.ledger)
        db.execute("CREATE TABLE calls(request_id TEXT,reserved INTEGER,charged INTEGER,status TEXT)")
        db.executemany("INSERT INTO calls VALUES(?,?,?,?)", [(self.request1, 500000, 500000, "unknown_charge"),
                       (self.request2, 500000, 125000, "completed")])
        db.commit()
        db.close()
        self.put(self.batch / "specification.json", {
            "batch": "demo", "mode": "matrix", "formal_experiment": False,
            "clinical_evaluation": "pending_independent_review", "business_protocol_version": "frozen-version-7",
            "models": {"role_model": "frozen-role-model", "tool_models": {"glm": "frozen-tool-model"},
                       "endpoint": "https://secret-endpoint.invalid"},
            "code_hashes": {"clinical.py": "a" * 64}, "catalog_hash": "b" * 64,
            "cases": [{"case_id": self.case, "data_kind": "synthetic", "split": "engineering",
                       "runtime_input_hash": "c" * 64}],
            "conditions": [{"config_id": "documentation:manual", "task": "documentation", "workflow": "manual"}],
            "budget_ledger": {"resolved_path": str(self.ledger)},
            "private_unknown": {"api_key": "private-key-no-export", "source_id": "patient-raw-native-998"}})
        self.put(self.batch / "batch_summary.json", {"finished": True, "budget": {"spent_cny": 999}})
        (self.batch / "batch.lock").touch()
        self.run_path = self.batch / self.case / "anchor/runs/run/run.json"
        self.put(self.batch / self.case / "attempts.json", {"case_id": self.case, "attempts": {
            "linked_baseline_anchor": {"status": "failed", "result_path": str(self.run_path)},
            "documentation__manual": {"status": "not_run_missing_frozen_input", "dependency": "linked_baseline_anchor"}}})
        self.put(self.run_path, {"run_id": self.run, "case_id": self.case, "status": "failed", "input_hash": "d" * 64,
            "artifacts": {"all_patient_history": self.sent * 10000, "patient_id": "patient-raw-native-998"},
            "calls": [{"sequence": 1, "role": "physician", "phase": "consultation", "model_name": "frozen-role-model",
                       "request_id": self.request2, "status": "completed", "observation_hash": "e" * 64,
                       "response_hash": "f" * 64, "observation": self.sent, "response": {"note": self.sent},
                       "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
                                 "completion_tokens_details": {"reasoning_tokens": 5}}}]})
        self.put(self.run_path.with_name("appendix_trace.json"), {"status": "failed", "evaluation_unit": "complete_encounter",
            "native_complete": False, "call_count": 1, "wall_seconds": 2,
            "clinical_quality": {"status": "not_independently_evaluated", "private_reason": self.sent},
            "artifact_hashes": {"note": "a" * 64}, "case_id": self.case,
            "events": [{"sequence": 1, "event": "business_operation", "operation": "history.index",
                        "readback_verified": True, "native_effect": False, "protection_source": "sidecar",
                        "returned_refs": ["patient-raw-native-998"]},
                       {"sequence": 2, "event": "business_operation", "operation": "order.save",
                        "readback_verified": True, "native_effect": True, "protection_source": "native_readback"},
                       {"sequence": 3, "event": "business_operation", "operation": "note.save",
                        "status": "failed_exception", "native_effect": None}]})
        for i, request in enumerate((self.request1, self.request2), 1):
            self.put(self.evidence / (request + ".json"), {
                "request_id": request, "run_id": self.run, "role": "physician", "model": "frozen-role-model",
                "recorded_at": f"2026-11-07T01:00:0{i}", "attempt": i, "retry_planned": i == 1,
                "outcome": "failed" if i == 1 else "completed", "request_body": {"messages": [self.sent]},
                "response_body": {"choices": [{"content": self.sent}]}, "accounted_upper_cny": .5 if i == 1 else .125,
                "usage": {"prompt_tokens": 100, "completion_tokens": 20}})
        self.put(self.evidence / "other.json", {"run_id": "other-private-batch", "request_id": "private-other",
                 "request_body": {"secret": self.sent}, "outcome": "failed"})

    def tearDown(self):
        self.temp.cleanup()

    def put(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False))

    def test_projection_skips_large_body_with_chunk_boundaries_and_unicode(self):
        import io
        text = json.dumps({"a": "hello\\\"😊", "skip": self.sent * 9000,
                           "list": [{"x": True, "skip": [1, {}, None]}], "n": 2.5}, ensure_ascii=False)
        reader = ProjectionReader(io.StringIO(text), chunk_size=7)
        self.assertEqual(reader.value({"a": S, "list": [{"x": S}], "n": S}),
                         {"a": "hello\\\"😊", "list": [{"x": True}], "n": 2.5})

    def test_diagnosis_display_policy_exports_version_without_free_text(self):
        from sandbox.experiments.linked.diagnosis_codes import POLICY as diagnosis_policy
        path = self.batch / 'specification.json'
        spec = json.loads(path.read_text())
        spec['diagnosis_code_policy'] = {**diagnosis_policy, 'private_text': self.sent}
        self.put(path, spec)
        export(self.batch, self.public, self.maps)
        public = json.loads((self.public / 'manifest.json').read_text())
        self.assertEqual(public['specification']['diagnosis_code_policy'], {
            'version': diagnosis_policy['version'], 'clinical_inference': False,
            'source_response_preserved': True})
        self.assertNotIn(self.sent, (self.public / 'manifest.json').read_text())

    def test_export_is_private_retry_aware_and_uses_frozen_versions(self):
        result = export(self.batch, self.public, self.maps)
        raw = "\n".join(p.read_text() for p in self.public.rglob("*") if p.is_file())
        for forbidden in (self.case, self.run, self.request1, self.request2, self.sent,
                          "patient-raw-native-998", "private-key-no-export", "secret-endpoint"):
            self.assertNotIn(forbidden, raw)
        summary = result["summary"]
        self.assertEqual(summary["planned_attempts"], 2)
        self.assertEqual(summary["observed_final_runs"], 1)
        self.assertEqual(summary["provider_http_attempts"], 2)
        self.assertEqual(summary["provider_attempt_number_gt_one"], 1)
        self.assertEqual(summary["uncertain_charge_requests"], 1)
        self.assertEqual(summary["batch_ledger_accounted_upper_cny"], .625)
        self.assertEqual(summary["business_evidence_counts"], {
            "native_effect_with_verified_readback": 1, "gateway_or_sidecar_verified": 1,
            "unverified_or_unknown_effect": 1})
        manifest = json.loads((self.public / "manifest.json").read_text())
        self.assertEqual(manifest["specification"]["business_protocol_version"], "frozen-version-7")
        self.assertIn("frozen-role-model", raw)
        trace = json.loads((self.public / "traces/S0001-A01.json").read_text())
        self.assertEqual(trace["model_calls"][0]["observation_hash"], "e" * 64)
        self.assertEqual(trace["model_calls"][0]["reasoning_tokens_subset_of_completion"], 5)
        self.assertEqual(os.stat(result["private_map"]).st_mode & 0o777, 0o600)
        self.assertIn(self.case, Path(result["private_map"]).read_text())
        # Read-only database behavior and deterministic public re-export.
        second = self.root / "public/reproduction"
        export(self.batch, second, self.maps)
        for p in self.public.rglob("*"):
            if p.is_file():
                self.assertEqual(p.read_bytes(), (second / p.relative_to(self.public)).read_bytes())

    def test_controlled_variant_metadata_exports_hashes_without_patient_or_label_prose(self):
        path=self.batch/'specification.json';spec=json.loads(path.read_text())
        spec['controlled_prescription_variant_policy']={
            'version':'controlled_original_order_route_v1','kind':'exact-product-route',
            'engineering_only':True,'formal_eligible':False,'registry_hash':'2'*64,
            'patient_facts_modified':False,'source_consultation_modified':False,
            'private_patient_text':self.sent,'reference_candidate':{'reason':self.sent}}
        self.put(path,spec)
        run=json.loads(self.run_path.read_text());run['controlled_variant_hash']='3'*64;self.put(self.run_path,run)
        export(self.batch,self.public,self.maps)
        raw='\n'.join(p.read_text() for p in self.public.rglob('*') if p.is_file())
        self.assertNotIn(self.sent,raw);self.assertNotIn('reference_candidate',raw)
        self.assertIn('3'*64,raw)
        policy=json.loads((self.public/'manifest.json').read_text())['specification']['controlled_prescription_variant_policy']
        self.assertEqual(policy['registry_hash'],'2'*64)
        self.assertFalse(policy['formal_eligible'])

    def test_running_or_unfinished_batch_is_rejected(self):
        with (self.batch / "batch.lock").open() as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, "still running"):
                export(self.batch, self.public, self.maps)
        (self.batch / "batch_summary.json").unlink()
        with self.assertRaisesRegex(ValueError, "finished"):
            export(self.batch, self.public, self.maps)

    def test_completion_limit_policy_and_retry_cause_export_without_raw_content(self):
        spec_path = self.batch / 'specification.json'
        spec = json.loads(spec_path.read_text())
        policy = {'thinking':True, 'reasoning_effort':'low', 'max_completion_tokens':16384,
            'truncation_retry':{'max_retries':1, 'max_completion_tokens':32768,
                'same_messages':True, 'shares_max_http_attempts':True, 'private_note':self.sent}}
        spec['generation_policy'] = {'role':policy, 'tools':{'glm':policy}}
        self.put(spec_path, spec)
        for rid,cap,index in [(self.request1,16384,0),(self.request2,32768,1)]:
            path=self.evidence/(rid+'.json'); row=json.loads(path.read_text())
            row.update(effective_max_completion_tokens=cap,truncation_retry_index=index,
                finish_reason='length' if index==0 else 'stop')
            if index==0:row.update(failure_reason_code='completion_length_limit',
                retry_reason='declared_same_input_completion_limit_increase')
            self.put(path,row)
        summary=export(self.batch,self.public,self.maps)['summary']
        self.assertEqual(summary['provider_completion_length_failures'],1)
        self.assertEqual(summary['provider_increased_completion_limit_attempts'],1)
        rows=json.loads((self.public/'request_index.json').read_text())
        self.assertEqual([x['effective_max_completion_tokens'] for x in rows],[16384,32768])
        self.assertIn('completion_length_limit',(self.public/'request_costs.csv').read_text())
        manifest=json.loads((self.public/'manifest.json').read_text())
        self.assertEqual(manifest['specification']['generation_policy']['role']['truncation_retry']['max_retries'],1)
        raw='\n'.join(p.read_text() for p in self.public.rglob('*') if p.is_file())
        self.assertNotIn(self.sent,raw)

    def test_new_provider_parameters_and_prospective_window_survive_public_projection(self):
        path=self.batch/'specification.json';spec=json.loads(path.read_text())
        window={'opens_at':'2026-11-07T01:00:00+00:00','closes_at':'2026-11-08T01:00:00+00:00',
                'unadmitted_cases':'retain_in_planned_denominator','private_note':self.sent}
        spec['execution_policy']={'workers':5,'formal_submission_window':window}
        spec['workflow_observation_policy']={'version':'prospective-workflow-window/1',
            'submission_window':window,'resources':{'private_text':self.sent}}
        spec['generation_policy']={'version':'explicit-chat-completions-v1','models':{'frozen-role-model':{
            'thinking':False,'max_completion_tokens':16384,'context_window_tokens':120000,
            'completion_limit_includes_reasoning':True,
            'request_parameters':{'thinking':{'type':'disabled'},'api_key':self.sent}}}}
        self.put(path,spec);export(self.batch,self.public,self.maps)
        raw=(self.public/'manifest.json').read_text();result=json.loads(raw)['specification']
        self.assertEqual(result['execution_policy']['formal_submission_window']['opens_at'],window['opens_at'])
        self.assertEqual(result['generation_policy']['models']['frozen-role-model']['request_parameters'],
                         {'thinking':{'type':'disabled'}})
        self.assertNotIn(self.sent,raw)
        self.assertNotIn('resources',result['workflow_observation_policy'])

    def test_private_crosswalk_cannot_be_inside_public_and_unknown_text_blocks(self):
        with self.assertRaisesRegex(ValueError, "separate"):
            export(self.batch, self.public, self.public / "private")
        trace = json.loads(self.run_path.with_name("appendix_trace.json").read_text())
        trace["error_type"] = self.sent
        self.put(self.run_path.with_name("appendix_trace.json"), trace)
        with self.assertRaisesRegex(ValueError, "Non-code text"):
            export(self.batch, self.public, self.maps)
        self.assertFalse(self.public.exists())

    def test_missing_provider_evidence_is_explicit_and_not_zeroed(self):
        (self.evidence / (self.request2 + ".json")).unlink()
        result = export(self.batch, self.public, self.maps)
        self.assertEqual(result["summary"]["missing_provider_evidence_for_final_call"], 1)
        self.assertFalse(result["summary"]["cost_is_complete"])
        self.assertEqual(result["summary"]["batch_ledger_accounted_upper_cny"], .5)

    def test_unindexed_requests_from_finished_driver_batch_are_not_silently_dropped(self):
        self.put(self.evidence / "unindexed.json", {"run_id": "demo:private-case-123:interrupted",
                 "request_id": "private-orphan-request", "outcome": "failed"})
        result = export(self.batch, self.public, self.maps)
        self.assertEqual(result["summary"]["unindexed_provider_attempts_matching_batch_prefix"], 1)
        self.assertFalse(result["summary"]["cost_is_complete"])
        self.assertIn("private-orphan-request", Path(result["private_map"]).read_text())
        self.assertNotIn("private-orphan-request", (self.public / "summary.json").read_text())

    def test_reused_source_anchor_is_provenance_not_a_new_attempt(self):
        spec = json.loads((self.batch / 'specification.json').read_text())
        spec['anchor_reuse'] = {'source_generation_version': 'engineering_v7', 'engineering_only': True,
            'source_specification_sha256': 'a'*64, 'source_batch_path': '/private/raw-source-path',
            'source_anchors_counted_as_new_runs': False, 'new_anchor_execution': False,
            'cases': [{'case_id': self.case, 'source_run_id': 'private-source-run',
                       'source_run_file_sha256': 'b'*64, 'stage_input_hashes': {'documentation': 'c'*64}}]}
        self.put(self.batch / 'specification.json', spec)
        path = self.batch / self.case / 'attempts.json'
        index = json.loads(path.read_text())
        index['attempts'] = {'documentation__manual': index['attempts']['linked_baseline_anchor']}
        self.put(path, index)
        result = export(self.batch, self.public, self.maps)
        self.assertEqual(result['summary']['planned_attempts'], 1)
        self.assertFalse(result['summary']['source_anchors_counted_as_new_runs'])
        manifest = json.loads((self.public / 'manifest.json').read_text())
        self.assertEqual(manifest['source_anchor_reuse']['sources'][0]['study_case'], 'S0001')
        self.assertNotIn('private-source-run', json.dumps(manifest))
        self.assertNotIn('/private/raw-source-path', json.dumps(manifest))

    def test_candidate_rule_and_diagnosis_metadata_remain_development_only(self):
        spec=json.loads((self.batch/'specification.json').read_text())
        spec.update(diagnosis_catalog_hash='9'*64,diagnosis_code_systems=['ICD-10-CM'],
            execution_policy={'workers':3,'dispatch_unit':'one_case_with_sequential_conditions',
                'formal_observation_window_seconds':None,'private_path':'/private/SHOULD_NOT_EXPORT'})
        spec['rules']={'engine':'documented-candidate','version':'dev-v1','engineering_only':False,
            'development_only':True,'independent_reference_status':'pending','registry_hash':'8'*64,
            'product_mapping_manifest_hash':'7'*64,'source_catalogue_file_sha256':'6'*64,'source_catalogue_hash':'5'*64,
            'source_path':'/private/SHOULD_NOT_EXPORT','shared_context_policy':{'version':'literal-dev-v1',
                'reads_hidden_patient_or_evaluator_data':False,'clinical_reference_status':'independent_review_pending',
                'api_key':'DO_NOT_EXPORT'}}
        self.put(self.batch/'specification.json',spec)
        export(self.batch,self.public,self.maps)
        projected=json.loads((self.public/'manifest.json').read_text())['specification']
        self.assertFalse(projected['formal_experiment'])
        self.assertFalse(projected['rules']['engineering_only'])
        self.assertTrue(projected['rules']['development_only'])
        self.assertEqual(projected['rules']['independent_reference_status'],'pending')
        self.assertEqual(projected['rules']['registry_hash'],'8'*64)
        self.assertEqual(projected['diagnosis_catalog_hash'],'9'*64)
        self.assertIsNone(projected['execution_policy']['formal_observation_window_seconds'])
        self.assertNotIn('SHOULD_NOT_EXPORT',json.dumps(projected))
        self.assertNotIn('DO_NOT_EXPORT',json.dumps(projected))

    def test_parsing_audit_exports_only_hashes_bounded_operations_and_error_positions(self):
        run=json.loads(self.run_path.read_text())
        run['calls'][0]['response_parse']={'policy':POLICY,'finish_reason':'stop',
            'original_text_sha256':'1'*64,'parser_input_sha256':'2'*64,'normalized_text_sha256':'3'*64,
            'normalization_performed':True,'original_text':self.sent,
            'original_parse_error':{'type':'JSONDecodeError','message':self.sent,'line':1,'column':10,'position':9},
            'operations':[{'operation':'append_missing_terminal_closers','characters':'}','count':1,'private_text':self.sent}]}
        self.put(self.run_path,run)
        export(self.batch,self.public,self.maps)
        trace=json.loads((self.public/'traces/S0001-A01.json').read_text())
        parse=trace['model_calls'][0]['response_parse']
        self.assertEqual(parse['original_text_sha256'],'1'*64)
        self.assertEqual(parse['operations'],[{'operation':'append_missing_terminal_closers','count':1,'closer_types':['object']}])
        self.assertNotIn(self.sent,json.dumps(parse))
        self.assertNotIn('message',parse['original_parse_error'])

    def test_literal_whitespace_encoding_exports_counts_without_patient_text(self):
        run=json.loads(self.run_path.read_text())
        run['calls'][0]['response_parse']={'policy':POLICY,'finish_reason':'stop',
            'original_text_sha256':'1'*64,'parser_input_sha256':'2'*64,'normalized_text_sha256':'3'*64,
            'normalization_performed':True,'original_text':self.sent,
            'original_parse_error':{'type':'JSONDecodeError','message':self.sent,'line':4,'column':9,'position':61},
            'operations':[{'operation':'escape_literal_string_whitespace','count':3,'private_text':self.sent}]}
        self.put(self.run_path,run)
        export(self.batch,self.public,self.maps)
        trace=json.loads((self.public/'traces/S0001-A01.json').read_text())
        parse=trace['model_calls'][0]['response_parse']
        self.assertEqual(parse['operations'],[{'operation':'escape_literal_string_whitespace','count':3,
            'encoding_only':['LF','CR','TAB']}])
        self.assertFalse(parse['policy']['decoded_string_characters_changed'])
        self.assertNotIn(self.sent,json.dumps(parse))
        self.assertNotIn('message',parse['original_parse_error'])

    def test_encoding_export_rejects_content_changes_and_keeps_legacy_support(self):
        from copy import deepcopy
        from tools.export_linked_appendix import safe_response_parse
        from sandbox.experiments.linked.json_boundary import LEGACY_POLICY
        value={'policy':deepcopy(POLICY),'finish_reason':'stop','original_text_sha256':'1'*64,
            'normalized_text_sha256':'2'*64,'normalization_performed':True,
            'operations':[{'operation':'escape_literal_string_whitespace','count':1}]}
        changed=deepcopy(value);changed['policy']['decoded_string_characters_changed']=True
        with self.assertRaises(ValueError):safe_response_parse(changed)
        changed=deepcopy(value);changed['operations'][0]['characters']=self.sent
        with self.assertRaises(ValueError):safe_response_parse(changed)
        changed=deepcopy(value);changed['policy']=LEGACY_POLICY
        with self.assertRaises(ValueError):safe_response_parse(changed)
        changed['operations']=[{'operation':'append_missing_terminal_closers','characters':'}','count':1}]
        self.assertEqual(safe_response_parse(changed)['policy']['version'],'terminal_json_delimiters_v1')

    def test_decision_note_and_projection_export_only_safe_states_counts_and_hashes(self):
        spec=json.loads((self.batch/'specification.json').read_text())
        spec['clinical_disposition_policy']={**CLINICAL_DISPOSITION_POLICY,'private_reason':self.sent}
        spec['history_access_policy']={'clinical_projection':{'version':'clinical_field_projection_v1',
            'clinical_values_changed':False,'context_budget_changed':False,'private_text':self.sent}}
        self.put(self.batch/'specification.json',spec)
        path=self.run_path.with_name('appendix_trace.json');trace=json.loads(path.read_text())
        trace['clinical_disposition_policy']=spec['clinical_disposition_policy']
        trace['clinical_disposition']={'status':'incomplete_clinical_decision',
            'clinical_appropriateness':'pending_independent_review','three_task_eligible':False,
            'drug_settlement_attempted':False,'reason':self.sent}
        trace['disposition_documentation_outcome']={'status':'attested','input_hash':'1'*64,'note_hash':'2'*64,
            'qualifies_complete_three_task_chain':False,'drug_settlement_attempted':False,
            'clinical_quality':'pending_independent_review','failure_reason':self.sent}
        trace['events'].append({'event':'history_lookup','section':'historical_observations',
            'result_status':'already_visible','raw_page_hash':'3'*64,'projected_page_hash':'4'*64,
            'projection_version':'clinical_field_projection_v1','raw_page_bytes':20000,'projected_page_bytes':5000,
            'retained_context_bytes':10000,'remaining_visible_bytes':48976,'returned_count':2,
            'matching_count':2,'next_offset':None,'accepted_into_context':True,
            'already_visible_record_refs':['private-chart-id-1','private-chart-id-2'],'new_record_refs':[],
            'query':self.sent,'records':[self.sent]})
        self.put(path,trace)
        result=export(self.batch,self.public,self.maps)
        self.assertEqual(result['summary']['core_native_complete_encounters'],0)
        self.assertEqual(result['summary']['disposition_documentation_status_counts'],{'attested':1})
        self.assertFalse(result['summary']['decision_note_completion_qualifies_as_full_three_task_completion'])
        public=json.loads((self.public/'traces/S0001-A01.json').read_text())
        event=public['events'][-1]
        self.assertEqual(event['already_visible_record_count'],2)
        self.assertEqual(event['raw_page_hash'],'3'*64)
        self.assertEqual(public['disposition_documentation_outcome']['note_hash'],'2'*64)
        self.assertEqual(public['clinical_disposition_metadata_scope'],'original_encounter_clinical_termination')
        raw='\n'.join(p.read_text() for p in self.public.rglob('*') if p.is_file())
        for forbidden in (self.sent,'private-chart-id','private_reason','"failure_reason":'):
            self.assertNotIn(forbidden,raw)
        # Token-shaped private data cannot sneak into newly selected enums/hashes.
        for malicious in ({'clinical_disposition':{'status':'PRIVATE_SECRET'}},
            {'events':[{'event':'history_lookup','raw_page_hash':'PRIVATE_SECRET'}]},
            {'events':[{'event':'history_lookup','raw_page_bytes':-1}]}):
            with self.assertRaises(ValueError):safe_trace_metadata(malicious)

    def test_supplemental_history_export_accepts_versioned_metadata_only(self):
        event={'event':'history_lookup','section':'historical_administrations',
            'projection_version':'clinical_field_projection_v2_supplemental_history',
            'result_status':'invalid_request','returned_count':0,'records':[self.sent],
            'query':self.sent}
        source=self.root/'supplemental-trace.json'
        self.put(source,{'events':[event]})
        result=safe_trace_metadata(project(source,TRACE))
        self.assertEqual(result['events'][0]['section'],'historical_administrations')
        self.assertEqual(result['events'][0]['result_status'],'invalid_request')
        self.assertNotIn(self.sent,json.dumps(result))
        event['projection_version']='unreviewed_future_version'
        with self.assertRaises(ValueError):safe_trace_metadata({'events':[event]})

    def test_fixed_I3_disposition_source_is_not_a_new_linked_clinical_termination(self):
        value={'evaluation_unit':'fixed_input_task','native_complete':False,
            'clinical_disposition':{'status':'incomplete_clinical_decision',
                'clinical_appropriateness':'pending_independent_review','three_task_eligible':False,
                'drug_settlement_attempted':False},'disposition_documentation_outcome':{}}
        result=safe_trace_metadata(value)
        self.assertEqual(result['clinical_disposition_metadata_scope'],'source_context_for_fixed_input_task')


if __name__ == "__main__":
    unittest.main()
