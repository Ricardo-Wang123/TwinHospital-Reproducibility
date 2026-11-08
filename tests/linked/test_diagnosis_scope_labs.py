import unittest

from sandbox.experiments.linked.case_preparation.diagnosis_scope_labs import summarize, has_result


def visit(subject, group, ages, *, alive=True, parsed=True):
    return {"subject_id": subject, "priority_1": [group], "any_sequence": [group],
            "survives_next_day": alive, "usable_rx": True, "oral_rx": True, "inhaled_rx": False,
            "group_parsed_rx": [group] if parsed else [], "legacy_history_ok": False,
            "latest_lab_age_hours": ages, "any_valid_timed_lab": bool(ages)}


class ScopeLabCountTests(unittest.TestCase):
    def test_numeric_source_result_is_retained_when_free_text_is_masked(self):
        self.assertTrue(has_result({"value": "___", "valuenum": "137"}))
        self.assertTrue(has_result({"value": "", "valuenum": "0"}))
        self.assertTrue(has_result({"value": "NEGATIVE", "valuenum": ""}))
        self.assertFalse(has_result({"value": "___", "valuenum": ""}))
        self.assertFalse(has_result({"value": "", "valuenum": "NaN"}))

    def test_time_windows_do_not_turn_missing_or_old_results_into_present(self):
        rows = [visit("one", "type2_diabetes", {"creatinine": 168, "glucose": 72}),
                visit("two", "type2_diabetes", {"creatinine": 169, "glucose": 24}),
                visit("three", "type2_diabetes", {"creatinine": 24}),
                visit("four", "type2_diabetes", {"creatinine": 24, "glucose": 24}, alive=False)]
        result = summarize(rows)["any_sequence"]["type2_diabetes"]
        self.assertEqual(result["counts"]["group_keyword_parsed_regimen_and_historical_lab_windows"]["unique_source_patients"], 1)
        self.assertEqual(result["counts"]["has_any_valid_timed_laboratory_record"]["unique_source_patients"], 3)

    def test_no_copd_lab_requirement_is_explicit_not_fabricated_labs(self):
        result = summarize([visit("one", "copd", {})])["any_sequence"]["copd"]
        self.assertTrue(result["no_required_lab_window_for_group"])
        self.assertEqual(result["counts"]["has_any_valid_timed_laboratory_record"]["unique_source_patients"], 0)
        self.assertEqual(result["counts"]["has_all_descriptive_historical_lab_windows"]["unique_source_patients"], 1)


if __name__ == "__main__":
    unittest.main()
