import unittest
from sandbox.experiments.linked.case_preparation.development_pilot50_v3 import select, GROUPS, window_ok


class PilotSelectionTests(unittest.TestCase):
    def row(self, sid, group, hadm=None):
        return {"subject_id": sid, "hadm_id": hadm or sid + "-stay", "any_sequence": [group],
            "survives_next_day": True, "group_parsed_rx": [group],
            "latest_lab_age_hours": {x: 25 for x in ("creatinine", "glucose", "potassium", "wbc", "urinalysis")}}

    def test_excludes_test_before_ranking_and_preserves_dev(self):
        rows = [self.row("fixture-test", GROUPS[0]), self.row("fixture-dev", GROUPS[0])]
        frozen = [{"subject_id": "fixture-test", "split": "test"}, {"subject_id": "fixture-dev", "split": "development"}]
        selected, summary = select(rows, frozen, b"fixture-key", per_group=1)
        self.assertEqual([r["subject_id"] for r in selected], ["fixture-dev"])
        self.assertEqual(selected[0]["previous_assignment"], "existing_development")
        self.assertEqual(summary["overlap_with_test"], 0)

    def test_patient_dedup_across_groups_and_determinism(self):
        rows = [self.row("shared", g) for g in GROUPS]
        rows += [self.row("private-" + g, g) for g in GROUPS]
        a, _ = select(rows, [], b"fixture-key", per_group=1)
        b, _ = select(list(reversed(rows)), [], b"fixture-key", per_group=1)
        self.assertEqual(a, b)
        self.assertEqual(len({x["subject_id"] for x in a}), 5)

    def test_unknown_lab_does_not_become_normal_and_values_unused(self):
        row = self.row("fictional", GROUPS[0]); row["latest_lab_age_hours"].pop("glucose")
        self.assertFalse(window_ok(row, GROUPS[0]))
        self.assertTrue(window_ok(row, "copd"))  # no required lab, not a normality claim


if __name__ == "__main__":
    unittest.main()
