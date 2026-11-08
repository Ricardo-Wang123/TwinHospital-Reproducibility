"""Small wholly invented source records exercise matching and temporal fences."""
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from sandbox.experiments.linked.case_preparation.mimic_cohort import (
    assign_patients, code_group, extract_private_cache, make_packages, GROUPS,
)


def fixture_zip(path):
    tables = {
        "patients": [{"subject_id": "FICTIONAL-P1", "gender": "F", "anchor_age": "55", "anchor_year": "2100", "dod": ""}],
        "admissions": [{"subject_id": "FICTIONAL-P1", "hadm_id": "FICTIONAL-H1", "admittime": "2100-01-01 00:00:00", "dischtime": "2100-01-04 00:00:00", "deathtime": "", "hospital_expire_flag": "0", "admission_type": "FICTIONAL"}],
        "diagnoses_icd": [{"subject_id": "FICTIONAL-P1", "hadm_id": "FICTIONAL-H1", "icd_code": "E119", "icd_version": "10", "seq_num": "1"}],
        "d_icd_diagnoses": [{"icd_code": "E119", "icd_version": "10", "long_title": "Fixture diabetes code"}],
        "prescriptions": [
            {"subject_id": "FICTIONAL-P1", "hadm_id": "FICTIONAL-H1", "starttime": "2100-01-02 00:00:00", "stoptime": "2100-01-03 00:00:00", "drug": "Fixture medicine", "drug_type": "MAIN", "dose_val_rx": "2-4", "dose_unit_rx": "unit", "route": "TEST", "ndc": "00123"},
            {"subject_id": "FICTIONAL-P1", "hadm_id": "FICTIONAL-H1", "starttime": "2100-01-05 00:00:00", "stoptime": "2100-01-06 00:00:00", "drug": "Future medicine", "drug_type": "MAIN", "dose_val_rx": "1", "dose_unit_rx": "unit", "route": "TEST", "ndc": "00124"}],
        "pharmacy": [{"subject_id": "FICTIONAL-P1", "hadm_id": "FICTIONAL-H1", "starttime": "2100-01-02 00:00:00", "stoptime": "2100-01-03 00:00:00", "frequency": "TEST"}],
        "labevents": [
            {"subject_id": "FICTIONAL-P1", "hadm_id": "FICTIONAL-H1", "itemid": "DEMO", "charttime": "2100-01-02 01:00:00", "storetime": "2100-01-02 02:00:00", "value": "4", "valuenum": "4", "valueuom": "unit"},
            {"subject_id": "FICTIONAL-P1", "hadm_id": "FICTIONAL-H1", "itemid": "DEMO", "charttime": "2100-01-02 01:00:00", "storetime": "2100-01-05 02:00:00", "value": "99", "valuenum": "99", "valueuom": "unit"}],
        "d_labitems": [{"itemid": "DEMO", "label": "Fixture lab", "fluid": "TEST", "category": "TEST"}],
    }
    with zipfile.ZipFile(path, "w") as z:
        for table, rows in tables.items():
            stream = io.StringIO()
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
            z.writestr("hosp/" + table + ".csv", stream.getvalue())


class CohortTests(unittest.TestCase):
    def test_patient_matching_reallocates_shared_candidates(self):
        pools = {g: {} for g in GROUPS}
        pools[GROUPS[0]] = {"A": [], "B": []}
        pools[GROUPS[1]] = {"A": []}
        quotas = {g: 1 if g in GROUPS[:2] else 0 for g in GROUPS}
        result = assign_patients(pools, quotas, b"test-only-key")
        self.assertEqual(result[GROUPS[0]], {"B"})
        self.assertEqual(result[GROUPS[1]], {"A"})

    def test_codes_do_not_silently_broaden_diseases(self):
        self.assertEqual(code_group("10", "E11.9"), "type2_diabetes")
        self.assertIsNone(code_group("10", "E10.9"))
        self.assertIsNone(code_group("9", "25000"))
        self.assertIsNone(code_group("10", "N10"))

    def test_private_extract_preserves_raw_values_and_blocks_future(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "private"
            out.mkdir(mode=0o700)
            source = Path(tmp) / "fictional.zip"
            fixture_zip(source)
            selected = [{"subject_id": "FICTIONAL-P1", "hadm_id": "FICTIONAL-H1", "group": "type2_diabetes", "split": "development"}]
            db = extract_private_cache(source, out, selected)
            summary = make_packages(out, selected, b"fixture-identity-key", db)
            self.assertEqual(summary["converted_cases"], 1)
            self.assertEqual(summary["historical_orders"], 1)
            self.assertEqual(summary["historical_observations"], 1)
            manifest = json.loads((out / "manifest_private.json").read_text())
            linked = json.loads((out / manifest["cases"][0]["path"]).read_text())
            order = linked["his_initial"]["historical_orders"][0]
            self.assertEqual(order["codes_raw"]["ndc"], "00123")
            self.assertEqual(order["regimen_raw"]["dose_val_rx"], "2-4")
            self.assertEqual(linked["his_initial"]["current_use_record"]["items"], [])
            self.assertEqual(linked["data_kind"], "mimic_derived")
            # This local fixture intentionally exercises the restricted-data path.
            self.assertFalse(manifest["external_api_allowed"])
            self.assertEqual((db.stat().st_mode & 0o777), 0o600)
            self.assertNotIn("FICTIONAL-P1", json.dumps(linked))


if __name__ == "__main__":
    unittest.main()
