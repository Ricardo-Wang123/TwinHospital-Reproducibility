from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from sandbox.experiments.linked.cases import build_synthetic_case
from sandbox.experiments.linked.reporting import load_runs, summarize_runs, write_summary, workflow_interval
from sandbox.experiments.linked.review_sources import review_candidates
from sandbox.experiments.linked.rules import EngineeringPPRS, clinical_rule_registry, inject_engineering_fault


def prescription():
    return {"items": [{"drug_code": "SIM-METFORMIN-500", "dose": "500", "dose_unit": "mg",
                       "route": "oral", "frequency": "BID", "quantity": "14", "quantity_unit": "tablet"}]}


def run(case, name="run-one", status="completed"):
    return {"schema_version": "twinhospital.linked.run/1", "run_id": name, "case_id": case["case_id"],
            "config_id": "test:glm", "evaluation_unit": "complete_encounter", "data_kind": "synthetic_independent",
            "status": status, "native_complete": status == "completed", "wall_seconds": 10,
            "error_type": "WorkflowFailure" if status != "completed" else None,
            "artifacts": {"final_handoff": {"text": "患者说口渴。", "facts": [{"fact_id": "invented"}]},
                          "original_prescription": prescription(), "original_prescription_review": {"decision": "approve"},
                          "attested_note": {"content": {"text": "患者说口渴。"}},
                          "transcript": [{"sequence": 1, "speaker_role": "preconsult_ai", "phase": "preconsult_collection", "text": "如何不舒服？"},
                                         {"sequence": 2, "speaker_role": "patient", "phase": "preconsult_collection", "text": "口渴。"},
                                         {"sequence": 3, "speaker_role": "doctor", "phase": "doctor_handoff_verification", "text": "LATER_DOCTOR_SUPPLEMENT"}]},
            "calls": [{"request_id": name, "role": "scribe", "model_name": "glm-test", "model_family": "glm",
                       "status": "completed", "wall_seconds": 8,
                       "usage": {"prompt_tokens": 20, "completion_tokens": 10,
                                 "completion_tokens_details": {"reasoning_tokens": 4}}}],
            "activities": [{"role": "doctor", "active_seconds": 30, "timing_source": "constructed_uncalibrated"}],
            "events": [{"event": "business_operation", "operation": "note.attest",
                        "response": {"status": "executed", "readback_verified": True, "native_effect": True}}]}


