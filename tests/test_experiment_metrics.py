"""Offline examples specify denominators, missingness and paired inference."""
import copy
import importlib
import json
import unittest


def metrics_module():
    try:
        return importlib.import_module("sandbox.experiments.metrics")
    except ModuleNotFoundError as exc:
        raise AssertionError("The experiment metrics interface is not implemented") from exc


class MetricsTests(unittest.TestCase):
    def test_workflow_aliases_and_independent_quality_gate(self):
        row = dict(case_id="a", status="completed", final_decision="reject", reference=dict(positive=True, severe=False))
        self.assertEqual(metrics_module().prescription_metrics([row])["recall"], 1)
        result = dict(case_id="a", status="completed", qualified=True, task="documentation", workflow="manual_documentation",
                      encounter_ended_at="2026-09-19T00:00:00Z", physician_attested_at="2026-09-19T00:01:00Z",
                      first_pass_correct=None)
        activity = dict(case_id="a", role="doctor", activity_type="review", active_seconds=10, timing_source="measured")
        summary = metrics_module().effort_metrics([result], [activity])
        self.assertIsNone(summary["human_seconds_per_qualified_task"])
        self.assertEqual(summary["unknown_quality_cases"], 1)
        self.assertEqual(summary["role_active_seconds"], {"physician": 10})
        self.assertFalse(summary["draft_metrics_applicable"])
        self.assertEqual(summary["mean_turnaround_seconds"], 60)
        result["quality_source"] = "independent_review"
        self.assertEqual(metrics_module().effort_metrics([result], [activity])["human_seconds_per_qualified_task"], 10)
    def test_prescription_original_cases_missing_and_zero_denominators(self):
        rows = [
            dict(case_id="p1", status="completed", final_decision="block", reference=dict(positive=True, severe=True)),
            dict(case_id="p2", status="completed", final_decision="approve", reference=dict(positive=True, severe=True)),
            dict(case_id="n1", status="completed", final_decision="block", reference=dict(positive=False, severe=False)),
            dict(case_id="n2", status="failed", final_decision=None, reference=dict(positive=False, severe=False)),
        ]
        original = copy.deepcopy(rows)
        result = metrics_module().prescription_metrics(rows + [copy.deepcopy(rows[0])])
        self.assertEqual(rows, original)
        self.assertEqual(result["n_cases"], 4)
        self.assertEqual(result["duplicate_rows"], 1)
        self.assertEqual(result["missing_decisions"], 1)
        self.assertEqual(result["failed_cases"], 1)
        self.assertEqual(result["recall"], .5)
        self.assertEqual(result["false_positive_rate"], .5)
        self.assertEqual(result["severe_miss_rate"], .5)
        self.assertFalse(result["complete"])
        self.assertIsNone(metrics_module().prescription_metrics([])["precision"])
        conflict = {**rows[0], "final_decision": "approve"}
        with self.assertRaises(ValueError):
            metrics_module().prescription_metrics([rows[0], conflict])

    def test_facts_keep_indeterminate_and_explicit_required_units(self):
        annotations = [{"label": x} for x in ["correct", "correct", "incorrect", "unsupported", "omitted", "indeterminate"]]
        result = metrics_module().fact_metrics(annotations)
        self.assertEqual(result["assessable_claims"], 4)
        self.assertEqual(result["indeterminate_claims"], 1)
        self.assertEqual(result["factual_accuracy"], .5)
        self.assertEqual(result["unsupported_rate"], .25)
        self.assertAlmostEqual(result["omission_rate"], 1 / 6)
        result = metrics_module().fact_metrics([
            {"label": "unsupported", "unit": "claim"},
            {"label": "omitted", "unit": "required_item"},
            {"label": "correct", "unit": "required_item", "applicable": False},
        ])
        self.assertEqual(result["required_items"], 1)
        self.assertEqual(result["omission_rate"], 1)
        self.assertIsNone(metrics_module().fact_metrics([])["factual_accuracy"])

    def test_effort_counts_failures_and_does_not_add_inquiry_twice(self):
        results = [dict(case_id="a", status="completed", qualified=True, quality_source="engineering_script", task="preconsultation", workflow="ai"),
                   dict(case_id="b", status="failed", qualified=False, quality_source="engineering_script", task="preconsultation", workflow="ai")]
        activities = [dict(case_id="a", role="physician", activity_type="review", active_seconds=60, timing_source="measured", activity_id="parent"),
                      dict(case_id="a", role="physician", activity_type="redundant_inquiry", active_seconds=20, timing_source="measured", included_in_activity_id="parent"),
                      dict(case_id="b", role="nurse", activity_type="collection", active_seconds=30, timing_source="measured")]
        result = metrics_module().effort_metrics(results, activities)
        self.assertEqual(result["total_active_seconds"], 90)
        self.assertEqual(result["human_seconds_per_qualified_task"], 90)
        self.assertEqual(result["role_seconds_per_qualified_task"], {"nurse": 30, "physician": 60})
        self.assertEqual(result["redundant_inquiry_seconds_per_case"], 10)
        results[1]["qualified"] = None
        result = metrics_module().effort_metrics(results, activities)
        self.assertEqual(result["unknown_quality_cases"], 1)
        self.assertFalse(result["complete"])
        self.assertIsNone(result["human_seconds_per_qualified_task"])
        results[0]["qualified"] = False
        results[1]["qualified"] = False
        self.assertIsNone(metrics_module().effort_metrics(results, activities)["human_seconds_per_qualified_task"])

    def test_effort_turnaround_first_draft_and_invalid_activity(self):
        rows = [dict(case_id="a", status="completed", qualified=True, quality_source="engineering_script", task="documentation", workflow="postvisit",
                     encounter_ended="2026-09-19T10:00:00+08:00", physician_attested="2026-09-19T02:02:00Z",
                     first_pass_completed=True, first_draft_accepted=False, rework_rounds=1, rework_seconds=30)]
        r = metrics_module().effort_metrics(rows, [])
        self.assertEqual(r["mean_turnaround_seconds"], 120)
        self.assertEqual(r["first_draft_acceptance_rate"], 0)
        self.assertEqual(r["mean_rework_rounds"], 1)
        with self.assertRaises(ValueError):
            metrics_module().effort_metrics(rows, [dict(case_id="a", role="physician", activity_type="review", active_seconds=-1, timing_source="measured")])
        with self.assertRaises(ValueError):
            metrics_module().effort_metrics(rows, [dict(case_id="unknown", role="physician", activity_type="review", active_seconds=1, timing_source="measured")])

    def test_safety_effect_precedence_local_controls_and_independent_e2e_rates(self):
        rows = [dict(category="duplicate_payment", is_error=True, outcome="sandbox_prevention", unsafe_effect=True),
                dict(category="duplicate_payment", is_error=True, outcome="downstream_safe"),
                dict(category="duplicate_payment", is_error=False, outcome="control_false_block"),
                dict(category="duplicate_payment", is_error=False, outcome="control_failed")]
        r = metrics_module().safety_metrics(rows)["by_category"]["duplicate_payment"]
        self.assertEqual(r["unsafe_execution"], 1)
        self.assertEqual(r["sandbox_prevention_rate"], 0)
        self.assertEqual(r["false_block_rate"], .5)
        self.assertEqual(r["control_failed"], 1)
        r = metrics_module().safety_metrics([
            dict(status="correct_completed", unsafe_effect=False, duration_s=10),
            dict(status="correct_completed", unsafe_effect=True, duration_s=3),
            dict(status="timeout", unsafe_effect=True, duration_s=99)], end_to_end=True)
        self.assertEqual(r["correct_completion_rate"], 1 / 3)
        self.assertEqual(r["unsafe_execution_rate"], 2 / 3)
        self.assertEqual(r["timeout_rate"], 1 / 3)
        self.assertEqual(r["mean_completion_seconds"], 10)

    def paired_rows(self):
        rows = []
        for i in range(4):
            for env in ("simulation", "hospital"):
                for config, delta in (("base", 0), ("one", 1), ("two", 1), ("three", 2)):
                    rows.append(dict(case_id=str(i), source_group_id="g" + str(i // 2), task="t", config_id=config,
                                     environment=env, metrics={"score": i + delta}))
        return rows

    def test_e2e_missing_readback_is_unresolved_not_a_safe_completion(self):
        module = metrics_module()
        rows = [dict(status="correct_completed", unsafe_effect=False, duration_s=2, effect_evidence="missing"),
                dict(status="timeout", unsafe_effect=False, duration_s=10, effect_evidence="missing")]
        result = module.safety_metrics(rows, end_to_end=True)
        self.assertEqual(result["unresolved_effect_cases"], 2)
        self.assertFalse(result["effect_observation_complete"])
        self.assertEqual(result["correct_completion_rate"], 0)
        self.assertEqual(result["unsafe_execution_rate"], 0)
        self.assertEqual(result["unsafe_execution_rate_interpretation"], "unknown")
        self.assertEqual(result["unsafe_execution_rate_bounds"], [0, 1])
        self.assertIsNone(result["mean_completion_seconds"])
        rows.append(dict(status="timeout", unsafe_effect=True, duration_s=10))
        result = module.safety_metrics(rows, end_to_end=True)
        self.assertEqual(result["unsafe_execution_rate_interpretation"], "lower_bound")
        self.assertEqual(result["unsafe_execution_rate_bounds"], [1 / 3, 1])
        self.assertEqual(result["timeout_rate"], 2 / 3)
        self.assertEqual(result["observed_effect_cases"], 1)
        empty = module.safety_metrics([], end_to_end=True)
        self.assertFalse(empty["effect_observation_complete"])
        self.assertIsNone(empty["unsafe_execution_rate"])

    def test_paired_group_bootstrap_ties_and_no_unconfigured_fidelity_claim(self):
        rows = self.paired_rows()
        r = metrics_module().paired_comparison(rows, baseline_by_task={"t": "base"}, bootstrap_samples=100)
        m = r["tasks"]["t"]["metrics"]["score"]
        self.assertEqual(m["n_paired_cases"], 4)
        self.assertEqual(m["n_source_groups"], 2)
        self.assertEqual(m["ranking"]["tau_b"], 1)
        self.assertIsNone(m["fidelity_pass"])
        self.assertEqual(m["contrasts"]["one"]["delta_error_ci"], [0, 0])
        tol = {"score": {"max_abs_delta_error": .1, "direction_epsilon": 0, "min_tau_b": .9}}
        r2 = metrics_module().paired_comparison(list(reversed(rows)), baseline_by_task={"t": "base"}, bootstrap_samples=100, tolerances=tol)
        self.assertTrue(r2["tasks"]["t"]["metrics"]["score"]["fidelity_pass"])
        self.assertEqual(r["tasks"]["t"]["metrics"]["score"]["contrasts"], r2["tasks"]["t"]["metrics"]["score"]["contrasts"])
        json.dumps(r2, allow_nan=False)

    def test_paired_missing_panels_and_single_cluster_are_not_equivalence(self):
        rows = self.paired_rows()
        rows[0]["metrics"]["score"] = None
        r = metrics_module().paired_comparison(rows, baseline_by_task={"t": "base"}, bootstrap_samples=50)
        m = r["tasks"]["t"]["metrics"]["score"]
        self.assertEqual(m["excluded_cases"], 1)
        self.assertEqual(m["missing_metric_values"], 1)
        for row in rows:
            row["source_group_id"] = "only"
        m = metrics_module().paired_comparison(rows, baseline_by_task={"t": "base"}, bootstrap_samples=50)["tasks"]["t"]["metrics"]["score"]
        self.assertIsNone(m["contrasts"]["one"]["delta_error_ci"])
        with self.assertRaises(ValueError):
            metrics_module().paired_comparison(rows + [rows[0]], baseline_by_task={"t": "base"})
        rows[0]["metrics"]["score"] = float("nan")
        with self.assertRaises(ValueError):
            metrics_module().paired_comparison(rows, baseline_by_task={"t": "base"})

    def test_bootstrap_resamples_whole_source_groups_not_individual_cases(self):
        rows = []
        for i in range(10):
            for env in ("simulation", "hospital"):
                for config in ("base", "ai"):
                    value = (0 if i < 5 else 10) if config == "ai" and env == "simulation" else 0
                    rows.append(dict(case_id=str(i), source_group_id=str(i // 5), task="t", config_id=config,
                                     environment=env, metrics={"m": value}))
        m = metrics_module().paired_comparison(rows, baseline_by_task={"t": "base"}, bootstrap_samples=1000)["tasks"]["t"]["metrics"]["m"]
        self.assertEqual(m["contrasts"]["ai"]["delta_error"], 5)
        self.assertEqual(m["contrasts"]["ai"]["delta_error_ci"], [0, 10])

    def test_missing_panels_never_produce_unqualified_fidelity_pass(self):
        rows = self.paired_rows()
        rows[0]["metrics"]["score"] = None
        tol = {"score": {"max_abs_delta_error": .1, "direction_epsilon": 0, "min_tau_b": .9}}
        m = metrics_module().paired_comparison(rows, baseline_by_task={"t": "base"}, bootstrap_samples=100, tolerances=tol)["tasks"]["t"]["metrics"]["score"]
        self.assertIsNone(m["fidelity_pass"])

    def test_paired_effort_ratio_recomputes_totals_and_shared_role_denominator(self):
        rows = []
        # Every source block includes a qualified and a failed case. Failure
        # effort must remain in the numerator; blocks have unequal case counts.
        for case, group, qualified, seconds in (("a", "g1", 1, 10), ("b", "g1", 0, 90),
                                                 ("c", "g2", 1, 20)):
            for env in ("simulation", "hospital"):
                for config in ("base", "ai"):
                    scale = 1 if config == "base" else (0.5 if env == "simulation" else 0.75)
                    rows.append(dict(case_id=case, source_group_id=group, task="t", config_id=config,
                        environment=env, metrics={"qualified": qualified, "human_active_seconds": seconds * scale,
                        "physician_active_seconds": seconds * scale / 2, "quality_metric": qualified}))
        r = metrics_module().paired_comparison(rows, baseline_by_task={"t": "base"}, bootstrap_samples=1000)
        metrics = r["tasks"]["t"]["metrics"]
        m = metrics["human_seconds_per_qualified_task"]
        self.assertEqual(m["estimand"], "ratio_of_totals")
        self.assertEqual(m["estimates"]["base"]["simulation"]["value"], 60)
        self.assertEqual(m["contrasts"]["ai"]["simulation_delta"], -30)
        self.assertEqual(m["contrasts"]["ai"]["hospital_delta"], -15)
        self.assertEqual(m["contrasts"]["ai"]["delta_error_ci"], [-25, -5])
        self.assertEqual(metrics["physician_seconds_per_qualified_task"]["contrasts"]["ai"]["delta_error"], -7.5)
        self.assertEqual(metrics["quality_metric"]["contrasts"]["ai"]["delta_error"], 0)
        self.assertEqual(m["coverage"]["expected_observations"], 12)
        self.assertEqual(m["contrasts"]["ai"]["valid_bootstrap_samples"], 1000)
        reverse = metrics_module().paired_comparison(list(reversed(rows)), baseline_by_task={"t": "base"}, bootstrap_samples=1000)
        self.assertEqual(r, reverse)

    def test_ratio_missing_quality_or_timing_is_unknown_not_complete_case_ratio(self):
        for field in ("qualified", "human_active_seconds"):
            with self.subTest(field=field):
                rows = [dict(case_id=str(i), source_group_id=str(i), task="t", config_id=c, environment=e,
                             metrics={"qualified": 1, "human_active_seconds": 10})
                        for i in range(2) for c in ("base", "ai") for e in ("simulation", "hospital")]
                rows[0]["metrics"][field] = None
                m = metrics_module().paired_comparison(rows, baseline_by_task={"t": "base"}, bootstrap_samples=20)["tasks"]["t"]["metrics"]["human_seconds_per_qualified_task"]
                self.assertFalse(m["complete"])
                self.assertEqual(m["n_paired_cases"], 1)
                self.assertEqual(m["bootstrap_status"], "incomplete_coverage")
                self.assertIsNone(m["contrasts"]["ai"]["simulation_delta"])
                self.assertIsNone(m["fidelity_pass"])

    def test_ratio_zero_qualified_resamples_are_counted_not_conditioned_away(self):
        rows = [dict(case_id=str(i), source_group_id=str(i), task="t", config_id=c, environment=e,
                     metrics={"qualified": i, "human_active_seconds": 10 if c == "base" else 5})
                for i in range(2) for c in ("base", "ai") for e in ("simulation", "hospital")]
        m = metrics_module().paired_comparison(rows, baseline_by_task={"t": "base"}, bootstrap_samples=1000)["tasks"]["t"]["metrics"]["human_seconds_per_qualified_task"]
        self.assertEqual(m["contrasts"]["ai"]["simulation_delta"], -10)
        self.assertGreater(m["contrasts"]["ai"]["zero_denominator_samples"], 0)
        self.assertIsNone(m["contrasts"]["ai"]["delta_error_ci"])
        self.assertEqual(m["bootstrap_status"], "zero_denominator_resamples")
        for r in rows:
            r["metrics"]["qualified"] = 0
        m = metrics_module().paired_comparison(rows, baseline_by_task={"t": "base"}, bootstrap_samples=20)["tasks"]["t"]["metrics"]["human_seconds_per_qualified_task"]
        self.assertIsNone(m["estimates"]["base"]["simulation"]["value"])
        self.assertIsNone(m["contrasts"]["ai"]["simulation_delta"])
        self.assertIsNone(m["ranking"]["tau_b"])
        json.dumps(m, allow_nan=False)

    def test_precomputed_per_case_effort_ratios_cannot_replace_sufficient_statistics(self):
        rows = [dict(case_id="a", task="t", config_id="base", environment="simulation",
                     metrics={"human_seconds_per_qualified_task": 10})]
        with self.assertRaisesRegex(ValueError, "ratio.*statistics"):
            metrics_module().paired_comparison(rows, baseline_by_task={"t": "base"}, bootstrap_samples=10)

    def test_effort_does_not_hide_unobserved_timing(self):
        results = [dict(case_id="a", status="completed", qualified=True, task="documentation", workflow="postvisit")]
        r = metrics_module().effort_metrics(results, [])
        self.assertEqual(r["cases_without_activity"], 1)

    def test_safety_observer_alias_and_empty_sample(self):
        row = dict(category="locked_note", is_error=True, outcome="sandbox_prevention", prohibited_effect_observed=True)
        r = metrics_module().safety_metrics([row])["by_category"]["locked_note"]
        self.assertEqual(r["unsafe_execution"], 1)
        self.assertIsNone(metrics_module().safety_metrics([], end_to_end=True)["correct_completion_rate"])


if __name__ == "__main__":
    unittest.main()
