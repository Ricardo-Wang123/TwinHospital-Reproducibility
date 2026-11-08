import csv
import gzip
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

ROOT=Path(__file__).resolve().parents[2]
HERE=ROOT/'deploy/openhis-experiment'
sys.path.insert(0,str(HERE))
spec=importlib.util.spec_from_file_location('mimic_diagnosis_builder',HERE/'build_diagnoses_mimic_v2.py')
builder=importlib.util.module_from_spec(spec);spec.loader.exec_module(builder)


class SourceDiagnosisBuilderTests(unittest.TestCase):
    def archive(self,directory,extra=()):
        stream=io.StringIO();writer=csv.writer(stream)
        writer.writerow(['icd_code','icd_version','long_title'])
        for code in ['E119','I509','J449','J189','N390','Z09','Z760','N183','R05']:
            writer.writerow([code,10,'Unit fixture description '+code])
        writer.writerow(['E119',9,'Different version is not a replacement'])
        for row in extra:writer.writerow(row)
        p=Path(directory)/'fictional_dictionary_only.zip'
        with zipfile.ZipFile(p,'w') as z:
            z.writestr('mimic-iv-2.2/hosp/d_icd_diagnoses.csv.gz',gzip.compress(stream.getvalue().encode()))
        return p

    def test_only_exact_source_terminal_codes_added_and_year_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            r=builder.build(self.archive(tmp),output_dir=Path(tmp)/'out')
            m=json.loads((Path(tmp)/'out/diagnosis_mapping_v2.json').read_text())
            self.assertEqual(len(m),9)
            self.assertIn('N18.3',m);self.assertNotIn('N18.30',m)
            self.assertEqual(m['E11.9']['display'],'Unit fixture description E119')
            self.assertEqual(m['E11.9']['source']['code_year'],'not_independently_verified')
            self.assertFalse(m['E11.9']['current_national_leaf_verified'])
            self.assertTrue(all(p['substitution_performed'] is False for p in r['pending']))
            self.assertTrue(r['no_icd9_conversion'])

    def test_parent_with_descendant_is_pending_not_leaf(self):
        with tempfile.TemporaryDirectory() as tmp:
            r=builder.build(self.archive(tmp,[('R059',10,'Unit descendant')]),output_dir=Path(tmp)/'out')
            pending={p['code']:p for p in r['pending']}
            self.assertEqual(pending['R05']['reason'],'source_dictionary_has_descendant_codes')
            m=json.loads((Path(tmp)/'out/diagnosis_mapping_v2.json').read_text())
            self.assertNotIn('R05',m);self.assertIn('R05.9',m)

    def test_duplicate_same_version_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                builder.read_dictionary(self.archive(tmp,[('E119',10,'conflicting description')]))


if __name__=='__main__':unittest.main()
