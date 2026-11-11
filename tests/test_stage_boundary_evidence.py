from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from results.audit_stage_inputs import checked_calls
from sandbox.experiments.linked.actors import system_prompt
from sandbox.experiments.linked.contracts import digest
from sandbox.experiments.linked.json_boundary import POLICY


class StageBoundaryEvidenceTests(unittest.TestCase):
    def fixture(self):
        base = {'action': 'review_prescription', 'prescription': {'items': []}}
        bad = {'action': 'review', 'decision': 'approve', 'flags': ['[']}
        good = {'action': 'review', 'decision': 'approve', 'flags': []}
        feedback = {'original_clinical_input_hash': digest(base),
                    'previous_invalid_response_hash': digest(bad)}
        observations = [{**base, 'data_kind': 'synthetic_independent'},
                        {**base, 'data_kind': 'synthetic_independent', 'schema_correction': feedback}]
        calls, events, records = [], [], {}
        for n, (obs, response, status) in enumerate(zip(observations, [bad, good],
                ['invalid_response_boundary', 'completed']), 1):
            calls.append({'sequence': n, 'phase': 'prescription_review_0:ai', 'role': 'rx_ai',
                'observation': obs, 'observation_hash': digest(obs), 'model_name': 'fixture-tool',
                'model_family': 'glm', 'original_clinical_input_hash': digest(base),
                'status': status, 'response': response, 'response_parse': {'policy': POLICY},
                'response_boundary_error': 'opaque serialized visible input must be structured JSON'})
            events.append({'event': 'role_call', 'sequence': n, 'call_sequence': n,
                'role': 'rx_ai', 'phase': calls[-1]['phase'], 'observation_hash': digest(obs),
                'response_hash': digest(response)})
            records[n] = {'request_body': {'model': 'fixture-tool', 'messages': [
                {'role': 'system', 'content': system_prompt('rx_ai')},
                {'role': 'user', 'content': json.dumps(obs)}]},
                'response_body': {'choices': [{'finish_reason': 'stop',
                                               'message': {'content': json.dumps(response)}}]}}
        run = {'calls': calls, 'events': events, 'artifacts': {},
               'conditions': {'prescription': {'model_family': 'glm'}}}
        return run, records

    def check(self, run, records):
        with patch('results.audit_stage_inputs.matching_model_record',
                   side_effect=lambda run, call, archive: records[call['sequence']]):
            return checked_calls(run, None, {'role_model': 'fixture-role', 'tool_models': {'glm': 'fixture-tool'}})

    def test_actual_invalid_fragment_and_same_input_correction_are_bound(self):
        run, records = self.fixture()
        phases = self.check(run, records)
        self.assertEqual([row['status'] for row in phases['prescription_review_0:ai']],
                         ['invalid_response_boundary', 'completed'])

    def test_wrong_previous_response_hash_is_rejected(self):
        run, records = self.fixture()
        run['calls'][1]['observation']['schema_correction']['previous_invalid_response_hash'] = 'changed'
        with self.assertRaisesRegex(ValueError, 'preceding response'):
            self.check(run, records)

    def test_clean_response_cannot_be_marked_boundary_invalid(self):
        run, records = self.fixture()
        value = {'action': 'review', 'decision': 'approve', 'flags': []}
        run['calls'][0]['response'] = value
        run['events'][0]['response_hash'] = digest(value)
        records[1]['response_body']['choices'][0]['message']['content'] = json.dumps(value)
        with self.assertRaisesRegex(ValueError, 'passes the strict boundary'):
            self.check(run, records)


if __name__ == '__main__':
    unittest.main()
