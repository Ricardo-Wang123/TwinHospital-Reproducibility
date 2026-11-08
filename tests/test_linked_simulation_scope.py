"""Scope clarification cannot create evidence, approval or a paid encounter."""
from copy import deepcopy
import unittest

from test_linked_clinical import example_case, FakeFactory, FakePort, FakeRules
from sandbox.experiments.linked.clinical import LinkedEncounterRunner
from sandbox.experiments.linked.simulation_scope import POLICY


class SimulationScopeTests(unittest.TestCase):
    def test_roles_keep_visibility_and_source_values(self):
        case = example_case(); before = deepcopy(case)
        factory = FakeFactory()
        result = LinkedEncounterRunner(factory, FakePort(), FakeRules()).run_case(case, run_id='scope')
        self.assertEqual(case, before)
        self.assertEqual(result['simulation_scope_policy'], POLICY)
        for role, _, observation in factory.observations:
            if role == 'patient':
                self.assertNotIn('simulation_scope', observation)
                self.assertNotIn('hospital_history', observation)
            else:
                self.assertEqual(observation['simulation_scope'], POLICY)
                self.assertNotIn('patient_memory', observation)
            self.assertNotIn('DO_NOT_DISCLOSE_EVALUATOR', str(observation))
        self.assertFalse(result['native_complete'])  # A fake backend stays fake.

    def test_scope_cannot_turn_no_prescription_into_payment(self):
        class NoRxFactory(FakeFactory):
            def session(self, *args, **kwargs):
                underlying = super().session(*args, **kwargs)
                class Session:
                    last_usage = {}
                    def respond(self, observation):
                        if observation['action'] == 'consult':
                            return {'action': 'consultation_complete', 'assessment': 'Fixture unknown renal function',
                                'plan': 'Need unavailable information', 'prescription': {'items': []},
                                'no_prescription_reason': 'Cannot determine indication in this fixture'}
                        return underlying.respond(observation)
                return Session()
        port = FakePort()
        result = LinkedEncounterRunner(NoRxFactory(), port, FakeRules()).run_case(example_case(), run_id='no-rx')
        self.assertEqual(result['status'], 'incomplete_clinical_decision')
        self.assertFalse(any(op in {'order.attest', 'payment.commit'} for op, _, _ in port.calls))


if __name__ == '__main__':
    unittest.main()