class RulesReportingTests(unittest.TestCase):
    def setUp(self):
        self.case = build_synthetic_case("type2_diabetes", 3)
        self.rules = EngineeringPPRS(self.case["formulary"])

    def test_pass_is_not_a_clinical_negative_label(self):
        result = self.rules.review(prescription())
        self.assertEqual(result["decision"], "pass")
        self.assertTrue(result["engineering_only"])
        self.assertEqual(result["clinical_clearance"], "not_assessed")
        self.assertEqual(len(result["clinical_categories_not_validated"]), 6)

    def test_structural_reject_and_repeat_ingredient_review_are_distinct(self):
        for kind in ("nonpositive_dose", "catalogue_route_mismatch"):
            variant = inject_engineering_fault(prescription(), kind)
            self.assertEqual(self.rules.review(variant["prescription"])["decision"], "reject")
            self.assertIsNone(variant["clinical_label"])
        repeated = inject_engineering_fault(prescription(), "repeated_ingredient")
        self.assertEqual(self.rules.review(repeated["prescription"])["decision"], "flag")
        self.assertEqual(len(prescription()["items"]), 1)

    def test_unknown_and_ambiguous_fields_are_not_guessed(self):
        value = prescription()
        value["items"][0]["drug_code"] = "NOT_MAPPED"
        self.assertEqual(self.rules.review(value)["decision"], "flag")
        value["items"][0]["quantity"] = "NaN"
        self.assertEqual(self.rules.review(value)["decision"], "reject")

    def test_explicit_alternate_dose_unit_keeps_numeric_dose_unchanged(self):
        formulary = [{'drug_code': 'SIM-TIOTROPIUM-18', 'dose_unit': 'capsule',
            'allowed_dose_units': ['capsule', 'mcg', 'ug', 'microgram'],
            'quantity_unit': 'capsule', 'route': 'inhaled', 'ingredient': 'tiotropium'}]
        rules = EngineeringPPRS(formulary)
        rx = {'items': [{'drug_code': 'SIM-TIOTROPIUM-18', 'dose': 18, 'dose_unit': 'mcg',
                         'route': 'inhaled', 'frequency': 'QD', 'quantity': 1, 'quantity_unit': 'capsule'}]}
        before = deepcopy(rx)
        self.assertNotIn('unmapped_dose_unit', rules.review(rx)['flags'])
        self.assertEqual(rx, before)
        rx['items'][0]['dose_unit'] = 'mg'
        self.assertIn('unmapped_dose_unit', rules.review(rx)['flags'])

    def test_all_six_candidate_rules_disabled_and_sourced(self):
        registry = clinical_rule_registry()
        self.assertEqual(len({x["category"] for x in registry["candidates"]}), 6)
        sources = {s["source_id"] for s in registry["sources"]}
        for rule in registry["candidates"]:
            self.assertFalse(rule["enabled"])
            self.assertEqual(rule["status"], "pending_independent_review")
            self.assertIn(rule["source_id"], sources)
            self.assertTrue(rule["required_inputs"])

    def test_execution_and_clinical_quality_denominators_stay_separate(self):
        runs = [run(self.case), run(self.case, "failed", "failed")]
        summary = summarize_runs(runs)
        self.assertEqual(summary["attempts"], 2)
        self.assertEqual(summary["distinct_case_ids"], 1)
        self.assertIsNone(summary["distinct_patients"])
        self.assertEqual(summary["complete_encounters_observed"], 1)
        self.assertEqual(summary["model_calls"][0]["completion_tokens"], 20)
        self.assertEqual(summary["model_calls"][0]["reasoning_tokens"], 8)
        self.assertEqual(summary["activity_totals"][0]["modeled_active_seconds"], 60)
        self.assertIsNone(summary["clinical_quality"]["quality_adjusted_effort"])
        with self.assertRaises(ValueError):
            summarize_runs([runs[0], runs[0]])

    def test_missing_usage_and_partial_journal_are_observed(self):
        value = run(self.case)
        value["calls"][0]["usage"] = None
        self.assertEqual(summarize_runs([value])["model_calls"][0]["usage_missing_calls"], 1)
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / "partial"
            directory.mkdir()
            (directory / "events.jsonl").write_text('{"event":"run_started"}\n')
            loaded, issues = load_runs(root)
            self.assertFalse(loaded)
            self.assertEqual(issues[0]["status"], "interrupted_without_final_envelope")
            (directory / "attempts.json").write_text(json.dumps({"case_id": "case", "attempts": {
                "pending": {"status": "not_run_budget_limit", "config_id": "test"}}}))
            loaded, issues = load_runs(root)
            summary = summarize_runs(loaded, incomplete=issues)
            self.assertEqual(summary["attempts"], 0)
            self.assertEqual(summary["unexecuted_planned_attempts"], {"not_run_budget_limit": 1})

    def test_clinical_decisions_remain_in_completion_denominator_without_error_labels(self):
        no_rx = run(self.case, 'no-rx', 'incomplete_clinical_decision')
        no_rx['artifacts'] = {'prescription_eligibility': {'decision': 'no_prescription_issued'}}
        deferred = run(self.case, 'deferred', 'incomplete_clinical_decision')
        deferred['artifacts']['unresolved_prescription_adjudication'] = {'status': 'deferred'}
        deferred['artifacts']['original_prescription_review']['decision'] = 'defer'
        summary = summarize_runs([run(self.case), no_rx, deferred, run(self.case, 'broken', 'failed')])
        self.assertEqual(summary['complete_encounter_attempts'], 4)
        self.assertEqual(summary['complete_encounters_observed'], 1)
        self.assertEqual(summary['failure_types'], {'WorkflowFailure': 1})
        self.assertEqual(summary['incomplete_clinical_decisions']['counts'], {
            'no_prescription_issued': 1, 'prescription_adjudication_deferred': 1})
        self.assertEqual(summary['configurations'][0]['completion_rate'], 0.25)
        self.assertEqual(summary['configurations'][0]['incomplete_clinical_decisions'], 2)
        self.assertEqual(summary['clinical_quality']['status'], 'not_assessed_outside_programmatic_scope')


    def test_inherited_handoff_is_preserved_as_input_not_scored_as_new_branch_output(self):
        value=run(self.case)
        value.update(evaluation_unit='fixed_input_task',conditions={'prescription':{'workflow':'parallel'}})
        handoff=deepcopy(value['artifacts']['final_handoff'])
        value['artifacts']['physician_verified_handoff']=deepcopy(handoff)
        value.setdefault('stage_inputs',{})['prescription']={'clinical_context':{'handoff':handoff}}
        candidates=review_candidates(value)
        self.assertFalse(any(c['task']=='preconsult' for c in candidates))
        rx=next(c for c in candidates if c['task']=='prescription_reference')
        self.assertEqual(rx['source_materials']['handoff'],handoff)
        value['conditions']={'preconsult':{'workflow':'nurse_only'}}
        self.assertTrue(any(c['task']=='preconsult' for c in review_candidates(value)))

    def test_decision_note_keeps_actual_disposition_and_does_not_complete_encounter(self):
        value=run(self.case,'no-rx-with-note','incomplete_clinical_decision')
        disposition={'status':'incomplete_clinical_decision','reason':'Unresolved current evidence',
            'approved_prescription_available':False,'drug_settlement_attempted':False}
        content={'text':'Actual decision note, no prescription issued.'}
        value['artifacts']={'prescription_eligibility':{'decision':'no_prescription_issued'},
            'clinical_disposition':disposition,
            'disposition_documentation_outcome':{'status':'attested'},
            'note_versions':[{'version':1,'content':content,'reason':'initial_draft'}],
            'attested_note':{'version':1,'content':content,'status':'locked'}}
        value['calls']=[{'sequence':1,'status':'completed','observation':{'action':'document',
            'clinical_disposition':disposition,'approved_prescription':{'items':[]},'is_final_prefix':True},
            'response':{'content':content}}]
        candidate=review_candidates(value)[0]
        self.assertIn('attested_final',candidate['assessment_stages'])
        self.assertIn('clinical_disposition_note',candidate['assessment_stages'])
        self.assertEqual(candidate['source_materials']['clinical_disposition'],disposition)
        summary=summarize_runs([value])
        self.assertEqual(summary['complete_encounters_observed'],0)
        self.assertEqual(summary['native_complete_encounters'],0)
        self.assertEqual(summary['clinical_disposition_documentation']['outcome_counts'],{'attested':1})
        self.assertIsNone(summary['clinical_disposition_documentation']['programmatic_qualification_denominator'])

    def test_failed_unsaved_decision_draft_is_reviewable_and_budget_does_not_erase_no_rx(self):
        value=run(self.case,'no-rx-unsaved','stopped_budget')
        value['error_type']='BudgetExceeded'
        disposition={'status':'incomplete_clinical_decision','reason':'Actual uncertainty'}
        value['artifacts']={'prescription_eligibility':{'decision':'no_prescription_issued'},
            'clinical_disposition':disposition,
            'disposition_documentation_outcome':{'status':'stopped_budget','error_type':'BudgetExceeded'}}
        value['calls']=[{'sequence':1,'status':'completed','observation':{'action':'document',
            'clinical_disposition':disposition,'is_final_prefix':True},'response':{'content':{'text':'Unsaved actual draft'}}}]
        candidates=review_candidates(value)
        self.assertEqual(len(candidates),1)
        self.assertIn('unsaved_generated_draft',candidates[0]['assessment_stages'])
        self.assertNotIn('attested_final',candidates[0]['assessment_stages'])
        self.assertEqual(candidates[0]['source_materials']['clinical_disposition'],disposition)
        summary=summarize_runs([value])
        self.assertEqual(summary['incomplete_clinical_decisions']['counts'],{'no_prescription_issued':1})
        self.assertEqual(summary['failure_types'],{'BudgetExceeded':1})
        self.assertEqual(summary['clinical_disposition_documentation']['failure_types'],{'BudgetExceeded':1})
        self.assertEqual(summary['complete_encounters_observed'],0)
        self.assertIsNone(summary['clinical_quality']['qualification_denominator'])

    def test_report_exports_only_aggregate_execution(self):
        summary = summarize_runs([run(self.case)], budget_summary={"cap_cny": 1000, "accounted_upper_cny": 1.2})
        with tempfile.TemporaryDirectory() as root:
            paths = write_summary(summary, root)
            page = Path(paths["html"]).read_text()
            self.assertNotIn("患者说口渴", page)
            self.assertIn("完成情况按实际交付及程序核验报告", page)
            self.assertIn("glm-test", page)
            self.assertIn(r"\begin{table*}", Path(paths["latex"]).read_text())

    def test_saved_versions_use_actual_prefix_sources_not_final_chart_or_signed_order(self):
        value = run(self.case, 'versioned', 'failed')
        initial = {'text': 'early draft'}
        corrected = {'text': 'corrected early draft'}
        final = {'text': 'final draft with signed prescription'}
        early = {'action': 'document', 'transcript': [{'sequence': 1, 'speaker_role': 'patient', 'text': 'EARLY_FACT'}],
            'hospital_history': {'records': ['EARLY_CHART']}, 'is_final_prefix': False,
            'consultation': None, 'approved_prescription': None, 'observed_business_state': {'prescription': None},
            'current_encounter_measurements': None}
        late = {'action': 'document', 'transcript': [{'sequence': 1, 'speaker_role': 'patient', 'text': 'EARLY_FACT'},
            {'sequence': 2, 'speaker_role': 'doctor', 'text': 'LATER_FACT'}],
            'hospital_history': {'records': ['EARLY_CHART', 'LATER_CHART']}, 'is_final_prefix': True,
            'consultation': {'plan': 'LATER_PLAN'}, 'approved_prescription': prescription(),
            'observed_business_state': {'prescription': {'status': 'signed', 'readback_verified': True}},
            'current_encounter_measurements': {'records': ['LATER_MEASUREMENT']}}
        value['calls'] = [
            {'sequence': 1, 'observation': early, 'response': {'action': 'draft', 'content': initial}},
            {'sequence': 2, 'observation': {**early, 'action': 'review_note'},
             'response': {'decision': 'revise', 'content': corrected}},
            {'sequence': 3, 'observation': late, 'response': {'action': 'draft', 'content': final}}]
        value['artifacts'].pop('attested_note')
        value['artifacts']['note_versions'] = [
            {'version': 1, 'content': initial, 'reason': 'initial_draft'},
            {'version': 2, 'content': corrected, 'reason': 'physician_revision'},
            {'version': 3, 'content': final, 'reason': 'incremental_generation'}]
        candidates = [c for c in review_candidates(value) if c['task'] == 'documentation']
        self.assertEqual(len(candidates), 3)
        self.assertIn('initial_draft', candidates[0]['assessment_stages'])
        self.assertIn('failed_run_last_saved_note', candidates[-1]['assessment_stages'])
        for candidate in candidates[:2]:
            text = json.dumps(candidate['source_materials'])
            self.assertNotIn('LATER_', text)
            self.assertIsNone(candidate['source_materials']['observed_business_state']['prescription'])
        self.assertEqual(candidates[-1]['source_materials']['approved_prescription'], prescription())
        self.assertEqual(candidates[-1]['source_materials']['observed_business_state']['prescription']['status'], 'signed')
        # Without actual generation evidence an early draft must stay unresolved.
        value['calls'] = []
        unresolved = [c for c in review_candidates(value) if c['task'] == 'documentation']
        self.assertTrue(all(c['source_materials']['evidence_scope'] == 'generation_sources_unavailable' for c in unresolved))


    def test_queue_to_paid_interval_excludes_registration_and_does_not_certify_safety(self):
        value = run(self.case)
        value['wall_seconds'] = 800
        def event(op, second, data=None, verified=True):
            return {'event': 'business_operation', 'operation': op, 'sequence': second,
                'occurred_at': '2026-11-07T10:00:'+str(second).zfill(2)+'+00:00',
                'response': {'status': 'executed', 'readback_verified': verified, 'data': data or {}}}
        value['events'] = [event('registration.create', 1), event('queue.enter', 10),
                           event('payment.commit', 40, {'status': 'paid'})]
        interval = workflow_interval(value)
        self.assertEqual(interval['observed_seconds'], 30)
        self.assertFalse(interval['registration_included'])
        self.assertIsNone(interval['formal_latency_eligible'])
        summary = summarize_runs([value])
        self.assertEqual(summary['workflow_timing']['observed_interval_count'], 1)
        self.assertIsNone(summary['workflow_timing']['formal_correct_completion_mean_seconds'])
        self.assertIsNone(summary['workflow_timing']['formal_unsafe_execution_rate'])
        value['events'][-1]['response']['readback_verified'] = False
        self.assertIsNone(workflow_interval(value)['observed_seconds'])


if __name__ == "__main__":
    unittest.main()
