import unittest

from sandbox.experiments.linked.case_preparation.diagnosis_scope_feasibility import summarize


def visit(subject, *, priority=(), diagnoses=(), alive=True, oral=False, inhaled=False, group_drug=(), parsed=(), history=False):
    return {"subject_id": subject, "priority_1": list(priority), "any_sequence": list(diagnoses),
            "survives_next_day": alive, "usable_rx": oral or inhaled, "oral_rx": oral, "inhaled_rx": inhaled,
            "parsed_oral_inhaled_rx": bool(parsed), "group_drug_rx": list(group_drug), "group_parsed_rx": list(parsed),
            "legacy_history_ok": history}


class ScopeCountTests(unittest.TestCase):
    def test_any_sequence_expansion_counts_patients_once_and_preserves_overlap(self):
        rows = [visit("one", priority=("heart_failure",), diagnoses=("heart_failure", "type2_diabetes"), oral=True,
                      group_drug=("heart_failure",), parsed=("heart_failure",), history=True),
                visit("one", priority=("type2_diabetes",), diagnoses=("type2_diabetes",), oral=True),
                visit("two", diagnoses=("type2_diabetes",), oral=True),
                visit("three", diagnoses=("type2_diabetes",), oral=True, alive=False)]
        result = summarize(rows, {"one"})
        primary = result["priority_1"]["groups"]["type2_diabetes"]
        any_seq = result["any_sequence"]["groups"]["type2_diabetes"]
        self.assertEqual(primary["adult_completed_stay_with_diagnosis"]["unique_source_patients"], 1)
        self.assertEqual(any_seq["adult_completed_stay_with_diagnosis"]["unique_source_patients"], 3)
        self.assertEqual(any_seq["same_stay_explicit_oral_prescription"]["unique_source_patients"], 2)
        self.assertEqual(any_seq["same_stay_explicit_oral_prescription"]["source_stays"], 3)
        self.assertEqual(any_seq["same_stay_explicit_oral_prescription"]["patients_not_in_frozen_2500"], 1)
        self.assertEqual(any_seq["same_stay_group_keyword_parsed_regimen"]["unique_source_patients"], 0)
        self.assertEqual(result["any_sequence"]["unique_patients_across_groups_at_first_stage"], 3)

    def test_legacy_history_is_a_separate_branch_not_new_hidden_inclusion_rule(self):
        result = summarize([visit("one", diagnoses=("copd",), inhaled=True, group_drug=("copd",), parsed=("copd",))], set())
        counts = result["any_sequence"]["groups"]["copd"]
        self.assertEqual(counts["same_stay_group_keyword_parsed_regimen"]["unique_source_patients"], 1)
        self.assertEqual(counts["legacy_history_requirement_plus_group_keyword_parsed_regimen"]["unique_source_patients"], 0)


if __name__ == "__main__":
    unittest.main()
