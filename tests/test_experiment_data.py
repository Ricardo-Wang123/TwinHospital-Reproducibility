import json
import tempfile
import unittest
from pathlib import Path


class DatasetTests(unittest.TestCase):
    def test_generated_calibration_and_survey_are_importable(self):
        from sandbox.experiments.data import generate_demo
        from sandbox.experiments.__main__ import read_rows
        from sandbox.experiments.research import calibrate, subjective_metrics
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "demo"
            generate_demo(path)
            result = calibrate(read_rows(path / "calibration_events.csv"), read_rows(path / "calibration_catalog.jsonl"))
            self.assertFalse(result["research_eligible"])
            self.assertEqual(result["anchor_cases"], 3)
            self.assertEqual(subjective_metrics(read_rows(path / "subjective.csv"))["n_participants"], 4)

    def test_demo_sizes_and_gold_never_enters_execution_view(self):
        from sandbox.experiments.data import generate_demo, CaseProvider
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "demo"
            generate_demo(path)
            provider = CaseProvider(path)
            cases = provider.execution_cases()
            self.assertEqual(len([c for c in cases if c["task"] == "prescription"]), 12)
            self.assertEqual(len([c for c in cases if c["task"] == "preconsult"]), 10)
            self.assertEqual(len([c for c in cases if c["task"] == "documentation"]), 10)
            self.assertFalse(any("reference" in c or "gold" in c for c in cases))
            self.assertEqual(provider.catalog["research_eligible"], False)

    def test_tampered_input_rejected_before_execution(self):
        from sandbox.experiments.data import generate_demo, CaseProvider
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "demo"
            generate_demo(path)
            target = path / "inputs.jsonl"
            rows = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
            rows[0]["input"]["unexpected"] = True
            target.write_text('\n'.join(json.dumps(row) for row in rows), encoding="utf-8")
            with self.assertRaises(ValueError):
                CaseProvider(path)

    def test_study_demo_config_reports_missing_materials(self):
        from sandbox.experiments.config import default_config, readiness
        config = default_config()
        config["purpose"] = "study"
        report = readiness(config)
        self.assertFalse(report["ready"])
        self.assertTrue(any("calibration" in item["field"] for item in report["missing"]))


if __name__ == "__main__":
    unittest.main()
