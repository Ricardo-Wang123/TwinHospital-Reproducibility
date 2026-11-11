import argparse
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from handoff import evidence


class PaidEvidenceScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        batch, inputs = self.root / 'batch', self.root / 'inputs'
        batch.mkdir(); inputs.mkdir()
        self.write(batch / 'specification.json', {'test_fixture': True})
        self.write(batch / 'trace.json', {'source': 'independent synthetic packaging fixture'})
        self.write(batch / 'batch_summary.json', {'cases': [{'case_id': 'case-1',
            'attempts': {'baseline': {'status': 'completed', 'result_path': 'trace.json'}}}]})
        self.write(inputs / 'clinical_catalogue.json', {'products': []})
        self.write(inputs / 'case.json', {'case_id': 'case-1', 'data_kind': 'synthetic_independent'})
        self.write(inputs / 'manifest.json', {'cases': [{'case_id': 'case-1', 'path': 'case.json'}]})
        self.write(self.root / 'audit.json', {'test_fixture': True})
        self.write(self.root / 'probes.json', {'test_fixture': True})
        self.args = argparse.Namespace(batch=batch, inputs=inputs,
            audit=self.root / 'audit.json', probes=self.root / 'probes.json',
            human_review=None, output=self.root / 'portable')
        self.metrics = {'verified_paid': 1, 'paid_cohort_sha256': 'fixture-digest'}
        self.patcher = patch.object(evidence, 'calculate', return_value=(
            self.metrics, b'{"analysis_fixture":true}\n', b'% No clinical or human-assessment claim.\n'))
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop(); self.temp.cleanup()

    @staticmethod
    def write(path, value):
        path.write_text(json.dumps(value))

    def pack(self):
        evidence.pack(self.args)
        return json.loads((self.args.output / 'bundle.json').read_bytes())

    def test_agent_review_bundle_round_trip_does_not_create_a_human_record(self):
        receipt = self.pack()
        self.assertEqual(receipt['version'], evidence.AGENT_REVIEW_VERSION)
        self.assertEqual(receipt['assessment_scope'], evidence.AGENT_ASSESSMENT_SCOPE)
        self.assertFalse((self.args.output / 'human_review.json').exists())
        index = json.loads((self.args.output / 'batch/batch_summary.json').read_bytes())
        self.assertEqual(index['cases'][0]['attempts']['1']['result_path'], 'runs/0001.json')
        output = self.root / 'recomputed'
        evidence.recompute(argparse.Namespace(bundle=self.args.output, output=output))
        self.assertEqual((output / 'verified_paid_results.json').read_bytes(), b'{"analysis_fixture":true}\n')

    def test_actual_supplied_human_record_retains_version_one_format(self):
        self.args.human_review = self.root / 'review.json'
        self.write(self.args.human_review, {'synthetic_fixture_only': True})
        receipt = self.pack()
        self.assertEqual(receipt['version'], evidence.VERSION)
        self.assertNotIn('assessment_scope', receipt)
        self.assertEqual((self.args.output / 'human_review.json').read_bytes(), self.args.human_review.read_bytes())

    def test_injected_human_record_cannot_change_an_agent_bundle_scope(self):
        self.pack()
        self.write(self.args.output / 'human_review.json', {'reviewed': 1})
        with self.assertRaisesRegex(ValueError, 'assessment scope'):
            evidence.recompute(argparse.Namespace(bundle=self.args.output, output=self.root / 'bad'))

    def test_old_version_cannot_silently_omit_its_human_record(self):
        receipt = self.pack(); receipt['version'] = evidence.VERSION
        self.write(self.args.output / 'bundle.json', receipt)
        with self.assertRaisesRegex(ValueError, 'assessment scope'):
            evidence.recompute(argparse.Namespace(bundle=self.args.output, output=self.root / 'bad'))

    def test_changed_trace_is_rejected_before_recomputation(self):
        self.pack()
        (self.args.output / 'batch/runs/0001.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            evidence.recompute(argparse.Namespace(bundle=self.args.output, output=self.root / 'bad'))


if __name__ == '__main__':
    unittest.main()
