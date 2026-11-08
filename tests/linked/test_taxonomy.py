import copy
import json
import tempfile
import unittest
from pathlib import Path

from sandbox.experiments.linked.case_preparation.taxonomy import (
    audit, classify_case, classify_source_code, registry,
)
from sandbox.experiments.linked.medication_catalogue import get_catalogue


def source_case(code, version, group, cid="synthetic-fixture"):
    return {"case_id": cid, "patient_key": "opaque-" + cid, "group": group, "split": "development",
            "his_initial": {"historical_encounters": [{"role": "anchor_historical_stay", "diagnoses": [{
                "record_id": "diagnosis-1", "source_sequence": 1, "display": "test fixture",
                "coding": {"system": "ICD", "version": version, "code": code},
                "source_ref": "raw-identity-must-not-escape"}]}]},
            "evaluation": {"hidden": "MUST_NOT_ESCAPE"}, "provenance": {"subject_id": "MUST_NOT_ESCAPE"}}


class TaxonomyTests(unittest.TestCase):
    def test_five_frozen_groups_map_to_four_systems(self):
        fixtures = [("10", "E11.65", "type2_diabetes", "endocrine_metabolic"),
                    ("9", "428.22", "heart_failure", "circulatory"),
                    ("10", "J44.1", "copd", "respiratory"),
                    ("9", "486", "pneumonia", "respiratory"),
                    ("10", "N39.0", "uti", "genitourinary")]
        for version, code, group, system in fixtures:
            with self.subTest(group=group):
                found = classify_source_code(version, code)
                self.assertEqual((found["source_subtype"], found["source_system"]), (group, system))

    def test_versions_and_uncertain_legacy_diabetes_are_not_merged(self):
        for version, code in [("9", "E119"), ("10", "4280"), ("9", "25000"),
                              ("9", "25002"), ("9", "25001"), ("10", "E109")]:
            self.assertIsNone(classify_source_code(version, code))
        with self.assertRaises(TypeError):
            classify_source_code("9", 486)

    def test_infection_scope_is_not_expanded_by_name_or_organism(self):
        for code in ("J15212", "B9562", "A4102", "N3000", "N10", "U071", "O85"):
            self.assertIsNone(classify_source_code("10", code))

    def test_anchor_and_group_are_verified_without_mutation_or_hidden_data(self):
        case = source_case("E1165", "10", "type2_diabetes")
        original = copy.deepcopy(case)
        result = classify_case(case)
        self.assertFalse(result["new_outpatient_diagnosis_inferred"])
        self.assertEqual(case, original)
        self.assertNotIn("MUST_NOT_ESCAPE", json.dumps(result))
        self.assertNotIn("raw-identity", json.dumps(result))
        case["group"] = "copd"
        with self.assertRaises(ValueError):
            classify_case(case)

    def test_medication_identity_bindings_do_not_invent_rxnorm_or_regimens(self):
        reg = registry()
        products = {p["drug_code"] for p in get_catalogue()}
        bindings = reg["medication_classification"]["bindings"]
        self.assertEqual(products, {p["catalogue_code"] for p in bindings})
        for item in bindings:
            self.assertIsNone(item["rxnorm_id"])
            self.assertIn("atcddd.fhi.no", item["reference_url"])
            self.assertNotIn("dose", item)

    def test_sidecar_preserves_split_bytes_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            cases = [source_case("J441", "10", "copd", "one"),
                     source_case("J189", "10", "pneumonia", "two")]
            cases[1]["split"] = "test"
            entries = []
            for case in cases:
                (base / (case["case_id"] + ".json")).write_text(json.dumps(case))
                entries.append({**{k: case[k] for k in ("case_id", "patient_key", "group", "split")},
                                "path": case["case_id"] + ".json"})
            (base / "manifest_private.json").write_text(json.dumps({"cases": entries}))
            original = {p.name: p.read_bytes() for p in base.iterdir()}
            summary = audit(base, base / "audit")
            self.assertEqual(summary["source_patients_unique"], 2)
            resp = next(x for x in summary["by_system"] if x["system"] == "respiratory")
            self.assertEqual((resp["development"], resp["test"]), (1, 1))
            self.assertEqual(original, {p.name: p.read_bytes() for p in base.iterdir() if p.is_file()})
            with self.assertRaises(FileExistsError):
                audit(base, base / "audit")


if __name__ == "__main__":
    unittest.main()
