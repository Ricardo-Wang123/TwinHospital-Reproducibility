import copy
import unittest

from sandbox.experiments.linked.cases import (
    build_synthetic_case, synthetic_cases, runtime_visible_case,
    validate_case, validate_patient_splits, GROUPS,
)


class CaseBoundaryTests(unittest.TestCase):
    def test_independent_and_nonoverlapping_synthetic_partitions(self):
        dev = synthetic_cases(100)
        engineering = synthetic_cases(1, split="engineering", start_index=1000)
        self.assertEqual(len(dev), 500)
        self.assertTrue(validate_patient_splits(dev + engineering))
        self.assertEqual({c["group"] for c in dev}, set(GROUPS))
        for case in dev:
            self.assertFalse(case["provenance"]["uses_patient_records"])
            self.assertEqual(case["evaluation"]["clinical_reference_status"], "not_assessed_outside_programmatic_scope")

    def test_role_input_does_not_contain_assessment_or_provenance(self):
        case = build_synthetic_case("copd", 0)
        view = runtime_visible_case(case)
        self.assertNotIn("evaluation", view)
        self.assertNotIn("provenance", view)
        self.assertNotIn("patient_facts", view["his_initial"])
        view["patient_initial"]["patient_facts"][0]["value"] = "changed"
        self.assertNotEqual(view["patient_initial"], case["patient_initial"])

    def test_renaming_real_data_is_rejected(self):
        case = build_synthetic_case("uti", 0)
        case["provenance"]["uses_patient_records"] = True
        with self.assertRaises(ValueError):
            validate_case(case)

    def test_same_source_patient_cannot_move_between_splits(self):
        first = build_synthetic_case("pneumonia", 1)
        second = build_synthetic_case("pneumonia", 1, split="test")
        second["case_id"] += "-variant"
        with self.assertRaises(ValueError):
            validate_patient_splits([first, second])

    def test_history_does_not_assert_current_medication(self):
        case = build_synthetic_case("type2_diabetes", 0)
        self.assertEqual(case["his_initial"]["current_use_record"]["status"], "not_reconciled")
        self.assertTrue(all(not item["current_use_inferred"] for item in case["his_initial"]["historical_orders"]))
        self.assertEqual(case["his_initial"]["allergy_record"]["status"], "unknown")
        self.assertFalse(case["patient_initial"]["communication_profile"]["automatic_age_memory_penalty"])


if __name__ == "__main__":
    unittest.main()
