import importlib.util
from pathlib import Path
import tempfile
import unittest
import zipfile
import json

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('diagnosis_builder', ROOT/'deploy/openhis-experiment/build_diagnoses_v2.py')
builder = importlib.util.module_from_spec(spec); spec.loader.exec_module(builder)


class DiagnosisBuilderTests(unittest.TestCase):
    def archive(self, directory, *, nonleaf=None, missing=None):
        codes = [c.replace('.', '') for group in builder.GROUPS.values() for c in group]
        path = Path(directory)/'unit-fixture-not-official.zip'
        with zipfile.ZipFile(path, 'w') as z:
            z.writestr('icd10cm-codes-2027.txt', '\n'.join(c+' Unit test description '+c for c in codes if c != missing))
            z.writestr('icd10cm_order_2027.txt', '\n'.join(f'{i:05} {c:7} {0 if c == nonleaf else 1} Unit fixture' for i, c in enumerate(codes, 1)))
        return path

    def test_old_native_ids_preserved_and_append_only_sql(self):
        old_path = builder.HERE/'diagnosis_mapping.json'; original = old_path.read_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            manifest = builder.build(self.archive(tmp), output_dir=Path(tmp)/'generated')
            mapping = json.loads((Path(tmp)/'generated/diagnosis_mapping_v2.json').read_text())
            self.assertEqual(manifest['selected_count'], 65)
            self.assertEqual({v['system'] for v in mapping.values()}, {'ICD-10-CM'})
            for code, row in json.loads(original).items():
                self.assertEqual(mapping[code]['conditionDefinitionId'], row['conditionDefinitionId'])
                self.assertTrue(mapping[code]['native_row_reused_without_modification'])
            sql = (Path(tmp)/'generated/experiment-diagnoses-v2.sql').read_text()
            self.assertNotIn('UPDATE ', sql); self.assertNotIn('DELETE ', sql)
            self.assertIn('Experimental diagnosis ID collision', sql)
            self.assertIn('ON CONFLICT(id) DO NOTHING', sql)
        self.assertEqual(old_path.read_bytes(), original)

    def test_missing_or_header_codes_are_not_silently_accepted(self):
        for args in ({'nonleaf': 'N1830'}, {'missing': 'Z760'}):
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(ValueError):
                    builder.build(self.archive(tmp, **args), output_dir=Path(tmp)/'generated')
                self.assertFalse((Path(tmp)/'generated/diagnosis_mapping_v2.json').exists())

    def test_wrong_version_or_unexpected_member_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'wrong.zip'
            with zipfile.ZipFile(p,'w') as z:
                z.writestr('icd10cm-codes-2026.txt','Z09 test')
            with self.assertRaises(ValueError):
                builder.read_archive(p)


if __name__ == '__main__':
    unittest.main()
