import json
from pathlib import Path
import tempfile
import unittest

from tools.audit_run_isolation import audit, EXPECTED_MEMORY


class RunIsolationAuditTests(unittest.TestCase):
    def batch(self, root, name, *, identity=None, memory=EXPECTED_MEMORY):
        path = Path(root)/name
        native = path/'case'/'condition'/'native'
        native.mkdir(parents=True)
        prefix = identity or name
        (native/'native_state.json').write_text(json.dumps({'bindings':{'current':{
            'run_id':prefix+'-run', 'patient_id':prefix+'-patient', 'encounter_id':prefix+'-visit'}}}))
        (path/'specification.json').write_text(json.dumps({'memory_policy':EXPECTED_MEMORY,'anchor_reuse':None}))
        run = path/'case'/'condition'/'runs'/'run'
        run.mkdir(parents=True)
        (run/'calls.jsonl').write_text(json.dumps({'memory_policy':memory})+'\n')
        return path

    def test_fresh_business_objects_pass_and_report_does_not_expose_ids(self):
        with tempfile.TemporaryDirectory() as root:
            a, b = self.batch(root, 'first'), self.batch(root, 'second')
            report = audit([a,b])
            self.assertTrue(report['observed_local_identity_isolation_passed'])
            self.assertTrue(report['declared_call_memory_policy_passed'])
            self.assertNotIn('first-patient', json.dumps(report))
            self.assertNotIn('second-visit', json.dumps(report))

    def test_reused_native_identity_is_detected_even_if_new_batch_name(self):
        with tempfile.TemporaryDirectory() as root:
            a = self.batch(root, 'first', identity='shared')
            b = self.batch(root, 'second', identity='shared')
            report = audit([a,b])
            self.assertFalse(report['observed_local_identity_isolation_passed'])
            self.assertEqual(report['cross_batch_comparisons'][0]['overlap_counts'],
                             {'run_id':1,'patient_id':1,'encounter_id':1})

    def test_unrecorded_memory_policy_cannot_be_reported_as_verified(self):
        with tempfile.TemporaryDirectory() as root:
            path = self.batch(root, 'first', memory=None)
            self.assertFalse(audit([path])['declared_call_memory_policy_passed'])


if __name__ == '__main__':
    unittest.main()
