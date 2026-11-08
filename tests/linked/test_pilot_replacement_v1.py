import sqlite3
import tempfile
from pathlib import Path
import unittest

from test_mimic_cohort import fixture_zip
from sandbox.experiments.linked.case_preparation.mimic_cohort import extract_private_cache
from sandbox.experiments.linked.case_preparation.pilot_replacement_v1 import adaptable, extract_fast


class ReplacementTests(unittest.TestCase):
    def test_route_and_schedule_gate_not_ingredient_only(self):
        row={"drug":"Vancomycin Oral Liquid","drug_type":"MAIN","route":"PO","dose_val_rx":"125","dose_unit_rx":"mg",
             "prod_strength":"125mg Oral Syringe","doses_per_24_hrs":"4"}
        self.assertFalse(adaptable(row,"pneumonia"))
        row["drug"]="Doxycycline Hyclate";self.assertTrue(adaptable(row,"pneumonia"))
        row["route"]="PO/NG";self.assertFalse(adaptable(row,"pneumonia"))
        row["route"]="PO";row["doses_per_24_hrs"]="0.5";self.assertFalse(adaptable(row,"pneumonia"))

    def test_fast_projection_preserves_exact_source_rows_and_fences(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);source=p/"fixture.zip";fixture_zip(source)
            a=p/"a";b=p/"b";a.mkdir();b.mkdir()
            selected=[{"subject_id":"FICTIONAL-P1","hadm_id":"FICTIONAL-H1","group":"type2_diabetes"}]
            pa=extract_private_cache(source,a,selected);pb=extract_fast(source,b,selected)
            with sqlite3.connect(pa) as da,sqlite3.connect(pb) as db:
                for table in ("rows","dictionary"):
                    left=sorted(da.execute("SELECT * FROM "+table).fetchall())
                    right=sorted(db.execute("SELECT * FROM "+table).fetchall())
                    self.assertEqual(left,right)


if __name__=="__main__":unittest.main()
