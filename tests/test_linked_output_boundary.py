from copy import deepcopy
import unittest

from test_linked_clinical import FakeFactory, FakePort, FakeRules, example_case, new_rx
from sandbox.experiments.linked.clinical import LinkedEncounterRunner
from sandbox.experiments.linked.contracts import TaskCondition
from sandbox.experiments.visibility import assert_visible


class BoundaryFactory(FakeFactory):
    always_invalid = False
    decision = 'approve'

    def session(self, scope, role, model_family=None):
        session = super().session(scope, role, model_family)
        original = session.respond
        sent = False

        def respond(observation):
            nonlocal sent
            if role == 'rx_ai' and (not sent or self.always_invalid):
                sent = True
                self.observations.append((role, model_family, deepcopy(observation)))
                session.last_usage = {'prompt_tokens': 11, 'completion_tokens': 7}
                return {'action': 'review', 'decision': self.decision,
                        'flags': ['clinical monitoring concern', '['], 'reason': 'unchanged source facts'}
            return original(observation)

        session.respond = respond
        return session


class OutputBoundaryTests(unittest.TestCase):
    def run_review(self, factory):
        return LinkedEncounterRunner(factory, FakePort(), FakeRules()).run_task(
            example_case(), TaskCondition('prescription', 'parallel', 'glm'),
            run_id='boundary-fixture', stage_input={'prescription': new_rx(), 'clinical_context': {}})

    def test_completed_json_with_invalid_embedded_fragment_gets_one_same_input_correction(self):
        result = self.run_review(BoundaryFactory())
        self.assertEqual(result['status'], 'completed', result.get('failure_reason'))
        calls = [c for c in result['calls'] if c['role'] == 'rx_ai']
        self.assertEqual([c['status'] for c in calls], ['invalid_response_boundary', 'completed'])
        self.assertEqual(calls[0]['response']['flags'][-1], '[')
        corrected = deepcopy(calls[1]['observation'])
        feedback = corrected.pop('schema_correction')
        self.assertEqual(corrected, calls[0]['observation'])
        self.assertNotIn('previous_invalid_response', feedback)
        self.assertEqual(feedback['previous_invalid_response_hash'], calls[0]['response_hash'])
        self.assertEqual(calls[0]['original_clinical_input_hash'], calls[1]['original_clinical_input_hash'])
        final = result['artifacts']['original_prescription_review']['ai_review']
        self.assertNotIn('[', final['flags'])

    def test_output_boundary_retry_is_bounded(self):
        factory = BoundaryFactory(); factory.always_invalid = True
        result = self.run_review(factory)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(len([c for c in result['calls'] if c['role'] == 'rx_ai']), 2)
        self.assertNotIn('final_prescription', result['artifacts'])

    def test_clinical_defer_is_not_retried_to_seek_approval(self):
        factory = BoundaryFactory(); factory.decision = 'defer'
        result = self.run_review(factory)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(len([c for c in result['calls'] if c['role'] == 'rx_ai']), 1)

    def test_input_visibility_is_not_relaxed(self):
        for value in [{'flags': ['[']}, {'payload': '{"gold":true}'}, {'api_key': 'fixture'}]:
            with self.assertRaises(ValueError):
                assert_visible(value)


if __name__ == '__main__':
    unittest.main()
