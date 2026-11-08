"""Execution telemetry must retain failed/ambiguous writes and exact clock boundaries."""
import unittest
from test_linked_clinical import example_case, FakeFactory, FakePort, FakeRules
from sandbox.experiments.linked.clinical import LinkedEncounterRunner


class WorkflowTraceTests(unittest.TestCase):
    def test_complete_trace_has_paired_intents_and_one_queue_clock_boundary(self):
        run=LinkedEncounterRunner(FakeFactory(),FakePort(),FakeRules()).run_case(example_case(),run_id='clock-proof')
        self.assertEqual(run['status'],'completed')
        events=run['events'];clock=run['workflow_clock_id']
        self.assertTrue(all(e['clock_id']==clock for e in events))
        times=[e['elapsed_seconds'] for e in events]
        self.assertEqual(times,sorted(times));self.assertTrue(all(t>=0 for t in times))
        starts={e['request_id']:e for e in events if e['event']=='business_request_intent'}
        ends={e['request_id']:e for e in events if e['event']=='business_request_outcome'}
        self.assertEqual(set(starts),set(ends))
        self.assertTrue(all(starts[k]['elapsed_seconds']<=ends[k]['elapsed_seconds'] for k in starts))
        boundaries=[e for e in events if e.get('clock_boundary')=='queue_entry_persisted']
        self.assertEqual(len(boundaries),1);self.assertEqual(boundaries[0]['operation'],'queue.enter')
        self.assertNotIn('workflow_policy_hash',run)  # telemetry cannot manufacture a frozen deadline

    def test_ambiguous_write_keeps_intent_and_unresolved_outcome(self):
        class FailedPort(FakePort):
            def execute(self,op,payload,context):
                if op=='order.save':raise RuntimeError('simulated ambiguous transport outcome')
                return super().execute(op,payload,context)
        run=LinkedEncounterRunner(FakeFactory(),FailedPort(),FakeRules()).run_case(example_case(),run_id='clock-failure')
        self.assertEqual(run['status'],'failed')
        rows=[e for e in run['events'] if e.get('operation')=='order.save']
        intent=next(e for e in rows if e['event']=='business_request_intent')
        outcome=next(e for e in rows if e['event']=='business_request_outcome')
        self.assertEqual(intent['request_id'],outcome['request_id'])
        self.assertEqual(outcome['status'],'unresolved_exception');self.assertIsNone(outcome['native_effect'])

    def test_failed_queue_readback_cannot_establish_latency_origin(self):
        run=LinkedEncounterRunner(FakeFactory(),FakePort(failed_operation='queue.enter'),FakeRules()).run_case(example_case(),run_id='failed-queue')
        self.assertEqual(run['status'],'failed')
        self.assertFalse(any(e.get('clock_boundary')=='queue_entry_persisted' for e in run['events']))


if __name__=='__main__':unittest.main()
