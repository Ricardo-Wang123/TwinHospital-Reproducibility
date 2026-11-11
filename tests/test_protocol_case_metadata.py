"""Bind archived, path-free specifications to exact source case bytes."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest

from tools.compare_protocol_runs import verified_case_metadata


class VerifiedCaseMetadataTests(unittest.TestCase):
    def fixture(self, root):
        case = dict(case_id='fictional-1', patient_key='fictional-person-1',
                    group='pneumonia', split='development', data_kind='synthetic')
        payload = json.dumps(case).encode()
        source = root / 'cases/example.json'
        source.parent.mkdir()
        source.write_bytes(payload)
        row = {**case, 'path': 'cases/example.json',
               'file_sha256': sha256(payload).hexdigest(),
               'runtime_input_hash': 'frozen-input'}
        manifest = root / 'manifest.json'
        manifest.write_text(json.dumps({'cases': [row]}))
        frozen = {key: row[key] for key in ('case_id', 'group', 'split',
                  'data_kind', 'file_sha256', 'runtime_input_hash')}
        return {'manifest': 'manifest.json'}, {'cases': [frozen]}, manifest, source

    def test_path_free_spec_recovers_verified_identity_without_mutation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config, spec, manifest, _ = self.fixture(root)
            original = deepcopy(spec)
            # A larger manifest pool must not inflate the selected source count.
            data = json.loads(manifest.read_text())
            data['cases'].append({'case_id': 'unused-pool-row'})
            manifest.write_text(json.dumps(data))
            result = verified_case_metadata(config, root, spec)
            self.assertEqual([r['patient_key'] for r in result], ['fictional-person-1'])
            self.assertEqual(spec, original)
            self.assertNotIn('path', result[0])

    def test_manifest_identity_cannot_override_frozen_source(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config, spec, manifest, _ = self.fixture(root)
            data = json.loads(manifest.read_text())
            data['cases'][0]['patient_key'] = 'different-person'
            manifest.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, 'source identity'):
                verified_case_metadata(config, root, spec)

    def test_modified_case_is_rejected_before_identity_recovery(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config, spec, _, source = self.fixture(root)
            source.write_text(source.read_text() + '\n')
            with self.assertRaisesRegex(ValueError, 'checksum'):
                verified_case_metadata(config, root, spec)

    def test_archived_metadata_cannot_be_changed_by_manifest(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config, spec, manifest, _ = self.fixture(root)
            data = json.loads(manifest.read_text())
            data['cases'][0]['runtime_input_hash'] = 'different-input'
            manifest.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, 'runtime_input_hash'):
                verified_case_metadata(config, root, spec)


if __name__ == '__main__':
    unittest.main()
