import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from sandbox.experiments.linked.batch_observation import observed_batch, deployment_metadata, capture_summary
from sandbox.experiments.linked.native_observation import IsolatedObservationDB, ObservationUnknown


class BatchObservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.deploy=self.root/'isolated';self.deploy.mkdir()
        for name in ('manage.py','compose.yaml','observation-v1.sql'):
            (self.deploy/name).write_text('# fixture')
        self.args=SimpleNamespace(capture_native=True,observation_deploy=self.deploy,batch='fixture',
            observation_metadata=deployment_metadata(self.deploy))

    def factory(self, *, unresolved=False):
        parent=self
        class Capture:
            def __init__(self,folder,db):
                self.root=folder;folder.mkdir();self.meta_path=folder/'observation_session.json';self.log_rows=[]
            def start(self,policy):
                self.meta_path.write_text(json.dumps({'status':'active','policy':policy}))
                parent.assertFalse(policy['formal_window_frozen'])
            def close(self):
                if unresolved:raise ObservationUnknown('uncertain write')
                self.meta_path.write_text(json.dumps({'status':'closed','coverage_unchanged':True,'observed_event_count':3}))
            def log(self,event,**kw):self.log_rows.append({'event':event,**kw})
        return Capture

    def test_disabled_capture_has_no_database_or_side_effects(self):
        with patch('sandbox.experiments.linked.batch_observation.IsolatedObservationDB') as db:
            with observed_batch(SimpleNamespace(capture_native=False),self.root) as value:self.assertIsNone(value)
            db.assert_not_called()
        self.assertIsNone(capture_summary(self.root))

    def test_capture_wraps_worker_lifetime_and_does_not_claim_safety(self):
        with patch('sandbox.experiments.linked.batch_observation.IsolatedObservationDB'), patch(
            'sandbox.experiments.linked.batch_observation.CaptureSession',self.factory()):
            with observed_batch(self.args,self.root) as capture:
                self.assertIs(self.args.active_native_capture,capture)
                self.assertEqual(capture_summary(self.root)['status'],'active')
        self.assertIsNone(self.args.active_native_capture)
        self.assertEqual(capture_summary(self.root)['observed_event_count'],3)
        self.assertIsNone(capture_summary(self.root)['whole_system_safe'])

    def test_unresolved_capture_is_preserved_without_automatic_release(self):
        with patch('sandbox.experiments.linked.batch_observation.IsolatedObservationDB'), patch(
            'sandbox.experiments.linked.batch_observation.CaptureSession',self.factory(unresolved=True)):
            with observed_batch(self.args,self.root) as capture:pass
        summary=capture_summary(self.root)
        self.assertEqual(summary['status'],'quarantined_unresolved')
        self.assertFalse(capture.log_rows[-1]['automatic_quarantine_release'])
        self.assertIsNone(summary['formal_correct_completion'])
        self.assertEqual(capture.meta_path.stat().st_mode&0o777,0o600)

    def test_worker_exception_still_closes_healthy_capture(self):
        with patch('sandbox.experiments.linked.batch_observation.IsolatedObservationDB'), patch(
            'sandbox.experiments.linked.batch_observation.CaptureSession',self.factory()):
            with self.assertRaisesRegex(RuntimeError,'worker'):
                with observed_batch(self.args,self.root):raise RuntimeError('worker')
        self.assertEqual(capture_summary(self.root)['status'],'closed')

    def test_changed_deployment_rejected_before_database_creation(self):
        (self.deploy/'observation-v1.sql').write_text('changed')
        with patch('sandbox.experiments.linked.batch_observation.IsolatedObservationDB') as db:
            with self.assertRaisesRegex(ValueError,'changed after preflight'):
                with observed_batch(self.args,self.root):pass
            db.assert_not_called()

    def test_observer_refuses_a_nonisolated_compose_project(self):
        (self.deploy/'manage.py').write_text("COMPOSE=['docker','compose','-p','production','-f','anything']\n")
        with self.assertRaisesRegex(ObservationUnknown,'isolated twinhospital-exp'):
            IsolatedObservationDB(self.deploy)


if __name__=='__main__':unittest.main()
