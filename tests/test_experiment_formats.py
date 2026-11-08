import tempfile
import unittest
from pathlib import Path


class FormatTests(unittest.TestCase):
    def test_real_generated_case_contract_and_numeric_id_rejection(self):
        from sandbox.experiments.data import generate_demo, read_jsonl, write_jsonl
        from sandbox.experiments.formats import validate_format
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            generate_demo(root / "data")
            source = root / "data/cases.jsonl"
            self.assertTrue(validate_format(source, "case.json")["valid"])
            rows = read_jsonl(source)
            rows[0]["case_id"] = 9007199254740993
            write_jsonl(root / "bad.jsonl", rows)
            self.assertFalse(validate_format(root / "bad.jsonl", "case.json")["valid"])

    def test_remote_schema_or_path_traversal_is_not_allowed(self):
        from sandbox.experiments.formats import validate_format
        with self.assertRaises(ValueError):
            validate_format("unused", "../schema.json")
